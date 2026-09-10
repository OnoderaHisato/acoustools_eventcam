#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import copy
import csv
import io
import json
import math
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

import stereo_stage_accuracy_capture as capture_module
from stereo_stage_accuracy_analyze import (
    accepted_sampled_span,
    aggregate_cube_station,
    apply_pat_frame_registration,
    apply_reference_registration,
    centre_for_pass_station,
    distance_interpretation,
)
from stereo_stage_accuracy_capture import (
    PatTarget,
    SafeOssilaAxis,
    create_or_resume_session,
    preflight_capture_software,
    run_camera_stream_preflight,
)
from stereo_stage_accuracy_common import (
    experiment_motion_vectors,
    file_sha256,
    generate_plan,
    gray_cube_vertices,
    kabsch,
    load_json,
    load_stereo_calibration,
    point_to_stereo_baseline_line,
    plan_hash,
    simple_depth_sigma_mm,
    transform_points,
    validate_config,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "stereo_stage_accuracy_config.json"
HISTORICAL_TEST_CALIBRATION = (
    ROOT
    / "stereo_checkerboard_blink5"
    / "stereo_calibration_square7p1_cam2left_cam1right_no_pose026.npz"
)
_load_json_file = load_json


def load_json(path: Path) -> dict[str, object]:
    """Keep unit tests independent of the not-yet-captured 7.12 mm artifact."""
    payload = _load_json_file(path)
    if Path(path).resolve() == DEFAULT_CONFIG.resolve():
        payload["camera"]["stereo_calibration"] = str(HISTORICAL_TEST_CALIBRATION)
        payload["camera"]["expected_square_mm"] = 7.1
        payload["analysis"]["evaluation_frame"] = "camera_relative"
    return payload


def make_mock_axis(
    *,
    length_mm: float = 200.0,
    posmode: str = "absolute",
    speed_mm_s: float = 0.0,
    current_position_mm: float = 100.0,
    acceleration_mm_s2: float = 10.0,
    deceleration_mm_s2: float = 10.0,
    end_switch: bool = False,
    alarms: list[str] | None = None,
) -> SafeOssilaAxis:
    axis = SafeOssilaAxis.__new__(SafeOssilaAxis)
    axis.config = {
        "expected_travel_mm": 200.0,
        "require_clear_alarms": True,
        "expected_device_response": "G2010B1",
        "expected_stage_serial_response": "MOCK-STAGE",
        "expected_acceleration_mm_s2": 10.0,
        "expected_deceleration_mm_s2": 10.0,
        "settings_match_tolerance_mm_s2": 0.001,
        "readback_tolerance_mm": 0.1,
    }
    axis.hardware_min_mm = 5.0
    axis.hardware_max_mm = 195.0
    axis.usb_serial = "MOCK-USB"
    text_values = {
        "device": "G2010B1",
        "firmware": "1.2.3",
        "serial": "MOCK-STAGE",
    }
    float_values = {
        "length": length_mm,
        "speed": 5.0,
        "acc": acceleration_mm_s2,
        "dec": deceleration_mm_s2,
    }
    axis.query_text = Mock(
        side_effect=lambda name: (
            text_values[name],
            f"<{name} {text_values[name]}>",
        )
    )
    axis.query_float = Mock(
        side_effect=lambda name: (
            float_values[name],
            f"<{name} {float_values[name]}>",
        )
    )
    axis.query_alarms = Mock(return_value=(alarms or [], "<alarms 0>"))
    axis.query_posmode = Mock(return_value=(posmode, f"<posmode {posmode}>"))
    axis.query_status = Mock(
        return_value={
            "frame": (
                f"<status {speed_mm_s} {current_position_mm} 0 "
                f"{int(end_switch)}>"
            ),
            "speed_mm_s": speed_mm_s,
            "position_hardware_mm": current_position_mm,
            "home_switch": False,
            "end_switch": end_switch,
        }
    )
    axis.emergency_stop = Mock(return_value=True)
    return axis


class GeometryTests(unittest.TestCase):
    def test_distance_outputs_do_not_claim_absolute_truth(self) -> None:
        registration = {
            "self_registered_reference_target_left_camera_mm": np.asarray(
                [30.0, 40.0, 300.0]
            ),
            "reference_stage_readback_hardware_mm": 200.0,
        }
        stations = [
            {
                "camera_z_median_mm": 300.0,
                "camera_slant_range_median_mm": math.sqrt(30.0**2 + 40.0**2 + 300.0**2),
            }
        ]
        result = distance_interpretation(
            registration,
            stations,
            load_json(DEFAULT_CONFIG),
            {
                "R": np.eye(3),
                "T": np.asarray([-120.0, 0.0, 0.0]),
            },
        )
        self.assertFalse(result["absolute_accuracy_available"])
        self.assertAlmostEqual(result["reference_camera_z_estimate_mm"], 300.0)
        self.assertAlmostEqual(
            result["reference_optical_center_slant_range_estimate_mm"],
            math.sqrt(30.0**2 + 40.0**2 + 300.0**2),
        )
        self.assertNotEqual(
            result["reference_camera_z_estimate_mm"],
            result["reference_optical_center_slant_range_estimate_mm"],
        )
        self.assertAlmostEqual(
            result["reference_pat_origin_to_stereo_baseline_estimate_mm"],
            math.sqrt(40.0**2 + 300.0**2),
        )
        self.assertAlmostEqual(
            result["reference_baseline_foot_from_left_optical_center_mm"],
            30.0,
        )

    def test_accepted_span_is_explicitly_relative(self) -> None:
        rows = [
            {
                "stage_global_mm": 0.0,
                "pass": True,
                "camera_z_median_mm": 300.0,
                "camera_slant_range_median_mm": 301.0,
                "stage_readback_hardware_median_mm": 195.0,
            },
            {
                "stage_global_mm": -20.0,
                "pass": True,
                "camera_z_median_mm": 320.0,
                "camera_slant_range_median_mm": 321.0,
                "stage_readback_hardware_median_mm": 175.0,
            },
        ]
        span = accepted_sampled_span(rows, 0.0)
        self.assertTrue(span["available"])
        self.assertIn("reference-registered relative-accuracy", span["interpretation"])
        self.assertEqual(span["minimum_stage_hardware_readback_mm"], 175.0)
        self.assertEqual(span["maximum_camera_slant_range_mm"], 321.0)

    def test_gray_order_changes_one_sign(self) -> None:
        vertices = gray_cube_vertices()
        self.assertEqual(len(vertices), 8)
        self.assertEqual(len(set(vertices)), 8)
        cyclic = vertices + [vertices[0]]
        for left, right in zip(cyclic, cyclic[1:]):
            changed = sum(a != b for a, b in zip(left, right))
            self.assertEqual(changed, 1)

    def test_plan_and_soft_limits(self) -> None:
        config = load_json(DEFAULT_CONFIG)
        resolved = validate_config(config, DEFAULT_CONFIG)
        samples = generate_plan(config)
        self.assertEqual(len(samples), 1110)
        self.assertEqual(sum(item["kind"] == "vertex" for item in samples), 888)
        self.assertEqual(sum(item["kind"] == "center_pre" for item in samples), 111)
        self.assertEqual(sum(item["kind"] == "center_post" for item in samples), 111)
        self.assertEqual(
            sum(item["pass_direction"] == "registration" for item in samples),
            10,
        )
        self.assertEqual(
            sum(item["pass_direction"] == "forward" for item in samples),
            550,
        )
        self.assertEqual(
            sum(item["pass_direction"] == "reverse" for item in samples),
            550,
        )
        self.assertTrue(
            all(
                item["status"]
                == item["capture_status"]
                == item["processing_status"]
                == "pending"
                for item in samples
            )
        )
        self.assertEqual(len({item["sample_id"] for item in samples}), len(samples))
        self.assertEqual(len(plan_hash(samples)), 64)
        direction = int(resolved["stage_direction"])
        datum = float(config["stage"]["datum_mm"])
        raw = [
            datum + direction * float(item)
            for item in config["stage"]["positions_global_mm"]
        ]
        self.assertGreaterEqual(min(raw), float(config["stage"]["hardware_min_mm"]))
        self.assertLessEqual(max(raw), float(config["stage"]["hardware_max_mm"]))

    def test_camera_stage_motion_reverses_stationary_target_truth(self) -> None:
        config = load_json(DEFAULT_CONFIG)
        moving_body, physical, apparent = experiment_motion_vectors(config)
        self.assertEqual(moving_body, "camera")
        np.testing.assert_allclose(physical, [0.0, 1.0, 0.0])
        np.testing.assert_allclose(apparent, [0.0, -1.0, 0.0])
        samples = generate_plan(config)
        first_away_station = next(
            sample
            for sample in samples
            if sample["pass_direction"] == "forward"
            and sample["kind"] == "center_pre"
            and float(sample["stage_delta_from_reference_mm"]) == -20.0
        )
        np.testing.assert_allclose(
            first_away_station["expected_relative_pat_mm"],
            [0.0, 20.0, 0.0],
        )

    def test_configured_scan_runs_hardware_200_to_5(self) -> None:
        config = load_json(DEFAULT_CONFIG)
        resolved = validate_config(config, DEFAULT_CONFIG)
        samples = generate_plan(config)
        forward_centres = [
            sample
            for sample in samples
            if sample["pass_name"] == "cycle01_forward"
            and sample["kind"] == "center_pre"
        ]
        direction = int(resolved["stage_direction"])
        datum = float(config["stage"]["datum_mm"])
        hardware = [
            datum + direction * float(sample["stage_command_global_mm"])
            for sample in forward_centres
        ]
        self.assertEqual(
            hardware,
            [200.0, 180.0, 160.0, 140.0, 120.0, 100.0, 80.0, 60.0, 40.0, 20.0, 5.0],
        )

    def test_point_to_calibrated_baseline_uses_shortest_line_distance(self) -> None:
        calibration = {
            "R": np.eye(3),
            "T": np.asarray([-120.0, 0.0, 0.0]),
        }
        result = point_to_stereo_baseline_line(
            np.asarray([60.0, 30.0, 400.0]),
            calibration,
        )
        self.assertAlmostEqual(
            float(result["perpendicular_distance_mm"]),
            math.sqrt(30.0**2 + 400.0**2),
        )
        np.testing.assert_allclose(
            result["foot_left_camera_mm"],
            [60.0, 0.0, 0.0],
        )
        self.assertAlmostEqual(
            float(result["foot_fraction_of_left_to_right_baseline"]),
            0.5,
        )

    def test_collinear_stage_sweep_ends_at_d0_plus_195(self) -> None:
        calibration = {
            "R": np.eye(3),
            "T": np.asarray([-120.0, 0.0, 0.0]),
        }
        d0 = float(
            point_to_stereo_baseline_line(
                np.asarray([60.0, 0.0, 300.0]),
                calibration,
            )["perpendicular_distance_mm"]
        )
        far = float(
            point_to_stereo_baseline_line(
                np.asarray([60.0, 0.0, 300.0 + (200.0 - 5.0)]),
                calibration,
            )["perpendicular_distance_mm"]
        )
        self.assertEqual(d0, 300.0)
        self.assertEqual(far, d0 + 195.0)

    def test_kabsch_recovers_rigid_transform(self) -> None:
        source = np.asarray(gray_cube_vertices(), dtype=np.float64) * np.array([5.0, 7.0, 9.0])
        angle = math.radians(23.0)
        rotation = np.array(
            [
                [math.cos(angle), 0.0, math.sin(angle)],
                [0.0, 1.0, 0.0],
                [-math.sin(angle), 0.0, math.cos(angle)],
            ]
        )
        translation = np.array([10.0, -4.0, 350.0])
        target = transform_points(source, rotation, translation)
        fitted_rotation, fitted_translation = kabsch(source, target)
        np.testing.assert_allclose(fitted_rotation, rotation, atol=1e-12)
        np.testing.assert_allclose(fitted_translation, translation, atol=1e-12)

    def test_simple_depth_noise_is_quadratic(self) -> None:
        near = simple_depth_sigma_mm(
            300.0,
            focal_px=1771.0,
            baseline_mm=120.0,
            per_camera_pixel_sigma=0.25,
        )
        far = simple_depth_sigma_mm(
            600.0,
            focal_px=1771.0,
            baseline_mm=120.0,
            per_camera_pixel_sigma=0.25,
        )
        self.assertAlmostEqual(far / near, 4.0, places=12)

    def test_theory_cli_does_not_emit_a_station_above_z_max(self) -> None:
        with tempfile.TemporaryDirectory(prefix="stage_theory_") as temporary:
            theory_config = Path(temporary) / "theory_config.json"
            theory_config.write_text(
                json.dumps(load_json(DEFAULT_CONFIG), indent=2),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "stereo_stage_accuracy_analyze.py"),
                    "--theory-only",
                    "--config",
                    str(theory_config),
                    "--z-min-mm",
                    "300",
                    "--z-max-mm",
                    "1000",
                    "--z-step-mm",
                    "250",
                    "--output-dir",
                    temporary,
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            with (Path(temporary) / "stereo_depth_theory.csv").open(
                newline="",
                encoding="utf-8",
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([float(row["z_mm"]) for row in rows], [300.0, 550.0, 800.0])


class ConfigurationSafetyTests(unittest.TestCase):
    def test_pat_transform_quality_and_calibration_are_locked(self) -> None:
        config = load_json(DEFAULT_CONFIG)
        with tempfile.TemporaryDirectory(prefix="pat_transform_") as temporary:
            transform_path = Path(temporary) / "camera_to_pat_transform.json"
            transform = {
                "R_camera_to_pat": np.eye(3).tolist(),
                "t_camera_to_pat_mm": [1.0, 2.0, 3.0],
                "stereo_calibration_sha256": file_sha256(
                    HISTORICAL_TEST_CALIBRATION
                ),
                "stage_hardware_readback_mm": 200.0,
                "pat_center_mm": [0.0, 0.0, 0.0],
                "acoustools_zero_in_pat_mm": [0.0, 0.0, 0.0],
                "quality": {"pass": True, "failures": []},
            }
            transform_path.write_text(
                json.dumps(transform),
                encoding="utf-8",
            )
            config["analysis"]["evaluation_frame"] = "pat"
            config["analysis"]["camera_to_pat_transform"] = str(
                transform_path
            )
            resolved = validate_config(config, DEFAULT_CONFIG)
            self.assertEqual(resolved["evaluation_frame"], "pat")
            self.assertEqual(
                resolved["camera_to_pat_data"]["transform_path"],
                str(transform_path.resolve()),
            )

            transform["quality"] = {
                "pass": False,
                "failures": ["synthetic failure"],
            }
            transform_path.write_text(
                json.dumps(transform),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SystemExit, "quality gate"):
                validate_config(config, DEFAULT_CONFIG)

    def test_capture_and_analysis_reference_must_match(self) -> None:
        config = load_json(DEFAULT_CONFIG)
        config["analysis"]["reference_stage_global_mm"] = 20.0
        with self.assertRaisesRegex(SystemExit, "must equal"):
            validate_config(config, DEFAULT_CONFIG)

    def test_declared_boolean_fields_are_strict(self) -> None:
        paths = [
            ("stage", "return_to_reference_on_success"),
            ("stage", "require_clear_alarms"),
            ("target", "return_to_center"),
            ("experiment", "center_before_and_after"),
            ("experiment", "dedicated_registration_pass"),
            ("camera", "preview", "enabled"),
        ]
        for path in paths:
            with self.subTest(path=".".join(path)):
                config = load_json(DEFAULT_CONFIG)
                node = config
                for key in path[:-1]:
                    node = node[key]
                node[path[-1]] = 1
                with self.assertRaisesRegex(SystemExit, "JSON boolean"):
                    validate_config(config, DEFAULT_CONFIG)

    def test_numeric_fields_reject_json_boolean(self) -> None:
        for path in (
            ("stage", "speed_mm_s"),
            ("experiment", "scan_cycles"),
        ):
            with self.subTest(path=path):
                config = load_json(DEFAULT_CONFIG)
                config[path[0]][path[1]] = True
                with self.assertRaises(SystemExit):
                    validate_config(config, DEFAULT_CONFIG)

    def test_registration_name_cannot_collide_with_evaluation_pass(self) -> None:
        config = load_json(DEFAULT_CONFIG)
        config["analysis"]["registration_pass_name"] = "cycle01_forward"
        with self.assertRaisesRegex(SystemExit, "collides"):
            validate_config(config, DEFAULT_CONFIG)

    def test_successful_pass_fraction_must_be_positive(self) -> None:
        config = load_json(DEFAULT_CONFIG)
        config["analysis"]["acceptance"]["minimum_successful_pass_fraction"] = 0.0
        with self.assertRaisesRegex(SystemExit, r"\(0, 1\]"):
            validate_config(config, DEFAULT_CONFIG)

    def test_resume_rejects_configuration_fingerprint_mismatch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="stage_fingerprint_") as temporary:
            session_dir = Path(temporary) / "session"
            args = Namespace(
                session_dir=session_dir,
                output_root=Path(temporary),
                execute=False,
            )
            config = load_json(DEFAULT_CONFIG)
            resolved = validate_config(config, DEFAULT_CONFIG)
            planned = generate_plan(config)
            create_or_resume_session(
                args,
                config,
                DEFAULT_CONFIG,
                resolved,
                planned,
            )
            changed = copy.deepcopy(config)
            changed["camera"]["record_sec"] += 0.1
            changed_resolved = validate_config(changed, DEFAULT_CONFIG)
            changed_plan = generate_plan(changed)
            self.assertEqual(plan_hash(changed_plan), plan_hash(planned))
            with self.assertRaisesRegex(SystemExit, "fingerprint differs"):
                create_or_resume_session(
                    args,
                    changed,
                    DEFAULT_CONFIG,
                    changed_resolved,
                    changed_plan,
                )

    def test_execute_requires_an_existing_reviewed_dry_plan_session(self) -> None:
        with tempfile.TemporaryDirectory(prefix="stage_execute_lock_") as temporary:
            config = load_json(DEFAULT_CONFIG)
            resolved = validate_config(config, DEFAULT_CONFIG)
            planned = generate_plan(config)
            args = Namespace(
                session_dir=Path(temporary) / "not_yet_planned",
                output_root=Path(temporary),
                execute=True,
                probe_stage_identity=False,
            )
            with self.assertRaisesRegex(SystemExit, "reviewed dry-plan session"):
                create_or_resume_session(
                    args,
                    config,
                    DEFAULT_CONFIG,
                    resolved,
                    planned,
                )

    def test_execute_can_only_resume_the_same_dry_plan_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="stage_execute_resume_") as temporary:
            session_dir = Path(temporary) / "reviewed"
            config = load_json(DEFAULT_CONFIG)
            resolved = validate_config(config, DEFAULT_CONFIG)
            planned = generate_plan(config)
            dry_args = Namespace(
                session_dir=session_dir,
                output_root=Path(temporary),
                execute=False,
                probe_stage_identity=False,
            )
            _, _, dry_manifest = create_or_resume_session(
                dry_args,
                config,
                DEFAULT_CONFIG,
                resolved,
                planned,
            )
            execute_args = Namespace(
                session_dir=session_dir,
                output_root=Path(temporary),
                execute=True,
                probe_stage_identity=False,
            )
            _, _, resumed_manifest = create_or_resume_session(
                execute_args,
                config,
                DEFAULT_CONFIG,
                resolved,
                planned,
            )
            self.assertEqual(resumed_manifest["plan_hash"], dry_manifest["plan_hash"])


