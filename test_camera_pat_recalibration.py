#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock

import numpy as np

from pat_stereo_grid_capture import (
    create_or_resume_session,
    generate_grid,
    load_json,
    pat_mm_to_acoustools_m,
)
from pat_stereo_grid_process import pat_origin_in_left_camera
from eventcam_scale_calibration_capture import open_event_camera
from stereo_checkerboard_sync_capture import (
    EXPECTED_SQUARE_MM,
    common_capture_interval,
    synchronized_candidate_windows,
    validate_args,
)
from stereo_stage_accuracy_capture import PatTarget


ROOT = Path(__file__).resolve().parent
EVENT_DTYPE = np.dtype(
    [("x", "<u2"), ("y", "<u2"), ("p", "u1"), ("t", "<i8")]
)


def make_events(timestamps: list[int]) -> np.ndarray:
    events = np.zeros(len(timestamps), dtype=EVENT_DTYPE)
    events["x"] = 10
    events["y"] = 20
    events["p"] = 0
    events["t"] = np.asarray(timestamps, dtype=np.int64)
    return events


class CoordinateMappingTests(unittest.TestCase):
    def test_pat_registration_manifest_records_stage_readback(self) -> None:
        config = load_json(ROOT / "pat_stereo_grid_config.json")
        points = generate_grid(config)
        with tempfile.TemporaryDirectory(prefix="pat_stage_reference_") as temporary:
            args = Namespace(
                session_dir=Path(temporary) / "session",
                output_root=Path(temporary),
                dry_run=True,
                stage_hardware_readback_mm=199.98,
            )
            _, _, manifest = create_or_resume_session(
                args,
                config,
                ROOT / "pat_stereo_grid_config.json",
                points,
            )
            self.assertEqual(manifest["schema_version"], 3)
            self.assertAlmostEqual(manifest["stage_hardware_readback_mm"], 199.98)
            self.assertEqual(
                manifest["stage_reference"]["nominal_hardware_mm"],
                200.0,
            )

    def test_camera_to_pat_inverse_reports_pat_origin_in_camera_frame(self) -> None:
        angle = np.deg2rad(30.0)
        rotation = np.asarray(
            [
                [np.cos(angle), 0.0, np.sin(angle)],
                [0.0, 1.0, 0.0],
                [-np.sin(angle), 0.0, np.cos(angle)],
            ]
        )
        pat_origin_camera = np.asarray([12.0, -4.0, 350.0])
        translation = -rotation @ pat_origin_camera
        recovered = pat_origin_in_left_camera(rotation, translation)
        np.testing.assert_allclose(recovered, pat_origin_camera, atol=1e-12)
        self.assertAlmostEqual(
            float(np.linalg.norm(recovered)),
            float(np.linalg.norm(pat_origin_camera)),
        )

    def test_acoustools_and_pat_share_the_software_origin(self) -> None:
        self.assertEqual(
            pat_mm_to_acoustools_m(
                [0.0, 0.0, 0.0],
                [0.0, 0.0, 0.0],
            ),
            (0.0, 0.0, 0.0),
        )
        np.testing.assert_allclose(
            pat_mm_to_acoustools_m(
                [10.0, -5.0, 10.0],
                [0.0, 0.0, 0.0],
            ),
            [0.010, -0.005, 0.010],
        )

    def test_grid_records_pat_and_acoustools_coordinates(self) -> None:
        config = load_json(ROOT / "pat_stereo_grid_config.json")
        points = generate_grid(config)
        centre = next(
            point
            for point in points
            if point["target_pat_mm"] == [0.0, 0.0, 0.0]
        )
        self.assertEqual(centre["target_acoustools_mm"], [0.0, 0.0, 0.0])
        self.assertEqual(len(points), 27)

    def test_stage_pat_target_sends_shared_software_origin(self) -> None:
        target = PatTarget.__new__(PatTarget)
        target.acoustools_zero_in_pat_mm = np.asarray([0.0, 0.0, 0.0])
        target.compute_holograms = Mock(return_value=["hologram"])
        target.mute_sync_transducer = Mock(side_effect=lambda value: value)
        self.assertEqual(
            target._hologram(np.asarray([0.0, 0.0, 0.0])),
            "hologram",
        )
        target.compute_holograms.assert_called_once_with([(0.0, 0.0, 0.0)])


