#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Capture time-aligned checkerboard images from two event cameras.

This setup-specific capture tool records each pose with
``stereo_eventcam_record_sync.py`` and renders left/right calibration images
from exactly the same hardware-camera-clock interval.  It is intended for the
10 x 7 square ``checkerboard_10x7_normal.png`` whose measured square side is
7.12 mm.

The normal PNG does not blink.  When an LCD refresh/backlight produces enough
events for both cameras, the displayed image should remain stationary.
Small image motion is only a fallback when the common-window corner detector
does not receive enough usable events.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

import eventcam_checkerboard_calibration_capture as mono_capture


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CHECKERBOARD = SCRIPT_DIR / "checkerboard_10x7_normal.png"
EXPECTED_CHECKERBOARD_NAME = "checkerboard_10x7_normal.png"
EXPECTED_SQUARE_MM = 7.12
DEFAULT_OUTPUT_DIR = Path("stereo_checkerboard_calib")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture synchronized stereo checkerboard poses from the normal "
            "10x7 checkerboard image."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--left-serial", default="00000508", help="Left camera serial.")
    parser.add_argument("--right-serial", default="00000509", help="Right camera serial.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--checkerboard-image",
        type=Path,
        default=DEFAULT_CHECKERBOARD,
        help="The non-blinking checkerboard source used for every pose.",
    )
    parser.add_argument(
        "--square-mm",
        type=float,
        default=EXPECTED_SQUARE_MM,
        help="Measured square side; locked to 7.12 mm for this setup.",
    )
    parser.add_argument(
        "--pose-count",
        type=int,
        default=30,
        help="Number of accepted paired poses. 0 continues until Q is entered.",
    )
    parser.add_argument(
        "--duration-sec",
        type=float,
        default=0.2,
        help="Shared hardware-clock recording interval per pose.",
    )
    parser.add_argument(
        "--delta-t-us",
        type=int,
        default=5000,
        help="Event slice duration used by each synchronized recorder.",
    )
    parser.add_argument("--start-delay-sec", type=float, default=1.0)
    parser.add_argument(
        "--hw-sync",
        choices=["left-master", "right-master"],
        default="left-master",
        help="Hardware synchronization is mandatory for calibration capture.",
    )
    parser.add_argument("--hw-sync-timeout-sec", type=float, default=10.0)
    parser.add_argument("--sensor-width", type=int, default=1280)
    parser.add_argument("--sensor-height", type=int, default=720)
    parser.add_argument(
        "--frame-window-us-list",
        default="10000,20000,30000,40000",
        help="Comma-separated accumulation windows tried at common timestamps.",
    )
    parser.add_argument(
        "--preferred-frame-window-us",
        type=int,
        default=20_000,
        help="Accumulation length tried first; other listed lengths are fallbacks.",
    )
    parser.add_argument(
        "--window-hop-us",
        type=int,
        default=5000,
        help="Start-time spacing of synchronized candidate windows.",
    )
    parser.add_argument(
        "--candidate-count",
        type=int,
        default=4,
        help="Highest-event common windows retained per accumulation length.",
    )
    parser.add_argument(
        "--max-candidate-pairs",
        type=int,
        default=12,
        help="Maximum synchronized image pairs tested before an attempt is rejected.",
    )
    parser.add_argument(
        "--render-mode",
        choices=["signed", "abs", "on", "off", "all"],
        default="all",
    )
    parser.add_argument("--scale-percentile", type=float, default=99.5)
    parser.add_argument("--min-events-per-camera", type=int, default=500)
    parser.add_argument("--max-attempts-per-pose", type=int, default=3)
    parser.add_argument(
        "--skip-preview",
        "--skip-initial-preview",
        dest="skip_preview",
        action="store_true",
        help="Do not open the dual-camera event preview before each pose.",
    )
    parser.add_argument("--preview-delta-t-us", type=int, default=5000)
    parser.add_argument("--preview-scale", type=float, default=0.5)
    parser.add_argument("--camera-reopen-wait-sec", type=float, default=1.0)
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="Delegate device listing to the synchronized recorder and exit.",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def validate_args(args: argparse.Namespace) -> tuple[Path, list[int]]:
    board = args.checkerboard_image.resolve()
    if not board.exists():
        raise SystemExit(f"Checkerboard image not found: {board}")
    if board.name.lower() != EXPECTED_CHECKERBOARD_NAME.lower() or "blink" in board.name.lower():
        raise SystemExit(
            "This workflow is locked to the non-blinking image "
            f"{EXPECTED_CHECKERBOARD_NAME}; selected: {board.name}"
        )
    if not math.isclose(float(args.square_mm), EXPECTED_SQUARE_MM, rel_tol=0.0, abs_tol=1e-9):
        raise SystemExit(
            "Checkerboard square-size lock failed: "
            f"--square-mm={float(args.square_mm):.12g}; required={EXPECTED_SQUARE_MM:.2f}. "
            "Do not use the historical 7.1 mm value."
        )
    if not str(args.left_serial).strip() or not str(args.right_serial).strip():
        raise SystemExit("Both camera serials are required.")
    if str(args.left_serial) == str(args.right_serial):
        raise SystemExit("Left and right serials must differ.")
    if args.pose_count < 0:
        raise SystemExit("--pose-count must be zero or positive.")
    if float(args.duration_sec) <= 0:
        raise SystemExit("--duration-sec must be positive.")
    if float(args.hw_sync_timeout_sec) <= 0:
        raise SystemExit("--hw-sync-timeout-sec must be positive.")
    for name in ("start_delay_sec", "camera_reopen_wait_sec"):
        if float(getattr(args, name)) < 0:
            raise SystemExit(f"--{name.replace('_', '-')} must not be negative.")
    if not 0 < float(args.scale_percentile) <= 100:
        raise SystemExit("--scale-percentile must be in (0, 100].")
    for name in (
        "delta_t_us",
        "sensor_width",
        "sensor_height",
        "window_hop_us",
        "candidate_count",
        "max_candidate_pairs",
        "preferred_frame_window_us",
        "min_events_per_camera",
        "max_attempts_per_pose",
    ):
        if int(getattr(args, name)) <= 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive.")
    windows = mono_capture.parse_int_list(
        str(args.frame_window_us_list),
        20_000,
        "--frame-window-us-list",
    )
    preferred_window_us = int(args.preferred_frame_window_us)
    if preferred_window_us not in windows:
        windows.append(preferred_window_us)
        windows.sort()
    capture_duration_us = int(round(float(args.duration_sec) * 1_000_000))
    if max(windows) > capture_duration_us:
        raise SystemExit(
            "The largest frame window exceeds the recording duration: "
            f"max_window={max(windows)} us, duration={capture_duration_us} us."
        )
    return board, windows


