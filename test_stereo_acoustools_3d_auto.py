#!/usr/bin/env python3
"""Tests for JSON-driven stereo 3D recording and deferred monitor orchestration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
import unittest

import acoustools_stereo_eventcam_3d_recording_auto as auto_record
import stereo_acoustools_3d_auto_monitor as monitor
from acoustools_cusped3d import Cusped3DParameters, generate_cusped_3d_positions
from stereo_acoustools_3d_auto_common import (
    load_auto_conditions,
    prepare_auto_trajectory,
    select_conditions,
)
from acoustools_random3d_no_eventcam import (
    Chirped3DParameters,
    ExtendedRandom3DParameters,
    generate_chirped_3d_positions,
    generate_extended_random_3d_positions,
)
from acoustools_eventcam_sync import should_report_hologram_progress


class StereoAutoTrajectoryTests(unittest.TestCase):
    def test_hologram_progress_uses_large_job_10000_point_checkpoints(self) -> None:
        self.assertFalse(should_report_hologram_progress(10_000, 10_000))
        self.assertFalse(should_report_hologram_progress(1, 100_000))
        self.assertFalse(should_report_hologram_progress(9_999, 100_000))
        self.assertTrue(should_report_hologram_progress(10_000, 100_000))
        self.assertTrue(should_report_hologram_progress(90_000, 100_000))
        self.assertTrue(should_report_hologram_progress(100_000, 100_000))
        self.assertFalse(should_report_hologram_progress(100_001, 100_001))

    def test_initial_json_resolves_eight_deterministic_3d_conditions(self) -> None:
        json_path, conditions = load_auto_conditions("long_random_3d_patterns_initial.json")

        self.assertTrue(json_path.is_absolute())
        self.assertEqual(len(conditions), 8)
        trajectory_a = prepare_auto_trajectory(conditions[0])
        trajectory_b = prepare_auto_trajectory(conditions[0])
        self.assertEqual(trajectory_a.positions, trajectory_b.positions)
        self.assertEqual(len(trajectory_a.positions), 80000)
        self.assertEqual(trajectory_a.rate.requested_hz, 10000)
        self.assertGreater(max(point[1] for point in trajectory_a.positions), 0.0)
        self.assertIn("label", trajectory_a.parameters)
        for condition in conditions:
            trajectory = prepare_auto_trajectory(condition)
            self.assertEqual(trajectory.rate.requested_hz, 10000)
            self.assertLessEqual(
                max(abs(value) for point in trajectory.positions for value in point),
                0.030000001,
            )

    def test_selection_uses_source_json_index_and_label(self) -> None:
        _, conditions = load_auto_conditions("long_random_3d_patterns_initial.json")
        selected = select_conditions(
            conditions,
            start_index=2,
            limit=1,
            labels=["depth_rich_10s_seed2303"],
        )
        self.assertEqual([condition.json_index for condition in selected], [2])

    def test_selection_can_filter_dataset_role(self) -> None:
        _, conditions = load_auto_conditions("extended_3d_dataset_plan.json")
        selected = select_conditions(conditions, roles=["validation"])
        self.assertTrue(selected)
        self.assertTrue(all(condition.role == "validation" for condition in selected))

    def test_unified_include_plan_preserves_global_and_source_indices(self) -> None:
        source_path, conditions = load_auto_conditions("all_additional_3d_dataset_plan.json")

        self.assertTrue(source_path.is_absolute())
        self.assertEqual(len(conditions), 48)
        self.assertEqual([condition.json_index for condition in conditions], list(range(48)))
        self.assertEqual(conditions[0].plan_group, "01_extended")
        self.assertEqual(conditions[0].source_json_index, 0)
        self.assertTrue(conditions[0].source_plan.endswith("extended_3d_dataset_plan.json"))
        self.assertEqual(conditions[40].plan_group, "89_cusped_retention_boundary")
        self.assertEqual(conditions[44].plan_group, "90_chirped_retention_boundary")
        self.assertEqual(conditions[-1].label, "boundary04_abrupt_15_180hz_amp3")
        self.assertTrue(all(
            condition.risk_level == "retention_boundary" for condition in conditions[40:]
        ))

    def test_low_risk_wide_midband_conditions_reach_requested_range(self) -> None:
        _, conditions = load_auto_conditions("extended_3d_dataset_plan.json")
        wide = [condition for condition in conditions if "_wide1p5_" in condition.label]

        self.assertEqual(len(wide), 4)
        self.assertTrue(all(condition.role == "train" for condition in wide))
        self.assertTrue(all(condition.risk_level == "standard" for condition in wide))
        for condition in wide:
            trajectory = prepare_auto_trajectory(condition)
            peak_mm = max(
                abs(value) * 1e3 for point in trajectory.positions for value in point
            )
            self.assertGreaterEqual(peak_mm, 1.49)
            self.assertLessEqual(peak_mm, 1.500001)
            self.assertAlmostEqual(
                trajectory.stats["amplitude_scale_applied"], 1.0, places=9
            )
            self.assertLessEqual(trajectory.stats["max_accel_mm_s2"], 65000.000001)

    def test_unknown_trajectory_parameter_is_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "invalid.json"
            path.write_text(
                json.dumps([
                    {"shape": "Long_Random_3D", "params": {"label": "bad", "bogus": 1}}
                ]),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unknown parameters"):
                load_auto_conditions(path)

    def test_extended_multiband_trajectory_is_deterministic_and_limited(self) -> None:
        params = ExtendedRandom3DParameters(
            duration_sec=0.2,
            sample_hz=1000.0,
            x_limit_mm=2.0,
            y_limit_mm=1.5,
            z_limit_mm=1.0,
            max_speed_mm_s=200.0,
            max_accel_mm_s2=5000.0,
        )
        positions_a, stats_a = generate_extended_random_3d_positions(params)
        positions_b, stats_b = generate_extended_random_3d_positions(params)
        self.assertEqual(positions_a, positions_b)
        self.assertEqual(len(positions_a), 200)
        self.assertLessEqual(stats_a["max_speed_mm_s"], 200.000001)
        self.assertLessEqual(stats_a["max_accel_mm_s2"], 5000.000001)
        self.assertEqual(stats_a["enabled_bands"], stats_b["enabled_bands"])

    def test_chirped_3d_is_deterministic_and_preserves_dynamic_limits(self) -> None:
        params = Chirped3DParameters(
            duration_sec=0.4,
            sample_hz=1000.0,
            x_amp_mm=1.0,
            y_amp_mm=0.8,
            z_amp_mm=0.6,
            x_f_start_hz=5.0,
            x_f_end_hz=80.0,
            y_f_start_hz=70.0,
            y_f_end_hz=4.0,
            z_f_start_hz=8.0,
            z_f_end_hz=90.0,
            sweep_law="logarithmic",
            max_speed_mm_s=150.0,
            max_accel_mm_s2=20000.0,
        )
        positions_a, stats_a = generate_chirped_3d_positions(params)
        positions_b, stats_b = generate_chirped_3d_positions(params)

        self.assertEqual(positions_a, positions_b)
        self.assertEqual(len(positions_a), 400)
        self.assertLessEqual(stats_a["max_speed_mm_s"], 150.000001)
        self.assertLessEqual(stats_a["max_accel_mm_s2"], 20000.000001)
        self.assertEqual(stats_a["sweep_law"], "logarithmic")
        self.assertEqual(stats_a["axis_chirps"], stats_b["axis_chirps"])

    def test_chirped_dataset_plans_resolve_roles_and_risk_levels(self) -> None:
        _, normal = load_auto_conditions("chirped_3d_dataset_plan.json")
        _, boundary = load_auto_conditions("chirped_3d_retention_boundary_plan.json")

        self.assertEqual(len(normal), 9)
        self.assertEqual(len(boundary), 4)
        self.assertTrue(all(item.trajectory_kind == "chirped_3d" for item in normal + boundary))
        self.assertEqual({item.role for item in normal}, {"train", "validation"})
        self.assertTrue(all(item.risk_level == "retention_boundary" for item in boundary))
        for condition in normal + boundary:
            trajectory = prepare_auto_trajectory(condition)
            self.assertEqual(trajectory.shape_name, "chirped_3d")
            self.assertLessEqual(trajectory.stats["amplitude_scale_applied"], 1.0)

    def test_exact_cusp_and_rounded_control_have_distinct_cusp_speed(self) -> None:
        sharp_params = Cusped3DParameters(
            curve="cardioid",
            plane="xz",
            out_of_plane_amp_mm=0.0,
            rounding=0.0,
            steps_per_cycle=1000,
            frequency_hz=4.0,
        )
        rounded_params = Cusped3DParameters(
            curve="cardioid",
            plane="xz",
            out_of_plane_amp_mm=0.0,
            rounding=0.08,
            steps_per_cycle=1000,
            frequency_hz=4.0,
        )
        sharp_positions, sharp_stats = generate_cusped_3d_positions(sharp_params)
        rounded_positions, rounded_stats = generate_cusped_3d_positions(rounded_params)

        self.assertEqual(len(sharp_positions), 1000)
        self.assertEqual(sharp_stats["cusp_count"], 1)
        self.assertLess(sharp_stats["cusp_to_peak_speed_ratio"], 0.001)
        self.assertGreater(rounded_stats["cusp_to_peak_speed_ratio"], 0.02)
        self.assertNotEqual(sharp_positions, rounded_positions)

    def test_cusped_dataset_plans_resolve_closed_3d_curves(self) -> None:
        _, normal = load_auto_conditions("cusped_3d_dataset_plan.json")
        _, boundary = load_auto_conditions("cusped_3d_retention_boundary_plan.json")

        self.assertEqual(len(normal), 12)
        self.assertEqual(len(boundary), 4)
        self.assertTrue(all(item.trajectory_kind == "cusped_3d" for item in normal + boundary))
        self.assertEqual({item.role for item in normal}, {"train", "validation"})
        self.assertTrue(all(item.risk_level == "retention_boundary" for item in boundary))
        curves = set()
        for condition in normal + boundary:
            trajectory = prepare_auto_trajectory(condition)
            curves.add(trajectory.stats["curve"])
            self.assertTrue(trajectory.closed_cycle)
            self.assertEqual(trajectory.loops, condition.params.loops)
            self.assertLessEqual(trajectory.stats["amplitude_scale_applied"], 1.0)
        self.assertEqual(curves, {"cardioid", "nephroid", "deltoid", "astroid", "hypocycloid"})

    def test_retention_boundary_requires_dedicated_acknowledgement(self) -> None:
        with mock.patch.object(auto_record, "open_recording_hardware_session") as open_hardware:
            result = auto_record.main([
                "--json", "chirped_3d_retention_boundary_plan.json",
                "--limit", "1",
                "--no-preview",
                "--acknowledge-extended-trajectory-safety",
            ])

        self.assertEqual(result, 2)
        open_hardware.assert_not_called()

    def test_retention_boundary_requires_one_condition_and_confirmation(self) -> None:
        with mock.patch.object(auto_record, "open_recording_hardware_session") as open_hardware:
            result = auto_record.main([
                "--json", "cusped_3d_retention_boundary_plan.json",
                "--limit", "1",
                "--no-preview",
                "--acknowledge-extended-trajectory-safety",
                "--acknowledge-retention-boundary-risk",
            ])

        self.assertEqual(result, 2)
        open_hardware.assert_not_called()

    def test_retention_tail_forces_preview_and_confirmation_per_condition(self) -> None:
        with TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "records"
            hardware_session = mock.sentinel.hardware_session
            with (
                mock.patch.object(
                    auto_record,
                    "open_recording_hardware_session",
                    return_value=hardware_session,
                ) as open_hardware,
                mock.patch.object(
                    auto_record, "shutdown_recording_hardware_session"
                ) as shutdown_hardware,
                mock.patch.object(
                    auto_record,
                    "run_recording",
                    side_effect=[(0, output_root / "run46"), (0, output_root / "run47")],
                ) as run_recording,
            ):
                result = auto_record.main([
                    "--json", "all_additional_3d_dataset_plan.json",
                    "--start-index", "46",
                    "--no-preview",
                    "--output-dir", str(output_root),
                    "--acknowledge-extended-trajectory-safety",
                    "--acknowledge-retention-boundary-risk",
                    "--allow-retention-boundary-tail",
                ])

        self.assertEqual(result, 0)
        open_hardware.assert_called_once_with()
        shutdown_hardware.assert_called_once_with(hardware_session)
        self.assertEqual(run_recording.call_count, 2)
        for call in run_recording.call_args_list:
            self.assertFalse(call.args[0].no_preview)
            self.assertTrue(call.kwargs["prompt_before_capture"])

    def test_retention_tail_rejects_keep_going_before_hardware_open(self) -> None:
        with mock.patch.object(auto_record, "open_recording_hardware_session") as open_hardware:
            result = auto_record.main([
                "--json", "all_additional_3d_dataset_plan.json",
                "--start-index", "46",
                "--acknowledge-extended-trajectory-safety",
                "--acknowledge-retention-boundary-risk",
                "--allow-retention-boundary-tail",
                "--keep-going",
            ])

        self.assertEqual(result, 2)
        open_hardware.assert_not_called()

    def test_auto_json_accepts_extended_and_parametric_shapes(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "extended.json"
            path.write_text(json.dumps([
                {
                    "shape": "Extended_Random_3D",
                    "params": {
                        "label": "multiband",
                        "duration_sec": 0.2,
                        "sample_hz": 1000,
                        "x_limit_mm": 1,
                        "y_limit_mm": 1,
                        "z_limit_mm": 1,
                    },
                },
                {
                    "shape": "Heart",
                    "params": {
                        "label": "heart_variant",
                        "amp_scale_mm": 3,
                        "steps_per_cycle": 400,
                        "frequency_hz": 5,
                        "loops": 2,
                        "max_speed_mm_s": 500,
                        "max_accel_mm_s2": 50000,
                    },
                },
                {
                    "shape": "Lissajous_3D",
                    "params": {
                        "label": "lissajous_variant",
                        "amp_x_mm": 2,
                        "amp_y_mm": 1.5,
                        "amp_z_mm": 1,
                        "steps_per_cycle": 400,
                        "frequency_hz": 5,
                        "loops": 2,
                        "max_speed_mm_s": 500,
                        "max_accel_mm_s2": 50000,
                    },
                },
            ]), encoding="utf-8")
            _, conditions = load_auto_conditions(path)
            trajectories = [prepare_auto_trajectory(condition) for condition in conditions]

        self.assertEqual([condition.trajectory_kind for condition in conditions], [
            "extended_random_3d", "parametric", "parametric"
        ])
        self.assertEqual(trajectories[0].shape_name, "extended_random_3d")
        self.assertEqual(trajectories[1].shape_name, "heart_shape")
        self.assertEqual(trajectories[2].shape_name, "lissajous_3d")
        self.assertEqual(trajectories[1].loops, 2)
        heart_span = max(point[0] for point in trajectories[1].positions) - min(
            point[0] for point in trajectories[1].positions
        )
        self.assertAlmostEqual(heart_span, 0.006, places=6)

    def test_auto_entry_hands_prepared_trajectory_to_recording_core(self) -> None:
        with TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "records"
            synthetic_run = output_root / "synthetic_run"
            hardware_session = mock.sentinel.hardware_session
            with (
                mock.patch.object(
                    auto_record,
                    "open_recording_hardware_session",
                    return_value=hardware_session,
                ) as open_hardware,
                mock.patch.object(
                    auto_record, "shutdown_recording_hardware_session"
                ) as shutdown_hardware,
                mock.patch.object(
                    auto_record, "run_recording", return_value=(0, synthetic_run)
                ) as run_recording,
            ):
                result = auto_record.main([
                    "--limit", "1", "--no-preview", "--output-dir", str(output_root)
                ])

            self.assertEqual(result, 0)
            open_hardware.assert_called_once_with()
            shutdown_hardware.assert_called_once_with(hardware_session)
            kwargs = run_recording.call_args.kwargs
            self.assertEqual(kwargs["prepared_trajectory"].shape_name, "long_random_3d")
            self.assertFalse(kwargs["prompt_before_capture"])
            self.assertTrue(kwargs["return_to_centre"])
            self.assertEqual(kwargs["automation_metadata"]["json_index"], 0)
            self.assertIs(kwargs["hardware_session"], hardware_session)

    def test_all_auto_conditions_share_one_hardware_session(self) -> None:
        with TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "records"
            hardware_session = mock.sentinel.hardware_session
            run_dirs = [output_root / "run_0", output_root / "run_1"]
            with (
                mock.patch.object(
                    auto_record,
                    "open_recording_hardware_session",
                    return_value=hardware_session,
                ) as open_hardware,
                mock.patch.object(
                    auto_record, "shutdown_recording_hardware_session"
                ) as shutdown_hardware,
                mock.patch.object(
                    auto_record,
                    "run_recording",
                    side_effect=[(0, run_dirs[0]), (0, run_dirs[1])],
                ) as run_recording,
            ):
                result = auto_record.main([
                    "--limit", "2", "--no-preview", "--output-dir", str(output_root)
                ])

            self.assertEqual(result, 0)
            open_hardware.assert_called_once_with()
            shutdown_hardware.assert_called_once_with(hardware_session)
            self.assertEqual(run_recording.call_count, 2)
            for call in run_recording.call_args_list:
                self.assertIs(call.kwargs["hardware_session"], hardware_session)


class StereoAutoMonitorTests(unittest.TestCase):
    def _make_run(self, root: Path, status: str = "pending") -> Path:
        run_dir = root / "run_001"
        (run_dir / "stereo_recording" / "left").mkdir(parents=True)
        (run_dir / "stereo_recording" / "right").mkdir(parents=True)
        files = {
            run_dir / "capture_start_marker.json": "{}",
            run_dir / "pat_camera_timing.json": "{}",
            run_dir / "stereo_recording" / "stereo_recording_manifest.json": "{}",
        }
        for path, text in files.items():
            path.write_text(text, encoding="utf-8")
        (run_dir / "stereo_recording" / "left" / "left_events.npz").write_bytes(b"left")
        (run_dir / "stereo_recording" / "right" / "right_events.npz").write_bytes(b"right")
        (run_dir / "pipeline_manifest.json").write_text(
            json.dumps({
                "capture_complete": True,
                "processing_status": status,
                "automation": {"label": "condition_a"},
            }),
            encoding="utf-8",
        )
        return run_dir

    def test_monitor_processes_pending_manifest_once(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = self._make_run(root)
            args = argparse.Namespace(
                retry_failed=False,
                stable_sec=0.0,
                stale_lock_sec=3600.0,
                window_us=300,
            )
            log_path = root / "monitor.csv"
            with mock.patch.object(monitor, "run_postprocess", return_value=0) as postprocess:
                result, failed = monitor.process_manifest(
                    run_dir / "pipeline_manifest.json", args, log_path
                )

            self.assertEqual(result, "processed")
            self.assertFalse(failed)
            postprocess.assert_called_once_with(run_dir.resolve(), {"window_us": 300})
            self.assertTrue(log_path.exists())
            self.assertFalse((run_dir / monitor.LOCK_NAME).exists())

    def test_failed_run_requires_retry_flag(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = self._make_run(root, status="failed")
            manifest = monitor.load_manifest(run_dir / "pipeline_manifest.json")
            self.assertEqual(monitor.candidate_status(manifest, False), "failed")
            self.assertEqual(monitor.candidate_status(manifest, True), "eligible")

    def test_complete_run_is_not_postprocessed_again(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = self._make_run(root, status="complete")
            args = argparse.Namespace(retry_failed=False, stable_sec=0.0, stale_lock_sec=3600.0)
            with mock.patch.object(monitor, "run_postprocess") as postprocess:
                result, failed = monitor.process_manifest(
                    run_dir / "pipeline_manifest.json", args, root / "monitor.csv"
                )
            self.assertEqual(result, "skip")
            self.assertFalse(failed)
            postprocess.assert_not_called()


if __name__ == "__main__":
    unittest.main()
