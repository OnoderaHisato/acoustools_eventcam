"""Particle presence check after each run (particle_presence.py) and the warn / pause-and-retake handling."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import numpy as np

import acoustools_stereo_eventcam_3d_recording_auto as auto_record
import particle_presence
from test_thermal_hold_test import write_export

PRESENT = {"verdict": "present", "cameras": {"left": {"particle_px": [664, 388], "pre_peak_per_s": 1.4e6, "ratio": 1.0},
                                             "right": {"particle_px": [604, 404], "pre_peak_per_s": 1.2e6, "ratio": 1.0}}}
LOST = {"verdict": "lost", "cameras": {"left": {"ratio": 0.0}, "right": {"ratio": 0.01}}}


def run_with_checks(root: Path, verdicts: list[dict], answers: list[str], extra: list[str], runs=("kx", "ky")):
    export_dir = write_export(root)
    argv = ["--hf-export-dir", str(export_dir), "--output-dir", str(root / "rec"),
            "--unattended-after-first-checkpoint", "--acknowledge-step-response-risk", "--no-notify", *extra]
    for name in runs:
        argv += ["--hf-run", f"{name}=1"]
    checks = iter(verdicts)
    calls: list[str] = []

    def fake_check(run_dir, reference):
        calls.append(str(run_dir))
        return json.loads(json.dumps(next(checks)))

    run_dirs = iter(root / f"run{i}" for i in range(20))

    def fake_record_dir(*_a, **_k):
        d = next(run_dirs)
        d.mkdir()
        (d / "pipeline_manifest.json").write_text("{}", encoding="utf-8")
        return 0, d

    answer_iter = iter(answers)
    with mock.patch.object(particle_presence, "check_run", side_effect=fake_check), \
            mock.patch("builtins.input", side_effect=lambda *_a: next(answer_iter)):
        with mock.patch.object(auto_record, "run_recording", side_effect=fake_record_dir) as record:
            result, _events, _ = run_main_with_record(argv, record)
    session = json.loads(next((root / "rec").glob("auto_recording_session_*.json")).read_text(encoding="utf-8"))
    return result, session, record, calls


def run_main_with_record(argv, record_mock):
    events = mock.Mock()
    session = SimpleNamespace(current_pos=(0.0, 0.0, 0.0), active_output_started_wall_ns=None)
    events.open_hw.return_value = session
    events.preview.return_value = True
    events.precompute.return_value = SimpleNamespace(name="first-run-playback")
    patches = [
        mock.patch.object(auto_record, "open_recording_hardware_session", events.open_hw),
        mock.patch.object(auto_record, "shutdown_recording_hardware_session", events.close_hw),
        mock.patch.object(auto_record, "precompute_hologram_playback", events.precompute),
        mock.patch.object(auto_record, "run_particle_preview", events.preview),
    ]
    for patch in patches:
        patch.start()
    try:
        result = auto_record.main(argv)
    finally:
        for patch in reversed(patches):
            patch.stop()
    return result, events, []


class PresenceHandlingTests(unittest.TestCase):
    def test_present_runs_are_recorded_and_nothing_stops(self):
        with TemporaryDirectory() as temporary:
            result, session, record, calls = run_with_checks(Path(temporary), [PRESENT, PRESENT], [],
                                                             ["--particle-check", "pause"])
            self.assertEqual(result, 0)
            self.assertEqual(record.call_count, 2)
            self.assertEqual([r["particle_presence"]["verdict"] for r in session["runs"]], ["present", "present"])
            self.assertNotIn("particle_losses", session)
            manifest = json.loads((Path(temporary) / "run0" / "pipeline_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["particle_presence"]["verdict"], "present")

    def test_warn_records_the_loss_and_goes_on(self):
        with TemporaryDirectory() as temporary:
            result, session, record, _ = run_with_checks(Path(temporary), [PRESENT, LOST], [],
                                                         ["--particle-check", "warn"])
            self.assertEqual(result, 0)
            self.assertEqual(record.call_count, 2)
            self.assertEqual([(l["label"], l["verdict"]) for l in session["particle_losses"]], [("ky", "lost")])

    def test_pause_retakes_the_run_once_with_a_preview(self):
        with TemporaryDirectory() as temporary:
            result, session, record, _ = run_with_checks(Path(temporary), [LOST, PRESENT, PRESENT], [""],
                                                         ["--particle-check", "pause"])
            self.assertEqual(result, 0)
            labels = [c.kwargs["prepared_trajectory"].run_label for c in record.call_args_list]
            self.assertEqual(len(labels), 3)                       # kx, kx again, ky
            self.assertTrue(labels[0].startswith("000_kx") or "kx" in labels[0])
            self.assertIn("kx", labels[1])
            self.assertIn("ky", labels[2])
            prompts = [c.kwargs["prompt_before_capture"] for c in record.call_args_list]
            self.assertEqual(prompts, [True, True, False])          # the retake confirms the new particle
            self.assertEqual(session["particle_losses"][0]["answer"], "enter")
            self.assertEqual([r["particle_presence"]["verdict"] for r in session["runs"]], ["lost", "present", "present"])

    def test_lost_twice_goes_on_to_the_next_run(self):
        with TemporaryDirectory() as temporary:
            result, session, record, _ = run_with_checks(Path(temporary), [LOST, LOST, PRESENT], ["", ""],
                                                         ["--particle-check", "pause"])
            self.assertEqual(result, 0)
            self.assertEqual(record.call_count, 3)                  # kx, kx again, ky (no third kx)
            self.assertEqual([l["attempt"] for l in session["particle_losses"]], [1, 2])
            prompts = [c.kwargs["prompt_before_capture"] for c in record.call_args_list]
            self.assertEqual(prompts, [True, True, True])           # the next run confirms the new particle

    def test_skip_and_quit(self):
        with TemporaryDirectory() as temporary:
            result, session, record, _ = run_with_checks(Path(temporary), [LOST, PRESENT], ["s"],
                                                         ["--particle-check", "pause"])
            self.assertEqual((result, record.call_count), (0, 2))
        with TemporaryDirectory() as temporary:
            result, session, record, _ = run_with_checks(Path(temporary), [LOST], ["q"],
                                                         ["--particle-check", "pause"])
            self.assertEqual((result, record.call_count, session["status"]), (130, 1, "interrupted"))

    def test_off_does_not_check(self):
        with TemporaryDirectory() as temporary:
            result, session, _record, calls = run_with_checks(Path(temporary), [], [], [])
            self.assertEqual((result, calls), (0, []))


class CheckerTests(unittest.TestCase):
    def test_synthetic_run(self):
        """A blob at the centre before and after the motion is present; without it in the tail it is lost."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, tail_blob in (("kept", True), ("gone", False)):
                run = root / name
                for side in ("left", "right"):
                    (run / "stereo_recording" / side).mkdir(parents=True)
                    rng = np.random.default_rng(0)
                    t = []
                    xs = []
                    ys = []
                    for t0, t1, blob in ((0.0, 1.0, True), (1.0, 3.0, False), (3.0, 5.0, tail_blob)):
                        n_bg = int((t1 - t0) * 20000)
                        t += list(rng.uniform(t0, t1, n_bg)); xs += list(rng.integers(0, 1280, n_bg)); ys += list(rng.integers(0, 720, n_bg))
                        if blob:
                            n = int((t1 - t0) * 200000)
                            t += list(rng.uniform(t0, t1, n)); xs += list(rng.integers(660, 668, n)); ys += list(rng.integers(384, 392, n))
                    order = np.argsort(t)
                    ev = np.zeros(len(t), dtype=[("x", "<u2"), ("y", "<u2"), ("p", "<i2"), ("t", "<i8")])
                    ev["t"] = (np.asarray(t)[order] * 1e6).astype(np.int64) + 1_000_000
                    ev["x"] = np.asarray(xs)[order]
                    ev["y"] = np.asarray(ys)[order]
                    np.savez(run / "stereo_recording" / side / f"{side}_events.npz", events=ev,
                             output_start_ts_us=np.int64(1_000_000), output_end_ts_us=np.int64(6_000_000))
                (run / "pat_camera_timing.json").write_text(json.dumps(
                    {"pat_start_led": {"sync_t_recording_sec": 1.0}, "expected_motion_duration_sec": 1.5}), encoding="utf-8")
            kept = particle_presence.check_run(root / "kept")
            gone = particle_presence.check_run(root / "gone", kept)
            self.assertEqual(kept["verdict"], "present")
            self.assertEqual(gone["verdict"], "lost")
            self.assertGreater(kept["cameras"]["left"]["ratio"], 0.8)
            self.assertLess(gone["cameras"]["left"]["ratio"], 0.05)


if __name__ == "__main__":
    unittest.main()
