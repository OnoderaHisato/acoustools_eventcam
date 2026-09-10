#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Plot the frozen three-axis stage capture targets from a dry-plan session."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np


MANIFEST_NAME = "stereo_3axis_stage_accuracy_manifest.json"
AXES = ("x", "y", "z")
CATEGORY_STYLE = {
    "experiment reference": {"color": "black", "marker": "*", "size": 150},
    "orientation": {"color": "#9467bd", "marker": "o", "size": 48},
    "axis": {"color": "#1f77b4", "marker": "o", "size": 44},
    "depth": {"color": "#ff7f0e", "marker": "^", "size": 48},
    "diagonal": {"color": "#2ca02c", "marker": "s", "size": 46},
    "cuboid": {"color": "#00897b", "marker": "D", "size": 48},
    "centres": {"color": "#d62728", "marker": "P", "size": 54},
    "other": {"color": "#7f7f7f", "marker": "o", "size": 38},
    "approach preposition": {"color": "#bdbdbd", "marker": "x", "size": 30},
}
CATEGORY_ORDER = (
    "centres",
    "orientation",
    "axis",
    "depth",
    "diagonal",
    "cuboid",
    "other",
    "approach preposition",
)
CATEGORY_LABEL = {
    "centres": "Pass centres — reference/return drift",
    "orientation": "Orientation — fit camera→stage R,t",
    "axis": "Local X/Z axis — scale/cross-talk versus depth",
    "depth": "Depth — error versus Y distance",
    "diagonal": "Local XZ diagonal — four corners versus depth",
    "cuboid": "3D cuboid surface grids — side lengths 40/50/60 mm",
    "other": "Other capture target",
    "approach preposition": "Approach preposition — no capture",
    "experiment reference": "Experiment reference D₀",
}
CATEGORY_PURPOSE_JA = {
    "centres": "各passの撮影前後の基準。復帰誤差と時間driftを診断する。",
    "orientation": "validationとは独立にcamera→stageの剛体変換R,tを求める。",
    "axis": "複数のY面でX/Zを1軸ずつ動かし、scale、軸方向誤差、cross-talkを評価する。",
    "depth": "中心線上の近・中・遠点で、距離に伴うステレオ誤差の増加を評価する。",
    "diagonal": "各Y面のローカル基準からXZ四隅への複合変位を評価する。",
    "cuboid": "各奥行中心で半幅20/25/30 mmの3D直方体表面26点を評価する。",
    "approach preposition": "指定方向から評価点へ入るための事前位置。ここでは撮影しない。",
    "other": "上記以外の計画点。",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plot capture targets from an immutable stereo three-axis dry-plan "
            "session. This does not open a stage or camera."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("session_dir", type=Path)
    parser.add_argument(
        "--coordinates",
        choices=("logical", "hardware", "both"),
        default="both",
    )
    parser.add_argument(
        "--include-prepositions",
        action="store_true",
        help="Also show controlled-approach prepositions as grey crosses.",
    )
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--show",
        action="store_true",
        help="Open an interactive matplotlib window after saving the PNG.",
    )
    return parser.parse_args()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_frozen_plan(session_dir: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    manifest_path = session_dir / MANIFEST_NAME
    csv_path = session_dir / "planned_motion_targets.csv"
    if not manifest_path.is_file():
        raise SystemExit(f"Dry-plan manifest not found: {manifest_path}")
    if not csv_path.is_file():
        raise SystemExit(f"Planned motion CSV not found: {csv_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_hash = str(manifest.get("motion_review_csv_sha256", "")).strip()
    actual_hash = file_sha256(csv_path)
    if not expected_hash or actual_hash != expected_hash:
        raise SystemExit(
            "planned_motion_targets.csv does not match the immutable dry-plan "
            "manifest. Create a fresh dry-plan session."
        )
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit(f"Planned motion CSV is empty: {csv_path}")
    return manifest, rows


def category_for(row: dict[str, str]) -> str:
    if row["phase"] != "capture_target":
        return "approach preposition"
    if row.get("sweep_name", "").lower().startswith("cuboid_"):
        return "cuboid"
    role = row["data_role"].lower()
    if "center" in role:
        return "centres"
    if "orientation" in role:
        return "orientation"
    for name in ("axis", "depth", "diagonal"):
        if name in role:
            return name
    return "other"


def reference_point(
    manifest: dict[str, Any],
    coordinates: str,
) -> np.ndarray:
    config = manifest["config"]
    logical = np.asarray(
        config["experiment"]["reference_global_mm"],
        dtype=np.float64,
    )
    if coordinates == "logical":
        return logical
    hardware = []
    for index, axis in enumerate(AXES):
        spec = config["stages"]["axes"][axis]
        hardware.append(
            float(spec["datum_mm"])
            + int(spec["direction"]) * float(logical[index])
        )
    return np.asarray(hardware, dtype=np.float64)


def unique_points(
    rows: list[dict[str, str]],
    coordinates: str,
    *,
    include_prepositions: bool,
) -> tuple[dict[str, np.ndarray], Counter[str]]:
    prefix = "logical" if coordinates == "logical" else "hardware"
    points: dict[str, list[list[float]]] = {}
    seen: set[tuple[str, float, float, float]] = set()
    occurrences: Counter[str] = Counter()
    for row in rows:
        category = category_for(row)
        if category == "approach preposition" and not include_prepositions:
            continue
        point = [
            float(row[f"{prefix}_x_mm"]),
            float(row[f"{prefix}_y_mm"]),
            float(row[f"{prefix}_z_mm"]),
        ]
        occurrences[category] += 1
        key = (category, *(round(value, 9) for value in point))
        if key in seen:
            continue
        seen.add(key)
        points.setdefault(category, []).append(point)
    return {
        category: np.asarray(values, dtype=np.float64)
        for category, values in points.items()
    }, occurrences


def set_axis_extent(axis: Any, arrays: list[np.ndarray]) -> None:
    from matplotlib.ticker import MaxNLocator

    combined = np.vstack([array for array in arrays if array.size])
    lower = combined.min(axis=0)
    upper = combined.max(axis=0)
    span = np.maximum(upper - lower, 1.0)
    padding = np.maximum(0.05 * span, 1.0)
    axis.set_xlim(lower[0] - padding[0], upper[0] + padding[0])
    axis.set_ylim(lower[1] - padding[1], upper[1] + padding[1])
    axis.set_zlim(lower[2] - padding[2], upper[2] + padding[2])
    normalized = span / max(float(np.min(span)), 1.0)
    axis.set_box_aspect(np.clip(normalized, 1.0, 4.0))
    axis.xaxis.set_major_locator(MaxNLocator(nbins=5))
    axis.yaxis.set_major_locator(MaxNLocator(nbins=7))
    axis.zaxis.set_major_locator(MaxNLocator(nbins=5))


def ordered_categories(points: dict[str, np.ndarray]) -> list[str]:
    return [category for category in CATEGORY_ORDER if category in points]


def plot_3d(
    axis: Any,
    points: dict[str, np.ndarray],
    reference: np.ndarray,
    *,
    title: str,
) -> None:
    arrays = [reference]
    for category in ordered_categories(points):
        values = points[category]
        style = CATEGORY_STYLE[category]
        arrays.append(values)
        axis.scatter(
            values[:, 0],
            values[:, 1],
            values[:, 2],
            color=style["color"],
            marker=style["marker"],
            s=style["size"],
            alpha=0.88,
            edgecolors="white" if style["marker"] not in {"x", "+"} else None,
            linewidths=0.5,
            depthshade=False,
        )
    style = CATEGORY_STYLE["experiment reference"]
    axis.scatter(
        reference[:, 0],
        reference[:, 1],
        reference[:, 2],
        color=style["color"],
        marker=style["marker"],
        s=style["size"],
        depthshade=False,
    )
    set_axis_extent(axis, arrays)
    axis.set_xlabel("X [mm]", labelpad=7)
    axis.set_ylabel("Y depth [mm]", labelpad=9)
    axis.set_zlabel("Z [mm]", labelpad=6)
    axis.set_title(f"{title}: 3D overview", fontsize=11)
    axis.view_init(elev=24, azim=-58)
    axis.grid(True, alpha=0.28)


def plot_projection(
    axis: Any,
    points: dict[str, np.ndarray],
    reference: np.ndarray,
    *,
    horizontal: int,
    vertical: int,
    title: str,
    coordinates: str,
) -> None:
    from matplotlib.ticker import MaxNLocator

    arrays = [reference]
    for category in ordered_categories(points):
        values = points[category]
        style = CATEGORY_STYLE[category]
        arrays.append(values)
        axis.scatter(
            values[:, horizontal],
            values[:, vertical],
            color=style["color"],
            marker=style["marker"],
            s=style["size"] * 0.82,
            alpha=0.88,
            edgecolors="white" if style["marker"] not in {"x", "+"} else None,
            linewidths=0.5,
            zorder=3,
        )
    style = CATEGORY_STYLE["experiment reference"]
    axis.scatter(
        reference[:, horizontal],
        reference[:, vertical],
        color=style["color"],
        marker=style["marker"],
        s=style["size"] * 0.92,
        zorder=5,
    )
    combined = np.vstack([array for array in arrays if array.size])
    lower = combined[:, [horizontal, vertical]].min(axis=0)
    upper = combined[:, [horizontal, vertical]].max(axis=0)
    span = np.maximum(upper - lower, 1.0)
    padding = np.maximum(0.07 * span, 0.8)
    axis.set_xlim(lower[0] - padding[0], upper[0] + padding[0])
    axis.set_ylim(lower[1] - padding[1], upper[1] + padding[1])
    axis.xaxis.set_major_locator(MaxNLocator(nbins=6))
    axis.yaxis.set_major_locator(MaxNLocator(nbins=6))
    axis.set_xlabel(f"{AXES[horizontal].upper()} [mm]")
    axis.set_ylabel(f"{AXES[vertical].upper()} [mm]")
    axis.set_title(title, fontsize=10)
    axis.grid(True, alpha=0.30)
    axis.text(
        0.02,
        0.98,
        "independent axis scales",
        transform=axis.transAxes,
        va="top",
        ha="left",
        fontsize=7,
        color="#666666",
    )
    if coordinates == "logical" and horizontal == 1:
        for y_value in (0.0, 20.0, 40.0, 60.0, 100.0, 140.0, 160.0, 165.0):
            candidates = combined[
                np.isclose(combined[:, 1], y_value)
                & np.isclose(combined[:, 0], 0.0)
                & np.isclose(combined[:, 2], 0.0)
            ]
            if not len(candidates):
                continue
            label = "D₀" if y_value == 0.0 else f"D₀+{y_value:g}"
            point = candidates[0]
            axis.annotate(
                label,
                (point[horizontal], point[vertical]),
                xytext=(4, 4),
                textcoords="offset points",
                fontsize=7,
                color="#333333",
                zorder=6,
            )


def point_text(
    points: np.ndarray,
) -> str:
    return ", ".join(
        f"[{value[0]:g},{value[1]:g},{value[2]:g}]"
        for value in points
    )


def write_point_guide(
    path: Path,
    manifest: dict[str, Any],
    rows: list[dict[str, str]],
) -> None:
    logical_reference = reference_point(manifest, "logical").reshape(1, 3)
    hardware_reference = reference_point(manifest, "hardware").reshape(1, 3)
    logical_points, occurrences = unique_points(
        rows,
        "logical",
        include_prepositions=True,
    )
    hardware_points, _ = unique_points(
        rows,
        "hardware",
        include_prepositions=True,
    )
    lines = [
        "# 3軸ステージ dry-plan測定点ガイド",
        "",
        f"- profile: `{manifest.get('plan_profile', '')}`",
        f"- planned captures: `{len(manifest.get('samples', []))}`",
        "- 座標表記: `g`は実験用論理座標、`h`はOssila hardware readback座標",
        "- 同じ座標が複数の目的で撮影される場合がある",
        "",
        "| 種類 | 目的 | capture数 | unique logical g [mm] | unique hardware h [mm] |",
        "|---|---|---:|---|---|",
    ]
    for category in CATEGORY_ORDER:
        if category not in logical_points:
            continue
        lines.append(
            "| "
            + " | ".join(
                (
                    CATEGORY_LABEL[category],
                    CATEGORY_PURPOSE_JA[category],
                    str(occurrences[category]),
                    f"`{point_text(logical_points[category])}`",
                    f"`{point_text(hardware_points[category])}`",
                )
            )
            + " |"
        )
    lines.extend(
        (
            "",
            "## Experiment reference",
            "",
            (
                f"`g={point_text(logical_reference)} → "
                f"h={point_text(hardware_reference)} mm`がD₀です。"
            ),
            "黒い星は新しいcaptureを追加する印ではなく、この基準位置を強調したものです。",
            "",
            "## 図の読み方",
            "",
            "- 3D overviewは全体配置を示す。",
            "- X–Yは横方向Xと奥行Y、Y–Zは奥行Yと高さZを示す。",
            "- X–Zは奥行を潰した断面なので、異なるYの点が重なる。",
            "- 2D投影は読みやすさのため軸ごとに独立scaleであり、物理的な縦横比ではない。",
            "- 線を描いていないため、図は移動経路ではなくcapture targetの一覧である。",
        )
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    session_dir = args.session_dir.resolve()
    manifest, rows = load_frozen_plan(session_dir)
    if not args.show:
        import matplotlib

        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    coordinate_sets = (
        ("logical", "Logical stage coordinates g"),
        ("hardware", "Ossila hardware readback coordinates h"),
    )
    if args.coordinates != "both":
        coordinate_sets = tuple(
            item for item in coordinate_sets if item[0] == args.coordinates
        )
    row_count = len(coordinate_sets)
    figure = plt.figure(figsize=(18.5, 5.3 * row_count + 1.7))
    grid = figure.add_gridspec(
        row_count,
        4,
        width_ratios=(1.45, 1.0, 1.0, 1.0),
        hspace=0.32,
        wspace=0.28,
    )
    count_messages = []
    all_categories: set[str] = set()
    for plot_index, (coordinates, title) in enumerate(coordinate_sets):
        points, occurrences = unique_points(
            rows,
            coordinates,
            include_prepositions=bool(args.include_prepositions),
        )
        all_categories.update(points)
        reference = reference_point(manifest, coordinates).reshape(1, 3)
        plot_3d(
            figure.add_subplot(grid[plot_index, 0], projection="3d"),
            points,
            reference,
            title=title,
        )
        for column, horizontal, vertical, projection_title in (
            (1, 0, 1, "X–Y: lateral versus depth"),
            (2, 1, 2, "Y–Z: depth versus vertical"),
            (3, 0, 2, "X–Z: transverse section"),
        ):
            plot_projection(
                figure.add_subplot(grid[plot_index, column]),
                points,
                reference,
                horizontal=horizontal,
                vertical=vertical,
                title=projection_title,
                coordinates=coordinates,
            )
        count_messages.append(
            f"{coordinates}: "
            + ", ".join(
                f"{name}={count}"
                for name, count in sorted(occurrences.items())
            )
        )
    output = (
        args.output.resolve()
        if args.output is not None
        else session_dir / "planned_motion_targets_3d.png"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    guide = session_dir / "planned_motion_targets_guide.md"
    write_point_guide(guide, manifest, rows)
    figure.suptitle(
        (
            "Stereo three-axis dry plan — capture targets, not motion paths\n"
            f"profile={manifest.get('plan_profile', 'unknown')}; "
            "2D panels use independent axis scales"
        ),
        fontsize=15,
        y=0.995,
    )
    from matplotlib.lines import Line2D

    legend_categories = [
        category for category in CATEGORY_ORDER if category in all_categories
    ]
    legend_handles = []
    legend_labels = []
    for category in legend_categories:
        style = CATEGORY_STYLE[category]
        legend_handles.append(
            Line2D(
                [0],
                [0],
                color="none",
                marker=style["marker"],
                markerfacecolor=style["color"],
                markeredgecolor=style["color"],
                markersize=7,
            )
        )
        logical_points, _ = unique_points(
            rows,
            "logical",
            include_prepositions=bool(args.include_prepositions),
        )
        legend_labels.append(
            f"{CATEGORY_LABEL[category]} ({len(logical_points[category])} unique)"
        )
    reference_style = CATEGORY_STYLE["experiment reference"]
    legend_handles.append(
        Line2D(
            [0],
            [0],
            color="none",
            marker=reference_style["marker"],
            markerfacecolor=reference_style["color"],
            markeredgecolor=reference_style["color"],
            markersize=10,
        )
    )
    legend_labels.append(CATEGORY_LABEL["experiment reference"])
    figure.legend(
        legend_handles,
        legend_labels,
        loc="lower center",
        ncol=min(len(legend_labels), 3),
        fontsize=9,
        bbox_to_anchor=(0.5, 0.01),
    )
    figure.subplots_adjust(
        left=0.04,
        right=0.985,
        top=0.91,
        bottom=0.14 if len(legend_labels) <= 3 else 0.18,
    )
    figure.savefig(output, dpi=180)
    print(f"Saved: {output}")
    print(f"Guide: {guide}")
    print(f"Planned CSV SHA-256 verified: {file_sha256(session_dir / 'planned_motion_targets.csv')}")
    for message in count_messages:
        print(message)
    if args.show:
        plt.show()
    plt.close(figure)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