class CameraPreflightTests(unittest.TestCase):
    @staticmethod
    def _write_success_manifest(command: list[str], *, sync_verified: bool = True) -> None:
        run_dir = Path(command[command.index("--run-dir") + 1])
        left_serial = command[command.index("--left-serial") + 1]
        right_serial = command[command.index("--right-serial") + 1]
        run_dir.mkdir(parents=True)
        side_payloads = {}
        for side, serial in (("left", left_serial), ("right", right_serial)):
            side_payloads[side] = {
                "ok": True,
                "meta": {
                    "serial": serial,
                    "capture_interval_complete": True,
                    "total_events": 10,
                },
            }
        payload = {
            "left_serial": left_serial,
            "right_serial": right_serial,
            "left": side_payloads["left"],
            "right": side_payloads["right"],
            "hw_sync": {"verified": sync_verified},
        }
        (run_dir / "stereo_recording_manifest.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

    def test_stream_preflight_runs_real_recorder_contract_before_motion(self) -> None:
        config = load_json(DEFAULT_CONFIG)
        config["camera"]["camera_reopen_wait_sec"] = 0.0
        with tempfile.TemporaryDirectory(prefix="stage_camera_health_") as temporary:
            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                self._write_success_manifest(command)
                return subprocess.CompletedProcess(command, 0, "stream ok", "")

            with patch(
                "stereo_stage_accuracy_capture.subprocess.run",
                side_effect=fake_run,
            ):
                result = run_camera_stream_preflight(
                    config,
                    Path(validate_config(config, DEFAULT_CONFIG)["stereo_calibration"]),
                    Path(temporary),
                )
            self.assertTrue(result["hardware_sync_verified"])
            self.assertEqual(result["left_total_events"], 10)
            command_result = Path(result["attempt_dir"]) / "command_result.json"
            self.assertTrue(command_result.is_file())

    def test_stream_preflight_rejects_unverified_hardware_sync(self) -> None:
        config = load_json(DEFAULT_CONFIG)
        config["camera"]["camera_reopen_wait_sec"] = 0.0
        with tempfile.TemporaryDirectory(prefix="stage_camera_sync_") as temporary:
            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                self._write_success_manifest(command, sync_verified=False)
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch(
                "stereo_stage_accuracy_capture.subprocess.run",
                side_effect=fake_run,
            ), self.assertRaisesRegex(RuntimeError, "synchronization was not verified"):
                run_camera_stream_preflight(
                    config,
                    Path(validate_config(config, DEFAULT_CONFIG)["stereo_calibration"]),
                    Path(temporary),
                )

    def test_discovery_serial_matching_is_exact_not_substring(self) -> None:
        config = load_json(DEFAULT_CONFIG)
        config["target"]["mode"] = "manual"
        calibration = Path(validate_config(config, DEFAULT_CONFIG)["stereo_calibration"])
        with tempfile.TemporaryDirectory(prefix="stage_camera_exact_") as temporary:
            with patch(
                "stereo_eventcam_record_sync.list_devices",
                return_value=["prefix-00000508-suffix", "00000509"],
            ), patch(
                "stereo_stage_accuracy_capture.run_camera_stream_preflight"
            ), self.assertRaisesRegex(RuntimeError, "were not discovered"):
                preflight_capture_software(
                    config,
                    calibration,
                    Path(temporary),
                )


class MainLifecycleSafetyTests(unittest.TestCase):
    def test_cleanup_failure_is_sticky_and_done_is_not_printed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="stage_cleanup_main_") as temporary:
            session_dir = Path(temporary) / "session"
            session_dir.mkdir()
            manifest_path = session_dir / "stereo_stage_accuracy_manifest.json"
            sample = {
                "sample_id": "only_sample",
                "pass_name": "registration",
                "kind": "center_pre",
                "vertex_id": "",
                "stage_command_global_mm": 0.0,
                "pat_target_mm": [0.0, 0.0, 0.0],
            }
            manifest = {
                "status": "planned",
                "samples": [sample],
                "stereo_calibration": str(
                    validate_config(
                        load_json(DEFAULT_CONFIG),
                        DEFAULT_CONFIG,
                    )["stereo_calibration"]
                ),
            }
            config = load_json(DEFAULT_CONFIG)
            config["target"]["mode"] = "manual"
            config["stage"]["expected_device_response"] = "G2010B1"
            config["stage"]["expected_stage_serial_response"] = "MOCK-STAGE"
            config["stage"]["expected_acceleration_mm_s2"] = 10.0
            config["stage"]["expected_deceleration_mm_s2"] = 10.0
            resolved = {
                "stage_axis": "y",
                "stage_serial": "MOCK-USB",
                "stage_direction": 1,
            }
            args = Namespace(
                config=DEFAULT_CONFIG,
                output_root=Path(temporary),
                session_dir=session_dir,
                execute=True,
                probe_stage_identity=False,
                home_depth_axis=False,
                yes=True,
                no_preview=True,
                trace_cube_preview=False,
                process_each=False,
                continue_on_error=False,
            )
            stage = Mock()
            stage.query_status.return_value = {
                "speed_mm_s": 0.0,
                "position_hardware_mm": 100.0,
            }
            stage.preflight.return_value = {
                "stage_serial": "MOCK-STAGE",
                "length_mm": 200.0,
                "acceleration_mm_s2": 10.0,
                "deceleration_mm_s2": 10.0,
                "status": {
                    "speed_mm_s": 0.0,
                    "position_hardware_mm": 100.0,
                },
            }
            stage.device_snapshot.return_value = {"status": "stopped"}
            stage.hardware_from_global.side_effect = lambda value: 100.0 + float(value)
            stage.move_global.return_value = {
                "readback_global_mm": 0.0,
                "readback_hardware_mm": 100.0,
            }
            stage.global_position_mm.return_value = (0.0, 100.0, "<pos 100>")
            target = Mock()
            target.turn_off.return_value = False

            def fake_capture_sample(**kwargs: object) -> bool:
                captured_sample = kwargs["sample"]
                assert isinstance(captured_sample, dict)
                captured_sample["capture_status"] = "captured"
                return True

            output = io.StringIO()
            patches = (
                patch.object(capture_module, "parse_args", return_value=args),
                patch.object(capture_module, "load_json", return_value=config),
                patch.object(capture_module, "validate_config", return_value=resolved),
                patch.object(capture_module, "generate_plan", return_value=[sample]),
                patch.object(
                    capture_module,
                    "create_or_resume_session",
                    return_value=(session_dir, manifest_path, manifest),
                ),
                patch.object(capture_module, "print_plan"),
                patch.object(capture_module, "SafeOssilaAxis", return_value=stage),
                patch.object(capture_module, "ManualTarget", return_value=target),
                patch.object(
                    capture_module,
                    "preflight_capture_software",
                    return_value={"ok": True},
                ),
                patch.object(
                    capture_module,
                    "confirm_motion_authority",
                    return_value=True,
                ),
                patch.object(
                    capture_module,
                    "capture_sample",
                    side_effect=fake_capture_sample,
                ),
            )
            for active_patch in patches:
                active_patch.start()
            try:
                with patch("sys.stdout", output), patch("sys.stderr", output):
                    first_returncode = capture_module.main()
                    second_returncode = capture_module.main()
            finally:
                for active_patch in reversed(patches):
                    active_patch.stop()

            self.assertEqual(first_returncode, 2)
            self.assertEqual(second_returncode, 2)
            self.assertEqual(manifest["status"], "capture_complete_cleanup_failed")
            self.assertFalse(manifest["cleanup_verified"])
            self.assertNotIn("[DONE]", output.getvalue())
            self.assertEqual(stage.query_status.call_count, 1)


