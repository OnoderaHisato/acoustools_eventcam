"""Hardware-free tests for the 2026-09-24 scale-up campaign (26 / 44 / 60 mm cardioids,
the 28 mm w scan and the 2.5 / 3.0 mm horizontal steps) and the extra operator checkpoints."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np

import acoustools_stereo_eventcam_3d_recording_auto as auto_record
from stereo_acoustools_3d_recording_core import safe_run_dir_base
from test_thermal_hold_test import run_main, write_export

CAMPAIGN = Path("scaleup_20260924")
PLAN = CAMPAIGN / "scaleup_plan.json"
EXPORT = CAMPAIGN / "export_scaleup"
SUPPLIED_STEPS = Path("large_step_20260924/export_large_step_25_30")
DEFAULT_OUTPUT_ROOT = Path(
    r"C:\Users\digit\Documents\scripts\python\eventcam_control"
    r"\stereo_acoustools_3d_records_V15"
)
EXPECTED_ORDER = [
    "kcheck_x_S105_6jumps", "kcheck_y_S105_6jumps", "kcheck_z_S105_6jumps",
    "vzrstep_XL25", "vzrstep_XL30", "wscan_XZ_R28_a", "wscan_XZ_R28_b",
] + [
    f"cardioid_{size}_f10_{design}"
    for size in ("a10", "a17", "a23")
    for design in ("OFF", "A_delay", "C_delay", "C_nl", "OT_ident")
]


def plan() -> dict:
    return json.loads(PLAN.read_text(encoding="utf-8"))


def export_matches_plan() -> bool:
    manifest = EXPORT / "export_manifest.json"
    return manifest.is_file() and json.loads(manifest.read_text(encoding="utf-8")).get(
        "source_plan_sha256"
    ) == hashlib.sha256(PLAN.read_bytes()).hexdigest()


class ScaleUpPlanTests(unittest.TestCase):
    def test_plan_order_and_raised_limits(self) -> None:
        document = plan()
        self.assertEqual([entry["name"] for entry in document["experiments"]], EXPECTED_ORDER)
        self.assertEqual(document["defaults"]["safety_limits"], {
            "max_offset_mm": 35.0,
            "max_speed_mm_s": 3500.0,
            "max_acceleration_mm_s2": 350000.0,
            "max_endpoint_offset_mm": 0.01,
            "max_command_reference_distance_mm": 4.0,
        })

    def test_imported_commands_are_pinned_to_the_supplied_files(self) -> None:
        for entry in plan()["experiments"]:
            if entry["kind"] != "imported":
                continue
            source = CAMPAIGN / entry["source_npz"]
            self.assertTrue(source.is_file(), source)
            with np.load(source) as data:
                positions = np.ascontiguousarray(np.asarray(data["positions_mm"], dtype=np.float64))
            self.assertEqual(hashlib.sha256(positions.tobytes()).hexdigest(), entry["positions_sha256"])

    def test_horizontal_steps_say_the_vertical_criterion_does_not_apply(self) -> None:
        steps = [e for e in plan()["experiments"] if e["name"].startswith("vzrstep_")]
        self.assertEqual([e["name"] for e in steps], ["vzrstep_XL25", "vzrstep_XL30"])
        for entry in steps:
            self.assertIn("vertical lambda/4 criterion does not apply", entry["note"])
            self.assertAlmostEqual(entry["amplitudes_mm"][0] + 0.05, entry["safety_limits"]["max_step_mm"])

    def test_run_directories_keep_the_markers_the_analysis_side_matches(self) -> None:
        names = [entry["name"] for entry in plan()["experiments"]]
        for index, entry in enumerate(plan()["experiments"]):
            shape = (
                "step_response_identification" if entry["kind"] == "staircase"
                else "feedforward_validation"
            )
            label = f"{index:03d}_{entry['name']}_scale100_V15"
            base = safe_run_dir_base(DEFAULT_OUTPUT_ROOT, shape, label, "20260924_010203")
            self.assertEqual(base, f"{shape}_{label}_20260924_010203", entry["name"])
            if entry["name"].startswith("cardioid_"):
                size = entry["name"].split("_")[1]
                for marker in (f"cardioid_{size}", "_f10_", "_V15_"):
                    self.assertIn(marker, base, entry["name"])
        self.assertEqual(len(names), len(set(zip(names, range(len(names))))))

    @unittest.skipUnless(export_matches_plan(), "the export is older than the plan")
    def test_export_reproduces_the_supplied_commands_bitwise(self) -> None:
        for entry in plan()["experiments"]:
            exported = np.load(EXPORT / entry["name"] / "command_trajectory.npz")["offset_mm"]
            if entry["kind"] == "imported":
                with np.load(CAMPAIGN / entry["source_npz"]) as supplied:
                    np.testing.assert_array_equal(exported, supplied["positions_mm"])
            elif entry["name"].startswith("vzrstep_"):
                supplied = np.load(SUPPLIED_STEPS / entry["name"] / "command_trajectory.npz")
                np.testing.assert_array_equal(exported, supplied["offset_mm"])

    @unittest.skipUnless(export_matches_plan(), "the export is older than the plan")
    def test_the_largest_cardioid_is_inside_the_raised_limits(self) -> None:
        offset = np.load(EXPORT / "cardioid_a23_f10_OFF" / "command_trajectory.npz")["offset_mm"]
        # Inside the |x|, |z| <= 30 mm camera box, but 30.7 mm away from the centre diagonally,
        # which is why max_offset_mm had to go past 30.
        self.assertLess(float(np.max(np.abs(offset[:, 0]))), 30.0)
        self.assertLess(float(np.max(np.abs(offset[:, 2]))), 30.0)
        self.assertGreater(float(np.max(np.linalg.norm(offset, axis=1))), 30.0)
        self.assertLess(float(np.max(np.linalg.norm(offset, axis=1))), 35.0)


class ExtraCheckpointTests(unittest.TestCase):
    def test_named_runs_stop_for_the_operator(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = write_export(root)
            argv = ["--hf-export-dir", str(export_dir), "--output-dir", str(root / "rec")]
            for name in ("kx", "ky", "kz", "kx"):
                argv += ["--hf-run", f"{name}=1"]
            argv += [
                "--acknowledge-step-response-risk",
                "--unattended-after-first-checkpoint",
                "--checkpoint-run-numbers", "3",
            ]
            result, events, _starts = run_main(argv)
            self.assertEqual(result, 0)
            stops = [call.kwargs["prompt_before_capture"] for call in events.record.call_args_list]
            self.assertEqual(stops, [True, False, True, False])
            previews = [call.args[0].no_preview for call in events.record.call_args_list]
            self.assertEqual(previews, [False, True, False, True])

    def test_invalid_checkpoint_numbers_never_open_hardware(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = write_export(root)
            base = [
                "--hf-export-dir", str(export_dir), "--output-dir", str(root / "rec"),
                "--hf-run", "kx=1", "--hf-run", "ky=1",
                "--acknowledge-step-response-risk",
            ]
            rejected = {
                "without unattended": base + ["--checkpoint-run-numbers", "2"],
                "out of range": base + [
                    "--unattended-after-first-checkpoint", "--checkpoint-run-numbers", "5",
                ],
            }
            for name, argv in rejected.items():
                with self.subTest(case=name):
                    result, events, _starts = run_main(argv)
                    self.assertEqual(result, 2)
                    events.open_hw.assert_not_called()


if __name__ == "__main__":
    unittest.main()