def existing_pose_count(output_dir: Path) -> int:
    left_dir = output_dir / "left" / "calib_images"
    right_dir = output_dir / "right" / "calib_images"
    left = {path.name for path in left_dir.glob("pose_*.png")}
    right = {path.name for path in right_dir.glob("pose_*.png")}
    return len(left & right)


def next_pose_index(output_dir: Path) -> int:
    indices: list[int] = []
    for side in ("left", "right"):
        for path in (output_dir / side / "calib_images").glob("pose_*.png"):
            try:
                indices.append(int(path.stem.split("_")[-1]))
            except ValueError:
                continue
    return max(indices, default=0) + 1


def next_attempt_index(pose_root: Path) -> int:
    values: list[int] = []
    for path in pose_root.glob("attempt_*"):
        try:
            values.append(int(path.name.split("_")[-1]))
        except ValueError:
            continue
    return max(values, default=0) + 1


def load_scalar(data: np.lib.npyio.NpzFile, key: str, default: Any = None) -> Any:
    if key not in data.files:
        return default
    value = np.asarray(data[key])
    return value.reshape(-1)[0].item() if value.size else default


def common_capture_interval(
    left: np.lib.npyio.NpzFile,
    right: np.lib.npyio.NpzFile,
) -> tuple[int, int]:
    for side, data in (("Left", left), ("Right", right)):
        if not bool(load_scalar(data, "hardware_synchronized", False)):
            raise RuntimeError(f"{side} NPZ is not marked hardware synchronized.")
        if not bool(load_scalar(data, "sync_mode_verified", False)):
            raise RuntimeError(f"{side} NPZ synchronization mode is not verified.")
        if not bool(load_scalar(data, "capture_interval_complete", False)):
            raise RuntimeError(f"{side} NPZ did not complete the shared capture interval.")
        if bool(load_scalar(data, "event_limit_reached", False)):
            raise RuntimeError(f"{side} NPZ was truncated by the event limit.")
    left_domain = str(load_scalar(left, "timestamp_domain_id", ""))
    right_domain = str(load_scalar(right, "timestamp_domain_id", ""))
    if not left_domain or left_domain != right_domain:
        raise RuntimeError(
            "Left/right timestamp domains differ: "
            f"left={left_domain!r}, right={right_domain!r}."
        )
    roles = {
        str(load_scalar(left, "sync_mode_applied", "")),
        str(load_scalar(right, "sync_mode_applied", "")),
    }
    if roles != {"master", "slave"}:
        raise RuntimeError(f"Expected one Master and one Slave; observed roles={roles}.")
    left_start = int(load_scalar(left, "output_start_ts_us", 0))
    right_start = int(load_scalar(right, "output_start_ts_us", 0))
    left_end = int(load_scalar(left, "output_end_ts_us", 0))
    right_end = int(load_scalar(right, "output_end_ts_us", 0))
    if left_start != right_start or left_end != right_end:
        raise RuntimeError(
            "Hardware-synchronized NPZs do not have the same saved interval: "
            f"left=[{left_start}, {left_end}), "
            f"right=[{right_start}, {right_end})."
        )
    start = left_start
    end = left_end
    if end <= start:
        raise RuntimeError(f"No common camera-clock interval: [{start}, {end}).")
    return start, end


