"""Hardware-free tests for the 14 mm / 15 V session (2026-09-24) and the raised safety limits."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock  # noqa: F401  (kept for future patches)
import unittest

import numpy as np

import acoustools_stereo_eventcam_3d_recording_auto as auto_record
import hf_identification_trajectory as hf
from stereo_acoustools_3d_recording_core import safe_run_dir_base
from test_thermal_hold_test import FakeClock, SOUND_ON_SEC, run_main, write_export

CAMPAIGN = Path("ff_heart_15v_20260923")
PLAN = CAMPAIGN / "ff_heart_15v_plan.json"
EXPORT = CAMPAIGN / "export_ff_heart_15v"
SOURCE = CAMPAIGN / "source"
DEFAULT_OUTPUT_ROOT = Path(
    r"C:\Users\digit\Documents\scripts\python\eventcam_control"
    r"\stereo_acoustools_3d_records_V15"
)
# The runs the analysis side matches on, and the marker each folder name must keep.
REQUIRED_MARKERS = {
    "cardioid_a5p4_f10_OFF_Ws4": ("cardioid_a5p4", "_Ws4"),
    "cardioid_a5p4_f10_C_Ws4": ("cardioid_a5p4", "_Ws4"),
    "cardioid_a5p4_f10_C_Ws": ("cardioid_a5p4", "_Ws"),
    "cardioid_a5p4_f7_C_Ws4": ("cardioid_a5p4", "_Ws4"),
    "heart_f10_OFF_Ws4": ("_Ws4",),
    "heart_f10_C_delay_Ws": ("_Ws",),
}


def plan_experiments() -> list[dict]:
    return json.loads(PLAN.read_text(encoding="utf-8"))["experiments"]


class FifteenVoltPlanTests(unittest.TestCase):
    def test_plan_covers_the_session_and_pins_the_supplied_commands(self) -> None:
        experiments = plan_experiments()
        names = [entry["name"] for entry in experiments]
        self.assertEqual(len(names), 22)
        self.assertEqual(names[:5], [
            "kcheck_x_S105_6jumps", "kcheck_y_S105_6jumps", "kcheck_z_S105_6jumps",
            "wscan_XZ_a", "wscan_XZ_b",
        ])
        imported = [entry for entry in experiments if entry["kind"] == "imported"]
        self.assertEqual(len(imported), 19)
        for entry in imported:
            source = CAMPAIGN / entry["source_npz"]
            self.assertTrue(source.is_file(), source)
            with np.load(source) as data:
                positions = np.ascontiguousarray(np.asarray(data["positions_mm"], dtype=np.float64))
            self.assertEqual(
                hashlib.sha256(positions.tobytes()).hexdigest(),
                entry["positions_sha256"],
                f"{entry['name']}: the pinned hash must match the supplied command",
            )

    def test_every_design_records_the_fifteen_volt_identification(self) -> None:
        for entry in plan_experiments():
            design = entry.get("feedforward_design", {})
            if design.get("design") in (None, "WSCAN"):
                continue
            model = design["identified_model"]
            self.assertEqual(model["supply_voltage_V"], 15.0)
            self.assertEqual(model["tau0_ms"], 0.9)
            self.assertEqual(model["k_1_per_ms2"], {"x": 0.208, "y": 0.198, "z": 2.48})
            self.assertIn(design["w_compensation"], {"none", "Ws4", "Ws"})

    def test_run_directories_keep_the_voltage_tag_and_the_analysis_markers(self) -> None:
        for entry in plan_experiments():
            index = [e["name"] for e in plan_experiments()].index(entry["name"])
            shape = (
                "step_response_identification"
                if entry["kind"] == "staircase"
                else "feedforward_validation"
            )
            label = f"{index:03d}_{entry['name']}_scale100_V15"
            stamp = "20260924_010203"
            base = safe_run_dir_base(DEFAULT_OUTPUT_ROOT, shape, label, stamp)
            self.assertEqual(base, f"{shape}_{label}_{stamp}", f"{entry['name']} would be shortened")
            for marker in REQUIRED_MARKERS.get(entry["name"], ()):
                self.assertIn(marker, base, entry["name"])
            if entry["name"].endswith("_Ws4"):
                self.assertIn("_Ws4_", base)

    @unittest.skipUnless(
        (EXPORT / "export_manifest.json").is_file()
        and json.loads((EXPORT / "export_manifest.json").read_text(encoding="utf-8")).get(
            "source_plan_sha256"
        )
        == hashlib.sha256(PLAN.read_bytes()).hexdigest(),
        "the export is older than the plan",
    )
    def test_export_is_bitwise_equal_to_the_supplied_commands(self) -> None:
        for entry in plan_experiments():
            if entry["kind"] != "imported":
                continue
            exported = np.load(EXPORT / entry["name"] / "command_trajectory.npz")
            with np.load(CAMPAIGN / entry["source_npz"]) as supplied:
                np.testing.assert_array_equal(exported["offset_mm"], supplied["positions_mm"])
                if "reference_mm" in supplied.files:
                    np.testing.assert_array_equal(exported["reference_mm"], supplied["reference_mm"])


class RaisedSafetyLimitTests(unittest.TestCase):
    def test_staircase_jumps_up_to_the_hard_cap_are_accepted(self) -> None:
        defaults = {
            "sample_hz": 1000,
            "duration_sec": 1.0,
            "ramp_sec": 0.0,
            "safety_limits": {
                "max_offset_mm": 3.4, "max_step_mm": 3.4,
                "max_speed_mm_s": 1.0, "max_acceleration_mm_s2": 1.0,
            },
        }
        base = {
            "name": "scaleup_step", "kind": "staircase", "axis": "x",
            "hold_sec": 0.01, "initial_hold_sec": 0.02, "directions": [1], "tier": "C",
        }
        self.assertAlmostEqual(hf.STAIRCASE_MAX_JUMP_MM, 3.2)
        self.assertLess(hf.ESCAPE_OFFSET_MM, hf.STAIRCASE_MAX_JUMP_MM)
        for amplitude in (2.5, 3.0, 3.2):
            offset, metadata = hf.generate_trajectory({**base, "amplitudes_mm": [amplitude]}, defaults)
            self.assertAlmostEqual(float(np.max(np.abs(offset[:, 0]))), amplitude)
            safety = metadata["generation_detail"]["safety"]
            self.assertTrue(safety["escape_boundary_exceeded"])
            self.assertAlmostEqual(safety["staircase_max_jump_limit_mm"], 3.2)
        with self.assertRaisesRegex(ValueError, "exceeds the hard cap"):
            hf.generate_trajectory({**base, "amplitudes_mm": [3.25]}, defaults)

    def test_imported_limits_are_plan_data_and_reject_above_them(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            samples, sample_hz = 10000, 10000.0
            t = np.arange(samples) / sample_hz
            positions = np.zeros((samples, 3))
            positions[:, 0] = 25.0 * np.sin(2 * np.pi * 1.0 * t) ** 3
            np.savez(
                root / "wide.npz", positions_mm=positions, time_pat_sec=t,
                sample_hz=np.array(sample_hz),
            )
            spec = {
                "name": "wide", "kind": "imported", "source_npz": "wide.npz",
                "positions_key": "positions_mm", "extra_time_keys": ["time_pat_sec"],
                "positions_sha256": hashlib.sha256(
                    np.ascontiguousarray(positions, dtype=np.float64).tobytes()
                ).hexdigest(),
                "duration_sec": samples / sample_hz,
                "_plan_dir": str(root),
            }
            defaults = {"sample_hz": sample_hz, "duration_sec": samples / sample_hz, "ramp_sec": 0.0}
            narrow = {**defaults, "safety_limits": {
                "max_offset_mm": 9.5, "max_speed_mm_s": 3500.0,
                "max_acceleration_mm_s2": 350000.0, "max_endpoint_offset_mm": 0.01,
            }}
            wide = {**defaults, "safety_limits": {
                "max_offset_mm": 31.0, "max_speed_mm_s": 3500.0,
                "max_acceleration_mm_s2": 350000.0, "max_endpoint_offset_mm": 0.01,
            }}
            with self.assertRaisesRegex(ValueError, "max_offset_mm"):
                hf.generate_trajectory(spec, narrow)
            offset, _metadata = hf.generate_trajectory(spec, wide)
            self.assertAlmostEqual(float(np.max(np.abs(offset[:, 0]))), 25.0, places=6)


class UnevenScheduleTests(unittest.TestCase):
    def test_offsets_are_parsed_per_run(self) -> None:
        self.assertEqual(
            auto_record.parse_schedule_offsets("0,,300,,600", 5), [0.0, None, 300.0, None, 600.0]
        )
        for bad, message in (
            ("0,300", "one value per run"),
            ("0,,300,,200", "not go backwards"),
            ("0,,-5,,600", "finite"),
        ):
            with self.subTest(case=bad), self.assertRaisesRegex(ValueError, message):
                auto_record.parse_schedule_offsets(bad, 5)

    def test_runs_wait_for_their_own_minute(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = write_export(root)
            argv = ["--hf-export-dir", str(export_dir), "--output-dir", str(root / "rec")]
            for name in ("kx", "ky", "kz", "kx"):
                argv += ["--hf-run", f"{name}=1"]
            argv += [
                "--acknowledge-step-response-risk",
                "--unattended-after-first-checkpoint",
                "--supply-voltage-v", "15",
                # 0 min, straight after, 40 min, straight after
                "--schedule-offsets-sec", "0,,2400,",
            ]
            clock = FakeClock(SOUND_ON_SEC + 60.0)
            result, events, starts = run_main(argv, clock=clock, run_seconds=50.0)
            self.assertEqual(result, 0)
            self.assertEqual([s - SOUND_ON_SEC for s in starts], [60, 110, 2400, 2450])
            metadata = [call.kwargs["automation_metadata"] for call in events.record.call_args_list]
            self.assertEqual(metadata[2]["scheduled_start_offset_sec"], 2400.0)
            self.assertIsNone(metadata[1]["scheduled_start_offset_sec"])
            session = json.loads(
                next((root / "rec").glob("auto_recording_session_*.json")).read_text(encoding="utf-8")
            )
            self.assertEqual(session["schedule"]["start_offsets_sec"], [0.0, None, 2400.0, None])

    def test_offsets_require_unattended_and_exclude_the_interval_form(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = write_export(root)
            base = [
                "--hf-export-dir", str(export_dir), "--output-dir", str(root / "rec"),
                "--hf-run", "kx=1", "--hf-run", "ky=1",
                "--acknowledge-step-response-risk",
            ]
            rejected = {
                "without unattended": base + ["--schedule-offsets-sec", "0,"],
                "with the interval form": base + [
                    "--unattended-after-first-checkpoint",
                    "--schedule-offsets-sec", "0,",
                    "--schedule-interval-sec", "300",
                ],
            }
            for name, argv in rejected.items():
                with self.subTest(case=name):
                    result, events, _starts = run_main(argv)
                    self.assertEqual(result, 2)
                    events.open_hw.assert_not_called()


if __name__ == "__main__":
    unittest.main()
