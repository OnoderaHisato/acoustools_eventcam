#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AcousTools-only multi-trajectory levitation script.

This is the event-camera-free control loop distilled from
``acoustools_eventcam_sync.py``.  It keeps the AcousTools trajectory,
hologram preparation, PAT frame-rate validation, ideal-log writing, and
smooth transfer motion, but does not open or use the event camera.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

import torch

from acoustools.Levitator import LevitatorController
from acoustools.Utilities import add_lev_sig

from acoustools_eventcam_sync import (
    MODE_SHAPE_NAMES,
    PAT_UPDATE_BASE_HZ,
    compute_holograms_for_positions,
    compute_pat_frame_rate_info,
    describe_pat_frame_rate_info,
    fmt_mm,
    generate_cycle_positions,
    move_static_position,
    mute_sync_transducer,
    ns_to_s,
    prepare_message_from_holograms,
    sanitize_filename,
)


SAVE_DIR = "./rec_acoustools"
DEFAULT_STEPS_PER_CYCLE = 400
DEFAULT_FREQ_HZ = 10.0
DEFAULT_LOOPS = 10
CLOSED_3D_MODES = frozenset({12, 13, 14, 15})
MENU_MODE_MAP = {
    **{mode: mode for mode in range(1, 11)},
    11: 12,
    12: 13,
    13: 14,
    14: 15,
}


@dataclass(frozen=True)
class TrajectoryParameters:
    amp_x_mm: float = 0.0
    amp_y_mm: float = 0.0
    amp_z_mm: float = 0.0
    amp_scale_mm: float = 0.0
    helix_minor_radius_mm: float = 0.0
    helix_turns: int = 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run AcousTools trajectories without opening an event camera."
    )
    parser.add_argument(
        "--preview-3d-samples",
        action="store_true",
        help="Write the four 3D sample plots and exit without connecting to PAT.",
    )
    parser.add_argument(
        "--sample-output-dir",
        type=Path,
        default=Path("trajectory_samples_3d"),
        help="Output directory used by --preview-3d-samples.",
    )
    return parser.parse_args()


def prompt_mode() -> int | None:
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
    print("  --- Closed 3D trajectories ---")
    print(" 11: 3D figure-eight (XZ: figure-eight, XY: circle/ellipse)")
    print(" 12: Closed toroidal helix")
    print(" 13: Trefoil knot")
    print(" 14: 3D Lissajous (frequency ratio 1:2:3)")
    try:
        menu_mode = int(input("Select mode (1-14): ").strip())
    except (ValueError, EOFError):
        print("[WARN] Invalid input; please enter a number between 1 and 14.")
        return None
    if menu_mode not in MENU_MODE_MAP:
        print("[WARN] Mode must be between 1 and 14.")
        return None
    return MENU_MODE_MAP[menu_mode]


