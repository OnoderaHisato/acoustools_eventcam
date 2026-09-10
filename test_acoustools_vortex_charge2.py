#!/usr/bin/env python3
"""Hardware-free tests for the focus + charge-2 vortex hologram construction."""

from __future__ import annotations

import unittest

import numpy as np

from acoustools_vortex_charge2_hologram import (
    BOARD_SIZE,
    BOTTOM_SLICE,
    CONVENTIONS,
    TOP_SLICE,
    build_board,
    compose_hologram,
    element_winding_number,
    focus_hologram,
    helical_signature,
    symmetry_report,
)
from acoustools_vortex_charge2_sphere_field import (
    azimuthal_spectrum,
    ring_points,
    winding_number,
)


def make_hologram(convention: str, charge: int = 2):
    signs = CONVENTIONS[convention]
    board = build_board()
    signature = helical_signature(
        board, charge=charge, bottom_sign=signs["bottom"], top_sign=signs["top"]
    )
    return compose_hologram(focus_hologram(board), signature), board, signature


class BoardGeometryTest(unittest.TestCase):
    def test_first_half_is_the_top_board(self):
        board = build_board().detach().cpu().numpy().real
        self.assertEqual(board.shape, (2 * BOARD_SIZE, 3))
        self.assertTrue((board[TOP_SLICE, 2] > 0).all())
        self.assertTrue((board[BOTTOM_SLICE, 2] < 0).all())

    def test_boards_share_the_same_lateral_lattice_order(self):
        board = build_board().detach().cpu().numpy().real
        np.testing.assert_allclose(
            board[TOP_SLICE, :2], board[BOTTOM_SLICE, :2], atol=1e-12
        )


class HologramFormatTest(unittest.TestCase):
    def test_shape_dtype_and_unit_magnitude(self):
        for convention in CONVENTIONS:
            with self.subTest(convention=convention):
                hologram, _, _ = make_hologram(convention)
                self.assertEqual(hologram.shape, (1, 2 * BOARD_SIZE, 1))
                self.assertEqual(hologram.dtype, np.complex64)
                self.assertTrue(np.isfinite(hologram).all())
                np.testing.assert_allclose(np.abs(hologram), 1.0, atol=1e-6)

    def test_signature_is_exactly_the_requested_charge_times_azimuth(self):
        for convention, signs in CONVENTIONS.items():
            with self.subTest(convention=convention):
                _, board, signature = make_hologram(convention, charge=2)
                positions = board.detach().cpu().numpy().real
                azimuth = np.arctan2(positions[:, 1], positions[:, 0])
                np.testing.assert_allclose(
                    signature[TOP_SLICE], signs["top"] * 2 * azimuth[TOP_SLICE], atol=1e-12
                )
                np.testing.assert_allclose(
                    signature[BOTTOM_SLICE],
                    signs["bottom"] * 2 * azimuth[BOTTOM_SLICE],
                    atol=1e-12,
                )

    def test_removing_the_signature_recovers_the_plain_focus(self):
        hologram, board, signature = make_hologram("counter_rotating")
        focus = focus_hologram(board).detach().cpu().numpy().reshape(-1)
        recovered = hologram.reshape(-1) * np.exp(-1j * signature)
        residual = np.angle(np.exp(1j * (np.angle(recovered) - np.angle(focus))))
        self.assertLess(float(np.max(np.abs(residual))), 1e-4)


class ChiralityTest(unittest.TestCase):
    def test_element_winding_matches_the_requested_per_board_sign(self):
        expected = {
            "counter_rotating": (-2.0, 2.0),
            "co_rotating": (2.0, 2.0),
        }
        for convention, (top, bottom) in expected.items():
            with self.subTest(convention=convention):
                hologram, board, _ = make_hologram(convention)
                winding = element_winding_number(hologram, board)
                self.assertAlmostEqual(winding["top_ring_winding_number"], top, places=6)
                self.assertAlmostEqual(
                    winding["bottom_ring_winding_number"], bottom, places=6
                )

    def test_even_charge_is_invariant_under_a_180_degree_rotation(self):
        for convention in CONVENTIONS:
            with self.subTest(convention=convention):
                hologram, board, _ = make_hologram(convention, charge=2)
                report = symmetry_report(hologram, board, 2)
                self.assertLess(
                    report["rotation_180_about_z_max_wrapped_diff_rad"], 1e-5
                )

    def test_counter_rotating_top_is_the_mirror_image_of_the_bottom(self):
        hologram, board, _ = make_hologram("counter_rotating")
        report = symmetry_report(hologram, board, 2)
        self.assertLess(report["mirror_x_top_vs_bottom_max_wrapped_diff_rad"], 1e-5)

        hologram, board, _ = make_hologram("co_rotating")
        report = symmetry_report(hologram, board, 2)
        self.assertGreater(report["mirror_x_top_vs_bottom_max_wrapped_diff_rad"], 1.0)


class AzimuthalSpectrumTest(unittest.TestCase):
    def test_pure_vortex_is_reported_as_a_single_order(self):
        _, phi = ring_points(0.005)
        spectrum = azimuthal_spectrum(np.exp(2j * phi), phi)
        self.assertEqual(spectrum["dominant_order"], 2)
        self.assertAlmostEqual(spectrum["energy_fraction"]["2"], 1.0, places=6)
        self.assertAlmostEqual(spectrum["chirality_contrast"], 1.0, places=6)

    def test_standing_pattern_splits_energy_evenly(self):
        _, phi = ring_points(0.005)
        spectrum = azimuthal_spectrum(2.0 * np.cos(2 * phi), phi)
        self.assertAlmostEqual(spectrum["energy_fraction"]["2"], 0.5, places=6)
        self.assertAlmostEqual(spectrum["energy_fraction"]["-2"], 0.5, places=6)
        self.assertAlmostEqual(spectrum["chirality_contrast"], 0.0, places=6)

    def test_winding_number_counts_a_travelling_vortex(self):
        _, phi = ring_points(0.005)
        self.assertAlmostEqual(winding_number(np.exp(2j * phi)), 2.0, places=6)
        self.assertAlmostEqual(winding_number(np.exp(-2j * phi)), -2.0, places=6)


if __name__ == "__main__":
    unittest.main()
