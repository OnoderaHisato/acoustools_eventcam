"""Single-left-camera, single-X-axis feedback experiment for OpenMPD.

The default mode is a hardware-free simulation.  Real hardware requires an
explicit mode, acknowledgement flags, a tracking ROI, and an operator at the
apparatus.  The existing stereo recorder and measurement artifacts are not
modified by this entry point.
"""

from __future__ import annotations

import argparse
from collections import deque
import csv
import datetime as _datetime
import json
import math
from pathlib import Path
import sys
import time
from typing import Any

import cv2
import numpy as np

from eventcam_npz_track import centroid_from_events, parse_roi
from mono_feedback_core import (
    PaperPid1D,
    PidConfig,
    SecondOrderPlantConfig,
    assess_lock_stability,
    load_left_camera_axis_projection,
    simulate_closed_loop,
    trap_particle_limit_exceeded,
    trap_particle_separation_mm,
)


LEFT_SERIAL = "00000508"
STEREO_CALIBRATION = Path(
    "stereo_checkerboard_calib_extrinsics_20260805/"
    "stereo_calibration_square7p12_extrinsics_final.npz"
)
CAMERA_TO_PAT = Path(
    "pat_stereo_grid_records/pat_camera_registration_grid_27_20260728_144326/"
    "registration/camera_to_pat_transform.npz"
)
DEFAULT_OUTPUT_ROOT = Path("mono_feedback_records")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hardware-gated mono event-camera X-axis position feedback experiment."
    )
    parser.add_argument("--mode", choices=["simulate", "hardware"], default="simulate")
    parser.add_argument("--duration-sec", type=float, default=5.0)
    parser.add_argument("--sample-hz", type=float, default=2000.0)
    parser.add_argument("--kp", type=float, default=0.05)
    parser.add_argument("--integral-hz", type=float, default=0.0)
    parser.add_argument("--derivative-hz", type=float, default=0.0)
    parser.add_argument("--moving-average-samples", type=int, default=4)
    parser.add_argument("--paper-gains", action="store_true", help="Use kp=.35, fi=23 Hz, fd=3.6 Hz. Simulation by default; hardware also requires --acknowledge-advanced-gains.")
    parser.add_argument("--max-trap-offset-mm", type=float, default=0.20)
    parser.add_argument("--max-trap-particle-mm", type=float, default=0.30)
    parser.add_argument("--max-slew-mm-s", type=float, default=5.0)
    parser.add_argument("--integral-output-limit-mm", type=float, default=0.05)
    parser.add_argument("--reference-mm", type=float, default=0.0)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)

    parser.add_argument("--camera-serial", default=LEFT_SERIAL)
    parser.add_argument("--tracking-roi", default="", help="Required in hardware mode: x0,y0,x1,y1 around only the particle event cluster.")
    parser.add_argument("--min-events", type=int, default=20)
    parser.add_argument("--min-area", type=int, default=5)
    parser.add_argument("--min-mass", type=int, default=30)
    parser.add_argument("--max-centroid-step-px", type=float, default=4.0)
    parser.add_argument(
        "--lock-sec",
        type=float,
        default=0.25,
        help="Continuous time for which the rolling lock window must remain stable.",
    )
    parser.add_argument("--lock-window-sec", type=float, default=0.25)
    parser.add_argument("--lock-timeout-sec", type=float, default=15.0)
    parser.add_argument("--lock-max-robust-std-mm", type=float, default=0.05)
    parser.add_argument("--lock-max-p90-span-mm", type=float, default=0.12)
    parser.add_argument("--lock-max-endpoint-mm", type=float, default=0.05)
    parser.add_argument("--max-invalid-samples", type=int, default=20)
    parser.add_argument("--max-overrun-streak", type=int, default=20)
    parser.add_argument("--lookup-step-mm", type=float, default=0.005)
    parser.add_argument("--drive-amplitude-scale", type=float, default=1.0)
    parser.add_argument("--calibration", type=Path, default=STEREO_CALIBRATION)
    parser.add_argument("--camera-to-pat", type=Path, default=CAMERA_TO_PAT)
    parser.add_argument("--acknowledge-real-time-feedback-risk", action="store_true")
    parser.add_argument("--acknowledge-advanced-gains", action="store_true")

    parser.add_argument("--plant-natural-frequency-hz", type=float, default=12.0)
    parser.add_argument("--plant-damping-ratio", type=float, default=0.014)
    parser.add_argument("--plant-delay-ms", type=float, default=5.5)
    parser.add_argument("--initial-position-mm", type=float, default=0.10)
    return parser