class OssilaProtocolSafetyTests(unittest.TestCase):
    def test_measured_soft_limit_boundary_uses_readback_allowance(self) -> None:
        axis = make_mock_axis()
        self.assertFalse(axis.within_soft_limits(4.999))
        self.assertTrue(
            axis.within_soft_limits(
                4.999,
                readback_allowance_mm=0.1,
            )
        )
        self.assertFalse(
            axis.within_soft_limits(
                4.899,
                readback_allowance_mm=0.1,
            )
        )

    def test_home_waits_for_async_home_completion_without_status_polling(self) -> None:
        class FakeSerial:
            def __init__(self) -> None:
                self.buffer = bytearray()
                self.commands: list[str] = []

            @property
            def in_waiting(self) -> int:
                return len(self.buffer)

            def reset_input_buffer(self) -> None:
                self.buffer.clear()

            def write(self, payload: bytes) -> int:
                command = payload.decode("ascii")
                self.commands.append(command)
                responses = {
                    "<alarms?>": b"<alarms>",
                    "<home>": b"<homing><home>",
                    "<posmode?>": b"<posmode absolute>",
                    "<pos?>": b"<pos 0.000000>",
                    "<status?>": b"<status 0.000000 0.000000 0 0>",
                }
                self.buffer.extend(responses[command])
                return len(payload)

            def read(self, size: int) -> bytes:
                chunk = bytes(self.buffer[:size])
                del self.buffer[:size]
                return chunk

        serial_port = FakeSerial()
        axis = SafeOssilaAxis.__new__(SafeOssilaAxis)
        axis.config = {
            "require_clear_alarms": True,
            "motion_timeout_sec": 1.0,
            "home_readback_tolerance_mm": 0.5,
        }
        axis.axis = "y"
        axis.direction = 1
        axis.datum_mm = 20.0
        axis.command_timeout_sec = 0.1
        axis.device = Mock(ser=serial_port)
        axis.closed = False
        axis.protocol_log = []
        axis.receive_buffer = bytearray()

        result = axis.home()

        self.assertEqual(result["home_response"], "<homing>")
        self.assertEqual(result["home_completion_response"], "<home>")
        self.assertFalse(result["home_switch_active_after_completion"])
        self.assertEqual(
            serial_port.commands,
            [
                "<alarms?>",
                "<home>",
                "<posmode?>",
                "<pos?>",
                "<status?>",
                "<alarms?>",
            ],
        )

    def test_frame_and_float_parsers_are_strict(self) -> None:
        self.assertEqual(
            SafeOssilaAxis._frame_parts("<status 0 12.5 0 1>", "status"),
            ["status", "0", "12.5", "0", "1"],
        )
        for frame in ("status 0 0 0 0", "<>", "<status 0 0 0 0"):
            with self.subTest(frame=frame), self.assertRaises(RuntimeError):
                SafeOssilaAxis._frame_parts(frame)
        with self.assertRaisesRegex(RuntimeError, "Expected Ossila"):
            SafeOssilaAxis._frame_parts("<pos 1>", "status")
        self.assertEqual(SafeOssilaAxis._float_token("-1.25e2", "x"), -125.0)
        for token in ("nan", "inf", "1.0mm", ""):
            with self.subTest(token=token), self.assertRaises(RuntimeError):
                SafeOssilaAxis._float_token(token, "x")

    def test_preflight_accepts_only_safe_expected_state(self) -> None:
        nominal = make_mock_axis().preflight(require_absolute=True)
        self.assertEqual(nominal["length_mm"], 200.0)
        cases = [
            (make_mock_axis(length_mm=100.0), "Configured travel"),
            (make_mock_axis(speed_mm_s=0.01), "already moving"),
            (make_mock_axis(end_switch=True), "end switch is active"),
            (make_mock_axis(posmode="relative"), "posmode is relative"),
            (make_mock_axis(alarms=["overcurrent"]), "active alarms"),
        ]
        for axis, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                RuntimeError,
                message,
            ):
                axis.preflight(require_absolute=True)

    def test_firmware_length_in_micrometres_is_normalized_only_when_locked(self) -> None:
        result = make_mock_axis(length_mm=200000.0).preflight(require_absolute=True)
        self.assertEqual(result["length_mm"], 200.0)
        self.assertEqual(result["length_raw_response_value"], 200000.0)
        self.assertEqual(
            result["length_unit_interpretation"],
            "raw_um_normalized_to_mm",
        )
        with self.assertRaisesRegex(RuntimeError, "neither raw mm nor raw/1000"):
            make_mock_axis(length_mm=12345.0).preflight(require_absolute=True)

    def test_end_switch_is_allowed_only_for_explicit_homing_preflight(self) -> None:
        axis = make_mock_axis(end_switch=True, posmode="relative")
        result = axis.preflight(
            require_absolute=False,
            allow_end_switch_for_homing=True,
        )
        self.assertTrue(result["status"]["end_switch"])
        with self.assertRaisesRegex(RuntimeError, "end switch is active"):
            axis.preflight(require_absolute=False)

    def test_preflight_requires_internal_stage_serial_lock(self) -> None:
        axis = make_mock_axis()
        axis.config["expected_stage_serial_response"] = ""
        with self.assertRaisesRegex(RuntimeError, "expected_stage_serial_response is empty"):
            axis.preflight(require_absolute=True)

    def test_preflight_requires_exact_device_product_lock(self) -> None:
        axis = make_mock_axis()
        axis.config["expected_device_response"] = ""
        with self.assertRaisesRegex(RuntimeError, "expected_device_response is empty"):
            axis.preflight(require_absolute=True)

    def test_preflight_requires_approved_acceleration_settings(self) -> None:
        axis = make_mock_axis(acceleration_mm_s2=12.0)
        with self.assertRaisesRegex(RuntimeError, "acceleration mismatch"):
            axis.preflight(require_absolute=True)

    def test_outside_soft_limit_requires_explicit_homing_path(self) -> None:
        axis = make_mock_axis(current_position_mm=0.0)
        with self.assertRaisesRegex(RuntimeError, "outside experiment soft limits"):
            axis.preflight(require_absolute=True)
        result = axis.preflight(
            require_absolute=True,
            allow_outside_soft_limit_for_homing=True,
        )
        self.assertEqual(result["status"]["position_hardware_mm"], 0.0)

    def test_pat_off_retries_after_keyboard_interrupt_and_disconnects(self) -> None:
        target = PatTarget.__new__(PatTarget)
        controller = Mock()
        controller.turn_off.side_effect = [KeyboardInterrupt(), None]
        controller.disconnect.return_value = None
        target.controller = controller
        target.current_hologram = object()
        self.assertTrue(target.turn_off())
        self.assertIsNone(target.controller)
        self.assertEqual(controller.turn_off.call_count, 2)
        controller.disconnect.assert_called_once()


