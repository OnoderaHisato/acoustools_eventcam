#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Capture a static PAT particle at a non-coplanar 3D grid with two event cameras.

The PAT controller remains open in this parent process. Each stereo capture is
started as a short-lived subprocess, so both event cameras are fully closed
before the particle moves to the next point. The session manifest is updated
after every attempt and can be resumed after interruption.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import numpy as np


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "pat_stereo_grid_config.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Move a PAT particle through a 3D grid and capture paired event-camera recordings.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Grid/camera/tracking configuration JSON.")
    parser.add_argument("--output-root", type=Path, default=Path("pat_stereo_grid_records"), help="New session root.")
    parser.add_argument("--session-dir", type=Path, default=None, help="Exact session directory; resumes it when a manifest exists.")
    parser.add_argument("--dry-run", action="store_true", help="Generate and validate the session plan without opening hardware.")
    parser.add_argument(
        "--stage-hardware-readback-mm",
        type=float,
        default=None,
        help=(
            "Actual Ossila hardware readback at this camera-to-PAT registration. "
            "Required for non-dry capture and stored in the session manifest."
        ),
    )
    parser.add_argument("--no-preview", action="store_true", help="Skip the initial dual-camera particle preview.")
    parser.add_argument("--yes", action="store_true", help="Start the grid automatically without the final Enter confirmation.")
    parser.add_argument("--continue-on-error", action="store_true", help="Continue to later points after a point exhausts its capture attempts.")
    parser.add_argument("--process-each", action="store_true", help="Run stereo_process_recording.py after every accepted capture.")
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Configuration not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc


def resolve_from_config(path_text: str, config_path: Path) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path.resolve()
    config_relative = (config_path.parent / path).resolve()
    if config_relative.exists():
        return config_relative
    return (SCRIPT_DIR / path).resolve()


def float_triplet(value: Any, name: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise SystemExit(f"{name} must be a three-element JSON array.")
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"{name} must contain numeric values.") from exc


def numeric_list(value: Any, name: str) -> list[float]:
    if not isinstance(value, list) or not value:
        raise SystemExit(f"{name} must be a non-empty JSON array.")
    try:
        values = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"{name} must contain numeric values.") from exc
    if len(set(values)) != len(values):
        raise SystemExit(f"{name} contains duplicate coordinates.")
    return values


