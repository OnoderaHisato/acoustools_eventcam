#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dedicated AcousTools recording workflow for the heart delay A/B experiment.

Workflow:
  1. Generate/prepare a planar, random 3D, or closed 3D PAT trajectory.
  2. Record both event cameras in one verified hardware timestamp domain.
  3. Start PAT motion only after the shared camera interval is active.
  4. Detect PAT start from the selected camera's LED events.
  5. Commit the recording manifest with postprocessing status ``pending``.

No particle tracking, triangulation, or ideal comparison runs in this module.


B1 variant: adds --inverse2nd-feedforward (design C, per-axis second-order
inverse with periodic wrap for closed-cycle trajectories). Existing machine
files are not modified; this file is deployed alongside them.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Tuple

B1_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = B1_DIR.parent
for module_dir in (PROJECT_ROOT, B1_DIR):
    module_dir_text = str(module_dir)
    if module_dir_text not in sys.path:
        sys.path.insert(0, module_dir_text)

import torch

from heart_delay_feedforward import apply_delay_feedforward
from inverse2nd_feedforward import DEFAULT_PHYS_AXES, apply_inverse2nd_feedforward

from acoustools.Levitator import LevitatorController
from acoustools.Utilities import add_lev_sig

from acoustools_eventcam_sync import (
    MODE_SHAPE_NAMES,
    PAT_UPDATE_BASE_HZ,
    compute_holograms_for_positions,
    generate_cycle_positions,
    move_static_position,
    mute_sync_transducer,
    ns_to_s,
    prepare_message_from_holograms,
    sanitize_filename,
)
from acoustools_multitraj_no_eventcam import (
    TrajectoryParameters,
    print_trajectory_stats,
    prompt_amplitudes_mm,
    prompt_timing,
    save_trajectory_preview,
    trajectory_stats,
)
from acoustools_random3d_no_eventcam import (
    generate_random_3d_positions,
    prompt_closed_parameters,
    prompt_closed_timing,
    prompt_random_parameters,
)
import stereo_eventcam_record_sync as stereo_record
from stereo_detect_pat_start_led import detect_led_sync_npz, parse_roi as parse_led_roi
from stereo_acoustools_heart_delay_common import (
    DEFAULT_CALIBRATION,
    atomic_write_json,
    processing_config_from_args,
    validate_calibration,
)


def build_recording_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record one AcousTools 3D trajectory with synchronized stereo event cameras.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--stereo-calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--camera-to-pat-transform", type=Path, default=None)
    parser.add_argument("--left-serial", default="00000508")
    parser.add_argument("--right-serial", default="00000509")
    parser.add_argument("--hw-sync", choices=["left-master", "right-master"], default="left-master")
    parser.add_argument("--output-dir", type=Path, default=Path("stereo_acoustools_3d_records"))
    parser.add_argument("--no-preview", action="store_true")
    parser.add_argument("--preview-scale", type=float, default=0.5)
    parser.add_argument("--preview-delta-t-us", type=int, default=5000)
    parser.add_argument("--camera-delta-t-us", type=int, default=1000)
    parser.add_argument("--camera-start-delay-sec", type=float, default=1.0)
    parser.add_argument("--capture-marker-timeout-sec", type=float, default=20.0)
    parser.add_argument("--post-roll-sec", type=float, default=0.5)
    parser.add_argument(
        "--capture-tail-margin-sec",
        type=float,
        default=2.0,
        help=(
            "Extra camera time after the nominal PAT duration. The 2 s default "
            "covers observed PAT message-transfer startup latency."
        ),
    )
    parser.add_argument("--npz-compression", choices=["none", "compressed"], default="none")
    parser.add_argument("--window-us", type=int, default=200)
    parser.add_argument("--hop-us", type=int, default=100)
    parser.add_argument("--dt-us", type=float, default=100.0)
    parser.add_argument("--max-interp-gap-sec", type=float, default=0.005)
    parser.add_argument("--max-step-px", type=float, default=15.0)
    parser.add_argument("--roi", default="0,0,1280,720")
    parser.add_argument("--tracking-method", default="event_weighted")
    parser.add_argument("--threshold-count", type=int, default=1)
    parser.add_argument("--min-events", type=int, default=20)
    parser.add_argument("--min-area", type=int, default=5)
    parser.add_argument("--min-mass", type=int, default=30)
    parser.add_argument("--polarity", choices=["all", "on", "off"], default="all")
    parser.add_argument("--max-time-gap-sec", type=float, default=0.001)
    parser.add_argument("--max-reprojection-error-px", type=float, default=3.0)
    parser.add_argument(
        "--pat-start-led-side",
        choices=["off", "left", "right"],
        default="left",
        help="Detect PAT start from an LED visible in only this synchronized camera.",
    )
    parser.add_argument("--pat-start-led-roi", default="600,0,1280,180", help="LED ROI on the selected side only.")
    parser.add_argument("--pat-start-led-bin-us", type=int, default=100)
    parser.add_argument("--pat-start-led-threshold", type=int, default=0, help="0 selects an event-count threshold automatically.")
    parser.add_argument("--pat-start-led-search-sec", type=float, default=0.2, help="Search half-width around the PC timing estimate.")
    parser.add_argument(
        "--refine-led-time",
        action="store_true",
        help="Allow trajectory-fit time refinement after LED detection. By default the detected LED onset is fixed.",
    )
    parser.add_argument(
        "--keep-failed-captures",
        action="store_true",
        help="Keep temporary stereo RAW/NPZ and LED diagnostics when a capture or LED detection fails.",
    )
    parser.add_argument("--render-overlay", action="store_true")
    parser.add_argument("--auto-time-search-sec", type=float, default=0.02)
    parser.add_argument("--auto-time-step-sec", type=float, default=0.0001)
    parser.add_argument(
        "--delay-feedforward",
        action="store_true",
        help="Apply the fixed first-order inverse u=r+tau*dr/dt before hologram generation.",
    )
    parser.add_argument(
        "--delay-tau-ms",
        type=float,
        default=2.05,
        help="Frozen common delay time constant used only with --delay-feedforward.",
    )
    parser.add_argument(
        "--delay-max-offset-mm",
        type=float,
        default=1.0,
        help="Vector-norm limit on u-r. Saturation statistics are saved in the manifest.",
    )
    parser.add_argument(
        "--delay-limit-strategy",
        choices=["global_scale", "pointwise_clip"],
        default="global_scale",
        help=(
            "Limit u-r by smooth whole-trajectory scaling or pointwise clipping. "
            "Global scaling avoids clipping-induced acceleration discontinuities."
        ),
    )
    parser.add_argument(
        "--inverse2nd-feedforward",
        action="store_true",
        help=(
            "Apply the per-axis second-order inverse (design C) "
            "u = r(t+tau) + (r'' + gamma r')(t+tau)/w0^2 before hologram generation. "
            "Mutually exclusive with --delay-feedforward."
        ),
    )
    parser.add_argument(
        "--ff-f0-hz",
        default=",".join(f"{p[0]:g}" for p in DEFAULT_PHYS_AXES),
        help="Per-axis trap resonance f0 [Hz] as x,y,z for --inverse2nd-feedforward.",
    )
    parser.add_argument(
        "--ff-gamma",
        default=",".join(f"{p[1]:g}" for p in DEFAULT_PHYS_AXES),
        help="Per-axis damping gamma [1/s] as x,y,z for --inverse2nd-feedforward.",
    )
    parser.add_argument(
        "--ff-tau-ms",
        default=",".join(f"{p[2] * 1e3:g}" for p in DEFAULT_PHYS_AXES),
        help="Per-axis command-to-trap delay [ms] as x,y,z for --inverse2nd-feedforward.",
    )
    parser.add_argument(
        "--ff-max-offset-mm",
        type=float,
        default=1.0,
        help="Vector-norm limit on u-r for --inverse2nd-feedforward (lambda/8 = 1.07 mm).",
    )
    parser.add_argument(
        "--ff-limit-strategy",
        choices=["pointwise_clip", "global_scale"],
        default="pointwise_clip",
        help=(
            "Limit strategy for --inverse2nd-feedforward. pointwise_clip matches the "
            "offline simulation that produced the improvement prediction."
        ),
    )
    parser.add_argument(
        "--ff-sg-window-ms",
        type=float,
        default=5.0,
        help="Savitzky-Golay window for r', r'' in --inverse2nd-feedforward.",
    )
    return parser


