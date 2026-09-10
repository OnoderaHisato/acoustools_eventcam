#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Extract particle centroids directly from filtered event-camera NPZ files.

The input NPZ is produced by ``acoustools_eventcam_sync.py`` or
``eventcam_raw_postprocess.py`` with ``--export-filtered-events-npz``.

Outputs include:
  - processed_centres.npy: Nx2 pixel centres, compatible with the video notebook
  - event_centres_raw.csv: centroid per event-time bin
  - event_centres_interp.csv / .npy: uniformly interpolated trajectory
  - t_xy.png: t-x and t-y plots
  - xy.png: x-y camera-plane plot
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np


def parse_roi(text: str, name: str) -> tuple[int, int, int, int] | None:
    if not text:
        return None
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if len(parts) != 4:
        raise SystemExit(f"{name} must be x0,y0,x1,y1: {text}")
    x0, y0, x1, y1 = [int(part) for part in parts]
    if x1 <= x0 or y1 <= y0:
        raise SystemExit(f"{name} must satisfy x1>x0 and y1>y0: {text}")
    return x0, y0, x1, y1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Track particle centroid from sync-clipped, LED-masked event NPZ.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("npz", help="Input *_events.npz file.")
    parser.add_argument("--output-dir", default="", help="Output directory. Empty uses <npz parent>/event_tracking.")
    parser.add_argument("--bin-us", type=int, default=500, help="Event bin width for raw centroid extraction. Used as window and hop unless overridden.")
    parser.add_argument("--window-us", type=int, default=0, help="Event integration window width. 0 uses --bin-us.")
    parser.add_argument("--hop-us", type=int, default=0, help="Step between consecutive centroid estimates. 0 uses --bin-us.")
    parser.add_argument("--t-start-sec", type=float, default=0.0, help="Start time after NPZ output_start in seconds.")
    parser.add_argument("--t-end-sec", type=float, default=0.0, help="End time after NPZ output_start in seconds. 0 uses all events.")
    parser.add_argument("--dt-us", type=float, default=0.0, help="Uniform interpolation dt in us. 0 uses bin_us/interp_factor.")
    parser.add_argument("--interp-factor", type=float, default=2.0, help="Interpolation factor relative to --bin-us when --dt-us=0.")
    parser.add_argument("--max-interp-gap-sec", type=float, default=0.0, help="Do not interpolate across raw gaps longer than this. 0 disables.")
    parser.add_argument("--roi", default="", help="Tracking ROI x0,y0,x1,y1. Empty uses the full sensor extent from events.")
    parser.add_argument(
        "--mask-roi",
        action="append",
        default=[],
        help="Exclude events in x0,y0,x1,y1 before tracking. May be repeated (for example, PAT-start LED ROI).",
    )
    parser.add_argument(
        "--tracking-method",
        choices=[
            "event_weighted",
            "component_center",
            "hough_circle",
            "distance_transform_center",
            "min_enclosing_circle",
            "fit_ellipse",
            "ransac_circle",
        ],
        default="event_weighted",
        help="How to derive the centre from the selected connected component.",
    )
    parser.add_argument("--min-events", type=int, default=20, help="Minimum events in a bin to attempt centroid extraction.")
    parser.add_argument("--threshold-count", type=int, default=1, help="Pixel event-count threshold before connected components.")
    parser.add_argument("--blur", type=int, default=0, help="Odd Gaussian blur kernel for event-count image. 0 disables blur.")
    parser.add_argument("--morph-open", type=int, default=0, help="Morphological open kernel size. 0 disables.")
    parser.add_argument("--morph-close", type=int, default=0, help="Morphological close kernel size. 0 disables.")
    parser.add_argument("--min-area", type=int, default=5, help="Minimum connected-component area in pixels.")
    parser.add_argument("--min-mass", type=int, default=30, help="Minimum summed event count in the selected component. 0 disables.")
    parser.add_argument("--hough-dp", type=float, default=1.2, help="HoughCircles inverse accumulator resolution for --tracking-method hough_circle.")
    parser.add_argument("--hough-min-dist-px", type=float, default=8.0, help="Minimum circle centre distance for HoughCircles.")
    parser.add_argument("--hough-param1", type=float, default=100.0, help="Canny high threshold for HoughCircles.")
    parser.add_argument("--hough-param2", type=float, default=8.0, help="Accumulator threshold for HoughCircles. Lower detects weaker circles.")
    parser.add_argument("--hough-min-radius-px", type=int, default=2, help="Minimum circle radius for HoughCircles.")
    parser.add_argument("--hough-max-radius-px", type=int, default=0, help="Maximum circle radius for HoughCircles. 0 lets OpenCV choose.")
    parser.add_argument("--ransac-iterations", type=int, default=80, help="Random circle hypotheses for --tracking-method ransac_circle.")
    parser.add_argument("--ransac-residual-px", type=float, default=2.0, help="Inlier distance threshold in pixels for --tracking-method ransac_circle.")
    parser.add_argument("--max-step-px", type=float, default=0.0, help="Reject centroids that jump farther than this from the previous valid bin. 0 disables.")
    parser.add_argument("--polarity", choices=["all", "on", "off"], default="all", help="Which events to use.")
    parser.add_argument("--sensor-width", type=int, default=0, help="Sensor width override. 0 infers from events.")
    parser.add_argument("--sensor-height", type=int, default=0, help="Sensor height override. 0 infers from events.")
    parser.add_argument("--scale-px-per-mm", type=float, default=0.0, help="Optional pixel/mm scale for mm output columns.")
    parser.add_argument("--scale-mm-per-px", type=float, default=0.0, help="Optional mm/pixel scale. Overrides --scale-px-per-mm when >0.")
    parser.add_argument("--center-mode", choices=["none", "first", "mean"], default="none", help="Origin for mm output.")
    parser.add_argument("--keep-leading-nan", action="store_true", help="Keep leading NaN values before the first valid centroid.")
    parser.add_argument("--x-invert", action="store_true", help="Invert x in mm output.")
    parser.add_argument("--y-invert", action="store_true", help="Invert y in mm output.")
    parser.add_argument("--ideal-log", default="", help="Optional *_ideal_log.csv to overlay with measured trajectory.")
    parser.add_argument("--ideal-time-offset-sec", type=float, default=0.0, help="Time offset added to ideal-log time before overlay.")
    return parser.parse_args()


