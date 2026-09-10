#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
acoustools_eventcam_sync.py
===========================

This script is derived from the AcousTools + INFINICAM multi-trajectory
script, but replaces INFINICAM operation with a Metavision/OpenEB event
camera recorder. Camera acquisition runs in a background thread only while a
trial is being recorded. After each run the recorded RAW event stream is
converted by the embedded LED-sync postprocessor in this file.

This variant opens the event camera only around each recording, then runs the
LED-sync postprocess synchronously before continuing to the next trajectory.
That keeps the event-camera drain thread from competing with hologram
preparation and smooth PAT positioning.

This variant enables the event camera Trigger In facility. External trigger
edges are stored by the Metavision RAW stream and exported after each run as
``*_eventcam_trigger_events.csv`` for hardware synchronization checks.

The trajectories implemented here are directly ported from the legacy
pyftdi/FASTCAM example and include

    1. Z–axis vibration
    2. Elliptical orbit in the XZ plane
    3. Diagonal line in the XZ plane
    4. Heart‑shaped orbit in the XZ plane
    5. Rectangular orbit in the XZ plane
    6. Vertical figure–eight (lasso) orbit
    7. Horizontal “infinity” (∞) orbit
    8. Single slanted line
    9. S‑shaped orbit
    10. X–axis vibration
    11. Long random stroke
    12. 3D figure–eight with a circular/elliptical XY projection
    13. Closed toroidal helix
    14. Trefoil knot
    15. 3D Lissajous orbit

For each run the user enters basic parameters (mode, amplitudes, frame rate,
and loop count).  The script computes a single cycle of positions,
generates holograms for each step, transfers them to the PAT in a loop via
``send_message``, records a synchronized event-camera RAW file and then logs
the ideal timing/position sequence as a CSV file. Prior to starting the motion
the particle is moved smoothly from its current resting location to the
beginning of the orbit and after completing the motion the user may return it
to the centre.

**Why no Python loops during motion?**  The PAT supports hardware looping.
We therefore prepare a sequence of holograms once and ask the controller to
loop over them at a fixed frame rate for the requested number of cycles.
To provide an ideal log of the expected focal positions versus time, we
validate that the requested frame rate is an exact divider of OpenMPD's
40 kHz update clock, then compute ``dt = 1 / effective_fps``. This keeps the
log aligned with the hardware playback clock even though we are not sending
packets on every frame from Python.

