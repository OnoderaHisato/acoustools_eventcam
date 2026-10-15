"""live_session_watch.py: findings per run, particle loss, stall, and the end of the watch (no hardware)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import live_session_watch as watch

PRESENT = {"verdict": "present", "cameras": {"left": {"ratio": 1.0, "particle_px": [664, 388], "pre_peak_per_s": 1e6},
                                             "right": {"ratio": 1.0, "particle_px": [604, 404], "pre_peak_per_s": 1e6}}}
LOST = {"verdict": "lost", "cameras": {"left": {"ratio": 0.0}, "right": {"ratio": 0.0}}}


class FindingsTests(unittest.TestCase):
    def test_each_kind_of_finding(self):
        run = {"label": "kx", "exit_code": 0, "capture_report": {"led_warnings": ["onset off by 1.1 s"], "failures": ["a1: no LED"]},
               "supply": {"events": [{"kind": "current_drop"}]}, "pat_temperature": {"interrupted": True},
               "environment": {"stale": True}}
        found, presence = watch.findings_for_run(3, run, None, check_particle=False)
        self.assertEqual([e for e, _ in found], ["led_warning", "capture_retried", "supply_event",
                                                 "pat_temp_interrupted", "environment_stale"])
        self.assertIsNone(presence)
        found, _ = watch.findings_for_run(4, {"label": "ky", "exit_code": 1, "error": "boom"}, None, False)
        self.assertEqual(found[0][0], "run_failed")

    def test_particle_loss_is_reported(self):
        with TemporaryDirectory() as temporary:
            run = {"label": "kx", "exit_code": 0, "run_dir": temporary}
            with mock.patch.object(watch.particle_presence, "check_run", return_value=LOST):
                found, presence = watch.findings_for_run(5, run, PRESENT, True)
            self.assertEqual(found[0][0], "particle_lost")
            self.assertEqual(presence["verdict"], "lost")


class LoopTests(unittest.TestCase):
    def test_new_runs_are_checked_until_the_session_ends(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            session_path = root / "auto_recording_session_20261016_010101.json"
            (root / "r1").mkdir()
            (root / "r2").mkdir()
            states = [
                {"status": "running", "runs": [{"label": "a", "exit_code": 0, "run_dir": str(root / "r1")}]},
                {"status": "running", "runs": [{"label": "a", "exit_code": 0, "run_dir": str(root / "r1")},
                                               {"label": "b", "exit_code": 0, "run_dir": str(root / "r2")}]},
                {"status": "complete", "runs": [{"label": "a", "exit_code": 0, "run_dir": str(root / "r1")},
                                                {"label": "b", "exit_code": 0, "run_dir": str(root / "r2")}]},
            ]
            reads = iter([states[0], states[0], states[1], states[2]])
            sent = []
            with mock.patch.object(watch, "read_session", side_effect=lambda _p: next(reads)), \
                    mock.patch.object(watch.particle_presence, "check_run", side_effect=[PRESENT, LOST]), \
                    mock.patch.object(watch.session_notify, "notify", side_effect=lambda e, s, **k: sent.append(e)), \
                    mock.patch.object(watch.time, "sleep"):
                code = watch.main(["--session", str(session_path), "--from-run", "1"])
            self.assertEqual(code, 0)
            self.assertEqual(sent, ["particle_lost"])
            state = json.loads((root / "live_watch_20261016_010101.json").read_text(encoding="utf-8"))
            self.assertEqual([p["verdict"] for p in state["particle"]], ["present", "lost"])
            self.assertEqual(state["session_status"], "complete")

    def test_stall_is_reported_once(self):
        with TemporaryDirectory() as temporary:
            session_path = Path(temporary) / "auto_recording_session_x.json"
            running = {"status": "running", "runs": []}
            reads = iter([running] * 4 + [{"status": "failed", "runs": []}])
            clock = iter([0.0, 0.0, 100.0, 500.0, 900.0, 1300.0, 1700.0, 2100.0])
            sent = []
            with mock.patch.object(watch, "read_session", side_effect=lambda _p: next(reads)), \
                    mock.patch.object(watch.session_notify, "notify", side_effect=lambda e, s, **k: sent.append(e)), \
                    mock.patch.object(watch.time, "time", side_effect=lambda: next(clock)), \
                    mock.patch.object(watch.time, "sleep"):
                watch.main(["--session", str(session_path), "--stall-min", "6"])
            self.assertEqual(sent, ["session_stall"])


class WaitingTests(unittest.TestCase):
    def test_unfinished_newest_folder(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            done = root / "feedforward_validation_001_a_scale100_V15_x"
            done.mkdir()
            (done / "pipeline_manifest.json").write_text("{}", encoding="utf-8")
            runs = [{"label": "a", "run_dir": str(done), "finished_at": "2000-01-01T00:00:00"}]
            self.assertIsNone(watch.unfinished_run_folder(root, runs))
            busy = root / "feedforward_validation_002_b_scale100_V15_y"
            busy.mkdir()
            self.assertEqual(watch.unfinished_run_folder(root, runs), busy)
            (busy / "pipeline_manifest.json").write_text("{}", encoding="utf-8")
            self.assertIsNone(watch.unfinished_run_folder(root, runs))


if __name__ == "__main__":
    unittest.main()