def finite_interp(t_src: np.ndarray, y_src: np.ndarray, t_dst: np.ndarray, max_gap_sec: float = 0.0) -> np.ndarray:
    mask = np.isfinite(t_src) & np.isfinite(y_src)
    if mask.sum() < 2:
        return np.full_like(t_dst, np.nan, dtype=float)
    t_valid = t_src[mask]
    y_valid = y_src[mask]
    out = np.interp(t_dst, t_valid, y_valid)
    out[(t_dst < t_valid[0]) | (t_dst > t_valid[-1])] = np.nan
    if max_gap_sec > 0:
        gaps = np.flatnonzero(np.diff(t_valid) > max_gap_sec)
        for gap_i in gaps:
            in_gap = (t_dst > t_valid[gap_i]) & (t_dst < t_valid[gap_i + 1])
            out[in_gap] = np.nan
    return out


def fill_leading_with_first_valid_pair(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray, int | None]:
    valid = np.flatnonzero(np.isfinite(x) & np.isfinite(y))
    if valid.size == 0:
        return x, y, None
    first = int(valid[0])
    if first <= 0:
        return x, y, first
    x_filled = x.copy()
    y_filled = y.copy()
    x_filled[:first] = x_filled[first]
    y_filled[:first] = y_filled[first]
    return x_filled, y_filled, first


def write_xy_to_rows(rows: list[dict[str, object]], x: np.ndarray, y: np.ndarray, upto: int) -> None:
    for idx in range(max(0, int(upto))):
        rows[idx]["x_px"] = f"{float(x[idx]):.6f}" if np.isfinite(x[idx]) else ""
        rows[idx]["y_px"] = f"{float(y[idx]):.6f}" if np.isfinite(y[idx]) else ""


def reject_large_jumps(x: np.ndarray, y: np.ndarray, max_step_px: float) -> np.ndarray:
    """Return a mask of points that look like single-bin tracking jumps."""
    rejected = np.zeros_like(x, dtype=bool)
    if max_step_px <= 0:
        return rejected

    last_x = np.nan
    last_y = np.nan
    for i, (cx, cy) in enumerate(zip(x, y)):
        if not (np.isfinite(cx) and np.isfinite(cy)):
            continue
        if np.isfinite(last_x) and np.hypot(cx - last_x, cy - last_y) > max_step_px:
            rejected[i] = True
            continue
        last_x = float(cx)
        last_y = float(cy)
    return rejected


