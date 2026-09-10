#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
acoustools_infinicam_multitraj.py
=================================

This script extends the original single‐process AcousTools + INFINICAM
integration to support a variety of trajectories beyond a simple circle.
It preserves the core philosophy of the provided "acoustools_infinicam_sync"
template: camera and PAT are orchestrated from one process to minimize
temporal skew, and the levitation controller is used in its native loop
mode rather than Python side loops.  The trajectories implemented here are
directly ported from the legacy pyftdi/FASTCAM example and include

    1. Z–axis vibration
    2. Elliptical orbit in the XZ plane
    3. Diagonal line in the XZ plane
    4. Heart‑shaped orbit in the XZ plane
    5. Rectangular orbit in the XZ plane
    6. Vertical figure–eight (lasso) orbit
    7. Horizontal “infinity” (∞) orbit
    8. Single slanted line
    9. S‑shaped orbit

For each run the user enters basic parameters (mode, amplitudes, frame rate,
and loop count).  The script computes a single cycle of positions,
generates holograms for each step, transfers them to the PAT in a loop
via ``send_message``, records a synchronized video using INFINICAM and then
logs the ideal timing/position sequence as a CSV file.  Prior to starting
the motion the particle is moved smoothly from its current resting location
to the beginning of the orbit and after completing the motion the user may
return it to the centre.

**Why no Python loops during motion?**  The PAT supports hardware looping.
We therefore prepare a sequence of holograms once and ask the controller to
loop over them at a fixed frame rate for the requested number of cycles.
To provide an ideal log of the expected focal positions versus time, we
simply compute ``dt = 1 / actual_fps`` (as reported by the PAT) and
construct a list of timestamps paired with the target coordinates.  This
ensures that the log reflects the intention even though we are not sending
packets on every frame from Python.

This script imports ``InfinicamRecorder`` and related helpers from the
original ``acoustools_infinicam_sync_single_with_preview_v5.py`` so as not
to duplicate camera code.  Be sure that file is available on the Python
path.  Running this script will not trigger the ``main`` function of the
imported module because it is guarded by ``if __name__ == '__main__'``.

