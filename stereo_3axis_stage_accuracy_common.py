#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared validation and immutable planning for a three-axis Ossila experiment."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np


SCRIPT_DIR = Path(__file__).resolve().parent
AXES = ("x", "y", "z")


def _deep_merge_json(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge JSON objects while replacing arrays and scalar values."""

    result = dict(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge_json(result[key], value)
        else:
            result[key] = value
    return result


def load_json(path: Path, *, _inheritance_stack: tuple[Path, ...] = ()) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"Top-level JSON value must be an object: {path}")
    base_text = payload.pop("extends_config", None)
    if base_text is not None:
        if not isinstance(base_text, str) or not base_text.strip():
            raise SystemExit(f"extends_config must be a non-empty path string: {path}")
        resolved_path = path.resolve()
        if resolved_path in _inheritance_stack:
            chain = " -> ".join(str(item) for item in (*_inheritance_stack, resolved_path))
            raise SystemExit(f"Circular config inheritance: {chain}")
        base_path = Path(base_text)
        if not base_path.is_absolute():
            base_path = (path.parent / base_path).resolve()
        base = load_json(
            base_path,
            _inheritance_stack=(*_inheritance_stack, resolved_path),
        )
        payload = _deep_merge_json(base, payload)
    return payload


def resolve_from_config(path_text: str, config_path: Path) -> Path:
    path = Path(str(path_text))
    if path.is_absolute():
        return path.resolve()
    config_relative = (config_path.parent / path).resolve()
    if config_relative.exists():
        return config_relative
    return (SCRIPT_DIR / path).resolve()


def strict_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise SystemExit(f"{name} must be a JSON boolean.")
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


def numeric_triplet(value: Any, name: str) -> np.ndarray:
    if not isinstance(value, list) or len(value) != 3:
        raise SystemExit(f"{name} must be a three-element JSON array.")
    result = np.asarray(
        [strict_number(item, f"{name}[{index}]") for index, item in enumerate(value)],
        dtype=np.float64,
    )
    return result


def point_list(value: Any, name: str, *, allow_empty: bool = False) -> list[np.ndarray]:
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "an array" if allow_empty else "a non-empty array"
        raise SystemExit(f"{name} must be {qualifier} of three-element points.")
    return [numeric_triplet(item, f"{name}[{index}]") for index, item in enumerate(value)]


def validation_sweeps(experiment: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize new local-reference sweeps or the legacy three-group plan."""
    raw_sweeps = experiment.get("validation_sweeps")
    if raw_sweeps is None:
        references = experiment.get("validation_reference_global_mm_by_group")
        if not isinstance(references, dict) or set(references) != {
            "axis",
            "depth",
            "diagonal",
        }:
            raise SystemExit(
                "experiment.validation_reference_global_mm_by_group must contain "
                "axis, depth, and diagonal when validation_sweeps is absent."
            )
        return [
            {
                "name": group,
                "group": group,
                "reference": numeric_triplet(
                    references[group],
                    f"experiment.validation_reference_global_mm_by_group.{group}",
                ),
                "points": point_list(
                    experiment.get(f"validation_{group}_points_global_mm"),
                    f"experiment.validation_{group}_points_global_mm",
                ),
                "approach_axis": (
                    str(experiment.get("diagonal_approach_axis", "")).lower()
                    if group == "diagonal"
                    else ""
                ),
            }
            for group in ("axis", "depth", "diagonal")
        ]

    if not isinstance(raw_sweeps, list) or not raw_sweeps:
        raise SystemExit(
            "experiment.validation_sweeps must be a non-empty JSON array."
        )
    sweeps: list[dict[str, Any]] = []
    names: set[str] = set()
    groups: set[str] = set()
    for index, raw in enumerate(raw_sweeps):
        prefix = f"experiment.validation_sweeps[{index}]"
        if not isinstance(raw, dict):
            raise SystemExit(f"{prefix} must be a JSON object.")
        name = str(raw.get("name", "")).strip().lower()
        if (
            not name
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in name)
        ):
            raise SystemExit(
                f"{prefix}.name must contain only lowercase letters, digits, and underscores."
            )
        if name in names:
            raise SystemExit(f"Duplicate validation sweep name: {name!r}.")
        names.add(name)
        group = str(raw.get("group", "")).strip().lower()
        if group not in {"axis", "depth", "diagonal"}:
            raise SystemExit(f"{prefix}.group must be axis, depth, or diagonal.")
        groups.add(group)
        approach_axis = str(raw.get("approach_axis", "")).strip().lower()
        if approach_axis and approach_axis not in AXES:
            raise SystemExit(f"{prefix}.approach_axis must be x, y, or z.")
        if group == "diagonal" and not approach_axis:
            raise SystemExit(f"{prefix}.approach_axis is required for a diagonal sweep.")
        reference = numeric_triplet(
            raw.get("reference_global_mm"),
            f"{prefix}.reference_global_mm",
        )
        cuboid_extents_raw = raw.get("cuboid_half_extents_mm")
        if cuboid_extents_raw is not None:
            if group != "diagonal":
                raise SystemExit(
                    f"{prefix}.cuboid_half_extents_mm requires group='diagonal'."
                )
            if raw.get("points_global_mm") is not None:
                raise SystemExit(
                    f"{prefix} cannot specify both points_global_mm and "
                    "cuboid_half_extents_mm."
                )
            if not isinstance(cuboid_extents_raw, list) or not cuboid_extents_raw:
                raise SystemExit(
                    f"{prefix}.cuboid_half_extents_mm must be a non-empty array."
                )
            cuboid_extents = [
                strict_number(value, f"{prefix}.cuboid_half_extents_mm[{extent_index}]")
                for extent_index, value in enumerate(cuboid_extents_raw)
            ]
            if any(value <= 0.0 for value in cuboid_extents):
                raise SystemExit(
                    f"{prefix}.cuboid_half_extents_mm values must be positive."
                )
            rounded_extents = [round(value, 9) for value in cuboid_extents]
            if len(set(rounded_extents)) != len(rounded_extents):
                raise SystemExit(
                    f"{prefix}.cuboid_half_extents_mm contains a duplicate value."
                )
            geometry = (
                str(raw.get("geometry", "cuboid_vertices")).strip().lower()
                or "cuboid_vertices"
            )
            if geometry not in {"cuboid_vertices", "cuboid_surface_grid"}:
                raise SystemExit(
                    f"{prefix}.geometry must be cuboid_vertices or "
                    "cuboid_surface_grid when cuboid_half_extents_mm is used."
                )
            sign_patterns = (
                itertools.product((-1.0, 1.0), repeat=3)
                if geometry == "cuboid_vertices"
                else (
                    signs
                    for signs in itertools.product((-1.0, 0.0, 1.0), repeat=3)
                    if signs != (0.0, 0.0, 0.0)
                )
            )
            patterns = list(sign_patterns)
            points = [
                reference
                + extent * np.asarray(signs, dtype=np.float64)
                for extent in cuboid_extents
                for signs in patterns
            ]
        else:
            points = point_list(
                raw.get("points_global_mm"),
                f"{prefix}.points_global_mm",
            )
            cuboid_extents = []
            geometry = str(raw.get("geometry", "points")).strip().lower() or "points"
        rounded = [tuple(np.round(point, decimals=9)) for point in points]
        if len(set(rounded)) != len(rounded):
            raise SystemExit(f"{prefix}.points_global_mm contains a duplicate point.")
        for point_index, point in enumerate(points):
            nonzero_axes = [
                axis
                for axis_index, axis in enumerate(AXES)
                if abs(float(point[axis_index] - reference[axis_index])) > 1e-9
            ]
            if group in {"axis", "depth"} and len(nonzero_axes) != 1:
                raise SystemExit(
                    f"{prefix}.points_global_mm[{point_index}] must differ from "
                    "its local reference on exactly one axis."
                )
            if (
                group == "diagonal"
                and geometry != "cuboid_surface_grid"
                and len(nonzero_axes) < 2
            ):
                raise SystemExit(
                    f"{prefix}.points_global_mm[{point_index}] must differ from "
                    "its local reference on at least two axes."
                )
        sweeps.append(
            {
                "name": name,
                "group": group,
                "reference": reference,
                "points": points,
                "approach_axis": approach_axis,
                "geometry": geometry,
                "cuboid_half_extents_mm": cuboid_extents,
            }
        )
    if groups != {"axis", "depth", "diagonal"}:
        raise SystemExit(
            "experiment.validation_sweeps must include axis, depth, and diagonal groups."
        )
    return sweeps


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    return value


