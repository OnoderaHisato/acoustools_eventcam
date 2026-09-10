#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Capture a calibration RAW, export NPZ, and estimate pixel/mm scale.

This script is intentionally standalone: it does not import the other local
eventcam_*.py files. It only depends on Metavision/OpenEB, numpy, and OpenCV.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gc
import json
import os
import time
from pathlib import Path
from typing import Any, Sequence

import cv2
import numpy as np


_DLL_DIRECTORY_HANDLES: list[Any] = []
_DLL_DIRECTORY_PATHS: set[str] = set()


def prepend_environment_paths(variable_name: str, directories: Sequence[Path]) -> None:
    existing_parts = [part for part in os.environ.get(variable_name, "").split(os.pathsep) if part]
    seen = {os.path.normcase(os.path.abspath(part)) for part in existing_parts}
    new_parts: list[str] = []
    for directory in directories:
        if not directory.exists():
            continue
        resolved = str(directory.resolve())
        key = os.path.normcase(os.path.abspath(resolved))
        if key in seen:
            continue
        new_parts.append(resolved)
        seen.add(key)
    if new_parts:
        os.environ[variable_name] = os.pathsep.join(new_parts + existing_parts)


def configure_local_metavision_environment() -> None:
    project_dir = Path(__file__).resolve().parent
    openeb_install_dir = project_dir / "openeb_install"
    openeb_bin_dir = openeb_install_dir / "bin"
    vcpkg_bin_dir = project_dir / "vcpkg" / "vcpkg_installed" / "x64-windows" / "bin"
    if not vcpkg_bin_dir.exists():
        vcpkg_bin_dir = project_dir / "vcpkg" / "installed" / "x64-windows" / "bin"
    hal_plugin_dir = openeb_install_dir / "lib" / "metavision" / "hal" / "plugins"
    hdf5_plugin_dir = openeb_install_dir / "lib" / "hdf5" / "plugin"
    centuryarks_dir = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "CenturyArks"
    centuryarks_bin_dir = centuryarks_dir / "bin"
    centuryarks_plugin_dir = centuryarks_dir / "plugins"

    dll_dirs = [openeb_bin_dir, vcpkg_bin_dir, hal_plugin_dir, centuryarks_bin_dir, centuryarks_plugin_dir]
    prepend_environment_paths("PATH", dll_dirs)
    prepend_environment_paths("MV_HAL_PLUGIN_PATH", [hal_plugin_dir, centuryarks_plugin_dir])
    prepend_environment_paths("HDF5_PLUGIN_PATH", [hdf5_plugin_dir])
    os.environ.setdefault("PYTHONNOUSERSITE", "true")

    if hasattr(os, "add_dll_directory"):
        for directory in dll_dirs:
            if not directory.exists():
                continue
            resolved = str(directory.resolve())
            if resolved in _DLL_DIRECTORY_PATHS:
                continue
            _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(resolved))
            _DLL_DIRECTORY_PATHS.add(resolved)


def import_metavision() -> tuple[Any, ...]:
    configure_local_metavision_environment()
    try:
        from metavision_core.event_io import EventsIterator
        from metavision_core.event_io.raw_reader import initiate_device
        import metavision_hal
        from metavision_sdk_core import PeriodicFrameGenerationAlgorithm, ColorPalette
        from metavision_sdk_ui import EventLoop, BaseWindow, MTWindow, UIAction, UIKeyEvent
    except Exception as exc:
        raise SystemExit(
            "Metavision Python/UI bindings are not available. "
            "Run .\\activate_metavision_env.ps1 first, or verify openeb_install and vcpkg. "
            f"Original error: {exc}"
        ) from exc
    return (
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
    )


def parse_roi(text: str, name: str = "--roi") -> tuple[int, int, int, int] | None:
    if not text:
        return None
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if len(parts) != 4:
        raise SystemExit(f"{name} must be x0,y0,x1,y1: {text}")
    x0, y0, x1, y1 = [int(part) for part in parts]
    if x1 <= x0 or y1 <= y0:
        raise SystemExit(f"{name} must satisfy x1>x0 and y1>y0: {text}")
    return x0, y0, x1, y1