def prompt_amplitudes_mm(mode: int) -> TrajectoryParameters | None:
    amp_x_mm = 0.0
    amp_y_mm = 0.0
    amp_z_mm = 0.0
    amp_scale_mm = 0.0
    helix_minor_radius_mm = 0.0
    helix_turns = 3
    try:
        if mode == 1:
            amp_z_mm = float(input("Enter Z-axis amplitude (+/-mm) [e.g. 0.15]: ").strip())
        elif mode == 10:
            amp_x_mm = float(input("Enter X-axis amplitude (+/-mm) [e.g. 0.15]: ").strip())
        elif mode == 2:
            amp_x_mm = float(input("Enter X-axis semi-axis (+/-mm) [e.g. 2.0]: ").strip())
            amp_z_mm = float(input("Enter Z-axis semi-axis (+/-mm) [e.g. 1.5]: ").strip())
        elif mode == 3:
            amp_x_mm = float(input("Enter X-axis amplitude (+/-mm): ").strip())
            amp_z_mm = float(input("Enter Z-axis amplitude (+/-mm): ").strip())
        elif mode == 4:
            amp_scale_mm = float(input("Enter heart scale (mm): ").strip())
        elif mode in (5, 6, 7, 8, 9):
            amp_x_mm = float(input("Enter X-axis amplitude (+/-mm): ").strip())
            amp_z_mm = float(input("Enter Z-axis amplitude (+/-mm): ").strip())
        elif mode == 12:
            amp_x_mm = float(input("Enter X radius (+/-mm) [default 2.0]: ").strip() or "2.0")
            amp_y_mm = float(
                input(f"Enter Y radius (+/-mm) [default {amp_x_mm:g}, gives XY circle]: ").strip()
                or str(amp_x_mm)
            )
            amp_z_mm = float(input("Enter Z figure-eight amplitude (+/-mm) [default 1.5]: ").strip() or "1.5")
        elif mode == 13:
            amp_x_mm = float(input("Enter torus major radius (mm) [default 2.5]: ").strip() or "2.5")
            helix_minor_radius_mm = float(
                input("Enter radial winding radius (mm) [default 0.75]: ").strip() or "0.75"
            )
            amp_z_mm = float(input("Enter Z winding amplitude (+/-mm) [default 0.75]: ").strip() or "0.75")
            helix_turns = int(input("Enter integer windings per closed cycle [default 3]: ").strip() or "3")
            if helix_turns <= 0:
                raise ValueError
            if abs(helix_minor_radius_mm) >= abs(amp_x_mm):
                print("[WARN] Radial winding radius is at least the major radius; the orbit may fold through its centre.")
        elif mode == 14:
            amp_x_mm = float(input("Enter trefoil X scale (mm) [default 2.5]: ").strip() or "2.5")
            amp_y_mm = float(input("Enter trefoil Y scale (mm) [default 2.5]: ").strip() or "2.5")
            amp_z_mm = float(input("Enter trefoil Z amplitude (+/-mm) [default 1.5]: ").strip() or "1.5")
        elif mode == 15:
            amp_x_mm = float(input("Enter Lissajous X amplitude (+/-mm) [default 2.0]: ").strip() or "2.0")
            amp_y_mm = float(input("Enter Lissajous Y amplitude (+/-mm) [default 2.0]: ").strip() or "2.0")
            amp_z_mm = float(input("Enter Lissajous Z amplitude (+/-mm) [default 2.0]: ").strip() or "2.0")
    except (ValueError, EOFError):
        print("[WARN] Invalid amplitude; please enter numeric values.")
        return None
    return TrajectoryParameters(
        amp_x_mm=amp_x_mm,
        amp_y_mm=amp_y_mm,
        amp_z_mm=amp_z_mm,
        amp_scale_mm=amp_scale_mm,
        helix_minor_radius_mm=helix_minor_radius_mm,
        helix_turns=helix_turns,
    )


def prompt_timing(mode: int) -> tuple[int, float, int, object] | None:
    default_frequency_hz = 2.0 if mode in CLOSED_3D_MODES else DEFAULT_FREQ_HZ
    while True:
        try:
            n_steps = int(
                input(
                    f"Enter number of steps per cycle [default {DEFAULT_STEPS_PER_CYCLE}]: "
                ).strip()
                or str(DEFAULT_STEPS_PER_CYCLE)
            )
            if n_steps <= 0:
                raise ValueError
        except (ValueError, EOFError):
            print("[WARN] Invalid step count; please enter a positive integer.")
            continue

        try:
            rev_hz = float(
                input(f"Enter motion frequency in Hz [default {default_frequency_hz:g}]: ").strip()
                or str(default_frequency_hz)
            )
            if rev_hz <= 0:
                raise ValueError
        except (ValueError, EOFError):
            print("[WARN] Invalid frequency; please enter a positive number.")
            continue

        try:
            num_loops = int(
                input(f"Enter number of loops (cycles) [default {DEFAULT_LOOPS}]: ").strip()
                or str(DEFAULT_LOOPS)
            )
            if num_loops <= 0:
                raise ValueError
        except (ValueError, EOFError):
            print("[WARN] Invalid loop count; please enter a positive integer.")
            continue

        pat_rate = compute_pat_frame_rate_info(n_steps, rev_hz)
        if pat_rate.is_supported:
            print(f"[INFO] PAT frame rate check OK: {describe_pat_frame_rate_info(pat_rate)}")
            return n_steps, rev_hz, num_loops, pat_rate

        print(f"[WARN] Unsupported PAT frame rate: {describe_pat_frame_rate_info(pat_rate)}")
        print(
            "[WARN] Please choose steps/frequency so that "
            f"steps * frequency is an integer divisor of {PAT_UPDATE_BASE_HZ} Hz "
            "(e.g. 500, 800, 1000, 1250, 1600, 2000, 2500, 4000 Hz)."
        )


