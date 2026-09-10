#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Plan and capture a resumable three-axis Ossila/stereo validation experiment.

Without ``--execute`` this program only validates files, builds an immutable
plan, and writes a dry-plan session.  Full execution requires that reviewed
session, explicit identity/settings locks for all three axes, and absolute
position mode.  X/Y/Z moves are issued one axis at a time.
"""

from __future__ import annotations

import argparse
import copy
import csv
import datetime as dt
import json
import math
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from stereo_3axis_stage_accuracy_common import (
    AXES,
    SCRIPT_DIR,
    axis_driver_inputs,
    configuration_fingerprint,
    file_sha256,
    generate_plan,
    json_ready,
    load_json,
    plan_hash,
    validate_config,
)
from stereo_stage_accuracy_capture import (
    SafeOssilaAxis,
    read_capture_counts,
    run_camera_stream_preflight,
)


DEFAULT_CONFIG = SCRIPT_DIR / "stereo_3axis_stage_accuracy_config.json"
MANIFEST_NAME = "stereo_3axis_stage_accuracy_manifest.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Three-axis Ossila stage accuracy capture with synchronized stereo recording.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("stereo_3axis_stage_accuracy_records"),
    )
    parser.add_argument("--session-dir", type=Path, default=None)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Open hardware and execute; otherwise create/review a dry plan only.",
    )
    parser.add_argument(
        "--pilot",
        action="store_true",
        help=(
            "Use a compact near/mid/far commissioning subset. "
            "Pass the same flag again when executing its reviewed session."
        ),
    )
    parser.add_argument(
        "--list-stage-ports",
        action="store_true",
        help="Enumerate serial ports without opening them or creating a session.",
    )
    parser.add_argument(
        "--probe-stage-identities",
        action="store_true",
        help="With --execute, query identity/settings on X/Y/Z without home/goto/camera commands.",
    )
    parser.add_argument(
        "--home-axes",
        nargs="+",
        choices=AXES,
        default=[],
        help="Home only the listed axes before moving to the reference coordinate.",
    )
    parser.add_argument(
        "--resume-from-cycle",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Explicitly resume a partial session by reacquiring cycle N and every "
            "later cycle. Requires --acknowledge-setup-unchanged and cannot be "
            "combined with --home-axes. Existing attempts are preserved."
        ),
    )
    parser.add_argument(
        "--recapture-block",
        nargs=2,
        default=None,
        metavar=("START_ID", "END_ID"),
        help=(
            "Reacquire one completed validation block, inclusively, from its "
            "validation_center_pre START_ID through validation_center_post "
            "END_ID. Requires --acknowledge-setup-unchanged; prior attempts "
            "are preserved."
        ),
    )
    parser.add_argument(
        "--acknowledge-setup-unchanged",
        action="store_true",
        help=(
            "Acknowledge that camera rig, focus, target, stage mechanics, and powered "
            "absolute stage coordinates have not changed since the recorded stop."
        ),
    )
    parser.add_argument("--yes", action="store_true", help="Skip the final RUN confirmation.")
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue after a sample exhausts its camera retries.",
    )
    return parser.parse_args()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(json_ready(payload), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    # On Windows, a reader (the incremental analyzer, an indexer, or antivirus)
    # can briefly prevent replacement of an otherwise writable manifest.  The
    # payload is already complete in the temporary file, so retry only the
    # atomic rename instead of aborting a multi-hour hardware run.
    started = time.monotonic()
    waiting_reported = False
    for retry_index in range(1200):
        try:
            os.replace(temporary, path)
            if waiting_reported:
                print(
                    f"[MANIFEST][RECOVERED] Atomic replace succeeded after "
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
                    f"[MANIFEST][WAIT] Windows is temporarily locking {path}; "
                    f"retrying atomic replace ({time.monotonic() - started:.1f} s elapsed).",
                    file=sys.stderr,
                    flush=True,
                )
            time.sleep(0.1)


def validate_resume_fingerprint(
    stored: dict[str, Any],
    expected: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Allow only the capture orchestrator itself to differ for audited recovery."""

    allowed = {"three_axis_capture_source_sha256"}
    differences = {
        key: {"stored": stored.get(key), "current": expected.get(key)}
        for key in sorted(set(stored) | set(expected))
        if stored.get(key) != expected.get(key)
    }
    disallowed = sorted(set(differences) - allowed)
    if disallowed:
        raise SystemExit(
            "Partial resume rejected because locked inputs changed: "
            + ", ".join(disallowed)
            + ". Preserve this session and create a new dry-plan session."
        )
    return differences