"""

from __future__ import annotations

import ctypes
import csv
import datetime
import gc
import json
import math
import os
import queue
import random
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import torch

from acoustools.Levitator import LevitatorController
from acoustools.Solvers import wgs, kd_solver
from acoustools.Utilities import transducers, add_lev_sig, create_points, BOARD_POSITIONS

import re
import hashlib

# =========================
# User settings
# =========================
SAVE_DIR = "./rec_eventcam"  # イベントカメラRAW/同期ログ/軌道ログの保存先

# Event camera settings
EVENTCAM_SERIAL = ""  # 空文字なら最初に見つかったイベントカメラを開く
EVENTCAM_LIVE_DELTA_T_US = 10_000  # 撮影スレッドがイベントを排出する時間幅。RAW自体は全イベントを保存する
# EVENTCAM_POSTPROCESS_FPS = 2000.0  # イベントを何fps相当で画像化するか
# EVENTCAM_POSTPROCESS_VIDEO_FPS = 50.0  # AVIに書き込む再生fps。50ならスロー再生になる
# EVENTCAM_POSTPROCESS_VIDEO_FPS = 2000.0  # AVIに書き込む再生fps。等速確認向け
# EVENTCAM_POSTPROCESS_ACCUMULATION_US = 200  # 1枚の画像に積算する時間幅。短いほど軌跡の残像が減る
EVENTCAM_POSTPROCESS_FPS = 10000.0
EVENTCAM_POSTPROCESS_VIDEO_FPS = 120.0
EVENTCAM_POSTPROCESS_ACCUMULATION_US = int(round(1_000_000 / EVENTCAM_POSTPROCESS_FPS))
EVENTCAM_POSTPROCESS_OUTPUT_DIR = "./proc_eventcam"
EVENTCAM_POSTPROCESS_WRITE_VIDEO = False  # TrueにするとRAW→AVI確認動画も自動生成する
EVENTCAM_SYNC_LED_ROI = "0,0,400,160"  # LED同期マーカーROI。必要なら右上/左下などへ変更する
EVENTCAM_SYNC_LED_BIN_US = 100
EVENTCAM_SYNC_LED_THRESHOLD = 0  # 0なら自動推定
EVENTCAM_SYNC_LED_PREVIEW_PNG = True  # TrueならLED同期検出時点の確認PNGを保存する
EVENTCAM_SYNC_LED_PREVIEW_WINDOW_US = 1000  # PNG化する同期ピーク周辺の時間幅
EVENTCAM_SYNC_PRE_ROLL_US = 0
EVENTCAM_SYNC_DURATION_US = 0  # 0なら同期点からRAW末尾まで
EVENTCAM_MASK_LED_ROI = True
EVENTCAM_EXPORT_FILTERED_EVENTS_NPZ = True
EVENTCAM_AUTO_TRACK_NPZ = True
EVENTCAM_TRACK_OUTPUT_SUBDIR = "event_tracking_npz_auto"
# EVENTCAM_TRACK_WINDOW_US = 500
# EVENTCAM_TRACK_WINDOW_US = 300
EVENTCAM_TRACK_WINDOW_US = 200
# EVENTCAM_TRACK_WINDOW_US = 100
EVENTCAM_TRACK_HOP_US = 100
EVENTCAM_TRACK_DT_US = 100
EVENTCAM_TRACK_T_END_SEC = 0.0
EVENTCAM_TRACK_ROI = "0,0,1280,720"
EVENTCAM_TRACK_MIN_EVENTS = 20
EVENTCAM_TRACK_THRESHOLD_COUNT = 1
EVENTCAM_TRACK_MIN_AREA = 5
EVENTCAM_TRACK_MIN_MASS = 30
# EVENTCAM_TRACK_METHODS = ("event_weighted",)  # 比較時は ("event_weighted", "component_center", "hough_circle")
EVENTCAM_TRACK_METHODS = (
    "event_weighted",
    # "component_center",
    # "hough_circle",
    # "distance_transform_center",
    # "min_enclosing_circle",
    # "fit_ellipse",
    # "ransac_circle",
)
EVENTCAM_TRACK_HOUGH_DP = 1.2
EVENTCAM_TRACK_HOUGH_MIN_DIST_PX = 8.0
EVENTCAM_TRACK_HOUGH_PARAM1 = 100.0
EVENTCAM_TRACK_HOUGH_PARAM2 = 8.0
EVENTCAM_TRACK_HOUGH_MIN_RADIUS_PX = 2
EVENTCAM_TRACK_HOUGH_MAX_RADIUS_PX = 0
EVENTCAM_TRACK_RANSAC_ITERATIONS = 80
EVENTCAM_TRACK_RANSAC_RESIDUAL_PX = 2.0
####PX_PER_MM
EVENTCAM_TRACK_SCALE_PX_PER_MM = 8.2031
EVENTCAM_TRACK_CENTER_MODE = "first"
EVENTCAM_TRACK_Y_INVERT = True
EVENTCAM_LOG_QUEUE_MAX = 200_000  # 同期ログ行のキュー上限
EVENTCAM_LOG_DROP_OLD_WHEN_FULL = True
EVENTCAM_PREVIEW_BEFORE_RUN = True  # 撮影直前だけプレビューを開く
EVENTCAM_PREVIEW_WINDOW_NAME = "EVENTCAM preview"
EVENTCAM_PREVIEW_FPS = 25.0
EVENTCAM_PREVIEW_ACCUMULATION_US = 10_000
EVENTCAM_STOP_RAW_TIMEOUT_SEC = 5.0
EVENTCAM_LOGGER_STOP_TIMEOUT_SEC = 2.0
EVENTCAM_CLOSE_JOIN_TIMEOUT_SEC = 3.0
EVENTCAM_RELEASE_SETTLE_SEC = 1.0
EVENTCAM_OPEN_RETRIES = 5
EVENTCAM_OPEN_RETRY_DELAY_SEC = 1.0
EVENTCAM_ENABLE_TRIGGER_IN = True  # 外部TTLトリガをイベントストリーム内の時刻付きイベントとして記録する
EVENTCAM_TRIGGER_CHANNEL_NAMES = ("MAIN",)  # SilkyEvCam HD / IMX636ではまずMAINを使う

# Hologram preparation timing. Large jobs report every 10,000 points.
# Jobs with 10,000 points or fewer omit the per-point progress output.
PROFILE_HOLOGRAM_COMPUTE = True
HOLOGRAM_PROGRESS_INTERVAL = 10_000
HOLOGRAM_PROGRESS_MIN_POINTS = 10_000

# 同期/トリガ確認用に取り出すトランスデューサ。軌道送信前後の静止・移動中はミュートする。
SYNC_TRANSDUCER_INDEX = 303

# The PAT/OpenMPD update clock is based on 40 kHz update slots.  Requested
# frame rates are realized as 40000 / integer_divider, so only exact divisors
# avoid time-scale drift between the PAT and ideal_log.csv.
PAT_UPDATE_BASE_HZ = 40_000
PAT_MIN_FRAME_RATE_HZ = 157

# Static transfer before/after each trajectory.  Keep this independent from
# trajectory n_steps so large n_steps do not make setup motion unnecessarily slow.
TRANSFER_STEP_MM = 0.25
TRANSFER_DWELL_SEC = 0.04

# 録画停止の安全マージン（最後のフレームを取り逃がさないため）
# POST_ROLL_SEC = 0.05
POST_ROLL_SEC = 0.4

# Event-slice timestamp CSV (sync verification)
ENABLE_TIMESTAMP_LOG = True

# Mapping from trajectory mode to a descriptive shape name.
# Used for directory organization: ./recordings/{shape}/{run_dir}/...
MODE_SHAPE_NAMES: Dict[int, str] = {
    1: "Z-axis_vibration",
    2: "elliptical_orbit",
    3: "diagonal_line",
    4: "heart_shape",
    5: "rectangular_orbit",
    6: "vertical_figure_eight",
    7: "horizontal_infinity",
    8: "single_slanted_line",
    9: "s_shaped_orbit",
    10: "X-axis_vibration",
    11: "long_random_stroke",
    12: "figure_eight_3d_circle_xy",
    13: "closed_toroidal_helix",
    14: "trefoil_knot",
    15: "lissajous_3d",
}

_INVALID_WIN_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1F]')

def sanitize_filename(name: str, max_len: int = 80) -> str:
    """Windowsでも安全＆短めのファイル名 stem を作る"""
    name = str(name)
    name = _INVALID_WIN_CHARS_RE.sub("_", name)
    name = re.sub(r"\s+", "_", name).strip(" ._")
    if not name:
        name = "run"
    if len(name) > max_len:
        h = hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]
        name = name[: max(8, max_len - 11)].rstrip("._") + "_" + h
    return name

def fmt_mm(v: float) -> str:
    # 11.0 -> 11p0  (ファイル名に '.' を残さない)
    return f"{v:.1f}".replace(".", "p")


@dataclass(frozen=True)
class PatFrameRateInfo:
    """Frame-rate validation result for PAT/OpenMPD playback."""

    requested_float_hz: float
    requested_hz: int
    divider: Optional[int]
    effective_hz: Optional[float]
    alpha: Optional[float]
    error_ms_per_s: Optional[float]
    is_integer_request: bool
    is_exact_divider: bool
    is_supported: bool
    reason: str = ""


def compute_pat_frame_rate_info(n_steps: int, rev_hz: float) -> PatFrameRateInfo:
    """Return the realizable PAT frame rate implied by ``n_steps * rev_hz``.

    OpenMPD realizes update rates as ``PAT_UPDATE_BASE_HZ / integer_divider``.
    If the requested value is not an exact divisor, the PAT can run at a
    different time scale than ideal_log.csv unless the condition is rejected.
    """
    requested_float = float(n_steps) * float(rev_hz)
    requested_hz = int(round(requested_float))
    is_integer = math.isclose(requested_float, requested_hz, rel_tol=0.0, abs_tol=1e-9)
    if n_steps <= 0 or rev_hz <= 0:
        return PatFrameRateInfo(
            requested_float,
            requested_hz,
            None,
            None,
            None,
            None,
            is_integer,
            False,
            False,
            "n_steps and frequency must be positive",
        )
    if not is_integer:
        return PatFrameRateInfo(
            requested_float,
            requested_hz,
            None,
            None,
            None,
            None,
            False,
            False,
            False,
            "n_steps * frequency must be an integer Hz value",
        )
    if requested_hz < PAT_MIN_FRAME_RATE_HZ:
        return PatFrameRateInfo(
            requested_float,
            requested_hz,
            None,
            None,
            None,
            None,
            True,
            False,
            False,
            f"requested fps is below PAT/OpenMPD minimum {PAT_MIN_FRAME_RATE_HZ} Hz",
        )

    divider = PAT_UPDATE_BASE_HZ // requested_hz
    if divider <= 0:
        return PatFrameRateInfo(
            requested_float,
            requested_hz,
            None,
            None,
            None,
            None,
            True,
            False,
            False,
            f"requested fps exceeds PAT/OpenMPD base clock {PAT_UPDATE_BASE_HZ} Hz",
        )

    effective_hz = PAT_UPDATE_BASE_HZ / float(divider)
    alpha = effective_hz / float(requested_hz)
    error_ms_per_s = (alpha - 1.0) * 1000.0
    is_exact = (PAT_UPDATE_BASE_HZ % requested_hz) == 0
    reason = "" if is_exact else (
        f"{requested_hz} Hz is not an exact {PAT_UPDATE_BASE_HZ} Hz divider; "
        f"PAT would run at {effective_hz:.6f} Hz "
        f"(alpha={alpha:.6f}, error={error_ms_per_s:.3f} ms/s)"
    )
    return PatFrameRateInfo(
        requested_float,
        requested_hz,
        divider,
        effective_hz,
        alpha,
        error_ms_per_s,
        True,
        is_exact,
        is_exact,
        reason,
    )


def describe_pat_frame_rate_info(info: PatFrameRateInfo) -> str:
    if info.is_supported:
        return (
            f"requested={info.requested_hz} Hz, divider={info.divider}, "
            f"effective={info.effective_hz:.6f} Hz"
        )
    return info.reason or f"unsupported requested fps: {info.requested_float_hz:.6f} Hz"


def _csv_scalar(value: Any) -> Any:
    """Convert numpy scalars to plain Python values before CSV/JSON output."""
    try:
        return value.item()
    except Exception:
        return value


def get_param(params: Dict[str, Any], *names: str, default: float = 0.0) -> float:
    normalized = {str(k).strip().lower().replace("_", "").replace(" ", ""): v for k, v in params.items()}
    for name in names:
        key = str(name).strip().lower().replace("_", "").replace(" ", "")
        if key in normalized:
            try:
                return float(normalized[key])
            except Exception:
                return float(default)
    return float(default)


def smoothstep(value: float) -> float:
    value = max(0.0, min(1.0, float(value)))
    return value * value * (3.0 - 2.0 * value)


def interp_random_waypoint(t: float, duration_sec: float, values: List[float]) -> float:
    if not values:
        return 0.0
    if len(values) == 1 or duration_sec <= 0:
        return float(values[0])
    scaled = max(0.0, min(1.0, t / duration_sec)) * (len(values) - 1)
    idx = min(int(math.floor(scaled)), len(values) - 2)
    frac = smoothstep(scaled - idx)
    return float(values[idx]) * (1.0 - frac) + float(values[idx + 1]) * frac


def trajectory_stats(
    positions: List[Tuple[float, float, float]],
    sample_hz: float,
) -> Dict[str, float]:
    if not positions:
        return {}
    xs = [p[0] * 1e3 for p in positions]
    zs = [p[2] * 1e3 for p in positions]
    dt = 1.0 / float(sample_hz) if sample_hz > 0 else 0.0
    speeds: List[float] = []
    accels: List[float] = []
    for i in range(1, len(positions)):
        dx = (positions[i][0] - positions[i - 1][0]) * 1e3
        dz = (positions[i][2] - positions[i - 1][2]) * 1e3
        speeds.append(math.hypot(dx, dz) / dt if dt > 0 else 0.0)
    for i in range(1, len(speeds)):
        accels.append(abs(speeds[i] - speeds[i - 1]) / dt if dt > 0 else 0.0)
    return {
        "points": float(len(positions)),
        "duration_sec": float(len(positions) / sample_hz) if sample_hz > 0 else 0.0,
        "sample_hz": float(sample_hz),
        "x_min_mm": float(min(xs)),
        "x_max_mm": float(max(xs)),
        "z_min_mm": float(min(zs)),
        "z_max_mm": float(max(zs)),
        "max_speed_mm_s": float(max(speeds) if speeds else 0.0),
        "max_accel_mm_s2": float(max(accels) if accels else 0.0),
    }


def generate_long_random_stroke_positions(
    *,
    params: Dict[str, Any],
    x_center: float,
    y_center: float,
    z_center: float,
) -> Tuple[List[Tuple[float, float, float]], Dict[str, float]]:
    duration_sec = max(0.1, get_param(params, "duration_sec", "duration", default=8.0))
    sample_hz = max(1.0, get_param(params, "sample_hz", "fps", "pat_fps", default=1000.0))
    n_points = max(2, int(round(duration_sec * sample_hz)))
    duration_sec = n_points / sample_hz
    x_limit_mm = max(0.0, get_param(params, "x_limit_mm", "X_limit(mm)", "X(mm)", "x", default=20.0))
    z_limit_mm = max(0.0, get_param(params, "z_limit_mm", "Z_limit(mm)", "Z(mm)", "z", default=20.0))
    f_min_hz = max(0.01, get_param(params, "f_min_hz", "min_freq_hz", default=0.2))
    f_max_hz = max(f_min_hz, get_param(params, "f_max_hz", "max_freq_hz", default=12.0))
    components = max(1, int(round(get_param(params, "components", default=9.0))))
    chirps = max(0, int(round(get_param(params, "chirps", default=3.0))))
    waypoint_count = max(2, int(round(get_param(params, "waypoints", default=9.0))))
    drift_weight = max(0.0, get_param(params, "drift_weight", default=0.35))
    max_speed = get_param(params, "max_speed_mm_s", default=0.0)
    max_accel = get_param(params, "max_accel_mm_s2", default=0.0)
    seed = int(round(get_param(params, "seed", default=1.0)))
    rng = random.Random(seed)

    def make_axis(limit_mm: float) -> List[float]:
        if limit_mm <= 0:
            return [0.0 for _ in range(n_points)]
        comps = []
        for _ in range(components):
            freq = math.exp(rng.uniform(math.log(f_min_hz), math.log(f_max_hz)))
            amp = rng.uniform(0.35, 1.0) / math.sqrt(freq)
            phase = rng.uniform(0.0, 2.0 * math.pi)
            comps.append((amp, freq, phase))
        chirp_terms = []
        for _ in range(chirps):
            f0 = rng.uniform(f_min_hz, max(f_min_hz, f_max_hz * 0.35))
            f1 = rng.uniform(max(f0, f_max_hz * 0.45), f_max_hz)
            amp = rng.uniform(0.15, 0.5)
            phase = rng.uniform(0.0, 2.0 * math.pi)
            chirp_terms.append((amp, f0, f1, phase))
        waypoints = [rng.uniform(-1.0, 1.0) for _ in range(waypoint_count)]
        values: List[float] = []
        for i in range(n_points):
            t = i / sample_hz
            v = 0.0
            for amp, freq, phase in comps:
                v += amp * math.sin(2.0 * math.pi * freq * t + phase)
            for amp, f0, f1, phase in chirp_terms:
                k = (f1 - f0) / duration_sec
                v += amp * math.sin(2.0 * math.pi * (f0 * t + 0.5 * k * t * t) + phase)
            v += drift_weight * interp_random_waypoint(t, duration_sec, waypoints)
            values.append(v)
        mean = sum(values) / len(values)
        values = [v - mean for v in values]
        max_abs = max(max(abs(v) for v in values), 1e-9)
        return [v / max_abs * limit_mm for v in values]

    x_mm = make_axis(x_limit_mm)
    z_mm = make_axis(z_limit_mm)
    positions = [
        (x_center + x_mm[i] * 1e-3, y_center, z_center + z_mm[i] * 1e-3)
        for i in range(n_points)
    ]

    stats = trajectory_stats(positions, sample_hz)
    scale = 1.0
    if max_speed > 0 and stats.get("max_speed_mm_s", 0.0) > max_speed:
        scale = min(scale, max_speed / max(stats["max_speed_mm_s"], 1e-9))
    if max_accel > 0 and stats.get("max_accel_mm_s2", 0.0) > max_accel:
        scale = min(scale, max_accel / max(stats["max_accel_mm_s2"], 1e-9))
    if scale < 1.0:
        positions = [
            (
                x_center + (p[0] - x_center) * scale,
                y_center,
                z_center + (p[2] - z_center) * scale,
            )
            for p in positions
        ]
        stats = trajectory_stats(positions, sample_hz)
    stats.update(
        {
            "seed": float(seed),
            "x_limit_mm": float(x_limit_mm),
            "z_limit_mm": float(z_limit_mm),
            "f_min_hz": float(f_min_hz),
            "f_max_hz": float(f_max_hz),
            "components": float(components),
            "chirps": float(chirps),
            "waypoints": float(waypoint_count),
            "amplitude_scale_applied": float(scale),
        }
    )
    return positions, stats


def _write_structured_array_csv(path: str, arr: Any) -> int:
    """Write a Metavision structured numpy array such as EventExtTrigger to CSV."""
    names = list(getattr(getattr(arr, "dtype", None), "names", None) or [])
    count = int(getattr(arr, "size", 0) or 0)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["index"] + names)
        if not names or arr is None:
            return 0
        for idx, row in enumerate(arr):
            writer.writerow([idx] + [_csv_scalar(row[name]) for name in names])
    return count


def postprocess_output_dir_for_raw(output_root: str, raw_path: Path, payload: Optional[Dict[str, Any]] = None) -> Path:
    root = Path(output_root)
    try:
        rel_parent = raw_path.parent.resolve().relative_to(Path(SAVE_DIR).resolve())
        return root / rel_parent
    except Exception:
        pass

    extra_meta = (payload or {}).get("extra_meta", {})
    if isinstance(extra_meta, dict):
        shape_name = str(extra_meta.get("shape_name") or "").strip()
        run_dir = str(extra_meta.get("run_dir") or "").strip()
        if shape_name and run_dir:
            return root / sanitize_filename(shape_name, max_len=60) / sanitize_filename(Path(run_dir).name, max_len=80)

    return root / raw_path.stem


_DLL_DIRECTORY_HANDLES: List[Any] = []
_DLL_DIRECTORY_PATHS: set[str] = set()


def prepend_environment_paths(variable_name: str, directories: Sequence[Path]) -> None:
    existing_parts = [part for part in os.environ.get(variable_name, "").split(os.pathsep) if part]
    seen = {os.path.normcase(os.path.abspath(part)) for part in existing_parts}
    new_parts: List[str] = []
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


def parse_roi(text: str, name: str) -> Tuple[int, int, int, int]:
    parts = [part.strip() for part in str(text).split(",") if part.strip()]
    if len(parts) != 4:
        raise ValueError(f"{name} must be x0,y0,x1,y1: {text}")
    x0, y0, x1, y1 = [int(part) for part in parts]
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"{name} must satisfy x1>x0 and y1>y0: {text}")
    return x0, y0, x1, y1


def make_roi_mask(events: Any, roi: Tuple[int, int, int, int]) -> Any:
    x0, y0, x1, y1 = roi
    return (events["x"] >= x0) & (events["x"] < x1) & (events["y"] >= y0) & (events["y"] < y1)


def remove_masked_events(events: Any, rois: Sequence[Tuple[int, int, int, int]]) -> Any:
    if not rois or int(events.size) == 0:
        return events
    import numpy as np

    keep = np.ones(events.shape[0], dtype=bool)
    for roi in rois:
        keep &= ~make_roi_mask(events, roi)
    return events[keep]


def detect_led_sync(
    EventsIterator: Any,
    raw_path: Path,
    roi: Tuple[int, int, int, int],
    bin_us: int,
    threshold: int,
    output_dir: Path,
) -> Dict[str, Any]:
    import numpy as np

    if bin_us <= 0:
        raise ValueError("sync LED bin_us must be positive.")

    iterator = EventsIterator(
        input_path=str(raw_path),
        mode="delta_t",
        delta_t=int(bin_us),
        relative_timestamps=False,
    )
    rows: List[Dict[str, int]] = []
    for bin_index, events in enumerate(iterator):
        bin_start_ts_us = int(bin_index * bin_us)
        if events.size == 0:
            roi_count = 0
            roi_first_ts_us = -1
            roi_last_ts_us = -1
        else:
            roi_events = events[make_roi_mask(events, roi)]
            roi_count = int(roi_events.size)
            roi_first_ts_us = int(roi_events["t"][0]) if roi_count else -1
            roi_last_ts_us = int(roi_events["t"][-1]) if roi_count else -1
        rows.append(
            {
                "bin_index": int(bin_index),
                "bin_start_ts_us": bin_start_ts_us,
                "roi_count": roi_count,
                "roi_first_ts_us": roi_first_ts_us,
                "roi_last_ts_us": roi_last_ts_us,
            }
        )

    if not rows:
        raise RuntimeError("No event slices were read while detecting LED sync.")

    counts = np.array([row["roi_count"] for row in rows], dtype=np.int64)
    if threshold <= 0:
        median = float(np.median(counts))
        mad = float(np.median(np.abs(counts - median)))
        threshold = int(np.ceil(median + max(10.0, 8.0 * mad)))

    peak_index = int(np.argmax(counts))
    hit_indices = np.flatnonzero(counts >= threshold)
    if hit_indices.size == 0:
        raise RuntimeError(
            "LED sync was not detected. "
            f"max_count={int(counts[peak_index])}, threshold={threshold}, "
            f"peak_time_us={rows[peak_index]['bin_start_ts_us']}."
        )

    # The marker LED is expected to stay on for a relatively long pulse.  Do
    # not use the first threshold crossing in the whole RAW file, because a
    # small early burst in the ROI can otherwise shift t=0 too early.  Instead,
    # anchor on the strongest LED peak and walk back to the first bin in the
    # contiguous above-threshold run that contains that peak.
    sync_bin_index = int(peak_index)
    while sync_bin_index > 0 and int(counts[sync_bin_index - 1]) >= int(threshold):
        sync_bin_index -= 1
    sync_row = rows[sync_bin_index]
    sync_ts_us = int(sync_row["roi_first_ts_us"] if sync_row["roi_first_ts_us"] >= 0 else sync_row["bin_start_ts_us"])
    peak_row = rows[peak_index]

    report_path = output_dir / f"{raw_path.stem}_led_sync_detection.csv"
    with report_path.open("w", newline="", encoding="utf-8") as csv_file:
        fieldnames = ["bin_index", "bin_start_ts_us", "roi_count", "roi_first_ts_us", "roi_last_ts_us"]
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return {
        "sync_ts_us": int(sync_ts_us),
        "sync_bin_index": int(sync_bin_index),
        "sync_bin_start_ts_us": int(sync_row["bin_start_ts_us"]),
        "threshold": int(threshold),
        "peak_bin_index": int(peak_index),
        "peak_bin_start_ts_us": int(peak_row["bin_start_ts_us"]),
        "peak_count": int(peak_row["roi_count"]),
        "report_path": str(report_path.resolve()),
    }


def write_led_sync_preview_png(
    EventsIterator: Any,
    raw_path: Path,
    output_dir: Path,
    roi: Tuple[int, int, int, int],
    sync_result: Dict[str, Any],
    window_us: int,
) -> str:
    """Save a full-sensor PNG around the detected LED sync peak."""
    import numpy as np

    window_us = max(1, int(window_us))
    peak_start_us = int(sync_result.get("peak_bin_start_ts_us", sync_result.get("sync_ts_us", 0)) or 0)
    # EventsIterator requires start_ts to be a multiple of delta_t.  Align the
    # preview window so it always contains the detected peak bin.
    start_ts_us = max(0, peak_start_us - (peak_start_us % window_us))

    iterator = EventsIterator(
        input_path=str(raw_path),
        start_ts=int(start_ts_us),
        mode="delta_t",
        delta_t=int(window_us),
        max_duration=int(window_us),
        relative_timestamps=False,
    )
    height, width = iterator.get_size()
    events = None
    for chunk in iterator:
        events = chunk
        break
    if events is None:
        events = np.empty(0, dtype=[("x", "<u2"), ("y", "<u2"), ("p", "u1"), ("t", "<i8")])

    frame = np.zeros((int(height), int(width), 3), dtype=np.uint8)
    if int(events.size) > 0:
        x = np.asarray(events["x"], dtype=np.int64)
        y = np.asarray(events["y"], dtype=np.int64)
        p = np.asarray(events["p"], dtype=np.int64)
        inside = (x >= 0) & (x < int(width)) & (y >= 0) & (y < int(height))
        x = x[inside]
        y = y[inside]
        p = p[inside]
        frame[y[p <= 0], x[p <= 0]] = (180, 80, 40)
        frame[y[p > 0], x[p > 0]] = (230, 230, 230)

    x0, y0, x1, y1 = roi
    x0 = max(0, min(int(width) - 1, int(x0)))
    y0 = max(0, min(int(height) - 1, int(y0)))
    x1 = max(0, min(int(width), int(x1)))
    y1 = max(0, min(int(height), int(y1)))
    cv2.rectangle(frame, (x0, y0), (max(x0, x1 - 1), max(y0, y1 - 1)), (0, 0, 255), 3)

    roi_count = 0
    led_bbox_text = "led_bbox=none"
    if int(events.size) > 0:
        roi_mask = make_roi_mask(events, roi)
        roi_count = int(np.count_nonzero(roi_mask))
        if roi_count > 0:
            roi_events = events[roi_mask]
            led_x0 = int(np.min(roi_events["x"]))
            led_y0 = int(np.min(roi_events["y"]))
            led_x1 = int(np.max(roi_events["x"])) + 1
            led_y1 = int(np.max(roi_events["y"])) + 1
            cv2.rectangle(
                frame,
                (led_x0, led_y0),
                (max(led_x0, led_x1 - 1), max(led_y0, led_y1 - 1)),
                (0, 255, 0),
                2,
            )
            led_bbox_text = f"led_bbox=({led_x0},{led_y0},{led_x1},{led_y1})"
    text_lines = [
        f"sync_ts_us={int(sync_result.get('sync_ts_us', -1))}",
        f"threshold={int(sync_result.get('threshold', -1))} peak_count={int(sync_result.get('peak_count', -1))}",
        f"preview_start_us={start_ts_us} window_us={window_us} roi_count={roi_count}",
        f"roi=({roi[0]},{roi[1]},{roi[2]},{roi[3]})",
        led_bbox_text,
    ]
    for idx, line in enumerate(text_lines):
        y_text = 28 + idx * 24
        cv2.putText(frame, line, (12, y_text), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(frame, line, (12, y_text), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 1, cv2.LINE_AA)

    png_path = output_dir / f"{raw_path.stem}_led_sync_preview.png"
    if not cv2.imwrite(str(png_path), frame):
        raise RuntimeError(f"Could not write LED sync preview PNG: {png_path}")
    return str(png_path.resolve())


def run_embedded_eventcam_postprocess(
    *,
    summary_json: str,
    output_dir: str,
    fps: float,
    video_fps: float,
    accumulation_us: int,
    sync_led_roi: str,
    sync_led_bin_us: int,
    sync_led_threshold: int,
    sync_pre_roll_us: int,
    sync_duration_us: int,
    mask_led_roi: bool,
    export_filtered_events_npz: bool,
    write_video: bool,
    auto_track_npz: bool,
) -> Dict[str, str]:
    import numpy as np
    from metavision_core.event_io import EventsIterator
    if write_video:
        from metavision_sdk_core import PeriodicFrameGenerationAlgorithm, ColorPalette

    summary_path = Path(summary_json).resolve()
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    raw_path = Path(payload["raw_path"]).resolve()
    if not raw_path.exists():
        raise FileNotFoundError(f"RAW file not found: {raw_path}")

    frame_period_us = int(round(1_000_000 / float(fps)))
    out_dir = postprocess_output_dir_for_raw(output_dir, raw_path, payload)
    out_dir.mkdir(parents=True, exist_ok=True)

    led_roi = parse_roi(sync_led_roi, "EVENTCAM_SYNC_LED_ROI") if sync_led_roi else None
    mask_rois: List[Tuple[int, int, int, int]] = []
    sync_result: Optional[Dict[str, Any]] = None
    sync_ts_us: Optional[int] = None
    if led_roi is not None:
        sync_result = detect_led_sync(
            EventsIterator=EventsIterator,
            raw_path=raw_path,
            roi=led_roi,
            bin_us=int(sync_led_bin_us),
            threshold=int(sync_led_threshold),
            output_dir=out_dir,
        )
        sync_ts_us = int(sync_result["sync_ts_us"])
        if mask_led_roi:
            mask_rois.append(led_roi)
        if EVENTCAM_SYNC_LED_PREVIEW_PNG:
            try:
                preview_png = write_led_sync_preview_png(
                    EventsIterator=EventsIterator,
                    raw_path=raw_path,
                    output_dir=out_dir,
                    roi=led_roi,
                    sync_result=sync_result,
                    window_us=int(EVENTCAM_SYNC_LED_PREVIEW_WINDOW_US),
                )
                sync_result["preview_png"] = preview_png
            except Exception as exc:
                print(f"[EVENTCAM][WARN] LED sync preview PNG failed: {exc}")
        print(
            "[EVENTCAM] LED sync detected: "
            f"sync_ts_us={sync_ts_us}, threshold={sync_result['threshold']}, "
            f"peak_count={sync_result['peak_count']}"
        )
        if sync_result.get("preview_png"):
            print(f"[EVENTCAM] LED sync preview PNG: {sync_result['preview_png']}")

    output_start_ts_us = max(0, int(sync_ts_us or 0) - max(0, int(sync_pre_roll_us)))
    output_end_ts_us = None
    if int(sync_duration_us) > 0:
        output_end_ts_us = output_start_ts_us + int(sync_duration_us)
    iterator_start_ts_us = output_start_ts_us - (output_start_ts_us % frame_period_us)
    iterator_max_duration = None
    if output_end_ts_us is not None:
        iterator_max_duration = max(frame_period_us, output_end_ts_us - iterator_start_ts_us)

    iterator = EventsIterator(
        input_path=str(raw_path),
        start_ts=iterator_start_ts_us,
        mode="delta_t",
        delta_t=frame_period_us,
        max_duration=iterator_max_duration,
        relative_timestamps=False,
    )
    height, width = iterator.get_size()

    sync_tag = "_syncled" if led_roi is not None else ""
    mask_tag = "_masked" if mask_rois else ""
    acc_tag = "" if int(accumulation_us) == 0 else f"_acc{int(accumulation_us)}us"
    video_path = out_dir / f"{raw_path.stem}_clip_pat.avi" if write_video else None
    # Keep this filename short: long trajectory stems plus render/playback tags
    # can exceed the classic Windows MAX_PATH limit and prevent NPZ export.
    csv_path = out_dir / f"{raw_path.stem}_timestamps.csv"
    sync_json_path = out_dir / f"{raw_path.stem}_led_sync.json" if sync_result else None
    npz_path = out_dir / f"{raw_path.stem}{sync_tag}{mask_tag}_events.npz" if export_filtered_events_npz else None

    video_writer = None
    if write_video:
        fourcc = cv2.VideoWriter_fourcc(*"MJPG")
        video_writer = cv2.VideoWriter(str(video_path), fourcc, float(video_fps), (int(width), int(height)))
        if not video_writer.isOpened():
            raise RuntimeError(f"Could not open video writer: {video_path}")

    filtered_chunks: List[Any] = []
    with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
        fieldnames = [
            "frame_index",
            "frame_ts_us",
            "source_frame_ts_us",
            "sync_ts_us",
            "slice_start_ts_us",
            "slice_end_ts_us",
            "event_count",
            "on_count",
            "off_count",
        ]
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        out_idx = 0
        for source_idx, events in enumerate(iterator):
            source_ts = iterator_start_ts_us + source_idx * frame_period_us
            if events.size:
                keep_time = events["t"] >= output_start_ts_us
                if output_end_ts_us is not None:
                    keep_time &= events["t"] < output_end_ts_us
                events = events[keep_time]
            events = remove_masked_events(events, mask_rois)
            if source_ts + frame_period_us <= output_start_ts_us:
                continue
            if output_end_ts_us is not None and source_ts >= output_end_ts_us:
                break
            if npz_path is not None and events.size:
                filtered_chunks.append(events.copy())

            frame_ts = max(0, source_ts - output_start_ts_us)
            if video_writer is not None:
                frame = np.empty((int(height), int(width), 3), dtype=np.uint8)
                PeriodicFrameGenerationAlgorithm.generate_frame(
                    events,
                    frame,
                    int(accumulation_us),
                    ColorPalette.Dark,
                )
                video_writer.write(frame)
            count = int(events.size)
            writer.writerow(
                {
                    "frame_index": out_idx,
                    "frame_ts_us": frame_ts,
                    "source_frame_ts_us": source_ts,
                    "sync_ts_us": "" if sync_ts_us is None else sync_ts_us,
                    "slice_start_ts_us": frame_ts if count == 0 else int(events["t"][0]),
                    "slice_end_ts_us": frame_ts if count == 0 else int(events["t"][-1]),
                    "event_count": count,
                    "on_count": int(np.count_nonzero(events["p"])) if count else 0,
                    "off_count": count - int(np.count_nonzero(events["p"])) if count else 0,
                }
            )
            if write_video and out_idx % 50 == 0:
                print(f"[EVENTCAM] Rendered frame {out_idx}")
            out_idx += 1

    if video_writer is not None:
        video_writer.release()

    if sync_json_path is not None:
        sync_payload = {
            "raw_path": str(raw_path),
            "led_roi": list(led_roi) if led_roi else None,
            "mask_rois": [list(roi) for roi in mask_rois],
            "sync_result": sync_result,
            "output_start_ts_us": output_start_ts_us,
            "output_end_ts_us": output_end_ts_us,
        }
        sync_json_path.write_text(json.dumps(sync_payload, indent=2), encoding="utf-8")

    if npz_path is not None:
        if filtered_chunks:
            filtered_events = np.concatenate(filtered_chunks)
        else:
            filtered_events = np.empty(0, dtype=[("x", "<u2"), ("y", "<u2"), ("p", "u1"), ("t", "<i8")])
        np.savez_compressed(
            npz_path,
            events=filtered_events,
            sync_ts_us=-1 if sync_ts_us is None else int(sync_ts_us),
            output_start_ts_us=int(output_start_ts_us),
            output_end_ts_us=-1 if output_end_ts_us is None else int(output_end_ts_us),
            mask_rois=np.array(mask_rois, dtype=np.int32),
        )

    tracking_dir = ""
    if auto_track_npz and npz_path is not None and npz_path.exists():
        tracking_methods = tuple(str(method).strip() for method in EVENTCAM_TRACK_METHODS if str(method).strip())
        if not tracking_methods:
            tracking_methods = ("event_weighted",)
        valid_tracking_methods = {
            "event_weighted",
            "component_center",
            "hough_circle",
            "distance_transform_center",
            "min_enclosing_circle",
            "fit_ellipse",
            "ransac_circle",
        }
        invalid_methods = [method for method in tracking_methods if method not in valid_tracking_methods]
        if invalid_methods:
            raise ValueError(f"Invalid EVENTCAM_TRACK_METHODS: {invalid_methods}")
        tracking_root = out_dir / EVENTCAM_TRACK_OUTPUT_SUBDIR
        tracking_dir = str(tracking_root.resolve())
        extra_meta = payload.get("extra_meta", {})
        ideal_log_path = None
        if isinstance(extra_meta, dict) and extra_meta.get("ideal_log"):
            candidate = Path(str(extra_meta.get("ideal_log"))).resolve()
            if candidate.exists():
                ideal_log_path = candidate
            else:
                print(f"[EVENTCAM][WARN] ideal_log is listed but not found: {candidate}")
        for method in tracking_methods:
            method_dir = tracking_root if len(tracking_methods) == 1 else tracking_root / method
            cmd = [
                sys.executable,
                str((Path(__file__).resolve().parent / "eventcam_npz_track.py").resolve()),
                str(npz_path.resolve()),
                "--output-dir",
                str(method_dir.resolve()),
                "--tracking-method",
                method,
                "--window-us",
                str(int(EVENTCAM_TRACK_WINDOW_US)),
                "--hop-us",
                str(int(EVENTCAM_TRACK_HOP_US)),
                "--dt-us",
                str(int(EVENTCAM_TRACK_DT_US)),
                "--t-end-sec",
                f"{float(EVENTCAM_TRACK_T_END_SEC):g}",
                "--roi",
                str(EVENTCAM_TRACK_ROI),
                "--min-events",
                str(int(EVENTCAM_TRACK_MIN_EVENTS)),
                "--threshold-count",
                str(int(EVENTCAM_TRACK_THRESHOLD_COUNT)),
                "--min-area",
                str(int(EVENTCAM_TRACK_MIN_AREA)),
                "--min-mass",
                str(int(EVENTCAM_TRACK_MIN_MASS)),
                "--hough-dp",
                f"{float(EVENTCAM_TRACK_HOUGH_DP):g}",
                "--hough-min-dist-px",
                f"{float(EVENTCAM_TRACK_HOUGH_MIN_DIST_PX):g}",
                "--hough-param1",
                f"{float(EVENTCAM_TRACK_HOUGH_PARAM1):g}",
                "--hough-param2",
                f"{float(EVENTCAM_TRACK_HOUGH_PARAM2):g}",
                "--hough-min-radius-px",
                str(int(EVENTCAM_TRACK_HOUGH_MIN_RADIUS_PX)),
                "--hough-max-radius-px",
                str(int(EVENTCAM_TRACK_HOUGH_MAX_RADIUS_PX)),
                "--ransac-iterations",
                str(int(EVENTCAM_TRACK_RANSAC_ITERATIONS)),
                "--ransac-residual-px",
                f"{float(EVENTCAM_TRACK_RANSAC_RESIDUAL_PX):g}",
                "--scale-px-per-mm",
                f"{float(EVENTCAM_TRACK_SCALE_PX_PER_MM):g}",
                "--center-mode",
                str(EVENTCAM_TRACK_CENTER_MODE),
            ]
            if EVENTCAM_TRACK_Y_INVERT:
                cmd.append("--y-invert")
            if ideal_log_path is not None:
                cmd.extend(["--ideal-log", str(ideal_log_path)])
            print(f"[EVENTCAM] Starting NPZ particle tracking ({method})...")
            print("[EVENTCAM] " + " ".join(f'"{part}"' if " " in part else part for part in cmd))
            subprocess.run(cmd, check=True)

    result = {
        "avi": "" if video_path is None else str(video_path.resolve()),
        "csv": str(csv_path.resolve()),
        "sync_json": "" if sync_json_path is None else str(sync_json_path.resolve()),
        "events_npz": "" if npz_path is None else str(npz_path.resolve()),
        "tracking_dir": tracking_dir,
    }
    if video_path is not None:
        print(f"[EVENTCAM] AVI finished: {result['avi']}")
    if tracking_dir:
        print(f"[EVENTCAM] Tracking finished: {tracking_dir}")
    print(f"[EVENTCAM] Postprocess finished: NPZ={result['events_npz'] or 'off'}")
    return result


def _enable_event_camera_trigger_in(
    device: Any,
    metavision_hal: Any,
    channel_names: Optional[Sequence[str]],
) -> List[Dict[str, Any]]:
    """Enable Metavision Trigger In channels and return serializable metadata."""
    trigger_in = device.get_i_trigger_in() if hasattr(device, "get_i_trigger_in") else None
    if trigger_in is None:
        print("[EVENTCAM][WARN] Trigger In facility is not available on this device.")
        return []

    try:
        available = dict(trigger_in.get_available_channels())
    except Exception:
        available = {}

    channel_cls = getattr(getattr(metavision_hal, "I_TriggerIn", None), "Channel", None)
    requested = [str(ch).strip().upper() for ch in (channel_names or ()) if str(ch).strip()]
    channels: List[Any] = []

    for name in requested:
        ch = getattr(channel_cls, name, None) if channel_cls is not None else None
        if ch is not None:
            channels.append(ch)

    if not channels and available:
        channels = list(available.keys())

    enabled: List[Dict[str, Any]] = []
    for ch in channels:
        if available and ch not in available:
            continue
        try:
            ok = bool(trigger_in.enable(ch))
        except Exception as exc:
            print(f"[EVENTCAM][WARN] Failed to enable Trigger In channel {ch}: {exc}")
            ok = False
        event_id = available.get(ch, None) if available else None
        enabled.append(
            {
                "channel": str(ch),
                "event_id": _csv_scalar(event_id),
                "enabled": bool(ok),
            }
        )

    if enabled:
        text = ", ".join(e["channel"] for e in enabled if e["enabled"])
        print(f"[EVENTCAM] Trigger In enabled: {text if text else 'none'}")
    else:
        print("[EVENTCAM][WARN] No Trigger In channel was enabled.")
    return enabled

# =========================
# Event camera recorder
# =========================


@dataclass
class EventCameraRecordingSummary:
    """One event-camera recording result."""

    raw_path: str
    summary_json: str
    timestamps_csv: Optional[str]
    trigger_events_csv: Optional[str]
    rec_start_pc_ns: int
    rec_stop_pc_ns: int
    rec_start_wall_ns: int
    rec_stop_wall_ns: int
    sensor_width: int
    sensor_height: int
    total_slices: int
    total_events: int
    first_event_ts_us: Optional[int]
    last_event_ts_us: Optional[int]
    dropped_log_rows: int
    trigger_event_count: int = 0
    trigger_channels_enabled: Optional[List[Dict[str, Any]]] = None
    raw_stop_ok: bool = True
    postprocess_command: Optional[List[str]] = None


class EventCameraRecorder:
    """Metavision/OpenEB event camera RAW recorder with a background drain thread."""

    def __init__(
        self,
        *,
        save_dir: str,
        serial: str = "",
        live_delta_t_us: int = EVENTCAM_LIVE_DELTA_T_US,
        postprocess_fps: float = EVENTCAM_POSTPROCESS_FPS,
        postprocess_video_fps: float = EVENTCAM_POSTPROCESS_VIDEO_FPS,
        postprocess_accumulation_us: int = EVENTCAM_POSTPROCESS_ACCUMULATION_US,
        postprocess_output_dir: str = EVENTCAM_POSTPROCESS_OUTPUT_DIR,
        postprocess_write_video: bool = EVENTCAM_POSTPROCESS_WRITE_VIDEO,
        sync_led_roi: str = EVENTCAM_SYNC_LED_ROI,
        sync_led_bin_us: int = EVENTCAM_SYNC_LED_BIN_US,
        sync_led_threshold: int = EVENTCAM_SYNC_LED_THRESHOLD,
        sync_pre_roll_us: int = EVENTCAM_SYNC_PRE_ROLL_US,
        sync_duration_us: int = EVENTCAM_SYNC_DURATION_US,
        mask_led_roi: bool = EVENTCAM_MASK_LED_ROI,
        export_filtered_events_npz: bool = EVENTCAM_EXPORT_FILTERED_EVENTS_NPZ,
        auto_track_npz: bool = EVENTCAM_AUTO_TRACK_NPZ,
        log_queue_max: int = EVENTCAM_LOG_QUEUE_MAX,
        enable_trigger_in: bool = EVENTCAM_ENABLE_TRIGGER_IN,
        trigger_channel_names: Optional[Sequence[str]] = EVENTCAM_TRIGGER_CHANNEL_NAMES,
    ) -> None:
        self.save_dir = os.path.abspath(save_dir)
        self.serial = str(serial or "")
        self.live_delta_t_us = int(live_delta_t_us)
        self.postprocess_fps = float(postprocess_fps)
        self.postprocess_video_fps = float(postprocess_video_fps)
        self.postprocess_accumulation_us = int(postprocess_accumulation_us)
        self.postprocess_output_dir = str(postprocess_output_dir)
        self.postprocess_write_video = bool(postprocess_write_video)
        self.sync_led_roi = str(sync_led_roi or "")
        self.sync_led_bin_us = int(sync_led_bin_us)
        self.sync_led_threshold = int(sync_led_threshold)
        self.sync_pre_roll_us = int(sync_pre_roll_us)
        self.sync_duration_us = int(sync_duration_us)
        self.mask_led_roi = bool(mask_led_roi)
        self.export_filtered_events_npz = bool(export_filtered_events_npz)
        self.auto_track_npz = bool(auto_track_npz)
        self.log_queue_max = int(log_queue_max)
        self.enable_trigger_in = bool(enable_trigger_in)
        self.trigger_channel_names = tuple(trigger_channel_names or ())
        safe_mkdir(self.save_dir)

        self._configure_and_import_metavision()

        print("[EVENTCAM] Opening event camera...")
        self.device = None
        last_open_error: Optional[OSError] = None
        detected_text = "unknown"
        requested = self.serial if self.serial else "(first detected camera)"
        open_retries = max(1, int(EVENTCAM_OPEN_RETRIES))
        for attempt in range(1, open_retries + 1):
            try:
                self.device = self.initiate_device(path=self.serial)
                break
            except OSError as exc:
                last_open_error = exc
                try:
                    detected = self.metavision_hal.DeviceDiscovery.list()
                    detected_text = "none" if not detected else ", ".join(str(d) for d in detected)
                except Exception:
                    detected_text = "unavailable"
                if attempt >= open_retries:
                    break
                print(
                    "[EVENTCAM][WARN] Could not open camera "
                    f"(attempt {attempt}/{open_retries}): {exc}. "
                    f"Detected devices: {detected_text}. Retrying..."
                )
                gc.collect()
                time.sleep(max(0.0, float(EVENTCAM_OPEN_RETRY_DELAY_SEC)))
        if self.device is None:
            raise RuntimeError(
                "Could not open the event camera.\n"
                f"Requested device: {requested}\n"
                f"Detected devices: {detected_text}\n"
                f"Original error: {last_open_error}"
            ) from last_open_error

        self.trigger_channels_enabled: List[Dict[str, Any]] = []
        if self.enable_trigger_in:
            self.trigger_channels_enabled = _enable_event_camera_trigger_in(
                self.device,
                self.metavision_hal,
                self.trigger_channel_names,
            )

        self.events_stream = self.device.get_i_events_stream()
        if self.events_stream is None:
            raise RuntimeError("This event camera does not expose I_EventsStream; RAW logging is unavailable.")

        self.iterator = self.EventsIterator.from_device(
            device=self.device,
            mode="delta_t",
            delta_t=self.live_delta_t_us,
            max_duration=None,
            relative_timestamps=False,
        )
        height, width = self.iterator.get_size()
        self.sensor_height = int(height)
        self.sensor_width = int(width)
        print(f"[EVENTCAM] Sensor size: {self.sensor_width}x{self.sensor_height}")

        self.preview_active = threading.Event()
        self.preview_lock = threading.Lock()
        self.preview_frame = None
        self.preview_generator = self.PeriodicFrameGenerationAlgorithm(
            sensor_width=self.sensor_width,
            sensor_height=self.sensor_height,
            accumulation_time_us=int(EVENTCAM_PREVIEW_ACCUMULATION_US),
            fps=float(EVENTCAM_PREVIEW_FPS),
            palette=self.ColorPalette.Dark,
        )
        self.preview_generator.set_output_callback(self._on_preview_frame)

        self.is_saving = threading.Event()
        self.stop_evt = threading.Event()
        self.state_lock = threading.Lock()

        self.raw_path: Optional[Path] = None
        self.summary_json_path: Optional[Path] = None
        self.timestamps_csv: Optional[str] = None
        self.trigger_events_csv: Optional[str] = None
        self.extra_meta: Dict[str, Any] = {}

        self.rec_start_pc_ns = 0
        self.rec_stop_pc_ns = 0
        self.rec_start_wall_ns = 0
        self.rec_stop_wall_ns = 0
        self.total_slices = 0
        self.total_events = 0
        self.first_event_ts_us: Optional[int] = None
        self.last_event_ts_us: Optional[int] = None
        self.dropped_log_rows = 0
        self.slice_index = 0
        self.trigger_event_count = 0

        self.log_q: Optional[queue.Queue] = None
        self.log_thread: Optional[threading.Thread] = None
        self.log_fp = None
        self.log_writer = None
        self.thread_error: Optional[BaseException] = None

        self.capture_thread = threading.Thread(target=self._event_loop, name="EventCameraDrain", daemon=True)
        self.capture_thread.start()
        print("[EVENTCAM] Background drain thread started.")

    def _configure_and_import_metavision(self) -> None:
        configure_local_metavision_environment()
        from metavision_core.event_io import EventsIterator
        from metavision_core.event_io.raw_reader import initiate_device
        from metavision_sdk_core import PeriodicFrameGenerationAlgorithm, ColorPalette
        import metavision_hal

        self.EventsIterator = EventsIterator
        self.initiate_device = initiate_device
        self.PeriodicFrameGenerationAlgorithm = PeriodicFrameGenerationAlgorithm
        self.ColorPalette = ColorPalette
        self.metavision_hal = metavision_hal

    def _on_preview_frame(self, _timestamp_us: int, frame: Any) -> None:
        with self.preview_lock:
            self.preview_frame = frame.copy()

    def start_preview(self) -> None:
        with self.preview_lock:
            self.preview_frame = None
        try:
            self.preview_generator.reset()
        except Exception:
            pass
        self.preview_active.set()

    def stop_preview(self) -> None:
        self.preview_active.clear()

    def get_preview_frame(self):
        with self.preview_lock:
            return None if self.preview_frame is None else self.preview_frame.copy()

    def _reset_stats(self) -> None:
        self.total_slices = 0
        self.total_events = 0
        self.first_event_ts_us = None
        self.last_event_ts_us = None
        self.dropped_log_rows = 0
        self.slice_index = 0
        self.trigger_event_count = 0

    def _log_put_drop_old(self, item: tuple) -> None:
        if not ENABLE_TIMESTAMP_LOG or self.log_q is None:
            return
        try:
            self.log_q.put_nowait(item)
            return
        except queue.Full:
            self.dropped_log_rows += 1
            if not EVENTCAM_LOG_DROP_OLD_WHEN_FULL:
                return
            try:
                _ = self.log_q.get_nowait()
                self.log_q.task_done()
            except Exception:
                pass
            try:
                self.log_q.put_nowait(item)
            except Exception:
                return

    def _start_timestamp_logger(self, safe_base: str, ts: str) -> None:
        if not ENABLE_TIMESTAMP_LOG:
            return

        self.timestamps_csv = os.path.join(self.save_dir, f"{safe_base}_{ts}_eventcam_timestamps.csv")
        log_q: queue.Queue = queue.Queue(maxsize=self.log_queue_max)
        log_fp = open(self.timestamps_csv, "w", newline="", encoding="utf-8")
        log_writer = csv.writer(log_fp)
        self.log_q = log_q
        self.log_fp = log_fp
        self.log_writer = log_writer
        log_writer.writerow(
            [
                "kind",
                "index",
                "t_rel_sec",
                "perf_counter_ns",
                "wall_clock_ns",
                "event_start_ts_us",
                "event_end_ts_us",
                "event_count",
                "on_count",
                "off_count",
                "event",
                "note",
            ]
        )
        log_fp.flush()

        def _log_loop() -> None:
            while True:
                item = log_q.get()
                try:
                    if item is None:
                        break
                    log_writer.writerow(item)
                finally:
                    try:
                        log_q.task_done()
                    except Exception:
                        pass
            try:
                log_fp.flush()
                log_fp.close()
            except Exception:
                pass

        self.log_thread = threading.Thread(target=_log_loop, name="EventCameraTimestampLog", daemon=True)
        self.log_thread.start()

    def _stop_timestamp_logger(self) -> None:
        log_q = self.log_q
        log_thread = self.log_thread
        if log_q is None:
            return
        try:
            log_q.put(None, timeout=0.2)
        except queue.Full:
            try:
                _ = log_q.get_nowait()
                log_q.task_done()
            except Exception:
                pass
            try:
                log_q.put_nowait(None)
            except Exception:
                pass
        except Exception:
            pass
        if log_thread is not None:
            log_thread.join(timeout=float(EVENTCAM_LOGGER_STOP_TIMEOUT_SEC))
            if log_thread.is_alive():
                print("[EVENTCAM][WARN] timestamp logger did not stop within timeout.", flush=True)
        self.log_q = None
        self.log_thread = None
        self.log_fp = None
        self.log_writer = None

    def _clear_ext_trigger_buffer(self) -> bool:
        """Drop trigger events observed before the current recording starts."""
        try:
            reader = getattr(self.iterator, "reader", None)
            clear_fn = getattr(reader, "clear_ext_trigger_events", None)
            if clear_fn is None:
                return False
            clear_fn()
            return True
        except Exception as exc:
            print(f"[EVENTCAM][WARN] Could not clear external trigger buffer: {exc}")
            return False

    def _get_ext_trigger_events(self) -> Any:
        """Return trigger events loaded by the active EventsIterator."""
        try:
            return self.iterator.get_ext_trigger_events()
        except AssertionError:
            return None
        except Exception as exc:
            print(f"[EVENTCAM][WARN] Could not read external trigger events: {exc}")
            return None

    def _write_trigger_events_csv(self) -> int:
        if not self.trigger_events_csv:
            return 0
        events = self._get_ext_trigger_events()
        count = _write_structured_array_csv(self.trigger_events_csv, events)
        self.trigger_event_count = int(count)
        return int(count)

    def _event_loop(self) -> None:
        try:
            for events in self.iterator:
                if self.stop_evt.is_set():
                    break
                if self.preview_active.is_set() and not self.is_saving.is_set():
                    try:
                        self.preview_generator.process_events(events)
                    except Exception as exc:
                        print(f"[EVENTCAM][WARN] preview frame generation failed: {exc}")
                        self.preview_active.clear()
                    continue
                if not self.is_saving.is_set():
                    continue

                pc_ns = int(time.perf_counter_ns())
                wall_ns = int(time.time_ns())
                count = int(events.size)
                on_count = int((events["p"] != 0).sum()) if count else 0
                off_count = count - on_count
                first_ts = int(events["t"][0]) if count else None
                last_ts = int(events["t"][-1]) if count else None

                with self.state_lock:
                    self.total_slices += 1
                    self.total_events += count
                    if first_ts is not None and self.first_event_ts_us is None:
                        self.first_event_ts_us = first_ts
                    if last_ts is not None:
                        self.last_event_ts_us = last_ts
                    idx = self.slice_index
                    self.slice_index += 1

                t_rel = (pc_ns - int(self.rec_start_pc_ns)) * 1e-9 if self.rec_start_pc_ns else 0.0
                self._log_put_drop_old(
                    (
                        "SLICE",
                        idx,
                        f"{t_rel:.9f}",
                        pc_ns,
                        wall_ns,
                        "" if first_ts is None else first_ts,
                        "" if last_ts is None else last_ts,
                        count,
                        on_count,
                        off_count,
                        "",
                        "",
                    )
                )
        except BaseException as exc:
            self.thread_error = exc
            print(f"[EVENTCAM][ERROR] drain thread stopped: {exc}")

    def start_recording(self, safe_base: str, extra_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.is_saving.is_set():
            raise RuntimeError("Event camera recording is already active.")
        if self.thread_error is not None:
            raise RuntimeError(f"Event camera drain thread has stopped: {self.thread_error}")

        safe_base = sanitize_filename(safe_base, max_len=70)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.raw_path = Path(self.save_dir) / f"{safe_base}_{ts}.raw"
        self.summary_json_path = Path(self.save_dir) / f"{safe_base}_{ts}_eventcam_summary.json"
        self.trigger_events_csv = str(Path(self.save_dir) / f"{safe_base}_{ts}_eventcam_trigger_events.csv")
        self.extra_meta = dict(extra_meta or {})
        self._reset_stats()
        self._start_timestamp_logger(safe_base, ts)

        self.rec_start_pc_ns = int(time.perf_counter_ns())
        self.rec_start_wall_ns = int(time.time_ns())
        if self.enable_trigger_in:
            cleared = self._clear_ext_trigger_buffer()
            self.log_event("EXT_TRIGGER_BUFFER_CLEAR", pc_ns=self.rec_start_pc_ns, wall_ns=self.rec_start_wall_ns, note=f"cleared={cleared}")
        self.events_stream.log_raw_data(str(self.raw_path.resolve()))
        self.is_saving.set()
        self.log_event("CAM_REC_START", pc_ns=self.rec_start_pc_ns, wall_ns=self.rec_start_wall_ns)
        print(f"[EVENTCAM] RAW recording started: {self.raw_path}")

        return {
            "path": str(self.raw_path.resolve()),
            "raw_path": str(self.raw_path.resolve()),
            "summary_json": str(self.summary_json_path.resolve()),
            "timestamps_csv": self.timestamps_csv,
            "trigger_events_csv": self.trigger_events_csv,
        }

    def log_event(self, name: str, pc_ns: Optional[int] = None, wall_ns: Optional[int] = None, note: str = "") -> None:
        if not ENABLE_TIMESTAMP_LOG:
            return
        if pc_ns is None:
            pc_ns = int(time.perf_counter_ns())
        if wall_ns is None:
            wall_ns = int(time.time_ns())
        t_rel = (int(pc_ns) - int(self.rec_start_pc_ns)) * 1e-9 if self.rec_start_pc_ns else 0.0
        self._log_put_drop_old(
            (
                "EVENT",
                "",
                f"{t_rel:.9f}",
                int(pc_ns),
                int(wall_ns),
                "",
                "",
                "",
                "",
                "",
                str(name),
                str(note) if note else "",
            )
        )

    def _stop_raw_logging_with_timeout(self) -> bool:
        errors: List[BaseException] = []

        def _stop_raw() -> None:
            try:
                self.events_stream.stop_log_raw_data()
            except BaseException as exc:
                errors.append(exc)

        print("[EVENTCAM] Stopping RAW logger...", flush=True)
        stop_thread = threading.Thread(target=_stop_raw, name="EventCameraStopRaw", daemon=True)
        stop_thread.start()
        stop_thread.join(timeout=float(EVENTCAM_STOP_RAW_TIMEOUT_SEC))
        if stop_thread.is_alive():
            msg = (
                "stop_log_raw_data timed out. "
                "The RAW file may still be held by the SDK; restart this script before the next run."
            )
            self.thread_error = RuntimeError(msg)
            print(f"[EVENTCAM][WARN] {msg}", flush=True)
            return False
        if errors:
            print(f"[EVENTCAM][WARN] stop_log_raw_data failed: {errors[0]}", flush=True)
            return False
        print("[EVENTCAM] RAW logger stopped.", flush=True)
        return True

    def stop_recording(self) -> Optional[EventCameraRecordingSummary]:
        if not self.is_saving.is_set():
            return None

        self.rec_stop_pc_ns = int(time.perf_counter_ns())
        self.rec_stop_wall_ns = int(time.time_ns())
        self.log_event("CAM_REC_STOP", pc_ns=self.rec_stop_pc_ns, wall_ns=self.rec_stop_wall_ns)
        self.is_saving.clear()
        raw_stop_ok = self._stop_raw_logging_with_timeout()

        self._stop_timestamp_logger()
        trigger_count = self._write_trigger_events_csv() if self.enable_trigger_in else 0

        with self.state_lock:
            summary = EventCameraRecordingSummary(
                raw_path=str(self.raw_path.resolve()) if self.raw_path else "",
                summary_json=str(self.summary_json_path.resolve()) if self.summary_json_path else "",
                timestamps_csv=self.timestamps_csv,
                trigger_events_csv=self.trigger_events_csv,
                rec_start_pc_ns=int(self.rec_start_pc_ns),
                rec_stop_pc_ns=int(self.rec_stop_pc_ns),
                rec_start_wall_ns=int(self.rec_start_wall_ns),
                rec_stop_wall_ns=int(self.rec_stop_wall_ns),
                sensor_width=int(self.sensor_width),
                sensor_height=int(self.sensor_height),
                total_slices=int(self.total_slices),
                total_events=int(self.total_events),
                first_event_ts_us=self.first_event_ts_us,
                last_event_ts_us=self.last_event_ts_us,
                dropped_log_rows=int(self.dropped_log_rows),
                trigger_event_count=int(trigger_count),
                trigger_channels_enabled=list(self.trigger_channels_enabled),
                raw_stop_ok=bool(raw_stop_ok),
            )

        json_payload = {
            "raw_path": summary.raw_path,
            "summary_path": summary.summary_json,
            "timestamps_csv": summary.timestamps_csv,
            "trigger_events_csv": summary.trigger_events_csv,
            "sensor_size": {"width": summary.sensor_width, "height": summary.sensor_height},
            "started_at": datetime.datetime.fromtimestamp(summary.rec_start_wall_ns / 1e9).isoformat(timespec="seconds"),
            "stopped_at": datetime.datetime.fromtimestamp(summary.rec_stop_wall_ns / 1e9).isoformat(timespec="seconds"),
            "rec_start_pc_ns": summary.rec_start_pc_ns,
            "rec_stop_pc_ns": summary.rec_stop_pc_ns,
            "total_slices": summary.total_slices,
            "total_events": summary.total_events,
            "first_event_ts_us": summary.first_event_ts_us,
            "last_event_ts_us": summary.last_event_ts_us,
            "dropped_log_rows": summary.dropped_log_rows,
            "trigger_event_count": summary.trigger_event_count,
            "trigger_channels_enabled": summary.trigger_channels_enabled,
            "raw_stop_ok": summary.raw_stop_ok,
            "extra_meta": self.extra_meta,
        }
        if self.summary_json_path is not None:
            self.summary_json_path.write_text(json.dumps(json_payload, indent=2), encoding="utf-8")

        print(
            "[EVENTCAM] RAW recording stopped. "
            f"events={summary.total_events}, slices={summary.total_slices}, "
            f"triggers={summary.trigger_event_count}, summary={summary.summary_json}"
        )
        if summary.trigger_events_csv:
            print(f"[EVENTCAM] Trigger events CSV: {summary.trigger_events_csv}")
        if summary.raw_stop_ok:
            self._start_postprocess(summary)
        else:
            print("[EVENTCAM][WARN] Skipping automatic postprocess because RAW stop did not complete cleanly.")
        return summary

    def _start_postprocess(self, summary: EventCameraRecordingSummary) -> None:
        if not summary.summary_json or not EVENTCAM_POSTPROCESS_FPS:
            return

        summary.postprocess_command = [
            "embedded",
            "fps",
            f"{float(self.postprocess_fps):g}",
            "video_fps",
            f"{float(self.postprocess_video_fps):g}",
            "write_video",
            str(bool(self.postprocess_write_video)),
            "auto_track_npz",
            str(bool(self.auto_track_npz)),
            "sync_led_roi",
            self.sync_led_roi or "off",
        ]
        print(
            f"[EVENTCAM] Starting embedded conversion: render={self.postprocess_fps:g} fps, "
            f"playback={self.postprocess_video_fps:g} fps, "
            f"accumulation={self.postprocess_accumulation_us} us, "
            f"write_video={self.postprocess_write_video}, "
            f"auto_track_npz={self.auto_track_npz}, "
            f"sync_led_roi={self.sync_led_roi or 'off'}..."
        )
        try:
            run_embedded_eventcam_postprocess(
                summary_json=summary.summary_json,
                output_dir=self.postprocess_output_dir,
                fps=float(self.postprocess_fps),
                video_fps=float(self.postprocess_video_fps),
                accumulation_us=max(0, int(self.postprocess_accumulation_us)),
                sync_led_roi=self.sync_led_roi,
                sync_led_bin_us=max(1, int(self.sync_led_bin_us)),
                sync_led_threshold=max(0, int(self.sync_led_threshold)),
                sync_pre_roll_us=max(0, int(self.sync_pre_roll_us)),
                sync_duration_us=max(0, int(self.sync_duration_us)),
                mask_led_roi=bool(self.mask_led_roi),
                export_filtered_events_npz=bool(self.export_filtered_events_npz),
                write_video=bool(self.postprocess_write_video),
                auto_track_npz=bool(self.auto_track_npz),
            )
        except Exception as exc:
            print(f"[EVENTCAM][WARN] embedded postprocess failed: {exc}")

    def close(self) -> None:
        try:
            if self.is_saving.is_set():
                self.stop_recording()
        except Exception as exc:
            print(f"[EVENTCAM][WARN] stop during close failed: {exc}")

        self.stop_preview()
        self.is_saving.clear()
        self.stop_evt.set()

        # Stop the live event stream explicitly before waiting for the drain
        # thread.  Relying only on deleting EventsIterator can leave the USB
        # handle alive long enough for the next trajectory to fail with
        # LIBUSB_ERROR_ACCESS on Windows.
        reader = getattr(getattr(self, "iterator", None), "reader", None)
        stream = getattr(reader, "i_events_stream", None)
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass

        try:
            self.capture_thread.join(timeout=float(EVENTCAM_CLOSE_JOIN_TIMEOUT_SEC))
        except Exception:
            pass
        if getattr(self, "capture_thread", None) is not None and self.capture_thread.is_alive():
            print(
                "[EVENTCAM][WARN] drain thread did not exit before timeout; "
                "camera release may be delayed.",
                flush=True,
            )

        # The OpenEB RawReaderBase owns the HAL stream/decoder callbacks.
        # Calling its destructor directly is safe here and makes release
        # deterministic before the next loop opens the device again.
        if reader is not None:
            try:
                reader.__del__()
            except Exception:
                pass
        try:
            del self.iterator
        except Exception:
            pass
        try:
            del self.events_stream
        except Exception:
            pass
        try:
            del self.preview_generator
        except Exception:
            pass
        try:
            del self.device
        except Exception:
            pass
        gc.collect()
        if EVENTCAM_RELEASE_SETTLE_SEC and EVENTCAM_RELEASE_SETTLE_SEC > 0:
            time.sleep(float(EVENTCAM_RELEASE_SETTLE_SEC))


def eventcam_preview_loop(recorder: EventCameraRecorder) -> bool:
    """撮影前だけイベントカメラのライブプレビューを表示します。"""

    if recorder is None:
        return True

    print("[EVENTCAM] Preview open. Press Enter or q to close preview, Esc to abort.")
    recorder.start_preview()
    win = str(EVENTCAM_PREVIEW_WINDOW_NAME)
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, int(recorder.sensor_width), int(recorder.sensor_height))

    try:
        while True:
            frame = recorder.get_preview_frame()
            if frame is not None:
                disp = frame.copy()
                cv2.putText(
                    disp,
                    "EVENTCAM PREVIEW  Enter/q: close  Esc: abort",
                    (12, 28),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (255, 255, 255),
                    2,
                    cv2.LINE_AA,
                )
                cv2.imshow(win, disp)

            key = cv2.waitKey(20) & 0xFF
            if key in (10, 13, ord("q"), ord("Q")):
                return True
            if key == 27:
                return False

            try:
                if cv2.getWindowProperty(win, cv2.WND_PROP_VISIBLE) < 1:
                    return True
            except Exception:
                return True

    finally:
        recorder.stop_preview()
        try:
            cv2.destroyWindow(win)
        except Exception:
            pass


# =========================
# AcousTools -> ctypes prep
# =========================


def prepare_message_from_holograms(
    lev: LevitatorController,
    holograms: Sequence[torch.Tensor] | torch.Tensor,
    *,
    permute: bool = True,
) -> Tuple[ctypes.Array, ctypes.Array, int]:
    """lev.levitate() の前処理部分（Tensor→list→ctypes）を外に出した版"""

    # Normalize to list[Tensor]
    if isinstance(holograms, torch.Tensor):
        if holograms.ndim >= 1 and holograms.shape[0] > 1:
            holos = [h.unsqueeze(0).cpu().detach() for h in holograms]
        else:
            holos = [holograms.cpu().detach()]
    else:
        holos = [h.cpu().detach() for h in holograms]

    num_geometries = len(holos)
    per_geom = 256 * int(getattr(lev, "board_number", 1))

    to_output: List[float] = []
    to_output_amp: List[float] = []

    for phases_elem in holos:
        if permute:
            try:
                phases_elem = phases_elem[:, lev.IDX]
            except Exception:
                print("[WARN] permute failed; sending without permutation (check lev.IDX)")
                permute = False

        if torch.is_complex(phases_elem):
            amp_elem = torch.abs(phases_elem)
            phases_elem = torch.angle(phases_elem)
        else:
            amp_elem = torch.ones_like(phases_elem)

        to_output += phases_elem.squeeze().tolist()
        to_output_amp += amp_elem.squeeze().tolist()

    expected_len = per_geom * num_geometries
    phases_ct = (ctypes.c_float * expected_len)(*to_output)
    amps_ct = (ctypes.c_float * expected_len)(*to_output_amp)
    return phases_ct, amps_ct, num_geometries
# =========================
# Small helpers
# =========================

def ns_to_s(ns: int) -> float:
    return float(ns) * 1e-9


def safe_mkdir(p: str | Path) -> None:
    Path(p).mkdir(parents=True, exist_ok=True)



# --------------------------------------------------------------------------------------
# Trajectory generation
# --------------------------------------------------------------------------------------

def generate_cycle_positions(
    mode: int,
    n_steps: int,
    amp_x: float,
    amp_z: float,
    amp_scale: float,
    x_center: float = 0.0,
    y_center: float = 0.0,
    z_center: float = 0.0,
    amp_y: Optional[float] = None,
    helix_minor_radius: Optional[float] = None,
    helix_turns: int = 3,
) -> List[Tuple[float, float, float]]:
    """Compute one cycle of 3D positions for the specified trajectory.

    Parameters
    ----------
    mode : int
        Trajectory mode matching the descriptions in the module
        docstring.
    n_steps : int
        Number of discrete points per cycle.
    amp_x : float
        Amplitude along the x–axis in metres (used for modes where x varies).
    amp_z : float
        Amplitude along the z–axis in metres (used for modes where z varies).
    amp_scale : float
        General scale parameter for the heart trajectory.
    x_center, y_center, z_center : float, optional
        Base coordinates for the centre of the pattern.
    amp_y : float, optional
        Amplitude along y for the 3D closed curves.  When omitted, ``amp_x``
        is used so mode 12 has a circular XY projection by default.
    helix_minor_radius : float, optional
        Radial ripple of the closed toroidal helix.  Defaults to one quarter
        of ``amp_x``.
    helix_turns : int, optional
        Integer number of small helical windings around the toroidal loop.

    Returns
    -------
    list of (x, y, z)
        Sequence of positions for one complete cycle.
    """
    if n_steps <= 0:
        raise ValueError("n_steps must be a positive integer")
    amp_y_3d = amp_x if amp_y is None else amp_y
    helix_minor = 0.25 * amp_x if helix_minor_radius is None else helix_minor_radius
    helix_turns = int(helix_turns)
    if helix_turns <= 0:
        raise ValueError("helix_turns must be a positive integer")

    cycle_pos: List[Tuple[float, float, float]] = []
    for step in range(n_steps):
        # For closed curves we intentionally do NOT include the endpoint
        # (t_frac=1.0) because it duplicates the starting point and can
        # introduce a 1-frame "stall" at the loop boundary.
        #
        # Mode 8 is an open trajectory (single pass along a diagonal). For
        # that mode we DO include the endpoint so that the final sample lands
        # exactly at the intended end position.
        if mode == 8 and n_steps > 1:
            t_frac = step / (n_steps - 1)
        else:
            t_frac = step / n_steps
        theta = 2.0 * math.pi * t_frac
        # Start with centre
        x_p = x_center
        y_p = y_center
        z_p = z_center
        if mode == 1:
            # Z–axis vibration: simple sinusoidal in z only
            z_p = z_center + amp_z * math.sin(theta)
        elif mode == 10:
            # X-axis vibration: simple sinusoidal in x only
            x_p = x_center + amp_x * math.sin(theta)
        elif mode == 2:
            # Elliptical orbit in XZ plane
            x_p = x_center + amp_x * math.cos(theta)
            z_p = z_center + amp_z * math.sin(theta)
        elif mode == 3:
            # Diagonal line (left–down to right–up) in XZ plane
            x_p = x_center - amp_x * math.cos(theta)
            z_p = z_center - amp_z * math.cos(theta)
        elif mode == 4:
            # Heart shaped orbit based on parametric equations
            t = theta
            x_p = x_center + amp_scale * (math.sin(t) ** 3)
            z_p = z_center + amp_scale * (
                (13 * math.cos(t)
                 - 5 * math.cos(2 * t)
                 - 2 * math.cos(3 * t)
                 - math.cos(4 * t))
                / 13.0
            )
        elif mode == 5:
            # Rectangular orbit: four straight segments around a rectangle
            # t_frac divided into 4 quarters
            if t_frac < 0.25:
                # bottom edge: move from left to right at constant speed
                t_quad = t_frac * 4.0
                x_p = x_center + amp_x * (-1.0 + 2.0 * t_quad)
                z_p = z_center - amp_z
            elif t_frac < 0.5:
                # right edge: move upwards
                t_quad = (t_frac - 0.25) * 4.0
                x_p = x_center + amp_x
                z_p = z_center + amp_z * (-1.0 + 2.0 * t_quad)
            elif t_frac < 0.75:
                # top edge: move from right to left
                t_quad = (t_frac - 0.5) * 4.0
                x_p = x_center + amp_x * (1.0 - 2.0 * t_quad)
                z_p = z_center + amp_z
            else:
                # left edge: move downwards
                t_quad = (t_frac - 0.75) * 4.0
                x_p = x_center - amp_x
                z_p = z_center + amp_z * (1.0 - 2.0 * t_quad)
        elif mode == 6:
            # Vertical figure–eight: Lissajous pattern
            x_p = x_center + amp_x * math.sin(2.0 * theta)
            z_p = z_center + amp_z * math.sin(theta)
        elif mode == 7:
            # Horizontal infinity: rotated figure–eight
            x_p = x_center + amp_x * math.cos(theta)
            z_p = z_center + amp_z * math.sin(2.0 * theta)
        elif mode == 8:
            # Single slanted line (open): move from (-amp_x, -amp_z) to
            # (+amp_x, +amp_z) exactly once.
            # Using theta_single in [0, π] makes cos() go from +1 to -1.
            theta_single = math.pi * t_frac
            x_p = x_center - amp_x * math.cos(theta_single)
            z_p = z_center - amp_z * math.cos(theta_single)
        elif mode == 9:
            # S–shaped orbit: uses a portion of a Lissajous curve
            if t_frac < 0.5:
                # forward half
                t_path = t_frac * 2.0
            else:
                # reverse half
                t_path = (1.0 - t_frac) * 2.0
            # Map to S‑like figure between ~30° and ~165° (in radians)
            theta_start = math.pi * 0.1667
            theta_end = math.pi * 1.8333
            theta_s = theta_start + t_path * (theta_end - theta_start)
            x_p = x_center + amp_x * math.sin(2.0 * theta_s)
            z_p = z_center + amp_z * math.sin(theta_s)
        elif mode == 12:
            # XY is an ellipse (a circle when amp_x == amp_y), while XZ is
            # the familiar horizontal figure-eight x=sin(t), z=sin(2t).
            x_p = x_center + amp_x * math.sin(theta)
            y_p = y_center + amp_y_3d * math.cos(theta)
            z_p = z_center + amp_z * math.sin(2.0 * theta)
        elif mode == 13:
            # A conventional helix does not close because its axial position
            # drifts.  Winding it around a circular centreline gives a smooth,
            # exactly periodic toroidal helix.
            winding_theta = helix_turns * theta
            radial = amp_x + helix_minor * math.cos(winding_theta)
            x_p = x_center + radial * math.cos(theta)
            y_p = y_center + radial * math.sin(theta)
            z_p = z_center + amp_z * math.sin(winding_theta)
        elif mode == 14:
            # Standard trefoil-knot parameterisation.  Division by three
            # keeps each horizontal coordinate within its requested scale.
            x_p = x_center + (amp_x / 3.0) * (
                math.sin(theta) + 2.0 * math.sin(2.0 * theta)
            )
            y_p = y_center + (amp_y_3d / 3.0) * (
                math.cos(theta) - 2.0 * math.cos(2.0 * theta)
            )
            z_p = z_center - amp_z * math.sin(3.0 * theta)
        elif mode == 15:
            # Integer frequency ratios make the Lissajous curve close after
            # one base cycle.  Non-symmetric phases prevent the second half
            # from merely retracing the first half in 3D.
            x_p = x_center + amp_x * math.sin(theta)
            y_p = y_center + amp_y_3d * math.sin(2.0 * theta + math.pi / 3.0)
            z_p = z_center + amp_z * math.sin(3.0 * theta + math.pi / 5.0)
        else:
            raise ValueError(f"Unsupported mode {mode}")
        cycle_pos.append((x_p, y_p, z_p))
    return cycle_pos


def generate_smooth_positions(
    start_pos: Tuple[float, float, float],
    end_pos: Tuple[float, float, float],
    n_steps: int,
) -> List[Tuple[float, float, float]]:
    """Linearly interpolate between two 3D points over ``n_steps`` steps.

    A small helper for smoothly moving the particle between positions.  The
    interpolation excludes the starting position; each returned coordinate
    corresponds to a subsequent step towards ``end_pos``.
    """
    positions: List[Tuple[float, float, float]] = []
    if n_steps <= 0:
        return positions
    for i in range(n_steps):
        ratio = (i + 1) / n_steps
        x = start_pos[0] + (end_pos[0] - start_pos[0]) * ratio
        y = start_pos[1] + (end_pos[1] - start_pos[1]) * ratio
        z = start_pos[2] + (end_pos[2] - start_pos[2]) * ratio
        positions.append((x, y, z))
    return positions


def generate_transfer_positions(
    start_pos: Tuple[float, float, float],
    end_pos: Tuple[float, float, float],
) -> List[Tuple[float, float, float]]:
    """Interpolate static transfer motion using a fixed spatial step."""
    dx = float(end_pos[0]) - float(start_pos[0])
    dy = float(end_pos[1]) - float(start_pos[1])
    dz = float(end_pos[2]) - float(start_pos[2])
    distance_m = math.sqrt(dx * dx + dy * dy + dz * dz)
    if distance_m <= 0:
        return []
    max_step_m = max(1e-6, float(TRANSFER_STEP_MM) * 1e-3)
    n_transfer_steps = max(1, int(math.ceil(distance_m / max_step_m)))
    return generate_smooth_positions(start_pos, end_pos, n_transfer_steps)


def move_static_position(
    lev: LevitatorController,
    start_pos: Tuple[float, float, float],
    end_pos: Tuple[float, float, float],
    label: str,
    drive_amplitude_scale: float = 1.0,
) -> Tuple[float, float, float]:
    if not 0.0 < float(drive_amplitude_scale) <= 1.0:
        raise ValueError("drive_amplitude_scale must be in (0, 1]")
    transfer_positions = generate_transfer_positions(start_pos, end_pos)
    distance_mm = math.dist(start_pos, end_pos) * 1e3
    duration_s = len(transfer_positions) * float(TRANSFER_DWELL_SEC)
    print(
        f"[INFO] {label}: distance={distance_mm:.3f} mm, "
        f"steps={len(transfer_positions)}, dwell={TRANSFER_DWELL_SEC:.3f} s, "
        f"duration={duration_s:.3f} s"
    )
    for pos in transfer_positions:
        h = (
            mute_sync_transducer(compute_holograms_for_positions([pos])[0])
            * float(drive_amplitude_scale)
        )
        try:
            lev.levitate(h)
        except Exception as e:
            print(f"[WARN] levitate during transfer failed: {e}")
        time.sleep(float(TRANSFER_DWELL_SEC))
    return end_pos


def compute_holograms_for_positions(pos_list: Sequence[Tuple[float, float, float]]) -> List[torch.Tensor]:
    """Compute holograms for each target position using the KD solver.

    For each (x, y, z) triple in ``pos_list`` this function builds a point
    source, runs the KD solver to obtain the phases and then adds the
    levitation signature.  The result is a list of tensors suitable for
    consumption by ``prepare_message_from_holograms``.
    """
    positions = list(pos_list)
    holograms: List[torch.Tensor] = []
    if not positions:
        return holograms

    should_profile = (
        PROFILE_HOLOGRAM_COMPUTE
        and len(positions) > HOLOGRAM_PROGRESS_MIN_POINTS
    )
    t_start = time.perf_counter()
    board = transducers(16, BOARD_POSITIONS)
    for idx, (x, y, z) in enumerate(positions, start=1):
        # ``create_points`` expects x, y, z in metres.  We fix y to 0 for
        # two‑dimensional patterns but allow arbitrary y in the argument for
        # completeness.
        p = create_points(1, 1, x=x, y=y, z=z)

        holo = kd_solver(p, board)
        # holo = wgs(p)
        holo = add_lev_sig(holo)
        holograms.append(holo)

        if should_profile and should_report_hologram_progress(idx, len(positions)):
            elapsed = time.perf_counter() - t_start
            avg_ms = elapsed / idx * 1000.0
            print(
                f"[PREP] hologram {idx}/{len(positions)} "
                f"elapsed={elapsed:.3f}s avg={avg_ms:.1f}ms/point",
                flush=True,
            )

    if should_profile:
        total = time.perf_counter() - t_start
        print(f"[PREP] hologram preparation done in {total:.3f}s.", flush=True)
    return holograms


def should_report_hologram_progress(completed: int, total: int) -> bool:
    """Return whether a large hologram job reached a 10,000-point checkpoint."""
    if int(total) <= int(HOLOGRAM_PROGRESS_MIN_POINTS):
        return False
    return int(completed) > 0 and int(completed) % int(HOLOGRAM_PROGRESS_INTERVAL) == 0


def mute_sync_transducer(
    hologram: torch.Tensor,
    index: int = SYNC_TRANSDUCER_INDEX,
) -> torch.Tensor:
    """Return a copy with the sync transducer amplitude set to zero."""
    muted = hologram.clone()
    if not torch.is_complex(muted):
        return muted
    try:
        if muted.ndim >= 3:
            muted[:, index, :] = 0.0 + 0.0j
        elif muted.ndim == 2:
            muted[:, index] = 0.0 + 0.0j
        elif muted.ndim == 1:
            muted[index] = 0.0 + 0.0j
    except IndexError:
        print(f"[WARN] sync transducer index {index} out of range for hologram shape {tuple(muted.shape)}")
    return muted


# --------------------------------------------------------------------------------------
# Main control loop
# --------------------------------------------------------------------------------------

def main() -> None:
    """Interactive control loop for multi‑trajectory PAT recording.

    The workflow is:

      1. Prompt the user for a trajectory mode and its parameters.
      2. Compute one cycle of positions and corresponding holograms.
      3. Wake the PAT (levitate a static hologram) and set the desired frame
         rate (steps_per_cycle * frequency).  Obtain the actual FPS from the
         controller.
      4. Move smoothly from the current static position to the beginning of
         the orbit by linearly interpolating and levitating each step.
      5. Prompt the user to start recording.  Begin camera recording and
         immediately issue a PAT send_message with looping enabled.
      6. Wait for the expected duration plus an optional post‑roll margin.
      7. Stop the camera recording and save a CSV containing the ideal
         timestamps and positions.
      8. Offer to return the particle smoothly to the centre.

    The loop repeats until the user terminates with Ctrl+C.
    """
    # Ensure the recordings directory exists
    Path(SAVE_DIR).mkdir(parents=True, exist_ok=True)

    # Connect to the levitation controller
    lev = LevitatorController(ids=(101, 3))
    print("[INFO] Connected to PAT via AcousTools")

    # Event camera is opened lazily around each run. Keeping the drain thread
    # closed during hologram preparation avoids stealing CPU from AcousTools.
    recorder: EventCameraRecorder | None = None

    # Current static position and its hologram.  We keep track of the last
    # resting place so that smooth transitions can interpolate correctly.
    current_static_pos: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    current_static_holo: torch.Tensor | None = None
    if current_static_holo is None:
        # Compute a single hologram for the centre point
        current_static_holo = mute_sync_transducer(compute_holograms_for_positions([current_static_pos])[0])
        try:
            lev.levitate(current_static_holo)
            time.sleep(0.5)
        except Exception as e:
            print(f"[WARN] levitate failed during initialisation: {e}")

    try:
        while True:
            print("\n=== Trajectory Parameters ===")
            print("  1: Z-axis vibration")
            print("  2: Elliptical orbit (XZ plane)")
            print("  3: Diagonal line (XZ plane)")
            print("  4: Heart-shaped orbit (XZ plane)")
            print("  5: Rectangular orbit (XZ plane)")
            print("  6: Vertical figure-eight (XZ plane)")
            print("  7: Horizontal infinity (XZ plane)")
            print("  8: Single slanted line (XZ plane)")
            print("  9: S-shaped orbit (XZ plane)")
            print(" 10: X-axis vibration")
            print(" 11: Long random stroke")
            try:
                mode = int(input("Select mode (1-11): ").strip())
            except (ValueError, EOFError):
                print("[WARN] Invalid input; please enter a number between 1 and 11.")
                continue
            if mode not in range(1, 12):
                print("[WARN] Mode must be between 1 and 11.")
                continue
            # Read amplitudes as millimetres and convert to metres
            amp_x_mm = 0.0
            amp_z_mm = 0.0
            amp_scale_mm = 0.0
            long_random_params: Dict[str, Any] = {}
            long_random_stats: Dict[str, float] = {}
            try:
                if mode == 1:
                    amp_z_mm = float(input("Enter Z-axis amplitude (±mm) [e.g. 0.15]: ").strip())
                elif mode == 10:
                    amp_x_mm = float(input("Enter X-axis amplitude (±mm) [e.g. 0.15]: ").strip())
                elif mode == 11:
                    seed = int(input("Enter random seed [default 1101]: ").strip() or "1101")
                    duration_sec = float(input("Enter duration in seconds [default 8.0]: ").strip() or "8.0")
                    sample_hz = float(input("Enter PAT sample rate in Hz [default 1000]: ").strip() or "1000")
                    amp_x_mm = float(input("Enter X range limit (±mm) [default 22]: ").strip() or "22")
                    amp_z_mm = float(input("Enter Z range limit (±mm) [default 22]: ").strip() or "22")
                    f_min_hz = float(input("Enter min component frequency Hz [default 0.25]: ").strip() or "0.25")
                    f_max_hz = float(input("Enter max component frequency Hz [default 10]: ").strip() or "10")
                    max_speed_mm_s = float(input("Enter max speed mm/s, 0 disables [default 900]: ").strip() or "900")
                    max_accel_mm_s2 = float(input("Enter max accel mm/s^2, 0 disables [default 30000]: ").strip() or "30000")
                    long_random_params = {
                        "seed": seed,
                        "duration_sec": duration_sec,
                        "sample_hz": sample_hz,
                        "x_limit_mm": amp_x_mm,
                        "z_limit_mm": amp_z_mm,
                        "f_min_hz": f_min_hz,
                        "f_max_hz": f_max_hz,
                        "components": 9,
                        "chirps": 3,
                        "waypoints": 9,
                        "drift_weight": 0.35,
                        "max_speed_mm_s": max_speed_mm_s,
                        "max_accel_mm_s2": max_accel_mm_s2,
                    }
                elif mode == 2:
                    amp_x_mm = float(input("Enter X-axis semi-axis (±mm) [e.g. 2.0]: ").strip())
                    amp_z_mm = float(input("Enter Z-axis semi-axis (±mm) [e.g. 1.5]: ").strip())
                elif mode == 3:
                    amp_x_mm = float(input("Enter X-axis amplitude (±mm): ").strip())
                    amp_z_mm = float(input("Enter Z-axis amplitude (±mm): ").strip())
                elif mode == 4:
                    amp_scale_mm = float(input("Enter heart scale (mm): ").strip())
                elif mode == 5:
                    amp_x_mm = float(input("Enter X-axis half-length (±mm): ").strip())
                    amp_z_mm = float(input("Enter Z-axis half-length (±mm): ").strip())
                elif mode == 6:
                    amp_x_mm = float(input("Enter X-axis amplitude (±mm): ").strip())
                    amp_z_mm = float(input("Enter Z-axis amplitude (±mm): ").strip())
                elif mode == 7:
                    amp_x_mm = float(input("Enter X-axis amplitude (±mm): ").strip())
                    amp_z_mm = float(input("Enter Z-axis amplitude (±mm): ").strip())
                elif mode == 8:
                    amp_x_mm = float(input("Enter X-axis amplitude (±mm): ").strip())
                    amp_z_mm = float(input("Enter Z-axis amplitude (±mm): ").strip())
                elif mode == 9:
                    amp_x_mm = float(input("Enter X-axis amplitude (±mm): ").strip())
                    amp_z_mm = float(input("Enter Z-axis amplitude (±mm): ").strip())
            except (ValueError, EOFError):
                print("[WARN] Invalid amplitude; please enter numeric values.")
                continue
            # Convert mm to m
            amp_x = amp_x_mm * 1e-3
            amp_z = amp_z_mm * 1e-3
            amp_scale = amp_scale_mm * 1e-3
            # Number of points, motion frequency, and loops.  Reject frame
            # rates that cannot be represented exactly by OpenMPD's 40 kHz
            # divider clock; otherwise ideal_log.csv will drift from reality.
            while True:
                if mode == 11:
                    sample_hz = max(1.0, get_param(long_random_params, "sample_hz", default=1000.0))
                    duration_sec = max(0.1, get_param(long_random_params, "duration_sec", default=8.0))
                    n_steps = max(2, int(round(duration_sec * sample_hz)))
                    rev_hz = sample_hz / float(n_steps)
                    num_loops = 1
                    pat_rate = compute_pat_frame_rate_info(1, sample_hz)
                else:
                    try:
                        n_steps = int(input("Enter number of steps per cycle [default 40]: ").strip() or "40")
                        if n_steps <= 0:
                            raise ValueError
                    except (ValueError, EOFError):
                        print("[WARN] Invalid step count; please enter a positive integer.")
                        continue
                    try:
                        rev_hz = float(input("Enter motion frequency in Hz [default 10.0]: ").strip() or "10.0")
                        if rev_hz <= 0:
                            raise ValueError
                    except (ValueError, EOFError):
                        print("[WARN] Invalid frequency; please enter a positive number.")
                        continue
                    try:
                        num_loops = int(input("Enter number of loops (cycles) [default 10]: ").strip() or "10")
                        if num_loops <= 0:
                            raise ValueError
                    except (ValueError, EOFError):
                        print("[WARN] Invalid loop count; please enter a positive integer.")
                        continue

                    pat_rate = compute_pat_frame_rate_info(n_steps, rev_hz)
                if pat_rate.is_supported:
                    print(f"[INFO] PAT frame rate check OK: {describe_pat_frame_rate_info(pat_rate)}")
                    break
                print(f"[WARN] Unsupported PAT frame rate: {describe_pat_frame_rate_info(pat_rate)}")
                print(
                    "[WARN] Please choose steps/frequency so that "
                    f"steps * frequency is an integer divisor of {PAT_UPDATE_BASE_HZ} Hz "
                    "(e.g. 500, 800, 1000, 1250, 1600, 2000, 2500, 4000 Hz)."
                )

            # --- Output directory organization (shape/run) ---
            # Save everything under: ./recordings/{shape}/{run_dir}/
            # shape_name = MODE_SHAPE_NAMES.get(mode, f"mode{mode}")
            # # Parameter tag for easy identification (mm units)
            # param_tag = f"ampX{amp_x_mm:.3f}mm_ampZ{amp_z_mm:.3f}mm_ampScale{amp_scale_mm:.3f}mm"
            # run_stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            # # Basename used by recorder (it will append its own timestamp)
            # run_name = f"{shape_name}_steps{n_steps}_freq{rev_hz:.1f}_loops{num_loops}_{param_tag}"
            # # Unique run directory
            # run_dir = os.path.join(SAVE_DIR, shape_name, f"{run_name}_{run_stamp}")

            shape_name = MODE_SHAPE_NAMES.get(mode, f"mode{mode}")

            # 人間向けの詳細（長くてもOK：メタデータに入れる用）
            run_desc = (
                f"{shape_name}_steps{n_steps}_freq{rev_hz:.1f}_loops{num_loops}_"
                f"ampX{amp_x_mm:.3f}mm_ampZ{amp_z_mm:.3f}mm_ampScale{amp_scale_mm:.3f}mm"
            )

            # ファイル/フォルダ用の短いタグ
            param_parts = []
            if mode == 11:
                param_parts.append(f"seed{int(get_param(long_random_params, 'seed', default=1.0))}")
            if abs(amp_x_mm) > 1e-9:
                param_parts.append(f"x{fmt_mm(amp_x_mm)}")
            if abs(amp_z_mm) > 1e-9:
                param_parts.append(f"z{fmt_mm(amp_z_mm)}")
            if abs(amp_scale_mm) > 1e-9:
                param_parts.append(f"s{fmt_mm(amp_scale_mm)}")
            param_tag_short = "_".join(param_parts) if param_parts else "params"

            run_stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

            # ★ファイル名のベースは短くする（重要）
            run_base = sanitize_filename(
                f"{shape_name}_s{n_steps}_f{rev_hz:.1f}_l{num_loops}_{param_tag_short}",
                max_len=50
            )

            # ★ディレクトリ名にも軌道名を入れつつ、Windowsのパス長に余裕を持たせる
            run_folder = sanitize_filename(
                f"{shape_name}_{run_stamp}_{param_tag_short}",
                max_len=60,
            )
            run_dir = os.path.join(SAVE_DIR, shape_name, run_folder)
            Path(run_dir).mkdir(parents=True, exist_ok=True)

            if recorder is not None:
                recorder.save_dir = os.path.abspath(run_dir)

            # 1. Compute one cycle of positions
            if mode == 11:
                cycle_positions, long_random_stats = generate_long_random_stroke_positions(
                    params=long_random_params,
                    x_center=current_static_pos[0],
                    y_center=current_static_pos[1],
                    z_center=current_static_pos[2],
                )
                n_steps = len(cycle_positions)
                sample_hz = float(long_random_stats.get("sample_hz", get_param(long_random_params, "sample_hz", default=1000.0)))
                rev_hz = sample_hz / float(max(n_steps, 1))
                num_loops = 1
                pat_rate = compute_pat_frame_rate_info(1, sample_hz)
                print(
                    "[INFO] Generated long random stroke: "
                    f"points={n_steps}, sample_hz={sample_hz:g}, "
                    f"duration={long_random_stats.get('duration_sec', 0.0):.3f}s, "
                    f"max_speed={long_random_stats.get('max_speed_mm_s', 0.0):.1f}mm/s, "
                    f"max_accel={long_random_stats.get('max_accel_mm_s2', 0.0):.1f}mm/s^2"
                )
            else:
                cycle_positions = generate_cycle_positions(
                    mode=mode,
                    n_steps=n_steps,
                    amp_x=amp_x,
                    amp_z=amp_z,
                    amp_scale=amp_scale,
                    x_center=current_static_pos[0],
                    y_center=current_static_pos[1],
                    z_center=current_static_pos[2],
                )

            # 2. Compute holograms for the cycle
            print("[INFO] Computing holograms for one cycle…")
            cycle_holograms = compute_holograms_for_positions(cycle_positions)

            # --- Mode 8 special handling: single pass then hold ---
            # Keep the PAT update rate the same as other modes (n_steps * rev_hz),
            # but do NOT oscillate: move once along the diagonal for n_steps frames,
            # then hold the last position for the remaining frames so that total
            # duration matches (n_steps * num_loops) frames.
            if mode == 11:
                pat_loops = 1
            elif mode == 8:
                total_frames = n_steps * num_loops
                if total_frames > len(cycle_positions):
                    static_count = total_frames - len(cycle_positions)
                    cycle_positions = cycle_positions + [cycle_positions[-1]] * static_count
                    cycle_holograms = cycle_holograms + [cycle_holograms[-1]] * static_count
                pat_loops = 1
            else:
                pat_loops = num_loops

            # 3. Wake PAT at the first hologram and set frame rate
            # try:
            #     lev.levitate(cycle_holograms[0])
            # except Exception as e:
            #     print(f"[WARN] levitate (wake) failed: {e}")
            # Set PAT FPS.  The requested frame rate is steps_per_cycle * rev_hz
            fps_req = pat_rate.requested_hz
            set_fps_return_raw = lev.set_frame_rate(fps_req)
            try:
                set_fps_return = None if set_fps_return_raw is None else int(set_fps_return_raw)
            except Exception:
                set_fps_return = str(set_fps_return_raw)
            actual_fps = float(pat_rate.effective_hz or fps_req)
            if set_fps_return not in (None, 0, fps_req):
                print(f"[INFO] PAT set_frame_rate returned {set_fps_return!r}; using divider-derived fps for logs.")
            print(
                f"[INFO] PAT frame rate set: requested={fps_req} Hz, "
                f"divider={pat_rate.divider}, effective={actual_fps:.6f} Hz"
            )

            # Prepare ctypes buffers for the cycle
            print("[PREP] Preparing ctypes buffers for PAT send…")
            t_prep0 = time.perf_counter_ns()
            phases_ct, amps_ct, num_geometries = prepare_message_from_holograms(
                lev, cycle_holograms, permute=True
            )
            t_prep1 = time.perf_counter_ns()
            prep_s = ns_to_s(t_prep1 - t_prep0)
            print(f"[PREP] num_geometries={num_geometries}, prep_time={prep_s:.3f} s")
            # A sanity check
            if num_geometries != n_steps:
                print(
                    f"[WARN] Unexpected geometry count: got {num_geometries}, expected {n_steps}. "
                    "Using reported value."
                )

            # Compute start and end positions for logging and smooth motion
            start_pos = cycle_positions[0]
            end_pos = cycle_positions[-1]

            # 4. Smooth approach to the start position from current static
            current_static_pos = move_static_position(
                lev,
                current_static_pos,
                start_pos,
                "Moving smoothly to the orbit start position",
            )
            current_static_holo = mute_sync_transducer(cycle_holograms[0])

            # 5. Open the event camera only after hologram preparation and smooth approach.
            # This keeps Metavision's drain thread from slowing down solver/PAT prep.
            if recorder is None:
                try:
                    recorder = EventCameraRecorder(
                        save_dir=os.path.abspath(run_dir),
                        serial=EVENTCAM_SERIAL,
                        live_delta_t_us=EVENTCAM_LIVE_DELTA_T_US,
                        postprocess_fps=EVENTCAM_POSTPROCESS_FPS,
                        postprocess_video_fps=EVENTCAM_POSTPROCESS_VIDEO_FPS,
                        postprocess_accumulation_us=EVENTCAM_POSTPROCESS_ACCUMULATION_US,
                        postprocess_output_dir=EVENTCAM_POSTPROCESS_OUTPUT_DIR,
                        postprocess_write_video=EVENTCAM_POSTPROCESS_WRITE_VIDEO,
                        sync_led_roi=EVENTCAM_SYNC_LED_ROI,
                        sync_led_bin_us=EVENTCAM_SYNC_LED_BIN_US,
                        sync_led_threshold=EVENTCAM_SYNC_LED_THRESHOLD,
                        sync_pre_roll_us=EVENTCAM_SYNC_PRE_ROLL_US,
                        sync_duration_us=EVENTCAM_SYNC_DURATION_US,
                        mask_led_roi=EVENTCAM_MASK_LED_ROI,
                        export_filtered_events_npz=EVENTCAM_EXPORT_FILTERED_EVENTS_NPZ,
                        auto_track_npz=EVENTCAM_AUTO_TRACK_NPZ,
                        log_queue_max=EVENTCAM_LOG_QUEUE_MAX,
                        enable_trigger_in=EVENTCAM_ENABLE_TRIGGER_IN,
                        trigger_channel_names=EVENTCAM_TRIGGER_CHANNEL_NAMES,
                    )
                except Exception as e:
                    print(f"[WARN] Failed to initialise event camera recorder: {e}")
                    recorder = None

            # 6. Open a lightweight preview before recording, then close it before RAW logging starts.
            if recorder is not None and EVENTCAM_PREVIEW_BEFORE_RUN:
                if not eventcam_preview_loop(recorder):
                    print("[INFO] User aborted this run from event-camera preview.")
                    break

            # 7. Prompt for recording start.
            input("\n>>> Particle is at the start of the trajectory. Press Enter to start recording and motion. <<<\n")

            # Compute expected duration (pattern length * hardware loop count).
            expected_duration = (num_geometries * pat_loops) / float(actual_fps)

            # Save ideal log before recording stops, because automatic event NPZ
            # tracking runs inside stop_recording() and needs this file already.
            log_filename = os.path.abspath(os.path.join(run_dir, f"{run_base}_ideal_log.csv"))
            print(f"[INFO] Saving ideal position log to {log_filename}…")
            dt = 1.0 / float(actual_fps) if actual_fps else 0.0
            all_positions: List[Tuple[float, float, float]] = []
            for _ in range(int(pat_loops)):
                all_positions.extend(cycle_positions)
            with open(log_filename, "w", newline="") as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(["time", "target_x", "target_y", "target_z"])
                for i, pos in enumerate(all_positions):
                    t = i * dt
                    writer.writerow([f"{t:.6f}", f"{pos[0]:.6f}", f"{pos[1]:.6f}", f"{pos[2]:.6f}"])
            print(f"[INFO] Ideal log saved with {len(all_positions)} rows.")

            # Start camera recording. Note: recorder.save_dir already points to run_dir.
            if recorder is not None:
                recorder.start_recording(
                    run_base,
                    extra_meta={
                        "run_desc": run_desc,
                        "mode": mode,
                        "shape_name": shape_name,
                        "run_dir": os.path.abspath(run_dir),
                        "ideal_log": log_filename,
                        "steps_per_cycle": n_steps,
                        "rev_hz": rev_hz,
                        "num_loops": num_loops,
                        "pat_loops": pat_loops,
                        "amp_x_mm": amp_x_mm,
                        "amp_z_mm": amp_z_mm,
                        "amp_scale_mm": amp_scale_mm,
                        "pat_fps_requested": fps_req,
                        "pat_fps_actual": actual_fps,
                        "pat_fps_set_return": set_fps_return,
                        "pat_fps_base_hz": PAT_UPDATE_BASE_HZ,
                        "pat_fps_divider": pat_rate.divider,
                        "pat_fps_alpha": pat_rate.alpha,
                        "pat_fps_error_ms_per_s": pat_rate.error_ms_per_s,
                        "expected_duration_sec": expected_duration,
                        "prep_time_sec": prep_s,
                        "long_random_params": long_random_params,
                        "generated_trajectory": long_random_stats,
                        "eventcam_trigger_in_enabled": EVENTCAM_ENABLE_TRIGGER_IN,
                        "eventcam_trigger_channel_names": list(EVENTCAM_TRIGGER_CHANNEL_NAMES),
                        "sync_transducer_index": SYNC_TRANSDUCER_INDEX,
                    },
                )
            # Log PAT start event
            pat_start_pc_ns = int(time.perf_counter_ns())
            pat_start_wall_ns = int(time.time_ns())
            if recorder is not None:
                recorder.log_event(
                    "PAT_START",
                    pc_ns=pat_start_pc_ns,
                    wall_ns=pat_start_wall_ns,
                    note=(
                        f"expected_duration_sec={expected_duration:.6f}, "
                        f"pat_fps_actual={actual_fps}, num_geometries={num_geometries}, pat_loops={pat_loops}"
                    ),
                )
                recorder.log_event(
                    "PAT_SEND_CALL_START",
                    pc_ns=pat_start_pc_ns,
                    wall_ns=pat_start_wall_ns,
                    note=f"loop=True, num_loops={pat_loops}",
                )

            # 7. Send pattern to PAT with hardware looping
            try:
                lev.send_message(
                    phases_ct,
                    amps_ct,
                    0,
                    int(num_geometries),
                    sleep_ms=0,
                    loop=True,
                    num_loops=int(pat_loops),
                )
            except Exception as e:
                print(f"[ERROR] lev.send_message failed: {e}")
                # Even if sending fails we continue to stop camera properly

            pat_send_end_pc_ns = int(time.perf_counter_ns())
            pat_send_end_wall_ns = int(time.time_ns())
            send_call_time = ns_to_s(pat_send_end_pc_ns - pat_start_pc_ns)
            ratio = send_call_time / expected_duration if expected_duration > 0 else float('nan')
            print(f"[PAT] send_message() call_time={send_call_time:.6f} s | call/expected={ratio:.3f}")
            if recorder is not None:
                recorder.log_event(
                    "PAT_SEND_CALL_END",
                    pc_ns=pat_send_end_pc_ns,
                    wall_ns=pat_send_end_wall_ns,
                    note=f"call_time_sec={send_call_time:.6f}, call_over_expected={ratio:.3f}",
                )

            # 8. Wait for expected duration plus post‑roll
            stop_target_ns = pat_start_pc_ns + int(expected_duration * 1e9)
            if POST_ROLL_SEC and POST_ROLL_SEC > 0:
                stop_target_ns += int(POST_ROLL_SEC * 1e9)
            now_ns = time.perf_counter_ns()
            if now_ns < stop_target_ns:
                wait_s = (stop_target_ns - now_ns) * 1e-9
                print(f"[SYNC] Waiting {wait_s:.3f} s to ensure complete capture…")
                time.sleep(float(wait_s))
            else:
                print("[SYNC] No extra wait needed; send_message already covered the duration.")
            if recorder is not None:
                recorder.log_event(
                    "CAM_STOP_TARGET_REACHED",
                    pc_ns=int(stop_target_ns),
                    wall_ns=int(time.time_ns()),
                    note="stop_recording will be called next",
                )

            # 9. Stop camera recording
            summary = None
            if recorder is not None:
                try:
                    summary = recorder.stop_recording()
                except Exception as e:
                    print(f"[WARN] stop_recording failed: {e}")

            # 10b. Event-camera RAW is converted to a sync-clipped AVI by EventCameraRecorder.stop_recording().
            if summary is not None:
                try:
                    print(f"[EVENTCAM] RAW saved: {summary.raw_path}")
                    if summary.timestamps_csv:
                        print(f"[EVENTCAM] Sync log saved: {summary.timestamps_csv}")
                except Exception:
                    pass

            # This variant blocks in stop_recording() until LED-sync conversion finishes.
            # Close the camera now so no drain thread runs during return-to-centre or
            # the next trajectory's hologram preparation.
            if recorder is not None:
                try:
                    recorder.close()
                except Exception as e:
                    print(f"[EVENTCAM][WARN] close after run failed: {e}")
                recorder = None

            # 11. Update current static to end of cycle
            current_static_pos = end_pos
            current_static_holo = mute_sync_transducer(cycle_holograms[-1])
            # Put particle at end position explicitly
            try:
                lev.levitate(current_static_holo)
            except Exception as e:
                print(f"[WARN] levitate to end position failed: {e}")

            # 12. Optionally move back to centre
            ans = input("\nReturn to centre? (Y/n): ").strip().lower()
            if ans != "n":
                centre_pos = (0.0, 0.0, 0.0)
                current_static_pos = move_static_position(
                    lev,
                    current_static_pos,
                    centre_pos,
                    "Moving back to centre",
                )
                current_static_holo = mute_sync_transducer(compute_holograms_for_positions([centre_pos])[0])
                try:
                    lev.levitate(current_static_holo)
                except Exception:
                    pass
                print("[INFO] Particle returned to centre.")
            else:
                print("[INFO] Staying at current end position.")

    except KeyboardInterrupt:
        print("\n[INFO] User requested exit (Ctrl+C). Cleaning up…")
    finally:
        # Turn off the PAT safely
        try:
            # Send zeros to all channels to disable sound
            off_phase = torch.zeros_like(current_static_holo)
            off_phase = add_lev_sig(off_phase)
            phases_ct, amps_ct, _ = prepare_message_from_holograms(lev, [off_phase], permute=True)
            lev.send_message(phases_ct, amps_ct, 0, 1, sleep_ms=0, loop=False, num_loops=1)
        except Exception:
            pass
        # Close camera recorder and wait for any background 1000 fps conversion.
        if recorder is not None:
            try:
                recorder.close()
            except Exception:
                pass
        print("[INFO] Shutdown complete.")


if __name__ == "__main__":  # pragma: no cover
    main()
