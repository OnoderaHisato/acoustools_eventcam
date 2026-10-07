"""Hardware-free tests for the 2026-10-06 cooling remeasurement plan and the operator-action record."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
import unittest

import numpy as np

from stereo_acoustools_3d_recording_core import safe_run_dir_base
from test_thermal_hold_test import run_main, write_export

CAMPAIGN = Path("cooling_20261006")
PLAN = CAMPAIGN / "cooling_plan.json"
EXPORT = CAMPAIGN / "export_cooling"
OUTPUT_ROOT = Path(r"C:\Users\digit\Documents\scripts\python\eventcam_control")
SWEEPS = {"chirp_x_a0.05_40-110Hz": 0.05, "chirp_x_a0.1_40-110Hz": 0.1, "chirp_x_a0.2_40-110Hz": 0.2,
          "chirp_y_a0.05_40-110Hz": 0.05, "chirp_y_a0.1_40-110Hz": 0.1, "chirp_y_a0.2_40-110Hz": 0.2,
          "chirp_z_a0.01_200-300Hz": 0.01, "chirp_z_a0.02_200-300Hz": 0.02}


def plan() -> dict:
    return json.loads(PLAN.read_text(encoding="utf-8"))


def export_matches_plan() -> bool:
    manifest = EXPORT / "export_manifest.json"
    return manifest.is_file() and json.loads(manifest.read_text(encoding="utf-8")).get(
        "source_plan_sha256") == hashlib.sha256(PLAN.read_bytes()).hexdigest()


class CoolingPlanTests(unittest.TestCase):
    def test_limits_are_the_scale_up_values(self) -> None:
        limits = plan()["defaults"]["safety_limits"]
        self.assertEqual((limits["max_offset_mm"], limits["max_speed_mm_s"], limits["max_acceleration_mm_s2"],
                          limits["max_command_reference_distance_mm"], limits["max_endpoint_offset_mm"]),
                         (35.0, 3500.0, 350000.0, 4.5, 0.01))

    def test_sources_are_pinned(self) -> None:
        for entry in plan()["experiments"]:
            if entry["kind"] != "imported":
                continue
            with np.load(CAMPAIGN / entry["source_npz"]) as data:
                positions = np.ascontiguousarray(np.asarray(data["positions_mm"], dtype=np.float64))
            self.assertEqual(hashlib.sha256(positions.tobytes()).hexdigest(), entry["positions_sha256"], entry["name"])

    def test_sweeps_are_16p5_s_single_axis_and_uncompensated(self) -> None:
        entries = {e["name"]: e for e in plan()["experiments"]}
        for name, amplitude in SWEEPS.items():
            with np.load(CAMPAIGN / entries[name]["source_npz"]) as data:
                u, r = np.asarray(data["positions_mm"]), np.asarray(data["reference_mm"])
            self.assertEqual(entries[name]["duration_sec"], 16.5, name)
            self.assertAlmostEqual(float(np.abs(u).max()), amplitude, places=6)
            self.assertTrue(np.array_equal(u, r), name)
            moving = [axis for axis in range(3) if np.ptp(u[:, axis]) > 0]
            self.assertEqual(moving, ["xyz".index(name[6])], name)
        self.assertEqual(entries["multisine_xyz_5-150Hz_8s"]["duration_sec"], 8.0)

    def test_run_directories_are_not_shortened(self) -> None:
        for root in ("stereo_acoustools_3d_records_V15c", "stereo_acoustools_3d_records_V18c"):
            for index, entry in enumerate(plan()["experiments"]):
                shape = "step_response_identification" if entry["kind"] == "staircase" else "feedforward_validation"
                label = f"{index:03d}_{entry['name']}_scale100_V15"
                base = safe_run_dir_base(OUTPUT_ROOT / root, shape, label, "20261006_010203")
                self.assertEqual(base, f"{shape}_{label}_20261006_010203", entry["name"])

    def test_ws4_designs_are_the_sigma_0p3_remake(self) -> None:
        ws4 = [e for e in plan()["experiments"] if e["name"].endswith("_Ws4")]
        self.assertEqual(len(ws4), 6)
        for entry in ws4:
            design = entry["feedforward_design"]
            self.assertEqual(design["design_dir"], "ff_heart_15V_sig03", entry["name"])
            self.assertTrue(entry["source_npz"].startswith("source/sig03/"), entry["name"])
            self.assertEqual(design["params"]["w_compensation"]["smooth_sigma_mm"], 0.3, entry["name"])
        scans = {e["name"]: e["feedforward_design"]["params"]["w_compensation"]["scan_a"] for e in ws4}
        self.assertIn("20260924_062939", scans["heart_s7_f10_C_Ws4"])
        self.assertIn("20260925_110055", scans["heart_s7_xp15_f10_C_Ws4"])

    def test_slow_scans_are_48_s_and_uncompensated(self) -> None:
        entries = {e["name"]: e for e in plan()["experiments"]}
        fast = entries["wscan_XZ_R28_a"]
        for variant in "ab":
            entry = entries[f"wscan_XZ_R28slow_{variant}"]
            self.assertEqual(entry["duration_sec"], 48.0)
            self.assertTrue(entry["source_npz"].startswith("source/w_scan_slow/"))
            self.assertEqual(entry["feedforward_design"]["design"], "WSCAN")
            self.assertEqual(entry["feedforward_design"]["rotation_hz"], 0.75)
            with np.load(CAMPAIGN / entry["source_npz"]) as data:
                u, r = np.asarray(data["positions_mm"]), np.asarray(data["reference_mm"])
            self.assertTrue(np.array_equal(u, r))
            self.assertAlmostEqual(float(np.linalg.norm(u, axis=1).max()), 28.0, places=3)
            self.assertLess(float(np.linalg.norm(u[[0, -1]], axis=1).max()), 1e-9)
            self.assertEqual(float(np.ptp(u[:, 1])), 0.0)
        # The slow scans are appended, so the earlier export indices did not move.
        names = [e["name"] for e in plan()["experiments"]]
        self.assertEqual(names[-2:], ["wscan_XZ_R28slow_a", "wscan_XZ_R28slow_b"])
        self.assertLess(names.index(fast["name"]), 56)

    @unittest.skipUnless(export_matches_plan(), "the export is older than the plan")
    def test_export_has_every_run(self) -> None:
        manifest = json.loads((EXPORT / "export_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["runs"]), len(plan()["experiments"]))


class OperatorActionTests(unittest.TestCase):
    def test_actions_are_asked_and_their_times_recorded(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = write_export(root)
            argv = ["--hf-export-dir", str(export_dir), "--output-dir", str(root / "rec")]
            for name in ("kx", "ky", "kz", "kx"):
                argv += ["--hf-run", f"{name}=1"]
            argv += ["--acknowledge-step-response-risk", "--unattended-after-first-checkpoint",
                     "--operator-actions", "2:fans ON;4:fans OFF"]
            with mock.patch("builtins.input", return_value="") as asked:
                result, events, _starts = run_main(argv)
            self.assertEqual(result, 0)
            self.assertEqual(asked.call_count, 2)
            stops = [call.kwargs["prompt_before_capture"] for call in events.record.call_args_list]
            self.assertEqual(stops, [True, True, False, True])
            session = json.loads(next((root / "rec").glob("auto_recording_session_*.json")).read_text(encoding="utf-8"))
            actions = session["operator_actions"]
            self.assertEqual([(a["before_run_number"], a["action"]) for a in actions], [(2, "fans ON"), (4, "fans OFF")])
            self.assertTrue(all("confirmed_at" in a for a in actions))

    def test_invalid_actions_never_open_hardware(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = write_export(root)
            base = ["--hf-export-dir", str(export_dir), "--output-dir", str(root / "rec"),
                    "--hf-run", "kx=1", "--hf-run", "ky=1", "--acknowledge-step-response-risk"]
            for name, extra in {
                "without unattended": ["--operator-actions", "2:fans ON"],
                "no action text": ["--unattended-after-first-checkpoint", "--operator-actions", "2:"],
                "out of range": ["--unattended-after-first-checkpoint", "--operator-actions", "3:fans ON"],
            }.items():
                with self.subTest(case=name):
                    result, events, _starts = run_main(base + extra)
                    self.assertEqual(result, 2)
                    events.open_hw.assert_not_called()


if __name__ == "__main__":
    unittest.main()
