#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Analyse cuboid surface grids captured by the three-axis accuracy workflow.

The ordinary three-axis analyser estimates the rigid camera-to-stage transform.
This companion analysis reports position error for face centres, edge midpoints,
and vertices.  It also compares every reconstructed vertex pair with the same
pair of Ossila readbacks, reporting edges, face diagonals, and body diagonals
separately without imposing an arbitrary pass/fail tolerance.
"""

from __future__ import annotations

import argparse
import csv
import math
import subprocess
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from stereo_3axis_stage_accuracy_common import load_json, validation_sweeps


SCRIPT_DIR = Path(__file__).resolve().parent
BASE_ANALYSER = SCRIPT_DIR / "stereo_3axis_stage_accuracy_analyze.py"
PAIR_TYPES = ("edge", "face_diagonal", "body_diagonal")
PAIR_LABELS = {
    "edge": "Edge",
    "face_diagonal": "Face diagonal",
    "body_diagonal": "Body diagonal",
}
EXTENT_COLORS = {20.0: "#245d9c", 25.0: "#c46614", 30.0: "#27823a"}
POINT_CLASSES = ("face_center", "edge_midpoint", "vertex")
POINT_CLASS_LABELS = {
    "face_center": "Face center",
    "edge_midpoint": "Edge midpoint",
    "vertex": "Vertex",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyse 3D cuboid size accuracy at one or more D0 setups.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("session_dirs", type=Path, nargs="+")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--skip-base-analysis",
        action="store_true",
        help="Use existing analysis_3axis/measurements.csv files.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="For one session, monitor capture with the base analyser before cuboid analysis.",
    )
    parser.add_argument("--watch-interval-sec", type=float, default=5.0)
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Forward --continue-on-error to the base analyser.",
    )
    return parser.parse_args()


def run_base_analysis(session_dir: Path, args: argparse.Namespace) -> None:
    command = [sys.executable, str(BASE_ANALYSER), str(session_dir)]
    if args.watch:
        command.extend(
            ["--watch", "--watch-interval-sec", str(args.watch_interval_sec)]
        )
    if args.continue_on_error:
        command.append("--continue-on-error")
    print("[BASE ANALYSIS] " + subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, check=True)


def as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def vector(row: dict[str, Any], prefix: str) -> np.ndarray:
    return np.asarray(
        [float(row[f"{prefix}_{axis}_mm"]) for axis in ("x", "y", "z")],
        dtype=np.float64,
    )


def cuboid_definitions(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    definitions: dict[str, dict[str, Any]] = {}
    for sweep in validation_sweeps(config["experiment"]):
        geometry = str(sweep.get("geometry", ""))
        if geometry not in {"cuboid_vertices", "cuboid_surface_grid"}:
            continue
        definitions[str(sweep["name"])] = {
            "reference": np.asarray(sweep["reference"], dtype=np.float64),
            "half_extents": tuple(
                float(value) for value in sweep["cuboid_half_extents_mm"]
            ),
            "geometry": geometry,
            "expected_point_count": 26 if geometry == "cuboid_surface_grid" else 8,
        }
    if not definitions:
        raise SystemExit(
            "No cuboid_vertices or cuboid_surface_grid sweeps were found in the config."
        )
    return definitions


def load_session_rows(session_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    session_dir = session_dir.resolve()
    config_path = session_dir / "session_config.json"
    measurements_path = session_dir / "analysis_3axis" / "measurements.csv"
    config = load_json(config_path)
    definitions = cuboid_definitions(config)
    d0_mm = float(config["absolute_distance_reference"]["distance_mm"])
    with measurements_path.open("r", encoding="utf-8-sig", newline="") as stream:
        source_rows = list(csv.DictReader(stream))

    rows: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for source in source_rows:
        sweep_name = str(source.get("sweep_name", ""))
        if sweep_name not in definitions:
            continue
        definition = definitions[sweep_name]
        if not as_bool(source.get("usable")):
            rejected.append(
                {
                    "session": session_dir.name,
                    "capture_id": source.get("capture_id", ""),
                    "sweep_name": sweep_name,
                    "cycle": source.get("cycle", ""),
                    "direction": source.get("direction", ""),
                    "reason": source.get("reason", "unusable"),
                }
            )
            continue
        try:
            plan = vector(source, "plan_target")
            camera = vector(source, "measured_stage")
            stage = vector(source, "stage_readback")
        except (KeyError, TypeError, ValueError) as exc:
            rejected.append(
                {
                    "session": session_dir.name,
                    "capture_id": source.get("capture_id", ""),
                    "sweep_name": sweep_name,
                    "cycle": source.get("cycle", ""),
                    "direction": source.get("direction", ""),
                    "reason": f"invalid coordinate: {exc}",
                }
            )
            continue
        delta = plan - definition["reference"]
        extent = float(np.max(np.abs(delta)))
        if not any(math.isclose(extent, candidate, abs_tol=1e-6) for candidate in definition["half_extents"]):
            rejected.append(
                {
                    "session": session_dir.name,
                    "capture_id": source.get("capture_id", ""),
                    "sweep_name": sweep_name,
                    "cycle": source.get("cycle", ""),
                    "direction": source.get("direction", ""),
                    "reason": f"unexpected half extent {extent:g} mm",
                }
            )
            continue
        signs = tuple(int(round(value / extent)) for value in delta)
        allowed_signs = (
            {-1, 0, 1}
            if definition["geometry"] == "cuboid_surface_grid"
            else {-1, 1}
        )
        nonzero_count = sum(value != 0 for value in signs)
        if (
            any(value not in allowed_signs for value in signs)
            or nonzero_count == 0
        ):
            rejected.append(
                {
                    "session": session_dir.name,
                    "capture_id": source.get("capture_id", ""),
                    "sweep_name": sweep_name,
                    "cycle": source.get("cycle", ""),
                    "direction": source.get("direction", ""),
                    "reason": f"not a cuboid surface point: signs={signs}",
                }
            )
            continue
        point_class = POINT_CLASSES[nonzero_count - 1]
        residual = camera - stage
        point_error_3d_mm = float(np.linalg.norm(residual))
        rows.append(
            {
                "session": session_dir.name,
                "session_dir": str(session_dir),
                "d0_mm": d0_mm,
                "capture_id": str(source["capture_id"]),
                "sweep_name": sweep_name,
                "cycle": str(source.get("cycle", "")),
                "direction": str(source.get("direction", "")),
                "center_y_mm": float(definition["reference"][1]),
                "absolute_center_distance_mm": d0_mm
                + float(definition["reference"][1]),
                "half_extent_mm": extent,
                "side_length_mm": 2.0 * extent,
                "geometry": definition["geometry"],
                "expected_point_count": definition["expected_point_count"],
                "point_class": point_class,
                "vertex_sign_x": signs[0],
                "vertex_sign_y": signs[1],
                "vertex_sign_z": signs[2],
                "plan": plan,
                "camera": camera,
                "stage": stage,
                "residual": residual,
                "point_error_3d_mm": point_error_3d_mm,
                "vertex_error_3d_mm": point_error_3d_mm,
            }
        )
    return rows, rejected


def pair_orientation(sign_a: tuple[int, int, int], sign_b: tuple[int, int, int]) -> str:
    changed = [axis for axis, a, b in zip("xyz", sign_a, sign_b) if a != b]
    return "".join(changed)


def build_pair_rows(
    vertex_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in vertex_rows:
        key = (
            row["session"],
            row["sweep_name"],
            row["cycle"],
            row["direction"],
            row["half_extent_mm"],
        )
        grouped[key].append(row)

    pair_rows: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    for key, members in sorted(grouped.items()):
        unique_points = {
            (
                int(row["vertex_sign_x"]),
                int(row["vertex_sign_y"]),
                int(row["vertex_sign_z"]),
            ): row
            for row in members
        }
        unique_vertices = {
            signs: row
            for signs, row in unique_points.items()
            if str(row.get("point_class", "vertex")) == "vertex"
        }
        exemplar = members[0]
        expected_point_count = int(exemplar.get("expected_point_count", 8))
        points_complete = len(unique_points) == expected_point_count
        vertices_complete = len(unique_vertices) == 8
        coverage.append(
            {
                "session": exemplar["session"],
                "sweep_name": exemplar["sweep_name"],
                "cycle": exemplar["cycle"],
                "direction": exemplar["direction"],
                "absolute_center_distance_mm": exemplar[
                    "absolute_center_distance_mm"
                ],
                "half_extent_mm": exemplar["half_extent_mm"],
                "side_length_mm": exemplar["side_length_mm"],
                "geometry": exemplar.get("geometry", "cuboid_vertices"),
                "usable_surface_point_count": len(unique_points),
                "expected_surface_point_count": expected_point_count,
                "surface_complete": points_complete,
                "usable_vertex_count": len(unique_vertices),
                "expected_vertex_count": 8,
                "vertices_complete": vertices_complete,
                "complete": points_complete and vertices_complete,
            }
        )
        for (sign_a, first), (sign_b, second) in combinations(
            unique_vertices.items(), 2
        ):
            changed_count = sum(a != b for a, b in zip(sign_a, sign_b))
            pair_type = PAIR_TYPES[changed_count - 1]
            stage_delta = second["stage"] - first["stage"]
            camera_delta = second["camera"] - first["camera"]
            stage_distance = float(np.linalg.norm(stage_delta))
            camera_distance = float(np.linalg.norm(camera_delta))
            nominal_distance = (
                2.0 * float(exemplar["half_extent_mm"]) * math.sqrt(changed_count)
            )
            pair_rows.append(
                {
                    "session": exemplar["session"],
                    "session_dir": exemplar["session_dir"],
                    "d0_mm": exemplar["d0_mm"],
                    "sweep_name": exemplar["sweep_name"],
                    "cycle": exemplar["cycle"],
                    "direction": exemplar["direction"],
                    "absolute_center_distance_mm": exemplar[
                        "absolute_center_distance_mm"
                    ],
                    "half_extent_mm": exemplar["half_extent_mm"],
                    "side_length_mm": exemplar["side_length_mm"],
                    "pair_type": pair_type,
                    "pair_orientation": pair_orientation(sign_a, sign_b),
                    "capture_id_a": first["capture_id"],
                    "capture_id_b": second["capture_id"],
                    "vertex_a": "".join("+" if value > 0 else "-" for value in sign_a),
                    "vertex_b": "".join("+" if value > 0 else "-" for value in sign_b),
                    "nominal_distance_mm": nominal_distance,
                    "stage_distance_mm": stage_distance,
                    "camera_distance_mm": camera_distance,
                    "stage_minus_nominal_mm": stage_distance - nominal_distance,
                    "camera_minus_nominal_mm": camera_distance - nominal_distance,
                    "camera_minus_stage_mm": camera_distance - stage_distance,
                }
            )
    return pair_rows, coverage


def statistics(values: Iterable[float]) -> dict[str, float | int]:
    data = np.asarray(list(values), dtype=np.float64)
    if not data.size:
        return {"n": 0}
    absolute = np.abs(data)
    return {
        "n": int(data.size),
        "mean_error_mm": float(np.mean(data)),
        "standard_deviation_mm": (
            float(np.std(data, ddof=1)) if data.size > 1 else 0.0
        ),
        "mean_absolute_error_mm": float(np.mean(absolute)),
        "rmse_mm": float(np.sqrt(np.mean(np.square(data)))),
        "p95_absolute_error_mm": float(np.percentile(absolute, 95.0)),
        "minimum_error_mm": float(np.min(data)),
        "maximum_error_mm": float(np.max(data)),
        "maximum_absolute_error_mm": float(np.max(absolute)),
    }


def summarize_pairs(pair_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in pair_rows:
        key = (
            row["session"],
            float(row["d0_mm"]),
            float(row["absolute_center_distance_mm"]),
            float(row["half_extent_mm"]),
            str(row["pair_type"]),
        )
        groups[key].append(row)
    result: list[dict[str, Any]] = []
    for key, members in sorted(groups.items()):
        session, d0_mm, center_distance, extent, pair_type = key
        result.append(
            {
                "session": session,
                "d0_mm": d0_mm,
                "absolute_center_distance_mm": center_distance,
                "half_extent_mm": extent,
                "side_length_mm": 2.0 * extent,
                "pair_type": pair_type,
                "cycle_count": len({row["cycle"] for row in members}),
                "direction_count": len({row["direction"] for row in members}),
                **statistics(float(row["camera_minus_stage_mm"]) for row in members),
            }
        )
    return result


def summarize_point_positions(
    point_rows: list[dict[str, Any]],
    *,
    separate_classes: bool,
) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in point_rows:
        base_key = (
            row["session"],
            float(row["d0_mm"]),
            float(row["absolute_center_distance_mm"]),
            float(row["half_extent_mm"]),
        )
        key = (
            (*base_key, str(row["point_class"]))
            if separate_classes
            else (*base_key, "all_surface_points")
        )
        groups[key].append(row)
    result: list[dict[str, Any]] = []
    for key, members in sorted(groups.items()):
        session, d0_mm, center_distance, extent, point_class = key
        magnitudes = np.asarray(
            [
                float(
                    row["point_error_3d_mm"]
                    if "point_error_3d_mm" in row
                    else row["vertex_error_3d_mm"]
                )
                for row in members
            ],
            dtype=np.float64,
        )
        result.append(
            {
                "session": session,
                "d0_mm": d0_mm,
                "absolute_center_distance_mm": center_distance,
                "half_extent_mm": extent,
                "side_length_mm": 2.0 * extent,
                "point_class": point_class,
                "n": int(magnitudes.size),
                "mean_point_error_3d_mm": float(np.mean(magnitudes)),
                "standard_deviation_mm": (
                    float(np.std(magnitudes, ddof=1)) if magnitudes.size > 1 else 0.0
                ),
                "rmse_point_error_3d_mm": float(
                    np.sqrt(np.mean(np.square(magnitudes)))
                ),
                "p95_point_error_3d_mm": float(np.percentile(magnitudes, 95.0)),
                "maximum_point_error_3d_mm": float(np.max(magnitudes)),
            }
        )
    return result


def summarize_vertices(vertex_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Backward-compatible vertex-only position summary."""
    rows = [
        row
        for row in vertex_rows
        if str(row.get("point_class", "vertex")) == "vertex"
    ]
    return summarize_point_positions(rows, separate_classes=False)