def make_roi_mask(events: np.ndarray, roi: tuple[int, int, int, int]) -> np.ndarray:
    x0, y0, x1, y1 = roi
    return (events["x"] >= x0) & (events["x"] < x1) & (events["y"] >= y0) & (events["y"] < y1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One-shot event-camera scale calibration: RAW capture -> NPZ -> circle diameter px.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--serial", default="", help="Camera serial/device id. Empty opens the first detected camera.")
    parser.add_argument("--list-devices", action="store_true", help="List detected devices and exit.")
    parser.add_argument("--output-dir", default="scale_calibration_capture", help="Directory for all outputs.")
    parser.add_argument("--basename", default="scale_calib", help="Output filename prefix.")
    parser.add_argument("--diameter-mm", type=float, default=0.0, help="Known physical circle diameter in mm. 0 reports px only.")
    parser.add_argument("--roi", default="", help="Optional ROI x0,y0,x1,y1 around the calibration target.")
    parser.add_argument("--delta-t-us", type=int, default=10_000, help="Live event slice duration during capture.")
    parser.add_argument("--preview-fps", type=float, default=25.0, help="Preview update FPS.")
    parser.add_argument("--preview-accumulation-us", type=int, default=10_000, help="Preview event accumulation time.")
    parser.add_argument("--duration-sec", type=float, default=0.0, help="Automatic recording duration. 0 means Enter starts/stops.")
    parser.add_argument("--post-stop-wait-sec", type=float, default=0.2, help="Small wait after stop_log_raw_data before reading RAW.")
    parser.add_argument("--npz-delta-t-us", type=int, default=1_000, help="RAW read slice duration when exporting NPZ.")
    parser.add_argument("--t-start-sec", type=float, default=0.0, help="Start time in the NPZ/circle fit, relative to RAW first event.")
    parser.add_argument("--fit-duration-sec", type=float, default=0.0, help="Duration used for circle fit. 0 uses all remaining events.")
    parser.add_argument("--polarity", choices=["all", "on", "off"], default="all", help="Which events to use for circle fit.")
    parser.add_argument("--min-radius-px", type=int, default=10, help="Minimum circle radius for Hough fitting.")
    parser.add_argument("--max-radius-px", type=int, default=0, help="Maximum circle radius. 0 lets OpenCV choose.")
    parser.add_argument("--hough-param2", type=float, default=20.0, help="Hough accumulator threshold. Lower detects weaker circles.")
    parser.add_argument("--blur", type=int, default=7, help="Odd Gaussian blur kernel for accumulated event image.")
    parser.add_argument("--point-percentile", type=float, default=99.0, help="Percentile used to scale the saved event image.")
    return parser.parse_args()


def new_stats(raw_path: Path, sensor_width: int, sensor_height: int) -> dict[str, Any]:
    return {
        "raw_path": str(raw_path.resolve()),
        "sensor_size": {"width": int(sensor_width), "height": int(sensor_height)},
        "started_at": dt.datetime.now().isoformat(timespec="seconds"),
        "stopped_at": None,
        "total_slices": 0,
        "total_events": 0,
        "first_event_ts_us": None,
        "last_event_ts_us": None,
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


def open_event_camera(initiate_device: Any, metavision_hal: Any, serial: str, retries: int = 5, delay_sec: float = 1.0) -> Any:
    requested = serial or "(first detected camera)"
    last_error: Exception | None = None
    detected_text = "unavailable"
    for attempt in range(1, retries + 1):
        if serial:
            # A full DeviceDiscovery scan probes every USB camera. When one
            # stereo camera is already streaming, that unnecessary probe can
            # raise LIBUSB_ERROR_ACCESS and interfere with opening its mate.
            # An explicit serial is already the exact path used below, so open
            # it directly and reserve discovery for failure diagnostics.
            candidate_paths = [str(serial)]
        else:
            try:
                detected = list(metavision_hal.DeviceDiscovery.list())
                detected_text = (
                    "none"
                    if not detected
                    else ", ".join(str(item) for item in detected)
                )
            except Exception:
                detected = []
                detected_text = "unavailable"
            candidate_paths = ["", *(str(item) for item in detected)]

        seen: set[str] = set()
        candidate_paths = [path for path in candidate_paths if not (path in seen or seen.add(path))]

        try:
            for candidate_path in candidate_paths:
                try:
                    return initiate_device(path=candidate_path)
                except Exception as exc:
                    last_error = exc
            if serial:
                try:
                    detected = list(metavision_hal.DeviceDiscovery.list())
                    detected_text = (
                        "none"
                        if not detected
                        else ", ".join(str(item) for item in detected)
                    )
                except Exception:
                    detected_text = "unavailable"
            if attempt < retries:
                print(
                    f"[WARN] Could not open camera ({attempt}/{retries}): {last_error}. "
                    f"Requested: {requested}. Detected: {detected_text}"
                )
                gc.collect()
                time.sleep(delay_sec)
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                print(
                    f"[WARN] Could not open camera ({attempt}/{retries}): {exc}. "
                    f"Requested: {requested}. Detected: {detected_text}"
                )
                gc.collect()
                time.sleep(delay_sec)
    raise RuntimeError(
        "Could not open the event camera.\n"
        f"Requested device: {requested}\n"
        f"Detected devices: {detected_text}\n"
        f"Original error: {last_error}"
    ) from last_error


def make_output_stem(out_dir: Path, basename: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return out_dir / f"{basename}_{stamp}"


def capture_raw(args: argparse.Namespace) -> tuple[Path, Path, dict[str, Any]]:
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
    ) = import_metavision()

    if args.list_devices:
        devices = metavision_hal.DeviceDiscovery.list()
        print("Detected devices:")
        if not devices:
            print("  none")
        else:
            for device in devices:
                print(f"  {device}")
        raise SystemExit(0)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = make_output_stem(out_dir, args.basename)
    raw_path = stem.with_suffix(".raw")
    summary_path = stem.with_suffix(".json")

    device = None
    iterator = None
    is_recording = False
    stats: dict[str, Any] | None = None

    try:
        device = open_event_camera(initiate_device, metavision_hal, args.serial)
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
        width = int(width)
        height = int(height)
        stats = new_stats(raw_path, width, height)

        frame_generator = PeriodicFrameGenerationAlgorithm(
            sensor_width=width,
            sensor_height=height,
            accumulation_time_us=int(args.preview_accumulation_us),
            fps=float(args.preview_fps),
            palette=ColorPalette.Dark,
        )

        print("[CALIB] Preview opened.")
        print("[CALIB] Put the known-diameter circle in view.")
        print("[CALIB] Enter: start/stop recording, Esc/Q: quit.")
        if args.duration_sec > 0:
            print(f"[CALIB] Auto-stop duration: {args.duration_sec:.3f} s")

        recording_start_wall = 0.0

        with MTWindow(
            title="Event Camera Scale Calibration - Enter: REC/STOP, Esc/Q: Quit",
            width=width,
            height=height,
            mode=BaseWindow.RenderMode.BGR,
        ) as window:

            def on_frame(_: int, frame: Any) -> None:
                overlay = frame.copy()
                if is_recording:
                    cv2.putText(
                        overlay,
                        "REC",
                        (20, 40),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        1.2,
                        (0, 0, 255),
                        3,
                        cv2.LINE_AA,
                    )
                window.show_async(overlay)

            def stop_recording() -> None:
                nonlocal is_recording, stats
                if not is_recording:
                    return
                events_stream.stop_log_raw_data()
                time.sleep(max(0.0, float(args.post_stop_wait_sec)))
                is_recording = False
                if stats is not None:
                    stats["stopped_at"] = dt.datetime.now().isoformat(timespec="seconds")
                    summary_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
                print(f"[CALIB] RAW stopped: {raw_path}")
                window.set_close_flag()

            def start_recording() -> None:
                nonlocal is_recording, recording_start_wall
                if is_recording:
                    return
                events_stream.log_raw_data(str(raw_path.resolve()))
                recording_start_wall = time.perf_counter()
                is_recording = True
                print(f"[CALIB] RAW started: {raw_path}")

            def keyboard_cb(key: Any, scancode: int, action: Any, mods: int) -> None:
                del scancode, mods
                if action != UIAction.PRESS:
                    return
                if key == UIKeyEvent.KEY_ESCAPE or key == UIKeyEvent.KEY_Q:
                    stop_recording()
                    window.set_close_flag()
                    return
                if key == UIKeyEvent.KEY_ENTER or key == UIKeyEvent.KEY_KP_ENTER:
                    if is_recording:
                        stop_recording()
                    else:
                        start_recording()

            frame_generator.set_output_callback(on_frame)
            window.set_keyboard_callback(keyboard_cb)

            for events in iterator:
                EventLoop.poll_and_dispatch()
                frame_generator.process_events(events)
                if is_recording and stats is not None:
                    update_stats(stats, events)
                    if args.duration_sec > 0 and time.perf_counter() - recording_start_wall >= float(args.duration_sec):
                        stop_recording()
                if window.should_close():
                    break
    finally:
        if is_recording and device is not None:
            try:
                device.get_i_events_stream().stop_log_raw_data()
            except Exception:
                pass
        if stats is not None and stats.get("stopped_at") is None and raw_path.exists():
            stats["stopped_at"] = dt.datetime.now().isoformat(timespec="seconds")
            summary_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
        try:
            del iterator
        except Exception:
            pass
        try:
            del device
        except Exception:
            pass
        gc.collect()

    if not raw_path.exists():
        raise SystemExit("RAW was not recorded. Press Enter once to start and once to stop recording.")
    if stats is None:
        raise SystemExit("Capture stats were not initialized.")
    return raw_path, summary_path, stats


def export_raw_to_npz(raw_path: Path, args: argparse.Namespace) -> Path:
    EventsIterator = import_metavision()[0]
    npz_path = raw_path.with_name(f"{raw_path.stem}_events.npz")
    iterator = EventsIterator(
        input_path=str(raw_path.resolve()),
        mode="delta_t",
        delta_t=max(1, int(args.npz_delta_t_us)),
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
    print(f"[CALIB] NPZ saved: {npz_path} events={all_events.size}")
    return npz_path


def fit_circle_from_npz(npz_path: Path, raw_path: Path, args: argparse.Namespace, sensor_size: dict[str, int]) -> dict[str, Any]:
    data = np.load(npz_path, allow_pickle=False)
    events = data["events"]
    if events.size == 0:
        raise SystemExit("No events were saved into the NPZ.")

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
    if args.fit_duration_sec > 0:
        keep &= t_rel_us < start_us + int(round(float(args.fit_duration_sec) * 1_000_000.0))
    events = events[keep]
    if events.size == 0:
        raise SystemExit("No events remain in the requested fit time window.")

    width = int(sensor_size.get("width") or (int(events["x"].max()) + 1))
    height = int(sensor_size.get("height") or (int(events["y"].max()) + 1))
    width = max(width, int(events["x"].max()) + 1)
    height = max(height, int(events["y"].max()) + 1)

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

    positive = counts[counts > 0]
    scale_value = np.percentile(positive, float(args.point_percentile)) if positive.size else 1.0
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
            "or produce clearer edge events from the circle."
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
        label += f" scale={px_per_mm:.4f}px/mm"
    cv2.putText(vis, label, (20, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(vis, label, (20, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (255, 255, 255), 1, cv2.LINE_AA)

    raw_png = npz_path.with_name(f"{npz_path.stem}_accumulated_events.png")
    overlay_png = npz_path.with_name(f"{npz_path.stem}_circle_fit.png")
    cv2.imwrite(str(raw_png), gray)
    cv2.imwrite(str(overlay_png), vis)

    result = {
        "raw_path": str(raw_path.resolve()),
        "npz_path": str(npz_path.resolve()),
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
    result_path = npz_path.with_name(f"{npz_path.stem}_scale_calibration.json")
    result["result_json"] = str(result_path.resolve())
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    args = parse_args()
    if args.delta_t_us <= 0:
        raise SystemExit("--delta-t-us must be positive.")
    if args.npz_delta_t_us <= 0:
        raise SystemExit("--npz-delta-t-us must be positive.")
    if args.preview_fps <= 0:
        raise SystemExit("--preview-fps must be positive.")

    raw_path, summary_path, stats = capture_raw(args)
    npz_path = export_raw_to_npz(raw_path, args)
    result = fit_circle_from_npz(npz_path, raw_path, args, stats.get("sensor_size", {}))

    payload = dict(stats)
    payload["npz_path"] = str(npz_path.resolve())
    payload["scale_calibration"] = result
    summary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print("[CALIB] Done.")
    print(f"RAW: {raw_path}")
    print(f"NPZ: {npz_path}")
    print(f"Diameter: {result['diameter_px']:.3f} px")
    if result["scale_px_per_mm"] is not None:
        print(f"Scale: {result['scale_px_per_mm']:.6f} px/mm ({result['scale_mm_per_px']:.9f} mm/px)")
    print(f"PNG: {result['raw_png']}")
    print(f"Overlay: {result['overlay_png']}")
    print(f"JSON: {result['result_json']}")


if __name__ == "__main__":
    main()
