#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Plot triangulated stereo points in the left-camera or PAT coordinate frame."""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import matplotlib.pyplot as plt
import numpy as np


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot stereo_3d_points.npz as a 3D trajectory and orthographic projections.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("npz", type=Path, help="stereo_3d_points.npz")
    parser.add_argument("--output-dir", type=Path, default=None, help="Output directory. Defaults to the NPZ directory.")
    parser.add_argument("--max-points", type=int, default=5000, help="Maximum plotted points; evenly downsampled when exceeded.")
    parser.add_argument("--stereo-calibration", type=Path, default=None, help="Stereo calibration NPZ. Empty uses the path stored in the 3D NPZ.")
    parser.add_argument(
        "--frame",
        choices=["auto", "left-camera", "pat", "pat-relative"],
        default="auto",
        help=(
            "Coordinate array to plot. 'auto' prefers points_pat_mm, then "
            "points_pat_relative_mm, then points_left_cam_mm."
        ),
    )
    parser.add_argument(
        "--range-mode",
        choices=["auto", "calibration", "points"],
        default="auto",
        help=(
            "3D X/Y display range. 'auto' uses calibrated left-image FOV for "
            "left-camera data and point extents for PAT data."
        ),
    )
    parser.add_argument("--reference-depth-mm", type=float, default=0.0, help="Depth used for calibrated X/Y range. 0 uses the median reconstructed Z.")
    parser.add_argument("--elev", type=float, default=22.0, help="3D view elevation in degrees.")
    parser.add_argument("--azim", type=float, default=-62.0, help="3D view azimuth in degrees.")
    return parser.parse_args()


def select_frame(
    data: np.lib.npyio.NpzFile,
    requested: str,
) -> tuple[str, str, str, tuple[str, str, str], str]:
    definitions = {
        "left-camera": (
            "points_left_cam_mm",
            "Left OpenCV camera coordinates",
            (
                "X [mm] (image right)",
                "Y [mm] (image down)",
                "Z [mm] (camera forward)",
            ),
            "stereo_3d_trajectory.png",
        ),
        "pat": (
            "points_pat_mm",
            "PAT coordinates",
            ("PAT X [mm]", "PAT Y [mm]", "PAT Z [mm]"),
            "stereo_3d_trajectory_pat.png",
        ),
        "pat-relative": (
            "points_pat_relative_mm",
            "PAT displacement from initial stable median",
            ("PAT ΔX [mm]", "PAT ΔY [mm]", "PAT ΔZ [mm]"),
            "stereo_3d_trajectory_pat_relative.png",
        ),
    }
    if requested == "auto":
        for candidate in ("pat", "pat-relative", "left-camera"):
            if definitions[candidate][0] in data.files:
                requested = candidate
                break
        else:
            supported = ", ".join(item[0] for item in definitions.values())
            raise SystemExit(
                f"NPZ does not contain a supported 3D array ({supported})."
            )
    key, title, labels, filename = definitions[requested]
    if key not in data.files:
        raise SystemExit(
            f"--frame {requested} requires NPZ key {key!r}. "
            f"Available keys: {', '.join(data.files)}"
        )
    return requested, key, title, labels, filename


def equal_limits(values: np.ndarray, minimum_span: float = 1.0) -> tuple[float, float]:
    low = float(np.nanmin(values))
    high = float(np.nanmax(values))
    centre = 0.5 * (low + high)
    span = max(high - low, minimum_span)
    margin = 0.08 * span
    half = 0.5 * span + margin
    return centre - half, centre + half


def calibrated_xy_limits(
    calibration_path: Path,
    points: np.ndarray,
    reference_depth_mm: float,
) -> tuple[tuple[float, float], tuple[float, float], float]:
    calibration = np.load(calibration_path, allow_pickle=False)
    image_size = np.asarray(calibration["image_size"], dtype=int).reshape(-1)
    width, height = int(image_size[0]), int(image_size[1])
    camera_matrix = np.asarray(calibration["left_camera_matrix"], dtype=float)
    dist_coeffs = np.asarray(calibration["left_dist_coeffs"], dtype=float)
    depth = float(reference_depth_mm) if reference_depth_mm > 0 else float(np.nanmedian(points[:, 2]))
    if not np.isfinite(depth) or depth <= 0:
        raise SystemExit("Reference depth must be positive for calibration range mode.")

    corners = np.array(
        [[[0.0, 0.0]], [[width - 1.0, 0.0]], [[width - 1.0, height - 1.0]], [[0.0, height - 1.0]]],
        dtype=np.float64,
    )
    normalized = cv2.undistortPoints(corners, camera_matrix, dist_coeffs).reshape(-1, 2)
    xy = normalized * depth
    return (float(xy[:, 0].min()), float(xy[:, 0].max())), (float(xy[:, 1].min()), float(xy[:, 1].max())), depth