def parse_stage_config_md(path: Path) -> dict[str, dict[str, Any]]:
    """Parse X/Y/Z USB serial and direction without opening any serial port."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise SystemExit(f"Ossila axis config not found: {path}") from exc
    axes: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 2:
            raise SystemExit(f"Invalid Ossila config line {line_number}: {line!r}")
        axis = parts[0].lower()
        if axis not in AXES:
            raise SystemExit(f"Unsupported Ossila axis on line {line_number}: {parts[0]!r}")
        serial = str(parts[1]).strip()
        if serial == "-":
            continue
        try:
            direction = int(parts[2]) if len(parts) >= 3 else 1
        except ValueError as exc:
            raise SystemExit(f"Invalid direction on Ossila config line {line_number}.") from exc
        if direction not in {-1, 1}:
            raise SystemExit(f"Direction must be +1 or -1 on line {line_number}.")
        if axis in axes:
            raise SystemExit(f"Duplicate Ossila axis {axis.upper()} in {path}.")
        axes[axis] = {"serial_number": serial, "direction": direction}
    return axes


def _calibration_scalar_text(data: Any, key: str) -> str:
    if key not in data.files:
        return ""
    return str(np.asarray(data[key]).reshape(-1)[0])


def _validate_axis(
    axis: str,
    spec: dict[str, Any],
    parsed: dict[str, dict[str, Any]],
) -> None:
    prefix = f"stages.axes.{axis}"
    if axis not in parsed:
        raise SystemExit(f"{prefix} is enabled in JSON but absent from Ossila config.md.")
    usb_serial = str(spec.get("usb_serial", "")).strip()
    if not usb_serial:
        raise SystemExit(f"{prefix}.usb_serial must be non-empty.")
    if usb_serial != str(parsed[axis]["serial_number"]):
        raise SystemExit(
            f"{prefix}.usb_serial={usb_serial!r} does not match config.md "
            f"{parsed[axis]['serial_number']!r}."
        )
    direction = strict_integer(spec.get("direction"), f"{prefix}.direction")
    if direction not in {-1, 1}:
        raise SystemExit(f"{prefix}.direction must be +1 or -1.")
    if direction != int(parsed[axis]["direction"]):
        raise SystemExit(
            f"{prefix}.direction={direction:+d} does not match config.md "
            f"{int(parsed[axis]['direction']):+d}."
        )
    datum = strict_number(spec.get("datum_mm"), f"{prefix}.datum_mm")
    expected_travel = strict_number(
        spec.get("expected_travel_mm"),
        f"{prefix}.expected_travel_mm",
    )
    hardware_min = strict_number(spec.get("hardware_min_mm"), f"{prefix}.hardware_min_mm")
    hardware_max = strict_number(spec.get("hardware_max_mm"), f"{prefix}.hardware_max_mm")
    if expected_travel <= 0:
        raise SystemExit(f"{prefix}.expected_travel_mm must be positive.")
    if not 0 <= hardware_min < hardware_max <= expected_travel:
        raise SystemExit(
            f"{prefix} soft limits must satisfy "
            "0 <= hardware_min_mm < hardware_max_mm <= expected_travel_mm."
        )
    if not hardware_min <= datum <= hardware_max:
        raise SystemExit(f"{prefix}.datum_mm must be within that axis soft limits.")
    for key in (
        "speed_mm_s",
        "readback_tolerance_mm",
        "capture_drift_tolerance_mm",
        "motion_timeout_sec",
        "command_timeout_sec",
        "status_poll_sec",
        "settings_match_tolerance_mm_s2",
        "home_readback_tolerance_mm",
    ):
        if strict_number(spec.get(key), f"{prefix}.{key}") <= 0:
            raise SystemExit(f"{prefix}.{key} must be positive.")
    if strict_number(spec.get("settle_sec"), f"{prefix}.settle_sec") < 0:
        raise SystemExit(f"{prefix}.settle_sec must not be negative.")
    strict_bool(spec.get("require_clear_alarms"), f"{prefix}.require_clear_alarms")
    for key in ("expected_acceleration_mm_s2", "expected_deceleration_mm_s2"):
        value = spec.get(key)
        if value is not None and strict_number(value, f"{prefix}.{key}") <= 0:
            raise SystemExit(f"{prefix}.{key} must be positive when configured.")
    for key in ("expected_device_response", "expected_internal_serial_response"):
        value = spec.get(key)
        if not isinstance(value, str):
            raise SystemExit(f"{prefix}.{key} must be a JSON string.")


def hardware_from_global(spec: dict[str, Any], global_mm: float) -> float:
    return float(spec["datum_mm"]) + int(spec["direction"]) * float(global_mm)


def global_point_dict(point: Iterable[float]) -> dict[str, float]:
    values = list(point)
    return {axis: float(values[index]) for index, axis in enumerate(AXES)}


def hardware_point_dict(config: dict[str, Any], point: Iterable[float]) -> dict[str, float]:
    values = list(point)
    return {
        axis: hardware_from_global(config["stages"]["axes"][axis], values[index])
        for index, axis in enumerate(AXES)
    }


def _validate_plan_point(config: dict[str, Any], point: np.ndarray, name: str) -> None:
    for index, axis in enumerate(AXES):
        spec = config["stages"]["axes"][axis]
        hardware = hardware_from_global(spec, float(point[index]))
        lower = float(spec["hardware_min_mm"])
        upper = float(spec["hardware_max_mm"])
        if hardware < lower - 1e-9 or hardware > upper + 1e-9:
            raise SystemExit(
                f"{name} axis {axis.upper()} global={point[index]:.6f} maps to "
                f"hardware={hardware:.6f}, outside [{lower:.6f}, {upper:.6f}] mm."
            )


def _plan_point_within_limits(config: dict[str, Any], point: np.ndarray) -> bool:
    for index, axis in enumerate(AXES):
        spec = config["stages"]["axes"][axis]
        hardware = hardware_from_global(spec, float(point[index]))
        if (
            hardware < float(spec["hardware_min_mm"]) - 1e-9
            or hardware > float(spec["hardware_max_mm"]) + 1e-9
        ):
            return False
    return True


def validate_config(config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    stages = config.get("stages")
    experiment = config.get("experiment")
    camera = config.get("camera")
    target = config.get("target")
    if (
        not isinstance(stages, dict)
        or not isinstance(experiment, dict)
        or not isinstance(camera, dict)
        or not isinstance(target, dict)
    ):
        raise SystemExit("Config requires stages, experiment, camera, and target objects.")

    safety_acknowledgements = config.get("safety_acknowledgements")
    required_safety_acknowledgements = (
        "physical_xyz_mapping_and_sign_confirmed",
        "y_nearest_and_farther_direction_confirmed",
        "lateral_motion_clearance_confirmed",
        "full_motion_envelope_and_cables_confirmed",
        "target_mount_and_stack_load_confirmed",
    )
    if not isinstance(safety_acknowledgements, dict):
        raise SystemExit("Config requires a safety_acknowledgements object.")
    for key in required_safety_acknowledgements:
        strict_bool(
            safety_acknowledgements.get(key),
            f"safety_acknowledgements.{key}",
        )

    for key in (
        "type",
        "driver_control",
        "driver_description",
        "modulation_verification_instrument",
        "notes",
    ):
        if not isinstance(target.get(key), str):
            raise SystemExit(f"target.{key} must be a JSON string.")
    if not str(target.get("type", "")).strip():
        raise SystemExit("target.type must be non-empty.")
    if not str(target.get("driver_control", "")).strip():
        raise SystemExit("target.driver_control must be non-empty.")
    if strict_number(
        target.get("nominal_modulation_hz"),
        "target.nominal_modulation_hz",
    ) <= 0:
        raise SystemExit("target.nominal_modulation_hz must be positive.")
    duty_cycle = strict_number(target.get("duty_cycle"), "target.duty_cycle")
    if not 0 < duty_cycle < 1:
        raise SystemExit("target.duty_cycle must be in (0, 1).")
    for key in (
        "aperture_diameter_mm",
        "fiber_core_diameter_mm",
        "led_wavelength_nm",
    ):
        value = target.get(key)
        if value is not None and strict_number(value, f"target.{key}") <= 0:
            raise SystemExit(f"target.{key} must be positive when configured.")

    sample_dir = resolve_from_config(str(stages.get("sample_dir", "")), config_path)
    stage_config = resolve_from_config(str(stages.get("config", "")), config_path)
    if not sample_dir.is_dir():
        raise SystemExit(f"Ossila sample directory not found: {sample_dir}")
    parsed = parse_stage_config_md(stage_config)
    if set(parsed) != set(AXES):
        raise SystemExit(
            "The three-axis experiment requires X, Y, and Z enabled in config.md; "
            f"found {sorted(parsed)}."
        )
    axes_config = stages.get("axes")
    if not isinstance(axes_config, dict) or set(axes_config) != set(AXES):
        raise SystemExit("stages.axes must contain exactly x, y, and z objects.")
    for axis in AXES:
        if not isinstance(axes_config[axis], dict):
            raise SystemExit(f"stages.axes.{axis} must be an object.")
        _validate_axis(axis, axes_config[axis], parsed)

    move_order = stages.get("move_axis_order")
    if not isinstance(move_order, list) or len(move_order) != 3:
        raise SystemExit("stages.move_axis_order must list x, y, z exactly once.")
    normalized_order = [str(item).strip().lower() for item in move_order]
    if set(normalized_order) != set(AXES) or len(set(normalized_order)) != 3:
        raise SystemExit("stages.move_axis_order must be a permutation of x, y, z.")
    strict_bool(stages.get("return_to_reference_on_success"), "stages.return_to_reference_on_success")
    depth_axis = str(stages.get("depth_axis", "")).strip().lower()
    if depth_axis not in AXES:
        raise SystemExit("stages.depth_axis must be x, y, or z.")
    if depth_axis != "y":
        raise SystemExit(
            "This experiment currently supports stages.depth_axis='y' only; "
            "the HOME safety sequence is intentionally fail-closed."
        )
    lower_hardware_is_safer = strict_bool(
        stages.get("depth_lower_hardware_is_safer"),
        "stages.depth_lower_hardware_is_safer",
    )
    lower_hardware_is_farther = strict_bool(
        stages.get("depth_lower_hardware_is_farther"),
        "stages.depth_lower_hardware_is_farther",
    )
    lateral_clearance = strict_number(
        stages.get("lateral_motion_clearance_global_mm"),
        "stages.lateral_motion_clearance_global_mm",
    )

    reference = numeric_triplet(
        experiment.get("reference_global_mm"),
        "experiment.reference_global_mm",
    )
    sweeps = validation_sweeps(experiment)
    approach_offset = strict_number(
        experiment.get("approach_offset_mm"),
        "experiment.approach_offset_mm",
    )
    if approach_offset <= 0:
        raise SystemExit("experiment.approach_offset_mm must be positive.")
    diagonal_approach_axis = str(
        experiment.get("diagonal_approach_axis", "")
    ).strip().lower()
    if diagonal_approach_axis not in AXES:
        raise SystemExit("experiment.diagonal_approach_axis must be x, y, or z.")
    moving_body = str(experiment.get("moving_body", "")).strip().lower()
    if moving_body != "target":
        raise SystemExit(
            "experiment.moving_body must be 'target' for this target-stage experiment."
        )
    absolute_reference = config.get("absolute_distance_reference")
    if not isinstance(absolute_reference, dict):
        raise SystemExit("absolute_distance_reference must be an object.")
    absolute_available = strict_bool(
        absolute_reference.get("available"),
        "absolute_distance_reference.available",
    )
    for key in (
        "definition",
        "method",
        "instrument",
        "measured_at",
        "home_state",
    ):
        if not isinstance(absolute_reference.get(key), str):
            raise SystemExit(f"absolute_distance_reference.{key} must be a string.")
    for key in (
        "distance_mm",
        "standard_uncertainty_mm",
        "stage_standard_uncertainty_mm",
    ):
        value = absolute_reference.get(key)
        if value is not None:
            number = strict_number(value, f"absolute_distance_reference.{key}")
            if number < 0:
                raise SystemExit(
                    f"absolute_distance_reference.{key} must not be negative."
                )
    if absolute_available and absolute_reference.get("distance_mm") is None:
        raise SystemExit(
            "absolute_distance_reference.distance_mm is required when available=true."
        )
    absolute_stage_axis = str(
        absolute_reference.get("stage_axis", "")
    ).strip().lower()
    if absolute_stage_axis not in AXES:
        raise SystemExit("absolute_distance_reference.stage_axis must be x, y, or z.")
    absolute_sign = strict_integer(
        absolute_reference.get("sign"),
        "absolute_distance_reference.sign",
    )
    if absolute_sign not in {-1, 1}:
        raise SystemExit("absolute_distance_reference.sign must be +1 or -1.")
    absolute_reference_command = numeric_triplet(
        absolute_reference.get("reference_command_global_mm"),
        "absolute_distance_reference.reference_command_global_mm",
    )
    if str(absolute_reference.get("moving_body", "")).strip().lower() != "target":
        raise SystemExit("absolute_distance_reference.moving_body must be 'target'.")
    if absolute_stage_axis != depth_axis:
        raise SystemExit(
            "absolute_distance_reference.stage_axis must match stages.depth_axis."
        )
    if not np.allclose(
        absolute_reference_command,
        reference,
        rtol=0.0,
        atol=1e-9,
    ):
        raise SystemExit(
            "absolute_distance_reference.reference_command_global_mm must match "
            "experiment.reference_global_mm."
        )
    depth_direction = int(axes_config[depth_axis]["direction"])
    expected_distance_sign = (
        -depth_direction if lower_hardware_is_farther else depth_direction
    )
    safer_global_sign = (
        -depth_direction if lower_hardware_is_safer else depth_direction
    )
    if absolute_sign != expected_distance_sign:
        raise SystemExit(
            "absolute_distance_reference.sign is inconsistent with the depth-axis "
            "config.md direction and stages.depth_lower_hardware_is_farther."
        )
    readback_global_raw = absolute_reference.get(
        "reference_readback_global_xyz_mm"
    )
    readback_hardware_raw = absolute_reference.get(
        "reference_readback_hardware_xyz_mm"
    )
    if absolute_available:
        if (
            readback_global_raw is None
            or readback_hardware_raw is None
            or absolute_reference.get("standard_uncertainty_mm") is None
            or absolute_reference.get("stage_standard_uncertainty_mm") is None
        ):
            raise SystemExit(
                "An available absolute distance requires actual global/hardware "
                "readback and both standard uncertainty fields."
            )
        for key in ("method", "instrument", "measured_at", "home_state"):
            if not str(absolute_reference.get(key, "")).strip():
                raise SystemExit(
                    f"absolute_distance_reference.{key} is required when available=true."
                )
    if readback_global_raw is not None or readback_hardware_raw is not None:
        if readback_global_raw is None or readback_hardware_raw is None:
            raise SystemExit(
                "Absolute-distance global and hardware readbacks must be configured together."
            )
        readback_global = numeric_triplet(
            readback_global_raw,
            "absolute_distance_reference.reference_readback_global_xyz_mm",
        )
        readback_hardware = numeric_triplet(
            readback_hardware_raw,
            "absolute_distance_reference.reference_readback_hardware_xyz_mm",
        )
        for index, axis in enumerate(AXES):
            expected_hardware = hardware_from_global(
                axes_config[axis],
                float(readback_global[index]),
            )
            tolerance = float(axes_config[axis]["readback_tolerance_mm"])
            lower = float(axes_config[axis]["hardware_min_mm"])
            upper = float(axes_config[axis]["hardware_max_mm"])
            if not lower - tolerance <= expected_hardware <= upper + tolerance:
                raise SystemExit(
                    "absolute distance reference readback axis "
                    f"{axis.upper()} global={float(readback_global[index]):.6f} "
                    f"maps to hardware={expected_hardware:.6f}, outside "
                    f"[{lower:.6f}, {upper:.6f}] mm even with "
                    f"+/-{tolerance:.6f} mm readback allowance."
                )
            if (
                abs(expected_hardware - float(readback_hardware[index]))
                > tolerance
            ):
                raise SystemExit(
                    "absolute distance reference global/hardware readbacks are "
                    f"inconsistent on {axis.upper()}."
                )

    stage_truth = config.get("stage_truth")
    if not isinstance(stage_truth, dict):
        raise SystemExit("Config requires a stage_truth object.")
    for key in ("reference", "method", "instrument", "notes"):
        if not isinstance(stage_truth.get(key), str):
            raise SystemExit(f"stage_truth.{key} must be a JSON string.")
    external_truth = strict_bool(
        stage_truth.get("independent_external_displacement_truth_available"),
        "stage_truth.independent_external_displacement_truth_available",
    )
    truth_uncertainty = stage_truth.get("displacement_standard_uncertainty_mm")
    if truth_uncertainty is not None and strict_number(
        truth_uncertainty,
        "stage_truth.displacement_standard_uncertainty_mm",
    ) < 0:
        raise SystemExit(
            "stage_truth.displacement_standard_uncertainty_mm must not be negative."
        )
    if strict_number(
        stage_truth.get("coverage_sigma"),
        "stage_truth.coverage_sigma",
    ) <= 0:
        raise SystemExit("stage_truth.coverage_sigma must be positive.")
    if external_truth:
        raise SystemExit(
            "independent_external_displacement_truth_available=true is not "
            "supported until capture-by-capture external displacement vectors "
            "and their timestamps are implemented. Keep it false."
        )
    baseline_distance_definitions = {
        "target_point_to_stereo_optical_center_baseline_line_perpendicular",
        "point_to_stereo_optical_center_baseline_line_perpendicular",
        "stereo_baseline_perpendicular_distance",
    }
    if (
        absolute_available
        and str(absolute_reference.get("definition", "")).strip().lower()
        in baseline_distance_definitions
    ):
        if absolute_reference.get("axis_alignment_verified") is not True:
            raise SystemExit(
                "Optical baseline-perpendicular distance comparison requires "
                "absolute_distance_reference.axis_alignment_verified=true."
            )
        alignment_uncertainty = absolute_reference.get(
            "axis_alignment_standard_uncertainty_rad"
        )
        if alignment_uncertainty is None or strict_number(
            alignment_uncertainty,
            "absolute_distance_reference.axis_alignment_standard_uncertainty_rad",
        ) < 0:
            raise SystemExit(
                "Optical baseline comparison requires a non-negative axis "
                "alignment standard uncertainty."
            )
    orientation_reference = numeric_triplet(
        experiment.get("orientation_reference_global_mm"),
        "experiment.orientation_reference_global_mm",
    )
    orientation = point_list(
        experiment.get("orientation_points_global_mm"),
        "experiment.orientation_points_global_mm",
    )
    repeats = strict_integer(experiment.get("repeats"), "experiment.repeats")
    if repeats < 1:
        raise SystemExit("experiment.repeats must be at least 1.")
    strict_bool(experiment.get("forward_reverse"), "experiment.forward_reverse")
    if not strict_bool(
        experiment.get("center_before_after_each_pass"),
        "experiment.center_before_after_each_pass",
    ):
        raise SystemExit("center_before_after_each_pass must remain true for drift checks.")
    if not strict_bool(
        experiment.get("registration_center_before_after"),
        "experiment.registration_center_before_after",
    ):
        raise SystemExit(
            "registration_center_before_after must remain true for orientation drift checks."
        )

    registration_geometry = np.vstack([orientation_reference, *orientation])
    if np.unique(np.round(registration_geometry, decimals=9), axis=0).shape[0] < 4:
        raise SystemExit("Orientation registration requires at least four unique points.")
    rank = int(
        np.linalg.matrix_rank(
            registration_geometry - registration_geometry.mean(axis=0)
        )
    )
    if rank < 3:
        raise SystemExit("Orientation registration points must span three dimensions.")
    all_named_points: list[tuple[str, np.ndarray]] = [
        ("reference", reference),
        ("orientation_reference", orientation_reference),
    ]
    all_named_points.extend(
        (f"validation_sweep_{sweep['name']}_reference", sweep["reference"])
        for sweep in sweeps
    )
    all_named_points.extend(
        (f"orientation[{index}]", point) for index, point in enumerate(orientation)
    )
    for sweep in sweeps:
        all_named_points.extend(
            (f"validation_sweep_{sweep['name']}[{index}]", point)
            for index, point in enumerate(sweep["points"])
        )
    for name, point in all_named_points:
        _validate_plan_point(config, point, name)
        lateral_delta = point - reference
        has_lateral_offset = any(
            abs(float(lateral_delta[AXES.index(axis)])) > 1e-9
            for axis in AXES
            if axis != depth_axis
        )
        if has_lateral_offset:
            depth_value = float(point[AXES.index(depth_axis)])
            is_clear = (
                safer_global_sign * (depth_value - lateral_clearance) >= -1e-9
            )
            if not is_clear:
                raise SystemExit(
                    f"{name} has lateral offset at {depth_axis.upper()}="
                    f"{depth_value:.6f}, which is closer than "
                    "stages.lateral_motion_clearance_global_mm."
                )

    calibration = resolve_from_config(str(camera.get("stereo_calibration", "")), config_path)
    if not calibration.is_file():
        raise SystemExit(f"Stereo calibration not found: {calibration}")
    left_serial = str(camera.get("left_serial", "")).strip()
    right_serial = str(camera.get("right_serial", "")).strip()
    if not left_serial or not right_serial or left_serial == right_serial:
        raise SystemExit("camera left/right serials must be distinct and non-empty.")
    required_calibration = {
        "left_camera_matrix",
        "left_dist_coeffs",
        "right_camera_matrix",
        "right_dist_coeffs",
        "R",
        "T",
    }
    with np.load(calibration, allow_pickle=False) as data:
        missing = sorted(required_calibration.difference(data.files))
        if missing:
            raise SystemExit(f"Stereo calibration is missing keys: {missing}")
        expected_square = strict_number(
            camera.get("expected_square_mm"),
            "camera.expected_square_mm",
        )
        if "square_size_mm" not in data.files:
            raise SystemExit("Stereo calibration has no square_size_mm provenance.")
        actual_square = float(np.asarray(data["square_size_mm"]).reshape(-1)[0])
        if not math.isclose(expected_square, actual_square, rel_tol=0.0, abs_tol=1e-9):
            raise SystemExit(
                f"Calibration square size {actual_square:g} mm differs from "
                f"camera.expected_square_mm={expected_square:g}."
            )
        stored_left = _calibration_scalar_text(data, "left_camera_serial")
        stored_right = _calibration_scalar_text(data, "right_camera_serial")
        if stored_left and stored_left != left_serial:
            raise SystemExit(
                f"Calibration left serial {stored_left!r} differs from {left_serial!r}."
            )
        if stored_right and stored_right != right_serial:
            raise SystemExit(
                f"Calibration right serial {stored_right!r} differs from {right_serial!r}."
            )
    if strict_number(camera.get("record_sec"), "camera.record_sec") <= 0:
        raise SystemExit("camera.record_sec must be positive.")
    if strict_number(camera.get("start_delay_sec"), "camera.start_delay_sec") < 0:
        raise SystemExit("camera.start_delay_sec must not be negative.")
    if (
        strict_number(
            camera.get("hw_sync_timeout_sec"),
            "camera.hw_sync_timeout_sec",
        )
        <= 0
    ):
        raise SystemExit("camera.hw_sync_timeout_sec must be positive.")
    if (
        strict_number(
            camera.get("camera_reopen_wait_sec"),
            "camera.camera_reopen_wait_sec",
        )
        < 0
    ):
        raise SystemExit("camera.camera_reopen_wait_sec must not be negative.")
    for key in (
        "delta_t_us",
        "max_capture_attempts",
        "minimum_events_per_camera",
        "sensor_width",
        "sensor_height",
    ):
        if strict_integer(camera.get(key), f"camera.{key}") <= 0:
            raise SystemExit(f"camera.{key} must be positive.")
    max_events = strict_integer(camera.get("max_events"), "camera.max_events")
    if max_events < 0:
        raise SystemExit("camera.max_events must not be negative.")
    if str(camera.get("hw_sync", "")) not in {"left-master", "right-master"}:
        raise SystemExit("camera.hw_sync must be left-master or right-master.")
    if str(camera.get("npz_compression", "")) not in {"none", "compressed"}:
        raise SystemExit("camera.npz_compression must be none or compressed.")
    reuse_basis = camera.get("calibration_reuse_basis")
    if not isinstance(reuse_basis, dict):
        raise SystemExit("camera.calibration_reuse_basis must be an object.")
    for key in (
        "camera_rig_moved_as_one_fixture",
        "left_right_relative_mount_unchanged",
        "focus_unchanged",
    ):
        strict_bool(
            reuse_basis.get(key),
            f"camera.calibration_reuse_basis.{key}",
        )

    tracking = config.get("tracking")
    analysis = config.get("analysis")
    if not isinstance(tracking, dict) or not isinstance(analysis, dict):
        raise SystemExit("Config requires explicit tracking and analysis objects.")
    for key in ("window_us", "hop_us", "threshold_count", "min_events", "min_area", "min_mass"):
        if strict_integer(tracking.get(key), f"tracking.{key}") <= 0:
            raise SystemExit(f"tracking.{key} must be positive.")
    for key in ("dt_us", "max_time_gap_sec"):
        if strict_number(tracking.get(key), f"tracking.{key}") <= 0:
            raise SystemExit(f"tracking.{key} must be positive.")
    for key in ("roi", "tracking_method", "polarity"):
        if not str(tracking.get(key, "")).strip():
            raise SystemExit(f"tracking.{key} must be non-empty.")
    right_offset = tracking.get("right_time_offset_sec")
    if right_offset is not None:
        strict_number(right_offset, "tracking.right_time_offset_sec")
    if strict_integer(
        analysis.get("minimum_valid_samples"),
        "analysis.minimum_valid_samples",
    ) <= 0:
        raise SystemExit("analysis.minimum_valid_samples must be positive.")
    valid_fraction = strict_number(
        analysis.get("minimum_valid_fraction"),
        "analysis.minimum_valid_fraction",
    )
    if not 0 < valid_fraction <= 1:
        raise SystemExit("analysis.minimum_valid_fraction must be in (0, 1].")
    for key in (
        "trim_start_sec",
        "trim_end_sec",
        "outlier_floor_mm",
        "outlier_mad_factor",
        "return_position_tolerance_mm",
        "group_position_tolerance_mm",
        "axis_purity_ratio",
    ):
        if strict_number(analysis.get(key), f"analysis.{key}") < 0:
            raise SystemExit(f"analysis.{key} must not be negative.")
    pixel_sigmas = analysis.get("pixel_sigma_scenarios")
    if not isinstance(pixel_sigmas, list) or not pixel_sigmas:
        raise SystemExit("analysis.pixel_sigma_scenarios must be a non-empty array.")
    for index, value in enumerate(pixel_sigmas):
        if strict_number(
            value,
            f"analysis.pixel_sigma_scenarios[{index}]",
        ) <= 0:
            raise SystemExit(
                "analysis.pixel_sigma_scenarios values must be positive."
            )
    if strict_number(
        analysis.get("theory_coverage_sigma"),
        "analysis.theory_coverage_sigma",
    ) <= 0:
        raise SystemExit("analysis.theory_coverage_sigma must be positive.")
    if not isinstance(analysis.get("acceptance"), dict):
        raise SystemExit("analysis.acceptance must be an object.")

    return {
        "sample_dir": sample_dir,
        "stage_config": stage_config,
        "stereo_calibration": calibration,
        "move_axis_order": normalized_order,
        "depth_axis": depth_axis,
        "depth_lower_hardware_is_safer": lower_hardware_is_safer,
        "depth_lower_hardware_is_farther": lower_hardware_is_farther,
        "depth_safer_global_sign": safer_global_sign,
        "lateral_motion_clearance_global_mm": lateral_clearance,
        "axes": {
            axis: {
                "stage_axis": axis,
                "stage_serial": str(parsed[axis]["serial_number"]),
                "stage_direction": int(parsed[axis]["direction"]),
            }
            for axis in AXES
        },
    }


def axis_driver_inputs(
    config: dict[str, Any],
    resolved: dict[str, Any],
    axis: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Adapt one new-style axis config to the reviewed SafeOssilaAxis contract."""
    spec = dict(config["stages"]["axes"][axis])
    spec["expected_stage_serial_response"] = str(
        spec["expected_internal_serial_response"]
    )
    driver_config = {"stage": spec}
    driver_resolved = {
        "sample_dir": resolved["sample_dir"],
        "stage_axis": axis,
        "stage_serial": resolved["axes"][axis]["stage_serial"],
        "stage_direction": resolved["axes"][axis]["stage_direction"],
    }
    return driver_config, driver_resolved