def resolved_pid_config(args: argparse.Namespace) -> PidConfig:
    kp = 0.35 if args.paper_gains else float(args.kp)
    integral_hz = 23.0 if args.paper_gains else float(args.integral_hz)
    derivative_hz = 3.6 if args.paper_gains else float(args.derivative_hz)
    config = PidConfig(
        sample_hz=float(args.sample_hz),
        kp=kp,
        integral_hz=integral_hz,
        derivative_hz=derivative_hz,
        moving_average_samples=int(args.moving_average_samples),
        derivative_mode="measurement",
        max_trap_offset_mm=float(args.max_trap_offset_mm),
        max_trap_particle_mm=float(args.max_trap_particle_mm),
        max_slew_mm_s=float(args.max_slew_mm_s),
        integral_output_limit_mm=float(args.integral_output_limit_mm),
    )
    config.validate()
    return config


def make_run_dir(root: Path, mode: str) -> Path:
    stamp = _datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = root / f"mono_x_feedback_{mode}_{stamp}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def run_simulation(args: argparse.Namespace, config: PidConfig) -> Path:
    run_dir = make_run_dir(args.output_root, "simulate")
    controller = PaperPid1D(config)
    plant = SecondOrderPlantConfig(
        natural_frequency_hz=float(args.plant_natural_frequency_hz),
        damping_ratio=float(args.plant_damping_ratio),
        delay_sec=float(args.plant_delay_ms) * 1e-3,
    )
    result = simulate_closed_loop(
        controller,
        duration_sec=float(args.duration_sec),
        plant=plant,
        initial_position_mm=float(args.initial_position_mm),
        reference_mm=float(args.reference_mm),
    )
    csv_path = run_dir / "simulation.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["time_sec", "position_mm", "velocity_mm_s", "trap_mm"])
        for values in zip(
            result["time_sec"], result["position_mm"], result["velocity_mm_s"], result["trap_mm"]
        ):
            writer.writerow([f"{float(value):.12g}" for value in values])

    tail_count = max(1, int(round(0.5 * config.sample_hz)))
    tail = result["position_mm"][-tail_count:]
    summary = {
        "mode": "simulate",
        "pid": config.to_dict(),
        "plant": {
            "natural_frequency_hz": plant.natural_frequency_hz,
            "damping_ratio": plant.damping_ratio,
            "delay_sec": plant.delay_sec,
        },
        "duration_sec": float(args.duration_sec),
        "initial_position_mm": float(args.initial_position_mm),
        "final_position_mm": float(result["position_mm"][-1]),
        "tail_rms_mm": float(np.sqrt(np.mean(tail * tail))),
        "max_abs_position_mm": float(np.max(np.abs(result["position_mm"]))),
        "max_abs_trap_mm": float(np.max(np.abs(result["trap_mm"]))),
        "csv": str(csv_path.resolve()),
    }
    write_json(run_dir / "summary.json", summary)

    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(2, 1, figsize=(9, 6), sharex=True)
    axes[0].plot(result["time_sec"], result["position_mm"], label="particle")
    axes[0].axhline(float(args.reference_mm), color="black", ls="--", lw=0.8, label="reference")
    axes[0].set_ylabel("position [mm]")
    axes[0].grid(ls=":", alpha=0.5)
    axes[0].legend()
    axes[1].plot(result["time_sec"], result["trap_mm"], color="tab:orange")
    axes[1].set_xlabel("time [s]")
    axes[1].set_ylabel("trap [mm]")
    axes[1].grid(ls=":", alpha=0.5)
    figure.tight_layout()
    figure.savefig(run_dir / "simulation.png", dpi=160)
    plt.close(figure)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return run_dir


