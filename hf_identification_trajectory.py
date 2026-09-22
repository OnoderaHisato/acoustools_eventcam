#!/usr/bin/env python3
"""Generate bounded high-frequency PAT trajectories for stereo identification.

The generated coordinates are offsets from the levitation centre. Hardware
code must add its own centre before computing AcousTools holograms.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np


AXIS_INDEX = {"x": 0, "y": 1, "z": 2}
PAT_UPDATE_BASE_HZ = 40_000

# At 40 kHz with c=343 m/s, the Gor'kov restoring-force approximation
# sin(2*k*e) reaches its first maximum at pi/(4*k) and returns to zero at
# pi/(2*k). A staircase jump must remain inside the latter boundary.
K_WAVE_MM = 2.0 * math.pi * PAT_UPDATE_BASE_HZ / 343.0 / 1000.0
FORCE_PEAK_OFFSET_MM = math.pi / (4.0 * K_WAVE_MM)
ESCAPE_OFFSET_MM = math.pi / (2.0 * K_WAVE_MM)
STEP_RESPONSE_TIERS = frozenset("ABCDEFGH")
PLAN_B_CAMPAIGN = "plan_b_20260825"
PLAN_B_STAIRCASE_FAMILIES = frozenset({"plan_b2_z_line_source"})
# Tier H is an intentional destructive diagnostic.  Keep its opt-in bypass
# narrowly bounded instead of turning the estimated escape boundary into a
# general-purpose configurable limit.
ESCAPE_BOUNDARY_PROBE_HARD_LIMIT_MM = 2.30
# Multi-axis simultaneous sine excitation (vzr identification, 2026-09-17).
# Component phases default to the reference generator's fixed sequence so an
# export reproduces the measurement-plan command numerically.
MULTITONE_DEFAULT_PHASES_RAD = (0.0, 0.7, 2.1, 3.5)
VZR_IDENTIFICATION_FAMILY = "vzr_identification"
# Precomputed commands imported numerically unchanged from an analysis-side NPZ
# (feedforward validation of the 7 mm / 10 Hz heart, 2026-09-18).
FEEDFORWARD_VALIDATION_FAMILY = "feedforward_validation"


def load_plan(path: Path) -> dict[str, Any]:
    plan = json.loads(path.read_text(encoding="utf-8"))
    if int(plan.get("schema_version", 0)) != 1:
        raise ValueError("Only trajectory plan schema_version=1 is supported")
    if not isinstance(plan.get("experiments"), list) or not plan["experiments"]:
        raise ValueError("The plan must contain a non-empty experiments array")
    return plan


def smooth_envelope(count: int, sample_hz: float, ramp_sec: float) -> np.ndarray:
    """Return a sin^2 start/stop envelope with exact zero endpoints."""
    envelope = np.ones(count, dtype=float)
    ramp_count = min(int(round(ramp_sec * sample_hz)), max(0, (count - 1) // 2))
    if ramp_count <= 0:
        return envelope
    phase = np.linspace(0.0, math.pi / 2.0, ramp_count + 1)
    edge = np.sin(phase) ** 2
    envelope[: ramp_count + 1] = edge
    envelope[-(ramp_count + 1) :] = edge[::-1]
    return envelope


def trajectory_metrics(offset_mm: np.ndarray, sample_hz: float) -> dict[str, Any]:
    if offset_mm.ndim != 2 or offset_mm.shape[1] != 3:
        raise ValueError("offset_mm must have shape (samples, 3)")
    edge_order = 2 if offset_mm.shape[0] >= 3 else 1
    velocity = np.gradient(offset_mm, 1.0 / sample_hz, axis=0, edge_order=edge_order)
    acceleration = np.gradient(velocity, 1.0 / sample_hz, axis=0, edge_order=edge_order)

    def maxima(values: np.ndarray) -> dict[str, Any]:
        return {
            "vector": float(np.max(np.linalg.norm(values, axis=1))),
            "per_axis": {
                axis: float(np.max(np.abs(values[:, index])))
                for axis, index in AXIS_INDEX.items()
            },
        }

    return {
        "max_abs_offset_mm": maxima(offset_mm),
        "max_speed_mm_s": maxima(velocity),
        "max_acceleration_mm_s2": maxima(acceleration),
    }


def _generate_chirp(
    spec: dict[str, Any], time_sec: np.ndarray
) -> tuple[np.ndarray, dict[str, Any]]:
    f0 = float(spec["f_start_hz"])
    f1 = float(spec["f_end_hz"])
    duration = max(float(time_sec[-1]), np.finfo(float).eps)
    sweep_rate = (f1 - f0) / duration
    frequency = f0 + sweep_rate * time_sec
    phase = 2.0 * math.pi * (f0 * time_sec + 0.5 * sweep_rate * time_sec**2)
    displacement_cap = float(spec["displacement_cap_mm"])
    target_acceleration = float(spec["target_acceleration_mm_s2"])
    amplitude = np.minimum(
        displacement_cap,
        target_acceleration / np.maximum((2.0 * math.pi * frequency) ** 2, 1e-12),
    )
    signal = amplitude * np.sin(phase + float(spec.get("phase_rad", 0.0)))
    return signal, {
        "instantaneous_amplitude_mm": {
            "min": float(np.min(amplitude)),
            "max": float(np.max(amplitude)),
        }
    }


def _generate_multisine(
    spec: dict[str, Any], time_sec: np.ndarray
) -> tuple[np.ndarray, dict[str, Any]]:
    frequencies = np.asarray(spec["frequencies_hz"], dtype=float)
    if frequencies.ndim != 1 or frequencies.size == 0 or np.any(frequencies <= 0):
        raise ValueError("frequencies_hz must be a non-empty array of positive values")
    rng = np.random.default_rng(int(spec.get("seed", 0)))
    phases = rng.uniform(0.0, 2.0 * math.pi, frequencies.size)
    acceleration_per_tone = float(spec["target_acceleration_mm_s2"]) / math.sqrt(
        frequencies.size
    )
    amplitudes = acceleration_per_tone / (2.0 * math.pi * frequencies) ** 2
    tone_cap = float(spec.get("tone_displacement_cap_mm", np.inf))
    amplitudes = np.minimum(amplitudes, tone_cap)
    signal = np.sum(
        amplitudes[:, None]
        * np.sin(
            2.0 * math.pi * frequencies[:, None] * time_sec[None, :]
            + phases[:, None]
        ),
        axis=0,
    )
    return signal, {
        "frequencies_hz": frequencies.tolist(),
        "phases_rad": phases.tolist(),
        "tone_amplitudes_mm_before_global_scaling": amplitudes.tolist(),
    }


def _parse_multitone_components(
    spec: dict[str, Any], sample_hz: float
) -> list[dict[str, Any]]:
    """Validate the per-axis sine components of a multitone experiment."""
    raw_components = spec.get("components")
    if not isinstance(raw_components, list) or not raw_components:
        raise ValueError("multitone experiments require a non-empty components list")
    if "axis" in spec:
        raise ValueError(
            "multitone experiments name the axis inside each component; remove the "
            "top-level axis field"
        )
    components: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_components):
        if not isinstance(raw, dict):
            raise ValueError(f"multitone component #{index} must be an object")
        axis = str(raw.get("axis", "")).lower()
        if axis not in AXIS_INDEX:
            raise ValueError(f"multitone component #{index} has unknown axis {axis!r}")
        amplitude = float(raw["amplitude_mm"])
        if not math.isfinite(amplitude) or amplitude <= 0:
            raise ValueError(
                f"multitone component #{index} amplitude_mm must be positive and finite"
            )
        has_fixed = "frequency_hz" in raw
        has_chirp = "f_start_hz" in raw or "f_end_hz" in raw
        if has_fixed == has_chirp:
            raise ValueError(
                f"multitone component #{index} needs either frequency_hz or both "
                "f_start_hz and f_end_hz"
            )
        if has_fixed:
            f_start = f_end = float(raw["frequency_hz"])
        else:
            f_start = float(raw["f_start_hz"])
            f_end = float(raw["f_end_hz"])
        for value in (f_start, f_end):
            if not math.isfinite(value) or value <= 0 or value >= sample_hz / 2.0:
                raise ValueError(
                    f"multitone component #{index} frequencies must be positive and "
                    "below Nyquist"
                )
        default_phase = MULTITONE_DEFAULT_PHASES_RAD[index % len(MULTITONE_DEFAULT_PHASES_RAD)]
        phase = float(raw.get("phase_rad", default_phase))
        if not math.isfinite(phase):
            raise ValueError(f"multitone component #{index} phase_rad must be finite")
        component: dict[str, Any] = {
            "axis": axis,
            "amplitude_mm": amplitude,
            "phase_rad": phase,
            "chirp": not has_fixed,
        }
        if has_fixed:
            component["frequency_hz"] = f_start
        else:
            component["f_start_hz"] = f_start
            component["f_end_hz"] = f_end
        components.append(component)
    return components


def _generate_multitone(
    spec: dict[str, Any], time_sec: np.ndarray, sample_hz: float, duration_sec: float
) -> tuple[np.ndarray, dict[str, Any]]:
    """Sum fixed-frequency (or linearly chirped) sines on one or more axes.

    Every component is ``amplitude * sin(phase(t) + phase_rad)`` exactly as in the
    reference generator of the 2026-09-17 vzr measurement plan; the start/stop
    envelope is applied by the caller after all components are summed.
    """
    components = _parse_multitone_components(spec, sample_hz)
    offset_mm = np.zeros((time_sec.size, 3), dtype=float)
    for component in components:
        if component["chirp"]:
            f0 = float(component["f_start_hz"])
            f1 = float(component["f_end_hz"])
            phase = 2.0 * math.pi * (
                f0 * time_sec + 0.5 * (f1 - f0) / duration_sec * time_sec * time_sec
            )
        else:
            phase = 2.0 * math.pi * float(component["frequency_hz"]) * time_sec
        offset_mm[:, AXIS_INDEX[component["axis"]]] += float(
            component["amplitude_mm"]
        ) * np.sin(phase + float(component["phase_rad"]))
    axes: list[str] = []
    for component in components:
        if component["axis"] not in axes:
            axes.append(component["axis"])
    per_axis_amplitude = {
        axis: float(
            sum(
                float(component["amplitude_mm"])
                for component in components
                if component["axis"] == axis
            )
        )
        for axis in axes
    }
    detail = {
        "components": components,
        "axes": axes,
        "per_axis_command_amplitude_mm": per_axis_amplitude,
        "envelope": "raised_cosine_ramp_both_ends",
    }
    return offset_mm, detail


def _reject_above_limits(
    metrics: dict[str, Any], limits: dict[str, Any], name: str
) -> dict[str, float]:
    """Refuse a command above its limits instead of rescaling it silently."""
    checks = (
        ("max_abs_offset_mm", "max_offset_mm"),
        ("max_speed_mm_s", "max_speed_mm_s"),
        ("max_acceleration_mm_s2", "max_acceleration_mm_s2"),
    )
    fractions: dict[str, float] = {}
    for metric_name, limit_name in checks:
        observed = float(metrics[metric_name]["vector"])
        limit = float(limits[limit_name])
        if limit <= 0:
            raise ValueError(f"Safety limit {limit_name} must be positive")
        fractions[limit_name] = observed / limit
        if observed > limit * (1.0 + 1e-12):
            raise ValueError(
                f"{name}: {metric_name} {observed:.6g} exceeds {limit_name}={limit:.6g}; "
                "multitone commands are never rescaled silently"
            )
    return fractions


def resolve_safety_limits(spec: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Return the experiment's own safety_limits, or the plan defaults.

    A per-experiment override lets one plan hold commands of very different size
    (for example 1.05 mm stiffness-check steps beside a 7 mm heart) while every
    command stays pinned to limits chosen for that command.
    """
    limits = spec.get("safety_limits", defaults["safety_limits"])
    if not isinstance(limits, dict) or not limits:
        raise ValueError("safety_limits must be a non-empty object")
    return limits


