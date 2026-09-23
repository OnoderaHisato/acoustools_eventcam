"""Hardware-free tests for linking the supply log to runs (psu_run_link.py, psu_log_report.py,
and --psu-log in the auto entry)."""

from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock
import unittest

import acoustools_stereo_eventcam_3d_recording_auto as auto_record
import psu_log_report
import psu_run_link as link
import pwr01_logger
from test_thermal_hold_test import FakeClock, SOUND_ON_SEC, write_export


def write_log(path: Path, start: float, seconds: int, current, output_on=lambda t: True, skip=()):
    """1 s readings; current(t) and output_on(t) take seconds since `start`."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=pwr01_logger.CSV_COLUMNS)
        writer.writeheader()
        for k in range(seconds):
            if k in skip:
                continue
            t = start + k
            on = output_on(k)
            writer.writerow({
                "time_iso": dt.datetime.fromtimestamp(t).isoformat(), "time_unix_s": f"{t:.3f}",
                "voltage_V": "15.0000" if on else "0.0000", "current_A": f"{current(k) if on else 0.0:.4f}",
                "power_W": "", "output_on": "1" if on else "0", "voltage_set_V": "", "current_set_A": "",
                "error": "",
            })


class EventTests(unittest.TestCase):
    def test_a_fuse_trip_is_a_current_drop_with_the_output_still_on(self) -> None:
        with TemporaryDirectory() as temporary:
            log = Path(temporary) / "psu.csv"
            write_log(log, 1000.0, 120, lambda k: 4.0 if k < 60 else 0.9)
            events = link.detect_events(link.load_psu_rows(log))
            drops = [e for e in events if e["kind"] == "current_drop"]
            self.assertEqual(len(drops), 1)
            self.assertAlmostEqual(drops[0]["t"], 1060.0)
            self.assertEqual(drops[0]["from_A"], 4.0)
            self.assertFalse(any(e["kind"] == "output_off" for e in events))

    def test_output_off_and_gaps_are_reported(self) -> None:
        with TemporaryDirectory() as temporary:
            log = Path(temporary) / "psu.csv"
            write_log(log, 1000.0, 120, lambda k: 4.0, output_on=lambda k: k < 90, skip=range(30, 40))
            kinds = [e["kind"] for e in link.detect_events(link.load_psu_rows(log))]
            self.assertIn("output_off", kinds)
            self.assertIn("log_gap", kinds)

    def test_a_half_written_last_line_is_ignored(self) -> None:
        with TemporaryDirectory() as temporary:
            log = Path(temporary) / "psu.csv"
            write_log(log, 1000.0, 5, lambda k: 4.0)
            with log.open("a", encoding="utf-8") as handle:
                handle.write("2026-09-24T01:02:03,100")   # no newline, cut in the middle
            self.assertEqual(len(link.load_psu_rows(log)), 5)


class SteadinessTests(unittest.TestCase):
    def test_settling_time_and_drift(self) -> None:
        with TemporaryDirectory() as temporary:
            log = Path(temporary) / "psu.csv"
            # Current falls from 4.4 A and levels off at 4.0 A after about 20 minutes.
            write_log(log, 1000.0, 60 * 60, lambda k: 4.0 + 0.4 * max(0.0, 1 - k / 1200.0))
            result = link.steadiness(link.load_psu_rows(log), 1000.0)
            self.assertEqual(len(result["bins"]), 12)
            self.assertAlmostEqual(result["final_current_A"], 4.0, places=3)
            self.assertIsNotNone(result["settled_after_min"])
            self.assertTrue(15.0 <= result["settled_after_min"] <= 20.0, result["settled_after_min"])
            self.assertLess(abs(result["drift_last_window_percent"]), 0.5)

    def test_trips_do_not_count_as_settling(self) -> None:
        with TemporaryDirectory() as temporary:
            log = Path(temporary) / "psu.csv"
            write_log(log, 1000.0, 30 * 60, lambda k: 0.9 if 900 <= k < 960 else 4.0)
            result = link.steadiness(link.load_psu_rows(log), 1000.0)
            self.assertTrue(all(b["current_A"] > 3.9 for b in result["bins"]))


class AttachTests(unittest.TestCase):
    def test_the_rows_of_a_run_go_into_the_run_folder(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "psu_log.csv"
            write_log(log, 1000.0, 200, lambda k: 4.0 if k < 150 else 0.9)
            run_dir = root / "run"
            run_dir.mkdir()
            summary = link.attach_supply_record(run_dir, log, 1100.0, 1160.0, sound_on_unix=1000.0)
            with (run_dir / "supply_log.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 71)     # 1095 .. 1165 with the 5 s margin
            self.assertEqual(summary["run_file"], "supply_log.csv")
            self.assertTrue(summary["suspicious"])
            self.assertEqual([e["kind"] for e in summary["events"]], ["current_drop"])
            self.assertAlmostEqual(summary["minutes_since_sound_on_start"], 95 / 60.0, places=2)

    def test_a_missing_log_never_raises(self) -> None:
        with TemporaryDirectory() as temporary:
            summary = link.attach_supply_record(Path(temporary), Path(temporary) / "none.csv", 0.0, 1.0)
            self.assertIn("error", summary)


class AutoEntryTests(unittest.TestCase):
    def test_each_run_gets_its_supply_rows_and_summary(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = write_export(root)
            log = root / "psu_log_x.csv"
            write_log(log, SOUND_ON_SEC - 30.0, 400, lambda k: 0.05 if k < 30 else 4.0)
            output_root = root / "rec"
            clock = FakeClock(SOUND_ON_SEC + 20.0)
            run_dirs = []

            def record(*_args, **_kwargs):
                run_dir = output_root / f"run_{len(run_dirs)}"
                run_dir.mkdir(parents=True)
                (run_dir / "pipeline_manifest.json").write_text("{}", encoding="utf-8")
                run_dirs.append(run_dir)
                clock.now += 60.0
                return 0, run_dir

            events = mock.Mock()
            session = SimpleNamespace(current_pos=(0.0, 0.0, 0.0),
                                      active_output_started_wall_ns=int(SOUND_ON_SEC * 1e9))
            events.open_hw.return_value = session
            events.precompute.return_value = SimpleNamespace(name="first")
            argv = [
                "--hf-export-dir", str(export_dir), "--output-dir", str(output_root),
                "--hf-run", "kx=1", "--hf-run", "ky=1",
                "--acknowledge-step-response-risk", "--unattended-after-first-checkpoint",
                "--psu-log", str(log),
            ]
            with mock.patch.object(auto_record, "open_recording_hardware_session", events.open_hw), \
                    mock.patch.object(auto_record, "shutdown_recording_hardware_session", events.close_hw), \
                    mock.patch.object(auto_record, "precompute_hologram_playback", events.precompute), \
                    mock.patch.object(auto_record, "run_recording", side_effect=record), \
                    mock.patch.object(auto_record, "time", clock):
                self.assertEqual(auto_record.main(argv), 0)
            for run_dir in run_dirs:
                manifest = json.loads((run_dir / "pipeline_manifest.json").read_text(encoding="utf-8"))
                self.assertAlmostEqual(manifest["supply"]["current_mean_A"], 4.0)
                self.assertFalse(manifest["supply"]["suspicious"])
                self.assertTrue((run_dir / "supply_log.csv").is_file())
            session_json = json.loads(next(output_root.glob("auto_recording_session_*.json")).read_text(encoding="utf-8"))
            self.assertEqual(session_json["psu_log"], str(log.resolve()))
            self.assertTrue(all("supply" in run for run in session_json["runs"]))


class ReportTests(unittest.TestCase):
    def test_report_is_written_once_with_steadiness_and_events(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            sound_on = dt.datetime(2026, 9, 24, 20, 0, 0).timestamp()
            log = root / "psu_log_20260924_195950.csv"
            write_log(log, sound_on - 10, 40 * 60, lambda k: 0.05 if k < 10 else (0.9 if 1500 <= k < 1530 else 4.0))
            session = {
                "active_output_started_wall_ns": int(sound_on * 1e9), "supply_voltage_V": 15.0,
                "psu_log": str(log),
                "runs": [{"label": "kx", "exit_code": 0,
                          "run_dir": str(root / "step_response_identification_000_kx_scale100_V15_20260924_200500"),
                          "finished_at": dt.datetime.fromtimestamp(sound_on + 360).isoformat()}],
            }
            (root / "auto_recording_session_20260924_195959.json").write_text(json.dumps(session), encoding="utf-8")
            self.assertEqual(psu_log_report.main([str(root)]), 0)
            report = json.loads((root / "psu_report_20260924_195959.json").read_text(encoding="utf-8"))
            self.assertTrue((root / "psu_report_20260924_195959.png").is_file())
            self.assertEqual([e["kind"] for e in report["events"]], ["current_drop"])
            self.assertAlmostEqual(report["events"][0]["minute"], 24.83, places=1)
            self.assertIn("drift_last_window_percent", report["steadiness"])
            self.assertEqual(psu_log_report.main([str(root)]), 1)   # never overwritten


if __name__ == "__main__":
    unittest.main()
