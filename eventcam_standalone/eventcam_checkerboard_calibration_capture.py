#!/usr/bin/env python3
"""Capture event-camera checkerboard calibration poses.

This script records one short RAW file per checkerboard pose and writes one
calibration PNG per pose into ``calib_images``.  Optional NPZ export is
available for offline event processing.  The PNGs are intended for
``single_camera_calibrate.py``.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import gc
import hashlib
import json
import math
import re
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

import eventcam_scale_calibration_capture as scale_capture


PATTERN_SIZE = (9, 6)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactive checkerboard capture for event-camera intrinsic calibration.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--serial", default="", help="Camera serial/device id. Empty opens the first detected camera.")
    parser.add_argument("--list-devices", action="store_true", help="List detected devices and exit.")
    parser.add_argument("--output-dir", default="checkerboard_calibration_capture", help="Directory for outputs.")
    parser.add_argument("--basename", default="checkerboard_pose", help="Output filename prefix.")
    parser.add_argument(
        "--checkerboard-image",
        type=Path,
        default=None,
        help="Optional checkerboard source image recorded in the capture manifest.",
    )
    parser.add_argument(
        "--square-mm",
        type=float,
        default=0.0,
        help="Optional measured square side recorded in the capture manifest.",
    )
    parser.add_argument(
        "--require-square-mm",
        type=float,
        default=0.0,
        help="Safety lock: when positive, --square-mm must match this value.",
    )
    parser.add_argument("--duration-sec", type=float, default=0.6, help="Recording duration per pose. 0 means Enter toggles stop.")
    parser.add_argument("--delta-t-us", type=int, default=10_000, help="Live event slice duration during capture.")
    parser.add_argument("--preview-fps", type=float, default=25.0, help="Preview update FPS.")
    parser.add_argument("--preview-accumulation-us", type=int, default=10_000, help="Preview accumulation time.")
    parser.add_argument(
        "--preview-during-processing",
        action="store_true",
        help=(
            "Keep rendering live events while a completed pose is exported. "
            "By default, live events are drained but dropped from the preview "
            "until processing finishes, preventing stale-event catch-up."
        ),
    )
    parser.add_argument("--post-stop-wait-sec", type=float, default=0.2, help="Wait after stopping RAW logging.")
    parser.add_argument("--npz-delta-t-us", type=int, default=1_000, help="RAW read slice duration for NPZ export.")
    parser.add_argument(
        "--save-npz",
        action="store_true",
        help=(
            "Also export the recorded RAW to a compressed NPZ. RAW is always "
            "kept, so NPZ export is disabled by default to make pose capture faster."
        ),
    )
    parser.add_argument("--frame-window-us", type=int, default=20_000, help="Default accumulation window for candidate PNGs.")
    parser.add_argument(
        "--frame-window-us-list",
        default="",
        help="Comma-separated accumulation windows to try. Empty uses --frame-window-us.",
    )
    parser.add_argument("--candidate-count", type=int, default=1, help="Candidate windows saved per pose.")
    parser.add_argument("--render-mode", choices=["signed", "abs", "on", "off", "all"], default="off", help="How events are rendered.")
    parser.add_argument(
        "--exhaustive-candidates",
        action="store_true",
        help=(
            "Render and test every requested window/candidate/mode combination. "
            "By default, processing stops after the first successful corner detection."
        ),
    )
    parser.add_argument(
        "--max-candidate-tests",
        type=int,
        default=12,
        help=(
            "Maximum rendered/detected combinations when using the default "
            "first-found search. Ignored with --exhaustive-candidates."
        ),
    )
    parser.add_argument("--scale-percentile", type=float, default=99.5, help="Percentile used for PNG contrast scaling.")
    parser.add_argument("--min-events", type=int, default=200, help="Minimum events required in a candidate window.")
    return parser.parse_args()


def parse_int_list(text: str, default_value: int, name: str) -> list[int]:
    if not text.strip():
        return [int(default_value)]
    values: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        value = int(part)
        if value <= 0:
            raise SystemExit(f"{name} values must be positive: {text}")
        values.append(value)
    if not values:
        raise SystemExit(f"{name} did not contain any values.")
    return sorted(set(values))


def render_modes_for_arg(mode: str) -> list[str]:
    if mode == "all":
        # Signed images frequently have no detectable checkerboard, and the
        # exhaustive OpenCV failure path is much slower than a successful
        # detection.  Try the robust count images first.
        return ["abs", "off", "on", "signed"]
    return [mode]


def list_devices(metavision_hal: Any) -> None:
    devices = metavision_hal.DeviceDiscovery.list()
    print("Detected devices:")
    if not devices:
        print("  none")
        return
    for device in devices:
        print(f"  {device}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def make_paths(out_dir: Path, basename: str, pose_index: int) -> tuple[Path, Path]:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = out_dir / "raw" / f"{basename}_{pose_index:03d}_{stamp}"
    return stem.with_suffix(".raw"), stem.with_suffix(".json")


def max_existing_pose_index(out_dir: Path) -> int:
    patterns = [
        re.compile(r"pose_(\d+)", re.IGNORECASE),
        re.compile(r"checkerboard_pose_(\d+)", re.IGNORECASE),
    ]
    max_index = 0
    for folder in [out_dir / "calib_images", out_dir / "candidates", out_dir / "corner_debug", out_dir / "raw"]:
        if not folder.exists():
            continue
        for path in folder.rglob("*"):
            for pattern in patterns:
                match = pattern.search(path.name)
                if match:
                    max_index = max(max_index, int(match.group(1)))

    for summary_path in (out_dir / "raw").glob("*.json"):
        try:
            payload = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if "pose_index" in payload:
            max_index = max(max_index, int(payload["pose_index"]))
    return max_index


def new_stats(raw_path: Path, pose_index: int, sensor_width: int, sensor_height: int, args: argparse.Namespace) -> dict[str, Any]:
    return {
        "pose_index": int(pose_index),
        "raw_path": str(raw_path.resolve()),
        "sensor_size": {"width": int(sensor_width), "height": int(sensor_height)},
        "started_at": dt.datetime.now().isoformat(timespec="seconds"),
        "stopped_at": None,
        "total_slices": 0,
        "total_events": 0,
        "first_event_ts_us": None,
        "last_event_ts_us": None,
        "pattern_size": list(PATTERN_SIZE),
        "capture_settings": {
            "duration_sec": float(args.duration_sec),
            "delta_t_us": int(args.delta_t_us),
            "preview_accumulation_us": int(args.preview_accumulation_us),
            "frame_window_us": int(args.frame_window_us),
            "frame_window_us_list": parse_int_list(args.frame_window_us_list, args.frame_window_us, "--frame-window-us-list"),
            "render_mode": str(args.render_mode),
            "candidate_count": int(args.candidate_count),
            "min_events": int(args.min_events),
            "scale_percentile": float(args.scale_percentile),
            "exhaustive_candidates": bool(getattr(args, "exhaustive_candidates", False)),
            "max_candidate_tests": int(getattr(args, "max_candidate_tests", 12)),
            "save_npz": bool(getattr(args, "save_npz", False)),
            "checkerboard_image": (
                str(args.checkerboard_image.resolve())
                if getattr(args, "checkerboard_image", None) is not None
                else ""
            ),
            "square_size_mm": float(getattr(args, "square_mm", 0.0)),
        },
    }


def update_stats(stats: dict[str, Any], events: np.ndarray) -> None:
    stats["total_slices"] += 1
    count = int(events.size)
    stats["total_events"] += count
    if count == 0:
        return
    if stats["first_event_ts_us"] is None:
        stats["first_event_ts_us"] = int(events["t"][0])
    stats["last_event_ts_us"] = int(events["t"][-1])


def export_raw_to_npz(raw_path: Path, EventsIterator: Any, delta_t_us: int) -> Path:
    npz_path = raw_path.with_name(f"{raw_path.stem}_events.npz")
    iterator = EventsIterator(
        input_path=str(raw_path.resolve()),
        mode="delta_t",
        delta_t=max(1, int(delta_t_us)),
        relative_timestamps=False,
    )
    chunks: list[np.ndarray] = []
    output_start_ts_us: int | None = None
    output_end_ts_us: int | None = None
    for events in iterator:
        if events.size == 0:
            continue
        if output_start_ts_us is None:
            output_start_ts_us = int(events["t"][0])
        output_end_ts_us = int(events["t"][-1]) + 1
        chunks.append(events.copy())

    if chunks:
        all_events = np.concatenate(chunks)
    else:
        all_events = np.empty(0, dtype=[("x", "<u2"), ("y", "<u2"), ("p", "u1"), ("t", "<i8")])
        output_start_ts_us = 0
        output_end_ts_us = 0

    np.savez_compressed(
        npz_path,
        events=all_events,
        sync_ts_us=-1,
        output_start_ts_us=int(output_start_ts_us or 0),
        output_end_ts_us=int(output_end_ts_us or 0),
        mask_rois=np.empty((0, 4), dtype=np.int32),
    )
    return npz_path


def render_events(events: np.ndarray, width: int, height: int, mode: str, scale_percentile: float) -> np.ndarray:
    if events.size == 0:
        return np.full((height, width), 127, dtype=np.uint8)

    xs = events["x"].astype(np.int32)
    ys = events["y"].astype(np.int32)
    valid = (xs >= 0) & (xs < width) & (ys >= 0) & (ys < height)
    xs = xs[valid]
    ys = ys[valid]
    ps = events["p"][valid]

    if mode == "signed":
        accum = np.zeros((height, width), dtype=np.float32)
        vals = np.where(ps != 0, 1.0, -1.0).astype(np.float32)
        np.add.at(accum, (ys, xs), vals)
        positive = np.abs(accum[accum != 0])
        scale = np.percentile(positive, scale_percentile) if positive.size else 1.0
        scale = max(float(scale), 1.0)
        return np.clip(127.0 + accum / scale * 127.0, 0, 255).astype(np.uint8)

    counts = np.zeros((height, width), dtype=np.float32)
    if mode == "on":
        keep = ps != 0
        xs, ys = xs[keep], ys[keep]
    elif mode == "off":
        keep = ps == 0
        xs, ys = xs[keep], ys[keep]
    np.add.at(counts, (ys, xs), 1.0)
    positive = counts[counts > 0]
    scale = np.percentile(positive, scale_percentile) if positive.size else 1.0
    scale = max(float(scale), 1.0)
    return np.clip(counts / scale * 255.0, 0, 255).astype(np.uint8)


def candidate_windows(events: np.ndarray, window_us: int, min_events: int, candidate_count: int) -> list[tuple[int, int, int]]:
    if events.size == 0:
        return []
    t0 = int(events["t"][0])
    rel = events["t"].astype(np.int64) - t0
    bin_index = rel // int(window_us)
    counts = np.bincount(bin_index.astype(np.int64))
    order = np.argsort(counts)[::-1]
    selected: list[tuple[int, int, int]] = []
    for idx in order:
        count = int(counts[idx])
        if count < int(min_events):
            break
        start = t0 + int(idx) * int(window_us)
        end = start + int(window_us)
        selected.append((start, end, count))
        if len(selected) >= int(candidate_count):
            break
    return selected


def write_pose_images_from_events(
    events: np.ndarray,
    pose_index: int,
    out_dir: Path,
    args: argparse.Namespace,
    sensor_size: dict[str, int],
    source_path: Path,
    npz_path: Path | None = None,
) -> dict[str, Any]:
    width = int(sensor_size["width"])
    height = int(sensor_size["height"])
    candidates_dir = out_dir / "candidates" / f"pose_{pose_index:03d}"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    calib_dir = out_dir / "calib_images"
    calib_dir.mkdir(parents=True, exist_ok=True)
    debug_dir = out_dir / "corner_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)

    detector_flags = cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY
    rows: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    window_us_values = parse_int_list(args.frame_window_us_list, args.frame_window_us, "--frame-window-us-list")
    preferred_window_us = int(args.frame_window_us)
    window_us_values.sort(key=lambda value: (abs(value - preferred_window_us), value))
    exhaustive = bool(getattr(args, "exhaustive_candidates", False))
    modes = render_modes_for_arg(str(args.render_mode))
    if not exhaustive and str(args.render_mode) == "all":
        # Signed is retained when explicitly requested, but omitted from the
        # normal "all" fast path because count images are more reliable here.
        modes = [mode for mode in modes if mode != "signed"]
    max_candidate_tests = max(1, int(getattr(args, "max_candidate_tests", 12)))
    windows_by_size = {
        int(window_us): candidate_windows(
            events,
            int(window_us),
            int(args.min_events),
            int(args.candidate_count),
        )
        for window_us in window_us_values
    }
    stop_search = False

    max_windows = max((len(windows) for windows in windows_by_size.values()), default=0)
    for candidate_index in range(max_windows):
        for window_us in window_us_values:
            windows = windows_by_size[int(window_us)]
            if candidate_index >= len(windows):
                continue
            start_us, end_us, event_count = windows[candidate_index]
            keep = (events["t"] >= start_us) & (events["t"] < end_us)
            window_events = events[keep]
            for mode in modes:
                image = render_events(window_events, width, height, mode, float(args.scale_percentile))
                image_path = candidates_dir / f"pose_{pose_index:03d}_cand_{candidate_index:02d}_{mode}_{window_us}us.png"
                cv2.imwrite(str(image_path), image)

                found, corners = cv2.findChessboardCornersSB(image, PATTERN_SIZE, flags=detector_flags)
                debug = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
                if found:
                    cv2.drawChessboardCorners(debug, PATTERN_SIZE, corners, found)
                debug_path = debug_dir / image_path.name
                cv2.imwrite(str(debug_path), debug)

                row = {
                    "candidate_index": int(candidate_index),
                    "render_mode": mode,
                    "frame_window_us": int(window_us),
                    "start_ts_us": int(start_us),
                    "end_ts_us": int(end_us),
                    "event_count": int(event_count),
                    "found_corners": bool(found),
                    "image_path": str(image_path.resolve()),
                    "debug_path": str(debug_path.resolve()),
                }
                rows.append(row)
                if best is None:
                    best = row
                elif found and not best["found_corners"]:
                    best = row
                elif found == best["found_corners"] and event_count > int(best["event_count"]):
                    best = row
                if found and not exhaustive:
                    stop_search = True
                    break
                if not exhaustive and len(rows) >= max_candidate_tests:
                    stop_search = True
                    break
            if stop_search:
                break
        if stop_search:
            break

    selected_path = None
    if best is not None:
        selected_image = cv2.imread(best["image_path"], cv2.IMREAD_GRAYSCALE)
        selected_path = calib_dir / f"pose_{pose_index:03d}.png"
        cv2.imwrite(str(selected_path), selected_image)

    return {
        "source_path": str(source_path.resolve()),
        "npz_path": None if npz_path is None else str(npz_path.resolve()),
        "pose_index": int(pose_index),
        "selected_calibration_image": None if selected_path is None else str(selected_path.resolve()),
        "selected_found_corners": None if best is None else bool(best["found_corners"]),
        "search_policy": "exhaustive" if exhaustive else "first_found",
        "max_candidate_tests": None if exhaustive else int(max_candidate_tests),
        "candidates": rows,
    }


def write_pose_images(
    npz_path: Path,
    pose_index: int,
    out_dir: Path,
    args: argparse.Namespace,
    sensor_size: dict[str, int],
) -> dict[str, Any]:
    with np.load(npz_path, allow_pickle=False) as data:
        events = data["events"]
        return write_pose_images_from_events(
            events,
            pose_index,
            out_dir,
            args,
            sensor_size,
            source_path=npz_path,
            npz_path=npz_path,
        )


def main() -> None:
    args = parse_args()
    if args.delta_t_us <= 0 or args.npz_delta_t_us <= 0 or args.frame_window_us <= 0:
        raise SystemExit("Time arguments must be positive.")
    parse_int_list(args.frame_window_us_list, args.frame_window_us, "--frame-window-us-list")
    if args.preview_fps <= 0:
        raise SystemExit("--preview-fps must be positive.")
    if args.max_candidate_tests <= 0:
        raise SystemExit("--max-candidate-tests must be positive.")
    if args.checkerboard_image is not None and not args.checkerboard_image.exists():
        raise SystemExit(f"Checkerboard image not found: {args.checkerboard_image.resolve()}")
    if args.require_square_mm > 0 and not math.isclose(
        float(args.square_mm),
        float(args.require_square_mm),
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise SystemExit(
            "Checkerboard square-size safety lock failed: "
            f"--square-mm={float(args.square_mm):.12g}, "
            f"required={float(args.require_square_mm):.12g}."
        )

    (
        EventsIterator,
        initiate_device,
        metavision_hal,
        PeriodicFrameGenerationAlgorithm,
        ColorPalette,
        EventLoop,
        BaseWindow,
        MTWindow,
        UIAction,
        UIKeyEvent,
    ) = scale_capture.import_metavision()

    if args.list_devices:
        list_devices(metavision_hal)
        return

    out_dir = Path(args.output_dir)
    (out_dir / "raw").mkdir(parents=True, exist_ok=True)
    (out_dir / "calib_images").mkdir(parents=True, exist_ok=True)

    device = None
    iterator = None
    is_recording = False
    pose_index = max_existing_pose_index(out_dir)
    current_raw_path: Path | None = None
    current_summary_path: Path | None = None
    current_stats: dict[str, Any] | None = None
    current_event_chunks: list[np.ndarray] = []
    recording_start_wall = 0.0
    last_rate_wall = time.perf_counter()
    last_rate_events = 0
    processing_future: concurrent.futures.Future[dict[str, Any]] | None = None
    processing_error: BaseException | None = None

    try:
        device = scale_capture.open_event_camera(initiate_device, metavision_hal, args.serial)
        events_stream = device.get_i_events_stream()
        if events_stream is None:
            raise RuntimeError("This event camera does not expose I_EventsStream; RAW logging is unavailable.")

        iterator = EventsIterator.from_device(
            device=device,
            mode="delta_t",
            delta_t=int(args.delta_t_us),
            max_duration=None,
            relative_timestamps=False,
        )
        height, width = iterator.get_size()
        sensor_size = {"width": int(width), "height": int(height)}

        frame_generator = PeriodicFrameGenerationAlgorithm(
            sensor_width=int(width),
            sensor_height=int(height),
            accumulation_time_us=int(args.preview_accumulation_us),
            fps=float(args.preview_fps),
            palette=ColorPalette.Dark,
        )

        print(f"Sensor: {width} x {height}")
        print("Enter: capture one pose, Esc/Q: finish.")
        if pose_index > 0:
            print(f"Continuing after existing pose index {pose_index:03d}.")
        if args.duration_sec > 0:
            print(f"Each pose records for {args.duration_sec:.3f} s.")
        else:
            print("Press Enter again to stop the current pose.")

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="checkerboard-postprocess",
        ) as processing_executor, MTWindow(
                title="Checkerboard Calibration Capture - Enter: Capture Pose, Esc/Q: Quit",
                width=int(width),
                height=int(height),
                mode=BaseWindow.RenderMode.BGR,
        ) as window:

            def process_finished_pose(
                raw_path: Path,
                summary_path: Path,
                stats: dict[str, Any],
                event_chunks: list[np.ndarray],
                post_stop_wait_sec: float,
            ) -> dict[str, Any]:
                """Finalize one pose without blocking live-event draining."""
                processing_started = time.perf_counter()
                final_stats = dict(stats)
                if event_chunks:
                    events = np.concatenate(event_chunks)
                    event_chunks.clear()
                else:
                    events = np.empty(
                        0,
                        dtype=[("x", "<u2"), ("y", "<u2"), ("p", "u1"), ("t", "<i8")],
                    )

                # Create the calibration/debug images directly from the live
                # chunks.  This avoids RAW replay and NPZ compression on the
                # critical path to the next pose.
                render_report = write_pose_images_from_events(
                    events,
                    int(final_stats["pose_index"]),
                    out_dir,
                    args,
                    sensor_size,
                    source_path=raw_path,
                )
                final_stats["npz_path"] = None
                final_stats["render_report"] = render_report
                final_stats["image_processing_sec"] = float(
                    time.perf_counter() - processing_started
                )
                summary_path.write_text(
                    json.dumps(final_stats, indent=2),
                    encoding="utf-8",
                )

                if bool(getattr(args, "save_npz", False)):
                    elapsed = time.perf_counter() - processing_started
                    remaining_wait = max(0.0, post_stop_wait_sec - elapsed)
                    if remaining_wait > 0:
                        time.sleep(remaining_wait)
                    npz_path = export_raw_to_npz(
                        raw_path,
                        EventsIterator,
                        int(args.npz_delta_t_us),
                    )
                    final_stats["npz_path"] = str(npz_path.resolve())
                    render_report["npz_path"] = str(npz_path.resolve())

                final_stats["postprocess_sec"] = float(
                    time.perf_counter() - processing_started
                )
                summary_path.write_text(
                    json.dumps(final_stats, indent=2),
                    encoding="utf-8",
                )
                status = "OK" if render_report["selected_found_corners"] else "NO CORNERS"
                return {
                    "pose_index": int(final_stats["pose_index"]),
                    "status": status,
                    "total_events": int(final_stats["total_events"]),
                    "image": render_report["selected_calibration_image"],
                    "tested_candidates": len(render_report["candidates"]),
                    "elapsed_sec": float(final_stats["postprocess_sec"]),
                }

            def update_processing_state() -> None:
                nonlocal processing_future, processing_error
                if processing_future is None or not processing_future.done():
                    return
                try:
                    result = processing_future.result()
                    print(
                        f"Pose {result['pose_index']:03d}: {result['status']}, "
                        f"events={result['total_events']}, "
                        f"tested={result['tested_candidates']}, "
                        f"processing={result['elapsed_sec']:.2f}s, "
                        f"image={result['image']}"
                    )
                except BaseException as exc:
                    processing_error = exc
                    print(f"[POSTPROCESS][ERROR] {exc}")
                finally:
                    processing_future = None
                    gc.collect()

            def stop_recording() -> None:
                nonlocal is_recording, current_stats, current_event_chunks, processing_future
                if (
                    not is_recording
                    or current_stats is None
                    or current_summary_path is None
                    or current_raw_path is None
                ):
                    return
                events_stream.stop_log_raw_data()
                current_stats["stopped_at"] = dt.datetime.now().isoformat(timespec="seconds")
                current_summary_path.write_text(json.dumps(current_stats, indent=2), encoding="utf-8")
                is_recording = False
                finished_event_chunks = current_event_chunks
                current_event_chunks = []
                processing_future = processing_executor.submit(
                    process_finished_pose,
                    current_raw_path,
                    current_summary_path,
                    dict(current_stats),
                    finished_event_chunks,
                    max(0.0, float(args.post_stop_wait_sec)),
                )
                print(
                    "RAW stopped; processing in background. Live events are being "
                    "drained and excluded from preview until this pose is ready."
                )

            def start_recording() -> None:
                nonlocal is_recording, pose_index, current_raw_path, current_summary_path
                nonlocal current_stats, current_event_chunks, recording_start_wall
                if is_recording:
                    return
                update_processing_state()
                if processing_error is not None:
                    print(
                        "[POSTPROCESS][LOCK] The previous pose failed during "
                        "export. Inspect the error and restart after resolving it."
                    )
                    return
                if processing_future is not None:
                    print("[WAIT] Previous pose is still being exported; live events are discarded.")
                    return
                pose_index += 1
                raw_path, summary_path = make_paths(out_dir, args.basename, pose_index)
                current_raw_path = raw_path
                current_summary_path = summary_path
                current_stats = new_stats(raw_path, pose_index, int(width), int(height), args)
                current_event_chunks = []
                events_stream.log_raw_data(str(raw_path.resolve()))
                recording_start_wall = time.perf_counter()
                is_recording = True
                print(f"Recording pose {pose_index:03d}: {raw_path}")

            def on_frame(_: int, frame: Any) -> None:
                overlay = frame.copy()
                if is_recording:
                    cv2.putText(overlay, "REC", (20, 42), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 3, cv2.LINE_AA)
                window.show_async(overlay)

            def keyboard_cb(key: Any, scancode: int, action: Any, mods: int) -> None:
                del scancode, mods
                if action != UIAction.PRESS:
                    return
                if key == UIKeyEvent.KEY_ESCAPE or key == UIKeyEvent.KEY_Q:
                    if is_recording:
                        stop_recording()
                    window.set_close_flag()
                    return
                if key == UIKeyEvent.KEY_ENTER or key == UIKeyEvent.KEY_KP_ENTER:
                    if is_recording and args.duration_sec <= 0:
                        stop_recording()
                    elif not is_recording:
                        start_recording()

            frame_generator.set_output_callback(on_frame)
            window.set_keyboard_callback(keyboard_cb)

            try:
                for events in iterator:
                    EventLoop.poll_and_dispatch()
                    update_processing_state()
                    preview_is_suppressed = (
                        processing_future is not None
                        and not bool(args.preview_during_processing)
                    )
                    if not preview_is_suppressed:
                        frame_generator.process_events(events)
                    if is_recording and current_stats is not None:
                        if events.size:
                            current_event_chunks.append(events.copy())
                        update_stats(current_stats, events)
                        now = time.perf_counter()
                        if now - last_rate_wall >= 1.0:
                            recent_events = int(current_stats["total_events"]) - last_rate_events
                            recent_rate = recent_events / max(1e-9, now - last_rate_wall)
                            print(
                                f"Recording pose {current_stats['pose_index']:03d}: "
                                f"{recent_rate:,.0f} events/s, total={current_stats['total_events']:,}"
                            )
                            last_rate_wall = now
                            last_rate_events = int(current_stats["total_events"])
                        if args.duration_sec > 0 and time.perf_counter() - recording_start_wall >= float(args.duration_sec):
                            stop_recording()
                    update_processing_state()
                    if window.should_close():
                        break
            except KeyboardInterrupt:
                print("Interrupted by user. Closing the current RAW cleanly...")
                if is_recording:
                    stop_recording()
                window.set_close_flag()
            except Exception as exc:
                print(f"Event stream stopped with an error: {exc}")
                if is_recording:
                    try:
                        stop_recording()
                    except Exception as stop_exc:
                        print(f"Could not finalize the current RAW after stream error: {stop_exc}")
                raise
    finally:
        if is_recording and device is not None:
            try:
                device.get_i_events_stream().stop_log_raw_data()
            except Exception:
                pass
        try:
            del iterator
        except Exception:
            pass
        try:
            del device
        except Exception:
            pass
        gc.collect()

    manifest = {
        "output_dir": str(out_dir.resolve()),
        "calibration_images": str((out_dir / "calib_images").resolve()),
        "pattern_size": list(PATTERN_SIZE),
        "pose_count": int(pose_index),
        "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
        "checkerboard_image": (
            str(args.checkerboard_image.resolve())
            if args.checkerboard_image is not None
            else ""
        ),
        "checkerboard_sha256": (
            sha256_file(args.checkerboard_image.resolve())
            if args.checkerboard_image is not None
            else ""
        ),
        "square_size_mm": float(args.square_mm),
    }
    manifest_path = out_dir / "capture_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Finished. Calibration images: {out_dir / 'calib_images'}")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
