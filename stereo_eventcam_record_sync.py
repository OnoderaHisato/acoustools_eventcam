#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Record left/right event-camera NPZ files for synchronized stereo tracking.

The two cameras are opened in separate Python processes. In hardware-sync mode,
the Slave stream is started first, then the Master stream. The Master publishes
one future timestamp in the shared camera-clock domain and both workers save
exactly the same half-open timestamp interval. This preserves hardware time
alignment even if the first visible event occurs at a different time in each
camera.

Pass ``--hw-sync left-master`` / ``--hw-sync right-master`` to use the
Master/Slave sync cable (SilkyEvCam/EVK4-HD IX connector, SCSS-xm cable) via
the Metavision HAL ``I_CameraSynchronization`` facility
(``device.get_i_camera_synchronization()`` -> ``set_mode_master()`` /
``set_mode_slave()``). Per Prophesee's synchronization guide, the *slave*
camera must be configured and streaming before the *master* starts
transmitting its clock. ``--hw-sync off`` remains available as an explicitly
unsynchronized fallback and uses only a shared PC-clock start target.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gc
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
    parser.add_argument(
        "--start-delay-sec",
        type=float,
        default=2.0,
        help=(
            "Warm-up before the saved interval. In hardware-sync mode this is "
            "measured in the shared camera clock; with --hw-sync off it is a PC-clock delay."
        ),
    )
    parser.add_argument(
        "--hw-sync",
        choices=["off", "left-master", "right-master"],
        default="left-master",
        help=(
            "Hardware Master/Slave sync via the sync cable and "
            "I_CameraSynchronization. 'off' keeps PC-clock-only soft sync. "
            "'left-master' makes the left camera Master (SYNC_OUT) and the "
            "right camera Slave (SYNC_IN); 'right-master' is the reverse."
        ),
    )
    parser.add_argument(
        "--hw-sync-timeout-sec",
        type=float,
        default=10.0,
        help="Max seconds the Master worker waits for the Slave to finish configuring before giving up.",
    )
    parser.add_argument("--sensor-width", type=int, default=1280, help="Fallback sensor width.")
    parser.add_argument("--sensor-height", type=int, default=720, help="Fallback sensor height.")
    parser.add_argument("--max-events", type=int, default=0, help="Per-side event cap. 0 means no cap.")
    parser.add_argument("--stereo-calibration", default="", help="Optional stereo calibration NPZ path copied into manifest.")
    parser.add_argument("--note", default="", help="Free-form note stored in the manifest.")
    parser.add_argument(
        "--capture-start-marker",
        default="",
        help=(
            "Optional JSON path atomically written by the hardware-sync Master "
            "when the common saved interval becomes active. This is intended "
            "for an external PAT controller waiting to start motion."
        ),
    )
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


