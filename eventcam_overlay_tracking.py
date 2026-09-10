#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render a visual check video by overlaying event-camera tracking on a movie."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import cv2
import numpy as np


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Overlay event_centres_interp.csv tracking points onto a decoded event-camera video.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--video", required=True, help="Input MP4/AVI video.")
    parser.add_argument("--tracking-csv", required=True, help="event_centres_interp.csv from eventcam_npz_track.py.")
    parser.add_argument("--timestamps-csv", default="", help="Optional video timestamp CSV. Uses frame_ts_us when present.")
    parser.add_argument("--output", default="", help="Output AVI path. Empty writes beside tracking CSV.")
    parser.add_argument("--trail-sec", type=float, default=0.050, help="Seconds of trajectory trail to draw.")
    parser.add_argument("--radius", type=int, default=8, help="Current point marker radius in pixels.")
    parser.add_argument("--line-width", type=int, default=2, help="Overlay line width.")
    parser.add_argument("--max-frames", type=int, default=0, help="Stop after this many frames. 0 writes all frames.")
    parser.add_argument("--fps", type=float, default=0.0, help="Output FPS override. 0 uses input FPS.")
    parser.add_argument("--codec", default="MJPG", help="FourCC for output AVI.")
    parser.add_argument("--draw-time", action="store_true", help="Draw frame time and tracking status text.")
    return parser.parse_args()


def load_tracking(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    data = np.genfromtxt(path, delimiter=",", names=True, dtype=float, encoding="utf-8")
    names = set(data.dtype.names or [])
    required = {"t_sec", "x_px", "y_px"}
    if not required.issubset(names):
        raise SystemExit(f"Tracking CSV must contain {sorted(required)}: {path}")
    t = np.atleast_1d(data["t_sec"]).astype(float)
    x = np.atleast_1d(data["x_px"]).astype(float)
    y = np.atleast_1d(data["y_px"]).astype(float)
    return t, x, y


def load_frame_times(path: Path, frame_count: int, fps: float) -> np.ndarray:
    if path and path.exists():
        times: list[float] = []
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if "frame_ts_us" not in (reader.fieldnames or []):
                raise SystemExit(f"timestamps CSV must contain frame_ts_us: {path}")
            for row in reader:
                times.append(float(row["frame_ts_us"]) * 1e-6)
        if times:
            return np.asarray(times, dtype=float)
    if fps <= 0:
        raise SystemExit("FPS is unavailable; provide --timestamps-csv or --fps.")
    return np.arange(frame_count, dtype=float) / fps


def nearest_index(t: np.ndarray, value: float) -> int:
    idx = int(np.searchsorted(t, value, side="left"))
    if idx <= 0:
        return 0
    if idx >= len(t):
        return len(t) - 1
    if abs(t[idx] - value) < abs(value - t[idx - 1]):
        return idx
    return idx - 1


def finite_points_in_window(t: np.ndarray, x: np.ndarray, y: np.ndarray, start: float, end: float) -> np.ndarray:
    mask = (t >= start) & (t <= end) & np.isfinite(x) & np.isfinite(y)
    if not np.any(mask):
        return np.empty((0, 2), dtype=np.int32)
    return np.column_stack([np.rint(x[mask]).astype(np.int32), np.rint(y[mask]).astype(np.int32)])


def draw_polyline_with_gaps(frame: np.ndarray, pts: np.ndarray, color: tuple[int, int, int], thickness: int) -> None:
    if len(pts) < 2:
        return
    cv2.polylines(frame, [pts.reshape(-1, 1, 2)], isClosed=False, color=color, thickness=thickness, lineType=cv2.LINE_AA)


def main() -> None:
    args = parse_args()
    video_path = Path(args.video).resolve()
    tracking_path = Path(args.tracking_csv).resolve()
    timestamps_path = Path(args.timestamps_csv).resolve() if args.timestamps_csv else Path()
    if not video_path.exists():
        raise SystemExit(f"Video not found: {video_path}")
    if not tracking_path.exists():
        raise SystemExit(f"Tracking CSV not found: {tracking_path}")

    t_track, x_track, y_track = load_tracking(tracking_path)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise SystemExit(f"Could not open video: {video_path}")
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    in_fps = float(cap.get(cv2.CAP_PROP_FPS)) or 0.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    out_fps = float(args.fps) if args.fps > 0 else in_fps
    frame_times = load_frame_times(timestamps_path, frame_count, in_fps)

    output_path = Path(args.output).resolve() if args.output else tracking_path.with_name(f"{video_path.stem}_tracking_overlay.avi")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*args.codec[:4])
    writer = cv2.VideoWriter(str(output_path), fourcc, out_fps, (width, height), True)
    if not writer.isOpened():
        raise SystemExit(f"Could not open output writer: {output_path}")

    n_written = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame_i = n_written
        if args.max_frames > 0 and frame_i >= args.max_frames:
            break
        frame_t = float(frame_times[frame_i]) if frame_i < len(frame_times) else frame_i / out_fps

        trail_pts = finite_points_in_window(t_track, x_track, y_track, frame_t - float(args.trail_sec), frame_t)
        draw_polyline_with_gaps(frame, trail_pts, (0, 210, 255), int(args.line_width))

        idx = nearest_index(t_track, frame_t)
        cx = x_track[idx]
        cy = y_track[idx]
        tracked = np.isfinite(cx) and np.isfinite(cy)
        if tracked:
            p = (int(round(cx)), int(round(cy)))
            cv2.circle(frame, p, int(args.radius), (0, 0, 255), int(args.line_width), lineType=cv2.LINE_AA)
            cv2.drawMarker(
                frame,
                p,
                (255, 255, 255),
                markerType=cv2.MARKER_CROSS,
                markerSize=max(10, int(args.radius) * 2),
                thickness=max(1, int(args.line_width)),
                line_type=cv2.LINE_AA,
            )

        if args.draw_time:
            status = "tracked" if tracked else "no centroid"
            text = f"frame={frame_i}  t={frame_t * 1000.0:.3f} ms  {status}"
            cv2.putText(frame, text, (24, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 0, 0), 4, cv2.LINE_AA)
            cv2.putText(frame, text, (24, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (255, 255, 255), 1, cv2.LINE_AA)

        writer.write(frame)
        n_written += 1

    cap.release()
    writer.release()
    print(f"Input video: {video_path}")
    print(f"Tracking CSV: {tracking_path}")
    print(f"Output: {output_path}")
    print(f"Frames written: {n_written}")


if __name__ == "__main__":
    main()