def csv_ready(row: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, np.ndarray):
            for axis, component in zip("xyz", value):
                output[f"{key}_{axis}_mm"] = float(component)
        else:
            output[key] = value
    return output


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    converted = [csv_ready(row) for row in rows]
    if not converted:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in converted:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(converted)


def plot_pair_summary(summary: list[dict[str, Any]], output_dir: Path) -> list[Path]:
    figure, axes = plt.subplots(3, 2, figsize=(15.0, 12.0), sharex=True, constrained_layout=True)
    sessions = sorted({str(row["session"]) for row in summary})
    extents = sorted({float(row["half_extent_mm"]) for row in summary})
    for row_index, pair_type in enumerate(PAIR_TYPES):
        signed_axis, magnitude_axis = axes[row_index]
        for extent in extents:
            color = EXTENT_COLORS.get(extent, None)
            first_label = True
            for session in sessions:
                members = sorted(
                    (
                        row
                        for row in summary
                        if row["pair_type"] == pair_type
                        and float(row["half_extent_mm"]) == extent
                        and row["session"] == session
                    ),
                    key=lambda row: float(row["absolute_center_distance_mm"]),
                )
                if not members:
                    continue
                x = np.asarray([float(row["absolute_center_distance_mm"]) for row in members])
                mean = np.asarray([float(row["mean_error_mm"]) for row in members])
                sd = np.asarray([float(row["standard_deviation_mm"]) for row in members])
                label = f"side {2 * extent:g} mm" if first_label else None
                signed_axis.plot(x, mean, marker="o", color=color, label=label)
                signed_axis.fill_between(x, mean - sd, mean + sd, color=color, alpha=0.16)
                magnitude_axis.plot(
                    x,
                    [float(row["rmse_mm"]) for row in members],
                    marker="o",
                    color=color,
                    label=label,
                )
                magnitude_axis.plot(
                    x,
                    [float(row["maximum_absolute_error_mm"]) for row in members],
                    marker="x",
                    linestyle="--",
                    color=color,
                )
                first_label = False
        signed_axis.axhline(0.0, color="black", linewidth=0.8)
        signed_axis.set_ylabel(f"{PAIR_LABELS[pair_type]}\nmean +/- 1 SD [mm]")
        magnitude_axis.set_ylabel("RMSE / max |error| [mm]")
        signed_axis.grid(True, alpha=0.25)
        magnitude_axis.grid(True, alpha=0.25)
    axes[0, 0].legend(loc="best", ncols=min(3, len(extents)))
    axes[0, 1].text(
        0.02,
        0.96,
        "solid: RMSE   dashed x: maximum absolute error",
        transform=axes[0, 1].transAxes,
        va="top",
        fontsize=9,
    )
    axes[-1, 0].set_xlabel("Absolute cuboid-center distance [mm]")
    axes[-1, 1].set_xlabel("Absolute cuboid-center distance [mm]")
    figure.suptitle("Cuboid dimension error across distance and object size", fontsize=15)
    paths = [
        output_dir / "cuboid_dimension_error_summary.png",
        output_dir / "cuboid_dimension_error_summary.svg",
    ]
    figure.savefig(paths[0], dpi=200)
    figure.savefig(paths[1])
    plt.close(figure)
    return paths