def write_csv(path: Path, rows: Iterable[dict[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def make_kernel(size: int) -> np.ndarray | None:
    if size <= 0:
        return None
    size = int(size)
    if size % 2 == 0:
        size += 1
    return np.ones((size, size), dtype=np.uint8)


def fit_circle_from_three_points(points: np.ndarray) -> tuple[float, float, float] | None:
    (x1, y1), (x2, y2), (x3, y3) = points.astype(float)
    a = 2.0 * np.array([[x2 - x1, y2 - y1], [x3 - x1, y3 - y1]], dtype=float)
    b = np.array(
        [
            x2 * x2 + y2 * y2 - x1 * x1 - y1 * y1,
            x3 * x3 + y3 * y3 - x1 * x1 - y1 * y1,
        ],
        dtype=float,
    )
    det = float(np.linalg.det(a))
    if abs(det) < 1e-6:
        return None
    cx, cy = np.linalg.solve(a, b)
    radius = float(np.hypot(points[:, 0] - cx, points[:, 1] - cy).mean())
    if not np.isfinite(radius) or radius <= 0:
        return None
    return float(cx), float(cy), radius


def ransac_circle(points: np.ndarray, iterations: int, residual_px: float) -> tuple[float, float, float] | None:
    if points.shape[0] < 3:
        return None
    rng = np.random.default_rng(12345)
    best: tuple[int, float, float, float, float] | None = None
    residual_px = max(float(residual_px), 0.1)
    iterations = max(1, int(iterations))
    for _ in range(iterations):
        sample_idx = rng.choice(points.shape[0], size=3, replace=False)
        candidate = fit_circle_from_three_points(points[sample_idx])
        if candidate is None:
            continue
        cx, cy, radius = candidate
        residuals = np.abs(np.hypot(points[:, 0] - cx, points[:, 1] - cy) - radius)
        inliers = residuals <= residual_px
        score = int(inliers.sum())
        mean_residual = float(residuals[inliers].mean()) if score else float("inf")
        if best is None or score > best[0] or (score == best[0] and mean_residual < best[1]):
            best = (score, mean_residual, cx, cy, radius)
    if best is None or best[0] < 3:
        return None
    cx, cy, radius = best[2], best[3], best[4]
    residuals = np.abs(np.hypot(points[:, 0] - cx, points[:, 1] - cy) - radius)
    inliers = points[residuals <= residual_px]
    if inliers.shape[0] >= 3:
        distances = np.hypot(inliers[:, 0] - cx, inliers[:, 1] - cy)
        radius = float(np.median(distances))
    return float(cx), float(cy), float(radius)


def centroid_from_events(
    events: np.ndarray,
    roi: tuple[int, int, int, int],
    *,
    threshold_count: int,
    blur: int,
    morph_open: int,
    morph_close: int,
    min_area: int,
    tracking_method: str,
    hough_dp: float,
    hough_min_dist_px: float,
    hough_param1: float,
    hough_param2: float,
    hough_min_radius_px: int,
    hough_max_radius_px: int,
    ransac_iterations: int,
    ransac_residual_px: float,
) -> tuple[float, float, int, int, int, float]:
    x0, y0, x1, y1 = roi
    width = x1 - x0
    height = y1 - y0
    if events.size == 0:
        return np.nan, np.nan, 0, 0, 0, np.nan

    xs = events["x"].astype(np.int32) - x0
    ys = events["y"].astype(np.int32) - y0
    in_roi = (xs >= 0) & (xs < width) & (ys >= 0) & (ys < height)
    xs = xs[in_roi]
    ys = ys[in_roi]
    if xs.size == 0:
        return np.nan, np.nan, 0, 0, 0, np.nan

    flat = ys.astype(np.int64) * int(width) + xs.astype(np.int64)
    unique_flat, counts = np.unique(flat, return_counts=True)
    if unique_flat.size == 0:
        return np.nan, np.nan, int(xs.size), 0, 0, np.nan

    threshold = max(1, int(threshold_count))
    if blur and blur > 1:
        active_flat = unique_flat
    else:
        active_flat = unique_flat[counts >= threshold]
        if active_flat.size == 0:
            return np.nan, np.nan, int(xs.size), 0, 0, np.nan

    active_x = (active_flat % int(width)).astype(np.int32)
    active_y = (active_flat // int(width)).astype(np.int32)
    pad = max(int(blur or 0), int(morph_open or 0), int(morph_close or 0), 1) + 2
    crop_x0 = max(0, int(active_x.min()) - pad)
    crop_y0 = max(0, int(active_y.min()) - pad)
    crop_x1 = min(width, int(active_x.max()) + pad + 1)
    crop_y1 = min(height, int(active_y.max()) + pad + 1)

    img = np.zeros((crop_y1 - crop_y0, crop_x1 - crop_x0), dtype=np.uint16)
    pix_x = (unique_flat % int(width)).astype(np.int32)
    pix_y = (unique_flat // int(width)).astype(np.int32)
    in_crop = (pix_x >= crop_x0) & (pix_x < crop_x1) & (pix_y >= crop_y0) & (pix_y < crop_y1)
    img[pix_y[in_crop] - crop_y0, pix_x[in_crop] - crop_x0] = counts[in_crop].astype(np.uint16)

    work = img.astype(np.float32)
    if blur and blur > 1:
        k = int(blur)
        if k % 2 == 0:
            k += 1
        work = cv2.GaussianBlur(work, (k, k), 0)

    binary = (work >= threshold).astype(np.uint8)
    ko = make_kernel(morph_open)
    kc = make_kernel(morph_close)
    if ko is not None:
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, ko)
    if kc is not None:
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kc)

    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    if num_labels <= 1:
        return np.nan, np.nan, int(xs.size), 0, 0, np.nan

    areas = stats[1:, cv2.CC_STAT_AREA]
    valid = np.flatnonzero(areas >= max(1, int(min_area)))
    if valid.size == 0:
        return np.nan, np.nan, int(xs.size), 0, 0, np.nan

    # Pick the component with the largest summed event count, not just largest area.
    best_label = 0
    best_mass = -1.0
    best_area = 0
    for idx in valid:
        label = int(idx + 1)
        component = labels == label
        mass = float(img[component].sum())
        if mass > best_mass:
            best_mass = mass
            best_label = label
            best_area = int(stats[label, cv2.CC_STAT_AREA])

    component = labels == best_label
    weights = img.astype(np.float64) * component
    mass = float(weights.sum())
    if mass <= 0:
        return np.nan, np.nan, int(xs.size), best_area, 0, np.nan

    yy, xx = np.indices(weights.shape)
    comp_left = int(stats[best_label, cv2.CC_STAT_LEFT])
    comp_top = int(stats[best_label, cv2.CC_STAT_TOP])
    comp_width = int(stats[best_label, cv2.CC_STAT_WIDTH])
    comp_height = int(stats[best_label, cv2.CC_STAT_HEIGHT])
    comp_margin = max(comp_width, comp_height, 3)

    def inside_component_neighbourhood(px: float, py: float) -> bool:
        return (
            comp_left - comp_margin <= px <= comp_left + comp_width + comp_margin
            and comp_top - comp_margin <= py <= comp_top + comp_height + comp_margin
        )

    radius_px = np.nan
    if tracking_method == "component_center":
        ys_comp, xs_comp = np.nonzero(component)
        if xs_comp.size == 0:
            return np.nan, np.nan, int(xs.size), best_area, int(mass), np.nan
        cx = float(xs_comp.mean()) + x0 + crop_x0
        cy = float(ys_comp.mean()) + y0 + crop_y0
    elif tracking_method == "distance_transform_center":
        mask = component.astype(np.uint8)
        dist = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
        _, max_value, _, max_location = cv2.minMaxLoc(dist)
        if max_value <= 0:
            return np.nan, np.nan, int(xs.size), best_area, int(mass), np.nan
        cx = float(max_location[0]) + x0 + crop_x0
        cy = float(max_location[1]) + y0 + crop_y0
        radius_px = float(max_value)
    elif tracking_method == "min_enclosing_circle":
        ys_comp, xs_comp = np.nonzero(component)
        if xs_comp.size == 0:
            return np.nan, np.nan, int(xs.size), best_area, int(mass), np.nan
        points = np.column_stack([xs_comp.astype(np.float32), ys_comp.astype(np.float32)])
        (circle_x, circle_y), radius_px = cv2.minEnclosingCircle(points)
        cx = float(circle_x) + x0 + crop_x0
        cy = float(circle_y) + y0 + crop_y0
        radius_px = float(radius_px)
    elif tracking_method == "fit_ellipse":
        mask = component.astype(np.uint8) * 255
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not contours:
            return np.nan, np.nan, int(xs.size), best_area, int(mass), np.nan
        contour = max(contours, key=cv2.contourArea)
        if len(contour) < 5:
            return np.nan, np.nan, int(xs.size), best_area, int(mass), np.nan
        (ellipse_x, ellipse_y), axes, _ = cv2.fitEllipse(contour)
        if not inside_component_neighbourhood(float(ellipse_x), float(ellipse_y)):
            return np.nan, np.nan, int(xs.size), best_area, int(mass), np.nan
        if max(float(axes[0]), float(axes[1])) > 4.0 * max(comp_width, comp_height, 1):
            return np.nan, np.nan, int(xs.size), best_area, int(mass), np.nan
        cx = float(ellipse_x) + x0 + crop_x0
        cy = float(ellipse_y) + y0 + crop_y0
        radius_px = float((axes[0] + axes[1]) * 0.25)
    elif tracking_method == "ransac_circle":
        mask = component.astype(np.uint8) * 255
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if contours:
            contour = max(contours, key=cv2.contourArea)
            points = contour.reshape(-1, 2).astype(float)
        else:
            ys_comp, xs_comp = np.nonzero(component)
            points = np.column_stack([xs_comp.astype(float), ys_comp.astype(float)])
        fitted = ransac_circle(points, int(ransac_iterations), float(ransac_residual_px))
        if fitted is None:
            return np.nan, np.nan, int(xs.size), best_area, int(mass), np.nan
        circle_x, circle_y, radius_px = fitted
        if not inside_component_neighbourhood(float(circle_x), float(circle_y)):
            return np.nan, np.nan, int(xs.size), best_area, int(mass), np.nan
        cx = float(circle_x) + x0 + crop_x0
        cy = float(circle_y) + y0 + crop_y0
        radius_px = float(radius_px)
    elif tracking_method == "hough_circle":
        gray = np.zeros_like(img, dtype=np.uint8)
        if np.any(component):
            component_values = img[component].astype(np.float32)
            scale = max(float(np.percentile(component_values, 99.0)), 1.0)
            gray[component] = np.clip(img[component].astype(np.float32) / scale * 255.0, 0, 255).astype(np.uint8)
        if gray.size and min(gray.shape) >= 3:
            gray = cv2.GaussianBlur(gray, (3, 3), 0)
            circles = cv2.HoughCircles(
                gray,
                cv2.HOUGH_GRADIENT,
                dp=max(float(hough_dp), 1.0),
                minDist=max(float(hough_min_dist_px), 1.0),
                param1=max(float(hough_param1), 1.0),
                param2=max(float(hough_param2), 1.0),
                minRadius=max(1, int(hough_min_radius_px)),
                maxRadius=max(0, int(hough_max_radius_px)),
            )
        else:
            circles = None
        if circles is None or circles.size == 0:
            return np.nan, np.nan, int(xs.size), best_area, int(mass), np.nan
        weighted_cx = float((weights * xx).sum() / mass)
        weighted_cy = float((weights * yy).sum() / mass)
        candidates = np.asarray(circles[0], dtype=float)
        distances = np.hypot(candidates[:, 0] - weighted_cx, candidates[:, 1] - weighted_cy)
        circle = candidates[int(np.argmin(distances))]
        cx = float(circle[0]) + x0 + crop_x0
        cy = float(circle[1]) + y0 + crop_y0
        radius_px = float(circle[2])
    else:
        cx = float((weights * xx).sum() / mass) + x0 + crop_x0
        cy = float((weights * yy).sum() / mass) + y0 + crop_y0
    return cx, cy, int(xs.size), best_area, int(mass), radius_px


def save_plots(out_dir: Path, t: np.ndarray, x: np.ndarray, y: np.ndarray, t_i: np.ndarray, x_i: np.ndarray, y_i: np.ndarray) -> None:
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 1, figsize=(9, 6), sharex=True)
    axes[0].plot(t, x, ".", ms=2, alpha=0.45, label="raw bins")
    axes[0].plot(t_i, x_i, "-", lw=1.0, label="interpolated")
    axes[0].set_ylabel("x [px]")
    axes[0].grid(ls=":", alpha=0.5)
    axes[0].legend(fontsize=8)

    axes[1].plot(t, y, ".", ms=2, alpha=0.45, label="raw bins")
    axes[1].plot(t_i, y_i, "-", lw=1.0, label="interpolated")
    axes[1].set_xlabel("t [s]")
    axes[1].set_ylabel("y [px]")
    axes[1].grid(ls=":", alpha=0.5)
    axes[1].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_dir / "t_xy.png", dpi=160)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6, 6))
    ax.plot(x, y, ".", ms=2, alpha=0.35, label="raw bins")
    ax.plot(x_i, y_i, "-", lw=1.0, label="interpolated")
    ax.plot(x_i[0], y_i[0], "o", ms=6, label="start")
    ax.plot(x_i[-1], y_i[-1], "*", ms=9, label="end")
    ax.set_xlabel("x [px]")
    ax.set_ylabel("y [px]  (camera z-axis in the XZ setup)")
    ax.invert_yaxis()
    ax.set_aspect("equal", "box")
    ax.grid(ls=":", alpha=0.5)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_dir / "xy.png", dpi=160)
    plt.close(fig)


