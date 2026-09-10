#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compare all validation results from multiple 3-axis sessions.

The per-session analyser places each consecutive-step error at the midpoint of
the two mechanical distance labels.  This script preserves that convention,
puts all sessions on one absolute-distance axis, and explicitly marks gaps that
were not measured.  It also combines single-axis X/Z, D0-referenced depth, and
diagonal validation without mixing signed displacements or diagonal corners.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Iterable

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D


INCREMENTAL_FILENAME = "validation_depth_incremental.csv"
VALIDATION_FILENAME = "validation_accuracy.csv"
REQUIRED_COLUMNS = {
    "reference_id",
    "cycle",
    "direction",
    "start_plan_y_mm",
    "end_plan_y_mm",
    "midpoint_distance_label_mm",
    "stage_step_mm",
    "camera_step_mm",
    "distance_error_mm",
    "axial_error_mm",
    "lateral_error_mm",
    "error_3d_mm",
}
NUMERIC_COLUMNS = REQUIRED_COLUMNS - {"reference_id", "cycle", "direction"}
VALIDATION_REQUIRED_COLUMNS = {
    "capture_id",
    "reference_id",
    "validation_family",
    "direction",
    "cycle",
    "dominant_axis",
    "plan_target_x_mm",
    "plan_target_y_mm",
    "plan_target_z_mm",
    "reference_target_x_mm",
    "reference_target_y_mm",
    "reference_target_z_mm",
    "distance_norm_error_mm",
    "axial_error_mm",
    "lateral_error_mm",
    "error_3d_mm",
    "absolute_distance_label_mm",
}
VALIDATION_NUMERIC_COLUMNS = {
    "plan_target_x_mm",
    "plan_target_y_mm",
    "plan_target_z_mm",
    "reference_target_x_mm",
    "reference_target_y_mm",
    "reference_target_z_mm",
    "distance_norm_error_mm",
    "axial_error_mm",
    "lateral_error_mm",
    "error_3d_mm",
    "absolute_distance_label_mm",
}
CYCLE_COLORS = {"1": "#1f77b4", "2": "#ff7f0e", "3": "#2ca02c"}
DIRECTION_STYLES = {
    "positive": {"marker": "o", "linestyle": "-", "label": "Positive approach"},
    "negative": {"marker": "^", "linestyle": "--", "label": "Negative approach"},
}
SESSION_FACE_COLORS = ("#eaf2fb", "#fff2e2", "#eaf7ea", "#f4eafa")


@dataclass(frozen=True)
class DepthSession:
    session_dir: Path
    name: str
    d0_mm: float
    rows: tuple[dict[str, Any], ...]

    @property
    def measured_min_mm(self) -> float:
        return self.d0_mm + min(
            min(float(row["start_plan_y_mm"]), float(row["end_plan_y_mm"]))
            for row in self.rows
        )

    @property
    def measured_max_mm(self) -> float:
        return self.d0_mm + max(
            max(float(row["start_plan_y_mm"]), float(row["end_plan_y_mm"]))
            for row in self.rows
        )

    @property
    def plotted_min_mm(self) -> float:
        return min(float(row["midpoint_distance_label_mm"]) for row in self.rows)

    @property
    def plotted_max_mm(self) -> float:
        return max(float(row["midpoint_distance_label_mm"]) for row in self.rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Combine all 3-axis validation results on a common absolute "
            "mechanical-distance axis."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "session_dirs",
        nargs="+",
        type=Path,
        help="Analysed 3-axis session directories, ordered automatically by D0.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=(
            "Destination directory. By default, a combined_3axis_analysis "
            "directory is created beside the first session."
        ),
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Show the generated figures after saving them.",
    )
    return parser.parse_args()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"JSON root must be an object: {path}")
    return value


def _absolute_reference_candidates(document: dict[str, Any]) -> Iterable[dict[str, Any]]:
    direct = document.get("absolute_distance_reference")
    if isinstance(direct, dict):
        yield direct
    for container_name in ("config", "settings"):
        container = document.get(container_name)
        if not isinstance(container, dict):
            continue
        reference = container.get("absolute_distance_reference")
        if isinstance(reference, dict):
            yield reference


def load_d0_mm(session_dir: Path) -> float:
    checked: list[Path] = []
    values: list[tuple[Path, float]] = []
    for path in (
        session_dir / "session_config.json",
        session_dir / "stereo_3axis_stage_accuracy_manifest.json",
    ):
        if not path.exists():
            continue
        checked.append(path)
        document = _load_json(path)
        for reference in _absolute_reference_candidates(document):
            value = reference.get("distance_mm")
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise SystemExit(f"Invalid distance_mm in {path}: {value!r}") from exc
            if math.isfinite(number):
                values.append((path, number))
    if not values:
        locations = ", ".join(str(path) for path in checked) or "no config/manifest found"
        raise SystemExit(f"D0 distance_mm not found for {session_dir} ({locations}).")
    unique = sorted({round(value, 9) for _, value in values})
    if len(unique) != 1:
        details = ", ".join(f"{path}: {value:g}" for path, value in values)
        raise SystemExit(f"Conflicting D0 values for {session_dir}: {details}")
    return float(unique[0])


def _find_incremental_csv(session_dir: Path) -> Path:
    candidates = (
        session_dir / "analysis_3axis" / INCREMENTAL_FILENAME,
        session_dir / INCREMENTAL_FILENAME,
    )
    for path in candidates:
        if path.exists():
            return path
    expected = " or ".join(str(path) for path in candidates)
    raise SystemExit(f"Depth incremental analysis not found. Expected {expected}")


