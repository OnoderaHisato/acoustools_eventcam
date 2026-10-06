#!/usr/bin/env python3
"""Tests for the tracking-only left mask override and the 2D track re-acquisition (2026-09-26)."""

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
import unittest

import numpy as np

import stereo_acoustools_3d_postprocess_core as postprocess_core
from eventcam_npz_track import reject_large_jumps, reject_large_jumps_with_reacquire
from stereo_acoustools_3d_postprocess_core import processing_commands, processing_namespace
from stereo_process_recording import tracking_resume_config

ROOT = Path(__file__).resolve().parent
LED = {"pat_start_led": {"side": "left", "roi": "600,0,1280,180"}}


def lost_track(gap_bins: int = 100, after_bins: int = 50) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """100 us bins: 50 bins at (100, 100), gap_bins without a centroid, then after_bins at (300, 100)."""
    n = 50 + gap_bins + after_bins
    t = (np.arange(n) + 1) * 1e-4
    x = np.full(n, np.nan)
    y = np.full(n, np.nan)
    x[:50], y[:50] = 100.0, 100.0
    x[50 + gap_bins:], y[50 + gap_bins:] = 300.0, 100.0
    return t, x, y


class ReacquireFilterTests(unittest.TestCase):
    def test_off_is_the_original_filter(self) -> None:
        t, x, y = lost_track()
        legacy = reject_large_jumps(x, y, 15.0)
        rejected, events = reject_large_jumps_with_reacquire(x, y, 15.0, t=t, reacquire_after_sec=0.0)
        np.testing.assert_array_equal(rejected, legacy)
        self.assertEqual(int(legacy.sum()), 50)          # never comes back without re-acquisition
        self.assertEqual(events, [])

    def test_reacquires_after_the_gap_and_records_it(self) -> None:
        t, x, y = lost_track(gap_bins=100)
        rejected, events = reject_large_jumps_with_reacquire(x, y, 15.0, t=t, reacquire_after_sec=0.005)
        self.assertEqual(int(rejected.sum()), 0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["bin_index"], 150)
        self.assertAlmostEqual(events[0]["gap_sec"], 0.0101, places=6)
        self.assertEqual((events[0]["x_px"], events[0]["from_x_px"]), (300.0, 100.0))

    def test_waits_until_5_ms_after_the_last_accepted_point(self) -> None:
        t, x, y = lost_track(gap_bins=20)                 # far centroids from 2.1 ms after the last accepted one
        rejected, events = reject_large_jumps_with_reacquire(x, y, 15.0, t=t, reacquire_after_sec=0.005)
        self.assertTrue(rejected[70:99].all())            # still < 5 ms since bin 49
        self.assertFalse(rejected[99:].any())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["bin_index"], 99)
        self.assertAlmostEqual(events[0]["gap_sec"], 0.005, places=9)

    def test_inconsistent_centroids_are_not_reacquired(self) -> None:
        t, x, y = lost_track(gap_bins=100)
        x[150::2] = 500.0                                 # alternates 300 / 500: never 3 consistent bins
        rejected, events = reject_large_jumps_with_reacquire(x, y, 15.0, t=t, reacquire_after_sec=0.005)
        self.assertEqual(int(rejected.sum()), 50)
        self.assertEqual(events, [])

    def test_waits_for_the_first_consistent_run(self) -> None:
        t, x, y = lost_track(gap_bins=100)
        x[150] = 500.0                                    # one stray bin, then a steady track
        rejected, events = reject_large_jumps_with_reacquire(x, y, 15.0, t=t, reacquire_after_sec=0.005)
        self.assertTrue(rejected[150])
        self.assertEqual(int(rejected.sum()), 1)
        self.assertEqual(events[0]["bin_index"], 151)

    def test_a_fully_tracked_run_is_unchanged(self) -> None:
        t = (np.arange(2000) + 1) * 1e-4
        x = 640 + 30 * np.sin(2 * np.pi * 10 * t)
        y = 360 + 30 * np.cos(2 * np.pi * 10 * t)
        x[500] = x[500] + 40                              # single-bin spike, rejected both ways
        legacy = reject_large_jumps(x, y, 15.0)
        rejected, events = reject_large_jumps_with_reacquire(x, y, 15.0, t=t, reacquire_after_sec=0.005)
        np.testing.assert_array_equal(rejected, legacy)
        self.assertEqual(int(rejected.sum()), 1)
        self.assertEqual(events, [])


