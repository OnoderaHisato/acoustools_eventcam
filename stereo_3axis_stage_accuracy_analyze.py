#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Analyse a three-axis stage sweep directly from stereo displacement.

Stereo reconstruction is expressed in the left-camera frame.  The primary
distance metric is deliberately rotation-free:

    ||p_camera - p_camera_reference|| - ||s_stage - s_stage_reference||

A scale-free rigid rotation from camera axes to stage axes is estimated only
from captures whose role is ``orientation``.  It is used to decompose
``validation`` errors into stage-axis components; it is never used to rescale
the stereo measurements.  Similarity and affine fits are diagnostics only.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from stereo_3axis_stage_accuracy_common import plan_hash as capture_plan_hash


SCRIPT_DIR = Path(__file__).resolve().parent
AXIS_NAMES = ("x", "y", "z")
AXIS_FACE_COLORS = {
    "x": "#1f77b4",
    "y": "#ff7f0e",
    "z": "#2ca02c",
    "multi": "#d62728",
    "return": "#7f7f7f",
}
VALIDATION_ROLE_STYLES = {
    "single_axis": {
        "label": "Single-axis validation",
        "edgecolor": "#111111",
        "marker": "o",
    },
    "depth": {
        "label": "Depth validation",
        "edgecolor": "#cc33aa",
        "marker": "s",
    },
    "diagonal": {
        "label": "Diagonal validation",
        "edgecolor": "#00a6a6",
        "marker": "D",
    },
    "other": {
        "label": "Other validation",
        "edgecolor": "#7f7f7f",
        "marker": "^",
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyse stereo distance accuracy against a three-axis stage.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "session_dir",
        type=Path,
        help="Session containing stereo_3axis_stage_accuracy_manifest.json.",
    )
    parser.add_argument("--manifest", type=Path, default=None, help="Manifest override.")
    parser.add_argument("--config", type=Path, default=None, help="Optional analysis JSON override.")
    parser.add_argument(
        "--stereo-calibration",
        type=Path,
        default=None,
        help="Calibration used when stereo processing is required.",
    )
    parser.add_argument("--skip-processing", action="store_true", help="Do not process missing 3D files.")
    parser.add_argument("--force-processing", action="store_true", help="Re-run stereo processing.")
    parser.add_argument("--continue-on-error", action="store_true", help="Keep analysing after processing failures.")
    parser.add_argument("--output-dir", type=Path, default=None, help="Analysis output directory.")
    parser.add_argument(
        "--watch",
        action="store_true",
        help=(
            "Process newly captured samples while acquisition is running, then "
            "perform the complete analysis after capture finishes successfully."
        ),
    )
    parser.add_argument(
        "--watch-interval-sec",
        type=float,
        default=5.0,
        help="Manifest polling interval used with --watch.",
    )
    return parser.parse_args()


DEFAULTS: dict[str, Any] = {
    "analysis": {
        "minimum_valid_samples": 20,
        "minimum_valid_fraction": 0.8,
        "trim_start_sec": 0.05,
        "trim_end_sec": 0.05,
        "outlier_floor_mm": 0.5,
        "outlier_mad_factor": 6.0,
        "return_position_tolerance_mm": 0.1,
        "group_position_tolerance_mm": 0.1,
        "axis_purity_ratio": 0.1,
        "pixel_sigma_scenarios": [0.1, 0.25, 0.5],
        "theory_coverage_sigma": 1.96,
        "acceptance": {},
    },
    "tracking": {
        "window_us": 500,
        "hop_us": 500,
        "dt_us": 500,
        "roi": "0,0,1280,720",
        "tracking_method": "event_weighted",
        "threshold_count": 1,
        "min_events": 20,
        "min_area": 5,
        "min_mass": 30,
        "polarity": "all",
        "max_time_gap_sec": 0.001,
        "right_time_offset_sec": None,
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"JSON root must be an object: {path}")
    return value


def json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    if isinstance(value, np.ndarray):
        return json_ready(value.tolist())
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, Path):
        return str(value)
    return value


def atomic_replace_with_retry(temporary: Path, path: Path) -> None:
    started = time.monotonic()
    waiting_reported = False
    for retry_index in range(1200):
        try:
            os.replace(temporary, path)
            if waiting_reported:
                print(
                    f"[ANALYSIS][FILE][RECOVERED] Atomic replace succeeded after "
                    f"{time.monotonic() - started:.1f} s: {path}",
                    file=sys.stderr,
                    flush=True,
                )
            return
        except OSError as exc:
            transient_windows_lock = isinstance(exc, PermissionError) or getattr(
                exc, "winerror", None
            ) in {5, 32}
            if not transient_windows_lock or retry_index == 1199:
                raise
            if retry_index == 0 or (retry_index + 1) % 100 == 0:
                waiting_reported = True
                print(
                    f"[ANALYSIS][FILE][WAIT] Windows is temporarily locking {path}; "
                    f"retrying atomic replace ({time.monotonic() - started:.1f} s elapsed).",
                    file=sys.stderr,
                    flush=True,
                )
            time.sleep(0.1)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(json_ready(payload), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    atomic_replace_with_retry(temporary, path)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: (
                        ""
                        if value is None
                        or (
                            isinstance(value, (float, np.floating))
                            and not math.isfinite(float(value))
                        )
                        else value
                    )
                    for key, value in row.items()
                }
            )


def vector3(value: Any, name: str) -> np.ndarray:
    if isinstance(value, dict):
        lowered = {str(key).lower(): item for key, item in value.items()}
        if all(axis in lowered for axis in AXIS_NAMES):
            value = [lowered[axis] for axis in AXIS_NAMES]
    array = np.asarray(value, dtype=np.float64)
    if array.shape != (3,) or not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must be a finite XYZ triplet; got {value!r}.")
    return array


def nested_get(item: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = item
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def first_present(item: dict[str, Any], candidates: Iterable[str | tuple[str, ...]]) -> Any:
    for candidate in candidates:
        if isinstance(candidate, tuple):
            value = nested_get(item, candidate)
        else:
            value = item.get(candidate)
        if value is not None:
            return value
    return None


def resolve_path(value: Any, base_dir: Path) -> Path | None:
    if value is None or not str(value).strip():
        return None
    path = Path(str(value))
    return (path if path.is_absolute() else base_dir / path).resolve()


def selected_attempt(
    raw: dict[str, Any],
    base_dir: Path,
    *,
    require_accepted: bool = False,
) -> dict[str, Any]:
    attempts = raw.get("attempts")
    if not isinstance(attempts, list):
        return {}
    candidates = [item for item in attempts if isinstance(item, dict)]
    if not candidates:
        return {}
    accepted_run_dir = resolve_path(raw.get("accepted_run_dir"), base_dir)
    if accepted_run_dir is not None:
        for attempt in candidates:
            attempt_dir = resolve_path(
                first_present(attempt, ("run_dir", "recording_dir")),
                base_dir,
            )
            if attempt_dir == accepted_run_dir:
                accepted = bool(attempt.get("accepted", False))
                status = str(
                    first_present(
                        attempt,
                        ("status", "capture_status", "result"),
                    )
                    or ""
                ).lower()
                if accepted or status in {
                    "accepted",
                    "success",
                    "complete",
                    "completed",
                    "ok",
                }:
                    return attempt
                if require_accepted:
                    raise SystemExit(
                        "accepted_run_dir points to an attempt that is not accepted."
                    )
    for attempt in reversed(candidates):
        if bool(attempt.get("accepted", False)):
            return attempt
        status = str(
            first_present(attempt, ("status", "capture_status", "result")) or ""
        ).lower()
        if status in {"accepted", "success", "complete", "completed", "ok"}:
            return attempt
    return {} if require_accepted else candidates[-1]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def verify_accepted_artifacts(
    raw: dict[str, Any],
    run_dir: Path,
    capture_id: str,
) -> None:
    hashes = raw.get("accepted_artifact_sha256")
    if hashes is None:
        return
    if not isinstance(hashes, dict) or not hashes:
        raise SystemExit(
            f"Captured sample {capture_id} has an invalid accepted artifact hash table."
        )
    for relative_text, expected in hashes.items():
        relative = Path(str(relative_text))
        if relative.is_absolute() or ".." in relative.parts:
            raise SystemExit(
                f"Captured sample {capture_id} has an unsafe artifact path: {relative}"
            )
        path = (run_dir / relative).resolve()
        try:
            path.relative_to(run_dir.resolve())
        except ValueError as exc:
            raise SystemExit(
                f"Captured sample {capture_id} artifact escapes its run directory."
            ) from exc
        if not path.is_file():
            raise SystemExit(
                f"Captured sample {capture_id} artifact is missing: {path}"
            )
        if file_sha256(path) != str(expected):
            raise SystemExit(
                f"Captured sample {capture_id} artifact hash mismatch: {path}"
            )


def axes_readback(
    container: Any,
    name: str,
    *,
    coordinate: str = "global",
) -> np.ndarray | None:
    if not isinstance(container, dict):
        return None
    axes = container.get("axes", container)
    if not isinstance(axes, dict):
        return None
    if coordinate not in {"global", "hardware"}:
        raise ValueError(f"Unsupported readback coordinate: {coordinate}")
    keys = (
        (
            "global_mm",
            "readback_global_mm",
            "readback_mm",
            "position_global_mm",
            "position_mm",
        )
        if coordinate == "global"
        else (
            "hardware_mm",
            "readback_hardware_mm",
            "position_hardware_mm",
        )
    )
    values: list[float] = []
    for axis in AXIS_NAMES:
        entry = axes.get(axis, axes.get(axis.upper()))
        if isinstance(entry, dict):
            entry = first_present(entry, keys)
        elif coordinate == "hardware":
            # Legacy scalar readbacks are defined as logical/global values.
            return None
        if entry is None:
            return None
        try:
            values.append(float(entry))
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"{name}.{axis} readback is not numeric.") from exc
    array = np.asarray(values, dtype=np.float64)
    if not np.all(np.isfinite(array)):
        raise SystemExit(f"{name} contains a non-finite stage readback.")
    return array


