#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JSON-driven automated AcousTools + event-camera trajectory recorder.

This script reuses the camera/PAT implementation from ``acoustools_eventcam_sync.py``
and replaces the per-run trajectory prompts with a JSON parameter loop.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import torch

import acoustools_eventcam_sync as sync
from acoustools.Levitator import LevitatorController
from acoustools.Utilities import add_lev_sig


AUTO_SAVE_DIR = "./rec_eventcam_auto"
AUTO_POSTPROCESS_OUTPUT_DIR = "./proc_eventcam_auto"
AUTO_TRACK_NPZ = False
AUTO_RUN_BASE_MAX_LEN = 42
AUTO_RUN_FOLDER_MAX_LEN = 48
AUTO_TRANSFER_STEP_MM = 0.25
AUTO_TRANSFER_DWELL_SEC = 0.04

SHAPE_TO_MODE: Dict[str, int] = {
    "Line": 1,
    "Ellipse": 2,
    "Diagonal_Line": 3,
    "Heart": 4,
    "Rectangle": 5,
    "Eight": 6,
    "Infinite": 7,
    "Diagonal_One_Line": 8,
    "S": 9,
    "S_Shaped": 9,
    "S_shaped": 9,
    "X_Line": 10,
    "X-axis_vibration": 10,
    "X_axis_vibration": 10,
    "Long_Random_Stroke": 11,
    "Random_Stroke": 11,
}

LONG_RANDOM_SHAPES = {"Long_Random_Stroke", "Random_Stroke"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run event-camera AcousTools trajectories from a JSON parameter list.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--json",
        default="shape_param_combinations_10Hz_aspect2_extended_raw_headers.json",
        help="JSON file containing [{'shape': ..., 'params': {...}}, ...].",
    )
    parser.add_argument("--steps", type=int, default=100, help="Number of trajectory steps per cycle.")
    parser.add_argument("--freq-hz", type=float, default=10.0, help="Trajectory cycles per second.")
    parser.add_argument("--loops", type=int, default=10, help="Number of cycles to run per condition.")
    parser.add_argument("--start-index", type=int, default=0, help="Skip JSON entries before this zero-based index.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of valid entries to run. 0 means all.")
    parser.add_argument("--shape", action="append", default=[], help="Only run this shape. Can be specified multiple times.")
    parser.add_argument("--dry-run", action="store_true", help="Print resolved run list without connecting to hardware.")
    parser.add_argument("--no-preview", action="store_true", help="Skip the one-time event-camera preview before automatic runs.")
    parser.add_argument("--preview-once", action="store_true", help="Open the event-camera preview once before the first run. Kept for compatibility; this is now the default.")
    parser.add_argument("--preview-each-run", action="store_true", help="Open the event-camera preview before every run.")
    parser.add_argument("--no-return-centre", action="store_true", help="Do not move back to centre after each run.")
    parser.add_argument("--confirm-each-run", action="store_true", help="Wait for Enter before each run starts.")
    return parser.parse_args()


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


def load_parameter_combinations(json_path: str | Path) -> List[Dict[str, Any]]:
    path = Path(json_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, list):
        raise SystemExit(f"JSON root must be a list: {path}")
    return [entry for entry in payload if isinstance(entry, dict)]


def entry_to_run(entry: Dict[str, Any]) -> Dict[str, Any] | None:
    shape = entry.get("shape")
    if not isinstance(shape, str) or shape not in SHAPE_TO_MODE:
        return None
    params = entry.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    amp_x_mm = 0.0
    amp_z_mm = 0.0
    amp_scale_mm = 0.0
    if shape == "Line":
        amp_z_mm = get_param(params, "Amplitude(mm)", "amplitude_mm", "amplitude")
    elif shape in ("X_Line", "X-axis_vibration", "X_axis_vibration"):
        amp_x_mm = get_param(params, "Amplitude(mm)", "amplitude_mm", "amplitude", "X(mm)", "x_mm", "x")
    elif shape in LONG_RANDOM_SHAPES:
        amp_x_mm = get_param(params, "x_limit_mm", "X_limit(mm)", "X(mm)", "x_mm", "x")
        amp_z_mm = get_param(params, "z_limit_mm", "Z_limit(mm)", "Z(mm)", "z_mm", "z")
    elif shape == "Heart":
        amp_scale_mm = get_param(params, "Scale(mm)", "scale_mm", "scale")
    elif shape in ("Ellipse", "Diagonal_Line", "Diagonal_One_Line"):
        amp_x_mm = get_param(params, "Major axis(mm)", "major_axis_mm", "major_axis")
        amp_z_mm = get_param(params, "minor axis(mm)", "minor_axis_mm", "minor_axis")
    elif shape in ("Rectangle", "Eight", "Infinite"):
        amp_x_mm = get_param(params, "X(mm)", "x_mm", "x")
        amp_z_mm = get_param(params, "Z(mm)", "z_mm", "z")
    elif shape in ("S", "S_Shaped", "S_shaped"):
        amp_x_mm = get_param(params, "X(mm)", "x_mm", "x")
        amp_z_mm = get_param(params, "Z(mm)", "z_mm", "z")

    return {
        "shape": shape,
        "mode": SHAPE_TO_MODE[shape],
        "params": params,
        "amp_x_mm": float(amp_x_mm),
        "amp_z_mm": float(amp_z_mm),
        "amp_scale_mm": float(amp_scale_mm),
    }


