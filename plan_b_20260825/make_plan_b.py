#!/usr/bin/env python3
"""Generate Plan B acquisition plans for the hardware-PC recorder."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np


HERE = Path(__file__).resolve().parent
CAMPAIGN = "plan_b_20260825"
F_BASE = 2.5


def odd_lines(low_hz: float, high_hz: float) -> np.ndarray:
    indexes = np.arange(0, int(high_hz / F_BASE) + 2)
    frequencies = F_BASE * (2 * indexes + 1)
    return frequencies[(frequencies >= low_hz) & (frequencies <= high_hz)]


def split_excited_detection(
    lines: np.ndarray,
    seed: int,
    forbidden: tuple[float, float] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    mask = np.ones(len(lines), dtype=bool)
    if forbidden is not None:
        mask &= ~((lines >= forbidden[0]) & (lines <= forbidden[1]))
    candidates = np.where(mask)[0]
    keep_count = int(round((2.0 / 3.0) * len(candidates)))
    keep = {int(candidates[0]), int(candidates[-1])}
    remaining = list(candidates[1:-1])
    rng.shuffle(remaining)
    for index in remaining:
        if len(keep) >= keep_count:
            break
        keep.add(int(index))
    excited = lines[np.asarray(sorted(keep), dtype=int)]
    detection = np.asarray(
        [frequency for index, frequency in enumerate(lines) if index not in keep]
    )
    return excited, detection


def plan_b1() -> dict:
    family = "plan_b1_small_amplitude_multisine"
    experiments: list[dict] = [
        {
            "name": "00_static_baseline",
            "kind": "static",
            "axis": "x",
            "duration_sec": 5.0,
            "repeats": 2,
            "enabled": True,
            "campaign": CAMPAIGN,
            "measurement_family": family,
            "response_gate": "go",
            "operator_checkpoint": "record both baselines before rank 0",
        }
    ]
    ladders = {
        "x": [2.0, 5.0, 10.0, 20.0, 35.0],
        "y": [2.0, 5.0, 10.0, 20.0, 35.0],
        "z": [1.0, 2.0, 3.5, 5.0],
    }
    seeds = {"x": 9100, "y": 9200, "z": 9300}
    bands = {"x": (42.5, 127.5), "y": (42.5, 127.5), "z": (202.5, 347.5)}
    designs: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for axis in "xyz":
        designs[axis] = split_excited_detection(
            odd_lines(*bands[axis]),
            seeds[axis],
            forbidden=(290.0, 305.0) if axis == "z" else None,
        )

    # Rank-major ordering is intentional: all axes at a lower response level
    # must retain the particle before the next rank can be selected.
    for rank in range(5):
        for axis in "xyz":
            if rank >= len(ladders[axis]):
                continue
            amplitude_um = ladders[axis][rank]
            excited, detection = designs[axis]
            gate = "hold" if (
                (axis == "x" and rank == 4)
                or (axis == "y" and rank >= 3)
                or (axis == "z" and rank == 3)
            ) else "go"
            experiment = {
                "name": f"1{rank}_{axis}_multisine_rung{amplitude_um:04.1f}um",
                "kind": "multisine",
                "axis": axis,
                "frequencies_hz": [float(value) for value in excited],
                "detection_lines_hz": [float(value) for value in detection],
                "target_acceleration_mm_s2": 1.0e7,
                "tone_displacement_cap_mm": amplitude_um * 1e-3,
                "seed": seeds[axis] + 10 * rank,
                "repeats": 2,
                "enabled": True,
                "campaign": CAMPAIGN,
                "measurement_family": family,
                "escalation_rank": rank,
                "response_gate": gate,
                "operator_checkpoint": (
                    "HOLD: proceed only after every lower rank retained the same particle"
                    if gate == "hold"
                    else "confirm retention and bilateral visibility before the next rank"
                ),
            }
            # The preregistered nonlinear simulation marked only the first z
            # rank-3 phase realization HOLD. Preserve that exact distinction.
            if axis == "z" and rank == 3:
                experiment["repeat_overrides"] = [
                    {"response_gate": "hold"},
                    {"response_gate": "go"},
                ]
            experiments.append(experiment)
    return {
        "schema_version": 1,
        "campaign": CAMPAIGN,
        "measurement_family": family,
        "purpose": "Small-amplitude random-odd multisine ladder for amplitude-dependent damping.",
        "defaults": {
            "sample_hz": 10000,
            "duration_sec": 10.0,
            "ramp_sec": 0.5,
            "safety_limits": {
                "max_offset_mm": 0.75,
                "max_speed_mm_s": 400.0,
                "max_acceleration_mm_s2": 400000.0,
            },
        },
        "experiments": experiments,
    }


def plan_b2() -> dict:
    family = "plan_b2_z_line_source"
    experiments: list[dict] = []
    probe_lines = odd_lines(262.5, 332.5)
    drive_scales = {"driveA": 1.0, "driveB": 0.85, "driveC": 0.70}
    for condition, drive_scale in drive_scales.items():
        common = {
            "campaign": CAMPAIGN,
            "measurement_family": family,
            "condition": condition,
            "drive_amplitude_scale": drive_scale,
            "response_gate": "go",
            "operator_checkpoint": "confirm the displayed drive scale and particle retention",
        }
        experiments.extend(
            [
                {
                    **common,
                    "name": f"20_{condition}_static_30s",
                    "kind": "static",
                    "axis": "z",
                    "duration_sec": 30.0,
                    "repeats": 1,
                },
                {
                    **common,
                    "name": f"21_{condition}_z_staircase_ringdown",
                    "kind": "staircase",
                    "axis": "z",
                    "hold_sec": 1.0,
                    "initial_hold_sec": 2.0,
                    "final_hold_sec": 1.0,
                    "amplitudes_mm": [0.25],
                    "directions": [1, -1],
                    "repeats_within_run": 2,
                    "order_seed": 4201,
                    "repeats": 1,
                },
                {
                    **common,
                    "name": f"22_{condition}_z_probe_multisine_262_332hz",
                    "kind": "multisine",
                    "axis": "z",
                    "frequencies_hz": [float(value) for value in probe_lines],
                    "target_acceleration_mm_s2": 1.0e7,
                    "tone_displacement_cap_mm": 0.001,
                    "seed": 4301,
                    "repeats": 1,
                },
            ]
        )
    for particle_number in (1, 2, 3):
        condition = f"particle{particle_number}"
        common = {
            "campaign": CAMPAIGN,
            "measurement_family": family,
            "condition": condition,
            "drive_amplitude_scale": 1.0,
            "response_gate": "go",
            "requires_operator_context": ["particle_id", "particle_description"],
            "operator_checkpoint": "load and identify the requested particle before capture",
        }
        experiments.extend(
            [
                {
                    **common,
                    "name": f"30_{condition}_static_30s",
                    "kind": "static",
                    "axis": "z",
                    "duration_sec": 30.0,
                    "repeats": 1,
                },
                {
                    **common,
                    "name": f"31_{condition}_z_staircase_ringdown",
                    "kind": "staircase",
                    "axis": "z",
                    "hold_sec": 1.0,
                    "initial_hold_sec": 2.0,
                    "final_hold_sec": 1.0,
                    "amplitudes_mm": [0.25],
                    "directions": [1, -1],
                    "repeats_within_run": 2,
                    "order_seed": 4200 + particle_number,
                    "repeats": 1,
                },
            ]
        )
    return {
        "schema_version": 1,
        "campaign": CAMPAIGN,
        "measurement_family": family,
        "purpose": "Distinguish the coherent z line's drive-level and particle dependence.",
        "defaults": {
            "sample_hz": 10000,
            "duration_sec": 10.0,
            "ramp_sec": 0.5,
            "safety_limits": {
                "max_offset_mm": 0.5,
                "max_step_mm": 0.5,
                "max_speed_mm_s": 200.0,
                "max_acceleration_mm_s2": 400000.0,
            },
        },
        "experiments": experiments,
    }


def plan_b3() -> dict:
    family = "plan_b3_drift_source"
    conditions = [
        ("shield_off_warm00", 0, "no enclosure, first capture after active PAT output"),
        ("shield_off_warm05", 5, "no enclosure, 5 min of active PAT output"),
        ("shield_off_warm10", 10, "no enclosure, 10 min of active PAT output"),
        ("shield_on_warm05", 5, "acrylic enclosure, 5 min of active PAT output"),
        (
            "shield_on_hvac_off_warm05",
            5,
            "enclosure and HVAC/fans off, 5 min of active PAT output",
        ),
        (
            "shield_off_hvac_off_warm05",
            5,
            "no enclosure and HVAC/fans off, 5 min of active PAT output",
        ),
    ]
    experiments = [
        {
            "name": f"4{index}_{condition}_static_60s",
            "kind": "static",
            "axis": "x",
            "duration_sec": 60.0,
            "repeats": 1,
            "enabled": True,
            "campaign": CAMPAIGN,
            "measurement_family": family,
            "condition": condition,
            "note": note,
            "warmup_definition": "elapsed time since confirmed non-zero PAT output",
            "nominal_active_warmup_minutes": warmup_minutes,
            "drive_amplitude_scale": 1.0,
            "response_gate": "go",
            "requires_operator_context": [
                "actual_warmup_minutes",
                "shield_state",
                "hvac_state",
                "temperature",
            ],
            "operator_checkpoint": "verify environment and temperature context before capture",
        }
        for index, (condition, warmup_minutes, note) in enumerate(conditions)
    ]
    return {
        "schema_version": 1,
        "campaign": CAMPAIGN,
        "measurement_family": family,
        "purpose": "Attribute sub-5-Hz drift to enclosure airflow, HVAC, or warm-up.",
        "defaults": {
            "sample_hz": 10000,
            "duration_sec": 60.0,
            "ramp_sec": 0.0,
            "safety_limits": {
                "max_offset_mm": 0.5,
                "max_speed_mm_s": 200.0,
                "max_acceleration_mm_s2": 400000.0,
            },
        },
        "experiments": experiments,
    }


def main() -> None:
    plans = {
        "B1_small_amplitude_multisine_plan.json": plan_b1(),
        "B2_z_line_source_plan.json": plan_b2(),
        "B3_drift_source_plan.json": plan_b3(),
    }
    for filename, plan in plans.items():
        path = HERE / filename
        path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