def count_in_interval(timestamps: np.ndarray, start_us: int, end_us: int) -> int:
    begin = int(np.searchsorted(timestamps, start_us, side="left"))
    end = int(np.searchsorted(timestamps, end_us, side="left"))
    return max(0, end - begin)


def synchronized_candidate_windows(
    left_events: np.ndarray,
    right_events: np.ndarray,
    common_start_us: int,
    common_end_us: int,
    window_us_values: list[int],
    hop_us: int,
    minimum_events: int,
    candidate_count: int,
    preferred_window_us: int | None = None,
) -> list[dict[str, int]]:
    # A structured-array field is strided. Repeated scalar np.searchsorted
    # calls on that field can copy or scan the tens-of-millions-entry
    # timestamp array for every candidate window. Make one contiguous copy
    # per camera and perform vectorized searches instead.
    left_t = np.ascontiguousarray(left_events["t"], dtype=np.int64)
    right_t = np.ascontiguousarray(right_events["t"], dtype=np.int64)
    selected: list[dict[str, int]] = []
    for window_us in window_us_values:
        rows: list[dict[str, int]] = []
        latest_start = common_end_us - int(window_us)
        if latest_start < common_start_us:
            continue
        starts = np.arange(
            int(common_start_us),
            int(latest_start) + 1,
            int(hop_us),
            dtype=np.int64,
        )
        ends = starts + int(window_us)
        left_counts = (
            np.searchsorted(left_t, ends, side="left")
            - np.searchsorted(left_t, starts, side="left")
        )
        right_counts = (
            np.searchsorted(right_t, ends, side="left")
            - np.searchsorted(right_t, starts, side="left")
        )
        usable = np.minimum(left_counts, right_counts) >= int(minimum_events)
        for start_us, end_us, left_count, right_count in zip(
            starts[usable],
            ends[usable],
            left_counts[usable],
            right_counts[usable],
        ):
            if min(left_count, right_count) < int(minimum_events):
                continue
            rows.append(
                {
                    "start_ts_us": int(start_us),
                    "end_ts_us": int(end_us),
                    "frame_window_us": int(window_us),
                    "left_events": int(left_count),
                    "right_events": int(right_count),
                    "joint_event_score": int(min(left_count, right_count)),
                }
            )
        rows.sort(
            key=lambda row: (
                int(row["joint_event_score"]),
                -abs(int(row["left_events"]) - int(row["right_events"])),
            ),
            reverse=True,
        )
        selected.extend(rows[: int(candidate_count)])
    if preferred_window_us is None:
        selected.sort(
            key=lambda row: (
                int(row["joint_event_score"]),
                -abs(int(row["left_events"]) - int(row["right_events"])),
            ),
            reverse=True,
        )
    else:
        selected.sort(
            key=lambda row: (
                -abs(int(row["frame_window_us"]) - int(preferred_window_us)),
                -int(row["frame_window_us"]),
                int(row["joint_event_score"]),
                -abs(int(row["left_events"]) - int(row["right_events"])),
            ),
            reverse=True,
        )
    return selected