class TrackerEndToEndTests(unittest.TestCase):
    """Run eventcam_npz_track.py on a synthetic blob that disappears for 10 ms and comes back 200 px away."""

    @staticmethod
    def write_events(path: Path) -> None:
        rng = np.random.default_rng(0)
        chunks = []
        for t0, cx in ((0, 100), (30_000, 300)):          # 0-20 ms at x=100, 30-50 ms at x=300 (us)
            n = 20_000
            chunks.append(
                np.column_stack(
                    [
                        rng.integers(cx - 3, cx + 4, n),
                        rng.integers(97, 104, n),
                        rng.integers(0, 2, n),
                        np.sort(rng.integers(t0, t0 + 20_000, n)),
                    ]
                )
            )
        data = np.concatenate(chunks)
        events = np.zeros(len(data), dtype=[("x", "<u2"), ("y", "<u2"), ("p", "<i2"), ("t", "<i8")])
        events["x"], events["y"], events["p"], events["t"] = data.T
        np.savez(path, events=events, output_start_ts_us=np.int64(0))

    def run_track(self, npz: Path, out: Path, *extra: str) -> dict:
        subprocess.run(
            [sys.executable, str(ROOT / "eventcam_npz_track.py"), str(npz), "--output-dir", str(out),
             "--bin-us", "200", "--window-us", "200", "--hop-us", "100", "--dt-us", "100",
             "--max-interp-gap-sec", "0.005", "--max-step-px", "15", "--sensor-width", "1280",
             "--sensor-height", "720", *extra],
            check=True, capture_output=True,
        )
        return json.loads((out / "event_tracking_meta.json").read_text(encoding="utf-8"))

    def test_reacquisition_is_written_and_off_by_default(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            npz = root / "events.npz"
            self.write_events(npz)
            off = self.run_track(npz, root / "off")
            on = self.run_track(npz, root / "on", "--reacquire-after-sec", "0.005")
            self.assertEqual(off["reacquisitions"], 0)
            self.assertFalse((root / "off" / "tracking_reacquisitions.csv").exists())
            self.assertGreater(off["tracking_rejected_points"], 150)
            self.assertEqual(on["reacquisitions"], 1)
            self.assertEqual(on["tracking_rejected_points"], 0)
            with open(root / "on" / "tracking_reacquisitions.csv", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(float(rows[0]["x_px"]), 300.0, delta=2.0)
            self.assertGreaterEqual(float(rows[0]["gap_sec"]), 0.005)


class LeftMaskOverrideTests(unittest.TestCase):
    def commands(self, overrides: dict | None = None) -> list[str]:
        args = processing_namespace(LED, overrides or {})
        return processing_commands(args, Path("run"), Path("cal.npz"), Path("ideal.csv"), 0.1)[0]

    @staticmethod
    def value_after(command: list[str], flag: str) -> str | None:
        return command[command.index(flag) + 1] if flag in command else None

    def test_default_uses_the_led_roi_and_no_reacquisition(self) -> None:
        command = self.commands()
        self.assertEqual(self.value_after(command, "--left-mask-roi"), "600,0,1280,180")
        self.assertNotIn("--reacquire-after-sec", command)
        self.assertNotIn("--right-mask-roi", command)

    def test_override_replaces_only_the_tracking_mask(self) -> None:
        overrides = {"left_mask_roi_override": " 748,0,892,52 ", "reacquire_after_sec": 0.005, "reacquire_bins": 3}
        args = processing_namespace(LED, overrides)
        command = processing_commands(args, Path("run"), Path("cal.npz"), Path("ideal.csv"), 0.1)[0]
        self.assertEqual(self.value_after(command, "--left-mask-roi"), "748,0,892,52")
        self.assertEqual(command.count("--left-mask-roi"), 1)
        self.assertEqual(self.value_after(command, "--reacquire-after-sec"), "0.005")
        self.assertEqual(self.value_after(command, "--reacquire-bins"), "3")
        self.assertEqual(args.pat_start_led_roi, "600,0,1280,180")      # LED ROI itself untouched

    def test_bad_override_is_refused(self) -> None:
        for bad in ("748,0,892", "892,0,748,52", "a,b,c,d"):
            with self.assertRaises(ValueError):
                processing_namespace(LED, {"left_mask_roi_override": bad})

    def test_run_postprocess_records_the_settings(self) -> None:
        with TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            ideal = run_dir / "ideal.csv"
            ideal.write_text("time,target_x,target_y,target_z\n", encoding="utf-8")
            manifest = {
                "processing_status": "complete",
                "processing_completed_at": "2026-09-25T20:00:00",
                "stereo_calibration": str(run_dir / "cal.npz"),
                "left_serial": "l",
                "right_serial": "r",
                "ideal_log": str(ideal),
                **LED,
            }
            (run_dir / "pipeline_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            timing = {"ideal_start_in_recording_sec": 2.5, "sync_t_recording_sec": 2.5}
            (run_dir / "pat_camera_timing.json").write_text(json.dumps(timing), encoding="utf-8")
            for side in ("left", "right"):
                track = run_dir / "stereo_recording" / side / "event_tracking"
                track.mkdir(parents=True)
                (run_dir / "stereo_recording" / side / f"{side}_events.npz").write_bytes(b"x")
                meta = {"mask_rois": [[748, 0, 892, 52]] if side == "left" else [], "tracking_rejected_points": 0,
                        "reacquisitions": 2 if side == "left" else 0}
                (track / "event_tracking_meta.json").write_text(json.dumps(meta), encoding="utf-8")
            with (
                mock.patch.object(postprocess_core, "validate_calibration", return_value=run_dir / "cal.npz"),
                mock.patch.object(postprocess_core, "run_command"),
            ):
                postprocess_core.run_postprocess(
                    run_dir, {"left_mask_roi_override": "748,0,892,52", "reacquire_after_sec": 0.005}
                )
            saved = json.loads((run_dir / "pipeline_manifest.json").read_text(encoding="utf-8"))
            settings = saved["postprocess_settings"]
            self.assertEqual(settings["left_tracking_mask_roi"], "748,0,892,52")
            self.assertTrue(settings["left_mask_roi_overridden"])
            self.assertEqual(settings["pat_start_led_roi_for_timing"], "600,0,1280,180")
            self.assertEqual(settings["left_reacquisitions"], 2)
            self.assertEqual(settings["previous_processing_completed_at"], "2026-09-25T20:00:00")
            self.assertEqual(saved["pat_start_led"]["roi"], "600,0,1280,180")
            self.assertEqual(json.loads((run_dir / "pat_camera_timing.json").read_text()), timing)


class ResumeConfigTests(unittest.TestCase):
    def test_resume_config_changes_only_when_reacquisition_is_on(self) -> None:
        with TemporaryDirectory() as temporary:
            npz = Path(temporary) / "e.npz"
            npz.write_bytes(b"e")
            base = dict(window_us=200, hop_us=100, dt_us=100.0, max_interp_gap_sec=0.005, max_step_px=15.0,
                        t_start_sec=0.0, t_end_sec=0.0, roi="0,0,1280,720", tracking_method="event_weighted",
                        threshold_count=1, min_events=20, min_area=5, min_mass=30, polarity="all")
            legacy = tracking_resume_config(argparse.Namespace(**base), npz, "600,0,1280,180")
            off = tracking_resume_config(argparse.Namespace(**base, reacquire_after_sec=0.0, reacquire_bins=3), npz, "600,0,1280,180")
            on = tracking_resume_config(argparse.Namespace(**base, reacquire_after_sec=0.005, reacquire_bins=3), npz, "600,0,1280,180")
            self.assertEqual(off, legacy)
            self.assertEqual(on["reacquire_after_sec"], 0.005)


if __name__ == "__main__":
    unittest.main()