def parse_ff_axis_triple(text: str, name: str) -> tuple[float, float, float]:
    parts = [part.strip() for part in str(text).split(",")]
    if len(parts) != 3:
        raise SystemExit(f"{name} must be three comma-separated values (x,y,z): {text!r}")
    try:
        values = tuple(float(part) for part in parts)
    except ValueError:
        raise SystemExit(f"{name} must be numeric (x,y,z): {text!r}")
    return values  # type: ignore[return-value]


def ff_phys_axes_from_args(args: argparse.Namespace) -> tuple[tuple[float, float, float], ...]:
    f0 = parse_ff_axis_triple(args.ff_f0_hz, "--ff-f0-hz")
    gamma = parse_ff_axis_triple(args.ff_gamma, "--ff-gamma")
    tau_ms = parse_ff_axis_triple(args.ff_tau_ms, "--ff-tau-ms")
    return tuple((f0[a], gamma[a], tau_ms[a] * 1.0e-3) for a in range(3))


def parse_recording_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_recording_parser().parse_args(argv)


@dataclass(frozen=True)
class PreparedTrajectory:
    """Fully resolved trajectory passed to the hardware recording workflow."""

    shape_name: str
    positions: list[Tuple[float, float, float]]
    n_steps: int
    frequency_hz: float
    loops: int
    rate: Any
    parameters: dict[str, Any]
    closed_cycle: bool
    stats: dict[str, Any]
    run_label: str = ""
    source_artifacts: dict[str, Path] = field(default_factory=dict)
    trajectory_source: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecordingHardwareSession:
    """One persistent AcousTools/OpenMPD connection and its held particle state."""

    lev: LevitatorController
    current_pos: Tuple[float, float, float]
    current_hologram: torch.Tensor | None
    is_shutdown: bool = False


def write_control_logs(
    evaluation_path: Path,
    command_path: Path,
    command_positions: list[Tuple[float, float, float]],
    reference_positions: list[Tuple[float, float, float]],
    sample_hz: float,
) -> None:
    """Write an evaluation reference and the exact PAT command separately."""
    if len(command_positions) != len(reference_positions):
        raise ValueError("Command and reference logs must contain the same number of samples.")
    dt_sec = 1.0 / float(sample_hz)
    with evaluation_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "target_x", "target_y", "target_z"])
        for index, reference in enumerate(reference_positions):
            writer.writerow(
                [f"{index * dt_sec:.9f}", *(f"{value:.9f}" for value in reference)]
            )
    with command_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "time",
                "target_x",
                "target_y",
                "target_z",
                "reference_x",
                "reference_y",
                "reference_z",
            ]
        )
        for index, (command, reference) in enumerate(
            zip(command_positions, reference_positions)
        ):
            writer.writerow(
                [
                    f"{index * dt_sec:.9f}",
                    *(f"{value:.9f}" for value in command),
                    *(f"{value:.9f}" for value in reference),
                ]
            )