def events_in_interval(events: np.ndarray, start_us: int, end_us: int) -> np.ndarray:
    timestamps = np.asarray(events["t"], dtype=np.int64)
    begin = int(np.searchsorted(timestamps, start_us, side="left"))
    end = int(np.searchsorted(timestamps, end_us, side="left"))
    return events[begin:end]


def render_and_select_pair(
    *,
    left_npz: Path,
    right_npz: Path,
    output_dir: Path,
    pose_index: int,
    attempt_index: int,
    args: argparse.Namespace,
    window_us_values: list[int],
) -> dict[str, Any]:
    processing_started = time.perf_counter()
    left_data = np.load(left_npz, allow_pickle=False)
    right_data = np.load(right_npz, allow_pickle=False)
    try:
        left_events = np.asarray(left_data["events"])
        right_events = np.asarray(right_data["events"])
        for side, data, expected_serial in (
            ("left", left_data, str(args.left_serial)),
            ("right", right_data, str(args.right_serial)),
        ):
            actual_serial = str(load_scalar(data, "camera_serial", ""))
            if actual_serial != expected_serial:
                raise RuntimeError(
                    f"{side} NPZ camera_serial={actual_serial!r}; "
                    f"expected={expected_serial!r}."
                )
        common_start, common_end = common_capture_interval(left_data, right_data)
    finally:
        left_data.close()
        right_data.close()
    load_sec = time.perf_counter() - processing_started

    window_search_started = time.perf_counter()
    windows = synchronized_candidate_windows(
        left_events,
        right_events,
        common_start,
        common_end,
        window_us_values,
        int(args.window_hop_us),
        int(args.min_events_per_camera),
        int(args.candidate_count),
        preferred_window_us=int(args.preferred_frame_window_us),
    )
    candidate_windows_total = len(windows)
    windows = windows[: int(args.max_candidate_pairs)]
    window_search_sec = time.perf_counter() - window_search_started

    candidate_root = (
        output_dir
        / "paired_candidates"
        / f"pose_{pose_index:03d}"
        / f"attempt_{attempt_index:02d}"
    )
    candidate_root.mkdir(parents=True, exist_ok=True)
    detector_flags = cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY
    modes = mono_capture.render_modes_for_arg(str(args.render_mode))
    if str(args.render_mode) == "all":
        modes = [mode for mode in modes if mode != "signed"]
    rows: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    render_started = time.perf_counter()

    for candidate_index, window in enumerate(windows):
        start_us = int(window["start_ts_us"])
        end_us = int(window["end_ts_us"])
        left_window = events_in_interval(left_events, start_us, end_us)
        right_window = events_in_interval(right_events, start_us, end_us)
        for mode in modes:
            left_image = mono_capture.render_events(
                left_window,
                int(args.sensor_width),
                int(args.sensor_height),
                mode,
                float(args.scale_percentile),
            )
            right_image = mono_capture.render_events(
                right_window,
                int(args.sensor_width),
                int(args.sensor_height),
                mode,
                float(args.scale_percentile),
            )
            stem = (
                f"cand_{candidate_index:03d}_{mode}_"
                f"{int(window['frame_window_us'])}us_{start_us}"
            )
            left_path = candidate_root / f"{stem}_left.png"
            right_path = candidate_root / f"{stem}_right.png"
            cv2.imwrite(str(left_path), left_image)
            cv2.imwrite(str(right_path), right_image)

            left_found, left_corners = cv2.findChessboardCornersSB(
                left_image,
                mono_capture.PATTERN_SIZE,
                flags=detector_flags,
            )
            right_found, right_corners = cv2.findChessboardCornersSB(
                right_image,
                mono_capture.PATTERN_SIZE,
                flags=detector_flags,
            )
            left_debug = cv2.cvtColor(left_image, cv2.COLOR_GRAY2BGR)
            right_debug = cv2.cvtColor(right_image, cv2.COLOR_GRAY2BGR)
            if left_found:
                cv2.drawChessboardCorners(
                    left_debug,
                    mono_capture.PATTERN_SIZE,
                    left_corners,
                    bool(left_found),
                )
            if right_found:
                cv2.drawChessboardCorners(
                    right_debug,
                    mono_capture.PATTERN_SIZE,
                    right_corners,
                    bool(right_found),
                )
            left_debug_path = candidate_root / f"{stem}_left_debug.png"
            right_debug_path = candidate_root / f"{stem}_right_debug.png"
            cv2.imwrite(str(left_debug_path), left_debug)
            cv2.imwrite(str(right_debug_path), right_debug)

            row: dict[str, Any] = {
                **window,
                "candidate_index": int(candidate_index),
                "render_mode": mode,
                "left_found": bool(left_found),
                "right_found": bool(right_found),
                "both_found": bool(left_found and right_found),
                "left_image": str(left_path.resolve()),
                "right_image": str(right_path.resolve()),
                "left_debug": str(left_debug_path.resolve()),
                "right_debug": str(right_debug_path.resolve()),
            }
            rows.append(row)
            rank = (
                int(row["both_found"]),
                int(row["left_found"]) + int(row["right_found"]),
                int(row["joint_event_score"]),
                -abs(int(row["left_events"]) - int(row["right_events"])),
            )
            if best is None or rank > best["_rank"]:
                best = {**row, "_rank": rank}
            if row["both_found"]:
                # Windows are already ordered by joint event support. The first
                # pair with valid corners is sufficient and avoids writing
                # thousands of diagnostic PNGs per calibration session.
                break
        if best is not None and best["both_found"]:
            break

    accepted = bool(best is not None and best["both_found"])
    selected_left: Path | None = None
    selected_right: Path | None = None
    if accepted and best is not None:
        for side in ("left", "right"):
            (output_dir / side / "calib_images").mkdir(parents=True, exist_ok=True)
            (output_dir / side / "corner_debug").mkdir(parents=True, exist_ok=True)
        selected_left = output_dir / "left" / "calib_images" / f"pose_{pose_index:03d}.png"
        selected_right = output_dir / "right" / "calib_images" / f"pose_{pose_index:03d}.png"
        shutil.copy2(best["left_image"], selected_left)
        shutil.copy2(best["right_image"], selected_right)
        shutil.copy2(
            best["left_debug"],
            output_dir / "left" / "corner_debug" / f"pose_{pose_index:03d}.png",
        )
        shutil.copy2(
            best["right_debug"],
            output_dir / "right" / "corner_debug" / f"pose_{pose_index:03d}.png",
        )

    if best is not None:
        best = {key: value for key, value in best.items() if key != "_rank"}
    render_sec = time.perf_counter() - render_started
    processing_sec = time.perf_counter() - processing_started
    report = {
        "pose_index": int(pose_index),
        "attempt_index": int(attempt_index),
        "left_npz": str(left_npz.resolve()),
        "right_npz": str(right_npz.resolve()),
        "common_interval_us": [int(common_start), int(common_end)],
        "candidate_windows": int(candidate_windows_total),
        "candidate_windows_considered": len(windows),
        "rendered_pairs": len(rows),
        "accepted": accepted,
        "best": best,
        "selected_left": None if selected_left is None else str(selected_left.resolve()),
        "selected_right": None if selected_right is None else str(selected_right.resolve()),
        "timing_sec": {
            "load_npz": float(load_sec),
            "window_search": float(window_search_sec),
            "render_and_detect": float(render_sec),
            "total": float(processing_sec),
        },
        "candidates": rows,
    }
    atomic_write_json(candidate_root / "pair_render_report.json", report)
    print(
        "[PROCESS][TIMING] "
        f"load={load_sec:.2f}s, windows={window_search_sec:.2f}s, "
        f"render={render_sec:.2f}s, total={processing_sec:.2f}s"
    )
    return report