"""

from __future__ import annotations

import ctypes
import csv
import datetime
import json
import math
import os
import pickle
import queue
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np  # used in external sync helpers
import pypuclib
from pypuclib import CameraFactory, GPUSetup, PUC_SYNC_MODE, PUC_SIGNAL

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import torch
import cv2

from acoustools.Levitator import LevitatorController
from acoustools.Solvers import wgs, kd_solver
from acoustools.Utilities import transducers, add_lev_sig, create_points, BOARD_POSITIONS

import re
import hashlib

# =========================
# User settings
# =========================
SAVE_DIR = "./recordings"  # 録画/ログの保存先

# INFINICAM decode/record settings
# ROI の指定方法:
#   - None            : 既定 (0,0,512,512)
#   - (x,y,w,h)       : 明示ROI
#   - (w,h)           : **画面中央** の (w,h) を自動計算してROIにする（例: (256,256)）
# ROI = None
# ROI = (256, 256)
ROI = (768, 768)
DECODE_THREADS: Optional[int] = None
RING_BUFFER_COUNT = 64

# If set (e.g., 1000), force camera record rate without prompting.
# Use None to prompt interactively as before.
CAMERA_FPS_FORCE: int | None = None

# External-sync recording can use a different FPS than the PAT update rate.
# Set to None to follow the PAT FPS, or set e.g. 1000 to force 1000 fps capture.
EXTERNAL_CAPTURE_FPS: int | None = 1000

# The known-good standalone script (minimal_tiepie_camera_raw_v5.py) succeeded
# on CPU decode. Keep external capture on CPU by default to avoid GPU-specific
# black-frame regressions.
EXTERNAL_CAPTURE_USE_GPU: bool = False

# TiePie generator triggered-burst edge.  Use "falling" here to match the open-collector
# sync circuit: the PAT sync transducer drives the TiePie EXT input low when the drive
# signal begins, so a high→low transition should fire the burst.  Change back to "rising"
# if your trigger wiring inverts this polarity.
GEN_TRIGGER_EDGE: str = "falling"

# ROI alignment for decoder cropping. Many decoders are happier when x/y (and sometimes w/h) are aligned.
# Set to 16 by default (safe). Use 2 to only enforce even, or 1 to disable alignment.
ROI_ALIGN = 16
ROI_ALIGN_WH = 16

# 保存キュー（詰まり対策）
SAVE_QUEUE_MAX = 256
QUEUE_DROP_MARGIN = 2  # 残りがこの値以下なら decode 自体をスキップ

# Video/RAW 出力
RAW_MODE = False  # True: .raw 直書き（後でffmpeg等でエンコード）
RAW_SUFFIX = ".raw"
CODEC = cv2.VideoWriter_fourcc(*"MJPG")

# --- PAT clip extraction settings ---
# When enabled, a short clip around the PAT_START event will be created automatically
# after each recording. The clip starts at the PAT_START timestamp and ends after
# the expected pattern duration plus a small margin. This margin is defined
# separately from POST_ROLL_SEC so that recording can use a longer post-roll
# (e.g. 0.4 s) while the clip still ends closer to the expected finish (e.g. 0.2 s).
ENABLE_PAT_CLIP = True
# Extra margin (in seconds) added to the expected duration when creating the PAT clip.
# Set this to e.g. 0.2 to include a small buffer after the expected end of the pattern.
CLIP_TAIL_MARGIN_SEC = 0.3

# プレビュー（録画中は重いので基本OFF推奨）
ENABLE_PREVIEW = True
PREVIEW_MAX_FPS = 30
# ピント合わせ用途では縮小しない方が見やすいので既定=1（縮小なし）
PREVIEW_DOWNSCALE = 1
PREVIEW_WHILE_SAVING = False

# 起動後/各RUN前にフォーカス用プレビューウィンドウを開く
FOCUS_PREVIEW_BEFORE_RUN = True
FOCUS_PREVIEW_WINDOW_NAME = "INFINICAM"

# 録画停止の安全マージン（最後のフレームを取り逃がさないため）
# POST_ROLL_SEC = 0.05
POST_ROLL_SEC = 0.4

# Per-frame timestamp CSV (effective fps / frame interval verification)
ENABLE_TIMESTAMP_LOG = True
LOG_QUEUE_MAX = 200000  # enough for long runs @ 1000 fps
LOG_INCLUDE_CAM_TIMESTAMP = True  # if xferdata provides timestamp methods

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
}

_INVALID_WIN_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1F]')

try:
    import libtiepie
except ImportError:
    print("[WARN] libtiepie is not installed. Run 'pip install libtiepie'")
    libtiepie = None

# Enable network search:
libtiepie.network.auto_detect_enabled = True

# Search for devices:
libtiepie.device_list.update()

if len(libtiepie.device_list) > 0:
    print()
    print('Available devices:')

    for item in libtiepie.device_list:
        print(f'  Name: {item.name}')
        print(f'    Serial number  : {item.serial_number}')
        print(f'    Available types: {libtiepie.device_type_str(item.types)}')

        if item.has_server:
            print(f'    Server         : {item.server.url} ({item.server.name})')
else:
    print('No devices found!')

# ↓EXT_1を使うから使わない
# def init_tiepie_oscilloscope():
#     """HS5を検索し、トリガー検知用の設定を行う"""
#     if libtiepie is None:
#         return None

#     # デバイスリストの更新
#     libtiepie.network.auto_detect_enabled = True
#     libtiepie.device_list.update()

#     # オシロスコープを検索
#     scp = None
#     for item in libtiepie.device_list:
#         if item.can_open(libtiepie.DEVICETYPE_OSCILLOSCOPE):
#             scp = item.open_oscilloscope()
#             if 'HS5' in scp.name:
#                 break

#     if scp is None:
#         print("[WARN] Handyscope HS5 not found.")
#         return None

#     print(f"[INFO] Found Oscilloscope: {scp.name}")

#     # 測定モードの設定（ブロック測定モード）
#     scp.measure_mode = libtiepie.MM_BLOCK
    
#     # 【修正1】 'sample_rate' ではなく 'sample_frequency'
#     scp.sample_frequency = 1e6  # 1 MHz (1μsの精度)
#     scp.record_length = 100 # 波形データ自体は不要なため短く設定
#     scp.pre_sample_ratio = 0 

#     # チャンネル1 (CH1) を設定する
#     ch = scp.channels[0]
#     ch.enabled = True
#     # ch.range = 8.0         # ±8Vのレンジ
#     ch.range = 2.0         # ±8Vのレンジ
#     ch.coupling = libtiepie.CK_DCV

#     # 【修正2】すべてのチャンネルのトリガーをいったん無効化（誤検知防止）
#     for c in scp.channels:
#         c.trigger.enabled = False

#     # 【修正3】 'scp.trigger.timeout' ではなく 'scp.trigger_time_out' を使用
#     # 修正前（前回お伝えしたプロパティ名も間違っていた可能性があります、申し訳ありません）
#     # scp.trigger_time_out = libtiepie.TO_INFINITY 
    
#     # 修正後（確実な記述）
#     scp.trigger.timeout = libtiepie.TO_INFINITY #

#     # 【修正4】 CH1のアナログトリガー設定 (外部入力ではなくCH1を直接監視する)
#     ch.trigger.enabled = True
#     ch.trigger.kind = libtiepie.TK_RISINGEDGE
    
#     # トリガーレベルの設定 (相対値: 0.0 ~ 1.0)
#     # ±8Vレンジの場合、0.5が0V。2V でトリガーを掛けたい場合は:
#     # (2.0V - (-8.0V)) / 16.0V = 10 / 16 = 0.625
#     # ch.trigger.levels[0] = 0.625 
#     ch.trigger.levels[0] = 0.6125 
    
#     # 【追加】 ヒステリシス (微小なノイズによる誤検知を防ぐ。例: 5% = 0.05)
#     ch.trigger.hystereses[0] = 0.05

#     print("[INFO] TiePie Handyscope HS5 initialized for hardware trigger.")
#     return scp

# def wait_for_hardware_trigger(scp, recorder):
#     """別スレッドでHS5のトリガーを監視し、検知したらタイムスタンプを記録する"""
#     print("[TiePie] Waiting for OpenMPD signal on CH1...")
    
#     # 測定開始 (トリガー待ち状態に入る)
#     scp.start()

#     # トリガーが掛かり、データが取得可能になるまでループ
#     while not scp.is_data_ready:
#         if recorder and recorder.stop_evt.is_set():
#             # 録画が強制終了された場合は抜ける
#             return
#         time.sleep(0.001)

#     # ----------------------------------------------------
#     # トリガー検知！ (実機が動き出したドンピシャの瞬間)
#     # ----------------------------------------------------
#     trigger_pc_ns = time.perf_counter_ns()
#     trigger_wall_ns = time.time_ns()

#     print("[TiePie] >> HARDWARE TRIGGER DETECTED! <<")

#     if recorder is not None:
#         # CSVに真の開始時刻を書き込む
#         recorder.log_event(
#             "PHYSICAL_PAT_START",
#             pc_ns=trigger_pc_ns,
#             wall_ns=trigger_wall_ns,
#             note="Hardware trigger from Handyscope HS5"
#         )
    
#     # 内部ステータスをクリアするためにデータを読み捨てる
#     try:
#         scp.get_data()
#     except Exception:
#         pass


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

# =========================
# PAT clip extraction helper
# =========================
def make_pat_clip(
    src_avi: str,
    csv_path: str,
    expected_s: float,
    clip_tail_sec: float = CLIP_TAIL_MARGIN_SEC,
    out_suffix: str = "_clip_pat.avi",
) -> Optional[str]:
    """Create a short clip from the recorded AVI around the PAT_START event.

    This helper reads the per-frame/event timestamp CSV to locate the PAT_START
    event. It then determines the frame index corresponding to the PAT_START
    timestamp and the frame index for the end of the clip, defined as
    PAT_START + expected_s + clip_tail_sec. The frames between these indices
    (inclusive) are extracted into a new AVI file with the given suffix.

    Args:
        src_avi: Path to the recorded AVI file (full recording).
        csv_path: Path to the timestamps CSV generated during recording.
        expected_s: Expected duration of the PAT pattern in seconds. This is
            typically (steps_per_lev * num_loops) / pat_fps_actual and
            corresponds to the duration you wish to capture.
        clip_tail_sec: Additional margin (in seconds) added after the expected
            duration. Defaults to the module-level CLIP_TAIL_MARGIN_SEC.
        out_suffix: Suffix to append to the filename before the extension for
            the clip. Defaults to "_clip_pat.avi".

    Returns:
        The path to the newly created clip, or None if the operation failed.

    Raises:
        RuntimeError: If the source AVI cannot be read or if no PAT_START event
            is found in the CSV.
    """
    try:
        import pandas as pd  # type: ignore
        import numpy as np  # type: ignore
    except Exception as e:
        print(f"[WARN] pandas/numpy import failed in make_pat_clip: {e}")
        return None

# --------------------------------------------------------------------------------------
# External sync recording helpers (TiePie + INFINICAM)
#
# These functions implement a standalone RAW-first recorder that synchronises the
# INFINICAM camera to a TiePie generator via the camera's SYNC IN port.  They
# replicate the behaviour of the separate repro_external_sync_infinicam_tiepie.py
# script but integrate seamlessly into the multi‑trajectory control flow.

# Candidate frame rates that the camera may accept.  When applying a requested
# frame rate, the nearest supported value is chosen if the driver rejects the
# initial request.  The list covers common INFINICAM rates up to 30 kHz.
FRAMERATE_CANDIDATES: list[int] = [
    1, 10, 50, 100, 125, 250, 500, 950, 1000, 1500, 2000, 2500, 3000,
    3200, 4000, 5000, 8000, 10000, 20000, 25000, 30000,
]

def _nearest_candidate(x: float) -> int:
    """Return the nearest supported frame rate candidate for the requested value."""
    return int(min(FRAMERATE_CANDIDATES, key=lambda v: abs(v - x)))

def _open_tiepie_generator(require_burst: bool = True):
    """Search for and open a TiePie generator.  Raises if none found."""
    libtiepie.network.auto_detect_enabled = True
    libtiepie.device_list.update()
    for item in libtiepie.device_list:
        if item.can_open(libtiepie.DEVICETYPE_GENERATOR):
            gen = item.open_generator()
            if gen is None:
                continue
            # If burst mode is required, ensure the device supports it.
            if require_burst and hasattr(gen, "modes_native"):
                try:
                    if not (gen.modes_native & libtiepie.GM_BURST_COUNT):
                        try:
                            del gen
                        except Exception:
                            pass
                        continue
                except Exception:
                    pass
            return gen
    raise RuntimeError("No TiePie generator found")

def _set_dc_low(gen) -> None:
    """Force the generator output to a DC low level (0 V)."""
    gen.signal_type = libtiepie.ST_DC
    gen.offset = 0.0
    gen.output_enable = True

def _configure_square_burst(
    gen, freq_hz: float, count: int, duty: float, amplitude: float, offset: float
) -> None:
    """Configure the generator to output a square wave burst.

    Args:
        gen: The TiePie generator instance.
        freq_hz: Pulse repetition frequency in Hz.
        count: Number of pulses in the burst.
        duty: Duty cycle (0.0–1.0) of the square wave.
        amplitude: Peak‑to‑peak amplitude in volts.
        offset: DC offset in volts.
    """
    gen.signal_type = libtiepie.ST_SQUARE
    gen.frequency = float(freq_hz)
    gen.amplitude = float(amplitude)
    gen.offset = float(offset)
    gen.symmetry = float(duty)
    gen.mode = libtiepie.GM_BURST_COUNT
    gen.burst_count = int(count)
    gen.output_enable = True

def _configure_generator_trigger_input(gen, edge: str = "rising") -> None:
    """Arm TiePie triggered-burst on EXT1/EXT2.

    This follows TiePie’s official triggered-burst example: locate EXT1 (or EXT2),
    enable it, choose the edge, then call gen.start() to arm the burst. The
    actual waveform starts only when the external trigger edge arrives.
    """
    if not hasattr(gen, "trigger_inputs") or len(gen.trigger_inputs) == 0:
        raise RuntimeError("This TiePie generator has no trigger_inputs for triggered burst")
    trigger_input = gen.trigger_inputs.get_by_id(libtiepie.TIID_EXT1)
    if trigger_input is None:
        trigger_input = gen.trigger_inputs.get_by_id(libtiepie.TIID_EXT2)
    if trigger_input is None:
        raise RuntimeError("Unknown TiePie generator trigger input (EXT1/EXT2 not found)")
    trigger_input.enabled = True
    edge_l = str(edge).strip().lower()
    trigger_input.kind = libtiepie.TK_RISINGEDGE if edge_l != "falling" else libtiepie.TK_FALLINGEDGE

# --------------------------------------------------------------------------------------
# FASTCAM one-shot trigger helpers (TiePie only, no INFINICAM control)

# The current wiring uses the PAT sync edge (through the open-collector/NPN path)
# to pull TiePie EXT low.  TiePie then generates a single active-low pulse on its
# AWG output for the FASTCAM trigger input.
FASTCAM_TRIGGER_EDGE: str = "falling"
FASTCAM_IDLE_VOLTAGE: float = 5.0
FASTCAM_LOW_VOLTAGE: float = 0.0
FASTCAM_TRIGGER_PULSE_WIDTH_MS: float = 10.0
FASTCAM_TRIGGER_RECOVERY_MS: float = 20.0
FASTCAM_IDLE_SETTLE_SEC: float = 0.5
FASTCAM_POST_TRIGGER_SEC: float = 0.2

def _set_generator_output_enabled(gen, enabled: bool) -> None:
    if hasattr(gen, "output_enable"):
        gen.output_enable = bool(enabled)
    elif hasattr(gen, "output_on"):
        gen.output_on = bool(enabled)
    else:
        raise RuntimeError("TiePie generator has no output enable property")

def _set_generator_output_invert(gen, invert: bool) -> None:
    if hasattr(gen, "output_invert"):
        gen.output_invert = bool(invert)
    else:
        try:
            gen.set_output_invert(bool(invert))
        except Exception:
            print("[WARN] TiePie output invert property not available; using non-inverted output")

def _set_dc_level(gen, level_v: float) -> None:
    gen.signal_type = libtiepie.ST_DC
    gen.offset = float(level_v)
    _set_generator_output_invert(gen, False)
    _set_generator_output_enabled(gen, True)

def _configure_single_low_pulse_burst(
    gen,
    *,
    pulse_width_ms: float,
    recovery_ms: float,
    high_v: float,
    low_v: float,
) -> dict:
    """Configure one active-low pulse using the normal GeneratorTriggeredBurst flow.

    We keep the generator in triggered burst mode, but reduce the burst to exactly
    one period.  The base waveform is a 0..(high-low) square wave around the
    chosen offset; output inversion makes the first part of the period LOW, giving
    a single active-low pulse when the burst starts.
    """
    pulse_width_ms = max(0.1, float(pulse_width_ms))
    recovery_ms = max(0.1, float(recovery_ms))
    total_ms = pulse_width_ms + recovery_ms
    freq_hz = 1000.0 / total_ms
    duty = pulse_width_ms / total_ms

    high_v = float(high_v)
    low_v = float(low_v)
    lo = min(high_v, low_v)
    hi = max(high_v, low_v)
    amplitude = (hi - lo) / 2.0
    offset = (hi + lo) / 2.0

    gen.signal_type = libtiepie.ST_SQUARE
    gen.frequency = float(freq_hz)
    gen.amplitude = float(amplitude)
    gen.offset = float(offset)
    gen.symmetry = float(duty)
    gen.mode = libtiepie.GM_BURST_COUNT
    gen.burst_count = 1
    _set_generator_output_invert(gen, True)  # invert 0..5 V to 5..0 V => active-low pulse
    _set_generator_output_enabled(gen, True)

    return {
        "pulse_width_ms": pulse_width_ms,
        "recovery_ms": recovery_ms,
        "total_period_ms": total_ms,
        "frequency_hz": freq_hz,
        "duty": duty,
        "high_v": high_v,
        "low_v": low_v,
    }

def run_fastcam_one_shot_trigger(
    *,
    basename: str,
    save_dir: str,
    lev: Any,
    phases_ct: Any,
    amps_ct: Any,
    num_geometries: int,
    pat_loops: int,
    arm_delay_sec: float = 0.2,
    idle_settle_sec: float = FASTCAM_IDLE_SETTLE_SEC,
    pulse_width_ms: float = FASTCAM_TRIGGER_PULSE_WIDTH_MS,
    recovery_ms: float = FASTCAM_TRIGGER_RECOVERY_MS,
    post_trigger_sec: float = FASTCAM_POST_TRIGGER_SEC,
    idle_voltage: float = FASTCAM_IDLE_VOLTAGE,
    low_voltage: float = FASTCAM_LOW_VOLTAGE,
    trigger_edge: str = FASTCAM_TRIGGER_EDGE,
    extra_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Arm TiePie for a single active-low FASTCAM trigger pulse and then send PAT.

    Sequence:
      1. Hold AWG at the idle level (default: HIGH)
      2. Configure a one-period triggered burst
      3. Arm EXT1/EXT2 trigger input
      4. Call lev.send_message(); the PAT sync edge physically triggers TiePie
      5. Restore the idle level after the pulse
    """
    os.makedirs(save_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_base = basename.replace(" ", "_")
    meta_path = os.path.abspath(os.path.join(save_dir, f"{safe_base}_fastcam_trigger_{ts}.json"))

    gen = _open_tiepie_generator(require_burst=True)
    pulse_meta: Dict[str, Any] = {}
    pat_send_pc_ns: Optional[int] = None
    pat_send_wall_ns: Optional[int] = None
    try:
        print(f"[INFO] Forcing TiePie AWG idle level to {float(idle_voltage):.3f} V for {float(idle_settle_sec):.3f} s")
        _set_dc_level(gen, float(idle_voltage))
        time.sleep(max(0.0, float(idle_settle_sec)))

        print(f"[INFO] Waiting {float(arm_delay_sec):.3f} s before arming TiePie one-shot trigger…")
        time.sleep(max(0.0, float(arm_delay_sec)))

        pulse_meta = _configure_single_low_pulse_burst(
            gen,
            pulse_width_ms=float(pulse_width_ms),
            recovery_ms=float(recovery_ms),
            high_v=float(idle_voltage),
            low_v=float(low_voltage),
        )
        try:
            print(
                "[INFO] FASTCAM trigger pulse read-back: "
                f"freq={gen.frequency} Hz, amp={gen.amplitude} V, off={gen.offset} V, "
                f"duty={getattr(gen, 'symmetry', pulse_meta['duty'])}"
            )
        except Exception:
            pass

        _configure_generator_trigger_input(gen, edge=trigger_edge)
        print(f"[INFO] Arming TiePie triggered burst on EXT input ({trigger_edge} edge)…")
        gen.start()

        pat_send_pc_ns = time.perf_counter_ns()
        pat_send_wall_ns = time.time_ns()
        print("[INFO] Sending PAT message; TiePie will emit one active-low FASTCAM trigger pulse on the physical PAT sync edge…")
        lev.send_message(
            phases_ct,
            amps_ct,
            0,
            int(num_geometries),
            sleep_ms=0,
            loop=True,
            num_loops=int(pat_loops),
        )

        wait_after_send = max(0.0, (pulse_meta.get("total_period_ms", 0.0) * 1e-3) + float(post_trigger_sec))
        time.sleep(wait_after_send)

        print(f"[INFO] Restoring TiePie AWG idle level to {float(idle_voltage):.3f} V")
        _set_dc_level(gen, float(idle_voltage))

        meta: Dict[str, Any] = {
            "timestamp": ts,
            "meta_path": meta_path,
            "pat_send_pc_ns": int(pat_send_pc_ns) if pat_send_pc_ns is not None else None,
            "pat_send_wall_ns": int(pat_send_wall_ns) if pat_send_wall_ns is not None else None,
            "trigger_edge": str(trigger_edge),
            "idle_voltage": float(idle_voltage),
            "low_voltage": float(low_voltage),
            "arm_delay_sec": float(arm_delay_sec),
            "idle_settle_sec": float(idle_settle_sec),
            "post_trigger_sec": float(post_trigger_sec),
            "pulse": pulse_meta,
            "num_geometries": int(num_geometries),
            "pat_loops": int(pat_loops),
        }
        if extra_meta:
            meta.update(extra_meta)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        print(f"[INFO] FASTCAM trigger meta: {meta_path}")
        return meta
    finally:
        try:
            gen.stop()
        except Exception:
            pass
        try:
            _set_generator_output_enabled(gen, False)
        except Exception:
            pass
        try:
            del gen
        except Exception:
            pass

def _align_down(v: int, align: int) -> int:
    return int(v) if align <= 1 else (int(v) & ~(int(align) - 1))

def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(v, hi))