def validate_config(config: dict[str, Any], config_path: Path) -> None:
    pat = config.get("pat", {})
    camera = config.get("camera", {})
    tracking = config.get("tracking", {})
    stage_reference = config.get("stage_reference", {})
    if str(stage_reference.get("axis", "")).strip().lower() != "y":
        raise SystemExit("stage_reference.axis must be 'y' for this installation.")
    nominal_hardware = float(stage_reference.get("nominal_hardware_mm", float("nan")))
    if not math.isfinite(nominal_hardware) or nominal_hardware < 0:
        raise SystemExit(
            "stage_reference.nominal_hardware_mm must be a finite non-negative value."
        )
    readback_tolerance = float(
        stage_reference.get("readback_tolerance_mm", float("nan"))
    )
    if not math.isfinite(readback_tolerance) or readback_tolerance <= 0:
        raise SystemExit(
            "stage_reference.readback_tolerance_mm must be finite and positive."
        )
    float_triplet(pat.get("center_mm"), "pat.center_mm")
    float_triplet(
        pat.get("acoustools_zero_in_pat_mm"),
        "pat.acoustools_zero_in_pat_mm",
    )
    x_values = numeric_list(pat.get("x_offsets_mm"), "pat.x_offsets_mm")
    y_values = numeric_list(pat.get("y_offsets_mm"), "pat.y_offsets_mm")
    z_values = numeric_list(pat.get("z_offsets_mm"), "pat.z_offsets_mm")
    if len(x_values) < 2 or len(y_values) < 2 or len(z_values) < 2:
        raise SystemExit("Use at least two coordinates on each axis; a non-coplanar 3D grid is required.")
    if float(pat.get("transfer_step_mm", 0)) <= 0:
        raise SystemExit("pat.transfer_step_mm must be positive.")
    if float(pat.get("transfer_dwell_sec", 0)) < 0:
        raise SystemExit("pat.transfer_dwell_sec must not be negative.")
    if float(pat.get("settle_sec", 0)) < 0:
        raise SystemExit("pat.settle_sec must not be negative.")
    ids = pat.get("controller_ids")
    if not isinstance(ids, list) or not ids:
        raise SystemExit("pat.controller_ids must be a non-empty JSON array.")
    if not str(camera.get("left_serial", "")).strip() or not str(camera.get("right_serial", "")).strip():
        raise SystemExit("camera.left_serial and camera.right_serial are required.")
    if str(camera["left_serial"]) == str(camera["right_serial"]):
        raise SystemExit("Left and right camera serials must differ.")
    calibration = resolve_from_config(str(camera.get("stereo_calibration", "")), config_path)
    if not calibration.exists():
        raise SystemExit(f"Stereo calibration not found: {calibration}")
    expected_square_mm = float(camera.get("expected_square_mm", 0.0))
    with np.load(calibration, allow_pickle=False) as calibration_data:
        if expected_square_mm > 0:
            if "square_size_mm" not in calibration_data.files:
                raise SystemExit(
                    "Stereo calibration has no square_size_mm provenance: "
                    f"{calibration}"
                )
            actual_square_mm = float(np.asarray(calibration_data["square_size_mm"]).reshape(-1)[0])
            if not math.isclose(
                actual_square_mm,
                expected_square_mm,
                rel_tol=0.0,
                abs_tol=1e-9,
            ):
                raise SystemExit(
                    "Stereo calibration square size does not match the PAT config: "
                    f"file={actual_square_mm:.12g} mm, "
                    f"expected={expected_square_mm:.12g} mm. "
                    "The historical 7.1 mm calibration must not be used."
                )
        for side in ("left", "right"):
            expected_serial = str(camera.get(f"{side}_serial", ""))
            key = f"{side}_camera_serial"
            if key in calibration_data.files:
                actual_serial = str(np.asarray(calibration_data[key]).reshape(-1)[0])
                if actual_serial and actual_serial != expected_serial:
                    raise SystemExit(
                        f"Stereo calibration {side} serial={actual_serial!r}, "
                        f"but config requires {expected_serial!r}."
                    )
    if float(camera.get("record_sec", 0)) <= 0:
        raise SystemExit("camera.record_sec must be positive.")
    if int(camera.get("delta_t_us", 0)) <= 0:
        raise SystemExit("camera.delta_t_us must be positive.")
    if int(camera.get("max_capture_attempts", 0)) <= 0:
        raise SystemExit("camera.max_capture_attempts must be positive.")
    if str(camera.get("hw_sync", "")).strip() not in {"left-master", "right-master"}:
        raise SystemExit(
            "camera.hw_sync must be 'left-master' or 'right-master'; "
            "camera-to-PAT registration requires hardware-synchronized stereo capture."
        )
    if float(camera.get("hw_sync_timeout_sec", 0)) <= 0:
        raise SystemExit("camera.hw_sync_timeout_sec must be positive.")
    if str(camera.get("npz_compression", "none")) not in {"none", "compressed"}:
        raise SystemExit("camera.npz_compression must be 'none' or 'compressed'.")
    if int(camera.get("max_events", 0)) < 0:
        raise SystemExit("camera.max_events must not be negative.")
    for key in ("window_us", "hop_us", "dt_us", "threshold_count", "min_events", "min_area", "min_mass"):
        if float(tracking.get(key, -1)) < 0:
            raise SystemExit(f"tracking.{key} must not be negative.")
    acceptance = config.get("registration", {}).get("acceptance", {})
    if int(acceptance.get("minimum_usable_points", 4)) < 4:
        raise SystemExit("registration.acceptance.minimum_usable_points must be at least 4.")
    inlier_fraction = float(acceptance.get("minimum_inlier_fraction", 0.0))
    if not 0.0 <= inlier_fraction <= 1.0:
        raise SystemExit(
            "registration.acceptance.minimum_inlier_fraction must be in [0, 1]."
        )
    for key in (
        "maximum_fit_rms_mm",
        "maximum_fit_max_mm",
        "maximum_validation_rms_mm",
        "maximum_similarity_scale_error",
    ):
        if float(acceptance.get(key, 1.0)) <= 0:
            raise SystemExit(f"registration.acceptance.{key} must be positive.")