def align_up(value: int, quantum: int) -> int:
    quantum = max(1, int(quantum))
    return ((int(value) + quantum - 1) // quantum) * quantum


def filter_events_to_window(events: np.ndarray, start_ts_us: int, end_ts_us: int) -> np.ndarray:
    """Returns events in the common half-open interval [start, end)."""
    if events.size == 0:
        return events
    keep = (events["t"] >= int(start_ts_us)) & (events["t"] < int(end_ts_us))
    return events[keep]


def sync_mode_name(mode: Any) -> str:
    name = getattr(mode, "name", None)
    if name:
        return str(name).lower()
    text = str(mode).strip().lower()
    return text.rsplit(".", 1)[-1]


def set_and_verify_sync_mode(*, side: str, i_sync: Any, sync_role: str) -> tuple[str, bool, str]:
    setter = getattr(i_sync, f"set_mode_{sync_role}", None)
    if setter is None:
        raise RuntimeError(f"{side}: synchronization facility has no set_mode_{sync_role}()")
    if not bool(setter()):
        raise RuntimeError(f"{side}: failed to set camera synchronization mode to {sync_role}")
    try:
        applied = sync_mode_name(i_sync.get_mode())
    except TypeError as exc:
        # Some vendor builds expose get_mode() but do not register SyncMode in
        # pybind11, making its C++ enum impossible to convert to Python. The
        # official bool return from set_mode_* remains authoritative.
        return sync_role, False, f"setter_return_only; get_mode unavailable: {exc}"
    if applied != sync_role:
        raise RuntimeError(
            f"{side}: requested synchronization mode {sync_role!r}, "
            f"but the camera reports {applied!r}"
        )
    return applied, True, "setter_return_and_get_mode_readback"


def wait_for_event_or_abort(
    event: Any,
    abort_event: Any,
    *,
    timeout_sec: float,
    description: str,
) -> None:
    deadline = time.monotonic() + float(timeout_sec)
    while not event.wait(timeout=0.1):
        if abort_event is not None and abort_event.is_set():
            raise RuntimeError(f"Aborted while waiting for {description}")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out after {timeout_sec:.1f}s waiting for {description}")


def read_shared_int(shared_value: Any) -> int:
    with shared_value.get_lock():
        return int(shared_value.value)


def publish_shared_int(shared_value: Any, value: int) -> None:
    with shared_value.get_lock():
        if int(shared_value.value) >= 0:
            raise RuntimeError("Shared capture timestamp was already published")
        shared_value.value = int(value)


def write_empty_npz(
    path: Path,
    *,
    side: str = "",
    serial: str = "",
    sync_role: str = "unknown",
    timestamp_domain_id: str = "",
    npz_compression: str = "none",
) -> None:
    npz_storage.save_npz_atomic(
        path,
        compression=npz_compression,
        arrays={
            "events": np.empty(0, dtype=EVENT_DTYPE),
            "sync_ts_us": np.asarray(-1, dtype=np.int64),
            "output_start_ts_us": np.asarray(0, dtype=np.int64),
            "output_end_ts_us": np.asarray(0, dtype=np.int64),
            "mask_rois": np.empty((0, 4), dtype=np.int32),
            "camera_serial": np.asarray(serial),
            "side": np.asarray(side),
            "sync_role": np.asarray(sync_role),
            "sync_mode_applied": np.asarray(""),
            "sync_mode_verified": np.asarray(False, dtype=np.bool_),
            "sync_mode_readback_verified": np.asarray(False, dtype=np.bool_),
            "sync_mode_verification": np.asarray("capture_failed"),
            "hardware_synchronized": np.asarray(False, dtype=np.bool_),
            "timestamp_domain_id": np.asarray(timestamp_domain_id),
            "capture_interval_complete": np.asarray(False, dtype=np.bool_),
            "event_limit_reached": np.asarray(False, dtype=np.bool_),
            "npz_compression": np.asarray(npz_compression),
        },
    )


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, path)


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
    ready_event: Any = None,
) -> None:
    device = None
    try:
        EventsIterator, initiate_device, metavision_hal = import_metavision()
        device = scale_capture.open_event_camera(initiate_device, metavision_hal, serial)
        width, height = get_sensor_size(device, fallback_width, fallback_height)
        status_queue.put({"side": side, "status": "opened", "width": width, "height": height})
        if ready_event is not None:
            ready_event.set()
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
        gc.collect()


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
    window_name: str = "Stereo particle preview",
    instruction_text: str = (
        "Enter: accept    R: discard backlog/refresh    Q/Esc: abort"
    ),
) -> bool:
    if display_scale <= 0:
        raise SystemExit("--preview-scale must be positive.")
    ctx = mp.get_context("spawn")
    accepted = False
    aborted = False
    print(f"[PREVIEW] {instruction_text}")

    opening_height = max(1, int(round(sensor_height * display_scale)))
    opening_width = max(1, int(round(sensor_width * display_scale * 2)))

    def show_message(message: str) -> None:
        canvas = np.zeros(
            (opening_height + 48, opening_width, 3),
            dtype=np.uint8,
        )
        cv2.putText(
            canvas,
            message,
            (18, 31),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
        cv2.imshow(window_name, canvas)
        cv2.waitKey(1)

    def stop_workers(stop_event: Any, started_workers: list[Any]) -> None:
        stop_event.set()
        for worker in started_workers:
            worker.join(timeout=3.0)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=2.0)

    try:
        # Make the GUI visible before opening either camera. On Windows an
        # OpenCV window may otherwise be created behind the PowerShell console.
        cv2.namedWindow(window_name, cv2.WINDOW_AUTOSIZE)
        show_message("Opening LEFT camera...")
        try:
            cv2.setWindowProperty(window_name, cv2.WND_PROP_TOPMOST, 1)
        except cv2.error:
            pass

        refresh_count = 0
        while not accepted and not aborted:
            frame_queue: mp.Queue = ctx.Queue(maxsize=2)
            status_queue: mp.Queue = ctx.Queue()
            stop_event = ctx.Event()
            ready_events = [ctx.Event(), ctx.Event()]
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
                        "ready_event": ready_events[0],
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
                        "ready_event": ready_events[1],
                    },
                ),
            ]
            frames: dict[str, np.ndarray] = {}
            statuses: dict[str, str] = {"left": "opening", "right": "waiting"}
            started_workers: list[Any] = []
            refresh_requested = False

            try:
                # Simultaneous DeviceDiscovery calls can race in the
                # CenturyArks/LibUSB plugin. Open LEFT first, then RIGHT once
                # LEFT owns its device. Both run concurrently after startup.
                for index, worker in enumerate(workers):
                    side = "left" if index == 0 else "right"
                    statuses[side] = "opening"
                    show_message(f"Opening {side.upper()} camera...")
                    worker.start()
                    started_workers.append(worker)
                    startup_deadline = time.monotonic() + 7.0
                    while (
                        worker.is_alive()
                        and not ready_events[index].is_set()
                        and time.monotonic() < startup_deadline
                    ):
                        key = cv2.waitKey(10) & 0xFF
                        if key in (27, ord("q"), ord("Q")):
                            aborted = True
                            break
                    if aborted:
                        break
                    if ready_events[index].is_set():
                        print(f"[PREVIEW][{side.upper()}] camera opened")
                    else:
                        print(
                            f"[PREVIEW][{side.upper()}][WARN] camera did not "
                            "open; the preview will show its error status."
                        )

                while not aborted:
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
                                statuses[side] = (
                                    f"ERROR: {status.get('error', '')}"
                                )
                            elif (
                                state == "closed"
                                and statuses.get(side, "").startswith("ERROR")
                            ):
                                pass
                            else:
                                statuses[side] = state
                        except queue.Empty:
                            break

                    image_list: list[np.ndarray] = []
                    for side in ("left", "right"):
                        frame = frames.get(side)
                        if frame is None:
                            frame = np.zeros(
                                (sensor_height, sensor_width, 3),
                                dtype=np.uint8,
                            )
                        if display_scale != 1.0:
                            frame = cv2.resize(
                                frame,
                                None,
                                fx=display_scale,
                                fy=display_scale,
                                interpolation=cv2.INTER_AREA,
                            )
                        cv2.putText(
                            frame,
                            side.upper(),
                            (18, 34),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.9,
                            (0, 255, 255),
                            2,
                            cv2.LINE_AA,
                        )
                        cv2.putText(
                            frame,
                            statuses.get(side, ""),
                            (18, 66),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.45,
                            (255, 220, 80),
                            1,
                            cv2.LINE_AA,
                        )
                        image_list.append(frame)
                    height = max(frame.shape[0] for frame in image_list)
                    width = max(frame.shape[1] for frame in image_list)
                    canvas = np.zeros(
                        (height + 48, width * 2, 3),
                        dtype=np.uint8,
                    )
                    for index, frame in enumerate(image_list):
                        canvas[
                            48 : 48 + frame.shape[0],
                            index * width : index * width + frame.shape[1],
                        ] = frame
                    cv2.putText(
                        canvas,
                        instruction_text,
                        (18, 31),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        (255, 255, 255),
                        1,
                        cv2.LINE_AA,
                    )
                    cv2.imshow(window_name, canvas)
                    key = cv2.waitKey(1) & 0xFF
                    if key in (ord("r"), ord("R")):
                        refresh_requested = True
                        refresh_count += 1
                        print(
                            "[PREVIEW][REFRESH] Discarding queued events and "
                            f"reopening both streams (refresh={refresh_count})."
                        )
                        show_message(
                            "Discarding buffered events; reopening cameras..."
                        )
                        break
                    if key in (13, 10):
                        unhealthy = [
                            side
                            for side in ("left", "right")
                            if side not in frames
                            or statuses.get(side, "").startswith("ERROR")
                            or statuses.get(side) == "closed"
                        ]
                        if unhealthy:
                            print(
                                "[PREVIEW] Cannot accept until both camera "
                                "streams have produced frames; "
                                f"unavailable={unhealthy}"
                            )
                            continue
                        accepted = True
                        break
                    if key in (27, ord("q"), ord("Q")):
                        aborted = True
                        break
                    try:
                        if cv2.getWindowProperty(
                            window_name,
                            cv2.WND_PROP_VISIBLE,
                        ) < 1:
                            aborted = True
                            break
                    except cv2.error:
                        aborted = True
                        break
            finally:
                stop_workers(stop_event, started_workers)
                try:
                    frame_queue.close()
                    status_queue.close()
                except (AttributeError, ValueError):
                    pass

            if refresh_requested and not accepted and not aborted:
                # Closing the worker processes releases the SDK iterator and
                # USB stream. A new pair of queues and workers then begins at
                # the current camera time instead of consuming old buffers.
                if reopen_wait_sec > 0:
                    time.sleep(float(reopen_wait_sec))
                continue
            break
    finally:
        try:
            cv2.destroyWindow(window_name)
            cv2.waitKey(1)
        except cv2.error:
            pass
    if accepted and reopen_wait_sec > 0:
        time.sleep(float(reopen_wait_sec))
    return accepted