def open_recording_hardware_session() -> RecordingHardwareSession:
    """Connect once and hold the particle at the PAT coordinate-system origin."""
    lev = LevitatorController(ids=(101, 3))
    current_pos: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    current_hologram = mute_sync_transducer(
        compute_holograms_for_positions([current_pos])[0]
    )
    lev.levitate(current_hologram)
    time.sleep(0.5)
    print("[PAT] Persistent AcousTools/OpenMPD session opened; particle held at centre.")
    return RecordingHardwareSession(lev, current_pos, current_hologram)


def shutdown_recording_hardware_session(session: RecordingHardwareSession) -> None:
    """Turn PAT output off exactly once at the end of an owning workflow."""
    if session.is_shutdown:
        return
    try:
        if session.current_hologram is not None:
            off_phase = add_lev_sig(torch.zeros_like(session.current_hologram))
            phases, amplitudes, _ = prepare_message_from_holograms(
                session.lev, [off_phase], permute=True
            )
            session.lev.send_message(
                phases, amplitudes, 0, 1, sleep_ms=0, loop=False, num_loops=1
            )
    except Exception as exc:
        print(f"[PAT][WARN] Final PAT shutdown command failed: {exc}")
    finally:
        session.is_shutdown = True
    print("[PAT] AcousTools/OpenMPD session shutdown complete.")


def choose_trajectory(
    centre: Tuple[float, float, float],
) -> PreparedTrajectory:
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
    print("  --- Existing stereo 3D trajectories ---")
    print(" 11: Long random 3D stroke (open, XYZ independent)")
    print(" 12: 3D figure-eight")
    print(" 13: Closed toroidal helix")
    print(" 14: Trefoil knot")
    print(" 15: 3D Lissajous")
    try:
        menu_mode = int(input("Select mode (1-15): ").strip())
    except (ValueError, EOFError):
        raise SystemExit("Mode must be a number from 1 to 15.")
    if menu_mode not in range(1, 16):
        raise SystemExit("Mode must be from 1 to 15.")

    if menu_mode == 11:
        params = prompt_random_parameters()
        if params is None or params.f_max_hz < params.f_min_hz:
            raise SystemExit("Invalid random 3D parameters.")
        from acoustools_eventcam_sync import compute_pat_frame_rate_info

        rate = compute_pat_frame_rate_info(1, params.sample_hz)
        if not rate.is_supported:
            raise SystemExit(f"PAT sample rate {params.sample_hz:g} Hz is not an exact 40 kHz divider.")
        positions, generated_stats = generate_random_3d_positions(params, centre)
        return PreparedTrajectory(
            shape_name="long_random_3d",
            positions=positions,
            n_steps=len(positions),
            frequency_hz=params.sample_hz / len(positions),
            loops=1,
            rate=rate,
            parameters=asdict(params),
            closed_cycle=False,
            stats=generated_stats,
        )

    if menu_mode <= 10:
        generator_mode = menu_mode
        params = prompt_amplitudes_mm(generator_mode)
        timing = prompt_timing(generator_mode) if params is not None else None
        invalid_message = "Invalid planar trajectory parameters."
    else:
        generator_mode = menu_mode
        params = prompt_closed_parameters(generator_mode)
        timing = prompt_closed_timing() if params is not None else None
        invalid_message = "Invalid closed 3D trajectory parameters."
    if params is None or timing is None:
        raise SystemExit(invalid_message)
    return prepare_parametric_trajectory(generator_mode, centre, params, timing)


def prepare_parametric_trajectory(
    generator_mode: int,
    centre: Tuple[float, float, float],
    params: TrajectoryParameters,
    timing: tuple[int, float, int, Any],
) -> PreparedTrajectory:
    """Build one legacy planar or closed-3D trajectory for stereo capture."""
    if generator_mode not in {*range(1, 11), *range(12, 16)}:
        raise ValueError(f"Unsupported interactive trajectory mode: {generator_mode}")
    n_steps, frequency, loops, rate = timing
    positions = generate_cycle_positions(
        mode=generator_mode,
        n_steps=n_steps,
        amp_x=params.amp_x_mm * 1e-3,
        amp_y=params.amp_y_mm * 1e-3,
        amp_z=params.amp_z_mm * 1e-3,
        amp_scale=params.amp_scale_mm * 1e-3,
        x_center=centre[0],
        y_center=centre[1],
        z_center=centre[2],
        helix_minor_radius=params.helix_minor_radius_mm * 1e-3,
        helix_turns=params.helix_turns,
    )

    requested_steps = n_steps
    requested_loops = loops
    closed_cycle = generator_mode != 8
    if generator_mode == 8:
        # Mode 8 is one open pass. Preserve the requested total duration by
        # holding its exact endpoint instead of discontinuously looping back
        # to the start for every requested repeat.
        total_frames = requested_steps * requested_loops
        if total_frames > len(positions):
            positions = positions + [positions[-1]] * (total_frames - len(positions))
        loops = 1
        n_steps = len(positions)

    sample_hz = float(rate.effective_hz or rate.requested_hz)
    stats = trajectory_stats(positions, sample_hz, closed_cycle)
    parameters = asdict(params)
    parameters.update(
        {
            "generator_mode": generator_mode,
            "requested_steps_per_cycle": requested_steps,
            "requested_loops": requested_loops,
        }
    )
    return PreparedTrajectory(
        shape_name=MODE_SHAPE_NAMES[generator_mode],
        positions=positions,
        n_steps=n_steps,
        frequency_hz=frequency,
        loops=loops,
        rate=rate,
        parameters=parameters,
        closed_cycle=closed_cycle,
        stats=stats,
    )