def _sample(
    config: dict[str, Any],
    *,
    sequence: int,
    pass_name: str,
    data_role: str,
    group: str,
    direction: str,
    repeat_index: int,
    point_index: int,
    point: np.ndarray,
    reference_sample_id: str,
    preposition: np.ndarray | None = None,
    approach_axis: str = "",
    sweep_name: str = "",
) -> dict[str, Any]:
    role = "orientation" if group == "orientation" else "validation"
    command_stage = global_point_dict(point)
    command_hardware = hardware_point_dict(config, point)
    return {
        "sample_id": f"q{sequence:04d}",
        "sequence_index": sequence,
        "role": role,
        "trajectory": group,
        "cycle": 0 if repeat_index < 0 else repeat_index + 1,
        "approach_direction": direction,
        "command_stage_mm": command_stage,
        "command_hardware_mm": command_hardware,
        "preposition_stage_mm": (
            global_point_dict(preposition) if preposition is not None else {}
        ),
        "preposition_hardware_mm": (
            hardware_point_dict(config, preposition)
            if preposition is not None
            else {}
        ),
        "approach_axis": str(approach_axis),
        "sweep_name": str(sweep_name),
        "reference_sample_id": str(reference_sample_id),
        "pass_name": pass_name,
        "data_role": data_role,
        "validation_group": group,
        "pass_direction": direction,
        "repeat_index": repeat_index,
        "point_index": point_index,
        "target_global_mm": command_stage,
        "target_hardware_mm": command_hardware,
        "capture_status": "pending",
        "status": "pending",
        "attempts": [],
        "accepted_run_dir": "",
        "capture_readback_before_all_axes": {},
        "capture_readback_after_all_axes": {},
    }


