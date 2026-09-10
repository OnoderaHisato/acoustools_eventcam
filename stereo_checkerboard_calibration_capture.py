#!/usr/bin/env python3
"""Capture paired checkerboard images for stereo event-camera calibration.

For each pose, this script records the left camera and then the right camera
without changing the pose index.  The rendered images are saved as matching
``pose_###.png`` files under ``left/calib_images`` and ``right/calib_images``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gc
import json
import tempfile
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

import eventcam_checkerboard_calibration_capture as mono_capture
import eventcam_scale_calibration_capture as scale_capture


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sequential left/right checkerboard capture for stereo calibration.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--left-serial", default="", help="Left camera serial/device id. Empty auto-selects first detected camera.")
    parser.add_argument("--right-serial", default="", help="Right camera serial/device id. Empty auto-selects second detected camera.")
    parser.add_argument("--list-devices", action="store_true", help="List detected devices and exit.")
    parser.add_argument("--output-dir", default="stereo_checkerboard_blink5", help="Stereo capture output directory.")
    parser.add_argument("--pose-count", type=int, default=0, help="Number of poses to capture. 0 means interactive until q.")
    parser.add_argument("--preview", action="store_true", help="Show a preview window for each camera before recording.")
    parser.add_argument("--dual-preview", action="store_true", help="Show left and right preview windows together before each pose capture.")
    parser.add_argument(
        "--dual-preview-mode",
        choices=["snapshot", "live"],
        default="snapshot",
        help="Dual preview implementation. snapshot opens cameras one at a time; live opens both together.",
    )
    parser.add_argument("--preview-fps", type=float, default=25.0, help="Preview update FPS when --preview is set.")
    parser.add_argument("--snapshot-sec", type=float, default=0.02, help="Per-camera event duration for snapshot dual preview.")
    parser.add_argument(
        "--snapshot-backend",
        choices=["live-slice", "raw-log"],
        default="live-slice",
        help="How dual-preview snapshots are acquired. live-slice reads a few decoded event slices; raw-log records a tiny RAW first.",
    )
    parser.add_argument("--snapshot-delta-t-us", type=int, default=5_000, help="Event slice duration used only for snapshot dual preview.")
    parser.add_argument("--snapshot-slices", type=int, default=2, help="Maximum decoded event slices per camera snapshot in live-slice mode.")
    parser.add_argument("--snapshot-max-events", type=int, default=200_000, help="Maximum events used per camera snapshot preview. 0 means no cap.")
    parser.add_argument("--camera-reopen-wait-sec", type=float, default=0.5, help="Wait after closing a camera before opening the next one.")
    parser.add_argument("--preview-max-width", type=int, default=1600, help="Maximum width of the combined dual-preview window.")
    parser.add_argument("--preview-max-height", type=int, default=900, help="Maximum height of the combined dual-preview window.")
    parser.add_argument("--duration-sec", type=float, default=0.6, help="Recording duration per camera per pose.")
    parser.add_argument(
        "--capture-backend",
        choices=["raw-log", "live-decode"],
        default="live-decode",
        help="raw-log records RAW without live Python decoding, then decodes after closing the camera.",
    )
    parser.add_argument("--sensor-width", type=int, default=1280, help="Fallback sensor width if geometry is unavailable.")
    parser.add_argument("--sensor-height", type=int, default=720, help="Fallback sensor height if geometry is unavailable.")
    parser.add_argument("--delta-t-us", type=int, default=10_000, help="Live event slice duration during capture.")
    parser.add_argument("--post-stop-wait-sec", type=float, default=0.2, help="Wait after stopping RAW logging.")
    parser.add_argument("--npz-delta-t-us", type=int, default=1_000, help="RAW read slice duration for NPZ export.")
    parser.add_argument("--preview-accumulation-us", type=int, default=10_000, help="Stored in metadata for consistency.")
    parser.add_argument("--frame-window-us", type=int, default=20_000, help="Accumulation window for rendered PNGs.")
    parser.add_argument("--frame-window-us-list", default="", help="Comma-separated accumulation windows to try.")
    parser.add_argument("--candidate-count", type=int, default=1, help="Candidate windows per pose.")
    parser.add_argument("--render-mode", choices=["signed", "abs", "on", "off", "all"], default="off", help="Render mode.")
    parser.add_argument("--scale-percentile", type=float, default=99.5, help="Contrast scaling percentile.")
    parser.add_argument("--min-events", type=int, default=200, help="Minimum events in a candidate window.")
    return parser.parse_args()


def list_devices(metavision_hal: Any) -> list[str]:
    devices = [str(device) for device in metavision_hal.DeviceDiscovery.list()]
    print("Detected devices:")
    if not devices:
        print("  none")
    for index, device in enumerate(devices):
        print(f"  [{index}] {device}")
    return devices


def resolve_serials(args: argparse.Namespace, metavision_hal: Any) -> tuple[str, str]:
    devices = [str(device) for device in metavision_hal.DeviceDiscovery.list()]
    left = str(args.left_serial or "")
    right = str(args.right_serial or "")
    if not left and len(devices) >= 1:
        left = devices[0]
    if not right and len(devices) >= 2:
        right = devices[1]
    if not left or not right:
        raise SystemExit(
            "Two cameras are required. Use --list-devices, then pass "
            "--left-serial and --right-serial explicitly."
        )
    if left == right:
        raise SystemExit(f"Left and right serials are the same: {left}")
    return left, right


def make_side_raw_paths(side_dir: Path, side: str, pose_index: int) -> tuple[Path, Path]:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = side_dir / "raw" / f"stereo_{side}_pose_{pose_index:03d}_{stamp}"
    return stem.with_suffix(".raw"), stem.with_suffix(".json")


def get_sensor_size_from_device(device: Any, args: argparse.Namespace) -> tuple[int, int]:
    try:
        geometry = device.get_i_geometry()
        if geometry is not None:
            return int(geometry.get_width()), int(geometry.get_height())
    except Exception:
        pass
    return int(args.sensor_width), int(args.sensor_height)


def max_existing_stereo_pose_index(out_dir: Path) -> int:
    return max(
        mono_capture.max_existing_pose_index(out_dir / "left"),
        mono_capture.max_existing_pose_index(out_dir / "right"),
    )


def capture_one_side(
    *,
    side: str,
    serial: str,
    pose_index: int,
    side_dir: Path,
    args: argparse.Namespace,
    EventsIterator: Any,
    initiate_device: Any,
    metavision_hal: Any,
    PeriodicFrameGenerationAlgorithm: Any,
    ColorPalette: Any,
    EventLoop: Any,
    BaseWindow: Any,
    MTWindow: Any,
    UIAction: Any,
    UIKeyEvent: Any,
) -> dict[str, Any]:
    raw_path, summary_path = make_side_raw_paths(side_dir, side, pose_index)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    device = None
    iterator = None
    is_recording = False
    stats: dict[str, Any] | None = None
    try:
        print(f"[{side.upper()}] opening camera: {serial}")
        device = scale_capture.open_event_camera(initiate_device, metavision_hal, serial)
        events_stream = device.get_i_events_stream()
        if events_stream is None:
            raise RuntimeError("This event camera does not expose I_EventsStream; RAW logging is unavailable.")

        if args.capture_backend == "raw-log":
            width, height = get_sensor_size_from_device(device, args)
            stats = mono_capture.new_stats(raw_path, pose_index, int(width), int(height), args)
            stats["stereo_side"] = side
            stats["camera_serial"] = serial
            stats["summary_path"] = str(summary_path.resolve())
            stats["capture_backend"] = "raw-log"
            stats["started_at"] = dt.datetime.now().isoformat(timespec="seconds")
            events_stream.log_raw_data(str(raw_path.resolve()))
            is_recording = True
            print(f"[{side.upper()}] RAW logging pose {pose_index:03d} for {args.duration_sec:.3f} s")
            time.sleep(float(args.duration_sec))
            events_stream.stop_log_raw_data()
            time.sleep(max(0.0, float(args.post_stop_wait_sec)))
            is_recording = False
            stats["stopped_at"] = dt.datetime.now().isoformat(timespec="seconds")
            summary_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")

            del device
            device = None
            gc.collect()
            time.sleep(max(0.0, float(args.camera_reopen_wait_sec)))

            npz_path = mono_capture.export_raw_to_npz(raw_path, EventsIterator, int(args.npz_delta_t_us))
            try:
                with np.load(npz_path, allow_pickle=False) as npz_data:
                    events_for_stats = npz_data["events"]
                    stats["total_events"] = int(events_for_stats.size)
                    if stats["total_events"]:
                        stats["first_event_ts_us"] = int(events_for_stats["t"][0])
                        stats["last_event_ts_us"] = int(events_for_stats["t"][-1])
            except Exception as exc:
                stats["npz_event_count_error"] = str(exc)
            render_report = mono_capture.write_pose_images(
                npz_path,
                pose_index,
                side_dir,
                args,
                {"width": int(width), "height": int(height)},
            )
            stats["npz_path"] = str(npz_path.resolve())
            stats["render_report"] = render_report
            summary_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
            status = "OK" if render_report["selected_found_corners"] else "NO CORNERS"
            print(
                f"[{side.upper()}] pose {pose_index:03d}: {status}, "
                f"events={stats.get('total_events')}, image={render_report['selected_calibration_image']}"
            )
            return stats

        iterator = EventsIterator.from_device(
            device=device,
            mode="delta_t",
            delta_t=int(args.delta_t_us),
            max_duration=None,
            relative_timestamps=False,
        )
        height, width = iterator.get_size()
        stats = mono_capture.new_stats(raw_path, pose_index, int(width), int(height), args)
        stats["stereo_side"] = side
        stats["camera_serial"] = serial
        stats["summary_path"] = str(summary_path.resolve())

        if args.preview:
            frame_generator = PeriodicFrameGenerationAlgorithm(
                sensor_width=int(width),
                sensor_height=int(height),
                accumulation_time_us=int(args.preview_accumulation_us),
                fps=float(args.preview_fps),
                palette=ColorPalette.Dark,
            )
            started = False
            aborted = False
            start_wall = 0.0
            print(f"[{side.upper()}] preview pose {pose_index:03d}. Enter=start recording, Esc/Q=abort.")

            with MTWindow(
                title=f"Stereo {side.upper()} preview - Enter: REC, Esc/Q: Abort",
                width=int(width),
                height=int(height),
                mode=BaseWindow.RenderMode.BGR,
            ) as window:

                def on_frame(_: int, frame: Any) -> None:
                    window.show_async(frame)

                def start_recording() -> None:
                    nonlocal started, start_wall, is_recording
                    if started:
                        return
                    stats["started_at"] = dt.datetime.now().isoformat(timespec="seconds")
                    events_stream.log_raw_data(str(raw_path.resolve()))
                    started = True
                    is_recording = True
                    start_wall = time.perf_counter()
                    print(f"[{side.upper()}] recording pose {pose_index:03d} for {args.duration_sec:.3f} s")

                def keyboard_cb(key: Any, scancode: int, action: Any, mods: int) -> None:
                    nonlocal aborted
                    del scancode, mods
                    if action != UIAction.PRESS:
                        return
                    if key == UIKeyEvent.KEY_ESCAPE or key == UIKeyEvent.KEY_Q:
                        aborted = True
                        window.set_close_flag()
                        return
                    if key == UIKeyEvent.KEY_ENTER or key == UIKeyEvent.KEY_KP_ENTER:
                        start_recording()

                frame_generator.set_output_callback(on_frame)
                window.set_keyboard_callback(keyboard_cb)

                for events in iterator:
                    EventLoop.poll_and_dispatch()
                    frame_generator.process_events(events)
                    if started:
                        mono_capture.update_stats(stats, events)
                        if time.perf_counter() - start_wall >= float(args.duration_sec):
                            break
                    if window.should_close():
                        break

            if aborted and not started:
                raise SystemExit(f"{side} capture aborted before recording.")
            if not started:
                raise SystemExit(f"{side} preview closed before recording started.")
        else:
            stats["started_at"] = dt.datetime.now().isoformat(timespec="seconds")
            events_stream.log_raw_data(str(raw_path.resolve()))
            is_recording = True
            start_wall = time.perf_counter()
            print(f"[{side.upper()}] recording pose {pose_index:03d} for {args.duration_sec:.3f} s")

            for events in iterator:
                mono_capture.update_stats(stats, events)
                if time.perf_counter() - start_wall >= float(args.duration_sec):
                    break

        events_stream.stop_log_raw_data()
        time.sleep(max(0.0, float(args.post_stop_wait_sec)))
        is_recording = False
        stats["stopped_at"] = dt.datetime.now().isoformat(timespec="seconds")
        summary_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")

        npz_path = mono_capture.export_raw_to_npz(raw_path, EventsIterator, int(args.npz_delta_t_us))
        render_report = mono_capture.write_pose_images(
            npz_path,
            pose_index,
            side_dir,
            args,
            {"width": int(width), "height": int(height)},
        )
        stats["npz_path"] = str(npz_path.resolve())
        stats["render_report"] = render_report
        summary_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")

        status = "OK" if render_report["selected_found_corners"] else "NO CORNERS"
        print(
            f"[{side.upper()}] pose {pose_index:03d}: {status}, "
            f"events={stats['total_events']:,}, image={render_report['selected_calibration_image']}"
        )
        return stats
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
        time.sleep(max(0.0, float(args.camera_reopen_wait_sec)))


def should_continue_interactive(pose_index: int) -> bool:
    answer = input(f"\nSet checkerboard pose {pose_index:03d}, then press Enter. Type q + Enter to finish: ").strip()
    return answer.lower() not in {"q", "quit", "exit"}


def accumulate_preview_image(
    *,
    serial: str,
    args: argparse.Namespace,
    EventsIterator: Any,
    initiate_device: Any,
    metavision_hal: Any,
) -> np.ndarray:
    if args.snapshot_backend == "raw-log":
        with tempfile.TemporaryDirectory(prefix="stereo_preview_") as tmp:
            tmp_dir = Path(tmp)
            raw_path = tmp_dir / "preview.raw"
            device = None
            is_recording = False
            try:
                device = scale_capture.open_event_camera(initiate_device, metavision_hal, serial)
                width, height = get_sensor_size_from_device(device, args)
                events_stream = device.get_i_events_stream()
                if events_stream is None:
                    raise RuntimeError("This event camera does not expose I_EventsStream; RAW logging is unavailable.")
                events_stream.log_raw_data(str(raw_path.resolve()))
                is_recording = True
                time.sleep(max(0.001, float(args.snapshot_sec)))
                events_stream.stop_log_raw_data()
                time.sleep(max(0.0, float(args.post_stop_wait_sec)))
                is_recording = False
            finally:
                if is_recording and device is not None:
                    try:
                        device.get_i_events_stream().stop_log_raw_data()
                    except Exception:
                        pass
                try:
                    del device
                except Exception:
                    pass
                gc.collect()
                time.sleep(max(0.0, float(args.camera_reopen_wait_sec)))

            npz_path = mono_capture.export_raw_to_npz(raw_path, EventsIterator, int(args.npz_delta_t_us))
            with np.load(npz_path, allow_pickle=False) as data:
                events = data["events"].copy()
            if int(args.snapshot_max_events) > 0 and events.size > int(args.snapshot_max_events):
                events = events[: int(args.snapshot_max_events)]
            mode = "off" if str(args.render_mode) == "all" else str(args.render_mode)
            gray = mono_capture.render_events(events, int(width), int(height), mode, float(args.scale_percentile))
            return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

    device = None
    iterator = None
    try:
        device = scale_capture.open_event_camera(initiate_device, metavision_hal, serial)
        iterator = EventsIterator.from_device(
            device=device,
            mode="delta_t",
            delta_t=max(1, int(args.snapshot_delta_t_us)),
            max_duration=None,
            relative_timestamps=False,
        )
        height, width = iterator.get_size()
        mode = str(args.render_mode)
        if mode == "all":
            mode = "off"
        if mode == "signed":
            accum = np.zeros((int(height), int(width)), dtype=np.float32)
        else:
            accum = np.zeros((int(height), int(width)), dtype=np.float32)

        start = time.perf_counter()
        total_events = 0
        total_slices = 0
        for events in iterator:
            total_slices += 1
            total_events += int(events.size)
            if events.size:
                xs = events["x"].astype(np.int32)
                ys = events["y"].astype(np.int32)
                valid = (xs >= 0) & (xs < int(width)) & (ys >= 0) & (ys < int(height))
                xs = xs[valid]
                ys = ys[valid]
                ps = events["p"][valid]
                if mode == "signed":
                    vals = np.where(ps != 0, 1.0, -1.0).astype(np.float32)
                    np.add.at(accum, (ys, xs), vals)
                elif mode == "on":
                    keep = ps != 0
                    np.add.at(accum, (ys[keep], xs[keep]), 1.0)
                elif mode == "off":
                    keep = ps == 0
                    np.add.at(accum, (ys[keep], xs[keep]), 1.0)
                else:
                    np.add.at(accum, (ys, xs), 1.0)
            if time.perf_counter() - start >= float(args.snapshot_sec):
                break
            if int(args.snapshot_max_events) > 0 and total_events >= int(args.snapshot_max_events):
                break
            if int(args.snapshot_slices) > 0 and total_slices >= int(args.snapshot_slices):
                break

        if mode == "signed":
            positive = np.abs(accum[accum != 0])
            scale = np.percentile(positive, float(args.scale_percentile)) if positive.size else 1.0
            gray = np.clip(127.0 + accum / max(float(scale), 1.0) * 127.0, 0, 255).astype(np.uint8)
        else:
            positive = accum[accum > 0]
            scale = np.percentile(positive, float(args.scale_percentile)) if positive.size else 1.0
            gray = np.clip(accum / max(float(scale), 1.0) * 255.0, 0, 255).astype(np.uint8)
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    finally:
        try:
            del iterator
        except Exception:
            pass
        try:
            del device
        except Exception:
            pass
        gc.collect()
        time.sleep(max(0.0, float(args.camera_reopen_wait_sec)))


def make_preview_error_image(label: str, message: str, width: int = 640, height: int = 360) -> np.ndarray:
    image = np.zeros((height, width, 3), dtype=np.uint8)
    cv2.putText(image, label, (24, 48), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(image, "PREVIEW ERROR", (24, 98), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2, cv2.LINE_AA)
    words = str(message).replace("\n", " ").split()
    line = ""
    y = 140
    for word in words:
        trial = f"{line} {word}".strip()
        if len(trial) > 58:
            cv2.putText(image, line, (24, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (220, 220, 220), 1, cv2.LINE_AA)
            y += 28
            line = word
            if y > height - 28:
                break
        else:
            line = trial
    if line and y <= height - 28:
        cv2.putText(image, line, (24, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (220, 220, 220), 1, cv2.LINE_AA)
    return image


def fit_image_to_bounds(image: np.ndarray, max_width: int, max_height: int) -> np.ndarray:
    max_width = max(320, int(max_width))
    max_height = max(240, int(max_height))
    scale = min(max_width / image.shape[1], max_height / image.shape[0], 1.0)
    if scale >= 1.0:
        return image
    width = max(1, int(round(image.shape[1] * scale)))
    height = max(1, int(round(image.shape[0] * scale)))
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)


def annotate_checkerboard_status(image: np.ndarray, label: str) -> tuple[np.ndarray, bool]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    flags = cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY
    found, corners = cv2.findChessboardCornersSB(gray, mono_capture.PATTERN_SIZE, flags=flags)
    annotated = image.copy()
    if found and corners is not None:
        cv2.drawChessboardCorners(annotated, mono_capture.PATTERN_SIZE, corners, found)
    status = "CORNERS OK" if found else "NO CORNERS"
    color = (0, 220, 0) if found else (0, 0, 255)
    cv2.putText(annotated, label, (20, 38), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(annotated, status, (20, 78), cv2.FONT_HERSHEY_SIMPLEX, 0.9, color, 2, cv2.LINE_AA)
    return annotated, bool(found)


def show_dual_snapshot_preview(
    *,
    left_serial: str,
    right_serial: str,
    pose_index: int,
    args: argparse.Namespace,
    EventsIterator: Any,
    initiate_device: Any,
    metavision_hal: Any,
) -> None:
    window_name = f"Stereo snapshot preview pose {pose_index:03d}"
    print("[PREVIEW] snapshot mode: R=refresh, Enter=accept, Esc/Q=abort.")
    while True:
        print(f"[PREVIEW] refreshing pose {pose_index:03d} snapshots...")
        try:
            left = accumulate_preview_image(
                serial=left_serial,
                args=args,
                EventsIterator=EventsIterator,
                initiate_device=initiate_device,
                metavision_hal=metavision_hal,
            )
        except Exception as exc:
            print(f"[PREVIEW][LEFT][WARN] {exc}")
            left = make_preview_error_image("LEFT", str(exc))
        try:
            right = accumulate_preview_image(
                serial=right_serial,
                args=args,
                EventsIterator=EventsIterator,
                initiate_device=initiate_device,
                metavision_hal=metavision_hal,
            )
        except Exception as exc:
            print(f"[PREVIEW][RIGHT][WARN] {exc}")
            right = make_preview_error_image("RIGHT", str(exc))
        left, left_found = annotate_checkerboard_status(left, "LEFT")
        right, right_found = annotate_checkerboard_status(right, "RIGHT")
        height = min(left.shape[0], right.shape[0])
        left = cv2.resize(left, (int(left.shape[1] * height / left.shape[0]), height))
        right = cv2.resize(right, (int(right.shape[1] * height / right.shape[0]), height))
        combined = np.hstack([left, right])
        pair_status = "PAIR OK" if left_found and right_found else "PAIR NOT READY"
        pair_color = (0, 220, 0) if left_found and right_found else (0, 0, 255)
        cv2.putText(combined, pair_status, (20, 118), cv2.FONT_HERSHEY_SIMPLEX, 0.9, pair_color, 2, cv2.LINE_AA)
        cv2.putText(
            combined,
            "Enter: accept   R: refresh   Q/Esc: abort",
            (20, combined.shape[0] - 20),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 255, 255),
            2,
            cv2.LINE_AA,
        )
        combined = fit_image_to_bounds(combined, int(args.preview_max_width), int(args.preview_max_height))
        cv2.imshow(window_name, combined)
        while True:
            key = cv2.waitKey(0) & 0xFF
            if key in (13, 10):
                cv2.destroyWindow(window_name)
                return
            if key in (ord("q"), 27):
                cv2.destroyWindow(window_name)
                raise SystemExit("Dual snapshot preview aborted.")
            if key in (ord("r"), ord("R")):
                break


def show_dual_preview(
    *,
    left_serial: str,
    right_serial: str,
    pose_index: int,
    args: argparse.Namespace,
    EventsIterator: Any,
    initiate_device: Any,
    metavision_hal: Any,
    PeriodicFrameGenerationAlgorithm: Any,
    ColorPalette: Any,
    EventLoop: Any,
    BaseWindow: Any,
    MTWindow: Any,
    UIAction: Any,
    UIKeyEvent: Any,
) -> None:
    left_device = None
    right_device = None
    left_iterator = None
    right_iterator = None
    try:
        print(f"[PREVIEW] opening left/right cameras for pose {pose_index:03d}")
        left_device = scale_capture.open_event_camera(initiate_device, metavision_hal, left_serial)
        right_device = scale_capture.open_event_camera(initiate_device, metavision_hal, right_serial)
        left_iterator = EventsIterator.from_device(
            device=left_device,
            mode="delta_t",
            delta_t=int(args.delta_t_us),
            max_duration=None,
            relative_timestamps=False,
        )
        right_iterator = EventsIterator.from_device(
            device=right_device,
            mode="delta_t",
            delta_t=int(args.delta_t_us),
            max_duration=None,
            relative_timestamps=False,
        )
        left_height, left_width = left_iterator.get_size()
        right_height, right_width = right_iterator.get_size()
        left_gen = PeriodicFrameGenerationAlgorithm(
            sensor_width=int(left_width),
            sensor_height=int(left_height),
            accumulation_time_us=int(args.preview_accumulation_us),
            fps=float(args.preview_fps),
            palette=ColorPalette.Dark,
        )
        right_gen = PeriodicFrameGenerationAlgorithm(
            sensor_width=int(right_width),
            sensor_height=int(right_height),
            accumulation_time_us=int(args.preview_accumulation_us),
            fps=float(args.preview_fps),
            palette=ColorPalette.Dark,
        )
        done = False
        aborted = False

        with MTWindow(
            title=f"LEFT pose {pose_index:03d} - Enter: accept, Esc/Q: abort",
            width=int(left_width),
            height=int(left_height),
            mode=BaseWindow.RenderMode.BGR,
        ) as left_window, MTWindow(
            title=f"RIGHT pose {pose_index:03d} - Enter: accept, Esc/Q: abort",
            width=int(right_width),
            height=int(right_height),
            mode=BaseWindow.RenderMode.BGR,
        ) as right_window:

            def accept() -> None:
                nonlocal done
                done = True
                left_window.set_close_flag()
                right_window.set_close_flag()

            def abort() -> None:
                nonlocal aborted
                aborted = True
                left_window.set_close_flag()
                right_window.set_close_flag()

            def keyboard_cb(key: Any, scancode: int, action: Any, mods: int) -> None:
                del scancode, mods
                if action != UIAction.PRESS:
                    return
                if key == UIKeyEvent.KEY_ESCAPE or key == UIKeyEvent.KEY_Q:
                    abort()
                elif key == UIKeyEvent.KEY_ENTER or key == UIKeyEvent.KEY_KP_ENTER:
                    accept()

            left_gen.set_output_callback(lambda _ts, frame: left_window.show_async(frame))
            right_gen.set_output_callback(lambda _ts, frame: right_window.show_async(frame))
            left_window.set_keyboard_callback(keyboard_cb)
            right_window.set_keyboard_callback(keyboard_cb)

            left_iter = iter(left_iterator)
            right_iter = iter(right_iterator)
            print("[PREVIEW] position checkerboard so both views see all corners, then press Enter in either window.")
            while not done and not aborted:
                EventLoop.poll_and_dispatch()
                try:
                    left_events = next(left_iter)
                    left_gen.process_events(left_events)
                except StopIteration:
                    break
                try:
                    right_events = next(right_iter)
                    right_gen.process_events(right_events)
                except StopIteration:
                    break
                if left_window.should_close() or right_window.should_close():
                    break
        if aborted:
            raise SystemExit("Dual preview aborted.")
        if not done:
            raise SystemExit("Dual preview closed before accepting the pose.")
    finally:
        try:
            del left_iterator
        except Exception:
            pass
        try:
            del right_iterator
        except Exception:
            pass
        try:
            del left_device
        except Exception:
            pass
        try:
            del right_device
        except Exception:
            pass
        gc.collect()


def main() -> None:
    args = parse_args()
    if args.duration_sec <= 0:
        raise SystemExit("--duration-sec must be positive for stereo paired capture.")
    if args.delta_t_us <= 0 or args.npz_delta_t_us <= 0 or args.frame_window_us <= 0:
        raise SystemExit("Time arguments must be positive.")
    if args.pose_count < 0:
        raise SystemExit("--pose-count must be zero or positive.")
    mono_capture.parse_int_list(args.frame_window_us_list, args.frame_window_us, "--frame-window-us-list")

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

    left_serial, right_serial = resolve_serials(args, metavision_hal)
    out_dir = Path(args.output_dir)
    left_dir = out_dir / "left"
    right_dir = out_dir / "right"
    left_dir.mkdir(parents=True, exist_ok=True)
    right_dir.mkdir(parents=True, exist_ok=True)

    pose_index = max_existing_stereo_pose_index(out_dir)
    print(f"Left camera:  {left_serial}")
    print(f"Right camera: {right_serial}")
    if pose_index > 0:
        print(f"Continuing after existing pose index {pose_index:03d}.")

    captured: list[dict[str, Any]] = []
    target_count = int(args.pose_count)
    while True:
        next_pose = pose_index + 1
        if target_count == 0:
            if not should_continue_interactive(next_pose):
                break
        elif len(captured) >= target_count:
            break
        else:
            print(f"\nCapturing stereo pose {next_pose:03d}/{pose_index + target_count:03d}")

        if args.dual_preview:
            if args.dual_preview_mode == "live":
                show_dual_preview(
                    left_serial=left_serial,
                    right_serial=right_serial,
                    pose_index=next_pose,
                    args=args,
                    EventsIterator=EventsIterator,
                    initiate_device=initiate_device,
                    metavision_hal=metavision_hal,
                    PeriodicFrameGenerationAlgorithm=PeriodicFrameGenerationAlgorithm,
                    ColorPalette=ColorPalette,
                    EventLoop=EventLoop,
                    BaseWindow=BaseWindow,
                    MTWindow=MTWindow,
                    UIAction=UIAction,
                    UIKeyEvent=UIKeyEvent,
                )
            else:
                show_dual_snapshot_preview(
                    left_serial=left_serial,
                    right_serial=right_serial,
                    pose_index=next_pose,
                    args=args,
                    EventsIterator=EventsIterator,
                    initiate_device=initiate_device,
                    metavision_hal=metavision_hal,
                )

        left_stats = capture_one_side(
            side="left",
            serial=left_serial,
            pose_index=next_pose,
            side_dir=left_dir,
            args=args,
            EventsIterator=EventsIterator,
            initiate_device=initiate_device,
            metavision_hal=metavision_hal,
            PeriodicFrameGenerationAlgorithm=PeriodicFrameGenerationAlgorithm,
            ColorPalette=ColorPalette,
            EventLoop=EventLoop,
            BaseWindow=BaseWindow,
            MTWindow=MTWindow,
            UIAction=UIAction,
            UIKeyEvent=UIKeyEvent,
        )
        right_stats = capture_one_side(
            side="right",
            serial=right_serial,
            pose_index=next_pose,
            side_dir=right_dir,
            args=args,
            EventsIterator=EventsIterator,
            initiate_device=initiate_device,
            metavision_hal=metavision_hal,
            PeriodicFrameGenerationAlgorithm=PeriodicFrameGenerationAlgorithm,
            ColorPalette=ColorPalette,
            EventLoop=EventLoop,
            BaseWindow=BaseWindow,
            MTWindow=MTWindow,
            UIAction=UIAction,
            UIKeyEvent=UIKeyEvent,
        )
        captured.append({"pose_index": next_pose, "left": left_stats, "right": right_stats})
        pose_index = next_pose

    manifest = {
        "output_dir": str(out_dir.resolve()),
        "left_serial": left_serial,
        "right_serial": right_serial,
        "left_images": str((left_dir / "calib_images").resolve()),
        "right_images": str((right_dir / "calib_images").resolve()),
        "pose_count_this_run": len(captured),
        "last_pose_index": pose_index,
        "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
        "capture_settings": {
            "duration_sec": float(args.duration_sec),
            "frame_window_us": int(args.frame_window_us),
            "frame_window_us_list": mono_capture.parse_int_list(
                args.frame_window_us_list, args.frame_window_us, "--frame-window-us-list"
            ),
            "render_mode": str(args.render_mode),
            "candidate_count": int(args.candidate_count),
        },
    }
    manifest_path = out_dir / "stereo_capture_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nFinished. Left images:  {left_dir / 'calib_images'}")
    print(f"Finished. Right images: {right_dir / 'calib_images'}")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