def array_sha256(values: np.ndarray) -> str:
    """Platform-independent digest of an array's float64 content."""
    return hashlib.sha256(
        np.ascontiguousarray(values, dtype="<f8").tobytes()
    ).hexdigest()


@dataclass(frozen=True)
class ImportedCommand:
    """One precomputed command loaded numerically unchanged from a source NPZ."""

    offset_mm: np.ndarray
    reference_mm: np.ndarray | None
    sample_hz: float
    source_path: Path
    source_sha256: str
    positions_sha256: str
    reference_sha256: str
    source_params: dict[str, Any]
    extra_arrays: dict[str, np.ndarray] = field(default_factory=dict)
    # Content hash of the source array before swap_axes; equals positions_sha256 without a swap.
    source_positions_sha256: str = ""
    swap_axes: tuple[str, ...] = ()


def load_imported_command(spec: dict[str, Any]) -> ImportedCommand:
    """Load and validate the source NPZ named by an ``imported`` experiment."""
    raw_path = str(spec.get("source_npz", "")).strip()
    if not raw_path:
        raise ValueError("imported experiments require source_npz")
    source_path = Path(raw_path)
    if not source_path.is_absolute():
        source_path = Path(str(spec.get("_plan_dir", "."))) / source_path
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise ValueError(f"{spec.get('name', 'imported')}: source_npz not found: {source_path}")
    positions_key = str(spec.get("positions_key", "positions_mm"))
    reference_key = str(spec.get("reference_key", "reference_mm"))
    sample_hz_key = str(spec.get("sample_hz_key", "sample_hz"))
    extra_keys = [str(key) for key in spec.get("extra_time_keys", [])]
    name = str(spec.get("name", source_path.stem))
    with np.load(source_path, allow_pickle=False) as data:
        if positions_key not in data.files:
            raise ValueError(f"{name}: {source_path.name} has no array {positions_key!r}")
        if sample_hz_key not in data.files:
            raise ValueError(f"{name}: {source_path.name} has no {sample_hz_key!r}")
        offset_mm = np.array(data[positions_key], dtype=float)
        sample_hz = float(data[sample_hz_key])
        reference_mm = (
            np.array(data[reference_key], dtype=float) if reference_key in data.files else None
        )
        extra_arrays: dict[str, np.ndarray] = {}
        for key in extra_keys:
            if key not in data.files:
                raise ValueError(f"{name}: {source_path.name} has no array {key!r}")
            extra_arrays[key] = np.array(data[key], dtype=float)
        source_params: dict[str, Any] = {}
        if "params" in data.files:
            try:
                parsed = json.loads(str(data["params"]))
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                source_params = parsed
    if offset_mm.ndim != 2 or offset_mm.shape[1] != 3 or offset_mm.shape[0] < 3:
        raise ValueError(f"{name}: {positions_key} must have shape (samples>=3, 3)")
    if not np.all(np.isfinite(offset_mm)):
        raise ValueError(f"{name}: {positions_key} contains non-finite values")
    if reference_mm is not None:
        if reference_mm.shape != offset_mm.shape:
            raise ValueError(f"{name}: {reference_key} shape differs from {positions_key}")
        if not np.all(np.isfinite(reference_mm)):
            raise ValueError(f"{name}: {reference_key} contains non-finite values")
    for key, values in extra_arrays.items():
        if values.shape != (offset_mm.shape[0],) or not np.all(np.isfinite(values)):
            raise ValueError(f"{name}: {key} must be a finite array of one value per sample")
    positions_sha256 = array_sha256(offset_mm)
    expected = str(spec.get("positions_sha256", "")).strip().lower()
    if expected and expected != positions_sha256:
        raise ValueError(
            f"{name}: {positions_key} content hash {positions_sha256} does not match the "
            f"pinned positions_sha256 {expected}; the source file is not the reviewed command"
        )
    # Optional: play the same reviewed command in another plane, e.g. ["x", "y"] turns an
    # XZ-plane command into a YZ-plane one. The pin above still refers to the source file.
    source_positions_sha256 = positions_sha256
    swap_axes = tuple(str(axis).lower() for axis in spec.get("swap_axes", ()))
    if swap_axes:
        if len(swap_axes) != 2 or swap_axes[0] == swap_axes[1] or any(
            axis not in AXIS_INDEX for axis in swap_axes
        ):
            raise ValueError(f"{name}: swap_axes must name two different axes out of x, y, z")
        order = [0, 1, 2]
        first, second = AXIS_INDEX[swap_axes[0]], AXIS_INDEX[swap_axes[1]]
        order[first], order[second] = second, first
        offset_mm = np.ascontiguousarray(offset_mm[:, order])
        if reference_mm is not None:
            reference_mm = np.ascontiguousarray(reference_mm[:, order])
        positions_sha256 = array_sha256(offset_mm)
    return ImportedCommand(
        offset_mm=offset_mm,
        reference_mm=reference_mm,
        sample_hz=sample_hz,
        source_path=source_path,
        source_sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
        positions_sha256=positions_sha256,
        reference_sha256="" if reference_mm is None else array_sha256(reference_mm),
        source_params=source_params,
        extra_arrays=extra_arrays,
        source_positions_sha256=source_positions_sha256,
        swap_axes=swap_axes,
    )


