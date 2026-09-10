"""Causal trajectory pre-emphasis for the identified first-order delay model."""

from __future__ import annotations

import math
from typing import Sequence


Position = tuple[float, float, float]


def apply_delay_feedforward(
    positions: Sequence[Position],
    sample_hz: float,
    tau_sec: float = 0.00205,
    max_offset_mm: float = 1.0,
    periodic: bool = False,
    limit_strategy: str = "pointwise_clip",
) -> tuple[list[Position], dict[str, float | str]]:
    """Return ``u = r + tau * r_dot`` with a vector-norm safety limit."""
    points = [tuple(float(value) for value in position) for position in positions]
    if len(points) < 2:
        raise ValueError("at least two trajectory positions are required")
    if sample_hz <= 0 or tau_sec < 0 or max_offset_mm <= 0:
        raise ValueError("sample_hz and max_offset_mm must be positive; tau_sec cannot be negative")

    max_offset_m = max_offset_mm * 1.0e-3
    if limit_strategy not in {"pointwise_clip", "global_scale"}:
        raise ValueError("limit_strategy must be 'pointwise_clip' or 'global_scale'")
    raw_offsets: list[Position] = []
    for index, point in enumerate(points):
        if periodic:
            previous = points[(index - 1) % len(points)]
            following = points[(index + 1) % len(points)]
            derivative = tuple((following[axis] - previous[axis]) * sample_hz / 2.0 for axis in range(3))
        elif index == 0:
            derivative = tuple((points[1][axis] - point[axis]) * sample_hz for axis in range(3))
        elif index == len(points) - 1:
            derivative = tuple((point[axis] - points[index - 1][axis]) * sample_hz for axis in range(3))
        else:
            derivative = tuple(
                (points[index + 1][axis] - points[index - 1][axis]) * sample_hz / 2.0
                for axis in range(3)
            )

        raw_offsets.append(tuple(tau_sec * value for value in derivative))

    raw_norms = [math.sqrt(sum(value * value for value in offset)) for offset in raw_offsets]
    raw_max = max(raw_norms)
    global_scale = (
        min(1.0, max_offset_m / raw_max)
        if limit_strategy == "global_scale" and raw_max > 0.0
        else 1.0
    )
    compensated: list[Position] = []
    offset_norms: list[float] = []
    clipped = 0
    for point, raw_offset, raw_norm in zip(points, raw_offsets, raw_norms):
        offset = tuple(global_scale * value for value in raw_offset)
        norm = raw_norm * global_scale
        if limit_strategy == "pointwise_clip" and norm > max_offset_m:
            scale = max_offset_m / norm
            offset = tuple(value * scale for value in offset)
            norm = max_offset_m
            clipped += 1
        compensated.append(tuple(point[axis] + offset[axis] for axis in range(3)))
        offset_norms.append(norm)

    return compensated, {
        "enabled": 1.0,
        "tau_sec": float(tau_sec),
        "max_offset_mm": float(max_offset_mm),
        "limit_strategy": limit_strategy,
        "raw_max_offset_mm": raw_max * 1.0e3,
        "global_scale_applied": float(global_scale),
        "effective_tau_sec": float(tau_sec * global_scale),
        "observed_max_offset_mm": max(offset_norms) * 1.0e3,
        "rms_offset_mm": math.sqrt(sum(value * value for value in offset_norms) / len(offset_norms)) * 1.0e3,
        "clipped_points": float(clipped),
        "clipped_fraction": float(clipped / len(points)),
        "limited_points": float(len(points) if global_scale < 1.0 else clipped),
        "limited_fraction": float(1.0 if global_scale < 1.0 else clipped / len(points)),
    }
