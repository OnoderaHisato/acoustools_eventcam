#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render event-camera NPZ events into a video at an arbitrary frame rate."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import cv2
import numpy as np


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render *_events.npz into a video, optionally overlaying eventcam_npz_track.py results.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("npz", help="Input *_events.npz.")
    parser.add_argument("--output", default="", help="Output video path. Empty writes beside NPZ.")
    parser.add_argument("--fps", type=float, default=10000.0, help="Render frame rate, in frames/sec.")
    parser.add_argument("--video-fps", type=float, default=0.0, help="Playback FPS in the video header. 0 uses --fps.")
    parser.add_argument(
        "--accumulation-us",
        type=int,
        default=0,
        help="Event accumulation window per rendered frame, in microseconds. 0 uses the render frame period.",
    )
    parser.add_argument(
        "--accumulation-sec",
        type=float,
        default=0.0,
        help="Event accumulation window per rendered frame, in seconds. Overrides --accumulation-us when > 0.",
    )
    parser.add_argument("--t-start-sec", type=float, default=0.0, help="Start time after NPZ output_start.")
    parser.add_argument("--duration-sec", type=float, default=1.3, help="Duration to render. 0 renders all events.")
    parser.add_argument("--max-frames", type=int, default=0, help="Maximum rendered frames. 0 disables.")
    parser.add_argument("--sensor-width", type=int, default=0, help="Sensor width override. 0 infers from events.")
    parser.add_argument("--sensor-height", type=int, default=0, help="Sensor height override. 0 infers from events.")
    parser.add_argument("--tracking-csv", default="", help="Optional event_centres_interp.csv to overlay.")
    parser.add_argument("--tracking-time-offset-sec", type=float, default=0.0, help="Offset added to video time when looking up tracking CSV time.")
    parser.add_argument("--trail-sec", type=float, default=0.030, help="Tracking trail length in seconds.")
    parser.add_argument("--radius", type=int, default=7, help="Current tracking marker radius.")
    parser.add_argument("--line-width", type=int, default=2, help="Overlay line width.")
    parser.add_argument("--point-size", type=int, default=1, help="Rendered event dot size in pixels.")
    parser.add_argument("--crop", default="", help="Optional output crop x0,y0,x1,y1 before scaling.")
    parser.add_argument("--scale", type=float, default=1.0, help="Output scale applied after --crop.")
    parser.add_argument("--draw-time", action="store_true", help="Draw frame time/status text.")
    parser.add_argument("--codec", default="", help="FourCC override. Empty uses mp4v for mp4, MJPG otherwise.")
    return parser.parse_args()


def parse_crop(text: str, width: int, height: int) -> tuple[int, int, int, int] | None:
    if not text:
        return None
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 4:
        raise SystemExit("--crop must be x0,y0,x1,y1")
    x0, y0, x1, y1 = [int(part) for part in parts]
    x0 = max(0, min(x0, width - 1))
    y0 = max(0, min(y0, height - 1))
    x1 = max(x0 + 1, min(x1, width))
    y1 = max(y0 + 1, min(y1, height))
    return x0, y0, x1, y1


