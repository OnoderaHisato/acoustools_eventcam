#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AcousTools-only controller for random and closed 3D trajectories.

The planar trajectory modes 1--10 from ``acoustools_eventcam_sync.py`` are
intentionally not exposed here.  Mode 1 is an open, reproducible random 3D
stroke; modes 2--5 are the existing closed 3D curves (internally modes
12--15 of ``generate_cycle_positions``); mode 6 is a deterministic multi-band
3D identification excitation; mode 7 is an open, deterministic three-axis
chirp excitation; mode 8 is a closed cusped planar/lifted-3D curve.

Positions written to the ideal CSV are in metres, matching AcousTools.  The
interactive limits and all preview/statistics labels are in millimetres.
"""

from __future__ import annotations

import argparse
import datetime
import math
import os
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Tuple

import torch

from acoustools.Levitator import LevitatorController
from acoustools.Utilities import add_lev_sig

from acoustools_eventcam_sync import (
    MODE_SHAPE_NAMES,
    PAT_UPDATE_BASE_HZ,
    compute_holograms_for_positions,
    compute_pat_frame_rate_info,
    describe_pat_frame_rate_info,
    generate_cycle_positions,
    move_static_position,
    mute_sync_transducer,
    ns_to_s,
    prepare_message_from_holograms,
    sanitize_filename,
)
from acoustools_multitraj_no_eventcam import (
    TrajectoryParameters,
    print_trajectory_stats,
    save_trajectory_preview,
    trajectory_stats,
    write_ideal_log,
    write_run_meta,
)
from acoustools_cusped3d import (
    generate_cusped_3d_positions,
    prompt_cusped_3d_parameters,
)


SAVE_DIR = "./rec_acoustools_3d"
DEFAULT_STEPS_PER_CYCLE = 400
DEFAULT_FREQUENCY_HZ = 2.0
DEFAULT_LOOPS = 10
MENU_TO_GENERATOR_MODE = {2: 12, 3: 13, 4: 14, 5: 15}


@dataclass(frozen=True)
class Random3DParameters:
    seed: int = 1101
    duration_sec: float = 8.0
    sample_hz: float = 1000.0
    x_limit_mm: float = 10.0
    y_limit_mm: float = 10.0
    z_limit_mm: float = 10.0
    f_min_hz: float = 0.25
    f_max_hz: float = 10.0
    max_speed_mm_s: float = 900.0
    max_accel_mm_s2: float = 30000.0
    components: int = 9
    chirps: int = 3
    waypoints: int = 9
    drift_weight: float = 0.35


@dataclass(frozen=True)
class ExtendedRandom3DParameters:
    """Deterministic multi-band 3D excitation for reusable system datasets."""

    seed: int = 3101
    duration_sec: float = 10.0
    sample_hz: float = 10000.0
    x_limit_mm: float = 2.0
    y_limit_mm: float = 2.0
    z_limit_mm: float = 2.0
    low_f_min_hz: float = 0.25
    low_f_max_hz: float = 16.0
    low_components: int = 8
    low_weight: float = 1.0
    mid_f_min_hz: float = 16.0
    mid_f_max_hz: float = 50.0
    mid_components: int = 8
    mid_weight: float = 0.30
    high_f_min_hz: float = 50.0
    high_f_max_hz: float = 130.0
    high_components: int = 6
    high_weight: float = 0.06
    chirps_per_enabled_band: int = 1
    waypoints: int = 9
    drift_weight: float = 0.10
    spectral_decay_power: float = 0.5
    max_speed_mm_s: float = 900.0
    max_accel_mm_s2: float = 28000.0


@dataclass(frozen=True)
class Chirped3DParameters:
    """Independent per-axis chirps that can form swept 3D curves."""

    duration_sec: float = 8.0
    sample_hz: float = 10000.0
    x_amp_mm: float = 1.5
    y_amp_mm: float = 1.5
    z_amp_mm: float = 1.5
    x_f_start_hz: float = 2.0
    x_f_end_hz: float = 50.0
    y_f_start_hz: float = 3.0
    y_f_end_hz: float = 70.0
    z_f_start_hz: float = 5.0
    z_f_end_hz: float = 90.0
    x_phase_deg: float = 0.0
    y_phase_deg: float = 120.0
    z_phase_deg: float = 240.0
    sweep_law: str = "linear"
    envelope: str = "tukey"
    taper_fraction: float = 0.10
    amplitude_modulation_hz: float = 0.0
    amplitude_modulation_depth: float = 0.0
    max_speed_mm_s: float = 900.0
    max_accel_mm_s2: float = 50000.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Draw random or predefined 3D particle trajectories with AcousTools."
    )
    parser.add_argument(
        "--preview-random",
        action="store_true",
        help="Generate the default random 3D trajectory preview and exit without connecting to PAT.",
    )
    parser.add_argument(
        "--preview-output",
        type=Path,
        default=Path("trajectory_samples_3d/random_3d.png"),
        help="PNG path used by --preview-random.",
    )
    parser.add_argument("--seed", type=int, default=1101, help="Seed used by --preview-random.")
    return parser.parse_args()


def _smoothstep(value: float) -> float:
    value = max(0.0, min(1.0, float(value)))
    return value * value * (3.0 - 2.0 * value)


def _interpolate_waypoints(t: float, duration_sec: float, values: list[float]) -> float:
    if len(values) == 1 or duration_sec <= 0.0:
        return float(values[0])
    scaled = max(0.0, min(1.0, t / duration_sec)) * (len(values) - 1)
    index = min(int(math.floor(scaled)), len(values) - 2)
    fraction = _smoothstep(scaled - index)
    return values[index] * (1.0 - fraction) + values[index + 1] * fraction


def generate_random_3d_positions(
    params: Random3DParameters,
    centre: Tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> tuple[list[Tuple[float, float, float]], dict[str, Any]]:
    """Generate a deterministic, smooth, open 3D stroke.

    Each axis receives an independent mixture of log-uniform sinusoids,
    chirps, and smoothly interpolated random waypoints.  All three axes are
    then scaled together when a requested vector speed or acceleration limit
    is exceeded, preserving the shape of the generated path.
    """
    if params.duration_sec <= 0.0 or params.sample_hz <= 0.0:
        raise ValueError("duration_sec and sample_hz must be positive")
    if min(params.x_limit_mm, params.y_limit_mm, params.z_limit_mm) < 0.0:
        raise ValueError("axis limits must be non-negative")
    if params.f_min_hz <= 0.0 or params.f_max_hz < params.f_min_hz:
        raise ValueError("frequency range must satisfy 0 < f_min_hz <= f_max_hz")
    if min(params.components, params.waypoints) < 1 or params.chirps < 0:
        raise ValueError("components/waypoints must be positive and chirps non-negative")

    n_points = max(2, int(round(params.duration_sec * params.sample_hz)))
    duration_sec = n_points / params.sample_hz
    rng = random.Random(params.seed)

    def make_axis(limit_mm: float) -> list[float]:
        if limit_mm == 0.0:
            return [0.0] * n_points
        components: list[tuple[float, float, float]] = []
        for _ in range(params.components):
            frequency = math.exp(rng.uniform(math.log(params.f_min_hz), math.log(params.f_max_hz)))
            amplitude = rng.uniform(0.35, 1.0) / math.sqrt(frequency)
            components.append((amplitude, frequency, rng.uniform(0.0, 2.0 * math.pi)))
        chirps: list[tuple[float, float, float, float]] = []
        for _ in range(params.chirps):
            f0 = rng.uniform(params.f_min_hz, max(params.f_min_hz, params.f_max_hz * 0.35))
            f1 = rng.uniform(max(f0, params.f_max_hz * 0.45), params.f_max_hz)
            chirps.append((rng.uniform(0.15, 0.5), f0, f1, rng.uniform(0.0, 2.0 * math.pi)))
        waypoints = [rng.uniform(-1.0, 1.0) for _ in range(params.waypoints)]
        values: list[float] = []
        for index in range(n_points):
            t = index / params.sample_hz
            value = sum(
                amplitude * math.sin(2.0 * math.pi * frequency * t + phase)
                for amplitude, frequency, phase in components
            )
            for amplitude, f0, f1, phase in chirps:
                chirp_rate = (f1 - f0) / duration_sec
                value += amplitude * math.sin(
                    2.0 * math.pi * (f0 * t + 0.5 * chirp_rate * t * t) + phase
                )
            value += params.drift_weight * _interpolate_waypoints(t, duration_sec, waypoints)
            values.append(value)
        mean = sum(values) / len(values)
        centred = [value - mean for value in values]
        peak = max(max(abs(value) for value in centred), 1e-12)
        return [value * limit_mm / peak for value in centred]

    offsets_mm = list(zip(
        make_axis(params.x_limit_mm),
        make_axis(params.y_limit_mm),
        make_axis(params.z_limit_mm),
    ))
    positions = [
        tuple(centre[axis] + offset[axis] * 1e-3 for axis in range(3))
        for offset in offsets_mm
    ]
    stats = trajectory_stats(positions, params.sample_hz, closed_cycle=False)
    scale = 1.0
    if params.max_speed_mm_s > 0.0 and stats["max_speed_mm_s"] > params.max_speed_mm_s:
        scale = min(scale, params.max_speed_mm_s / stats["max_speed_mm_s"])
    if params.max_accel_mm_s2 > 0.0 and stats["max_accel_mm_s2"] > params.max_accel_mm_s2:
        scale = min(scale, params.max_accel_mm_s2 / stats["max_accel_mm_s2"])
    if scale < 1.0:
        positions = [
            tuple(centre[axis] + (point[axis] - centre[axis]) * scale for axis in range(3))
            for point in positions
        ]
        stats = trajectory_stats(positions, params.sample_hz, closed_cycle=False)
    stats.update(
        {
            "points": n_points,
            "duration_sec": duration_sec,
            "sample_hz": params.sample_hz,
            "seed": params.seed,
            "amplitude_scale_applied": scale,
        }
    )
    return positions, stats


def generate_extended_random_3d_positions(
    params: ExtendedRandom3DParameters,
    centre: Tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> tuple[list[Tuple[float, float, float]], dict[str, Any]]:
    """Generate an independent-XYZ, multi-band random excitation.

    Band weights are applied before each axis is normalized to its requested
    spatial limit. Vector speed/acceleration limits are then enforced with one
    common scale so the 3D shape and relative spectral composition are kept.
    Set an axis limit to zero for a plane or line experiment; set a band weight
    or component count to zero to disable that band.
    """
    if params.duration_sec <= 0.0 or params.sample_hz <= 0.0:
        raise ValueError("duration_sec and sample_hz must be positive")
    if min(params.x_limit_mm, params.y_limit_mm, params.z_limit_mm) < 0.0:
        raise ValueError("axis limits must be non-negative")
    if params.spectral_decay_power < 0.0:
        raise ValueError("spectral_decay_power must be non-negative")
    if params.chirps_per_enabled_band < 0 or params.waypoints < 1:
        raise ValueError("chirps must be non-negative and waypoints positive")

    bands = [
        ("low", params.low_f_min_hz, params.low_f_max_hz, params.low_components, params.low_weight),
        ("mid", params.mid_f_min_hz, params.mid_f_max_hz, params.mid_components, params.mid_weight),
        ("high", params.high_f_min_hz, params.high_f_max_hz, params.high_components, params.high_weight),
    ]
    enabled_bands: list[tuple[str, float, float, int, float]] = []
    for name, f_min, f_max, components, weight in bands:
        if components < 0 or weight < 0.0:
            raise ValueError(f"{name} band components/weight must be non-negative")
        if components == 0 or weight == 0.0:
            continue
        if f_min <= 0.0 or f_max < f_min or f_max >= 0.5 * params.sample_hz:
            raise ValueError(
                f"{name} band must satisfy 0 < f_min <= f_max < Nyquist"
            )
        enabled_bands.append((name, f_min, f_max, components, weight))
    if not enabled_bands and params.drift_weight == 0.0:
        raise ValueError("at least one spectral band or drift must be enabled")

    n_points = max(2, int(round(params.duration_sec * params.sample_hz)))
    duration_sec = n_points / params.sample_hz
    rng = random.Random(params.seed)

    def make_axis(limit_mm: float) -> list[float]:
        if limit_mm == 0.0:
            return [0.0] * n_points
        tones: list[tuple[float, float, float]] = []
        chirps: list[tuple[float, float, float, float]] = []
        for _, f_min, f_max, components, weight in enabled_bands:
            for _ in range(components):
                frequency = math.exp(rng.uniform(math.log(f_min), math.log(f_max)))
                amplitude = weight * rng.uniform(0.65, 1.0) / (
                    frequency ** params.spectral_decay_power
                )
                tones.append((amplitude, frequency, rng.uniform(0.0, 2.0 * math.pi)))
            for _ in range(params.chirps_per_enabled_band):
                chirps.append(
                    (
                        weight * rng.uniform(0.15, 0.35),
                        f_min,
                        f_max,
                        rng.uniform(0.0, 2.0 * math.pi),
                    )
                )
        waypoint_values = [rng.uniform(-1.0, 1.0) for _ in range(params.waypoints)]
        values: list[float] = []
        for index in range(n_points):
            t = index / params.sample_hz
            value = sum(
                amplitude * math.sin(2.0 * math.pi * frequency * t + phase)
                for amplitude, frequency, phase in tones
            )
            for amplitude, f0, f1, phase in chirps:
                chirp_rate = (f1 - f0) / duration_sec
                value += amplitude * math.sin(
                    2.0 * math.pi * (f0 * t + 0.5 * chirp_rate * t * t) + phase
                )
            value += params.drift_weight * _interpolate_waypoints(
                t, duration_sec, waypoint_values
            )
            values.append(value)
        mean = sum(values) / len(values)
        centred = [value - mean for value in values]
        peak = max(max(abs(value) for value in centred), 1e-12)
        return [value * limit_mm / peak for value in centred]

    offsets_mm = list(
        zip(
            make_axis(params.x_limit_mm),
            make_axis(params.y_limit_mm),
            make_axis(params.z_limit_mm),
        )
    )
    positions = [
        tuple(centre[axis] + offset[axis] * 1e-3 for axis in range(3))
        for offset in offsets_mm
    ]
    stats = trajectory_stats(positions, params.sample_hz, closed_cycle=False)
    scale = 1.0
    if params.max_speed_mm_s > 0.0 and stats["max_speed_mm_s"] > params.max_speed_mm_s:
        scale = min(scale, params.max_speed_mm_s / stats["max_speed_mm_s"])
    if params.max_accel_mm_s2 > 0.0 and stats["max_accel_mm_s2"] > params.max_accel_mm_s2:
        scale = min(scale, params.max_accel_mm_s2 / stats["max_accel_mm_s2"])
    if scale < 1.0:
        positions = [
            tuple(centre[axis] + (point[axis] - centre[axis]) * scale for axis in range(3))
            for point in positions
        ]
        stats = trajectory_stats(positions, params.sample_hz, closed_cycle=False)
    stats.update(
        {
            "points": n_points,
            "duration_sec": duration_sec,
            "sample_hz": params.sample_hz,
            "seed": params.seed,
            "amplitude_scale_applied": scale,
            "enabled_bands": [
                {
                    "name": name,
                    "f_min_hz": f_min,
                    "f_max_hz": f_max,
                    "components_per_axis": components,
                    "weight": weight,
                }
                for name, f_min, f_max, components, weight in enabled_bands
            ],
        }
    )
    return positions, stats


def _chirp_phase_cycles(
    t: float, duration_sec: float, f_start_hz: float, f_end_hz: float, law: str
) -> float:
    """Return integrated chirp phase in cycles at time ``t``."""
    if law == "linear":
        rate_hz_s = (f_end_hz - f_start_hz) / duration_sec
        return f_start_hz * t + 0.5 * rate_hz_s * t * t
    if law == "logarithmic":
        if math.isclose(f_start_hz, f_end_hz, rel_tol=1e-12, abs_tol=1e-12):
            return f_start_hz * t
        ratio = f_end_hz / f_start_hz
        return f_start_hz * duration_sec * (ratio ** (t / duration_sec) - 1.0) / math.log(ratio)
    raise ValueError("sweep_law must be 'linear' or 'logarithmic'")


def _chirp_envelope(progress: float, kind: str, taper_fraction: float) -> float:
    progress = max(0.0, min(1.0, float(progress)))
    if kind == "constant":
        return 1.0
    if kind == "ramp_up":
        return _smoothstep(progress)
    if kind == "ramp_down":
        return _smoothstep(1.0 - progress)
    if kind == "sine":
        return math.sin(math.pi * progress) ** 2
    if kind == "tukey":
        if taper_fraction <= 0.0:
            return 1.0
        if progress < taper_fraction:
            return math.sin(0.5 * math.pi * progress / taper_fraction) ** 2
        if progress > 1.0 - taper_fraction:
            return math.sin(0.5 * math.pi * (1.0 - progress) / taper_fraction) ** 2
        return 1.0
    raise ValueError("envelope must be constant, ramp_up, ramp_down, sine, or tukey")


def generate_chirped_3d_positions(
    params: Chirped3DParameters,
    centre: Tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> tuple[list[Tuple[float, float, float]], dict[str, Any]]:
    """Generate an open 3D path from independently configurable axis chirps.

    Equal X/Z sweeps with a 90-degree phase offset form a swept helix. Opposite
    start/end values form counter-sweeps, while distinct ranges excite
    cross-axis coupling over a broad volume. All axes share one envelope and
    one dynamic-limit scale so their relative amplitudes remain unchanged.
    """
    if params.duration_sec <= 0.0 or params.sample_hz <= 0.0:
        raise ValueError("duration_sec and sample_hz must be positive")
    amplitudes_mm = (params.x_amp_mm, params.y_amp_mm, params.z_amp_mm)
    if min(amplitudes_mm) < 0.0 or max(amplitudes_mm) <= 0.0:
        raise ValueError("axis amplitudes must be non-negative and at least one must be positive")
    frequency_pairs = (
        (params.x_f_start_hz, params.x_f_end_hz),
        (params.y_f_start_hz, params.y_f_end_hz),
        (params.z_f_start_hz, params.z_f_end_hz),
    )
    nyquist_hz = 0.5 * params.sample_hz
    for axis, (amplitude, frequencies) in enumerate(zip(amplitudes_mm, frequency_pairs)):
        if amplitude == 0.0:
            continue
        if min(frequencies) <= 0.0 or max(frequencies) >= nyquist_hz:
            axis_name = "XYZ"[axis]
            raise ValueError(
                f"{axis_name} chirp must satisfy 0 < start/end frequency < Nyquist"
            )
    law = str(params.sweep_law).strip().lower()
    envelope_kind = str(params.envelope).strip().lower()
    if law not in {"linear", "logarithmic"}:
        raise ValueError("sweep_law must be 'linear' or 'logarithmic'")
    if envelope_kind not in {"constant", "ramp_up", "ramp_down", "sine", "tukey"}:
        raise ValueError("unsupported chirp envelope")
    if not 0.0 <= params.taper_fraction <= 0.5:
        raise ValueError("taper_fraction must be between 0 and 0.5")
    if params.amplitude_modulation_hz < 0.0:
        raise ValueError("amplitude_modulation_hz must be non-negative")
    if not 0.0 <= params.amplitude_modulation_depth <= 1.0:
        raise ValueError("amplitude_modulation_depth must be between 0 and 1")
    if params.max_speed_mm_s <= 0.0 or params.max_accel_mm_s2 <= 0.0:
        raise ValueError("chirped 3D trajectories require positive speed/acceleration limits")

    n_points = max(2, int(round(params.duration_sec * params.sample_hz)))
    duration_sec = n_points / params.sample_hz
    phases_rad = tuple(
        math.radians(value)
        for value in (params.x_phase_deg, params.y_phase_deg, params.z_phase_deg)
    )
    positions: list[Tuple[float, float, float]] = []
    for index in range(n_points):
        t = index / params.sample_hz
        progress = t / duration_sec
        envelope = _chirp_envelope(progress, envelope_kind, params.taper_fraction)
        if params.amplitude_modulation_hz > 0.0 and params.amplitude_modulation_depth > 0.0:
            modulation = 1.0 - params.amplitude_modulation_depth * 0.5 * (
                1.0 - math.sin(2.0 * math.pi * params.amplitude_modulation_hz * t)
            )
        else:
            modulation = 1.0
        offsets_mm = []
        for amplitude, (f_start, f_end), phase in zip(
            amplitudes_mm, frequency_pairs, phases_rad
        ):
            if amplitude == 0.0:
                offsets_mm.append(0.0)
                continue
            cycles = _chirp_phase_cycles(t, duration_sec, f_start, f_end, law)
            offsets_mm.append(
                amplitude * envelope * modulation * math.sin(2.0 * math.pi * cycles + phase)
            )
        positions.append(tuple(
            centre[axis] + offsets_mm[axis] * 1e-3 for axis in range(3)
        ))

    stats = trajectory_stats(positions, params.sample_hz, closed_cycle=False)
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
        stats = trajectory_stats(positions, params.sample_hz, closed_cycle=False)
    stats.update(
        {
            "points": n_points,
            "duration_sec": duration_sec,
            "sample_hz": params.sample_hz,
            "amplitude_scale_applied": scale,
            "sweep_law": law,
            "envelope": envelope_kind,
            "axis_chirps": {
                axis: {
                    "amplitude_mm": amplitude,
                    "f_start_hz": frequencies[0],
                    "f_end_hz": frequencies[1],
                    "phase_deg": phase,
                }
                for axis, amplitude, frequencies, phase in zip(
                    "xyz",
                    amplitudes_mm,
                    frequency_pairs,
                    (params.x_phase_deg, params.y_phase_deg, params.z_phase_deg),
                )
            },
        }
    )
    return positions, stats


def prompt_float(label: str, default: float, *, minimum: float | None = None) -> float:
    value = float(input(f"{label} [default {default:g}]: ").strip() or str(default))
    if minimum is not None and value < minimum:
        raise ValueError
    return value


def prompt_random_parameters() -> Random3DParameters | None:
    try:
        seed = int(input("Enter random seed [default 1101]: ").strip() or "1101")
        return Random3DParameters(
            seed=seed,
            duration_sec=prompt_float("Enter duration in seconds", 8.0, minimum=0.1),
            sample_hz=prompt_float("Enter PAT sample rate in Hz", 1000.0, minimum=1.0),
            x_limit_mm=prompt_float("Enter X range limit (+/-mm)", 10.0, minimum=0.0),
            y_limit_mm=prompt_float("Enter Y range limit (+/-mm)", 10.0, minimum=0.0),
            z_limit_mm=prompt_float("Enter Z range limit (+/-mm)", 10.0, minimum=0.0),
            f_min_hz=prompt_float("Enter min component frequency Hz", 0.25, minimum=0.01),
            f_max_hz=prompt_float("Enter max component frequency Hz", 10.0, minimum=0.01),
            max_speed_mm_s=prompt_float("Enter max vector speed mm/s (0 disables)", 900.0, minimum=0.0),
            max_accel_mm_s2=prompt_float(
                "Enter max vector acceleration mm/s^2 (0 disables)", 30000.0, minimum=0.0
            ),
        )
    except (ValueError, EOFError):
        print("[WARN] Invalid random trajectory parameter.")
        return None


def prompt_extended_random_parameters() -> ExtendedRandom3DParameters | None:
    try:
        seed = int(input("Enter random seed [default 3101]: ").strip() or "3101")
        return ExtendedRandom3DParameters(
            seed=seed,
            duration_sec=prompt_float("Enter duration in seconds", 10.0, minimum=0.1),
            sample_hz=prompt_float("Enter PAT sample rate in Hz", 10000.0, minimum=1.0),
            x_limit_mm=prompt_float("Enter X range limit (+/-mm)", 2.0, minimum=0.0),
            y_limit_mm=prompt_float("Enter Y range limit (+/-mm)", 2.0, minimum=0.0),
            z_limit_mm=prompt_float("Enter Z range limit (+/-mm)", 2.0, minimum=0.0),
            low_f_min_hz=prompt_float("Low-band minimum Hz", 0.25, minimum=0.01),
            low_f_max_hz=prompt_float("Low-band maximum Hz", 16.0, minimum=0.01),
            low_components=int(input("Low-band components/axis [default 8]: ").strip() or "8"),
            low_weight=prompt_float("Low-band relative weight", 1.0, minimum=0.0),
            mid_f_min_hz=prompt_float("Mid-band minimum Hz", 16.0, minimum=0.01),
            mid_f_max_hz=prompt_float("Mid-band maximum Hz", 50.0, minimum=0.01),
            mid_components=int(input("Mid-band components/axis [default 8]: ").strip() or "8"),
            mid_weight=prompt_float("Mid-band relative weight", 0.30, minimum=0.0),
            high_f_min_hz=prompt_float("High-band minimum Hz", 50.0, minimum=0.01),
            high_f_max_hz=prompt_float("High-band maximum Hz", 130.0, minimum=0.01),
            high_components=int(input("High-band components/axis [default 6]: ").strip() or "6"),
            high_weight=prompt_float("High-band relative weight", 0.06, minimum=0.0),
            max_speed_mm_s=prompt_float("Max vector speed mm/s (0 disables)", 900.0, minimum=0.0),
            max_accel_mm_s2=prompt_float(
                "Max vector acceleration mm/s^2 (0 disables)", 28000.0, minimum=0.0
            ),
        )
    except (ValueError, EOFError):
        print("[WARN] Invalid extended random trajectory parameter.")
        return None


def prompt_chirped_3d_parameters() -> Chirped3DParameters | None:
    try:
        return Chirped3DParameters(
            duration_sec=prompt_float("Enter chirp duration in seconds", 8.0, minimum=0.1),
            sample_hz=prompt_float("Enter PAT sample rate in Hz", 10000.0, minimum=1.0),
            x_amp_mm=prompt_float("Enter X amplitude (+/-mm)", 1.5, minimum=0.0),
            y_amp_mm=prompt_float("Enter Y amplitude (+/-mm)", 1.5, minimum=0.0),
            z_amp_mm=prompt_float("Enter Z amplitude (+/-mm)", 1.5, minimum=0.0),
            x_f_start_hz=prompt_float("X chirp start Hz", 2.0, minimum=0.01),
            x_f_end_hz=prompt_float("X chirp end Hz", 50.0, minimum=0.01),
            y_f_start_hz=prompt_float("Y chirp start Hz", 3.0, minimum=0.01),
            y_f_end_hz=prompt_float("Y chirp end Hz", 70.0, minimum=0.01),
            z_f_start_hz=prompt_float("Z chirp start Hz", 5.0, minimum=0.01),
            z_f_end_hz=prompt_float("Z chirp end Hz", 90.0, minimum=0.01),
            max_speed_mm_s=prompt_float("Max vector speed mm/s", 900.0, minimum=0.01),
            max_accel_mm_s2=prompt_float(
                "Max vector acceleration mm/s^2", 50000.0, minimum=0.01
            ),
        )
    except (ValueError, EOFError):
        print("[WARN] Invalid chirped 3D trajectory parameter.")
        return None


def prompt_closed_parameters(mode: int) -> TrajectoryParameters | None:
    try:
        if mode == 12:
            x = prompt_float("Enter X radius (+/-mm)", 2.0, minimum=0.0)
            y = prompt_float("Enter Y radius (+/-mm)", x, minimum=0.0)
            z = prompt_float("Enter Z figure-eight amplitude (+/-mm)", 1.5, minimum=0.0)
            return TrajectoryParameters(amp_x_mm=x, amp_y_mm=y, amp_z_mm=z)
        if mode == 13:
            major = prompt_float("Enter torus major radius (mm)", 2.5, minimum=0.0)
            minor = prompt_float("Enter radial winding radius (mm)", 0.75, minimum=0.0)
            z = prompt_float("Enter Z winding amplitude (+/-mm)", 0.75, minimum=0.0)
            turns = int(input("Enter integer windings per closed cycle [default 3]: ").strip() or "3")
            if turns <= 0:
                raise ValueError
            return TrajectoryParameters(
                amp_x_mm=major, amp_z_mm=z, helix_minor_radius_mm=minor, helix_turns=turns
            )
        if mode == 14:
            return TrajectoryParameters(
                amp_x_mm=prompt_float("Enter trefoil X scale (mm)", 2.5, minimum=0.0),
                amp_y_mm=prompt_float("Enter trefoil Y scale (mm)", 2.5, minimum=0.0),
                amp_z_mm=prompt_float("Enter trefoil Z amplitude (+/-mm)", 1.5, minimum=0.0),
            )
        return TrajectoryParameters(
            amp_x_mm=prompt_float("Enter Lissajous X amplitude (+/-mm)", 2.0, minimum=0.0),
            amp_y_mm=prompt_float("Enter Lissajous Y amplitude (+/-mm)", 2.0, minimum=0.0),
            amp_z_mm=prompt_float("Enter Lissajous Z amplitude (+/-mm)", 2.0, minimum=0.0),
        )
    except (ValueError, EOFError):
        print("[WARN] Invalid 3D trajectory parameter.")
        return None


def prompt_closed_timing() -> tuple[int, float, int, Any] | None:
    try:
        n_steps = int(input(f"Enter steps per cycle [default {DEFAULT_STEPS_PER_CYCLE}]: ").strip() or str(DEFAULT_STEPS_PER_CYCLE))
        frequency = prompt_float("Enter motion frequency in Hz", DEFAULT_FREQUENCY_HZ, minimum=1e-9)
        loops = int(input(f"Enter loops [default {DEFAULT_LOOPS}]: ").strip() or str(DEFAULT_LOOPS))
        if n_steps <= 0 or loops <= 0:
            raise ValueError
    except (ValueError, EOFError):
        print("[WARN] Steps, frequency, and loops must be positive.")
        return None
    rate = compute_pat_frame_rate_info(n_steps, frequency)
    if not rate.is_supported:
        print(f"[WARN] Unsupported PAT frame rate: {describe_pat_frame_rate_info(rate)}")
        return None
    return n_steps, frequency, loops, rate


def preview_default_random(path: Path, seed: int) -> None:
    params = Random3DParameters(seed=seed)
    positions, stats = generate_random_3d_positions(params)
    path.parent.mkdir(parents=True, exist_ok=True)
    print_trajectory_stats(stats)
    if not save_trajectory_preview(str(path), positions, (0.0, 0.0, 0.0), f"random_3d: seed {seed}"):
        raise RuntimeError("failed to render random 3D preview")
    print(f"[INFO] Preview saved: {path.resolve()}")


def main() -> None:
    args = parse_args()
    if args.preview_random:
        preview_default_random(args.preview_output, args.seed)
        return

    Path(SAVE_DIR).mkdir(parents=True, exist_ok=True)
    lev = LevitatorController(ids=(101, 3))
    print("[INFO] Connected to PAT via AcousTools; no event camera is used.")
    current_pos: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    current_hologram: torch.Tensor | None = None
    try:
        current_hologram = mute_sync_transducer(compute_holograms_for_positions([current_pos])[0])
        lev.levitate(current_hologram)
        time.sleep(0.5)
        while True:
            print("\n=== 3D Trajectories ===")
            print(" 1: Long random 3D stroke (open, XYZ independent)")
            print(" 2: 3D figure-eight (XZ figure-eight, XY circle/ellipse)")
            print(" 3: Closed toroidal helix")
            print(" 4: Trefoil knot")
            print(" 5: 3D Lissajous (frequency ratio 1:2:3)")
            print(" 6: Extended multi-band random 3D excitation")
            print(" 7: Three-axis chirp excitation")
            print(" 8: Cusped planar/lifted-3D curve")
            try:
                menu_mode = int(input("Select mode (1-8): ").strip())
            except (ValueError, EOFError):
                print("[WARN] Enter a number from 1 to 8.")
                continue
            if menu_mode not in range(1, 9):
                print("[WARN] Mode must be from 1 to 8.")
                continue

            random_params: Random3DParameters | None = None
            closed_params: TrajectoryParameters | None = None
            if menu_mode == 8:
                cusped_params = prompt_cusped_3d_parameters()
                if cusped_params is None:
                    continue
                rate = compute_pat_frame_rate_info(
                    cusped_params.steps_per_cycle, cusped_params.frequency_hz
                )
                if not rate.is_supported:
                    print(f"[WARN] Unsupported PAT frame rate: {describe_pat_frame_rate_info(rate)}")
                    continue
                positions, stats = generate_cusped_3d_positions(cusped_params, current_pos)
                shape_name = "cusped_3d"
                parameter_payload = asdict(cusped_params)
                n_steps = len(positions)
                frequency = cusped_params.frequency_hz
                loops = cusped_params.loops
                closed_cycle = True
            elif menu_mode in {1, 6, 7}:
                if menu_mode == 1:
                    random_params = prompt_random_parameters()
                    extended_params = None
                    chirped_params = None
                elif menu_mode == 6:
                    random_params = None
                    extended_params = prompt_extended_random_parameters()
                    chirped_params = None
                else:
                    random_params = None
                    extended_params = None
                    chirped_params = prompt_chirped_3d_parameters()
                if menu_mode == 1 and (
                    random_params is None or random_params.f_max_hz < random_params.f_min_hz
                ):
                    print("[WARN] Maximum frequency must be at least the minimum frequency.")
                    continue
                if menu_mode == 6 and extended_params is None:
                    continue
                if menu_mode == 7 and chirped_params is None:
                    continue
                selected_params = random_params or extended_params or chirped_params
                sample_hz = selected_params.sample_hz
                rate = compute_pat_frame_rate_info(1, sample_hz)
                if not rate.is_supported:
                    print(f"[WARN] Unsupported PAT frame rate: {describe_pat_frame_rate_info(rate)}")
                    continue
                if random_params is not None:
                    positions, stats = generate_random_3d_positions(random_params, current_pos)
                    shape_name = "long_random_3d"
                    parameter_payload: dict[str, Any] = asdict(random_params)
                elif extended_params is not None:
                    positions, stats = generate_extended_random_3d_positions(
                        extended_params, current_pos
                    )
                    shape_name = "extended_random_3d"
                    parameter_payload = asdict(extended_params)
                else:
                    positions, stats = generate_chirped_3d_positions(
                        chirped_params, current_pos
                    )
                    shape_name = "chirped_3d"
                    parameter_payload = asdict(chirped_params)
                n_steps, frequency, loops = len(positions), sample_hz / len(positions), 1
                closed_cycle = False
            else:
                generator_mode = MENU_TO_GENERATOR_MODE[menu_mode]
                closed_params = prompt_closed_parameters(generator_mode)
                timing = prompt_closed_timing() if closed_params is not None else None
                if closed_params is None or timing is None:
                    continue
                n_steps, frequency, loops, rate = timing
                positions = generate_cycle_positions(
                    mode=generator_mode,
                    n_steps=n_steps,
                    amp_x=closed_params.amp_x_mm * 1e-3,
                    amp_y=closed_params.amp_y_mm * 1e-3,
                    amp_z=closed_params.amp_z_mm * 1e-3,
                    amp_scale=0.0,
                    x_center=current_pos[0],
                    y_center=current_pos[1],
                    z_center=current_pos[2],
                    helix_minor_radius=closed_params.helix_minor_radius_mm * 1e-3,
                    helix_turns=closed_params.helix_turns,
                )
                shape_name = MODE_SHAPE_NAMES[generator_mode]
                parameter_payload = asdict(closed_params)
                closed_cycle = True
                stats = trajectory_stats(positions, float(rate.effective_hz or rate.requested_hz), True)

            print(f"[INFO] PAT frame rate check OK: {describe_pat_frame_rate_info(rate)}")
            print_trajectory_stats(stats)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            run_dir = Path(SAVE_DIR) / shape_name / sanitize_filename(f"{shape_name}_{stamp}", max_len=60)
            run_dir.mkdir(parents=True, exist_ok=True)
            run_base = sanitize_filename(f"{shape_name}_s{n_steps}_f{rate.requested_hz:g}", max_len=50)
            preview_path = (run_dir / f"{run_base}_trajectory_preview.png").resolve()
            if save_trajectory_preview(str(preview_path), positions, current_pos, f"{shape_name}: one run"):
                print(f"[INFO] Preview saved: {preview_path}")

            print(f"[INFO] Computing {len(positions)} holograms...")
            holograms = compute_holograms_for_positions(positions)
            lev.set_frame_rate(rate.requested_hz)
            actual_fps = float(rate.effective_hz or rate.requested_hz)
            phases, amplitudes, geometry_count = prepare_message_from_holograms(lev, holograms, permute=True)
            start_pos = positions[0]
            end_pos = positions[0] if closed_cycle else positions[-1]
            end_hologram = holograms[0] if closed_cycle else holograms[-1]
            current_pos = move_static_position(lev, current_pos, start_pos, "Moving smoothly to trajectory start")
            input("\n>>> Particle is at the start. Press Enter to start motion. <<<\n")

            all_positions = positions * loops
            ideal_log = (run_dir / f"{run_base}_ideal_log.csv").resolve()
            write_ideal_log(str(ideal_log), all_positions, actual_fps)
            expected_duration = geometry_count * loops / actual_fps
            meta_path = (run_dir / f"{run_base}_run_meta.json").resolve()
            write_run_meta(
                str(meta_path),
                {
                    "script": Path(__file__).name,
                    "shape_name": shape_name,
                    "is_random_3d": menu_mode in {1, 6},
                    "closed_cycle": closed_cycle,
                    "parameters": parameter_payload,
                    "steps": n_steps,
                    "frequency_hz": frequency,
                    "loops": loops,
                    "pat_fps_requested": rate.requested_hz,
                    "pat_fps_actual": actual_fps,
                    "pat_fps_base_hz": PAT_UPDATE_BASE_HZ,
                    "pat_fps_divider": rate.divider,
                    "expected_duration_sec": expected_duration,
                    "ideal_log": str(ideal_log),
                    "trajectory_preview": str(preview_path),
                    "trajectory_stats": stats,
                },
            )
            start_ns = time.perf_counter_ns()
            lev.send_message(
                phases, amplitudes, 0, int(geometry_count), sleep_ms=0, loop=True, num_loops=loops
            )
            print(
                f"[PAT] completed in {ns_to_s(time.perf_counter_ns() - start_ns):.6f} s "
                f"(expected {expected_duration:.6f} s)"
            )
            current_pos = end_pos
            current_hologram = mute_sync_transducer(end_hologram)
            lev.levitate(current_hologram)
            if input("\nReturn to centre? (Y/n): ").strip().lower() != "n":
                current_pos = move_static_position(lev, current_pos, (0.0, 0.0, 0.0), "Moving back to centre")
                current_hologram = mute_sync_transducer(compute_holograms_for_positions([current_pos])[0])
                lev.levitate(current_hologram)
    except KeyboardInterrupt:
        print("\n[INFO] User requested exit. Cleaning up...")
    finally:
        try:
            if current_hologram is not None:
                off_phase = add_lev_sig(torch.zeros_like(current_hologram))
                phases, amplitudes, _ = prepare_message_from_holograms(lev, [off_phase], permute=True)
                lev.send_message(phases, amplitudes, 0, 1, sleep_ms=0, loop=False, num_loops=1)
        except Exception:
            pass
        print("[INFO] Shutdown complete.")


if __name__ == "__main__":
    main()
