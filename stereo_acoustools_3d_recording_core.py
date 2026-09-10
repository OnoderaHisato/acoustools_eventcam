#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Core AcousTools + synchronized stereo event-camera 3D recording workflow.

Workflow:
  1. Generate/prepare a planar, random 3D, or closed 3D PAT trajectory.
  2. Record both event cameras in one verified hardware timestamp domain.
  3. Start PAT motion only after the shared camera interval is active.
  4. Detect PAT start from the selected camera's LED events.
  5. Commit the recording manifest with postprocessing status ``pending``.

No particle tracking, triangulation, or ideal comparison runs in this module.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Tuple

import torch

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
    write_ideal_log,
)
from acoustools_cusped3d import (
    generate_cusped_3d_positions,
    prompt_cusped_3d_parameters,
)
from acoustools_random3d_no_eventcam import (
    generate_chirped_3d_positions,
    generate_extended_random_3d_positions,
    generate_random_3d_positions,
    prompt_chirped_3d_parameters,
    prompt_closed_parameters,
    prompt_closed_timing,
    prompt_random_parameters,
    prompt_extended_random_parameters,
)
import stereo_eventcam_record_sync as stereo_record
from stereo_detect_pat_start_led import detect_led_sync_npz, parse_roi as parse_led_roi
from stereo_acoustools_3d_common import (
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
    return parser


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


@dataclass(frozen=True)
class PrecomputedHologramPlayback:
    """Hardware-independent holograms and their exact OpenMPD playback counts."""

    holograms: list[torch.Tensor]
    source_frame_count: int
    message_geometry_count: int
    message_loops: int
    static_compacted: bool
    drive_amplitude_scale: float
    preparation_seconds: float


@dataclass
class RecordingHardwareSession:
    """One persistent AcousTools/OpenMPD connection and its held particle state."""

    lev: LevitatorController
    current_pos: Tuple[float, float, float]
    current_hologram: torch.Tensor | None
    drive_amplitude_scale: float = 1.0
    is_shutdown: bool = False
    active_output_started_perf_ns: int | None = None
    active_output_started_wall_ns: int | None = None
    last_active_warmup: dict[str, Any] = field(default_factory=dict)


def precompute_hologram_playback(
    trajectory: PreparedTrajectory,
) -> PrecomputedHologramPlayback:
    """Compute holograms before PAT output and compact an exactly static command."""
    positions = list(trajectory.positions)
    loops = int(trajectory.loops)
    if not positions:
        raise ValueError("Cannot precompute an empty trajectory")
    if loops <= 0:
        raise ValueError("Trajectory loops must be positive")
    scale = float(trajectory.trajectory_source.get("drive_amplitude_scale", 1.0))
    if not 0.0 < scale <= 1.0:
        raise ValueError("drive_amplitude_scale must be in (0, 1]")

    static_compacted = all(position == positions[0] for position in positions[1:])
    compute_positions = [positions[0]] if static_compacted else positions
    message_loops = len(positions) * loops if static_compacted else loops
    started = time.perf_counter()
    holograms = [
        scale_hologram_drive_amplitude(hologram, scale)
        for hologram in compute_holograms_for_positions(compute_positions)
    ]
    preparation_seconds = time.perf_counter() - started
    playback = PrecomputedHologramPlayback(
        holograms=holograms,
        source_frame_count=len(positions) * loops,
        message_geometry_count=len(holograms),
        message_loops=message_loops,
        static_compacted=static_compacted,
        drive_amplitude_scale=scale,
        preparation_seconds=preparation_seconds,
    )
    print(
        "[PREP] Holograms ready before PAT output: "
        f"source_frames={playback.source_frame_count}, "
        f"message_geometries={playback.message_geometry_count}, "
        f"message_loops={playback.message_loops}, "
        f"static_compacted={playback.static_compacted}, "
        f"elapsed={playback.preparation_seconds:.3f}s",
        flush=True,
    )
    return playback


def _mark_active_output_started(session: RecordingHardwareSession) -> None:
    """Start the warm-up clock only after a non-zero PAT command returned."""
    session.active_output_started_perf_ns = time.perf_counter_ns()
    session.active_output_started_wall_ns = time.time_ns()
    session.last_active_warmup = {}


def wait_for_active_warmup(
    session: RecordingHardwareSession, target_minutes: float
) -> dict[str, Any]:
    """Wait only the remaining time since confirmed non-zero PAT output."""
    target = float(target_minutes)
    if not math.isfinite(target) or target < 0.0:
        raise ValueError("active_warmup_target_minutes must be finite and non-negative")
    if session.active_output_started_perf_ns is None:
        raise RuntimeError("Active PAT output start time is unavailable")

    target_sec = target * 60.0
    wait_started_perf_ns = time.perf_counter_ns()
    elapsed_at_wait_start_sec = ns_to_s(
        wait_started_perf_ns - session.active_output_started_perf_ns
    )
    remaining_sec = max(0.0, target_sec - elapsed_at_wait_start_sec)
    print(
        f"[WARMUP] Active PAT target={target:.3f} min; "
        f"elapsed={elapsed_at_wait_start_sec:.3f}s; "
        f"remaining={remaining_sec:.3f}s.",
        flush=True,
    )
    while remaining_sec > 0.0:
        time.sleep(min(30.0, remaining_sec))
        now_perf_ns = time.perf_counter_ns()
        remaining_sec = max(
            0.0,
            target_sec
            - ns_to_s(now_perf_ns - session.active_output_started_perf_ns),
        )
        if remaining_sec > 0.0:
            print(f"[WARMUP] remaining={remaining_sec:.1f}s", flush=True)

    wait_finished_perf_ns = time.perf_counter_ns()
    payload = {
        "definition": "elapsed time since confirmed non-zero PAT output",
        "target_minutes": target,
        "target_seconds": target_sec,
        "active_output_started_perf_ns": session.active_output_started_perf_ns,
        "active_output_started_wall_ns": session.active_output_started_wall_ns,
        "elapsed_at_wait_start_sec": elapsed_at_wait_start_sec,
        "intentional_wait_sec": ns_to_s(wait_finished_perf_ns - wait_started_perf_ns),
        "elapsed_after_wait_sec": ns_to_s(
            wait_finished_perf_ns - session.active_output_started_perf_ns
        ),
    }
    session.last_active_warmup = dict(payload)
    print(
        f"[WARMUP] Target reached; active PAT elapsed={payload['elapsed_after_wait_sec']:.3f}s.",
        flush=True,
    )
    return payload


def scale_hologram_drive_amplitude(
    hologram: torch.Tensor, drive_amplitude_scale: float
) -> torch.Tensor:
    """Scale active transducer amplitudes while preserving their phases."""
    scale = float(drive_amplitude_scale)
    if not 0.0 < scale <= 1.0:
        raise ValueError("drive_amplitude_scale must be in (0, 1]")
    return hologram.clone() * scale


def set_recording_drive_amplitude(
    session: RecordingHardwareSession, drive_amplitude_scale: float
) -> None:
    """Apply a run's drive level before its stereo preview and capture."""
    scale = float(drive_amplitude_scale)
    if not 0.0 < scale <= 1.0:
        raise ValueError("drive_amplitude_scale must be in (0, 1]")
    if abs(float(session.drive_amplitude_scale) - scale) <= 1e-12:
        return
    base = mute_sync_transducer(
        compute_holograms_for_positions([session.current_pos])[0]
    )
    current_hologram = scale_hologram_drive_amplitude(base, scale)
    session.lev.levitate(current_hologram)
    session.current_hologram = current_hologram
    session.drive_amplitude_scale = scale
    _mark_active_output_started(session)
    time.sleep(0.5)
    print(f"[PAT] Drive amplitude scale set to {scale:.3f} before preview/capture.")


def open_recording_hardware_session(
    *,
    initial_hologram: torch.Tensor | None = None,
    drive_amplitude_scale: float = 1.0,
) -> RecordingHardwareSession:
    """Connect once and hold the particle at the PAT coordinate-system origin."""
    lev = LevitatorController(ids=(101, 3))
    current_pos: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale = float(drive_amplitude_scale)
    if not 0.0 < scale <= 1.0:
        raise ValueError("drive_amplitude_scale must be in (0, 1]")
    if initial_hologram is None:
        current_hologram = scale_hologram_drive_amplitude(
            mute_sync_transducer(compute_holograms_for_positions([current_pos])[0]),
            scale,
        )
    else:
        current_hologram = mute_sync_transducer(initial_hologram)
    lev.levitate(current_hologram)
    session = RecordingHardwareSession(
        lev,
        current_pos,
        current_hologram,
        drive_amplitude_scale=scale,
    )
    _mark_active_output_started(session)
    time.sleep(0.5)
    print("[PAT] Persistent AcousTools/OpenMPD session opened; particle held at centre.")
    return session


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
    print(" 16: Extended multi-band random 3D excitation")
    print(" 17: Three-axis chirp excitation")
    print(" 18: Cusped planar/lifted-3D curve")
    try:
        menu_mode = int(input("Select mode (1-18): ").strip())
    except (ValueError, EOFError):
        raise SystemExit("Mode must be a number from 1 to 18.")
    if menu_mode not in range(1, 19):
        raise SystemExit("Mode must be from 1 to 18.")

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

    if menu_mode == 16:
        params = prompt_extended_random_parameters()
        if params is None:
            raise SystemExit("Invalid extended random 3D parameters.")
        from acoustools_eventcam_sync import compute_pat_frame_rate_info

        rate = compute_pat_frame_rate_info(1, params.sample_hz)
        if not rate.is_supported:
            raise SystemExit(f"PAT sample rate {params.sample_hz:g} Hz is not an exact 40 kHz divider.")
        positions, generated_stats = generate_extended_random_3d_positions(params, centre)
        return PreparedTrajectory(
            shape_name="extended_random_3d",
            positions=positions,
            n_steps=len(positions),
            frequency_hz=params.sample_hz / len(positions),
            loops=1,
            rate=rate,
            parameters=asdict(params),
            closed_cycle=False,
            stats=generated_stats,
        )

    if menu_mode == 17:
        params = prompt_chirped_3d_parameters()
        if params is None:
            raise SystemExit("Invalid chirped 3D parameters.")
        from acoustools_eventcam_sync import compute_pat_frame_rate_info

        rate = compute_pat_frame_rate_info(1, params.sample_hz)
        if not rate.is_supported:
            raise SystemExit(f"PAT sample rate {params.sample_hz:g} Hz is not an exact 40 kHz divider.")
        positions, generated_stats = generate_chirped_3d_positions(params, centre)
        return PreparedTrajectory(
            shape_name="chirped_3d",
            positions=positions,
            n_steps=len(positions),
            frequency_hz=params.sample_hz / len(positions),
            loops=1,
            rate=rate,
            parameters=asdict(params),
            closed_cycle=False,
            stats=generated_stats,
        )

    if menu_mode == 18:
        params = prompt_cusped_3d_parameters()
        if params is None:
            raise SystemExit("Invalid cusped 3D parameters.")
        from acoustools_eventcam_sync import compute_pat_frame_rate_info

        rate = compute_pat_frame_rate_info(params.steps_per_cycle, params.frequency_hz)
        if not rate.is_supported:
            raise SystemExit(
                f"PAT sample rate {params.steps_per_cycle * params.frequency_hz:g} Hz "
                "is not an exact 40 kHz divider."
            )
        positions, generated_stats = generate_cusped_3d_positions(params, centre)
        return PreparedTrajectory(
            shape_name="cusped_3d",
            positions=positions,
            n_steps=len(positions),
            frequency_hz=params.frequency_hz,
            loops=params.loops,
            rate=rate,
            parameters=asdict(params),
            closed_cycle=True,
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


RUN_PATH_BUDGET_CHARS = 248
RUN_LONGEST_CAPTURE_RELATIVE_PATH = Path(
    "_capture_attempt_999/stereo_recording/right/right_recording_meta.json"
)
RUN_COLLISION_SUFFIX_CHARS = len("_999")


def safe_run_dir_base(root: Path, shape_name: str, run_label: str, stamp: str) -> str:
    """Build a readable run name while reserving room for nested capture files."""
    label_part = f"_{run_label}" if str(run_label).strip() else ""
    descriptor = f"{shape_name}{label_part}"
    timestamp_suffix = f"_{stamp}"
    available = (
        RUN_PATH_BUDGET_CHARS
        - len(str(root.resolve()))
        - 1
        - 1
        - len(str(RUN_LONGEST_CAPTURE_RELATIVE_PATH))
        - RUN_COLLISION_SUFFIX_CHARS
    )
    descriptor_limit = available - len(timestamp_suffix)
    if descriptor_limit < 19:
        raise RuntimeError(
            "Output directory is too long for Windows-safe stereo capture paths: "
            f"{root.resolve()}. Choose a shorter --output-dir."
        )
    safe_descriptor = sanitize_filename(descriptor, max_len=descriptor_limit)
    return f"{safe_descriptor}{timestamp_suffix}"


def create_run_dir(root: Path, shape_name: str, run_label: str = "") -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    root.mkdir(parents=True, exist_ok=True)
    base = safe_run_dir_base(root, shape_name, run_label, stamp)
    candidate = root / base
    for index in range(1, 1000):
        path = candidate if index == 1 else root / f"{base}_{index:03d}"
        if not path.exists():
            path.mkdir(parents=True)
            return path.resolve()
    raise SystemExit("Could not create a unique pipeline run directory.")


RUN_ARTIFACT_PATH_BUDGET_CHARS = 248
RUN_ARTIFACT_LONGEST_SUFFIX = "_trajectory_preview.png"


def safe_run_artifact_stem(run_dir: Path, preferred_stem: str) -> str:
    """Keep generated run artifacts below the conservative Windows path limit."""
    resolved_run_dir = run_dir.resolve()
    available = (
        RUN_ARTIFACT_PATH_BUDGET_CHARS
        - len(str(resolved_run_dir))
        - 1
        - len(RUN_ARTIFACT_LONGEST_SUFFIX)
    )
    if available < 19:
        raise RuntimeError(
            "Output directory is too long for Windows-safe run artifacts even after "
            f"filename shortening: {resolved_run_dir}. Choose a shorter --output-dir."
        )
    return sanitize_filename(preferred_stem, max_len=min(70, available))


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
    precomputed_playback: PrecomputedHologramPlayback | None = None,
    active_warmup_target_minutes: float | None = None,
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
    drive_amplitude_scale = float(
        (prepared_trajectory.trajectory_source if prepared_trajectory else {}).get(
            "drive_amplitude_scale", 1.0
        )
    )
    recorder_process: subprocess.Popen[Any] | None = None
    active_attempt_dir: Path | None = None
    run_dir: Path | None = None
    successful_capture_committed = False
    prepared_artifacts_manifest: dict[str, dict[str, Any]] = {}
    active_warmup: dict[str, Any] = {}
    try:
        set_recording_drive_amplitude(session, drive_amplitude_scale)
        current_hologram = session.current_hologram
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
        positions = trajectory.positions
        n_steps = trajectory.n_steps
        frequency = trajectory.frequency_hz
        loops = trajectory.loops
        rate = trajectory.rate
        parameters = trajectory.parameters
        closed_cycle = trajectory.closed_cycle
        stats = trajectory.stats
        print_trajectory_stats(stats)
        run_dir = create_run_dir(output_root, shape_name, trajectory.run_label)
        prepared_artifacts_manifest = copy_prepared_trajectory_artifacts(
            run_dir, trajectory.source_artifacts
        )
        preferred_run_base = f"{shape_name}_s{n_steps}_f{rate.requested_hz:g}_l{loops}"
        run_base = safe_run_artifact_stem(run_dir, preferred_run_base)
        if run_base != sanitize_filename(preferred_run_base, max_len=70):
            print(
                "[PATH] Shortened generated artifact names for Windows path safety: "
                f"{run_base}"
            )
        preview_path = run_dir / f"{run_base}_trajectory_preview.png"
        save_trajectory_preview(str(preview_path), positions, current_pos, f"{shape_name}: ideal 3D trajectory")

        if precomputed_playback is None:
            print(f"[PREP] Computing {len(positions)} holograms before cameras are opened...")
            holograms = [
                scale_hologram_drive_amplitude(hologram, drive_amplitude_scale)
                for hologram in compute_holograms_for_positions(positions)
            ]
            playback_source_frame_count = len(positions) * int(loops)
            playback_message_loops = int(loops)
            playback_static_compacted = False
            playback_preparation_seconds = None
        else:
            if precomputed_playback.source_frame_count != len(positions) * int(loops):
                raise ValueError("Precomputed playback frame count does not match trajectory")
            if not math.isclose(
                precomputed_playback.drive_amplitude_scale,
                drive_amplitude_scale,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise ValueError("Precomputed playback drive amplitude does not match trajectory")
            holograms = precomputed_playback.holograms
            playback_source_frame_count = precomputed_playback.source_frame_count
            playback_message_loops = precomputed_playback.message_loops
            playback_static_compacted = precomputed_playback.static_compacted
            playback_preparation_seconds = precomputed_playback.preparation_seconds
            print(
                "[PREP] Reusing holograms computed before PAT output; "
                f"message_geometries={len(holograms)}, "
                f"message_loops={playback_message_loops}.",
                flush=True,
            )
        actual_fps = float(rate.effective_hz or rate.requested_hz)
        lev.set_frame_rate(rate.requested_hz)
        phases, amplitudes, geometry_count = prepare_message_from_holograms(lev, holograms, permute=True)
        expected_duration = geometry_count * playback_message_loops / actual_fps
        all_positions = positions * loops
        ideal_log = (run_dir / f"{run_base}_ideal_log.csv").resolve()
        write_ideal_log(str(ideal_log), all_positions, actual_fps)

        start_pos = positions[0]
        end_pos = positions[0] if closed_cycle else positions[-1]
        end_hologram = holograms[0] if closed_cycle else holograms[-1]
        current_pos = move_static_position(
            lev,
            current_pos,
            start_pos,
            "Moving smoothly to trajectory start",
            drive_amplitude_scale=drive_amplitude_scale,
        )
        current_hologram = mute_sync_transducer(holograms[0])
        lev.levitate(current_hologram)
        if prompt_before_capture:
            input("\n>>> Particle is at the start. Press Enter to arm stereo capture and record. <<<\n")
        if active_warmup_target_minutes is not None:
            active_warmup = wait_for_active_warmup(
                session, float(active_warmup_target_minutes)
            )

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
            if active_warmup:
                camera_start_perf_ns = int(marker["pc_start_perf_ns"])
                measured_sec = ns_to_s(
                    camera_start_perf_ns - int(session.active_output_started_perf_ns)
                )
                active_warmup.update(
                    {
                        "camera_capture_started_perf_ns": camera_start_perf_ns,
                        "camera_capture_started_wall_ns": marker.get("pc_start_wall_ns"),
                        "measured_to_camera_start_sec": measured_sec,
                        "measured_to_camera_start_minutes": measured_sec / 60.0,
                        "target_met": measured_sec + 1e-6
                        >= float(active_warmup["target_seconds"]),
                    }
                )
                session.last_active_warmup = dict(active_warmup)
            print(
                "[CAPTURE] Shared hardware-synchronized interval is active; "
                f"marker latency={(marker_seen_perf_ns - int(marker['pc_start_perf_ns'])) * 1e-6:.3f} ms"
            )

            pat_start_perf_ns = time.perf_counter_ns()
            pat_start_wall_ns = time.time_ns()
            send_error = ""
            try:
                lev.send_message(
                    phases,
                    amplitudes,
                    0,
                    int(geometry_count),
                    sleep_ms=0,
                    loop=True,
                    num_loops=int(playback_message_loops),
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
                "active_warmup": dict(active_warmup),
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
                        lev,
                        current_pos,
                        start_pos,
                        "Returning smoothly to trajectory start for retry",
                        drive_amplitude_scale=drive_amplitude_scale,
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
            "hologram_playback": {
                "source_frame_count": playback_source_frame_count,
                "message_geometry_count": int(geometry_count),
                "message_loops": int(playback_message_loops),
                "static_compacted": bool(playback_static_compacted),
                "precomputed_before_pat_output": precomputed_playback is not None,
                "preparation_seconds": playback_preparation_seconds,
            },
            "active_warmup": dict(active_warmup),
            "successful_capture_attempt": attempt_number,
            "ideal_log": str(ideal_log),
            "trajectory_preview": str(preview_path.resolve()),
            "trajectory_stats": stats,
            "drive_amplitude_scale": drive_amplitude_scale,
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
            str((Path(__file__).resolve().parent / "stereo_acoustools_3d_postprocess.py").resolve()),
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
            current_pos = move_static_position(
                lev,
                current_pos,
                (0.0, 0.0, 0.0),
                "Moving back to centre",
                drive_amplitude_scale=drive_amplitude_scale,
            )
            current_hologram = scale_hologram_drive_amplitude(
                mute_sync_transducer(compute_holograms_for_positions([current_pos])[0]),
                drive_amplitude_scale,
            )
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