def iter_runs(entries: Iterable[Dict[str, Any]], shapes: set[str], start_index: int, limit: int) -> List[Dict[str, Any]]:
    runs: List[Dict[str, Any]] = []
    for json_index, entry in enumerate(entries):
        if json_index < max(0, int(start_index)):
            continue
        run = entry_to_run(entry)
        if run is None:
            continue
        if shapes and run["shape"] not in shapes:
            continue
        run["json_index"] = json_index
        runs.append(run)
        if limit > 0 and len(runs) >= limit:
            break
    return runs


def make_param_tag(amp_x_mm: float, amp_z_mm: float, amp_scale_mm: float) -> str:
    parts: List[str] = []
    if abs(amp_x_mm) > 1e-9:
        parts.append(f"x{sync.fmt_mm(amp_x_mm)}")
    if abs(amp_z_mm) > 1e-9:
        parts.append(f"z{sync.fmt_mm(amp_z_mm)}")
    if abs(amp_scale_mm) > 1e-9:
        parts.append(f"s{sync.fmt_mm(amp_scale_mm)}")
    return "_".join(parts) if parts else "params"


def write_ideal_log(path: str | Path, positions: List[Tuple[float, float, float]], actual_fps: float) -> None:
    dt = 1.0 / float(actual_fps) if actual_fps else 0.0
    with open(path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["time", "target_x", "target_y", "target_z"])
        for i, pos in enumerate(positions):
            writer.writerow([f"{i * dt:.6f}", f"{pos[0]:.6f}", f"{pos[1]:.6f}", f"{pos[2]:.6f}"])


def is_long_random_run(run: Dict[str, Any]) -> bool:
    return str(run.get("shape", "")) in LONG_RANDOM_SHAPES or int(run.get("mode", 0)) == 11


def generate_auto_transfer_positions(
    start_pos: Tuple[float, float, float],
    end_pos: Tuple[float, float, float],
) -> List[Tuple[float, float, float]]:
    dx = float(end_pos[0]) - float(start_pos[0])
    dy = float(end_pos[1]) - float(start_pos[1])
    dz = float(end_pos[2]) - float(start_pos[2])
    distance_m = math.sqrt(dx * dx + dy * dy + dz * dz)
    if distance_m <= 0:
        return []
    max_step_m = max(1e-6, float(AUTO_TRANSFER_STEP_MM) * 1e-3)
    n_transfer_steps = max(1, int(math.ceil(distance_m / max_step_m)))
    return sync.generate_smooth_positions(start_pos, end_pos, n_transfer_steps)


def move_static_position(
    lev: LevitatorController,
    start_pos: Tuple[float, float, float],
    end_pos: Tuple[float, float, float],
    label: str,
) -> Tuple[float, float, float]:
    transfer_positions = generate_auto_transfer_positions(start_pos, end_pos)
    distance_mm = math.dist(start_pos, end_pos) * 1e3
    duration_s = len(transfer_positions) * float(AUTO_TRANSFER_DWELL_SEC)
    print(
        f"[INFO] {label}: distance={distance_mm:.3f} mm, "
        f"steps={len(transfer_positions)}, dwell={AUTO_TRANSFER_DWELL_SEC:.3f} s, "
        f"duration={duration_s:.3f} s"
    )
    for pos in transfer_positions:
        h = sync.mute_sync_transducer(sync.compute_holograms_for_positions([pos])[0])
        try:
            lev.levitate(h)
        except Exception as exc:
            print(f"[WARN] levitate during transfer failed: {exc}")
        time.sleep(float(AUTO_TRANSFER_DWELL_SEC))
    return end_pos