def main() -> int:
    args = parse_args()
    input_path = args.npz.resolve()
    if not input_path.exists():
        raise SystemExit(f"NPZ not found: {input_path}")

    data = np.load(input_path, allow_pickle=False)
    frame, points_key, frame_title, axis_labels, output_filename = select_frame(
        data,
        str(args.frame),
    )
    points = np.asarray(data[points_key], dtype=float)
    if points.ndim != 2 or points.shape[1] != 3:
        raise SystemExit(
            f"NPZ key {points_key!r} must have shape (N, 3); got {points.shape}."
        )
    valid = np.asarray(data["valid"], dtype=bool) if "valid" in data else np.ones(len(points), dtype=bool)
    t_sec = np.asarray(data["t_sec"], dtype=float) if "t_sec" in data else np.arange(len(points), dtype=float)
    if valid.shape != (len(points),):
        raise SystemExit(
            f"NPZ valid mask must have shape ({len(points)},); got {valid.shape}."
        )
    if t_sec.shape != (len(points),):
        raise SystemExit(
            f"NPZ t_sec must have shape ({len(points)},); got {t_sec.shape}."
        )
    valid &= np.isfinite(points).all(axis=1) & np.isfinite(t_sec)
    points = points[valid]
    t_sec = t_sec[valid]
    if points.size == 0:
        raise SystemExit("No finite 3D points found.")

    if args.max_points > 0 and len(points) > args.max_points:
        indices = np.linspace(0, len(points) - 1, args.max_points, dtype=int)
        points_plot = points[indices]
        t_plot = t_sec[indices]
    else:
        points_plot = points
        t_plot = t_sec

    output_dir = (args.output_dir or input_path.parent).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / output_filename

    x, y, z = points_plot.T
    range_mode = str(args.range_mode)
    if range_mode == "auto":
        range_mode = "calibration" if frame == "left-camera" else "points"
    if range_mode == "calibration" and frame != "left-camera":
        raise SystemExit(
            "--range-mode calibration is defined only for --frame left-camera. "
            "Use --range-mode points for PAT coordinates."
        )
    if range_mode == "calibration":
        calibration_path = args.stereo_calibration
        if calibration_path is None and "stereo_calibration" in data:
            calibration_path = Path(str(data["stereo_calibration"].item()))
        if calibration_path is None or not calibration_path.exists():
            raise SystemExit("Calibration range mode requires --stereo-calibration or a valid path in the 3D NPZ.")
        x_limits, y_limits, reference_depth = calibrated_xy_limits(calibration_path.resolve(), points, float(args.reference_depth_mm))
    else:
        x_limits = equal_limits(points[:, 0])
        y_limits = equal_limits(points[:, 1])
        reference_depth = None
    z_limits = equal_limits(points[:, 2])

    fig = plt.figure(figsize=(14, 8))
    grid = fig.add_gridspec(2, 3, height_ratios=[2.2, 1.0])
    ax3d = fig.add_subplot(grid[0, :], projection="3d")
    scatter = ax3d.scatter(x, y, z, c=t_plot, cmap="viridis", s=3, alpha=0.55, linewidths=0)
    ax3d.plot(x, y, z, color="tab:orange", linewidth=0.45, alpha=0.55)
    ax3d.scatter([x[0]], [y[0]], [z[0]], color="green", s=40, label="start")
    ax3d.scatter([x[-1]], [y[-1]], [z[-1]], color="red", marker="*", s=70, label="end")
    ax3d.set_xlabel(axis_labels[0])
    ax3d.set_ylabel(axis_labels[1])
    ax3d.set_zlabel(axis_labels[2])
    ax3d.set_xlim(x_limits)
    ax3d.set_ylim(y_limits)
    ax3d.set_zlim(z_limits)
    ax3d.set_box_aspect((1, 1, 1))
    ax3d.view_init(elev=args.elev, azim=args.azim)
    title = (
        f"Stereo 3D trajectory in {frame_title} | N={len(points)} | "
        f"span=({np.ptp(points[:, 0]):.3f}, {np.ptp(points[:, 1]):.3f}, "
        f"{np.ptp(points[:, 2]):.3f}) mm"
    )
    if reference_depth is not None:
        title += f"; calibrated XY range at camera Z={reference_depth:.1f} mm"
    ax3d.set_title(title)
    ax3d.legend(loc="upper left")
    fig.colorbar(scatter, ax=ax3d, pad=0.08, label="time [s]")

    projections = [
        (0, 2, axis_labels[0], axis_labels[2], "XZ projection"),
        (0, 1, axis_labels[0], axis_labels[1], "XY projection"),
        (1, 2, axis_labels[1], axis_labels[2], "YZ projection"),
    ]
    for column, (ia, ib, xlabel, ylabel, title) in enumerate(projections):
        ax = fig.add_subplot(grid[1, column])
        ax.scatter(points_plot[:, ia], points_plot[:, ib], c=t_plot, cmap="viridis", s=2, alpha=0.45, linewidths=0)
        if range_mode == "calibration":
            if ia == 0:
                ax.set_xlim(x_limits)
            elif ia == 1:
                ax.set_xlim(y_limits)
            if ib == 0:
                ax.set_ylim(x_limits)
            elif ib == 1:
                ax.set_ylim(y_limits)
            elif ib == 2:
                ax.set_ylim(z_limits)
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.grid(True, linestyle=":", alpha=0.5)

    fig.tight_layout()
    fig.savefig(output_path, dpi=180)
    plt.close(fig)

    centre = np.mean(points, axis=0)
    span = np.ptp(points, axis=0)
    print(f"Input: {input_path}")
    print(f"Frame: {frame} ({points_key})")
    if frame != "left-camera" and args.stereo_calibration is not None:
        print(
            "[INFO] --stereo-calibration is not used for PAT-coordinate plot limits."
        )
    print(f"Points: {len(points)}")
    print(f"Mean [mm]: X={centre[0]:.6f}, Y={centre[1]:.6f}, Z={centre[2]:.6f}")
    print(f"Span [mm]: X={span[0]:.6f}, Y={span[1]:.6f}, Z={span[2]:.6f}")
    print(f"Output: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