def generate_grid(config: dict[str, Any]) -> list[dict[str, Any]]:
    pat = config["pat"]
    center = float_triplet(pat["center_mm"], "pat.center_mm")
    acoustools_zero = float_triplet(
        pat["acoustools_zero_in_pat_mm"],
        "pat.acoustools_zero_in_pat_mm",
    )
    xs = sorted(numeric_list(pat["x_offsets_mm"], "pat.x_offsets_mm"))
    ys = sorted(numeric_list(pat["y_offsets_mm"], "pat.y_offsets_mm"))
    zs = sorted(numeric_list(pat["z_offsets_mm"], "pat.z_offsets_mm"))
    points: list[dict[str, Any]] = []
    order_index = 0
    for z_index, z_offset in enumerate(zs):
        y_order = ys if z_index % 2 == 0 else list(reversed(ys))
        for row_index, y_offset in enumerate(y_order):
            x_order = xs if (z_index + row_index) % 2 == 0 else list(reversed(xs))
            for x_offset in x_order:
                order_index += 1
                target = [
                    center[0] + x_offset,
                    center[1] + y_offset,
                    center[2] + z_offset,
                ]
                target_acoustools_mm = [
                    target[axis] - acoustools_zero[axis]
                    for axis in range(3)
                ]
                points.append(
                    {
                        "point_id": f"p{order_index:03d}",
                        "offset_mm": [x_offset, y_offset, z_offset],
                        "target_pat_mm": target,
                        "target_acoustools_mm": target_acoustools_mm,
                        "status": "pending",
                        "attempts": [],
                        "accepted_run_dir": "",
                    }
                )
    return points


