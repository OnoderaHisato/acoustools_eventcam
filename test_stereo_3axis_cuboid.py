#!/usr/bin/env python3

from __future__ import annotations

import json
import csv
import tempfile
import unittest
from collections import Counter
from itertools import product
from pathlib import Path

import numpy as np

from stereo_3axis_cuboid_analyze import (
    build_pair_rows,
    load_session_rows,
    summarize_pairs,
)
from stereo_3axis_stage_accuracy_common import (
    generate_plan,
    load_json,
    validate_config,
    validation_sweeps,
)


ROOT = Path(__file__).resolve().parent


class ConfigInheritanceTests(unittest.TestCase):
    def test_recursive_object_merge_replaces_arrays(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base.json"
            child = root / "child.json"
            base.write_text(
                json.dumps({"nested": {"kept": 1, "changed": 2}, "items": [1, 2]}),
                encoding="utf-8",
            )
            child.write_text(
                json.dumps(
                    {
                        "extends_config": "base.json",
                        "nested": {"changed": 3},
                        "items": [9],
                    }
                ),
                encoding="utf-8",
            )
            merged = load_json(child)
        self.assertEqual(merged["nested"], {"kept": 1, "changed": 3})
        self.assertEqual(merged["items"], [9])
        self.assertNotIn("extends_config", merged)


class CuboidPlanTests(unittest.TestCase):
    def test_compact_cuboid_sweep_expands_to_three_surface_grids(self) -> None:
        experiment = {
            "validation_sweeps": [
                {
                    "name": "axis_control",
                    "group": "axis",
                    "reference_global_mm": [0, 10, 0],
                    "points_global_mm": [[1, 10, 0]],
                },
                {
                    "name": "depth_control",
                    "group": "depth",
                    "reference_global_mm": [0, 0, 0],
                    "points_global_mm": [[0, 1, 0]],
                },
                {
                    "name": "cube",
                    "group": "diagonal",
                    "geometry": "cuboid_surface_grid",
                    "reference_global_mm": [0, 50, 0],
                    "approach_axis": "y",
                    "cuboid_half_extents_mm": [20, 25, 30],
                },
            ]
        }
        cube = next(sweep for sweep in validation_sweeps(experiment) if sweep["name"] == "cube")
        self.assertEqual(len(cube["points"]), 78)
        for extent in (20.0, 25.0, 30.0):
            shell = [
                point
                for point in cube["points"]
                if np.isclose(np.max(np.abs(point - cube["reference"])), extent)
            ]
            self.assertEqual(len(shell), 26)
            classes = Counter(
                int(np.count_nonzero(point - cube["reference"])) for point in shell
            )
            self.assertEqual(classes, Counter({1: 6, 2: 12, 3: 8}))

    def test_all_review_configs_have_expected_envelope_and_count(self) -> None:
        for d0 in (210, 410, 610):
            path = ROOT / f"stereo_3axis_cuboid_config_d{d0}.json"
            config = load_json(path)
            validate_config(config, path)
            plan = generate_plan(config)
            cuboid = [
                sample
                for sample in plan
                if sample["sweep_name"].startswith("cuboid_")
                and sample["data_role"] == "validation_diagonal"
            ]
            self.assertEqual(len(plan), 1985)
            self.assertEqual(len(cuboid), 1872)
            xyz = np.asarray(
                [
                    [sample["target_global_mm"][axis] for axis in ("x", "y", "z")]
                    for sample in cuboid
                ]
            )
            np.testing.assert_allclose(xyz.min(axis=0), [-30.0, 22.0, -30.0])
            np.testing.assert_allclose(xyz.max(axis=0), [30.0, 163.0, 30.0])
            self.assertFalse(
                config["safety_acknowledgements"][
                    "full_motion_envelope_and_cables_confirmed"
                ]
            )


class CuboidPairTests(unittest.TestCase):
    def test_load_surface_grid_rows_classifies_all_26_points(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory)
            analysis = session / "analysis_3axis"
            analysis.mkdir()
            config = {
                "absolute_distance_reference": {"distance_mm": 210.0},
                "experiment": {
                    "validation_sweeps": [
                        {
                            "name": "axis_control",
                            "group": "axis",
                            "reference_global_mm": [0, 0, 0],
                            "points_global_mm": [[1, 0, 0]],
                        },
                        {
                            "name": "depth_control",
                            "group": "depth",
                            "reference_global_mm": [0, 0, 0],
                            "points_global_mm": [[0, 1, 0]],
                        },
                        {
                            "name": "cuboid_y050",
                            "group": "diagonal",
                            "geometry": "cuboid_surface_grid",
                            "reference_global_mm": [0, 50, 0],
                            "approach_axis": "y",
                            "cuboid_half_extents_mm": [20],
                        },
                    ]
                },
            }
            (session / "session_config.json").write_text(
                json.dumps(config), encoding="utf-8"
            )
            fields = [
                "capture_id",
                "sweep_name",
                "cycle",
                "direction",
                "usable",
                "reason",
                *[f"plan_target_{axis}_mm" for axis in "xyz"],
                *[f"measured_stage_{axis}_mm" for axis in "xyz"],
                *[f"stage_readback_{axis}_mm" for axis in "xyz"],
            ]
            with (analysis / "measurements.csv").open(
                "w", encoding="utf-8-sig", newline=""
            ) as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for index, signs in enumerate(product((-1, 0, 1), repeat=3)):
                    if signs == (0, 0, 0):
                        continue
                    point = np.asarray([0.0, 50.0, 0.0]) + 20.0 * np.asarray(signs)
                    row = {
                        "capture_id": f"q{index:04d}",
                        "sweep_name": "cuboid_y050",
                        "cycle": "1",
                        "direction": "positive",
                        "usable": "true",
                        "reason": "",
                    }
                    for prefix in ("plan_target", "measured_stage", "stage_readback"):
                        for axis, value in zip("xyz", point):
                            row[f"{prefix}_{axis}_mm"] = value
                    writer.writerow(row)
            rows, rejected = load_session_rows(session)
        self.assertEqual(len(rows), 26)
        self.assertFalse(rejected)
        self.assertEqual(
            Counter(row["point_class"] for row in rows),
            Counter({"face_center": 6, "edge_midpoint": 12, "vertex": 8}),
        )

    def test_complete_cube_produces_edges_face_and_body_diagonals(self) -> None:
        extent = 20.0
        rows = []
        signs_list = [
            signs
            for signs in product((-1, 0, 1), repeat=3)
            if signs != (0, 0, 0)
        ]
        for index, signs in enumerate(signs_list):
            point = extent * np.asarray(signs, dtype=float)
            nonzero_count = sum(value != 0 for value in signs)
            rows.append(
                {
                    "session": "synthetic",
                    "session_dir": "synthetic",
                    "d0_mm": 210.0,
                    "capture_id": f"q{index:04d}",
                    "sweep_name": "cuboid_y100",
                    "cycle": "1",
                    "direction": "positive",
                    "center_y_mm": 100.0,
                    "absolute_center_distance_mm": 310.0,
                    "half_extent_mm": extent,
                    "side_length_mm": 2.0 * extent,
                    "geometry": "cuboid_surface_grid",
                    "expected_point_count": 26,
                    "point_class": ("face_center", "edge_midpoint", "vertex")[
                        nonzero_count - 1
                    ],
                    "vertex_sign_x": signs[0],
                    "vertex_sign_y": signs[1],
                    "vertex_sign_z": signs[2],
                    "plan": point.copy(),
                    "camera": point.copy(),
                    "stage": point.copy(),
                    "residual": np.zeros(3),
                    "vertex_error_3d_mm": 0.0,
                }
            )
        pairs, coverage = build_pair_rows(rows)
        self.assertEqual(len(pairs), 28)
        self.assertTrue(coverage[0]["complete"])
        self.assertEqual(coverage[0]["usable_surface_point_count"], 26)
        self.assertEqual(coverage[0]["usable_vertex_count"], 8)
        self.assertEqual(
            Counter(row["pair_type"] for row in pairs),
            Counter({"edge": 12, "face_diagonal": 12, "body_diagonal": 4}),
        )
        self.assertTrue(all(abs(row["camera_minus_stage_mm"]) < 1e-12 for row in pairs))
        summary = summarize_pairs(pairs)
        self.assertEqual(len(summary), 3)
        self.assertTrue(all(row["maximum_absolute_error_mm"] == 0.0 for row in summary))


if __name__ == "__main__":
    unittest.main()