def load_tracking(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    data = np.genfromtxt(path, delimiter=",", names=True, dtype=float, encoding="utf-8")
    names = set(data.dtype.names or [])
    required = {"t_sec", "x_px", "y_px"}
    if not required.issubset(names):
        raise SystemExit(f"Tracking CSV must contain {sorted(required)}: {path}")
    return (
        np.atleast_1d(data["t_sec"]).astype(float),
        np.atleast_1d(data["x_px"]).astype(float),
        np.atleast_1d(data["y_px"]).astype(float),
    )


def nearest_index(t: np.ndarray, value: float) -> int:
    idx = int(np.searchsorted(t, value, side="left"))
    if idx <= 0:
        return 0
    if idx >= len(t):
        return len(t) - 1
    return idx if abs(t[idx] - value) < abs(value - t[idx - 1]) else idx - 1


def finite_points_in_window(
    t: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    start: float,
    end: float,
) -> np.ndarray:
    mask = (t >= start) & (t <= end) & np.isfinite(x) & np.isfinite(y)
    if not np.any(mask):
        return np.empty((0, 2), dtype=np.int32)
    return np.column_stack([np.rint(x[mask]).astype(np.int32), np.rint(y[mask]).astype(np.int32)])


def draw_tracking(
    frame: np.ndarray,
    frame_t_sec: float,
    tracking: tuple[np.ndarray, np.ndarray, np.ndarray] | None,
    trail_sec: float,
    radius: int,
    line_width: int,
) -> bool:
    if tracking is None:
        return False
    t_track, x_track, y_track = tracking
    pts = finite_points_in_window(t_track, x_track, y_track, frame_t_sec - trail_sec, frame_t_sec)
    if len(pts) >= 2:
        cv2.polylines(frame, [pts.reshape(-1, 1, 2)], False, (0, 210, 255), line_width, cv2.LINE_AA)

    idx = nearest_index(t_track, frame_t_sec)
    cx = x_track[idx]
    cy = y_track[idx]
    if not (np.isfinite(cx) and np.isfinite(cy)):
        return False
    p = (int(round(cx)), int(round(cy)))
    cv2.circle(frame, p, radius, (0, 0, 255), line_width, lineType=cv2.LINE_AA)
    cv2.drawMarker(
        frame,
        p,
        (255, 255, 255),
        markerType=cv2.MARKER_CROSS,
        markerSize=max(10, radius * 2),
        thickness=max(1, line_width),
        line_type=cv2.LINE_AA,
    )
    return True


def render_events(
    events: np.ndarray,
    width: int,
    height: int,
    point_size: int,
) -> np.ndarray:
    frame = np.full((height, width, 3), (24, 34, 45), dtype=np.uint8)
    if events.size == 0:
        return frame

    xs = events["x"].astype(np.int32)
    ys = events["y"].astype(np.int32)
    valid = (xs >= 0) & (xs < width) & (ys >= 0) & (ys < height)
    if not np.any(valid):
        return frame
    xs = xs[valid]
    ys = ys[valid]
    ps = events["p"][valid]

    on = ps != 0
    if np.any(~on):
        frame[ys[~on], xs[~on]] = (150, 150, 150)
    if np.any(on):
        frame[ys[on], xs[on]] = (255, 255, 255)

    if point_size > 1:
        mask = np.any(frame != np.array((24, 34, 45), dtype=np.uint8), axis=2).astype(np.uint8) * 255
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (point_size, point_size))
        dilated = cv2.dilate(mask, kernel)
        frame[dilated > 0] = np.maximum(frame[dilated > 0], np.array((180, 180, 180), dtype=np.uint8))
        if np.any(on):
            on_mask = np.zeros((height, width), dtype=np.uint8)
            on_mask[ys[on], xs[on]] = 255
            on_dilated = cv2.dilate(on_mask, kernel)
            frame[on_dilated > 0] = (255, 255, 255)
    return frame


