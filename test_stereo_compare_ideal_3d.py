from __future__ import annotations

import numpy as np

from stereo_compare_ideal_3d import (
    candidate_score,
    fit_rigid,
    interpolate_ideal,
    transform_points,
)


def test_fit_rigid_recovers_known_camera_to_pat_transform() -> None:
    rng = np.random.default_rng(42)
    camera = rng.normal(size=(200, 3)) * np.asarray([12.0, 8.0, 5.0])
    angle = np.deg2rad(31.0)
    expected_rotation = np.asarray(
        [
            [np.cos(angle), -np.sin(angle), 0.0],
            [np.sin(angle), np.cos(angle), 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    expected_translation = np.asarray([18.0, -7.0, 103.0])
    pat = transform_points(camera, expected_rotation, expected_translation)

    rotation, translation = fit_rigid(camera, pat)

    np.testing.assert_allclose(rotation, expected_rotation, atol=1e-12)
    np.testing.assert_allclose(translation, expected_translation, atol=1e-12)
    assert np.linalg.det(rotation) > 0.999999999


def test_interpolate_ideal_rejects_outside_support() -> None:
    ideal_t = np.asarray([0.0, 0.5, 1.0])
    ideal_xyz = np.column_stack([ideal_t, 2.0 * ideal_t, -ideal_t])
    query = np.asarray([-0.1, 0.25, 0.75, 1.1])

    points, supported = interpolate_ideal(ideal_t, ideal_xyz, query)

    np.testing.assert_array_equal(supported, [False, True, True, False])
    np.testing.assert_allclose(points[1:3], [[0.25, 0.5, -0.25], [0.75, 1.5, -0.75]])
    assert np.isnan(points[[0, 3]]).all()


def test_time_search_score_finds_known_offset_with_run_fit() -> None:
    ideal_t = np.arange(0.0, 1.0, 0.001)
    ideal_xyz = np.column_stack(
        [
            5.0 * np.sin(2.0 * np.pi * 3.0 * ideal_t),
            3.0 * np.cos(2.0 * np.pi * 5.0 * ideal_t + 0.2),
            4.0 * np.sin(2.0 * np.pi * 7.0 * ideal_t + 0.7),
        ]
    )
    camera = ideal_xyz + np.asarray([20.0, -8.0, 150.0])
    known_offset = 0.037
    measured_t = ideal_t + known_offset
    candidates = np.arange(0.030, 0.045, 0.001)

    scores = [
        candidate_score(
            measured_t,
            camera,
            ideal_t,
            ideal_xyz,
            float(offset),
            "fit-run",
            None,
            None,
            0.0,
        )[0]
        for offset in candidates
    ]

    resolved = float(candidates[int(np.argmin(scores))])
    assert abs(resolved - known_offset) < 1e-12