def generate_plan(config: dict[str, Any]) -> list[dict[str, Any]]:
    experiment = config["experiment"]
    reference = numeric_triplet(
        experiment["reference_global_mm"],
        "experiment.reference_global_mm",
    )
    orientation_reference = numeric_triplet(
        experiment["orientation_reference_global_mm"],
        "experiment.orientation_reference_global_mm",
    )
    orientation = point_list(
        experiment["orientation_points_global_mm"],
        "experiment.orientation_points_global_mm",
    )
    sweeps = validation_sweeps(experiment)
    approach_offset = float(experiment["approach_offset_mm"])
    diagonal_approach_axis = str(experiment["diagonal_approach_axis"]).lower()
    samples: list[dict[str, Any]] = []
    sequence = 0

    def append(
        *,
        pass_name: str,
        data_role: str,
        group: str,
        direction: str,
        repeat_index: int,
        point_index: int,
        point: np.ndarray,
        reference_sample_id: str = "",
        preposition: np.ndarray | None = None,
        approach_axis: str = "",
        sweep_name: str = "",
    ) -> str:
        nonlocal sequence
        sequence += 1
        samples.append(
            _sample(
                config,
                sequence=sequence,
                pass_name=pass_name,
                data_role=data_role,
                group=group,
                direction=direction,
                repeat_index=repeat_index,
                point_index=point_index,
                point=point,
                reference_sample_id=reference_sample_id,
                preposition=preposition,
                approach_axis=approach_axis,
                sweep_name=sweep_name,
            )
        )
        if not samples[-1]["reference_sample_id"] and data_role.endswith("center_pre"):
            samples[-1]["reference_sample_id"] = str(samples[-1]["sample_id"])
        return str(samples[-1]["sample_id"])

    registration_reference_id = ""
    if bool(experiment["registration_center_before_after"]):
        registration_reference_id = append(
            pass_name="orientation_registration",
            data_role="registration_center_pre",
            group="orientation",
            direction="registration",
            repeat_index=-1,
            point_index=-1,
            point=orientation_reference,
        )
    for point_index, point in enumerate(orientation):
        append(
            pass_name="orientation_registration",
            data_role="orientation_registration",
            group="orientation",
            direction="registration",
            repeat_index=-1,
            point_index=point_index,
            point=point,
            reference_sample_id=registration_reference_id,
        )
    if bool(experiment["registration_center_before_after"]):
        append(
            pass_name="orientation_registration",
            data_role="registration_center_post",
            group="orientation",
            direction="registration",
            repeat_index=-1,
            point_index=len(orientation),
            point=orientation_reference,
            reference_sample_id=registration_reference_id,
        )

    directions = (
        ["positive", "negative"]
        if experiment["forward_reverse"]
        else ["positive"]
    )
    repeats = int(experiment["repeats"])
    for repeat_index in range(repeats):
        for sweep in sweeps:
            sweep_name = str(sweep["name"])
            group_name = str(sweep["group"])
            points = list(sweep["points"])
            group_reference = np.asarray(sweep["reference"], dtype=np.float64)
            for direction in directions:
                ordered = points if direction == "positive" else list(reversed(points))
                pass_name = f"repeat{repeat_index + 1:02d}_{sweep_name}_{direction}"
                pass_reference_id = append(
                    pass_name=pass_name,
                    data_role="validation_center_pre",
                    group=group_name,
                    direction=direction,
                    repeat_index=repeat_index,
                    point_index=-1,
                    point=group_reference,
                    sweep_name=sweep_name,
                )
                for point_index, point in enumerate(ordered):
                    delta = point - group_reference
                    nonzero_axes = [
                        axis
                        for axis_index, axis in enumerate(AXES)
                        if abs(float(delta[axis_index])) > 1e-9
                    ]
                    if group_name == "diagonal":
                        approach_axis = str(sweep["approach_axis"] or diagonal_approach_axis)
                    elif len(nonzero_axes) == 1:
                        approach_axis = nonzero_axes[0]
                    else:
                        raise SystemExit(
                            f"validation_{group_name}[{point_index}] does not "
                            "define one approach axis relative to its group reference."
                        )
                    approach_index = AXES.index(approach_axis)
                    preposition = point.copy()
                    preposition[approach_index] += (
                        -approach_offset
                        if direction == "positive"
                        else approach_offset
                    )
                    actual_direction = direction
                    if not _plan_point_within_limits(config, preposition):
                        if bool(experiment["forward_reverse"]):
                            # A hard endpoint can only be approached from the
                            # available side. Do not duplicate it under a false
                            # direction label.
                            continue
                        actual_direction = (
                            "negative"
                            if direction == "positive"
                            else "positive"
                        )
                        preposition = point.copy()
                        preposition[approach_index] += (
                            -approach_offset
                            if actual_direction == "positive"
                            else approach_offset
                        )
                        if not _plan_point_within_limits(config, preposition):
                            raise SystemExit(
                                f"No feasible controlled approach exists for "
                                f"validation_{group_name}[{point_index}]."
                            )
                    _validate_plan_point(
                        config,
                        preposition,
                        (
                            f"validation_{group_name}[{point_index}] "
                            f"{actual_direction} preposition"
                        ),
                    )
                    append(
                        pass_name=pass_name,
                        data_role=f"validation_{group_name}",
                        group=group_name,
                        direction=actual_direction,
                        repeat_index=repeat_index,
                        point_index=point_index,
                        point=point,
                        reference_sample_id=pass_reference_id,
                        preposition=preposition,
                        approach_axis=approach_axis,
                        sweep_name=sweep_name,
                    )
                append(
                    pass_name=pass_name,
                    data_role="validation_center_post",
                    group=group_name,
                    direction=direction,
                    repeat_index=repeat_index,
                    point_index=len(ordered),
                    point=group_reference,
                    reference_sample_id=pass_reference_id,
                    sweep_name=sweep_name,
                )
    return samples


