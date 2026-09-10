#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Record synchronized-ish left/right event-camera NPZ files for stereo tracking.

This script is intentionally limited to acquisition.  It opens the two cameras
in separate Python processes, waits for a shared PC-clock start time, then saves
one NPZ per side in the same format that ``eventcam_npz_track.py`` can read.

The timestamps are not hardware synchronized.  The manifest stores each
process' PC-clock start/end times so a later alignment step can reason about
the remaining offset.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import multiprocessing as mp
import os
import queue
import time
from pathlib import Path
from typing import Any

import numpy as np
import cv2

import eventcam_npz_storage as npz_storage
import eventcam_scale_calibration_capture as scale_capture


EVENT_DTYPE = np.dtype([("x", "<u2"), ("y", "<u2"), ("p", "u1"), ("t", "<i8")])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record paired left/right event-camera NPZ files for stereo measurement.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--left-serial", default="", help="Left camera serial/device id.")
    parser.add_argument("--right-serial", default="", help="Right camera serial/device id.")
    parser.add_argument("--list-devices", action="store_true", help="List detected devices and exit.")
    parser.add_argument("--output-dir", default="stereo_eventcam_records", help="Root output directory.")
    parser.add_argument("--basename", default="stereo_event", help="Run folder basename.")
    parser.add_argument(
        "--run-dir",
        default="",
        help="Exact run directory. When set, --output-dir/--basename timestamp generation is bypassed.",
    )
    parser.add_argument("--duration-sec", type=float, default=2.0, help="Recording duration.")
    parser.add_argument(
        "--delta-t-us",
        type=int,
        default=10_000,
        help="EventsIterator acquisition slice duration. Event timestamps themselves are unchanged.",
    )
    parser.add_argument(
        "--npz-compression",
        choices=npz_storage.NPZ_COMPRESSION_CHOICES,
        default="none",
        help=(
            "NPZ storage compression. 'none' is much faster and remains compatible "
            "with NumPy/post-processing; 'compressed' reduces disk usage at higher CPU cost."
        ),
    )
    parser.add_argument("--start-delay-sec", type=float, default=2.0, help="Delay before shared PC-clock start.")
    parser.add_argument("--sensor-width", type=int, default=1280, help="Fallback sensor width.")
    parser.add_argument("--sensor-height", type=int, default=720, help="Fallback sensor height.")
    parser.add_argument("--max-events", type=int, default=0, help="Per-side event cap. 0 means no cap.")
    parser.add_argument("--stereo-calibration", default="", help="Optional stereo calibration NPZ path copied into manifest.")
    parser.add_argument("--note", default="", help="Free-form note stored in the manifest.")
    parser.add_argument("--no-preview", action="store_true", help="Skip the initial left/right particle preview.")
    parser.add_argument("--preview-delta-t-us", type=int, default=5_000, help="Event accumulation slice used by the preview workers.")
    parser.add_argument("--preview-scale", type=float, default=0.5, help="Display scale for each preview camera image.")
    parser.add_argument("--preview-point-size", type=int, default=1, help="Preview event point size in pixels.")
    parser.add_argument("--preview-reopen-wait-sec", type=float, default=1.0, help="Wait after closing preview cameras before recording.")
    return parser.parse_args()


def import_metavision() -> tuple[Any, Any, Any]:
    (
        EventsIterator,
        initiate_device,
        metavision_hal,
        *_,
    ) = scale_capture.import_metavision()
    return EventsIterator, initiate_device, metavision_hal


def list_devices() -> list[str]:
    _, _, metavision_hal = import_metavision()
    devices = [str(device) for device in metavision_hal.DeviceDiscovery.list()]
    print("Detected devices:")
    if not devices:
        print("  none")
    for index, device in enumerate(devices):
        print(f"  [{index}] {device}")
    return devices