def make_run_paths(
    mode: int,
    n_steps: int,
    rev_hz: float,
    num_loops: int,
    params: TrajectoryParameters,
) -> tuple[str, str, str, str, str]:
    shape_name = MODE_SHAPE_NAMES.get(mode, f"mode{mode}")
    run_desc = (
        f"{shape_name}_steps{n_steps}_freq{rev_hz:.1f}_loops{num_loops}_"
        f"ampX{params.amp_x_mm:.3f}mm_ampY{params.amp_y_mm:.3f}mm_"
        f"ampZ{params.amp_z_mm:.3f}mm_ampScale{params.amp_scale_mm:.3f}mm_"
        f"helixMinor{params.helix_minor_radius_mm:.3f}mm_turns{params.helix_turns}"
    )

    param_parts = []
    if abs(params.amp_x_mm) > 1e-9:
        param_parts.append(f"x{fmt_mm(params.amp_x_mm)}")
    if abs(params.amp_y_mm) > 1e-9:
        param_parts.append(f"y{fmt_mm(params.amp_y_mm)}")
    if abs(params.amp_z_mm) > 1e-9:
        param_parts.append(f"z{fmt_mm(params.amp_z_mm)}")
    if abs(params.amp_scale_mm) > 1e-9:
        param_parts.append(f"s{fmt_mm(params.amp_scale_mm)}")
    if abs(params.helix_minor_radius_mm) > 1e-9:
        param_parts.append(f"r{fmt_mm(params.helix_minor_radius_mm)}")
    if mode == 13:
        param_parts.append(f"k{params.helix_turns}")
    param_tag_short = "_".join(param_parts) if param_parts else "params"

    run_stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_base = sanitize_filename(
        f"{shape_name}_s{n_steps}_f{rev_hz:.1f}_l{num_loops}_{param_tag_short}",
        max_len=50,
    )
    run_folder = sanitize_filename(f"{shape_name}_{run_stamp}_{param_tag_short}", max_len=60)
    run_dir = os.path.join(SAVE_DIR, shape_name, run_folder)
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    return shape_name, run_desc, run_base, run_dir, param_tag_short


def write_ideal_log(
    path: str,
    positions: list[Tuple[float, float, float]],
    actual_fps: float,
) -> None:
    dt = 1.0 / float(actual_fps) if actual_fps else 0.0
    with open(path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["time", "target_x", "target_y", "target_z"])
        for i, pos in enumerate(positions):
            writer.writerow([f"{i * dt:.6f}", f"{pos[0]:.6f}", f"{pos[1]:.6f}", f"{pos[2]:.6f}"])