def _parse_roi_like_trig(roi_spec: Optional[str], full_w: int, full_h: int, align_xy: int = 16) -> tuple[int, int, int, int]:
    """Interpret ROI specification similar to the *_trig.py shorthand.

    The ROI specification may be of the form "w,h" (centre‑crop with alignment) or
    "x,y,w,h".  Alignment on x/y ensures many decoders work reliably.
    """
    if roi_spec is None or str(roi_spec).strip() == "":
        roi_spec = "768,768"
    parts = [p.strip() for p in str(roi_spec).split(",") if p.strip() != ""]
    if len(parts) == 2:
        rw = _clamp(int(parts[0]), 2, full_w)
        rh = _clamp(int(parts[1]), 2, full_h)
        rx = _align_down((full_w - rw) // 2, align_xy)
        ry = _align_down((full_h - rh) // 2, align_xy)
        return (_clamp(rx, 0, full_w - rw), _clamp(ry, 0, full_h - rh), rw, rh)
    if len(parts) == 4:
        rx, ry, rw, rh = map(int, parts)
        rw = _clamp(rw, 2, full_w)
        rh = _clamp(rh, 2, full_h)
        return (_clamp(rx, 0, full_w - rw), _clamp(ry, 0, full_h - rh), rw, rh)
    raise ValueError("ROI must be 'w,h' or 'x,y,w,h'")

def _apply_camera_fps(cam, requested_fps: float, shutter_fps: int) -> float:
    """Apply the requested frame rate and shutter setting to the camera.

    Some INFINICAM drivers may reject arbitrary frame rates.  If the request
    fails, the nearest supported candidate is applied.  The actual frame rate
    read back from the camera is returned.
    """
    rate = int(round(requested_fps))
    shutter_den = int(max(1, shutter_fps))

    def _apply(r: int) -> None:
        if hasattr(cam, "setFramerateShutter"):
            cam.setFramerateShutter(int(r), int(shutter_den))
        else:
            if hasattr(cam, "setFramerate"):
                cam.setFramerate(int(r))
            if hasattr(cam, "setShutter"):
                try:
                    cam.setShutter(int(shutter_den))
                except Exception:
                    pass

    try:
        _apply(rate)
    except Exception:
        cand = _nearest_candidate(requested_fps)
        print(f"[WARN] setFramerateShutter({rate}) failed, retry {cand}")
        _apply(cand)
    try:
        actual = float(cam.framerate())
        print(f"[INFO] camera framerate read‑back = {actual} fps")
        try:
            print(f"[INFO] camera shutter read‑back = 1/{cam.shutter()}")
        except Exception:
            pass
        return actual
    except Exception:
        print("[WARN] camera framerate read‑back unavailable; using requested fps")
        return float(rate)

def _encode_raw_to_avi(raw_path: str, meta_path: str, avi_path: str, fps: Optional[float] = None) -> str:
    """Convert a monochrome RAW sequence to an MJPEG AVI.

    Reads the width, height and frame count from the provided meta.json.  If
    ``fps`` is None, the meta's FPS value is used.  Returns the path to the
    generated AVI.
    """
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    width = int(meta["width"])
    height = int(meta["height"])
    n_frames = int(meta["frames_written"])
    use_fps = float(meta["fps"] if fps is None else fps)
    frame_bytes = width * height
    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
    writer = cv2.VideoWriter(avi_path, fourcc, use_fps, (width, height), False)
    if not writer.isOpened():
        raise RuntimeError(f"VideoWriter open failed: {avi_path}")
    with open(raw_path, "rb") as fp:
        for _ in range(n_frames):
            buf = fp.read(frame_bytes)
            if len(buf) != frame_bytes:
                break
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(height, width)
            writer.write(frame)
    writer.release()
    return avi_path

def run_external_sync_recording(
    *,
    basename: str,
    save_dir: str,
    n_frames: int,
    requested_fps: float,
    roi_spec: Optional[str],
    lev: Any,
    phases_ct: Any,
    amps_ct: Any,
    num_geometries: int,
    pat_loops: int,
    extra_meta: Optional[Dict[str, Any]] = None,
    duty: float = 0.5,
    amplitude: float = 2.5,
    offset: float = 2.5,
    arm_delay_sec: float = 0.2,
    low_hold_sec: float = 0.5,
    queue_max: int = 128,
    decode_threads: Optional[int] = None,
    use_gpu: bool = True,
    encode_after: bool = True,
    encode_fps: Optional[float] = None,
) -> Dict[str, Any]:
    """Record a PAT sequence using external sync between TiePie and INFINICAM.

    This helper configures the camera to use EXTERNAL SYNC IN, arms the camera
    and TiePie generator, triggers the PAT via ``lev.send_message`` and
    captures ``n_frames`` frames at ``requested_fps``.  Frames are saved
    as a RAW file and logged with timestamps.  Optionally the RAW file is
    converted to an AVI after capture.  A meta dictionary is returned with
    details of the capture.

    Args:
        basename: Base name for output files (no extension).  A timestamp and
            run details are appended automatically.
        save_dir: Directory to save recordings.
        n_frames: Total number of frames to capture.
        requested_fps: Requested camera frame rate prior to switching to
            external sync.  This should match the PAT FPS for best results.
        roi_spec: ROI specification as in *_trig.py (e.g. '768,768' or 'x,y,w,h').
        lev: The LevitatorController instance used to send holograms.
        phases_ct, amps_ct: Data for ``lev.send_message``.
        num_geometries: Number of geometry steps (PAT sequence length).
        pat_loops: Hardware loop count for the PAT.
        extra_meta: Additional metadata to write into meta.json.
        duty, amplitude, offset: TiePie square wave burst parameters.
        arm_delay_sec: Delay in seconds between arming the camera and starting
            the TiePie burst (allows camera ring buffers to fill).
        low_hold_sec: Duration to hold the TiePie output low before and after
            the burst (avoids spurious edges).
        queue_max: Maximum number of frames to buffer between the callback
            and disk writer.  RAW mode blocks if the queue is full.
        decode_threads: Optional number of decode threads.  None auto-detects.
        use_gpu: Attempt GPU decoding if supported.  Falls back to CPU.
        encode_after: If True, convert the RAW file to AVI after capture.
        encode_fps: Override FPS when encoding AVI (None uses meta FPS).

    Returns:
        A dictionary describing the capture (meta data), similar to the
        InfinicamRecorder summary.
    """
    import numpy as np  # local import to avoid global dependency
    os.makedirs(save_dir, exist_ok=True)
    # Generate a unique timestamped stem for all outputs
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_base = basename.replace(" ", "_")
    stem = os.path.abspath(os.path.join(save_dir, f"{safe_base}_{n_frames}f_{int(requested_fps)}fps_{ts}"))
    raw_path = stem + ".raw"
    csv_path = stem + "_timestamps.csv"
    meta_path = stem + "_meta.json"
    avi_path = stem + ".avi"

    gen = _open_tiepie_generator(require_burst=True)
    # open TiePie and hold output low before arming the camera
    try:
        print(f"[INFO] Forcing TiePie line LOW for {low_hold_sec:.3f}s before camera EXTERNAL mode")
        _set_dc_low(gen)
        time.sleep(max(0.0, low_hold_sec))

        print("[INFO] Opening INFINICAM (external sync)")
        cam = CameraFactory().create(0, True)
        res = cam.resolution()
        full_w, full_h = int(res.width), int(res.height)
        # Apply requested FPS; shutter FPS uses a sensible value (e.g. 2×fps)
        shutter_fps = int(max(1, requested_fps * 2.0))
        # shutter_fps = int(max(1, requested_fps))
        actual_cam_fps = _apply_camera_fps(cam, requested_fps, shutter_fps)
        # Parse ROI like *_trig.py using the provided roi_spec
        rx, ry, rw, rh = _parse_roi_like_trig(roi_spec, full_w, full_h, align_xy=16)
        print(f"[INFO] ROI: x={rx}, y={ry}, w={rw}, h={rh} (camera reso {full_w}x{full_h})")
        print("[INFO] Setting SYNC IN = External / Positive")
        try:
            cam.setSyncInMode(int(PUC_SYNC_MODE.PUC_SYNC_EXTERNAL), int(PUC_SIGNAL.PUC_SIGNAL_NEGA))
            # cam.setSyncInMode(int(PUC_SYNC_MODE.PUC_SYNC_EXTERNAL), int(PUC_SIGNAL.PUC_SIGNAL_POSI))
            # cam.setSyncInMode(int(PUC_SYNC_MODE.PUC_SYNC_INTERNAL), int(PUC_SIGNAL.PUC_SIGNAL_POSI))
            # cam.setSyncInMode(int(PUC_SYNC_MODE.PUC_SYNC_INTERNAL), int(PUC_SIGNAL.PUC_SIGNAL_NEGA))
            # cam.setSyncInMode(1, 1)
            # cam.setSyncInMode(0, 0)
            # cam.setSyncInMode(int(PUC_SYNC_MODE.PUC_SYNC_EXTERNAL), int(PUC_SIGNAL.PUC_SIGNAL_POS))
            time.sleep(2.0)
        except Exception as e:
            print(f"[WARN] setSyncInMode failed: {e}")

        try:
            print(f"[INFO] syncInMode() read‑back: {cam.syncInMode()}")
        except Exception:
            print("[INFO] syncInMode() read‑back unavailable")
        # Increase ring buffer size to absorb decode backlog
        try:
            cam.setRingBufferCount(max(256, min(int(n_frames) + 200, 8192)))
        except Exception as e:
            print(f"[WARN] setRingBufferCount failed: {e}")
        dec = cam.decoder()
        # Auto‑detect decode threads if None
        if decode_threads is None:
            try:
                import multiprocessing  # type: ignore

                decode_threads = min(8, multiprocessing.cpu_count())
            except Exception:
                decode_threads = None
        if decode_threads:
            try:
                dec.setNumDecodeThread(int(decode_threads))
                print(f"[INFO] decoder threads = {int(decode_threads)}")
            except Exception as e:
                print(f"[WARN] dec.setNumDecodeThread failed: {e}")
        gpu_enabled = False
        if use_gpu:
            try:
                if dec.getAvailableGPUProcess():
                    dec.setupGPUDecode(GPUSetup(full_w, full_h))
                    gpu_enabled = True
                    print("[INFO] GPU decode enabled")
            except Exception as e:
                print(f"[WARN] GPU decode setup failed -> CPU decode: {e}")
        if not gpu_enabled:
            print("[INFO] CPU decode ROI")

        # Prepare CSV logger and RAW writer
        csv_fp = open(csv_path, "w", encoding="utf-8", newline="")
        csv_writer = csv.writer(csv_fp)
        # Header for event/frame log (compatible with InfinicamRecorder)
        csv_writer.writerow([
            "kind",
            "index",
            "seq",
            "t_rel_sec",
            "perf_counter_ns",
            "wall_clock_ns",
            "cam_timestamp",
            "event",
            "note",
        ])
        raw_fp = open(raw_path, "wb", buffering=16 * 1024 * 1024)

        # Queues and state for producer/consumer threads
        frame_q: queue.Queue[Optional[np.ndarray]] = queue.Queue(maxsize=int(queue_max))
        log_q: queue.Queue[Optional[tuple]] = queue.Queue(maxsize=max(2048, int(queue_max) * 4))
        stop_evt = threading.Event()
        accept_frames_evt = threading.Event()
        cb_counter_lock = threading.Lock()
        cb_inflight = 0
        frames_in = 0
        frames_written = 0
        frames_logged = 0
        frames_dropped_queue = 0
        log_rows_dropped = 0
        last_seq: Optional[int] = None
        last_pc_ns: Optional[int] = None
        cam_ts_attr: Optional[str] = None
        cam_ts_unit: Optional[str] = None
        cam_ts_checked = False
        rec_start_pc_ns: Optional[int] = None
        rec_start_wall_ns: Optional[int] = None

        def writer_loop() -> None:
            nonlocal frames_written
            while True:
                item = frame_q.get()
                try:
                    if item is None:
                        return
                    raw_fp.write(memoryview(item))
                    frames_written += 1
                finally:
                    try:
                        frame_q.task_done()
                    except Exception:
                        pass

        def logger_loop() -> None:
            nonlocal frames_logged
            while True:
                item = log_q.get()
                try:
                    if item is None:
                        return
                    csv_writer.writerow(item)
                    frames_logged += 1
                finally:
                    try:
                        log_q.task_done()
                    except Exception:
                        pass

        # Launch writer and logger threads
        wt = threading.Thread(target=writer_loop, daemon=True)
        lt = threading.Thread(target=logger_loop, daemon=True)
        wt.start()
        lt.start()

        def cb(xfer) -> None:
            nonlocal frames_in, frames_dropped_queue, last_pc_ns, last_seq, cb_inflight, cam_ts_attr, cam_ts_unit, cam_ts_checked, log_rows_dropped
            if not accept_frames_evt.is_set() or stop_evt.is_set():
                return
            with cb_counter_lock:
                cb_inflight_now = cb_inflight + 1
                cb_inflight = cb_inflight_now
            try:
                pc_ns = time.perf_counter_ns()
                wall_ns = time.time_ns()
                seq = -1
                try:
                    seq = int(xfer.sequenceNo())
                except Exception:
                    seq = -1
                # De‑duplicate sequence numbers
                if last_seq is not None and seq == last_seq:
                    return
                last_seq = seq
                cam_ts_val = None
                if not cam_ts_checked:
                    # Discover a usable camera timestamp attribute on the fly
                    for a in (
                        "timeStamp",
                        "timestamp",
                        "getTimestamp",
                        "frameTime",
                        "frame_timestamp",
                        "deviceTimestamp",
                        "deviceTimeStamp",
                        "captureTime",
                        "captureTimestamp",
                        "timeStampUs",
                        "timeStampNS",
                        "timeStampNs",
                        "timeStamp10ns",
                    ):
                        if hasattr(xfer, a):
                            cam_ts_attr = a
                            break
                    if cam_ts_attr is None:
                        try:
                            for name in dir(xfer):
                                n = name.lower()
                                obj = getattr(xfer, name, None)
                                if ("time" in n or "stamp" in n) and callable(obj):
                                    if any(k in n for k in ("stamp", "timestamp", "frametime", "capture")):
                                        cam_ts_attr = name
                                        break
                        except Exception:
                            pass
                    if cam_ts_attr:
                        nn = str(cam_ts_attr).lower()
                        if "10ns" in nn:
                            cam_ts_unit = "10ns"
                        elif "ns" in nn:
                            cam_ts_unit = "ns"
                        elif "us" in nn:
                            cam_ts_unit = "us"
                        elif "ms" in nn:
                            cam_ts_unit = "ms"
                        else:
                            cam_ts_unit = "unknown"
                    cam_ts_checked = True
                if cam_ts_attr:
                    try:
                        attr = getattr(xfer, cam_ts_attr)
                        cam_ts_val = attr() if callable(attr) else attr
                    except Exception:
                        cam_ts_val = None
                # Compute relative time
                t_rel_sec = 0.0
                if rec_start_pc_ns is not None:
                    t_rel_sec = (int(pc_ns) - int(rec_start_pc_ns)) * 1e-9
                # Write log row for this frame
                try:
                    log_q.put_nowait(
                        (
                            "FRAME",
                            int(frames_in),
                            int(seq),
                            f"{t_rel_sec:.9f}",
                            int(pc_ns),
                            int(wall_ns),
                            "" if cam_ts_val is None else cam_ts_val,
                            "",
                            "",
                        )
                    )
                except queue.Full:
                    log_rows_dropped += 1
                # Decode the frame
                if gpu_enabled:
                    try:
                        full = dec.decodeGPU(xfer, False, full_w)
                        gray = np.asarray(full[ry : ry + rh, rx : rx + rw]).copy()
                    except Exception:
                        # fallback to CPU decode if GPU fails
                        gray = np.asarray(dec.decode(xfer, rx, ry, rw, rh)).copy()
                else:
                    gray = np.asarray(dec.decode(xfer, rx, ry, rw, rh)).copy()
                if gray.dtype != np.uint8:
                    if gray.dtype == np.uint16:
                        gray = (gray >> 8).astype(np.uint8, copy=False)
                    else:
                        gray = np.clip(gray, 0, 255).astype(np.uint8, copy=False)
                if not gray.flags["C_CONTIGUOUS"]:
                    gray = np.ascontiguousarray(gray)
                try:
                    frame_q.put_nowait(gray)
                except queue.Full:
                    frames_dropped_queue += 1
                    # In RAW mode we prefer blocking over dropping; fallback to blocking put
                    frame_q.put(gray)
                frames_in += 1
                if frames_in >= int(n_frames):
                    stop_evt.set()
            finally:
                with cb_counter_lock:
                    cb_inflight -= 1

        # Arm camera transfer
        print("[INFO] Arming INFINICAM (beginXfer) for external sync…")
        cam.beginXfer(cb)
        # Give ring buffer time to fill before capture
        print(f"[INFO] Waiting {arm_delay_sec:.3f}s before sending PAT and starting TiePie burst…")
        time.sleep(max(0.0, arm_delay_sec))
        # Configure generator frequency from camera read‑back fps
        burst_fps = float(actual_cam_fps)
        if abs(burst_fps - float(requested_fps)) > 1e-6:
            print(
                f"[WARN] TiePie burst frequency follows camera read‑back fps: {burst_fps} Hz (requested {requested_fps})"
            )
        _configure_square_burst(gen, burst_fps, int(n_frames), float(duty), float(amplitude), float(offset))
        try:
            print(
                f"[INFO] Generator read‑back: freq={gen.frequency}Hz amp={gen.amplitude}V off={gen.offset}V"
            )
            try:
                print(f"[INFO] Generator symmetry read‑back: {gen.symmetry}")
            except Exception:
                pass
        except Exception:
            pass
        # Arm TiePie in *triggered burst* mode before sending the PAT.  This is
        # the critical change: lev.send_message() can block for roughly the whole
        # motion duration, so starting the burst *after* send_message causes the
        # camera to watch the wrong time window.  By arming the generator on
        # EXT1/EXT2 first, the actual PAT sync edge starts the burst in hardware.
        _configure_generator_trigger_input(gen, edge=GEN_TRIGGER_EDGE)
        accept_frames_evt.set()
        print(f"[INFO] Arming TiePie triggered burst on EXT input ({GEN_TRIGGER_EDGE} edge)…")
        gen.start()
        # Use software PAT send time as the reference origin for logging.
        rec_start_pc_ns = time.perf_counter_ns()
        rec_start_wall_ns = time.time_ns()
        try:
            log_q.put_nowait(
                (
                    "EVENT",
                    "",
                    "",
                    f"{0.0:.9f}",
                    int(rec_start_pc_ns),
                    int(rec_start_wall_ns),
                    "",
                    "SOFTWARE_PAT_SEND",
                    f"expected_frames={n_frames}, requested_fps={requested_fps}",
                )
            )
        except Exception:
            pass
        # Issue PAT send_message (hardware looping).  The TiePie burst is already
        # armed, so the external sync burst will start at the actual PAT sync edge
        # even if this call blocks in Python.
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
        t0 = time.perf_counter()
        # Timeout calculation: give generous margin if timeout <= 0
        timeout_sec = float((n_frames / max(burst_fps, 1e-9)) + 8.0)
        while not stop_evt.is_set():
            if (time.perf_counter() - t0) > timeout_sec:
                print("[WARN] Timeout waiting for all frames; forcing stop")
                stop_evt.set()
                break
            time.sleep(0.002)
        accept_frames_evt.clear()
        # Wait for in‑flight callbacks to finish
        tw = time.perf_counter()
        while True:
            with cb_counter_lock:
                inflight_now = cb_inflight
            if inflight_now <= 0:
                break
            if (time.perf_counter() - tw) > 3.0:
                print(f"[WARN] Timed out waiting for in‑flight callbacks: {inflight_now}")
                break
            time.sleep(0.001)
        # Hold DC low again after capture to reset
        _set_dc_low(gen)
        time.sleep(max(0.0, low_hold_sec))
        # Flush queues and close threads
        try:
            frame_q.join()
            log_q.join()
        except Exception:
            pass
        frame_q.put(None)
        log_q.put(None)
        wt.join()
        lt.join()
        cam.endXfer()
        if gpu_enabled:
            try:
                dec.teardownGPUDecode()
            except Exception:
                pass
        cam.close()
        raw_fp.close()
        csv_fp.close()
        # Build meta information
        meta: Dict[str, Any] = {
            "raw_path": raw_path,
            "csv_path": csv_path,
            "width": rw,
            "height": rh,
            "fps": float(actual_cam_fps),
            "requested_fps": float(requested_fps),
            "frames_in": int(frames_in),
            "frames_written": int(frames_written),
            "frames_logged": int(frames_logged),
            "queue_drop": int(frames_dropped_queue),
            "log_rows_dropped": int(log_rows_dropped),
            "roi": {"x": rx, "y": ry, "w": rw, "h": rh},
            "timestamp": ts,
            "cam_timestamp_attr": cam_ts_attr,
            "cam_timestamp_unit_guess": cam_ts_unit,
            "rec_start_pc_ns": int(rec_start_pc_ns) if rec_start_pc_ns is not None else None,
            "rec_start_wall_ns": int(rec_start_wall_ns) if rec_start_wall_ns is not None else None,
        }
        # Merge extra meta data if provided
        if extra_meta:
            meta.update(extra_meta)
        # Write meta JSON
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        # Optionally encode to AVI
        avi_generated = None
        if encode_after:
            try:
                print("[INFO] Encoding RAW -> AVI after capture…")
                avi_generated = _encode_raw_to_avi(raw_path, meta_path, avi_path, fps=encode_fps)
                print(f"[INFO] AVI: {avi_generated}")
            except Exception as e:
                print(f"[WARN] RAW->AVI encoding failed: {e}")
        if avi_generated:
            meta["avi_path"] = avi_generated
        else:
            meta["avi_path"] = None
        return meta
    finally:
        # Ensure generator is turned off
        try:
            gen.stop()
        except Exception:
            pass
        try:
            gen.output_enable = False
        except Exception:
            pass
        try:
            del gen
        except Exception:
            pass

    if not src_avi or not os.path.isfile(src_avi):
        print(f"[WARN] make_pat_clip: source AVI '{src_avi}' not found")
        return None
    if not csv_path or not os.path.isfile(csv_path):
        print(f"[WARN] make_pat_clip: CSV '{csv_path}' not found")
        return None

    # compute output path
    root, ext = os.path.splitext(src_avi)
    out_path = root + str(out_suffix)

    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        print(f"[WARN] make_pat_clip: failed to read CSV: {e}")
        return None

    # find PAT_START event
    # try:
    #     row = df[(df.kind == "EVENT") & (df.event == "PAT_START")].iloc[0]
    #     t_pat = int(row["perf_counter_ns"])
    # except Exception:
    #     print("[WARN] make_pat_clip: PAT_START event not found in CSV")
    #     return None

    # find PHYSICAL_PAT_START (or fallback to SOFTWARE_PAT_SEND)
    try:
        events = df[(df.kind == "EVENT") & ((df.event == "PHYSICAL_PAT_START") | (df.event == "SOFTWARE_PAT_SEND"))]
        
        if (events.event == "PHYSICAL_PAT_START").any():
            row = events[events.event == "PHYSICAL_PAT_START"].iloc[0]
            print("[INFO] make_pat_clip: Using accurate PHYSICAL hardware trigger")
        else:
            row = events[events.event == "SOFTWARE_PAT_SEND"].iloc[0]
            print("[INFO] make_pat_clip: Hardware trigger not found, falling back to SOFTWARE trigger")
            
        t_pat = int(row["perf_counter_ns"])
    except Exception:
        print("[WARN] make_pat_clip: trigger events not found in CSV")
        return None

    # isolate frame rows
    frames = df[df.kind == "FRAME"].copy()
    try:
        frames["perf_counter_ns"] = frames["perf_counter_ns"].astype(np.int64)
    except Exception:
        # if conversion fails, fallback to python int conversion
        frames["perf_counter_ns"] = frames["perf_counter_ns"].apply(int)

    # remove duplicate sequence numbers if present
    if "seq" in frames.columns and frames["seq"].notna().any():
        frames = frames.drop_duplicates(subset=["seq"], keep="first")

    # assign frame_index column (use 'index' if present)
    if "index" in frames.columns and frames["index"].notna().any():
        frames["frame_index"] = frames["index"].astype(np.int64)
    else:
        frames = frames.sort_values("perf_counter_ns")
        frames["frame_index"] = np.arange(len(frames), dtype=np.int64)

    frames = frames.sort_values("frame_index")

    # compute start and end perf_counter_ns range
    t0 = t_pat
    t1 = t_pat + int((expected_s + clip_tail_sec) * 1e9)

    pc = frames["perf_counter_ns"].to_numpy(np.int64)
    fi = frames["frame_index"].to_numpy(np.int64)

    # start index: first frame with perf_counter >= t0
    spos = int(np.searchsorted(pc, t0, side="left"))
    # end index: last frame with perf_counter <= t1
    epos = int(np.searchsorted(pc, t1, side="right") - 1)
    epos = max(epos, spos)
    start_idx = int(fi[spos])
    end_idx = int(fi[epos])

    print(f"[PAT_CLIP] clip frames: {start_idx} to {end_idx}, count={end_idx - start_idx + 1}")

    # open source video
    cap = cv2.VideoCapture(src_avi)
    if not cap.isOpened():
        print("[WARN] make_pat_clip: cannot open source AVI")
        return None
    # position to start frame
    cap.set(cv2.CAP_PROP_POS_FRAMES, float(start_idx))
    ok, frame0 = cap.read()
    if not ok:
        print("[WARN] make_pat_clip: cannot read start frame")
        cap.release()
        return None
    h, w = frame0.shape[:2]
    is_color = not (frame0.ndim == 2)
    # use same fps as source
    fps_out = cap.get(cv2.CAP_PROP_FPS) or 1000.0
    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
    wr = cv2.VideoWriter(out_path, fourcc, float(fps_out), (w, h), isColor=is_color)
    # write first frame
    wr.write(frame0)
    cur = start_idx + 1
    while cur <= end_idx:
        ok, fr = cap.read()
        if not ok:
            break
        wr.write(fr)
        cur += 1
    wr.release()
    cap.release()
    print(f"[PAT_CLIP] wrote: {out_path}")
    return out_path


def focus_preview_loop(recorder: "InfinicamRecorder") -> bool:
    """フォーカス合わせ用のライブプレビュー。

    - q : プレビューを閉じて続行
    - Esc : 全体終了

    Returns:
        True  -> continue
        False -> quit
    """

    if recorder is None:
        print("[WARN] preview requested but recorder is None")
        return True
    if not (recorder.preview_enabled and ENABLE_PREVIEW):
        print("[WARN] preview is disabled (ENABLE_PREVIEW=False)")
        return True

    win = str(FOCUS_PREVIEW_WINDOW_NAME)
    try:
        cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    except Exception:
        pass

    print("[PREVIEW] Live view for focusing: press 'q' to close, 'Esc' to quit.")

    # display fps (imshow side)
    disp_n = 0
    disp_fps = 0.0
    t0 = time.perf_counter()

    while True:
        frame = recorder.get_preview_frame()

        if frame is not None:
            disp_n += 1
            now = time.perf_counter()
            if now - t0 >= 0.5:
                dt = now - t0
                if dt > 0:
                    disp_fps = disp_n / dt
                disp_n = 0
                t0 = now

            # overlay (grayscale safe)
            color = 255 if getattr(frame, "ndim", 2) == 2 else (255, 255, 255)
            status = "REC" if recorder.is_saving.is_set() else "LIVE"
            cv2.putText(
                frame,
                f"{status}  preview_fps~{disp_fps:4.1f}  ROI={recorder.roi_w}x{recorder.roi_h}  (q:close, Esc:quit)",
                (10, 30),
                cv2.FONT_HERSHEY_PLAIN,
                1.6,
                color,
                1,
            )
            try:
                cv2.imshow(win, frame)
            except Exception:
                pass

        k = cv2.waitKey(1) & 0xFF
        if k == ord("q"):
            try:
                cv2.destroyWindow(win)
            except Exception:
                pass
            return True
        if k == 27:  # Esc
            try:
                cv2.destroyWindow(win)
            except Exception:
                pass
            return False

        time.sleep(0.001)


# =========================
# Camera recorder (single-process)
# =========================


@dataclass
class RecorderSummary:
    rec_start_pc_ns: int
    rec_stop_pc_ns: int
    rec_start_wall_ns: int
    rec_stop_wall_ns: int
    first_frame_pc_ns: Optional[int]
    last_frame_pc_ns: Optional[int]
    frames_seen: int
    frames_written: int
    frames_logged: int
    decoded_ok: int
    decode_failed: int
    decode_skipped_bp: int
    enqueued_ok: int
    dropped_queue_full: int
    missing_seq_total: int
    last_seq: Optional[int]
    timestamps_csv: Optional[str]
    dropped_log_rows: int

    def duration_sec(self) -> Optional[float]:
        if self.first_frame_pc_ns is None or self.last_frame_pc_ns is None:
            return None
        if self.last_frame_pc_ns <= self.first_frame_pc_ns:
            return None
        return (self.last_frame_pc_ns - self.first_frame_pc_ns) * 1e-9


class InfinicamRecorder:

    """INFINICAM を beginXfer 常時ストリームし、必要な区間だけ保存する"""

    def __init__(
        self,
        *,
        save_dir: str,
        roi=None,
        decode_threads: Optional[int] = None,
        use_gpu_if_available: bool = True,
        save_queue_max: int = 256,
        queue_drop_margin: int = 2,
        raw_mode: bool = False,
        preview_enabled: bool = True,
    ):
        self.save_dir = os.path.abspath(save_dir)
        safe_mkdir(self.save_dir)

        self.raw_mode = bool(raw_mode)
        self.queue_drop_margin = int(queue_drop_margin)

        # camera
        print(pypuclib.__doc__)
        self.cam = CameraFactory().create()
        self.applied_fps = prompt_and_apply_cam_fps(self.cam)

        # ring buffer
        try:
            if hasattr(self.cam, "setRingBufferCount"):
                self.cam.setRingBufferCount(int(RING_BUFFER_COUNT))
                print(f"[INFO] camera ring buffer = {RING_BUFFER_COUNT}")
        except Exception as e:
            print(f"[WARN] setRingBufferCount failed: {e}")

        self.dec = self.cam.decoder()
        self.reso = self.cam.resolution()

        # ROI
        # self.roi_x, self.roi_y, self.roi_w, self.roi_h = normalize_roi(roi, self.reso)


        # self.roi_w = 256
        # self.roi_h = 256

        # ALIGN = 16  # 4でも良いが、迷ったら16が無難
        # self.roi_x = ((self.reso.width  - self.roi_w) // 2) & ~(ALIGN - 1)
        # self.roi_y = ((self.reso.height - self.roi_h) // 2) & ~(ALIGN - 1)

        # # 念のためクランプ（はみ出し防止）
        # self.roi_x = max(0, min(self.roi_x, self.reso.width  - self.roi_w))
        # self.roi_y = max(0, min(self.roi_y, self.reso.height - self.roi_h))

        # ROI
        # roi 引数は None / (x,y,w,h) / (w,h) を許可
        if roi is None:
            self.roi_x, self.roi_y, self.roi_w, self.roi_h = 0, 0, min(512, self.reso.width), min(512, self.reso.height)

        elif isinstance(roi, (tuple, list)) and len(roi) == 2:
            # --- (w,h) shorthand = centered crop (stable recipe) ---
            self.roi_w = int(roi[0])
            self.roi_h = int(roi[1])

            ALIGN = 16  # ここが重要（この値はあなたの環境で効いている）
            self.roi_x = ((int(self.reso.width)  - self.roi_w) // 2) & ~(ALIGN - 1)
            self.roi_y = ((int(self.reso.height) - self.roi_h) // 2) & ~(ALIGN - 1)

            # clamp（はみ出し防止）
            self.roi_x = max(0, min(self.roi_x, int(self.reso.width)  - self.roi_w))
            self.roi_y = max(0, min(self.roi_y, int(self.reso.height) - self.roi_h))

        elif isinstance(roi, (tuple, list)) and len(roi) == 4:
            self.roi_x, self.roi_y, self.roi_w, self.roi_h = map(int, roi)

            # 念のため clamp
            self.roi_w = max(2, min(self.roi_w, int(self.reso.width)))
            self.roi_h = max(2, min(self.roi_h, int(self.reso.height)))
            self.roi_x = max(0, min(self.roi_x, int(self.reso.width)  - self.roi_w))
            self.roi_y = max(0, min(self.roi_y, int(self.reso.height) - self.roi_h))

        else:
            raise ValueError(f"Invalid ROI format: {roi!r} (use None, (x,y,w,h), or (w,h))")

        print(f"[INFO] ROI: x={self.roi_x}, y={self.roi_y}, w={self.roi_w}, h={self.roi_h} (camera reso {self.reso.width}x{self.reso.height})")



        try:
            print(
                f"[INFO] ROI: x={self.roi_x}, y={self.roi_y}, w={self.roi_w}, h={self.roi_h} "
                f"(camera reso {int(self.reso.width)}x{int(self.reso.height)})"
            )
        except Exception:
            print(f"[INFO] ROI: x={self.roi_x}, y={self.roi_y}, w={self.roi_w}, h={self.roi_h}")

        # GPU decode
        self.use_gpu = False
        try:
            if use_gpu_if_available and self.dec.getAvailableGPUProcess():
                self.dec.setupGPUDecode(GPUSetup(self.reso.width, self.reso.height))
                self.use_gpu = True
                print("[INFO] GPU decode enabled")
            else:
                print("[INFO] CPU decode")
        except Exception as e:
            print(f"[WARN] GPU setup failed -> CPU decode: {e}")
            self.use_gpu = False

        # decode threads
        if decode_threads is None:
            try:
                import multiprocessing

                decode_threads = max(1, min(12, multiprocessing.cpu_count()))
            except Exception:
                decode_threads = 4
        self.decode_threads = int(decode_threads)
        try:
            self.dec.setNumDecodeThread(self.decode_threads)
        except Exception as e:
            print(f"[WARN] setNumDecodeThread failed: {e}")

        # writer
        self.writer_lock = threading.Lock()
        self.writer = cv2.VideoWriter()
        self.raw_fp = None
        self.target_path: Optional[str] = None

        # state
        self.is_saving = threading.Event()
        self.stop_evt = threading.Event()

        self.preview_enabled = bool(preview_enabled)
        self.preview_lock = threading.Lock()
        self.preview_frame = None
        self._last_preview_ns = 0

        # stats (per recording)
        # timestamp log (per recording run; created in start_recording)
        self.log_q: Optional[queue.Queue] = None
        self.log_thread: Optional[threading.Thread] = None
        self.log_fp = None
        self.log_writer = None
        self.timestamps_csv: Optional[str] = None
        self.frames_logged = 0
        self.dropped_log_rows = 0
        self._log_zero_pc_ns = 0

        # camera-provided timestamp (if available on xferdata)
        self._cam_ts_attr: Optional[str] = None
        self._cam_ts_checked: bool = False

        # guard: wait for in-flight callbacks that saw saving=True
        self._cb_counter_lock = threading.Lock()
        self._saving_cb_inflight = 0

        self._reset_stats()

        # queue + writer thread
        self.save_q: queue.Queue = queue.Queue(maxsize=int(save_queue_max))
        self.t_writer = threading.Thread(target=self._writer_loop, daemon=True)
        self.t_writer.start()

        # start transfer
        self.cam.beginXfer(self._xfer_callback)
        time.sleep(0.2)  # warmup

    def _reset_stats(self) -> None:
        self.frames_seen = 0
        self.frames_written = 0
        self.frames_logged = 0
        self.decoded_ok = 0
        self.decode_failed = 0
        self.decode_skipped_bp = 0
        self.enqueued_ok = 0
        self.dropped_queue_full = 0
        self.missing_seq_total = 0

        self.dropped_log_rows = 0
        self.log_dup_skipped = 0  # duplicates skipped at CSV writer

        self.last_seqno_seen: Optional[int] = None
        self.last_seqno_cb: Optional[int] = None
        self.first_frame_pc_ns: Optional[int] = None
        self.last_frame_pc_ns: Optional[int] = None

        self.rec_start_pc_ns = 0
        self.rec_stop_pc_ns = 0
        self.rec_start_wall_ns = 0
        self.rec_stop_wall_ns = 0

        self.timestamps_csv = None

    # ---- timestamp log helpers ----

    def _log_put_drop_old(self, item: tuple) -> None:
        """ログキューが満杯なら最古を捨てて入れる（join が詰まらないよう task_done も調整）"""
        if not ENABLE_TIMESTAMP_LOG:
            return
        if self.log_q is None:
            return
        try:
            self.log_q.put_nowait(item)
            return
        except queue.Full:
            self.dropped_log_rows += 1
            try:
                _ = self.log_q.get_nowait()
                try:
                    self.log_q.task_done()
                except Exception:
                    pass
            except queue.Empty:
                pass
            try:
                self.log_q.put_nowait(item)
            except Exception:
                # give up
                return

    def log_event(self, name: str, pc_ns: Optional[int] = None, wall_ns: Optional[int] = None, note: str = "") -> None:
        """メインスレッドからイベント(PAT_START 等)を timestamps.csv に書き込む"""
        if not ENABLE_TIMESTAMP_LOG:
            return
        if self.log_q is None:
            return
        if pc_ns is None:
            pc_ns = time.perf_counter_ns()
        if wall_ns is None:
            wall_ns = time.time_ns()
        self._log_put_drop_old(("EVENT", str(name), int(pc_ns), int(wall_ns), str(note) if note else ""))

    def _start_timestamp_logger(self, safe_base: str, ts: str) -> None:
        if not ENABLE_TIMESTAMP_LOG:
            return

        # reset per-run log state
        self.frames_logged = 0
        self.dropped_log_rows = 0
        self.log_dup_skipped = 0  # duplicates skipped at CSV writer (per run)

        self.timestamps_csv = os.path.join(self.save_dir, f"{safe_base}_{ts}_timestamps.csv")
        self._log_zero_pc_ns = int(self.rec_start_pc_ns)

        self.log_q = queue.Queue(maxsize=int(LOG_QUEUE_MAX))
        self.log_fp = open(self.timestamps_csv, "w", newline="", encoding="utf-8")
        self.log_writer = csv.writer(self.log_fp)

        # Header:
        # kind: FRAME / EVENT
        # index: frame index (0..), only for FRAME
        # seq: camera sequenceNo (only for FRAME)
        # t_rel_sec: perf_counter relative to rec_start
        # perf_counter_ns / wall_clock_ns: raw timestamps
        # cam_timestamp: optional (if SDK provides)
        # event: event name for EVENT
        # note: optional extra
        self.log_writer.writerow(
            [
                "kind",
                "index",
                "seq",
                "t_rel_sec",
                "perf_counter_ns",
                "wall_clock_ns",
                "cam_timestamp",
                "event",
                "note",
            ]
        )
        try:
            self.log_fp.flush()
        except Exception:
            pass

        def _log_loop() -> None:
            frame_idx = 0
            seen_seq = set()  # de-duplicate by sequenceNo within a run
            last_seq_written = None  # for wrap-around handling
            while True:
                item = self.log_q.get()
                try:
                    if item is None:
                        break

                    kind = item[0]
                    if kind == "FRAME":
                        _, seq, pc_ns, wall_ns, cam_ts = item
                        # De-dup: some SDK builds may log the same sequenceNo multiple times.
                        # Handle rare sequenceNo wrap-around within a long run.
                        if last_seq_written is not None:
                            try:
                                if int(last_seq_written) > 60000 and int(seq) < 1000:
                                    seen_seq.clear()
                            except Exception:
                                pass
                        if int(seq) in seen_seq:
                            self.log_dup_skipped += 1
                            continue
                        seen_seq.add(int(seq))
                        last_seq_written = int(seq)
                        t_rel = (int(pc_ns) - int(self._log_zero_pc_ns)) * 1e-9
                        self.log_writer.writerow(
                            [
                                "FRAME",
                                frame_idx,
                                int(seq),
                                f"{t_rel:.9f}",
                                int(pc_ns),
                                int(wall_ns),
                                "" if cam_ts is None else cam_ts,
                                "",
                                "",
                            ]
                        )
                        frame_idx += 1
                        self.frames_logged = frame_idx
                    elif kind == "EVENT":
                        _, ev, pc_ns, wall_ns, note = item
                        t_rel = (int(pc_ns) - int(self._log_zero_pc_ns)) * 1e-9
                        self.log_writer.writerow(
                            [
                                "EVENT",
                                "",
                                "",
                                f"{t_rel:.9f}",
                                int(pc_ns),
                                int(wall_ns),
                                "",
                                str(ev),
                                str(note) if note else "",
                            ]
                        )
                    else:
                        # unknown -> dump
                        self.log_writer.writerow(list(item))

                finally:
                    try:
                        self.log_q.task_done()
                    except Exception:
                        pass

            # close
            try:
                self.log_fp.flush()
            except Exception:
                pass
            try:
                self.log_fp.close()
            except Exception:
                pass

        self.log_thread = threading.Thread(target=_log_loop, daemon=True)
        self.log_thread.start()

        # mark camera start event
        self._log_put_drop_old(("EVENT", "CAM_REC_START", int(self.rec_start_pc_ns), int(self.rec_start_wall_ns), ""))

    def _stop_timestamp_logger(self) -> None:
        if not ENABLE_TIMESTAMP_LOG:
            return
        if self.log_q is None:
            return

        # Ensure no more callback-generated logs can arrive *after* the sentinel.
        # Wait briefly for in-flight callbacks that entered saving branch to finish.
        t0 = time.perf_counter()
        while True:
            with self._cb_counter_lock:
                n = int(self._saving_cb_inflight)
            if n <= 0:
                break
            if (time.perf_counter() - t0) > 1.0:
                print(f"[WARN] waiting for in-flight saving callbacks timed out (n={n})")
                break
            time.sleep(0.001)

        # mark camera stop event (timestamp value is already captured)
        self._log_put_drop_old(("EVENT", "CAM_REC_STOP", int(self.rec_stop_pc_ns), int(self.rec_stop_wall_ns), ""))

        # close sentinel
        try:
            self.log_q.put(None, timeout=1.0)
        except Exception:
            try:
                self.log_q.put_nowait(None)
            except Exception:
                pass

        # wait drain
        try:
            self.log_q.join()
        except Exception:
            pass
        try:
            if self.log_thread is not None:
                self.log_thread.join(timeout=2.0)
        except Exception:
            pass

        # clear refs
        self.log_q = None
        self.log_thread = None
        self.log_fp = None
        self.log_writer = None

    def _q_put_drop_old(self, item: Tuple) -> None:
        """Queueが満杯なら最古を捨てて入れる（ドロップ回数は呼び出し側で数える）"""
        try:
            self.save_q.put_nowait(item)
            return
        except queue.Full:
            try:
                _ = self.save_q.get_nowait()
                # 捨てた分の unfinished_tasks を減らす
                try:
                    self.save_q.task_done()
                except Exception:
                    pass
            except queue.Empty:
                pass
            try:
                self.save_q.put_nowait(item)
            except queue.Full:
                return

    def _xfer_callback(self, xferdata) -> None:
        if self.stop_evt.is_set():
            return

        saving = self.is_saving.is_set()
        seq = xferdata.sequenceNo()

        # dedup guard: some SDK builds may invoke callback multiple times for the same sequenceNo
        # (e.g., internal buffering / scheduling). This inflates FPS estimates and duplicates frames.
        if self.last_seqno_cb is not None:
            try:
                last = int(self.last_seqno_cb)
                cur = int(seq)
                if cur == last:
                    return
                # rough 16-bit wrap-around handling
                if last > 60000 and cur < 1000:
                    pass
            except Exception:
                pass
        self.last_seqno_cb = int(seq)

        # 録画中のみ統計を取る（同期目的）
        if saving:
            # in-flight guard (avoid closing log while callback still writing)
            with self._cb_counter_lock:
                self._saving_cb_inflight += 1
            try:
                # --- timing log (as early as possible; BEFORE decode) ---
                pc_ns = time.perf_counter_ns()
                wall_ns = time.time_ns()

                cam_ts_val = None
                if ENABLE_TIMESTAMP_LOG and LOG_INCLUDE_CAM_TIMESTAMP:
                    if not self._cam_ts_checked:
                        # try a broad set of likely attribute names
                        for a in (
                            "timeStamp", "timestamp", "getTimestamp", "frameTime", "frame_timestamp",
                            "deviceTimestamp", "deviceTimeStamp", "captureTime", "captureTimestamp",
                            "timeStampUs", "timeStampNS", "timeStampNs", "timeStamp10ns"
                        ):
                            if hasattr(xferdata, a):
                                self._cam_ts_attr = a
                                break
                        self._cam_ts_checked = True
                        if self._cam_ts_attr is None:
                            try:
                                for name in dir(xferdata):
                                    n = name.lower()
                                    if ("time" in n or "stamp" in n) and callable(getattr(xferdata, name, None)):
                                        if any(k in n for k in ("stamp", "timestamp", "frametime", "capture")):
                                            self._cam_ts_attr = name
                                            break
                            except Exception:
                                pass
                    if self._cam_ts_attr:
                        try:
                            cam_ts_val = getattr(xferdata, self._cam_ts_attr)()
                        except Exception:
                            cam_ts_val = None

                self._log_put_drop_old(("FRAME", int(seq), int(pc_ns), int(wall_ns), cam_ts_val))

                # --- seq/drop stats ---
                if self.last_seqno_seen is not None and seq > self.last_seqno_seen + 1:
                    self.missing_seq_total += int(seq - self.last_seqno_seen - 1)
                self.last_seqno_seen = int(seq)

                self.frames_seen += 1
                if self.first_frame_pc_ns is None:
                    self.first_frame_pc_ns = int(pc_ns)
                self.last_frame_pc_ns = int(pc_ns)

                # backpressure: キューが詰まり気味なら decode 自体をスキップ
                if self.save_q.qsize() >= (self.save_q.maxsize - self.queue_drop_margin):
                    self.decode_skipped_bp += 1
                    return

                # decode (ROI)
                try:
                    img = self.dec.decode(xferdata, self.roi_x, self.roi_y, self.roi_w, self.roi_h)
                    self.decoded_ok += 1
                except Exception:
                    self.decode_failed += 1
                    return

                # queueへ（デコーダ内部バッファ再利用対策で copy）
                frame = img.copy()

                # writer thread が task_done する前提で enqueue
                try:
                    self.save_q.put_nowait(("frame", seq, frame))
                    self.enqueued_ok += 1
                except queue.Full:
                    self.dropped_queue_full += 1
                    self._q_put_drop_old(("frame", seq, frame))

                # 録画中プレビュー抑止が基本
                if self.preview_enabled and PREVIEW_WHILE_SAVING:
                    prev = frame
                    if PREVIEW_DOWNSCALE and PREVIEW_DOWNSCALE >= 2:
                        try:
                            prev = cv2.resize(prev, (self.roi_w // PREVIEW_DOWNSCALE, self.roi_h // PREVIEW_DOWNSCALE))
                        except Exception:
                            pass
                    with self.preview_lock:
                        self.preview_frame = prev
                return
            finally:
                with self._cb_counter_lock:
                    self._saving_cb_inflight -= 1

        # 未録画時：必要ならプレビューだけ更新（間引き）
        if not (self.preview_enabled and ENABLE_PREVIEW):
            return

        now_ns = time.perf_counter_ns()
        if now_ns - self._last_preview_ns < int(1e9 / max(1, PREVIEW_MAX_FPS)):
            return
        self._last_preview_ns = now_ns

        try:
            img = self.dec.decode(xferdata, self.roi_x, self.roi_y, self.roi_w, self.roi_h)
        except Exception:
            return

        if PREVIEW_DOWNSCALE and PREVIEW_DOWNSCALE >= 2:
            try:
                img = cv2.resize(img, (self.roi_w // PREVIEW_DOWNSCALE, self.roi_h // PREVIEW_DOWNSCALE))
            except Exception:
                pass
        with self.preview_lock:
            self.preview_frame = img

    def _writer_loop(self) -> None:
        """非同期writer"""
        # NOTE:
        # stop_evt は callback 停止用。
        # writer はキューを吐き切ってから終了したいので、ここでは stop_evt を見ず、
        # 明示的な "quit" トークンで終了する。
        while True:
            try:
                item = self.save_q.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                kind = item[0]
                if kind == "close":
                    # close writer/raw
                    with self.writer_lock:
                        if self.writer.isOpened():
                            self.writer.release()
                        if self.raw_fp is not None:
                            try:
                                self.raw_fp.flush()
                                self.raw_fp.close()
                            except Exception:
                                pass
                            self.raw_fp = None
                    continue

                if kind == "quit":
                    break

                # frame
                _, seq, frame = item

                # writer open (start_recording で open するが、念のため)
                with self.writer_lock:
                    if self.raw_mode:
                        if self.raw_fp is None:
                            # ここに来るなら start_recording 失敗
                            pass
                        else:
                            try:
                                self.raw_fp.write(frame.tobytes())
                                self.frames_written += 1
                            except Exception:
                                pass
                    else:
                        if not self.writer.isOpened():
                            # ここに来るなら start_recording 失敗
                            pass
                        else:
                            try:
                                self.writer.write(frame)
                                self.frames_written += 1
                            except Exception:
                                pass

            finally:
                try:
                    self.save_q.task_done()
                except Exception:
                    pass

        # quit: ensure writer closed
        with self.writer_lock:
            try:
                if self.writer.isOpened():
                    self.writer.release()
            except Exception:
                pass
            try:
                if self.raw_fp is not None:
                    self.raw_fp.flush(); self.raw_fp.close(); self.raw_fp = None
            except Exception:
                pass

    def start_recording(self, basename: str, *, extra_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """録画を開始（同一プロセス内でPAT開始と近接させるため、ここで writer を open する）"""
        if self.is_saving.is_set():
            raise RuntimeError("Recording already active")

        self._reset_stats()

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_base = basename.replace(" ", "_")

        if self.raw_mode:
            path = os.path.join(self.save_dir, f"{safe_base}_{ts}{RAW_SUFFIX}")
        else:
            path = os.path.join(self.save_dir, f"{safe_base}_{ts}.avi")

        meta_path = os.path.join(self.save_dir, f"{safe_base}_{ts}_session.json")

        # open writer/file first
        with self.writer_lock:
            self.target_path = os.path.abspath(path)
            if self.raw_mode:
                self.raw_fp = open(self.target_path, "wb")
            else:
                # ok = self.writer.open(
                #     self.target_path,
                #     CODEC,
                #     float(self._header_fps()),
                #     (int(self.roi_w), int(self.roi_h)),
                #     False,
                # )
                # if not ok:
                #     self.target_path = None
                #     raise RuntimeError("cv2.VideoWriter.open failed")

                # 直前にサンプルフレームから isColor を推定（プレビューが有効なら取れる）
                sample = self.get_preview_frame()
                is_color = bool(sample is not None and getattr(sample, "ndim", 2) == 3 and sample.shape[2] >= 3)

                ok = self.writer.open(
                    self.target_path,
                    CODEC,
                    float(self._header_fps()),
                    (int(self.roi_w), int(self.roi_h)),
                    is_color,
                )
                if not ok:
                    # 予備：codec を変えて再試行（環境によって効く）
                    for fourcc in ("XVID", "MP4V", "MJPG"):
                        try:
                            tmp = cv2.VideoWriter_fourcc(*fourcc)
                            ok = self.writer.open(self.target_path, tmp, float(self._header_fps()),
                                                (int(self.roi_w), int(self.roi_h)), is_color)
                            if ok:
                                print(f"[WARN] VideoWriter fallback codec -> {fourcc}")
                                break
                        except Exception:
                            pass

                if not ok:
                    self.target_path = None
                    raise RuntimeError(f"cv2.VideoWriter.open failed (path_len={len(os.path.abspath(path))})")

        # capture start timestamps (used as t=0 for timestamps.csv)
        self.rec_start_pc_ns = time.perf_counter_ns()
        self.rec_start_wall_ns = time.time_ns()

        # start timestamps.csv logger BEFORE enabling saving (so early frames can be logged)
        try:
            self._start_timestamp_logger(safe_base, ts)
        except Exception as e:
            print(f"[WARN] failed to start timestamp logger: {e}")

        self.is_saving.set()

        meta: Dict[str, Any] = {
            "timestamp": datetime.datetime.now().isoformat(),
            "save_dir": self.save_dir,
            "path": self.target_path,
            "raw_mode": self.raw_mode,
            "codec": "RAW" if self.raw_mode else "MJPG",
            "video_header_fps": self._header_fps(),
            "roi": {"x": int(self.roi_x), "y": int(self.roi_y), "w": int(self.roi_w), "h": int(self.roi_h)},
            "decode_threads": int(self.decode_threads),
            "gpu_decode": bool(self.use_gpu),
            "ring_buffer": int(RING_BUFFER_COUNT),
            "rec_start_perf_counter_ns": int(self.rec_start_pc_ns),
            "rec_start_wall_clock_ns": int(self.rec_start_wall_ns),
            "timestamps_csv": self.timestamps_csv,
        }
        if extra_meta:
            meta.update(extra_meta)

        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[WARN] failed to write meta json: {e}")

        print(f"[INFO] INFINICAM: start recording -> {self.target_path}")
        if self.timestamps_csv:
            print(f"[INFO] timestamps csv -> {self.timestamps_csv}")
        return meta

    def stop_recording(self) -> RecorderSummary:
        """録画停止：callback の enqueue を止め、writer と timestamps.csv を flush"""
        if not self.is_saving.is_set():
            raise RuntimeError("Recording is not active")

        # stop enqueueing new frames (callback side)
        self.is_saving.clear()

        # capture stop timestamps
        self.rec_stop_pc_ns = time.perf_counter_ns()
        self.rec_stop_wall_ns = time.time_ns()

        # writer close token（キュー満杯なら古いフレームを捨ててでも入れる）
        self._q_put_drop_old(("close",))

        # キューが吐けるまで待つ
        try:
            self.save_q.join()
        except Exception:
            pass

        # 念のため release
        with self.writer_lock:
            try:
                if self.writer.isOpened():
                    self.writer.release()
            except Exception:
                pass
            if self.raw_fp is not None:
                try:
                    self.raw_fp.flush()
                    self.raw_fp.close()
                except Exception:
                    pass
                self.raw_fp = None

        # stop timestamps logger (drain & close)
        try:
            self._stop_timestamp_logger()
        except Exception as e:
            print(f"[WARN] failed to stop timestamp logger: {e}")

        summ = RecorderSummary(
            rec_start_pc_ns=int(self.rec_start_pc_ns),
            rec_stop_pc_ns=int(self.rec_stop_pc_ns),
            rec_start_wall_ns=int(self.rec_start_wall_ns),
            rec_stop_wall_ns=int(self.rec_stop_wall_ns),
            first_frame_pc_ns=self.first_frame_pc_ns,
            last_frame_pc_ns=self.last_frame_pc_ns,
            frames_seen=int(self.frames_seen),
            frames_written=int(self.frames_written),
            frames_logged=int(self.frames_logged),
            decoded_ok=int(self.decoded_ok),
            decode_failed=int(self.decode_failed),
            decode_skipped_bp=int(self.decode_skipped_bp),
            enqueued_ok=int(self.enqueued_ok),
            dropped_queue_full=int(self.dropped_queue_full),
            missing_seq_total=int(self.missing_seq_total),
            last_seq=int(self.last_seqno_seen) if self.last_seqno_seen is not None else None,
            timestamps_csv=self.timestamps_csv,
            dropped_log_rows=int(self.dropped_log_rows),
        )

        print(
            "[INFO] INFINICAM: stop recording. "
            f"frames_written={summ.frames_written}, seen={summ.frames_seen}, logged={summ.frames_logged}, "
            f"skipped={summ.decode_skipped_bp}, qdrop={summ.dropped_queue_full}, missing_seq={summ.missing_seq_total}, "
            f"logdrop={summ.dropped_log_rows}"
        )
        return summ

    def _header_fps(self) -> int:
        # VideoWriter header fps. If camera fps applied, match it.
        if self.applied_fps and self.applied_fps > 0:
            return int(self.applied_fps)
        # fallback
        try:
            cur = self.cam.framerate() if hasattr(self.cam, "framerate") else None
            if cur:
                return int(cur)
        except Exception:
            pass
        return 1000

    def get_preview_frame(self):
        with self.preview_lock:
            return None if self.preview_frame is None else self.preview_frame.copy()

    def close(self) -> None:
        # stop recording if active
        try:
            if self.is_saving.is_set():
                self.stop_recording()
        except Exception:
            pass

        # stop transfer
        self.stop_evt.set()
        try:
            self.cam.endXfer()
        except Exception as e:
            print(f"[WARN] cam.endXfer failed: {e}")

        # stop writer thread
        self._q_put_drop_old(("quit",))
        try:
            self.save_q.join()
        except Exception:
            pass
        try:
            self.t_writer.join(timeout=1.0)
        except Exception:
            pass

        # GPU teardown
        try:
            if self.use_gpu:
                self.dec.teardownGPUDecode()
        except Exception as e:
            print(f"[WARN] teardownGPUDecode failed: {e}")

        # camera close
        try:
            self.cam.close()
        except Exception as e:
            print(f"[WARN] cam.close failed: {e}")


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


def _nearest_candidate(x: float, candidates: Sequence[int]) -> int:
    return int(min(candidates, key=lambda v: abs(v - x)))


FRAMERATE_CANDIDATES = [
    1,
    10,
    50,
    100,
    125,
    250,
    500,
    950,
    1000,
    1500,
    2000,
    2500,
    3000,
    3200,
    4000,
    5000,
    8000,
    10000,
    20000,
    25000,
    30000,
]


def prompt_and_apply_cam_fps(cam) -> Optional[int]:
    """robust4 系の実装をベースに、RecordRate を設定"""

    # Optional forced fps (no prompt)
    if CAMERA_FPS_FORCE is not None:
        desired_str = str(int(CAMERA_FPS_FORCE))
        print(f"[INFO] forcing camera framerate to {desired_str} fps (CAMERA_FPS_FORCE)")
        s = desired_str
    else:
        s = None
    try:
        current = cam.framerate() if hasattr(cam, "framerate") else None
    except Exception:
        current = None

    prompt = "Enter camera Record Rate fps (blank=keep"
    if current is not None:
        prompt += f", current={current}"
    prompt += "): "

    if s is None:
        try:
            s = input(prompt).strip()
        except EOFError:
            s = ""

    if not s:
        print("[INFO] keep camera framerate (no change).")
        return None

    try:
        desired = float(s)
        assert desired > 0
    except Exception:
        print(f"[WARN] invalid fps '{s}'. keep current.")
        return None

    def _apply(rate_float: float) -> bool:
        rate = int(round(rate_float))
        shutter_den = max(1, rate)
        if hasattr(cam, "setFramerateShutter"):
            cam.setFramerateShutter(rate, shutter_den)
            return True
        ok = False
        if hasattr(cam, "setFramerate"):
            cam.setFramerate(rate)
            ok = True
        if hasattr(cam, "setShutter"):
            try:
                cam.setShutter(shutter_den)
            except Exception:
                pass
        return ok

    try:
        if not _apply(desired):
            raise RuntimeError
    except Exception:
        cand = _nearest_candidate(desired, FRAMERATE_CANDIDATES)
        print(f"[WARN] setFramerateShutter({desired}) failed, retry with nearest {cand}…")
        _apply(cand)

    try:
        new_fps = cam.framerate()
        new_shutter = cam.shutter() if hasattr(cam, "shutter") else None
        print(f"[INFO] camera framerate={new_fps} fps, shutter=1/{new_shutter if new_shutter else 'n/a'}")
        return int(new_fps)
    except Exception:
        return None


def parse_yes_no(prompt: str, default_yes: bool = True) -> bool:
    """console yes/no"""
    suf = "[Y/n]" if default_yes else "[y/N]"
    try:
        s = input(f"{prompt} {suf} ").strip().lower()
    except EOFError:
        s = ""
    if not s:
        return default_yes
    if s in ("y", "yes"):
        return True
    if s in ("n", "no"):
        return False
    return default_yes

# try:
#     # Import camera recorder and helpers from the provided sync script.
#     # The file must reside in the same directory or be on the Python path.
#     sys.path.append(str(Path(__file__).resolve().parent))
#     from acoustools_infinicam_sync_single_with_preview_v5 import (
#         InfinicamRecorder,
#         prepare_message_from_holograms,
#         focus_preview_loop,
#         ns_to_s,
#         SAVE_DIR,
#         ROI,
#         DECODE_THREADS,
#         SAVE_QUEUE_MAX,
#         QUEUE_DROP_MARGIN,
#         RAW_MODE,
#         ENABLE_PREVIEW,
#         FOCUS_PREVIEW_BEFORE_RUN,
#         POST_ROLL_SEC,
#         NONBLOCKING_THRESHOLD_RATIO,
#     )
# except Exception as e:  # pragma: no cover
#     # If the import fails we raise a friendly error.  This import is
#     # critical because it contains the camera recorder implementation.
#     raise RuntimeError(
#         "Failed to import InfinicamRecorder and helpers. "
#         "Make sure 'acoustools_infinicam_sync_single_with_preview_v5.py' is present"
#     ) from e


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
) -> List[Tuple[float, float, float]]:
    """Compute one cycle of 3D positions for the specified trajectory.

    Parameters
    ----------
    mode : int
        Trajectory mode (1–9) matching the descriptions in the module
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

    Returns
    -------
    list of (x, y, z)
        Sequence of positions for one complete cycle.
    """
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


def compute_holograms_for_positions(pos_list: Sequence[Tuple[float, float, float]]) -> List[torch.Tensor]:
    """Compute holograms for each target position using the WGS solver.

    For each (x, y, z) triple in ``pos_list`` this function builds a point
    source, runs the WGS solver to obtain the phases and then adds the
    levitation signature.  The result is a list of tensors suitable for
    consumption by ``prepare_message_from_holograms``.
    """
    holograms: List[torch.Tensor] = []
    for (x, y, z) in pos_list:
        # ``create_points`` expects x, y, z in metres.  We fix y to 0 for
        # two‑dimensional patterns but allow arbitrary y in the argument for
        # completeness.
        p = create_points(1, 1, x=x, y=y, z=z)

        board = transducers(16, BOARD_POSITIONS)
        holo = kd_solver(p,board)
        # holo = wgs(p)
        holo = add_lev_sig(holo)
        holograms.append(holo)
    return holograms


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

    The loop repeats until the user terminates with Ctrl+C or by closing
    the preview window.
    """
    # Ensure the recordings directory exists
    Path(SAVE_DIR).mkdir(parents=True, exist_ok=True)

    # Connect to the levitation controller
    lev = LevitatorController(ids=(101, 3))
    print("[INFO] Connected to PAT via AcousTools")

    # Do not open the TiePie oscilloscope here.  The previous integration used
    # CH1 in Python as a trigger source, but that kept software in the critical
    # path.  External capture now follows the generator-triggered-burst route
    # instead, which uses the TiePie generator's hardware trigger input.
    scp = None

    def _open_preview_recorder() -> InfinicamRecorder | None:
        """Open INFINICAM only for preview/focus use.

        The external-sync capture path opens the camera again in EXTERNAL mode,
        so this preview handle must be closed right before capture starts.
        """
        try:
            return InfinicamRecorder(
                save_dir=SAVE_DIR,
                roi=ROI,
                decode_threads=DECODE_THREADS,
                use_gpu_if_available=True,
                save_queue_max=SAVE_QUEUE_MAX,
                queue_drop_margin=QUEUE_DROP_MARGIN,
                raw_mode=RAW_MODE,
                preview_enabled=ENABLE_PREVIEW,
            )
        except Exception as e:
            print(f"[WARN] Failed to initialise INFINICAM preview recorder: {e}")
            return None

    # Preview/live-view camera handle. This is closed before external-sync capture.
    recorder: InfinicamRecorder | None = _open_preview_recorder()

    # Current static position and its hologram.  We keep track of the last
    # resting place so that smooth transitions can interpolate correctly.
    current_static_pos: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    current_static_holo: torch.Tensor | None = None
    # if current_static_holo is None:
    #     # Compute a single hologram for the centre point
    #     current_static_holo = compute_holograms_for_positions([current_static_pos])[0]
    #     try:
    #         lev.levitate(current_static_holo)
    # SYNC_TRANSDUCER_INDEX = 431  # オシロスコープを繋ぐトランスデューサの番号 (0〜511)
    # SYNC_TRANSDUCER_INDEX = 447  # オシロスコープを繋ぐトランスデューサの番号 (0〜511)
    SYNC_TRANSDUCER_INDEX = 303  # オシロスコープを繋ぐトランスデューサの番号 (0〜511)

    if current_static_holo is None:
        # Compute a single hologram for the centre point
        base_holo = compute_holograms_for_positions([current_static_pos])[0]
        
        # 複素数テンソルの場合、指定したインデックスの振幅を0にする
        current_static_holo = base_holo.clone()
        if torch.is_complex(current_static_holo):
            # 位相はそのままに、振幅だけ0にする
            # print(f'current_static_holo.shape: {current_static_holo.shape}')
            # current_static_holo[0, SYNC_TRANSDUCER_INDEX, 0] = 0.0 + 0.0j
            # またはバッチ次元をワイルドカードにして:
            current_static_holo[:, SYNC_TRANSDUCER_INDEX, :] = 0.0 + 0.0j
        
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
            try:
                mode = int(input("Select mode (1-9): ").strip())
            except (ValueError, EOFError):
                print("[WARN] Invalid input; please enter a number between 1 and 9.")
                continue
            if mode not in range(1, 10):
                print("[WARN] Mode must be between 1 and 9.")
                continue
            # Read amplitudes as millimetres and convert to metres
            amp_x_mm = 0.0
            amp_z_mm = 0.0
            amp_scale_mm = 0.0
            try:
                if mode == 1:
                    amp_z_mm = float(input("Enter Z-axis amplitude (±mm) [e.g. 0.15]: ").strip())
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
            # Number of points per cycle (default 40 for 40‑step quantisation)
            try:
                n_steps = int(input("Enter number of steps per cycle [default 40]: ").strip() or "40")
            except (ValueError, EOFError):
                print("[WARN] Invalid step count; using 40.")
                n_steps = 40
            # Motion frequency in Hz (how many cycles per second)
            try:
                rev_hz = float(input("Enter motion frequency in Hz [default 10.0]: ").strip() or "10.0")
                if rev_hz <= 0:
                    raise ValueError
            except (ValueError, EOFError):
                print("[WARN] Invalid frequency; using 10.0 Hz.")
                rev_hz = 10.0
            # Number of cycles (loops) to run
            try:
                num_loops = int(input("Enter number of loops (cycles) [default 10]: ").strip() or "10")
                if num_loops <= 0:
                    raise ValueError
            except (ValueError, EOFError):
                print("[WARN] Invalid loop count; using 10.")
                num_loops = 10

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
                max_len=60
            )

            # ★ディレクトリも短くする（重要）
            run_dir = os.path.join(SAVE_DIR, shape_name, f"{run_stamp}_{param_tag_short}")
            Path(run_dir).mkdir(parents=True, exist_ok=True)

            if recorder is not None:
                recorder.save_dir = os.path.abspath(run_dir)

            Path(run_dir).mkdir(parents=True, exist_ok=True)
            if recorder is not None:
                # Ensure camera outputs (avi/json/timestamps) go into run_dir
                recorder.save_dir = os.path.abspath(run_dir)

            # 1. Compute one cycle of positions
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
            if mode == 8:
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
            fps_req = int(n_steps * rev_hz)
            actual_fps = lev.set_frame_rate(fps_req) or fps_req
            print(f"[INFO] PAT frame rate set: requested={fps_req} Hz, actual={actual_fps} Hz")

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
            # print("[INFO] Moving smoothly to the orbit start position…")
            # # Use the same number of steps as n_steps for the approach
            # approach_positions = generate_smooth_positions(current_static_pos, start_pos, n_steps)
            # for pos in approach_positions:
            #     # Compute a hologram for each intermediate point and apply it
            #     h = compute_holograms_for_positions([pos])[0]
            #     try:
            #         lev.levitate(h)
            #     except Exception as e:
            #         print(f"[WARN] levitate during approach failed: {e}")
            #     # Use a gentle pause.  We avoid busy‑waiting here; 50 ms gives a
            #     # perceptibly smooth trajectory for human eyes and is longer
            #     # than the PAT frame time at typical rates.
            #     time.sleep(0.05)
            # # Update current static to the start of the cycle
            # current_static_pos = start_pos
            # current_static_holo = cycle_holograms[0]

            # 4. Smooth approach to the start position from current static
            print("[INFO] Moving smoothly to the orbit start position…")
            approach_positions = generate_smooth_positions(current_static_pos, start_pos, n_steps)
            for pos in approach_positions:
                h = compute_holograms_for_positions([pos])[0]
                h = compute_holograms_for_positions([pos])[0]
                
                # 移動中も同期用ピンはミュートしておく
                if torch.is_complex(h):
                    h[0, SYNC_TRANSDUCER_INDEX, 0] = 0.0 + 0.0j
                
                try:
                    lev.levitate(h)
                except Exception as e:
                    print(f"[WARN] levitate during approach failed: {e}")
                time.sleep(0.05)
            
            # 最終的な静止ホログラムも更新
            current_static_pos = start_pos
            current_static_holo = cycle_holograms[0].clone()
            if torch.is_complex(current_static_holo):
                 current_static_holo[0, SYNC_TRANSDUCER_INDEX, 0] = 0.0 + 0.0j
            

            # 5. Focus preview if enabled
            if ENABLE_PREVIEW and FOCUS_PREVIEW_BEFORE_RUN:
                if recorder is None:
                    recorder = _open_preview_recorder()
                if recorder is not None:
                    print("\n[FOCUS] Opening live preview for focusing. Press 'q' to close, 'Esc' to quit.")
                    if not focus_preview_loop(recorder):
                        print("[INFO] User aborted via preview. Exiting loop.")
                        break

            # 6. Prompt for recording start
            input("\n>>> Particle is at the start of the trajectory. Press Enter to start recording and motion. <<<\n")

            # Compute expected duration (pattern length * hardware loop count).
            expected_duration = (num_geometries * pat_loops) / float(actual_fps)
            # ----- External sync capture -----
            # Instead of using the internal InfinicamRecorder and hardware trigger
            # detection, perform an external-sync capture.  The TiePie generator
            # produces a burst of pulses driving the camera via SYNC IN, while
            # the PAT is started immediately before the burst.  This ensures
            # minimal latency between levitation control and camera exposure.
            n_frames = int(num_geometries * pat_loops)
            # Convert ROI to string format for the recorder helper
            if ROI is None:
                roi_spec = None
            elif isinstance(ROI, (tuple, list)):
                roi_spec = ",".join(str(int(v)) for v in ROI)
            else:
                roi_spec = str(ROI)
            # Build additional metadata for the capture
            capture_meta = {
                "run_desc": run_desc,
                "mode": mode,
                "shape_name": shape_name,
                "run_dir": os.path.abspath(run_dir),
                "steps_per_cycle": n_steps,
                "rev_hz": rev_hz,
                "num_loops": num_loops,
                "pat_loops": pat_loops,
                "amp_x_mm": amp_x_mm,
                "amp_z_mm": amp_z_mm,
                "amp_scale_mm": amp_scale_mm,
                "pat_fps_requested": fps_req,
                "pat_fps_actual": actual_fps,
                "expected_duration_sec": expected_duration,
                "prep_time_sec": prep_s,
            }

            # Release the preview camera handle before opening INFINICAM again
            # in EXTERNAL sync mode. Keeping the preview recorder open can make
            # PUC_OpenDevice fail inside run_external_sync_recording().
            if recorder is not None:
                try:
                    print("[INFO] Closing preview INFINICAM handle before external-sync capture...")
                    recorder.close()
                except Exception as e:
                    print(f"[WARN] Failed to close preview recorder cleanly: {e}")
                finally:
                    recorder = None

            # Execute external-sync recording.  The capture FPS can differ from
            # the PAT FPS; when it does, record enough camera frames to span the
            # full PAT duration.
            capture_fps = float(EXTERNAL_CAPTURE_FPS) if EXTERNAL_CAPTURE_FPS is not None else float(actual_fps)
            # n_frames_capture = int(math.ceil(float(num_geometries * pat_loops) * capture_fps / max(float(actual_fps), 1e-9)))
            n_frames_capture = capture_fps*1.400
            summary = run_external_sync_recording(
                basename=run_base,
                save_dir=run_dir,
                n_frames=n_frames_capture,
                requested_fps=capture_fps,
                roi_spec=roi_spec,
                lev=lev,
                phases_ct=phases_ct,
                amps_ct=amps_ct,
                num_geometries=int(num_geometries),
                pat_loops=int(pat_loops),
                extra_meta=capture_meta,
                duty=0.5,
                amplitude=2.5,
                offset=2.5,
                # Preserve the longer wait from the attached FASTCAM one-shot variant.
                arm_delay_sec=5.0,
                low_hold_sec=0.5,
                queue_max=128,
                decode_threads=DECODE_THREADS,
                use_gpu=EXTERNAL_CAPTURE_USE_GPU,
                encode_after=True,
                encode_fps=None,
            )

            # 10. Save ideal log of timestamps and positions (in run_dir)
            # log_filename = os.path.join(run_dir, f"{run_name}_ideal_log.csv")
            log_filename = os.path.join(run_dir, f"{run_base}_ideal_log.csv")
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

            # 10b. Create PAT_START-based clip AVI (same folder) if enabled
            if ENABLE_PAT_CLIP and summary is not None:
                try:
                    # Determine AVI and CSV paths from the summary/meta dict.
                    src_avi_path = None
                    csv_path = None
                    if isinstance(summary, dict):
                        src_avi_path = summary.get("avi_path") or summary.get("path")
                        # In RAW-first capture meta, csv_path points to *_timestamps.csv
                        csv_path = summary.get("csv_path") or summary.get("timestamps_csv")
                    if src_avi_path and csv_path:
                        clip_path = make_pat_clip(
                            src_avi=src_avi_path,
                            csv_path=csv_path,
                            expected_s=float(expected_duration),
                            clip_tail_sec=float(CLIP_TAIL_MARGIN_SEC),
                            out_suffix="_clip_pat.avi",
                        )
                        if clip_path:
                            print(f"[INFO] PAT clip saved: {clip_path}")
                    else:
                        print("[WARN] cannot create PAT clip: missing video or CSV path")
                except Exception as e:
                    print(f"[WARN] PAT clip creation failed: {e}")

            # 11. Update current static to end of cycle
            current_static_pos = end_pos
            current_static_holo = cycle_holograms[-1]
            # Put particle at end position explicitly
            try:
                lev.levitate(current_static_holo)
            except Exception as e:
                print(f"[WARN] levitate to end position failed: {e}")

            # 12. Optionally move back to centre
            ans = input("\nReturn to centre? (Y/n): ").strip().lower()
            if ans != "n":
                # When returning to the centre, mute the sync transducer to avoid spurious
                # triggers.  We zero the amplitude for SYNC_TRANSDUCER_INDEX on each
                # intermediate hologram and on the final static hologram.
                centre_pos = (0.0, 0.0, 0.0)
                print("[INFO] Moving back to centre…")
                return_positions = generate_smooth_positions(current_static_pos, centre_pos, n_steps)
                for pos in return_positions:
                    h = compute_holograms_for_positions([pos])[0]
                    # Ensure the sync transducer produces no signal during the return
                    if torch.is_complex(h):
                        h[0, SYNC_TRANSDUCER_INDEX, 0] = 0.0 + 0.0j
                    try:
                        lev.levitate(h)
                    except Exception as e:
                        print(f"[WARN] levitate during return failed: {e}")
                    time.sleep(0.05)
                # Update static position and hologram
                current_static_pos = centre_pos
                current_static_holo = compute_holograms_for_positions([centre_pos])[0]
                # Zero the sync transducer on the final static hologram
                if torch.is_complex(current_static_holo):
                    current_static_holo[:, SYNC_TRANSDUCER_INDEX, :] = 0.0 + 0.0j
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
        # Close camera recorder
        if recorder is not None:
            try:
                recorder.close()
            except Exception:
                pass
        print("[INFO] Shutdown complete.")


if __name__ == "__main__":  # pragma: no cover
    main()