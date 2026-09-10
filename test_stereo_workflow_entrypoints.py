#!/usr/bin/env python3
"""Architecture tests for recording, postprocess, and full pipeline entry points."""

from pathlib import Path
from unittest import mock
import unittest

import acoustools_stereo_eventcam_3d_recording as interactive_recording
import acoustools_stereo_eventcam_3d_pipeline as pipeline
from acoustools_eventcam_sync import MODE_SHAPE_NAMES, compute_pat_frame_rate_info
from acoustools_multitraj_no_eventcam import TrajectoryParameters
from stereo_acoustools_3d_recording_core import prepare_parametric_trajectory
from stereo_acoustools_3d_postprocess import parse_args as parse_postprocess_args


class StereoWorkflowEntrypointTests(unittest.TestCase):
    def test_all_legacy_planar_and_existing_closed_3d_modes_prepare(self) -> None:
        centre = (0.001, -0.002, 0.003)
        params = TrajectoryParameters(
            amp_x_mm=2.5,
            amp_y_mm=2.0,
            amp_z_mm=1.5,
            amp_scale_mm=1.0,
            helix_minor_radius_mm=0.5,
            helix_turns=3,
        )
        rate = compute_pat_frame_rate_info(400, 2.0)
        self.assertTrue(rate.is_supported)

        for mode in [*range(1, 11), *range(12, 16)]:
            with self.subTest(mode=mode):
                trajectory = prepare_parametric_trajectory(
                    mode,
                    centre,
                    params,
                    (400, 2.0, 3, rate),
                )
                self.assertEqual(trajectory.shape_name, MODE_SHAPE_NAMES[mode])
                self.assertEqual(trajectory.parameters["generator_mode"], mode)
                self.assertEqual(trajectory.parameters["requested_loops"], 3)
                if mode == 8:
                    self.assertFalse(trajectory.closed_cycle)
                    self.assertEqual(trajectory.loops, 1)
                    self.assertEqual(len(trajectory.positions), 1200)
                    self.assertEqual(trajectory.positions[-1], trajectory.positions[399])
                    self.assertNotEqual(trajectory.positions[0], trajectory.positions[399])
                else:
                    self.assertTrue(trajectory.closed_cycle)
                    self.assertEqual(trajectory.loops, 3)
                    self.assertEqual(len(trajectory.positions), 400)
                if mode <= 10:
                    self.assertTrue(all(point[1] == centre[1] for point in trajectory.positions))

    def test_interactive_recording_defaults_to_multi_trajectory_loop(self) -> None:
        args = interactive_recording.parse_args([])
        self.assertFalse(args.single_run)

    def test_interactive_recording_reuses_one_hardware_session(self) -> None:
        args = mock.Mock(single_run=False)
        session = mock.sentinel.hardware_session
        with (
            mock.patch.object(interactive_recording, "parse_args", return_value=args),
            mock.patch.object(
                interactive_recording,
                "open_recording_hardware_session",
                return_value=session,
            ) as open_hardware,
            mock.patch.object(
                interactive_recording,
                "run_recording",
                side_effect=[(0, Path("run_a")), (0, Path("run_b"))],
            ) as record,
            mock.patch.object(
                interactive_recording,
                "prompt_record_another",
                side_effect=[True, False],
            ),
            mock.patch.object(
                interactive_recording,
                "shutdown_recording_hardware_session",
            ) as shutdown_hardware,
        ):
            result = interactive_recording.main([])

        self.assertEqual(result, 0)
        self.assertEqual(record.call_count, 2)
        open_hardware.assert_called_once_with()
        shutdown_hardware.assert_called_once_with(session)
        for call in record.call_args_list:
            self.assertIs(call.kwargs["hardware_session"], session)
        self.assertEqual(args.entry_script, "acoustools_stereo_eventcam_3d_recording.py")

    def test_interactive_single_run_preserves_previous_entry_behavior(self) -> None:
        args = mock.Mock(single_run=True)
        run_dir = Path("run_a")
        with (
            mock.patch.object(interactive_recording, "parse_args", return_value=args),
            mock.patch.object(
                interactive_recording,
                "run_recording",
                return_value=(0, run_dir),
            ) as record,
            mock.patch.object(
                interactive_recording,
                "open_recording_hardware_session",
            ) as open_hardware,
            mock.patch.object(
                interactive_recording,
                "shutdown_recording_hardware_session",
            ) as shutdown_hardware,
        ):
            result = interactive_recording.main([])

        self.assertEqual(result, 0)
        record.assert_called_once_with(args)
        open_hardware.assert_not_called()
        shutdown_hardware.assert_not_called()

    def test_full_pipeline_hands_successful_recording_to_postprocess(self) -> None:
        run_dir = Path("synthetic_run").resolve()
        with (
            mock.patch.object(pipeline, "run_recording", return_value=(0, run_dir)) as record,
            mock.patch.object(pipeline, "run_postprocess", return_value=0) as postprocess,
        ):
            result = pipeline.main([])

        self.assertEqual(result, 0)
        self.assertTrue(record.call_args.kwargs["postprocess_will_follow"])
        postprocess.assert_called_once_with(run_dir)

    def test_failed_recording_is_not_postprocessed(self) -> None:
        with (
            mock.patch.object(pipeline, "run_recording", return_value=(2, None)),
            mock.patch.object(pipeline, "run_postprocess") as postprocess,
        ):
            result = pipeline.main([])

        self.assertEqual(result, 2)
        postprocess.assert_not_called()

    def test_legacy_process_only_routes_directly_to_postprocess_core(self) -> None:
        run_dir = Path("existing_run").resolve()
        with mock.patch.object(pipeline, "run_postprocess", return_value=0) as postprocess:
            result = pipeline.main(["--process-only", str(run_dir), "--window-us", "300"])

        self.assertEqual(result, 0)
        postprocess.assert_called_once_with(run_dir, {"window_us": 300})

    def test_recording_and_postprocess_cores_do_not_depend_on_pipeline(self) -> None:
        root = Path(__file__).resolve().parent
        recording_source = (root / "stereo_acoustools_3d_recording_core.py").read_text(encoding="utf-8")
        postprocess_source = (root / "stereo_acoustools_3d_postprocess_core.py").read_text(encoding="utf-8")
        postprocess_entry_source = (root / "stereo_acoustools_3d_postprocess.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("acoustools_stereo_eventcam_3d_pipeline", recording_source)
        self.assertNotIn("acoustools_stereo_eventcam_3d_pipeline", postprocess_source)
        self.assertNotIn("acoustools_stereo_eventcam_3d_pipeline", postprocess_entry_source)

    def test_postprocess_entry_accepts_overrides_without_pipeline_args(self) -> None:
        args = parse_postprocess_args(["run_a", "--window-us", "300", "--render-overlay"])
        self.assertEqual(args.run_dirs, [Path("run_a")])
        self.assertEqual(args.window_us, 300)
        self.assertTrue(args.render_overlay)


if __name__ == "__main__":
    unittest.main()