def build_event_recorder(run_dir: str) -> sync.EventCameraRecorder:
    return sync.EventCameraRecorder(
        save_dir=os.path.abspath(run_dir),
        serial=sync.EVENTCAM_SERIAL,
        live_delta_t_us=sync.EVENTCAM_LIVE_DELTA_T_US,
        postprocess_fps=sync.EVENTCAM_POSTPROCESS_FPS,
        postprocess_video_fps=sync.EVENTCAM_POSTPROCESS_VIDEO_FPS,
        postprocess_accumulation_us=sync.EVENTCAM_POSTPROCESS_ACCUMULATION_US,
        postprocess_output_dir=AUTO_POSTPROCESS_OUTPUT_DIR,
        postprocess_write_video=sync.EVENTCAM_POSTPROCESS_WRITE_VIDEO,
        sync_led_roi=sync.EVENTCAM_SYNC_LED_ROI,
        sync_led_bin_us=sync.EVENTCAM_SYNC_LED_BIN_US,
        sync_led_threshold=sync.EVENTCAM_SYNC_LED_THRESHOLD,
        sync_pre_roll_us=sync.EVENTCAM_SYNC_PRE_ROLL_US,
        sync_duration_us=sync.EVENTCAM_SYNC_DURATION_US,
        mask_led_roi=sync.EVENTCAM_MASK_LED_ROI,
        export_filtered_events_npz=sync.EVENTCAM_EXPORT_FILTERED_EVENTS_NPZ,
        auto_track_npz=AUTO_TRACK_NPZ,
        log_queue_max=sync.EVENTCAM_LOG_QUEUE_MAX,
        enable_trigger_in=sync.EVENTCAM_ENABLE_TRIGGER_IN,
        trigger_channel_names=sync.EVENTCAM_TRIGGER_CHANNEL_NAMES,
    )