def apply_hw_sync_mode(
    *,
    side: str,
    device: Any,
    sync_role: str,
    hw_sync_ready_event: Any,
    abort_event: Any,
    hw_sync_timeout_sec: float,
) -> tuple[str, bool, str]:
    """Configure I_CameraSynchronization before the EventsIterator is created.

    Per Prophesee's synchronization guide the Slave must be configured (and
    start listening) before the Master starts transmitting its clock, so the
    Slave signals ``hw_sync_ready_event`` after its EventsIterator has started
    the stream, and the Master blocks on it here before calling set_mode_master().
    """
    i_sync = device.get_i_camera_synchronization()
    if i_sync is None:
        if sync_role == "standalone":
            return "standalone", False, "facility_unavailable_in_standalone_mode"
        raise RuntimeError(
            f"{side}: camera does not expose I_CameraSynchronization; "
            "this device/plugin does not support hardware Master/Slave sync."
        )

    if sync_role == "standalone":
        return set_and_verify_sync_mode(side=side, i_sync=i_sync, sync_role="standalone")
    if sync_role == "slave":
        return set_and_verify_sync_mode(side=side, i_sync=i_sync, sync_role="slave")
    if sync_role == "master":
        if hw_sync_ready_event is not None:
            wait_for_event_or_abort(
                hw_sync_ready_event,
                abort_event,
                timeout_sec=float(hw_sync_timeout_sec),
                description="the Slave camera stream to start",
            )
        return set_and_verify_sync_mode(side=side, i_sync=i_sync, sync_role="master")
    raise RuntimeError(f"{side}: unknown sync_role={sync_role!r}")


