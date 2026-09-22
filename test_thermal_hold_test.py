"""Hardware-free tests for the reduced-voltage thermal hold test (THERMAL_PLAN_20260922.md, 2026-09-23).

Covers the voltage tag on run directories, the sound-on anchored schedule (kcheck groups every
5 minutes), the option checks, and thermal_log_prefill.py.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock
import unittest

import acoustools_stereo_eventcam_3d_recording_auto as auto_record
import hf_identification_trajectory as hf
import thermal_log_prefill

SOUND_ON_SEC = 1_000_000.0


class FakeClock:
    """Wall clock that only moves when the code sleeps or a run takes time."""

    def __init__(self, start: float) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


def write_export(root: Path) -> Path:
    def spec(name: str, axis: str) -> dict:
        return {
            "name": name,
            "kind": "staircase",
            "tier": "C",
            "axis": axis,
            "hold_sec": 0.01,
            "initial_hold_sec": 0.02,
            "final_hold_sec": 0.02,
            "amplitudes_mm": [1.05],
            "directions": [1, -1],
            "repeats_within_run": 1,
            "enabled": True,
        }

    plan = {
        "schema_version": 1,
        "defaults": {
            "sample_hz": 10000,
            "duration_sec": 0.1,
            "ramp_sec": 0.0,
            "safety_limits": {
                "max_offset_mm": 1.1,
                "max_step_mm": 1.1,
                "max_speed_mm_s": 100.0,
                "max_acceleration_mm_s2": 28000.0,
            },
        },
        "experiments": [spec("kx", "x"), spec("ky", "y"), spec("kz", "z")],
    }
    (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    with mock.patch.object(
        hf, "write_preview", side_effect=lambda path, *_a, **_k: path.write_bytes(b"p")
    ):
        hf.export_plan(root / "plan.json", root / "export", False, False)
    return root / "export"


def run_main(argv: list[str], clock: FakeClock | None = None, run_seconds: float = 50.0):
    events = mock.Mock()
    session = SimpleNamespace(
        current_pos=(0.0, 0.0, 0.0),
        active_output_started_wall_ns=int(SOUND_ON_SEC * 1e9),
    )
    events.open_hw.return_value = session
    events.precompute.return_value = SimpleNamespace(name="first-run-playback")
    starts: list[float] = []

    def record(*_args, **_kwargs):
        if clock is not None:
            starts.append(clock.now)
            clock.now += run_seconds
        return 0, None

    events.record.side_effect = record
    patches = [
        mock.patch.object(auto_record, "open_recording_hardware_session", events.open_hw),
        mock.patch.object(auto_record, "shutdown_recording_hardware_session", events.close_hw),
        mock.patch.object(auto_record, "precompute_hologram_playback", events.precompute),
        mock.patch.object(auto_record, "run_recording", events.record),
    ]
    if clock is not None:
        patches.append(mock.patch.object(auto_record, "time", clock))
    for patch in patches:
        patch.start()
    try:
        result = auto_record.main(argv)
    finally:
        for patch in reversed(patches):
            patch.stop()
    return result, events, starts


class HelperTests(unittest.TestCase):
    def test_voltage_tag(self) -> None:
        self.assertEqual(auto_record.voltage_tag(15), "V15")
        self.assertEqual(auto_record.voltage_tag(12.0), "V12")
        self.assertEqual(auto_record.voltage_tag(13.5), "V13p5")

    def test_only_the_first_run_of_later_groups_waits(self) -> None:
        offsets = [auto_record.scheduled_group_offset_sec(i, 3, 300.0) for i in range(8)]
        self.assertEqual(offsets, [None, None, None, 300.0, None, None, 600.0, None])


class ScheduledSessionTests(unittest.TestCase):
    def test_groups_start_on_the_sound_on_clock_and_runs_are_tagged(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = write_export(root)
            output_root = root / "rec"
            argv = ["--hf-export-dir", str(export_dir), "--output-dir", str(output_root)]
            for _group in range(3):
                for name in ("kx", "ky", "kz"):
                    argv += ["--hf-run", f"{name}=1"]
            argv += [
                "--acknowledge-step-response-risk",
                "--unattended-after-first-checkpoint",
                "--prompt-on-capture-failure",
                "--supply-voltage-v", "15",
                "--schedule-interval-sec", "300",
                "--schedule-group-size", "3",
            ]
            # The operator takes 2 minutes to confirm the first run.
            clock = FakeClock(SOUND_ON_SEC + 120.0)
            result, events, starts = run_main(argv, clock=clock, run_seconds=50.0)
            self.assertEqual(result, 0)
            offsets = [start - SOUND_ON_SEC for start in starts]
            # Group 0 right after the checkpoint, groups 1 and 2 at exactly 5 and 10 minutes.
            self.assertEqual(offsets, [120, 170, 220, 300, 350, 400, 600, 650, 700])

            calls = events.record.call_args_list
            self.assertEqual(len(calls), 9)
            for index, call in enumerate(calls):
                trajectory = call.kwargs["prepared_trajectory"]
                self.assertTrue(trajectory.run_label.endswith("_V15"), trajectory.run_label)
                metadata = call.kwargs["automation_metadata"]
                self.assertEqual(metadata["supply_voltage_V"], 15.0)
                self.assertEqual(metadata["run_label_tag"], "V15")
                self.assertEqual(metadata["schedule_group"], index // 3)
                self.assertEqual(metadata["scheduled_group_offset_sec"], (index // 3) * 300.0)
                self.assertAlmostEqual(
                    metadata["seconds_since_sound_on_at_run_start"], offsets[index], places=1
                )
            # Only the first run keeps the preview and Enter.
            self.assertTrue(calls[0].kwargs["prompt_before_capture"])
            self.assertFalse(any(c.kwargs["prompt_before_capture"] for c in calls[1:]))

            session = json.loads(
                next(output_root.glob("auto_recording_session_*.json")).read_text(encoding="utf-8")
            )
            self.assertEqual(session["status"], "complete")
            self.assertEqual(session["supply_voltage_V"], 15.0)
            self.assertEqual(session["run_label_tag"], "V15")
            self.assertEqual(session["schedule"]["interval_sec"], 300.0)
            self.assertEqual(session["schedule"]["group_size"], 3)

    def test_a_late_group_starts_immediately(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = write_export(root)
            argv = [
                "--hf-export-dir", str(export_dir), "--output-dir", str(root / "rec"),
                "--hf-run", "kx=1", "--hf-run", "ky=1",
                "--acknowledge-step-response-risk",
                "--unattended-after-first-checkpoint",
                "--schedule-interval-sec", "60",
            ]
            clock = FakeClock(SOUND_ON_SEC)
            result, _events, starts = run_main(argv, clock=clock, run_seconds=90.0)
            self.assertEqual(result, 0)
            # The first run overran the 60 s slot, so the second starts without waiting.
            self.assertEqual([s - SOUND_ON_SEC for s in starts], [0, 90])

    def test_invalid_options_never_open_hardware(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = write_export(root)
            base = [
                "--hf-export-dir", str(export_dir), "--output-dir", str(root / "rec"),
                "--acknowledge-step-response-risk",
            ]
            rejected = {
                "schedule without unattended": base + ["--schedule-interval-sec", "300"],
                "group size without interval": base
                + ["--unattended-after-first-checkpoint", "--schedule-group-size", "3"],
                "non-positive interval": base
                + ["--unattended-after-first-checkpoint", "--schedule-interval-sec", "0"],
                "voltage out of range": base + ["--supply-voltage-v", "0"],
            }
            for name, argv in rejected.items():
                with self.subTest(case=name):
                    result, events, _starts = run_main(argv)
                    self.assertEqual(result, 2)
                    events.open_hw.assert_not_called()

    def test_a_voltage_tag_that_would_be_cut_is_refused(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            export_dir = write_export(root)
            # Leave room for the untagged directory name but not for "_V15".
            # descriptor limit = 248 - len(output root) - 2 - 69 - 4 - 16; the untagged
            # descriptor "step_response_identification_000_kx_scale100" has 44 characters.
            target_root_length = 248 - 2 - 69 - 4 - 16 - 46
            filler = "r" * (target_root_length - len(str(root)) - 1)
            output_root = root / filler
            base = [
                "--hf-export-dir", str(export_dir), "--output-dir", str(output_root),
                "--hf-run", "kx=1", "--dry-run",
            ]
            result, _events, _starts = run_main(base)
            self.assertEqual(result, 0)
            result, events, _starts = run_main(base + ["--supply-voltage-v", "15"])
            self.assertEqual(result, 2)
            events.open_hw.assert_not_called()


class ThermalLogPrefillTests(unittest.TestCase):
    def test_rows_are_prefilled_and_never_overwritten(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            sound_on_ns = int(1_790_000_000 * 1e9)
            session = {
                "active_output_started_wall_ns": sound_on_ns,
                "supply_voltage_V": 15.0,
                "runs": [
                    {
                        "exit_code": 0,
                        "run_dir": str(root / "step_response_identification_000_kcheck_x_S105_6jumps_scale100_V15_20260923_200500"),
                        "schedule_group": 1,
                    },
                    {"exit_code": 1, "run_dir": "", "error": "failed"},
                ],
            }
            path = root / "auto_recording_session_20260923_195800.json"
            path.write_text(json.dumps(session), encoding="utf-8")
            self.assertEqual(thermal_log_prefill.main([str(root)]), 0)
            output = root / "thermal_log_20260923_195800.csv"
            with output.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(list(row), thermal_log_prefill.COLUMNS)
            self.assertEqual(row["record_start"], "2026-09-23 20:05:00")
            self.assertTrue(row["run_name"].endswith("_V15_20260923_200500"))
            self.assertEqual(row["supply_V"], "15")
            self.assertEqual(row["supply_A"], "")
            self.assertIn("group 1", row["note"])
            with self.assertRaises(SystemExit):
                thermal_log_prefill.main([str(path)])


if __name__ == "__main__":
    unittest.main()