def load_depth_session(session_dir: Path) -> DepthSession:
    session_dir = session_dir.resolve()
    csv_path = _find_incremental_csv(session_dir)
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        columns = set(reader.fieldnames or ())
        missing = sorted(REQUIRED_COLUMNS - columns)
        if missing:
            raise SystemExit(f"Missing columns in {csv_path}: {', '.join(missing)}")
        rows: list[dict[str, Any]] = []
        for line_number, source in enumerate(reader, start=2):
            row: dict[str, Any] = dict(source)
            try:
                for column in NUMERIC_COLUMNS:
                    row[column] = float(source[column])
            except (TypeError, ValueError) as exc:
                raise SystemExit(
                    f"Invalid numeric value in {csv_path}:{line_number}: {exc}"
                ) from exc
            row["cycle"] = str(source["cycle"]).strip()
            row["direction"] = str(source["direction"]).strip().lower()
            if row["direction"] not in DIRECTION_STYLES:
                raise SystemExit(
                    f"Unknown direction in {csv_path}:{line_number}: {row['direction']!r}"
                )
            rows.append(row)
    if not rows:
        raise SystemExit(f"No rows in {csv_path}")

    d0_mm = load_d0_mm(session_dir)
    for row in rows:
        expected_midpoint = d0_mm + 0.5 * (
            float(row["start_plan_y_mm"]) + float(row["end_plan_y_mm"])
        )
        actual_midpoint = float(row["midpoint_distance_label_mm"])
        if not math.isclose(actual_midpoint, expected_midpoint, abs_tol=1e-6):
            raise SystemExit(
                f"Distance-label mismatch in {csv_path}: expected {expected_midpoint:g} mm "
                f"from D0 and interval, found {actual_midpoint:g} mm."
            )
    return DepthSession(
        session_dir=session_dir,
        name=session_dir.name,
        d0_mm=d0_mm,
        rows=tuple(rows),
    )


def load_validation_rows(session: DepthSession) -> list[dict[str, Any]]:
    path = session.session_dir / "analysis_3axis" / VALIDATION_FILENAME
    if not path.exists():
        fallback = session.session_dir / VALIDATION_FILENAME
        if fallback.exists():
            path = fallback
        else:
            raise SystemExit(
                f"Validation analysis not found. Expected {path} or {fallback}"
            )
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        columns = set(reader.fieldnames or ())
        missing = sorted(VALIDATION_REQUIRED_COLUMNS - columns)
        if missing:
            raise SystemExit(f"Missing columns in {path}: {', '.join(missing)}")
        rows: list[dict[str, Any]] = []
        for line_number, source in enumerate(reader, start=2):
            row: dict[str, Any] = dict(source)
            try:
                for column in VALIDATION_NUMERIC_COLUMNS:
                    row[column] = float(source[column])
            except (TypeError, ValueError) as exc:
                raise SystemExit(
                    f"Invalid numeric value in {path}:{line_number}: {exc}"
                ) from exc
            row["cycle"] = str(source["cycle"]).strip()
            row["direction"] = str(source["direction"]).strip().lower()
            row["validation_family"] = str(source["validation_family"]).strip().lower()
            row["dominant_axis"] = str(source["dominant_axis"]).strip().lower()
            expected_distance = session.d0_mm + float(row["plan_target_y_mm"])
            actual_distance = float(row["absolute_distance_label_mm"])
            if not math.isclose(actual_distance, expected_distance, abs_tol=1e-6):
                raise SystemExit(
                    f"Absolute-distance mismatch in {path}:{line_number}: expected "
                    f"{expected_distance:g} mm, found {actual_distance:g} mm."
                )
            if row["direction"] not in DIRECTION_STYLES:
                raise SystemExit(
                    f"Unknown direction in {path}:{line_number}: {row['direction']!r}"
                )
            delta = {
                axis: float(row[f"plan_target_{axis}_mm"])
                - float(row[f"reference_target_{axis}_mm"])
                for axis in ("x", "y", "z")
            }
            rows.append(
                {
                    "session": session.name,
                    "session_dir": str(session.session_dir),
                    "d0_mm": session.d0_mm,
                    "signed_delta_x_mm": delta["x"],
                    "signed_delta_y_mm": delta["y"],
                    "signed_delta_z_mm": delta["z"],
                    **row,
                }
            )
    if not rows:
        raise SystemExit(f"No rows in {path}")
    return rows


def load_sessions(session_dirs: Iterable[Path]) -> list[DepthSession]:
    sessions = sorted(
        (load_depth_session(path) for path in session_dirs),
        key=lambda session: session.d0_mm,
    )
    if len(sessions) < 2:
        raise SystemExit("At least two sessions are required for a combined plot.")
    duplicate_d0 = [
        value
        for value, count in _counts(session.d0_mm for session in sessions).items()
        if count > 1
    ]
    if duplicate_d0:
        raise SystemExit(
            "Duplicate D0 values are ambiguous: "
            + ", ".join(f"{value:g} mm" for value in duplicate_d0)
        )
    return sessions


def _counts(values: Iterable[Any]) -> dict[Any, int]:
    result: dict[Any, int] = defaultdict(int)
    for value in values:
        result[value] += 1
    return dict(result)


def combined_rows(sessions: Iterable[DepthSession]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for session in sessions:
        for row in session.rows:
            result.append(
                {
                    "session": session.name,
                    "session_dir": str(session.session_dir),
                    "d0_mm": session.d0_mm,
                    **row,
                }
            )
    return sorted(
        result,
        key=lambda row: (
            float(row["midpoint_distance_label_mm"]),
            str(row["cycle"]),
            str(row["direction"]),
        ),
    )


def summarize_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, float, float], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            str(row["session"]),
            float(row["d0_mm"]),
            round(float(row["midpoint_distance_label_mm"]), 9),
        )
        groups[key].append(row)
    result: list[dict[str, Any]] = []
    for (session, d0_mm, midpoint), members in sorted(
        groups.items(), key=lambda item: (item[0][1], item[0][2])
    ):
        errors = np.asarray(
            [float(member["distance_error_mm"]) for member in members], dtype=float
        )
        absolute = np.abs(errors)
        result.append(
            {
                "session": session,
                "d0_mm": d0_mm,
                "midpoint_distance_label_mm": midpoint,
                "n": int(errors.size),
                "cycle_count": len({str(member["cycle"]) for member in members}),
                "direction_count": len(
                    {str(member["direction"]) for member in members}
                ),
                "mean_error_mm": float(np.mean(errors)),
                "standard_deviation_mm": (
                    float(np.std(errors, ddof=1)) if errors.size > 1 else 0.0
                ),
                "median_error_mm": float(median(errors.tolist())),
                "mean_absolute_error_mm": float(np.mean(absolute)),
                "rmse_mm": float(np.sqrt(np.mean(np.square(errors)))),
                "min_error_mm": float(np.min(errors)),
                "max_error_mm": float(np.max(errors)),
                "max_absolute_error_mm": float(np.max(absolute)),
            }
        )
    return result