def load_ideal_log(path: Path, time_offset_sec: float = 0.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    log = np.genfromtxt(path, delimiter=",", names=True, dtype=float, encoding="utf-8")
    if log.size == 0:
        raise SystemExit(f"Ideal log is empty: {path}")
    names = set(log.dtype.names or [])
    required = {"time", "target_x", "target_z"}
    if not required.issubset(names):
        raise SystemExit(f"Ideal log must contain columns {sorted(required)}: {path}")
    t = np.atleast_1d(log["time"]).astype(float) + float(time_offset_sec)
    x_mm = np.atleast_1d(log["target_x"]).astype(float) * 1000.0
    z_mm = np.atleast_1d(log["target_z"]).astype(float) * 1000.0
    if t.size:
        x_mm = x_mm - x_mm[0]
        z_mm = z_mm - z_mm[0]
    return t, x_mm, z_mm


def save_ideal_overlay_plots(
    out_dir: Path,
    t_i: np.ndarray,
    x_mm: np.ndarray,
    z_mm: np.ndarray,
    ideal_t: np.ndarray,
    ideal_x_mm: np.ndarray,
    ideal_z_mm: np.ndarray,
) -> None:
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 1, figsize=(9, 6), sharex=True)
    axes[0].plot(t_i, x_mm, "-", lw=1.0, label="measured")
    axes[0].plot(ideal_t, ideal_x_mm, "--", lw=1.0, label="ideal")
    axes[0].set_ylabel("x - x0 [mm]")
    axes[0].grid(ls=":", alpha=0.5)
    axes[0].legend(fontsize=8)

    axes[1].plot(t_i, z_mm, "-", lw=1.0, label="measured")
    axes[1].plot(ideal_t, ideal_z_mm, "--", lw=1.0, label="ideal")
    axes[1].set_xlabel("t [s]")
    axes[1].set_ylabel("z - z0 [mm]")
    axes[1].grid(ls=":", alpha=0.5)
    axes[1].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_dir / "t_xz_ideal_overlay_mm.png", dpi=160)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6, 6))
    ax.plot(x_mm, z_mm, "-", lw=1.0, label="measured")
    ax.plot(ideal_x_mm, ideal_z_mm, "--", lw=1.0, label="ideal")
    ax.plot(x_mm[0], z_mm[0], "o", ms=6, label="measured start")
    ax.set_xlabel("x - x0 [mm]")
    ax.set_ylabel("z - z0 [mm]")
    ax.set_aspect("equal", "box")
    ax.grid(ls=":", alpha=0.5)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_dir / "xz_ideal_overlay_mm.png", dpi=160)
    plt.close(fig)


