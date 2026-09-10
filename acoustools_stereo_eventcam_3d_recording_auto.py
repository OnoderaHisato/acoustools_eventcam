#!/usr/bin/env python3
"""JSON-driven automated AcousTools + synchronized stereo 3D recorder."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import math
from pathlib import Path
from typing import Any

from stereo_acoustools_3d_auto_common import (
    DEFAULT_AUTO_JSON,
    DEFAULT_AUTO_OUTPUT_DIR,
    DEFAULT_PREVIEW_DIR,
    automation_metadata,
    load_auto_conditions,
    prepare_auto_trajectory,
    select_conditions,
    write_preview_set,
)
from stereo_acoustools_3d_common import PROCESSING_CONFIG_FIELDS, atomic_write_json
from stereo_acoustools_3d_hf_common import (
    hf_run_selections,
    hf_automation_metadata,
    load_hf_export_runs,
    prepare_hf_trajectory,
    resolve_hf_run_requests,
    select_hf_runs,
)
from stereo_acoustools_3d_recording_core import (
    build_recording_parser,
    open_recording_hardware_session,
    precompute_hologram_playback,
    run_recording,
    shutdown_recording_hardware_session,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_recording_parser()
    parser.description = (
        "Record a JSON list of AcousTools 3D trajectories with synchronized stereo event cameras."
    )
    parser.set_defaults(output_dir=DEFAULT_AUTO_OUTPUT_DIR)
    parser.add_argument("--json", type=Path, default=DEFAULT_AUTO_JSON)
    parser.add_argument(
        "--hf-export-dir",
        type=Path,
        default=None,
        help="Record pre-exported high-frequency identification commands instead of --json.",
    )
    parser.add_argument(
        "--hf-command-scale",
        type=float,
        default=1.0,
        help="Scale exported HF offsets after loading; use 0.5 for the first x-chirp trial.",
    )
    parser.add_argument(
        "--hf-run",
        action="append",
        default=[],
        metavar="LABEL=SCALE",
        help=(
            "Ordered HF export/scale request; repeatable and may name the same export "
            "more than once, for example LABEL=0.5 followed by LABEL=1.0."
        ),
    )
    parser.add_argument(
        "--acknowledge-hf-retention-and-visibility",
        action="store_true",
        help="Required above 50%% HF scale after confirming particle retention and stereo visibility.",
    )
    parser.add_argument(
        "--plan-b-rank",
        type=int,
        default=None,
        help="Select one Plan B1 escalation rank (0-4); use --label for baselines.",
    )
    parser.add_argument(
        "--plan-b-condition",
        default="",
        help="Select one exact Plan B2/B3 condition from trajectory metadata.",
    )
    parser.add_argument(
        "--plan-b-context-json",
        type=Path,
        default=None,
        help=(
            "Operator context keyed by run label. Required for B2 particle identity "
            "and B3 environment/temperature records."
        ),
    )
    parser.add_argument(
        "--acknowledge-plan-b-protocol",
        action="store_true",
        help="Required for Plan B hardware runs after reading the response gate and checkpoints.",
    )
    parser.add_argument(
        "--acknowledge-plan-b-hold-runs",
        action="store_true",
        help="Required for B1 runs marked HOLD after lower ranks retained the particle.",
    )
    parser.add_argument(
        "--acknowledge-step-response-risk",
        action="store_true",
        help=(
            "Required for staircase trap-jump recording after inspecting the exact "
            "step preview and confirming particle/camera clearance."
        ),
    )
    parser.add_argument(
        "--acknowledge-step-response-tier-a-survived",
        action="store_true",
        help="Required before recording tier-B staircase runs.",
    )
    parser.add_argument(
        "--acknowledge-step-response-tier-b-survived",
        action="store_true",
        help="Required before recording tier-C staircase runs.",
    )
    parser.add_argument(
        "--acknowledge-step-response-tier-c-survived",
        action="store_true",
        help="Required before recording the higher-risk tier-D staircase runs.",
    )
    parser.add_argument(
        "--acknowledge-step-response-tier-d-survived",
        action="store_true",
        help="Required before recording the near-boundary tier-E staircase runs.",
    )
    parser.add_argument(
        "--acknowledge-step-response-tier-e-survived",
        action="store_true",
        help="Required before recording tier-F staircase runs near the escape boundary.",
    )
    parser.add_argument(
        "--acknowledge-step-response-tier-f-survived",
        action="store_true",
        help="Required before recording tier-G staircase runs immediately below escape.",
    )
    parser.add_argument(
        "--acknowledge-step-response-tier-g-survived",
        action="store_true",
        help="Required before recording tier-H escape-boundary crossing probes.",
    )
    parser.add_argument(
        "--acknowledge-step-response-escape-boundary-probe",
        action="store_true",
        help=(
            "Required for tier-H commands at or above the estimated escape boundary; "
            "particle ejection and re-levitation are expected outcomes."
        ),
    )
    parser.add_argument(
        "--acknowledge-extended-trajectory-safety",
        action="store_true",
        help=(
            "Required before recording Extended_Random_3D, Chirped_3D, Cusped_3D, "
            "or parametric JSON shapes, "
            "after inspecting previews, scaling, clearance, and particle retention."
        ),
    )
    parser.add_argument(
        "--acknowledge-retention-boundary-risk",
        action="store_true",
        help=(
            "Additionally required for JSON conditions marked risk_level=retention_boundary; "
            "these diagnostic commands may eject the particle."
        ),
    )
    parser.add_argument(
        "--allow-retention-boundary-tail",
        action="store_true",
        help=(
            "Allow multiple retention_boundary conditions only when they form the final "
            "contiguous block. Camera preview and Enter confirmation are forced for each."
        ),
    )
    parser.add_argument("--start-index", type=int, default=0, help="Skip source entries before this index.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum selected conditions; 0 means all.")
    parser.add_argument("--label", action="append", default=[], help="Run only this exact label; repeatable.")
    parser.add_argument(
        "--role",
        action="append",
        default=[],
        choices=["train", "validation", "diagnostic"],
        help="Run only this dataset role; repeatable and available for JSON mode.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Resolve and validate trajectories without hardware.")
    parser.add_argument(
        "--preview-only",
        action="store_true",
        help="Write JSON previews, or list exported HF previews, and exit without hardware.",
    )
    parser.add_argument("--preview-output-dir", type=Path, default=DEFAULT_PREVIEW_DIR)
    parser.add_argument(
        "--preview-each-run",
        action="store_true",
        help="Show the stereo camera preview before every condition instead of once.",
    )
    parser.add_argument(
        "--confirm-each-run",
        action="store_true",
        help="Wait for Enter after moving the particle to each trajectory start.",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="Continue to the next condition after a declined/failed recording.",
    )
    return parser.parse_args(argv)


def _print_run_list(conditions: list[Any]) -> None:
    print(f"[AUTO] Selected {len(conditions)} JSON condition(s).")
    for condition in conditions:
        if condition.trajectory_kind in {"random_3d", "extended_random_3d", "chirped_3d"}:
            params = condition.params
            detail = f"duration={params.duration_sec:g}s, sample={params.sample_hz:g}Hz"
            if condition.trajectory_kind == "random_3d":
                detail += f", frequency={params.f_min_hz:g}-{params.f_max_hz:g}Hz"
            elif condition.trajectory_kind == "extended_random_3d":
                detail += (
                    f", bands={params.low_f_min_hz:g}-{params.low_f_max_hz:g}/"
                    f"{params.mid_f_min_hz:g}-{params.mid_f_max_hz:g}/"
                    f"{params.high_f_min_hz:g}-{params.high_f_max_hz:g}Hz"
                )
            else:
                active_chirps = [
                    f"{axis.upper()}:{getattr(params, axis + '_f_start_hz'):g}->"
                    f"{getattr(params, axis + '_f_end_hz'):g}"
                    for axis in "xyz"
                    if getattr(params, axis + "_amp_mm") > 0.0
                ]
                detail += (
                    f", chirps={','.join(active_chirps)}Hz"
                )
        elif condition.trajectory_kind == "cusped_3d":
            params = condition.params
            detail = (
                f"curve={params.curve}, plane={params.plane}, cusps="
                f"{params.cusp_count if params.curve == 'hypocycloid' else 'fixed'}, "
                f"frequency={params.frequency_hz:g}Hz, loops={params.loops}, "
                f"rounding={params.rounding:g}"
            )
        else:
            params = condition.params
            detail = (
                f"cycles={params.loops}, base_frequency={params.frequency_hz:g}Hz, "
                f"steps={params.steps_per_cycle}"
            )
        print(
            f"[AUTO] JSON #{condition.json_index:03d} {condition.label}: "
            f"group={condition.plan_group or '-'}, shape={condition.shape}, "
            f"role={condition.role}, risk={condition.risk_level}, "
            f"repeat={condition.repeat}, {detail}"
        )


def _print_hf_run_list(conditions: list[Any]) -> None:
    print(f"[HF] Selected {len(conditions)} ordered identification run request(s).")
    for condition in conditions:
        metadata = condition.metadata
        metrics = metadata["metrics"]
        command_scale = float(condition.command_scale)
        if str(metadata.get("kind", "")).lower() == "staircase":
            detail = metadata["generation_detail"]
            prefix = "[STEP]" if metadata.get("step_response_tier") else "[HF][STAIRCASE]"
            tier_detail = (
                f"tier={metadata['step_response_tier']}, "
                if metadata.get("step_response_tier") else ""
            )
            print(
                f"{prefix} Export #{condition.export_index:03d} {condition.name}: "
                f"{tier_detail}axis={metadata['axis']}, "
                f"duration={metadata['duration_sec']:g}s, sample={metadata['sample_hz']:g}Hz, "
                f"holds={detail['n_holds']}, jumps={detail['n_jumps']}, "
                f"offset={metrics['max_abs_offset_mm']['vector'] * command_scale:.3f}mm, "
                f"max_step={detail['max_step_mm'] * command_scale:.3f}mm "
                f"({100.0 * detail['safety']['escape_margin_fraction'] * command_scale:.1f}% "
                "of estimated escape boundary)"
            )
            continue
        print(
            f"[HF] Export #{condition.export_index:03d} {condition.name}: "
            f"kind={metadata['kind']}, axis={metadata['axis']}, "
            f"duration={metadata['duration_sec']:g}s, sample={metadata['sample_hz']:g}Hz, "
            f"offset={metrics['max_abs_offset_mm']['vector'] * command_scale:.6f}mm, "
            f"speed={metrics['max_speed_mm_s']['vector'] * command_scale:.3f}mm/s, "
            f"accel={metrics['max_acceleration_mm_s2']['vector'] * command_scale:.3f}mm/s^2"
        )


def _plan_b_family(condition: Any) -> str:
    metadata = getattr(condition, "metadata", {})
    if str(metadata.get("campaign", "")) != "plan_b_20260825":
        return ""
    return str(metadata.get("measurement_family", "")).strip()


def _load_plan_b_context(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    payload = json.loads(path.resolve().read_text(encoding="utf-8"))
    raw_runs = payload.get("runs", payload) if isinstance(payload, dict) else None
    if not isinstance(raw_runs, dict):
        raise ValueError("Plan B context JSON must be an object or contain a runs object")
    result: dict[str, dict[str, Any]] = {}
    for label, context in raw_runs.items():
        if not isinstance(context, dict):
            raise ValueError(f"Plan B context for {label!r} must be an object")
        result[str(label)] = dict(context)
    return result


def _validate_plan_b_context(
    condition: Any, context: dict[str, Any]
) -> None:
    family = _plan_b_family(condition)
    if family == "plan_b2_z_line_source" and str(
        condition.metadata.get("condition", "")
    ).startswith("particle"):
        missing = [key for key in ("particle_id", "particle_description") if not context.get(key)]
        if missing:
            raise ValueError(
                f"{condition.label}: Plan B2 particle run context is missing {', '.join(missing)}"
            )
    if family == "plan_b3_drift_source":
        missing = [
            key for key in ("actual_warmup_minutes", "shield_state", "hvac_state")
            if context.get(key) in (None, "")
        ]
        has_temperatures = all(
            context.get(key) not in (None, "")
            for key in ("room_temperature_c", "array_surface_temperature_c")
        )
        if not has_temperatures and not context.get("temperature_unavailable_reason"):
            missing.append(
                "room_temperature_c/array_surface_temperature_c or temperature_unavailable_reason"
            )
        if missing:
            raise ValueError(
                f"{condition.label}: Plan B3 run context is missing {', '.join(missing)}"
            )
        try:
            warmup = float(context["actual_warmup_minutes"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{condition.label}: actual_warmup_minutes must be numeric"
            ) from exc
        if not math.isfinite(warmup) or warmup < 0.0:
            raise ValueError(
                f"{condition.label}: actual_warmup_minutes must be finite and non-negative"
            )
        for key in ("shield_state", "hvac_state"):
            if str(context[key]).strip().lower() not in {"on", "off"}:
                raise ValueError(f"{condition.label}: {key} must be 'on' or 'off'")
        if has_temperatures:
            for key in ("room_temperature_c", "array_surface_temperature_c"):
                try:
                    value = float(context[key])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{condition.label}: {key} must be numeric") from exc
                if not math.isfinite(value):
                    raise ValueError(f"{condition.label}: {key} must be finite")


def _condition_args(base_args: argparse.Namespace, processing_config: dict[str, Any] | None) -> argparse.Namespace:
    run_args = copy.copy(base_args)
    for field, value in (processing_config or {}).items():
        if field not in PROCESSING_CONFIG_FIELDS:
            raise ValueError(f"Unknown per-condition processing_config field: {field}")
        setattr(run_args, field, value)
    return run_args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        hf_mode = args.hf_export_dir is not None
        if (args.plan_b_rank is not None or args.plan_b_condition) and not hf_mode:
            raise ValueError("Plan B selectors require --hf-export-dir")
        plan_b_contexts = _load_plan_b_context(args.plan_b_context_json)
        if args.hf_run and not hf_mode:
            raise ValueError("--hf-run requires --hf-export-dir")
        if hf_mode and args.role:
            raise ValueError("--role is available only with JSON trajectory mode")
        if hf_mode:
            source_path, all_conditions = load_hf_export_runs(args.hf_export_dir)
            if args.hf_run:
                if args.label or int(args.start_index) != 0 or int(args.limit) != 0:
                    raise ValueError(
                        "--hf-run cannot be combined with --label, --start-index, or --limit"
                    )
                conditions = resolve_hf_run_requests(all_conditions, args.hf_run)
            else:
                selected_runs = select_hf_runs(
                    all_conditions,
                    start_index=int(args.start_index),
                    limit=int(args.limit),
                    labels=args.label,
                )
                conditions = hf_run_selections(
                    selected_runs, float(args.hf_command_scale)
                )
            if args.plan_b_rank is not None:
                if not 0 <= int(args.plan_b_rank) <= 4:
                    raise ValueError("--plan-b-rank must be from 0 through 4")
                conditions = [
                    condition for condition in conditions
                    if _plan_b_family(condition) == "plan_b1_small_amplitude_multisine"
                    and int(condition.metadata.get("escalation_rank", -999))
                    == int(args.plan_b_rank)
                ]
            if args.plan_b_condition:
                conditions = [
                    condition for condition in conditions
                    if str(condition.metadata.get("condition", ""))
                    == str(args.plan_b_condition)
                ]
            _print_hf_run_list(conditions)
        else:
            source_path, all_conditions = load_auto_conditions(args.json)
            conditions = select_conditions(
                all_conditions,
                start_index=int(args.start_index),
                limit=int(args.limit),
                labels=args.label,
                roles=args.role,
            )
            _print_run_list(conditions)
        if not conditions:
            raise ValueError("No trajectory conditions match the requested selection.")
        step_response_conditions = [
            condition for condition in conditions
            if hf_mode
            and str(condition.metadata.get("kind", "")).lower() == "staircase"
            and str(condition.metadata.get("measurement_family", ""))
            == "step_response_identification"
        ]
        step_response_selected = bool(step_response_conditions)
        plan_b_conditions = [
            condition for condition in conditions if hf_mode and _plan_b_family(condition)
        ]
        plan_b_selected = bool(plan_b_conditions)

        if args.preview_only:
            if hf_mode:
                for condition in conditions:
                    print(f"[HF] Preview: {condition.preview_png}")
                print("[HF] Existing exported previews listed; no hardware was opened.")
            else:
                output = write_preview_set(conditions, args.preview_output_dir)
                print(f"[AUTO] Preview set written: {output}")
            return 0

        # This also validates trajectory physics and the exact 40 kHz PAT divider.
        for condition in conditions:
            if hf_mode:
                prepare_hf_trajectory(
                    condition.run,
                    center_m=(0.0, 0.0, 0.0),
                    command_scale=float(condition.command_scale),
                )
            else:
                prepare_auto_trajectory(condition)
        if args.dry_run:
            mode_label = (
                "step-response identification"
                if step_response_selected
                else "Plan B identification"
                if plan_b_selected
                else "HF identification" if hf_mode else "JSON trajectory"
            )
            print(f"[AUTO] {mode_label} dry run complete; no hardware was opened.")
            return 0
        if plan_b_selected:
            if len(plan_b_conditions) != len(conditions):
                raise ValueError("Plan B and non-Plan-B exports cannot share a hardware session")
            if args.keep_going:
                raise ValueError(
                    "--keep-going is disabled for Plan B; loss, rejection, or capture failure must stop."
                )
            if not args.acknowledge_plan_b_protocol:
                raise ValueError(
                    "Plan B hardware recording requires --acknowledge-plan-b-protocol "
                    "after reading response_check.md and the operator checkpoints."
                )
            families = {_plan_b_family(condition) for condition in plan_b_conditions}
            if len(families) != 1:
                raise ValueError("Select exactly one Plan B measurement family per hardware session")
            family = next(iter(families))
            if family == "plan_b1_small_amplitude_multisine":
                ranks = {
                    int(condition.metadata["escalation_rank"])
                    for condition in plan_b_conditions
                    if "escalation_rank" in condition.metadata
                }
                baselines_selected = any(
                    "escalation_rank" not in condition.metadata
                    for condition in plan_b_conditions
                )
                if len(ranks) > 1 or (baselines_selected and ranks):
                    raise ValueError(
                        "Plan B1 hardware sessions may contain baselines or exactly one "
                        "escalation rank. Use --plan-b-rank or exact --label selections."
                    )
                if any(
                    str(condition.metadata.get("response_gate", "")).lower() == "hold"
                    for condition in plan_b_conditions
                ) and not args.acknowledge_plan_b_hold_runs:
                    raise ValueError(
                        "Selected Plan B1 HOLD runs require --acknowledge-plan-b-hold-runs "
                        "after all lower ranks retained the particle."
                    )
            elif family == "plan_b2_z_line_source":
                selected_conditions = {
                    str(condition.metadata.get("condition", ""))
                    for condition in plan_b_conditions
                }
                if len(selected_conditions) != 1:
                    raise ValueError(
                        "Plan B2 hardware sessions must select one drive or particle condition "
                        "with --plan-b-condition."
                    )
            elif family == "plan_b3_drift_source" and len(plan_b_conditions) != 1:
                raise ValueError(
                    "Plan B3 environment conditions must be recorded one run per hardware session."
                )
            for condition in plan_b_conditions:
                context = plan_b_contexts.get(condition.label, {})
                _validate_plan_b_context(condition, context)
        if (
            hf_mode
            and any(
                float(condition.command_scale) > 0.5
                and str(condition.metadata.get("kind", "")).lower()
                not in {"static", "staircase"}
                for condition in conditions
            )
            and not args.acknowledge_hf_retention_and_visibility
        ):
            raise ValueError(
                "HF commands above 50% scale require "
                "--acknowledge-hf-retention-and-visibility after inspecting the 50% x-chirp trial."
            )
        if step_response_selected:
            if args.keep_going:
                raise ValueError(
                    "--keep-going is disabled for staircase step-response recording; "
                    "a lost particle, declined checkpoint, or failed capture must stop the session."
                )
            if not args.acknowledge_step_response_risk:
                raise ValueError(
                    "Staircase trap jumps require --acknowledge-step-response-risk after "
                    "inspecting the exact exported previews. A stereo preview and Enter "
                    "confirmation will still be forced before every run."
                )
            selected_tiers = {
                str(condition.metadata["step_response_tier"]).upper()
                for condition in step_response_conditions
            }
            if "B" in selected_tiers and not args.acknowledge_step_response_tier_a_survived:
                raise ValueError(
                    "Tier-B staircase recording requires "
                    "--acknowledge-step-response-tier-a-survived."
                )
            if "C" in selected_tiers and not args.acknowledge_step_response_tier_b_survived:
                raise ValueError(
                    "Tier-C staircase recording requires "
                    "--acknowledge-step-response-tier-b-survived."
                )
            if "D" in selected_tiers:
                if not args.acknowledge_step_response_tier_c_survived:
                    raise ValueError(
                        "Tier-D staircase recording requires "
                        "--acknowledge-step-response-tier-c-survived."
                    )
            if "E" in selected_tiers:
                if not args.acknowledge_step_response_tier_d_survived:
                    raise ValueError(
                        "Tier-E staircase recording requires "
                        "--acknowledge-step-response-tier-d-survived."
                    )
            if "F" in selected_tiers:
                if not args.acknowledge_step_response_tier_e_survived:
                    raise ValueError(
                        "Tier-F staircase recording requires "
                        "--acknowledge-step-response-tier-e-survived."
                    )
            if "G" in selected_tiers:
                if not args.acknowledge_step_response_tier_f_survived:
                    raise ValueError(
                        "Tier-G staircase recording requires "
                        "--acknowledge-step-response-tier-f-survived."
                    )
            if "H" in selected_tiers:
                if not args.acknowledge_step_response_tier_g_survived:
                    raise ValueError(
                        "Tier-H staircase recording requires "
                        "--acknowledge-step-response-tier-g-survived."
                    )
                if not args.acknowledge_step_response_escape_boundary_probe:
                    raise ValueError(
                        "Tier-H staircase recording crosses the estimated escape boundary "
                        "and requires --acknowledge-step-response-escape-boundary-probe."
                    )
                if len(conditions) != 1:
                    raise ValueError(
                        "Tier-H escape-boundary recording is limited to exactly one selected "
                        "run per hardware session. Use --label with one exact exported run name."
                    )
        if (
            not hf_mode
            and any(condition.trajectory_kind != "random_3d" for condition in conditions)
            and not args.acknowledge_extended_trajectory_safety
        ):
            raise ValueError(
                "Extended/chirped/cusped/parametric JSON trajectories require "
                "--acknowledge-extended-trajectory-safety after preview inspection."
            )
        retention_boundary_selected = (
            not hf_mode
            and any(condition.risk_level == "retention_boundary" for condition in conditions)
        )
        if retention_boundary_selected:
            if args.keep_going:
                raise ValueError(
                    "--keep-going is disabled when retention-boundary conditions are selected; "
                    "an aborted preview, lost particle, or failed capture must stop the session."
                )
            if not args.acknowledge_retention_boundary_risk:
                raise ValueError(
                    "Retention-boundary diagnostics may eject the particle and require "
                    "--acknowledge-retention-boundary-risk. Run them one at a time in increasing "
                    "severity with --confirm-each-run."
                )
            retention_indices = [
                index for index, condition in enumerate(conditions)
                if condition.risk_level == "retention_boundary"
            ]
            if len(retention_indices) == 1 and len(conditions) == 1:
                if not args.confirm_each_run and not args.allow_retention_boundary_tail:
                    raise ValueError(
                        "A single retention-boundary hardware run requires --confirm-each-run "
                        "or --allow-retention-boundary-tail."
                    )
            else:
                if not args.allow_retention_boundary_tail:
                    raise ValueError(
                        "Multiple retention-boundary conditions require "
                        "--allow-retention-boundary-tail. Each condition will force a camera "
                        "preview and Enter confirmation."
                    )
                first_retention = retention_indices[0]
                if retention_indices != list(range(first_retention, len(conditions))):
                    raise ValueError(
                        "Retention-boundary conditions must form one contiguous suffix after "
                        "all standard/challenge conditions."
                    )

        precomputed_b3: dict[str, tuple[Any, Any]] = {}
        if (
            plan_b_selected
            and _plan_b_family(plan_b_conditions[0]) == "plan_b3_drift_source"
        ):
            print(
                "[AUTO][B3] Precomputing the selected static hologram before "
                "starting non-zero PAT output...",
                flush=True,
            )
            for condition in plan_b_conditions:
                trajectory = prepare_hf_trajectory(
                    condition.run,
                    center_m=(0.0, 0.0, 0.0),
                    command_scale=float(condition.command_scale),
                )
                precomputed_b3[condition.label] = (
                    trajectory,
                    precompute_hologram_playback(trajectory),
                )

        output_root = Path(args.output_dir).resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        session_stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        session_path = output_root / f"auto_recording_session_{session_stamp}.json"
        session: dict[str, Any] = {
            "schema_version": 1,
            "started_at": dt.datetime.now().isoformat(timespec="milliseconds"),
            "script": Path(__file__).name,
            "trajectory_mode": (
                "step_response_identification"
                if step_response_selected and len(step_response_conditions) == len(conditions)
                else "plan_b_identification" if plan_b_selected
                else "high_frequency_identification" if hf_mode else "json_3d"
            ),
            "output_dir": str(output_root),
            "hardware_connection_scope": "entire_auto_session",
            "status": "running",
            "runs": [],
        }
        if hf_mode:
            session["source_export_manifest"] = str(source_path)
            session["hf_run_requests"] = [
                {"label": condition.label, "command_scale": float(condition.command_scale)}
                for condition in conditions
            ]
            session["delay_feedforward_applied"] = False
            if plan_b_selected:
                session["plan_b_context_json"] = (
                    str(args.plan_b_context_json.resolve())
                    if args.plan_b_context_json is not None else ""
                )
                session["plan_b_run_contexts"] = {
                    condition.label: plan_b_contexts.get(condition.label, {})
                    for condition in plan_b_conditions
                }
        else:
            session["source_json"] = str(source_path)
            session["retention_boundary_tail_mode"] = bool(
                retention_boundary_selected and args.allow_retention_boundary_tail
            )
        atomic_write_json(session_path, session)

        try:
            if precomputed_b3:
                _trajectory, initial_playback = next(iter(precomputed_b3.values()))
                hardware_session = open_recording_hardware_session(
                    initial_hologram=initial_playback.holograms[0],
                    drive_amplitude_scale=initial_playback.drive_amplitude_scale,
                )
            else:
                hardware_session = open_recording_hardware_session()
        except Exception as exc:
            session["status"] = "failed"
            session["hardware_session_error"] = repr(exc)
            session["finished_at"] = dt.datetime.now().isoformat(timespec="milliseconds")
            atomic_write_json(session_path, session)
            print(f"[AUTO][ERROR] Could not open AcousTools/OpenMPD session: {exc}")
            return 1

        session["hardware_session_opened_at"] = dt.datetime.now().isoformat(timespec="milliseconds")
        session["active_output_started_perf_ns"] = getattr(
            hardware_session, "active_output_started_perf_ns", None
        )
        session["active_output_started_wall_ns"] = getattr(
            hardware_session, "active_output_started_wall_ns", None
        )
        atomic_write_json(session_path, session)
        try:
            overall_code = 0
            preview_completed = False
            for condition in conditions:
                for repeat_index in range(condition.repeat):
                    if hf_mode:
                        if condition.label in precomputed_b3:
                            trajectory, prepared_playback = precomputed_b3[
                                condition.label
                            ]
                        else:
                            trajectory = prepare_hf_trajectory(
                                condition.run,
                                center_m=hardware_session.current_pos,
                                command_scale=float(condition.command_scale),
                            )
                            prepared_playback = None
                        run_automation_metadata = hf_automation_metadata(
                            condition.run, source_path, float(condition.command_scale)
                        )
                        if _plan_b_family(condition):
                            run_automation_metadata["operator_context"] = dict(
                                plan_b_contexts.get(condition.label, {})
                            )
                    else:
                        # Every automatic condition returns to the PAT origin,
                        # so JSON trajectories are resolved in that stable
                        # coordinate frame just as in the original workflow.
                        trajectory = prepare_auto_trajectory(condition, repeat_index)
                        run_automation_metadata = automation_metadata(
                            condition, source_path, repeat_index
                        )
                        prepared_playback = None
                    run_args = _condition_args(args, condition.processing_config)
                    run_args.entry_script = Path(__file__).name
                    step_checkpoint = bool(
                        hf_mode
                        and str(condition.metadata.get("kind", "")).lower() == "staircase"
                        and str(condition.metadata.get("measurement_family", ""))
                        == "step_response_identification"
                    )
                    plan_b_checkpoint = bool(hf_mode and _plan_b_family(condition))
                    retention_checkpoint = bool(
                        not hf_mode and condition.risk_level == "retention_boundary"
                    )
                    # The core opens the camera preview only after PAT is holding the particle at centre.
                    run_args.no_preview = (
                        False if (retention_checkpoint or step_checkpoint or plan_b_checkpoint) else bool(
                            args.no_preview
                            or (preview_completed and not args.preview_each_run)
                        )
                    )
                    source_label = (
                        "step-response export"
                        if step_checkpoint
                        else "Plan B export" if plan_b_checkpoint
                        else "HF export" if hf_mode else "JSON"
                    )
                    if step_checkpoint:
                        print(
                            "\n[AUTO][STEP CHECKPOINT] Stereo preview and explicit Enter "
                            "confirmation are mandatory. Abort if the particle was lost, is "
                            "not stable at centre, or is not visible in both cameras."
                        )
                    if retention_checkpoint:
                        print(
                            "\n[AUTO][RETENTION CHECKPOINT] Stereo preview and explicit "
                            "Enter confirmation are mandatory. Abort if the particle is not "
                            "retained and visible."
                        )
                    if plan_b_checkpoint:
                        print(
                            "\n[AUTO][PLAN B CHECKPOINT] Confirm the named condition, drive "
                            "scale, particle/environment context, particle retention, and "
                            "visibility in both cameras before pressing Enter."
                        )
                    print(
                        f"\n[AUTO] Recording {source_label} #{condition.json_index:03d} "
                        f"{condition.label} ({repeat_index + 1}/{condition.repeat})"
                    )
                    try:
                        code, run_dir = run_recording(
                            run_args,
                            prepared_trajectory=trajectory,
                            prompt_before_capture=bool(
                                args.confirm_each_run
                                or retention_checkpoint
                                or step_checkpoint
                                or plan_b_checkpoint
                            ),
                            return_to_centre=True,
                            automation_metadata=run_automation_metadata,
                            hardware_session=hardware_session,
                            precomputed_playback=prepared_playback,
                            active_warmup_target_minutes=(
                                float(
                                    plan_b_contexts[condition.label][
                                        "actual_warmup_minutes"
                                    ]
                                )
                                if _plan_b_family(condition)
                                == "plan_b3_drift_source"
                                else None
                            ),
                        )
                        error = ""
                    except KeyboardInterrupt:
                        code, run_dir, error = 130, None, "KeyboardInterrupt"
                    except SystemExit as exc:
                        raw_code = exc.code if isinstance(exc.code, int) else 1
                        code, run_dir, error = int(raw_code), None, str(exc)
                        print(f"[AUTO][ERROR] {condition.label}: {exc}")
                    except Exception as exc:
                        code, run_dir, error = 1, None, repr(exc)
                        print(f"[AUTO][ERROR] {condition.label}: {exc}")
                    if not run_args.no_preview:
                        preview_completed = True
                    session["runs"].append(
                        {
                            **run_automation_metadata,
                            "exit_code": int(code),
                            "run_dir": "" if run_dir is None else str(run_dir),
                            "error": error,
                            "active_warmup": dict(
                                getattr(hardware_session, "last_active_warmup", {})
                            ),
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
            print(f"[AUTO] Recording session complete: {session_path}")
            return overall_code
        finally:
            shutdown_recording_hardware_session(hardware_session)
            session["hardware_session_closed_at"] = dt.datetime.now().isoformat(timespec="milliseconds")
            atomic_write_json(session_path, session)
    except ValueError as exc:
        print(f"[AUTO][ERROR] {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