def validation_type_key(row: dict[str, Any]) -> str:
    family = str(row["validation_family"])
    axis = str(row["dominant_axis"])
    if family == "single_axis" and axis in {"x", "z"}:
        return f"axis_{axis}"
    if family == "depth":
        return "depth"
    if family == "diagonal":
        return "diagonal"
    return f"{family}_{axis}".strip("_")


def _error_statistics(values: Iterable[float]) -> dict[str, float | int]:
    errors = np.asarray(list(values), dtype=float)
    if errors.size == 0:
        raise ValueError("At least one error value is required.")
    absolute = np.abs(errors)
    return {
        "n": int(errors.size),
        "mean_error_mm": float(np.mean(errors)),
        "standard_deviation_mm": (
            float(np.std(errors, ddof=1)) if errors.size > 1 else 0.0
        ),
        "median_error_mm": float(np.median(errors)),
        "mean_absolute_error_mm": float(np.mean(absolute)),
        "rmse_mm": float(np.sqrt(np.mean(np.square(errors)))),
        "min_error_mm": float(np.min(errors)),
        "max_error_mm": float(np.max(errors)),
        "max_absolute_error_mm": float(np.max(absolute)),
    }


def summarize_validation_types(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, float, str, float], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            str(row["session"]),
            float(row["d0_mm"]),
            validation_type_key(row),
            round(float(row["absolute_distance_label_mm"]), 9),
        )
        groups[key].append(row)
    result: list[dict[str, Any]] = []
    for (session, d0_mm, validation_type, distance), members in sorted(
        groups.items(), key=lambda item: (item[0][1], item[0][2], item[0][3])
    ):
        result.append(
            {
                "session": session,
                "d0_mm": d0_mm,
                "validation_type": validation_type,
                "absolute_distance_label_mm": distance,
                "cycle_count": len({str(row["cycle"]) for row in members}),
                "direction_count": len({str(row["direction"]) for row in members}),
                **_error_statistics(
                    float(row["distance_norm_error_mm"]) for row in members
                ),
            }
        )
    return result