def normalize_manifest(
    manifest: dict[str, Any],
    *,
    manifest_path: Path,
    include_capture_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw_captures = first_present(manifest, ("captures", "samples", "records"))
    if not isinstance(raw_captures, list) or not raw_captures:
        raise SystemExit("Manifest must contain a non-empty captures/samples/records list.")
    if (
        "samples" in manifest
        and manifest.get("plan_hash")
        and capture_plan_hash(raw_captures) != str(manifest["plan_hash"])
    ):
        raise SystemExit("Manifest samples no longer match the immutable plan_hash.")
    base_dir = manifest_path.parent
    formal_manifest = bool(
        "samples" in manifest
        and (
            "configuration_fingerprint" in manifest
            or "plan_hash" in manifest
            or any(
                isinstance(item, dict) and "capture_status" in item
                for item in raw_captures
            )
        )
    )
    global_reference = str(
        first_present(manifest, ("reference_id", "default_reference_id")) or ""
    ).strip()
    rows: list[dict[str, Any]] = []
    skipped_uncaptured = 0
    for index, raw in enumerate(raw_captures):
        if not isinstance(raw, dict):
            raise SystemExit(f"Capture {index} is not an object.")
        capture_id = str(
            first_present(raw, ("capture_id", "sample_id", "id", "name"))
            or f"capture_{index + 1:04d}"
        ).strip()
        formal_capture = formal_manifest
        if formal_capture:
            capture_status = str(raw.get("capture_status", "")).strip().lower()
            if capture_status != "captured":
                skipped_uncaptured += 1
                continue
            if include_capture_ids is not None and capture_id not in include_capture_ids:
                continue
        role = str(first_present(raw, ("role", "data_role", "purpose")) or "").lower()
        if role in {"registration", "orient", "axis_orientation"}:
            role = "orientation"
        if role in {"evaluation", "test", "measure"}:
            role = "validation"
        if role not in {"orientation", "validation"}:
            raise SystemExit(
                f"Capture {capture_id!r} role must be orientation or validation; got {role!r}."
            )
        attempt = selected_attempt(
            raw,
            base_dir,
            require_accepted=formal_capture,
        )
        if formal_capture and not attempt:
            raise SystemExit(
                f"Captured sample {capture_id} has no accepted attempt."
            )
        sample_before = first_present(raw, ("capture_readback_before_all_axes",))
        sample_after = first_present(raw, ("capture_readback_after_all_axes",))
        use_sample_before = isinstance(sample_before, dict) and bool(sample_before)
        use_sample_after = isinstance(sample_after, dict) and bool(sample_after)
        attempt_before = first_present(
            attempt,
            (
                "stage_readback_before_capture",
                # Pre-schema aliases retained for already-created mock sessions.
                "stage_before_capture",
                ("stage", "before_capture"),
            ),
        )
        attempt_after = first_present(
            attempt,
            (
                "stage_readback_after_capture",
                # Pre-schema aliases retained for already-created mock sessions.
                "stage_after_capture",
                ("stage", "after_capture"),
            ),
        )
        before_container = sample_before if use_sample_before else attempt_before
        after_container = sample_after if use_sample_after else attempt_after
        before = axes_readback(
            before_container,
            (
                f"{capture_id}.capture_readback_before_all_axes"
                if use_sample_before
                else f"{capture_id}.stage_readback_before_capture"
            ),
        )
        after = axes_readback(
            after_container,
            (
                f"{capture_id}.capture_readback_after_all_axes"
                if use_sample_after
                else f"{capture_id}.stage_readback_after_capture"
            ),
        )
        before_hardware = axes_readback(
            before_container,
            (
                f"{capture_id}.capture_readback_before_all_axes"
                if use_sample_before
                else f"{capture_id}.stage_readback_before_capture"
            ),
            coordinate="hardware",
        )
        after_hardware = axes_readback(
            after_container,
            (
                f"{capture_id}.capture_readback_after_all_axes"
                if use_sample_after
                else f"{capture_id}.stage_readback_after_capture"
            ),
            coordinate="hardware",
        )
        direct_stage = first_present(
            raw,
            (
                "stage_readback_xyz_mm",
                "stage_readback_mm",
                "readback_stage_mm",
                "readback_xyz_mm",
                ("stage", "readback_xyz_mm"),
                ("stage", "readback_mm"),
                ("stage_arrival", "readback_xyz_mm"),
            ),
        )
        try:
            if before is not None and after is not None:
                stage_readback = 0.5 * (before + after)
                if use_sample_before and use_sample_after:
                    readback_source = (
                        "mean(capture_readback_before_all_axes, "
                        "capture_readback_after_all_axes)"
                    )
                else:
                    readback_source = (
                        "mean(stage_readback_before_capture, "
                        "stage_readback_after_capture)"
                    )
            elif before is not None:
                stage_readback = before
                readback_source = (
                    "capture_readback_before_all_axes"
                    if use_sample_before
                    else "stage_readback_before_capture"
                )
            elif after is not None:
                stage_readback = after
                readback_source = (
                    "capture_readback_after_all_axes"
                    if use_sample_after
                    else "stage_readback_after_capture"
                )
            else:
                stage_readback = vector3(
                    direct_stage,
                    f"{capture_id}.stage_readback_xyz_mm",
                )
                readback_source = "sample-level readback"
            if before_hardware is not None and after_hardware is not None:
                stage_hardware_readback = 0.5 * (
                    before_hardware + after_hardware
                )
            elif before_hardware is not None:
                stage_hardware_readback = before_hardware
            elif after_hardware is not None:
                stage_hardware_readback = after_hardware
            else:
                stage_hardware_readback = np.full(3, np.nan)
            plan_target = vector3(
                first_present(
                    raw,
                    (
                        "plan_target_xyz_mm",
                        "target_xyz_mm",
                        "command_stage_mm",
                        "stage_target_xyz_mm",
                        "planned_stage_xyz_mm",
                        ("plan", "target_xyz_mm"),
                        ("stage", "target_xyz_mm"),
                    ),
                ),
                f"{capture_id}.plan_target_xyz_mm",
            )
        except (TypeError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        run_dir = resolve_path(
            first_present(raw, ("run_dir", "accepted_run_dir", "recording_dir"))
            or first_present(attempt, ("run_dir", "recording_dir")),
            base_dir,
        )
        npz_path = resolve_path(
            first_present(raw, ("stereo_3d_npz", "points_3d_npz", "points_npz"))
            or first_present(
                attempt,
                ("stereo_3d_npz", "points_3d_npz", "points_npz"),
            ),
            base_dir,
        )
        if npz_path is None and run_dir is not None:
            npz_path = run_dir / "stereo_3d" / "stereo_3d_points.npz"
        if formal_capture:
            accepted_run_dir = resolve_path(
                raw.get("accepted_run_dir"),
                base_dir,
            )
            if accepted_run_dir is None or run_dir != accepted_run_dir:
                raise SystemExit(
                    f"Captured sample {capture_id} does not resolve to its "
                    "accepted_run_dir."
                )
            if raw.get("accepted_artifact_sha256") is None:
                raise SystemExit(
                    f"Formal captured sample {capture_id} has no accepted "
                    "raw artifact hash table."
                )
            else:
                verify_accepted_artifacts(raw, run_dir, capture_id)
        reference_id = str(
            first_present(
                raw,
                (
                    "reference_id",
                    "reference_sample_id",
                    "reference_capture_id",
                    "origin_id",
                ),
            )
            or global_reference
        ).strip()
        direction = str(
            first_present(
                raw,
                (
                    "direction",
                    "approach_direction",
                    "pass_direction",
                    "scan_direction",
                ),
            )
            or "unspecified"
        ).strip().lower()
        cycle_value = first_present(raw, ("cycle", "cycle_index", "repeat"))
        try:
            cycle = int(cycle_value) if cycle_value is not None else 0
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"{capture_id}.cycle must be an integer.") from exc
        rows.append(
            {
                "capture_id": capture_id,
                "sequence_index": int(raw.get("sequence_index", index)),
                "role": role,
                "stage_readback_mm": stage_readback,
                "stage_hardware_readback_mm": stage_hardware_readback,
                "stage_before_capture_mm": (
                    before if before is not None else np.full(3, np.nan)
                ),
                "stage_after_capture_mm": (
                    after if after is not None else np.full(3, np.nan)
                ),
                "stage_readback_source": readback_source,
                "stage_drift_during_capture_mm": (
                    after - before
                    if before is not None and after is not None
                    else np.full(3, np.nan)
                ),
                "plan_target_mm": plan_target,
                "trajectory": str(raw.get("trajectory", "")),
                "sweep_name": str(raw.get("sweep_name", "")),
                "data_role": str(raw.get("data_role", "")).strip().lower(),
                "pass_name": str(raw.get("pass_name", "")),
                "run_dir": run_dir,
                "stereo_3d_npz": npz_path,
                "reference_id": reference_id,
                "direction": direction,
                "cycle": cycle,
                "usable": False,
                "reason": "",
                "formal_capture": formal_capture,
                "strict_processing_provenance": formal_capture,
                "accepted_artifact_sha256": raw.get(
                    "accepted_artifact_sha256",
                    {},
                ),
            }
        )
    ids = [row["capture_id"] for row in rows]
    duplicates = sorted({value for value in ids if ids.count(value) > 1})
    if duplicates:
        raise SystemExit(f"Capture IDs must be unique; duplicates: {duplicates}")
    if not global_reference:
        orientation_ids = [
            str(row["capture_id"]) for row in rows if row["role"] == "orientation"
        ]
        global_reference = orientation_ids[0] if orientation_ids else ""
    for row in rows:
        if not row["reference_id"]:
            row["reference_id"] = global_reference
    return rows, {
        "default_reference_id": global_reference,
        "skipped_uncaptured": skipped_uncaptured,
    }


def resolve_calibration(
    args: argparse.Namespace,
    manifest: dict[str, Any],
    manifest_path: Path,
) -> Path | None:
    if args.stereo_calibration is not None:
        return args.stereo_calibration.resolve()
    value = first_present(
        manifest,
        (
            "stereo_calibration",
            ("camera", "stereo_calibration"),
            ("processing", "stereo_calibration"),
        ),
    )
    return resolve_path(value, manifest_path.parent)


def processing_command(
    run_dir: Path,
    calibration: Path,
    settings: dict[str, Any],
) -> list[str]:
    tracking = settings["tracking"]
    command = [
        sys.executable,
        str(SCRIPT_DIR / "stereo_process_recording.py"),
        str(run_dir),
        "--stereo-calibration",
        str(calibration),
        "--window-us",
        str(int(tracking["window_us"])),
        "--hop-us",
        str(int(tracking["hop_us"])),
        "--dt-us",
        str(float(tracking["dt_us"])),
        "--roi",
        str(tracking["roi"]),
        "--tracking-method",
        str(tracking["tracking_method"]),
        "--threshold-count",
        str(int(tracking["threshold_count"])),
        "--min-events",
        str(int(tracking["min_events"])),
        "--min-area",
        str(int(tracking["min_area"])),
        "--min-mass",
        str(int(tracking["min_mass"])),
        "--polarity",
        str(tracking["polarity"]),
        "--max-time-gap-sec",
        str(float(tracking["max_time_gap_sec"])),
    ]
    offset = tracking.get("right_time_offset_sec")
    if offset is not None:
        command.extend(["--right-time-offset-sec", str(float(offset))])
    return command


def validate_processing_provenance(
    row: dict[str, Any],
    calibration: Path,
    settings: dict[str, Any],
) -> tuple[bool, str]:
    run_dir = row.get("run_dir")
    output = row.get("stereo_3d_npz")
    if run_dir is None or output is None:
        return False, "run/output path unavailable"
    run_dir = Path(run_dir)
    output = Path(output)
    manifest_path = run_dir / "stereo_3d" / "processing_manifest.json"
    if not manifest_path.is_file():
        return False, "processing_manifest.json is missing"
    try:
        provenance = load_json(manifest_path)
    except SystemExit as exc:
        return False, str(exc)
    if int(provenance.get("schema_version", 0)) != 1:
        return False, "unsupported processing provenance schema"
    if not output.is_file():
        return False, "stereo 3D output is missing"
    if str(provenance.get("output_npz_sha256", "")) != file_sha256(output):
        return False, "stereo 3D output hash mismatch"
    if str(provenance.get("stereo_calibration_sha256", "")) != file_sha256(
        calibration
    ):
        return False, "processing calibration hash mismatch"
    raw = provenance.get("raw_inputs")
    if not isinstance(raw, dict):
        return False, "processing raw-input provenance is missing"
    accepted_hashes = row.get("accepted_artifact_sha256", {})
    expected_left = str(accepted_hashes.get("left/left_events.npz", ""))
    expected_right = str(accepted_hashes.get("right/right_events.npz", ""))
    if not expected_left or not expected_right:
        return False, "capture raw hashes are unavailable"
    if str(raw.get("left_events_sha256", "")) != expected_left:
        return False, "left raw hash differs from accepted capture"
    if str(raw.get("right_events_sha256", "")) != expected_right:
        return False, "right raw hash differs from accepted capture"
    tracking = provenance.get("tracking")
    if not isinstance(tracking, dict):
        return False, "processing tracking provenance is missing"
    expected_tracking = settings["tracking"]
    comparisons = {
        "window_us": int(expected_tracking["window_us"]),
        "hop_us": int(expected_tracking["hop_us"]),
        "dt_us": float(expected_tracking["dt_us"]),
        "t_start_sec": 0.0,
        "t_end_sec": 0.0,
        "roi": str(expected_tracking["roi"]),
        "tracking_method": str(expected_tracking["tracking_method"]),
        "threshold_count": int(expected_tracking["threshold_count"]),
        "min_events": int(expected_tracking["min_events"]),
        "min_area": int(expected_tracking["min_area"]),
        "min_mass": int(expected_tracking["min_mass"]),
        "polarity": str(expected_tracking["polarity"]),
        "max_time_gap_sec": float(expected_tracking["max_time_gap_sec"]),
        "right_time_offset_requested_sec": (
            float(expected_tracking["right_time_offset_sec"])
            if expected_tracking.get("right_time_offset_sec") is not None
            else None
        ),
    }
    for key, expected in comparisons.items():
        actual = tracking.get(key)
        if isinstance(expected, float):
            if actual is None or not math.isclose(
                float(actual),
                expected,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                return False, f"processing tracking mismatch: {key}"
        elif actual != expected:
            return False, f"processing tracking mismatch: {key}"
    tracking_artifacts = provenance.get("tracking_artifacts")
    if not isinstance(tracking_artifacts, dict):
        return False, "processing tracking-artifact provenance is missing"
    if tracking_artifacts.get("mode") != "generated_from_raw_events":
        return False, "tracking CSVs were reused instead of generated from accepted raw events"
    for key in ("left_interp_csv_sha256", "right_interp_csv_sha256"):
        value = str(tracking_artifacts.get(key, ""))
        if len(value) != 64:
            return False, f"processing tracking-artifact hash is missing: {key}"
    source_hashes = provenance.get("source_sha256")
    if not isinstance(source_hashes, dict):
        return False, "processing source hashes are missing"
    source_paths = {
        "stereo_process_recording.py": SCRIPT_DIR / "stereo_process_recording.py",
        "eventcam_npz_track.py": SCRIPT_DIR / "eventcam_npz_track.py",
        "stereo_triangulate_tracks.py": SCRIPT_DIR / "stereo_triangulate_tracks.py",
    }
    for name, source in source_paths.items():
        if str(source_hashes.get(name, "")) != file_sha256(source):
            return False, f"processing source hash mismatch: {name}"
    return True, ""


def ensure_processed(
    rows: list[dict[str, Any]],
    *,
    args: argparse.Namespace,
    calibration: Path | None,
    settings: dict[str, Any],
) -> None:
    for row in rows:
        path = row["stereo_3d_npz"]
        strict = bool(row.get("strict_processing_provenance", False))
        existing_valid = False
        invalid_reason = ""
        if path is not None and Path(path).exists() and not args.force_processing:
            if strict:
                if calibration is None or not calibration.exists():
                    invalid_reason = "calibration unavailable for provenance check"
                else:
                    existing_valid, invalid_reason = validate_processing_provenance(
                        row,
                        calibration,
                        settings,
                    )
            else:
                existing_valid = True
        if existing_valid:
            continue
        if args.skip_processing:
            if strict:
                row["reason"] = (
                    f"{row['capture_id']}: stale/unverified processed output: "
                    f"{invalid_reason}"
                )
                row["stereo_3d_npz"] = None
            continue
        run_dir = row["run_dir"]
        if run_dir is None:
            message = f"{row['capture_id']}: no run_dir for stereo processing"
            if args.continue_on_error:
                row["reason"] = message
                continue
            raise SystemExit(message)
        if calibration is None or not calibration.exists():
            raise SystemExit(
                "A valid stereo calibration is required to process missing 3D files."
            )
        completed = subprocess.run(
            processing_command(Path(run_dir), calibration, settings),
            cwd=SCRIPT_DIR,
            check=False,
        )
        candidate = Path(run_dir) / "stereo_3d" / "stereo_3d_points.npz"
        row["stereo_3d_npz"] = candidate
        if completed.returncode != 0 or not candidate.exists():
            row["stereo_3d_npz"] = None
            message = (
                f"{row['capture_id']}: stereo processing failed "
                f"(return code {completed.returncode})"
            )
            if args.continue_on_error:
                row["reason"] = message
                continue
            raise SystemExit(message)
        if strict:
            valid, reason = validate_processing_provenance(
                row,
                calibration,
                settings,
            )
            if not valid:
                message = (
                    f"{row['capture_id']}: processing provenance validation "
                    f"failed: {reason}"
                )
                if args.continue_on_error:
                    row["reason"] = message
                    row["stereo_3d_npz"] = None
                    continue
                raise SystemExit(message)


def robust_static_point(path: Path, analysis: dict[str, Any]) -> dict[str, Any]:
    with np.load(path, allow_pickle=False) as data:
        if "points_left_cam_mm" not in data.files:
            raise ValueError("NPZ has no points_left_cam_mm.")
        points = np.asarray(data["points_left_cam_mm"], dtype=np.float64)
        if points.ndim != 2 or points.shape[1] != 3:
            raise ValueError(f"points_left_cam_mm must be Nx3; got {points.shape}.")
        valid = (
            np.asarray(data["valid"], dtype=bool).reshape(-1)
            if "valid" in data.files
            else np.all(np.isfinite(points), axis=1)
        )
        t_sec = (
            np.asarray(data["t_sec"], dtype=np.float64).reshape(-1)
            if "t_sec" in data.files
            else np.arange(points.shape[0], dtype=np.float64)
        )
    if valid.size != points.shape[0] or t_sec.size != points.shape[0]:
        raise ValueError("points, valid, and t_sec lengths differ.")
    mask = valid & np.all(np.isfinite(points), axis=1) & np.isfinite(t_sec)
    raw_count = int(mask.sum())
    raw_fraction = raw_count / points.shape[0] if points.shape[0] else 0.0
    if mask.any() and "t_sec" in locals():
        first = float(np.min(t_sec[mask]))
        last = float(np.max(t_sec[mask]))
        mask &= t_sec >= first + max(0.0, float(analysis["trim_start_sec"]))
        mask &= t_sec <= last - max(0.0, float(analysis["trim_end_sec"]))
    samples = points[mask]
    if samples.size == 0:
        return {
            "center_camera_mm": np.full(3, np.nan),
            "input_samples": int(points.shape[0]),
            "raw_valid_samples": raw_count,
            "raw_valid_fraction": raw_fraction,
            "robust_samples": 0,
            "scatter_rms_mm": float("nan"),
            "scatter_p95_mm": float("nan"),
        }
    initial = np.median(samples, axis=0)
    radial = np.linalg.norm(samples - initial, axis=1)
    radial_median = float(np.median(radial))
    radial_mad = float(np.median(np.abs(radial - radial_median)))
    sigma = 1.4826 * radial_mad
    threshold = max(
        float(analysis["outlier_floor_mm"]),
        radial_median + float(analysis["outlier_mad_factor"]) * sigma,
    )
    inliers = samples[radial <= threshold]
    center = np.median(inliers, axis=0) if inliers.size else np.full(3, np.nan)
    scatter = (
        np.linalg.norm(inliers - center, axis=1)
        if inliers.size
        else np.asarray([], dtype=np.float64)
    )
    return {
        "center_camera_mm": center,
        "input_samples": int(points.shape[0]),
        "raw_valid_samples": raw_count,
        "raw_valid_fraction": raw_fraction,
        "robust_samples": int(inliers.shape[0]),
        "scatter_rms_mm": (
            float(np.sqrt(np.mean(scatter * scatter))) if scatter.size else float("nan")
        ),
        "scatter_p95_mm": (
            float(np.percentile(scatter, 95)) if scatter.size else float("nan")
        ),
    }


def load_centres(rows: list[dict[str, Any]], settings: dict[str, Any]) -> None:
    analysis = settings["analysis"]
    for row in rows:
        if row.get("reason"):
            continue
        path = row["stereo_3d_npz"]
        if path is None or not Path(path).exists():
            row["reason"] = row["reason"] or "missing stereo_3d_points.npz"
            continue
        try:
            stats = robust_static_point(Path(path), analysis)
        except Exception as exc:
            row["reason"] = f"could not read 3D points: {exc}"
            continue
        row.update(stats)
        row["camera_left_optical_center_slant_distance_mm"] = float(
            np.linalg.norm(np.asarray(stats["center_camera_mm"], dtype=np.float64))
        )
        row["camera_forward_z_mm"] = float(
            np.asarray(stats["center_camera_mm"], dtype=np.float64)[2]
        )
        if float(stats["raw_valid_fraction"]) < float(analysis["minimum_valid_fraction"]):
            row["reason"] = (
                f"valid fraction {stats['raw_valid_fraction']:.3f} < "
                f"{float(analysis['minimum_valid_fraction']):.3f}"
            )
        elif int(stats["robust_samples"]) < int(analysis["minimum_valid_samples"]):
            row["reason"] = (
                f"robust samples {stats['robust_samples']} < "
                f"{int(analysis['minimum_valid_samples'])}"
            )
        elif not np.all(np.isfinite(stats["center_camera_mm"])):
            row["reason"] = "non-finite robust centre"
        else:
            row["usable"] = True


def kabsch(source: np.ndarray, target: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    source = np.asarray(source, dtype=np.float64)
    target = np.asarray(target, dtype=np.float64)
    if source.shape != target.shape or source.ndim != 2 or source.shape[1] != 3:
        raise ValueError("Kabsch inputs must be matching Nx3 arrays.")
    if source.shape[0] < 4:
        raise ValueError("At least four orientation points are required.")
    source_center = source.mean(axis=0)
    target_center = target.mean(axis=0)
    covariance = (source - source_center).T @ (target - target_center)
    u, _, vt = np.linalg.svd(covariance)
    rotation = vt.T @ u.T
    if np.linalg.det(rotation) < 0:
        vt[-1] *= -1
        rotation = vt.T @ u.T
    translation = target_center - rotation @ source_center
    return rotation, translation


def rms(values: Iterable[float]) -> float:
    array = np.asarray(list(values), dtype=np.float64)
    array = array[np.isfinite(array)]
    return float(np.sqrt(np.mean(array * array))) if array.size else float("nan")


def percentile(values: Iterable[float], q: float) -> float:
    array = np.asarray(list(values), dtype=np.float64)
    array = array[np.isfinite(array)]
    return float(np.percentile(array, q)) if array.size else float("nan")


def signed_summary(values: Iterable[float]) -> dict[str, Any]:
    array = np.asarray(list(values), dtype=np.float64)
    array = array[np.isfinite(array)]
    if not array.size:
        return {"count": 0}
    return {
        "count": int(array.size),
        "mean_mm": float(np.mean(array)),
        "median_mm": float(np.median(array)),
        "rms_mm": float(np.sqrt(np.mean(array * array))),
        "p95_abs_mm": float(np.percentile(np.abs(array), 95)),
        "max_abs_mm": float(np.max(np.abs(array))),
    }


def fit_orientation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    orientation = [
        row
        for row in rows
        if row["role"] == "orientation"
        and row["usable"]
        and row.get("data_role")
        not in {"registration_center_pre", "registration_center_post"}
    ]
    if len(orientation) < 4:
        raise SystemExit(
            f"At least four usable orientation captures are required; got {len(orientation)}."
        )
    camera = np.vstack([row["center_camera_mm"] for row in orientation])
    stage = np.vstack([row["stage_readback_mm"] for row in orientation])
    target_rank = int(np.linalg.matrix_rank(stage - stage.mean(axis=0), tol=1e-9))
    camera_rank = int(np.linalg.matrix_rank(camera - camera.mean(axis=0), tol=1e-9))
    if min(target_rank, camera_rank) < 3:
        raise SystemExit(
            "Orientation captures must span all three axes (centred coordinate rank 3)."
        )
    rotation, translation = kabsch(camera, stage)
    predicted = (rotation @ camera.T).T + translation
    residual_vectors = predicted - stage
    residual_norms = np.linalg.norm(residual_vectors, axis=1)
    camera_zero = camera - camera.mean(axis=0)
    stage_zero = stage - stage.mean(axis=0)
    rotated_zero = (rotation @ camera_zero.T).T
    denominator = float(np.sum(rotated_zero * rotated_zero))
    similarity_scale = (
        float(np.sum(rotated_zero * stage_zero) / denominator)
        if denominator > 0
        else float("nan")
    )
    row_mapping, _, _, _ = np.linalg.lstsq(camera_zero, stage_zero, rcond=None)
    affine = row_mapping.T
    return {
        "method": "scale-free Kabsch/SVD using orientation captures only",
        "orientation_capture_ids": [row["capture_id"] for row in orientation],
        "validation_captures_used_for_fit": 0,
        "rotation_camera_to_stage": rotation,
        "translation_stage_mm": translation,
        "rotation_determinant": float(np.linalg.det(rotation)),
        "rotation_orthogonality_error": float(
            np.linalg.norm(rotation.T @ rotation - np.eye(3), ord="fro")
        ),
        "orientation_stage_rank": target_rank,
        "orientation_camera_rank": camera_rank,
        "rigid_fit_residual_rms_mm": rms(residual_norms),
        "rigid_fit_residual_max_mm": (
            float(np.max(residual_norms)) if residual_norms.size else float("nan")
        ),
        "similarity_scale_diagnostic_only": similarity_scale,
        "affine_matrix_diagnostic_only": affine,
        "affine_singular_values_diagnostic_only": np.linalg.svd(
            affine, compute_uv=False
        ),
        "affine_determinant_diagnostic_only": float(np.linalg.det(affine)),
        "diagnostic_fits_applied_to_validation": False,
    }


def dominant_axis(delta: np.ndarray, purity_ratio: float) -> str:
    absolute = np.abs(delta)
    index = int(np.argmax(absolute))
    main = float(absolute[index])
    if main <= 1e-12:
        return "return"
    off = float(np.linalg.norm(np.delete(absolute, index)))
    return AXIS_NAMES[index] if off <= float(purity_ratio) * main else "multi"


def apply_validation_metrics(
    rows: list[dict[str, Any]],
    orientation: dict[str, Any],
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    by_id = {str(row["capture_id"]): row for row in rows}
    rotation = np.asarray(orientation["rotation_camera_to_stage"], dtype=np.float64)
    translation = np.asarray(orientation["translation_stage_mm"], dtype=np.float64)
    purity = float(settings["analysis"]["axis_purity_ratio"])
    return_tolerance = float(settings["analysis"]["return_position_tolerance_mm"])
    stages_settings = settings.get("stages", {})
    distance_settings = settings.get(
        "absolute_distance_reference",
        settings.get("distance_reference", {}),
    )
    depth_axis_name = str(
        stages_settings.get(
            "depth_axis",
            distance_settings.get("stage_axis", "y"),
        )
    ).lower()
    depth_axis_index = AXIS_NAMES.index(depth_axis_name)
    distance_sign = float(distance_settings.get("sign", 1.0))
    validation: list[dict[str, Any]] = []
    for row in rows:
        if not row["usable"]:
            continue
        row["measured_stage_frame_mm"] = (
            rotation @ np.asarray(row["center_camera_mm"]) + translation
        )
        row["absolute_stage_frame_residual_mm"] = (
            row["measured_stage_frame_mm"] - row["stage_readback_mm"]
        )
        if row["role"] != "validation":
            continue
        reference = by_id.get(str(row["reference_id"]))
        if reference is None:
            row["reason"] = f"unknown reference_id {row['reference_id']!r}"
            row["usable"] = False
            continue
        if not reference["usable"]:
            row["reason"] = f"reference {row['reference_id']!r} is not usable"
            row["usable"] = False
            continue
        delta_camera = np.asarray(row["center_camera_mm"]) - np.asarray(
            reference["center_camera_mm"]
        )
        delta_stage = np.asarray(row["stage_readback_mm"]) - np.asarray(
            reference["stage_readback_mm"]
        )
        observed_delta_stage = rotation @ delta_camera
        residual = observed_delta_stage - delta_stage
        stage_norm = float(np.linalg.norm(delta_stage))
        camera_norm = float(np.linalg.norm(delta_camera))
        axis = dominant_axis(delta_stage, purity)
        if stage_norm > return_tolerance:
            unit = delta_stage / stage_norm
            axial = float(np.dot(residual, unit))
            lateral_vector = residual - axial * unit
        else:
            unit = np.full(3, np.nan)
            axial = float("nan")
            lateral_vector = residual.copy()
            axis = "return"
        is_return_check = (
            row.get("data_role") == "validation_center_post"
            or (
                not row.get("data_role")
                and stage_norm <= return_tolerance
            )
        )
        if is_return_check:
            axis = "return"
        row.update(
            {
                "reference_stage_readback_mm": np.asarray(
                    reference["stage_readback_mm"]
                ),
                "reference_center_camera_mm": np.asarray(
                    reference["center_camera_mm"]
                ),
                "reference_plan_target_mm": np.asarray(
                    reference["plan_target_mm"]
                ),
                "delta_camera_mm": delta_camera,
                "delta_stage_mm": delta_stage,
                "observed_delta_stage_frame_mm": observed_delta_stage,
                "delta_residual_stage_frame_mm": residual,
                "stage_displacement_norm_mm": stage_norm,
                "camera_displacement_norm_mm": camera_norm,
                "distance_norm_error_mm": camera_norm - stage_norm,
                "stage_depth_from_reference_mm": (
                    distance_sign * float(delta_stage[depth_axis_index])
                ),
                "movement_unit_stage": unit,
                "axial_error_mm": axial,
                "lateral_error_mm": float(np.linalg.norm(lateral_vector)),
                "error_3d_mm": float(np.linalg.norm(residual)),
                "dominant_axis": axis,
                "return_drift_3d_mm": (
                    camera_norm
                    if is_return_check
                    else float("nan")
                ),
                "stage_return_offset_3d_mm": (
                    stage_norm if is_return_check else float("nan")
                ),
                "camera_return_shift_3d_mm": (
                    camera_norm if is_return_check else float("nan")
                ),
                "return_residual_3d_mm": (
                    float(np.linalg.norm(residual))
                    if is_return_check
                    else float("nan")
                ),
                "primary_evaluation": row.get("data_role")
                not in {
                    "validation_center_pre",
                    "validation_center_post",
                },
                "return_check": is_return_check,
            }
        )
        validation.append(row)
    return validation


def linear_fit(x: np.ndarray, y: np.ndarray) -> dict[str, float] | None:
    finite = np.isfinite(x) & np.isfinite(y)
    x = np.asarray(x[finite], dtype=np.float64)
    y = np.asarray(y[finite], dtype=np.float64)
    if x.size < 2 or np.unique(np.round(x, 9)).size < 2:
        return None
    design = np.column_stack([np.ones(x.size), x])
    coefficients, _, _, _ = np.linalg.lstsq(design, y, rcond=None)
    predicted = design @ coefficients
    residual = y - predicted
    total = float(np.sum((y - np.mean(y)) ** 2))
    return {
        "count": int(x.size),
        "intercept_mm": float(coefficients[0]),
        "slope_mm_per_mm": float(coefficients[1]),
        "residual_rms_mm": rms(residual),
        "r_squared": (
            float(1.0 - np.sum(residual * residual) / total)
            if total > 0
            else float("nan")
        ),
    }


def axis_response_diagnostics(
    validation: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], np.ndarray]:
    rows: list[dict[str, Any]] = []
    response = np.full((3, 3), np.nan, dtype=np.float64)
    for input_index, input_axis in enumerate(AXIS_NAMES):
        members = [
            row for row in validation if row.get("dominant_axis") == input_axis
        ]
        if not members:
            continue
        truth = np.asarray(
            [row["delta_stage_mm"][input_index] for row in members],
            dtype=np.float64,
        )
        for output_index, output_axis in enumerate(AXIS_NAMES):
            observed = np.asarray(
                [row["observed_delta_stage_frame_mm"][output_index] for row in members],
                dtype=np.float64,
            )
            fit = linear_fit(truth, observed)
            if fit is None:
                continue
            response[output_index, input_index] = fit["slope_mm_per_mm"]
            rows.append(
                {
                    "input_axis": input_axis,
                    "output_axis": output_axis,
                    "is_primary_axis": input_axis == output_axis,
                    "cross_talk": input_axis != output_axis,
                    **fit,
                }
            )
    return rows, response


def position_key(point: np.ndarray, tolerance: float) -> tuple[int, int, int]:
    tolerance = max(float(tolerance), 1e-9)
    return tuple(int(round(float(value) / tolerance)) for value in point)


def repeatability_diagnostics(
    validation: list[dict[str, Any]],
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    tolerance = float(settings["analysis"]["group_position_tolerance_mm"])
    grouped: dict[tuple[int, int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in validation:
        grouped[position_key(row["plan_target_mm"], tolerance)].append(row)
    output: list[dict[str, Any]] = []
    for key, members in sorted(grouped.items()):
        if len(members) < 2:
            continue
        residuals = np.vstack(
            [row["absolute_stage_frame_residual_mm"] for row in members]
        )
        median_residual = np.median(residuals, axis=0)
        deviations = residuals - median_residual
        radial = np.linalg.norm(deviations, axis=1)
        output.append(
            {
                "position_key": ",".join(str(value) for value in key),
                "plan_target_x_mm": float(np.median([row["plan_target_mm"][0] for row in members])),
                "plan_target_y_mm": float(np.median([row["plan_target_mm"][1] for row in members])),
                "plan_target_z_mm": float(np.median([row["plan_target_mm"][2] for row in members])),
                "captures": len(members),
                "cycles": len({int(row["cycle"]) for row in members}),
                "directions": ",".join(sorted({str(row["direction"]) for row in members})),
                "repeatability_std_x_mm": (
                    float(np.std(deviations[:, 0], ddof=1)) if len(members) > 1 else 0.0
                ),
                "repeatability_std_y_mm": (
                    float(np.std(deviations[:, 1], ddof=1)) if len(members) > 1 else 0.0
                ),
                "repeatability_std_z_mm": (
                    float(np.std(deviations[:, 2], ddof=1)) if len(members) > 1 else 0.0
                ),
                "repeatability_radial_rms_mm": rms(radial),
                "repeatability_radial_p95_mm": percentile(radial, 95),
                "repeatability_radial_max_mm": (
                    float(np.max(radial)) if radial.size else float("nan")
                ),
            }
        )
    return output


def direction_diagnostics(
    validation: list[dict[str, Any]],
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    tolerance = float(settings["analysis"]["group_position_tolerance_mm"])
    grouped: dict[tuple[int, int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in validation:
        grouped[position_key(row["plan_target_mm"], tolerance)].append(row)
    output: list[dict[str, Any]] = []
    forward_names = {"forward", "outbound", "positive"}
    reverse_names = {"reverse", "return", "inbound", "negative"}
    for key, members in sorted(grouped.items()):
        forward = [
            row
            for row in members
            if str(row["direction"]).lower() in forward_names
        ]
        reverse = [
            row
            for row in members
            if str(row["direction"]).lower() in reverse_names
        ]
        if not forward or not reverse:
            continue
        forward_residual = np.median(
            np.vstack([row["absolute_stage_frame_residual_mm"] for row in forward]),
            axis=0,
        )
        reverse_residual = np.median(
            np.vstack([row["absolute_stage_frame_residual_mm"] for row in reverse]),
            axis=0,
        )
        difference = forward_residual - reverse_residual
        output.append(
            {
                "position_key": ",".join(str(value) for value in key),
                "forward_captures": len(forward),
                "reverse_captures": len(reverse),
                "forward_minus_reverse_x_mm": float(difference[0]),
                "forward_minus_reverse_y_mm": float(difference[1]),
                "forward_minus_reverse_z_mm": float(difference[2]),
                "forward_minus_reverse_3d_mm": float(np.linalg.norm(difference)),
            }
        )
    return output


def distance_reference_config(
    manifest: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    candidates = (
        manifest.get("absolute_distance_reference"),
        manifest.get("distance_reference"),
        manifest.get("distance_reporting"),
        settings.get("absolute_distance_reference"),
        settings.get("distance_reference"),
    )
    raw = next((value for value in candidates if isinstance(value, dict)), {})
    if raw.get("available") is False:
        return {
            "available": False,
            "reason": "absolute distance reference is explicitly disabled until measured",
            "definition": str(raw.get("definition", "")).strip(),
        }
    distance = first_present(
        raw,
        (
            "nearest_baseline_distance_mm",
            "distance_mm",
            "nearest_distance_mm",
        ),
    )
    if distance is None:
        return {
            "available": False,
            "reason": "independent reference distance is not configured",
            "definition": str(raw.get("definition", "")).strip(),
        }
    uncertainty = first_present(
        raw,
        (
            "standard_uncertainty_mm",
            "nearest_baseline_standard_uncertainty_mm",
            "uncertainty_mm",
        ),
    )
    axis_name = str(raw.get("stage_axis", raw.get("axis", "y"))).strip().lower()
    if axis_name not in AXIS_NAMES:
        raise SystemExit("absolute distance stage_axis must be x, y, or z.")
    sign = float(raw.get("sign", raw.get("distance_sign", 1.0)))
    if sign not in {-1.0, 1.0}:
        raise SystemExit("absolute distance sign must be +1 or -1.")
    axis = np.zeros(3, dtype=np.float64)
    axis[AXIS_NAMES.index(axis_name)] = sign
    reference_global_value = first_present(
        raw,
        (
            "reference_readback_global_xyz_mm",
            "reference_stage_xyz_mm",
        ),
    )
    reference_stage = (
        vector3(
            reference_global_value,
            "reference_readback_global_xyz_mm",
        )
        if reference_global_value is not None
        else None
    )
    reference_hardware_value = raw.get(
        "reference_readback_hardware_xyz_mm"
    )
    reference_hardware = (
        vector3(
            reference_hardware_value,
            "reference_readback_hardware_xyz_mm",
        )
        if reference_hardware_value is not None
        else None
    )
    if raw.get("available") is True and (
        reference_stage is None or reference_hardware is None
    ):
        raise SystemExit(
            "Available absolute distance requires its actual reference "
            "global and hardware readbacks."
        )
    stages = settings.get("stages", {})
    axes_config = stages.get("axes", {}) if isinstance(stages, dict) else {}
    if reference_stage is not None and reference_hardware is not None:
        for index, axis_name_item in enumerate(AXIS_NAMES):
            spec = axes_config.get(axis_name_item)
            if not isinstance(spec, dict):
                continue
            expected_hardware = float(spec["datum_mm"]) + int(
                spec["direction"]
            ) * float(reference_stage[index])
            tolerance = float(spec.get("readback_tolerance_mm", 0.1))
            if (
                abs(expected_hardware - float(reference_hardware[index]))
                > tolerance
            ):
                raise SystemExit(
                    "Absolute-distance frozen global/hardware readback "
                    f"is inconsistent on {axis_name_item.upper()}."
                )
    return {
        "available": True,
        "reference_distance_mm": float(distance),
        "standard_uncertainty_mm": float(uncertainty or 0.0),
        "stage_standard_uncertainty_mm": float(
            raw.get("stage_standard_uncertainty_mm") or 0.0
        ),
        "stage_axis": axis_name,
        "sign": sign,
        "depth_axis_stage": axis,
        "reference_id": str(
            first_present(
                raw,
                (
                    "reference_id",
                    "reference_sample_id",
                    "nearest_reference_id",
                ),
            )
            or ""
        ),
        "reference_stage_xyz_mm": reference_stage,
        "reference_command_global_mm": raw.get(
            "reference_command_global_mm"
        ),
        "reference_readback_hardware_xyz_mm": reference_hardware,
        "measured_at": str(raw.get("measured_at", "")),
        "home_state": str(raw.get("home_state", "")),
        "method": str(raw.get("method", "independent mechanical measurement")),
        "instrument": str(raw.get("instrument", "")),
        "definition": str(raw.get("definition", "")).strip(),
        "moving_body": str(raw.get("moving_body", "target")).strip().lower(),
        "axis_alignment_verified": bool(
            raw.get("axis_alignment_verified", False)
        ),
        "axis_alignment_standard_uncertainty_rad": raw.get(
            "axis_alignment_standard_uncertainty_rad"
        ),
        "label_role": "independent mechanical distance label; not a stereo-estimated distance",
    }


def apply_absolute_distance_labels(
    rows: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    if not config.get("available"):
        return config
    by_id = {str(row["capture_id"]): row for row in rows}
    reference_stage = config.get("reference_stage_xyz_mm")
    if reference_stage is None:
        reference_id = str(config.get("reference_id") or "")
        if not reference_id:
            reference_id = next(
                (
                    str(row["capture_id"])
                    for row in rows
                    if row["role"] == "orientation"
                ),
                "",
            )
        if reference_id not in by_id:
            raise SystemExit(
                f"Absolute-distance reference_id {reference_id!r} is not in the manifest."
            )
        reference_stage = np.asarray(by_id[reference_id]["stage_readback_mm"])
        config["reference_id"] = reference_id
    axis = np.asarray(config["depth_axis_stage"], dtype=np.float64)
    combined_uncertainty = math.hypot(
        float(config["standard_uncertainty_mm"]),
        float(config["stage_standard_uncertainty_mm"]),
    )
    for row in rows:
        offset = float(
            np.dot(np.asarray(row["stage_readback_mm"]) - reference_stage, axis)
        )
        label = float(config["reference_distance_mm"]) + offset
        row["absolute_distance_label_mm"] = label
        row["absolute_distance_standard_uncertainty_mm"] = combined_uncertainty
        row["absolute_distance_coverage95_mm"] = 1.96 * combined_uncertainty
    config["reference_stage_xyz_mm"] = reference_stage
    config["combined_standard_uncertainty_mm"] = combined_uncertainty
    config["label_formula"] = (
        "distance_mm + sign * "
        f"(stage_{config['stage_axis']}_mm - reference_stage_{config['stage_axis']}_mm)"
    )
    return config


BASELINE_DISTANCE_DEFINITIONS = {
    "target_point_to_stereo_optical_center_baseline_line_perpendicular",
    "point_to_stereo_optical_center_baseline_line_perpendicular",
    "stereo_baseline_perpendicular_distance",
}


def stereo_baseline_distance_mm(
    point_left_camera_mm: np.ndarray,
    calibration: dict[str, np.ndarray],
) -> float:
    rotation_left_to_right = np.asarray(calibration["R"], dtype=np.float64)
    translation_left_to_right = np.asarray(calibration["T"], dtype=np.float64).reshape(3)
    right_center_left = -rotation_left_to_right.T @ translation_left_to_right
    length = float(np.linalg.norm(right_center_left))
    if length <= 0 or not math.isfinite(length):
        raise ValueError("Stereo optical-centre baseline is invalid.")
    unit = right_center_left / length
    point = np.asarray(point_left_camera_mm, dtype=np.float64).reshape(3)
    perpendicular = point - unit * float(np.dot(point, unit))
    return float(np.linalg.norm(perpendicular))


def load_baseline_calibration(path: Path | None) -> dict[str, np.ndarray] | None:
    if path is None or not path.exists():
        return None
    with np.load(path, allow_pickle=False) as data:
        if "R" not in data.files or "T" not in data.files:
            return None
        return {
            "R": np.asarray(data["R"], dtype=np.float64),
            "T": np.asarray(data["T"], dtype=np.float64),
        }


def load_full_calibration(
    path: Path | None,
) -> tuple[dict[str, np.ndarray] | None, str]:
    if path is None or not path.exists():
        return None, "stereo calibration file is unavailable"
    required = (
        "left_camera_matrix",
        "left_dist_coeffs",
        "right_camera_matrix",
        "right_dist_coeffs",
        "R",
        "T",
    )
    with np.load(path, allow_pickle=False) as data:
        missing = [key for key in required if key not in data.files]
        if missing:
            return None, f"stereo calibration lacks {missing}"
        calibration = {
            key: np.asarray(data[key], dtype=np.float64)
            for key in required
        }
        if "stereo_rms" in data.files:
            calibration["stereo_rms"] = np.asarray(
                data["stereo_rms"],
                dtype=np.float64,
            )
    return calibration, ""


def project_stereo_observation(
    point_left_camera_mm: np.ndarray,
    calibration: dict[str, np.ndarray],
) -> np.ndarray:
    import cv2

    point = np.asarray(point_left_camera_mm, dtype=np.float64).reshape(1, 1, 3)
    zero = np.zeros((3, 1), dtype=np.float64)
    left_px, _ = cv2.projectPoints(
        point,
        zero,
        zero,
        calibration["left_camera_matrix"],
        calibration["left_dist_coeffs"],
    )
    right_rvec, _ = cv2.Rodrigues(calibration["R"])
    right_px, _ = cv2.projectPoints(
        point,
        right_rvec,
        calibration["T"].reshape(3, 1),
        calibration["right_camera_matrix"],
        calibration["right_dist_coeffs"],
    )
    return np.concatenate((left_px.reshape(2), right_px.reshape(2)))


def triangulate_stereo_observation(
    observation_px: np.ndarray,
    calibration: dict[str, np.ndarray],
) -> np.ndarray:
    import cv2

    observation = np.asarray(observation_px, dtype=np.float64).reshape(4)
    left = cv2.undistortPoints(
        observation[:2].reshape(1, 1, 2),
        calibration["left_camera_matrix"],
        calibration["left_dist_coeffs"],
    ).reshape(2)
    right = cv2.undistortPoints(
        observation[2:].reshape(1, 1, 2),
        calibration["right_camera_matrix"],
        calibration["right_dist_coeffs"],
    ).reshape(2)
    projection_left = np.hstack((np.eye(3), np.zeros((3, 1))))
    projection_right = np.hstack(
        (calibration["R"], calibration["T"].reshape(3, 1))
    )
    homogeneous = cv2.triangulatePoints(
        projection_left,
        projection_right,
        left.reshape(2, 1),
        right.reshape(2, 1),
    )
    return homogeneous[:3, 0] / homogeneous[3, 0]


def numerical_triangulation_covariance(
    point_left_camera_mm: np.ndarray,
    calibration: dict[str, np.ndarray],
    *,
    per_camera_pixel_sigma: float,
    finite_difference_px: float = 1e-3,
) -> np.ndarray:
    observation = project_stereo_observation(
        point_left_camera_mm,
        calibration,
    )
    jacobian = np.empty((3, 4), dtype=np.float64)
    for column in range(4):
        plus = observation.copy()
        minus = observation.copy()
        plus[column] += finite_difference_px
        minus[column] -= finite_difference_px
        jacobian[:, column] = (
            triangulate_stereo_observation(plus, calibration)
            - triangulate_stereo_observation(minus, calibration)
        ) / (2.0 * finite_difference_px)
    observation_covariance = (
        np.eye(4, dtype=np.float64) * float(per_camera_pixel_sigma) ** 2
    )
    return jacobian @ observation_covariance @ jacobian.T


def triangulation_angle_deg(
    point_left_camera_mm: np.ndarray,
    calibration: dict[str, np.ndarray],
) -> float:
    point = np.asarray(point_left_camera_mm, dtype=np.float64).reshape(3)
    right_center_left = -calibration["R"].T @ calibration["T"].reshape(3)
    ray_left = point
    ray_right = point - right_center_left
    denominator = float(np.linalg.norm(ray_left) * np.linalg.norm(ray_right))
    if denominator <= 0:
        return float("nan")
    cosine = float(
        np.clip(np.dot(ray_left, ray_right) / denominator, -1.0, 1.0)
    )
    return float(np.degrees(np.arccos(cosine)))


def theory_diagnostics(
    validation: list[dict[str, Any]],
    orientation: dict[str, Any],
    settings: dict[str, Any],
    calibration_path: Path | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    calibration, reason = load_full_calibration(calibration_path)
    if calibration is None:
        return {"available": False, "reason": reason}, []
    try:
        import cv2  # noqa: F401
    except ImportError as exc:
        return {"available": False, "reason": f"OpenCV unavailable: {exc}"}, []
    analysis = settings["analysis"]
    pixel_sigmas = [
        float(value)
        for value in analysis.get(
            "pixel_sigma_scenarios",
            [0.1, 0.25, 0.5],
        )
    ]
    coverage = float(analysis.get("theory_coverage_sigma", 1.96))
    rotation = np.asarray(
        orientation["rotation_camera_to_stage"],
        dtype=np.float64,
    )
    depth_axis_name = str(
        settings.get("stages", {}).get("depth_axis", "y")
    ).strip().lower()
    if depth_axis_name not in AXIS_NAMES:
        depth_axis_name = "y"
    depth_axis_stage = np.zeros(3, dtype=np.float64)
    depth_axis_stage[AXIS_NAMES.index(depth_axis_name)] = 1.0
    depth_axis_camera = rotation.T @ depth_axis_stage
    covariance_cache: dict[tuple[str, float], np.ndarray] = {}
    rows: list[dict[str, Any]] = []
    for item in validation:
        delta_camera = np.asarray(item["delta_camera_mm"], dtype=np.float64)
        delta_norm = float(np.linalg.norm(delta_camera))
        displacement_unit_camera = (
            delta_camera / delta_norm
            if delta_norm > 0
            else np.full(3, np.nan)
        )
        current = np.asarray(item["center_camera_mm"], dtype=np.float64)
        reference = np.asarray(
            item["reference_center_camera_mm"],
            dtype=np.float64,
        )
        for pixel_sigma in pixel_sigmas:
            current_key = (str(item["capture_id"]), pixel_sigma)
            reference_key = (str(item["reference_id"]), pixel_sigma)
            if current_key not in covariance_cache:
                covariance_cache[current_key] = numerical_triangulation_covariance(
                    current,
                    calibration,
                    per_camera_pixel_sigma=pixel_sigma,
                )
            if reference_key not in covariance_cache:
                covariance_cache[reference_key] = (
                    numerical_triangulation_covariance(
                        reference,
                        calibration,
                        per_camera_pixel_sigma=pixel_sigma,
                    )
                )
            covariance_current = covariance_cache[current_key]
            covariance_delta = (
                covariance_current + covariance_cache[reference_key]
            )
            sigma_xyz = np.sqrt(
                np.maximum(0.0, np.diag(covariance_current))
            )
            sigma_depth = math.sqrt(
                max(
                    0.0,
                    float(
                        depth_axis_camera
                        @ covariance_delta
                        @ depth_axis_camera
                    ),
                )
            )
            sigma_norm = (
                math.sqrt(
                    max(
                        0.0,
                        float(
                            displacement_unit_camera
                            @ covariance_delta
                            @ displacement_unit_camera
                        ),
                    )
                )
                if delta_norm > 0
                else float("nan")
            )
            rows.append(
                {
                    "capture_id": item["capture_id"],
                    "reference_id": item["reference_id"],
                    "dominant_axis": item["dominant_axis"],
                    "absolute_distance_label_mm": item.get(
                        "absolute_distance_label_mm",
                        float("nan"),
                    ),
                    "camera_x_mm": current[0],
                    "camera_y_mm": current[1],
                    "camera_z_mm": current[2],
                    "triangulation_angle_deg": triangulation_angle_deg(
                        current,
                        calibration,
                    ),
                    "pixel_sigma_per_camera_px": pixel_sigma,
                    "single_point_sigma_x_mm": sigma_xyz[0],
                    "single_point_sigma_y_mm": sigma_xyz[1],
                    "single_point_sigma_z_mm": sigma_xyz[2],
                    "displacement_sigma_norm_mm": sigma_norm,
                    "displacement_sigma_depth_axis_mm": sigma_depth,
                    "coverage_sigma": coverage,
                    "displacement_coverage_depth_axis_mm": (
                        coverage * sigma_depth
                    ),
                }
            )
    translation = calibration["T"].reshape(3)
    left_fx = float(calibration["left_camera_matrix"][0, 0])
    right_fx = float(calibration["right_camera_matrix"][0, 0])
    return (
        {
            "available": True,
            "model": (
                "numerical Jacobian of calibrated distorted projection, "
                "undistortion, and convergent stereo triangulation"
            ),
            "pixel_sigma_scenarios_per_camera_px": pixel_sigmas,
            "coverage_sigma": coverage,
            "optical_center_baseline_mm": float(np.linalg.norm(translation)),
            "mean_raw_fx_px": 0.5 * (left_fx + right_fx),
            "depth_axis_stage": depth_axis_name,
            "estimator_scope": (
                "one independently triangulated point at each displacement endpoint"
            ),
            "measured_scope": (
                "robust median of many correlated event-tracking windows"
            ),
            "warning": (
                "Pixel sigma is a scenario, not inferred from stereo RMS. "
                "The prediction excludes calibration bias, target size, timestamp "
                "error, stage error, vibration, and correlation between windows."
            ),
            "rows": len(rows),
        },
        rows,
    )


def apply_stereo_baseline_comparison(
    rows: list[dict[str, Any]],
    distance_config: dict[str, Any],
    calibration_path: Path | None,
) -> dict[str, Any]:
    definition = str(distance_config.get("definition", "")).lower()
    definition_matches = definition in BASELINE_DISTANCE_DEFINITIONS
    moving_body_matches = str(distance_config.get("moving_body", "target")) == "target"
    alignment_verified = bool(
        distance_config.get("axis_alignment_verified", False)
    )
    calibration = load_baseline_calibration(calibration_path)
    estimate_count = 0
    if calibration is not None:
        for row in rows:
            if not row.get("usable"):
                continue
            row["stereo_baseline_distance_estimate_mm"] = (
                stereo_baseline_distance_mm(
                    row["center_camera_mm"],
                    calibration,
                )
            )
            estimate_count += 1
    distance_config["stereo_baseline_estimate_available"] = (
        calibration is not None
    )
    distance_config["stereo_baseline_estimated_captures"] = estimate_count
    distance_config["stereo_baseline_estimate_role"] = (
        "stereo-only estimate; not independent ground truth"
    )
    enabled = bool(
        distance_config.get("available")
        and definition_matches
        and moving_body_matches
        and alignment_verified
        and calibration is not None
    )
    distance_config["stereo_baseline_comparison_available"] = enabled
    if not enabled:
        reasons: list[str] = []
        if not distance_config.get("available"):
            reasons.append("mechanical distance label unavailable")
        if not definition_matches:
            reasons.append("distance definition does not explicitly match baseline perpendicular distance")
        if not moving_body_matches:
            reasons.append("comparison currently requires moving_body=target")
        if not alignment_verified:
            reasons.append(
                "stage depth-axis alignment to the baseline-perpendicular "
                "distance direction is not independently verified"
            )
        if calibration is None:
            reasons.append("stereo calibration R/T unavailable")
        distance_config["stereo_baseline_comparison_reason"] = "; ".join(reasons)
        return distance_config
    reference_stage = np.asarray(
        distance_config["reference_stage_xyz_mm"],
        dtype=np.float64,
    )
    depth_axis_name = str(distance_config["stage_axis"])
    depth_index = AXIS_NAMES.index(depth_axis_name)
    compared = 0
    skipped_lateral = 0
    for row in rows:
        if not row.get("usable"):
            continue
        stage_delta = (
            np.asarray(row["stage_readback_mm"], dtype=np.float64)
            - reference_stage
        )
        lateral_delta = np.delete(stage_delta, depth_index)
        if float(np.linalg.norm(lateral_delta)) > 1e-6:
            skipped_lateral += 1
            continue
        row["absolute_baseline_distance_label_mm"] = float(
            row["absolute_distance_label_mm"]
        )
        row["absolute_baseline_distance_standard_uncertainty_mm"] = float(
            row["absolute_distance_standard_uncertainty_mm"]
        )
        estimate = float(row["stereo_baseline_distance_estimate_mm"])
        row["stereo_baseline_distance_estimate_mm"] = estimate
        row["stereo_minus_mechanical_baseline_distance_mm"] = (
            estimate - float(row["absolute_distance_label_mm"])
        )
        compared += 1
    distance_config["stereo_baseline_comparison_reason"] = (
        "enabled because the configured mechanical and stereo quantities are both "
        "target-point to optical-centre baseline-line perpendicular distances; "
        "only zero-lateral-offset depth samples were compared"
    )
    distance_config["stereo_baseline_compared_captures"] = compared
    distance_config["stereo_baseline_skipped_lateral_captures"] = (
        skipped_lateral
    )
    return distance_config


def acceptance_result(
    validation: list[dict[str, Any]],
    settings: dict[str, Any],
    coverage: dict[str, Any],
    return_checks: list[dict[str, Any]],
) -> dict[str, Any]:
    configured = settings["analysis"].get("acceptance", {})
    truth = settings.get("stage_truth", {})
    external_truth = bool(
        truth.get("independent_external_displacement_truth_available", False)
    )
    if external_truth:
        raise SystemExit(
            "External stage truth is flagged available, but this schema has no "
            "capture-by-capture external displacement vectors. Refusing an "
            "independent-truth PASS."
        )
    truth_uncertainty = (
        float(truth["displacement_standard_uncertainty_mm"])
        if external_truth
        and truth.get("displacement_standard_uncertainty_mm") is not None
        else 0.0
    )
    truth_coverage_sigma = float(truth.get("coverage_sigma", 1.96))
    guard_band = truth_coverage_sigma * truth_uncertainty
    checks = {
        "max_abs_distance_norm_error_mm": max(
            (abs(float(row["distance_norm_error_mm"])) for row in validation),
            default=float("nan"),
        ),
        "max_abs_axial_error_mm": max(
            (
                abs(float(row["axial_error_mm"]))
                for row in validation
                if math.isfinite(float(row["axial_error_mm"]))
            ),
            default=float("nan"),
        ),
        "max_lateral_error_mm": max(
            (float(row["lateral_error_mm"]) for row in validation),
            default=float("nan"),
        ),
        "max_3d_error_mm": max(
            (float(row["error_3d_mm"]) for row in validation),
            default=float("nan"),
        ),
    }
    failures: list[str] = []
    applied: dict[str, Any] = {}
    for key, observed in checks.items():
        limit = configured.get(key)
        if limit is None:
            continue
        guarded = observed + guard_band
        applied[key] = {
            "observed_mm": observed,
            "truth_guard_band_mm": guard_band,
            "guarded_observed_mm": guarded,
            "limit_mm": float(limit),
        }
        if not math.isfinite(guarded) or guarded > float(limit):
            failures.append(
                f"{key}+truth_guard={guarded:.6g} > {float(limit):.6g}"
            )
    if coverage.get("available") and not coverage.get("complete"):
        failures.append("mandatory capture/processing coverage is incomplete")
    return_limit = float(
        settings["analysis"]["return_position_tolerance_mm"]
    )
    maximum_stage_return = max(
        (
            float(row["stage_return_offset_3d_mm"])
            for row in return_checks
        ),
        default=float("nan"),
    )
    if return_checks and (
        not math.isfinite(maximum_stage_return)
        or maximum_stage_return > return_limit
    ):
        failures.append(
            "maximum stage return offset "
            f"{maximum_stage_return:.6g} mm exceeds {return_limit:.6g} mm"
        )
    if failures:
        status = "fail"
    elif not applied:
        status = "not_configured"
    elif external_truth:
        status = "pass_with_independent_stage_truth"
    else:
        status = "pass_relative_to_ossila_readback"
    return {
        "status": status,
        "scope": (
            "camera_accuracy_against_independent_stage_truth"
            if external_truth
            else "agreement_with_Ossila_encoder_readback"
        ),
        "camera_only_absolute_accuracy_status": (
            status if external_truth else "not_evaluable_without_independent_stage_truth"
        ),
        "external_stage_truth_available": external_truth,
        "stage_truth_standard_uncertainty_mm": (
            truth_uncertainty if external_truth else None
        ),
        "truth_guard_band_mm": guard_band if external_truth else None,
        "return_position_tolerance_mm": return_limit,
        "maximum_stage_return_offset_mm": maximum_stage_return,
        "checks": applied,
        "failures": failures,
    }


def coverage_diagnostics(
    manifest: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    samples = manifest.get("samples")
    if not isinstance(samples, list) or not any(
        isinstance(sample, dict) and "capture_status" in sample
        for sample in samples
    ):
        return {
            "available": False,
            "complete": False,
            "reason": "legacy manifest has no formal capture_status coverage",
        }
    usable = {
        str(row["capture_id"]): bool(row["usable"])
        for row in rows
    }
    expected: dict[str, list[str]] = {
        "orientation_fit": [],
        "primary_validation": [],
        "return_drift": [],
        "other": [],
    }
    captured_ids: set[str] = set()
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        sample_id = str(sample.get("sample_id", ""))
        role = str(sample.get("role", "")).lower()
        data_role = str(sample.get("data_role", "")).lower()
        if role == "orientation" and data_role not in {
            "registration_center_pre",
            "registration_center_post",
        }:
            category = "orientation_fit"
        elif role == "validation" and data_role == "validation_center_post":
            category = "return_drift"
        elif role == "validation" and data_role != "validation_center_pre":
            category = "primary_validation"
        else:
            category = "other"
        expected[category].append(sample_id)
        if str(sample.get("capture_status", "")).lower() == "captured":
            captured_ids.add(sample_id)
    categories: dict[str, Any] = {}
    complete = True
    for category, ids in expected.items():
        missing_capture = [sample_id for sample_id in ids if sample_id not in captured_ids]
        unusable = [
            sample_id
            for sample_id in ids
            if sample_id in captured_ids and not usable.get(sample_id, False)
        ]
        if missing_capture or unusable:
            complete = False
        categories[category] = {
            "expected": len(ids),
            "captured": sum(sample_id in captured_ids for sample_id in ids),
            "usable": sum(bool(usable.get(sample_id, False)) for sample_id in ids),
            "missing_capture_ids": missing_capture,
            "unusable_capture_ids": unusable,
        }
    return {
        "available": True,
        "complete": complete,
        "planned_captures": len(samples),
        "captured_captures": len(captured_ids),
        "categories": categories,
    }


def validation_role_key(row: dict[str, Any]) -> str:
    """Return the experimental validation family independently of dominant axis."""
    data_role = str(row.get("data_role", "")).strip().lower()
    trajectory = str(row.get("trajectory", "")).strip().lower()
    sweep_name = str(row.get("sweep_name", "")).strip().lower()
    if sweep_name.startswith("cuboid_"):
        return "cuboid"
    if data_role == "validation_axis" or trajectory == "axis":
        return "single_axis"
    if data_role == "validation_depth" or trajectory == "depth":
        return "depth"
    if data_role == "validation_diagonal" or trajectory == "diagonal":
        return "diagonal"
    return "other"


def capture_purpose(
    row: dict[str, Any],
    *,
    orientation_ids: set[str],
    primary_ids: set[str],
    secondary_ids: set[str],
    return_ids: set[str],
) -> tuple[str, str, str]:
    """Return a compact purpose key, Japanese label, and analysis destination."""
    capture_id = str(row["capture_id"])
    data_role = str(row.get("data_role", "")).strip().lower()
    if capture_id in orientation_ids:
        return (
            "orientation_fit",
            "Orientation登録：camera→stageの剛体変換R,tを求める",
            "orientation fit + measurements.csv",
        )
    if data_role == "registration_center_pre":
        return (
            "orientation_reference_pre",
            "Orientation登録前の中心確認点",
            "measurements.csv",
        )
    if data_role == "registration_center_post":
        return (
            "orientation_reference_post",
            "Orientation登録後の中心確認点",
            "measurements.csv",
        )
    if data_role == "validation_center_pre":
        return (
            "pass_reference",
            "validation pass直前の差分基準点",
            "measurements.csv; referenced by validation_accuracy.csv",
        )
    if capture_id in return_ids or data_role == "validation_center_post":
        return (
            "return_drift",
            "validation pass後の基準復帰・時間drift確認点",
            "return_drift.csv + measurements.csv",
        )
    family = validation_role_key(row)
    if family == "single_axis":
        return (
            "primary_single_axis",
            "Single-axis検証：基準点からの1軸変位を主評価",
            "validation_accuracy.csv; primary summary",
        )
    if family == "depth":
        return (
            "primary_depth",
            "Depth検証：基準点からのY変位と距離依存性を主評価",
            "validation_accuracy.csv; primary summary",
        )
    if family == "diagonal":
        return (
            "secondary_diagonal",
            "Diagonal検証：基準点からの複合XYZ変位を副評価",
            "validation_accuracy.csv; secondary summary",
        )
    if family == "cuboid":
        return (
            "secondary_cuboid",
            "3D直方体表面点：寸法・面内・奥行位置誤差の追加評価",
            "validation_accuracy.csv; stereo_3axis_cuboid_analyze.py",
        )
    if capture_id in primary_ids:
        return (
            "primary_validation",
            "分類未指定の主validation",
            "validation_accuracy.csv; primary summary",
        )
    if capture_id in secondary_ids:
        return (
            "secondary_validation",
            "分類未指定の副validation",
            "validation_accuracy.csv; secondary summary",
        )
    return ("other", "その他のcapture", "measurements.csv")


def build_capture_role_map(
    rows: list[dict[str, Any]],
    *,
    orientation: dict[str, Any],
    primary_validation: list[dict[str, Any]],
    secondary_validation: list[dict[str, Any]],
    return_checks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build a concise all-capture map including reference and analysis membership."""
    by_id = {str(row["capture_id"]): row for row in rows}
    orientation_ids = {
        str(value) for value in orientation.get("orientation_capture_ids", [])
    }
    primary_ids = {str(row["capture_id"]) for row in primary_validation}
    secondary_ids = {str(row["capture_id"]) for row in secondary_validation}
    return_ids = {str(row["capture_id"]) for row in return_checks}
    output: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: int(item["sequence_index"])):
        capture_id = str(row["capture_id"])
        reference_id = str(row.get("reference_id", ""))
        reference = by_id.get(reference_id)
        target = np.asarray(row["plan_target_mm"], dtype=np.float64)
        reference_target = (
            np.asarray(reference["plan_target_mm"], dtype=np.float64)
            if reference is not None
            else np.full(3, np.nan)
        )
        intended_delta = target - reference_target
        purpose_key, purpose_ja, output_destination = capture_purpose(
            row,
            orientation_ids=orientation_ids,
            primary_ids=primary_ids,
            secondary_ids=secondary_ids,
            return_ids=return_ids,
        )
        stage = np.asarray(row["stage_readback_mm"], dtype=np.float64)
        hardware = np.asarray(
            row.get("stage_hardware_readback_mm", np.full(3, np.nan)),
            dtype=np.float64,
        )
        output.append(
            {
                "capture_id": capture_id,
                "sequence_index": int(row["sequence_index"]),
                "purpose_key": purpose_key,
                "purpose_ja": purpose_ja,
                "role": row.get("role", ""),
                "data_role": row.get("data_role", ""),
                "trajectory": row.get("trajectory", ""),
                "sweep_name": row.get("sweep_name", ""),
                "direction": row.get("direction", ""),
                "cycle": row.get("cycle", 0),
                "reference_id": reference_id,
                "plan_target_x_mm": target[0],
                "plan_target_y_mm": target[1],
                "plan_target_z_mm": target[2],
                "reference_target_x_mm": reference_target[0],
                "reference_target_y_mm": reference_target[1],
                "reference_target_z_mm": reference_target[2],
                "intended_delta_x_mm": intended_delta[0],
                "intended_delta_y_mm": intended_delta[1],
                "intended_delta_z_mm": intended_delta[2],
                "intended_delta_norm_mm": (
                    float(np.linalg.norm(intended_delta))
                    if np.all(np.isfinite(intended_delta))
                    else float("nan")
                ),
                "stage_readback_x_mm": stage[0],
                "stage_readback_y_mm": stage[1],
                "stage_readback_z_mm": stage[2],
                "stage_hardware_x_mm": hardware[0],
                "stage_hardware_y_mm": hardware[1],
                "stage_hardware_z_mm": hardware[2],
                "dominant_axis": row.get("dominant_axis", ""),
                "usable": bool(row.get("usable", False)),
                "included_in_orientation_fit": capture_id in orientation_ids,
                "included_in_primary_summary": capture_id in primary_ids,
                "included_in_secondary_summary": capture_id in secondary_ids,
                "included_in_return_drift": capture_id in return_ids,
                "analysis_destination": output_destination,
            }
        )
    return output


def flatten_measurements(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        camera = np.asarray(row.get("center_camera_mm", np.full(3, np.nan)))
        measured_stage = np.asarray(
            row.get("measured_stage_frame_mm", np.full(3, np.nan))
        )
        absolute_stage_residual = np.asarray(
            row.get("absolute_stage_frame_residual_mm", np.full(3, np.nan))
        )
        stage = np.asarray(row["stage_readback_mm"])
        stage_hardware = np.asarray(
            row.get("stage_hardware_readback_mm", np.full(3, np.nan))
        )
        target = np.asarray(row["plan_target_mm"])
        before = np.asarray(row.get("stage_before_capture_mm", np.full(3, np.nan)))
        after = np.asarray(row.get("stage_after_capture_mm", np.full(3, np.nan)))
        drift = np.asarray(row.get("stage_drift_during_capture_mm", np.full(3, np.nan)))
        output.append(
            {
                "capture_id": row["capture_id"],
                "sequence_index": row["sequence_index"],
                "role": row["role"],
                "reference_id": row["reference_id"],
                "direction": row["direction"],
                "cycle": row["cycle"],
                "trajectory": row.get("trajectory", ""),
                "sweep_name": row.get("sweep_name", ""),
                "data_role": row.get("data_role", ""),
                "primary_evaluation": bool(row.get("primary_evaluation", False)),
                "return_check": bool(row.get("return_check", False)),
                "usable": row["usable"],
                "reason": row["reason"],
                "stage_readback_x_mm": stage[0],
                "stage_readback_y_mm": stage[1],
                "stage_readback_z_mm": stage[2],
                "stage_readback_source": row.get("stage_readback_source", ""),
                "stage_hardware_x_mm": stage_hardware[0],
                "stage_hardware_y_mm": stage_hardware[1],
                "stage_hardware_z_mm": stage_hardware[2],
                "stage_before_x_mm": before[0],
                "stage_before_y_mm": before[1],
                "stage_before_z_mm": before[2],
                "stage_after_x_mm": after[0],
                "stage_after_y_mm": after[1],
                "stage_after_z_mm": after[2],
                "stage_drift_x_mm": drift[0],
                "stage_drift_y_mm": drift[1],
                "stage_drift_z_mm": drift[2],
                "plan_target_x_mm": target[0],
                "plan_target_y_mm": target[1],
                "plan_target_z_mm": target[2],
                "camera_x_mm": camera[0],
                "camera_y_mm": camera[1],
                "camera_z_mm": camera[2],
                "measured_stage_x_mm": measured_stage[0],
                "measured_stage_y_mm": measured_stage[1],
                "measured_stage_z_mm": measured_stage[2],
                "absolute_stage_residual_x_mm": absolute_stage_residual[0],
                "absolute_stage_residual_y_mm": absolute_stage_residual[1],
                "absolute_stage_residual_z_mm": absolute_stage_residual[2],
                "camera_forward_z_mm": row.get(
                    "camera_forward_z_mm",
                    camera[2],
                ),
                "camera_left_optical_center_slant_distance_mm": row.get(
                    "camera_left_optical_center_slant_distance_mm",
                    float(np.linalg.norm(camera)),
                ),
                "robust_samples": row.get("robust_samples", 0),
                "raw_valid_fraction": row.get("raw_valid_fraction", 0.0),
                "scatter_rms_mm": row.get("scatter_rms_mm", float("nan")),
                "scatter_p95_mm": row.get("scatter_p95_mm", float("nan")),
                "absolute_distance_label_mm": row.get(
                    "absolute_distance_label_mm", float("nan")
                ),
                "absolute_distance_standard_uncertainty_mm": row.get(
                    "absolute_distance_standard_uncertainty_mm",
                    float("nan"),
                ),
                "absolute_baseline_distance_label_mm": row.get(
                    "absolute_baseline_distance_label_mm", float("nan")
                ),
                "absolute_baseline_distance_standard_uncertainty_mm": row.get(
                    "absolute_baseline_distance_standard_uncertainty_mm",
                    float("nan"),
                ),
                "stereo_baseline_distance_estimate_mm": row.get(
                    "stereo_baseline_distance_estimate_mm",
                    float("nan"),
                ),
                "stereo_minus_mechanical_baseline_distance_mm": row.get(
                    "stereo_minus_mechanical_baseline_distance_mm",
                    float("nan"),
                ),
                "stereo_3d_npz": str(row["stereo_3d_npz"] or ""),
            }
        )
    return output


def write_camera_to_stage_transform(
    path: Path,
    orientation: dict[str, Any],
) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with temporary.open("wb") as stream:
        np.savez_compressed(
            stream,
            R_camera_to_stage=np.asarray(
                orientation["rotation_camera_to_stage"],
                dtype=np.float64,
            ),
            t_camera_to_stage_mm=np.asarray(
                orientation["translation_stage_mm"],
                dtype=np.float64,
            ),
            rigid_fit_residual_rms_mm=np.asarray(
                float(orientation["rigid_fit_residual_rms_mm"]),
                dtype=np.float64,
            ),
            rigid_fit_residual_max_mm=np.asarray(
                float(orientation["rigid_fit_residual_max_mm"]),
                dtype=np.float64,
            ),
            rotation_determinant=np.asarray(
                float(orientation["rotation_determinant"]),
                dtype=np.float64,
            ),
            orientation_capture_ids=np.asarray(
                orientation["orientation_capture_ids"],
                dtype=np.str_,
            ),
            method=np.asarray(str(orientation["method"]), dtype=np.str_),
            equation=np.asarray(
                "p_stage = R_camera_to_stage @ p_left_camera "
                "+ t_camera_to_stage_mm",
                dtype=np.str_,
            ),
        )
    atomic_replace_with_retry(temporary, path)


def flatten_validation(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        dc = np.asarray(row["delta_camera_mm"])
        ds = np.asarray(row["delta_stage_mm"])
        observed = np.asarray(row["observed_delta_stage_frame_mm"])
        residual = np.asarray(row["delta_residual_stage_frame_mm"])
        target = np.asarray(row["plan_target_mm"])
        reference_target = np.asarray(
            row.get("reference_plan_target_mm", np.full(3, np.nan))
        )
        output.append(
            {
                "capture_id": row["capture_id"],
                "reference_id": row["reference_id"],
                "data_role": row.get("data_role", ""),
                "trajectory": row.get("trajectory", ""),
                "validation_family": validation_role_key(row),
                "summary_group": row.get("summary_group", ""),
                "direction": row["direction"],
                "cycle": row["cycle"],
                "dominant_axis": row["dominant_axis"],
                "plan_target_x_mm": target[0],
                "plan_target_y_mm": target[1],
                "plan_target_z_mm": target[2],
                "reference_target_x_mm": reference_target[0],
                "reference_target_y_mm": reference_target[1],
                "reference_target_z_mm": reference_target[2],
                "delta_camera_x_mm": dc[0],
                "delta_camera_y_mm": dc[1],
                "delta_camera_z_mm": dc[2],
                "delta_stage_x_mm": ds[0],
                "delta_stage_y_mm": ds[1],
                "delta_stage_z_mm": ds[2],
                "observed_stage_x_mm": observed[0],
                "observed_stage_y_mm": observed[1],
                "observed_stage_z_mm": observed[2],
                "residual_stage_x_mm": residual[0],
                "residual_stage_y_mm": residual[1],
                "residual_stage_z_mm": residual[2],
                "camera_displacement_norm_mm": row["camera_displacement_norm_mm"],
                "stage_displacement_norm_mm": row["stage_displacement_norm_mm"],
                "stage_depth_from_reference_mm": row.get(
                    "stage_depth_from_reference_mm",
                    float("nan"),
                ),
                "distance_norm_error_mm": row["distance_norm_error_mm"],
                "axial_error_mm": row["axial_error_mm"],
                "lateral_error_mm": row["lateral_error_mm"],
                "error_3d_mm": row["error_3d_mm"],
                "return_drift_3d_mm": row["return_drift_3d_mm"],
                "stage_return_offset_3d_mm": row.get(
                    "stage_return_offset_3d_mm",
                    float("nan"),
                ),
                "camera_return_shift_3d_mm": row.get(
                    "camera_return_shift_3d_mm",
                    float("nan"),
                ),
                "return_residual_3d_mm": row.get(
                    "return_residual_3d_mm",
                    float("nan"),
                ),
                "absolute_distance_label_mm": row.get(
                    "absolute_distance_label_mm", float("nan")
                ),
                "absolute_baseline_distance_label_mm": row.get(
                    "absolute_baseline_distance_label_mm", float("nan")
                ),
                "stereo_baseline_distance_estimate_mm": row.get(
                    "stereo_baseline_distance_estimate_mm",
                    float("nan"),
                ),
                "stereo_minus_mechanical_baseline_distance_mm": row.get(
                    "stereo_minus_mechanical_baseline_distance_mm",
                    float("nan"),
                ),
            }
        )
    return output


def make_plots(
    output_dir: Path,
    validation: list[dict[str, Any]],
    distance_config: dict[str, Any],
) -> list[str]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.lines import Line2D
    except ImportError as exc:
        raise SystemExit(f"matplotlib is required to create analysis plots: {exc}") from exc
    if not validation:
        return []

    def role_indices(role_key: str) -> list[int]:
        return [
            index
            for index, row in enumerate(validation)
            if validation_role_key(row) == role_key
        ]

    direction_styles = {
        "positive": ("#1f77b4", "o", "Positive approach"),
        "negative": ("#ff7f0e", "^", "Negative approach"),
        "other": ("#7f7f7f", "s", "Other approach"),
    }
    line_styles = {
        "positive": "-",
        "negative": "--",
        "other": ":",
    }

    def direction_matches(row: dict[str, Any], direction_key: str) -> bool:
        direction = str(row.get("direction", "")).lower()
        if direction_key == "other":
            return direction not in {"positive", "negative"}
        return direction == direction_key

    def jitter(values: np.ndarray, fraction: float = 0.045) -> np.ndarray:
        """Apply deterministic display-only jitter to exactly overlapping X values."""
        result = np.asarray(values, dtype=np.float64).copy()
        finite = result[np.isfinite(result)]
        unique = np.unique(np.round(finite, decimals=9))
        if unique.size > 1:
            span = max(1e-6, float(np.min(np.diff(unique))) * fraction)
        else:
            span = 0.1
        for value in unique:
            indices = np.flatnonzero(np.isclose(result, value, rtol=0.0, atol=1e-9))
            if indices.size > 1:
                result[indices] += np.linspace(-span, span, indices.size)
        return result

    # Split the two overview figures by validation family. Single-axis validation
    # is also split by axis because its error mechanism is direction-dependent.
    panel_groups: list[tuple[str, list[int]]] = []
    for axis_name in AXIS_NAMES:
        indices = [
            index
            for index, row in enumerate(validation)
            if validation_role_key(row) == "single_axis"
            and str(row.get("dominant_axis", "")) == axis_name
        ]
        if indices:
            panel_groups.append((f"Single-axis {axis_name.upper()}", indices))
    for role_key, label in (("depth", "Depth"), ("diagonal", "Diagonal")):
        indices = role_indices(role_key)
        if indices:
            panel_groups.append((label, indices))
    grouped_indices = {index for _, indices in panel_groups for index in indices}
    other_indices = [
        index for index in range(len(validation)) if index not in grouped_indices
    ]
    if other_indices:
        panel_groups.append(("Other validation", other_indices))

    displacement = np.asarray(
        [row["stage_displacement_norm_mm"] for row in validation],
        dtype=np.float64,
    )
    distance_error = np.asarray(
        [row["distance_norm_error_mm"] for row in validation],
        dtype=np.float64,
    )
    cycle_values = sorted(
        {str(row.get("cycle", "?")) for row in validation},
        key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value),
    )
    cycle_palette = [
        "#1f77b4",
        "#ff7f0e",
        "#2ca02c",
        "#d62728",
        "#9467bd",
        "#8c564b",
    ]
    cycle_colors = {
        cycle: cycle_palette[index % len(cycle_palette)]
        for index, cycle in enumerate(cycle_values)
    }
    present_direction_keys = [
        direction_key
        for direction_key in direction_styles
        if any(direction_matches(row, direction_key) for row in validation)
    ]
    columns = 2
    rows_count = int(math.ceil(len(panel_groups) / columns))
    figure, subplot_grid = plt.subplots(
        rows_count,
        columns,
        figsize=(11.5, 4.0 * rows_count + 1.0),
        squeeze=False,
    )
    flat_axes = list(subplot_grid.flat)
    for subplot, (label, indices) in zip(flat_axes, panel_groups):
        display_x = displacement[indices]
        for cycle in cycle_values:
            for direction_key, (_, marker, _) in direction_styles.items():
                local_indices = [
                    local_index
                    for local_index, validation_index in enumerate(indices)
                    if str(validation[validation_index].get("cycle", "?")) == cycle
                    and direction_matches(validation[validation_index], direction_key)
                ]
                if not local_indices:
                    continue
                subplot.scatter(
                    display_x[local_indices],
                    distance_error[np.asarray(indices)[local_indices]],
                    s=35,
                    marker=marker,
                    color=cycle_colors[cycle],
                    edgecolor="white",
                    linewidth=0.45,
                    alpha=0.72,
                )
        group_error = distance_error[indices]
        bias = float(np.mean(group_error))
        rms = float(np.sqrt(np.mean(np.square(group_error))))
        subplot.axhline(0.0, color="black", linewidth=0.9)
        subplot.set_xlabel("Stage displacement norm from pass reference [mm]")
        subplot.set_ylabel("||delta camera|| - ||delta stage|| [mm]")
        subplot.set_title(
            f"{label} (n={len(indices)})\nBias={bias:+.3f} mm, RMS={rms:.3f} mm"
        )
        subplot.grid(True, alpha=0.28)
    for subplot in flat_axes[len(panel_groups) :]:
        subplot.set_visible(False)
    cycle_handles = [
        Line2D(
            [],
            [],
            linestyle="",
            marker="o",
            markersize=7,
            markerfacecolor=cycle_colors[cycle],
            markeredgecolor="white",
            label=f"Cycle {cycle}",
        )
        for cycle in cycle_values
    ]
    direction_handles = [
        Line2D(
            [],
            [],
            linestyle="",
            marker=marker,
            markersize=7,
            markerfacecolor="#777777",
            markeredgecolor="white",
            label=label,
        )
        for direction_key, (_, marker, label) in direction_styles.items()
        if direction_key in present_direction_keys
    ]
    figure.suptitle(
        "Transformation-free stereo distance error — validation groups separated\n"
        "Each panel uses its own Y scale",
        y=0.985,
    )
    figure.legend(
        handles=[*cycle_handles, *direction_handles],
        loc="upper center",
        bbox_to_anchor=(0.5, 0.91),
        ncols=min(6, len(cycle_handles) + len(direction_handles)),
        framealpha=0.95,
    )
    figure.text(
        0.5,
        0.02,
        "Horizontal position is displacement from each pass reference, not absolute stage Y. "
        "No horizontal jitter is applied; overlapping markers indicate close repeats.",
        ha="center",
        fontsize=8,
        color="#444444",
    )
    figure.subplots_adjust(
        left=0.08,
        right=0.98,
        bottom=0.09,
        top=0.82,
        wspace=0.25,
        hspace=0.38,
    )
    distance_plot = output_dir / "stereo_3axis_distance_error.png"
    figure.savefig(distance_plot, dpi=180)
    plt.close(figure)

    if distance_config.get("available"):
        horizontal = np.asarray(
            [row["absolute_distance_label_mm"] for row in validation],
            dtype=np.float64,
        )
        horizontal_label = "Independent mechanical distance label [mm]"
    else:
        horizontal = displacement
        horizontal_label = "Stage displacement norm from each pass reference [mm]"
    residual = np.vstack(
        [row["delta_residual_stage_frame_mm"] for row in validation]
    )
    figure, subplot_grid = plt.subplots(
        rows_count,
        columns,
        figsize=(11.5, 4.0 * rows_count + 1.0),
        squeeze=False,
    )
    flat_axes = list(subplot_grid.flat)
    for subplot, (label, indices) in zip(flat_axes, panel_groups):
        group_x = horizontal[indices]
        display_x = group_x
        for component_index, component in enumerate(AXIS_NAMES):
            for direction_key, (_, marker, _) in direction_styles.items():
                local_indices = [
                    local_index
                    for local_index, validation_index in enumerate(indices)
                    if direction_matches(validation[validation_index], direction_key)
                ]
                if not local_indices:
                    continue
                subplot.scatter(
                    display_x[local_indices],
                    residual[np.asarray(indices)[local_indices], component_index],
                    s=32,
                    marker=marker,
                    color=AXIS_FACE_COLORS[component],
                    edgecolor="white",
                    linewidth=0.45,
                    alpha=0.68,
                )
        component_rms = np.sqrt(np.mean(np.square(residual[indices]), axis=0))
        subplot.axhline(0.0, color="black", linewidth=0.9)
        subplot.set_xlabel(horizontal_label)
        subplot.set_ylabel("Stage-frame residual [mm]")
        subplot.set_title(
            f"{label} (n={len(indices)})\n"
            f"RMS X/Y/Z={component_rms[0]:.3f}/{component_rms[1]:.3f}/"
            f"{component_rms[2]:.3f} mm"
        )
        subplot.grid(True, alpha=0.28)
    for subplot in flat_axes[len(panel_groups) :]:
        subplot.set_visible(False)
    component_handles = [
        Line2D(
            [],
            [],
            linestyle="",
            marker="o",
            markersize=7,
            markerfacecolor=AXIS_FACE_COLORS[component],
            markeredgecolor="white",
            label=f"Residual {component.upper()}",
        )
        for component in AXIS_NAMES
    ]
    figure.suptitle(
        "Residual components in stage-axis orientation — validation groups separated\n"
        "Each panel uses its own Y scale",
        y=0.985,
    )
    figure.legend(
        handles=[*component_handles, *direction_handles],
        loc="upper center",
        bbox_to_anchor=(0.5, 0.88),
        ncols=min(6, len(component_handles) + len(direction_handles)),
        framealpha=0.95,
    )
    figure.text(
        0.5,
        0.02,
        "No horizontal jitter is applied; overlapping markers indicate close repeats.",
        ha="center",
        fontsize=8,
        color="#444444",
    )
    figure.subplots_adjust(
        left=0.08,
        right=0.98,
        bottom=0.09,
        top=0.79,
        wspace=0.25,
        hspace=0.38,
    )
    residual_plot = output_dir / "stereo_3axis_residual_components.png"
    figure.savefig(residual_plot, dpi=180)
    plt.close(figure)

    detail_plots: list[Path] = []

    # Show how the transformed 3D residual separates into along-motion and
    # cross-motion contributions. This complements the transformation-free
    # distance-norm error without implying that the metrics are interchangeable.
    error_metric_specs = [
        (
            "Axial (signed)",
            np.asarray([row["axial_error_mm"] for row in validation], dtype=np.float64),
            "#1f77b4",
        ),
        (
            "Lateral magnitude",
            np.asarray([row["lateral_error_mm"] for row in validation], dtype=np.float64),
            "#ff7f0e",
        ),
        (
            "3D magnitude",
            np.asarray([row["error_3d_mm"] for row in validation], dtype=np.float64),
            "#2ca02c",
        ),
    ]
    figure, subplot_grid = plt.subplots(
        rows_count,
        columns,
        figsize=(11.5, 4.0 * rows_count + 1.0),
        squeeze=False,
    )
    flat_axes = list(subplot_grid.flat)
    validation_index_array = np.arange(len(validation), dtype=np.int64)
    for subplot, (label, indices) in zip(flat_axes, panel_groups):
        group_index_array = validation_index_array[indices]
        for metric_label, metric_values, metric_color in error_metric_specs:
            for direction_key, (_, marker, _) in direction_styles.items():
                local_indices = [
                    local_index
                    for local_index, validation_index in enumerate(indices)
                    if direction_matches(validation[validation_index], direction_key)
                ]
                if not local_indices:
                    continue
                subplot.scatter(
                    horizontal[group_index_array[local_indices]],
                    metric_values[group_index_array[local_indices]],
                    s=30,
                    marker=marker,
                    color=metric_color,
                    edgecolor="white",
                    linewidth=0.4,
                    alpha=0.65,
                )
        metric_rms = [
            float(np.sqrt(np.mean(np.square(values[indices]))))
            for _, values, _ in error_metric_specs
        ]
        subplot.axhline(0.0, color="black", linewidth=0.9)
        subplot.set_xlabel(horizontal_label)
        subplot.set_ylabel("Error [mm]")
        subplot.set_title(
            f"{label} (n={len(indices)})\n"
            f"RMS axial/lateral/3D={metric_rms[0]:.3f}/"
            f"{metric_rms[1]:.3f}/{metric_rms[2]:.3f} mm"
        )
        subplot.grid(True, alpha=0.28)
    for subplot in flat_axes[len(panel_groups) :]:
        subplot.set_visible(False)
    metric_handles = [
        Line2D(
            [],
            [],
            linestyle="",
            marker="o",
            markersize=7,
            markerfacecolor=color,
            markeredgecolor="white",
            label=label,
        )
        for label, _, color in error_metric_specs
    ]
    figure.suptitle(
        "Axial, lateral, and 3D validation errors — validation groups separated\n"
        "Metrics depend on the fitted camera-to-stage rotation",
        y=0.985,
    )
    figure.legend(
        handles=[*metric_handles, *direction_handles],
        loc="upper center",
        bbox_to_anchor=(0.5, 0.88),
        ncols=min(5, len(metric_handles) + len(direction_handles)),
        framealpha=0.95,
    )
    figure.text(
        0.5,
        0.02,
        "Axial is signed; lateral and 3D are non-negative magnitudes. No jitter is applied.",
        ha="center",
        fontsize=8,
        color="#444444",
    )
    figure.subplots_adjust(
        left=0.08,
        right=0.98,
        bottom=0.09,
        top=0.79,
        wspace=0.25,
        hspace=0.38,
    )
    error_decomposition_path = output_dir / "validation_error_decomposition.png"
    figure.savefig(error_decomposition_path, dpi=180)
    plt.close(figure)
    detail_plots.append(error_decomposition_path)

    def scatter_by_direction(
        plot_axis: Any,
        x_values: np.ndarray,
        y_values: np.ndarray,
        members: list[dict[str, Any]],
    ) -> None:
        display_x = jitter(x_values)
        for direction_key, (color, marker, label) in direction_styles.items():
            indices = [
                index
                for index, row in enumerate(members)
                if (
                    str(row.get("direction", "")).lower() == direction_key
                    if direction_key != "other"
                    else str(row.get("direction", "")).lower()
                    not in {"positive", "negative"}
                )
            ]
            if not indices:
                continue
            plot_axis.scatter(
                display_x[indices],
                y_values[indices],
                s=34,
                color=color,
                marker=marker,
                alpha=0.72,
                edgecolor="white",
                linewidth=0.45,
                label=label,
            )

    heatmap_rows: list[dict[str, Any]] = []

    # Single-axis validation is split first by X/Y/Z and then by reference-depth
    # plane. This prevents the five local sweeps from hiding one another.
    for axis_name in AXIS_NAMES:
        members = [
            row
            for row in validation
            if validation_role_key(row) == "single_axis"
            and str(row.get("dominant_axis", "")) == axis_name
        ]
        if not members:
            continue
        axis_index = AXIS_NAMES.index(axis_name)
        reference_depths = sorted(
            {
                round(float(np.asarray(row["reference_plan_target_mm"])[1]), 6)
                for row in members
            }
        )
        columns = 2
        rows_count = int(math.ceil(len(reference_depths) / columns))
        figure, subplot_grid = plt.subplots(
            rows_count,
            columns,
            figsize=(11.0, 3.7 * rows_count),
            squeeze=False,
        )
        flat_axes = list(subplot_grid.flat)
        for subplot, reference_depth in zip(flat_axes, reference_depths):
            plane = [
                row
                for row in members
                if math.isclose(
                    float(np.asarray(row["reference_plan_target_mm"])[1]),
                    reference_depth,
                    rel_tol=0.0,
                    abs_tol=1e-6,
                )
            ]
            horizontal = np.asarray(
                [np.asarray(row["delta_stage_mm"])[axis_index] for row in plane],
                dtype=np.float64,
            )
            vertical = np.asarray(
                [row["distance_norm_error_mm"] for row in plane],
                dtype=np.float64,
            )
            scatter_by_direction(subplot, horizontal, vertical, plane)
            subplot.axhline(0.0, color="black", linewidth=0.9)
            subplot.set_title(
                f"Reference Y={reference_depth:g} mm (n={len(plane)})"
            )
            subplot.set_xlabel(f"Signed stage delta {axis_name.upper()} [mm]")
            subplot.set_ylabel("Distance-norm error [mm]")
            subplot.grid(True, alpha=0.28)
        for subplot in flat_axes[len(reference_depths) :]:
            subplot.set_visible(False)
        handles, labels = flat_axes[0].get_legend_handles_labels()
        if handles:
            figure.legend(
                handles,
                labels,
                loc="upper center",
                bbox_to_anchor=(0.5, 0.94),
                ncols=3,
            )
        figure.suptitle(
            f"Single-axis {axis_name.upper()} validation — reference depths separated\n"
            "Horizontal jitter is display-only for repeated measurements",
            y=0.995,
        )
        figure.tight_layout(rect=(0.02, 0.02, 0.98, 0.89))
        detail_path = output_dir / f"validation_axis_{axis_name}.png"
        figure.savefig(detail_path, dpi=180)
        plt.close(figure)
        detail_plots.append(detail_path)

        # Complementary view: hold the signed X/Z displacement fixed and expose
        # how its distance error changes with reference depth and acquisition cycle.
        planned_deltas = sorted(
            {
                round(
                    float(np.asarray(row["plan_target_mm"])[axis_index])
                    - float(
                        np.asarray(row["reference_plan_target_mm"])[axis_index]
                    ),
                    6,
                )
                for row in members
            }
        )
        delta_columns = 3
        delta_rows_count = int(math.ceil(len(planned_deltas) / delta_columns))
        figure, subplot_grid = plt.subplots(
            delta_rows_count,
            delta_columns,
            figsize=(13.5, 3.7 * delta_rows_count + 1.0),
            sharex=True,
            sharey=True,
            squeeze=False,
        )
        flat_axes = list(subplot_grid.flat)
        for subplot, planned_delta in zip(flat_axes, planned_deltas):
            delta_members = [
                row
                for row in members
                if math.isclose(
                    float(np.asarray(row["plan_target_mm"])[axis_index])
                    - float(
                        np.asarray(row["reference_plan_target_mm"])[axis_index]
                    ),
                    planned_delta,
                    rel_tol=0.0,
                    abs_tol=1e-6,
                )
            ]
            for cycle in cycle_values:
                for direction_key, (_, marker, _) in direction_styles.items():
                    series = sorted(
                        [
                            row
                            for row in delta_members
                            if str(row.get("cycle", "?")) == cycle
                            and direction_matches(row, direction_key)
                        ],
                        key=lambda row: float(
                            np.asarray(row["reference_plan_target_mm"])[1]
                        ),
                    )
                    if not series:
                        continue
                    subplot.plot(
                        [
                            float(np.asarray(row["reference_plan_target_mm"])[1])
                            for row in series
                        ],
                        [float(row["distance_norm_error_mm"]) for row in series],
                        color=cycle_colors[cycle],
                        marker=marker,
                        linestyle=line_styles[direction_key],
                        linewidth=0.9,
                        markersize=4.5,
                        alpha=0.72,
                    )
            subplot.axhline(0.0, color="black", linewidth=0.9)
            subplot.set_title(
                f"Signed delta {axis_name.upper()}={planned_delta:+g} mm "
                f"(n={len(delta_members)})"
            )
            subplot.set_xlabel("Reference Y [mm]")
            subplot.set_ylabel("Distance-norm error [mm]")
            subplot.set_xticks(reference_depths)
            subplot.grid(True, alpha=0.28)
        for subplot in flat_axes[len(planned_deltas) :]:
            subplot.set_visible(False)
        figure.suptitle(
            f"Single-axis {axis_name.upper()} validation — depth dependence at fixed displacement",
            y=0.985,
        )
        figure.legend(
            handles=[*cycle_handles, *direction_handles],
            loc="upper center",
            bbox_to_anchor=(0.5, 0.91),
            ncols=min(5, len(cycle_handles) + len(direction_handles)),
            framealpha=0.95,
        )
        figure.text(
            0.5,
            0.02,
            "Lines connect the same cycle/approach across reference depths as a visual guide; "
            "no horizontal jitter is applied.",
            ha="center",
            fontsize=8,
            color="#444444",
        )
        figure.subplots_adjust(
            left=0.07,
            right=0.98,
            bottom=0.11,
            top=0.82,
            wspace=0.22,
            hspace=0.32,
        )
        by_delta_path = output_dir / f"validation_axis_{axis_name}_by_delta.png"
        figure.savefig(by_delta_path, dpi=180)
        plt.close(figure)
        detail_plots.append(by_delta_path)

        mean_grid = np.full(
            (len(planned_deltas), len(reference_depths)),
            np.nan,
            dtype=np.float64,
        )
        std_grid = np.full_like(mean_grid, np.nan)
        for delta_index, planned_delta in enumerate(planned_deltas):
            for depth_index, reference_depth in enumerate(reference_depths):
                cell = [
                    row
                    for row in members
                    if math.isclose(
                        float(np.asarray(row["plan_target_mm"])[axis_index])
                        - float(
                            np.asarray(row["reference_plan_target_mm"])[axis_index]
                        ),
                        planned_delta,
                        rel_tol=0.0,
                        abs_tol=1e-6,
                    )
                    and math.isclose(
                        float(np.asarray(row["reference_plan_target_mm"])[1]),
                        reference_depth,
                        rel_tol=0.0,
                        abs_tol=1e-6,
                    )
                ]
                values = np.asarray(
                    [row["distance_norm_error_mm"] for row in cell],
                    dtype=np.float64,
                )
                if values.size:
                    mean_grid[delta_index, depth_index] = float(np.mean(values))
                    std_grid[delta_index, depth_index] = (
                        float(np.std(values, ddof=1)) if values.size > 1 else 0.0
                    )
                    heatmap_rows.append(
                        {
                            "axis": axis_name,
                            "signed_delta_mm": planned_delta,
                            "reference_y_mm": reference_depth,
                            "count": int(values.size),
                            "distance_error_mean_mm": float(np.mean(values)),
                            "distance_error_std_mm": (
                                float(np.std(values, ddof=1))
                                if values.size > 1
                                else 0.0
                            ),
                            "distance_error_rms_mm": float(
                                np.sqrt(np.mean(np.square(values)))
                            ),
                            "distance_error_min_mm": float(np.min(values)),
                            "distance_error_max_mm": float(np.max(values)),
                        }
                    )
        figure, heatmap_axes = plt.subplots(
            1,
            2,
            figsize=(12.0, 5.8),
            constrained_layout=True,
        )
        finite_mean = np.abs(mean_grid[np.isfinite(mean_grid)])
        mean_limit = float(np.max(finite_mean)) if finite_mean.size else 1.0
        mean_limit = max(mean_limit, 1e-9)
        mean_image = heatmap_axes[0].imshow(
            mean_grid,
            origin="lower",
            aspect="auto",
            cmap="coolwarm",
            vmin=-mean_limit,
            vmax=mean_limit,
        )
        std_image = heatmap_axes[1].imshow(
            std_grid,
            origin="lower",
            aspect="auto",
            cmap="viridis",
            vmin=0.0,
            vmax=max(float(np.nanmax(std_grid)), 1e-9),
        )
        for plot_axis, values, title in (
            (heatmap_axes[0], mean_grid, "Mean distance error [mm]"),
            (heatmap_axes[1], std_grid, "Repeatability standard deviation [mm]"),
        ):
            plot_axis.set_xticks(range(len(reference_depths)))
            plot_axis.set_xticklabels([f"{value:g}" for value in reference_depths])
            plot_axis.set_yticks(range(len(planned_deltas)))
            plot_axis.set_yticklabels([f"{value:+g}" for value in planned_deltas])
            plot_axis.set_xlabel("Reference Y [mm]")
            plot_axis.set_ylabel(f"Signed delta {axis_name.upper()} [mm]")
            plot_axis.set_title(title)
            for row_index in range(values.shape[0]):
                for column_index in range(values.shape[1]):
                    value = float(values[row_index, column_index])
                    if math.isfinite(value):
                        plot_axis.text(
                            column_index,
                            row_index,
                            f"{value:.3f}",
                            ha="center",
                            va="center",
                            fontsize=7.5,
                            color="black",
                        )
        figure.colorbar(mean_image, ax=heatmap_axes[0], shrink=0.86)
        figure.colorbar(std_image, ax=heatmap_axes[1], shrink=0.86)
        figure.suptitle(
            f"Single-axis {axis_name.upper()} distance-error map — systematic versus repeatability"
        )
        heatmap_path = output_dir / f"validation_axis_{axis_name}_heatmap.png"
        figure.savefig(heatmap_path, dpi=180)
        plt.close(figure)
        detail_plots.append(heatmap_path)

    if heatmap_rows:
        write_csv(output_dir / "validation_axis_heatmap.csv", heatmap_rows)

    depth_members = [
        row for row in validation if validation_role_key(row) == "depth"
    ]
    if depth_members:
        depth_x = np.asarray(
            [row.get("absolute_distance_label_mm", float("nan")) for row in depth_members],
            dtype=np.float64,
        )
        if not np.all(np.isfinite(depth_x)):
            depth_x = np.asarray(
                [np.asarray(row["plan_target_mm"])[1] for row in depth_members],
                dtype=np.float64,
            )
            depth_label = "Planned stage Y [mm]"
        else:
            depth_label = "Independent mechanical distance label [mm]"
        figure, depth_axes = plt.subplots(
            2,
            1,
            figsize=(9.2, 8.0),
            sharex=True,
            constrained_layout=True,
        )
        depth_error = np.asarray(
            [row["distance_norm_error_mm"] for row in depth_members],
            dtype=np.float64,
        )
        scatter_by_direction(depth_axes[0], depth_x, depth_error, depth_members)
        depth_axes[0].axhline(0.0, color="black", linewidth=0.9)
        depth_axes[0].set_ylabel("Distance-norm error [mm]")
        depth_axes[0].set_title(f"Depth validation (n={len(depth_members)})")
        depth_axes[0].grid(True, alpha=0.28)
        depth_axes[0].legend(loc="best")
        residual_values = np.vstack(
            [row["delta_residual_stage_frame_mm"] for row in depth_members]
        )
        spacing_values = np.unique(np.round(depth_x[np.isfinite(depth_x)], 9))
        component_offset = (
            float(np.min(np.diff(spacing_values))) * 0.012
            if spacing_values.size > 1
            else 0.08
        )
        for component_index, component in enumerate(AXIS_NAMES):
            depth_axes[1].scatter(
                jitter(depth_x) + (component_index - 1) * component_offset,
                residual_values[:, component_index],
                s=30,
                color=AXIS_FACE_COLORS[component],
                alpha=0.68,
                label=f"Residual {component.upper()}",
            )
        depth_axes[1].axhline(0.0, color="black", linewidth=0.9)
        depth_axes[1].set_xlabel(depth_label)
        depth_axes[1].set_ylabel("Stage-frame residual [mm]")
        depth_axes[1].grid(True, alpha=0.28)
        depth_axes[1].legend(loc="best", ncols=3)
        depth_path = output_dir / "validation_depth.png"
        figure.savefig(depth_path, dpi=180)
        plt.close(figure)
        detail_plots.append(depth_path)

        # Re-reference the depth sweep locally between consecutive Y samples.
        # This reveals whether the large D0-referenced error accumulates smoothly
        # or is concentrated in particular depth intervals.
        depth_passes: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in depth_members:
            depth_passes[
                (
                    str(row.get("reference_id", "")),
                    str(row.get("cycle", "?")),
                    str(row.get("direction", "other")).lower(),
                )
            ].append(row)
        incremental_rows: list[dict[str, Any]] = []
        for (reference_id, cycle, direction), pass_members in depth_passes.items():
            first = pass_members[0]
            first_label = float(first.get("absolute_distance_label_mm", float("nan")))
            first_depth = float(first.get("stage_depth_from_reference_mm", float("nan")))
            reference_label = (
                first_label - first_depth
                if math.isfinite(first_label) and math.isfinite(first_depth)
                else float(np.asarray(first["reference_plan_target_mm"])[1])
            )
            reference_observed = (
                np.asarray(first["measured_stage_frame_mm"], dtype=np.float64)
                - np.asarray(
                    first["observed_delta_stage_frame_mm"], dtype=np.float64
                )
            )
            points = [
                {
                    "label_mm": reference_label,
                    "plan_y_mm": float(
                        np.asarray(first["reference_plan_target_mm"])[1]
                    ),
                    "camera_mm": np.asarray(
                        first["reference_center_camera_mm"], dtype=np.float64
                    ),
                    "stage_mm": np.asarray(
                        first["reference_stage_readback_mm"], dtype=np.float64
                    ),
                    "observed_mm": reference_observed,
                }
            ]
            for row in pass_members:
                label = float(row.get("absolute_distance_label_mm", float("nan")))
                plan_y = float(np.asarray(row["plan_target_mm"])[1])
                points.append(
                    {
                        "label_mm": label if math.isfinite(label) else plan_y,
                        "plan_y_mm": plan_y,
                        "camera_mm": np.asarray(row["center_camera_mm"], dtype=np.float64),
                        "stage_mm": np.asarray(row["stage_readback_mm"], dtype=np.float64),
                        "observed_mm": np.asarray(
                            row["measured_stage_frame_mm"], dtype=np.float64
                        ),
                    }
                )
            points.sort(key=lambda point: float(point["label_mm"]))
            for start, end in zip(points[:-1], points[1:]):
                delta_camera = np.asarray(end["camera_mm"]) - np.asarray(
                    start["camera_mm"]
                )
                delta_stage = np.asarray(end["stage_mm"]) - np.asarray(
                    start["stage_mm"]
                )
                delta_observed = np.asarray(end["observed_mm"]) - np.asarray(
                    start["observed_mm"]
                )
                stage_norm = float(np.linalg.norm(delta_stage))
                if stage_norm <= 1e-9:
                    continue
                camera_norm = float(np.linalg.norm(delta_camera))
                residual_step = delta_observed - delta_stage
                movement_unit = delta_stage / stage_norm
                axial_step = float(np.dot(residual_step, movement_unit))
                lateral_step = float(
                    np.linalg.norm(residual_step - axial_step * movement_unit)
                )
                incremental_rows.append(
                    {
                        "reference_id": reference_id,
                        "cycle": cycle,
                        "direction": direction,
                        "start_plan_y_mm": float(start["plan_y_mm"]),
                        "end_plan_y_mm": float(end["plan_y_mm"]),
                        "midpoint_distance_label_mm": 0.5
                        * (float(start["label_mm"]) + float(end["label_mm"])),
                        "stage_step_mm": stage_norm,
                        "camera_step_mm": camera_norm,
                        "distance_error_mm": camera_norm - stage_norm,
                        "axial_error_mm": axial_step,
                        "lateral_error_mm": lateral_step,
                        "error_3d_mm": float(np.linalg.norm(residual_step)),
                    }
                )
        if incremental_rows:
            incremental_csv = output_dir / "validation_depth_incremental.csv"
            write_csv(incremental_csv, incremental_rows)
            figure, incremental_axis = plt.subplots(
                figsize=(9.5, 5.8),
                constrained_layout=True,
            )
            for cycle in cycle_values:
                for direction_key, (_, marker, _) in direction_styles.items():
                    series = sorted(
                        [
                            row
                            for row in incremental_rows
                            if str(row["cycle"]) == cycle
                            and str(row["direction"]) == direction_key
                        ],
                        key=lambda row: float(row["midpoint_distance_label_mm"]),
                    )
                    if not series:
                        continue
                    incremental_axis.plot(
                        [row["midpoint_distance_label_mm"] for row in series],
                        [row["distance_error_mm"] for row in series],
                        color=cycle_colors[cycle],
                        marker=marker,
                        linestyle=line_styles[direction_key],
                        linewidth=1.0,
                        markersize=5.0,
                        alpha=0.75,
                    )
            incremental_axis.axhline(0.0, color="black", linewidth=0.9)
            incremental_axis.set_xlabel(
                "Midpoint independent mechanical distance label [mm]"
            )
            incremental_axis.set_ylabel(
                "Consecutive-step ||delta camera|| - ||delta stage|| [mm]"
            )
            incremental_axis.set_title(
                "Local depth increment error — consecutive samples within each pass"
            )
            incremental_axis.grid(True, alpha=0.28)
            incremental_axis.legend(
                handles=[*cycle_handles, *direction_handles],
                loc="best",
                ncols=min(5, len(cycle_handles) + len(direction_handles)),
            )
            incremental_axis.text(
                0.01,
                0.01,
                "Most intervals are 20 mm; the final 160-165 mm interval exists only "
                "in the positive approach. Lines are visual guides.",
                transform=incremental_axis.transAxes,
                fontsize=7.5,
                color="#444444",
                va="bottom",
            )
            incremental_path = output_dir / "validation_depth_incremental.png"
            figure.savefig(incremental_path, dpi=180)
            plt.close(figure)
            detail_plots.append(incremental_path)

    diagonal_members = [
        row for row in validation if validation_role_key(row) == "diagonal"
    ]
    if diagonal_members:
        corners = sorted(
            {
                (
                    round(float(np.asarray(row["plan_target_mm"])[0]), 6),
                    round(float(np.asarray(row["plan_target_mm"])[2]), 6),
                )
                for row in diagonal_members
            }
        )
        columns = 2
        rows_count = int(math.ceil(len(corners) / columns))
        figure, subplot_grid = plt.subplots(
            rows_count,
            columns,
            figsize=(11.0, 3.8 * rows_count),
            squeeze=False,
        )
        flat_axes = list(subplot_grid.flat)
        for subplot, (corner_x, corner_z) in zip(flat_axes, corners):
            corner_rows = [
                row
                for row in diagonal_members
                if math.isclose(
                    float(np.asarray(row["plan_target_mm"])[0]),
                    corner_x,
                    abs_tol=1e-6,
                )
                and math.isclose(
                    float(np.asarray(row["plan_target_mm"])[2]),
                    corner_z,
                    abs_tol=1e-6,
                )
            ]
            horizontal = np.asarray(
                [row.get("absolute_distance_label_mm", float("nan")) for row in corner_rows],
                dtype=np.float64,
            )
            if not np.all(np.isfinite(horizontal)):
                horizontal = np.asarray(
                    [np.asarray(row["plan_target_mm"])[1] for row in corner_rows],
                    dtype=np.float64,
                )
                horizontal_label = "Planned stage Y [mm]"
            else:
                horizontal_label = "Independent mechanical distance label [mm]"
            vertical = np.asarray(
                [row["distance_norm_error_mm"] for row in corner_rows],
                dtype=np.float64,
            )
            scatter_by_direction(subplot, horizontal, vertical, corner_rows)
            subplot.axhline(0.0, color="black", linewidth=0.9)
            subplot.set_title(
                f"Corner X={corner_x:g}, Z={corner_z:g} mm (n={len(corner_rows)})"
            )
            subplot.set_xlabel(horizontal_label)
            subplot.set_ylabel("Distance-norm error [mm]")
            subplot.grid(True, alpha=0.28)
        for subplot in flat_axes[len(corners) :]:
            subplot.set_visible(False)
        handles, labels = flat_axes[0].get_legend_handles_labels()
        if handles:
            figure.legend(
                handles,
                labels,
                loc="upper center",
                bbox_to_anchor=(0.5, 0.92),
                ncols=3,
            )
        figure.suptitle(
            "Diagonal validation — X/Z corners separated\n"
            "Horizontal jitter is display-only for repeated measurements",
            y=0.995,
        )
        figure.tight_layout(rect=(0.02, 0.02, 0.98, 0.86))
        diagonal_path = output_dir / "validation_diagonal.png"
        figure.savefig(diagonal_path, dpi=180)
        plt.close(figure)
        detail_plots.append(diagonal_path)

    return [str(distance_plot), str(residual_plot), *(str(path) for path in detail_plots)]


def write_report(
    path: Path,
    summary: dict[str, Any],
) -> None:
    primary = summary["primary_distance_error"]
    orientation = summary["orientation"]
    distance = summary["absolute_distance_labels"]
    theory = summary["theory"]
    coverage = summary["coverage"]
    acceptance = summary["acceptance"]
    lines = [
        "# ステレオ3軸ステージ距離精度解析",
        "",
        "## 主評価",
        "",
        (
            "主評価は座標変換を必要としない "
            "`||Δcamera|| - ||Δstage readback||` です。"
        ),
        (
            f"validation `{primary.get('count', 0)}` 点、"
            f"RMS `{primary.get('rms_mm', float('nan')):.6f} mm`、"
            f"絶対値P95 `{primary.get('p95_abs_mm', float('nan')):.6f} mm`、"
            f"最大絶対値 `{primary.get('max_abs_mm', float('nan')):.6f} mm` でした。"
        ),
        (
            "主集計はsingle-axisとdepth captureです。diagonal/multi-axisは"
            "secondaryへ分離しています。"
        ),
        (
            f"coverageは `{'complete' if coverage.get('complete') else 'incomplete/unverified'}`、"
            f"acceptanceは `{acceptance['status']}`、scopeは "
            f"`{acceptance['scope']}` です。"
        ),
        "",
        "## 3軸方向の同定",
        "",
        (
            "camera→stageの回転はorientation専用captureだけからscaleなしKabsch/SVDで"
            "求め、validationはfitへ使用していません。"
        ),
        (
            f"orientation rigid-fit RMSは "
            f"`{orientation['rigid_fit_residual_rms_mm']:.6f} mm`、"
            f"similarity scale診断は "
            f"`{orientation['similarity_scale_diagnostic_only']:.9f}` です。"
        ),
        "Similarity/affine結果は診断のみで、測定値の補正には適用していません。",
        "",
        "## validation集計",
        "",
        (
            f"軸方向誤差RMS: `{summary['axial_error'].get('rms_mm', float('nan')):.6f} mm`、"
            f"横方向誤差RMS: `{summary['lateral_error'].get('rms_mm', float('nan')):.6f} mm`、"
            f"3D誤差RMS: `{summary['error_3d'].get('rms_mm', float('nan')):.6f} mm`。"
        ),
        (
            f"center returnのstage offset最大値: "
            f"`{summary['stage_return_offset'].get('max_abs_mm', float('nan')):.6f} mm`、"
            f"camera−stage return residual RMS: "
            f"`{summary['return_camera_minus_stage_residual'].get('rms_mm', float('nan')):.6f} mm`。"
        ),
        (
            "種類・軸を混ぜない数値表は "
            "`validation_summary_by_type_axis.md` と同名CSVへ出力しています。"
        ),
        "",
        "## 絶対距離ラベル",
        "",
    ]
    if distance.get("available"):
        lines.extend(
            [
                (
                    f"基準位置の独立な機械測定距離 "
                    f"`{distance['reference_distance_mm']:.6f} mm` を基準に、"
                    "設定したstage深度軸へのreadback射影から各captureの距離ラベルを生成しました。"
                ),
                (
                    f"合成標準不確かさは "
                    f"`{distance['combined_standard_uncertainty_mm']:.6f} mm` です。"
                ),
                "これは独立な機械距離ラベルであり、ステレオ推定距離ではありません。",
            ]
        )
        if distance.get("stereo_baseline_comparison_available"):
            comparison = summary["stereo_minus_mechanical_baseline_distance"]
            lines.append(
                "距離定義が一致したため、ステレオbaseline垂線距離−機械距離ラベルも"
                f"算出しました（RMS `{comparison.get('rms_mm', float('nan')):.6f} mm`）。"
            )
        else:
            lines.append(
                "ステレオbaseline距離との差は出していません: "
                + str(distance.get("stereo_baseline_comparison_reason", "definition mismatch"))
            )
    else:
        lines.append(
            "独立な機械測定距離が未設定または無効のため、絶対距離ラベルは生成していません。"
        )
    lines.extend(["", "## 理論感度", ""])
    if theory.get("available"):
        lines.extend(
            [
                (
                    f"光学中心間基線は `{theory['optical_center_baseline_mm']:.6f} mm`、"
                    f"生intrinsicの平均fxは `{theory['mean_raw_fx_px']:.3f} px` です。"
                ),
                (
                    "設定した1カメラ当たりの画素中心標準偏差 "
                    f"`{theory['pixel_sigma_scenarios_per_camera_px']}` pxを、"
                    "実校正の収束幾何へ数値Jacobianで伝播しました。"
                ),
                (
                    "これは1回の三角測量点のランダム誤差モデルです。実測値は多数windowの"
                    "robust medianなので、同一の推定量として直接比較しません。"
                ),
            ]
        )
    else:
        lines.append(
            "理論感度を生成できませんでした: "
            + str(theory.get("reason", "unknown reason"))
        )
    lines.extend(
        [
            "",
            "## 解釈上の注意",
            "",
            "- stage readbackを相対変位の基準として扱います。",
            "- 回転同定は軸成分の分解だけに使用し、主距離誤差には影響しません。",
            "- orientation fitの誤差、ステージ直進性、追跡、三角測量を含むシステム評価です。",
            "- sampled capture間の未測定距離について精度を保証しません。",
            "",
            "## 出力",
            "",
            "- `stereo_3axis_stage_accuracy_summary.json`",
            "- `capture_role_map.csv`（全q番号の目的・座標・基準点・集計先）",
            "- `measurements.csv`",
            "- `validation_accuracy.csv`",
            "- `validation_summary_by_type_axis.csv` / `.md`",
            "- `axis_response.csv`",
            "- `repeatability.csv`",
            "- `forward_reverse.csv`",
            "- `return_drift.csv`",
            "- `theory_per_capture.csv`",
            "- `stereo_3axis_distance_error.png`",
            "- `stereo_3axis_residual_components.png`",
            "- `validation_error_decomposition.png`（axial/lateral/3D）",
            "- `validation_axis_[x|y|z].png`（存在する軸のみ）",
            "- `validation_axis_[x|y|z]_by_delta.png`（固定変位ごとの深度依存）",
            "- `validation_axis_[x|y|z]_heatmap.png` / `validation_axis_heatmap.csv`",
            "- `validation_depth.png`",
            "- `validation_depth_incremental.png` / `.csv`",
            "- `validation_diagonal.png`",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def prepare_analysis_inputs(
    *,
    manifest_path: Path,
    args: argparse.Namespace,
    include_capture_ids: set[str] | None = None,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, Any],
    Path | None,
    Path | None,
]:
    """Load one atomic manifest snapshot and validate its frozen inputs."""
    manifest = load_json(manifest_path)
    settings = deep_merge(DEFAULTS, {})
    session_config_value = manifest.get("session_config")
    session_config_path = resolve_path(session_config_value, manifest_path.parent)
    if session_config_path is not None:
        if not session_config_path.exists():
            raise SystemExit(
                f"Manifest session_config does not exist: {session_config_path}"
            )
        settings = deep_merge(settings, load_json(session_config_path))
    settings = deep_merge(settings, manifest.get("config", {}))
    settings = deep_merge(settings, {
        "analysis": manifest.get("analysis", {}),
        "tracking": manifest.get("tracking", {}),
    })
    if args.config is not None:
        settings = deep_merge(settings, load_json(args.config.resolve()))
    rows, manifest_info = normalize_manifest(
        manifest,
        manifest_path=manifest_path,
        include_capture_ids=include_capture_ids,
    )
    calibration = resolve_calibration(args, manifest, manifest_path)
    fingerprint = manifest.get("configuration_fingerprint")
    if isinstance(fingerprint, dict):
        expected_config_hash = str(fingerprint.get("config_sha256", ""))
        frozen_config = manifest.get("config")
        if (
            expected_config_hash
            and isinstance(frozen_config, dict)
            and canonical_json_sha256(frozen_config) != expected_config_hash
        ):
            raise SystemExit(
                "Manifest embedded config no longer matches its immutable fingerprint."
            )
        if (
            expected_config_hash
            and session_config_path is not None
            and canonical_json_sha256(load_json(session_config_path))
            != expected_config_hash
        ):
            raise SystemExit(
                "Session config no longer matches its immutable fingerprint."
            )
        expected_calibration_hash = str(
            fingerprint.get("stereo_calibration_sha256", "")
        )
        if (
            expected_calibration_hash
            and calibration is not None
            and file_sha256(calibration) != expected_calibration_hash
        ):
            raise SystemExit(
                "Stereo calibration no longer matches the capture fingerprint."
            )
        expected_stage_hash = str(fingerprint.get("stage_config_sha256", ""))
        stage_copy = resolve_path(manifest.get("stage_config_copy"), manifest_path.parent)
        if (
            expected_stage_hash
            and (
                stage_copy is None
                or not stage_copy.is_file()
                or file_sha256(stage_copy) != expected_stage_hash
            )
        ):
            raise SystemExit(
                "Frozen Ossila stage config no longer matches the capture fingerprint."
            )
    return (
        manifest,
        settings,
        rows,
        manifest_info,
        calibration,
        session_config_path,
    )


def manifest_capture_progress(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return acquisition progress from one manifest snapshot."""
    raw_samples = manifest.get("samples")
    if not isinstance(raw_samples, list) or not raw_samples:
        raise SystemExit("Watch mode requires a non-empty formal samples list.")
    captured = [
        sample
        for sample in raw_samples
        if isinstance(sample, dict)
        and str(sample.get("capture_status", "")).strip().lower() == "captured"
    ]
    recording = sum(
        isinstance(sample, dict)
        and str(sample.get("capture_status", "")).strip().lower() == "recording"
        for sample in raw_samples
    )
    latest_id = ""
    if captured:
        latest = max(
            captured,
            key=lambda sample: int(sample.get("sequence_index", 0)),
        )
        latest_id = str(latest.get("sample_id", ""))
    return {
        "session_status": str(manifest.get("status", "")).strip().lower(),
        "total": len(raw_samples),
        "captured": len(captured),
        "recording": int(recording),
        "latest_capture_id": latest_id,
    }


def write_watch_status(
    output_dir: Path,
    *,
    phase: str,
    progress: dict[str, Any],
    prepared_captures: int,
) -> None:
    atomic_write_json(
        output_dir / "incremental_processing_status.json",
        {
            "schema_version": 1,
            "phase": phase,
            **progress,
            "incrementally_prepared_captures": prepared_captures,
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        },
    )


def watch_analysis(
    *,
    session_dir: Path,
    manifest_path: Path,
    output_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Incrementally process completed captures, then run one final analysis."""
    prepared_ids: set[str] = set()
    last_progress_key: tuple[Any, ...] | None = None
    failure_statuses = {
        "capture_failed",
        "captured_partial_cleanup_pending",
        "capture_complete_cleanup_failed",
        "cleanup_failed",
        "error",
        "interrupted",
        "interrupted_cleanup_verified",
    }
    print(
        f"[WATCH] Monitoring {manifest_path} every "
        f"{args.watch_interval_sec:g} seconds. Ctrl+C stops analysis only; "
        "acquisition continues in its own process.",
        flush=True,
    )
    while True:
        manifest = load_json(manifest_path)
        progress = manifest_capture_progress(manifest)
        new_ids = {
            str(sample.get("sample_id", ""))
            for sample in manifest["samples"]
            if isinstance(sample, dict)
            and str(sample.get("capture_status", "")).strip().lower() == "captured"
            and str(sample.get("sample_id", "")) not in prepared_ids
        }
        if new_ids:
            (
                _manifest,
                settings,
                new_rows,
                _manifest_info,
                calibration,
                _session_config_path,
            ) = prepare_analysis_inputs(
                manifest_path=manifest_path,
                args=args,
                include_capture_ids=new_ids,
            )
            new_rows.sort(key=lambda row: int(row["sequence_index"]))
            first_id = str(new_rows[0]["capture_id"])
            last_id = str(new_rows[-1]["capture_id"])
            print(
                f"[WATCH][PROCESS] {len(new_rows)} new capture(s): "
                f"{first_id}..{last_id}",
                flush=True,
            )
            for row in new_rows:
                capture_id = str(row["capture_id"])
                ensure_processed(
                    [row],
                    args=args,
                    calibration=calibration,
                    settings=settings,
                )
                prepared_ids.add(capture_id)
                write_watch_status(
                    output_dir,
                    phase="incremental_processing",
                    progress=progress,
                    prepared_captures=len(prepared_ids),
                )
                print(
                    f"[WATCH][PROCESS][OK] {capture_id}; "
                    f"prepared={len(prepared_ids)}/{progress['total']}",
                    flush=True,
                )
        progress_key = (
            progress["session_status"],
            progress["captured"],
            progress["recording"],
        )
        if progress_key != last_progress_key:
            percent = 100.0 * float(progress["captured"]) / float(progress["total"])
            print(
                f"[WATCH][CAPTURE] {progress['captured']}/{progress['total']} "
                f"({percent:.1f}%) status={progress['session_status']} "
                f"latest={progress['latest_capture_id'] or '-'}",
                flush=True,
            )
            last_progress_key = progress_key
        write_watch_status(
            output_dir,
            phase="incremental_processing",
            progress=progress,
            prepared_captures=len(prepared_ids),
        )
        if (
            progress["session_status"] == "captured"
            and progress["captured"] == progress["total"]
        ):
            print(
                "[WATCH][FINAL] Acquisition and cleanup completed; running "
                "the comprehensive analysis.",
                flush=True,
            )
            write_watch_status(
                output_dir,
                phase="final_analysis",
                progress=progress,
                prepared_captures=len(prepared_ids),
            )
            final_args = argparse.Namespace(**vars(args))
            # --force-processing applies once as each capture enters the watcher.
            # The final pass validates and reuses those products instead of doing
            # all expensive processing a second time.
            final_args.force_processing = False
            summary = run_analysis(
                session_dir=session_dir,
                manifest_path=manifest_path,
                output_dir=output_dir,
                args=final_args,
            )
            write_watch_status(
                output_dir,
                phase="complete",
                progress=progress,
                prepared_captures=len(prepared_ids),
            )
            return summary
        if progress["session_status"] in failure_statuses:
            raise SystemExit(
                "Acquisition ended without a clean complete session: "
                f"status={progress['session_status']}, "
                f"captured={progress['captured']}/{progress['total']}. "
                "Incremental products were preserved; inspect capture safety/status "
                "before running a final analysis."
            )
        time.sleep(float(args.watch_interval_sec))


def run_analysis(
    *,
    session_dir: Path,
    manifest_path: Path,
    output_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    (
        manifest,
        settings,
        rows,
        manifest_info,
        calibration,
        session_config_path,
    ) = prepare_analysis_inputs(manifest_path=manifest_path, args=args)
    ensure_processed(
        rows,
        args=args,
        calibration=calibration,
        settings=settings,
    )
    load_centres(rows, settings)
    orientation = fit_orientation(rows)
    validation_all = apply_validation_metrics(rows, orientation, settings)
    validation = [
        row for row in validation_all if bool(row.get("primary_evaluation", True))
    ]
    primary_validation = [
        row
        for row in validation
        if row.get("dominant_axis") in AXIS_NAMES
        and str(row.get("trajectory", "")).lower() != "diagonal"
    ]
    primary_ids = {
        str(row["capture_id"]) for row in primary_validation
    }
    secondary_validation = [
        row
        for row in validation
        if str(row["capture_id"]) not in primary_ids
    ]
    for row in validation:
        row["summary_group"] = (
            "primary"
            if str(row["capture_id"]) in primary_ids
            else "secondary"
        )
    return_checks = [
        row for row in validation_all if bool(row.get("return_check", False))
    ]
    if not primary_validation:
        raise SystemExit("No usable validation captures remain after reference checks.")
    axis_rows, response_matrix = axis_response_diagnostics(validation)
    repeatability = repeatability_diagnostics(validation, settings)
    forward_reverse = direction_diagnostics(validation, settings)
    distance_config = distance_reference_config(manifest, settings)
    distance_config = apply_absolute_distance_labels(rows, distance_config)
    distance_config = apply_stereo_baseline_comparison(
        rows,
        distance_config,
        calibration,
    )
    theory, theory_rows = theory_diagnostics(
        validation,
        orientation,
        settings,
        calibration,
    )
    coverage = coverage_diagnostics(manifest, rows)
    acceptance = acceptance_result(
        primary_validation,
        settings,
        coverage,
        return_checks,
    )
    return_drifts = [
        float(row["return_drift_3d_mm"])
        for row in return_checks
        if math.isfinite(float(row["return_drift_3d_mm"]))
    ]
    stage_return_offsets = [
        float(row["stage_return_offset_3d_mm"])
        for row in return_checks
    ]
    return_residuals = [
        float(row["return_residual_3d_mm"])
        for row in return_checks
    ]
    capture_role_rows = build_capture_role_map(
        rows,
        orientation=orientation,
        primary_validation=primary_validation,
        secondary_validation=secondary_validation,
        return_checks=return_checks,
    )
    summary: dict[str, Any] = {
        "schema_version": 1,
        "analysis_definition": {
            "primary_metric": "norm(delta_camera_mm) - norm(delta_stage_readback_mm)",
            "primary_metric_requires_coordinate_transform": False,
            "axis_decomposition": (
                "scale-free camera-to-stage rotation fitted from orientation role only"
            ),
            "scale_correction_applied": False,
            "affine_correction_applied": False,
        },
        "session_dir": str(session_dir),
        "manifest": str(manifest_path),
        "session_config": (
            str(session_config_path) if session_config_path is not None else ""
        ),
        "stereo_calibration": str(calibration) if calibration is not None else "",
        "target": settings.get("target", manifest.get("target", {})),
        "camera_provenance": settings.get("camera", {}),
        "default_reference_id": manifest_info["default_reference_id"],
        "input_captures": len(rows),
        "usable_captures": sum(bool(row["usable"]) for row in rows),
        "usable_validation_captures": len(validation),
        "usable_primary_single_axis_captures": len(primary_validation),
        "usable_secondary_captures": len(secondary_validation),
        "skipped_uncaptured": int(manifest_info["skipped_uncaptured"]),
        "return_drift_checks": len(return_checks),
        "capture_role_map_count": len(capture_role_rows),
        "orientation": orientation,
        "primary_distance_error": signed_summary(
            row["distance_norm_error_mm"] for row in primary_validation
        ),
        "axial_error": signed_summary(
            row["axial_error_mm"]
            for row in primary_validation
            if math.isfinite(float(row["axial_error_mm"]))
        ),
        "lateral_error": signed_summary(
            row["lateral_error_mm"] for row in primary_validation
        ),
        "error_3d": signed_summary(
            row["error_3d_mm"] for row in primary_validation
        ),
        "secondary_distance_error": signed_summary(
            row["distance_norm_error_mm"] for row in secondary_validation
        ),
        "secondary_error_3d": signed_summary(
            row["error_3d_mm"] for row in secondary_validation
        ),
        "return_drift": signed_summary(return_drifts),
        "stage_return_offset": signed_summary(stage_return_offsets),
        "return_camera_minus_stage_residual": signed_summary(
            return_residuals
        ),
        "validation_summary_by_type_axis": validation_group_summaries(validation),
        "axis_response_rows": axis_rows,
        "axis_response_matrix_output_by_input": response_matrix,
        "cross_talk_definition": (
            "off-diagonal slopes of observed stage-frame displacement versus "
            "single-axis stage readback displacement"
        ),
        "repeatability": repeatability,
        "forward_reverse": forward_reverse,
        "absolute_distance_labels": distance_config,
        "theory": theory,
        "stereo_minus_mechanical_baseline_distance": signed_summary(
            row.get("stereo_minus_mechanical_baseline_distance_mm", float("nan"))
            for row in validation
        ),
        "acceptance": acceptance,
        "coverage": coverage,
        "captures": flatten_measurements(rows),
        "validation": flatten_validation(validation),
        "return_drift_captures": flatten_validation(return_checks),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    transform_path = output_dir / "camera_to_stage_transform.npz"
    write_camera_to_stage_transform(transform_path, orientation)
    summary["camera_to_stage_transform_npz"] = str(transform_path)
    write_csv(output_dir / "measurements.csv", summary["captures"])
    role_map_path = output_dir / "capture_role_map.csv"
    write_csv(role_map_path, capture_role_rows)
    summary["capture_role_map_csv"] = str(role_map_path)
    write_csv(output_dir / "validation_accuracy.csv", summary["validation"])
    validation_summary_csv = output_dir / "validation_summary_by_type_axis.csv"
    validation_summary_md = output_dir / "validation_summary_by_type_axis.md"
    write_csv(
        validation_summary_csv,
        summary["validation_summary_by_type_axis"],
    )
    write_validation_summary_markdown(
        validation_summary_md,
        summary["validation_summary_by_type_axis"],
    )
    summary["validation_summary_by_type_axis_csv"] = str(validation_summary_csv)
    summary["validation_summary_by_type_axis_markdown"] = str(validation_summary_md)
    write_csv(
        output_dir / "return_drift.csv",
        summary["return_drift_captures"],
    )
    write_csv(output_dir / "axis_response.csv", axis_rows)
    write_csv(output_dir / "repeatability.csv", repeatability)
    write_csv(output_dir / "forward_reverse.csv", forward_reverse)
    write_csv(output_dir / "theory_per_capture.csv", theory_rows)
    summary["plots"] = make_plots(output_dir, validation, distance_config)
    atomic_write_json(
        output_dir / "stereo_3axis_stage_accuracy_summary.json",
        summary,
    )
    write_report(
        output_dir / "stereo_3axis_stage_accuracy_report.md",
        summary,
    )
    return summary


def main() -> int:
    args = parse_args()
    if args.skip_processing and args.force_processing:
        raise SystemExit("--skip-processing and --force-processing are mutually exclusive.")
    if not math.isfinite(float(args.watch_interval_sec)) or args.watch_interval_sec <= 0:
        raise SystemExit("--watch-interval-sec must be a finite positive number.")
    session_dir = args.session_dir.resolve()
    manifest_path = (
        args.manifest.resolve()
        if args.manifest is not None
        else session_dir / "stereo_3axis_stage_accuracy_manifest.json"
    )
    if (
        args.manifest is None
        and not manifest_path.exists()
        and (session_dir / "manifest.json").exists()
    ):
        manifest_path = session_dir / "manifest.json"
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else session_dir / "analysis_3axis"
    )
    if args.watch:
        summary = watch_analysis(
            session_dir=session_dir,
            manifest_path=manifest_path,
            output_dir=output_dir,
            args=args,
        )
    else:
        summary = run_analysis(
            session_dir=session_dir,
            manifest_path=manifest_path,
            output_dir=output_dir,
            args=args,
        )
    primary = summary["primary_distance_error"]
    print(f"Analysis: {output_dir}")
    print(
        "[RESULT][TRANSFORM-FREE] "
        f"count={primary.get('count', 0)} "
        f"RMS={primary.get('rms_mm', float('nan')):.6f} mm "
        f"P95(abs)={primary.get('p95_abs_mm', float('nan')):.6f} mm "
        f"max(abs)={primary.get('max_abs_mm', float('nan')):.6f} mm"
    )
    print(
        "[ORIENTATION] validation points used for fit: "
        f"{summary['orientation']['validation_captures_used_for_fit']}; "
        "similarity/affine diagnostics were not applied"
    )
    print(
        "[SCOPE] "
        f"coverage_complete={summary['coverage'].get('complete', False)} "
        f"acceptance={summary['acceptance']['status']} "
        f"scope={summary['acceptance']['scope']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