def validate_hardware_request(args: argparse.Namespace, config: PidConfig) -> tuple[int, int, int, int]:
    if not args.acknowledge_real_time_feedback_risk:
        raise SystemExit("Hardware mode requires --acknowledge-real-time-feedback-risk.")
    if str(args.camera_serial) != LEFT_SERIAL:
        raise SystemExit(f"Initial reproduction is locked to the left camera serial {LEFT_SERIAL}.")
    if not str(args.tracking_roi).strip():
        raise SystemExit("Hardware mode requires an explicit tight --tracking-roi x0,y0,x1,y1.")
    roi = parse_roi(str(args.tracking_roi), "--tracking-roi")
    if roi is None:
        raise SystemExit("--tracking-roi is invalid")
    if float(args.duration_sec) <= 0 or float(args.duration_sec) > 10.0:
        raise SystemExit("The initial hardware run duration must be in (0, 10] seconds.")
    if config.max_trap_offset_mm > 0.25:
        raise SystemExit("The initial hardware entry point caps --max-trap-offset-mm at 0.25 mm.")
    advanced = config.kp > 0.10 or config.integral_hz > 0 or config.derivative_hz > 0
    if advanced and not args.acknowledge_advanced_gains:
        raise SystemExit(
            "kp>0.10 or I/D action requires --acknowledge-advanced-gains after a low-gain P-only run."
        )
    if not 0 < float(args.drive_amplitude_scale) <= 1.0:
        raise SystemExit("--drive-amplitude-scale must be in (0, 1].")
    if float(args.lookup_step_mm) <= 0 or float(args.lookup_step_mm) > 0.025:
        raise SystemExit("--lookup-step-mm must be in (0, 0.025] mm.")
    if abs(float(args.reference_mm)) > 1e-12:
        raise SystemExit("The initial hardware reproduction uses a fixed zero reference only.")
    lock_positive = {
        "--lock-sec": args.lock_sec,
        "--lock-window-sec": args.lock_window_sec,
        "--lock-timeout-sec": args.lock_timeout_sec,
        "--lock-max-robust-std-mm": args.lock_max_robust_std_mm,
        "--lock-max-p90-span-mm": args.lock_max_p90_span_mm,
        "--lock-max-endpoint-mm": args.lock_max_endpoint_mm,
    }
    for name, value in lock_positive.items():
        if not math.isfinite(float(value)) or float(value) <= 0:
            raise SystemExit(f"{name} must be positive.")
    minimum_lock_time = float(args.lock_window_sec) + float(args.lock_sec)
    if float(args.lock_timeout_sec) <= minimum_lock_time:
        raise SystemExit(
            "--lock-timeout-sec must exceed --lock-window-sec + --lock-sec."
        )
    return roi


def track_slice(events: np.ndarray, roi: tuple[int, int, int, int], args: argparse.Namespace) -> tuple[float, float, int, int]:
    if int(events.size) < int(args.min_events):
        return math.nan, math.nan, int(events.size), 0
    cx, cy, event_count, _area, mass, _radius = centroid_from_events(
        events,
        roi,
        threshold_count=1,
        blur=0,
        morph_open=0,
        morph_close=0,
        min_area=int(args.min_area),
        tracking_method="event_weighted",
        hough_dp=1.2,
        hough_min_dist_px=8.0,
        hough_param1=100.0,
        hough_param2=8.0,
        hough_min_radius_px=2,
        hough_max_radius_px=0,
        ransac_iterations=80,
        ransac_residual_px=2.0,
    )
    if int(mass) < int(args.min_mass):
        return math.nan, math.nan, int(event_count), int(mass)
    return float(cx), float(cy), int(event_count), int(mass)


def release_event_camera(iterator: Any, events_stream: Any, device: Any) -> None:
    reader = getattr(iterator, "reader", None)
    stream = getattr(reader, "i_events_stream", None)
    if stream is not None:
        try:
            stream.stop()
        except Exception:
            pass
    if reader is not None:
        try:
            reader.__del__()
        except Exception:
            pass
    del events_stream
    del iterator
    del device


