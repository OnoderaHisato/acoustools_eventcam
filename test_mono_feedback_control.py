from __future__ import annotations

import math
import unittest

import numpy as np

from mono_eventcam_1axis_feedback import build_parser, resolved_pid_config, validate_hardware_request
from mono_feedback_core import (
    MonoAxisProjection,
    PaperPid1D,
    PidConfig,
    SecondOrderPlantConfig,
    assess_lock_stability,
    load_left_camera_axis_projection,
    simulate_closed_loop,
    trap_particle_limit_exceeded,
    trap_particle_separation_mm,
)


CALIBRATION = (
    "stereo_checkerboard_calib_extrinsics_20260805/"
    "stereo_calibration_square7p12_extrinsics_final.npz"
)
REGISTRATION = (
    "pat_stereo_grid_records/pat_camera_registration_grid_27_20260728_144326/"
    "registration/camera_to_pat_transform.npz"
)


class MonoFeedbackTests(unittest.TestCase):
    @staticmethod
    def simple_x_projection() -> MonoAxisProjection:
        return MonoAxisProjection(
            axis="x",
            predicted_origin_px=(0.0, 0.0),
            vector_px_per_mm=(10.0, 0.0),
        )

    def test_paper_time_constants_and_equivalent_terms(self) -> None:
        config = PidConfig(kp=0.35, integral_hz=23.0, derivative_hz=3.6)
        self.assertAlmostEqual(config.integral_time_sec, 1.0 / (2.0 * math.pi * 23.0))
        self.assertAlmostEqual(config.derivative_time_sec, 1.0 / (2.0 * math.pi * 3.6))
        self.assertAlmostEqual(config.kp / config.integral_time_sec, 50.58, delta=0.06)
        self.assertAlmostEqual(config.kp * config.derivative_time_sec, 0.01547, delta=0.00002)

    def test_positive_particle_error_commands_negative_trap(self) -> None:
        controller = PaperPid1D(
            PidConfig(
                kp=0.05,
                moving_average_samples=1,
                max_trap_offset_mm=0.2,
                max_trap_particle_mm=0.3,
                max_slew_mm_s=1000.0,
            )
        )
        sample = controller.update(reference_mm=0.0, measured_mm=0.1)
        self.assertAlmostEqual(sample.raw_error_mm, -0.1)
        self.assertAlmostEqual(sample.trap_mm, -0.005)

    def test_relative_separation_interlock_holds_last_trap(self) -> None:
        controller = PaperPid1D(
            PidConfig(
                kp=10.0,
                moving_average_samples=1,
                max_trap_offset_mm=0.2,
                max_trap_particle_mm=0.05,
                max_slew_mm_s=10000.0,
            )
        )
        controller.reset(trap_mm=0.01)
        sample = controller.update(reference_mm=0.0, measured_mm=0.1)
        self.assertLessEqual(abs(sample.trap_mm), 0.2)
        self.assertAlmostEqual(sample.absolute_limited_trap_mm, -0.2)
        self.assertAlmostEqual(sample.requested_trap_particle_mm, 0.3)
        self.assertAlmostEqual(sample.trap_mm, 0.01)
        self.assertTrue(sample.output_saturated)
        self.assertTrue(sample.relative_limit_exceeded)
        self.assertTrue(sample.relative_saturated)
        self.assertFalse(sample.slew_limited)

    def test_failed_run_condition_never_reverses_restoring_command(self) -> None:
        controller = PaperPid1D(
            PidConfig(
                kp=0.05,
                moving_average_samples=1,
                max_trap_offset_mm=0.05,
                max_trap_particle_mm=0.15,
                max_slew_mm_s=1000.0,
            )
        )
        sample = controller.update(reference_mm=0.0, measured_mm=0.248)
        self.assertAlmostEqual(sample.unsaturated_trap_mm, -0.0124)
        self.assertAlmostEqual(sample.absolute_limited_trap_mm, -0.0124)
        self.assertTrue(sample.relative_limit_exceeded)
        self.assertAlmostEqual(sample.trap_mm, 0.0)
        self.assertLessEqual(sample.trap_mm, 0.0)

    def test_relative_limit_does_not_modify_an_accepted_command(self) -> None:
        controller = PaperPid1D(
            PidConfig(
                kp=0.05,
                moving_average_samples=1,
                max_trap_offset_mm=0.05,
                max_trap_particle_mm=0.15,
                max_slew_mm_s=1000.0,
            )
        )
        sample = controller.update(reference_mm=0.0, measured_mm=0.1)
        self.assertFalse(sample.relative_limit_exceeded)
        self.assertAlmostEqual(sample.trap_mm, -0.005)

    def test_current_applied_trap_interlock(self) -> None:
        self.assertAlmostEqual(
            trap_particle_separation_mm(trap_mm=0.05, measured_mm=0.248), 0.198
        )
        self.assertTrue(
            trap_particle_limit_exceeded(trap_mm=0.05, measured_mm=0.248, limit_mm=0.15)
        )
        self.assertFalse(
            trap_particle_limit_exceeded(trap_mm=0.0, measured_mm=0.1, limit_mm=0.15)
        )

    def test_quantized_command_is_checked_at_relative_limit_boundary(self) -> None:
        unquantized_trap = 0.0019
        quantized_trap = 0.0025
        measured_mm = -0.148
        self.assertFalse(
            trap_particle_limit_exceeded(
                trap_mm=unquantized_trap, measured_mm=measured_mm, limit_mm=0.15
            )
        )
        self.assertTrue(
            trap_particle_limit_exceeded(
                trap_mm=quantized_trap, measured_mm=measured_mm, limit_mm=0.15
            )
        )

    def test_stable_lock_accepts_a_quiet_recent_window(self) -> None:
        phase = np.linspace(0.0, 4.0 * math.pi, 500)
        points = np.column_stack((100.0 + 0.1 * np.sin(phase), np.full(500, 50.0)))
        result = assess_lock_stability(
            points,
            self.simple_x_projection(),
            max_robust_std_mm=0.05,
            max_p90_span_mm=0.12,
            max_endpoint_offset_mm=0.05,
        )
        self.assertTrue(result.stable)
        self.assertLess(result.robust_std_mm, 0.02)
        self.assertLess(result.p90_span_mm, 0.03)
        self.assertLess(result.endpoint_offset_mm, 0.01)

    def test_stable_lock_rejects_smooth_drift_even_when_every_point_is_valid(self) -> None:
        points = np.column_stack((np.linspace(100.0, 104.0, 500), np.full(500, 50.0)))
        result = assess_lock_stability(
            points,
            self.simple_x_projection(),
            max_robust_std_mm=0.05,
            max_p90_span_mm=0.12,
            max_endpoint_offset_mm=0.05,
        )
        self.assertFalse(result.stable)
        self.assertGreater(result.p90_span_mm, 0.3)
        self.assertGreater(result.endpoint_offset_mm, 0.19)

    def test_left_projection_matches_existing_x_scale_and_direction(self) -> None:
        projection = load_left_camera_axis_projection(CALIBRATION, REGISTRATION, axis="x")
        self.assertAlmostEqual(projection.scale_px_per_mm, 7.8554, delta=0.008)
        self.assertGreater(projection.unit_vector_px[0], 0.99)
        origin = (584.0, 477.0)
        point = tuple(np.asarray(origin) + np.asarray(projection.vector_px_per_mm) * 0.2)
        self.assertAlmostEqual(projection.displacement_mm(point, origin), 0.2)

    def test_paper_controller_stabilizes_paper_model_in_simulation(self) -> None:
        plant = SecondOrderPlantConfig(natural_frequency_hz=12.0, damping_ratio=0.014, delay_sec=0.0055)
        open_loop = simulate_closed_loop(
            PaperPid1D(PidConfig(kp=0.0, max_slew_mm_s=1000.0)),
            duration_sec=1.0,
            plant=plant,
            initial_position_mm=0.1,
        )
        closed_loop = simulate_closed_loop(
            PaperPid1D(
                PidConfig(
                    kp=0.35,
                    integral_hz=23.0,
                    derivative_hz=3.6,
                    max_trap_offset_mm=0.5,
                    max_trap_particle_mm=0.5,
                    max_slew_mm_s=1000.0,
                    integral_output_limit_mm=0.5,
                )
            ),
            duration_sec=1.0,
            plant=plant,
            initial_position_mm=0.1,
        )
        open_tail = open_loop["position_mm"][-500:]
        closed_tail = closed_loop["position_mm"][-500:]
        self.assertLess(np.sqrt(np.mean(closed_tail**2)), 0.1 * np.sqrt(np.mean(open_tail**2)))

    def test_hardware_mode_is_gated_before_any_hardware_import(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--mode", "hardware", "--tracking-roi", "520,430,650,520"])
        config = resolved_pid_config(args)
        with self.assertRaisesRegex(SystemExit, "acknowledge-real-time-feedback-risk"):
            validate_hardware_request(args, config)

    def test_initial_hardware_defaults_are_p_only_and_bounded(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--mode",
                "hardware",
                "--tracking-roi",
                "520,430,650,520",
                "--acknowledge-real-time-feedback-risk",
            ]
        )
        config = resolved_pid_config(args)
        roi = validate_hardware_request(args, config)
        self.assertEqual(roi, (520, 430, 650, 520))
        self.assertAlmostEqual(config.kp, 0.05)
        self.assertEqual(config.integral_hz, 0)
        self.assertEqual(config.derivative_hz, 0)
        self.assertAlmostEqual(config.max_trap_offset_mm, 0.20)
        self.assertAlmostEqual(args.lock_window_sec, 0.25)
        self.assertAlmostEqual(args.lock_sec, 0.25)
        self.assertAlmostEqual(args.lock_timeout_sec, 15.0)

    def test_hardware_lock_timeout_must_cover_window_and_stable_duration(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--mode",
                "hardware",
                "--tracking-roi",
                "520,430,650,520",
                "--acknowledge-real-time-feedback-risk",
                "--lock-window-sec",
                "1.0",
                "--lock-sec",
                "1.0",
                "--lock-timeout-sec",
                "2.0",
            ]
        )
        with self.assertRaisesRegex(SystemExit, "must exceed"):
            validate_hardware_request(args, resolved_pid_config(args))


if __name__ == "__main__":
    unittest.main()