def create_run_dir(root: Path, shape_name: str, run_label: str = "") -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    root.mkdir(parents=True, exist_ok=True)
    label_part = f"_{run_label}" if str(run_label).strip() else ""
    base = sanitize_filename(f"{shape_name}{label_part}_{stamp}", max_len=100)
    candidate = root / base
    for index in range(1, 1000):
        path = candidate if index == 1 else root / f"{base}_{index:03d}"
        if not path.exists():
            path.mkdir(parents=True)
            return path.resolve()
    raise SystemExit("Could not create a unique pipeline run directory.")


def wait_for_capture_marker(process: subprocess.Popen[Any], path: Path, timeout_sec: float) -> dict[str, Any]:
    deadline = time.monotonic() + float(timeout_sec)
    while time.monotonic() < deadline:
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(0.002)
                continue
            if payload.get("hardware_synchronized") and int(payload.get("pc_start_perf_ns", 0)) > 0:
                return payload
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"Stereo recorder exited with code {return_code} before capture became active.")
        time.sleep(0.002)
    raise TimeoutError(f"Timed out after {timeout_sec:g} s waiting for synchronized camera capture start.")


def compute_led_search_half_window_sec(
    base_half_window_sec: float,
    pat_send_call_duration_sec: float,
    ideal_motion_duration_sec: float,
) -> float:
    """Include observed PAT message-transfer overhead in the LED search window."""
    return float(base_half_window_sec) + max(
        0.0,
        float(pat_send_call_duration_sec) - float(ideal_motion_duration_sec),
    )


def remove_capture_attempt(attempt_dir: Path, run_dir: Path) -> None:
    """Permanently remove one pipeline-owned temporary capture attempt."""
    target = attempt_dir.resolve()
    owner = run_dir.resolve()
    if target.parent != owner or not target.name.startswith("_capture_attempt_"):
        raise RuntimeError(f"Refusing to remove non-attempt directory: {target}")
    if target.exists():
        shutil.rmtree(target)


def remove_failed_run(run_dir: Path, output_root: Path) -> None:
    """Permanently remove a newly-created run after the operator declines retry."""
    target = run_dir.resolve()
    owner = output_root.resolve()
    if target.parent != owner:
        raise RuntimeError(f"Refusing to remove run outside configured output directory: {target}")
    if target.exists():
        shutil.rmtree(target)


def promote_capture_attempt(attempt_dir: Path, run_dir: Path) -> None:
    """Move only a successful attempt into the stable pipeline layout."""
    attempt = attempt_dir.resolve()
    owner = run_dir.resolve()
    if attempt.parent != owner or not attempt.name.startswith("_capture_attempt_"):
        raise RuntimeError(f"Refusing to promote non-attempt directory: {attempt}")
    for name in ("stereo_recording", "capture_start_marker.json", "pat_start_led"):
        source = attempt / name
        if not source.exists():
            continue
        destination = owner / name
        if destination.exists():
            raise RuntimeError(f"Successful-capture destination already exists: {destination}")
        shutil.move(str(source), str(destination))
    attempt.rmdir()


def remap_attempt_result_paths(
    result: dict[str, Any] | None,
    attempt_dir: Path,
    run_dir: Path,
) -> dict[str, Any] | None:
    """Update detector report paths after a successful attempt is promoted."""
    if result is None:
        return None
    old_root = attempt_dir.resolve()
    new_root = run_dir.resolve()
    for key in ("input_npz", "report_csv", "report_plot", "report_json"):
        value = str(result.get(key, "")).strip()
        if not value:
            continue
        try:
            relative = Path(value).resolve().relative_to(old_root)
        except ValueError:
            continue
        result[key] = str(new_root / relative)
    return result


def copy_prepared_trajectory_artifacts(
    run_dir: Path, artifacts: Mapping[str, Path]
) -> dict[str, dict[str, Any]]:
    """Copy exact command inputs into a new run and record their hashes."""
    copied: dict[str, dict[str, Any]] = {}
    for destination_name, source_value in artifacts.items():
        destination = Path(destination_name)
        if destination.name != destination_name or destination.is_absolute():
            raise ValueError(f"Prepared trajectory artifact name must be a filename: {destination_name}")
        source = Path(source_value).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Prepared trajectory artifact not found: {source}")
        target = (run_dir / destination_name).resolve()
        target.relative_to(run_dir.resolve())
        if target.exists():
            raise FileExistsError(f"Prepared trajectory artifact target already exists: {target}")
        shutil.copy2(source, target)
        copied[destination_name] = {
            "path": str(target),
            "source_path": str(source),
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "size_bytes": target.stat().st_size,
        }
    return copied