def plot_point_summary(summary: list[dict[str, Any]], output_dir: Path) -> list[Path]:
    figure, (mean_axis, maximum_axis) = plt.subplots(
        2, 1, figsize=(14.0, 9.0), sharex=True, constrained_layout=True
    )
    sessions = sorted({str(row["session"]) for row in summary})
    extents = sorted({float(row["half_extent_mm"]) for row in summary})
    for extent in extents:
        color = EXTENT_COLORS.get(extent, None)
        first_label = True
        for session in sessions:
            members = sorted(
                (
                    row
                    for row in summary
                    if float(row["half_extent_mm"]) == extent
                    and row["session"] == session
                ),
                key=lambda row: float(row["absolute_center_distance_mm"]),
            )
            if not members:
                continue
            x = np.asarray([float(row["absolute_center_distance_mm"]) for row in members])
            mean = np.asarray([float(row["mean_point_error_3d_mm"]) for row in members])
            sd = np.asarray([float(row["standard_deviation_mm"]) for row in members])
            label = f"side {2 * extent:g} mm" if first_label else None
            mean_axis.plot(x, mean, marker="o", color=color, label=label)
            mean_axis.fill_between(x, np.maximum(0.0, mean - sd), mean + sd, color=color, alpha=0.16)
            maximum_axis.plot(
                x,
                [float(row["maximum_point_error_3d_mm"]) for row in members],
                marker="x",
                linestyle="--",
                color=color,
                label=label,
            )
            first_label = False
    mean_axis.set_ylabel("Mean surface-point 3D error +/- 1 SD [mm]")
    maximum_axis.set_ylabel("Maximum surface-point 3D error [mm]")
    maximum_axis.set_xlabel("Absolute cuboid-center distance [mm]")
    for axis in (mean_axis, maximum_axis):
        axis.grid(True, alpha=0.25)
        axis.legend(loc="best", ncols=min(3, len(extents)))
    figure.suptitle(
        "Cuboid surface-point error across distance and object size", fontsize=15
    )
    paths = [
        output_dir / "cuboid_point_position_error_summary.png",
        output_dir / "cuboid_point_position_error_summary.svg",
    ]
    figure.savefig(paths[0], dpi=200)
    figure.savefig(paths[1])
    plt.close(figure)
    return paths