def _check_imported_limits(
    offset_mm: np.ndarray,
    reference_mm: np.ndarray | None,
    limits: dict[str, Any],
    name: str,
) -> dict[str, Any]:
    """Endpoint and command-to-reference checks that only make sense for imports."""
    endpoint = float(
        max(np.linalg.norm(offset_mm[0]), np.linalg.norm(offset_mm[-1]))
    )
    detail: dict[str, Any] = {"max_endpoint_offset_mm": endpoint}
    if "max_endpoint_offset_mm" in limits:
        limit = float(limits["max_endpoint_offset_mm"])
        if endpoint > limit * (1.0 + 1e-12):
            raise ValueError(
                f"{name}: command starts or ends {endpoint:.6g} mm from the centre, above "
                f"max_endpoint_offset_mm={limit:.6g}"
            )
    if reference_mm is not None:
        distance = float(np.max(np.linalg.norm(offset_mm - reference_mm, axis=1)))
        detail["max_command_reference_distance_mm"] = distance
        if "max_command_reference_distance_mm" in limits:
            limit = float(limits["max_command_reference_distance_mm"])
            if distance > limit * (1.0 + 1e-12):
                raise ValueError(
                    f"{name}: command departs {distance:.6g} mm from its reference, above "
                    f"max_command_reference_distance_mm={limit:.6g}"
                )
    return detail


def _generate_imported(
    spec: dict[str, Any], defaults: dict[str, Any], limits: dict[str, Any]
) -> tuple[np.ndarray, dict[str, Any]]:
    name = str(spec["name"])
    command = load_imported_command(spec)
    sample_hz = float(spec.get("sample_hz", defaults["sample_hz"]))
    if abs(command.sample_hz - sample_hz) > 1e-9:
        raise ValueError(
            f"{name}: source sample_hz={command.sample_hz:g} differs from the plan's {sample_hz:g}"
        )
    divider = PAT_UPDATE_BASE_HZ / sample_hz
    duration_sec = float(spec.get("duration_sec", defaults["duration_sec"]))
    expected_count = int(round(duration_sec * sample_hz))
    offset_mm = command.offset_mm
    if offset_mm.shape[0] != expected_count:
        raise ValueError(
            f"{name}: source has {offset_mm.shape[0]} samples but duration_sec={duration_sec:g} "
            f"at {sample_hz:g} Hz requires {expected_count}"
        )
    metrics = trajectory_metrics(offset_mm, sample_hz)
    fractions = _reject_above_limits(metrics, limits, name)
    import_checks = _check_imported_limits(offset_mm, command.reference_mm, limits, name)
    axes = [
        axis for axis, index in AXIS_INDEX.items() if np.any(offset_mm[:, index] != 0.0)
    ]
    detail = {
        "source_file": command.source_path.name,
        "source_sha256": command.source_sha256,
        "positions_key": str(spec.get("positions_key", "positions_mm")),
        "positions_sha256": command.positions_sha256,
        "positions_sha256_pinned": bool(str(spec.get("positions_sha256", "")).strip()),
        "reference_available": command.reference_mm is not None,
        "reference_sha256": command.reference_sha256,
        "extra_arrays": sorted(command.extra_arrays),
        "source_params": command.source_params,
        "axes": axes,
        **import_checks,
    }
    if command.swap_axes:
        detail["swap_axes"] = list(command.swap_axes)
        detail["source_positions_sha256"] = command.source_positions_sha256
    metadata = {
        "name": name,
        "kind": "imported",
        "axis": "".join(axes) or "none",
        "axes": axes,
        "enabled": bool(spec.get("enabled", True)),
        "sample_hz": sample_hz,
        "duration_sec": offset_mm.shape[0] / sample_hz,
        "samples": int(offset_mm.shape[0]),
        "pat_divider": int(round(divider)),
        "ramp_sec": float(spec.get("ramp_sec", defaults["ramp_sec"])),
        "safety_scale_applied": 1.0,
        "safety_policy": "reject_above_limits",
        "safety_limit_fractions": fractions,
        "safety_limits": limits,
        "metrics": metrics,
        "generation_detail": detail,
    }
    _copy_experiment_metadata(spec, metadata)
    return offset_mm, metadata


