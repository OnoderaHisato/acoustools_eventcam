#!/usr/bin/env python3
"""Zero-shot baseline vs second-order-inverse (design C) A/B on a heart trajectory.

Visual companion to acoustools_stereo_b1_ff_ab.py: does the heart render
cleanly with the inverse-2nd feedforward ON?  Counterbalanced OFF/ON pairs
within one hardware session:

    pair 1: OFF ON      pair 2: ON OFF      ...

OFF  = baseline command u = r (the plain heart, same as the 2026-08 delay A/B).
ON   = design C, per axis: u = r(t+tau) + (r'' + gamma r')(t+tau) / w0^2,
       periodic wrap over the closed heart cycle, |u-r| <= --ff-max-offset-mm.

The evaluation reference r(t) is written to *_ideal_log.csv in BOTH conditions.
Zero-shot: do not refit f0/gamma/tau on these runs.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

B1_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = B1_DIR.parent
for module_dir in (PROJECT_ROOT, B1_DIR):
    module_dir_text = str(module_dir)
    if module_dir_text not in sys.path:
        sys.path.insert(0, module_dir_text)

from acoustools_eventcam_sync import compute_pat_frame_rate_info
from acoustools_multitraj_no_eventcam import TrajectoryParameters
from inverse2nd_feedforward import apply_inverse2nd_feedforward
from stereo_acoustools_heart_delay_common import atomic_write_json
from stereo_acoustools_b1_heart_recording_core import (
    build_recording_parser,
    ff_phys_axes_from_args,
    open_recording_hardware_session,
    prepare_parametric_trajectory,
    run_recording,
    shutdown_recording_hardware_session,
)

MODES = ("baseline", "inverse2nd")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_recording_parser()
    parser.description = (
        "Zero-shot A/B test of the design-C second-order inverse feedforward on a heart trajectory."
    )
    parser.set_defaults(output_dir=Path("stereo_b1_heart_ab_records"))
    parser.add_argument("--heart-scale-mm", type=float, default=7.0)
    parser.add_argument("--heart-frequency-hz", type=float, default=10.0)
    parser.add_argument("--steps-per-cycle", type=int, default=800)
    parser.add_argument("--loops", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=2, help="Number of OFF/ON pairs.")
    parser.add_argument("--dry-run", action="store_true", help="Validate the command without hardware.")
    parser.add_argument("--confirm-each-run", action="store_true")
    parser.add_argument(
        "--require-unclipped-ff",
        action="store_true",
        help="Abort before hardware when any u-r sample reaches the offset limit.",
    )
    args = parser.parse_args(argv)
    if bool(args.delay_feedforward) or bool(args.inverse2nd_feedforward):
        raise SystemExit(
            "Do not pass --delay-feedforward/--inverse2nd-feedforward here; "
            "this script sets the control mode per run."
        )
    if int(args.repeats) <= 0:
        raise SystemExit("--repeats must be positive.")
    return args


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
    trajectory = prepare_parametric_trajectory(
        4,
        centre,
        params,
        (int(steps_per_cycle), float(frequency_hz), int(loops), rate),
    )
    spans_m = [
        max(position[axis] for position in trajectory.positions)
        - min(position[axis] for position in trajectory.positions)
        for axis in range(3)
    ]
    if max(spans_m) <= 1.0e-9:
        raise RuntimeError(
            "Generated heart trajectory is degenerate; verify amp_scale propagation."
        )
    print(
        "[TRAJECTORY] Heart span: "
        + ", ".join(
            f"{axis}={span_m * 1.0e3:.3f} mm"
            for axis, span_m in zip("xyz", spans_m)
        )
    )
    return trajectory


def feedforward_preflight(trajectory: Any, args: argparse.Namespace) -> dict[str, Any]:
    sample_hz = float(trajectory.rate.effective_hz or trajectory.rate.requested_hz)
    _, stats = apply_inverse2nd_feedforward(
        trajectory.positions,
        sample_hz=sample_hz,
        phys_axes=ff_phys_axes_from_args(args),
        max_offset_mm=float(args.ff_max_offset_mm),
        limit_strategy=str(args.ff_limit_strategy),
        sg_window_ms=float(args.ff_sg_window_ms),
        periodic=bool(trajectory.closed_cycle),
    )
    print(
        "[PREFLIGHT] inverse2nd (periodic heart cycle): "
        f"sample={sample_hz:g} Hz, "
        f"max |u-r|={stats['observed_max_offset_mm']:.4f} mm "
        f"(raw {stats['raw_max_offset_mm']:.4f} mm), "
        f"rms |u-r|={stats['rms_offset_mm']:.4f} mm, "
        f"limited={int(stats['limited_points'])}/{len(trajectory.positions)} "
        f"({100.0 * float(stats['limited_fraction']):.2f}%)"
    )
    if float(stats["limited_fraction"]) > 0.0:
        print(
            "[CONTROL][WARN] The command saturates the offset limit; this tests bounded "
            "compensation, not the exact model inverse. Reduce --heart-frequency-hz or "
            "--heart-scale-mm for an unclipped command."
        )
        if bool(args.require_unclipped_ff):
            raise SystemExit("Feedforward command clips but --require-unclipped-ff was requested.")
    return dict(stats)


def counterbalanced_modes(repeat_index: int) -> tuple[str, str]:
    return MODES if repeat_index % 2 == 0 else tuple(reversed(MODES))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    template = make_heart_trajectory(
        (0.0, 0.0, 0.0),
        float(args.heart_scale_mm),
        float(args.heart_frequency_hz),
        int(args.steps_per_cycle),
        int(args.loops),
    )
    preflight = feedforward_preflight(template, args)
    if bool(args.dry_run):
        print("[DRY-RUN] A/B plan validated; no hardware was opened.")
        return 0

    output_root = Path(args.output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    session_path = output_root / f"b1_heart_ab_session_{stamp}.json"
    phys = ff_phys_axes_from_args(args)
    session_manifest: dict[str, Any] = {
        "schema_version": 1,
        "protocol": "b1_heart_inverse2nd_ab",
        "started_at": dt.datetime.now().isoformat(timespec="milliseconds"),
        "script": Path(__file__).name,
        "heart_scale_mm": float(args.heart_scale_mm),
        "heart_frequency_hz": float(args.heart_frequency_hz),
        "steps_per_cycle": int(args.steps_per_cycle),
        "loops": int(args.loops),
        "repeats": int(args.repeats),
        "control_on": {
            "mode": "inverse2nd_feedforward",
            "equation": "u = r(t+tau) + (r'' + gamma r')(t+tau) / w0^2 per axis, periodic",
            "f0_hz": [p[0] for p in phys],
            "gamma_1_per_s": [p[1] for p in phys],
            "tau_ms": [p[2] * 1e3 for p in phys],
            "max_offset_mm": float(args.ff_max_offset_mm),
            "limit_strategy": str(args.ff_limit_strategy),
            "sg_window_ms": float(args.ff_sg_window_ms),
            "zero_shot": True,
        },
        "control_off": {"mode": "baseline", "equation": "u = r"},
        "preflight": preflight,
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
                run_args.inverse2nd_feedforward = mode == "inverse2nd"
                run_args.delay_feedforward = False
                run_args.no_preview = bool(args.no_preview or preview_completed)
                code, run_dir = run_recording(
                    run_args,
                    prepared_trajectory=trajectory,
                    prompt_before_capture=bool(args.confirm_each_run),
                    return_to_centre=True,
                    automation_metadata={
                        "protocol": "b1_heart_inverse2nd_ab",
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
