#!/usr/bin/env python3
"""Cusped planar and lifted-3D trajectories for AcousTools experiments."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Tuple

from acoustools_multitraj_no_eventcam import trajectory_stats


@dataclass(frozen=True)
class Cusped3DParameters:
    """One closed cusped curve, optionally lifted along the third axis."""

    curve: str = "cardioid"
    plane: str = "xz"
    cusp_count: int = 3
    u_amp_mm: float = 3.0
    v_amp_mm: float = 3.0
    out_of_plane_amp_mm: float = 0.75
    out_of_plane_harmonic_multiple: int = 1
    rounding: float = 0.0
    phase_deg: float = 0.0
    plane_rotation_deg: float = 0.0
    steps_per_cycle: int = 1000
    frequency_hz: float = 4.0
    loops: int = 8
    max_speed_mm_s: float = 900.0
    max_accel_mm_s2: float = 500000.0


CURVE_CUSP_COUNTS = {
    "cardioid": 1,
    "nephroid": 2,
    "deltoid": 3,
    "astroid": 4,
}


def _normalized(values: list[float]) -> list[float]:
    peak = max(max(abs(value) for value in values), 1e-12)
    return [value / peak for value in values]


def _sharp_curve(curve: str, theta: float, cusp_count: int) -> tuple[float, float]:
    if curve == "cardioid":
        return (
            2.0 * math.cos(theta) - math.cos(2.0 * theta),
            2.0 * math.sin(theta) - math.sin(2.0 * theta),
        )
    if curve == "nephroid":
        return (
            3.0 * math.cos(theta) - math.cos(3.0 * theta),
            3.0 * math.sin(theta) - math.sin(3.0 * theta),
        )
    if curve == "deltoid":
        return (
            2.0 * math.cos(theta) + math.cos(2.0 * theta),
            2.0 * math.sin(theta) - math.sin(2.0 * theta),
        )
    if curve == "astroid":
        return math.cos(theta) ** 3, math.sin(theta) ** 3
    if curve == "hypocycloid":
        n = float(cusp_count)
        return (
            (n - 1.0) * math.cos(theta) + math.cos((n - 1.0) * theta),
            (n - 1.0) * math.sin(theta) - math.sin((n - 1.0) * theta),
        )
    raise ValueError(
        "curve must be cardioid, nephroid, deltoid, astroid, or hypocycloid"
    )


def _cusp_indices(n_steps: int, phase_rad: float, cusp_count: int) -> list[int]:
    indices: list[int] = []
    for cusp_index in range(cusp_count):
        theta = 2.0 * math.pi * cusp_index / cusp_count
        progress = ((theta - phase_rad) % (2.0 * math.pi)) / (2.0 * math.pi)
        indices.append(int(round(progress * n_steps)) % n_steps)
    return sorted(set(indices))


def _central_speed_mm_s(
    positions: list[Tuple[float, float, float]], index: int, sample_hz: float
) -> float:
    previous = positions[(index - 1) % len(positions)]
    following = positions[(index + 1) % len(positions)]
    velocity = [
        (following[axis] - previous[axis]) * 1e3 * sample_hz * 0.5
        for axis in range(3)
    ]
    return math.sqrt(sum(value * value for value in velocity))


def generate_cusped_3d_positions(
    params: Cusped3DParameters,
    centre: Tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> tuple[list[Tuple[float, float, float]], dict[str, Any]]:
    """Generate one closed cycle and scale all axes to dynamic limits."""
    curve = str(params.curve).strip().lower()
    plane = str(params.plane).strip().lower()
    if curve not in {*CURVE_CUSP_COUNTS, "hypocycloid"}:
        raise ValueError("unsupported cusped curve")
    if plane not in {"xy", "xz", "yz"}:
        raise ValueError("plane must be xy, xz, or yz")
    if curve == "hypocycloid" and (params.cusp_count < 3 or params.cusp_count > 12):
        raise ValueError("cusp_count must be between 3 and 12")
    actual_cusp_count = CURVE_CUSP_COUNTS.get(curve, params.cusp_count)
    if min(params.u_amp_mm, params.v_amp_mm, params.out_of_plane_amp_mm) < 0.0:
        raise ValueError("trajectory amplitudes must be non-negative")
    if max(params.u_amp_mm, params.v_amp_mm) <= 0.0:
        raise ValueError("at least one in-plane amplitude must be positive")
    if params.out_of_plane_harmonic_multiple < 1:
        raise ValueError("out_of_plane_harmonic_multiple must be positive")
    if not 0.0 <= params.rounding <= 0.5:
        raise ValueError("rounding must be between 0 and 0.5")
    if params.steps_per_cycle < 16 or params.frequency_hz <= 0.0 or params.loops < 1:
        raise ValueError("steps_per_cycle, frequency_hz, and loops must be positive")
    if params.max_speed_mm_s <= 0.0 or params.max_accel_mm_s2 <= 0.0:
        raise ValueError("cusped trajectories require positive dynamic limits")

    phase_rad = math.radians(params.phase_deg)
    theta_values = [
        2.0 * math.pi * index / params.steps_per_cycle + phase_rad
        for index in range(params.steps_per_cycle)
    ]
    sharp = [_sharp_curve(curve, theta, actual_cusp_count) for theta in theta_values]
    sharp_u = _normalized([point[0] for point in sharp])
    sharp_v = _normalized([point[1] for point in sharp])
    blended_u = [
        (1.0 - params.rounding) * value + params.rounding * math.cos(theta)
        for value, theta in zip(sharp_u, theta_values)
    ]
    blended_v = [
        (1.0 - params.rounding) * value + params.rounding * math.sin(theta)
        for value, theta in zip(sharp_v, theta_values)
    ]
    u_values = _normalized(blended_u)
    v_values = _normalized(blended_v)

    rotation = math.radians(params.plane_rotation_deg)
    cosine, sine = math.cos(rotation), math.sin(rotation)
    rotated_u = [cosine * u - sine * v for u, v in zip(u_values, v_values)]
    rotated_v = [sine * u + cosine * v for u, v in zip(u_values, v_values)]
    rotated_u = _normalized(rotated_u)
    rotated_v = _normalized(rotated_v)
    out_values = [
        math.cos(
            actual_cusp_count
            * params.out_of_plane_harmonic_multiple
            * theta
        )
        for theta in theta_values
    ]

    offsets_mm: list[tuple[float, float, float]] = []
    for u, v, out in zip(rotated_u, rotated_v, out_values):
        planar_u = params.u_amp_mm * u
        planar_v = params.v_amp_mm * v
        lifted = params.out_of_plane_amp_mm * out
        if plane == "xy":
            offsets_mm.append((planar_u, planar_v, lifted))
        elif plane == "xz":
            offsets_mm.append((planar_u, lifted, planar_v))
        else:
            offsets_mm.append((lifted, planar_u, planar_v))

    positions = [
        tuple(centre[axis] + offset[axis] * 1e-3 for axis in range(3))
        for offset in offsets_mm
    ]
    sample_hz = params.steps_per_cycle * params.frequency_hz
    stats = trajectory_stats(positions, sample_hz, closed_cycle=True)
    scale = min(
        1.0,
        params.max_speed_mm_s / max(float(stats["max_speed_mm_s"]), 1e-12),
        params.max_accel_mm_s2 / max(float(stats["max_accel_mm_s2"]), 1e-12),
    )
    if scale < 1.0:
        positions = [
            tuple(centre[axis] + (point[axis] - centre[axis]) * scale for axis in range(3))
            for point in positions
        ]
        stats = trajectory_stats(positions, sample_hz, closed_cycle=True)

    cusp_indices = _cusp_indices(params.steps_per_cycle, phase_rad, actual_cusp_count)
    cusp_speeds = [
        _central_speed_mm_s(positions, index, sample_hz) for index in cusp_indices
    ]
    highest_geometric_harmonic = max(
        {"cardioid": 2, "nephroid": 3, "deltoid": 2, "astroid": 3}.get(
            curve, actual_cusp_count - 1
        ),
        actual_cusp_count * params.out_of_plane_harmonic_multiple
        if params.out_of_plane_amp_mm > 0.0
        else 0,
    )
    stats.update(
        {
            "points": params.steps_per_cycle,
            "duration_sec": params.loops / params.frequency_hz,
            "cycle_duration_sec": 1.0 / params.frequency_hz,
            "sample_hz": sample_hz,
            "base_frequency_hz": params.frequency_hz,
            "curve": curve,
            "plane": plane,
            "cusp_count": actual_cusp_count,
            "rounding": params.rounding,
            "cusp_sample_indices": cusp_indices,
            "max_cusp_speed_mm_s": max(cusp_speeds, default=0.0),
            "cusp_to_peak_speed_ratio": (
                max(cusp_speeds, default=0.0)
                / max(float(stats["max_speed_mm_s"]), 1e-12)
            ),
            "highest_geometric_harmonic": highest_geometric_harmonic,
            "highest_geometric_frequency_hz": (
                highest_geometric_harmonic * params.frequency_hz
            ),
            "amplitude_scale_applied": scale,
        }
    )
    return positions, stats


def _prompt_float(label: str, default: float, minimum: float = 0.0) -> float:
    value = float(input(f"{label} [default {default:g}]: ").strip() or str(default))
    if value < minimum:
        raise ValueError
    return value


def prompt_cusped_3d_parameters() -> Cusped3DParameters | None:
    try:
        curve = input(
            "Curve cardioid/nephroid/deltoid/astroid/hypocycloid [cardioid]: "
        ).strip().lower() or "cardioid"
        plane = input("Primary plane xy/xz/yz [xz]: ").strip().lower() or "xz"
        cusp_count = int(input("Hypocycloid cusp count [3]: ").strip() or "3")
        return Cusped3DParameters(
            curve=curve,
            plane=plane,
            cusp_count=cusp_count,
            u_amp_mm=_prompt_float("In-plane U amplitude (+/-mm)", 3.0),
            v_amp_mm=_prompt_float("In-plane V amplitude (+/-mm)", 3.0),
            out_of_plane_amp_mm=_prompt_float("Out-of-plane amplitude (+/-mm)", 0.75),
            rounding=_prompt_float("Cusp rounding 0=sharp, 0.5=max", 0.0),
            steps_per_cycle=int(input("Steps per cycle [1000]: ").strip() or "1000"),
            frequency_hz=_prompt_float("Cycles per second (Hz)", 4.0, 1e-9),
            loops=int(input("Loops [8]: ").strip() or "8"),
            max_speed_mm_s=_prompt_float("Max vector speed mm/s", 900.0, 1e-9),
            max_accel_mm_s2=_prompt_float(
                "Max vector acceleration mm/s^2", 500000.0, 1e-9
            ),
        )
    except (ValueError, EOFError):
        print("[WARN] Invalid cusped 3D trajectory parameter.")
        return None
