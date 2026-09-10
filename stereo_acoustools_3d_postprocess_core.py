#!/usr/bin/env python3
"""Core deferred 2D tracking, stereo triangulation, and ideal comparison workflow."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from stereo_acoustools_3d_common import (
    DEFAULT_PROCESSING_CONFIG,
    PROCESSING_CONFIG_FIELDS,
    apply_manifest_processing_config,
    atomic_write_json,
    validate_calibration,
)


def processing_namespace(
    manifest: Mapping[str, Any],
    overrides: Mapping[str, Any] | argparse.Namespace | None = None,
) -> argparse.Namespace:
    args = argparse.Namespace(**DEFAULT_PROCESSING_CONFIG)
    apply_manifest_processing_config(args, manifest)
    override_values = vars(overrides) if isinstance(overrides, argparse.Namespace) else dict(overrides or {})
    for field in PROCESSING_CONFIG_FIELDS:
        if field in override_values:
            setattr(args, field, override_values[field])
    led_manifest = manifest.get("pat_start_led", {})
    if isinstance(led_manifest, Mapping):
        args.pat_start_led_side = str(led_manifest.get("side", "off"))
        args.pat_start_led_roi = str(led_manifest.get("roi", ""))
    else:
        args.pat_start_led_side = "off"
        args.pat_start_led_roi = ""
    transform_override = override_values.get("camera_to_pat_transform")
    transform_text = str(manifest.get("camera_to_pat_transform", "")).strip()
    args.camera_to_pat_transform = (
        Path(transform_override).resolve()
        if transform_override is not None
        else (Path(transform_text).resolve() if transform_text else None)
    )
    return args


def run_command(command: list[str]) -> None:
    print("[POSTPROCESS] " + subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, check=True)


def processing_commands(
    args: argparse.Namespace,
    run_dir: Path,
    calibration: Path,
    ideal_log: Path,
    initial_time_offset_sec: float,
    *,
    resume: bool = False,
) -> list[list[str]]:
    script_dir = Path(__file__).resolve().parent
    camera_run = run_dir / "stereo_recording"
    process_command = [
        sys.executable,
        str(script_dir / "stereo_process_recording.py"),
        str(camera_run),
        "--stereo-calibration",
        str(calibration),
        "--window-us",
        str(args.window_us),
        "--hop-us",
        str(args.hop_us),
        "--dt-us",
        str(args.dt_us),
        "--max-interp-gap-sec",
        str(args.max_interp_gap_sec),
        "--max-step-px",
        str(args.max_step_px),
        "--roi",
        str(args.roi),
        "--tracking-method",
        str(args.tracking_method),
        "--threshold-count",
        str(args.threshold_count),
        "--min-events",
        str(args.min_events),
        "--min-area",
        str(args.min_area),
        "--min-mass",
        str(args.min_mass),
        "--polarity",
        str(args.polarity),
        "--max-time-gap-sec",
        str(args.max_time_gap_sec),
        "--max-reprojection-error-px",
        str(args.max_reprojection_error_px),
    ]
    if args.render_overlay:
        process_command.append("--render-overlay")
    if resume:
        process_command.append("--resume")
    if args.pat_start_led_side == "left":
        process_command.extend(["--left-mask-roi", str(args.pat_start_led_roi)])
    elif args.pat_start_led_side == "right":
        process_command.extend(["--right-mask-roi", str(args.pat_start_led_roi)])

    stereo_npz = camera_run / "stereo_3d" / "stereo_3d_points.npz"
    comparison_time_search = (
        float(args.auto_time_search_sec)
        if args.pat_start_led_side == "off" or bool(args.refine_led_time)
        else 0.0
    )
    compare_command = [
        sys.executable,
        str(script_dir / "stereo_compare_ideal_3d.py"),
        str(stereo_npz),
        str(ideal_log),
        "--output-dir",
        str(run_dir / "ideal_comparison_3d"),
        "--time-offset-sec",
        f"{initial_time_offset_sec:.12f}",
        "--auto-time-search-sec",
        str(comparison_time_search),
        "--auto-time-step-sec",
        str(args.auto_time_step_sec),
    ]
    if args.camera_to_pat_transform is not None:
        compare_command.extend(
            [
                "--camera-to-pat-transform",
                str(args.camera_to_pat_transform),
                "--spatial-alignment",
                "fixed",
            ]
        )
    else:
        compare_command.extend(["--spatial-alignment", "fit-run"])
    return [process_command, compare_command]


def validate_run_dir(run_dir: Path) -> Path:
    run_dir = run_dir.resolve()
    required = (
        run_dir / "pipeline_manifest.json",
        run_dir / "pat_camera_timing.json",
        run_dir / "stereo_recording" / "left" / "left_events.npz",
        run_dir / "stereo_recording" / "right" / "right_events.npz",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError("Deferred postprocess input is incomplete; missing: " + ", ".join(missing))
    return run_dir


def processing_status(run_dir: Path) -> str:
    try:
        manifest = json.loads((run_dir / "pipeline_manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unknown"
    return str(manifest.get("processing_status", "unknown"))


def resolve_run_artifact(run_dir: Path, configured_path: str | Path) -> Path:
    """Resolve a recorded artifact after its complete run directory was relocated."""
    configured = Path(str(configured_path)).resolve()
    if configured.exists():
        return configured
    relocated = (run_dir.resolve() / Path(str(configured_path)).name).resolve()
    return relocated if relocated.exists() else configured


def run_postprocess(
    run_dir: Path,
    overrides: Mapping[str, Any] | argparse.Namespace | None = None,
    *,
    resume: bool = False,
) -> int:
    run_dir = validate_run_dir(run_dir)
    manifest_path = run_dir / "pipeline_manifest.json"
    timing_path = run_dir / "pat_camera_timing.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    timing = json.loads(timing_path.read_text(encoding="utf-8"))
    args = processing_namespace(manifest, overrides)
    calibration = validate_calibration(
        Path(str(manifest["stereo_calibration"])),
        str(manifest["left_serial"]),
        str(manifest["right_serial"]),
    )
    ideal_log = resolve_run_artifact(run_dir, str(manifest["ideal_log"]))
    initial_offset = float(timing["ideal_start_in_recording_sec"])

    manifest["processing_complete"] = False
    manifest["processing_status"] = "running"
    manifest["processing_started_at"] = dt.datetime.now().isoformat(timespec="milliseconds")
    manifest.pop("processing_error", None)
    atomic_write_json(manifest_path, manifest)
    try:
        for command in processing_commands(
            args,
            run_dir,
            calibration,
            ideal_log,
            initial_offset,
            resume=resume,
        ):
            run_command(command)
    except KeyboardInterrupt:
        manifest["processing_status"] = "interrupted"
        manifest["processing_error"] = "KeyboardInterrupt"
        atomic_write_json(manifest_path, manifest)
        raise
    except Exception as exc:
        manifest["processing_status"] = "failed"
        manifest["processing_error"] = repr(exc)
        manifest["processing_failed_at"] = dt.datetime.now().isoformat(timespec="milliseconds")
        atomic_write_json(manifest_path, manifest)
        raise

    manifest["processing_complete"] = True
    manifest["processing_status"] = "complete"
    manifest["processing_completed_at"] = dt.datetime.now().isoformat(timespec="milliseconds")
    camera_run = run_dir / "stereo_recording"
    manifest["stereo_3d_npz"] = str(camera_run / "stereo_3d" / "stereo_3d_points.npz")
    manifest["ideal_comparison_summary"] = str(
        run_dir / "ideal_comparison_3d" / "stereo_ideal_comparison_summary.json"
    )
    atomic_write_json(manifest_path, manifest)
    print(f"[POSTPROCESS] Complete: {run_dir}")
    return 0
