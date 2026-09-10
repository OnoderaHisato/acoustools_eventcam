#!/usr/bin/env python3
"""Record a zero-shot baseline/delay-feedforward A/B test on a heart trajectory."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
from dataclasses import replace
from pathlib import Path
from typing import Any

from acoustools_eventcam_sync import compute_pat_frame_rate_info
from acoustools_multitraj_no_eventcam import TrajectoryParameters
from heart_delay_feedforward import apply_delay_feedforward
from stereo_acoustools_heart_delay_common import atomic_write_json
from stereo_acoustools_heart_delay_recording_core import (
    build_recording_parser,
    open_recording_hardware_session,
    prepare_parametric_trajectory,
    run_recording,
    shutdown_recording_hardware_session,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_recording_parser()
    parser.description = "Zero-shot A/B test of the frozen delay model on a heart trajectory."
    parser.set_defaults(output_dir=Path("stereo_acoustools_heart_delay_ab_records"))
    parser.add_argument("--heart-scale-mm", type=float, default=7.0)
    parser.add_argument("--heart-frequency-hz", type=float, default=10.0)
    parser.add_argument("--steps-per-cycle", type=int, default=800)
    parser.add_argument("--loops", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=3, help="Number of baseline/delay pairs.")
    parser.add_argument("--dry-run", action="store_true", help="Validate both commands without hardware.")
    parser.add_argument("--confirm-each-run", action="store_true")
    parser.add_argument(
        "--acknowledge-delay-offset-above-1mm",
        action="store_true",
        help="Required when --delay-max-offset-mm is greater than 1 mm.",
    )
    parser.add_argument(
        "--require-unclipped-delay",
        action="store_true",
        help="Abort before hardware when any u-r sample reaches the configured offset limit.",
    )
    return parser.parse_args(argv)


def make_heart_trajectory(
    centre: tuple[float, float, float],
    scale_mm: float,
    frequency_hz: float,
    steps_per_cycle: int,
    loops: int,
):
    if min(scale_mm, frequency_hz, steps_per_cycle, loops) <= 0:
        raise ValueError("Heart scale, frequency, steps, and loops must all be positive.")
    rate = compute_pat_frame_rate_info(int(steps_per_cycle), float(frequency_hz))
    if not rate.is_supported:
        raise ValueError(
            f"PAT rate {steps_per_cycle * frequency_hz:g} Hz is not an exact 40 kHz divider."
        )
    params = TrajectoryParameters(
        amp_x_mm=0.0,
        amp_y_mm=0.0,
        amp_z_mm=0.0,
        amp_scale_mm=float(scale_mm),
        helix_minor_radius_mm=0.0,
        helix_turns=1,
    )
    return prepare_parametric_trajectory(
        4,
        centre,
        params,
        (int(steps_per_cycle), float(frequency_hz), int(loops), rate),
    )


def delay_dry_run(trajectory: Any, args: argparse.Namespace) -> dict[str, float]:
    sample_hz = float(trajectory.rate.effective_hz or trajectory.rate.requested_hz)
    _, stats = apply_delay_feedforward(
        trajectory.positions,
        sample_hz=sample_hz,
        tau_sec=float(args.delay_tau_ms) * 1.0e-3,
        max_offset_mm=float(args.delay_max_offset_mm),
        periodic=True,
        limit_strategy=str(args.delay_limit_strategy),
    )
    print(
        "[DRY-RUN] Delay command: "
        f"sample={sample_hz:g} Hz, tau={float(args.delay_tau_ms):.3f} ms, "
        f"strategy={stats['limit_strategy']}, "
        f"effective_tau={1000.0 * float(stats['effective_tau_sec']):.3f} ms, "
        f"max |u-r|={stats['observed_max_offset_mm']:.6f} mm, "
        f"RMS |u-r|={stats['rms_offset_mm']:.6f} mm, "
        f"limited={int(stats['limited_points'])}/{len(trajectory.positions)} "
        f"({100.0 * float(stats['limited_fraction']):.2f}%)"
    )
    return stats


def counterbalanced_modes(repeat_index: int) -> tuple[str, str]:
    return ("baseline", "delay") if repeat_index % 2 == 0 else ("delay", "baseline")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if int(args.repeats) <= 0:
        raise SystemExit("--repeats must be positive.")
    if float(args.delay_tau_ms) < 0.0 or float(args.delay_max_offset_mm) <= 0.0:
        raise SystemExit("Delay tau cannot be negative and max offset must be positive.")
    if (
        float(args.delay_max_offset_mm) > 1.0
        and not bool(args.acknowledge_delay_offset_above_1mm)
    ):
        raise SystemExit(
            "An offset limit above 1 mm requires --acknowledge-delay-offset-above-1mm."
        )

    template = make_heart_trajectory(
        (0.0, 0.0, 0.0),
        float(args.heart_scale_mm),
        float(args.heart_frequency_hz),
        int(args.steps_per_cycle),
        int(args.loops),
    )
    delay_stats = delay_dry_run(template, args)
    if float(delay_stats["limited_fraction"]) > 0.0:
        print(
            "[CONTROL][WARN] The configured command is saturated; this tests bounded "
            "delay compensation, not the exact tau=2.05 ms model inverse."
        )
        if bool(args.require_unclipped_delay):
            raise SystemExit("Delay command clips but --require-unclipped-delay was requested.")
    if bool(args.dry_run):
        print("[DRY-RUN] A/B plan validated; no hardware was opened.")
        return 0

    output_root = Path(args.output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    session_path = output_root / f"heart_delay_ab_session_{stamp}.json"
    session_manifest: dict[str, Any] = {
        "schema_version": 1,
        "protocol": "zero-shot heart baseline vs frozen common-delay feedforward",
        "started_at": dt.datetime.now().isoformat(timespec="milliseconds"),
        "heart_scale_mm": float(args.heart_scale_mm),
        "heart_frequency_hz": float(args.heart_frequency_hz),
        "steps_per_cycle": int(args.steps_per_cycle),
        "loops": int(args.loops),
        "repeats": int(args.repeats),
        "delay_tau_ms": float(args.delay_tau_ms),
        "delay_max_offset_mm": float(args.delay_max_offset_mm),
        "delay_limit_strategy": str(args.delay_limit_strategy),
        "delay_dry_run_stats": delay_stats,
        "model_refit_on_heart": False,
        "primary_error": "measured_position - original_heart_reference",
        "status": "running",
        "runs": [],
    }
    atomic_write_json(session_path, session_manifest)

    hardware = open_recording_hardware_session()
    preview_completed = False
    try:
        sequence_index = 0
        for repeat_index in range(int(args.repeats)):
            for mode in counterbalanced_modes(repeat_index):
                sequence_index += 1
                trajectory = make_heart_trajectory(
                    hardware.current_pos,
                    float(args.heart_scale_mm),
                    float(args.heart_frequency_hz),
                    int(args.steps_per_cycle),
                    int(args.loops),
                )
                label = f"{mode}_r{repeat_index + 1:02d}"
                trajectory = replace(trajectory, run_label=label)
                run_args = copy.copy(args)
                run_args.entry_script = Path(__file__).name
                run_args.delay_feedforward = mode == "delay"
                run_args.no_preview = bool(args.no_preview or preview_completed)
                code, run_dir = run_recording(
                    run_args,
                    prepared_trajectory=trajectory,
                    prompt_before_capture=bool(args.confirm_each_run),
                    return_to_centre=True,
                    automation_metadata={
                        "protocol": "heart_delay_ab",
                        "sequence_index": sequence_index,
                        "repeat_number": repeat_index + 1,
                        "condition": mode,
                        "model_refit_on_heart": False,
                    },
                    hardware_session=hardware,
                )
                if not run_args.no_preview:
                    preview_completed = True
                session_manifest["runs"].append(
                    {
                        "sequence_index": sequence_index,
                        "repeat_number": repeat_index + 1,
                        "condition": mode,
                        "exit_code": int(code),
                        "run_dir": "" if run_dir is None else str(run_dir),
                    }
                )
                atomic_write_json(session_path, session_manifest)
                if code != 0:
                    session_manifest["status"] = "failed"
                    session_manifest["finished_at"] = dt.datetime.now().isoformat(
                        timespec="milliseconds"
                    )
                    atomic_write_json(session_path, session_manifest)
                    return int(code)
        session_manifest["status"] = "complete"
        session_manifest["finished_at"] = dt.datetime.now().isoformat(
            timespec="milliseconds"
        )
        atomic_write_json(session_path, session_manifest)
        print(f"[A/B] Session complete: {session_path}")
        return 0
    finally:
        shutdown_recording_hardware_session(hardware)


if __name__ == "__main__":
    raise SystemExit(main())