def run_recording(
    args: argparse.Namespace,
    *,
    postprocess_will_follow: bool = False,
    prepared_trajectory: PreparedTrajectory | None = None,
    prompt_before_capture: bool = True,
    return_to_centre: bool | None = None,
    automation_metadata: Mapping[str, Any] | None = None,
    hardware_session: RecordingHardwareSession | None = None,
) -> tuple[int, Path | None]:
    if min(args.camera_delta_t_us, args.window_us, args.hop_us) <= 0 or args.dt_us <= 0:
        raise SystemExit("Camera/tracking time windows must be positive.")
    if min(args.capture_marker_timeout_sec, args.post_roll_sec, args.capture_tail_margin_sec) < 0:
        raise SystemExit("Timeout/margin values must be non-negative.")
    output_root = args.output_dir.resolve()
    calibration = validate_calibration(args.stereo_calibration, args.left_serial, args.right_serial)
    if args.camera_to_pat_transform is not None and not args.camera_to_pat_transform.resolve().exists():
        raise SystemExit(f"Camera-to-PAT transform not found: {args.camera_to_pat_transform.resolve()}")

    owns_hardware_session = hardware_session is None
    session = hardware_session or open_recording_hardware_session()
    if session.is_shutdown:
        raise RuntimeError("Cannot record with an AcousTools/OpenMPD session that is already shut down.")
    lev = session.lev
    current_pos = session.current_pos
    current_hologram = session.current_hologram
    recorder_process: subprocess.Popen[Any] | None = None
    active_attempt_dir: Path | None = None
    run_dir: Path | None = None
    successful_capture_committed = False
    prepared_artifacts_manifest: dict[str, dict[str, Any]] = {}
    try:
        if not args.no_preview:
            accepted = stereo_record.run_preview(
                left_serial=str(args.left_serial), right_serial=str(args.right_serial),
                sensor_width=1280, sensor_height=720,
                delta_t_us=int(args.preview_delta_t_us), display_scale=float(args.preview_scale),
                point_size=1, reopen_wait_sec=1.0,
                instruction_text="Enter: accept particle visibility    R: refresh    Q/Esc: abort",
            )
            if not accepted:
                print("[PIPELINE] Preview aborted.")
                return 1, None

        trajectory = prepared_trajectory or choose_trajectory(current_pos)
        shape_name = trajectory.shape_name
        reference_positions = list(trajectory.positions)
        positions = list(reference_positions)
        n_steps = trajectory.n_steps
        frequency = trajectory.frequency_hz
        loops = trajectory.loops
        rate = trajectory.rate
        parameters = dict(trajectory.parameters)
        closed_cycle = trajectory.closed_cycle
        reference_stats = dict(trajectory.stats)
        actual_fps = float(rate.effective_hz or rate.requested_hz)
        feedforward_stats: dict[str, Any] = {
            "enabled": False,
            "tau_sec": 0.0,
            "max_offset_mm": 0.0,
            "limit_strategy": "none",
            "raw_max_offset_mm": 0.0,
            "global_scale_applied": 1.0,
            "effective_tau_sec": 0.0,
            "observed_max_offset_mm": 0.0,
            "rms_offset_mm": 0.0,
            "clipped_points": 0,
            "clipped_fraction": 0.0,
            "limited_points": 0,
            "limited_fraction": 0.0,
        }
        run_label = trajectory.run_label
        if bool(args.delay_feedforward) and bool(getattr(args, "inverse2nd_feedforward", False)):
            raise SystemExit(
                "--delay-feedforward and --inverse2nd-feedforward are mutually exclusive."
            )
        if bool(getattr(args, "inverse2nd_feedforward", False)):
            if float(args.ff_max_offset_mm) <= 0.0:
                raise SystemExit("--ff-max-offset-mm must be positive.")
            positions, raw_stats = apply_inverse2nd_feedforward(
                reference_positions,
                sample_hz=actual_fps,
                phys_axes=ff_phys_axes_from_args(args),
                max_offset_mm=float(args.ff_max_offset_mm),
                limit_strategy=str(args.ff_limit_strategy),
                sg_window_ms=float(args.ff_sg_window_ms),
                periodic=bool(closed_cycle),
            )
            feedforward_stats = {"enabled": True, **raw_stats}
            run_label = "_".join(part for part in (run_label, "inv2ff") if part)
            print(
                "[CONTROL] Inverse-2nd feedforward (design C): "
                f"f0={args.ff_f0_hz} Hz, gamma={args.ff_gamma} 1/s, tau={args.ff_tau_ms} ms, "
                f"periodic={bool(closed_cycle)}, strategy={raw_stats['limit_strategy']}, "
                f"max |u-r|={raw_stats['observed_max_offset_mm']:.4f} mm "
                f"(raw {raw_stats['raw_max_offset_mm']:.4f} mm), "
                f"rms |u-r|={raw_stats['rms_offset_mm']:.4f} mm, "
                f"limited={int(raw_stats['limited_points'])}/{len(positions)} "
                f"({100.0 * float(raw_stats['limited_fraction']):.2f}%)"
            )
        elif bool(args.delay_feedforward):
            if float(args.delay_tau_ms) < 0.0:
                raise SystemExit("--delay-tau-ms cannot be negative.")
            if float(args.delay_max_offset_mm) <= 0.0:
                raise SystemExit("--delay-max-offset-mm must be positive.")
            positions, raw_stats = apply_delay_feedforward(
                reference_positions,
                sample_hz=actual_fps,
                tau_sec=float(args.delay_tau_ms) * 1.0e-3,
                max_offset_mm=float(args.delay_max_offset_mm),
                periodic=bool(closed_cycle),
                limit_strategy=str(args.delay_limit_strategy),
            )
            feedforward_stats = {"enabled": True, **raw_stats}
            suffix = f"delayff_tau{float(args.delay_tau_ms):.3f}ms"
            run_label = "_".join(part for part in (run_label, suffix) if part)
            print(
                "[CONTROL] Delay feedforward: "
                f"tau={float(args.delay_tau_ms):.3f} ms, "
                f"strategy={raw_stats['limit_strategy']}, "
                f"effective_tau={1000.0 * float(raw_stats['effective_tau_sec']):.3f} ms, "
                f"max |u-r|={raw_stats['observed_max_offset_mm']:.6f} mm, "
                f"limited={int(raw_stats['limited_points'])}/{len(positions)} "
                f"({100.0 * float(raw_stats['limited_fraction']):.2f}%)"
            )
        stats = trajectory_stats(positions, actual_fps, closed_cycle)
        if bool(getattr(args, "inverse2nd_feedforward", False)):
            parameters["control_mode"] = "inverse2nd_feedforward"
        elif bool(args.delay_feedforward):
            parameters["control_mode"] = "delay_feedforward"
        else:
            parameters["control_mode"] = "baseline"
        print_trajectory_stats(stats)
        run_dir = create_run_dir(output_root, shape_name, run_label)
        prepared_artifacts_manifest = copy_prepared_trajectory_artifacts(
            run_dir, trajectory.source_artifacts
        )
        run_base = sanitize_filename(f"{shape_name}_s{n_steps}_f{rate.requested_hz:g}_l{loops}", max_len=70)
        preview_path = run_dir / f"{run_base}_command_trajectory_preview.png"
        reference_preview_path = run_dir / f"{run_base}_reference_trajectory_preview.png"
        save_trajectory_preview(
            str(preview_path), positions, current_pos, f"{shape_name}: PAT command u(t)"
        )
        save_trajectory_preview(
            str(reference_preview_path),
            reference_positions,
            current_pos,
            f"{shape_name}: tracking reference r(t)",
        )

        print(f"[PREP] Computing {len(positions)} holograms before cameras are opened...")
        holograms = compute_holograms_for_positions(positions)
        lev.set_frame_rate(rate.requested_hz)
        phases, amplitudes, geometry_count = prepare_message_from_holograms(lev, holograms, permute=True)
        expected_duration = geometry_count * loops / actual_fps
        all_positions = positions * loops
        all_reference_positions = reference_positions * loops
        ideal_log = (run_dir / f"{run_base}_ideal_log.csv").resolve()
        command_log = (run_dir / f"{run_base}_command_log.csv").resolve()
        write_control_logs(
            ideal_log,
            command_log,
            all_positions,
            all_reference_positions,
            actual_fps,
        )

        start_pos = positions[0]
        end_pos = positions[0] if closed_cycle else positions[-1]
        end_hologram = holograms[0] if closed_cycle else holograms[-1]
        current_pos = move_static_position(lev, current_pos, start_pos, "Moving smoothly to trajectory start")
        current_hologram = mute_sync_transducer(holograms[0])
        lev.levitate(current_hologram)
        if prompt_before_capture:
            input("\n>>> Particle is at the start. Press Enter to arm stereo capture and record. <<<\n")

        capture_duration = expected_duration + float(args.post_roll_sec) + float(args.capture_tail_margin_sec)
        led_roi = (
            parse_led_roi(str(args.pat_start_led_roi))
            if args.pat_start_led_side != "off"
            else None
        )
        attempt_number = 0
        led_result: dict[str, Any] | None = None
        while True:
            attempt_number += 1
            attempt_dir = (run_dir / f"_capture_attempt_{attempt_number:02d}").resolve()
            attempt_dir.mkdir(parents=False, exist_ok=False)
            active_attempt_dir = attempt_dir
            marker_path = (attempt_dir / "capture_start_marker.json").resolve()
            attempt_camera_run = (attempt_dir / "stereo_recording").resolve()
            recorder_command = [
                sys.executable, str(Path(stereo_record.__file__).resolve()),
                "--run-dir", str(attempt_camera_run), "--left-serial", str(args.left_serial),
                "--right-serial", str(args.right_serial), "--duration-sec", f"{capture_duration:.9f}",
                "--delta-t-us", str(args.camera_delta_t_us),
                "--start-delay-sec", str(args.camera_start_delay_sec),
                "--hw-sync", str(args.hw_sync), "--npz-compression", str(args.npz_compression),
                "--stereo-calibration", str(calibration), "--capture-start-marker", str(marker_path),
                "--no-preview", "--note", f"AcousTools 3D pipeline: {shape_name}; attempt={attempt_number}",
            ]
            print(f"[CAPTURE] Attempt {attempt_number}: " + subprocess.list2cmdline(recorder_command), flush=True)
            recorder_process = subprocess.Popen(recorder_command)
            marker = wait_for_capture_marker(
                recorder_process, marker_path, float(args.capture_marker_timeout_sec)
            )
            marker_seen_perf_ns = time.perf_counter_ns()
            print(
                "[CAPTURE] Shared hardware-synchronized interval is active; "
                f"marker latency={(marker_seen_perf_ns - int(marker['pc_start_perf_ns'])) * 1e-6:.3f} ms"
            )

            pat_start_perf_ns = time.perf_counter_ns()
            pat_start_wall_ns = time.time_ns()
            send_error = ""
            try:
                lev.send_message(
                    phases, amplitudes, 0, int(geometry_count), sleep_ms=0, loop=True, num_loops=int(loops)
                )
            except Exception as exc:
                send_error = repr(exc)
            pat_send_end_perf_ns = time.perf_counter_ns()
            call_duration = ns_to_s(pat_send_end_perf_ns - pat_start_perf_ns)
            current_pos = end_pos
            current_hologram = mute_sync_transducer(end_hologram)
            lev.levitate(current_hologram)
            marker_camera_elapsed_sec = (
                int(marker.get("camera_ts_at_marker_us", marker["capture_start_ts_us"]))
                - int(marker["capture_start_ts_us"])
            ) * 1e-6
            ideal_start_in_recording_sec = marker_camera_elapsed_sec + (
                pat_start_perf_ns - int(marker["pc_start_perf_ns"])
            ) * 1e-9
            timing_payload = {
                "schema_version": 1,
                "capture_attempt_number": attempt_number,
                "capture_marker": marker,
                "capture_marker_seen_perf_ns": marker_seen_perf_ns,
                "pat_start_perf_ns": pat_start_perf_ns,
                "pat_start_wall_ns": pat_start_wall_ns,
                "pat_send_end_perf_ns": pat_send_end_perf_ns,
                "pat_send_call_duration_sec": call_duration,
                "pat_send_overhead_sec": max(0.0, call_duration - expected_duration),
                "expected_motion_duration_sec": expected_duration,
                "camera_elapsed_at_marker_sec": marker_camera_elapsed_sec,
                "ideal_start_in_recording_sec": ideal_start_in_recording_sec,
                "time_definition": "ideal_t = recording_t - ideal_start_in_recording_sec",
                "pat_send_error": send_error,
            }
            print(
                f"[PAT] send call={call_duration:.6f} s, expected={expected_duration:.6f} s, "
                f"ideal starts at recording t={ideal_start_in_recording_sec:.9f} s"
            )
            if recorder_process.wait(timeout=max(30.0, capture_duration + 20.0)) != 0:
                raise RuntimeError(f"Stereo recording failed with code {recorder_process.returncode}.")
            recorder_process = None
            if send_error:
                raise RuntimeError(f"PAT send failed: {send_error}")

            led_result = None
            led_failure: RuntimeError | None = None
            ideal_start_source = "camera-marker plus PC monotonic PAT-send timing"
            if args.pat_start_led_side != "off":
                led_side = str(args.pat_start_led_side)
                led_npz = attempt_camera_run / led_side / f"{led_side}_events.npz"
                led_search_half_window_sec = compute_led_search_half_window_sec(
                    float(args.pat_start_led_search_sec), call_duration, expected_duration
                )
                timing_payload["pat_start_led_search_half_window_sec_used"] = (
                    led_search_half_window_sec
                )
                print(
                    "[SYNC][LED] Search half-window: "
                    f"{led_search_half_window_sec:.6f} s "
                    f"(base={float(args.pat_start_led_search_sec):.6f} s, "
                    f"PAT send overhead={max(0.0, call_duration - expected_duration):.6f} s)"
                )
                try:
                    led_result = detect_led_sync_npz(
                        led_npz,
                        led_roi,
                        bin_us=int(args.pat_start_led_bin_us),
                        threshold=int(args.pat_start_led_threshold),
                        output_dir=attempt_dir / "pat_start_led",
                        expected_t_recording_sec=float(ideal_start_in_recording_sec),
                        search_half_window_sec=led_search_half_window_sec,
                    )
                    if not bool(led_result.get("hardware_synchronized")):
                        raise RuntimeError(
                            "The selected LED camera does not report hardware-synchronized timestamps."
                        )
                except RuntimeError as exc:
                    led_failure = exc

            if led_failure is not None:
                print(f"\n[SYNC][LED] LED detection failed on attempt {attempt_number}: {led_failure}")
                if args.keep_failed_captures:
                    print(f"[CAPTURE] Failed capture retained for debugging: {attempt_dir}")
                    active_attempt_dir = None
                else:
                    remove_capture_attempt(attempt_dir, run_dir)
                    active_attempt_dir = None
                    print("[CAPTURE] Failed stereo RAW/NPZ and LED diagnostics were permanently deleted.")
                try:
                    retry_answer = input(
                        "\nLEDを確認・調整して、同じ軌道を再計測しますか？ (Y/n): "
                    ).strip().lower()
                except EOFError:
                    retry_answer = "n"
                if retry_answer not in {"n", "no", "いいえ"}:
                    current_pos = move_static_position(
                        lev, current_pos, start_pos, "Returning smoothly to trajectory start for retry"
                    )
                    current_hologram = mute_sync_transducer(holograms[0])
                    lev.levitate(current_hologram)
                    print("[CAPTURE] Ready. Re-arming both cameras with the prepared holograms.")
                    continue
                if not args.keep_failed_captures:
                    failed_run = run_dir
                    remove_failed_run(failed_run, output_root)
                    run_dir = None
                    print(
                        f"[PIPELINE] Retry declined; failed run was permanently deleted: {failed_run}"
                    )
                else:
                    print(f"[PIPELINE] Retry declined; debug artifacts remain in: {run_dir}")
                return 2, None

            if led_result is not None:
                ideal_start_in_recording_sec = float(led_result["sync_t_recording_sec"])
                ideal_start_source = f"PAT-start LED onset detected in {args.pat_start_led_side} camera only"
                print(
                    f"[SYNC][LED] PAT start detected from {str(args.pat_start_led_side).upper()} only: "
                    f"recording t={ideal_start_in_recording_sec:.9f} s, "
                    f"ROI={args.pat_start_led_roi}"
                )

            promote_capture_attempt(attempt_dir, run_dir)
            active_attempt_dir = None
            led_result = remap_attempt_result_paths(led_result, attempt_dir, run_dir)
            camera_run = (run_dir / "stereo_recording").resolve()
            marker_path = (run_dir / "capture_start_marker.json").resolve()
            timing_payload["ideal_start_in_recording_sec"] = ideal_start_in_recording_sec
            timing_payload["ideal_start_source"] = ideal_start_source
            timing_payload["pat_start_led"] = led_result
            if led_result is not None and str(led_result.get("report_json", "")).strip():
                atomic_write_json(Path(str(led_result["report_json"])), led_result)
            atomic_write_json(run_dir / "pat_camera_timing.json", timing_payload)
            successful_capture_committed = True
            break

        pipeline_manifest = {
            "schema_version": 1,
            "created_at": dt.datetime.now().isoformat(timespec="milliseconds"),
            "script": str(getattr(args, "entry_script", Path(__file__).name)),
            "recording_core": Path(__file__).name,
            "run_dir": str(run_dir),
            "shape_name": shape_name,
            "parameters": parameters,
            "closed_cycle": closed_cycle,
            "steps": n_steps,
            "frequency_hz": frequency,
            "loops": loops,
            "pat_fps_requested": rate.requested_hz,
            "pat_fps_actual": actual_fps,
            "pat_update_base_hz": PAT_UPDATE_BASE_HZ,
            "expected_duration_sec": expected_duration,
            "successful_capture_attempt": attempt_number,
            "ideal_log": str(ideal_log),
            "ideal_log_semantics": "tracking reference r(t); postprocess errors are measured_position - reference",
            "command_log": str(command_log),
            "command_log_semantics": "exact PAT command u(t), with tracking reference columns",
            "trajectory_preview": str(preview_path.resolve()),
            "reference_trajectory_preview": str(reference_preview_path.resolve()),
            "trajectory_stats": stats,
            "reference_trajectory_stats": reference_stats,
            "control": {
                "mode": parameters["control_mode"],
                "equation": (
                    "u = r(t+tau) + (r'' + gamma r')(t+tau) / w0^2 per axis"
                    if parameters["control_mode"] == "inverse2nd_feedforward"
                    else "u(t) = r(t) + tau * dr(t)/dt"
                    if parameters["control_mode"] == "delay_feedforward"
                    else "u = r"
                ),
                "feedforward": feedforward_stats,
                "evaluation_error": "measured_position - reference",
                "command_error_is_primary_metric": False,
            },
            "prepared_trajectory_artifacts": prepared_artifacts_manifest,
            "stereo_calibration": str(calibration),
            "camera_to_pat_transform": (
                str(args.camera_to_pat_transform.resolve()) if args.camera_to_pat_transform is not None else ""
            ),
            "comparison_kind": (
                "absolute fixed camera-to-PAT registration"
                if args.camera_to_pat_transform is not None
                else "per-run rigid fit (shape comparison only)"
            ),
            "left_serial": str(args.left_serial),
            "right_serial": str(args.right_serial),
            "hw_sync": str(args.hw_sync),
            "pat_start_led": {
                "side": str(args.pat_start_led_side),
                "roi": str(args.pat_start_led_roi),
                "bin_us": int(args.pat_start_led_bin_us),
                "threshold": int(args.pat_start_led_threshold),
                "search_half_window_sec": float(args.pat_start_led_search_sec),
                "comparison_time_refinement_enabled": bool(args.refine_led_time),
                "result": led_result,
            },
            "stereo_recording_dir": str(camera_run),
            "pat_camera_timing": str((run_dir / "pat_camera_timing.json").resolve()),
            "capture_complete": True,
            "capture_completed_at": dt.datetime.now().isoformat(timespec="milliseconds"),
            "processing_config": processing_config_from_args(args),
            "processing_complete": False,
            "processing_status": "pending",
        }
        if automation_metadata is not None:
            pipeline_manifest["automation"] = dict(automation_metadata)
        if trajectory.trajectory_source:
            pipeline_manifest["trajectory_source"] = dict(trajectory.trajectory_source)
        atomic_write_json(run_dir / "pipeline_manifest.json", pipeline_manifest)

        deferred_command = [
            sys.executable,
            str((PROJECT_ROOT / "stereo_acoustools_3d_postprocess.py").resolve()),
            str(run_dir),
        ]
        pipeline_manifest["deferred_postprocess_command"] = deferred_command
        atomic_write_json(run_dir / "pipeline_manifest.json", pipeline_manifest)
        print(f"\n[CAPTURE] Complete: {run_dir}")
        if postprocess_will_follow:
            print("[POSTPROCESS] Capture handed back to the full pipeline.")
        else:
            print("[POSTPROCESS] Deferred. Run later with:")
            print("  " + subprocess.list2cmdline(deferred_command))

        should_return_to_centre = return_to_centre
        if should_return_to_centre is None:
            should_return_to_centre = input("\nReturn particle to centre? (Y/n): ").strip().lower() != "n"
        if should_return_to_centre:
            current_pos = move_static_position(lev, current_pos, (0.0, 0.0, 0.0), "Moving back to centre")
            current_hologram = mute_sync_transducer(compute_holograms_for_positions([current_pos])[0])
            lev.levitate(current_hologram)
        return 0, run_dir
    except KeyboardInterrupt:
        print("\n[PIPELINE] Interrupted by user.")
        return 130, None
    finally:
        if recorder_process is not None and recorder_process.poll() is None:
            print("[CAPTURE] Waiting for the armed stereo recording to finish and save...")
            try:
                recorder_process.wait(timeout=30.0)
            except subprocess.TimeoutExpired:
                recorder_process.terminate()
                recorder_process.wait(timeout=5.0)
        if active_attempt_dir is not None and run_dir is not None and active_attempt_dir.exists():
            if args.keep_failed_captures:
                print(f"[CAPTURE] Incomplete capture retained for debugging: {active_attempt_dir}")
            else:
                try:
                    remove_capture_attempt(active_attempt_dir, run_dir)
                    print("[CAPTURE] Incomplete stereo capture was permanently deleted.")
                except Exception as exc:
                    print(f"[CAPTURE] WARNING: could not delete incomplete capture: {exc}")
        if (
            run_dir is not None
            and run_dir.exists()
            and not successful_capture_committed
            and not args.keep_failed_captures
        ):
            try:
                failed_run = run_dir
                remove_failed_run(failed_run, output_root)
                run_dir = None
                print(f"[PIPELINE] Unsuccessful run was permanently deleted: {failed_run}")
            except Exception as exc:
                print(f"[PIPELINE] WARNING: could not delete unsuccessful run: {exc}")
        session.current_pos = current_pos
        session.current_hologram = current_hologram
        if owns_hardware_session:
            shutdown_recording_hardware_session(session)
        else:
            print("[PAT] Persistent AcousTools/OpenMPD session kept open for the next run.")


def recording_main(argv: list[str] | None = None) -> int:
    args = parse_recording_args(argv)
    args.entry_script = "acoustools_stereo_eventcam_3d_recording.py"
    exit_code, _ = run_recording(args)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(recording_main())