def plan_hash(samples: Iterable[dict[str, Any]]) -> str:
    stable = []
    for item in samples:
        stable.append(
            {
                "sample_id": str(item["sample_id"]),
                "sequence_index": int(item["sequence_index"]),
                "role": str(item["role"]),
                "trajectory": str(item["trajectory"]),
                "cycle": int(item["cycle"]),
                "approach_direction": str(item["approach_direction"]),
                "approach_axis": str(item.get("approach_axis", "")),
                "sweep_name": str(item.get("sweep_name", "")),
                "reference_sample_id": str(item["reference_sample_id"]),
                "pass_name": str(item["pass_name"]),
                "data_role": str(item["data_role"]),
                "validation_group": str(item["validation_group"]),
                "pass_direction": str(item["pass_direction"]),
                "repeat_index": int(item["repeat_index"]),
                "point_index": int(item["point_index"]),
                "target_global_mm": {
                    axis: round(float(item["target_global_mm"][axis]), 9)
                    for axis in AXES
                },
                "target_hardware_mm": {
                    axis: round(float(item["target_hardware_mm"][axis]), 9)
                    for axis in AXES
                },
                "command_stage_mm": {
                    axis: round(float(item["command_stage_mm"][axis]), 9)
                    for axis in AXES
                },
                "command_hardware_mm": {
                    axis: round(float(item["command_hardware_mm"][axis]), 9)
                    for axis in AXES
                },
                "preposition_stage_mm": {
                    axis: round(
                        float(item.get("preposition_stage_mm", {}).get(axis)),
                        9,
                    )
                    for axis in AXES
                }
                if item.get("preposition_stage_mm")
                else {},
                "preposition_hardware_mm": {
                    axis: round(
                        float(item.get("preposition_hardware_mm", {}).get(axis)),
                        9,
                    )
                    for axis in AXES
                }
                if item.get("preposition_hardware_mm")
                else {},
            }
        )
    return canonical_json_sha256(stable)