def prepare_partial_cycle_resume(
    manifest: dict[str, Any],
    *,
    resume_cycle: int,
    current_fingerprint: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Restart an incomplete cycle while retaining every earlier raw attempt."""

    if resume_cycle <= 0:
        raise SystemExit("--resume-from-cycle must be a positive integer.")
    if manifest.get("cleanup_verified") is not True:
        raise SystemExit(
            "Partial resume requires a previously verified all-axis stop/cleanup."
        )
    recorded_stop = manifest.get("stage_readback_after_stop")
    if not isinstance(recorded_stop, dict) or not isinstance(
        recorded_stop.get("axes"), dict
    ):
        raise SystemExit(
            "Partial resume requires stage_readback_after_stop from the prior run."
        )

    samples = list(manifest.get("samples", []))
    incomplete = [
        sample for sample in samples if sample.get("capture_status") != "captured"
    ]
    if not incomplete:
        raise SystemExit("The session has no incomplete samples to resume.")
    first_incomplete = min(incomplete, key=lambda sample: int(sample["sequence_index"]))
    first_incomplete_cycle = int(first_incomplete.get("cycle", 0))
    if first_incomplete_cycle != resume_cycle:
        raise SystemExit(
            f"The first incomplete sample {first_incomplete['sample_id']} belongs to "
            f"cycle {first_incomplete_cycle}; use --resume-from-cycle "
            f"{first_incomplete_cycle}."
        )
    earlier_incomplete = [
        sample
        for sample in samples
        if int(sample.get("cycle", 0)) < resume_cycle
        and sample.get("capture_status") != "captured"
    ]
    if earlier_incomplete:
        raise SystemExit(
            "Partial resume rejected because an earlier cycle is incomplete: "
            + ", ".join(str(sample["sample_id"]) for sample in earlier_incomplete[:5])
        )

    restart_samples = [
        sample for sample in samples if int(sample.get("cycle", 0)) >= resume_cycle
    ]
    if not restart_samples:
        raise SystemExit(f"No samples exist at or after cycle {resume_cycle}.")
    restart_samples.sort(key=lambda sample: int(sample["sequence_index"]))
    first_restart = restart_samples[0]
    if str(first_restart.get("data_role", "")) != "validation_center_pre":
        raise SystemExit(
            f"Cycle {resume_cycle} does not start with a validation center reference; "
            "a safe cycle-boundary resume is not available."
        )

    previous_status = str(manifest.get("status", ""))
    superseded = [
        str(sample["sample_id"])
        for sample in restart_samples
        if sample.get("capture_status") == "captured"
    ]
    epoch = {
        "epoch_index": len(manifest.get("execution_epochs", [])) + 1,
        "kind": "partial_cycle_resume",
        "status": "prepared",
        "prepared_at": dt.datetime.now().isoformat(timespec="milliseconds"),
        "resume_from_cycle": resume_cycle,
        "resume_from_sample_id": str(first_restart["sample_id"]),
        "previous_first_incomplete_sample_id": str(first_incomplete["sample_id"]),
        "superseded_capture_ids": superseded,
        "setup_unchanged_acknowledged": True,
        "previous_session_status": previous_status,
        "previous_run_error": copy.deepcopy(manifest.get("run_error")),
        "required_start_readback": copy.deepcopy(recorded_stop),
        "source_fingerprint": copy.deepcopy(current_fingerprint),
    }
    manifest.setdefault("execution_epochs", []).append(epoch)

    dynamic_keys = {
        "accepted_run_dir",
        "accepted_artifact_sha256",
        "capture_readback_before_all_axes",
        "capture_readback_after_all_axes",
        "move_trace",
        "arrival_readback_all_axes",
        "approach_verification",
        "recapture_extra_settle_sec",
    }
    for sample in restart_samples:
        sample["status"] = "pending"
        sample["capture_status"] = "pending"
        for key in dynamic_keys:
            sample.pop(key, None)
        # attempts and their raw artifacts are deliberately retained.  A new
        # accepted acquisition will use the next attempt_XX directory.
        sample.setdefault("attempts", [])

    manifest["capture_complete"] = False
    manifest.pop("run_error", None)
    manifest["active_execution_epoch"] = int(epoch["epoch_index"])
    return restart_samples, epoch


def prepare_selective_block_recapture(
    manifest: dict[str, Any],
    *,
    start_capture_id: str,
    end_capture_id: str,
    current_fingerprint: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Reopen one complete reference-bounded validation block for capture."""

    if manifest.get("cleanup_verified") is not True:
        raise SystemExit(
            "Selective recapture requires a previously verified all-axis "
            "stop/cleanup."
        )
    recorded_stop = manifest.get("stage_readback_after_stop")
    if not isinstance(recorded_stop, dict) or not isinstance(
        recorded_stop.get("axes"), dict
    ):
        raise SystemExit(
            "Selective recapture requires stage_readback_after_stop from the "
            "prior run."
        )

    samples = sorted(
        list(manifest.get("samples", [])),
        key=lambda sample: int(sample["sequence_index"]),
    )
    by_id = {str(sample["sample_id"]): sample for sample in samples}
    missing = [
        capture_id
        for capture_id in (start_capture_id, end_capture_id)
        if capture_id not in by_id
    ]
    if missing:
        raise SystemExit(
            "Selective recapture capture ID not found: " + ", ".join(missing)
        )
    start = by_id[start_capture_id]
    end = by_id[end_capture_id]
    start_index = int(start["sequence_index"])
    end_index = int(end["sequence_index"])
    if start_index > end_index:
        raise SystemExit(
            "--recapture-block START_ID must precede END_ID in the immutable "
            "plan."
        )
    selected = [
        sample
        for sample in samples
        if start_index <= int(sample["sequence_index"]) <= end_index
    ]
    if str(start.get("data_role", "")) != "validation_center_pre":
        raise SystemExit(
            f"Selective recapture must start at validation_center_pre; "
            f"{start_capture_id} is {start.get('data_role', '')!r}."
        )
    if str(end.get("data_role", "")) != "validation_center_post":
        raise SystemExit(
            f"Selective recapture must end at validation_center_post; "
            f"{end_capture_id} is {end.get('data_role', '')!r}."
        )
    block_identity = (
        int(start.get("cycle", 0)),
        str(start.get("trajectory", "")),
        str(start.get("direction", "")),
        str(start.get("sweep_name", "")),
        str(start.get("approach_direction", "")),
    )
    if any(
        (
            int(sample.get("cycle", 0)),
            str(sample.get("trajectory", "")),
            str(sample.get("direction", "")),
            str(sample.get("sweep_name", "")),
            str(sample.get("approach_direction", "")),
        )
        != block_identity
        for sample in selected
    ):
        raise SystemExit(
            "Selective recapture range crosses a cycle, trajectory, or "
            "direction boundary. Choose one complete validation block."
        )
    explicit_reference_ids = {
        str(sample.get("reference_id", "")).strip()
        for sample in selected
        if str(sample.get("reference_id", "")).strip()
    }
    if explicit_reference_ids and explicit_reference_ids != {start_capture_id}:
        raise SystemExit(
            "Selective recapture range is not wholly referenced to its START_ID."
        )
    incomplete = [
        str(sample["sample_id"])
        for sample in samples
        if sample.get("capture_status") != "captured"
    ]
    if incomplete:
        raise SystemExit(
            "Selective recapture requires a completed session; incomplete "
            "capture(s): " + ", ".join(incomplete[:5])
        )

    previous_status = str(manifest.get("status", ""))
    superseded = [str(sample["sample_id"]) for sample in selected]
    epoch = {
        "epoch_index": len(manifest.get("execution_epochs", [])) + 1,
        "kind": "selective_block_recapture",
        "status": "prepared",
        "prepared_at": dt.datetime.now().isoformat(timespec="milliseconds"),
        "recapture_start_id": start_capture_id,
        "recapture_end_id": end_capture_id,
        "recapture_capture_ids": superseded,
        "superseded_capture_ids": superseded,
        "setup_unchanged_acknowledged": True,
        "previous_session_status": previous_status,
        "previous_run_error": copy.deepcopy(manifest.get("run_error")),
        "required_start_readback": copy.deepcopy(recorded_stop),
        "source_fingerprint": copy.deepcopy(current_fingerprint),
    }
    manifest.setdefault("execution_epochs", []).append(epoch)

    dynamic_keys = {
        "accepted_run_dir",
        "accepted_artifact_sha256",
        "capture_readback_before_all_axes",
        "capture_readback_after_all_axes",
        "move_trace",
        "arrival_readback_all_axes",
        "approach_verification",
        "recapture_extra_settle_sec",
    }
    for sample in selected:
        sample["status"] = "pending"
        sample["capture_status"] = "pending"
        for key in dynamic_keys:
            sample.pop(key, None)
        sample.setdefault("attempts", [])

    manifest["capture_complete"] = False
    manifest.pop("run_error", None)
    manifest["active_execution_epoch"] = int(epoch["epoch_index"])
    return selected, epoch


def build_resume_position_verification(
    *,
    preflight: dict[str, Any],
    required_start_readback: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Compare current powered coordinates with the recorded safe-stop position."""

    axes: dict[str, Any] = {}
    errors: list[str] = []
    recorded_axes = required_start_readback.get("axes", {})
    for axis in AXES:
        try:
            expected = float(recorded_axes[axis]["global_mm"])
            actual = float(preflight[axis]["position_global_mm"])
            tolerance = float(config["stages"]["axes"][axis]["readback_tolerance_mm"])
            error = actual - expected
            verified = abs(error) <= tolerance
        except (KeyError, TypeError, ValueError) as exc:
            axes[axis] = {"verified": False, "error": f"invalid readback: {exc}"}
            errors.append(f"{axis}:missing_or_invalid_readback")
            continue
        axes[axis] = {
            "expected_global_mm": expected,
            "actual_global_mm": actual,
            "error_mm": error,
            "tolerance_mm": tolerance,
            "verified": verified,
        }
        if not verified:
            errors.append(
                f"{axis}:position_delta={error:+.6f}>{tolerance:.6f} mm"
            )
    return {
        "verified": not errors,
        "checked_at": dt.datetime.now().isoformat(timespec="milliseconds"),
        "axes": axes,
        "errors": errors,
    }


def update_manifest(
    manifest_path: Path,
    manifest: dict[str, Any],
    *,
    status: str | None = None,
) -> None:
    manifest["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    if status is not None:
        manifest["status"] = status
    atomic_write_json(manifest_path, manifest)


def list_stage_ports() -> int:
    try:
        from serial.tools import list_ports
    except Exception as exc:
        raise SystemExit(f"pyserial is required to list stage ports: {exc}") from exc
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports are currently detected.")
        return 0
    print("Detected serial ports (ports are not opened):")
    for port in ports:
        print(
            f"  port={port.device} usb_serial={port.serial_number or '-'} "
            f"description={port.description or '-'}"
        )
        print(f"    hwid={port.hwid or '-'}")
    return 0


def apply_pilot_profile(config: dict[str, Any]) -> dict[str, Any]:
    """Return an explicit, fingerprinted commissioning subset."""
    pilot = copy.deepcopy(config)
    pilot["name"] = (
        f"{str(config.get('name', 'stereo_3axis_stage_accuracy'))}_pilot"
    )
    pilot["plan_profile"] = "pilot_near_mid_far"
    experiment = pilot["experiment"]
    # The full plan uses multiple local-reference sweeps.  The commissioning
    # profile intentionally retains its compact legacy three-pass structure.
    experiment.pop("validation_sweeps", None)
    experiment["validation_reference_global_mm_by_group"] = {
        "axis": [0.0, 20.0, 0.0],
        "depth": [0.0, 0.0, 0.0],
        "diagonal": [0.0, 0.0, 0.0],
    }
    experiment["validation_axis_points_global_mm"] = [
        [-5.0, -20.0, 0.0],
        [5.0, -20.0, 0.0],
        [0.0, -20.0, -5.0],
        [0.0, -20.0, 5.0],
        [0.0, -15.0, 0.0],
        [0.0, -25.0, 0.0],
    ]
    experiment["validation_depth_points_global_mm"] = [
        [0.0, -20.0, 0.0],
        [0.0, -100.0, 0.0],
        [0.0, -195.0, 0.0],
    ]
    experiment["validation_diagonal_points_global_mm"] = [
        [-5.0, -100.0, -5.0],
        [5.0, -100.0, 5.0],
    ]
    experiment["repeats"] = 1
    experiment["forward_reverse"] = True
    return pilot


def _verify_session_input(
    manifest: dict[str, Any],
    manifest_key: str,
    expected_hash: str,
    label: str,
) -> None:
    path = Path(str(manifest.get(manifest_key, "")))
    if not path.is_file() or file_sha256(path) != expected_hash:
        raise SystemExit(f"Immutable session {label} is missing or changed: {path}")


def write_motion_review_csv(
    path: Path,
    samples: list[dict[str, Any]],
) -> None:
    rows: list[dict[str, Any]] = []
    for sample in samples:
        phases = []
        if sample.get("preposition_stage_mm"):
            phases.append(
                (
                    "controlled_approach_preposition",
                    sample["preposition_stage_mm"],
                    sample["preposition_hardware_mm"],
                )
            )
        phases.append(
            (
                "capture_target",
                sample["command_stage_mm"],
                sample["command_hardware_mm"],
            )
        )
        for phase, logical, hardware in phases:
            rows.append(
                {
                    "sequence_index": sample["sequence_index"],
                    "sample_id": sample["sample_id"],
                    "pass_name": sample["pass_name"],
                    "sweep_name": sample.get("sweep_name", ""),
                    "data_role": sample["data_role"],
                    "phase": phase,
                    "approach_axis": sample.get("approach_axis", ""),
                    "approach_direction": sample["approach_direction"],
                    "logical_x_mm": logical["x"],
                    "logical_y_mm": logical["y"],
                    "logical_z_mm": logical["z"],
                    "hardware_x_mm": hardware["x"],
                    "hardware_y_mm": hardware["y"],
                    "hardware_z_mm": hardware["z"],
                }
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def create_or_resume_session(
    args: argparse.Namespace,
    config: dict[str, Any],
    config_path: Path,
    resolved: dict[str, Any],
    planned: list[dict[str, Any]],
) -> tuple[Path, Path, dict[str, Any]]:
    if args.session_dir is not None:
        session_dir = args.session_dir.resolve()
    else:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = str(config.get("name", "stereo_3axis_stage_accuracy"))
        session_dir = (args.output_root / f"{name}_{stamp}").resolve()
    manifest_path = session_dir / MANIFEST_NAME
    is_probe = bool(args.probe_stage_identities)
    if args.execute and not is_probe and not manifest_path.is_file():
        raise SystemExit(
            "Full --execute requires an existing reviewed dry-plan session. "
            "Run once without --execute, then reuse its exact --session-dir."
        )

    expected_plan_hash = plan_hash(planned)
    expected_fingerprint = configuration_fingerprint(config, resolved)
    if manifest_path.is_file():
        manifest = load_json(manifest_path)
        if str(manifest.get("plan_hash", "")) != expected_plan_hash:
            raise SystemExit("Existing session plan differs from the selected config.")
        try:
            stored_plan_hash = plan_hash(manifest["samples"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SystemExit(f"Stored sample plan is invalid: {exc}") from exc
        if stored_plan_hash != expected_plan_hash:
            raise SystemExit("Stored samples no longer match the immutable plan hash.")
        if manifest.get("configuration_fingerprint") != expected_fingerprint:
            raise SystemExit(
                "Config, source, stage mapping, or calibration fingerprint changed. "
                "Create a new dry-plan session."
            )
        _verify_session_input(
            manifest,
            "stereo_calibration",
            str(expected_fingerprint["stereo_calibration_sha256"]),
            "stereo calibration",
        )
        _verify_session_input(
            manifest,
            "stage_config_copy",
            str(expected_fingerprint["stage_config_sha256"]),
            "stage config",
        )
        if manifest.get("motion_review_csv"):
            _verify_session_input(
                manifest,
                "motion_review_csv",
                str(manifest.get("motion_review_csv_sha256", "")),
                "motion review CSV",
            )
        print(f"[RESUME] {session_dir}")
        return session_dir, manifest_path, manifest

    inputs_dir = session_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    calibration_copy = inputs_dir / "stereo_calibration.npz"
    stage_config_copy = inputs_dir / "ossila_3axis_config.md"
    motion_review_csv = session_dir / "planned_motion_targets.csv"
    shutil.copy2(Path(resolved["stereo_calibration"]), calibration_copy)
    shutil.copy2(Path(resolved["stage_config"]), stage_config_copy)
    write_motion_review_csv(motion_review_csv, planned)
    manifest = {
        "schema_version": 1,
        "name": str(config.get("name", "stereo_3axis_stage_accuracy")),
        "session_dir": str(session_dir),
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "status": "planned" if not args.execute else "initializing",
        "plan_profile": str(config.get("plan_profile", "full")),
        "config": config,
        "tracking": config["tracking"],
        "analysis": config["analysis"],
        "config_source": str(config_path),
        "session_config": str((session_dir / "session_config.json").resolve()),
        "stage_config_source": str(resolved["stage_config"]),
        "stage_config_copy": str(stage_config_copy.resolve()),
        "motion_review_csv": str(motion_review_csv.resolve()),
        "motion_review_csv_sha256": file_sha256(motion_review_csv),
        "stereo_calibration_source": str(resolved["stereo_calibration"]),
        "stereo_calibration": str(calibration_copy.resolve()),
        "axes": {
            axis: {
                "usb_serial": str(resolved["axes"][axis]["stage_serial"]),
                "direction": int(resolved["axes"][axis]["stage_direction"]),
            }
            for axis in AXES
        },
        "moving_body": "target",
        "absolute_distance_reference": config["absolute_distance_reference"],
        "coordinate_system": (
            "command_stage_mm is logical XYZ in millimetres; for each axis "
            "global=(hardware-datum)/config.md_direction."
        ),
        "configuration_fingerprint": expected_fingerprint,
        "plan_hash": expected_plan_hash,
        "samples": planned,
    }
    atomic_write_json(session_dir / "session_config.json", config)
    atomic_write_json(manifest_path, manifest)
    return session_dir, manifest_path, manifest


def print_plan(
    config: dict[str, Any],
    resolved: dict[str, Any],
    samples: list[dict[str, Any]],
) -> None:
    print("=== Three-axis stage accuracy dry plan ===")
    print(
        f"Base move order: {' -> '.join(axis.upper() for axis in resolved['move_axis_order'])}; "
        f"depth={str(resolved['depth_axis']).upper()} with "
        f"{'lower' if resolved['depth_lower_hardware_is_safer'] else 'higher'} "
        "hardware coordinate treated as safer"
    )
    for axis in AXES:
        spec = config["stages"]["axes"][axis]
        print(
            f"{axis.upper()}: USB={spec['usb_serial']} direction={int(spec['direction']):+d} "
            f"datum={float(spec['datum_mm']):g} mm, "
            f"soft=[{float(spec['hardware_min_mm']):g},"
            f"{float(spec['hardware_max_mm']):g}] mm, "
            f"speed={float(spec['speed_mm_s']):g} mm/s"
        )
    role_counts = Counter(str(sample["role"]) for sample in samples)
    trajectory_counts = Counter(str(sample["trajectory"]) for sample in samples)
    approach_counts = Counter(
        str(sample["approach_direction"])
        for sample in samples
        if sample.get("preposition_stage_mm")
    )
    print(
        f"Samples: {len(samples)}; roles={dict(role_counts)}; "
        f"trajectories={dict(trajectory_counts)}; "
        f"controlled approaches={dict(approach_counts)}"
    )
    depth = str(resolved["depth_axis"])
    depth_spec = config["stages"]["axes"][depth]
    reference_hardware = float(depth_spec["datum_mm"])
    far_hardware = float(
        depth_spec[
            "hardware_min_mm"
            if bool(resolved["depth_lower_hardware_is_farther"])
            else "hardware_max_mm"
        ]
    )
    far_global = (
        far_hardware - reference_hardware
    ) / int(depth_spec["direction"])
    distance_sign = int(config["absolute_distance_reference"]["sign"])
    print(
        f"Depth mapping: hardware {reference_hardware:g} -> logical 0 -> D0; "
        f"hardware {far_hardware:g} -> logical {far_global:g} -> "
        f"D0{distance_sign * far_global:+g} mm"
    )
    print("First/last planned coordinates:")
    for sample in (samples[:3] + samples[-3:] if len(samples) > 6 else samples):
        print(
            f"  {sample['sample_id']} {sample['pass_name']} "
            f"{sample['data_role']} logical={sample['command_stage_mm']} "
            f"hardware={sample['command_hardware_mm']}"
        )


def missing_execution_locks(config: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for axis in AXES:
        spec = config["stages"]["axes"][axis]
        prefix = f"stages.axes.{axis}"
        if not str(spec.get("expected_device_response", "")).strip():
            missing.append(f"{prefix}.expected_device_response")
        if not str(spec.get("expected_internal_serial_response", "")).strip():
            missing.append(f"{prefix}.expected_internal_serial_response")
        for key in ("expected_acceleration_mm_s2", "expected_deceleration_mm_s2"):
            if spec.get(key) is None:
                missing.append(f"{prefix}.{key}")
    for key, confirmed in config["safety_acknowledgements"].items():
        if confirmed is not True:
            missing.append(f"safety_acknowledgements.{key}")
    target = config["target"]
    for key in ("driver_description", "modulation_verification_instrument"):
        if not str(target.get(key, "")).strip():
            missing.append(f"target.{key}")
    target_type = str(target.get("type", "")).lower()
    if "fiber" in target_type:
        if target.get("fiber_core_diameter_mm") is None:
            missing.append("target.fiber_core_diameter_mm")
    elif target.get("aperture_diameter_mm") is None:
        missing.append("target.aperture_diameter_mm")
    return missing


def probe_one_axis(
    config: dict[str, Any],
    resolved: dict[str, Any],
    axis: str,
) -> dict[str, Any]:
    driver_config, driver_resolved = axis_driver_inputs(config, resolved, axis)
    driver: SafeOssilaAxis | None = None
    try:
        driver = SafeOssilaAxis(driver_config, driver_resolved)
        status = driver.query_status()
        if abs(float(status["speed_mm_s"])) > 1e-9:
            driver.emergency_stop()
            raise RuntimeError(
                f"Axis {axis.upper()} was moving when opened; stop was attempted."
            )
        device, device_response = driver.query_text("device")
        firmware, firmware_response = driver.query_text("firmware")
        internal_serial, serial_response = driver.query_text("serial")
        length_raw, length_response = driver.query_float("length")
        expected_travel = float(config["stages"]["axes"][axis]["expected_travel_mm"])
        normalization_error = ""
        try:
            length_mm, interpretation = driver.normalize_reported_length_mm(
                length_raw,
                expected_travel,
            )
        except RuntimeError as exc:
            length_mm = None
            interpretation = "does_not_match_configured_expected_travel"
            normalization_error = str(exc)
        acceleration, acceleration_response = driver.query_float("acc")
        deceleration, deceleration_response = driver.query_float("dec")
        speed, speed_response = driver.query_float("speed")
        posmode, posmode_response = driver.query_posmode()
        alarms, alarms_response = driver.query_alarms()
        return {
            "axis": axis,
            "usb_serial": str(resolved["axes"][axis]["stage_serial"]),
            "config_direction": int(resolved["axes"][axis]["stage_direction"]),
            "device": device,
            "device_response": device_response,
            "firmware": firmware,
            "firmware_response": firmware_response,
            "internal_serial": internal_serial,
            "serial_response": serial_response,
            "length_mm": length_mm,
            "length_raw": length_raw,
            "length_unit_interpretation": interpretation,
            "length_normalization_error": normalization_error,
            "length_response": length_response,
            "acceleration_mm_s2": acceleration,
            "acceleration_response": acceleration_response,
            "deceleration_mm_s2": deceleration,
            "deceleration_response": deceleration_response,
            "stored_speed_mm_s": speed,
            "speed_response": speed_response,
            "posmode": posmode,
            "posmode_response": posmode_response,
            "alarms": alarms,
            "alarms_response": alarms_response,
            "status": status,
            "probed_at": dt.datetime.now().isoformat(timespec="milliseconds"),
        }
    finally:
        if driver is not None:
            driver.close()


class ThreeAxisStage:
    """Coordinate three reviewed single-axis drivers without simultaneous moves."""

    def __init__(
        self,
        config: dict[str, Any],
        resolved: dict[str, Any],
        drivers: dict[str, SafeOssilaAxis],
    ):
        self.config = config
        self.resolved = resolved
        self.drivers = drivers
        self.move_order = list(resolved["move_axis_order"])

    def move_order_for(
        self,
        before: dict[str, Any],
        target: dict[str, float],
    ) -> list[str]:
        depth = str(self.resolved["depth_axis"])
        lateral = [axis for axis in self.move_order if axis != depth]
        current_hardware = float(before["axes"][depth]["hardware_mm"])
        target_hardware = self.drivers[depth].hardware_from_global(target[depth])
        lower_is_safer = bool(self.resolved["depth_lower_hardware_is_safer"])
        moving_safer = (
            target_hardware < current_hardware - 1e-9
            if lower_is_safer
            else target_hardware > current_hardware + 1e-9
        )
        if moving_safer:
            return [depth, *lateral]
        return [*lateral, depth]

    def read_all(self, label: str) -> dict[str, Any]:
        axes: dict[str, Any] = {}
        for axis in AXES:
            driver = self.drivers[axis]
            status = driver.query_status()
            global_mm, hardware_mm, position_response = driver.global_position_mm()
            axes[axis] = {
                "global_mm": global_mm,
                "hardware_mm": hardware_mm,
                "position_response": position_response,
                "status": status,
            }
        return {
            "label": label,
            "read_at": dt.datetime.now().isoformat(timespec="milliseconds"),
            "axes": axes,
        }

    def validate_readback(
        self,
        readback: dict[str, Any],
        target: dict[str, float],
        *,
        drift_mode: bool = False,
    ) -> list[str]:
        errors: list[str] = []
        for axis in AXES:
            spec = self.config["stages"]["axes"][axis]
            item = readback["axes"][axis]
            status = item["status"]
            if abs(float(status["speed_mm_s"])) > 1e-9:
                errors.append(f"{axis}:speed={float(status['speed_mm_s']):+.6f}")
            if bool(status["end_switch"]):
                errors.append(f"{axis}:end_switch_active")
            status_position_error = (
                float(status["position_hardware_mm"]) - float(item["hardware_mm"])
            )
            if abs(status_position_error) > float(spec["readback_tolerance_mm"]):
                errors.append(
                    f"{axis}:status_vs_pos={status_position_error:+.6f}>"
                    f"{float(spec['readback_tolerance_mm']):.6f}"
                )
            tolerance_key = (
                "capture_drift_tolerance_mm" if drift_mode else "readback_tolerance_mm"
            )
            error = float(item["global_mm"]) - float(target[axis])
            if abs(error) > float(spec[tolerance_key]):
                errors.append(
                    f"{axis}:position_error={error:+.6f}>"
                    f"{float(spec[tolerance_key]):.6f}"
                )
        return errors

    def _move_absolute_direct(
        self,
        target: dict[str, float],
        *,
        reason: str,
    ) -> dict[str, Any]:
        initial = self.read_all(f"{reason}:before")
        actual_order = self.move_order_for(initial, target)
        trace: dict[str, Any] = {
            "reason": reason,
            "target_global_mm": {axis: float(target[axis]) for axis in AXES},
            "started_at": dt.datetime.now().isoformat(timespec="milliseconds"),
            "before_all_axes": initial,
            "actual_axis_order": actual_order,
            "axis_steps": [],
        }
        for axis in actual_order:
            before_step = self.read_all(f"{reason}:before_{axis}")
            current = float(before_step["axes"][axis]["global_mm"])
            requested = float(target[axis])
            if math.isclose(current, requested, rel_tol=0.0, abs_tol=1e-9):
                arrival: dict[str, Any] = {
                    "skipped": True,
                    "reason": "already_at_target",
                    "command_global_mm": requested,
                }
            else:
                print(
                    f"[MOVE] {axis.upper()} global {current:+.3f} -> {requested:+.3f} mm"
                )
                arrival = self.drivers[axis].move_global(requested)
            after_step = self.read_all(f"{reason}:after_{axis}")
            errors = self.validate_readback(
                after_step,
                {
                    other: (
                        float(target[other])
                        if other == axis
                        else float(before_step["axes"][other]["global_mm"])
                    )
                    for other in AXES
                },
            )
            if errors:
                raise RuntimeError(
                    f"Three-axis state invalid after {axis.upper()} move: {errors}"
                )
            trace["axis_steps"].append(
                {
                    "axis": axis,
                    "before_all_axes": before_step,
                    "axis_arrival": arrival,
                    "after_all_axes": after_step,
                }
            )
        final = self.read_all(f"{reason}:final")
        errors = self.validate_readback(final, target)
        if errors:
            raise RuntimeError(f"Final three-axis arrival is invalid: {errors}")
        trace["after_all_axes"] = final
        trace["finished_at"] = dt.datetime.now().isoformat(timespec="milliseconds")
        return trace

    def move_absolute(
        self,
        target: dict[str, float],
        *,
        reason: str,
    ) -> dict[str, Any]:
        """Move through a mandatory depth-clearance waypoint when needed."""
        initial = self.read_all(f"{reason}:clearance_check")
        depth = str(self.resolved["depth_axis"])
        lateral = [axis for axis in AXES if axis != depth]
        lateral_change = any(
            not math.isclose(
                float(initial["axes"][axis]["global_mm"]),
                float(target[axis]),
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            for axis in lateral
        )
        clearance = float(
            self.config["stages"]["lateral_motion_clearance_global_mm"]
        )
        distance_sign = float(
            self.config["absolute_distance_reference"]["sign"]
        )

        def at_or_beyond_clearance(value: float) -> bool:
            return distance_sign * (float(value) - clearance) >= -1e-9

        current_depth = float(initial["axes"][depth]["global_mm"])
        target_depth = float(target[depth])
        needs_clearance_reroute = (
            lateral_change
            and not at_or_beyond_clearance(current_depth)
            and not at_or_beyond_clearance(target_depth)
        )
        if not needs_clearance_reroute:
            return self._move_absolute_direct(target, reason=reason)

        current = {
            axis: float(initial["axes"][axis]["global_mm"]) for axis in AXES
        }
        retract = dict(current)
        retract[depth] = clearance
        lateral_waypoint = {
            axis: (
                clearance
                if axis == depth
                else float(target[axis])
            )
            for axis in AXES
        }
        print(
            f"[CLEARANCE] retract {depth.upper()} to {clearance:+.3f} mm "
            "before lateral motion"
        )
        segments = [
            self._move_absolute_direct(
                retract,
                reason=f"{reason}:clearance_retract",
            ),
            self._move_absolute_direct(
                lateral_waypoint,
                reason=f"{reason}:clearance_lateral",
            ),
            self._move_absolute_direct(
                target,
                reason=f"{reason}:clearance_approach",
            ),
        ]
        return {
            "reason": reason,
            "target_global_mm": {
                axis: float(target[axis]) for axis in AXES
            },
            "clearance_reroute": True,
            "clearance_global_mm": clearance,
            "before_all_axes": initial,
            "axis_steps": [
                step
                for segment in segments
                for step in segment.get("axis_steps", [])
            ],
            "clearance_segments": segments,
            "after_all_axes": segments[-1]["after_all_axes"],
            "started_at": segments[0]["started_at"],
            "finished_at": segments[-1]["finished_at"],
        }

    def emergency_stop_all(self) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for axis in AXES:
            driver = self.drivers.get(axis)
            if driver is None:
                continue
            try:
                results[axis] = {
                    "verified": bool(driver.emergency_stop()),
                    "error": "",
                }
            except BaseException as exc:
                results[axis] = {
                    "verified": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        return results


def confirm_motion(
    args: argparse.Namespace,
    config: dict[str, Any],
    resolved: dict[str, Any],
) -> bool:
    home_axes = [str(axis).lower() for axis in args.home_axes]
    if home_axes:
        print("Homing safety check:")
        print(
            "  - requested axes: "
            f"{', '.join(a.upper() for a in home_axes)}; "
            "execution order is Y clearance first, then X, then Z"
        )
        print("  - every complete HOME path and every HOME-to-reference path is clear")
        print("  - cables remain slack, and accessible power disconnects are within reach")
        expected = "HOME " + " ".join(axis.upper() for axis in home_axes)
        return input(f"Type {expected} to authorize: ").strip() == expected
    if args.yes:
        return True
    reference = config["experiment"]["reference_global_mm"]
    print("Physical safety check:")
    print("  - X/Y/Z are homed and absolute position mode is active")
    print("  - all listed paths and sequential intermediate positions are collision-free")
    print("  - cables are slack and all accessible power disconnects are within reach")
    print(
        "  - reference logical XYZ is "
        f"{[float(item) for item in reference]} mm; move order is "
        f"{[axis.upper() for axis in resolved['move_axis_order']]}"
    )
    return input("Type RUN to authorize three-axis motion: ").strip() == "RUN"


def build_record_command(
    config: dict[str, Any],
    calibration: Path,
    run_dir: Path,
    sample: dict[str, Any],
) -> list[str]:
    camera = config["camera"]
    note_payload = {
        "experiment": "stereo_3axis_stage_accuracy",
        "sample_id": sample["sample_id"],
        "role": sample["role"],
        "trajectory": sample["trajectory"],
        "sweep_name": sample.get("sweep_name", ""),
        "cycle": sample["cycle"],
        "approach_direction": sample["approach_direction"],
        "approach_axis": sample.get("approach_axis", ""),
        "preposition_stage_mm": sample.get("preposition_stage_mm", {}),
        "command_stage_mm": sample["command_stage_mm"],
    }
    command = [
        sys.executable,
        str(SCRIPT_DIR / "stereo_eventcam_record_sync.py"),
        "--left-serial",
        str(camera["left_serial"]),
        "--right-serial",
        str(camera["right_serial"]),
        "--run-dir",
        str(run_dir),
        "--duration-sec",
        str(float(camera["record_sec"])),
        "--delta-t-us",
        str(int(camera["delta_t_us"])),
        "--start-delay-sec",
        str(float(camera["start_delay_sec"])),
        "--hw-sync",
        str(camera["hw_sync"]),
        "--hw-sync-timeout-sec",
        str(float(camera["hw_sync_timeout_sec"])),
        "--npz-compression",
        str(camera["npz_compression"]),
        "--stereo-calibration",
        str(calibration),
        "--note",
        json.dumps(note_payload, separators=(",", ":")),
        "--no-preview",
    ]
    max_events = int(camera["max_events"])
    if max_events > 0:
        command.extend(["--max-events", str(max_events)])
    return command


def _artifact_hashes(run_dir: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    candidates = [
        run_dir / "stereo_recording_manifest.json",
        run_dir / "left" / "left_recording_meta.json",
        run_dir / "left" / "left_events.npz",
        run_dir / "right" / "right_recording_meta.json",
        run_dir / "right" / "right_events.npz",
    ]
    for path in candidates:
        if path.is_file():
            result[path.relative_to(run_dir).as_posix()] = file_sha256(path)
    return result


def verify_captured_samples(manifest: dict[str, Any]) -> None:
    for sample in manifest["samples"]:
        if sample.get("capture_status") != "captured":
            continue
        run_text = str(sample.get("accepted_run_dir", "")).strip()
        if not run_text:
            raise SystemExit(
                f"Captured sample {sample['sample_id']} has no accepted_run_dir."
            )
        run_dir = Path(run_text)
        required = [
            run_dir / "stereo_recording_manifest.json",
            run_dir / "left" / "left_recording_meta.json",
            run_dir / "left" / "left_events.npz",
            run_dir / "right" / "right_recording_meta.json",
            run_dir / "right" / "right_events.npz",
        ]
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise SystemExit(
                f"Captured sample {sample['sample_id']} is incomplete: {missing}"
            )
        stored_hashes = sample.get("accepted_artifact_sha256", {})
        if stored_hashes:
            actual_hashes = _artifact_hashes(run_dir)
            if stored_hashes != actual_hashes:
                raise SystemExit(
                    f"Captured artifacts changed for {sample['sample_id']}; "
                    "preserve this session and create a new one."
                )


def capture_sample(
    *,
    config: dict[str, Any],
    calibration: Path,
    session_dir: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    sample: dict[str, Any],
    stages: ThreeAxisStage,
    move_trace: dict[str, Any],
) -> bool:
    target = {axis: float(sample["command_stage_mm"][axis]) for axis in AXES}
    sample["move_trace"] = move_trace
    sample["arrival_readback_all_axes"] = move_trace["after_all_axes"]
    update_manifest(manifest_path, manifest)
    camera = config["camera"]
    sample_root = session_dir / "captures" / str(sample["sample_id"])
    sample_root.mkdir(parents=True, exist_ok=True)
    used_indices: list[int] = []
    for attempt in sample.get("attempts", []):
        try:
            used_indices.append(int(attempt["attempt"]))
        except (KeyError, TypeError, ValueError):
            pass
    for path in sample_root.glob("attempt_*"):
        try:
            used_indices.append(int(path.name.rsplit("_", 1)[-1]))
        except ValueError:
            pass
    first_index = max(used_indices, default=0) + 1
    max_attempts = int(camera["max_capture_attempts"])
    minimum_events = int(camera["minimum_events_per_camera"])

    for local_attempt in range(max_attempts):
        attempt_index = first_index + local_attempt
        run_dir = sample_root / f"attempt_{attempt_index:02d}"
        before = stages.read_all(
            f"{sample['sample_id']}:attempt{attempt_index}:before_capture"
        )
        before_errors = stages.validate_readback(before, target)
        if before_errors:
            raise RuntimeError(
                f"All-axis pre-capture readback failed for {sample['sample_id']}: "
                f"{before_errors}"
            )
        attempt = {
            "attempt": attempt_index,
            "run_dir": str(run_dir.resolve()),
            "started_at": dt.datetime.now().isoformat(timespec="milliseconds"),
            "finished_at": "",
            "return_code": None,
            "left_events": 0,
            "right_events": 0,
            "stage_readback_before_capture": before,
            "stage_readback_after_capture": {},
            "stage_drift_global_mm": {},
            "stage_stable": False,
            "accepted": False,
            "status": "recording",
        }
        sample.setdefault("attempts", []).append(attempt)
        sample["status"] = "recording"
        sample["capture_status"] = "recording"
        update_manifest(manifest_path, manifest)
        command = build_record_command(config, calibration, run_dir, sample)
        print(
            f"[CAPTURE] {sample['sample_id']} {sample['pass_name']} "
            f"logical={sample['command_stage_mm']} attempt={local_attempt + 1}/{max_attempts}",
            flush=True,
        )
        return_code: int | None
        command_error = ""
        try:
            completed = subprocess.run(command, check=False, cwd=SCRIPT_DIR)
            return_code = int(completed.returncode)
        except Exception as exc:
            return_code = None
            command_error = f"{type(exc).__name__}: {exc}"
        after = stages.read_all(
            f"{sample['sample_id']}:attempt{attempt_index}:after_capture"
        )
        after_errors = stages.validate_readback(after, target)
        drift: dict[str, float] = {}
        drift_errors: list[str] = []
        for axis in AXES:
            value = (
                float(after["axes"][axis]["global_mm"])
                - float(before["axes"][axis]["global_mm"])
            )
            drift[axis] = value
            tolerance = float(
                config["stages"]["axes"][axis]["capture_drift_tolerance_mm"]
            )
            if abs(value) > tolerance:
                drift_errors.append(
                    f"{axis}:drift={value:+.6f}>{tolerance:.6f}"
                )
        left_events, right_events = read_capture_counts(run_dir)
        stable = not after_errors and not drift_errors
        accepted = (
            return_code == 0
            and left_events >= minimum_events
            and right_events >= minimum_events
            and stable
        )
        attempt.update(
            {
                "finished_at": dt.datetime.now().isoformat(timespec="milliseconds"),
                "return_code": return_code,
                "command_error": command_error,
                "left_events": left_events,
                "right_events": right_events,
                "stage_readback_after_capture": after,
                "stage_drift_global_mm": drift,
                "stage_validation_errors": after_errors + drift_errors,
                "stage_stable": stable,
                "artifact_sha256": _artifact_hashes(run_dir),
                "accepted": accepted,
                "status": "accepted" if accepted else "failed",
            }
        )
        if accepted:
            sample["status"] = "captured"
            sample["capture_status"] = "captured"
            sample["accepted_run_dir"] = str(run_dir.resolve())
            sample["accepted_artifact_sha256"] = attempt["artifact_sha256"]
            sample["capture_readback_before_all_axes"] = before
            sample["capture_readback_after_all_axes"] = after
            update_manifest(manifest_path, manifest)
            print(
                f"[CAPTURE][OK] left={left_events:,} right={right_events:,} "
                f"drift={drift}"
            )
            return True
        sample["status"] = (
            "retry_pending" if local_attempt + 1 < max_attempts else "failed"
        )
        sample["capture_status"] = sample["status"]
        update_manifest(manifest_path, manifest)
        print(
            f"[CAPTURE][FAIL] return={return_code} left={left_events:,} "
            f"right={right_events:,} stage_stable={stable}"
        )
        wait_sec = float(camera["camera_reopen_wait_sec"])
        if wait_sec > 0:
            time.sleep(wait_sec)
    return False


def open_and_preflight_axes(
    config: dict[str, Any],
    resolved: dict[str, Any],
    home_axes: set[str],
) -> tuple[dict[str, SafeOssilaAxis], dict[str, Any]]:
    drivers: dict[str, SafeOssilaAxis] = {}
    preflight: dict[str, Any] = {}
    try:
        for axis in AXES:
            driver_config, driver_resolved = axis_driver_inputs(
                config,
                resolved,
                axis,
            )
            driver = SafeOssilaAxis(driver_config, driver_resolved)
            drivers[axis] = driver
            initial_status = driver.query_status()
            if abs(float(initial_status["speed_mm_s"])) > 1e-9:
                driver.emergency_stop()
                raise RuntimeError(
                    f"Axis {axis.upper()} was moving when opened; stop attempted."
                )
            result = driver.preflight(
                require_absolute=axis not in home_axes,
                allow_end_switch_for_homing=axis in home_axes,
                allow_outside_soft_limit_for_homing=axis in home_axes,
            )
            preflight[axis] = driver.device_snapshot(result)
        return drivers, preflight
    except BaseException:
        for driver in drivers.values():
            try:
                driver.emergency_stop()
            except BaseException:
                pass
            try:
                driver.close()
            except BaseException:
                pass
        raise


def main() -> int:
    args = parse_args()
    if args.list_stage_ports:
        if any((args.execute, args.probe_stage_identities, args.home_axes)):
            raise SystemExit(
                "--list-stage-ports cannot be combined with hardware-control options."
            )
        return list_stage_ports()
    if args.probe_stage_identities and not args.execute:
        raise SystemExit("--probe-stage-identities requires --execute.")
    if args.probe_stage_identities and args.home_axes:
        raise SystemExit("The read-only identity probe cannot be combined with --home-axes.")
    if args.home_axes and args.yes:
        raise SystemExit("Homing always requires its dedicated typed confirmation; omit --yes.")
    if len(set(args.home_axes)) != len(args.home_axes):
        raise SystemExit("--home-axes contains a duplicate axis.")

    config_path = args.config.resolve()
    config = load_json(config_path)
    if args.pilot:
        config = apply_pilot_profile(config)
    resolved = validate_config(config, config_path)
    planned = generate_plan(config)
    session_dir, manifest_path, manifest = create_or_resume_session(
        args,
        config,
        config_path,
        resolved,
        planned,
    )
    print_plan(config, resolved, manifest["samples"])
    print(f"Session: {session_dir}")

    if not args.execute:
        if manifest.get("cleanup_verified") is False:
            print(
                "[SAFETY LOCK] Prior cleanup was not verified; preserve this session "
                "and create a new dry plan.",
                file=sys.stderr,
            )
            return 2
        update_manifest(manifest_path, manifest, status="planned")
        command = subprocess.list2cmdline(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--config",
                str(config_path),
                "--session-dir",
                str(session_dir),
                *(["--pilot"] if args.pilot else []),
                "--execute",
            ]
        )
        print("[DRY PLAN] No serial port or camera was opened.")
        locks = missing_execution_locks(config)
        if locks:
            print(
                "[EXECUTION LOCKS] Fill/confirm these fields, then create a "
                "fresh dry plan:"
            )
            for lock in locks:
                print(f"  - {lock}")
            print(
                "[NEXT] Do not execute this locked session. Update the source "
                "config and create a new dry-plan session."
            )
        else:
            print("[NEXT] After physical review, reuse this exact session:")
            print(command)
        return 0

    if args.probe_stage_identities:
        probes: dict[str, Any] = {}
        try:
            for axis in AXES:
                print(
                    f"[PROBE] {axis.upper()} identity/settings; no home/goto/camera "
                    "command (a moving axis is stopped for safety)"
                )
                probes[axis] = probe_one_axis(config, resolved, axis)
        except Exception as exc:
            manifest["axis_identity_probe"] = probes
            manifest["axis_identity_probe_error"] = f"{type(exc).__name__}: {exc}"
            update_manifest(manifest_path, manifest, status="axis_probe_failed")
            print(f"[PROBE][ERROR] {exc}", file=sys.stderr)
            return 2
        manifest["axis_identity_probe"] = probes
        update_manifest(manifest_path, manifest, status="axis_identity_probed")
        print(json.dumps(json_ready(probes), indent=2, ensure_ascii=False))
        print(
            "Copy each axis device, internal_serial, acceleration_mm_s2, and "
            "deceleration_mm_s2 into the execution-lock fields, then create a fresh dry plan."
        )
        return 0

    if manifest.get("cleanup_verified") is False:
        print(
            "[SAFETY LOCK] Previous run did not verify all-axis stop/close. "
            "Inspect all stages and disconnect accessible power. This session is not resumable.",
            file=sys.stderr,
        )
        return 2
    locks = missing_execution_locks(config)
    if locks:
        raise SystemExit(
            "Execution is locked until physical values are filled: "
            + ", ".join(locks)
            + ". Run the read-only identity probe, update config, and create a fresh dry plan."
        )
    verify_captured_samples(manifest)
    captured_existing = [
        sample
        for sample in manifest["samples"]
        if sample.get("capture_status") == "captured"
    ]
    pending = [
        sample
        for sample in manifest["samples"]
        if sample.get("capture_status") != "captured"
    ]
    if captured_existing and pending:
        raise SystemExit(
            "A partially captured session cannot be resumed across process/run "
            "epochs because its orientation and center references would be stale. "
            "Preserve this session and create a fresh dry-plan session."
        )
    if not pending:
        update_manifest(manifest_path, manifest, status="captured")
        print("All planned samples are already captured and verified.")
        return 0

    calibration = Path(str(manifest["stereo_calibration"]))
    drivers: dict[str, SafeOssilaAxis] = {}
    stages: ThreeAxisStage | None = None
    completed_normally = False
    interrupted = False
    capture_result = 2
    cleanup_errors: list[str] = []
    reference = {
        axis: float(config["experiment"]["reference_global_mm"][index])
        for index, axis in enumerate(AXES)
    }
    try:
        manifest["cleanup_verified"] = False
        manifest["cleanup_errors"] = []
        update_manifest(
            manifest_path,
            manifest,
            status="run_active_cleanup_required",
        )
        drivers, preflight = open_and_preflight_axes(
            config,
            resolved,
            set(args.home_axes),
        )
        stages = ThreeAxisStage(config, resolved, drivers)
        manifest["stage_preflight_before"] = preflight
        update_manifest(manifest_path, manifest)

        print("[PREFLIGHT] synchronized stereo stream before any motion")
        camera_preflight = run_camera_stream_preflight(
            config,
            calibration,
            session_dir,
        )
        minimum_events = int(config["camera"]["minimum_events_per_camera"])
        if (
            int(camera_preflight["left_total_events"]) < minimum_events
            or int(camera_preflight["right_total_events"]) < minimum_events
        ):
            raise RuntimeError(
                "Camera preflight streamed successfully but the stationary reference "
                f"did not produce at least {minimum_events} events per camera."
            )
        manifest["camera_stream_preflight"] = camera_preflight
        update_manifest(manifest_path, manifest)
        if not confirm_motion(args, config, resolved):
            update_manifest(manifest_path, manifest, status="user_aborted")
            print("Aborted before any home/goto command.")
            capture_result = 1
            return capture_result

        requested_home = set(args.home_axes)
        lateral_home = requested_home.intersection({"x", "z"})
        depth_spec = config["stages"]["axes"]["y"]
        safe_home_hardware = (
            float(depth_spec["hardware_min_mm"])
            if bool(resolved["depth_lower_hardware_is_safer"])
            else float(depth_spec["hardware_max_mm"])
        )
        safe_home_depth = (
            safe_home_hardware - float(depth_spec["datum_mm"])
        ) / int(depth_spec["direction"])
        if "y" in requested_home:
            print("[HOME] Y (first, before any lateral HOME)")
            manifest.setdefault("stage_home", {})["y"] = drivers["y"].home()
            drivers["y"].authorize_recovery_move(safe_home_depth)
            manifest["stage_home"]["y_safe_recovery"] = drivers["y"].move_global(
                safe_home_depth
            )
            update_manifest(manifest_path, manifest)
        elif lateral_home:
            # X/Z may still be in relative mode, where this firmware returns
            # ``<pos undef>``.  Move only the already-absolute depth axis; an
            # all-axis coordinate read is neither available nor needed here.
            manifest["lateral_home_clearance_move"] = {
                "reason": "lateral_home_full_depth_clearance",
                "axis": "y",
                "axis_arrival": drivers["y"].move_global(safe_home_depth),
            }
            update_manifest(manifest_path, manifest)
        for axis in ("x", "z"):
            if axis not in requested_home:
                continue
            print(f"[HOME] {axis.upper()} at Y={safe_home_depth:+.3f} mm")
            manifest.setdefault("stage_home", {})[axis] = drivers[axis].home()
            drivers[axis].authorize_recovery_move(reference[axis])
            manifest["stage_home"][f"{axis}_reference_recovery"] = drivers[
                axis
            ].move_global(reference[axis])
            update_manifest(manifest_path, manifest)

        # A complete absolute-coordinate read is valid only after every axis
        # requested above has been homed.
        manifest["stage_readback_before_run"] = stages.read_all(
            "after_requested_homing"
        )
        manifest["reference_move_before_capture"] = stages.move_absolute(
            reference,
            reason="initial_reference",
        )
        update_manifest(manifest_path, manifest, status="capturing")

        last_target: dict[str, float] | None = None
        for sample in pending:
            target = {
                axis: float(sample["command_stage_mm"][axis]) for axis in AXES
            }
            preposition_raw = sample.get("preposition_stage_mm", {})
            if isinstance(preposition_raw, dict) and preposition_raw:
                preposition = {
                    axis: float(preposition_raw[axis]) for axis in AXES
                }
                preposition_trace = stages.move_absolute(
                    preposition,
                    reason=f"sample:{sample['sample_id']}:approach_preposition",
                )
                move_trace = stages.move_absolute(
                    target,
                    reason=f"sample:{sample['sample_id']}:controlled_approach",
                )
                move_trace["preposition_move_trace"] = preposition_trace
                approach_axis = str(sample.get("approach_axis", "")).lower()
                approach_delta = (
                    float(
                        move_trace["after_all_axes"]["axes"][approach_axis][
                            "global_mm"
                        ]
                    )
                    - float(
                        move_trace["before_all_axes"]["axes"][approach_axis][
                            "global_mm"
                        ]
                    )
                )
                expected_positive = (
                    str(sample.get("approach_direction")) == "positive"
                )
                direction_matches = (
                    approach_delta > 0 if expected_positive else approach_delta < 0
                )
                sample["approach_verification"] = {
                    "axis": approach_axis,
                    "expected_direction": sample.get("approach_direction"),
                    "observed_final_move_global_mm": approach_delta,
                    "matches": direction_matches,
                }
                if not direction_matches:
                    raise RuntimeError(
                        f"Controlled approach direction failed for "
                        f"{sample['sample_id']}: {sample['approach_verification']}"
                    )
            elif last_target is not None and all(
                math.isclose(
                    target[axis],
                    last_target[axis],
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                for axis in AXES
            ):
                readback = stages.read_all(f"{sample['sample_id']}:same_coordinate")
                errors = stages.validate_readback(readback, target)
                if errors:
                    raise RuntimeError(
                        f"Same-coordinate validation failed for {sample['sample_id']}: "
                        f"{errors}"
                    )
                move_trace = {
                    "reason": f"sample:{sample['sample_id']}",
                    "target_global_mm": target,
                    "skipped": True,
                    "before_all_axes": readback,
                    "axis_steps": [],
                    "after_all_axes": readback,
                    "started_at": readback["read_at"],
                    "finished_at": readback["read_at"],
                }
            else:
                move_trace = stages.move_absolute(
                    target,
                    reason=f"sample:{sample['sample_id']}",
                )
            last_target = target
            ok = capture_sample(
                config=config,
                calibration=calibration,
                session_dir=session_dir,
                manifest_path=manifest_path,
                manifest=manifest,
                sample=sample,
                stages=stages,
                move_trace=move_trace,
            )
            if not ok and not args.continue_on_error:
                update_manifest(manifest_path, manifest, status="capture_failed")
                capture_result = 2
                return capture_result

        captured_count = sum(
            sample.get("capture_status") == "captured"
            for sample in manifest["samples"]
        )
        completed_normally = captured_count == len(manifest["samples"])
        manifest["capture_complete"] = completed_normally
        update_manifest(
            manifest_path,
            manifest,
            status=(
                "capture_complete_cleanup_pending"
                if completed_normally
                else "captured_partial_cleanup_pending"
            ),
        )
        capture_result = 0 if completed_normally else 2
        return capture_result
    except KeyboardInterrupt:
        interrupted = True
        if stages is not None:
            manifest["immediate_stop_on_interrupt"] = (
                stages.emergency_stop_all()
            )
        update_manifest(manifest_path, manifest, status="interrupted")
        print("\n[STOP] Interrupted; stopping all opened axes.", file=sys.stderr)
        capture_result = 130
        return capture_result
    except BaseException as exc:
        if stages is not None:
            manifest["immediate_stop_on_error"] = stages.emergency_stop_all()
        update_manifest(manifest_path, manifest, status="error")
        print(f"[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        capture_result = 2
        return capture_result
    finally:
        if stages is not None:
            if completed_normally and bool(
                config["stages"]["return_to_reference_on_success"]
            ):
                try:
                    manifest["reference_move_after_capture"] = stages.move_absolute(
                        reference,
                        reason="successful_return_to_reference",
                    )
                except BaseException as exc:
                    cleanup_errors.append(
                        f"return_to_reference: {type(exc).__name__}: {exc}"
                    )
            stop_results = stages.emergency_stop_all()
            manifest["all_axis_stop_results"] = stop_results
            for axis, result in stop_results.items():
                if not result.get("verified"):
                    cleanup_errors.append(
                        f"{axis}_stop_unverified:{result.get('error', '')}"
                    )
            try:
                manifest["stage_readback_after_stop"] = stages.read_all(
                    "cleanup_after_stop"
                )
            except BaseException as exc:
                # Position can legitimately be unavailable as ``<pos undef>``
                # while an axis remains unhomed.  Cleanup verification is based
                # on verified stop commands and successful connection closes;
                # retain this readback failure as diagnostic information.
                manifest["stage_readback_after_stop_error"] = (
                    f"final_readback: {type(exc).__name__}: {exc}"
                )
        for axis, driver in drivers.items():
            try:
                driver.close()
            except BaseException as exc:
                cleanup_errors.append(
                    f"{axis}_close:{type(exc).__name__}: {exc}"
                )
        if drivers:
            manifest["cleanup_verified"] = not cleanup_errors
            manifest["cleanup_errors"] = cleanup_errors
            manifest["cleanup_completed_at"] = dt.datetime.now().isoformat(
                timespec="milliseconds"
            )
            final_status: str | None = None
            if cleanup_errors:
                final_status = (
                    "capture_complete_cleanup_failed"
                    if completed_normally
                    else "cleanup_failed"
                )
                print(
                    "[SAFETY][CLEANUP FAILED] "
                    + " | ".join(cleanup_errors)
                    + ". Inspect all stages and disconnect accessible power.",
                    file=sys.stderr,
                )
            elif completed_normally:
                final_status = "captured"
            elif interrupted:
                final_status = "interrupted_cleanup_verified"
            try:
                update_manifest(manifest_path, manifest, status=final_status)
            except BaseException as exc:
                cleanup_errors.append(
                    f"final_manifest_write:{type(exc).__name__}:{exc}"
                )
                manifest["cleanup_verified"] = False
                print(
                    f"[SAFETY][WARN] Final manifest write failed: {exc}",
                    file=sys.stderr,
                )
        if completed_normally and not cleanup_errors:
            print(
                f"[DONE] {len(manifest['samples'])} samples captured; "
                "reference return and all-axis stop/close verified."
            )
        if drivers and cleanup_errors:
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