def record_worker(
    *,
    side: str,
    serial: str,
    output_dir: str,
    start_perf_ns: int,
    start_delay_sec: float,
    duration_sec: float,
    delta_t_us: int,
    sensor_width: int,
    sensor_height: int,
    max_events: int,
    result_queue: mp.Queue,
    sync_role: str = "standalone",
    hw_sync_ready_event: Any = None,
    capture_ts_ready_event: Any = None,
    shared_capture_start_ts_us: Any = None,
    abort_event: Any = None,
    hw_sync_timeout_sec: float = 10.0,
    timestamp_domain_id: str = "",
    npz_compression: str = "none",
    capture_start_marker: str = "",
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
        "sync_role": sync_role,
        "sync_mode_applied": None,
        "sync_mode_verified": False,
        "sync_mode_readback_verified": False,
        "sync_mode_verification": None,
        "hardware_synchronized": bool(sync_role in {"master", "slave"}),
        "timestamp_domain_id": str(timestamp_domain_id),
        "capture_start_ts_us": None,
        "capture_end_ts_us": None,
        "capture_window_source": None,
        "capture_interval_complete": False,
        "event_limit_reached": False,
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

        sync_mode_applied, sync_mode_readback_verified, sync_mode_verification = apply_hw_sync_mode(
            side=side,
            device=device,
            sync_role=sync_role,
            hw_sync_ready_event=hw_sync_ready_event,
            abort_event=abort_event,
            hw_sync_timeout_sec=hw_sync_timeout_sec,
        )
        meta["sync_mode_applied"] = sync_mode_applied
        meta["sync_mode_verified"] = True
        meta["sync_mode_readback_verified"] = bool(sync_mode_readback_verified)
        meta["sync_mode_verification"] = str(sync_mode_verification)

        iterator = EventsIterator.from_device(
            device=device,
            mode="delta_t",
            delta_t=max(1, int(delta_t_us)),
            max_duration=None,
            relative_timestamps=False,
        )

        if sync_mode_applied == "slave" and hw_sync_ready_event is not None:
            hw_sync_ready_event.set()

        hardware_synchronized = sync_mode_applied in {"master", "slave"}
        duration_us = max(1, int(round(float(duration_sec) * 1_000_000)))
        start_delay_us = max(0, int(round(float(start_delay_sec) * 1_000_000)))
        capture_start_ts_us: int | None = None
        capture_end_ts_us: int | None = None
        pc_start_perf_ns: int | None = None
        pc_start_wall_ns: int | None = None
        stop_perf_ns: int | None = None
        stream_started_perf_ns = time.perf_counter_ns()
        chunks: list[np.ndarray] = []
        first_ts: int | None = None
        last_ts: int | None = None
        total_events = 0
        capture_complete = False
        capture_loop_started = time.perf_counter()
        for events in iterator:
            now_ns = time.perf_counter_ns()
            meta["total_slices"] = int(meta["total_slices"]) + 1

            if abort_event is not None and abort_event.is_set():
                raise RuntimeError(f"{side}: paired capture was aborted")

            if hardware_synchronized:
                if sync_mode_applied == "master" and capture_ts_ready_event is not None:
                    if not capture_ts_ready_event.is_set():
                        current_ts_us = int(iterator.get_current_time())
                        min_lead_us = max(start_delay_us, 10 * max(1, int(delta_t_us)))
                        common_start = align_up(current_ts_us + min_lead_us, max(1, int(delta_t_us)))
                        publish_shared_int(shared_capture_start_ts_us, common_start)
                        capture_ts_ready_event.set()

                if capture_ts_ready_event is None or shared_capture_start_ts_us is None:
                    raise RuntimeError(f"{side}: missing shared hardware-sync capture state")
                if not capture_ts_ready_event.is_set():
                    if now_ns - stream_started_perf_ns > int(float(hw_sync_timeout_sec) * 1_000_000_000):
                        raise RuntimeError(f"{side}: timed out waiting for the common camera timestamp")
                    continue

                if capture_start_ts_us is None:
                    capture_start_ts_us = read_shared_int(shared_capture_start_ts_us)
                    if capture_start_ts_us < 0:
                        raise RuntimeError(f"{side}: invalid shared capture timestamp {capture_start_ts_us}")
                    capture_end_ts_us = capture_start_ts_us + duration_us
                    meta["capture_start_ts_us"] = int(capture_start_ts_us)
                    meta["capture_end_ts_us"] = int(capture_end_ts_us)
                    meta["capture_window_source"] = "shared_master_camera_clock"

                assert capture_end_ts_us is not None
                selected = filter_events_to_window(events, capture_start_ts_us, capture_end_ts_us)
                if pc_start_perf_ns is None and int(iterator.get_current_time()) >= capture_start_ts_us:
                    camera_ts_at_marker_us = int(iterator.get_current_time())
                    pc_start_perf_ns = now_ns
                    pc_start_wall_ns = time.time_ns()
                    meta["started_at"] = dt.datetime.now().isoformat(timespec="milliseconds")
                    meta["pc_start_perf_ns"] = int(pc_start_perf_ns)
                    meta["pc_start_wall_ns"] = int(pc_start_wall_ns)
                    if sync_mode_applied == "master" and str(capture_start_marker).strip():
                        atomic_write_json(
                            Path(capture_start_marker).resolve(),
                            {
                                "schema_version": 1,
                                "side": side,
                                "serial": serial,
                                "sync_role": sync_mode_applied,
                                "hardware_synchronized": True,
                                "timestamp_domain_id": str(timestamp_domain_id),
                                "capture_start_ts_us": int(capture_start_ts_us),
                                "capture_end_ts_us": int(capture_end_ts_us),
                                "camera_ts_at_marker_us": camera_ts_at_marker_us,
                                "pc_start_perf_ns": int(pc_start_perf_ns),
                                "pc_start_wall_ns": int(pc_start_wall_ns),
                            },
                        )
                capture_complete = int(iterator.get_current_time()) >= capture_end_ts_us
            else:
                if now_ns < int(start_perf_ns):
                    continue
                if capture_start_ts_us is None:
                    capture_start_ts_us = int(events["t"][0]) if events.size else int(iterator.get_current_time())
                    capture_end_ts_us = None
                    pc_start_perf_ns = now_ns
                    pc_start_wall_ns = time.time_ns()
                    stop_perf_ns = now_ns + int(float(duration_sec) * 1_000_000_000)
                    meta["started_at"] = dt.datetime.now().isoformat(timespec="milliseconds")
                    meta["pc_start_perf_ns"] = int(pc_start_perf_ns)
                    meta["pc_start_wall_ns"] = int(pc_start_wall_ns)
                    meta["capture_start_ts_us"] = int(capture_start_ts_us)
                    meta["capture_window_source"] = "local_camera_clock_at_shared_pc_gate"
                selected = events
                capture_complete = bool(stop_perf_ns is not None and now_ns >= stop_perf_ns)

            if selected.size:
                if max_events > 0 and total_events + int(selected.size) > max_events:
                    selected = selected[: max(0, int(max_events) - total_events)]
                copied = selected.copy()
                chunks.append(copied)
                total_events += int(copied.size)
                if copied.size:
                    if first_ts is None:
                        first_ts = int(copied["t"][0])
                    last_ts = int(copied["t"][-1])
            event_limit_reached = bool(max_events > 0 and total_events >= max_events)
            if event_limit_reached:
                meta["event_limit_reached"] = True
            if capture_complete or event_limit_reached:
                break

        meta["capture_loop_sec"] = float(time.perf_counter() - capture_loop_started)
        pc_end_perf_ns = time.perf_counter_ns()
        pc_end_wall_ns = time.time_ns()
        if not capture_complete and not bool(meta["event_limit_reached"]):
            raise RuntimeError(f"{side}: event stream ended before the requested capture interval completed")
        meta["capture_interval_complete"] = bool(capture_complete)
        if capture_start_ts_us is None:
            raise RuntimeError(f"{side}: capture never reached its start condition")
        if hardware_synchronized:
            assert capture_end_ts_us is not None
            output_start_ts_us = int(capture_start_ts_us)
            output_end_ts_us = int(capture_end_ts_us)
            sync_ts_us = int(capture_start_ts_us)
        else:
            output_start_ts_us = int(capture_start_ts_us)
            output_end_ts_us = int((last_ts + 1) if last_ts is not None else capture_start_ts_us)
            sync_ts_us = -1
            meta["capture_end_ts_us"] = int(output_end_ts_us)
        all_events, concatenate_sec = npz_storage.concatenate_event_chunks(
            chunks,
            dtype=EVENT_DTYPE,
        )
        meta["concatenate_sec"] = float(concatenate_sec)

        save_stats = npz_storage.save_npz_atomic(
            npz_path,
            compression=npz_compression,
            arrays={
                "events": all_events,
                "sync_ts_us": np.asarray(sync_ts_us, dtype=np.int64),
                "output_start_ts_us": np.asarray(output_start_ts_us, dtype=np.int64),
                "output_end_ts_us": np.asarray(output_end_ts_us, dtype=np.int64),
                "mask_rois": np.empty((0, 4), dtype=np.int32),
                "pc_start_perf_ns": np.asarray(pc_start_perf_ns, dtype=np.int64),
                "pc_end_perf_ns": np.asarray(pc_end_perf_ns, dtype=np.int64),
                "pc_start_wall_ns": np.asarray(pc_start_wall_ns, dtype=np.int64),
                "pc_end_wall_ns": np.asarray(pc_end_wall_ns, dtype=np.int64),
                "camera_serial": np.asarray(serial),
                "side": np.asarray(side),
                "sync_role": np.asarray(sync_role),
                "sync_mode_applied": np.asarray(sync_mode_applied),
                "sync_mode_verified": np.asarray(True, dtype=np.bool_),
                "sync_mode_readback_verified": np.asarray(bool(sync_mode_readback_verified), dtype=np.bool_),
                "sync_mode_verification": np.asarray(str(sync_mode_verification)),
                "hardware_synchronized": np.asarray(hardware_synchronized, dtype=np.bool_),
                "timestamp_domain_id": np.asarray(timestamp_domain_id),
                "capture_window_source": np.asarray(str(meta["capture_window_source"])),
                "capture_interval_complete": np.asarray(bool(meta["capture_interval_complete"]), dtype=np.bool_),
                "event_limit_reached": np.asarray(bool(meta["event_limit_reached"]), dtype=np.bool_),
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
        if abort_event is not None:
            abort_event.set()
        meta["error"] = repr(exc)
        meta["stopped_at"] = dt.datetime.now().isoformat(timespec="milliseconds")
        try:
            write_empty_npz(
                npz_path,
                side=side,
                serial=serial,
                sync_role=sync_role,
                timestamp_domain_id=timestamp_domain_id,
                npz_compression=npz_compression,
            )
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
    if args.duration_sec <= 0:
        raise SystemExit("--duration-sec must be positive.")
    if args.delta_t_us <= 0:
        raise SystemExit("--delta-t-us must be positive.")
    if args.start_delay_sec < 0:
        raise SystemExit("--start-delay-sec must be non-negative.")
    if args.hw_sync_timeout_sec <= 0:
        raise SystemExit("--hw-sync-timeout-sec must be positive.")
    if args.max_events < 0:
        raise SystemExit("--max-events must be non-negative.")
    if args.list_devices:
        list_devices()
        return 0

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

    sync_roles = {"left": "standalone", "right": "standalone"}
    if args.hw_sync == "left-master":
        sync_roles = {"left": "master", "right": "slave"}
    elif args.hw_sync == "right-master":
        sync_roles = {"left": "slave", "right": "master"}

    ctx = mp.get_context("spawn")
    result_queue: mp.Queue = ctx.Queue()
    hw_sync_ready_event = ctx.Event()
    capture_ts_ready_event = ctx.Event()
    shared_capture_start_ts_us = ctx.Value("q", -1)
    abort_event = ctx.Event()
    master_side = "left" if args.hw_sync == "left-master" else "right"
    master_serial = left_serial if master_side == "left" else right_serial
    timestamp_domain_id = (
        f"hardware-master:{master_serial}" if args.hw_sync != "off" else "unsynchronized"
    )
    workers = [
        ctx.Process(
            target=record_worker,
            kwargs={
                "side": "left",
                "serial": left_serial,
                "output_dir": str(left_dir),
                "start_perf_ns": int(start_perf_ns),
                "start_delay_sec": float(args.start_delay_sec),
                "duration_sec": float(args.duration_sec),
                "delta_t_us": int(args.delta_t_us),
                "sensor_width": int(args.sensor_width),
                "sensor_height": int(args.sensor_height),
                "max_events": int(args.max_events),
                "result_queue": result_queue,
                "sync_role": sync_roles["left"],
                "hw_sync_ready_event": hw_sync_ready_event,
                "capture_ts_ready_event": capture_ts_ready_event,
                "shared_capture_start_ts_us": shared_capture_start_ts_us,
                "abort_event": abort_event,
                "hw_sync_timeout_sec": float(args.hw_sync_timeout_sec),
                "timestamp_domain_id": timestamp_domain_id if args.hw_sync != "off" else f"local:{left_serial}",
                "npz_compression": str(args.npz_compression),
                "capture_start_marker": str(args.capture_start_marker),
            },
        ),
        ctx.Process(
            target=record_worker,
            kwargs={
                "side": "right",
                "serial": right_serial,
                "output_dir": str(right_dir),
                "start_perf_ns": int(start_perf_ns),
                "start_delay_sec": float(args.start_delay_sec),
                "duration_sec": float(args.duration_sec),
                "delta_t_us": int(args.delta_t_us),
                "sensor_width": int(args.sensor_width),
                "sensor_height": int(args.sensor_height),
                "max_events": int(args.max_events),
                "result_queue": result_queue,
                "sync_role": sync_roles["right"],
                "hw_sync_ready_event": hw_sync_ready_event,
                "capture_ts_ready_event": capture_ts_ready_event,
                "shared_capture_start_ts_us": shared_capture_start_ts_us,
                "abort_event": abort_event,
                "hw_sync_timeout_sec": float(args.hw_sync_timeout_sec),
                "timestamp_domain_id": timestamp_domain_id if args.hw_sync != "off" else f"local:{right_serial}",
                "npz_compression": str(args.npz_compression),
                "capture_start_marker": str(args.capture_start_marker),
            },
        ),
    ]

    print(f"Output: {run_dir}")
    print(f"Left camera:  {left_serial} (sync={sync_roles['left']})")
    print(f"Right camera: {right_serial} (sync={sync_roles['right']})")
    if args.hw_sync == "off":
        print(f"Starting the saved interval in about {args.start_delay_sec:.3f} s (PC clock; unsynchronized)")
    else:
        print(f"Hardware-sync warm-up before saved interval: {args.start_delay_sec:.3f} s")
    started_worker_indices: list[int] = []
    startup_error = ""
    if args.hw_sync == "off":
        for index, worker in enumerate(workers):
            worker.start()
            started_worker_indices.append(index)
    else:
        slave_index = 0 if sync_roles["left"] == "slave" else 1
        master_index = 1 - slave_index
        workers[slave_index].start()
        started_worker_indices.append(slave_index)
        try:
            wait_for_event_or_abort(
                hw_sync_ready_event,
                abort_event,
                timeout_sec=float(args.hw_sync_timeout_sec),
                description="the Slave camera stream to start",
            )
        except RuntimeError as exc:
            startup_error = str(exc)
            abort_event.set()
        else:
            workers[master_index].start()
            started_worker_indices.append(master_index)

    results: list[dict[str, Any]] = []
    deadline = time.monotonic() + float(args.duration_sec) + float(args.start_delay_sec) + 30.0
    while len(results) < len(started_worker_indices) and time.monotonic() < deadline:
        try:
            result = result_queue.get(timeout=0.5)
            results.append(result)
            if not bool(result.get("ok")):
                abort_event.set()
                break
        except queue.Empty:
            pass

    timed_out_indices: set[int] = set()
    for index in started_worker_indices:
        worker = workers[index]
        worker.join(timeout=5.0)
        if worker.is_alive():
            timed_out_indices.add(index)
            abort_event.set()
            worker.join(timeout=1.0)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=2.0)

    while True:
        try:
            results.append(result_queue.get_nowait())
        except queue.Empty:
            break

    side_specs = [
        ("left", left_serial, left_dir, sync_roles["left"]),
        ("right", right_serial, right_dir, sync_roles["right"]),
    ]
    returned_sides = {str(result.get("side")) for result in results}
    for index, (side, serial, side_dir, sync_role) in enumerate(side_specs):
        if side in returned_sides:
            continue
        if index not in started_worker_indices:
            error = startup_error or "Worker was not started because paired-camera setup failed."
        elif index in timed_out_indices:
            if sync_role == "slave":
                error = (
                    "Slave worker timed out while waiting for synchronized camera data. "
                    "Check that the sync cable connects Master SYNC_OUT to Slave SYNC_IN "
                    "and that --hw-sync selects the intended Master."
                )
            else:
                error = "Camera worker timed out before completing or saving the requested capture interval."
        else:
            error = f"Camera worker exited without returning a result (exitcode={workers[index].exitcode})."
        side_dir.mkdir(parents=True, exist_ok=True)
        npz_path = side_dir / f"{side}_events.npz"
        write_empty_npz(
            npz_path,
            side=side,
            serial=serial,
            sync_role=sync_role,
            timestamp_domain_id=timestamp_domain_id,
            npz_compression=str(args.npz_compression),
        )
        meta = {
            "side": side,
            "serial": serial,
            "npz_path": str(npz_path.resolve()),
            "sync_role": sync_role,
            "sync_mode_applied": None,
            "sync_mode_verified": False,
            "hardware_synchronized": False,
            "timestamp_domain_id": timestamp_domain_id,
            "total_events": 0,
            "npz_compression": str(args.npz_compression),
            "error": error,
        }
        (side_dir / f"{side}_recording_meta.json").write_text(
            json.dumps(meta, indent=2),
            encoding="utf-8",
        )
        results.append({"side": side, "ok": False, "meta": meta})

    by_side = {str(result.get("side")): result for result in results}
    workers_ok = all(bool(by_side.get(side, {}).get("ok")) for side in ("left", "right"))
    pair_errors: list[str] = []
    if args.hw_sync != "off" and not workers_ok:
        for side in ("left", "right"):
            result = by_side.get(side, {})
            if not bool(result.get("ok")):
                error = result.get("meta", {}).get("error", "unknown worker failure")
                pair_errors.append(f"{side} capture failed: {error}")
    if args.hw_sync != "off" and workers_ok:
        left_meta = by_side["left"]["meta"]
        right_meta = by_side["right"]["meta"]
        for key in ("capture_start_ts_us", "capture_end_ts_us", "timestamp_domain_id"):
            if left_meta.get(key) != right_meta.get(key):
                pair_errors.append(
                    f"{key} mismatch: left={left_meta.get(key)!r}, right={right_meta.get(key)!r}"
                )
        if {left_meta.get("sync_mode_applied"), right_meta.get("sync_mode_applied")} != {"master", "slave"}:
            pair_errors.append("the verified camera modes are not one Master and one Slave")
        if not all(bool(meta.get("sync_mode_verified")) for meta in (left_meta, right_meta)):
            pair_errors.append("one or both synchronization modes were not verified")
        if any(bool(meta.get("event_limit_reached")) for meta in (left_meta, right_meta)):
            pair_errors.append(
                "--max-events truncated at least one camera before the shared capture interval completed"
            )
        if not all(bool(meta.get("capture_interval_complete")) for meta in (left_meta, right_meta)):
            pair_errors.append("one or both cameras did not complete the shared capture interval")

    pair_sync_verified = bool(args.hw_sync != "off" and workers_ok and not pair_errors)
    if args.hw_sync == "off":
        sync_note = "PC-clock start only; event timestamps are not hardware synchronized."
    elif pair_sync_verified:
        common_start = read_shared_int(shared_capture_start_ts_us)
        common_end = common_start + int(round(float(args.duration_sec) * 1_000_000))
        sync_note = (
            f"Hardware synchronization verified (master={master_side}, "
            f"slave={'right' if master_side == 'left' else 'left'}). Both NPZ files use "
            f"the same camera-clock interval [{common_start}, {common_end}) us."
        )
    else:
        sync_note = "Hardware synchronization was requested but could not be verified."
    published_common_start = read_shared_int(shared_capture_start_ts_us)
    common_camera_window = None
    if published_common_start >= 0:
        common_camera_window = {
            "start_ts_us": int(published_common_start),
            "end_ts_us": int(
                published_common_start + round(float(args.duration_sec) * 1_000_000)
            ),
            "interval": "[start_ts_us, end_ts_us)",
        }
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
        "hw_sync": {
            "mode": args.hw_sync,
            "roles": dict(sync_roles),
            "timeout_sec": float(args.hw_sync_timeout_sec),
            "verified": pair_sync_verified,
            "timestamp_domain_id": timestamp_domain_id,
            "pair_validation_errors": pair_errors,
        },
        "preview": {
            "enabled": bool(not args.no_preview),
            "delta_t_us": int(args.preview_delta_t_us),
            "scale": float(args.preview_scale),
            "point_size": int(args.preview_point_size),
        },
        "shared_start": {
            "pc_start_perf_ns_target": int(start_perf_ns) if args.hw_sync == "off" else None,
            "pc_start_wall_ns_approx": int(start_wall_ns) if args.hw_sync == "off" else None,
            "common_camera_window": common_camera_window,
        },
        "left": by_side.get("left", {}),
        "right": by_side.get("right", {}),
        "sync_note": sync_note,
    }
    (run_dir / "stereo_recording_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    ok = workers_ok
    if args.hw_sync != "off":
        ok = bool(ok and pair_sync_verified)
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
    for error in pair_errors:
        print(f"[PAIR][ERROR] {error}")

    print(f"Manifest: {run_dir / 'stereo_recording_manifest.json'}")
    return 0 if ok else 1


if __name__ == "__main__":
    if os.name == "nt":
        mp.freeze_support()
    raise SystemExit(main())