def plot_point_class_summary(
    summary: list[dict[str, Any]], output_dir: Path
) -> list[Path]:
    figure, axes = plt.subplots(
        3, 2, figsize=(15.0, 12.0), sharex=True, constrained_layout=True
    )
    sessions = sorted({str(row["session"]) for row in summary})
    extents = sorted({float(row["half_extent_mm"]) for row in summary})
    for row_index, point_class in enumerate(POINT_CLASSES):
        mean_axis, maximum_axis = axes[row_index]
        for extent in extents:
            color = EXTENT_COLORS.get(extent, None)
            first_label = True
            for session in sessions:
                members = sorted(
                    (
                        row
                        for row in summary
                        if row["point_class"] == point_class
                        and float(row["half_extent_mm"]) == extent
                        and row["session"] == session
                    ),
                    key=lambda row: float(row["absolute_center_distance_mm"]),
                )
                if not members:
                    continue
                x = np.asarray(
                    [float(row["absolute_center_distance_mm"]) for row in members]
                )
                mean = np.asarray(
                    [float(row["mean_point_error_3d_mm"]) for row in members]
                )
                sd = np.asarray(
                    [float(row["standard_deviation_mm"]) for row in members]
                )
                label = f"side {2 * extent:g} mm" if first_label else None
                mean_axis.plot(x, mean, marker="o", color=color, label=label)
                mean_axis.fill_between(
                    x,
                    np.maximum(0.0, mean - sd),
                    mean + sd,
                    color=color,
                    alpha=0.16,
                )
                maximum_axis.plot(
                    x,
                    [float(row["maximum_point_error_3d_mm"]) for row in members],
                    marker="x",
                    linestyle="--",
                    color=color,
                    label=label,
                )
                first_label = False
        mean_axis.set_ylabel(
            f"{POINT_CLASS_LABELS[point_class]}\nmean +/- 1 SD [mm]"
        )
        maximum_axis.set_ylabel("Maximum 3D error [mm]")
        mean_axis.grid(True, alpha=0.25)
        maximum_axis.grid(True, alpha=0.25)
    axes[0, 0].legend(loc="best", ncols=min(3, len(extents)))
    axes[-1, 0].set_xlabel("Absolute cuboid-center distance [mm]")
    axes[-1, 1].set_xlabel("Absolute cuboid-center distance [mm]")
    figure.suptitle("Cuboid 3D error by surface-point class", fontsize=15)
    paths = [
        output_dir / "cuboid_point_class_error_summary.png",
        output_dir / "cuboid_point_class_error_summary.svg",
    ]
    figure.savefig(paths[0], dpi=200)
    figure.savefig(paths[1])
    plt.close(figure)
    return paths


