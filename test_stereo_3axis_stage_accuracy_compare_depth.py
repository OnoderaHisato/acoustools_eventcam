# -*- coding: utf-8 -*-

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from stereo_3axis_stage_accuracy_compare_depth import (
    combined_rows,
    load_depth_session,
    load_sessions,
    load_validation_rows,
    plot_validation_type_summary,
    summarize_rows,
    summarize_validation_types,
)


class CombinedDepthAnalysisTest(unittest.TestCase):
    def make_session(self, root: Path, name: str, d0_mm: float) -> Path:
        session = root / name
        analysis = session / "analysis_3axis"
        analysis.mkdir(parents=True)
        (session / "session_config.json").write_text(
            json.dumps(
                {
                    "absolute_distance_reference": {
                        "available": True,
                        "distance_mm": d0_mm,
                        "reference_command_global_mm": [0.0, 0.0, 0.0],
                    }
                }
            ),
            encoding="utf-8",
        )
        rows = [
            {
                "reference_id": "q0001",
                "cycle": cycle,
                "direction": direction,
                "start_plan_y_mm": 0.0,
                "end_plan_y_mm": 20.0,
                "midpoint_distance_label_mm": d0_mm + 10.0,
                "stage_step_mm": 20.0,
                "camera_step_mm": 20.0 + error,
                "distance_error_mm": error,
                "axial_error_mm": error,
                "lateral_error_mm": abs(error) / 2.0,
                "error_3d_mm": abs(error),
            }
            for cycle, direction, error in (
                (1, "positive", 0.2),
                (1, "negative", -0.1),
                (2, "positive", 0.3),
            )
        ]
        path = analysis / "validation_depth_incremental.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        validation_rows = []
        for index, (family, axis, x, y, z, error) in enumerate(
            (
                ("single_axis", "x", 5.0, 20.0, 0.0, 0.05),
                ("single_axis", "z", 0.0, 20.0, 5.0, -0.04),
                ("depth", "y", 0.0, 40.0, 0.0, 0.12),
                ("diagonal", "multi", 15.0, 40.0, -15.0, 0.08),
            ),
            start=1,
        ):
            validation_rows.append(
                {
                    "capture_id": f"q{index:04d}",
                    "reference_id": "q0000",
                    "validation_family": family,
                    "direction": "positive",
                    "cycle": "1",
                    "dominant_axis": axis,
                    "plan_target_x_mm": x,
                    "plan_target_y_mm": y,
                    "plan_target_z_mm": z,
                    "reference_target_x_mm": 0.0,
                    "reference_target_y_mm": y if family != "depth" else 0.0,
                    "reference_target_z_mm": 0.0,
                    "distance_norm_error_mm": error,
                    "axial_error_mm": error,
                    "lateral_error_mm": abs(error) / 2.0,
                    "error_3d_mm": abs(error),
                    "absolute_distance_label_mm": d0_mm + y,
                }
            )
        validation_path = analysis / "validation_accuracy.csv"
        with validation_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream, fieldnames=list(validation_rows[0])
            )
            writer.writeheader()
            writer.writerows(validation_rows)
        return session

    def test_loads_and_sorts_sessions_by_d0(self) -> None:
        with tempfile.TemporaryDirectory(prefix="combined_depth_") as temporary:
            root = Path(temporary)
            near = self.make_session(root, "near", 210.0)
            far = self.make_session(root, "far", 610.0)
            sessions = load_sessions([far, near])
        self.assertEqual([session.d0_mm for session in sessions], [210.0, 610.0])
        self.assertEqual(sessions[0].measured_min_mm, 210.0)
        self.assertEqual(sessions[0].measured_max_mm, 230.0)
        self.assertEqual(sessions[0].plotted_min_mm, 220.0)

    def test_summary_uses_all_repeats_at_each_midpoint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="combined_depth_") as temporary:
            session_path = self.make_session(Path(temporary), "near", 210.0)
            session = load_depth_session(session_path)
            summary = summarize_rows(combined_rows([session]))
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["n"], 3)
        self.assertEqual(summary[0]["cycle_count"], 2)
        self.assertEqual(summary[0]["direction_count"], 2)
        self.assertAlmostEqual(summary[0]["mean_error_mm"], 0.4 / 3.0)
        self.assertAlmostEqual(summary[0]["max_absolute_error_mm"], 0.3)

    def test_rejects_midpoint_inconsistent_with_d0(self) -> None:
        with tempfile.TemporaryDirectory(prefix="combined_depth_") as temporary:
            session_path = self.make_session(Path(temporary), "near", 210.0)
            path = (
                session_path
                / "analysis_3axis"
                / "validation_depth_incremental.csv"
            )
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace("220.0", "221.0"), encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "Distance-label mismatch"):
                load_depth_session(session_path)

    def test_validation_summary_keeps_validation_types_separate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="combined_depth_") as temporary:
            session_path = self.make_session(Path(temporary), "near", 210.0)
            session = load_depth_session(session_path)
            rows = load_validation_rows(session)
            summary = summarize_validation_types(rows)
        self.assertEqual(
            {row["validation_type"] for row in summary},
            {"axis_x", "axis_z", "depth", "diagonal"},
        )
        axis_x = next(row for row in summary if row["validation_type"] == "axis_x")
        self.assertEqual(axis_x["absolute_distance_label_mm"], 230.0)
        self.assertAlmostEqual(axis_x["rmse_mm"], 0.05)

    def test_writes_separate_validation_summary_png_and_svg(self) -> None:
        with tempfile.TemporaryDirectory(prefix="combined_depth_") as temporary:
            root = Path(temporary)
            near = self.make_session(root, "near", 210.0)
            far = self.make_session(root, "far", 610.0)
            sessions = load_sessions([near, far])
            rows = [
                row for session in sessions for row in load_validation_rows(session)
            ]
            summary = summarize_validation_types(rows)
            outputs = plot_validation_type_summary(
                sessions,
                rows,
                summary,
                "axis_x",
                "Single-axis X validation",
                "axis_x_summary_test",
                root,
            )
            self.assertEqual(
                {path.suffix for path in outputs},
                {".png", ".svg"},
            )
            self.assertTrue(all(path.is_file() for path in outputs))


if __name__ == "__main__":
    unittest.main()