def _generate_staircase(
    spec: dict[str, Any], sample_hz: float
) -> tuple[np.ndarray, dict[str, Any]]:
    """Generate centre-to-target-to-centre trap jumps with exact flat holds."""
    hold_sec = float(spec["hold_sec"])
    initial_hold_sec = float(spec.get("initial_hold_sec", hold_sec))
    final_hold_sec = float(spec.get("final_hold_sec", hold_sec))
    if min(hold_sec, initial_hold_sec, final_hold_sec) <= 0:
        raise ValueError("staircase hold durations must be positive")
    if min(
        round(hold_sec * sample_hz),
        round(initial_hold_sec * sample_hz),
        round(final_hold_sec * sample_hz),
    ) < 2:
        raise ValueError("staircase hold durations are too short for the requested sample_hz")

    if "level_sequence_mm" in spec:
        return _generate_level_sequence_staircase(
            spec, sample_hz, hold_sec, initial_hold_sec, final_hold_sec
        )

    amplitudes = [float(value) for value in spec["amplitudes_mm"]]
    if not amplitudes or any(not math.isfinite(value) or value <= 0 for value in amplitudes):
        raise ValueError("amplitudes_mm must be a non-empty list of positive finite values")
    directions = [int(value) for value in spec.get("directions", (1, -1))]
    if not directions or any(value not in (1, -1) for value in directions):
        raise ValueError("directions must be a non-empty list containing only +1 and -1")

    axis = str(spec.get("axis", "x")).lower()
    if axis not in AXIS_INDEX:
        raise ValueError(f"Unknown axis: {axis}")
    repeats = int(spec.get("repeats_within_run", 1))
    if repeats <= 0:
        raise ValueError("repeats_within_run must be positive")

    targets = [
        direction * amplitude for amplitude in amplitudes for direction in directions
    ]
    order_seed = spec.get("order_seed")
    schedule: list[float] = []
    for repeat in range(repeats):
        block = list(targets)
        if order_seed is not None:
            rng = np.random.default_rng(int(order_seed) + repeat)
            rng.shuffle(block)
        for target in block:
            schedule.extend((float(target), 0.0))

    holds: list[tuple[float, int]] = [
        (0.0, int(round(initial_hold_sec * sample_hz)))
    ]
    for index, level in enumerate(schedule):
        duration = final_hold_sec if index == len(schedule) - 1 else hold_sec
        holds.append((level, int(round(duration * sample_hz))))

    signal = np.concatenate(
        [np.full(count, level, dtype=float) for level, count in holds]
    )
    offset_mm = np.zeros((signal.size, 3), dtype=float)
    offset_mm[:, AXIS_INDEX[axis]] = signal
    levels = np.asarray([level for level, _ in holds], dtype=float)
    steps = np.abs(np.diff(levels))
    jump_indices = np.cumsum([count for _, count in holds])[:-1]
    detail = {
        "hold_sec": hold_sec,
        "initial_hold_sec": initial_hold_sec,
        "final_hold_sec": final_hold_sec,
        "levels_mm": levels.tolist(),
        "n_holds": len(holds),
        "n_jumps": int(steps.size),
        "max_step_mm": float(np.max(steps)) if steps.size else 0.0,
        "step_sizes_mm": sorted({round(float(value), 6) for value in steps}),
        "jump_sample_indices": jump_indices.tolist(),
        "jump_times_sec": (jump_indices / sample_hz).tolist(),
        "force_peak_offset_mm": FORCE_PEAK_OFFSET_MM,
        "escape_offset_mm": ESCAPE_OFFSET_MM,
    }
    return offset_mm, detail