def plan_hash(points: Iterable[dict[str, Any]]) -> str:
    payload = [
        {
            "point_id": point["point_id"],
            "target_pat_mm": [round(float(value), 9) for value in point["target_pat_mm"]],
            "target_acoustools_mm": [
                round(float(value), 9)
                for value in point["target_acoustools_mm"]
            ],
        }
        for point in points
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def create_or_resume_session(
    args: argparse.Namespace,
    config: dict[str, Any],
    config_path: Path,
    planned_points: list[dict[str, Any]],
) -> tuple[Path, Path, dict[str, Any]]:
    if args.session_dir is not None:
        session_dir = args.session_dir.resolve()
    else:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        session_dir = (args.output_root / f"{config.get('name', 'pat_stereo_grid')}_{stamp}").resolve()
    session_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = session_dir / "pat_stereo_grid_manifest.json"
    expected_hash = plan_hash(planned_points)
    if manifest_path.exists():
        manifest = load_json(manifest_path)
        if str(manifest.get("plan_hash", "")) != expected_hash:
            raise SystemExit(
                "Existing session grid differs from the selected configuration. "
                f"Use the original config or a new --session-dir: {session_dir}"
            )
        supplied_readback = args.stage_hardware_readback_mm
        stored_readback = manifest.get("stage_hardware_readback_mm")
        if supplied_readback is not None:
            if not math.isfinite(float(supplied_readback)):
                raise SystemExit("--stage-hardware-readback-mm must be finite.")
            if stored_readback is not None and not math.isclose(
                float(stored_readback),
                float(supplied_readback),
                rel_tol=0.0,
                abs_tol=1e-9,
            ):
                raise SystemExit(
                    "The supplied stage hardware readback differs from the resumed "
                    "session manifest."
                )
            if stored_readback is None:
                manifest["stage_hardware_readback_mm"] = float(supplied_readback)
                atomic_write_json(manifest_path, manifest)
        print(f"[RESUME] session: {session_dir}")
        return session_dir, manifest_path, manifest

    calibration = resolve_from_config(str(config["camera"]["stereo_calibration"]), config_path)
    manifest = {
        "schema_version": 3,
        "session_dir": str(session_dir),
        "name": str(config.get("name", "pat_stereo_grid")),
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "status": "planned" if args.dry_run else "capturing",
        "config_source": str(config_path),
        "stereo_calibration": str(calibration),
        "coordinate_system": "PAT target coordinates in millimetres; config values are converted to metres for AcousTools.",
        "stage_reference": config["stage_reference"],
        "stage_hardware_readback_mm": (
            None
            if args.stage_hardware_readback_mm is None
            else float(args.stage_hardware_readback_mm)
        ),
        "acoustools_mapping": (
            "p_acoustools_m = "
            "(p_pat_mm - acoustools_zero_in_pat_mm) * 1e-3"
        ),
        "plan_hash": expected_hash,
        "points": planned_points,
    }
    atomic_write_json(session_dir / "session_config.json", config)
    atomic_write_json(manifest_path, manifest)
    return session_dir, manifest_path, manifest


def update_manifest(path: Path, manifest: dict[str, Any], status: str | None = None) -> None:
    manifest["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    if status is not None:
        manifest["status"] = status
    atomic_write_json(path, manifest)


def print_plan(
    points: list[dict[str, Any]],
    acoustools_zero_in_pat_mm: Iterable[float],
) -> None:
    zero = [float(value) for value in acoustools_zero_in_pat_mm]
    print(f"Grid points: {len(points)}")
    for point in points:
        x_mm, y_mm, z_mm = point["target_pat_mm"]
        command = [
            float(value)
            for value in point.get(
                "target_acoustools_mm",
                [x_mm - zero[0], y_mm - zero[1], z_mm - zero[2]],
            )
        ]
        print(
            f"  {point['point_id']}: "
            f"PAT=({x_mm:8.3f}, {y_mm:8.3f}, {z_mm:8.3f}) mm  "
            f"AcousTools=({command[0]:+8.3f}, {command[1]:+8.3f}, "
            f"{command[2]:+8.3f}) mm"
        )


def pat_mm_to_acoustools_m(
    position_pat_mm: Iterable[float],
    acoustools_zero_in_pat_mm: Iterable[float],
) -> tuple[float, float, float]:
    position = [float(value) for value in position_pat_mm]
    zero = [float(value) for value in acoustools_zero_in_pat_mm]
    return tuple((position[axis] - zero[axis]) * 1e-3 for axis in range(3))


def move_particle(
    lev: Any,
    start_m: tuple[float, float, float],
    end_m: tuple[float, float, float],
    step_mm: float,
    dwell_sec: float,
) -> tuple[tuple[float, float, float], Any]:
    from acoustools_eventcam_sync import (
        compute_holograms_for_positions,
        generate_smooth_positions,
        mute_sync_transducer,
    )

    distance_m = math.dist(start_m, end_m)
    if distance_m <= 1e-12:
        hologram = mute_sync_transducer(compute_holograms_for_positions([end_m])[0])
        lev.levitate(hologram)
        return end_m, hologram
    steps = max(1, int(math.ceil(distance_m / (float(step_mm) * 1e-3))))
    # Use the same endpoint-excluding-start linear transfer interpolation as
    # acoustools_multitraj_no_eventcam.py / move_static_position().
    positions = generate_smooth_positions(start_m, end_m, steps)
    print(
        f"[PAT] move {distance_m * 1e3:.3f} mm in {steps} steps "
        f"({float(step_mm):.3f} mm max step)"
    )
    holograms = [
        mute_sync_transducer(hologram)
        for hologram in compute_holograms_for_positions(positions)
    ]
    for hologram in holograms:
        lev.levitate(hologram)
        if dwell_sec > 0:
            time.sleep(float(dwell_sec))
    return end_m, holograms[-1]


def run_initial_preview(config: dict[str, Any]) -> bool:
    from stereo_eventcam_record_sync import run_preview

    camera = config["camera"]
    preview = camera.get("preview", {})
    return run_preview(
        left_serial=str(camera["left_serial"]),
        right_serial=str(camera["right_serial"]),
        sensor_width=int(camera.get("sensor_width", 1280)),
        sensor_height=int(camera.get("sensor_height", 720)),
        delta_t_us=int(preview.get("delta_t_us", 5000)),
        display_scale=float(preview.get("scale", 0.5)),
        point_size=int(preview.get("point_size", 1)),
        reopen_wait_sec=float(camera.get("camera_reopen_wait_sec", 1.0)),
    )


def read_capture_counts(run_dir: Path) -> tuple[int, int]:
    counts: list[int] = []
    for side in ("left", "right"):
        meta_path = run_dir / side / f"{side}_recording_meta.json"
        if not meta_path.exists():
            counts.append(0)
            continue
        meta = load_json(meta_path)
        counts.append(int(meta.get("total_events", 0) or 0))
    return counts[0], counts[1]


def build_record_command(config: dict[str, Any], calibration: Path, run_dir: Path, point: dict[str, Any]) -> list[str]:
    camera = config["camera"]
    target = point["target_pat_mm"]
    note = (
        f"PAT registration {point['point_id']}; "
        f"target_pat_mm=[{target[0]:.6f},{target[1]:.6f},{target[2]:.6f}]; "
        "target_acoustools_mm=["
        + ",".join(f"{float(value):.6f}" for value in point["target_acoustools_mm"])
        + "]"
    )
    return [
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
        str(float(camera.get("start_delay_sec", 1.0))),
        "--hw-sync",
        str(camera["hw_sync"]),
        "--hw-sync-timeout-sec",
        str(float(camera["hw_sync_timeout_sec"])),
        "--npz-compression",
        str(camera.get("npz_compression", "none")),
        "--max-events",
        str(int(camera.get("max_events", 0))),
        "--sensor-width",
        str(int(camera.get("sensor_width", 1280))),
        "--sensor-height",
        str(int(camera.get("sensor_height", 720))),
        "--stereo-calibration",
        str(calibration),
        "--note",
        note,
        "--no-preview",
    ]


def process_recording(config: dict[str, Any], calibration: Path, run_dir: Path) -> None:
    tracking = config["tracking"]
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
        str(float(tracking.get("max_time_gap_sec", 0.001))),
    ]
    right_time_offset = tracking.get("right_time_offset_sec")
    if right_time_offset is not None:
        command.extend(["--right-time-offset-sec", str(float(right_time_offset))])
    print("[PROCESS] " + " ".join(command), flush=True)
    subprocess.run(command, check=True)


def capture_point(
    *,
    config: dict[str, Any],
    calibration: Path,
    session_dir: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    point: dict[str, Any],
    process_each: bool,
) -> bool:
    camera = config["camera"]
    attempts_this_run = int(camera["max_capture_attempts"])
    min_events = int(camera.get("minimum_events_per_camera", 1))
    existing_attempts = len(point.get("attempts", []))
    last_attempt = existing_attempts + attempts_this_run
    point_root = session_dir / "captures" / str(point["point_id"])
    point_root.mkdir(parents=True, exist_ok=True)

    for attempt_index in range(existing_attempts + 1, last_attempt + 1):
        attempt_dir = point_root / f"attempt_{attempt_index:02d}"
        command = build_record_command(config, calibration, attempt_dir, point)
        local_attempt = attempt_index - existing_attempts
        print(
            f"[CAPTURE] {point['point_id']} attempt {local_attempt}/{attempts_this_run} "
            f"(session attempt {attempt_index})",
            flush=True,
        )
        started_at = dt.datetime.now().isoformat(timespec="seconds")
        completed = subprocess.run(command, check=False)
        left_events, right_events = read_capture_counts(attempt_dir)
        accepted = completed.returncode == 0 and left_events >= min_events and right_events >= min_events
        attempt = {
            "attempt": attempt_index,
            "run_dir": str(attempt_dir.resolve()),
            "started_at": started_at,
            "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
            "return_code": int(completed.returncode),
            "left_events": int(left_events),
            "right_events": int(right_events),
            "accepted": bool(accepted),
        }
        point.setdefault("attempts", []).append(attempt)
        if accepted:
            point["status"] = "captured"
            point["accepted_run_dir"] = str(attempt_dir.resolve())
            update_manifest(manifest_path, manifest)
            print(
                f"[CAPTURE][OK] {point['point_id']} "
                f"left={left_events:,} right={right_events:,}"
            )
            if process_each:
                process_recording(config, calibration, attempt_dir)
                point["status"] = "processed"
                update_manifest(manifest_path, manifest)
            return True

        point["status"] = "retry_pending" if attempt_index < last_attempt else "failed"
        update_manifest(manifest_path, manifest)
        print(
            f"[CAPTURE][FAIL] {point['point_id']} return={completed.returncode} "
            f"left={left_events:,} right={right_events:,}"
        )
        wait_sec = float(camera.get("camera_reopen_wait_sec", 1.0))
        if wait_sec > 0:
            time.sleep(wait_sec)
    return False


def turn_off_pat(lev: Any, current_hologram: Any) -> None:
    if lev is None or current_hologram is None:
        return
    try:
        import torch
        from acoustools.Utilities import add_lev_sig
        from acoustools_eventcam_sync import prepare_message_from_holograms

        off_phase = add_lev_sig(torch.zeros_like(current_hologram))
        phases, amplitudes, _ = prepare_message_from_holograms(lev, [off_phase], permute=True)
        lev.send_message(phases, amplitudes, 0, 1, sleep_ms=0, loop=False, num_loops=1)
    except Exception as exc:
        print(f"[PAT][WARN] could not send the final off frame: {exc}")


def main() -> int:
    args = parse_args()
    config_path = args.config.resolve()
    config = load_json(config_path)
    validate_config(config, config_path)
    planned_points = generate_grid(config)
    session_dir, manifest_path, manifest = create_or_resume_session(
        args,
        config,
        config_path,
        planned_points,
    )
    print_plan(
        manifest["points"],
        config["pat"]["acoustools_zero_in_pat_mm"],
    )
    print(f"Session: {session_dir}")
    if args.dry_run:
        update_manifest(manifest_path, manifest, status="planned")
        print("[DRY RUN] Hardware was not opened.")
        return 0
    if manifest.get("stage_hardware_readback_mm") is None:
        raise SystemExit(
            "Actual PAT registration requires --stage-hardware-readback-mm. "
            "Read the Ossila absolute position at the fixed registration station "
            "and resume this planned session with that numeric value."
        )
    nominal_stage_hardware = float(
        config["stage_reference"]["nominal_hardware_mm"]
    )
    actual_stage_hardware = float(manifest["stage_hardware_readback_mm"])
    stage_readback_tolerance = float(
        config["stage_reference"]["readback_tolerance_mm"]
    )
    if abs(actual_stage_hardware - nominal_stage_hardware) > stage_readback_tolerance:
        raise SystemExit(
            "PAT registration stage readback differs from the configured nominal "
            f"position: actual={actual_stage_hardware:.6f} mm, "
            f"nominal={nominal_stage_hardware:.6f} mm, "
            f"tolerance={stage_readback_tolerance:.6f} mm."
        )

    calibration = Path(manifest["stereo_calibration"]).resolve()
    pat = config["pat"]
    camera = config["camera"]
    acoustools_zero = pat["acoustools_zero_in_pat_mm"]
    center_m = pat_mm_to_acoustools_m(pat["center_mm"], acoustools_zero)
    current_m = center_m
    current_hologram: Any = None
    lev: Any = None
    interrupted = False

    try:
        from acoustools.Levitator import LevitatorController
        from acoustools_eventcam_sync import compute_holograms_for_positions, mute_sync_transducer

        controller_ids = tuple(int(value) for value in pat["controller_ids"])
        lev = LevitatorController(ids=controller_ids)
        print(f"[PAT] connected: ids={controller_ids}")
        current_hologram = mute_sync_transducer(compute_holograms_for_positions([center_m])[0])
        lev.levitate(current_hologram)
        time.sleep(max(0.2, float(pat.get("settle_sec", 0.8))))

        preview_enabled = bool(camera.get("preview", {}).get("enabled", True))
        if preview_enabled and not args.no_preview:
            if not run_initial_preview(config):
                update_manifest(manifest_path, manifest, status="preview_aborted")
                print("[PREVIEW] aborted; no grid points were captured.")
                return 1

        pending = [
            point
            for point in manifest["points"]
            if point.get("status") not in {"captured", "processed"}
        ]
        print(f"Pending points: {len(pending)}/{len(manifest['points'])}")
        if not pending:
            update_manifest(manifest_path, manifest, status="captured")
            print("All grid points are already captured.")
            return 0
        if not args.yes:
            answer = input("Press Enter to start the grid. Type q then Enter to abort: ").strip().lower()
            if answer == "q":
                update_manifest(manifest_path, manifest, status="user_aborted")
                return 1

        update_manifest(manifest_path, manifest, status="capturing")
        for point in pending:
            target_m = pat_mm_to_acoustools_m(
                point["target_pat_mm"],
                acoustools_zero,
            )
            current_m, current_hologram = move_particle(
                lev,
                current_m,
                target_m,
                float(pat["transfer_step_mm"]),
                float(pat["transfer_dwell_sec"]),
            )
            settle_sec = float(pat.get("settle_sec", 0.8))
            if settle_sec > 0:
                print(f"[PAT] settle {settle_sec:.3f} s")
                time.sleep(settle_sec)
            point["arrived_at"] = dt.datetime.now().isoformat(timespec="seconds")
            update_manifest(manifest_path, manifest)
            ok = capture_point(
                config=config,
                calibration=calibration,
                session_dir=session_dir,
                manifest_path=manifest_path,
                manifest=manifest,
                point=point,
                process_each=bool(args.process_each),
            )
            if not ok and not args.continue_on_error:
                update_manifest(manifest_path, manifest, status="capture_failed")
                print("Capture stopped. Re-run with --session-dir to resume.")
                return 2

        completed = sum(
            point.get("status") in {"captured", "processed"}
            for point in manifest["points"]
        )
        final_status = "captured" if completed == len(manifest["points"]) else "captured_partial"
        update_manifest(manifest_path, manifest, status=final_status)
        print(f"[DONE] captured {completed}/{len(manifest['points'])} points")
        print(
            "Next: "
            f"{sys.executable} {SCRIPT_DIR / 'pat_stereo_grid_process.py'} {session_dir}"
        )
        return 0 if completed == len(manifest["points"]) else 2
    except KeyboardInterrupt:
        interrupted = True
        update_manifest(manifest_path, manifest, status="interrupted")
        print("\n[STOP] interrupted; the session can be resumed.")
        return 130
    finally:
        if lev is not None and bool(pat.get("return_to_center", True)) and current_m != center_m:
            try:
                current_m, current_hologram = move_particle(
                    lev,
                    current_m,
                    center_m,
                    float(pat["transfer_step_mm"]),
                    float(pat["transfer_dwell_sec"]),
                )
                print("[PAT] returned to configured centre.")
            except Exception as exc:
                print(f"[PAT][WARN] could not return to centre: {exc}")
        turn_off_pat(lev, current_hologram)
        if interrupted:
            print(f"Resume: {sys.executable} {Path(__file__).name} --session-dir {session_dir}")


if __name__ == "__main__":
    if os.name == "nt":
        import multiprocessing as mp

        mp.freeze_support()
    raise SystemExit(main())
