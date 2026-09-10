#!/usr/bin/env python3
"""Hardware-free tests for AcousTools phase/field visualization helpers."""

import unittest

import numpy as np
import torch

from acoustools_visualize_phase_field import (
    make_plane_coordinates,
    phase_boards,
    simulate_pressure,
)


class PhaseFieldVisualisationTests(unittest.TestCase):
    def test_plane_coordinate_mapping(self) -> None:
        horizontal = np.array([-1.0, 1.0], dtype=np.float32)
        vertical = np.array([-2.0, 0.0, 2.0], dtype=np.float32)
        _, _, xy = make_plane_coordinates("xy", horizontal, vertical)
        _, _, xz = make_plane_coordinates("xz", horizontal, vertical)
        _, _, yz = make_plane_coordinates("yz", horizontal, vertical)

        self.assertEqual(xy.shape, (6, 3))
        np.testing.assert_array_equal(xy[:, 2], 0.0)
        np.testing.assert_array_equal(xz[:, 1], 0.0)
        np.testing.assert_array_equal(yz[:, 0], 0.0)

    def test_phase_board_split_and_orientation(self) -> None:
        phase = np.arange(512, dtype=np.float32).reshape(1, 512, 1) * 0.001
        hologram = torch.from_numpy(np.exp(1j * phase).astype(np.complex64))
        top, bottom = phase_boards(hologram)

        self.assertEqual(top.shape, (16, 16))
        self.assertEqual(bottom.shape, (16, 16))
        self.assertAlmostEqual(float(top[0, 1]), 0.016, places=6)
        self.assertAlmostEqual(float(bottom[0, 0]), 0.256, places=6)

    def test_simulation_chunks_and_concatenates(self) -> None:
        hologram = torch.ones((1, 512, 1), dtype=torch.complex64)
        xyz = np.zeros((5, 3), dtype=np.float32)
        calls: list[int] = []

        def fake_propagate(_hologram: torch.Tensor, points: torch.Tensor) -> torch.Tensor:
            count = points.shape[2]
            calls.append(count)
            return torch.arange(count, dtype=torch.float32).to(torch.complex64).unsqueeze(0)

        result = simulate_pressure(
            hologram,
            xyz,
            chunk_size=2,
            propagate_function=fake_propagate,
        )

        self.assertEqual(calls, [2, 2, 1])
        np.testing.assert_array_equal(result.real, [0.0, 1.0, 0.0, 1.0, 0.0])


if __name__ == "__main__":
    unittest.main()
