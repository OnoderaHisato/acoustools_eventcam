#!/usr/bin/env python3

from __future__ import annotations

import tempfile
import unittest
import gc
from pathlib import Path

import cv2
import numpy as np

import stereo_checkerboard_evaluate as evaluate


class StereoCheckerboardEvaluateTests(unittest.TestCase):
    def test_candidate_order_spreads_early_samples_across_time(self) -> None:
        rows = [
            {
                "start_ts_us": start,
                "end_ts_us": start + 10,
                "joint_event_score": 1000 - index,
            }
            for index, start in enumerate(range(0, 100, 10))
        ]
        ordered = evaluate.temporal_spread_order(rows)
        first_four = [int(row["start_ts_us"]) for row in ordered[:4]]
        self.assertEqual(first_four[0], 0)
        self.assertGreaterEqual(max(first_four) - min(first_four), 80)

    def test_memory_maps_uncompressed_npz_events(self) -> None:
        events = np.zeros(
            4,
            dtype=[("x", "<u2"), ("y", "<u2"), ("p", "<i2"), ("t", "<i8")],
        )
        events["x"] = [1, 2, 3, 4]
        events["t"] = [10, 20, 30, 40]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.npz"
            np.savez(path, events=events)
            mapped = evaluate.load_events(path)
            self.assertIsInstance(mapped, np.memmap)
            np.testing.assert_array_equal(mapped, events)
            mapped._mmap.close()
            del mapped
            gc.collect()

    def test_corner_alignment_and_triangulation_preserve_square_size(self) -> None:
        rows, cols = 6, 9
        square_mm = 5.341125
        camera_matrix = np.asarray(
            [[1200.0, 0.0, 640.0], [0.0, 1200.0, 360.0], [0.0, 0.0, 1.0]]
        )
        rotation = np.eye(3, dtype=np.float64)
        translation = np.asarray([[-120.0], [0.0], [0.0]])
        points = np.zeros((rows * cols, 3), dtype=np.float64)
        points[:, :2] = (
            np.mgrid[0:cols, 0:rows].T.reshape(-1, 2) * square_mm
        )
        points[:, 0] -= 20.0
        points[:, 1] -= 12.0
        points[:, 2] = 500.0

        left, _ = cv2.projectPoints(
            points,
            np.zeros((3, 1)),
            np.zeros((3, 1)),
            camera_matrix,
            np.zeros(5),
        )
        right_rvec, _ = cv2.Rodrigues(rotation)
        right, _ = cv2.projectPoints(
            points,
            right_rvec,
            translation,
            camera_matrix,
            np.zeros(5),
        )
        tx = np.asarray(
            [
                [0.0, -translation[2, 0], translation[1, 0]],
                [translation[2, 0], 0.0, -translation[0, 0]],
                [-translation[1, 0], translation[0, 0], 0.0],
            ]
        )
        inverse_k = np.linalg.inv(camera_matrix)
        fundamental = inverse_k.T @ tx @ rotation @ inverse_k
        reversed_right = right.reshape(rows, cols, 2)[::-1, ::-1].reshape(-1, 2)

        aligned, orientation, epipolar_rms = evaluate.align_right_corners(
            left.reshape(-1, 2),
            reversed_right,
            rows,
            cols,
            fundamental,
            {
                "left_k": camera_matrix,
                "left_dist": np.zeros(5),
                "right_k": camera_matrix,
                "right_dist": np.zeros(5),
                "R": rotation,
                "T": translation,
            },
        )
        self.assertEqual(orientation, "flip_rows_cols")
        self.assertLess(epipolar_rms, 1e-9)

        calibration = {
            "left_k": camera_matrix,
            "left_dist": np.zeros(5),
            "right_k": camera_matrix,
            "right_dist": np.zeros(5),
            "R": rotation,
            "T": translation,
        }
        reconstructed, left_error, right_error = evaluate.triangulate_corners(
            left.reshape(-1, 2), aligned, calibration
        )
        edges = evaluate.edge_rows_for_sample(
            1, reconstructed, rows, cols, square_mm
        )
        distances = np.asarray([edge["measured_mm"] for edge in edges])
        self.assertEqual(distances.size, 93)
        np.testing.assert_allclose(distances, square_mm, rtol=0.0, atol=1e-9)
        self.assertLess(float(np.max(left_error)), 1e-9)
        self.assertLess(float(np.max(right_error)), 1e-9)


if __name__ == "__main__":
    unittest.main()
