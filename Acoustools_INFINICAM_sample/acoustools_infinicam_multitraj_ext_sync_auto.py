#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
acoustools_infinicam_multitraj_ext_sync_auto.py
================================================

Standalone automated multi-trajectory recording script combining:

* The JSON-driven loop structure from ``acoustools_infinicam_multitraj_kd_auto.py``
  (sequential execution of all shape/parameter combinations without per-run prompts).
* The external-sync recording method from
  ``acoustools_infinicam_multitraj_kd_no-auto_trig_pulse_ext_triggered_restored.py``
  (TiePie generator triggered-burst + INFINICAM SYNC IN).

This file is completely self-contained: it does NOT import either of the above
scripts as modules.  All required functions, classes and constants are defined
here.
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
import re
import hashlib
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import pypuclib
from pypuclib import CameraFactory, GPUSetup, PUC_SYNC_MODE, PUC_SIGNAL

import torch

from acoustools.Levitator import LevitatorController
from acoustools.Solvers import wgs, kd_solver
from acoustools.Utilities import transducers, add_lev_sig, create_points, BOARD_POSITIONS

# ===========================================================================
# User settings
# ===========================================================================
SAVE_DIR = "./recordings"

# INFINICAM decode/record settings
ROI = (768, 768)
DECODE_THREADS: Optional[int] = None
RING_BUFFER_COUNT = 64

# If set (e.g., 1000), force camera record rate without prompting.
CAMERA_FPS_FORCE: int | None = None

# External-sync recording can use a different FPS than the PAT update rate.
EXTERNAL_CAPTURE_FPS: int | None = 1000

# CPU decode for external capture (avoid GPU black-frame regressions).
EXTERNAL_CAPTURE_USE_GPU: bool = False

# TiePie trigger edge.
GEN_TRIGGER_EDGE: str = "falling"

# ROI alignment
ROI_ALIGN = 16
ROI_ALIGN_WH = 16

# Save queue
SAVE_QUEUE_MAX = 256
QUEUE_DROP_MARGIN = 2

# Video/RAW output
RAW_MODE = False
RAW_SUFFIX = ".raw"
CODEC = cv2.VideoWriter_fourcc(*"MJPG")

# PAT clip extraction
ENABLE_PAT_CLIP = True
CLIP_TAIL_MARGIN_SEC = 0.4

# Preview
ENABLE_PREVIEW = True
PREVIEW_MAX_FPS = 30
PREVIEW_DOWNSCALE = 1
PREVIEW_WHILE_SAVING = False

# Focus preview
FOCUS_PREVIEW_BEFORE_RUN = True
FOCUS_PREVIEW_WINDOW_NAME = "INFINICAM"

# Post-roll
POST_ROLL_SEC = 0.4

# Timestamp log
ENABLE_TIMESTAMP_LOG = True
LOG_QUEUE_MAX = 200000
LOG_INCLUDE_CAM_TIMESTAMP = True

# Mode shape names (for directory organisation in interactive mode)
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

# Shape-to-mode mapping for the JSON-driven auto loop (from auto.py)
SHAPE_TO_MODE: Dict[str, int] = {
    "Line": 1,
    "Ellipse": 2,
    "Diagonal_Line": 3,
    "Heart": 4,
    "Rectangle": 5,
    "Eight": 6,
    "Infinite": 7,
    "Diagonal_One_Line": 8,
}

# ===========================================================================
# TiePie library (optional)
# ===========================================================================
try:
    import libtiepie
except ImportError:
    print("[WARN] libtiepie is not installed. Run 'pip install libtiepie'")
    libtiepie = None

if libtiepie is not None:
    libtiepie.network.auto_detect_enabled = True
    libtiepie.device_list.update()

    if len(libtiepie.device_list) > 0:
        print()
        print("Available devices:")
        for item in libtiepie.device_list:
            print(f"  Name: {item.name}")
            print(f"    Serial number  : {item.serial_number}")
            print(f"    Available types: {libtiepie.device_type_str(item.types)}")
            if item.has_server:
                print(f"    Server         : {item.server.url} ({item.server.name})")
    else:
        print("No devices found!")

# ===========================================================================
# Candidate frame rates
# ===========================================================================
FRAMERATE_CANDIDATES: list[int] = [
    1, 10, 50, 100, 125, 250, 500, 950, 1000, 1500, 2000, 2500, 3000,
    3200, 4000, 5000, 8000, 10000, 20000, 25000, 30000,
]


# ===========================================================================
# Small helpers
# ===========================================================================

def sanitize_filename(name: str, max_len: int = 80) -> str:
    """Windows-safe short filename stem."""
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
    return f"{v:.1f}".replace(".", "p")


def ns_to_s(ns: int) -> float:
    return float(ns) * 1e-9


def safe_mkdir(p: str | Path) -> None:
    Path(p).mkdir(parents=True, exist_ok=True)


def _nearest_candidate(x: float, candidates: Sequence[int] = FRAMERATE_CANDIDATES) -> int:
    """Return the nearest supported frame rate candidate for the requested value."""
    return int(min(candidates, key=lambda v: abs(v - x)))


def parse_yes_no(prompt: str, default_yes: bool = True) -> bool:
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


def prompt_and_apply_cam_fps(cam) -> Optional[int]:
    """Set camera Record Rate (fps) interactively or via CAMERA_FPS_FORCE."""
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
        print(f"[WARN] setFramerateShutter({desired}) failed, retry with nearest {cand}...")
        _apply(cand)

    try:
        new_fps = cam.framerate()
        new_shutter = cam.shutter() if hasattr(cam, "shutter") else None
        print(f"[INFO] camera framerate={new_fps} fps, shutter=1/{new_shutter if new_shutter else 'n/a'}")
        return int(new_fps)
    except Exception:
        return None


# ===========================================================================
# PAT clip extraction helper
# ===========================================================================

