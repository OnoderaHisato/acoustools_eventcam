#!/usr/bin/env python3
"""B1: baseline vs second-order-inverse feedforward A/B on the long_random_3d set.

For every selected JSON trajectory condition this records --pairs OFF/ON pairs
in counterbalanced order within one hardware session:

    condition 0: OFF ON | ON OFF | OFF ON
    condition 1: ON OFF | OFF ON | ON OFF
    ...

OFF  = baseline command u = r (identical to the 2026-08-07 auto session).
ON   = design C, per axis: u = r(t+tau) + (r'' + gamma r')(t+tau) / w0^2,
       |u-r| <= --ff-max-offset-mm (vector norm).

The evaluation reference r(t) is written to *_ideal_log.csv in BOTH conditions,
so the existing stereo postprocess (measured - reference) needs no change.

Zero-shot: do not refit f0/gamma/tau on these runs when reporting the result.
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

from inverse2nd_feedforward import apply_inverse2nd_feedforward
from stereo_acoustools_3d_auto_common import (
    DEFAULT_AUTO_JSON,
    automation_metadata,
    load_auto_conditions,
    prepare_auto_trajectory,
    select_conditions,
)
from stereo_acoustools_3d_common import PROCESSING_CONFIG_FIELDS, atomic_write_json
from stereo_acoustools_b1_recording_core import (
    build_recording_parser,
    ff_phys_axes_from_args,
    open_recording_hardware_session,
    run_recording,
    shutdown_recording_hardware_session,
)

MODES = ("baseline", "inverse2nd")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_recording_parser()
    parser.description = (
        "B1 zero-shot A/B test: baseline vs per-axis second-order inverse feedforward "
        "on the long_random_3d JSON trajectory set."
    )
    parser.set_defaults(output_dir=Path("stereo_b1_ff_ab_records"))
    parser.add_argument("--json", type=Path, default=DEFAULT_AUTO_JSON)
    parser.add_argument("--start-index", type=int, default=0, help="Skip JSON entries before this index.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum selected conditions; 0 means all.")
    parser.add_argument("--label", action="append", default=[], help="Run only this exact label; repeatable.")
    parser.add_argument("--pairs", type=int, default=3, help="OFF/ON pairs per trajectory condition.")
    parser.add_argument("--dry-run", action="store_true", help="Validate trajectories and feedforward without hardware.")
    parser.add_argument("--confirm-each-run", action="store_true", help="Wait for Enter before each capture.")
    parser.add_argument("--preview-each-run", action="store_true", help="Camera preview before every run instead of once.")
    parser.add_argument("--keep-going", action="store_true", help="Continue after a failed run instead of aborting the session.")
    args = parser.parse_args(argv)
    if bool(args.delay_feedforward) or bool(args.inverse2nd_feedforward):
        raise SystemExit(
            "Do not pass --delay-feedforward/--inverse2nd-feedforward here; "
            "this script sets the control mode per run."
        )
    if int(args.pairs) <= 0:
        raise SystemExit("--pairs must be positive.")
    return args


def pair_mode_order(condition_index: int, pair_index: int) -> tuple[str, str]:
    """Counterbalance across both pairs and conditions."""
    if (condition_index + pair_index) % 2 == 0:
        return ("baseline", "inverse2nd")
    return ("inverse2nd", "baseline")


def feedforward_preflight(trajectory: Any, args: argparse.Namespace) -> dict[str, Any]:
    sample_hz = float(trajectory.rate.effective_hz or trajectory.rate.requested_hz)
    _, stats = apply_inverse2nd_feedforward(
        trajectory.positions,
        sample_hz=sample_hz,
        phys_axes=ff_phys_axes_from_args(args),
        max_offset_mm=float(args.ff_max_offset_mm),
        limit_strategy=str(args.ff_limit_strategy),
        sg_window_ms=float(args.ff_sg_window_ms),
    )
    print(
        "[PREFLIGHT] inverse2nd: "
        f"max |u-r|={stats['observed_max_offset_mm']:.4f} mm "
        f"(raw {stats['raw_max_offset_mm']:.4f} mm), "
        f"rms |u-r|={stats['rms_offset_mm']:.4f} mm, "
        f"limited={int(stats['limited_points'])}/{len(trajectory.positions)} "
        f"({100.0 * float(stats['limited_fraction']):.2f}%)"
    )
    return dict(stats)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source_path, all_conditions = load_auto_conditions(args.json)
    conditions = select_conditions(
        all_conditions,
        start_index=int(args.start_index),
        limit=int(args.limit),
        labels=args.label,
    )
    if not conditions:
        raise SystemExit("No trajectory conditions match the requested selection.")
    print(f"[B1] Selected {len(conditions)} condition(s), {int(args.pairs)} OFF/ON pair(s) each "
          f"-> {2 * int(args.pairs) * len(conditions)} recordings.")
    for condition in conditions:
        params = condition.params
        print(
            f"[B1] JSON #{condition.json_index:03d} {condition.label}: "
            f"duration={params.duration_sec:g}s, sample={params.sample_hz:g}Hz"
        )

    # Validate every trajectory and its feedforward command before hardware.
    preflight: dict[str, dict[str, Any]] = {}
    for condition in conditions:
        trajectory = prepare_auto_trajectory(condition)
        print(f"[PREFLIGHT] {condition.label}: {len(trajectory.positions)} steps")
        preflight[condition.label] = feedforward_preflight(trajectory, args)
    if args.dry_run:
        print("[B1] Dry run complete; no hardware was opened.")
        return 0

    output_root = Path(args.output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    session_path = output_root / f"b1_ff_ab_session_{stamp}.json"
    phys = ff_phys_axes_from_args(args)
    session: dict[str, Any] = {
        "schema_version": 1,
        "protocol": "b1_longrandom_inverse2nd_ab",
        "started_at": dt.datetime.now().isoformat(timespec="milliseconds"),
        "script": Path(__file__).name,
        "source_json": str(source_path),
        "pairs_per_condition": int(args.pairs),
        "control_on": {
            "mode": "inverse2nd_feedforward",
            "equation": "u = r(t+tau) + (r'' + gamma r')(t+tau) / w0^2 per axis",
            "f0_hz": [p[0] for p in phys],
            "gamma_1_per_s": [p[1] for p in phys],
            "tau_ms": [p[2] * 1e3 for p in phys],
            "max_offset_mm": float(args.ff_max_offset_mm),
            "limit_strategy": str(args.ff_limit_strategy),
            "sg_window_ms": float(args.ff_sg_window_ms),
            "zero_shot": True,
        },
        "control_off": {"mode": "baseline", "equation": "u = r"},
        "evaluation": "measured - reference r(t), t >= 0.5 s, per axis rms; ideal_log holds r in both conditions",
        "preflight": preflight,
        "status": "running",
        "runs": [],
    }
    atomic_write_json(session_path, session)

    hardware = open_recording_hardware_session()
    preview_completed = False
    overall_code = 0
    try:
        sequence_index = 0
        for condition_index, condition in enumerate(conditions):
            for pair_index in range(int(args.pairs)):
                for mode in pair_mode_order(condition_index, pair_index):
                    sequence_index += 1
                    trajectory = prepare_auto_trajectory(condition, pair_index)
                    trajectory = replace(
                        trajectory,
                        run_label="_".join(
                            part
                            for part in (
                                trajectory.run_label,
                                f"p{pair_index + 1:02d}",
                                "off" if mode == "baseline" else "on",
                            )
                            if part
                        ),
                    )
                    run_args = copy.copy(args)
                    for field, value in (condition.processing_config or {}).items():
                        if field not in PROCESSING_CONFIG_FIELDS:
                            raise SystemExit(f"Unknown per-condition processing_config field: {field}")
                        setattr(run_args, field, value)
                    run_args.entry_script = Path(__file__).name
                    run_args.inverse2nd_feedforward = mode == "inverse2nd"
                    run_args.delay_feedforward = False
                    run_args.no_preview = bool(
                        args.no_preview or (preview_completed and not args.preview_each_run)
                    )
                    metadata = dict(automation_metadata(condition, source_path, pair_index))
                    metadata.update(
                        {
                            "protocol": "b1_longrandom_inverse2nd_ab",
                            "sequence_index": sequence_index,
                            "pair_number": pair_index + 1,
                            "condition": mode,
                        }
                    )
                    print(
                        f"\n[B1] Recording #{sequence_index:03d} {condition.label} "
                        f"pair {pair_index + 1}/{int(args.pairs)} mode={mode}"
                    )
                    try:
                        code, run_dir = run_recording(
                            run_args,
                            prepared_trajectory=trajectory,
                            prompt_before_capture=bool(args.confirm_each_run),
                            return_to_centre=True,
                            automation_metadata=metadata,
                            hardware_session=hardware,
                        )
                        error = ""
                    except KeyboardInterrupt:
                        code, run_dir, error = 130, None, "KeyboardInterrupt"
                    except SystemExit as exc:
                        raw_code = exc.code if isinstance(exc.code, int) else 1
                        code, run_dir, error = int(raw_code), None, str(exc)
                        print(f"[B1][ERROR] {condition.label}: {exc}")
                    except Exception as exc:
                        code, run_dir, error = 1, None, repr(exc)
                        print(f"[B1][ERROR] {condition.label}: {exc}")
                    if not run_args.no_preview:
                        preview_completed = True
                    session["runs"].append(
                        {
                            "sequence_index": sequence_index,
                            "label": condition.label,
                            "json_index": condition.json_index,
                            "pair_number": pair_index + 1,
                            "condition": mode,
                            "exit_code": int(code),
                            "run_dir": "" if run_dir is None else str(run_dir),
                            "error": error,
                            "finished_at": dt.datetime.now().isoformat(timespec="milliseconds"),
                        }
                    )
                    atomic_write_json(session_path, session)
                    if code != 0:
                        overall_code = int(code)
                        if code == 130 or not args.keep_going:
                            session["status"] = "interrupted" if code == 130 else "failed"
                            session["finished_at"] = dt.datetime.now().isoformat(timespec="milliseconds")
                            atomic_write_json(session_path, session)
                            return overall_code
        session["status"] = "complete" if overall_code == 0 else "complete_with_errors"
        session["finished_at"] = dt.datetime.now().isoformat(timespec="milliseconds")
        atomic_write_json(session_path, session)
        print(f"[B1] Session complete: {session_path}")
        return overall_code
    finally:
        shutdown_recording_hardware_session(hardware)


if __name__ == "__main__":
    raise SystemExit(main())