def main() -> None:
    args = parse_args()
    npz_path = Path(args.npz).resolve()
    if not npz_path.exists():
        raise SystemExit(f"NPZ not found: {npz_path}")
    if args.fps <= 0:
        raise SystemExit("--fps must be positive.")

    data = np.load(npz_path, allow_pickle=False)
    events = data["events"]
    if events.size == 0:
        raise SystemExit("events array is empty.")
    output_start_ts_us = int(data["output_start_ts_us"]) if "output_start_ts_us" in data else int(events["t"][0])

    width = int(args.sensor_width or (int(events["x"].max()) + 1))
    height = int(args.sensor_height or (int(events["y"].max()) + 1))
    width = max(width, 1280)
    height = max(height, 720)
    crop = parse_crop(args.crop, width, height)
    if args.scale <= 0:
        raise SystemExit("--scale must be positive.")
    output_width = int(round((crop[2] - crop[0]) * args.scale)) if crop else int(round(width * args.scale))
    output_height = int(round((crop[3] - crop[1]) * args.scale)) if crop else int(round(height * args.scale))
    output_width = max(1, output_width)
    output_height = max(1, output_height)

    period_us = int(round(1_000_000.0 / float(args.fps)))
    if period_us <= 0:
        raise SystemExit("--fps is too high for integer microsecond frame periods.")
    accumulation_us = (
        int(round(float(args.accumulation_sec) * 1_000_000.0))
        if float(args.accumulation_sec) > 0
        else int(args.accumulation_us)
    )
    if accumulation_us <= 0:
        accumulation_us = period_us
    start_us = int(round(float(args.t_start_sec) * 1_000_000.0))
    rel_t = events["t"].astype(np.int64) - int(output_start_ts_us)

    keep = rel_t >= start_us
    if args.duration_sec > 0:
        end_us = start_us + int(round(float(args.duration_sec) * 1_000_000.0))
        keep &= rel_t < end_us
    else:
        end_us = int(rel_t.max()) + 1
    events = events[keep]
    rel_t = rel_t[keep] - start_us
    if events.size == 0:
        raise SystemExit("No events remain in the requested time window.")

    order = np.argsort(rel_t, kind="stable")
    events = events[order]
    rel_t = rel_t[order]
    duration_us = int(round(float(args.duration_sec) * 1_000_000.0)) if args.duration_sec > 0 else int(rel_t.max()) + 1
    n_frames = int(np.ceil(duration_us / period_us))
    if args.max_frames > 0:
        n_frames = min(n_frames, int(args.max_frames))

    video_fps = float(args.video_fps) if args.video_fps > 0 else float(args.fps)
    output_path = Path(args.output).resolve() if args.output else npz_path.with_name(f"{npz_path.stem}_{args.fps:g}fps.mp4")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fourcc_text = args.codec if args.codec else ("mp4v" if output_path.suffix.lower() == ".mp4" else "MJPG")
    fourcc = cv2.VideoWriter_fourcc(*fourcc_text[:4])
    writer = cv2.VideoWriter(str(output_path), fourcc, video_fps, (output_width, output_height), True)
    if not writer.isOpened():
        raise SystemExit(f"Could not open video writer: {output_path}")

    tracking = load_tracking(Path(args.tracking_csv).resolve()) if args.tracking_csv else None
    timestamp_csv = output_path.with_name(output_path.stem + "_timestamps.csv")

    left = 0
    n_events = int(events.size)
    with timestamp_csv.open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["frame_index", "frame_ts_us", "slice_start_ts_us", "slice_end_ts_us", "event_count", "on_count", "off_count", "tracked"]
        csv_writer = csv.DictWriter(f, fieldnames=fieldnames)
        csv_writer.writeheader()
        for frame_i in range(n_frames):
            frame_start = frame_i * period_us
            frame_end = min(frame_start + accumulation_us, duration_us)
            while left < n_events and rel_t[left] < frame_start:
                left += 1
            right = left
            while right < n_events and rel_t[right] < frame_end:
                right += 1
            chunk = events[left:right]
            frame = render_events(chunk, width, height, int(args.point_size))
            frame_t_sec = frame_start * 1e-6
            tracked = draw_tracking(
                frame,
                frame_t_sec + float(args.tracking_time_offset_sec),
                tracking,
                float(args.trail_sec),
                int(args.radius),
                int(args.line_width),
            )
            if args.draw_time:
                text = f"frame={frame_i}  t={frame_t_sec * 1000.0:.3f} ms  {'tracked' if tracked else 'no centroid'}"
                cv2.putText(frame, text, (24, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 0, 0), 4, cv2.LINE_AA)
                cv2.putText(frame, text, (24, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (255, 255, 255), 1, cv2.LINE_AA)
            if crop:
                x0, y0, x1, y1 = crop
                frame = frame[y0:y1, x0:x1]
            if args.scale != 1.0:
                frame = cv2.resize(frame, (output_width, output_height), interpolation=cv2.INTER_NEAREST)
            writer.write(frame)
            event_count = int(chunk.size)
            on_count = int(np.count_nonzero(chunk["p"])) if event_count else 0
            csv_writer.writerow(
                {
                    "frame_index": frame_i,
                    "frame_ts_us": frame_start,
                    "slice_start_ts_us": frame_start,
                    "slice_end_ts_us": frame_end,
                    "event_count": event_count,
                    "on_count": on_count,
                    "off_count": event_count - on_count,
                    "tracked": int(bool(tracked)),
                }
            )

    writer.release()
    print(f"Input NPZ: {npz_path}")
    print(f"Output video: {output_path}")
    print(f"Timestamps CSV: {timestamp_csv}")
    print(f"Render FPS: {float(args.fps):g}")
    print(f"Playback FPS: {video_fps:g}")
    print(f"Frames: {n_frames}")


if __name__ == "__main__":
    main()
