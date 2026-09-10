#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared planning, geometry, and statistics for the stereo stage experiment.

The experiment uses two independent motions:

* an Ossila stage translates the apparatus along one configured laboratory axis;
* the PAT moves one particle to the centre and the eight vertices of a cuboid.

Stereo triangulation is produced in the left OpenCV camera frame.  Final
evaluation can either use the historical reference-relative camera frame or a
previously registered PAT frame.  In PAT mode the camera-to-PAT translation is
updated at every station from the Ossila readback because the camera moves.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np


SCRIPT_DIR = Path(__file__).resolve().parent


def ensure_finite_json(value: Any, path: str = "config") -> None:
    """Reject non-standard JSON NaN/Infinity values before safety comparisons."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise SystemExit(f"{path} must be finite, got {value!r}.")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            ensure_finite_json(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            ensure_finite_json(item, f"{path}.{key}")
        return
    raise SystemExit(f"{path} contains unsupported JSON value {type(value).__name__}.")


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Configuration not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"Configuration root must be a JSON object: {path}")
    ensure_finite_json(payload)
    return payload


def resolve_from_config(path_text: str, config_path: Path) -> Path:
    """Resolve a path first against the config directory, then this source tree."""
    path = Path(str(path_text))
    if path.is_absolute():
        return path.resolve()
    config_relative = (config_path.parent / path).resolve()
    if config_relative.exists():
        return config_relative
    return (SCRIPT_DIR / path).resolve()


def numeric_triplet(value: Any, name: str) -> np.ndarray:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise SystemExit(f"{name} must be a three-element array.")
    if any(
        isinstance(item, bool) or not isinstance(item, (int, float))
        for item in value
    ):
        raise SystemExit(f"{name} must contain JSON numbers.")
    try:
        result = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"{name} must contain numeric values.") from exc
    if not np.all(np.isfinite(result)):
        raise SystemExit(f"{name} contains a non-finite value.")
    return result


def numeric_list(value: Any, name: str) -> list[float]:
    if not isinstance(value, list) or not value:
        raise SystemExit(f"{name} must be a non-empty JSON array.")
    if any(
        isinstance(item, bool) or not isinstance(item, (int, float))
        for item in value
    ):
        raise SystemExit(f"{name} must contain JSON numbers.")
    try:
        values = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"{name} must contain numeric values.") from exc
    if not all(math.isfinite(item) for item in values):
        raise SystemExit(f"{name} contains a non-finite value.")
    if len(set(values)) != len(values):
        raise SystemExit(f"{name} contains duplicate values.")
    return values


def strict_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise SystemExit(f"{name} must be a JSON boolean (true or false).")
    return value


def strict_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SystemExit(f"{name} must be a JSON number.")
    result = float(value)
    if not math.isfinite(result):
        raise SystemExit(f"{name} must be finite.")
    return result


def strict_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SystemExit(f"{name} must be a JSON integer.")
    return int(value)


def experiment_motion_vectors(
    config: dict[str, Any],
) -> tuple[str, np.ndarray, np.ndarray]:
    """Return moving body, its physical PAT-frame motion, and target-apparent motion."""
    experiment = config["experiment"]
    moving_body = str(experiment.get("moving_body", "target")).strip().lower()
    if moving_body not in {"camera", "target"}:
        raise SystemExit("experiment.moving_body must be 'camera' or 'target'.")
    if "moving_body_translation_per_global_mm_in_pat" in experiment:
        physical_vector = numeric_triplet(
            experiment["moving_body_translation_per_global_mm_in_pat"],
            "experiment.moving_body_translation_per_global_mm_in_pat",
        )
    else:
        # Backward compatibility: the former field described apparent target
        # translation and therefore implied a stationary camera.
        physical_vector = numeric_triplet(
            experiment.get("stage_translation_per_global_mm_in_pat"),
            "experiment.stage_translation_per_global_mm_in_pat",
        )
        moving_body = "target"
    if np.linalg.norm(physical_vector) <= 1e-12:
        raise SystemExit(
            "experiment.moving_body_translation_per_global_mm_in_pat must be non-zero."
        )
    apparent_target_vector = (
        -physical_vector if moving_body == "camera" else physical_vector.copy()
    )
    return moving_body, physical_vector, apparent_target_vector


def parse_stage_config_md(path: Path) -> dict[str, dict[str, Any]]:
    """Parse the small Ossila sample config without importing pyserial."""
    axes: dict[str, dict[str, Any]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise SystemExit(f"Ossila stage configuration not found: {path}") from exc
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 2:
            raise SystemExit(f"Invalid Ossila config line {line_number}: {line!r}")
        axis = parts[0].lower()
        if axis not in {"x", "y", "z"}:
            raise SystemExit(f"Unsupported Ossila axis on line {line_number}: {parts[0]!r}")
        serial_number = parts[1]
        if serial_number == "-":
            continue
        try:
            direction = int(parts[2]) if len(parts) >= 3 else 1
        except ValueError as exc:
            raise SystemExit(f"Invalid direction on Ossila config line {line_number}.") from exc
        if direction not in {-1, 1}:
            raise SystemExit(f"Ossila direction must be +1 or -1 on line {line_number}.")
        axes[axis] = {"serial_number": serial_number, "direction": direction}
    return axes


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_camera_to_pat_transform_json(path: Path) -> dict[str, Any]:
    """Load and validate a camera-to-PAT registration summary JSON."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Camera-to-PAT transform not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid camera-to-PAT transform JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"Camera-to-PAT transform root must be an object: {path}")
    required = {
        "R_camera_to_pat",
        "t_camera_to_pat_mm",
        "stereo_calibration_sha256",
        "stage_hardware_readback_mm",
        "quality",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise SystemExit(
            f"Camera-to-PAT transform JSON is missing keys {missing}: {path}"
        )
    rotation = np.asarray(payload["R_camera_to_pat"], dtype=np.float64)
    translation = np.asarray(payload["t_camera_to_pat_mm"], dtype=np.float64).reshape(-1)
    if rotation.shape != (3, 3) or translation.shape != (3,):
        raise SystemExit(
            "Camera-to-PAT transform must contain a 3x3 rotation and "
            "three-element translation."
        )
    if not np.all(np.isfinite(rotation)) or not np.all(np.isfinite(translation)):
        raise SystemExit("Camera-to-PAT rotation/translation contains non-finite values.")
    orthogonality_error = float(
        np.linalg.norm(rotation.T @ rotation - np.eye(3), ord="fro")
    )
    determinant = float(np.linalg.det(rotation))
    if orthogonality_error > 1e-6 or not math.isclose(
        determinant,
        1.0,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        raise SystemExit(
            "Camera-to-PAT transform rotation is not a proper rigid rotation: "
            f"orthogonality_error={orthogonality_error:.6g}, det={determinant:.6g}."
        )
    quality = payload["quality"]
    if not isinstance(quality, dict) or not bool(quality.get("pass", False)):
        failures = quality.get("failures", []) if isinstance(quality, dict) else []
        raise SystemExit(
            "Camera-to-PAT registration did not pass its quality gate: "
            + ("; ".join(str(item) for item in failures) or "quality.pass is false")
        )
    stage_readback = payload["stage_hardware_readback_mm"]
    if isinstance(stage_readback, bool) or not isinstance(stage_readback, (int, float)):
        raise SystemExit(
            "Camera-to-PAT stage_hardware_readback_mm must be a JSON number."
        )
    stage_readback_value = float(stage_readback)
    if not math.isfinite(stage_readback_value):
        raise SystemExit(
            "Camera-to-PAT stage_hardware_readback_mm must be finite."
        )
    calibration_hash = str(payload["stereo_calibration_sha256"]).strip().lower()
    if len(calibration_hash) != 64 or any(
        character not in "0123456789abcdef" for character in calibration_hash
    ):
        raise SystemExit(
            "Camera-to-PAT stereo_calibration_sha256 is not a SHA-256 hex digest."
        )
    return {
        **payload,
        "R_camera_to_pat": rotation,
        "t_camera_to_pat_mm": translation,
        "stage_hardware_readback_mm": stage_readback_value,
        "stereo_calibration_sha256": calibration_hash,
        "transform_path": str(path.resolve()),
        "transform_sha256": file_sha256(path),
        "rotation_orthogonality_error": orthogonality_error,
        "rotation_determinant": determinant,
    }


def canonical_json_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def configuration_fingerprint(
    config: dict[str, Any],
    resolved: dict[str, Any],
) -> dict[str, Any]:
    stage_source = Path(resolved["sample_dir"]) / "ossila_stage" / "stage.py"
    capture_sources = {
        "stage_accuracy_common_source_sha256": Path(__file__).resolve(),
        "stage_accuracy_capture_source_sha256": SCRIPT_DIR
        / "stereo_stage_accuracy_capture.py",
        "stereo_recorder_source_sha256": SCRIPT_DIR
        / "stereo_eventcam_record_sync.py",
        "pat_grid_capture_source_sha256": SCRIPT_DIR / "pat_stereo_grid_capture.py",
    }
    fingerprint = {
        "config_sha256": canonical_json_sha256(config),
        "stage_config_sha256": file_sha256(Path(resolved["stage_config"])),
        "stereo_calibration_sha256": file_sha256(Path(resolved["stereo_calibration"])),
        "ossila_stage_source_sha256": file_sha256(stage_source),
        **{
            key: file_sha256(path)
            for key, path in capture_sources.items()
        },
        "stage_axis": str(resolved["stage_axis"]),
        "stage_usb_serial": str(resolved["stage_serial"]),
        "stage_direction": int(resolved["stage_direction"]),
        "stage_datum_mm": float(config["stage"]["datum_mm"]),
        "stage_hardware_min_mm": float(config["stage"]["hardware_min_mm"]),
        "stage_hardware_max_mm": float(config["stage"]["hardware_max_mm"]),
        "stage_speed_mm_s": float(config["stage"]["speed_mm_s"]),
        "camera_left_serial": str(config["camera"]["left_serial"]),
        "camera_right_serial": str(config["camera"]["right_serial"]),
        "camera_hw_sync": str(config["camera"]["hw_sync"]),
        "target_mode": str(config["target"]["mode"]),
        "target_controller_ids": list(config["target"].get("controller_ids", [])),
        "evaluation_frame": str(resolved.get("evaluation_frame", "camera_relative")),
    }
    transform_path = resolved.get("camera_to_pat_transform")
    transform_data = resolved.get("camera_to_pat_data")
    if transform_path is not None and transform_data is not None:
        fingerprint.update(
            {
                "camera_to_pat_transform_sha256": file_sha256(Path(transform_path)),
                "camera_to_pat_stereo_calibration_sha256": str(
                    transform_data["stereo_calibration_sha256"]
                ),
                "camera_to_pat_stage_hardware_readback_mm": float(
                    transform_data["stage_hardware_readback_mm"]
                ),
            }
        )
    return fingerprint


def processing_fingerprint(
    config: dict[str, Any],
    calibration_path: Path,
) -> str:
    """Hash every input that can change tracking/triangulation output."""
    processing_script = SCRIPT_DIR / "stereo_process_recording.py"
    payload = {
        "tracking": config["tracking"],
        "calibration_sha256": file_sha256(calibration_path),
        "processing_script_sha256": file_sha256(processing_script),
    }
    return canonical_json_sha256(payload)


def gray_cube_vertices() -> list[tuple[int, int, int]]:
    """A cyclic Gray-code order: consecutive vertices differ on one axis."""
    return [
        (-1, -1, -1),
        (+1, -1, -1),
        (+1, +1, -1),
        (-1, +1, -1),
        (-1, +1, +1),
        (+1, +1, +1),
        (+1, -1, +1),
        (-1, -1, +1),
    ]


def vertex_id(signs: Iterable[int]) -> str:
    return "v" + "".join("+" if int(value) > 0 else "-" for value in signs)


def validate_config(config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    """Validate configuration and return resolved paths/direction metadata."""
    stage = config.get("stage", {})
    target = config.get("target", {})
    experiment = config.get("experiment", {})
    camera = config.get("camera", {})
    tracking = config.get("tracking", {})
    analysis = config.get("analysis", {})

    stage_config = resolve_from_config(str(stage.get("config", "")), config_path)
    sample_dir = resolve_from_config(str(stage.get("sample_dir", "")), config_path)
    axes = parse_stage_config_md(stage_config)
    axis = str(stage.get("axis", "")).strip().lower()
    if axis not in axes:
        raise SystemExit(
            f"stage.axis={axis!r} is not an enabled axis in {stage_config}. "
            f"Enabled axes: {sorted(axes)}"
        )
    datum = strict_number(stage.get("datum_mm", 100.0), "stage.datum_mm")
    hardware_min = strict_number(
        stage.get("hardware_min_mm", 0.0),
        "stage.hardware_min_mm",
    )
    hardware_max = strict_number(
        stage.get("hardware_max_mm", 200.0),
        "stage.hardware_max_mm",
    )
    if hardware_min >= hardware_max:
        raise SystemExit("stage.hardware_min_mm must be below stage.hardware_max_mm.")
    positions = numeric_list(stage.get("positions_global_mm"), "stage.positions_global_mm")
    reference = strict_number(
        stage.get("reference_global_mm", 0.0),
        "stage.reference_global_mm",
    )
    if not any(math.isclose(value, reference, abs_tol=1e-9) for value in positions):
        raise SystemExit("stage.reference_global_mm must be included in stage.positions_global_mm.")
    analysis_reference = strict_number(
        analysis.get("reference_stage_global_mm", reference),
        "analysis.reference_stage_global_mm",
    )
    if not math.isclose(analysis_reference, reference, rel_tol=0.0, abs_tol=1e-9):
        raise SystemExit(
            "analysis.reference_stage_global_mm must equal "
            "stage.reference_global_mm. A different capture and analysis reference "
            "would invalidate the registration."
        )
    direction = int(axes[axis]["direction"])
    raw_targets = [datum + direction * value for value in positions]
    outside = [
        (global_mm, raw_mm)
        for global_mm, raw_mm in zip(positions, raw_targets)
        if raw_mm < hardware_min - 1e-9 or raw_mm > hardware_max + 1e-9
    ]
    if outside:
        details = ", ".join(f"global {g:.3f} -> hardware {h:.3f}" for g, h in outside)
        raise SystemExit(
            "Stage plan exceeds the configured hardware soft limits "
            f"[{hardware_min:.3f}, {hardware_max:.3f}] mm: {details}"
        )
    if not sample_dir.is_dir():
        raise SystemExit(f"Ossila sample directory not found: {sample_dir}")
    if strict_number(stage.get("settle_sec", 0.0), "stage.settle_sec") < 0:
        raise SystemExit("stage.settle_sec must not be negative.")
    if strict_number(
        stage.get("readback_tolerance_mm", 0.0),
        "stage.readback_tolerance_mm",
    ) <= 0:
        raise SystemExit("stage.readback_tolerance_mm must be positive.")
    if strict_number(
        stage.get("motion_timeout_sec", 0.0),
        "stage.motion_timeout_sec",
    ) <= 0:
        raise SystemExit("stage.motion_timeout_sec must be positive.")
    speed = stage.get("speed_mm_s")
    if speed is None or strict_number(speed, "stage.speed_mm_s") <= 0:
        raise SystemExit(
            "stage.speed_mm_s must be an explicit positive value; null would use maximum speed."
        )
    for key in (
        "expected_acceleration_mm_s2",
        "expected_deceleration_mm_s2",
    ):
        value = stage.get(key)
        if value is not None and strict_number(value, f"stage.{key}") <= 0:
            raise SystemExit(f"stage.{key} must be positive when configured.")
    if strict_number(
        stage.get("settings_match_tolerance_mm_s2", 0.0),
        "stage.settings_match_tolerance_mm_s2",
    ) <= 0:
        raise SystemExit("stage.settings_match_tolerance_mm_s2 must be positive.")
    expected_travel = strict_number(
        stage.get("expected_travel_mm", 0.0),
        "stage.expected_travel_mm",
    )
    if expected_travel <= 0:
        raise SystemExit("stage.expected_travel_mm must be positive.")
    if hardware_min < 0 or hardware_max > expected_travel:
        raise SystemExit(
            "Stage soft limits must remain within [0, stage.expected_travel_mm]."
        )
    if strict_number(
        stage.get("command_timeout_sec", 0.0),
        "stage.command_timeout_sec",
    ) <= 0:
        raise SystemExit("stage.command_timeout_sec must be positive.")
    if strict_number(
        stage.get("home_readback_tolerance_mm", 0.0),
        "stage.home_readback_tolerance_mm",
    ) <= 0:
        raise SystemExit("stage.home_readback_tolerance_mm must be positive.")
    if strict_number(stage.get("status_poll_sec", 0.0), "stage.status_poll_sec") <= 0:
        raise SystemExit("stage.status_poll_sec must be positive.")
    strict_bool(
        stage.get("return_to_reference_on_success"),
        "stage.return_to_reference_on_success",
    )
    strict_bool(stage.get("require_clear_alarms"), "stage.require_clear_alarms")

    distance_reporting = config.get("distance_reporting", {})
    intended_reference_hardware = strict_number(
        distance_reporting.get("intended_reference_hardware_mm", datum + direction * reference),
        "distance_reporting.intended_reference_hardware_mm",
    )
    intended_sweep = numeric_list(
        distance_reporting.get(
            "intended_sweep_hardware_mm",
            [intended_reference_hardware],
        ),
        "distance_reporting.intended_sweep_hardware_mm",
    )
    if any(value < 0 or value > expected_travel for value in intended_sweep):
        raise SystemExit(
            "distance_reporting.intended_sweep_hardware_mm must remain within "
            "[0, stage.expected_travel_mm]."
        )
    if not any(
        math.isclose(value, intended_reference_hardware, rel_tol=0.0, abs_tol=1e-9)
        for value in intended_sweep
    ):
        raise SystemExit(
            "distance_reporting.intended_reference_hardware_mm must be included in "
            "distance_reporting.intended_sweep_hardware_mm."
        )
    independent_reference = distance_reporting.get("independent_absolute_reference", {})
    independent_available = strict_bool(
        independent_reference.get("available", False),
        "distance_reporting.independent_absolute_reference.available",
    )
    if independent_available:
        raise SystemExit(
            "A scalar independent distance is not yet sufficient for full-range "
            "absolute 3D truth. Keep independent_absolute_reference.available=false "
            "until an independently surveyed reference point and stage axis are supplied."
        )

    mode = str(target.get("mode", "pat")).strip().lower()
    if mode not in {"pat", "manual"}:
        raise SystemExit("target.mode must be 'pat' or 'manual'.")
    numeric_triplet(target.get("pat_center_mm"), "target.pat_center_mm")
    numeric_triplet(
        target.get("acoustools_zero_in_pat_mm"),
        "target.acoustools_zero_in_pat_mm",
    )
    half_extent = numeric_triplet(target.get("cube_half_extent_mm"), "target.cube_half_extent_mm")
    if np.any(half_extent <= 0):
        raise SystemExit("target.cube_half_extent_mm values must be positive.")
    if mode == "pat":
        ids = target.get("controller_ids")
        if not isinstance(ids, list) or not ids:
            raise SystemExit("target.controller_ids must be a non-empty list in PAT mode.")
        for index, controller_id in enumerate(ids):
            strict_integer(controller_id, f"target.controller_ids[{index}]")
        if strict_number(
            target.get("transfer_step_mm", 0.0),
            "target.transfer_step_mm",
        ) <= 0:
            raise SystemExit("target.transfer_step_mm must be positive.")
        if strict_number(
            target.get("transfer_dwell_sec", 0.0),
            "target.transfer_dwell_sec",
        ) < 0:
            raise SystemExit("target.transfer_dwell_sec must not be negative.")
    if strict_number(target.get("settle_sec", 0.0), "target.settle_sec") < 0:
        raise SystemExit("target.settle_sec must not be negative.")
    strict_bool(target.get("return_to_center"), "target.return_to_center")

    experiment_motion_vectors(config)
    scan_order = str(experiment.get("scan_order", "ascending_global")).strip().lower()
    if scan_order not in {"ascending_global", "configured"}:
        raise SystemExit(
            "experiment.scan_order must be 'ascending_global' or 'configured'."
        )
    if scan_order == "configured" and not math.isclose(
        positions[0],
        reference,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise SystemExit(
            "With experiment.scan_order='configured', the first "
            "stage.positions_global_mm value must be the reference station."
        )
    scan_cycles = strict_integer(
        experiment.get("scan_cycles", 1),
        "experiment.scan_cycles",
    )
    if scan_cycles < 1:
        raise SystemExit("experiment.scan_cycles must be at least 1.")
    strict_bool(
        experiment.get("center_before_and_after"),
        "experiment.center_before_and_after",
    )
    strict_bool(
        experiment.get("dedicated_registration_pass"),
        "experiment.dedicated_registration_pass",
    )
    cube_positions = experiment.get("cube_positions_global_mm", "all")
    if cube_positions != "all":
        requested = numeric_list(cube_positions, "experiment.cube_positions_global_mm")
        unknown = [
            value
            for value in requested
            if not any(math.isclose(value, known, abs_tol=1e-9) for known in positions)
        ]
        if unknown:
            raise SystemExit(f"Cube positions are not stage stations: {unknown}")

    left_serial = str(camera.get("left_serial", "")).strip()
    right_serial = str(camera.get("right_serial", "")).strip()
    if not left_serial or not right_serial or left_serial == right_serial:
        raise SystemExit("camera.left_serial and camera.right_serial must be distinct non-empty values.")
    calibration = resolve_from_config(str(camera.get("stereo_calibration", "")), config_path)
    if not calibration.exists():
        raise SystemExit(f"Stereo calibration not found: {calibration}")
    expected_square_mm = strict_number(
        camera.get("expected_square_mm", 0.0),
        "camera.expected_square_mm",
    )
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
                    "Stereo calibration square size does not match the stage config: "
                    f"file={actual_square_mm:.12g} mm, "
                    f"expected={expected_square_mm:.12g} mm. "
                    "The historical 7.1 mm calibration must not be used."
                )
        for side, expected_serial in (
            ("left", left_serial),
            ("right", right_serial),
        ):
            key = f"{side}_camera_serial"
            if key in calibration_data.files:
                actual_serial = str(np.asarray(calibration_data[key]).reshape(-1)[0])
                if actual_serial and actual_serial != expected_serial:
                    raise SystemExit(
                        f"Stereo calibration {side} serial={actual_serial!r}, "
                        f"but stage config requires {expected_serial!r}."
                    )
    evaluation_frame = str(
        analysis.get("evaluation_frame", "camera_relative")
    ).strip().lower()
    if evaluation_frame not in {"pat", "camera_relative"}:
        raise SystemExit(
            "analysis.evaluation_frame must be 'pat' or 'camera_relative'."
        )
    camera_to_pat_transform: Path | None = None
    camera_to_pat_data: dict[str, Any] | None = None
    if evaluation_frame == "pat":
        camera_to_pat_transform = resolve_from_config(
            str(analysis.get("camera_to_pat_transform", "")),
            config_path,
        )
        camera_to_pat_data = load_camera_to_pat_transform_json(
            camera_to_pat_transform
        )
        calibration_hash = file_sha256(calibration)
        if str(camera_to_pat_data["stereo_calibration_sha256"]) != calibration_hash:
            raise SystemExit(
                "Camera-to-PAT registration and stage experiment use different "
                "stereo calibration files (SHA-256 mismatch)."
            )
        reference_hardware = datum + direction * reference
        transform_stage_tolerance = strict_number(
            analysis.get("transform_stage_readback_tolerance_mm", 0.0),
            "analysis.transform_stage_readback_tolerance_mm",
        )
        if transform_stage_tolerance <= 0:
            raise SystemExit(
                "analysis.transform_stage_readback_tolerance_mm must be positive."
            )
        if abs(
            float(camera_to_pat_data["stage_hardware_readback_mm"])
            - reference_hardware
        ) > transform_stage_tolerance:
            raise SystemExit(
                "Camera-to-PAT transform was registered at a different Ossila "
                "hardware position: "
                f"transform={float(camera_to_pat_data['stage_hardware_readback_mm']):.6f} mm, "
                f"stage reference={reference_hardware:.6f} mm, "
                f"tolerance={transform_stage_tolerance:.6f} mm."
            )
        for transform_key, target_key, label in (
            ("pat_center_mm", "pat_center_mm", "PAT center"),
            (
                "acoustools_zero_in_pat_mm",
                "acoustools_zero_in_pat_mm",
                "AcousTools zero",
            ),
        ):
            if transform_key in camera_to_pat_data:
                transform_value = numeric_triplet(
                    camera_to_pat_data[transform_key],
                    f"camera_to_pat_transform.{transform_key}",
                )
                target_value = numeric_triplet(
                    target[target_key],
                    f"target.{target_key}",
                )
                if not np.allclose(
                    transform_value,
                    target_value,
                    rtol=0.0,
                    atol=1e-9,
                ):
                    raise SystemExit(
                        f"Camera-to-PAT {label} differs from the stage target config."
                    )
        for transform_key, expected_serial, label in (
            ("left_camera_serial", left_serial, "left"),
            ("right_camera_serial", right_serial, "right"),
        ):
            if (
                transform_key in camera_to_pat_data
                and str(camera_to_pat_data[transform_key]) != expected_serial
            ):
                raise SystemExit(
                    f"Camera-to-PAT {label} camera serial "
                    f"{camera_to_pat_data[transform_key]!r} differs from the "
                    f"stage config serial {expected_serial!r}."
                )
    if strict_number(camera.get("record_sec", 0.0), "camera.record_sec") <= 0:
        raise SystemExit("camera.record_sec must be positive.")
    if strict_integer(camera.get("delta_t_us", 0), "camera.delta_t_us") <= 0:
        raise SystemExit("camera.delta_t_us must be positive.")
    if strict_integer(
        camera.get("max_capture_attempts", 0),
        "camera.max_capture_attempts",
    ) < 1:
        raise SystemExit("camera.max_capture_attempts must be at least 1.")
    if str(camera.get("hw_sync", "left-master")) not in {"off", "left-master", "right-master"}:
        raise SystemExit("camera.hw_sync must be off, left-master, or right-master.")
    if str(camera.get("npz_compression", "none")) not in {"none", "compressed"}:
        raise SystemExit("camera.npz_compression must be 'none' or 'compressed'.")
    strict_bool(
        camera.get("preview", {}).get("enabled"),
        "camera.preview.enabled",
    )
    for key in ("window_us", "hop_us", "dt_us", "threshold_count", "min_events", "min_area", "min_mass"):
        if strict_number(tracking.get(key, -1), f"tracking.{key}") < 0:
            raise SystemExit(f"tracking.{key} must not be negative.")
    if strict_integer(
        analysis.get("minimum_valid_samples", 0),
        "analysis.minimum_valid_samples",
    ) < 1:
        raise SystemExit("analysis.minimum_valid_samples must be at least 1.")
    minimum_sample_valid_fraction = strict_number(
        analysis.get("minimum_sample_valid_fraction", -1),
        "analysis.minimum_sample_valid_fraction",
    )
    if not 0 <= minimum_sample_valid_fraction <= 1:
        raise SystemExit("analysis.minimum_sample_valid_fraction must be in [0, 1].")
    registration_pass_name = str(analysis.get("registration_pass_name", "")).strip()
    if not registration_pass_name:
        raise SystemExit("analysis.registration_pass_name must be non-empty.")
    evaluation_pass_names = {
        f"cycle{cycle_index + 1:02d}_{direction}"
        for cycle_index in range(scan_cycles)
        for direction in ("forward", "reverse")
    }
    if registration_pass_name in evaluation_pass_names:
        raise SystemExit(
            "analysis.registration_pass_name collides with an evaluation pass name: "
            f"{registration_pass_name!r}."
        )
    for key in (
        "trim_start_sec",
        "trim_end_sec",
        "sample_outlier_floor_mm",
        "sample_outlier_mad_factor",
    ):
        if strict_number(analysis.get(key, -1), f"analysis.{key}") < 0:
            raise SystemExit(f"analysis.{key} must not be negative.")
    if strict_number(
        analysis.get("theory_coverage_sigma", 0),
        "analysis.theory_coverage_sigma",
    ) <= 0:
        raise SystemExit("analysis.theory_coverage_sigma must be positive.")
    pixel_sigmas = numeric_list(
        analysis.get("pixel_sigma_scenarios"),
        "analysis.pixel_sigma_scenarios",
    )
    if any(value <= 0 for value in pixel_sigmas):
        raise SystemExit("analysis.pixel_sigma_scenarios values must be positive.")
    for key in (
        "minimum_vertex_fraction",
        "max_abs_center_axial_error_mm",
        "max_cube_vertex_rmse_mm",
        "max_abs_edge_error_mm",
        "max_median_static_scatter_p95_mm",
    ):
        if strict_number(
            analysis.get("acceptance", {}).get(key, -1),
            f"analysis.acceptance.{key}",
        ) < 0:
            raise SystemExit(f"analysis.acceptance.{key} must not be negative.")
    minimum_vertex_fraction = strict_number(
        analysis.get("acceptance", {}).get("minimum_vertex_fraction", -1),
        "analysis.acceptance.minimum_vertex_fraction",
    )
    if minimum_vertex_fraction > 1:
        raise SystemExit("analysis.acceptance.minimum_vertex_fraction must not exceed 1.")
    minimum_successful_pass_fraction = strict_number(
        analysis.get("acceptance", {}).get("minimum_successful_pass_fraction", -1),
        "analysis.acceptance.minimum_successful_pass_fraction",
    )
    if not 0 < minimum_successful_pass_fraction <= 1:
        raise SystemExit(
            "analysis.acceptance.minimum_successful_pass_fraction must be in (0, 1]."
        )
    minimum_center_capture_fraction = strict_number(
        analysis.get("acceptance", {}).get(
            "minimum_center_capture_fraction_per_pass",
            -1,
        ),
        "analysis.acceptance.minimum_center_capture_fraction_per_pass",
    )
    if not 0 <= minimum_center_capture_fraction <= 1:
        raise SystemExit(
            "analysis.acceptance.minimum_center_capture_fraction_per_pass must be in [0, 1]."
        )

    return {
        "stage_config": stage_config,
        "sample_dir": sample_dir,
        "stage_axis": axis,
        "stage_serial": str(axes[axis]["serial_number"]),
        "stage_direction": direction,
        "stereo_calibration": calibration,
        "evaluation_frame": evaluation_frame,
        "camera_to_pat_transform": camera_to_pat_transform,
        "camera_to_pat_data": camera_to_pat_data,
    }


def cube_station_enabled(value: float, configured: Any) -> bool:
    if configured == "all":
        return True
    return any(math.isclose(float(value), float(item), abs_tol=1e-9) for item in configured)


def generate_plan(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Generate a dedicated registration pass plus forward/reverse evaluation passes."""
    stage = config["stage"]
    target = config["target"]
    experiment = config["experiment"]
    configured_positions = [
        float(item) for item in stage["positions_global_mm"]
    ]
    positions = sorted(configured_positions)
    scan_order = str(experiment.get("scan_order", "ascending_global")).strip().lower()
    forward_positions = (
        configured_positions
        if scan_order == "configured"
        else positions
    )
    reference = float(stage["reference_global_mm"])
    center = numeric_triplet(target["pat_center_mm"], "target.pat_center_mm")
    half_extent = numeric_triplet(target["cube_half_extent_mm"], "target.cube_half_extent_mm")
    _, _, stage_vector = experiment_motion_vectors(config)
    include_centers = strict_bool(
        experiment["center_before_and_after"],
        "experiment.center_before_and_after",
    )
    dedicated_registration = strict_bool(
        experiment["dedicated_registration_pass"],
        "experiment.dedicated_registration_pass",
    )
    cube_positions = experiment.get("cube_positions_global_mm", "all")
    cycles = int(experiment.get("scan_cycles", 1))
    station_lookup = {value: index for index, value in enumerate(positions)}

    samples: list[dict[str, Any]] = []
    sequence = 0
    gray = gray_cube_vertices()

    def append_station(
        *,
        pass_name: str,
        pass_direction: str,
        cycle_index: int,
        stage_position: float,
        vertex_order: list[tuple[int, int, int]],
        force_cube: bool = False,
    ) -> None:
        nonlocal sequence
        stage_delta = float(stage_position - reference)
        stage_offset = stage_vector * stage_delta
        point_specs: list[tuple[str, tuple[int, int, int] | None]] = []
        if include_centers:
            point_specs.append(("center_pre", None))
        if force_cube or cube_station_enabled(stage_position, cube_positions):
            point_specs.extend(("vertex", signs) for signs in vertex_order)
        if include_centers:
            point_specs.append(("center_post", None))
        if not point_specs:
            point_specs.append(("center", None))

        for kind, signs in point_specs:
            sequence += 1
            local_offset = (
                np.zeros(3, dtype=np.float64)
                if signs is None
                else half_extent * np.asarray(signs, dtype=np.float64)
            )
            pat_target = center + local_offset
            expected = stage_offset + local_offset
            samples.append(
                {
                    "sample_id": f"q{sequence:04d}",
                    "sequence_index": sequence,
                    "cycle_index": cycle_index,
                    "pass_name": pass_name,
                    "pass_direction": pass_direction,
                    "station_index": int(station_lookup[stage_position]),
                    "stage_command_global_mm": float(stage_position),
                    "stage_delta_from_reference_mm": stage_delta,
                    "kind": kind,
                    "vertex_id": "" if signs is None else vertex_id(signs),
                    "vertex_signs": None if signs is None else list(signs),
                    "pat_target_mm": pat_target.tolist(),
                    "expected_relative_pat_mm": expected.tolist(),
                    "status": "pending",
                    "capture_status": "pending",
                    "processing_status": "pending",
                    "attempts": [],
                    "accepted_run_dir": "",
                    "stereo_3d_npz": "",
                }
            )

    if dedicated_registration:
        append_station(
            pass_name=str(config["analysis"]["registration_pass_name"]),
            pass_direction="registration",
            cycle_index=-1,
            stage_position=reference,
            vertex_order=gray,
            force_cube=True,
        )

    for cycle_index in range(cycles):
        for pass_direction in ("forward", "reverse"):
            pass_positions = (
                forward_positions
                if pass_direction == "forward"
                else list(reversed(forward_positions))
            )
            vertex_order = gray if pass_direction == "forward" else list(reversed(gray))
            pass_name = f"cycle{cycle_index + 1:02d}_{pass_direction}"
            for stage_position in pass_positions:
                append_station(
                    pass_name=pass_name,
                    pass_direction=pass_direction,
                    cycle_index=cycle_index,
                    stage_position=stage_position,
                    vertex_order=vertex_order,
                )
    return samples


def plan_hash(samples: Iterable[dict[str, Any]]) -> str:
    stable = [
        {
            "sample_id": item["sample_id"],
            "sequence_index": int(item["sequence_index"]),
            "cycle_index": int(item["cycle_index"]),
            "pass_name": item["pass_name"],
            "pass_direction": item["pass_direction"],
            "station_index": int(item["station_index"]),
            "stage_command_global_mm": round(float(item["stage_command_global_mm"]), 9),
            "stage_delta_from_reference_mm": round(
                float(item["stage_delta_from_reference_mm"]),
                9,
            ),
            "kind": item["kind"],
            "vertex_id": item["vertex_id"],
            "vertex_signs": item["vertex_signs"],
            "pat_target_mm": [round(float(value), 9) for value in item["pat_target_mm"]],
            "expected_relative_pat_mm": [
                round(float(value), 9) for value in item["expected_relative_pat_mm"]
            ],
        }
        for item in samples
    ]
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def kabsch(source: np.ndarray, target: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Fit target ~= R @ source + t without scale."""
    source = np.asarray(source, dtype=np.float64)
    target = np.asarray(target, dtype=np.float64)
    if source.shape != target.shape or source.ndim != 2 or source.shape[1] != 3:
        raise ValueError("source and target must both be Nx3 arrays.")
    if source.shape[0] < 3:
        raise ValueError("At least three correspondences are required.")
    source_center = source.mean(axis=0)
    target_center = target.mean(axis=0)
    source_zero = source - source_center
    target_zero = target - target_center
    u, _, vt = np.linalg.svd(source_zero.T @ target_zero)
    rotation = vt.T @ u.T
    if np.linalg.det(rotation) < 0:
        vt[-1, :] *= -1
        rotation = vt.T @ u.T
    translation = target_center - rotation @ source_center
    return rotation, translation


def transform_points(points: np.ndarray, rotation: np.ndarray, translation: np.ndarray) -> np.ndarray:
    points = np.asarray(points, dtype=np.float64)
    return (np.asarray(rotation) @ points.T).T + np.asarray(translation)


def similarity_scale(source: np.ndarray, target: np.ndarray, rotation: np.ndarray) -> float:
    source = np.asarray(source, dtype=np.float64)
    target = np.asarray(target, dtype=np.float64)
    source_zero = source - source.mean(axis=0)
    target_zero = target - target.mean(axis=0)
    rotated = (np.asarray(rotation) @ source_zero.T).T
    denominator = float(np.sum(rotated * rotated))
    if denominator <= 0:
        return float("nan")
    return float(np.sum(rotated * target_zero) / denominator)


def robust_static_point(
    npz_path: Path,
    *,
    trim_start_sec: float,
    trim_end_sec: float,
    outlier_floor_mm: float,
    mad_factor: float,
) -> dict[str, Any]:
    """Reduce one static 3D track to a robust centre and radial scatter."""
    with np.load(npz_path, allow_pickle=False) as data:
        points = np.asarray(data["points_left_cam_mm"], dtype=np.float64)
        valid = np.asarray(data["valid"], dtype=bool)
        t_sec = np.asarray(data["t_sec"], dtype=np.float64)
    mask = valid & np.all(np.isfinite(points), axis=1) & np.isfinite(t_sec)
    raw_valid_samples = int(mask.sum())
    raw_valid_fraction = (
        float(raw_valid_samples / points.shape[0]) if points.shape[0] else 0.0
    )
    if mask.any():
        first_t = float(np.min(t_sec[mask]))
        last_t = float(np.max(t_sec[mask]))
        mask &= t_sec >= first_t + max(0.0, float(trim_start_sec))
        mask &= t_sec <= last_t - max(0.0, float(trim_end_sec))
    samples = points[mask]
    empty = {
        "center_mm": np.full(3, np.nan),
        "input_samples": int(points.shape[0]),
        "raw_valid_samples": raw_valid_samples,
        "raw_valid_fraction": raw_valid_fraction,
        "trimmed_valid_samples": int(samples.shape[0]),
        "robust_samples": 0,
        "sample_threshold_mm": float("nan"),
        "scatter_rms_mm": float("nan"),
        "scatter_p95_mm": float("nan"),
        "scatter_max_mm": float("nan"),
    }
    if samples.size == 0:
        return empty
    initial_center = np.median(samples, axis=0)
    radial = np.linalg.norm(samples - initial_center, axis=1)
    radial_median = float(np.median(radial))
    radial_mad = float(np.median(np.abs(radial - radial_median)))
    threshold = max(
        float(outlier_floor_mm),
        radial_median + float(mad_factor) * 1.4826 * radial_mad,
    )
    robust = samples[np.isfinite(radial) & (radial <= threshold)]
    if robust.size == 0:
        robust = samples
    center = np.median(robust, axis=0)
    scatter = np.linalg.norm(robust - center, axis=1)
    return {
        "center_mm": center,
        "input_samples": int(points.shape[0]),
        "raw_valid_samples": raw_valid_samples,
        "raw_valid_fraction": raw_valid_fraction,
        "trimmed_valid_samples": int(samples.shape[0]),
        "robust_samples": int(robust.shape[0]),
        "sample_threshold_mm": float(threshold),
        "scatter_rms_mm": float(np.sqrt(np.mean(scatter * scatter))),
        "scatter_p95_mm": float(np.percentile(scatter, 95)),
        "scatter_max_mm": float(np.max(scatter)),
    }


def cube_edge_pairs() -> list[tuple[str, str, int]]:
    """Return the 12 undirected cube edges and their changed axis index."""
    vertices = gray_cube_vertices()
    result: list[tuple[str, str, int]] = []
    for index, left in enumerate(vertices):
        for right in vertices[index + 1 :]:
            changed = [axis for axis in range(3) if left[axis] != right[axis]]
            if len(changed) == 1:
                result.append((vertex_id(left), vertex_id(right), changed[0]))
    return result


def fit_line_against_stage(
    stage_delta_mm: np.ndarray,
    measured_points_mm: np.ndarray,
) -> dict[str, Any]:
    """Fit p(s)=intercept+slope*s for a diagnostic stage-axis estimate."""
    stage_delta_mm = np.asarray(stage_delta_mm, dtype=np.float64).reshape(-1)
    measured_points_mm = np.asarray(measured_points_mm, dtype=np.float64)
    if measured_points_mm.shape != (stage_delta_mm.size, 3) or stage_delta_mm.size < 2:
        raise ValueError("At least two stage/3D point pairs are required.")
    design = np.column_stack([np.ones(stage_delta_mm.size), stage_delta_mm])
    coefficients, _, _, _ = np.linalg.lstsq(design, measured_points_mm, rcond=None)
    intercept = coefficients[0]
    slope = coefficients[1]
    scale = float(np.linalg.norm(slope))
    direction = slope / scale if scale > 0 else np.full(3, np.nan)
    predicted = design @ coefficients
    residual_vectors = measured_points_mm - predicted
    radial = np.linalg.norm(residual_vectors, axis=1)
    return {
        "intercept_mm": intercept,
        "slope_camera_mm_per_stage_mm": slope,
        "scale_mm_per_mm": scale,
        "direction_camera": direction,
        "predicted_mm": predicted,
        "residual_vectors_mm": residual_vectors,
        "residual_rms_mm": float(np.sqrt(np.mean(radial * radial))),
        "residual_max_mm": float(np.max(radial)),
    }


def load_stereo_calibration(path: Path) -> dict[str, np.ndarray]:
    required = [
        "left_camera_matrix",
        "left_dist_coeffs",
        "right_camera_matrix",
        "right_dist_coeffs",
        "R",
        "T",
    ]
    with np.load(path, allow_pickle=False) as data:
        missing = [key for key in required if key not in data.files]
        if missing:
            raise SystemExit(f"Stereo calibration is missing keys: {missing}")
        result = {key: np.asarray(data[key], dtype=np.float64) for key in required}
        for optional in ("stereo_rms", "image_size"):
            if optional in data.files:
                result[optional] = np.asarray(data[optional])
    return result


def calibration_baseline_mm(calibration: dict[str, np.ndarray]) -> float:
    return float(np.linalg.norm(np.asarray(calibration["T"], dtype=np.float64).reshape(3)))


def stereo_baseline_line_left_camera(
    calibration: dict[str, np.ndarray],
) -> dict[str, np.ndarray | float]:
    """Return the calibrated optical-centre baseline line in the left-camera frame."""
    rotation_left_to_right = np.asarray(calibration["R"], dtype=np.float64)
    translation_left_to_right = np.asarray(
        calibration["T"],
        dtype=np.float64,
    ).reshape(3)
    if rotation_left_to_right.shape != (3, 3):
        raise ValueError("Stereo calibration R must have shape (3, 3).")
    left_center = np.zeros(3, dtype=np.float64)
    right_center = (
        -rotation_left_to_right.T @ translation_left_to_right
    )
    baseline_vector = right_center - left_center
    baseline_length = float(np.linalg.norm(baseline_vector))
    if not math.isfinite(baseline_length) or baseline_length <= 0:
        raise ValueError("Stereo optical-centre baseline must be finite and positive.")
    return {
        "left_optical_center_mm": left_center,
        "right_optical_center_mm": right_center,
        "baseline_unit_left_camera": baseline_vector / baseline_length,
        "baseline_length_mm": baseline_length,
    }


def point_to_stereo_baseline_line(
    point_left_camera_mm: np.ndarray,
    calibration: dict[str, np.ndarray],
) -> dict[str, np.ndarray | float | bool]:
    """Project one left-camera point onto the infinite optical-centre baseline line."""
    point = np.asarray(point_left_camera_mm, dtype=np.float64).reshape(3)
    geometry = stereo_baseline_line_left_camera(calibration)
    left_center = np.asarray(geometry["left_optical_center_mm"], dtype=np.float64)
    baseline_unit = np.asarray(
        geometry["baseline_unit_left_camera"],
        dtype=np.float64,
    )
    baseline_length = float(geometry["baseline_length_mm"])
    relative = point - left_center
    foot_from_left = float(np.dot(relative, baseline_unit))
    foot = left_center + foot_from_left * baseline_unit
    perpendicular = point - foot
    return {
        **geometry,
        "point_left_camera_mm": point,
        "foot_left_camera_mm": foot,
        "foot_from_left_optical_center_mm": foot_from_left,
        "foot_fraction_of_left_to_right_baseline": foot_from_left / baseline_length,
        "foot_is_between_optical_centers": bool(
            -1e-9 <= foot_from_left <= baseline_length + 1e-9
        ),
        "perpendicular_vector_left_camera_mm": perpendicular,
        "perpendicular_distance_mm": float(np.linalg.norm(perpendicular)),
    }


def calibration_focal_px(calibration: dict[str, np.ndarray]) -> float:
    left_fx = float(calibration["left_camera_matrix"][0, 0])
    right_fx = float(calibration["right_camera_matrix"][0, 0])
    return 0.5 * (left_fx + right_fx)


def simple_depth_sigma_mm(
    z_mm: float,
    *,
    focal_px: float,
    baseline_mm: float,
    per_camera_pixel_sigma: float,
) -> float:
    disparity_sigma = math.sqrt(2.0) * float(per_camera_pixel_sigma)
    return float(z_mm) ** 2 * disparity_sigma / (float(focal_px) * float(baseline_mm))


def simple_depth_limit_mm(
    tolerance_mm: float,
    *,
    focal_px: float,
    baseline_mm: float,
    per_camera_pixel_sigma: float,
    coverage_sigma: float = 1.96,
) -> float:
    disparity_sigma = math.sqrt(2.0) * float(per_camera_pixel_sigma)
    denominator = float(coverage_sigma) * disparity_sigma
    if denominator <= 0:
        return float("inf")
    return math.sqrt(float(tolerance_mm) * float(focal_px) * float(baseline_mm) / denominator)


def triangulate_observation(
    observation_px: np.ndarray,
    calibration: dict[str, np.ndarray],
) -> np.ndarray:
    import cv2

    obs = np.asarray(observation_px, dtype=np.float64).reshape(4)
    left = cv2.undistortPoints(
        obs[:2].reshape(1, 1, 2),
        calibration["left_camera_matrix"],
        calibration["left_dist_coeffs"],
    ).reshape(2)
    right = cv2.undistortPoints(
        obs[2:].reshape(1, 1, 2),
        calibration["right_camera_matrix"],
        calibration["right_dist_coeffs"],
    ).reshape(2)
    p_left = np.hstack([np.eye(3), np.zeros((3, 1))])
    p_right = np.hstack(
        [
            np.asarray(calibration["R"], dtype=np.float64),
            np.asarray(calibration["T"], dtype=np.float64).reshape(3, 1),
        ]
    )
    homogeneous = cv2.triangulatePoints(
        p_left,
        p_right,
        left.reshape(2, 1),
        right.reshape(2, 1),
    )
    return (homogeneous[:3, 0] / homogeneous[3, 0]).astype(np.float64)


def project_left_camera_point(
    point_left_camera_mm: np.ndarray,
    calibration: dict[str, np.ndarray],
) -> np.ndarray:
    import cv2

    point = np.asarray(point_left_camera_mm, dtype=np.float64).reshape(1, 1, 3)
    zeros = np.zeros((3, 1), dtype=np.float64)
    left_px, _ = cv2.projectPoints(
        point,
        zeros,
        zeros,
        calibration["left_camera_matrix"],
        calibration["left_dist_coeffs"],
    )
    right_rvec, _ = cv2.Rodrigues(np.asarray(calibration["R"], dtype=np.float64))
    right_px, _ = cv2.projectPoints(
        point,
        right_rvec,
        np.asarray(calibration["T"], dtype=np.float64).reshape(3, 1),
        calibration["right_camera_matrix"],
        calibration["right_dist_coeffs"],
    )
    return np.concatenate([left_px.reshape(2), right_px.reshape(2)])


def numerical_triangulation_covariance(
    point_left_camera_mm: np.ndarray,
    calibration: dict[str, np.ndarray],
    *,
    per_camera_pixel_sigma: float,
    finite_difference_px: float = 1e-3,
) -> dict[str, Any]:
    """Propagate independent (uL,vL,uR,vR) noise through actual triangulation."""
    observation = project_left_camera_point(point_left_camera_mm, calibration)
    jacobian = np.empty((3, 4), dtype=np.float64)
    epsilon = float(finite_difference_px)
    for column in range(4):
        plus = observation.copy()
        minus = observation.copy()
        plus[column] += epsilon
        minus[column] -= epsilon
        jacobian[:, column] = (
            triangulate_observation(plus, calibration)
            - triangulate_observation(minus, calibration)
        ) / (2.0 * epsilon)
    observation_covariance = np.eye(4) * float(per_camera_pixel_sigma) ** 2
    covariance = jacobian @ observation_covariance @ jacobian.T
    sigma_xyz = np.sqrt(np.maximum(0.0, np.diag(covariance)))
    return {
        "observation_px": observation,
        "jacobian_mm_per_px": jacobian,
        "covariance_mm2": covariance,
        "sigma_xyz_mm": sigma_xyz,
    }


def triangulation_angle_deg(
    point_left_camera_mm: np.ndarray,
    calibration: dict[str, np.ndarray],
) -> float:
    point = np.asarray(point_left_camera_mm, dtype=np.float64).reshape(3)
    camera_two_center = -np.asarray(calibration["R"]).T @ np.asarray(
        calibration["T"], dtype=np.float64
    ).reshape(3)
    ray_one = point
    ray_two = point - camera_two_center
    denominator = float(np.linalg.norm(ray_one) * np.linalg.norm(ray_two))
    if denominator <= 0:
        return float("nan")
    cosine = float(np.clip(np.dot(ray_one, ray_two) / denominator, -1.0, 1.0))
    return float(np.degrees(np.arccos(cosine)))


def json_ready(value: Any) -> Any:
    """Convert numpy/scalar non-finite values for strict, readable JSON output."""
    if isinstance(value, np.ndarray):
        return json_ready(value.tolist())
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    return value
