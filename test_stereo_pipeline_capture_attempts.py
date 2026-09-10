#!/usr/bin/env python3
"""Tests for transactional stereo capture attempt handling."""

from pathlib import Path
from tempfile import TemporaryDirectory
from argparse import Namespace
from unittest import mock
import unittest

import torch

from stereo_acoustools_3d_common import apply_manifest_processing_config
from stereo_acoustools_3d_recording_core import (
    PreparedTrajectory,
    RecordingHardwareSession,
    compute_led_search_half_window_sec,
    create_run_dir,
    promote_capture_attempt,
    remap_attempt_result_paths,
    remove_capture_attempt,
    remove_failed_run,
    RUN_ARTIFACT_LONGEST_SUFFIX,
    RUN_ARTIFACT_PATH_BUDGET_CHARS,
    RUN_LONGEST_CAPTURE_RELATIVE_PATH,
    RUN_PATH_BUDGET_CHARS,
    safe_run_artifact_stem,
    precompute_hologram_playback,
    shutdown_recording_hardware_session,
    wait_for_active_warmup,
)


class CaptureAttemptTests(unittest.TestCase):
    def test_static_hologram_precompute_compacts_identical_frames(self) -> None:
        trajectory = PreparedTrajectory(
            shape_name="static",
            positions=[(0.0, 0.0, 0.0)] * 6,
            n_steps=6,
            frequency_hz=1.0,
            loops=2,
            rate=mock.sentinel.rate,
            parameters={},
            closed_cycle=True,
            stats={},
        )
        hologram = torch.ones((1, 4), dtype=torch.complex64)
        with mock.patch(
            "stereo_acoustools_3d_recording_core.compute_holograms_for_positions",
            return_value=[hologram],
        ) as compute:
            playback = precompute_hologram_playback(trajectory)

        compute.assert_called_once_with([(0.0, 0.0, 0.0)])
        self.assertEqual(playback.source_frame_count, 12)
        self.assertEqual(playback.message_geometry_count, 1)
        self.assertEqual(playback.message_loops, 12)
        self.assertTrue(playback.static_compacted)

    def test_active_warmup_waits_only_remaining_pat_output_time(self) -> None:
        session = RecordingHardwareSession(
            lev=mock.Mock(),
            current_pos=(0.0, 0.0, 0.0),
            current_hologram=torch.ones((1, 4), dtype=torch.complex64),
            active_output_started_perf_ns=1_000_000_000,
            active_output_started_wall_ns=2_000_000_000,
        )
        with (
            mock.patch(
                "stereo_acoustools_3d_recording_core.time.perf_counter_ns",
                side_effect=[31_000_000_000, 61_000_000_000, 61_000_000_000],
            ),
            mock.patch("stereo_acoustools_3d_recording_core.time.sleep") as sleep,
        ):
            result = wait_for_active_warmup(session, 1.0)

        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 30.0)
        self.assertAlmostEqual(result["elapsed_at_wait_start_sec"], 30.0)
        self.assertAlmostEqual(result["intentional_wait_sec"], 30.0)
        self.assertAlmostEqual(result["elapsed_after_wait_sec"], 60.0)

    def test_long_run_name_reserves_space_for_nested_capture_metadata(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "records"
            run_dir = create_run_dir(
                root,
                "step_response_identification",
                "000_E0_x_staircase_boundary_long_hold_r01_scale100_" + "x" * 80,
            )
            deepest = run_dir / RUN_LONGEST_CAPTURE_RELATIVE_PATH

            self.assertLessEqual(len(str(deepest.resolve())), RUN_PATH_BUDGET_CHARS)
            self.assertRegex(run_dir.name, r"_[0-9a-f]{10}_\d{8}_\d{6}$")

    def test_run_artifact_stem_is_unchanged_when_path_is_short(self) -> None:
        with TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            run_dir.mkdir()
            preferred = "step_response_identification_s180000_f10000_l1"

            self.assertEqual(safe_run_artifact_stem(run_dir, preferred), preferred)

    def test_run_artifact_stem_is_shortened_to_windows_safe_path_budget(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            component_length = max(32, 195 - len(str(root)) - 1)
            run_dir = root / ("r" * component_length)
            run_dir.mkdir()
            preferred = "step_response_identification_s180000_f10000_l1"

            stem = safe_run_artifact_stem(run_dir, preferred)
            preview = run_dir / f"{stem}{RUN_ARTIFACT_LONGEST_SUFFIX}"

            self.assertNotEqual(stem, preferred)
            self.assertLessEqual(len(str(preview.resolve())), RUN_ARTIFACT_PATH_BUDGET_CHARS)
            self.assertRegex(stem, r"_[0-9a-f]{10}$")

    def test_persistent_hardware_session_shutdown_is_idempotent(self) -> None:
        lev = mock.Mock()
        session = RecordingHardwareSession(
            lev=lev,
            current_pos=(0.0, 0.0, 0.0),
            current_hologram=torch.zeros((4, 256)),
        )
        with (
            mock.patch(
                "stereo_acoustools_3d_recording_core.add_lev_sig",
                return_value=mock.sentinel.off_phase,
            ),
            mock.patch(
                "stereo_acoustools_3d_recording_core.prepare_message_from_holograms",
                return_value=(mock.sentinel.phases, mock.sentinel.amplitudes, 1),
            ) as prepare,
        ):
            shutdown_recording_hardware_session(session)
            shutdown_recording_hardware_session(session)

        self.assertTrue(session.is_shutdown)
        prepare.assert_called_once()
        lev.send_message.assert_called_once_with(
            mock.sentinel.phases,
            mock.sentinel.amplitudes,
            0,
            1,
            sleep_ms=0,
            loop=False,
            num_loops=1,
        )

    def test_deferred_processing_restores_config_but_keeps_explicit_override(self) -> None:
        args = Namespace(window_us=999, hop_us=999)
        manifest = {"processing_config": {"window_us": 200, "hop_us": 100}}

        apply_manifest_processing_config(args, manifest, {"window_us"})

        self.assertEqual(args.window_us, 999)
        self.assertEqual(args.hop_us, 100)

    def test_led_search_window_includes_observed_pat_send_overhead(self) -> None:
        self.assertAlmostEqual(compute_led_search_half_window_sec(0.2, 11.1955, 10.0), 1.3955)
        self.assertAlmostEqual(compute_led_search_half_window_sec(0.2, 7.9, 8.0), 0.2)

    def test_failed_attempt_is_removed_without_touching_run(self) -> None:
        with TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            run_dir = output_root / "run_001"
            attempt = run_dir / "_capture_attempt_01"
            (attempt / "stereo_recording" / "left").mkdir(parents=True)
            (attempt / "stereo_recording" / "left" / "left_events.npz").write_bytes(b"failed")
            (run_dir / "ideal_log.csv").write_text("t,x,y,z\n", encoding="utf-8")

            remove_capture_attempt(attempt, run_dir)

            self.assertFalse(attempt.exists())
            self.assertTrue((run_dir / "ideal_log.csv").exists())

    def test_remove_attempt_refuses_a_sibling_directory(self) -> None:
        with TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            run_dir = output_root / "run_001"
            sibling = output_root / "_capture_attempt_01"
            run_dir.mkdir()
            sibling.mkdir()

            with self.assertRaises(RuntimeError):
                remove_capture_attempt(sibling, run_dir)
            self.assertTrue(sibling.exists())

    def test_successful_attempt_is_promoted_and_report_paths_are_remapped(self) -> None:
        with TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run_001"
            attempt = run_dir / "_capture_attempt_02"
            camera_npz = attempt / "stereo_recording" / "left" / "left_events.npz"
            led_json = attempt / "pat_start_led" / "left_pat_start_led.json"
            camera_npz.parent.mkdir(parents=True)
            led_json.parent.mkdir(parents=True)
            camera_npz.write_bytes(b"success")
            led_json.write_text("{}", encoding="utf-8")
            (attempt / "capture_start_marker.json").write_text("{}", encoding="utf-8")
            result = {"input_npz": str(camera_npz), "report_json": str(led_json)}

            promote_capture_attempt(attempt, run_dir)
            remapped = remap_attempt_result_paths(result, attempt, run_dir)

            self.assertFalse(attempt.exists())
            self.assertTrue((run_dir / "stereo_recording" / "left" / "left_events.npz").exists())
            self.assertTrue((run_dir / "pat_start_led" / "left_pat_start_led.json").exists())
            self.assertEqual(
                Path(str(remapped["report_json"])),
                (run_dir / "pat_start_led" / "left_pat_start_led.json").resolve(),
            )

    def test_failed_run_removal_is_scoped_to_output_root(self) -> None:
        with TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "output"
            output_root.mkdir()
            run_dir = output_root / "run_001"
            run_dir.mkdir()
            outside = Path(temporary) / "outside"
            outside.mkdir()

            remove_failed_run(run_dir, output_root)
            self.assertFalse(run_dir.exists())
            with self.assertRaises(RuntimeError):
                remove_failed_run(outside, output_root)
            self.assertTrue(outside.exists())


if __name__ == "__main__":
    unittest.main()
