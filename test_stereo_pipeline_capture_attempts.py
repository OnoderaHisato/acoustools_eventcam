#!/usr/bin/env python3
"""Tests for transactional stereo capture attempt handling."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from argparse import Namespace
from types import SimpleNamespace
from unittest import mock
import unittest

import torch

from stereo_acoustools_3d_common import apply_manifest_processing_config
import stereo_acoustools_3d_recording_core as recording_core
from stereo_acoustools_3d_recording_core import (
    PreparedTrajectory,
    RecordingHardwareSession,
    compute_led_search_half_window_sec,
    compute_scaled_holograms_for_positions,
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

    def test_distinct_positions_are_solved_once_and_shared(self) -> None:
        centre = (0.001, -0.002, 0.003)
        plus = (0.0014, -0.002, 0.003)
        minus = (0.0006, -0.002, 0.003)
        positions = [centre] * 3 + [plus] * 2 + [centre] * 2 + [minus] * 2 + [centre]
        with (
            mock.patch(
                "stereo_acoustools_3d_recording_core.compute_holograms_for_positions",
                side_effect=lambda points: [f"solved{point}" for point in points],
            ) as compute,
            mock.patch(
                "stereo_acoustools_3d_recording_core.scale_hologram_drive_amplitude",
                side_effect=lambda hologram, scale: ("scaled", hologram, scale),
            ) as scale,
        ):
            holograms, distinct = compute_scaled_holograms_for_positions(positions, 0.85)

        compute.assert_called_once_with([centre, plus, minus])
        self.assertEqual(scale.call_count, 3)
        self.assertEqual(distinct, 3)
        self.assertEqual(len(holograms), len(positions))
        for position, hologram in zip(positions, holograms):
            self.assertEqual(hologram, ("scaled", f"solved{position}", 0.85))
        self.assertIs(holograms[0], holograms[-1])
        self.assertIs(holograms[3], holograms[4])
        self.assertIsNot(holograms[0], holograms[3])

    def test_distinct_positions_keep_negative_zero_and_pass_values_unchanged(self) -> None:
        negative_zero = (-0.0, 0.0, 0.0)
        positive_zero = (0.0, 0.0, 0.0)
        with (
            mock.patch(
                "stereo_acoustools_3d_recording_core.compute_holograms_for_positions",
                side_effect=lambda points: list(points),
            ) as compute,
            mock.patch(
                "stereo_acoustools_3d_recording_core.scale_hologram_drive_amplitude",
                side_effect=lambda hologram, scale: hologram,
            ),
        ):
            holograms, distinct = compute_scaled_holograms_for_positions(
                [positive_zero, negative_zero, positive_zero], 1.0
            )
        self.assertEqual(distinct, 2)
        solved = compute.call_args.args[0]
        self.assertIs(solved[0], positive_zero)
        self.assertIs(solved[1], negative_zero)
        self.assertIs(holograms[2], positive_zero)

    def test_unique_trajectory_is_solved_frame_by_frame_as_before(self) -> None:
        positions = [(0.0001 * index, 0.0, 0.0) for index in range(5)]
        with (
            mock.patch(
                "stereo_acoustools_3d_recording_core.compute_holograms_for_positions",
                side_effect=lambda points: [f"h{index}" for index in range(len(points))],
            ) as compute,
            mock.patch(
                "stereo_acoustools_3d_recording_core.scale_hologram_drive_amplitude",
                side_effect=lambda hologram, scale: hologram,
            ),
        ):
            holograms, distinct = compute_scaled_holograms_for_positions(positions, 1.0)
        compute.assert_called_once_with(positions)
        self.assertEqual(holograms, ["h0", "h1", "h2", "h3", "h4"])
        self.assertEqual(distinct, 5)

    def test_staircase_precompute_solves_each_level_once(self) -> None:
        levels = [(0.0, 0.0, 0.0), (0.00025, 0.0, 0.0), (-0.00025, 0.0, 0.0)]
        positions = (
            [levels[0]] * 4 + [levels[1]] * 4 + [levels[0]] * 4 + [levels[2]] * 4 + [levels[0]] * 4
        )
        trajectory = PreparedTrajectory(
            shape_name="step_response_identification",
            positions=positions,
            n_steps=len(positions),
            frequency_hz=1.0,
            loops=1,
            rate=mock.sentinel.rate,
            parameters={},
            closed_cycle=False,
            stats={},
        )
        with (
            mock.patch(
                "stereo_acoustools_3d_recording_core.compute_holograms_for_positions",
                side_effect=lambda points: [f"level{index}" for index in range(len(points))],
            ) as compute,
            mock.patch(
                "stereo_acoustools_3d_recording_core.scale_hologram_drive_amplitude",
                side_effect=lambda hologram, scale: hologram,
            ),
        ):
            playback = precompute_hologram_playback(trajectory)

        compute.assert_called_once_with(levels)
        self.assertFalse(playback.static_compacted)
        self.assertEqual(playback.source_frame_count, 20)
        self.assertEqual(playback.message_geometry_count, 20)
        self.assertEqual(playback.message_loops, 1)
        self.assertEqual(playback.distinct_position_count, 3)
        self.assertEqual(playback.holograms[4:8], ["level1"] * 4)
        self.assertEqual(playback.holograms[12:16], ["level2"] * 4)

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


class AutomaticCaptureRetryTests(unittest.TestCase):
    """Unattended re-recording of failed capture attempts inside run_recording."""

    MARKER = {
        "hardware_synchronized": True,
        "pc_start_perf_ns": 1,
        "capture_start_ts_us": 0,
        "camera_ts_at_marker_us": 0,
    }
    LED_OK = {
        "hardware_synchronized": True,
        "sync_t_recording_sec": 0.5,
        "report_json": "",
    }

    def _record(
        self,
        root: Path,
        *,
        retries: int | None,
        recorder_exit_codes: list[int] | None = None,
        marker_results: list[object] | None = None,
        led_results: list[object] | None = None,
        input_answers: list[str] | None = None,
        reference_positions: list[tuple[float, float, float]] | None = None,
        recorder_hangs: int = 0,
        extra_argv: list[str] | None = None,
    ):
        output_root = root / "records"
        args = recording_core.build_recording_parser().parse_args(
            ["--output-dir", str(output_root), "--no-preview", *(extra_argv or [])]
        )
        trajectory = PreparedTrajectory(
            shape_name="step_response_identification",
            positions=[(0.0, 0.0, 0.0), (0.0004, 0.0, 0.0), (0.0, 0.0, 0.0)],
            n_steps=3,
            frequency_hz=1.0,
            loops=1,
            rate=SimpleNamespace(requested_hz=1000, effective_hz=1000.0),
            parameters={},
            closed_cycle=False,
            stats={},
            run_label="retry_test",
            reference_positions=reference_positions,
        )
        session = RecordingHardwareSession(
            lev=mock.Mock(),
            current_pos=(0.0, 0.0, 0.0),
            current_hologram=mock.sentinel.centre_hologram,
        )
        exit_codes = list(recorder_exit_codes or [0])
        processes: list[mock.Mock] = []
        hangs_left = [int(recorder_hangs)]

        def fake_popen(command, *_args, **_kwargs):
            run_dir = Path(command[command.index("--run-dir") + 1])
            (run_dir / "left").mkdir(parents=True)
            process = mock.Mock()
            processes.append(process)
            if hangs_left[0] > 0:
                # A recorder whose camera stream stalled: it never exits on its own and
                # only returns from wait() after the parent terminates it.
                hangs_left[0] -= 1
                state = {"terminated": False}

                def wait(timeout=None):
                    if not state["terminated"]:
                        raise recording_core.subprocess.TimeoutExpired(command, timeout)
                    return -15

                def terminate():
                    state["terminated"] = True

                process.returncode = None
                process.wait.side_effect = wait
                process.terminate.side_effect = terminate
                process.kill.side_effect = terminate
                process.poll.side_effect = lambda: -15 if state["terminated"] else None
                return process
            (run_dir / "left" / "left_events.npz").write_bytes(b"events")
            code = exit_codes.pop(0) if exit_codes else 0
            process.returncode = code
            process.wait.return_value = code
            process.poll.return_value = code
            return process

        def fake_marker(process, *_args, **_kwargs):
            result = marker_results.pop(0) if marker_results else self.MARKER
            if isinstance(result, BaseException):
                process.poll.return_value = None  # recorder still armed
                raise result
            return result

        led_queue = list(led_results or [self.LED_OK])

        def fake_led(*_args, **_kwargs):
            result = led_queue.pop(0) if led_queue else self.LED_OK
            if isinstance(result, BaseException):
                raise result
            return dict(result)

        answers = list(input_answers or [])

        def fake_input(*_args, **_kwargs):
            if not answers:
                raise AssertionError("run_recording prompted unexpectedly")
            return answers.pop(0)

        moves: list[str] = []

        def fake_move(_lev, _start, end, label, drive_amplitude_scale=1.0):
            moves.append(label)
            return end

        detect = mock.Mock(side_effect=fake_led)
        ideal_writer = mock.Mock()
        compute = mock.Mock(
            side_effect=lambda positions: [f"h{i}" for i in range(len(positions))]
        )
        patches = mock.patch.multiple(
            recording_core,
            validate_calibration=mock.Mock(return_value=root / "calibration.npz"),
            print_trajectory_stats=mock.Mock(),
            save_trajectory_preview=mock.Mock(),
            compute_holograms_for_positions=compute,
            scale_hologram_drive_amplitude=mock.Mock(side_effect=lambda hologram, _s: hologram),
            prepare_message_from_holograms=mock.Mock(return_value=("phases", "amps", 3)),
            write_ideal_log=ideal_writer,
            move_static_position=mock.Mock(side_effect=fake_move),
            mute_sync_transducer=mock.Mock(side_effect=lambda hologram: hologram),
            wait_for_capture_marker=mock.Mock(side_effect=fake_marker),
            detect_led_sync_npz=detect,
        )
        with (
            patches,
            mock.patch.object(recording_core.subprocess, "Popen", side_effect=fake_popen),
            mock.patch.object(recording_core.time, "sleep"),
            mock.patch("builtins.input", side_effect=fake_input),
        ):
            try:
                outcome = recording_core.run_recording(
                    args,
                    prepared_trajectory=trajectory,
                    prompt_before_capture=False,
                    return_to_centre=True,
                    hardware_session=session,
                    automatic_capture_retries=retries,
                )
            except Exception as exc:  # returned for assertions
                outcome = exc
        return SimpleNamespace(
            outcome=outcome,
            output_root=output_root,
            processes=processes,
            moves=moves,
            detect=detect,
            compute=compute,
            ideal_writer=ideal_writer,
            answers_left=answers,
            session=session,
        )

    def _manifest(self, run_dir: Path) -> dict:
        return json.loads((run_dir / "pipeline_manifest.json").read_text(encoding="utf-8"))

    def test_led_failure_is_re_recorded_without_prompt(self) -> None:
        with TemporaryDirectory() as temporary:
            result = self._record(
                Path(temporary),
                retries=2,
                led_results=[RuntimeError("no LED onset"), self.LED_OK],
            )
            code, run_dir = result.outcome
            self.assertEqual(code, 0)
            self.assertEqual(len(result.processes), 2)
            self.assertEqual(result.detect.call_count, 2)
            self.assertIn("Returning smoothly to trajectory start for retry", result.moves)
            self.assertEqual(sorted(p.name for p in run_dir.glob("_capture_attempt_*")), [])
            self.assertTrue((run_dir / "stereo_recording" / "left" / "left_events.npz").is_file())
            manifest = self._manifest(run_dir)
            self.assertEqual(manifest["successful_capture_attempt"], 2)
            self.assertEqual(manifest["capture_retry"]["mode"], "automatic")
            self.assertEqual(manifest["capture_retry"]["automatic_retry_limit"], 2)
            self.assertEqual(len(manifest["capture_retry"]["automatic_failures"]), 1)
            self.assertIn("no LED onset", manifest["capture_retry"]["automatic_failures"][0])

    def test_recorder_failure_is_re_recorded(self) -> None:
        with TemporaryDirectory() as temporary:
            result = self._record(Path(temporary), retries=2, recorder_exit_codes=[1, 0])
            code, run_dir = result.outcome
            self.assertEqual(code, 0)
            self.assertEqual(len(result.processes), 2)
            self.assertEqual(result.detect.call_count, 1)
            manifest = self._manifest(run_dir)
            self.assertEqual(manifest["successful_capture_attempt"], 2)
            self.assertIn(
                "Stereo recording failed with code 1",
                manifest["capture_retry"]["automatic_failures"][0],
            )
            self.assertFalse(list(run_dir.glob("_capture_attempt_*")))

    def test_camera_start_timeout_terminates_recorder_and_re_records(self) -> None:
        with TemporaryDirectory() as temporary:
            result = self._record(
                Path(temporary),
                retries=1,
                marker_results=[TimeoutError("no synchronized start"), self.MARKER],
            )
            code, run_dir = result.outcome
            self.assertEqual(code, 0)
            self.assertEqual(len(result.processes), 2)
            result.processes[0].terminate.assert_called_once()
            result.processes[1].terminate.assert_not_called()
            self.assertEqual(self._manifest(run_dir)["successful_capture_attempt"], 2)

    def test_exhausted_led_retries_delete_the_run_and_return_2(self) -> None:
        with TemporaryDirectory() as temporary:
            failure = RuntimeError("no LED onset")
            result = self._record(
                Path(temporary), retries=1, led_results=[failure, failure, failure]
            )
            self.assertEqual(result.outcome, (2, None))
            self.assertEqual(result.detect.call_count, 2)
            self.assertEqual(list(result.output_root.iterdir()), [])
            self.assertEqual(result.answers_left, [])

    def test_exhausted_recorder_retries_raise_and_delete_the_run(self) -> None:
        with TemporaryDirectory() as temporary:
            result = self._record(Path(temporary), retries=0, recorder_exit_codes=[1])
            self.assertIsInstance(result.outcome, RuntimeError)
            self.assertIn("Stereo recording failed with code 1", str(result.outcome))
            self.assertEqual(len(result.processes), 1)
            self.assertEqual(list(result.output_root.iterdir()), [])

    def test_hung_recorder_is_terminated_and_re_recorded_when_unattended(self) -> None:
        with TemporaryDirectory() as temporary:
            result = self._record(Path(temporary), retries=2, recorder_hangs=1)
            code, run_dir = result.outcome
            self.assertEqual(code, 0)
            self.assertEqual(len(result.processes), 2)
            hung, recorded = result.processes
            hung.terminate.assert_called_once()
            recorded.terminate.assert_not_called()
            manifest = self._manifest(run_dir)
            self.assertEqual(manifest["successful_capture_attempt"], 2)
            retry = manifest["capture_retry"]
            self.assertEqual(retry["mode"], "automatic")
            self.assertEqual(len(retry["automatic_failures"]), 1)
            self.assertIn("timed out", retry["automatic_failures"][0])
            self.assertFalse((run_dir / "_capture_attempt_01").exists())
            self.assertIn("Returning smoothly to trajectory start for retry", result.moves)

    def test_declined_re_record_after_a_hung_recorder_stops_the_session_and_deletes_the_run(self) -> None:
        with TemporaryDirectory() as temporary:
            result = self._record(
                Path(temporary), retries=None, recorder_hangs=1, input_answers=["n"]
            )
            self.assertIsInstance(result.outcome, recording_core.subprocess.TimeoutExpired)
            self.assertEqual(len(result.processes), 1)
            result.processes[0].terminate.assert_called()
            self.assertEqual(result.answers_left, [])
            self.assertEqual(list(result.output_root.glob("*")), [])

    def test_interactive_operator_can_re_record_after_a_hung_recorder(self) -> None:
        with TemporaryDirectory() as temporary:
            # An empty answer is the default "yes", exactly like the LED re-record prompt.
            result = self._record(
                Path(temporary), retries=None, recorder_hangs=1, input_answers=[""]
            )
            code, run_dir = result.outcome
            self.assertEqual(code, 0)
            self.assertEqual(len(result.processes), 2)
            result.processes[0].terminate.assert_called_once()
            manifest = self._manifest(run_dir)
            self.assertEqual(manifest["successful_capture_attempt"], 2)
            retry = manifest["capture_retry"]
            self.assertEqual(retry["mode"], "interactive")
            self.assertEqual(retry["automatic_failures"], [])
            self.assertEqual(len(retry["interactive_failures"]), 1)
            self.assertIn("timed out", retry["interactive_failures"][0])
            self.assertFalse((run_dir / "_capture_attempt_01").exists())
            self.assertIn("Returning smoothly to trajectory start for retry", result.moves)

    def test_interactive_operator_can_re_record_after_a_recorder_error_or_failed_start(self) -> None:
        with TemporaryDirectory() as temporary:
            failed = self._record(
                Path(temporary) / "exit", retries=None, recorder_exit_codes=[1, 0], input_answers=["y"]
            )
            code, run_dir = failed.outcome
            self.assertEqual(code, 0)
            self.assertEqual(len(failed.processes), 2)
            self.assertIn(
                "Stereo recording failed with code 1",
                self._manifest(run_dir)["capture_retry"]["interactive_failures"][0],
            )
            not_started = self._record(
                Path(temporary) / "marker",
                retries=None,
                marker_results=[TimeoutError("no synchronized start"), self.MARKER],
                input_answers=["y"],
            )
            code, run_dir = not_started.outcome
            self.assertEqual(code, 0)
            self.assertEqual(len(not_started.processes), 2)
            not_started.processes[0].terminate.assert_called_once()
            self.assertIn(
                "did not start",
                self._manifest(run_dir)["capture_retry"]["interactive_failures"][0],
            )

    def test_led_detection_receives_the_predicted_onset_tolerance_and_margin(self) -> None:
        with TemporaryDirectory() as temporary:
            result = self._record(Path(temporary) / "default", retries=None)
            code, run_dir = result.outcome
            self.assertEqual(code, 0)
            kwargs = result.detect.call_args.kwargs
            # Default: the detector never rejects on these grounds (acceptance as before
            # 2026-09-18); it only receives the prediction so that it can report the residual.
            self.assertEqual(kwargs["onset_tolerance_sec"], 0.0)
            self.assertEqual(kwargs["min_peak_to_threshold_ratio"], 1.0)
            # predicted onset = PC estimate of the send call + measured send overhead (>= 0)
            self.assertGreaterEqual(
                kwargs["expected_onset_t_recording_sec"], kwargs["expected_t_recording_sec"]
            )
            led_settings = self._manifest(run_dir)["pat_start_led"]
            self.assertEqual(led_settings["onset_tolerance_sec"], 0.0)
            self.assertEqual(led_settings["min_peak_to_threshold_ratio"], 1.0)
            self.assertFalse(led_settings["consistency"]["suspicious"])
            strict = self._record(
                Path(temporary) / "strict",
                retries=None,
                extra_argv=[
                    "--pat-start-led-onset-tolerance-sec", "0.1",
                    "--pat-start-led-min-peak-ratio", "1.5",
                ],
            )
            kwargs = strict.detect.call_args.kwargs
            self.assertEqual(kwargs["onset_tolerance_sec"], 0.1)
            self.assertEqual(kwargs["min_peak_to_threshold_ratio"], 1.5)

    def test_inconsistent_led_detection_is_kept_but_flagged(self) -> None:
        # The numbers of the false detection recorded at 21:21 on 2026-09-18.
        suspicious = dict(
            self.LED_OK,
            sync_t_recording_sec=0.295,
            onset_residual_sec=-1.3745,
            peak_to_threshold_ratio=1.0,
        )
        with TemporaryDirectory() as temporary:
            result = self._record(Path(temporary), retries=None, led_results=[suspicious])
            code, run_dir = result.outcome
            self.assertEqual(code, 0)
            self.assertEqual(len(result.processes), 1)
            consistency = self._manifest(run_dir)["pat_start_led"]["consistency"]
            self.assertTrue(consistency["suspicious"])
            self.assertEqual(len(consistency["warnings"]), 2)
            self.assertIn("-1374.5 ms", consistency["warnings"][0])
            self.assertIn("1.00x the threshold", consistency["warnings"][1])
            timing = json.loads((run_dir / "pat_camera_timing.json").read_text(encoding="utf-8"))
            self.assertTrue(timing["pat_start_led_consistency"]["suspicious"])
            report = result.session.last_capture_report
            self.assertEqual(report["failures"], [])
            self.assertEqual(len(report["led_warnings"]), 2)
        self.assertEqual(recording_core.led_consistency_warnings(None), [])
        self.assertEqual(
            recording_core.led_consistency_warnings(
                {"onset_residual_sec": 0.0077, "peak_to_threshold_ratio": 3.2}
            ),
            [],
        )
        # The -55 ms outlier among the genuine detections stays inside the advisory limit.
        self.assertEqual(
            recording_core.led_consistency_warnings(
                {"onset_residual_sec": -0.0551, "peak_to_threshold_ratio": 9.6}
            ),
            [],
        )

    def test_failure_reasons_survive_the_deleted_run_in_the_session_report(self) -> None:
        with TemporaryDirectory() as temporary:
            result = self._record(
                Path(temporary),
                retries=None,
                led_results=[RuntimeError("no LED onset in the ROI")],
                input_answers=["n"],
            )
            self.assertEqual(result.outcome, (2, None))
            report = result.session.last_capture_report
            self.assertEqual(len(report["failures"]), 1)
            self.assertIn("LED detection failed: no LED onset in the ROI", report["failures"][0])
            hung = self._record(
                Path(temporary) / "hang", retries=None, recorder_hangs=1, input_answers=["n"]
            )
            self.assertIn("timed out", hung.session.last_capture_report["failures"][0])

    def test_recorder_gets_the_stall_watchdog_and_the_parent_outwaits_its_deadline(self) -> None:
        with TemporaryDirectory() as temporary:
            with mock.patch.object(
                recording_core.subprocess, "list2cmdline", side_effect=lambda command: " ".join(command)
            ) as cmdline:
                result = self._record(Path(temporary), retries=None)
            code, run_dir = result.outcome
            self.assertEqual(code, 0)
            # The first command printed is the stereo recorder (the last is the deferred postprocess).
            command = cmdline.call_args_list[0].args[0]
            self.assertIn("stereo_eventcam_record_sync.py", command[1])
            self.assertEqual(command[command.index("--stream-stall-timeout-sec") + 1], "3")
            wait_timeout = result.processes[0].wait.call_args.kwargs["timeout"]
            self.assertGreaterEqual(wait_timeout, 60.0)
            self.assertEqual(self._manifest(run_dir)["camera_stall_timeout_sec"], 3.0)
        # Long recordings keep the old "duration + 20 s" headroom for saving large NPZ files.
        self.assertEqual(recording_core.compute_recorder_wait_timeout_sec(13.5, 0.5, 5.0), 60.5)
        self.assertEqual(recording_core.compute_recorder_wait_timeout_sec(13.5, 0.5, 2.0), 60.0)
        self.assertEqual(recording_core.compute_recorder_wait_timeout_sec(80.0, 0.5, 2.0), 100.0)

    def test_invalid_led_and_watchdog_settings_are_rejected_before_hardware_use(self) -> None:
        for argv in (
            ["--pat-start-led-onset-tolerance-sec", "-0.1"],
            ["--pat-start-led-min-peak-ratio", "0.9"],
            ["--camera-stall-timeout-sec", "-1"],
        ):
            with self.subTest(argv=argv), TemporaryDirectory() as temporary:
                # SystemExit is raised during argument validation, before any recorder starts.
                with self.assertRaises(SystemExit):
                    self._record(Path(temporary), retries=None, extra_argv=argv)

    def test_interactive_mode_still_asks_after_led_failure(self) -> None:
        with TemporaryDirectory() as temporary:
            result = self._record(
                Path(temporary),
                retries=None,
                led_results=[RuntimeError("no LED onset")],
                input_answers=["n"],
            )
            self.assertEqual(result.outcome, (2, None))
            self.assertEqual(result.answers_left, [])
            self.assertEqual(len(result.processes), 1)

    def test_interactive_mode_still_raises_on_recorder_failure_when_re_record_is_declined(self) -> None:
        with TemporaryDirectory() as temporary:
            result = self._record(
                Path(temporary), retries=None, recorder_exit_codes=[1], input_answers=["n"]
            )
            self.assertIsInstance(result.outcome, RuntimeError)
            self.assertEqual(len(result.processes), 1)
            self.assertEqual(result.answers_left, [])

    def test_playback_solves_each_distinct_position_once(self) -> None:
        with TemporaryDirectory() as temporary:
            result = self._record(Path(temporary), retries=None)
            code, run_dir = result.outcome
            self.assertEqual(code, 0)
            playback_call = result.compute.call_args_list[0]
            self.assertEqual(
                playback_call.args[0], [(0.0, 0.0, 0.0), (0.0004, 0.0, 0.0)]
            )
            playback = self._manifest(run_dir)["hologram_playback"]
            self.assertEqual(playback["source_frame_count"], 3)
            self.assertEqual(playback["distinct_position_count"], 2)
            self.assertIsInstance(playback["hologram_solve_seconds"], float)

    def test_reference_trajectory_is_logged_beside_the_command(self) -> None:
        reference = [(0.0, 0.0, 0.0), (0.0003, 0.0, 0.0), (0.0, 0.0, 0.0)]
        with TemporaryDirectory() as temporary:
            result = self._record(
                Path(temporary), retries=None, reference_positions=reference
            )
            code, run_dir = result.outcome
            self.assertEqual(code, 0)
            calls = result.ideal_writer.call_args_list
            self.assertEqual(len(calls), 2)
            ideal_path, ideal_positions, ideal_fps = calls[0].args
            reference_path, reference_positions, reference_fps = calls[1].args
            self.assertTrue(ideal_path.endswith("_ideal_log.csv"))
            self.assertTrue(reference_path.endswith("_reference_log.csv"))
            # PAT plays the command; the reference is only written to disk.
            self.assertEqual(ideal_positions[1], (0.0004, 0.0, 0.0))
            self.assertEqual(reference_positions, reference)
            self.assertEqual(ideal_fps, reference_fps)
            manifest = self._manifest(run_dir)
            self.assertEqual(manifest["reference_log"], reference_path)
            self.assertIn("desired", manifest["reference_log_note"])
            self.assertEqual(
                result.compute.call_args_list[0].args[0],
                [(0.0, 0.0, 0.0), (0.0004, 0.0, 0.0)],
            )

    def test_runs_without_a_reference_write_only_the_ideal_log(self) -> None:
        with TemporaryDirectory() as temporary:
            result = self._record(Path(temporary), retries=None)
            code, run_dir = result.outcome
            self.assertEqual(code, 0)
            self.assertEqual(result.ideal_writer.call_count, 1)
            manifest = self._manifest(run_dir)
            self.assertEqual(manifest["reference_log"], "")
            self.assertEqual(manifest["reference_log_note"], "")

    def test_reference_length_mismatch_is_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            result = self._record(
                Path(temporary), retries=None, reference_positions=[(0.0, 0.0, 0.0)]
            )
            self.assertIsInstance(result.outcome, ValueError)
            self.assertIn("reference_positions", str(result.outcome))

    def test_invalid_retry_limits_are_rejected_before_hardware_use(self) -> None:
        for value in (-1, True, 1.5):
            with self.subTest(value=value), TemporaryDirectory() as temporary:
                result = self._record(Path(temporary), retries=value)
                self.assertIsInstance(result.outcome, ValueError)
                self.assertEqual(result.processes, [])
                result.session.lev.levitate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