def configuration_fingerprint(
    config: dict[str, Any],
    resolved: dict[str, Any],
) -> dict[str, Any]:
    stage_source = Path(resolved["sample_dir"]) / "ossila_stage" / "stage.py"
    sources = {
        "three_axis_common_source_sha256": Path(__file__).resolve(),
        "three_axis_capture_source_sha256": SCRIPT_DIR
        / "stereo_3axis_stage_accuracy_capture.py",
        "stereo_recorder_source_sha256": SCRIPT_DIR / "stereo_eventcam_record_sync.py",
        "safe_axis_source_sha256": SCRIPT_DIR / "stereo_stage_accuracy_capture.py",
        "ossila_stage_source_sha256": stage_source,
    }
    return {
        "config_sha256": canonical_json_sha256(config),
        "stage_config_sha256": file_sha256(Path(resolved["stage_config"])),
        "stereo_calibration_sha256": file_sha256(
            Path(resolved["stereo_calibration"])
        ),
        **{name: file_sha256(path) for name, path in sources.items()},
        "move_axis_order": list(resolved["move_axis_order"]),
        "depth_axis": str(resolved["depth_axis"]),
        "depth_lower_hardware_is_safer": bool(
            resolved["depth_lower_hardware_is_safer"]
        ),
        "depth_lower_hardware_is_farther": bool(
            resolved["depth_lower_hardware_is_farther"]
        ),
        "axes": {
            axis: {
                "usb_serial": str(resolved["axes"][axis]["stage_serial"]),
                "direction": int(resolved["axes"][axis]["stage_direction"]),
                "datum_mm": float(config["stages"]["axes"][axis]["datum_mm"]),
                "hardware_min_mm": float(
                    config["stages"]["axes"][axis]["hardware_min_mm"]
                ),
                "hardware_max_mm": float(
                    config["stages"]["axes"][axis]["hardware_max_mm"]
                ),
            }
            for axis in AXES
        },
        "camera_left_serial": str(config["camera"]["left_serial"]),
        "camera_right_serial": str(config["camera"]["right_serial"]),
        "camera_hw_sync": str(config["camera"]["hw_sync"]),
    }