class SynchronizedCheckerboardTests(unittest.TestCase):
    def test_explicit_camera_serial_opens_without_full_usb_discovery(self) -> None:
        device = object()
        initiate_device = Mock(return_value=device)
        discovery_list = Mock(return_value=["00000508", "00000509"])
        metavision_hal = Namespace(
            DeviceDiscovery=Namespace(list=discovery_list),
        )

        opened = open_event_camera(
            initiate_device,
            metavision_hal,
            "00000508",
            retries=1,
            delay_sec=0.0,
        )

        self.assertIs(opened, device)
        initiate_device.assert_called_once_with(path="00000508")
        discovery_list.assert_not_called()

    def test_candidate_windows_share_exact_start_and_end(self) -> None:
        left = make_events(list(range(1000, 2000, 10)))
        right = make_events(list(range(1005, 2000, 10)))
        windows = synchronized_candidate_windows(
            left,
            right,
            common_start_us=1100,
            common_end_us=1900,
            window_us_values=[200],
            hop_us=100,
            minimum_events=10,
            candidate_count=3,
        )
        self.assertEqual(len(windows), 3)
        for row in windows:
            self.assertEqual(row["end_ts_us"] - row["start_ts_us"], 200)
            self.assertGreaterEqual(row["left_events"], 10)
            self.assertGreaterEqual(row["right_events"], 10)

    def test_preferred_window_is_ranked_before_longer_higher_count_window(
        self,
    ) -> None:
        left = make_events(list(range(0, 1000, 10)))
        right = make_events(list(range(0, 1000, 10)))
        windows = synchronized_candidate_windows(
            left,
            right,
            common_start_us=0,
            common_end_us=1000,
            window_us_values=[400, 200],
            hop_us=100,
            minimum_events=10,
            candidate_count=1,
            preferred_window_us=200,
        )
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0]["frame_window_us"], 200)
        self.assertGreater(
            windows[1]["joint_event_score"],
            windows[0]["joint_event_score"],
        )

    def test_common_interval_requires_shared_hardware_clock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sync_npz_") as temporary:
            left_path = Path(temporary) / "left.npz"
            right_path = Path(temporary) / "right.npz"
            common = {
                "events": make_events([100, 200]),
                "hardware_synchronized": np.asarray(True),
                "timestamp_domain_id": np.asarray("hardware-master:00508"),
                "sync_mode_verified": np.asarray(True),
                "capture_interval_complete": np.asarray(True),
                "event_limit_reached": np.asarray(False),
            }
            np.savez(
                left_path,
                **common,
                sync_mode_applied=np.asarray("master"),
                output_start_ts_us=np.asarray(100),
                output_end_ts_us=np.asarray(500),
            )
            np.savez(
                right_path,
                **common,
                sync_mode_applied=np.asarray("slave"),
                output_start_ts_us=np.asarray(100),
                output_end_ts_us=np.asarray(500),
            )
            with np.load(left_path, allow_pickle=False) as left, np.load(
                right_path,
                allow_pickle=False,
            ) as right:
                self.assertEqual(common_capture_interval(left, right), (100, 500))

    def test_capture_rejects_historical_square_size(self) -> None:
        args = Namespace(
            checkerboard_image=ROOT / "checkerboard_10x7_normal.png",
            square_mm=7.1,
            left_serial="00000508",
            right_serial="00000509",
            pose_count=30,
            duration_sec=0.8,
            start_delay_sec=1.0,
            hw_sync_timeout_sec=10.0,
            camera_reopen_wait_sec=1.0,
            delta_t_us=1000,
            sensor_width=1280,
            sensor_height=720,
            window_hop_us=5000,
            candidate_count=8,
            min_events_per_camera=500,
            max_attempts_per_pose=3,
            frame_window_us_list="10000,20000",
            scale_percentile=99.5,
        )
        with self.assertRaisesRegex(SystemExit, "historical 7.1"):
            validate_args(args)

    def test_single_calibration_lock_stops_before_reading_images(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "single_camera_calibrate.py"),
                "--images",
                str(ROOT / "does_not_exist"),
                "--square-mm",
                "7.1",
                "--require-square-mm",
                f"{EXPECTED_SQUARE_MM:.2f}",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("safety lock failed", completed.stderr)
        self.assertNotIn("No images found", completed.stderr)


if __name__ == "__main__":
    unittest.main()