def build_record_command(
    args: argparse.Namespace,
    run_dir: Path,
    pose_index: int,
    attempt_index: int,
) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT_DIR / "stereo_eventcam_record_sync.py"),
        "--left-serial",
        str(args.left_serial),
        "--right-serial",
        str(args.right_serial),
        "--run-dir",
        str(run_dir),
        "--duration-sec",
        str(float(args.duration_sec)),
        "--delta-t-us",
        str(int(args.delta_t_us)),
        "--start-delay-sec",
        str(float(args.start_delay_sec)),
        "--hw-sync",
        str(args.hw_sync),
        "--hw-sync-timeout-sec",
        str(float(args.hw_sync_timeout_sec)),
        "--sensor-width",
        str(int(args.sensor_width)),
        "--sensor-height",
        str(int(args.sensor_height)),
        "--npz-compression",
        "none",
        "--max-events",
        "0",
        "--note",
        (
            f"synchronized checkerboard pose={pose_index:03d} "
            f"attempt={attempt_index:02d}; source={EXPECTED_CHECKERBOARD_NAME}; "
            f"square_mm={EXPECTED_SQUARE_MM:.2f}"
        ),
        "--no-preview",
    ]


def run_pose_preview(args: argparse.Namespace) -> bool:
    from stereo_eventcam_record_sync import run_preview

    return run_preview(
        left_serial=str(args.left_serial),
        right_serial=str(args.right_serial),
        sensor_width=int(args.sensor_width),
        sensor_height=int(args.sensor_height),
        delta_t_us=int(args.preview_delta_t_us),
        display_scale=float(args.preview_scale),
        point_size=1,
        reopen_wait_sec=float(args.camera_reopen_wait_sec),
        window_name="Stereo checkerboard pose preview",
        instruction_text=(
            "Enter: capture    R: discard backlog/refresh    Q/Esc: finish"
        ),
    )