def _generate_level_sequence_staircase(
    spec: dict[str, Any],
    sample_hz: float,
    hold_sec: float,
    initial_hold_sec: float,
    final_hold_sec: float,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Generate flat holds at explicit [x, y, z] levels, so several axes may jump at once.

    level_sequence_mm lists the holds of one block after the initial centre hold.
    The block is repeated repeats_within_run times, so it has to end at the centre.
    """
    for key in ("amplitudes_mm", "directions", "order_seed"):
        if key in spec:
            raise ValueError(f"level_sequence_mm cannot be combined with {key}")
    block = np.asarray(spec["level_sequence_mm"], dtype=float)
    if block.ndim != 2 or block.shape[1] != 3 or block.shape[0] < 1:
        raise ValueError("level_sequence_mm must be a non-empty list of [x, y, z] levels")
    if not np.all(np.isfinite(block)):
        raise ValueError("level_sequence_mm must contain only finite values")
    if np.any(block[-1] != 0.0):
        raise ValueError("level_sequence_mm must end at the centre [0, 0, 0]")
    repeats = int(spec.get("repeats_within_run", 1))
    if repeats <= 0:
        raise ValueError("repeats_within_run must be positive")

    # "+ 0.0" turns -0.0 into 0.0 so equal positions share one hologram downstream.
    levels = np.vstack([np.zeros((1, 3)), np.tile(block, (repeats, 1))]) + 0.0
    changes = np.diff(levels, axis=0)
    steps = np.linalg.norm(changes, axis=1)
    if np.any(steps == 0.0):
        raise ValueError("level_sequence_mm must not hold the same level twice in a row")
    axes = [name for name, index in AXIS_INDEX.items() if np.any(levels[:, index] != 0.0)]
    if not axes:
        raise ValueError("level_sequence_mm never leaves the centre")
    axis = "".join(axes)
    if "axis" in spec and str(spec["axis"]).lower() != axis:
        raise ValueError(
            f"axis={spec['axis']!r} does not match the axes driven by level_sequence_mm ({axis!r})"
        )

    counts = np.full(levels.shape[0], int(round(hold_sec * sample_hz)), dtype=int)
    counts[0] = int(round(initial_hold_sec * sample_hz))
    counts[-1] = int(round(final_hold_sec * sample_hz))
    offset_mm = np.repeat(levels, counts, axis=0)
    jump_indices = np.cumsum(counts)[:-1]
    jump_kinds = [
        "".join(name for name, index in AXIS_INDEX.items() if change[index] != 0.0)
        for change in changes
    ]
    detail = {
        "hold_sec": hold_sec,
        "initial_hold_sec": initial_hold_sec,
        "final_hold_sec": final_hold_sec,
        "axis": axis,
        "axes": axes,
        "level_sequence_mm": block.tolist(),
        "repeats_within_run": repeats,
        "levels_xyz_mm": levels.tolist(),
        "n_holds": int(levels.shape[0]),
        "n_jumps": int(steps.size),
        "max_step_mm": float(np.max(steps)),
        "step_sizes_mm": sorted({round(float(value), 6) for value in steps}),
        "jump_kinds": jump_kinds,
        "jump_kind_counts": {kind: jump_kinds.count(kind) for kind in sorted(set(jump_kinds))},
        "jump_sample_indices": jump_indices.tolist(),
        "jump_times_sec": (jump_indices / sample_hz).tolist(),
        "force_peak_offset_mm": FORCE_PEAK_OFFSET_MM,
        "escape_offset_mm": ESCAPE_OFFSET_MM,
    }
    return offset_mm, detail


def _staircase_safety_check(
    offset_mm: np.ndarray,
    limits: dict[str, Any],
    *,
    allow_escape_boundary_probe: bool = False,
) -> dict[str, Any]:
    """Check staircase displacement without differentiating its intentional jumps."""
    if offset_mm.ndim != 2 or offset_mm.shape[1] != 3 or offset_mm.shape[0] < 2:
        raise ValueError("staircase offset_mm must have shape (samples>=2, 3)")
    max_offset = float(np.max(np.linalg.norm(offset_mm, axis=1)))
    steps = np.diff(offset_mm, axis=0)
    max_step = float(np.max(np.linalg.norm(steps, axis=1)))
    offset_limit = float(limits["max_offset_mm"])
    step_limit = float(limits.get("max_step_mm", offset_limit))
    if min(offset_limit, step_limit) <= 0:
        raise ValueError("staircase max_offset_mm and max_step_mm must be positive")
    if max_offset > offset_limit * (1.0 + 1e-12):
        raise ValueError(
            f"staircase commanded offset {max_offset:.3f} mm exceeds "
            f"max_offset_mm={offset_limit:.3f}"
        )
    if max_step > step_limit * (1.0 + 1e-12):
        raise ValueError(
            f"staircase jump {max_step:.3f} mm exceeds max_step_mm={step_limit:.3f}"
        )
    escape_boundary_exceeded = max_step >= ESCAPE_OFFSET_MM
    if escape_boundary_exceeded and not allow_escape_boundary_probe:
        raise ValueError(
            f"staircase jump {max_step:.3f} mm is at or beyond the estimated "
            f"escape boundary {ESCAPE_OFFSET_MM:.3f} mm"
        )
    if allow_escape_boundary_probe and max_step > ESCAPE_BOUNDARY_PROBE_HARD_LIMIT_MM * (1.0 + 1e-12):
        raise ValueError(
            f"escape-boundary probe jump {max_step:.3f} mm exceeds the hard probe cap "
            f"{ESCAPE_BOUNDARY_PROBE_HARD_LIMIT_MM:.3f} mm"
        )
    return {
        "max_abs_offset_mm": max_offset,
        "max_step_mm": max_step,
        "max_offset_limit_mm": offset_limit,
        "max_step_limit_mm": step_limit,
        "escape_offset_mm": ESCAPE_OFFSET_MM,
        "escape_margin_fraction": max_step / ESCAPE_OFFSET_MM,
        "escape_boundary_exceeded": escape_boundary_exceeded,
        "escape_boundary_probe_enabled": bool(allow_escape_boundary_probe),
        "escape_boundary_probe_hard_limit_mm": ESCAPE_BOUNDARY_PROBE_HARD_LIMIT_MM,
        "force_peak_offset_mm": FORCE_PEAK_OFFSET_MM,
        "derivative_limits_applied": False,
        "derivative_limits_reason": "intentional_discontinuous_trap_command",
    }


def _safety_scale(
    offset_mm: np.ndarray, sample_hz: float, limits: dict[str, Any]
) -> tuple[np.ndarray, float]:
    metrics = trajectory_metrics(offset_mm, sample_hz)
    candidates = [1.0]
    checks = (
        ("max_abs_offset_mm", "max_offset_mm"),
        ("max_speed_mm_s", "max_speed_mm_s"),
        ("max_acceleration_mm_s2", "max_acceleration_mm_s2"),
    )
    for metric_name, limit_name in checks:
        observed = float(metrics[metric_name]["vector"])
        limit = float(limits[limit_name])
        if limit <= 0:
            raise ValueError(f"Safety limit {limit_name} must be positive")
        if observed > 0:
            candidates.append(limit / observed)
    scale = min(candidates)
    return offset_mm * scale, float(scale)


def generate_trajectory(
    spec: dict[str, Any], defaults: dict[str, Any]
) -> tuple[np.ndarray, dict[str, Any]]:
    sample_hz = float(spec.get("sample_hz", defaults["sample_hz"]))
    duration_sec = float(spec.get("duration_sec", defaults["duration_sec"]))
    if sample_hz <= 0 or duration_sec <= 0:
        raise ValueError("sample_hz and duration_sec must be positive")
    divider = PAT_UPDATE_BASE_HZ / sample_hz
    if abs(divider - round(divider)) > 1e-9:
        raise ValueError(
            f"sample_hz={sample_hz:g} must divide the {PAT_UPDATE_BASE_HZ} Hz PAT base rate"
        )

    count = max(3, int(round(duration_sec * sample_hz)))
    time_sec = np.arange(count, dtype=float) / sample_hz
    kind = str(spec["kind"]).lower()
    limits = resolve_safety_limits(spec, defaults)
    detail: dict[str, Any] = {}
    if kind == "imported":
        return _generate_imported(spec, defaults, limits)
    if kind == "multitone":
        ramp_sec = float(spec.get("ramp_sec", defaults["ramp_sec"]))
        offset_mm, detail = _generate_multitone(
            spec, time_sec, sample_hz, count / sample_hz
        )
        offset_mm *= smooth_envelope(count, sample_hz, ramp_sec)[:, None]
        metrics = trajectory_metrics(offset_mm, sample_hz)
        fractions = _reject_above_limits(metrics, limits, str(spec["name"]))
        metadata = {
            "name": str(spec["name"]),
            "kind": kind,
            "axis": "".join(detail["axes"]),
            "axes": list(detail["axes"]),
            "enabled": bool(spec.get("enabled", True)),
            "sample_hz": sample_hz,
            "duration_sec": count / sample_hz,
            "samples": count,
            "pat_divider": int(round(divider)),
            "ramp_sec": ramp_sec,
            "safety_scale_applied": 1.0,
            "safety_policy": "reject_above_limits",
            "safety_limit_fractions": fractions,
            "safety_limits": limits,
            "metrics": metrics,
            "generation_detail": detail,
        }
        _copy_experiment_metadata(spec, metadata)
        return offset_mm, metadata
    multi_axis_staircase = kind == "staircase" and "level_sequence_mm" in spec
    axis = str(spec.get("axis", "x")).lower()
    if axis not in AXIS_INDEX and not multi_axis_staircase:
        raise ValueError(f"Unknown axis: {axis}")
    if kind == "staircase":
        tier = str(spec.get("tier", "")).strip().upper()
        measurement_family = str(spec.get("measurement_family", "")).strip()
        is_step_response = bool(tier)
        if is_step_response and tier not in STEP_RESPONSE_TIERS:
            raise ValueError("step-response staircase experiments require tier=A through H")
        if is_step_response:
            if measurement_family and measurement_family != "step_response_identification":
                raise ValueError(
                    "tiered staircase experiments must use "
                    "measurement_family=step_response_identification"
                )
            measurement_family = "step_response_identification"
        elif measurement_family not in PLAN_B_STAIRCASE_FAMILIES:
            raise ValueError(
                "non-tiered staircase experiments require an explicitly supported "
                "measurement_family"
            )
        allow_escape_boundary_probe = bool(
            spec.get("allow_escape_boundary_probe", False)
        )
        if allow_escape_boundary_probe and (not is_step_response or tier != "H"):
            raise ValueError(
                "allow_escape_boundary_probe is reserved for tier H staircase experiments"
            )
        if tier == "H" and not allow_escape_boundary_probe:
            raise ValueError(
                "tier H staircase experiments require allow_escape_boundary_probe=true"
            )
        offset_mm, detail = _generate_staircase(spec, sample_hz)
        if multi_axis_staircase:
            axis = detail["axis"]
        detail["safety"] = _staircase_safety_check(
            offset_mm,
            limits,
            allow_escape_boundary_probe=allow_escape_boundary_probe,
        )
        metrics = trajectory_metrics(offset_mm, sample_hz)
        metadata = {
            "name": str(spec["name"]),
            "kind": kind,
            "axis": axis,
            "enabled": bool(spec.get("enabled", True)),
            "measurement_family": measurement_family,
            "escape_boundary_probe_enabled": allow_escape_boundary_probe,
            "sample_hz": sample_hz,
            "duration_sec": offset_mm.shape[0] / sample_hz,
            "samples": int(offset_mm.shape[0]),
            "pat_divider": int(round(divider)),
            "ramp_sec": 0.0,
            "safety_scale_applied": 1.0,
            "safety_limits": limits,
            "metrics": metrics,
            "derivative_metrics_interpretable": False,
            "generation_detail": detail,
        }
        if multi_axis_staircase:
            metadata["axes"] = list(detail["axes"])
        if is_step_response:
            metadata["step_response_tier"] = tier
        _copy_experiment_metadata(spec, metadata)
        return offset_mm, metadata
    if kind == "static":
        signal = np.zeros(count, dtype=float)
    elif kind == "chirp":
        if max(float(spec["f_start_hz"]), float(spec["f_end_hz"])) >= sample_hz / 2.0:
            raise ValueError("Chirp frequency must be below Nyquist")
        signal, detail = _generate_chirp(spec, time_sec)
    elif kind == "multisine":
        if max(float(value) for value in spec["frequencies_hz"]) >= sample_hz / 2.0:
            raise ValueError("Multisine frequencies must be below Nyquist")
        signal, detail = _generate_multisine(spec, time_sec)
    else:
        raise ValueError(f"Unknown trajectory kind: {kind}")

    signal *= smooth_envelope(
        count, sample_hz, float(spec.get("ramp_sec", defaults["ramp_sec"]))
    )
    offset_mm = np.zeros((count, 3), dtype=float)
    offset_mm[:, AXIS_INDEX[axis]] = signal
    offset_mm, scale = _safety_scale(offset_mm, sample_hz, limits)
    metrics = trajectory_metrics(offset_mm, sample_hz)
    metadata = {
        "name": str(spec["name"]),
        "kind": kind,
        "axis": axis,
        "enabled": bool(spec.get("enabled", True)),
        "sample_hz": sample_hz,
        "duration_sec": count / sample_hz,
        "samples": count,
        "pat_divider": int(round(divider)),
        "ramp_sec": float(spec.get("ramp_sec", defaults["ramp_sec"])),
        "safety_scale_applied": scale,
        "safety_limits": limits,
        "metrics": metrics,
        "generation_detail": detail,
    }
    _copy_experiment_metadata(spec, metadata)
    return offset_mm, metadata


def _copy_experiment_metadata(
    spec: dict[str, Any], metadata: dict[str, Any]
) -> None:
    """Preserve acquisition semantics needed after a command is exported.

    The numeric command remains the authority for playback. These fields are
    deliberately limited to JSON-compatible campaign metadata used by safety
    gates, operator checkpoints, and the recording manifest.
    """
    fields = (
        "campaign",
        "measurement_family",
        "condition",
        "note",
        "escalation_rank",
        "response_gate",
        "drive_amplitude_scale",
        "particle_id",
        "operator_checkpoint",
        "requires_operator_context",
        "warmup_definition",
        "nominal_active_warmup_minutes",
        "reference",
        "feedforward_design",
    )
    for field in fields:
        if field in spec:
            metadata[field] = spec[field]


def positions_about_center(
    offset_mm: np.ndarray, center_m: tuple[float, float, float]
) -> np.ndarray:
    center = np.asarray(center_m, dtype=float)
    if center.shape != (3,):
        raise ValueError("center_m must contain exactly three coordinates")
    return center[None, :] + np.asarray(offset_mm, dtype=float) * 1e-3


def load_trajectory_for_hardware(
    path: Path,
    center_m: tuple[float, float, float],
    command_scale: float = 1.0,
) -> tuple[list[tuple[float, float, float]], float, dict[str, Any]]:
    """Load an export, apply a test scale, recheck limits, and add the PAT centre."""
    if not 0.0 < command_scale <= 1.0:
        raise ValueError("command_scale must be in the interval (0, 1]")
    with np.load(path) as data:
        offset_mm = np.asarray(data["offset_mm"], dtype=float) * float(command_scale)
        sample_hz = float(data["sample_hz"])
    metadata_path = path.with_name("trajectory_metadata.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    observed = trajectory_metrics(offset_mm, sample_hz)
    limits = metadata["safety_limits"]
    hardware_safety: dict[str, Any] | None = None
    if str(metadata.get("kind", "")).lower() == "staircase":
        hardware_safety = _staircase_safety_check(
            offset_mm,
            limits,
            allow_escape_boundary_probe=bool(
                metadata.get("escape_boundary_probe_enabled", False)
            ),
        )
    else:
        comparisons = (
            ("max_abs_offset_mm", "max_offset_mm"),
            ("max_speed_mm_s", "max_speed_mm_s"),
            ("max_acceleration_mm_s2", "max_acceleration_mm_s2"),
        )
        for metric_name, limit_name in comparisons:
            if observed[metric_name]["vector"] > float(limits[limit_name]) * (1.0 + 1e-9):
                raise ValueError(f"Exported trajectory exceeds {limit_name} after loading")
        if str(metadata.get("kind", "")).lower() == "imported":
            expected_digest = str(
                metadata.get("generation_detail", {}).get("positions_sha256", "")
            )
            with np.load(path) as data:
                unscaled = np.asarray(data["offset_mm"], dtype=float)
                reference_mm = (
                    np.asarray(data["reference_mm"], dtype=float) * float(command_scale)
                    if "reference_mm" in data.files
                    else None
                )
            if expected_digest and array_sha256(unscaled) != expected_digest:
                raise ValueError(
                    "Imported command_trajectory.npz no longer matches the positions_sha256 "
                    "recorded in trajectory_metadata.json"
                )
            hardware_safety = _check_imported_limits(
                offset_mm, reference_mm, limits, str(metadata.get("name", path.parent.name))
            )
    metadata = dict(metadata)
    metadata["hardware_command_scale"] = float(command_scale)
    metadata["hardware_load_metrics"] = observed
    if hardware_safety is not None:
        metadata["hardware_load_safety"] = hardware_safety
    positions = positions_about_center(offset_mm, center_m)
    return [tuple(float(value) for value in row) for row in positions], sample_hz, metadata


def load_reference_for_hardware(
    path: Path,
    center_m: tuple[float, float, float],
    command_scale: float = 1.0,
) -> list[tuple[float, float, float]] | None:
    """Return the desired trajectory stored beside an imported command, if any.

    The reference is what the particle should follow; the command may differ from
    it on purpose (offline feedforward). It is scaled and centred exactly like the
    command so both logs share one coordinate frame.
    """
    if not 0.0 < command_scale <= 1.0:
        raise ValueError("command_scale must be in the interval (0, 1]")
    with np.load(path) as data:
        if "reference_mm" not in data.files:
            return None
        reference_mm = np.asarray(data["reference_mm"], dtype=float) * float(command_scale)
    positions = positions_about_center(reference_mm, center_m)
    return [tuple(float(value) for value in row) for row in positions]


def write_offset_log(
    path: Path, offset_mm: np.ndarray, sample_hz: float
) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "offset_x_m", "offset_y_m", "offset_z_m"])
        for index, row in enumerate(offset_mm * 1e-3):
            writer.writerow(
                [f"{index / sample_hz:.9f}", *(f"{value:.9f}" for value in row)]
            )


def write_preview(
    path: Path,
    offset_mm: np.ndarray,
    metadata: dict[str, Any],
    reference_mm: np.ndarray | None = None,
) -> None:
    import matplotlib.pyplot as plt

    time_sec = np.arange(offset_mm.shape[0]) / float(metadata["sample_hz"])
    colors = ("#0072B2", "#D55E00", "#009E73")
    if str(metadata["kind"]).lower() == "multitone":
        _write_multitone_preview(path, offset_mm, metadata, time_sec, colors)
        return
    if str(metadata["kind"]).lower() == "imported":
        _write_imported_preview(path, offset_mm, metadata, time_sec, colors, reference_mm)
        return
    figure, axes = plt.subplots(2, 1, figsize=(16, 6), constrained_layout=True)
    for index, axis in enumerate(("x", "y", "z")):
        axes[0].plot(
            time_sec,
            offset_mm[:, index],
            color=colors[index],
            linewidth=0.8,
            label=axis,
        )
    axes[0].set_ylabel("Command offset [mm]")
    axes[0].legend(ncol=3, loc="upper right")
    axes[0].grid(alpha=0.25)
    if str(metadata["kind"]).lower() == "staircase":
        detail = metadata["generation_detail"]
        driven = [name for name in metadata.get("axes", ()) if name in AXIS_INDEX]
        if len(driven) > 1:
            for name in driven:
                axes[1].step(
                    time_sec, offset_mm[:, AXIS_INDEX[name]], color=colors[AXIS_INDEX[name]],
                    linewidth=0.9, where="post", label=f"command {name}",
                )
        else:
            axes[1].step(
                time_sec, offset_mm[:, AXIS_INDEX[metadata["axis"]]], color="#CC79A7",
                linewidth=0.9, where="post", label="command",
            )
        for sign, label in ((1.0, "escape boundary"), (-1.0, None)):
            axes[1].axhline(
                sign * ESCAPE_OFFSET_MM, color="black", linestyle="--",
                linewidth=0.8, label=label,
            )
        for sign, label in ((1.0, "force peak"), (-1.0, None)):
            axes[1].axhline(
                sign * FORCE_PEAK_OFFSET_MM, color="#009E73", linestyle=":",
                linewidth=0.8, label=label,
            )
        axes[1].set_xlabel("Time [s]")
        axes[1].set_ylabel("Command offset [mm]")
        axes[1].set_title(
            f"{detail['n_jumps']} jumps, max step {detail['max_step_mm']:.3f} mm "
            f"({100.0 * detail['safety']['escape_margin_fraction']:.1f}% of escape boundary)"
        )
        axes[1].legend(loc="upper right", ncol=3, fontsize=8)
        axes[1].grid(alpha=0.25)
        figure.suptitle(metadata["name"])
        figure.savefig(path, dpi=180)
        plt.close(figure)
        return
    active = offset_mm[:, AXIS_INDEX[metadata["axis"]]]
    edge_order = 2 if active.size >= 3 else 1
    acceleration = np.gradient(
        np.gradient(
            active, 1.0 / metadata["sample_hz"], edge_order=edge_order
        ),
        1.0 / metadata["sample_hz"],
        edge_order=edge_order,
    )
    axes[1].plot(
        time_sec,
        acceleration,
        color="#CC79A7",
        linewidth=0.7,
        label="command acceleration",
    )
    limit = float(metadata["safety_limits"]["max_acceleration_mm_s2"])
    axes[1].axhline(
        limit, color="black", linestyle="--", linewidth=0.8, label="safety limit"
    )
    axes[1].axhline(-limit, color="black", linestyle="--", linewidth=0.8)
    axes[1].set_xlabel("Time [s]")
    axes[1].set_ylabel("Acceleration [mm/s²]")
    axes[1].legend(loc="upper right")
    axes[1].grid(alpha=0.25)
    figure.suptitle(metadata["name"])
    figure.savefig(path, dpi=180)
    plt.close(figure)


def _write_multitone_preview(
    path: Path,
    offset_mm: np.ndarray,
    metadata: dict[str, Any],
    time_sec: np.ndarray,
    colors: tuple[str, str, str],
) -> None:
    import matplotlib.pyplot as plt

    sample_hz = float(metadata["sample_hz"])
    detail = metadata["generation_detail"]
    driven = [str(axis) for axis in detail["axes"]]
    figure, axes = plt.subplots(3, 1, figsize=(16, 9), constrained_layout=True)
    for index, axis in enumerate(("x", "y", "z")):
        axes[0].plot(
            time_sec, offset_mm[:, index], color=colors[index], linewidth=0.6, label=axis
        )
    axes[0].set_ylabel("Command offset [mm]")
    axes[0].legend(ncol=3, loc="upper right")
    axes[0].grid(alpha=0.25)
    axes[0].set_title("Full command (all axes)")

    zoom_sec = 0.1
    zoom_start = max(0.0, 0.5 * float(time_sec[-1]) - 0.5 * zoom_sec)
    zoom_mask = (time_sec >= zoom_start) & (time_sec <= zoom_start + zoom_sec)
    for axis in driven:
        index = AXIS_INDEX[axis]
        axes[1].plot(
            time_sec[zoom_mask],
            offset_mm[zoom_mask, index],
            color=colors[index],
            linewidth=1.0,
            marker=".",
            markersize=2,
            label=axis,
        )
    axes[1].set_ylabel("Command offset [mm]")
    axes[1].set_title(f"{zoom_sec:g} s window at mid-run (driven axes, individual samples)")
    axes[1].legend(ncol=3, loc="upper right")
    axes[1].grid(alpha=0.25)

    edge_order = 2 if offset_mm.shape[0] >= 3 else 1
    velocity = np.gradient(offset_mm, 1.0 / sample_hz, axis=0, edge_order=edge_order)
    acceleration = np.gradient(velocity, 1.0 / sample_hz, axis=0, edge_order=edge_order)
    for axis in driven:
        index = AXIS_INDEX[axis]
        axes[2].plot(
            time_sec, acceleration[:, index], color=colors[index], linewidth=0.5, label=axis
        )
    axes[2].plot(
        time_sec,
        np.linalg.norm(acceleration, axis=1),
        color="#CC79A7",
        linewidth=0.5,
        label="|a|",
    )
    limit = float(metadata["safety_limits"]["max_acceleration_mm_s2"])
    axes[2].axhline(limit, color="black", linestyle="--", linewidth=0.8, label="safety limit")
    axes[2].axhline(-limit, color="black", linestyle="--", linewidth=0.8)
    axes[2].set_xlabel("Time [s]")
    axes[2].set_ylabel("Acceleration [mm/s^2]")
    axes[2].legend(loc="upper right", ncol=5, fontsize=8)
    axes[2].grid(alpha=0.25)

    parts = []
    for component in detail["components"]:
        if component.get("chirp"):
            frequency = f"{component['f_start_hz']:g}->{component['f_end_hz']:g} Hz"
        else:
            frequency = f"{component['frequency_hz']:g} Hz"
        parts.append(f"{component['axis']} {component['amplitude_mm']:g} mm @ {frequency}")
    metrics = metadata["metrics"]
    figure.suptitle(
        f"{metadata['name']}  |  {' + '.join(parts)}  |  "
        f"max |v| {metrics['max_speed_mm_s']['vector']:.0f} mm/s, "
        f"max |a| {metrics['max_acceleration_mm_s2']['vector']:.0f} mm/s^2, "
        f"ramp {metadata['ramp_sec']:g} s"
    )
    figure.savefig(path, dpi=180)
    plt.close(figure)


def _write_imported_preview(
    path: Path,
    offset_mm: np.ndarray,
    metadata: dict[str, Any],
    time_sec: np.ndarray,
    colors: tuple[str, str, str],
    reference_mm: np.ndarray | None,
) -> None:
    import matplotlib.pyplot as plt

    sample_hz = float(metadata["sample_hz"])
    limits = metadata["safety_limits"]
    driven = [str(axis) for axis in metadata.get("axes", [])] or ["x", "z"]
    plane = (driven + [axis for axis in ("x", "z", "y") if axis not in driven])[:2]
    first, second = AXIS_INDEX[plane[0]], AXIS_INDEX[plane[1]]
    figure, axes = plt.subplots(2, 2, figsize=(16, 11), constrained_layout=True)

    path_axis = axes[0][0]
    if reference_mm is not None:
        path_axis.plot(
            reference_mm[:, first], reference_mm[:, second],
            color="black", linewidth=1.0, label="reference r",
        )
    path_axis.plot(
        offset_mm[:, first], offset_mm[:, second],
        color="#D55E00", linewidth=0.5, alpha=0.8, label="command u",
    )
    path_axis.plot([0.0], [0.0], marker="+", color="#0072B2", markersize=12, label="centre")
    path_axis.set_aspect("equal", adjustable="datalim")
    path_axis.set_xlabel(f"{plane[0]} offset [mm]")
    path_axis.set_ylabel(f"{plane[1]} offset [mm]")
    path_axis.set_title("Path about the levitation centre")
    path_axis.legend(loc="upper right", fontsize=8)
    path_axis.grid(alpha=0.25)

    for axis in driven:
        index = AXIS_INDEX[axis]
        axes[0][1].plot(
            time_sec, offset_mm[:, index], color=colors[index], linewidth=0.5, label=f"u {axis}"
        )
    axes[0][1].set_xlabel("Time [s]")
    axes[0][1].set_ylabel("Command offset [mm]")
    axes[0][1].set_title("Full command")
    axes[0][1].legend(loc="upper right", ncol=3, fontsize=8)
    axes[0][1].grid(alpha=0.25)

    zoom_sec = 0.2
    zoom_start = max(0.0, 0.5 * float(time_sec[-1]) - 0.5 * zoom_sec)
    zoom_mask = (time_sec >= zoom_start) & (time_sec <= zoom_start + zoom_sec)
    for axis in driven:
        index = AXIS_INDEX[axis]
        if reference_mm is not None:
            axes[1][0].plot(
                time_sec[zoom_mask], reference_mm[zoom_mask, index],
                color="black", linewidth=1.0, linestyle="--",
            )
        axes[1][0].plot(
            time_sec[zoom_mask], offset_mm[zoom_mask, index],
            color=colors[index], linewidth=0.9, label=f"u {axis}",
        )
    axes[1][0].set_xlabel("Time [s]")
    axes[1][0].set_ylabel("Offset [mm]")
    axes[1][0].set_title(f"{zoom_sec:g} s window at mid-run (dashed black: reference r)")
    axes[1][0].legend(loc="upper right", ncol=3, fontsize=8)
    axes[1][0].grid(alpha=0.25)

    edge_order = 2 if offset_mm.shape[0] >= 3 else 1
    velocity = np.gradient(offset_mm, 1.0 / sample_hz, axis=0, edge_order=edge_order)
    acceleration = np.gradient(velocity, 1.0 / sample_hz, axis=0, edge_order=edge_order)
    speed_limit = float(limits["max_speed_mm_s"])
    accel_limit = float(limits["max_acceleration_mm_s2"])
    fraction_axis = axes[1][1]
    fraction_axis.plot(
        time_sec, np.linalg.norm(velocity, axis=1) / speed_limit,
        color="#0072B2", linewidth=0.5, label=f"|v| / {speed_limit:g} mm/s",
    )
    fraction_axis.plot(
        time_sec, np.linalg.norm(acceleration, axis=1) / accel_limit,
        color="#CC79A7", linewidth=0.5, label=f"|a| / {accel_limit:g} mm/s^2",
    )
    if reference_mm is not None and "max_command_reference_distance_mm" in limits:
        distance_limit = float(limits["max_command_reference_distance_mm"])
        fraction_axis.plot(
            time_sec, np.linalg.norm(offset_mm - reference_mm, axis=1) / distance_limit,
            color="#009E73", linewidth=0.7, label=f"|u-r| / {distance_limit:g} mm",
        )
    fraction_axis.axhline(1.0, color="black", linestyle="--", linewidth=0.8, label="limit")
    fraction_axis.set_ylim(0.0, 1.15)
    fraction_axis.set_xlabel("Time [s]")
    fraction_axis.set_ylabel("Fraction of safety limit")
    fraction_axis.set_title("Safety limits (a command above any limit is rejected, never rescaled)")
    fraction_axis.legend(loc="lower center", ncol=2, fontsize=8)
    fraction_axis.grid(alpha=0.25)

    metrics = metadata["metrics"]
    detail = metadata["generation_detail"]
    distance_text = (
        f", max |u-r| {detail['max_command_reference_distance_mm']:.3f} mm"
        if "max_command_reference_distance_mm" in detail
        else ""
    )
    figure.suptitle(
        f"{metadata['name']}  |  imported {detail['source_file']}  |  "
        f"max |u| {metrics['max_abs_offset_mm']['vector']:.2f} mm, "
        f"max |v| {metrics['max_speed_mm_s']['vector']:.0f} mm/s, "
        f"max |a| {metrics['max_acceleration_mm_s2']['vector']:.0f} mm/s^2{distance_text}"
    )
    figure.savefig(path, dpi=150)
    plt.close(figure)


def export_plan(
    plan_path: Path, output_dir: Path, include_disabled: bool, write_csv: bool
) -> dict[str, Any]:
    plan = load_plan(plan_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    runs = []
    for spec in plan["experiments"]:
        if not bool(spec.get("enabled", True)) and not include_disabled:
            continue
        repeats = int(spec.get("repeats", 1))
        for repeat_index in range(repeats):
            run_spec = dict(spec)
            run_spec["name"] = (
                f"{spec['name']}_r{repeat_index + 1:02d}"
                if repeats > 1
                else str(spec["name"])
            )
            if run_spec["kind"] == "multisine":
                run_spec["seed"] = int(spec.get("seed", 0)) + repeat_index
            repeat_overrides = spec.get("repeat_overrides", [])
            if repeat_overrides:
                if not isinstance(repeat_overrides, list) or len(repeat_overrides) != repeats:
                    raise ValueError(
                        f"{spec['name']}: repeat_overrides must contain exactly {repeats} objects"
                    )
                override = repeat_overrides[repeat_index]
                if not isinstance(override, dict):
                    raise ValueError(
                        f"{spec['name']}: every repeat_overrides entry must be an object"
                    )
                run_spec.update(override)
            run_spec["_plan_dir"] = str(plan_path.parent)
            offset_mm, metadata = generate_trajectory(run_spec, plan["defaults"])
            run_dir = output_dir / metadata["name"]
            run_dir.mkdir(parents=True, exist_ok=True)
            arrays: dict[str, np.ndarray] = {
                "time_sec": np.arange(offset_mm.shape[0]) / metadata["sample_hz"],
                "offset_mm": offset_mm,
                "offset_m": offset_mm * 1e-3,
                "sample_hz": np.asarray(metadata["sample_hz"]),
            }
            reference_mm: np.ndarray | None = None
            if str(metadata["kind"]).lower() == "imported":
                imported = load_imported_command(run_spec)
                reference_mm = imported.reference_mm
                if reference_mm is not None:
                    arrays["reference_mm"] = reference_mm
                for key, values in imported.extra_arrays.items():
                    if key in arrays:
                        raise ValueError(f"{metadata['name']}: extra array {key!r} is reserved")
                    arrays[key] = values
            np.savez_compressed(run_dir / "command_trajectory.npz", **arrays)
            if write_csv or str(metadata["kind"]).lower() in {
                "staircase",
                "multitone",
                "imported",
            }:
                write_offset_log(
                    run_dir / "command_offset_log.csv",
                    offset_mm,
                    metadata["sample_hz"],
                )
            if reference_mm is not None:
                write_preview(
                    run_dir / "trajectory_preview.png", offset_mm, metadata, reference_mm
                )
            else:
                write_preview(run_dir / "trajectory_preview.png", offset_mm, metadata)
            (run_dir / "trajectory_metadata.json").write_text(
                json.dumps(metadata, indent=2), encoding="utf-8"
            )
            runs.append(
                {"name": metadata["name"], "directory": str(run_dir), "metadata": metadata}
            )

    digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "source_plan": str(plan_path.resolve()),
        "source_plan_sha256": digest,
        "coordinate_convention": (
            "offset_mm, offset_m, and command_offset_log.csv are offsets from "
            "the hardware levitation centre"
        ),
        "delay_feedforward_applied": False,
        "campaign": plan.get("campaign", ""),
        "measurement_family": plan.get("measurement_family", ""),
        "purpose": plan.get("purpose", ""),
        "runs": runs,
    }
    (output_dir / "export_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path(__file__).with_name("high_frequency_identification_plan.json"),
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("hf_identification_export")
    )
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="Also export diagnostic runs marked enabled=false",
    )
    parser.add_argument(
        "--write-csv",
        action="store_true",
        help="Write large command_offset_log.csv files in addition to NPZ",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = export_plan(
        args.plan.resolve(), args.output_dir.resolve(), args.include_disabled, args.write_csv
    )
    print(f"Exported {len(manifest['runs'])} runs to {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
