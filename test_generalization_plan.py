"""Hardware-free tests for the 2026-09-27 generalization session plan and the particle-change record."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
import unittest

import numpy as np

import acoustools_stereo_eventcam_3d_recording_auto as auto_record
from stereo_acoustools_3d_recording_core import safe_run_dir_base
from test_thermal_hold_test import run_main, write_export

CAMPAIGN = Path("generalization_20260927")
PLAN = CAMPAIGN / "generalization_plan.json"
EXPORT = CAMPAIGN / "export_generalization"
DEFAULT_OUTPUT_ROOT = Path(
    r"C:\Users\digit\Documents\scripts\python\eventcam_control"
    r"\stereo_acoustools_3d_records_V15g"
)
SHAPES = ("heart_s7", "cardioid_a10", "lissajous_s15", "circle_s20")


def plan() -> dict:
    return json.loads(PLAN.read_text(encoding="utf-8"))


def export_matches_plan() -> bool:
    manifest = EXPORT / "export_manifest.json"
    return manifest.is_file() and json.loads(manifest.read_text(encoding="utf-8")).get(
        "source_plan_sha256"
    ) == hashlib.sha256(PLAN.read_bytes()).hexdigest()


class GeneralizationPlanTests(unittest.TestCase):
    def test_every_command_of_the_readme_is_there(self) -> None:
        names = {entry["name"] for entry in plan()["experiments"]}
        wanted = {"kcheck_x_S105_6jumps", "kcheck_y_S105_6jumps", "kcheck_z_S105_6jumps", "vzrstep_XL30",
                  "wscan_XZ_R28_a", "wscan_XZ_R28_b", "hold_center_10s"}
        wanted |= {f"{s}_f10_{d}" for s in SHAPES for d in ("OFF", "C_delay", "C_Ws4")}
        wanted |= {f"heart_s7_{o}_f10_C_Ws4" for o in ("xp15", "xm15", "zp15", "zm15")}
        wanted |= {f"heart_s7_{o}_f10_C_{w}" for o in ("yp10", "ym10") for w in ("W3", "W3f")}
        wanted |= {f"{s}_f10_{d}" for s in ("circle_tilt30_s13", "lissajous3d_s13") for d in ("OFF", "C_W3", "C_W3f")}
        self.assertEqual(names, wanted)

    def test_limits_are_the_scale_up_values_with_4p5_mm(self) -> None:
        limits = plan()["defaults"]["safety_limits"]
        self.assertEqual(limits["max_offset_mm"], 35.0)
        self.assertEqual(limits["max_speed_mm_s"], 3500.0)
        self.assertEqual(limits["max_acceleration_mm_s2"], 350000.0)
        self.assertEqual(limits["max_command_reference_distance_mm"], 4.5)

    def test_sources_are_pinned(self) -> None:
        for entry in plan()["experiments"]:
            if entry["kind"] != "imported":
                continue
            with np.load(CAMPAIGN / entry["source_npz"]) as data:
                positions = np.ascontiguousarray(np.asarray(data["positions_mm"], dtype=np.float64))
            self.assertEqual(hashlib.sha256(positions.tobytes()).hexdigest(), entry["positions_sha256"], entry["name"])

    def test_run_directories_are_not_shortened(self) -> None:
        for index, entry in enumerate(plan()["experiments"]):
            shape = "step_response_identification" if entry["kind"] == "staircase" else "feedforward_validation"
            label = f"{index:03d}_{entry['name']}_scale100_V15"
            base = safe_run_dir_base(DEFAULT_OUTPUT_ROOT, shape, label, "20260928_010203")
            self.assertEqual(base, f"{shape}_{label}_20260928_010203", entry["name"])

    def test_three_dimensional_commands_move_y(self) -> None:
        for entry in plan()["experiments"]:
            name = entry["name"]
            if not ("tilt30" in name or "lissajous3d" in name or "_yp10_" in name or "_ym10_" in name):
                continue
            with np.load(CAMPAIGN / entry["source_npz"]) as data:
                y = np.asarray(data["positions_mm"])[:, 1]
            self.assertGreater(float(np.ptp(y)), 9.0, name)

    @unittest.skipUnless(export_matches_plan(), "the export is older than the plan")
    def test_export_loads_for_the_hardware_path(self) -> None:
        from stereo_acoustools_3d_hf_common import load_hf_export_runs, prepare_hf_trajectory
        _manifest, runs = load_hf_export_runs(EXPORT)
        by_name = {run.name: run for run in runs}
        for name in ("circle_tilt30_s13_f10_C_W3", "lissajous3d_s13_f10_OFF", "heart_s7_yp10_f10_C_W3", "circle_s20_f10_C_Ws4"):
            trajectory = prepare_hf_trajectory(by_name[name], center_m=(0.0, 0.0, 0.0), command_scale=1.0)
            positions = np.asarray(trajectory.positions, dtype=float)
            self.assertEqual(positions.shape[1], 3, name)
            if "tilt30" in name or "3d" in name or "_yp10_" in name:
                self.assertGreater(float(np.ptp(positions[:, 1])), 0.009, name)   # metres: y really moves


class ParticleChangeTests(unittest.TestCase):
    def test_the_change_is_asked_before_the_run_and_its_time_recorded(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = write_export(root)
            argv = ["--hf-export-dir", str(export_dir), "--output-dir", str(root / "rec")]
            for name in ("kx", "ky", "kz", "kx"):
                argv += ["--hf-run", f"{name}=1"]
            argv += ["--acknowledge-step-response-risk", "--unattended-after-first-checkpoint",
                     "--particle-change-run-numbers", "3"]
            with mock.patch("builtins.input", return_value="") as asked:
                result, events, _starts = run_main(argv)
            self.assertEqual(result, 0)
            self.assertEqual(asked.call_count, 1)
            stops = [call.kwargs["prompt_before_capture"] for call in events.record.call_args_list]
            self.assertEqual(stops, [True, False, True, False])      # the change also gets the preview
            session = json.loads(next((root / "rec").glob("auto_recording_session_*.json")).read_text(encoding="utf-8"))
            changes = session["particle_changes"]
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0]["before_run_number"], 3)
            self.assertEqual(changes[0]["before_label"], "kz")
            self.assertIn("confirmed_at", changes[0])

    def test_invalid_change_numbers_never_open_hardware(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = write_export(root)
            base = ["--hf-export-dir", str(export_dir), "--output-dir", str(root / "rec"),
                    "--hf-run", "kx=1", "--hf-run", "ky=1", "--acknowledge-step-response-risk"]
            for name, extra in {
                "without unattended": ["--particle-change-run-numbers", "2"],
                "first run": ["--unattended-after-first-checkpoint", "--particle-change-run-numbers", "1"],
                "out of range": ["--unattended-after-first-checkpoint", "--particle-change-run-numbers", "3"],
            }.items():
                with self.subTest(case=name):
                    result, events, _starts = run_main(base + extra)
                    self.assertEqual(result, 2)
                    events.open_hw.assert_not_called()


if __name__ == "__main__":
    unittest.main()