def main() -> int:
    args = parse_args()
    if args.list_devices:
        return subprocess.call(
            [sys.executable, str(SCRIPT_DIR / "stereo_eventcam_record_sync.py"), "--list-devices"]
        )

    board, window_us_values = validate_args(args)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    for side in ("left", "right"):
        (output_dir / side / "calib_images").mkdir(parents=True, exist_ok=True)
        (output_dir / side / "corner_debug").mkdir(parents=True, exist_ok=True)

    manifest_path = output_dir / "stereo_sync_checkerboard_manifest.json"
    manifest: dict[str, Any] = {
        "schema_version": 2,
        "capture_script": Path(__file__).name,
        "output_dir": str(output_dir),
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "left_serial": str(args.left_serial),
        "right_serial": str(args.right_serial),
        "checkerboard_image": str(board),
        "checkerboard_sha256": sha256_file(board),
        "checkerboard_pattern": {
            "squares": [10, 7],
            "internal_corners": list(mono_capture.PATTERN_SIZE),
            "square_size_mm": EXPECTED_SQUARE_MM,
            "blinking": False,
        },
        "hardware_sync": str(args.hw_sync),
        "capture_settings": {
            "duration_sec": float(args.duration_sec),
            "delta_t_us": int(args.delta_t_us),
            "frame_window_us_list": window_us_values,
            "preferred_frame_window_us": int(args.preferred_frame_window_us),
            "window_hop_us": int(args.window_hop_us),
            "candidate_count": int(args.candidate_count),
            "max_candidate_pairs": int(args.max_candidate_pairs),
            "render_mode": str(args.render_mode),
        },
        "attempts": [],
    }
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        locks = (
            ("left_serial", str(args.left_serial)),
            ("right_serial", str(args.right_serial)),
            ("checkerboard_sha256", manifest["checkerboard_sha256"]),
            ("hardware_sync", str(args.hw_sync)),
        )
        for key, expected in locks:
            if existing.get(key) != expected:
                raise SystemExit(
                    f"Existing output manifest {key}={existing.get(key)!r}, "
                    f"but this run requests {expected!r}. Use a new --output-dir."
                )
        old_square = float(existing.get("checkerboard_pattern", {}).get("square_size_mm", 0.0))
        if not math.isclose(old_square, EXPECTED_SQUARE_MM, rel_tol=0.0, abs_tol=1e-9):
            raise SystemExit(
                f"Existing output uses square_size_mm={old_square}; expected {EXPECTED_SQUARE_MM:.2f}."
            )
        manifest = existing
    manifest["schema_version"] = 2
    manifest["capture_settings_current"] = {
        "duration_sec": float(args.duration_sec),
        "delta_t_us": int(args.delta_t_us),
        "frame_window_us_list": window_us_values,
        "preferred_frame_window_us": int(args.preferred_frame_window_us),
        "window_hop_us": int(args.window_hop_us),
        "candidate_count": int(args.candidate_count),
        "max_candidate_pairs": int(args.max_candidate_pairs),
        "render_mode": str(args.render_mode),
    }
    atomic_write_json(manifest_path, manifest)

    print("=== Synchronized stereo checkerboard capture ===")
    print(f"Left:  {args.left_serial}")
    print(f"Right: {args.right_serial}")
    print(f"Source: {board.name} (non-blinking)")
    print(f"Square: {EXPECTED_SQUARE_MM:.2f} mm  [LOCKED; 7.1 mm is invalid]")
    print(f"Output: {output_dir}")
    print(
        "Keep normal.png stationary when LCD refresh produces sufficient events. "
        "Use small image motion only if common-window corner detection repeatedly fails."
    )
    print(
        f"Fast capture: duration={float(args.duration_sec):.3f}s, "
        f"preferred_window={int(args.preferred_frame_window_us)}us. "
        "The preview closes during synchronized recording and reopens for every pose."
    )

    accepted_total = existing_pose_count(output_dir)
    pose_index = next_pose_index(output_dir)
    while args.pose_count == 0 or accepted_total < int(args.pose_count):
        print(
            f"\nPose {pose_index:03d}: show {EXPECTED_CHECKERBOARD_NAME}, keep all "
            "10x7 squares visible in both views, vary position/roll/pitch/yaw."
        )
        if args.skip_preview:
            response = input(
                "Keep the displayed image stationary and press Enter to capture; "
                "Q=finish: "
            ).strip().lower()
            if response in {"q", "quit", "exit"}:
                break
        else:
            if not run_pose_preview(args):
                print("[PREVIEW] Session finished by user before this pose.")
                break

        pose_root = output_dir / "raw" / f"pose_{pose_index:03d}"
        pose_root.mkdir(parents=True, exist_ok=True)
        accepted = False
        for local_attempt in range(1, int(args.max_attempts_per_pose) + 1):
            attempt_index = next_attempt_index(pose_root)
            run_dir = pose_root / f"attempt_{attempt_index:02d}"
            command = build_record_command(args, run_dir, pose_index, attempt_index)
            print(
                f"[CAPTURE] pose={pose_index:03d} "
                f"attempt={local_attempt}/{args.max_attempts_per_pose}"
            )
            completed = subprocess.run(command, check=False)
            attempt_row: dict[str, Any] = {
                "pose_index": int(pose_index),
                "attempt_index": int(attempt_index),
                "run_dir": str(run_dir.resolve()),
                "record_return_code": int(completed.returncode),
                "capture_settings": dict(manifest["capture_settings_current"]),
                "accepted": False,
            }
            if completed.returncode == 0:
                try:
                    report = render_and_select_pair(
                        left_npz=run_dir / "left" / "left_events.npz",
                        right_npz=run_dir / "right" / "right_events.npz",
                        output_dir=output_dir,
                        pose_index=pose_index,
                        attempt_index=attempt_index,
                        args=args,
                        window_us_values=window_us_values,
                    )
                    attempt_row["render_report"] = report
                    accepted = bool(report["accepted"])
                except Exception as exc:
                    attempt_row["processing_error"] = repr(exc)
                    print(f"[PROCESS][FAIL] {exc}")
            attempt_row["accepted"] = accepted
            manifest.setdefault("attempts", []).append(attempt_row)
            manifest["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
            manifest["accepted_pose_count"] = existing_pose_count(output_dir)
            atomic_write_json(manifest_path, manifest)

            if accepted:
                best = attempt_row["render_report"]["best"]
                print(
                    f"[OK] paired corners found at common interval "
                    f"[{best['start_ts_us']}, {best['end_ts_us']}) us; "
                    f"mode={best['render_mode']}."
                )
                break
            print(
                "[RETRY] The 9x6 internal corners were not found in both cameras "
                "at one common time window."
            )
            if local_attempt < int(args.max_attempts_per_pose):
                if args.skip_preview:
                    retry = input(
                        "Adjust pose/display/event visibility and press Enter to retry; "
                        "Q=finish: "
                    ).strip().lower()
                    if retry in {"q", "quit", "exit"}:
                        manifest["status"] = "stopped_by_user"
                        atomic_write_json(manifest_path, manifest)
                        return 0
                else:
                    print("Adjust the pose, then accept the refreshed dual-camera preview.")
                    if not run_pose_preview(args):
                        manifest["status"] = "stopped_by_user"
                        atomic_write_json(manifest_path, manifest)
                        return 0

        if accepted:
            accepted_total += 1
            pose_index += 1
        else:
            print(
                f"Pose {pose_index:03d} exhausted its attempts. It was not added "
                "to either calib_images folder."
            )
            while True:
                if args.skip_preview:
                    prompt = "Enter=retry the same pose; Q=finish: "
                else:
                    prompt = (
                        "R/Enter=reopen the preview and retry the same pose; "
                        "Q=finish: "
                    )
                response = input(prompt).strip().lower()
                if response in {"q", "quit", "exit"}:
                    break
                if response in {"", "r", "refresh"}:
                    if not args.skip_preview:
                        print(
                            f"[PREVIEW][CLI REFRESH] Reopening the dual-camera "
                            f"preview for Pose {pose_index:03d}."
                        )
                    break
                print("Enter R (refresh/retry) or Q (finish).")
            if response in {"q", "quit", "exit"}:
                break

    manifest["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    manifest["accepted_pose_count"] = existing_pose_count(output_dir)
    manifest["status"] = "complete" if args.pose_count and accepted_total >= args.pose_count else "stopped"
    atomic_write_json(manifest_path, manifest)
    print(f"Accepted paired poses: {manifest['accepted_pose_count']}")
    print(f"Left images:  {output_dir / 'left' / 'calib_images'}")
    print(f"Right images: {output_dir / 'right' / 'calib_images'}")
    print(f"Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    if os.name == "nt":
        import multiprocessing as mp

        mp.freeze_support()
    raise SystemExit(main())