def resolve_serials(left_serial: str, right_serial: str) -> tuple[str, str]:
    devices = list_devices()
    left = str(left_serial or "")
    right = str(right_serial or "")
    if not left and len(devices) >= 1:
        left = devices[0]
    if not right and len(devices) >= 2:
        right = devices[1]
    if not left or not right:
        raise SystemExit("Two cameras are required. Pass --left-serial and --right-serial.")
    if left == right:
        raise SystemExit(f"Left and right serials are the same: {left}")
    return left, right


def get_sensor_size(device: Any, fallback_width: int, fallback_height: int) -> tuple[int, int]:
    try:
        geometry = device.get_i_geometry()
        if geometry is not None:
            return int(geometry.get_width()), int(geometry.get_height())
    except Exception:
        pass
    return int(fallback_width), int(fallback_height)


def write_empty_npz(path: Path, *, npz_compression: str = "none") -> None:
    npz_storage.save_npz_atomic(
        path,
        compression=npz_compression,
        arrays={
            "events": np.empty(0, dtype=EVENT_DTYPE),
            "sync_ts_us": np.asarray(-1, dtype=np.int64),
            "output_start_ts_us": np.asarray(0, dtype=np.int64),
            "output_end_ts_us": np.asarray(0, dtype=np.int64),
            "mask_rois": np.empty((0, 4), dtype=np.int32),
            "npz_compression": np.asarray(npz_compression),
        },
    )


def render_preview_events(events: np.ndarray, width: int, height: int, point_size: int) -> np.ndarray:
    frame = np.full((height, width, 3), (24, 34, 45), dtype=np.uint8)
    if events.size == 0:
        return frame
    xs = events["x"].astype(np.int32)
    ys = events["y"].astype(np.int32)
    valid = (xs >= 0) & (xs < width) & (ys >= 0) & (ys < height)
    xs = xs[valid]
    ys = ys[valid]
    polarity = events["p"][valid]
    if xs.size == 0:
        return frame
    on = polarity != 0
    frame[ys[~on], xs[~on]] = (150, 150, 150)
    frame[ys[on], xs[on]] = (255, 255, 255)
    if int(point_size) > 1:
        mask = np.zeros((height, width), dtype=np.uint8)
        mask[ys, xs] = 255
        kernel_size = max(1, int(point_size))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
        expanded = cv2.dilate(mask, kernel)
        frame[expanded > 0] = np.maximum(frame[expanded > 0], np.asarray((180, 180, 180), dtype=np.uint8))
    return frame


def preview_worker(
    *,
    side: str,
    serial: str,
    delta_t_us: int,
    fallback_width: int,
    fallback_height: int,
    point_size: int,
    frame_queue: mp.Queue,
    status_queue: mp.Queue,
    stop_event: Any,
) -> None:
    device = None
    try:
        EventsIterator, initiate_device, metavision_hal = import_metavision()
        device = scale_capture.open_event_camera(initiate_device, metavision_hal, serial)
        width, height = get_sensor_size(device, fallback_width, fallback_height)
        status_queue.put({"side": side, "status": "opened", "width": width, "height": height})
        iterator = EventsIterator.from_device(
            device=device,
            mode="delta_t",
            delta_t=max(1, int(delta_t_us)),
            max_duration=None,
            relative_timestamps=False,
        )
        for events in iterator:
            if stop_event.is_set():
                break
            frame = render_preview_events(events, width, height, point_size)
            try:
                frame_queue.put_nowait((side, frame))
            except queue.Full:
                try:
                    frame_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    frame_queue.put_nowait((side, frame))
                except queue.Full:
                    pass
    except Exception as exc:
        try:
            status_queue.put({"side": side, "status": "error", "error": repr(exc)})
        except Exception:
            pass
    finally:
        try:
            status_queue.put({"side": side, "status": "closed"})
        except Exception:
            pass
        try:
            del device
        except Exception:
            pass