def run_one(
    *,
    lev: LevitatorController,
    run: Dict[str, Any],
    n_steps: int,
    rev_hz: float,
    num_loops: int,
    current_static_pos: Tuple[float, float, float],
    return_centre: bool,
    confirm_each_run: bool,
    preview_each_run: bool,
) -> Tuple[Tuple[float, float, float], torch.Tensor | None]:
    mode = int(run["mode"])
    shape_name = sync.MODE_SHAPE_NAMES.get(mode, str(run["shape"]))
    amp_x_mm = float(run["amp_x_mm"])
    amp_z_mm = float(run["amp_z_mm"])
    amp_scale_mm = float(run["amp_scale_mm"])
    amp_x = amp_x_mm * 1e-3
    amp_z = amp_z_mm * 1e-3
    amp_scale = amp_scale_mm * 1e-3
    params = run.get("params", {})
    if not isinstance(params, dict):
        params = {}
    long_random = is_long_random_run(run)

    param_tag = make_param_tag(amp_x_mm, amp_z_mm, amp_scale_mm)
    trajectory_extra: Dict[str, float] = {}
    run_n_steps = int(n_steps)
    run_rev_hz = float(rev_hz)
    run_num_loops = int(num_loops)
    if long_random:
        duration_sec = max(0.1, get_param(params, "duration_sec", "duration", default=8.0))
        sample_hz = max(1.0, get_param(params, "sample_hz", "fps", "pat_fps", default=1000.0))
        seed = int(round(get_param(params, "seed", default=1.0)))
        run_n_steps = max(2, int(round(duration_sec * sample_hz)))
        run_rev_hz = float(sample_hz) / float(run_n_steps)
        run_num_loops = 1
        param_tag = sync.sanitize_filename(
            f"seed{seed}_x{sync.fmt_mm(amp_x_mm)}_z{sync.fmt_mm(amp_z_mm)}_"
            f"{duration_sec:g}s_{sample_hz:g}Hz",
            max_len=48,
        )

    run_stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_base = sync.sanitize_filename(
        f"{shape_name}_s{run_n_steps}_f{run_rev_hz:.4f}_l{run_num_loops}_{param_tag}",
        max_len=AUTO_RUN_BASE_MAX_LEN,
    )
    run_folder = sync.sanitize_filename(
        f"{shape_name}_{run_stamp}_{param_tag}",
        max_len=AUTO_RUN_FOLDER_MAX_LEN,
    )
    run_dir = os.path.join(AUTO_SAVE_DIR, shape_name, run_folder)
    Path(run_dir).mkdir(parents=True, exist_ok=True)

    run_desc = (
        f"{shape_name}_steps{run_n_steps}_freq{run_rev_hz:.4f}_loops{run_num_loops}_"
        f"ampX{amp_x_mm:.3f}mm_ampZ{amp_z_mm:.3f}mm_ampScale{amp_scale_mm:.3f}mm"
    )
    print(f"\n[AUTO] JSON #{run['json_index']}: {run_desc}")

    if long_random:
        cycle_positions, trajectory_extra = sync.generate_long_random_stroke_positions(
            params=params,
            x_center=current_static_pos[0],
            y_center=current_static_pos[1],
            z_center=current_static_pos[2],
        )
        run_n_steps = len(cycle_positions)
        sample_hz = float(trajectory_extra.get("sample_hz", get_param(params, "sample_hz", default=1000.0)))
        run_rev_hz = sample_hz / float(max(run_n_steps, 1))
        run_num_loops = 1
        print(
            "[AUTO] Generated long random stroke: "
            f"points={run_n_steps}, sample_hz={sample_hz:g}, "
            f"duration={trajectory_extra.get('duration_sec', 0.0):.3f}s, "
            f"max_speed={trajectory_extra.get('max_speed_mm_s', 0.0):.1f}mm/s, "
            f"max_accel={trajectory_extra.get('max_accel_mm_s2', 0.0):.1f}mm/s^2"
        )
    else:
        cycle_positions = sync.generate_cycle_positions(
            mode=mode,
            n_steps=run_n_steps,
            amp_x=amp_x,
            amp_z=amp_z,
            amp_scale=amp_scale,
            x_center=current_static_pos[0],
            y_center=current_static_pos[1],
            z_center=current_static_pos[2],
        )
    print("[INFO] Computing holograms for one cycle...")
    cycle_holograms = sync.compute_holograms_for_positions(cycle_positions)

    if long_random:
        pat_loops = 1
    elif mode == 8:
        total_frames = run_n_steps * run_num_loops
        if total_frames > len(cycle_positions):
            static_count = total_frames - len(cycle_positions)
            cycle_positions = cycle_positions + [cycle_positions[-1]] * static_count
            cycle_holograms = cycle_holograms + [cycle_holograms[-1]] * static_count
        pat_loops = 1
    else:
        pat_loops = run_num_loops

    if long_random:
        pat_rate = sync.compute_pat_frame_rate_info(1, float(trajectory_extra.get("sample_hz", 1000.0)))
    else:
        pat_rate = sync.compute_pat_frame_rate_info(run_n_steps, run_rev_hz)
    if not pat_rate.is_supported:
        raise ValueError(f"Unsupported PAT frame rate: {sync.describe_pat_frame_rate_info(pat_rate)}")
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

    print("[PREP] Preparing ctypes buffers for PAT send...")
    t_prep0 = time.perf_counter_ns()
    phases_ct, amps_ct, num_geometries = sync.prepare_message_from_holograms(lev, cycle_holograms, permute=True)
    prep_s = sync.ns_to_s(time.perf_counter_ns() - t_prep0)
    print(f"[PREP] num_geometries={num_geometries}, prep_time={prep_s:.3f} s")

    start_pos = cycle_positions[0]
    end_pos = cycle_positions[-1]
    current_static_pos = move_static_position(
        lev,
        current_static_pos,
        start_pos,
        "Moving smoothly to the orbit start position",
    )

    recorder = build_event_recorder(run_dir)
    try:
        if preview_each_run:
            if not sync.eventcam_preview_loop(recorder):
                raise KeyboardInterrupt("aborted from event-camera preview")

        if confirm_each_run:
            input("\n>>> Press Enter to start recording and motion for this JSON entry. <<<\n")

        expected_duration = (num_geometries * pat_loops) / float(actual_fps)
        all_positions: List[Tuple[float, float, float]] = []
        for _ in range(int(pat_loops)):
            all_positions.extend(cycle_positions)
        log_filename = os.path.abspath(os.path.join(run_dir, f"{run_base}_ideal_log.csv"))
        print(f"[INFO] Saving ideal position log to {log_filename}...")
        write_ideal_log(log_filename, all_positions, actual_fps)
        print(f"[INFO] Ideal log saved with {len(all_positions)} rows.")

        recorder.start_recording(
            run_base,
            extra_meta={
                "run_desc": run_desc,
                "mode": mode,
                "shape_name": shape_name,
                "run_dir": os.path.abspath(run_dir),
                "ideal_log": log_filename,
                "steps_per_cycle": run_n_steps,
                "rev_hz": run_rev_hz,
                "num_loops": run_num_loops,
                "pat_loops": pat_loops,
                "amp_x_mm": amp_x_mm,
                "amp_z_mm": amp_z_mm,
                "amp_scale_mm": amp_scale_mm,
                "pat_fps_requested": fps_req,
                "pat_fps_actual": actual_fps,
                "pat_fps_set_return": set_fps_return,
                "pat_fps_base_hz": sync.PAT_UPDATE_BASE_HZ,
                "pat_fps_divider": pat_rate.divider,
                "pat_fps_alpha": pat_rate.alpha,
                "pat_fps_error_ms_per_s": pat_rate.error_ms_per_s,
                "expected_duration_sec": expected_duration,
                "prep_time_sec": prep_s,
                "json_index": int(run["json_index"]),
                "json_shape": str(run["shape"]),
                "json_params": run.get("params", {}),
                "generated_trajectory": trajectory_extra,
                "eventcam_trigger_in_enabled": sync.EVENTCAM_ENABLE_TRIGGER_IN,
                "eventcam_trigger_channel_names": list(sync.EVENTCAM_TRIGGER_CHANNEL_NAMES),
                "sync_transducer_index": sync.SYNC_TRANSDUCER_INDEX,
            },
        )

        pat_start_pc_ns = int(time.perf_counter_ns())
        pat_start_wall_ns = int(time.time_ns())
        recorder.log_event(
            "PAT_START",
            pc_ns=pat_start_pc_ns,
            wall_ns=pat_start_wall_ns,
            note=(
                f"expected_duration_sec={expected_duration:.6f}, "
                f"pat_fps_actual={actual_fps}, num_geometries={num_geometries}, pat_loops={pat_loops}"
            ),
        )
        recorder.log_event("PAT_SEND_CALL_START", pc_ns=pat_start_pc_ns, wall_ns=pat_start_wall_ns, note=f"loop=True, num_loops={pat_loops}")

        try:
            lev.send_message(phases_ct, amps_ct, 0, int(num_geometries), sleep_ms=0, loop=True, num_loops=int(pat_loops))
        except Exception as exc:
            print(f"[ERROR] lev.send_message failed: {exc}")

        pat_send_end_pc_ns = int(time.perf_counter_ns())
        send_call_time = sync.ns_to_s(pat_send_end_pc_ns - pat_start_pc_ns)
        ratio = send_call_time / expected_duration if expected_duration > 0 else float("nan")
        print(f"[PAT] send_message() call_time={send_call_time:.6f} s | call/expected={ratio:.3f}")
        recorder.log_event(
            "PAT_SEND_CALL_END",
            pc_ns=pat_send_end_pc_ns,
            wall_ns=int(time.time_ns()),
            note=f"call_time_sec={send_call_time:.6f}, call_over_expected={ratio:.3f}",
        )

        stop_target_ns = pat_start_pc_ns + int((expected_duration + max(0.0, float(sync.POST_ROLL_SEC))) * 1e9)
        now_ns = time.perf_counter_ns()
        if now_ns < stop_target_ns:
            wait_s = (stop_target_ns - now_ns) * 1e-9
            print(f"[SYNC] Waiting {wait_s:.3f} s to ensure complete capture...")
            time.sleep(float(wait_s))
        recorder.log_event("CAM_STOP_TARGET_REACHED", pc_ns=int(stop_target_ns), wall_ns=int(time.time_ns()), note="stop_recording will be called next")

        summary = recorder.stop_recording()
        if summary is not None:
            print(f"[EVENTCAM] RAW saved: {summary.raw_path}")
    finally:
        try:
            recorder.close()
        except Exception as exc:
            print(f"[EVENTCAM][WARN] close after run failed: {exc}")

    current_static_pos = end_pos
    current_static_holo = sync.mute_sync_transducer(cycle_holograms[-1])
    try:
        lev.levitate(current_static_holo)
    except Exception as exc:
        print(f"[WARN] levitate to end position failed: {exc}")

    if return_centre:
        centre_pos = (0.0, 0.0, 0.0)
        current_static_pos = move_static_position(
            lev,
            current_static_pos,
            centre_pos,
            "Moving back to centre",
        )
        current_static_holo = sync.mute_sync_transducer(sync.compute_holograms_for_positions([centre_pos])[0])
        try:
            lev.levitate(current_static_holo)
        except Exception:
            pass
        print("[INFO] Particle returned to centre.")

    return current_static_pos, current_static_holo