def write_run_meta(path: str, payload: dict) -> None:
    import json

    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def trajectory_stats(
    positions: list[Tuple[float, float, float]],
    sample_hz: float,
    closed_cycle: bool,
) -> dict:
    if not positions:
        raise ValueError("trajectory contains no positions")

    points_mm = [tuple(value * 1e3 for value in point) for point in positions]
    bounds_mm = {
        axis: {
            "min": min(point[index] for point in points_mm),
            "max": max(point[index] for point in points_mm),
        }
        for index, axis in enumerate(("x", "y", "z"))
    }

    pair_count = len(points_mm) if closed_cycle else max(0, len(points_mm) - 1)
    velocities: list[Tuple[float, float, float]] = []
    step_distances_mm: list[float] = []
    for index in range(pair_count):
        next_index = (index + 1) % len(points_mm)
        delta = tuple(points_mm[next_index][axis] - points_mm[index][axis] for axis in range(3))
        step_distances_mm.append(math.sqrt(sum(value * value for value in delta)))
        velocities.append(tuple(value * sample_hz for value in delta))

    accelerations: list[Tuple[float, float, float]] = []
    velocity_pair_count = len(velocities) if closed_cycle else max(0, len(velocities) - 1)
    for index in range(velocity_pair_count):
        next_index = (index + 1) % len(velocities)
        accelerations.append(
            tuple(
                (velocities[next_index][axis] - velocities[index][axis]) * sample_hz
                for axis in range(3)
            )
        )

    speed_mm_s = [math.sqrt(sum(value * value for value in velocity)) for velocity in velocities]
    accel_mm_s2 = [math.sqrt(sum(value * value for value in accel)) for accel in accelerations]
    return {
        "closed_cycle": closed_cycle,
        "bounds_mm": bounds_mm,
        "max_speed_mm_s": max(speed_mm_s, default=0.0),
        "max_accel_mm_s2": max(accel_mm_s2, default=0.0),
        "mean_step_mm": (
            sum(step_distances_mm) / len(step_distances_mm) if step_distances_mm else 0.0
        ),
        "loop_seam_step_mm": step_distances_mm[-1] if closed_cycle and step_distances_mm else None,
    }


def print_trajectory_stats(stats: dict) -> None:
    bounds = stats["bounds_mm"]
    print(
        "[TRAJ] Bounds [mm]: "
        f"X={bounds['x']['min']:.3f}..{bounds['x']['max']:.3f}, "
        f"Y={bounds['y']['min']:.3f}..{bounds['y']['max']:.3f}, "
        f"Z={bounds['z']['min']:.3f}..{bounds['z']['max']:.3f}"
    )
    print(
        "[TRAJ] Discrete kinematics: "
        f"max speed={stats['max_speed_mm_s']:.1f} mm/s, "
        f"max acceleration={stats['max_accel_mm_s2']:.1f} mm/s^2"
    )
    if stats["closed_cycle"]:
        print(
            "[TRAJ] Closed playback seam: "
            f"{stats['loop_seam_step_mm']:.4f} mm "
            f"(mean step {stats['mean_step_mm']:.4f} mm). "
            "The duplicate endpoint is omitted to avoid a one-frame pause."
        )


def save_trajectory_preview(
    path: str,
    positions: list[Tuple[float, float, float]],
    centre: Tuple[float, float, float],
    title: str,
) -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        print(f"[WARN] Trajectory preview is unavailable: {exc}")
        return False

    xyz_mm = [
        tuple((point[axis] - centre[axis]) * 1e3 for axis in range(3))
        for point in positions
    ]
    x = [point[0] for point in xyz_mm]
    y = [point[1] for point in xyz_mm]
    z = [point[2] for point in xyz_mm]

    fig = plt.figure(figsize=(10, 8), constrained_layout=True)
    fig.suptitle(title)
    ax_3d = fig.add_subplot(2, 2, 1, projection="3d")
    ax_xy = fig.add_subplot(2, 2, 2)
    ax_xz = fig.add_subplot(2, 2, 3)
    ax_yz = fig.add_subplot(2, 2, 4)

    ax_3d.plot(x, y, z, color="#16a6a1", linewidth=1.6)
    ax_3d.scatter([x[0]], [y[0]], [z[0]], color="#ef6461", s=28, label="start")
    ax_3d.set_xlabel("X [mm]")
    ax_3d.set_ylabel("Y [mm]")
    ax_3d.set_zlabel("Z [mm]")
    ax_3d.legend(loc="upper right")

    for axis, horizontal, vertical, x_label, y_label, name in (
        (ax_xy, x, y, "X [mm]", "Y [mm]", "XY projection"),
        (ax_xz, x, z, "X [mm]", "Z [mm]", "XZ projection"),
        (ax_yz, y, z, "Y [mm]", "Z [mm]", "YZ projection"),
    ):
        axis.plot(horizontal, vertical, color="#16a6a1", linewidth=1.6)
        axis.scatter([horizontal[0]], [vertical[0]], color="#ef6461", s=24)
        axis.set_xlabel(x_label)
        axis.set_ylabel(y_label)
        axis.set_title(name)
        axis.set_aspect("equal", adjustable="datalim")
        axis.grid(True, alpha=0.25)

    fig.savefig(path, dpi=160)
    plt.close(fig)
    return True