def run_preview(
    *,
    left_serial: str,
    right_serial: str,
    sensor_width: int,
    sensor_height: int,
    delta_t_us: int,
    display_scale: float,
    point_size: int,
    reopen_wait_sec: float,
) -> bool:
    if display_scale <= 0:
        raise SystemExit("--preview-scale must be positive.")
    ctx = mp.get_context("spawn")
    frame_queue: mp.Queue = ctx.Queue(maxsize=2)
    status_queue: mp.Queue = ctx.Queue()
    stop_event = ctx.Event()
    workers = [
        ctx.Process(
            target=preview_worker,
            kwargs={
                "side": "left",
                "serial": left_serial,
                "delta_t_us": int(delta_t_us),
                "fallback_width": int(sensor_width),
                "fallback_height": int(sensor_height),
                "point_size": int(point_size),
                "frame_queue": frame_queue,
                "status_queue": status_queue,
                "stop_event": stop_event,
            },
        ),
        ctx.Process(
            target=preview_worker,
            kwargs={
                "side": "right",
                "serial": right_serial,
                "delta_t_us": int(delta_t_us),
                "fallback_width": int(sensor_width),
                "fallback_height": int(sensor_height),
                "point_size": int(point_size),
                "frame_queue": frame_queue,
                "status_queue": status_queue,
                "stop_event": stop_event,
            },
        ),
    ]
    for worker in workers:
        worker.start()

    frames: dict[str, np.ndarray] = {}
    statuses: dict[str, str] = {"left": "opening", "right": "opening"}
    accepted = False
    window_name = "Stereo particle preview"
    print("[PREVIEW] Enter=accept, Q/Esc=abort")
    try:
        while True:
            while True:
                try:
                    side, frame = frame_queue.get_nowait()
                    frames[str(side)] = frame
                except queue.Empty:
                    break
            while True:
                try:
                    status = status_queue.get_nowait()
                    side = str(status.get("side", ""))
                    state = str(status.get("status", ""))
                    if state == "error":
                        statuses[side] = f"ERROR: {status.get('error', '')}"
                    else:
                        statuses[side] = state
                except queue.Empty:
                    break

            image_list: list[np.ndarray] = []
            for side in ("left", "right"):
                frame = frames.get(side)
                if frame is None:
                    frame = np.zeros((sensor_height, sensor_width, 3), dtype=np.uint8)
                if display_scale != 1.0:
                    frame = cv2.resize(frame, None, fx=display_scale, fy=display_scale, interpolation=cv2.INTER_AREA)
                cv2.putText(frame, side.upper(), (18, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 2, cv2.LINE_AA)
                cv2.putText(frame, statuses.get(side, ""), (18, 66), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 220, 80), 1, cv2.LINE_AA)
                image_list.append(frame)
            height = max(frame.shape[0] for frame in image_list)
            width = max(frame.shape[1] for frame in image_list)
            canvas = np.zeros((height + 48, width * 2, 3), dtype=np.uint8)
            for index, frame in enumerate(image_list):
                canvas[48 : 48 + frame.shape[0], index * width : index * width + frame.shape[1]] = frame
            cv2.putText(canvas, "Enter: accept particle setting    Q/Esc: abort", (18, 31), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 1, cv2.LINE_AA)
            cv2.imshow(window_name, canvas)
            key = cv2.waitKey(1) & 0xFF
            if key in (13, 10):
                accepted = True
                break
            if key in (27, ord("q"), ord("Q")):
                break
    finally:
        stop_event.set()
        for worker in workers:
            worker.join(timeout=3.0)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=2.0)
        cv2.destroyWindow(window_name)
        cv2.waitKey(1)
    if accepted and reopen_wait_sec > 0:
        time.sleep(float(reopen_wait_sec))
    return accepted