def main() -> None:
    args = parse_args()
    npz_path = Path(args.npz).resolve()
    if not npz_path.exists():
        raise SystemExit(f"NPZ not found: {npz_path}")
    if args.bin_us <= 0:
        raise SystemExit("--bin-us must be positive.")
    window_us = int(args.window_us or args.bin_us)
    hop_us = int(args.hop_us or args.bin_us)
    if window_us <= 0 or hop_us <= 0:
        raise SystemExit("--window-us and --hop-us must be positive.")

    data = np.load(npz_path, allow_pickle=False)
    events = data["events"]
    if events.size == 0:
        raise SystemExit("events array is empty.")

    mask_rois = [parse_roi(text, "--mask-roi") for text in args.mask_roi]
    if mask_rois:
        keep = np.ones(events.size, dtype=bool)
        for mask_roi in mask_rois:
            assert mask_roi is not None
            x0, y0, x1, y1 = mask_roi
            keep &= ~(
                (events["x"] >= x0) & (events["x"] < x1)
                & (events["y"] >= y0) & (events["y"] < y1)
            )
        events = events[keep]
        if events.size == 0:
            raise SystemExit("No events remain after --mask-roi filtering.")

    if args.polarity == "on":
        events = events[events["p"] != 0]
    elif args.polarity == "off":
        events = events[events["p"] == 0]
    if events.size == 0:
        raise SystemExit("No events remain after polarity filtering.")

    output_start_ts_us = int(data["output_start_ts_us"]) if "output_start_ts_us" in data else int(events["t"][0])
    t_rel_us = events["t"].astype(np.int64) - output_start_ts_us
    analysis_start_us = max(0, int(round(float(args.t_start_sec) * 1_000_000)))
    analysis_end_us = int(round(float(args.t_end_sec) * 1_000_000)) if args.t_end_sec > 0 else None
    keep_analysis = t_rel_us >= analysis_start_us
    if analysis_end_us is not None:
        keep_analysis &= t_rel_us < analysis_end_us
    events = events[keep_analysis]
    if events.size == 0:
        raise SystemExit("No events remain after time-window filtering.")
    t_rel_us = events["t"].astype(np.int64) - output_start_ts_us - analysis_start_us

    sensor_width = int(args.sensor_width or (int(events["x"].max()) + 1))
    sensor_height = int(args.sensor_height or (int(events["y"].max()) + 1))
    roi = parse_roi(args.roi, "--roi") or (0, 0, sensor_width, sensor_height)

    out_dir = Path(args.output_dir) if args.output_dir else (npz_path.parent / "event_tracking")
    out_dir.mkdir(parents=True, exist_ok=True)

    duration_us = int(t_rel_us.max()) + 1
    starts = np.arange(0, max(duration_us - window_us, 0) + hop_us, hop_us, dtype=np.int64)
    if starts.size == 0:
        starts = np.array([0], dtype=np.int64)

    t_centres: list[float] = []
    x_centres: list[float] = []
    y_centres: list[float] = []
    rows: list[dict[str, object]] = []

    order = np.argsort(t_rel_us, kind="stable")
    events_sorted = events[order]
    t_sorted = t_rel_us[order]
    left = 0
    n_events = int(events_sorted.size)
    for i, start_value in enumerate(starts):
        start = int(start_value)
        end = int(start + window_us)
        while left < n_events and t_sorted[left] < start:
            left += 1
        right = left
        while right < n_events and t_sorted[right] < end:
            right += 1
        chunk = events_sorted[left:right]
        if chunk.size < int(args.min_events):
            cx = cy = np.nan
            roi_event_count = int(chunk.size)
            area = mass = 0
            radius = np.nan
        else:
            cx, cy, roi_event_count, area, mass, radius = centroid_from_events(
                chunk,
                roi,
                threshold_count=int(args.threshold_count),
                blur=int(args.blur),
                morph_open=int(args.morph_open),
                morph_close=int(args.morph_close),
                min_area=int(args.min_area),
                tracking_method=str(args.tracking_method),
                hough_dp=float(args.hough_dp),
                hough_min_dist_px=float(args.hough_min_dist_px),
                hough_param1=float(args.hough_param1),
                hough_param2=float(args.hough_param2),
                hough_min_radius_px=int(args.hough_min_radius_px),
                hough_max_radius_px=int(args.hough_max_radius_px),
                ransac_iterations=int(args.ransac_iterations),
                ransac_residual_px=float(args.ransac_residual_px),
            )
            if int(args.min_mass) > 0 and mass < int(args.min_mass):
                cx = cy = np.nan
                radius = np.nan
        t_s = (start + end) * 0.5e-6
        t_centres.append(t_s)
        x_centres.append(cx)
        y_centres.append(cy)
        rows.append(
            {
                "bin_index": i,
                "t_sec": f"{t_s:.9f}",
                "bin_start_us": start,
                "bin_end_us": end,
                "x_px": "" if not np.isfinite(cx) else f"{cx:.6f}",
                "y_px": "" if not np.isfinite(cy) else f"{cy:.6f}",
                "event_count": int(chunk.size),
                "roi_event_count": roi_event_count,
                "component_area_px": area,
                "component_event_mass": mass,
                "circle_radius_px": "" if not np.isfinite(radius) else f"{radius:.6f}",
                "tracking_rejected": 0,
            }
        )

    t = np.asarray(t_centres, dtype=float)
    x = np.asarray(x_centres, dtype=float)
    y = np.asarray(y_centres, dtype=float)
    rejected = reject_large_jumps(x, y, float(args.max_step_px))
    if rejected.any():
        x = x.copy()
        y = y.copy()
        x[rejected] = np.nan
        y[rejected] = np.nan
        for idx in np.flatnonzero(rejected):
            rows[int(idx)]["x_px"] = ""
            rows[int(idx)]["y_px"] = ""
            rows[int(idx)]["tracking_rejected"] = 1
    x_interp_src = x.copy()
    y_interp_src = y.copy()
    valid_raw_points_before_leading_fill = int(np.isfinite(x_interp_src).sum())
    first_valid_raw_index = None
    if not args.keep_leading_nan:
        x, y, first_valid_raw_index = fill_leading_with_first_valid_pair(x, y)
        if first_valid_raw_index is not None:
            write_xy_to_rows(rows, x, y, first_valid_raw_index)
    centres = np.column_stack([x, y])
    np.save(out_dir / "processed_centres.npy", centres)
    write_csv(
        out_dir / "event_centres_raw.csv",
        rows,
        [
            "bin_index",
            "t_sec",
            "bin_start_us",
            "bin_end_us",
            "x_px",
            "y_px",
            "event_count",
            "roi_event_count",
            "component_area_px",
            "component_event_mass",
            "circle_radius_px",
            "tracking_rejected",
        ],
    )

    dt_us = float(args.dt_us) if args.dt_us > 0 else float(hop_us) / max(float(args.interp_factor), 1.0)
    dt_s = dt_us * 1e-6
    t_i = np.arange(0.0, float(t[-1]) + 0.5 * dt_s, dt_s)
    x_i = finite_interp(t, x_interp_src, t_i, float(args.max_interp_gap_sec))
    y_i = finite_interp(t, y_interp_src, t_i, float(args.max_interp_gap_sec))
    first_valid_interp_index = None
    if not args.keep_leading_nan:
        x_i, y_i, first_valid_interp_index = fill_leading_with_first_valid_pair(x_i, y_i)
    interp = np.column_stack([t_i, x_i, y_i])

    origin_x = 0.0
    origin_y = 0.0
    if args.center_mode == "first":
        valid = np.flatnonzero(np.isfinite(x_i) & np.isfinite(y_i))
        if valid.size:
            origin_x = float(x_i[valid[0]])
            origin_y = float(y_i[valid[0]])
    elif args.center_mode == "mean":
        origin_x = float(np.nanmean(x_i))
        origin_y = float(np.nanmean(y_i))

    mm_per_px = 0.0
    if args.scale_mm_per_px > 0:
        mm_per_px = float(args.scale_mm_per_px)
    elif args.scale_px_per_mm > 0:
        mm_per_px = 1.0 / float(args.scale_px_per_mm)

    measured_x_mm = None
    measured_z_mm = None
    if mm_per_px > 0:
        sx = -1.0 if args.x_invert else 1.0
        sy = -1.0 if args.y_invert else 1.0
        measured_x_mm = sx * (x_i - origin_x) * mm_per_px
        measured_z_mm = sy * (y_i - origin_y) * mm_per_px
        interp = np.column_stack([t_i, x_i, y_i, measured_x_mm, measured_z_mm])
        interp_header = "t_sec,x_px,y_px,x_mm,y_mm"
    else:
        interp_header = "t_sec,x_px,y_px"

    np.save(out_dir / "event_centres_interp.npy", interp)
    np.savetxt(out_dir / "event_centres_interp.csv", interp, delimiter=",", header=interp_header, comments="")
    save_plots(out_dir, t, x, y, t_i, x_i, y_i)

    ideal_log_path = Path(args.ideal_log).resolve() if args.ideal_log else None
    if ideal_log_path is not None:
        if mm_per_px <= 0:
            raise SystemExit("--ideal-log requires --scale-px-per-mm or --scale-mm-per-px.")
        if not ideal_log_path.exists():
            raise SystemExit(f"Ideal log not found: {ideal_log_path}")
        ideal_t, ideal_x_mm, ideal_z_mm = load_ideal_log(ideal_log_path, float(args.ideal_time_offset_sec))
        save_ideal_overlay_plots(out_dir, t_i, measured_x_mm, measured_z_mm, ideal_t, ideal_x_mm, ideal_z_mm)

    meta = {
        "input_npz": str(npz_path),
        "output_dir": str(out_dir.resolve()),
        "bin_us": int(args.bin_us),
        "window_us": int(window_us),
        "hop_us": int(hop_us),
        "dt_unified_us": float(dt_us),
        "dt_unified_sec": float(dt_s),
        "interp_factor": float(args.interp_factor),
        "roi": list(roi),
        "mask_rois": [list(mask_roi) for mask_roi in mask_rois if mask_roi is not None],
        "polarity": args.polarity,
        "tracking_method": args.tracking_method,
        "valid_raw_points": valid_raw_points_before_leading_fill,
        "valid_raw_points_after_leading_fill": int(np.isfinite(x).sum()),
        "min_mass": int(args.min_mass),
        "hough_dp": float(args.hough_dp),
        "hough_min_dist_px": float(args.hough_min_dist_px),
        "hough_param1": float(args.hough_param1),
        "hough_param2": float(args.hough_param2),
        "hough_min_radius_px": int(args.hough_min_radius_px),
        "hough_max_radius_px": int(args.hough_max_radius_px),
        "ransac_iterations": int(args.ransac_iterations),
        "ransac_residual_px": float(args.ransac_residual_px),
        "tracking_rejected_points": int(rejected.sum()),
        "raw_points": int(len(x)),
        "interp_points": int(len(t_i)),
        "leading_nan_filled": bool(not args.keep_leading_nan),
        "first_valid_raw_index": first_valid_raw_index,
        "first_valid_interp_index": first_valid_interp_index,
        "scale_px_per_mm": float(args.scale_px_per_mm),
        "scale_mm_per_px": float(mm_per_px),
        "center_mode": args.center_mode,
        "ideal_log": None if ideal_log_path is None else str(ideal_log_path),
        "ideal_time_offset_sec": float(args.ideal_time_offset_sec),
        "output_start_ts_us": output_start_ts_us,
        "analysis_start_us": int(analysis_start_us),
        "analysis_end_us": None if analysis_end_us is None else int(analysis_end_us),
    }
    event_counts = np.asarray([int(row["event_count"]) for row in rows], dtype=np.int64)
    component_masses = np.asarray([int(row["component_event_mass"]) for row in rows], dtype=np.int64)
    raw_finite = np.isfinite(x) & np.isfinite(y)
    raw_finite_before_leading_fill = np.isfinite(x_interp_src) & np.isfinite(y_interp_src)
    summary = {
        "input_npz": str(npz_path),
        "output_dir": str(out_dir.resolve()),
        "raw_bins": int(len(rows)),
        "valid_raw_points": int(raw_finite_before_leading_fill.sum()),
        "valid_raw_points_after_leading_fill": int(raw_finite.sum()),
        "missing_raw_points": int(len(rows) - raw_finite.sum()),
        "missing_raw_points_before_leading_fill": int(len(rows) - raw_finite_before_leading_fill.sum()),
        "tracking_rejected_points": int(rejected.sum()),
        "interp_points": int(len(t_i)),
        "mask_rois": [list(mask_roi) for mask_roi in mask_rois if mask_roi is not None],
        "window_us": int(window_us),
        "hop_us": int(hop_us),
        "dt_unified_us": float(dt_us),
        "threshold_count": int(args.threshold_count),
        "min_events": int(args.min_events),
        "min_area": int(args.min_area),
        "min_mass": int(args.min_mass),
        "event_count": {
            "min": int(event_counts.min()) if event_counts.size else 0,
            "avg": float(event_counts.mean()) if event_counts.size else 0.0,
            "max": int(event_counts.max()) if event_counts.size else 0,
        },
        "component_event_mass": {
            "min": int(component_masses.min()) if component_masses.size else 0,
            "avg": float(component_masses.mean()) if component_masses.size else 0.0,
            "max": int(component_masses.max()) if component_masses.size else 0,
        },
    }
    (out_dir / "event_tracking_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (out_dir / "event_tracking_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"Input: {npz_path}")
    print(f"Output: {out_dir}")
    print(f"Raw bins: {len(x)} valid={valid_raw_points_before_leading_fill} after_leading_fill={np.isfinite(x).sum()}")
    print(f"Summary: {out_dir / 'event_tracking_summary.json'}")
    print(f"Interpolated: {len(t_i)} points, dt={dt_s:.9f} s ({1.0 / dt_s:.1f} Hz)")
    print(f"processed_centres.npy: {out_dir / 'processed_centres.npy'}")
    print(f"plots: {out_dir / 't_xy.png'}, {out_dir / 'xy.png'}")
    if ideal_log_path is not None:
        print(f"ideal overlays: {out_dir / 't_xz_ideal_overlay_mm.png'}, {out_dir / 'xz_ideal_overlay_mm.png'}")


if __name__ == "__main__":
    main()
