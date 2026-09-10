#!/usr/bin/env python3
"""Hardware-free tests for the precomputed AcousTools phase-map sender."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np
import torch

from acoustools_send_phase_npy import (
    COMPLEX_HOLOGRAM,
    EXPECTED_SHAPE,
    REAL_PHASE_RADIANS,
    TOTAL_TRANSDUCERS,
    load_phase_map,
)


class PhaseMapLoadTests(unittest.TestCase):
    def save(self, root: str, value: np.ndarray) -> Path:
        path = Path(root) / "phase.npy"
        np.save(path, value)
        return path

    def test_loads_exact_complex64_two_board_shape(self) -> None:
        phase = np.exp(
            1j * np.linspace(-np.pi, np.pi, np.prod(EXPECTED_SHAPE), dtype=np.float32)
        ).astype(np.complex64).reshape(EXPECTED_SHAPE)
        with TemporaryDirectory() as temporary:
            tensor, info = load_phase_map(self.save(temporary, phase))

        self.assertEqual(tuple(tensor.shape), EXPECTED_SHAPE)
        self.assertEqual(tensor.dtype, torch.complex64)
        self.assertEqual(info.dtype, "complex64")
        self.assertEqual(info.source_format, COMPLEX_HOLOGRAM)
        self.assertLessEqual(info.amplitude_max, 1.0 + 1e-5)

    def test_rejects_wrong_shape(self) -> None:
        with TemporaryDirectory() as temporary:
            path = self.save(temporary, np.ones((1, 256, 1), dtype=np.complex64))
            with self.assertRaisesRegex(ValueError, "Expected shape"):
                load_phase_map(path)

    def test_rejects_unsupported_dtype(self) -> None:
        with TemporaryDirectory() as temporary:
            path = self.save(temporary, np.zeros(EXPECTED_SHAPE, dtype=np.complex128))
            with self.assertRaisesRegex(ValueError, "Expected dtype complex64"):
                load_phase_map(path)

            path = self.save(temporary, np.zeros(EXPECTED_SHAPE, dtype=np.int32))
            with self.assertRaisesRegex(ValueError, "Expected dtype complex64"):
                load_phase_map(path)

    def test_rejects_nonfinite_or_overdrive_values(self) -> None:
        with TemporaryDirectory() as temporary:
            nonfinite = np.ones(EXPECTED_SHAPE, dtype=np.complex64)
            nonfinite[0, 0, 0] = np.nan + 0j
            with self.assertRaisesRegex(ValueError, "NaN or infinity"):
                load_phase_map(self.save(temporary, nonfinite))

            overdrive = np.ones(EXPECTED_SHAPE, dtype=np.complex64)
            overdrive[0, 0, 0] = 1.1 + 0j
            with self.assertRaisesRegex(ValueError, "magnitude exceeds 1"):
                load_phase_map(self.save(temporary, overdrive))


class RealRadianPhaseTests(unittest.TestCase):
    def save(self, root: str, value: np.ndarray) -> Path:
        path = Path(root) / "phase.npy"
        np.save(path, value)
        return path

    def radians(self) -> np.ndarray:
        return np.linspace(-3.0, 3.0, TOTAL_TRANSDUCERS, dtype=np.float64).reshape(
            1, TOTAL_TRANSDUCERS
        )

    def test_loads_flat_radian_array_as_unit_amplitude_hologram(self) -> None:
        phase = self.radians()
        with TemporaryDirectory() as temporary:
            tensor, info = load_phase_map(self.save(temporary, phase))

        self.assertEqual(tuple(tensor.shape), EXPECTED_SHAPE)
        self.assertEqual(tensor.dtype, torch.complex64)
        self.assertEqual(info.source_format, REAL_PHASE_RADIANS)
        self.assertEqual(info.source_shape, (1, TOTAL_TRANSDUCERS))
        self.assertEqual(info.source_dtype, "float64")
        self.assertEqual(info.wrapped_elements, 0)
        np.testing.assert_allclose(np.abs(tensor.numpy()), 1.0, atol=1e-6)

    def test_radians_survive_the_round_trip(self) -> None:
        phase = self.radians()
        with TemporaryDirectory() as temporary:
            tensor, _ = load_phase_map(self.save(temporary, phase))
        recovered = np.angle(tensor.numpy()).reshape(1, TOTAL_TRANSDUCERS)
        np.testing.assert_allclose(recovered, phase, atol=1e-6)

    def test_accepts_float32_and_the_three_dimensional_shape(self) -> None:
        phase = self.radians().astype(np.float32).reshape(EXPECTED_SHAPE)
        with TemporaryDirectory() as temporary:
            tensor, info = load_phase_map(self.save(temporary, phase))
        self.assertEqual(tuple(tensor.shape), EXPECTED_SHAPE)
        self.assertEqual(info.source_dtype, "float32")
        self.assertEqual(info.source_shape, EXPECTED_SHAPE)

    def test_unwrapped_input_is_wrapped_and_counted(self) -> None:
        phase = self.radians()
        phase[0, 0] = 3.0 * np.pi
        with TemporaryDirectory() as temporary:
            tensor, info = load_phase_map(self.save(temporary, phase))
        self.assertEqual(info.wrapped_elements, 1)
        self.assertAlmostEqual(info.raw_phase_max_rad, 3.0 * np.pi, places=9)
        self.assertAlmostEqual(
            abs(float(np.angle(tensor.numpy().reshape(-1)[0]))), np.pi, places=5
        )

    def test_rejects_wrong_real_shape_and_nonfinite_values(self) -> None:
        with TemporaryDirectory() as temporary:
            path = self.save(temporary, np.zeros((1, 256), dtype=np.float64))
            with self.assertRaisesRegex(ValueError, "real radian phase array"):
                load_phase_map(path)

            nonfinite = self.radians()
            nonfinite[0, 5] = np.inf
            with self.assertRaisesRegex(ValueError, "NaN or infinity"):
                load_phase_map(self.save(temporary, nonfinite))


if __name__ == "__main__":
    unittest.main()