def make_pat_clip(
    src_avi: str,
    csv_path: str,
    expected_s: float,
    clip_tail_sec: float = CLIP_TAIL_MARGIN_SEC,
    out_suffix: str = "_clip_pat.avi",
) -> Optional[str]:
    """Create a short clip from the recorded AVI around the PAT_START event."""
    try:
        import pandas as pd  # type: ignore
    except Exception as e:
        print(f"[WARN] pandas import failed in make_pat_clip: {e}")
        return None

    if not src_avi or not os.path.isfile(src_avi):
        print(f"[WARN] make_pat_clip: source AVI '{src_avi}' not found")
        return None
    if not csv_path or not os.path.isfile(csv_path):
        print(f"[WARN] make_pat_clip: CSV '{csv_path}' not found")
        return None

    root, ext = os.path.splitext(src_avi)
    out_path = root + str(out_suffix)

    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        print(f"[WARN] make_pat_clip: failed to read CSV: {e}")
        return None

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

    frames = df[df.kind == "FRAME"].copy()
    try:
        frames["perf_counter_ns"] = frames["perf_counter_ns"].astype(np.int64)
    except Exception:
        frames["perf_counter_ns"] = frames["perf_counter_ns"].apply(int)

    if "seq" in frames.columns and frames["seq"].notna().any():
        frames = frames.drop_duplicates(subset=["seq"], keep="first")

    if "index" in frames.columns and frames["index"].notna().any():
        frames["frame_index"] = frames["index"].astype(np.int64)
    else:
        frames = frames.sort_values("perf_counter_ns")
        frames["frame_index"] = np.arange(len(frames), dtype=np.int64)

    frames = frames.sort_values("frame_index")

    t0 = t_pat
    t1 = t_pat + int((expected_s + clip_tail_sec) * 1e9)

    pc = frames["perf_counter_ns"].to_numpy(np.int64)
    fi = frames["frame_index"].to_numpy(np.int64)

    spos = int(np.searchsorted(pc, t0, side="left"))
    epos = int(np.searchsorted(pc, t1, side="right") - 1)
    epos = max(epos, spos)
    start_idx = int(fi[spos])
    end_idx = int(fi[epos])

    print(f"[PAT_CLIP] clip frames: {start_idx} to {end_idx}, count={end_idx - start_idx + 1}")

    cap = cv2.VideoCapture(src_avi)
    if not cap.isOpened():
        print("[WARN] make_pat_clip: cannot open source AVI")
        return None
    cap.set(cv2.CAP_PROP_POS_FRAMES, float(start_idx))
    ok, frame0 = cap.read()
    if not ok:
        print("[WARN] make_pat_clip: cannot read start frame")
        cap.release()
        return None
    h, w = frame0.shape[:2]
    is_color = not (frame0.ndim == 2)
    fps_out = cap.get(cv2.CAP_PROP_FPS) or 1000.0
    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
    wr = cv2.VideoWriter(out_path, fourcc, float(fps_out), (w, h), isColor=is_color)
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


# ===========================================================================
# TiePie generator helpers
# ===========================================================================

def _open_tiepie_generator(require_burst: bool = True):
    """Search for and open a TiePie generator.  Raises if none found."""
    libtiepie.network.auto_detect_enabled = True
    libtiepie.device_list.update()
    for item in libtiepie.device_list:
        if item.can_open(libtiepie.DEVICETYPE_GENERATOR):
            gen = item.open_generator()
            if gen is None:
                continue
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
    """Force the generator output to a DC low level (0 V)."""
    gen.signal_type = libtiepie.ST_DC
    gen.offset = 0.0
    gen.output_enable = True


def _configure_square_burst(
    gen, freq_hz: float, count: int, duty: float, amplitude: float, offset: float
) -> None:
    gen.signal_type = libtiepie.ST_SQUARE
    gen.frequency = float(freq_hz)
    gen.amplitude = float(amplitude)
    gen.offset = float(offset)
    gen.symmetry = float(duty)
    gen.mode = libtiepie.GM_BURST_COUNT
    gen.burst_count = int(count)
    gen.output_enable = True


def _configure_generator_trigger_input(gen, edge: str = "rising") -> None:
    """Arm TiePie triggered-burst on EXT1/EXT2."""
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
    """Configure one active-low pulse using GeneratorTriggeredBurst flow."""
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
    _set_generator_output_invert(gen, True)
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


# ===========================================================================
# FASTCAM one-shot trigger helper
# ===========================================================================

FASTCAM_TRIGGER_EDGE: str = "falling"
FASTCAM_IDLE_VOLTAGE: float = 5.0
FASTCAM_LOW_VOLTAGE: float = 0.0
FASTCAM_TRIGGER_PULSE_WIDTH_MS: float = 10.0
FASTCAM_TRIGGER_RECOVERY_MS: float = 20.0
FASTCAM_IDLE_SETTLE_SEC: float = 0.5
FASTCAM_POST_TRIGGER_SEC: float = 0.2


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
    """Arm TiePie for a single active-low FASTCAM trigger pulse and then send PAT."""
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

        print(f"[INFO] Waiting {float(arm_delay_sec):.3f} s before arming TiePie one-shot trigger...")
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
        print(f"[INFO] Arming TiePie triggered burst on EXT input ({trigger_edge} edge)...")
        gen.start()

        pat_send_pc_ns = time.perf_counter_ns()
        pat_send_wall_ns = time.time_ns()
        print("[INFO] Sending PAT message; TiePie will emit one active-low FASTCAM trigger pulse on the physical PAT sync edge...")
        lev.send_message(
            phases_ct, amps_ct, 0, int(num_geometries),
            sleep_ms=0, loop=True, num_loops=int(pat_loops),
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


# ===========================================================================
# Camera / ROI helpers
# ===========================================================================

def _align_down(v: int, align: int) -> int:
    return int(v) if align <= 1 else (int(v) & ~(int(align) - 1))


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(v, hi))