def run_hardware(args: argparse.Namespace, config: PidConfig) -> Path:
    roi = validate_hardware_request(args, config)
    projection = load_left_camera_axis_projection(args.calibration, args.camera_to_pat, axis="x")
    run_dir = make_run_dir(args.output_root, "hardware")
    manifest_path = run_dir / "manifest.json"
    raw_path = run_dir / "left_events.raw"
    control_csv = run_dir / "control_log.csv"
    manifest: dict[str, Any] = {
        "status": "preparing",
        "mode": "hardware",
        "axis": "x",
        "camera_serial": LEFT_SERIAL,
        "camera_sync_role": "master",
        "tracking_roi": list(roi),
        "pid": config.to_dict(),
        "projection": projection.to_dict(),
        "calibration": str(args.calibration.resolve()),
        "camera_to_pat": str(args.camera_to_pat.resolve()),
        "duration_sec": float(args.duration_sec),
        "raw_path": str(raw_path.resolve()),
        "control_csv": str(control_csv.resolve()),
        "safety_policy": {
            "relative_limit_action": "hold_last_sent_trap_and_abort_before_send",
            "check_current_applied_trap": True,
            "check_requested_slew_limited_trap": True,
            "check_quantized_geometry_before_send": True,
        },
        "lock_policy": {
            "window_sec": float(args.lock_window_sec),
            "continuous_stable_sec": float(args.lock_sec),
            "timeout_sec": float(args.lock_timeout_sec),
            "max_robust_std_mm": float(args.lock_max_robust_std_mm),
            "max_p90_span_mm": float(args.lock_max_p90_span_mm),
            "max_endpoint_offset_mm": float(args.lock_max_endpoint_mm),
        },
    }
    write_json(manifest_path, manifest)

    print("\n[HARDWARE CHECKPOINT]")
    print(f"  left camera: {LEFT_SERIAL} (Master)")
    print("  axis: PAT X only")
    print(f"  tracking ROI: {roi}")
    print(f"  gains: kp={config.kp:g}, fi={config.integral_hz:g} Hz, fd={config.derivative_hz:g} Hz")
    print(f"  trap limit: +/-{config.max_trap_offset_mm:g} mm")
    print(f"  duration: {float(args.duration_sec):g} s")
    answer = input("Type RUN_MONO_X to compute the lookup table and connect OpenMPD: ").strip()
    if answer != "RUN_MONO_X":
        manifest["status"] = "cancelled_before_hardware"
        write_json(manifest_path, manifest)
        raise SystemExit("Cancelled before connecting hardware.")

    # Imports are deliberately delayed until the hardware checkpoint passes.
    from acoustools.Levitator import LevitatorController
    from acoustools_eventcam_sync import (
        compute_holograms_for_positions,
        configure_local_metavision_environment,
        mute_sync_transducer,
        prepare_message_from_holograms,
    )
    from eventcam_scale_calibration_capture import open_event_camera
    from stereo_eventcam_record_sync import apply_hw_sync_mode

    limit = config.max_trap_offset_mm
    lookup_values = np.arange(-limit, limit + float(args.lookup_step_mm) * 0.5, float(args.lookup_step_mm))
    if not np.any(np.isclose(lookup_values, 0.0, atol=1e-12)):
        lookup_values = np.sort(np.append(lookup_values, 0.0))
    positions = [(float(value) * 1e-3, 0.0, 0.0) for value in lookup_values]
    holograms = [
        mute_sync_transducer(hologram) * float(args.drive_amplitude_scale)
        for hologram in compute_holograms_for_positions(positions)
    ]

    lev: Any = None
    device: Any = None
    iterator: Any = None
    events_stream: Any = None
    raw_logging = False
    last_lookup_index = int(np.argmin(np.abs(lookup_values)))
    abort_reason = ""
    rows: list[dict[str, object]] = []
    last_lock_diagnostics: dict[str, object] | None = None
    try:
        lev = LevitatorController(ids=(101, 3))
        actual_frame_rate = int(lev.set_frame_rate(int(round(config.sample_hz))))
        messages = [prepare_message_from_holograms(lev, [hologram], permute=True)[:2] for hologram in holograms]
        phase_ct, amp_ct = messages[last_lookup_index]
        lev.send_message(phase_ct, amp_ct, 0, 1, sleep_ms=0, loop=False, num_loops=1)
        print(f"[PAT] Centre trap active; requested={config.sample_hz:g} Hz actual={actual_frame_rate} Hz")
        input("Load/confirm the particle at the centre, then press Enter to open the left camera: ")

        configure_local_metavision_environment()
        from metavision_core.event_io import EventsIterator
        from metavision_core.event_io.raw_reader import initiate_device
        import metavision_hal

        device = open_event_camera(initiate_device, metavision_hal, LEFT_SERIAL)
        sync_applied, sync_verified, sync_note = apply_hw_sync_mode(
            side="left",
            device=device,
            sync_role="master",
            hw_sync_ready_event=None,
            abort_event=None,
            hw_sync_timeout_sec=10.0,
        )
        iterator = EventsIterator.from_device(
            device=device,
            mode="delta_t",
            delta_t=int(round(1_000_000.0 / config.sample_hz)),
            max_duration=None,
            relative_timestamps=False,
        )
        events_stream = device.get_i_events_stream()
        events_stream.log_raw_data(str(raw_path.resolve()))
        raw_logging = True
        manifest.update(
            {
                "status": "running",
                "pat_frame_rate_actual_hz": actual_frame_rate,
                "camera_sync_applied": sync_applied,
                "camera_sync_verified": bool(sync_verified),
                "camera_sync_note": sync_note,
                "lookup_values_mm": lookup_values.tolist(),
                "started_at": _datetime.datetime.now().isoformat(timespec="seconds"),
            }
        )
        write_json(manifest_path, manifest)

        lock_window_samples = max(
            2, int(round(float(args.lock_window_sec) * config.sample_hz))
        )
        lock_stable_required = max(1, int(round(float(args.lock_sec) * config.sample_hz)))
        lock_samples: deque[tuple[float, float]] = deque(maxlen=lock_window_samples)
        lock_stable_streak = 0
        lock_valid_total = 0
        lock_report_interval = max(1, int(round(config.sample_hz)))
        lock_slice_count = 0
        lock_deadline_ns = time.perf_counter_ns() + int(float(args.lock_timeout_sec) * 1e9)
        controller = PaperPid1D(config)
        locked_origin: tuple[float, float] | None = None
        invalid_streak = 0
        overrun_streak = 0
        previous_centroid: tuple[float, float] | None = None
        control_started_ns = 0
        sample_index = 0

        for events in iterator:
            loop_start_ns = time.perf_counter_ns()
            cx, cy, event_count, mass = track_slice(events, roi, args)
            valid = math.isfinite(cx) and math.isfinite(cy)
            if valid and previous_centroid is not None:
                jump = math.hypot(cx - previous_centroid[0], cy - previous_centroid[1])
                if jump > float(args.max_centroid_step_px):
                    valid = False
            if valid:
                previous_centroid = (cx, cy)

            if locked_origin is None:
                lock_slice_count += 1
                if valid:
                    lock_samples.append((cx, cy))
                    lock_valid_total += 1
                else:
                    lock_samples.clear()
                    lock_stable_streak = 0

                if len(lock_samples) == lock_window_samples:
                    stability = assess_lock_stability(
                        np.asarray(lock_samples, dtype=float),
                        projection,
                        max_robust_std_mm=float(args.lock_max_robust_std_mm),
                        max_p90_span_mm=float(args.lock_max_p90_span_mm),
                        max_endpoint_offset_mm=float(args.lock_max_endpoint_mm),
                    )
                    last_lock_diagnostics = {
                        **stability.to_dict(),
                        "window_samples": lock_window_samples,
                        "stable_streak_samples": lock_stable_streak,
                        "valid_samples_total": lock_valid_total,
                    }
                    if stability.stable:
                        lock_stable_streak += 1
                    else:
                        lock_stable_streak = 0
                    last_lock_diagnostics["stable_streak_samples"] = lock_stable_streak

                    if lock_stable_streak >= lock_stable_required:
                        locked_origin = stability.origin_px
                        controller.reset(trap_mm=float(lookup_values[last_lookup_index]))
                        control_started_ns = time.perf_counter_ns()
                        manifest["lock_diagnostics"] = last_lock_diagnostics
                        write_json(manifest_path, manifest)
                        print(
                            f"[LOCK] stable origin=({locked_origin[0]:.3f}, "
                            f"{locked_origin[1]:.3f}) px, robust_std="
                            f"{stability.robust_std_mm:.4f} mm, p90_span="
                            f"{stability.p90_span_mm:.4f} mm, endpoint="
                            f"{stability.endpoint_offset_mm:.4f} mm"
                        )
                    elif lock_slice_count % lock_report_interval == 0:
                        print(
                            f"[LOCK WAIT] robust_std={stability.robust_std_mm:.4f} mm, "
                            f"p90_span={stability.p90_span_mm:.4f} mm, endpoint="
                            f"{stability.endpoint_offset_mm:.4f} mm, stable="
                            f"{lock_stable_streak}/{lock_stable_required} samples"
                        )

                if locked_origin is None and time.perf_counter_ns() >= lock_deadline_ns:
                    if last_lock_diagnostics is None:
                        abort_reason = (
                            "stable_lock_timeout_without_full_valid_window_"
                            f"{len(lock_samples)}_of_{lock_window_samples}_samples"
                        )
                    else:
                        abort_reason = "stable_lock_timeout_position_not_stationary"
                    break
                continue

            if (loop_start_ns - control_started_ns) * 1e-9 >= float(args.duration_sec):
                break
            measured_mm = math.nan
            command_mm = float(lookup_values[last_lookup_index])
            sent = False
            send_us = 0.0
            saturation = False
            raw_error_mm: float | str = ""
            filtered_error_mm: float | str = ""
            unsaturated_trap_mm: float | str = ""
            absolute_limited_trap_mm: float | str = ""
            requested_trap_mm: float | str = ""
            current_separation_mm: float | str = ""
            requested_separation_mm: float | str = ""
            output_saturated = False
            relative_limit_exceeded = False
            slew_limited = False
            if valid:
                invalid_streak = 0
                measured_mm = projection.displacement_mm((cx, cy), locked_origin)
                current_separation_mm = trap_particle_separation_mm(
                    trap_mm=command_mm, measured_mm=measured_mm
                )
                if trap_particle_limit_exceeded(
                    trap_mm=command_mm,
                    measured_mm=measured_mm,
                    limit_mm=config.max_trap_particle_mm,
                ):
                    relative_limit_exceeded = True
                    saturation = True
                    abort_reason = (
                        "current_trap_particle_separation_exceeded_"
                        f"{current_separation_mm:.6f}_mm_limit_"
                        f"{config.max_trap_particle_mm:.6f}_mm"
                    )
                else:
                    control = controller.update(
                        reference_mm=float(args.reference_mm),
                        measured_mm=measured_mm,
                        dt_sec=config.nominal_dt_sec,
                    )
                    raw_error_mm = control.raw_error_mm
                    filtered_error_mm = control.filtered_error_mm
                    unsaturated_trap_mm = control.unsaturated_trap_mm
                    absolute_limited_trap_mm = control.absolute_limited_trap_mm
                    requested_trap_mm = control.requested_trap_mm
                    requested_separation_mm = control.requested_trap_particle_mm
                    output_saturated = control.output_saturated
                    relative_limit_exceeded = control.relative_limit_exceeded
                    slew_limited = control.slew_limited
                    saturation = bool(output_saturated or relative_limit_exceeded or slew_limited)
                    if relative_limit_exceeded:
                        abort_reason = (
                            "requested_trap_particle_separation_exceeded_"
                            f"{control.requested_trap_particle_mm:.6f}_mm_limit_"
                            f"{config.max_trap_particle_mm:.6f}_mm"
                        )
                    else:
                        requested_index = int(
                            np.argmin(np.abs(lookup_values - control.requested_trap_mm))
                        )
                        requested_trap_mm = float(lookup_values[requested_index])
                        requested_separation_mm = trap_particle_separation_mm(
                            trap_mm=requested_trap_mm, measured_mm=measured_mm
                        )
                        if trap_particle_limit_exceeded(
                            trap_mm=requested_trap_mm,
                            measured_mm=measured_mm,
                            limit_mm=config.max_trap_particle_mm,
                        ):
                            relative_limit_exceeded = True
                            saturation = True
                            abort_reason = (
                                "quantized_trap_particle_separation_exceeded_"
                                f"{requested_separation_mm:.6f}_mm_limit_"
                                f"{config.max_trap_particle_mm:.6f}_mm"
                            )
                        else:
                            command_mm = requested_trap_mm
                            if requested_index != last_lookup_index:
                                send_start_ns = time.perf_counter_ns()
                                phase_ct, amp_ct = messages[requested_index]
                                lev.send_message(
                                    phase_ct,
                                    amp_ct,
                                    0,
                                    1,
                                    sleep_ms=0,
                                    loop=False,
                                    num_loops=1,
                                )
                                send_us = (time.perf_counter_ns() - send_start_ns) * 1e-3
                                last_lookup_index = requested_index
                                sent = True
            else:
                invalid_streak += 1
                if invalid_streak > int(args.max_invalid_samples):
                    abort_reason = f"tracking_invalid_for_{invalid_streak}_samples"
                    break

            processing_us = (time.perf_counter_ns() - loop_start_ns) * 1e-3
            if processing_us > config.nominal_dt_sec * 1e6:
                overrun_streak += 1
            else:
                overrun_streak = 0
            if not abort_reason and overrun_streak > int(args.max_overrun_streak):
                abort_reason = f"control_deadline_overrun_for_{overrun_streak}_samples"
            event_first_us = int(events["t"][0]) if int(events.size) else -1
            event_last_us = int(events["t"][-1]) if int(events.size) else -1
            rows.append(
                {
                    "sample": sample_index,
                    "event_first_us": event_first_us,
                    "event_last_us": event_last_us,
                    "centroid_x_px": cx if valid else "",
                    "centroid_y_px": cy if valid else "",
                    "measured_x_mm": measured_mm if valid else "",
                    "trap_x_mm": command_mm,
                    "raw_error_mm": raw_error_mm,
                    "filtered_error_mm": filtered_error_mm,
                    "unsaturated_trap_x_mm": unsaturated_trap_mm,
                    "absolute_limited_trap_x_mm": absolute_limited_trap_mm,
                    "requested_trap_x_mm": requested_trap_mm,
                    "current_trap_particle_mm": current_separation_mm,
                    "requested_trap_particle_mm": requested_separation_mm,
                    "event_count": event_count,
                    "component_mass": mass,
                    "valid": int(valid),
                    "sent": int(sent),
                    "send_us": send_us,
                    "processing_us": processing_us,
                    "output_saturated": int(output_saturated),
                    "relative_limit_exceeded": int(relative_limit_exceeded),
                    "slew_limited": int(slew_limited),
                    "safety_limited": int(saturation),
                }
            )
            sample_index += 1
            if abort_reason:
                break

        # A normally completed, valid run returns the trap to centre.  On a
        # tracking/deadline abort, holding the last command is safer than an
        # unobserved automatic move.
        if not abort_reason:
            centre_index = int(np.argmin(np.abs(lookup_values)))
            phase_ct, amp_ct = messages[centre_index]
            lev.send_message(phase_ct, amp_ct, 0, 1, sleep_ms=0, loop=False, num_loops=1)
            last_lookup_index = centre_index
            print("[PAT] Feedback run complete; trap returned to centre.")
        else:
            print(f"[ABORT] {abort_reason}; holding last trap at {lookup_values[last_lookup_index]:.4f} mm")

    except KeyboardInterrupt:
        abort_reason = abort_reason or "operator_keyboard_interrupt"
        print(f"[ABORT] {abort_reason}; holding the most recently sent trap.")
    finally:
        if raw_logging and events_stream is not None:
            try:
                events_stream.stop_log_raw_data()
            except Exception:
                pass
        if iterator is not None and device is not None:
            release_event_camera(iterator, events_stream, device)
        if rows:
            with control_csv.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        manifest.update(
            {
                "status": "aborted" if abort_reason else "complete",
                "abort_reason": abort_reason or None,
                "completed_at": _datetime.datetime.now().isoformat(timespec="seconds"),
                "control_samples": len(rows),
                "last_trap_mm": float(lookup_values[last_lookup_index]),
            }
        )
        if last_lock_diagnostics is not None:
            manifest["lock_diagnostics"] = last_lock_diagnostics
        write_json(manifest_path, manifest)
        if lev is not None:
            print("The PAT is still active to retain the particle.")
            input("Retrieve/secure the particle, then press Enter to turn off and disconnect OpenMPD: ")
            try:
                lev.turn_off()
            finally:
                lev.disconnect()
    return run_dir


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = resolved_pid_config(args)
    if args.mode == "simulate":
        run_dir = run_simulation(args, config)
    else:
        run_dir = run_hardware(args, config)
    print(f"Output: {run_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