def summarize_validation_details(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        validation_type = validation_type_key(row)
        if validation_type == "axis_x":
            detail = f"delta_x={float(row['signed_delta_x_mm']):+g}"
        elif validation_type == "axis_z":
            detail = f"delta_z={float(row['signed_delta_z_mm']):+g}"
        elif validation_type == "diagonal":
            detail = (
                f"corner_x={float(row['plan_target_x_mm']):+g},"
                f"z={float(row['plan_target_z_mm']):+g}"
            )
        else:
            detail = validation_type
        key = (
            str(row["session"]),
            float(row["d0_mm"]),
            validation_type,
            detail,
            round(float(row["absolute_distance_label_mm"]), 9),
        )
        groups[key].append(row)
    result: list[dict[str, Any]] = []
    for (session, d0_mm, validation_type, detail, distance), members in sorted(
        groups.items(), key=lambda item: (item[0][1], item[0][2], item[0][3], item[0][4])
    ):
        result.append(
            {
                "session": session,
                "d0_mm": d0_mm,
                "validation_type": validation_type,
                "detail": detail,
                "absolute_distance_label_mm": distance,
                "cycle_count": len({str(row["cycle"]) for row in members}),
                "direction_count": len({str(row["direction"]) for row in members}),
                **_error_statistics(
                    float(row["distance_norm_error_mm"]) for row in members
                ),
            }
        )
    return result


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _cycle_color(cycle: str, cycle_index: int) -> str:
    if cycle in CYCLE_COLORS:
        return CYCLE_COLORS[cycle]
    return plt.get_cmap("tab10")(cycle_index % 10)


def _legend_handles(sessions: Iterable[DepthSession]) -> list[Line2D]:
    cycles = sorted({str(row["cycle"]) for session in sessions for row in session.rows})
    handles = [
        Line2D(
            [0],
            [0],
            color=_cycle_color(cycle, index),
            marker="o",
            linestyle="none",
            label=f"Cycle {cycle}",
        )
        for index, cycle in enumerate(cycles)
    ]
    handles.extend(
        Line2D(
            [0],
            [0],
            color="#666666",
            marker=style["marker"],
            linestyle=style["linestyle"],
            label=style["label"],
        )
        for style in DIRECTION_STYLES.values()
    )
    return handles


def _plot_raw_session(axis: Any, session: DepthSession) -> None:
    cycles = sorted({str(row["cycle"]) for row in session.rows})
    for cycle_index, cycle in enumerate(cycles):
        for direction, style in DIRECTION_STYLES.items():
            series = sorted(
                (
                    row
                    for row in session.rows
                    if str(row["cycle"]) == cycle and row["direction"] == direction
                ),
                key=lambda row: float(row["midpoint_distance_label_mm"]),
            )
            if not series:
                continue
            axis.plot(
                [float(row["midpoint_distance_label_mm"]) for row in series],
                [float(row["distance_error_mm"]) for row in series],
                color=_cycle_color(cycle, cycle_index),
                marker=style["marker"],
                linestyle=style["linestyle"],
                linewidth=1.15,
                markersize=5.0,
                alpha=0.78,
            )


def _shade_sessions(axis: Any, sessions: list[DepthSession]) -> None:
    for index, session in enumerate(sessions):
        axis.axvspan(
            session.measured_min_mm,
            session.measured_max_mm,
            color=SESSION_FACE_COLORS[index % len(SESSION_FACE_COLORS)],
            alpha=0.34,
            zorder=-10,
        )
        axis.text(
            0.5 * (session.measured_min_mm + session.measured_max_mm),
            0.93,
            f"{session.name}: D0={session.d0_mm:g} mm",
            transform=axis.get_xaxis_transform(),
            ha="center",
            va="top",
            fontsize=8.5,
            color="#333333",
        )
    for previous, following in zip(sessions[:-1], sessions[1:]):
        gap_start = previous.measured_max_mm
        gap_end = following.measured_min_mm
        if gap_end <= gap_start:
            continue
        axis.axvspan(
            gap_start,
            gap_end,
            facecolor="#eeeeee",
            edgecolor="#bbbbbb",
            hatch="///",
            alpha=0.48,
            linewidth=0.0,
            zorder=-9,
        )
        axis.text(
            0.5 * (gap_start + gap_end),
            0.5,
            f"not measured\n{gap_start:g}-{gap_end:g} mm",
            transform=axis.get_xaxis_transform(),
            ha="center",
            va="center",
            rotation=90,
            fontsize=7.5,
            color="#666666",
        )


def _validation_ranges(
    sessions: list[DepthSession], rows: Iterable[dict[str, Any]]
) -> list[tuple[DepthSession, float, float]]:
    materialized = list(rows)
    result: list[tuple[DepthSession, float, float]] = []
    for session in sessions:
        distances = [
            float(row["absolute_distance_label_mm"])
            for row in materialized
            if row["session"] == session.name
        ]
        if distances:
            result.append((session, min(distances), max(distances)))
    return result


def _shade_validation_ranges(
    axis: Any,
    sessions: list[DepthSession],
    rows: Iterable[dict[str, Any]],
) -> None:
    ranges = _validation_ranges(sessions, rows)
    for index, (_, lower, upper) in enumerate(ranges):
        axis.axvspan(
            lower,
            upper,
            color=SESSION_FACE_COLORS[index % len(SESSION_FACE_COLORS)],
            alpha=0.30,
            zorder=-10,
        )
    for (_, _, previous_upper), (_, following_lower, _) in zip(ranges[:-1], ranges[1:]):
        if following_lower <= previous_upper:
            continue
        axis.axvspan(
            previous_upper,
            following_lower,
            facecolor="#eeeeee",
            edgecolor="#bbbbbb",
            hatch="///",
            alpha=0.42,
            linewidth=0.0,
            zorder=-9,
        )


def _plot_validation_series(
    axis: Any,
    sessions: list[DepthSession],
    rows: Iterable[dict[str, Any]],
) -> None:
    materialized = list(rows)
    cycles = sorted({str(row["cycle"]) for row in materialized})
    for session in sessions:
        session_rows = [row for row in materialized if row["session"] == session.name]
        for cycle_index, cycle in enumerate(cycles):
            for direction, style in DIRECTION_STYLES.items():
                series = sorted(
                    (
                        row
                        for row in session_rows
                        if str(row["cycle"]) == cycle
                        and row["direction"] == direction
                    ),
                    key=lambda row: float(row["absolute_distance_label_mm"]),
                )
                if not series:
                    continue
                axis.plot(
                    [float(row["absolute_distance_label_mm"]) for row in series],
                    [float(row["distance_norm_error_mm"]) for row in series],
                    color=_cycle_color(cycle, cycle_index),
                    marker=style["marker"],
                    linestyle=style["linestyle"],
                    linewidth=0.95,
                    markersize=4.2,
                    alpha=0.68,
                )


def plot_axis_validation(
    sessions: list[DepthSession],
    validation_rows: list[dict[str, Any]],
    axis_name: str,
    output_dir: Path,
) -> list[Path]:
    members = [
        row
        for row in validation_rows
        if validation_type_key(row) == f"axis_{axis_name}"
    ]
    if not members:
        return []
    delta_column = f"signed_delta_{axis_name}_mm"
    deltas = sorted({round(float(row[delta_column]), 9) for row in members})
    figure, subplot_grid = plt.subplots(
        2,
        3,
        figsize=(15.2, 8.2),
        sharex=True,
        sharey=True,
        constrained_layout=True,
    )
    flat_axes = list(subplot_grid.flat)
    for axis, delta in zip(flat_axes, deltas):
        delta_rows = [
            row
            for row in members
            if math.isclose(float(row[delta_column]), delta, abs_tol=1e-6)
        ]
        _shade_validation_ranges(axis, sessions, delta_rows)
        _plot_validation_series(axis, sessions, delta_rows)
        axis.axhline(0.0, color="black", linewidth=0.85)
        axis.set_title(f"Signed delta {axis_name.upper()}={delta:+g} mm")
        axis.grid(True, alpha=0.24)
        axis.set_xlabel("Absolute mechanical distance [mm]")
    for axis in flat_axes[len(deltas) :]:
        axis.set_visible(False)
    subplot_grid[0, 0].set_ylabel("Distance-norm error [mm]")
    subplot_grid[1, 0].set_ylabel("Distance-norm error [mm]")
    figure.suptitle(
        f"Single-axis {axis_name.upper()} validation across all distance setups",
        fontsize=15,
    )
    figure.legend(
        handles=_legend_handles(sessions),
        loc="outside lower center",
        ncols=5,
    )
    return _save_figure(
        figure, output_dir, f"combined_validation_axis_{axis_name}_by_delta"
    )


def plot_diagonal_validation(
    sessions: list[DepthSession],
    validation_rows: list[dict[str, Any]],
    output_dir: Path,
) -> list[Path]:
    members = [
        row for row in validation_rows if validation_type_key(row) == "diagonal"
    ]
    if not members:
        return []
    corners = sorted(
        {
            (
                round(float(row["plan_target_x_mm"]), 9),
                round(float(row["plan_target_z_mm"]), 9),
            )
            for row in members
        }
    )
    figure, subplot_grid = plt.subplots(
        2,
        2,
        figsize=(13.8, 8.0),
        sharex=True,
        sharey=True,
        constrained_layout=True,
    )
    flat_axes = list(subplot_grid.flat)
    for axis, (corner_x, corner_z) in zip(flat_axes, corners):
        corner_rows = [
            row
            for row in members
            if math.isclose(float(row["plan_target_x_mm"]), corner_x, abs_tol=1e-6)
            and math.isclose(float(row["plan_target_z_mm"]), corner_z, abs_tol=1e-6)
        ]
        _shade_validation_ranges(axis, sessions, corner_rows)
        _plot_validation_series(axis, sessions, corner_rows)
        axis.axhline(0.0, color="black", linewidth=0.85)
        axis.set_title(f"Corner X={corner_x:+g}, Z={corner_z:+g} mm")
        axis.set_xlabel("Absolute mechanical distance [mm]")
        axis.grid(True, alpha=0.24)
    for axis in flat_axes[len(corners) :]:
        axis.set_visible(False)
    subplot_grid[0, 0].set_ylabel("Distance-norm error [mm]")
    subplot_grid[1, 0].set_ylabel("Distance-norm error [mm]")
    figure.suptitle(
        "Diagonal validation across all distance setups - X/Z corners separated",
        fontsize=15,
    )
    figure.legend(
        handles=_legend_handles(sessions),
        loc="outside lower center",
        ncols=5,
    )
    return _save_figure(figure, output_dir, "combined_validation_diagonal_by_corner")


def _session_line_color(index: int) -> str:
    return ("#245d9c", "#c46614", "#27823a", "#744a9c")[index % 4]


def plot_all_validation_summary(
    sessions: list[DepthSession],
    validation_rows: list[dict[str, Any]],
    summary_rows: list[dict[str, Any]],
    output_dir: Path,
) -> list[Path]:
    validation_types = (
        ("axis_x", "Single-axis X"),
        ("axis_z", "Single-axis Z"),
        ("depth", "Depth (D0-referenced)"),
        ("diagonal", "Diagonal"),
    )
    figure, axes = plt.subplots(
        2,
        len(validation_types),
        figsize=(17.0, 8.5),
        sharex=True,
        sharey="row",
        constrained_layout=True,
    )
    for column, (validation_type, title) in enumerate(validation_types):
        raw_type_rows = [
            row
            for row in validation_rows
            if validation_type_key(row) == validation_type
        ]
        for row_index in range(2):
            _shade_validation_ranges(axes[row_index, column], sessions, raw_type_rows)
            axes[row_index, column].grid(True, alpha=0.24)
        axes[0, column].set_title(title)
        axes[1, column].set_xlabel("Absolute distance [mm]")
        for session_index, session in enumerate(sessions):
            members = sorted(
                (
                    row
                    for row in summary_rows
                    if row["session"] == session.name
                    and row["validation_type"] == validation_type
                ),
                key=lambda row: float(row["absolute_distance_label_mm"]),
            )
            if not members:
                continue
            x = np.asarray(
                [float(row["absolute_distance_label_mm"]) for row in members]
            )
            mean = np.asarray([float(row["mean_error_mm"]) for row in members])
            standard_deviation = np.asarray(
                [float(row["standard_deviation_mm"]) for row in members]
            )
            color = _session_line_color(session_index)
            axes[0, column].plot(x, mean, color=color, marker="o", linewidth=1.4)
            axes[0, column].fill_between(
                x,
                mean - standard_deviation,
                mean + standard_deviation,
                color=color,
                alpha=0.18,
            )
            axes[1, column].plot(
                x,
                [float(row["rmse_mm"]) for row in members],
                color=color,
                marker="o",
                linewidth=1.4,
            )
            axes[1, column].plot(
                x,
                [float(row["max_absolute_error_mm"]) for row in members],
                color=color,
                marker="x",
                linestyle="--",
                linewidth=1.0,
            )
        axes[0, column].axhline(0.0, color="black", linewidth=0.85)
    axes[0, 0].set_ylabel("Mean error +/- 1 SD [mm]")
    axes[1, 0].set_ylabel("Error magnitude [mm]")
    figure.suptitle("All validation types across the three absolute-distance setups")
    session_handles = [
        Line2D(
            [0],
            [0],
            color=_session_line_color(index),
            marker="o",
            label=f"{session.name} (D0={session.d0_mm:g} mm)",
        )
        for index, session in enumerate(sessions)
    ]
    metric_handles = [
        Line2D([0], [0], color="#555555", linewidth=1.4, label="RMSE"),
        Line2D(
            [0],
            [0],
            color="#555555",
            marker="x",
            linestyle="--",
            linewidth=1.0,
            label="Max |error|",
        ),
    ]
    figure.legend(
        handles=[*session_handles, *metric_handles],
        loc="outside lower center",
        ncols=5,
    )
    return _save_figure(figure, output_dir, "combined_validation_all_types_summary")


def plot_validation_type_summary(
    sessions: list[DepthSession],
    validation_rows: list[dict[str, Any]],
    summary_rows: list[dict[str, Any]],
    validation_type: str,
    title: str,
    output_stem: str,
    output_dir: Path,
) -> list[Path]:
    """Render one full-size two-panel summary for a validation type."""
    raw_type_rows = [
        row
        for row in validation_rows
        if validation_type_key(row) == validation_type
    ]
    if not raw_type_rows:
        return []
    figure, (mean_axis, magnitude_axis) = plt.subplots(
        2,
        1,
        figsize=(14.0, 9.0),
        sharex=True,
        constrained_layout=True,
    )
    for axis in (mean_axis, magnitude_axis):
        _shade_validation_ranges(axis, sessions, raw_type_rows)
        axis.grid(True, alpha=0.25)

    session_handles: list[Line2D] = []
    magnitude_handles: list[Line2D] = []
    for session_index, session in enumerate(sessions):
        members = sorted(
            (
                row
                for row in summary_rows
                if row["session"] == session.name
                and row["validation_type"] == validation_type
            ),
            key=lambda row: float(row["absolute_distance_label_mm"]),
        )
        if not members:
            continue
        x = np.asarray(
            [float(row["absolute_distance_label_mm"]) for row in members]
        )
        mean = np.asarray([float(row["mean_error_mm"]) for row in members])
        standard_deviation = np.asarray(
            [float(row["standard_deviation_mm"]) for row in members]
        )
        color = _session_line_color(session_index)
        mean_axis.plot(x, mean, color=color, marker="o", linewidth=1.5)
        mean_axis.fill_between(
            x,
            mean - standard_deviation,
            mean + standard_deviation,
            color=color,
            alpha=0.18,
        )
        session_handles.append(
            Line2D(
                [0],
                [0],
                color=color,
                marker="o",
                label=f"{session.name} (D0={session.d0_mm:g} mm)",
            )
        )
        rmse_line = magnitude_axis.plot(
            x,
            [float(row["rmse_mm"]) for row in members],
            color=color,
            marker="o",
            linewidth=1.5,
            label=f"{session.name} RMSE",
        )[0]
        maximum_line = magnitude_axis.plot(
            x,
            [float(row["max_absolute_error_mm"]) for row in members],
            color=color,
            marker="x",
            linestyle="--",
            linewidth=1.15,
            label=f"{session.name} max |error|",
        )[0]
        magnitude_handles.extend([rmse_line, maximum_line])

    mean_axis.axhline(0.0, color="black", linewidth=0.9)
    mean_axis.set_ylabel("Mean distance-norm error +/- 1 SD [mm]")
    mean_axis.set_title(f"{title} - repeated-pass mean and dispersion")
    if session_handles:
        mean_axis.legend(handles=session_handles, loc="best", ncols=len(session_handles))
    magnitude_axis.set_ylabel("Error magnitude [mm]")
    magnitude_axis.set_xlabel("Absolute mechanical distance [mm]")
    magnitude_axis.set_title("RMSE and maximum absolute error")
    if magnitude_handles:
        magnitude_axis.legend(
            handles=magnitude_handles,
            loc="best",
            ncols=min(3, len(sessions)),
        )
    figure.suptitle(
        f"{title} across the three absolute-distance setups",
        fontsize=15,
    )
    return _save_figure(figure, output_dir, output_stem)


def plot_separate_validation_summaries(
    sessions: list[DepthSession],
    validation_rows: list[dict[str, Any]],
    summary_rows: list[dict[str, Any]],
    output_dir: Path,
) -> list[Path]:
    specifications = (
        (
            "axis_x",
            "Single-axis X validation",
            "combined_validation_axis_x_summary",
        ),
        (
            "axis_z",
            "Single-axis Z validation",
            "combined_validation_axis_z_summary",
        ),
        (
            "depth",
            "Depth validation (D0-referenced)",
            "combined_validation_depth_d0_referenced_summary",
        ),
        (
            "diagonal",
            "Diagonal validation",
            "combined_validation_diagonal_summary",
        ),
    )
    outputs: list[Path] = []
    for validation_type, title, output_stem in specifications:
        outputs.extend(
            plot_validation_type_summary(
                sessions,
                validation_rows,
                summary_rows,
                validation_type,
                title,
                output_stem,
                output_dir,
            )
        )
    return outputs


def _save_figure(figure: Any, output_dir: Path, stem: str) -> list[Path]:
    paths = [output_dir / f"{stem}.png", output_dir / f"{stem}.svg"]
    figure.savefig(paths[0], dpi=200)
    figure.savefig(paths[1])
    return paths


def plot_combined_raw(sessions: list[DepthSession], output_dir: Path) -> list[Path]:
    figure, axis = plt.subplots(figsize=(14.0, 6.8), constrained_layout=True)
    _shade_sessions(axis, sessions)
    for session in sessions:
        _plot_raw_session(axis, session)
    axis.axhline(0.0, color="black", linewidth=0.9)
    axis.set_xlim(sessions[0].measured_min_mm - 5.0, sessions[-1].measured_max_mm + 5.0)
    axis.set_xlabel("Midpoint absolute mechanical distance [mm]")
    axis.set_ylabel("Consecutive-step distance error [mm]")
    axis.set_title("Local depth increment error across all distance setups")
    axis.grid(True, alpha=0.25)
    axis.legend(handles=_legend_handles(sessions), loc="upper left", ncols=5)
    axis.text(
        0.01,
        0.01,
        "Each value is plotted at the midpoint of its interval. Lines stop at "
        "session boundaries; hatched regions were not measured.",
        transform=axis.transAxes,
        fontsize=8,
        color="#444444",
        va="bottom",
    )
    paths = _save_figure(figure, output_dir, "combined_depth_incremental")
    return paths


def plot_stitched(sessions: list[DepthSession], output_dir: Path) -> list[Path]:
    figure, axes = plt.subplots(
        1,
        len(sessions),
        figsize=(14.5, 6.4),
        sharey=True,
        constrained_layout=True,
        gridspec_kw={"wspace": 0.035},
    )
    axes_array = np.atleast_1d(axes).ravel()
    for index, (axis, session) in enumerate(zip(axes_array, sessions)):
        axis.set_facecolor(SESSION_FACE_COLORS[index % len(SESSION_FACE_COLORS)])
        _plot_raw_session(axis, session)
        axis.axhline(0.0, color="black", linewidth=0.9)
        axis.set_xlim(session.measured_min_mm - 2.0, session.measured_max_mm + 2.0)
        axis.set_title(
            f"{session.name}\nD0={session.d0_mm:g} mm; measured "
            f"{session.measured_min_mm:g}-{session.measured_max_mm:g} mm",
            fontsize=10,
        )
        axis.grid(True, alpha=0.25)
        axis.set_xlabel("Interval midpoint [mm]")
        if index:
            axis.tick_params(labelleft=False)
        if index < len(axes_array) - 1:
            axis.spines["right"].set_linestyle((0, (2, 2)))
        if index > 0:
            axis.spines["left"].set_linestyle((0, (2, 2)))
    axes_array[0].set_ylabel("Consecutive-step distance error [mm]")
    figure.suptitle(
        "Local depth increment error - stitched distance sections (shared vertical scale)",
        fontsize=15,
    )
    figure.legend(
        handles=_legend_handles(sessions),
        loc="outside lower center",
        ncols=5,
    )
    paths = _save_figure(figure, output_dir, "combined_depth_incremental_stitched")
    return paths


def plot_summary(
    sessions: list[DepthSession],
    summary: list[dict[str, Any]],
    output_dir: Path,
) -> list[Path]:
    figure, (mean_axis, magnitude_axis) = plt.subplots(
        2,
        1,
        figsize=(14.0, 9.0),
        sharex=True,
        constrained_layout=True,
    )
    for axis in (mean_axis, magnitude_axis):
        _shade_sessions(axis, sessions)
        axis.grid(True, alpha=0.25)
    for index, session in enumerate(sessions):
        members = sorted(
            (row for row in summary if row["session"] == session.name),
            key=lambda row: float(row["midpoint_distance_label_mm"]),
        )
        x = np.asarray(
            [float(row["midpoint_distance_label_mm"]) for row in members]
        )
        mean = np.asarray([float(row["mean_error_mm"]) for row in members])
        standard_deviation = np.asarray(
            [float(row["standard_deviation_mm"]) for row in members]
        )
        color = ("#245d9c", "#c46614", "#27823a", "#744a9c")[index % 4]
        mean_axis.plot(x, mean, color=color, marker="o", linewidth=1.5)
        mean_axis.fill_between(
            x,
            mean - standard_deviation,
            mean + standard_deviation,
            color=color,
            alpha=0.18,
        )
        magnitude_axis.plot(
            x,
            [float(row["rmse_mm"]) for row in members],
            color=color,
            marker="o",
            linewidth=1.5,
            label=f"{session.name} RMSE",
        )
        magnitude_axis.plot(
            x,
            [float(row["max_absolute_error_mm"]) for row in members],
            color=color,
            marker="x",
            linestyle="--",
            linewidth=1.15,
            label=f"{session.name} max |error|",
        )
    mean_axis.axhline(0.0, color="black", linewidth=0.9)
    mean_axis.set_ylabel("Mean distance error +/- 1 SD [mm]")
    mean_axis.set_title("Repeated-pass summary at each depth interval")
    magnitude_axis.set_ylabel("Error magnitude [mm]")
    magnitude_axis.set_xlabel("Midpoint absolute mechanical distance [mm]")
    magnitude_axis.legend(loc="upper left", ncols=min(3, len(sessions)))
    magnitude_axis.set_xlim(
        sessions[0].measured_min_mm - 5.0, sessions[-1].measured_max_mm + 5.0
    )
    paths = _save_figure(figure, output_dir, "combined_depth_incremental_summary")
    return paths


def _overall_statistics(rows: Iterable[dict[str, Any]]) -> dict[str, float | int]:
    errors = np.asarray([float(row["distance_error_mm"]) for row in rows], dtype=float)
    return {
        "n": int(errors.size),
        "mean": float(np.mean(errors)),
        "mae": float(np.mean(np.abs(errors))),
        "rmse": float(np.sqrt(np.mean(np.square(errors)))),
        "max_abs": float(np.max(np.abs(errors))),
    }


def write_report(
    sessions: list[DepthSession],
    rows: list[dict[str, Any]],
    output_dir: Path,
) -> Path:
    lines = [
        "# Combined depth-increment analysis",
        "",
        "Each error is plotted at the midpoint of two consecutive mechanical "
        "distance labels. The session ranges are not a single continuous sweep; "
        "the gaps shown below were not measured.",
        "",
        "| Session | D0 [mm] | Measured point span [mm] | Plotted midpoint span [mm] | N | Mean [mm] | MAE [mm] | RMSE [mm] | Max abs [mm] |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for session in sessions:
        session_rows = [row for row in rows if row["session"] == session.name]
        stats = _overall_statistics(session_rows)
        lines.append(
            f"| {session.name} | {session.d0_mm:g} | "
            f"{session.measured_min_mm:g}-{session.measured_max_mm:g} | "
            f"{session.plotted_min_mm:g}-{session.plotted_max_mm:g} | "
            f"{stats['n']} | {stats['mean']:.4f} | {stats['mae']:.4f} | "
            f"{stats['rmse']:.4f} | {stats['max_abs']:.4f} |"
        )
    overall = _overall_statistics(rows)
    lines.append(
        f"| **All sessions** | - | {sessions[0].measured_min_mm:g}-"
        f"{sessions[-1].measured_max_mm:g} (with gaps) | "
        f"{sessions[0].plotted_min_mm:g}-{sessions[-1].plotted_max_mm:g} "
        f"(with gaps) | {overall['n']} | {overall['mean']:.4f} | "
        f"{overall['mae']:.4f} | {overall['rmse']:.4f} | "
        f"{overall['max_abs']:.4f} |"
    )
    lines.extend(["", "## Unmeasured gaps", ""])
    gaps = 0
    for previous, following in zip(sessions[:-1], sessions[1:]):
        if following.measured_min_mm > previous.measured_max_mm:
            gaps += 1
            lines.append(
                f"- {previous.measured_max_mm:g}-{following.measured_min_mm:g} mm"
            )
    if not gaps:
        lines.append("- None")
    lines.extend(
        [
            "",
            "## Outputs",
            "",
            "- `combined_depth_incremental.png`: all raw cycles and approaches on the true absolute-distance axis",
            "- `combined_depth_incremental_stitched.png`: distance sections placed side by side with one shared vertical scale",
            "- `combined_depth_incremental_summary.png`: mean +/- one standard deviation, RMSE, and maximum absolute error",
            "- `combined_depth_incremental_raw.csv`: concatenated source rows with session and D0 columns",
            "- `combined_depth_incremental_summary.csv`: statistics for each interval midpoint",
            "",
        ]
    )
    path = output_dir / "combined_depth_incremental_report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_validation_report(
    sessions: list[DepthSession],
    validation_rows: list[dict[str, Any]],
    output_dir: Path,
) -> Path:
    type_labels = {
        "axis_x": "Single-axis X",
        "axis_z": "Single-axis Z",
        "depth": "Depth (D0-referenced)",
        "diagonal": "Diagonal",
    }
    lines = [
        "# Combined all-validation analysis",
        "",
        "The distance-norm error is evaluated from each pass reference. Signed "
        "X/Z displacements and diagonal corners remain separated in the detail "
        "figures; the summary figure aggregates them only within each validation type.",
        "",
        "| Session | D0 [mm] | Validation | N | Mean [mm] | MAE [mm] | RMSE [mm] | Max abs [mm] |",
        "|---|---:|---|---:|---:|---:|---:|---:|",
    ]
    for session in sessions:
        for validation_type in ("axis_x", "axis_z", "depth", "diagonal"):
            members = [
                row
                for row in validation_rows
                if row["session"] == session.name
                and validation_type_key(row) == validation_type
            ]
            if not members:
                continue
            stats = _error_statistics(
                float(row["distance_norm_error_mm"]) for row in members
            )
            lines.append(
                f"| {session.name} | {session.d0_mm:g} | "
                f"{type_labels[validation_type]} | {stats['n']} | "
                f"{stats['mean_error_mm']:.4f} | "
                f"{stats['mean_absolute_error_mm']:.4f} | "
                f"{stats['rmse_mm']:.4f} | "
                f"{stats['max_absolute_error_mm']:.4f} |"
            )
    lines.extend(
        [
            "",
            "## Outputs",
            "",
            "- `combined_validation_axis_x_by_delta.png`: Single-axis X, separated into the six signed displacements",
            "- `combined_validation_axis_z_by_delta.png`: Single-axis Z, separated into the six signed displacements",
            "- `combined_validation_diagonal_by_corner.png`: diagonal validation, separated into four X/Z corners",
            "- `combined_validation_all_types_summary.png`: mean +/- one standard deviation, RMSE, and maximum absolute error for X, Z, depth, and diagonal",
            "- `combined_validation_axis_x_summary.png`: full-size Single-axis X summary",
            "- `combined_validation_axis_z_summary.png`: full-size Single-axis Z summary",
            "- `combined_validation_depth_d0_referenced_summary.png`: full-size D0-referenced depth summary",
            "- `combined_validation_diagonal_summary.png`: full-size diagonal summary",
            "- `combined_validation_raw.csv`: all validation rows with session and D0 columns",
            "- `combined_validation_type_summary.csv`: statistics by validation type and absolute distance",
            "- `combined_validation_detail_summary.csv`: statistics retaining signed displacement or diagonal corner",
            "",
            "Depth in the all-type summary is D0-referenced. The separate "
            "`combined_depth_incremental*` outputs instead compare consecutive local steps.",
            "",
        ]
    )
    path = output_dir / "combined_validation_report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def generate_outputs(
    sessions: list[DepthSession], output_dir: Path
) -> tuple[
    list[Path],
    dict[str, float | int],
    dict[str, dict[str, float | int]],
]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = combined_rows(sessions)
    summary = summarize_rows(rows)
    raw_csv = output_dir / "combined_depth_incremental_raw.csv"
    summary_csv = output_dir / "combined_depth_incremental_summary.csv"
    _write_csv(raw_csv, rows)
    _write_csv(summary_csv, summary)
    outputs = [raw_csv, summary_csv]
    outputs.extend(plot_combined_raw(sessions, output_dir))
    outputs.extend(plot_stitched(sessions, output_dir))
    outputs.extend(plot_summary(sessions, summary, output_dir))
    outputs.append(write_report(sessions, rows, output_dir))

    validation_rows = [
        row for session in sessions for row in load_validation_rows(session)
    ]
    validation_rows.sort(
        key=lambda row: (
            float(row["d0_mm"]),
            validation_type_key(row),
            float(row["absolute_distance_label_mm"]),
            str(row["cycle"]),
            str(row["direction"]),
            str(row["capture_id"]),
        )
    )
    type_summary = summarize_validation_types(validation_rows)
    detail_summary = summarize_validation_details(validation_rows)
    validation_raw_csv = output_dir / "combined_validation_raw.csv"
    validation_type_csv = output_dir / "combined_validation_type_summary.csv"
    validation_detail_csv = output_dir / "combined_validation_detail_summary.csv"
    _write_csv(validation_raw_csv, validation_rows)
    _write_csv(validation_type_csv, type_summary)
    _write_csv(validation_detail_csv, detail_summary)
    outputs.extend([validation_raw_csv, validation_type_csv, validation_detail_csv])
    outputs.extend(plot_axis_validation(sessions, validation_rows, "x", output_dir))
    outputs.extend(plot_axis_validation(sessions, validation_rows, "z", output_dir))
    outputs.extend(plot_diagonal_validation(sessions, validation_rows, output_dir))
    outputs.extend(
        plot_all_validation_summary(
            sessions, validation_rows, type_summary, output_dir
        )
    )
    outputs.extend(
        plot_separate_validation_summaries(
            sessions, validation_rows, type_summary, output_dir
        )
    )
    outputs.append(write_validation_report(sessions, validation_rows, output_dir))
    validation_statistics = {
        validation_type: _error_statistics(
            float(row["distance_norm_error_mm"])
            for row in validation_rows
            if validation_type_key(row) == validation_type
        )
        for validation_type in ("axis_x", "axis_z", "depth", "diagonal")
    }
    return outputs, _overall_statistics(rows), validation_statistics