def _parse_roi_like_trig(
    roi_spec: Optional[str], full_w: int, full_h: int, align_xy: int = 16
) -> tuple[int, int, int, int]:
    """Interpret ROI specification (``'w,h'`` or ``'x,y,w,h'``)."""
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
    """Apply the requested frame rate and shutter to the camera."""
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
        print(f"[INFO] camera framerate read-back = {actual} fps")
        try:
            print(f"[INFO] camera shutter read-back = 1/{cam.shutter()}")
        except Exception:
            pass
        return actual
    except Exception:
        print("[WARN] camera framerate read-back unavailable; using requested fps")
        return float(rate)


def _encode_raw_to_avi(raw_path: str, meta_path: str, avi_path: str, fps: Optional[float] = None) -> str:
    """Convert a monochrome RAW sequence to an MJPEG AVI."""
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


# ===========================================================================
# External sync recording (core capture function)
# ===========================================================================

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

    Configures the camera to use EXTERNAL SYNC IN, arms the camera and TiePie
    generator, triggers the PAT via ``lev.send_message`` and captures
    ``n_frames`` frames at ``requested_fps``.
    """
    os.makedirs(save_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_base = basename.replace(" ", "_")
    stem = os.path.abspath(os.path.join(save_dir, f"{safe_base}_{n_frames}f_{int(requested_fps)}fps_{ts}"))
    raw_path = stem + ".raw"
    csv_path = stem + "_timestamps.csv"
    meta_path = stem + "_meta.json"
    avi_path = stem + ".avi"

    gen = _open_tiepie_generator(require_burst=True)
    try:
        print(f"[INFO] Forcing TiePie line LOW for {low_hold_sec:.3f}s before camera EXTERNAL mode")
        _set_dc_low(gen)
        time.sleep(max(0.0, low_hold_sec))

        print("[INFO] Opening INFINICAM (external sync)")
        cam = CameraFactory().create(0, True)
        res = cam.resolution()
        full_w, full_h = int(res.width), int(res.height)
        shutter_fps = int(max(1, requested_fps * 2.0))
        actual_cam_fps = _apply_camera_fps(cam, requested_fps, shutter_fps)
        rx, ry, rw, rh = _parse_roi_like_trig(roi_spec, full_w, full_h, align_xy=16)
        print(f"[INFO] ROI: x={rx}, y={ry}, w={rw}, h={rh} (camera reso {full_w}x{full_h})")
        print("[INFO] Setting SYNC IN = External / Positive")
        cam.setSyncInMode(int(PUC_SYNC_MODE.PUC_SYNC_EXTERNAL), int(PUC_SIGNAL.PUC_SIGNAL_POSI))
        try:
            print(f"[INFO] syncInMode() read-back: {cam.syncInMode()}")
        except Exception:
            print("[INFO] syncInMode() read-back unavailable")
        try:
            cam.setRingBufferCount(max(256, min(int(n_frames) + 200, 8192)))
        except Exception as e:
            print(f"[WARN] setRingBufferCount failed: {e}")
        dec = cam.decoder()
        if decode_threads is None:
            try:
                import multiprocessing
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
        csv_writer.writerow([
            "kind", "index", "seq", "t_rel_sec",
            "perf_counter_ns", "wall_clock_ns", "cam_timestamp",
            "event", "note",
        ])
        raw_fp = open(raw_path, "wb", buffering=16 * 1024 * 1024)

        # Queues and state
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

        wt = threading.Thread(target=writer_loop, daemon=True)
        lt = threading.Thread(target=logger_loop, daemon=True)
        wt.start()
        lt.start()

        def cb(xfer) -> None:
            nonlocal frames_in, frames_dropped_queue, last_pc_ns, last_seq
            nonlocal cb_inflight, cam_ts_attr, cam_ts_unit, cam_ts_checked, log_rows_dropped
            if not accept_frames_evt.is_set() or stop_evt.is_set():
                return
            with cb_counter_lock:
                cb_inflight += 1
            try:
                pc_ns = time.perf_counter_ns()
                wall_ns = time.time_ns()
                seq = -1
                try:
                    seq = int(xfer.sequenceNo())
                except Exception:
                    seq = -1
                if last_seq is not None and seq == last_seq:
                    return
                last_seq = seq
                cam_ts_val = None
                if not cam_ts_checked:
                    for a in (
                        "timeStamp", "timestamp", "getTimestamp", "frameTime",
                        "frame_timestamp", "deviceTimestamp", "deviceTimeStamp",
                        "captureTime", "captureTimestamp", "timeStampUs",
                        "timeStampNS", "timeStampNs", "timeStamp10ns",
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
                t_rel_sec = 0.0
                if rec_start_pc_ns is not None:
                    t_rel_sec = (int(pc_ns) - int(rec_start_pc_ns)) * 1e-9
                try:
                    log_q.put_nowait((
                        "FRAME", int(frames_in), int(seq),
                        f"{t_rel_sec:.9f}", int(pc_ns), int(wall_ns),
                        "" if cam_ts_val is None else cam_ts_val, "", "",
                    ))
                except queue.Full:
                    log_rows_dropped += 1
                # Decode frame
                if gpu_enabled:
                    try:
                        full = dec.decodeGPU(xfer, False, full_w)
                        gray = np.asarray(full[ry : ry + rh, rx : rx + rw]).copy()
                    except Exception:
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
                    frame_q.put(gray)
                frames_in += 1
                if frames_in >= int(n_frames):
                    stop_evt.set()
            finally:
                with cb_counter_lock:
                    cb_inflight -= 1

        # Arm camera transfer
        print("[INFO] Arming INFINICAM (beginXfer) for external sync...")
        cam.beginXfer(cb)
        print(f"[INFO] Waiting {arm_delay_sec:.3f}s before sending PAT and starting TiePie burst...")
        time.sleep(max(0.0, arm_delay_sec))
        burst_fps = float(actual_cam_fps)
        if abs(burst_fps - float(requested_fps)) > 1e-6:
            print(f"[WARN] TiePie burst frequency follows camera read-back fps: {burst_fps} Hz (requested {requested_fps})")
        _configure_square_burst(gen, burst_fps, int(n_frames), float(duty), float(amplitude), float(offset))
        try:
            print(f"[INFO] Generator read-back: freq={gen.frequency}Hz amp={gen.amplitude}V off={gen.offset}V")
            try:
                print(f"[INFO] Generator symmetry read-back: {gen.symmetry}")
            except Exception:
                pass
        except Exception:
            pass
        _configure_generator_trigger_input(gen, edge=GEN_TRIGGER_EDGE)
        accept_frames_evt.set()
        print(f"[INFO] Arming TiePie triggered burst on EXT input ({GEN_TRIGGER_EDGE} edge)...")
        gen.start()
        rec_start_pc_ns = time.perf_counter_ns()
        rec_start_wall_ns = time.time_ns()
        try:
            log_q.put_nowait((
                "EVENT", "", "", f"{0.0:.9f}",
                int(rec_start_pc_ns), int(rec_start_wall_ns), "",
                "SOFTWARE_PAT_SEND",
                f"expected_frames={n_frames}, requested_fps={requested_fps}",
            ))
        except Exception:
            pass
        try:
            lev.send_message(
                phases_ct, amps_ct, 0, int(num_geometries),
                sleep_ms=0, loop=True, num_loops=int(pat_loops),
            )
        except Exception as e:
            print(f"[ERROR] lev.send_message failed: {e}")
        t0 = time.perf_counter()
        timeout_sec = float((n_frames / max(burst_fps, 1e-9)) + 8.0)
        while not stop_evt.is_set():
            if (time.perf_counter() - t0) > timeout_sec:
                print("[WARN] Timeout waiting for all frames; forcing stop")
                stop_evt.set()
                break
            time.sleep(0.002)
        accept_frames_evt.clear()
        tw = time.perf_counter()
        while True:
            with cb_counter_lock:
                inflight_now = cb_inflight
            if inflight_now <= 0:
                break
            if (time.perf_counter() - tw) > 3.0:
                print(f"[WARN] Timed out waiting for in-flight callbacks: {inflight_now}")
                break
            time.sleep(0.001)
        _set_dc_low(gen)
        time.sleep(max(0.0, low_hold_sec))
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
        if extra_meta:
            meta.update(extra_meta)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        avi_generated = None
        if encode_after:
            try:
                print("[INFO] Encoding RAW -> AVI after capture...")
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


# ===========================================================================
# InfinicamRecorder class (used only for focus preview)
# ===========================================================================

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
    """INFINICAM recorder for preview/focus use. External sync capture uses
    ``run_external_sync_recording`` directly instead."""

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
        if roi is None:
            self.roi_x, self.roi_y = 0, 0
            self.roi_w = min(512, self.reso.width)
            self.roi_h = min(512, self.reso.height)
        elif isinstance(roi, (tuple, list)) and len(roi) == 2:
            self.roi_w = int(roi[0])
            self.roi_h = int(roi[1])
            ALIGN = 16
            self.roi_x = ((int(self.reso.width)  - self.roi_w) // 2) & ~(ALIGN - 1)
            self.roi_y = ((int(self.reso.height) - self.roi_h) // 2) & ~(ALIGN - 1)
            self.roi_x = max(0, min(self.roi_x, int(self.reso.width)  - self.roi_w))
            self.roi_y = max(0, min(self.roi_y, int(self.reso.height) - self.roi_h))
        elif isinstance(roi, (tuple, list)) and len(roi) == 4:
            self.roi_x, self.roi_y, self.roi_w, self.roi_h = map(int, roi)
            self.roi_w = max(2, min(self.roi_w, int(self.reso.width)))
            self.roi_h = max(2, min(self.roi_h, int(self.reso.height)))
            self.roi_x = max(0, min(self.roi_x, int(self.reso.width)  - self.roi_w))
            self.roi_y = max(0, min(self.roi_y, int(self.reso.height) - self.roi_h))
        else:
            raise ValueError(f"Invalid ROI format: {roi!r}")

        print(
            f"[INFO] ROI: x={self.roi_x}, y={self.roi_y}, w={self.roi_w}, h={self.roi_h} "
            f"(camera reso {int(self.reso.width)}x{int(self.reso.height)})"
        )

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

        # stats
        self.log_q: Optional[queue.Queue] = None
        self.log_thread: Optional[threading.Thread] = None
        self.log_fp = None
        self.log_writer = None
        self.timestamps_csv: Optional[str] = None
        self.frames_logged = 0
        self.dropped_log_rows = 0
        self._log_zero_pc_ns = 0

        self._cam_ts_attr: Optional[str] = None
        self._cam_ts_checked: bool = False

        self._cb_counter_lock = threading.Lock()
        self._saving_cb_inflight = 0

        self._reset_stats()

        # queue + writer thread
        self.save_q: queue.Queue = queue.Queue(maxsize=int(save_queue_max))
        self.t_writer = threading.Thread(target=self._writer_loop, daemon=True)
        self.t_writer.start()

        # start transfer
        self.cam.beginXfer(self._xfer_callback)
        time.sleep(0.2)

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
        self.log_dup_skipped = 0
        self.last_seqno_seen: Optional[int] = None
        self.last_seqno_cb: Optional[int] = None
        self.first_frame_pc_ns: Optional[int] = None
        self.last_frame_pc_ns: Optional[int] = None
        self.rec_start_pc_ns = 0
        self.rec_stop_pc_ns = 0
        self.rec_start_wall_ns = 0
        self.rec_stop_wall_ns = 0
        self.timestamps_csv = None

    def _log_put_drop_old(self, item: tuple) -> None:
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
                return

    def log_event(self, name: str, pc_ns: Optional[int] = None, wall_ns: Optional[int] = None, note: str = "") -> None:
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
        self.frames_logged = 0
        self.dropped_log_rows = 0
        self.log_dup_skipped = 0
        self.timestamps_csv = os.path.join(self.save_dir, f"{safe_base}_{ts}_timestamps.csv")
        self._log_zero_pc_ns = int(self.rec_start_pc_ns)
        self.log_q = queue.Queue(maxsize=int(LOG_QUEUE_MAX))
        self.log_fp = open(self.timestamps_csv, "w", newline="", encoding="utf-8")
        self.log_writer = csv.writer(self.log_fp)
        self.log_writer.writerow([
            "kind", "index", "seq", "t_rel_sec",
            "perf_counter_ns", "wall_clock_ns", "cam_timestamp",
            "event", "note",
        ])
        try:
            self.log_fp.flush()
        except Exception:
            pass

        def _log_loop() -> None:
            frame_idx = 0
            seen_seq = set()
            last_seq_written = None
            while True:
                item = self.log_q.get()
                try:
                    if item is None:
                        break
                    kind = item[0]
                    if kind == "FRAME":
                        _, seq, pc_ns, wall_ns, cam_ts = item
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
                        self.log_writer.writerow([
                            "FRAME", frame_idx, int(seq),
                            f"{t_rel:.9f}", int(pc_ns), int(wall_ns),
                            "" if cam_ts is None else cam_ts, "", "",
                        ])
                        frame_idx += 1
                        self.frames_logged = frame_idx
                    elif kind == "EVENT":
                        _, ev, pc_ns, wall_ns, note = item
                        t_rel = (int(pc_ns) - int(self._log_zero_pc_ns)) * 1e-9
                        self.log_writer.writerow([
                            "EVENT", "", "",
                            f"{t_rel:.9f}", int(pc_ns), int(wall_ns),
                            "", str(ev), str(note) if note else "",
                        ])
                    else:
                        self.log_writer.writerow(list(item))
                finally:
                    try:
                        self.log_q.task_done()
                    except Exception:
                        pass
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
        self._log_put_drop_old(("EVENT", "CAM_REC_START", int(self.rec_start_pc_ns), int(self.rec_start_wall_ns), ""))

    def _stop_timestamp_logger(self) -> None:
        if not ENABLE_TIMESTAMP_LOG:
            return
        if self.log_q is None:
            return
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
        self._log_put_drop_old(("EVENT", "CAM_REC_STOP", int(self.rec_stop_pc_ns), int(self.rec_stop_wall_ns), ""))
        try:
            self.log_q.put(None, timeout=1.0)
        except Exception:
            try:
                self.log_q.put_nowait(None)
            except Exception:
                pass
        try:
            self.log_q.join()
        except Exception:
            pass
        try:
            if self.log_thread is not None:
                self.log_thread.join(timeout=2.0)
        except Exception:
            pass
        self.log_q = None
        self.log_thread = None
        self.log_fp = None
        self.log_writer = None

    def _q_put_drop_old(self, item: Tuple) -> None:
        try:
            self.save_q.put_nowait(item)
            return
        except queue.Full:
            try:
                _ = self.save_q.get_nowait()
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
        if self.last_seqno_cb is not None:
            try:
                last = int(self.last_seqno_cb)
                cur = int(seq)
                if cur == last:
                    return
                if last > 60000 and cur < 1000:
                    pass
            except Exception:
                pass
        self.last_seqno_cb = int(seq)

        if saving:
            with self._cb_counter_lock:
                self._saving_cb_inflight += 1
            try:
                pc_ns = time.perf_counter_ns()
                wall_ns = time.time_ns()
                cam_ts_val = None
                if ENABLE_TIMESTAMP_LOG and LOG_INCLUDE_CAM_TIMESTAMP:
                    if not self._cam_ts_checked:
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
                if self.last_seqno_seen is not None and seq > self.last_seqno_seen + 1:
                    self.missing_seq_total += int(seq - self.last_seqno_seen - 1)
                self.last_seqno_seen = int(seq)
                self.frames_seen += 1
                if self.first_frame_pc_ns is None:
                    self.first_frame_pc_ns = int(pc_ns)
                self.last_frame_pc_ns = int(pc_ns)
                if self.save_q.qsize() >= (self.save_q.maxsize - self.queue_drop_margin):
                    self.decode_skipped_bp += 1
                    return
                try:
                    img = self.dec.decode(xferdata, self.roi_x, self.roi_y, self.roi_w, self.roi_h)
                    self.decoded_ok += 1
                except Exception:
                    self.decode_failed += 1
                    return
                frame = img.copy()
                try:
                    self.save_q.put_nowait(("frame", seq, frame))
                    self.enqueued_ok += 1
                except queue.Full:
                    self.dropped_queue_full += 1
                    self._q_put_drop_old(("frame", seq, frame))
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

        # Not saving: update preview
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
        while True:
            try:
                item = self.save_q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                kind = item[0]
                if kind == "close":
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
                _, seq, frame = item
                with self.writer_lock:
                    if self.raw_mode:
                        if self.raw_fp is not None:
                            try:
                                self.raw_fp.write(frame.tobytes())
                                self.frames_written += 1
                            except Exception:
                                pass
                    else:
                        if self.writer.isOpened():
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
        with self.writer_lock:
            try:
                if self.writer.isOpened():
                    self.writer.release()
            except Exception:
                pass
            try:
                if self.raw_fp is not None:
                    self.raw_fp.flush()
                    self.raw_fp.close()
                    self.raw_fp = None
            except Exception:
                pass

    def start_recording(self, basename: str, *, extra_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
        with self.writer_lock:
            self.target_path = os.path.abspath(path)
            if self.raw_mode:
                self.raw_fp = open(self.target_path, "wb")
            else:
                sample = self.get_preview_frame()
                is_color = bool(sample is not None and getattr(sample, "ndim", 2) == 3 and sample.shape[2] >= 3)
                ok = self.writer.open(
                    self.target_path, CODEC, float(self._header_fps()),
                    (int(self.roi_w), int(self.roi_h)), is_color,
                )
                if not ok:
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
        self.rec_start_pc_ns = time.perf_counter_ns()
        self.rec_start_wall_ns = time.time_ns()
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
        if not self.is_saving.is_set():
            raise RuntimeError("Recording is not active")
        self.is_saving.clear()
        self.rec_stop_pc_ns = time.perf_counter_ns()
        self.rec_stop_wall_ns = time.time_ns()
        self._q_put_drop_old(("close",))
        try:
            self.save_q.join()
        except Exception:
            pass
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
        if self.applied_fps and self.applied_fps > 0:
            return int(self.applied_fps)
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
        try:
            if self.is_saving.is_set():
                self.stop_recording()
        except Exception:
            pass
        self.stop_evt.set()
        try:
            self.cam.endXfer()
        except Exception as e:
            print(f"[WARN] cam.endXfer failed: {e}")
        self._q_put_drop_old(("quit",))
        try:
            self.save_q.join()
        except Exception:
            pass
        try:
            self.t_writer.join(timeout=1.0)
        except Exception:
            pass
        try:
            if self.use_gpu:
                self.dec.teardownGPUDecode()
        except Exception as e:
            print(f"[WARN] teardownGPUDecode failed: {e}")
        try:
            self.cam.close()
        except Exception as e:
            print(f"[WARN] cam.close failed: {e}")


# ===========================================================================
# Focus preview
# ===========================================================================

def focus_preview_loop(recorder: "InfinicamRecorder") -> bool:
    """Live preview for focus adjustment.

    - q : close preview, continue
    - Esc : quit entirely

    Returns True to continue, False to quit.
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

            color = 255 if getattr(frame, "ndim", 2) == 2 else (255, 255, 255)
            status = "REC" if recorder.is_saving.is_set() else "LIVE"
            cv2.putText(
                frame,
                f"{status}  preview_fps~{disp_fps:4.1f}  ROI={recorder.roi_w}x{recorder.roi_h}  (q:close, Esc:quit)",
                (10, 30), cv2.FONT_HERSHEY_PLAIN, 1.6, color, 1,
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


# ===========================================================================
# AcousTools -> ctypes prep
# ===========================================================================

def prepare_message_from_holograms(
    lev: LevitatorController,
    holograms: Sequence[torch.Tensor] | torch.Tensor,
    *,
    permute: bool = True,
) -> Tuple[ctypes.Array, ctypes.Array, int]:
    """Convert hologram tensors to ctypes arrays for lev.send_message()."""
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


# ===========================================================================
# Trajectory generation
# ===========================================================================

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
    """Compute one cycle of 3D positions for the specified trajectory."""
    cycle_pos: List[Tuple[float, float, float]] = []
    for step in range(n_steps):
        if mode == 8 and n_steps > 1:
            t_frac = step / (n_steps - 1)
        else:
            t_frac = step / n_steps
        theta = 2.0 * math.pi * t_frac
        x_p = x_center
        y_p = y_center
        z_p = z_center
        if mode == 1:
            z_p = z_center + amp_z * math.sin(theta)
        elif mode == 2:
            x_p = x_center + amp_x * math.cos(theta)
            z_p = z_center + amp_z * math.sin(theta)
        elif mode == 3:
            x_p = x_center - amp_x * math.cos(theta)
            z_p = z_center - amp_z * math.cos(theta)
        elif mode == 4:
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
            if t_frac < 0.25:
                t_quad = t_frac * 4.0
                x_p = x_center + amp_x * (-1.0 + 2.0 * t_quad)
                z_p = z_center - amp_z
            elif t_frac < 0.5:
                t_quad = (t_frac - 0.25) * 4.0
                x_p = x_center + amp_x
                z_p = z_center + amp_z * (-1.0 + 2.0 * t_quad)
            elif t_frac < 0.75:
                t_quad = (t_frac - 0.5) * 4.0
                x_p = x_center + amp_x * (1.0 - 2.0 * t_quad)
                z_p = z_center + amp_z
            else:
                t_quad = (t_frac - 0.75) * 4.0
                x_p = x_center - amp_x
                z_p = z_center + amp_z * (1.0 - 2.0 * t_quad)
        elif mode == 6:
            x_p = x_center + amp_x * math.sin(2.0 * theta)
            z_p = z_center + amp_z * math.sin(theta)
        elif mode == 7:
            x_p = x_center + amp_x * math.cos(theta)
            z_p = z_center + amp_z * math.sin(2.0 * theta)
        elif mode == 8:
            theta_single = math.pi * t_frac
            x_p = x_center - amp_x * math.cos(theta_single)
            z_p = z_center - amp_z * math.cos(theta_single)
        elif mode == 9:
            if t_frac < 0.5:
                t_path = t_frac * 2.0
            else:
                t_path = (1.0 - t_frac) * 2.0
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
    """Linearly interpolate between two 3D points over ``n_steps`` steps."""
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
    """Compute holograms for each target position using the KD solver."""
    holograms: List[torch.Tensor] = []
    for (x, y, z) in pos_list:
        p = create_points(1, 1, x=x, y=y, z=z)
        board = transducers(16, BOARD_POSITIONS)
        holo = kd_solver(p, board)
        holo = add_lev_sig(holo)
        holograms.append(holo)
    return holograms


# ===========================================================================
# JSON parameter loader (from auto.py)
# ===========================================================================

def load_parameter_combinations(json_path: str) -> List[Dict[str, object]]:
    """Load the trajectory parameter combinations from a JSON file."""
    with open(json_path, "r", encoding="utf-8") as f:
        combos: List[Dict[str, object]] = json.load(f)
    return combos


def ask_global_parameters() -> Tuple[int, float, int]:
    """Prompt the user once for the global trajectory parameters.

    Returns (n_steps, rev_hz, num_loops).
    """
    try:
        n_steps_str = input("Enter number of steps per cycle [default 40]: ").strip()
        n_steps = int(n_steps_str) if n_steps_str else 40
        if n_steps <= 0:
            raise ValueError
    except Exception:
        print("[WARN] Invalid step count; using 40.")
        n_steps = 40
    try:
        rev_hz_str = input("Enter motion frequency in Hz [default 10.0]: ").strip()
        rev_hz = float(rev_hz_str) if rev_hz_str else 10.0
        if rev_hz <= 0:
            raise ValueError
    except Exception:
        print("[WARN] Invalid frequency; using 10.0 Hz.")
        rev_hz = 10.0
    try:
        num_loops_str = input("Enter number of loops (cycles) [default 10]: ").strip()
        num_loops = int(num_loops_str) if num_loops_str else 10
        if num_loops <= 0:
            raise ValueError
    except Exception:
        print("[WARN] Invalid loop count; using 10.")
        num_loops = 10
    return n_steps, rev_hz, num_loops


# ===========================================================================
# Main loop: run all trajectories with external sync recording
# ===========================================================================

def _open_preview_recorder() -> InfinicamRecorder | None:
    """Open INFINICAM for preview/focus only.

    The external-sync capture path opens the camera in EXTERNAL mode, so this
    preview handle must be closed before each capture.
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


def run_all_trajectories() -> None:
    """Execute all trajectories sequentially using external-sync recording.

    High-level steps:
    1. Connect to the PAT.
    2. Request global motion settings (steps, frequency, loops) once.
    3. Centre the particle and open a focus preview once.
    4. Iterate over each trajectory definition from the JSON file:
       - Compute holograms
       - Smooth move to start position
       - External-sync recording via run_external_sync_recording()
       - Save ideal log CSV
       - Move back to centre
    5. Shutdown PAT and camera.
    """
    Path(SAVE_DIR).mkdir(parents=True, exist_ok=True)

    # Connect to levitation controller
    lev = LevitatorController(ids=(101, 3))
    print("[INFO] Connected to PAT via AcousTools")

    # Compute centre hologram and levitate
    current_static_pos: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    current_static_holo = compute_holograms_for_positions([current_static_pos])[0]
    try:
        lev.levitate(current_static_holo)
        time.sleep(0.5)
    except Exception as e:
        print(f"[WARN] levitate failed during initialisation: {e}")

    # Ask global parameters once
    n_steps, rev_hz, num_loops = ask_global_parameters()

    # Focus preview once
    if ENABLE_PREVIEW:
        recorder = _open_preview_recorder()
        if recorder is not None:
            print("\n[FOCUS] Opening live preview for focusing. Press 'q' to close, 'Esc' to quit.")
            if not focus_preview_loop(recorder):
                print("[INFO] User aborted via focus preview. Exiting.")
                try:
                    recorder.close()
                except Exception:
                    pass
                return
            # Close preview recorder (external sync will open its own camera)
            try:
                recorder.close()
            except Exception:
                pass
            recorder = None

    # Load trajectory parameter combinations
    param_json_path = os.path.join(os.path.dirname(__file__), "shape_param_combinations_10Hz_aspect2_extended.json")
    combos = load_parameter_combinations(param_json_path)

    # Iterate over each shape/parameter combination
    for entry in combos:
        shape = entry.get("shape")
        if not isinstance(shape, str) or shape not in SHAPE_TO_MODE:
            continue
        params: Dict[str, float] = entry.get("params", {})  # type: ignore
        mode = SHAPE_TO_MODE[shape]

        # Extract amplitude parameters (mm)
        amp_x_mm = 0.0
        amp_z_mm = 0.0
        amp_scale_mm = 0.0
        if shape == "Line":
            amp_z_mm = float(params.get("amplitude_mm", 0.0))
        elif shape == "Heart":
            amp_scale_mm = float(params.get("scale_mm", 0.0))
        elif shape in ("Ellipse", "Diagonal_Line", "Diagonal_One_Line"):
            amp_x_mm = float(params.get("major_axis_mm", 0.0))
            amp_z_mm = float(params.get("minor_axis_mm", 0.0))
        elif shape in ("Rectangle", "Eight", "Infinite"):
            amp_x_mm = float(params.get("x_mm", 0.0))
            amp_z_mm = float(params.get("z_mm", 0.0))

        # Convert mm to m
        amp_x = amp_x_mm * 1e-3
        amp_z = amp_z_mm * 1e-3
        amp_scale = amp_scale_mm * 1e-3

        shape_name = shape

        # Build short parameter tag for directory name
        param_parts: List[str] = []
        if amp_x_mm:
            ax = f"{amp_x_mm:.1f}".replace('.', 'p')
            param_parts.append(f"x{ax}")
        if amp_z_mm:
            az = f"{amp_z_mm:.1f}".replace('.', 'p')
            param_parts.append(f"z{az}")
        if amp_scale_mm:
            ascale = f"{amp_scale_mm:.1f}".replace('.', 'p')
            param_parts.append(f"s{ascale}")
        param_tag = "_".join(param_parts) if param_parts else "params"

        run_stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        run_name = f"{shape_name}_s{n_steps}_f{rev_hz:.1f}_l{num_loops}_{param_tag}"
        run_base = sanitize_filename(run_name, max_len=60)
        run_dir = os.path.join(SAVE_DIR, shape_name, f"{run_stamp}_{param_tag}")
        Path(run_dir).mkdir(parents=True, exist_ok=True)

        # -----------------------------------------------------------
        # Compute one cycle of positions and holograms
        # -----------------------------------------------------------
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
        print(f"[INFO] Computing holograms for {shape_name} with parameters {params}...")
        cycle_holograms = compute_holograms_for_positions(cycle_positions)

        # Mode 8 special handling
        if mode == 8:
            total_frames = n_steps * num_loops
            if total_frames > len(cycle_positions):
                static_count = total_frames - len(cycle_positions)
                cycle_positions = cycle_positions + [cycle_positions[-1]] * static_count
                cycle_holograms = cycle_holograms + [cycle_holograms[-1]] * static_count
            pat_loops = 1
        else:
            pat_loops = num_loops

        # -----------------------------------------------------------
        # Prepare PAT
        # -----------------------------------------------------------
        fps_req = int(n_steps * rev_hz)
        actual_fps = lev.set_frame_rate(fps_req) or fps_req
        print(f"[INFO] PAT frame rate set: requested={fps_req} Hz, actual={actual_fps} Hz")

        print("[PREP] Preparing ctypes buffers for PAT send...")
        t_prep0 = time.perf_counter_ns()
        phases_ct, amps_ct, num_geometries = prepare_message_from_holograms(
            lev, cycle_holograms, permute=True
        )
        t_prep1 = time.perf_counter_ns()
        prep_s = ns_to_s(t_prep1 - t_prep0)
        print(f"[PREP] num_geometries={num_geometries}, prep_time={prep_s:.3f} s")

        expected_geoms = len(cycle_positions)
        if num_geometries != expected_geoms:
            print(
                f"[WARN] Unexpected geometry count: got {num_geometries}, expected {expected_geoms}. "
                "Using reported value."
            )

        start_pos = cycle_positions[0]
        end_pos = cycle_positions[-1]

        # -----------------------------------------------------------
        # Smooth approach to start position
        # -----------------------------------------------------------
        print("[INFO] Moving smoothly to the orbit start position...")
        approach_positions = generate_smooth_positions(current_static_pos, start_pos, n_steps)
        for pos in approach_positions:
            h = compute_holograms_for_positions([pos])[0]
            try:
                lev.levitate(h)
            except Exception as e:
                print(f"[WARN] levitate during approach failed: {e}")
            time.sleep(0.05)
        current_static_pos = start_pos
        current_static_holo = cycle_holograms[0]

        # -----------------------------------------------------------
        # External sync recording
        # -----------------------------------------------------------
        expected_duration = (num_geometries * pat_loops) / float(actual_fps)

        n_frames = int(num_geometries * pat_loops)

        # Convert ROI to string format for run_external_sync_recording
        if ROI is None:
            roi_spec = None
        elif isinstance(ROI, (tuple, list)):
            roi_spec = ",".join(str(int(v)) for v in ROI)
        else:
            roi_spec = str(ROI)

        capture_meta = {
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

        # Determine capture FPS (may differ from PAT FPS)
        capture_fps = float(EXTERNAL_CAPTURE_FPS) if EXTERNAL_CAPTURE_FPS is not None else float(actual_fps)
        # Compute capture frame count to span the full PAT duration at the capture FPS
        n_frames_capture = int(math.ceil(float(num_geometries * pat_loops) * capture_fps / max(float(actual_fps), 1e-9)))
        n_frames_capture = capture_fps*1.500


        print(f"[INFO] External sync recording: {n_frames_capture} frames @ {capture_fps} fps")
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
            arm_delay_sec=2.0,
            low_hold_sec=0.5,
            queue_max=128,
            decode_threads=DECODE_THREADS,
            use_gpu=EXTERNAL_CAPTURE_USE_GPU,
            encode_after=True,
            encode_fps=None,
        )

        # -----------------------------------------------------------
        # Save ideal position log
        # -----------------------------------------------------------
        log_filename = os.path.join(run_dir, f"{run_base}_ideal_log.csv")
        print(f"[INFO] Saving ideal position log to {log_filename}...")
        dt = 1.0 / float(actual_fps) if actual_fps else 0.0
        all_positions: List[Tuple[float, float, float]] = []
        for _ in range(int(pat_loops)):
            all_positions.extend(cycle_positions)
        with open(log_filename, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["time", "target_x", "target_y", "target_z"])
            for i, pos in enumerate(all_positions):
                t = i * dt
                writer.writerow([
                    f"{t:.6f}",
                    f"{pos[0]:.6f}",
                    f"{pos[1]:.6f}",
                    f"{pos[2]:.6f}",
                ])
        print(f"[INFO] Ideal log saved with {len(all_positions)} rows.")

        # Optionally create PAT clip
        if ENABLE_PAT_CLIP and summary is not None:
            try:
                src_avi_path = None
                csv_path_clip = None
                if isinstance(summary, dict):
                    src_avi_path = summary.get("avi_path") or summary.get("path")
                    csv_path_clip = summary.get("csv_path") or summary.get("timestamps_csv")
                if src_avi_path and csv_path_clip:
                    clip_path = make_pat_clip(
                        src_avi=src_avi_path,
                        csv_path=csv_path_clip,
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

        # -----------------------------------------------------------
        # Return to centre
        # -----------------------------------------------------------
        current_static_pos = end_pos
        current_static_holo = cycle_holograms[-1]
        try:
            lev.levitate(current_static_holo)
        except Exception as e:
            print(f"[WARN] levitate to end position failed: {e}")

        centre_pos: Tuple[float, float, float] = (0.0, 0.0, 0.0)
        print("[INFO] Moving back to centre...")
        return_positions = generate_smooth_positions(current_static_pos, centre_pos, n_steps)
        for pos in return_positions:
            h = compute_holograms_for_positions([pos])[0]
            try:
                lev.levitate(h)
            except Exception as e:
                print(f"[WARN] levitate during return failed: {e}")
            time.sleep(0.05)
        current_static_pos = centre_pos
        current_static_holo = compute_holograms_for_positions([centre_pos])[0]
        try:
            lev.levitate(current_static_holo)
        except Exception:
            pass
        print("[INFO] Particle returned to centre.")
    # End for each entry

    # -------------------------------------------------------------------
    # Shutdown
    # -------------------------------------------------------------------
    print("[INFO] All trajectories complete. Shutting down...")
    try:
        off_phase = torch.zeros_like(current_static_holo)
        off_phase = add_lev_sig(off_phase)
        phases_ct, amps_ct, _ = prepare_message_from_holograms(lev, [off_phase], permute=True)
        lev.send_message(phases_ct, amps_ct, 0, 1, sleep_ms=0, loop=False, num_loops=1)
    except Exception:
        pass
    print("[INFO] Shutdown complete.")


if __name__ == "__main__":  # pragma: no cover
    run_all_trajectories()