def record_worker(
    *,
    side: str,
    serial: str,
    output_dir: str,
    start_perf_ns: int,
    duration_sec: float,
    delta_t_us: int,
    sensor_width: int,
    sensor_height: int,
    max_events: int,
    result_queue: mp.Queue,
    npz_compression: str = "none",
) -> None:
    side_dir = Path(output_dir)
    side_dir.mkdir(parents=True, exist_ok=True)
    npz_path = side_dir / f"{side}_events.npz"
    meta_path = side_dir / f"{side}_recording_meta.json"

    meta: dict[str, Any] = {
        "side": side,
        "serial": serial,
        "npz_path": str(npz_path.resolve()),
        "started_at": None,
        "stopped_at": None,
        "pc_start_perf_ns": None,
        "pc_end_perf_ns": None,
        "pc_start_wall_ns": None,
        "pc_end_wall_ns": None,
        "sensor_size": {"width": int(sensor_width), "height": int(sensor_height)},
        "duration_sec": float(duration_sec),
        "delta_t_us": int(delta_t_us),
        "total_slices": 0,
        "total_events": 0,
        "first_event_ts_us": None,
        "last_event_ts_us": None,
        "max_events": int(max_events),
        "npz_compression": str(npz_compression),
        "capture_loop_sec": None,
        "concatenate_sec": None,
        "npz_save_sec": None,
        "npz_size_bytes": None,
        "error": "",
    }

    device = None
    try:
        EventsIterator, initiate_device, metavision_hal = import_metavision()
        device = scale_capture.open_event_camera(initiate_device, metavision_hal, serial)
        width, height = get_sensor_size(device, sensor_width, sensor_height)
        meta["sensor_size"] = {"width": int(width), "height": int(height)}

        iterator = EventsIterator.from_device(
            device=device,
            mode="delta_t",
            delta_t=max(1, int(delta_t_us)),
            max_duration=None,
            relative_timestamps=False,
        )

        chunks: list[np.ndarray] = []
        first_ts: int | None = None
        last_ts: int | None = None
        total_events = 0
        pc_start_perf_ns: int | None = None
        pc_start_wall_ns: int | None = None
        stop_perf_ns: int | None = None
        capture_loop_started = time.perf_counter()
        for events in iterator:
            now_ns = time.perf_counter_ns()
            meta["total_slices"] = int(meta["total_slices"]) + 1
            if now_ns < int(start_perf_ns):
                continue
            if pc_start_perf_ns is None:
                pc_start_perf_ns = now_ns
                pc_start_wall_ns = time.time_ns()
                stop_perf_ns = now_ns + int(float(duration_sec) * 1_000_000_000)
                meta["started_at"] = dt.datetime.now().isoformat(timespec="milliseconds")
                meta["pc_start_perf_ns"] = int(pc_start_perf_ns)
                meta["pc_start_wall_ns"] = int(pc_start_wall_ns)
            if events.size:
                if max_events > 0 and total_events + int(events.size) > max_events:
                    events = events[: max(0, int(max_events) - total_events)]
                copied = events.copy()
                chunks.append(copied)
                total_events += int(copied.size)
                if copied.size:
                    if first_ts is None:
                        first_ts = int(copied["t"][0])
                    last_ts = int(copied["t"][-1])
            if (stop_perf_ns is not None and now_ns >= stop_perf_ns) or (
                max_events > 0 and total_events >= max_events
            ):
                break

        meta["capture_loop_sec"] = float(time.perf_counter() - capture_loop_started)
        if pc_start_perf_ns is None or pc_start_wall_ns is None:
            raise RuntimeError(f"{side}: capture never reached its PC-clock start target")
        pc_end_perf_ns = time.perf_counter_ns()
        pc_end_wall_ns = time.time_ns()
        all_events, concatenate_sec = npz_storage.concatenate_event_chunks(
            chunks,
            dtype=EVENT_DTYPE,
        )
        meta["concatenate_sec"] = float(concatenate_sec)
        if all_events.size:
            output_start_ts_us = int(first_ts if first_ts is not None else all_events["t"][0])
            output_end_ts_us = int((last_ts if last_ts is not None else all_events["t"][-1]) + 1)
        else:
            output_start_ts_us = 0
            output_end_ts_us = 0

        save_stats = npz_storage.save_npz_atomic(
            npz_path,
            compression=npz_compression,
            arrays={
                "events": all_events,
                "sync_ts_us": np.asarray(-1, dtype=np.int64),
                "output_start_ts_us": np.asarray(output_start_ts_us, dtype=np.int64),
                "output_end_ts_us": np.asarray(output_end_ts_us, dtype=np.int64),
                "mask_rois": np.empty((0, 4), dtype=np.int32),
                "pc_start_perf_ns": np.asarray(pc_start_perf_ns, dtype=np.int64),
                "pc_end_perf_ns": np.asarray(pc_end_perf_ns, dtype=np.int64),
                "pc_start_wall_ns": np.asarray(pc_start_wall_ns, dtype=np.int64),
                "pc_end_wall_ns": np.asarray(pc_end_wall_ns, dtype=np.int64),
                "camera_serial": np.asarray(serial),
                "side": np.asarray(side),
                "npz_compression": np.asarray(npz_compression),
            },
        )
        meta.update(save_stats)
        meta["stopped_at"] = dt.datetime.now().isoformat(timespec="milliseconds")
        meta["pc_end_perf_ns"] = int(pc_end_perf_ns)
        meta["pc_end_wall_ns"] = int(pc_end_wall_ns)
        meta["total_events"] = int(all_events.size)
        meta["first_event_ts_us"] = None if first_ts is None else int(first_ts)
        meta["last_event_ts_us"] = None if last_ts is None else int(last_ts)
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        result_queue.put({"side": side, "ok": True, "meta": meta})
    except Exception as exc:
        meta["error"] = repr(exc)
        meta["stopped_at"] = dt.datetime.now().isoformat(timespec="milliseconds")
        try:
            write_empty_npz(npz_path, npz_compression=npz_compression)
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        except Exception:
            pass
        result_queue.put({"side": side, "ok": False, "meta": meta})
    finally:
        try:
            del device
        except Exception:
            pass


