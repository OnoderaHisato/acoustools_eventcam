#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Estimate pixel/mm scale from a moved circular calibration target in event NPZ."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np


def parse_roi(text: str) -> tuple[int, int, int, int] | None:
    if not text:
        return None
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if len(parts) != 4:
        raise SystemExit(f"--roi must be x0,y0,x1,y1: {text}")
    x0, y0, x1, y1 = [int(part) for part in parts]
    if x1 <= x0 or y1 <= y0:
        raise SystemExit(f"--roi must satisfy x1>x0 and y1>y0: {text}")
    return x0, y0, x1, y1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Accumulate events from a circular target, fit its diameter in pixels, and optionally compute px/mm.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("npz", help="Input *_events.npz from the calibration recording.")
    parser.add_argument("--output-dir", default="", help="Output directory. Empty uses <npz parent>/scale_calibration.")
    parser.add_argument("--diameter-mm", type=float, default=0.0, help="Known physical circle diameter in mm. 0 reports px only.")
    parser.add_argument("--t-start-sec", type=float, default=0.0, help="Start time after NPZ output_start.")
    parser.add_argument("--duration-sec", type=float, default=0.0, help="Duration to use. 0 uses all remaining events.")
    parser.add_argument("--roi", default="", help="Optional ROI x0,y0,x1,y1 around the calibration target.")
    parser.add_argument("--sensor-width", type=int, default=0, help="Sensor width override. 0 infers from events.")
    parser.add_argument("--sensor-height", type=int, default=0, help="Sensor height override. 0 infers from events.")
    parser.add_argument("--polarity", choices=["all", "on", "off"], default="all", help="Which events to use.")
    parser.add_argument("--min-radius-px", type=int, default=10, help="Minimum circle radius for Hough fitting.")
    parser.add_argument("--max-radius-px", type=int, default=0, help="Maximum circle radius. 0 lets OpenCV choose.")
    parser.add_argument("--hough-param2", type=float, default=20.0, help="Hough accumulator threshold. Lower detects weaker circles.")
    parser.add_argument("--blur", type=int, default=7, help="Odd Gaussian blur kernel for accumulated event image.")
    parser.add_argument("--point-percentile", type=float, default=99.0, help="Percentile used to scale the saved event image.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    npz_path = Path(args.npz).resolve()
    if not npz_path.exists():
        raise SystemExit(f"NPZ not found: {npz_path}")

    data = np.load(npz_path, allow_pickle=False)
    events = data["events"]
    if events.size == 0:
        raise SystemExit("events array is empty.")

    if args.polarity == "on":
        events = events[events["p"] != 0]
    elif args.polarity == "off":
        events = events[events["p"] == 0]
    if events.size == 0:
        raise SystemExit("No events remain after polarity filtering.")

    output_start_ts_us = int(data["output_start_ts_us"]) if "output_start_ts_us" in data else int(events["t"][0])
    t_rel_us = events["t"].astype(np.int64) - output_start_ts_us
    start_us = max(0, int(round(float(args.t_start_sec) * 1_000_000.0)))
    keep = t_rel_us >= start_us
    if args.duration_sec > 0:
        keep &= t_rel_us < start_us + int(round(float(args.duration_sec) * 1_000_000.0))
    events = events[keep]
    if events.size == 0:
        raise SystemExit("No events remain in the requested time window.")

    width = int(args.sensor_width or (int(events["x"].max()) + 1))
    height = int(args.sensor_height or (int(events["y"].max()) + 1))
    width = max(width, 1280)
    height = max(height, 720)
    roi = parse_roi(args.roi) or (0, 0, width, height)
    x0, y0, x1, y1 = roi

    xs = events["x"].astype(np.int32)
    ys = events["y"].astype(np.int32)
    in_roi = (xs >= x0) & (xs < x1) & (ys >= y0) & (ys < y1)
    xs = xs[in_roi] - x0
    ys = ys[in_roi] - y0
    if xs.size == 0:
        raise SystemExit("No calibration events remain inside ROI.")

    counts = np.zeros((y1 - y0, x1 - x0), dtype=np.uint16)
    np.add.at(counts, (ys, xs), 1)

    scale_value = np.percentile(counts[counts > 0], float(args.point_percentile)) if np.any(counts > 0) else 1.0
    scale_value = max(float(scale_value), 1.0)
    gray = np.clip(counts.astype(np.float32) / scale_value * 255.0, 0, 255).astype(np.uint8)
    if args.blur and args.blur > 1:
        k = int(args.blur)
        if k % 2 == 0:
            k += 1
        gray_for_fit = cv2.GaussianBlur(gray, (k, k), 0)
    else:
        gray_for_fit = gray

    circles = cv2.HoughCircles(
        gray_for_fit,
        cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=max(20, min(gray.shape) // 4),
        param1=100,
        param2=float(args.hough_param2),
        minRadius=max(1, int(args.min_radius_px)),
        maxRadius=max(0, int(args.max_radius_px)),
    )
    if circles is None or circles.size == 0:
        raise SystemExit(
            "Circle was not detected. Try a tighter --roi, lower --hough-param2, "
            "or move the calibration circle more clearly during capture."
        )

    circle = np.asarray(circles[0][0], dtype=float)
    cx = float(circle[0]) + x0
    cy = float(circle[1]) + y0
    radius_px = float(circle[2])
    diameter_px = 2.0 * radius_px

    px_per_mm = None
    mm_per_px = None
    if args.diameter_mm > 0:
        px_per_mm = diameter_px / float(args.diameter_mm)
        mm_per_px = float(args.diameter_mm) / diameter_px

    out_dir = Path(args.output_dir) if args.output_dir else (npz_path.parent / "scale_calibration")
    out_dir.mkdir(parents=True, exist_ok=True)

    vis = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    cv2.circle(vis, (int(round(circle[0])), int(round(circle[1]))), int(round(radius_px)), (0, 0, 255), 2, cv2.LINE_AA)
    cv2.drawMarker(
        vis,
        (int(round(circle[0])), int(round(circle[1]))),
        (0, 255, 255),
        markerType=cv2.MARKER_CROSS,
        markerSize=18,
        thickness=2,
        line_type=cv2.LINE_AA,
    )
    label = f"diameter={diameter_px:.2f}px"
    if px_per_mm is not None:
        label += f"  scale={px_per_mm:.4f}px/mm"
    cv2.putText(vis, label, (20, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(vis, label, (20, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (255, 255, 255), 1, cv2.LINE_AA)

    raw_png = out_dir / "calibration_accumulated_events.png"
    overlay_png = out_dir / "calibration_circle_fit.png"
    cv2.imwrite(str(raw_png), gray)
    cv2.imwrite(str(overlay_png), vis)

    result = {
        "input_npz": str(npz_path),
        "output_dir": str(out_dir.resolve()),
        "roi": list(roi),
        "event_count_used": int(xs.size),
        "center_x_px": cx,
        "center_y_px": cy,
        "radius_px": radius_px,
        "diameter_px": diameter_px,
        "diameter_mm": None if args.diameter_mm <= 0 else float(args.diameter_mm),
        "scale_px_per_mm": px_per_mm,
        "scale_mm_per_px": mm_per_px,
        "raw_png": str(raw_png.resolve()),
        "overlay_png": str(overlay_png.resolve()),
    }
    (out_dir / "scale_calibration.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(f"Input: {npz_path}")
    print(f"Output: {out_dir}")
    print(f"Diameter: {diameter_px:.3f} px")
    if px_per_mm is not None:
        print(f"Scale: {px_per_mm:.6f} px/mm ({mm_per_px:.9f} mm/px)")
    print(f"PNG: {raw_png}")
    print(f"Overlay: {overlay_png}")


if __name__ == "__main__":
    main()