def write_3d_sample_previews(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    sample_parameters = {
        12: TrajectoryParameters(amp_x_mm=2.0, amp_y_mm=2.0, amp_z_mm=1.5),
        13: TrajectoryParameters(
            amp_x_mm=2.5,
            amp_z_mm=0.75,
            helix_minor_radius_mm=0.75,
            helix_turns=3,
        ),
        14: TrajectoryParameters(amp_x_mm=2.5, amp_y_mm=2.5, amp_z_mm=1.5),
        15: TrajectoryParameters(amp_x_mm=2.0, amp_y_mm=2.0, amp_z_mm=2.0),
    }
    for mode, params in sample_parameters.items():
        positions = generate_cycle_positions(
            mode=mode,
            n_steps=400,
            amp_x=params.amp_x_mm * 1e-3,
            amp_y=params.amp_y_mm * 1e-3,
            amp_z=params.amp_z_mm * 1e-3,
            amp_scale=0.0,
            helix_minor_radius=params.helix_minor_radius_mm * 1e-3,
            helix_turns=params.helix_turns,
        )
        shape_name = MODE_SHAPE_NAMES[mode]
        stats = trajectory_stats(positions, sample_hz=800.0, closed_cycle=True)
        print(f"\n[SAMPLE] {shape_name}")
        print_trajectory_stats(stats)
        preview_path = output_dir / f"{shape_name}.png"
        if not save_trajectory_preview(
            str(preview_path),
            positions,
            (0.0, 0.0, 0.0),
            f"{shape_name}: sample parameters",
        ):
            raise RuntimeError("failed to render a 3D trajectory sample")
        print(f"[SAMPLE] Saved: {preview_path.resolve()}")


def main() -> None:
    args = parse_args()
    if args.preview_3d_samples:
        write_3d_sample_previews(args.sample_output_dir)
        return

    Path(SAVE_DIR).mkdir(parents=True, exist_ok=True)

    lev = LevitatorController(ids=(101, 3))
    print("[INFO] Connected to PAT via AcousTools")
    print("[INFO] Event camera is not used by this script.")

    current_static_pos: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    # current_static_pos: Tuple[float, float, float] = (0.020, 0.0, 0.0) #チェック用　PATは右手座標
    current_static_holo: torch.Tensor | None = None

    try:
        current_static_holo = mute_sync_transducer(
            compute_holograms_for_positions([current_static_pos])[0]
        )
        lev.levitate(current_static_holo)
        time.sleep(0.5)
    except Exception as exc:
        print(f"[WARN] levitate failed during initialisation: {exc}")

    try:
        while True:
            mode = prompt_mode()
            if mode is None:
                continue

            params = prompt_amplitudes_mm(mode)
            if params is None:
                continue
            amp_x = params.amp_x_mm * 1e-3
            amp_y = params.amp_y_mm * 1e-3
            amp_z = params.amp_z_mm * 1e-3
            amp_scale = params.amp_scale_mm * 1e-3
            helix_minor_radius = params.helix_minor_radius_mm * 1e-3

            timing = prompt_timing(mode)
            if timing is None:
                continue
            n_steps, rev_hz, num_loops, pat_rate = timing

            shape_name, run_desc, run_base, run_dir, _ = make_run_paths(
                mode, n_steps, rev_hz, num_loops, params
            )

            cycle_positions = generate_cycle_positions(
                mode=mode,
                n_steps=n_steps,
                amp_x=amp_x,
                amp_z=amp_z,
                amp_scale=amp_scale,
                x_center=current_static_pos[0],
                y_center=current_static_pos[1],
                z_center=current_static_pos[2],
                amp_y=amp_y,
                helix_minor_radius=helix_minor_radius,
                helix_turns=params.helix_turns,
            )

            closed_cycle = mode != 8
            trajectory_sample_hz = float(pat_rate.effective_hz or pat_rate.requested_hz)
            stats = trajectory_stats(cycle_positions, trajectory_sample_hz, closed_cycle)
            print_trajectory_stats(stats)
            preview_path = os.path.abspath(
                os.path.join(run_dir, f"{run_base}_trajectory_preview.png")
            )
            if save_trajectory_preview(
                preview_path,
                cycle_positions,
                current_static_pos,
                f"{shape_name}: one cycle",
            ):
                print(f"[INFO] Trajectory preview saved: {preview_path}")

            print("[INFO] Computing holograms for one cycle...")
            cycle_holograms = compute_holograms_for_positions(cycle_positions)

            if mode == 8:
                total_frames = n_steps * num_loops
                if total_frames > len(cycle_positions):
                    static_count = total_frames - len(cycle_positions)
                    cycle_positions = cycle_positions + [cycle_positions[-1]] * static_count
                    cycle_holograms = cycle_holograms + [cycle_holograms[-1]] * static_count
                pat_loops = 1
            else:
                pat_loops = num_loops

            fps_req = pat_rate.requested_hz
            set_fps_return_raw = lev.set_frame_rate(fps_req)
            try:
                set_fps_return = None if set_fps_return_raw is None else int(set_fps_return_raw)
            except Exception:
                set_fps_return = str(set_fps_return_raw)
            actual_fps = float(pat_rate.effective_hz or fps_req)
            print(
                f"[INFO] PAT frame rate set: requested={fps_req} Hz, "
                f"divider={pat_rate.divider}, effective={actual_fps:.6f} Hz"
            )
            if set_fps_return not in (None, 0, fps_req):
                print(f"[INFO] PAT set_frame_rate returned {set_fps_return!r}.")

            print("[PREP] Preparing ctypes buffers for PAT send...")
            t_prep0 = time.perf_counter_ns()
            phases_ct, amps_ct, num_geometries = prepare_message_from_holograms(
                lev, cycle_holograms, permute=True
            )
            prep_s = ns_to_s(time.perf_counter_ns() - t_prep0)
            print(f"[PREP] num_geometries={num_geometries}, prep_time={prep_s:.3f} s")

            start_pos = cycle_positions[0]
            if mode in CLOSED_3D_MODES:
                # The sampled cycle omits the duplicate t=2*pi endpoint.
                # Holding the first point after playback completes the final
                # regular seam step and leaves the particle exactly at start.
                end_pos = cycle_positions[0]
                end_hologram = cycle_holograms[0]
            else:
                end_pos = cycle_positions[-1]
                end_hologram = cycle_holograms[-1]
            current_static_pos = move_static_position(
                lev,
                current_static_pos,
                start_pos,
                "Moving smoothly to the orbit start position",
            )

            input("\n>>> Particle is at the start of the trajectory. Press Enter to start motion. <<<\n")

            expected_duration = (num_geometries * pat_loops) / float(actual_fps)
            log_filename = os.path.abspath(os.path.join(run_dir, f"{run_base}_ideal_log.csv"))
            all_positions: List[Tuple[float, float, float]] = []
            for _ in range(int(pat_loops)):
                all_positions.extend(cycle_positions)
            write_ideal_log(log_filename, all_positions, actual_fps)
            print(f"[INFO] Ideal log saved with {len(all_positions)} rows: {log_filename}")

            meta_path = os.path.abspath(os.path.join(run_dir, f"{run_base}_run_meta.json"))
            write_run_meta(
                meta_path,
                {
                    "script": Path(__file__).name,
                    "run_desc": run_desc,
                    "mode": mode,
                    "shape_name": shape_name,
                    "run_dir": os.path.abspath(run_dir),
                    "ideal_log": log_filename,
                    "steps_per_cycle": n_steps,
                    "rev_hz": rev_hz,
                    "num_loops": num_loops,
                    "pat_loops": pat_loops,
                    "amp_x_mm": params.amp_x_mm,
                    "amp_y_mm": params.amp_y_mm,
                    "amp_z_mm": params.amp_z_mm,
                    "amp_scale_mm": params.amp_scale_mm,
                    "helix_minor_radius_mm": params.helix_minor_radius_mm,
                    "helix_turns": params.helix_turns,
                    "closed_cycle": closed_cycle,
                    "duplicate_endpoint_in_sequence": False,
                    "trajectory_preview": preview_path,
                    "trajectory_stats": stats,
                    "pat_fps_requested": fps_req,
                    "pat_fps_actual": actual_fps,
                    "pat_fps_set_return": set_fps_return,
                    "pat_fps_base_hz": PAT_UPDATE_BASE_HZ,
                    "pat_fps_divider": pat_rate.divider,
                    "pat_fps_alpha": pat_rate.alpha,
                    "pat_fps_error_ms_per_s": pat_rate.error_ms_per_s,
                    "expected_duration_sec": expected_duration,
                    "prep_time_sec": prep_s,
                },
            )
            print(f"[INFO] Run metadata saved: {meta_path}")

            pat_start_pc_ns = time.perf_counter_ns()
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
            except Exception as exc:
                print(f"[ERROR] lev.send_message failed: {exc}")

            send_call_time = ns_to_s(time.perf_counter_ns() - pat_start_pc_ns)
            ratio = send_call_time / expected_duration if expected_duration > 0 else float("nan")
            print(f"[PAT] send_message() call_time={send_call_time:.6f} s | call/expected={ratio:.3f}")

            current_static_pos = end_pos
            current_static_holo = mute_sync_transducer(end_hologram)
            try:
                lev.levitate(current_static_holo)
            except Exception as exc:
                print(f"[WARN] levitate to end position failed: {exc}")

            ans = input("\nReturn to centre? (Y/n): ").strip().lower()
            if ans != "n":
                centre_pos = (0.0, 0.0, 0.0)
                current_static_pos = move_static_position(
                    lev,
                    current_static_pos,
                    centre_pos,
                    "Moving back to centre",
                )
                current_static_holo = mute_sync_transducer(
                    compute_holograms_for_positions([centre_pos])[0]
                )
                try:
                    lev.levitate(current_static_holo)
                except Exception:
                    pass
                print("[INFO] Particle returned to centre.")
            else:
                print("[INFO] Staying at current end position.")

    except KeyboardInterrupt:
        print("\n[INFO] User requested exit (Ctrl+C). Cleaning up...")
    finally:
        try:
            if current_static_holo is not None:
                off_phase = torch.zeros_like(current_static_holo)
                off_phase = add_lev_sig(off_phase)
                phases_ct, amps_ct, _ = prepare_message_from_holograms(lev, [off_phase], permute=True)
                lev.send_message(phases_ct, amps_ct, 0, 1, sleep_ms=0, loop=False, num_loops=1)
        except Exception:
            pass
        print("[INFO] Shutdown complete.")


if __name__ == "__main__":
    main()