def make_run_dir(root: Path, basename: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = root / f"{basename}_{stamp}"
    if not candidate.exists():
        return candidate
    for index in range(2, 1000):
        suffixed = root / f"{basename}_{stamp}_{index:03d}"
        if not suffixed.exists():
            return suffixed
    raise SystemExit("Could not create a unique run directory.")


def main() -> int:
    args = parse_args()
    if args.list_devices:
        list_devices()
        return 0
    if args.duration_sec <= 0:
        raise SystemExit("--duration-sec must be positive.")
    if args.delta_t_us <= 0:
        raise SystemExit("--delta-t-us must be positive.")
    if args.start_delay_sec < 0:
        raise SystemExit("--start-delay-sec must be non-negative.")
    if args.max_events < 0:
        raise SystemExit("--max-events must be non-negative.")

    left_serial, right_serial = resolve_serials(args.left_serial, args.right_serial)
    if not args.no_preview:
        accepted = run_preview(
            left_serial=left_serial,
            right_serial=right_serial,
            sensor_width=int(args.sensor_width),
            sensor_height=int(args.sensor_height),
            delta_t_us=int(args.preview_delta_t_us),
            display_scale=float(args.preview_scale),
            point_size=int(args.preview_point_size),
            reopen_wait_sec=float(args.preview_reopen_wait_sec),
        )
        if not accepted:
            print("[PREVIEW] aborted; no recording was started.")
            return 1

    if str(args.run_dir).strip():
        run_dir = Path(args.run_dir).resolve()
        if run_dir.exists() and any(run_dir.iterdir()):
            raise SystemExit(f"--run-dir already exists and is not empty: {run_dir}")
    else:
        root = Path(args.output_dir)
        run_dir = make_run_dir(root, args.basename)
    left_dir = run_dir / "left"
    right_dir = run_dir / "right"
    run_dir.mkdir(parents=True, exist_ok=True)

    start_perf_ns = time.perf_counter_ns() + int(float(args.start_delay_sec) * 1_000_000_000)
    start_wall_ns = time.time_ns() + int(float(args.start_delay_sec) * 1_000_000_000)

    ctx = mp.get_context("spawn")
    result_queue: mp.Queue = ctx.Queue()
    workers = [
        ctx.Process(
            target=record_worker,
            kwargs={
                "side": "left",
                "serial": left_serial,
                "output_dir": str(left_dir),
                "start_perf_ns": int(start_perf_ns),
                "duration_sec": float(args.duration_sec),
                "delta_t_us": int(args.delta_t_us),
                "sensor_width": int(args.sensor_width),
                "sensor_height": int(args.sensor_height),
                "max_events": int(args.max_events),
                "result_queue": result_queue,
                "npz_compression": str(args.npz_compression),
            },
        ),
        ctx.Process(
            target=record_worker,
            kwargs={
                "side": "right",
                "serial": right_serial,
                "output_dir": str(right_dir),
                "start_perf_ns": int(start_perf_ns),
                "duration_sec": float(args.duration_sec),
                "delta_t_us": int(args.delta_t_us),
                "sensor_width": int(args.sensor_width),
                "sensor_height": int(args.sensor_height),
                "max_events": int(args.max_events),
                "result_queue": result_queue,
                "npz_compression": str(args.npz_compression),
            },
        ),
    ]

    print(f"Output: {run_dir}")
    print(f"Left camera:  {left_serial}")
    print(f"Right camera: {right_serial}")
    print(f"Starting both workers in about {args.start_delay_sec:.3f} s")
    for worker in workers:
        worker.start()

    results: list[dict[str, Any]] = []
    deadline = time.monotonic() + float(args.duration_sec) + float(args.start_delay_sec) + 30.0
    while len(results) < 2 and time.monotonic() < deadline:
        try:
            results.append(result_queue.get(timeout=0.5))
        except queue.Empty:
            pass

    for worker in workers:
        worker.join(timeout=5.0)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=2.0)

    by_side = {str(result.get("side")): result for result in results}
    manifest = {
        "run_dir": str(run_dir.resolve()),
        "created_at": dt.datetime.now().isoformat(timespec="milliseconds"),
        "left_serial": left_serial,
        "right_serial": right_serial,
        "left_npz": str((left_dir / "left_events.npz").resolve()),
        "right_npz": str((right_dir / "right_events.npz").resolve()),
        "stereo_calibration": str(Path(args.stereo_calibration).resolve()) if args.stereo_calibration else "",
        "note": str(args.note),
        "requested": {
            "duration_sec": float(args.duration_sec),
            "delta_t_us": int(args.delta_t_us),
            "start_delay_sec": float(args.start_delay_sec),
            "sensor_width": int(args.sensor_width),
            "sensor_height": int(args.sensor_height),
            "max_events": int(args.max_events),
            "npz_compression": str(args.npz_compression),
        },
        "preview": {
            "enabled": bool(not args.no_preview),
            "delta_t_us": int(args.preview_delta_t_us),
            "scale": float(args.preview_scale),
            "point_size": int(args.preview_point_size),
        },
        "shared_start": {
            "pc_start_perf_ns_target": int(start_perf_ns),
            "pc_start_wall_ns_approx": int(start_wall_ns),
        },
        "left": by_side.get("left", {}),
        "right": by_side.get("right", {}),
        "sync_note": "PC-clock start only; event timestamps are not hardware synchronized.",
    }
    (run_dir / "stereo_recording_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    ok = all(bool(by_side.get(side, {}).get("ok")) for side in ("left", "right"))
    for side in ("left", "right"):
        result = by_side.get(side)
        if not result:
            print(f"[{side.upper()}][FAIL] no result from worker")
            continue
        meta = result.get("meta", {})
        status = "OK" if result.get("ok") else "FAIL"
        print(f"[{side.upper()}][{status}] events={meta.get('total_events')} npz={meta.get('npz_path')}")
        if result.get("ok"):
            print(
                f"[{side.upper()}][TIMING] capture={float(meta.get('capture_loop_sec') or 0):.3f}s "
                f"concat={float(meta.get('concatenate_sec') or 0):.3f}s "
                f"save={float(meta.get('npz_save_sec') or 0):.3f}s "
                f"compression={meta.get('npz_compression')}"
            )
        if meta.get("error"):
            print(f"[{side.upper()}][ERROR] {meta.get('error')}")

    print(f"Manifest: {run_dir / 'stereo_recording_manifest.json'}")
    return 0 if ok else 1


if __name__ == "__main__":
    if os.name == "nt":
        mp.freeze_support()
    raise SystemExit(main())