def main() -> None:
    args = parse_args()
    if args.steps <= 0:
        raise SystemExit("--steps must be positive.")
    if args.freq_hz <= 0:
        raise SystemExit("--freq-hz must be positive.")
    if args.loops <= 0:
        raise SystemExit("--loops must be positive.")

    entries = load_parameter_combinations(args.json)
    runs = iter_runs(entries, set(args.shape), int(args.start_index), int(args.limit))
    pat_rate = sync.compute_pat_frame_rate_info(int(args.steps), float(args.freq_hz))
    if pat_rate.is_supported:
        print(f"[AUTO] PAT frame rate check OK: {sync.describe_pat_frame_rate_info(pat_rate)}")
    else:
        raise SystemExit(
            "[AUTO][ERROR] Unsupported PAT frame rate: "
            f"{sync.describe_pat_frame_rate_info(pat_rate)}\n"
            "[AUTO][ERROR] Choose --steps and --freq-hz so that steps * freq-hz "
            f"is an integer divisor of {sync.PAT_UPDATE_BASE_HZ} Hz "
            "(e.g. 500, 800, 1000, 1250, 1600, 2000, 2500, 4000 Hz)."
        )
    print(f"[AUTO] Loaded {len(entries)} JSON entries; selected {len(runs)} run(s).")
    for idx, run in enumerate(runs, start=1):
        print(
            f"[AUTO] {idx:03d}: json#{run['json_index']} {run['shape']} "
            f"x={run['amp_x_mm']:.3f}mm z={run['amp_z_mm']:.3f}mm scale={run['amp_scale_mm']:.3f}mm"
        )
    if args.dry_run:
        return
    if not runs:
        raise SystemExit("No runnable JSON entries selected.")

    Path(AUTO_SAVE_DIR).mkdir(parents=True, exist_ok=True)
    lev = LevitatorController(ids=(101, 3))
    print("[INFO] Connected to PAT via AcousTools")

    current_static_pos: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    current_static_holo: torch.Tensor | None = None
    try:
        current_static_holo = sync.mute_sync_transducer(sync.compute_holograms_for_positions([current_static_pos])[0])
        lev.levitate(current_static_holo)
        time.sleep(0.5)

        if not args.no_preview:
            recorder = build_event_recorder(os.path.abspath(AUTO_SAVE_DIR))
            try:
                print("[FOCUS] Opening one-time event-camera preview.")
                if not sync.eventcam_preview_loop(recorder):
                    print("[INFO] User aborted from preview.")
                    return
            finally:
                recorder.close()

        for run in runs:
            current_static_pos, current_static_holo = run_one(
                lev=lev,
                run=run,
                n_steps=int(args.steps),
                rev_hz=float(args.freq_hz),
                num_loops=int(args.loops),
                current_static_pos=current_static_pos,
                return_centre=not args.no_return_centre,
                confirm_each_run=bool(args.confirm_each_run),
                preview_each_run=bool(args.preview_each_run),
            )
    except KeyboardInterrupt:
        print("\n[INFO] User requested exit. Cleaning up...")
    finally:
        try:
            if current_static_holo is None:
                current_static_holo = sync.compute_holograms_for_positions([(0.0, 0.0, 0.0)])[0]
            off_phase = torch.zeros_like(current_static_holo)
            off_phase = add_lev_sig(off_phase)
            phases_ct, amps_ct, _ = sync.prepare_message_from_holograms(lev, [off_phase], permute=True)
            lev.send_message(phases_ct, amps_ct, 0, 1, sleep_ms=0, loop=False, num_loops=1)
        except Exception:
            pass
        print("[INFO] Shutdown complete.")


if __name__ == "__main__":
    main()