def write_report(
    output_dir: Path,
    pair_summary: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
) -> Path:
    complete = sum(bool(row["complete"]) for row in coverage)
    lines = [
        "# Three-dimensional cuboid accuracy",
        "",
        "No fixed acceptance tolerance is applied. Errors are reported as reconstructed "
        "camera pair distance minus the corresponding Ossila-readback pair distance.",
        "",
        f"- Complete cuboid surface-grid passes: {complete}/{len(coverage)}",
        f"- Rejected/unusable surface-point rows: {len(rejected)}",
        "",
        "| Session | Center distance [mm] | Side [mm] | Pair | N | Mean [mm] | RMSE [mm] | P95 abs [mm] | Max abs [mm] |",
        "|---|---:|---:|---|---:|---:|---:|---:|---:|",
    ]
    for row in pair_summary:
        lines.append(
            f"| {row['session']} | {float(row['absolute_center_distance_mm']):.1f} | "
            f"{float(row['side_length_mm']):.1f} | {PAIR_LABELS[str(row['pair_type'])]} | "
            f"{int(row['n'])} | {float(row['mean_error_mm']):.4f} | "
            f"{float(row['rmse_mm']):.4f} | "
            f"{float(row['p95_absolute_error_mm']):.4f} | "
            f"{float(row['maximum_absolute_error_mm']):.4f} |"
        )
    lines.extend(
        [
            "",
            "## Output interpretation",
            "",
            "- Edge: nominal lengths 40, 50, or 60 mm.",
            "- Face diagonal: nominal lengths 56.57, 70.71, or 84.85 mm.",
            "- Body diagonal: nominal lengths 69.28, 86.60, or 103.92 mm.",
            "- A positive error means the stereo reconstruction measured the pair too long.",
            "- Surface-point 3D error evaluates position; vertex-pair error evaluates object dimensions.",
            "",
        ]
    )
    path = output_dir / "cuboid_accuracy_report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    args = parse_args()
    sessions = [path.resolve() for path in args.session_dirs]
    if args.watch and len(sessions) != 1:
        raise SystemExit("--watch accepts exactly one session directory.")
    if not math.isfinite(args.watch_interval_sec) or args.watch_interval_sec <= 0:
        raise SystemExit("--watch-interval-sec must be positive and finite.")
    if not args.skip_base_analysis:
        for session in sessions:
            run_base_analysis(session, args)

    if args.output_dir is not None:
        output_dir = args.output_dir.resolve()
    elif len(sessions) == 1:
        output_dir = sessions[0] / "analysis_3axis" / "cuboid_analysis"
    else:
        output_dir = sessions[0].parent / "combined_cuboid_analysis"
    output_dir.mkdir(parents=True, exist_ok=True)

    points: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for session in sessions:
        session_points, session_rejected = load_session_rows(session)
        points.extend(session_points)
        rejected.extend(session_rejected)
    if not points:
        raise SystemExit("No usable cuboid surface points were found after base analysis.")

    vertices = [row for row in points if row["point_class"] == "vertex"]
    pair_rows, coverage = build_pair_rows(points)
    pair_summary = summarize_pairs(pair_rows)
    point_summary = summarize_point_positions(points, separate_classes=False)
    point_class_summary = summarize_point_positions(points, separate_classes=True)
    vertex_summary = summarize_vertices(vertices)
    write_csv(output_dir / "cuboid_surface_points.csv", points)
    write_csv(output_dir / "cuboid_vertices.csv", vertices)
    write_csv(output_dir / "cuboid_pairwise_measurements.csv", pair_rows)
    write_csv(output_dir / "cuboid_dimension_summary.csv", pair_summary)
    write_csv(output_dir / "cuboid_point_summary.csv", point_summary)
    write_csv(output_dir / "cuboid_point_class_summary.csv", point_class_summary)
    write_csv(output_dir / "cuboid_vertex_summary.csv", vertex_summary)
    write_csv(output_dir / "cuboid_coverage.csv", coverage)
    write_csv(output_dir / "cuboid_rejected_points.csv", rejected)
    outputs = [
        *plot_pair_summary(pair_summary, output_dir),
        *plot_point_summary(point_summary, output_dir),
        *plot_point_class_summary(point_class_summary, output_dir),
        write_report(output_dir, pair_summary, coverage, rejected),
    ]
    print(f"Cuboid surface points: {len(points)}")
    print(f"Cuboid vertices used for dimensions: {len(vertices)}")
    print(f"Cuboid vertex pairs: {len(pair_rows)}")
    print(
        f"Complete cuboid surface-grid passes: "
        f"{sum(bool(row['complete']) for row in coverage)}/"
        f"{len(coverage)}"
    )
    for path in outputs:
        print(f"Saved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
