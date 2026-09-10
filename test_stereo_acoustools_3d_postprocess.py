#!/usr/bin/env python3
"""Tests for the deferred stereo AcousTools postprocess entry point."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
import unittest

import stereo_acoustools_3d_postprocess_core as postprocess_core
from stereo_acoustools_3d_postprocess_core import (
    processing_commands,
    processing_status,
    resolve_run_artifact,
    validate_run_dir,
)
from stereo_process_recording import (
    TRACKING_RESUME_OUTPUTS,
    tracking_can_resume,
    tracking_resume_config,
    write_tracking_resume_checkpoint,
)


class DeferredPostprocessTests(unittest.TestCase):
    @staticmethod
    def make_complete_capture(run_dir: Path) -> None:
        ideal_log = run_dir / "ideal.csv"
        ideal_log.write_text("time,target_x,target_y,target_z\n", encoding="utf-8")
        manifest = {
            "processing_status": "pending",
            "stereo_calibration": str(run_dir / "calibration.npz"),
            "left_serial": "left",
            "right_serial": "right",
            "ideal_log": str(ideal_log),
            "camera_to_pat_transform": "",
            "pat_start_led": {"side": "left", "roi": "600,0,1280,180"},
            "processing_config": {"window_us": 200},
        }
        (run_dir / "pipeline_manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (run_dir / "pat_camera_timing.json").write_text(
            json.dumps({"ideal_start_in_recording_sec": 0.1}), encoding="utf-8"
        )
        for side in ("left", "right"):
            path = run_dir / "stereo_recording" / side / f"{side}_events.npz"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"placeholder")

    def test_validate_run_dir_requires_both_camera_npz_files(self) -> None:
        with TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "pipeline_manifest.json").write_text("{}", encoding="utf-8")
            (run_dir / "pat_camera_timing.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "left_events.npz"):
                validate_run_dir(run_dir)

    def test_valid_capture_and_pending_status(self) -> None:
        with TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "pipeline_manifest.json").write_text(
                json.dumps({"processing_status": "pending"}), encoding="utf-8"
            )
            (run_dir / "pat_camera_timing.json").write_text("{}", encoding="utf-8")
            for side in ("left", "right"):
                path = run_dir / "stereo_recording" / side / f"{side}_events.npz"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"placeholder")

            self.assertEqual(validate_run_dir(run_dir), run_dir.resolve())
            self.assertEqual(processing_status(run_dir), "pending")

    def test_relocated_run_resolves_ideal_log_from_new_run_directory(self) -> None:
        with TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "relocated_run"
            run_dir.mkdir()
            relocated_log = run_dir / "ideal.csv"
            relocated_log.write_text("time,target_x,target_y,target_z\n", encoding="utf-8")
            stale_path = Path(temporary) / "old_root" / "ideal.csv"

            self.assertEqual(
                resolve_run_artifact(run_dir, stale_path), relocated_log.resolve()
            )

    def test_core_updates_manifest_without_routing_through_pipeline(self) -> None:
        with TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            self.make_complete_capture(run_dir)
            with (
                mock.patch.object(
                    postprocess_core,
                    "validate_calibration",
                    return_value=(run_dir / "calibration.npz").resolve(),
                ),
                mock.patch.object(postprocess_core, "processing_commands", return_value=[]),
            ):
                result = postprocess_core.run_postprocess(run_dir)

            manifest = json.loads(
                (run_dir / "pipeline_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result, 0)
            self.assertTrue(manifest["processing_complete"])
            self.assertEqual(manifest["processing_status"], "complete")

    def test_resume_is_forwarded_to_stereo_processing_command(self) -> None:
        with TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            args = postprocess_core.processing_namespace(
                {"pat_start_led": {"side": "left", "roi": "600,0,1280,180"}}
            )
            commands = processing_commands(
                args,
                run_dir,
                run_dir / "calibration.npz",
                run_dir / "ideal.csv",
                0.1,
                resume=True,
            )
            self.assertIn("--resume", commands[0])

    def test_tracking_resume_checkpoint_requires_exact_configuration(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_npz = root / "left_events.npz"
            input_npz.write_bytes(b"events")
            track_dir = root / "event_tracking"
            track_dir.mkdir()
            for name in TRACKING_RESUME_OUTPUTS:
                (track_dir / name).write_bytes(b"complete")
            args = argparse.Namespace(
                window_us=200,
                hop_us=100,
                dt_us=100.0,
                max_interp_gap_sec=0.005,
                max_step_px=15.0,
                t_start_sec=0.0,
                t_end_sec=0.0,
                roi="0,0,1280,720",
                tracking_method="event_weighted",
                threshold_count=1,
                min_events=20,
                min_area=5,
                min_mass=30,
                polarity="all",
            )
            config = tracking_resume_config(args, input_npz, "600,0,1280,180")
            write_tracking_resume_checkpoint(track_dir, config)
            self.assertTrue(tracking_can_resume(track_dir, config)[0])

            changed = dict(config)
            changed["window_us"] = 300
            self.assertFalse(tracking_can_resume(track_dir, changed)[0])


if __name__ == "__main__":
    unittest.main()