def main() -> int:
    args = parse_args()
    sessions = load_sessions(args.session_dirs)
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else (sessions[0].session_dir.parent / "combined_3axis_analysis").resolve()
    )
    outputs, stats, validation_statistics = generate_outputs(sessions, output_dir)
    print("Combined 3-axis validation analysis complete.")
    for session in sessions:
        print(
            f"  {session.name}: D0={session.d0_mm:g} mm; measured "
            f"{session.measured_min_mm:g}-{session.measured_max_mm:g} mm; "
            f"midpoints {session.plotted_min_mm:g}-{session.plotted_max_mm:g} mm"
        )
    print(
        f"  Depth incremental: N={stats['n']}; mean={stats['mean']:.4f} mm; "
        f"MAE={stats['mae']:.4f} mm; RMSE={stats['rmse']:.4f} mm; "
        f"max |error|={stats['max_abs']:.4f} mm"
    )
    for validation_type, type_stats in validation_statistics.items():
        print(
            f"  {validation_type}: N={type_stats['n']}; "
            f"mean={type_stats['mean_error_mm']:.4f} mm; "
            f"RMSE={type_stats['rmse_mm']:.4f} mm; "
            f"max |error|={type_stats['max_absolute_error_mm']:.4f} mm"
        )
    print(f"Output: {output_dir}")
    for path in outputs:
        print(f"  {path.name}")
    if args.show:
        plt.show()
    else:
        plt.close("all")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