class AnalysisIsolationTests(unittest.TestCase):
    @staticmethod
    def _row(
        *,
        sample_id: str,
        pass_name: str,
        kind: str,
        local_offset: np.ndarray,
        measured: np.ndarray,
        readback_mm: float,
        command_mm: float = 0.0,
        vertex_id: str = "",
    ) -> dict[str, object]:
        return {
            "sample_id": sample_id,
            "pass_name": pass_name,
            "pass_direction": (
                "registration" if pass_name == "registration" else "forward"
            ),
            "cycle_index": -1 if pass_name == "registration" else 0,
            "station_index": 0,
            "stage_global_mm": command_mm,
            "stage_delta_mm": command_mm,
            "stage_command_global_mm": command_mm,
            "stage_command_delta_mm": command_mm,
            "kind": kind,
            "vertex_id": vertex_id,
            "vertex_signs": None,
            "expected_mm": np.asarray(local_offset, dtype=np.float64)
            + np.array([0.0, command_mm, 0.0]),
            "command_expected_mm": np.asarray(local_offset, dtype=np.float64)
            + np.array([0.0, command_mm, 0.0]),
            "pat_target_mm": np.asarray(local_offset, dtype=np.float64),
            "local_offset_mm": np.asarray(local_offset, dtype=np.float64),
            "data_role": (
                "registration" if pass_name == "registration" else "evaluation"
            ),
            "stage_readback_global_mm": readback_mm,
            "stage_readback_hardware_mm": 200.0 + readback_mm,
            "stage_command_hardware_mm": 200.0 + command_mm,
            "stage_readback_error_mm": readback_mm - command_mm,
            "stereo_3d_npz": "",
            "usable": True,
            "reason": "",
            "center_mm": np.asarray(measured, dtype=np.float64),
            "center_camera_mm": np.asarray(measured, dtype=np.float64),
            "robust_samples": 400,
            "raw_valid_fraction": 1.0,
            "scatter_p95_mm": 0.02,
        }

    def _registration_rows(self, translation: np.ndarray) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for index, signs in enumerate(gray_cube_vertices()):
            local = np.asarray(signs, dtype=np.float64) * 5.0
            rows.append(
                self._row(
                    sample_id=f"reg{index}",
                    pass_name="registration",
                    kind="vertex",
                    local_offset=local,
                    measured=translation + local,
                    readback_mm=0.0,
                    vertex_id="v" + "".join("+" if value > 0 else "-" for value in signs),
                )
            )
        return rows

    def test_registration_ignores_evaluation_reference_vertices(self) -> None:
        config = load_json(DEFAULT_CONFIG)
        translation = np.array([40.0, 3.0, 350.0])
        rows = self._registration_rows(translation)
        for index, signs in enumerate(gray_cube_vertices()):
            local = np.asarray(signs, dtype=np.float64) * 5.0
            rows.append(
                self._row(
                    sample_id=f"eval{index}",
                    pass_name="cycle01_forward",
                    kind="vertex",
                    local_offset=local,
                    measured=translation + 3.0 * local + np.array([20.0, 0.0, 0.0]),
                    readback_mm=0.0,
                    vertex_id="v" + "".join("+" if value > 0 else "-" for value in signs),
                )
            )
        registration = apply_reference_registration(rows, config)
        np.testing.assert_allclose(
            registration["translation_left_camera_mm"],
            translation,
            atol=1e-12,
        )
        np.testing.assert_allclose(
            registration["rotation_expected_to_left_camera"],
            np.eye(3),
            atol=1e-12,
        )
        self.assertTrue(
            all(
                sample_id.startswith("reg")
                for sample_id in registration["reference_sample_ids"]
            )
        )

    def test_readback_truth_and_command_diagnostic_are_separate(self) -> None:
        config = load_json(DEFAULT_CONFIG)
        config["experiment"]["moving_body"] = "target"
        translation = np.array([40.0, 3.0, 350.0])
        rows = self._registration_rows(translation)
        evaluation = self._row(
            sample_id="centre",
            pass_name="cycle01_forward",
            kind="center_pre",
            local_offset=np.zeros(3),
            measured=translation + np.array([0.0, 19.5, 0.0]),
            readback_mm=19.5,
            command_mm=20.0,
        )
        rows.append(evaluation)
        apply_reference_registration(rows, config)
        self.assertAlmostEqual(float(evaluation["axial_error_mm"]), 0.0, places=12)
        self.assertAlmostEqual(
            float(evaluation["command_axial_error_mm"]),
            -0.5,
            places=12,
        )

    def test_center_pre_post_opposite_errors_do_not_cancel(self) -> None:
        config = load_json(DEFAULT_CONFIG)
        translation = np.array([40.0, 3.0, 350.0])
        rows = self._registration_rows(translation)
        for kind, error in (("center_pre", 2.0), ("center_post", -2.0)):
            rows.append(
                self._row(
                    sample_id=kind,
                    pass_name="cycle01_forward",
                    kind=kind,
                    local_offset=np.zeros(3),
                    measured=translation + np.array([0.0, error, 0.0]),
                    readback_mm=0.0,
                )
            )
        registration = apply_reference_registration(rows, config)
        calibration = load_stereo_calibration(
            validate_config(config, DEFAULT_CONFIG)["stereo_calibration"]
        )
        result = centre_for_pass_station(
            rows[-2:],
            registration,
            calibration,
            config,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertAlmostEqual(result["center_axial_error_max_abs_mm"], 2.0)
        np.testing.assert_allclose(result["center_camera_mm"], translation, atol=1e-12)

    def test_opposite_cube_scale_errors_are_kept_per_pass(self) -> None:
        config = load_json(DEFAULT_CONFIG)
        translation = np.array([40.0, 3.0, 350.0])
        registration = {
            "rotation_expected_to_left_camera": np.eye(3),
            "translation_left_camera_mm": translation,
        }
        aggregates = []
        for pass_name, scale in (
            ("cycle01_forward", 1.3),
            ("cycle01_reverse", 0.7),
        ):
            rows = []
            for index, signs in enumerate(gray_cube_vertices()):
                local = np.asarray(signs, dtype=np.float64) * 5.0
                rows.append(
                    self._row(
                        sample_id=f"{pass_name}_{index}",
                        pass_name=pass_name,
                        kind="vertex",
                        local_offset=local,
                        measured=translation + local * np.array([scale, 1.0, 1.0]),
                        readback_mm=0.0,
                        vertex_id=(
                            "v"
                            + "".join("+" if value > 0 else "-" for value in signs)
                        ),
                    )
                )
            aggregates.append(aggregate_cube_station(rows, registration, config))
        for aggregate in aggregates:
            self.assertEqual(aggregate["valid_unique_vertices"], 8)
            self.assertEqual(aggregate["edge_count"], 12)
            self.assertGreater(aggregate["vertex_rmse_mm"], 1.4)
            self.assertGreater(aggregate["edge_error_max_abs_mm"], 2.9)

    def test_pat_transform_composes_camera_motion_from_actual_readback(self) -> None:
        config = load_json(DEFAULT_CONFIG)
        angle = math.radians(23.0)
        rotation = np.array(
            [
                [math.cos(angle), 0.0, math.sin(angle)],
                [0.0, 1.0, 0.0],
                [-math.sin(angle), 0.0, math.cos(angle)],
            ]
        )
        translation = np.array([12.0, -34.0, 56.0])
        transform_global = -0.2
        transform = {
            "R_camera_to_pat": rotation,
            "t_camera_to_pat_mm": translation,
            "stage_hardware_readback_mm": 199.8,
            "transform_path": "synthetic.json",
            "transform_sha256": "a" * 64,
            "stereo_calibration_sha256": "b" * 64,
            "quality": {"pass": True},
        }
        targets = (
            np.array([1.0, 2.0, 3.0]),
            np.array([-4.0, 5.0, 6.0]),
        )
        globals_mm = (0.0, -195.0)
        rows = []
        for index, (target, global_mm) in enumerate(
            zip(targets, globals_mm)
        ):
            current_translation = (
                translation
                + np.array([0.0, 1.0, 0.0])
                * (global_mm - transform_global)
            )
            camera = rotation.T @ (target - current_translation)
            row = self._row(
                sample_id=f"pat{index}",
                pass_name="cycle01_forward",
                kind="center_pre",
                local_offset=target,
                measured=camera,
                readback_mm=global_mm,
                command_mm=global_mm,
            )
            row["pat_target_mm"] = target
            rows.append(row)

        registration = apply_pat_frame_registration(
            rows,
            config,
            transform,
            +1,
        )

        self.assertEqual(registration["evaluation_frame"], "pat")
        self.assertEqual(
            registration["registration_source"],
            "external_fixed_camera_to_pat",
        )
        for row, target in zip(rows, targets):
            np.testing.assert_allclose(row["center_pat_mm"], target, atol=1e-12)
            np.testing.assert_allclose(
                row["residual_pat_mm"],
                np.zeros(3),
                atol=1e-12,
            )

    def test_pat_residual_keeps_known_axial_and_cross_axis_errors(self) -> None:
        config = load_json(DEFAULT_CONFIG)
        transform = {
            "R_camera_to_pat": np.eye(3),
            "t_camera_to_pat_mm": np.array([10.0, 20.0, 30.0]),
            "stage_hardware_readback_mm": 200.0,
            "transform_path": "synthetic.json",
            "transform_sha256": "a" * 64,
            "stereo_calibration_sha256": "b" * 64,
            "quality": {"pass": True},
        }
        global_mm = -100.0
        target = np.zeros(3)
        error_pat = np.array([0.4, 1.25, -0.3])
        current_translation = (
            transform["t_camera_to_pat_mm"]
            + np.array([0.0, 1.0, 0.0]) * global_mm
        )
        camera = target + error_pat - current_translation
        row = self._row(
            sample_id="known_error",
            pass_name="cycle01_forward",
            kind="center_pre",
            local_offset=target,
            measured=camera,
            readback_mm=global_mm,
            command_mm=global_mm,
        )
        registration = apply_pat_frame_registration(
            [row],
            config,
            transform,
            +1,
        )
        self.assertAlmostEqual(float(row["axial_error_mm"]), 1.25)
        self.assertAlmostEqual(float(row["cross_axis_error_mm"]), 0.5)
        np.testing.assert_allclose(row["residual_pat_mm"], error_pat)
        calibration = load_stereo_calibration(
            validate_config(config, DEFAULT_CONFIG)["stereo_calibration"]
        )
        centre = centre_for_pass_station(
            [row],
            registration,
            calibration,
            config,
        )
        assert centre is not None
        np.testing.assert_allclose(centre["center_pat_mm"], error_pat)
        np.testing.assert_allclose(centre["center_camera_mm"], camera)


class SyntheticSessionTest(unittest.TestCase):
    def test_full_analysis_on_ideal_synthetic_session(self) -> None:
        with tempfile.TemporaryDirectory(prefix="stereo_stage_accuracy_") as temporary:
            session = Path(temporary)
            config = load_json(DEFAULT_CONFIG)
            config["stage"]["positions_global_mm"] = [0.0, -20.0, -40.0]
            config["experiment"]["scan_cycles"] = 1
            config["analysis"]["minimum_valid_samples"] = 20
            config_path = session / "session_config.json"
            config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
            resolved = validate_config(config, config_path)
            samples = generate_plan(config)

            angle_y = math.radians(17.0)
            angle_x = math.radians(-3.0)
            rotation_y = np.array(
                [
                    [math.cos(angle_y), 0.0, math.sin(angle_y)],
                    [0.0, 1.0, 0.0],
                    [-math.sin(angle_y), 0.0, math.cos(angle_y)],
                ]
            )
            rotation_x = np.array(
                [
                    [1.0, 0.0, 0.0],
                    [0.0, math.cos(angle_x), -math.sin(angle_x)],
                    [0.0, math.sin(angle_x), math.cos(angle_x)],
                ]
            )
            rotation = rotation_x @ rotation_y
            translation = np.array([45.0, 2.0, 360.0])
            rng = np.random.default_rng(12345)
            for sample in samples:
                run_dir = session / "captures" / sample["sample_id"] / "attempt_01"
                output_dir = run_dir / "stereo_3d"
                output_dir.mkdir(parents=True)
                truth = np.asarray(sample["expected_relative_pat_mm"], dtype=np.float64)
                centre = rotation @ truth + translation
                count = 401
                points = centre + rng.normal(0.0, 0.015, size=(count, 3))
                npz_path = output_dir / "stereo_3d_points.npz"
                np.savez(
                    npz_path,
                    t_sec=np.linspace(0.0, 0.4, count),
                    points_left_cam_mm=points,
                    valid=np.ones(count, dtype=bool),
                )
                sample["status"] = "processed"
                sample["accepted_run_dir"] = str(run_dir)
                sample["stereo_3d_npz"] = str(npz_path)
                sample["stage_arrival"] = {
                    "readback_global_mm": sample["stage_command_global_mm"],
                    "readback_error_mm": 0.0,
                }

            manifest = {
                "schema_version": 1,
                "session_dir": str(session),
                "stereo_calibration": str(resolved["stereo_calibration"]),
                "plan_hash": plan_hash(samples),
                "samples": samples,
            }
            manifest_path = session / "stereo_stage_accuracy_manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            command = [
                sys.executable,
                str(ROOT / "stereo_stage_accuracy_analyze.py"),
                str(session),
                "--skip-processing",
                "--minimum-valid-samples",
                "20",
            ]
            completed = subprocess.run(
                command,
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, msg=completed.stdout)
            summary_path = session / "analysis" / "stereo_stage_accuracy_summary.json"
            self.assertTrue(summary_path.exists())
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["usable_static_captures"], len(samples))
            self.assertTrue(all(station["pass"] for station in summary["stations"]))
            self.assertTrue(summary["maximum_accurate_range"]["available"])
            self.assertLess(summary["registration"]["reference_fit_rms_mm"], 0.01)
            self.assertAlmostEqual(
                summary["stage_line_diagnostic"]["scale_mm_per_mm"],
                1.0,
                delta=0.001,
            )


if __name__ == "__main__":
    unittest.main()
