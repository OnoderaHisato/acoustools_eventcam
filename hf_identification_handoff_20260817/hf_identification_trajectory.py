#!/usr/bin/env python3
"""Generate bounded high-frequency PAT trajectories for stereo identification.

The generated coordinates are offsets from the levitation centre.  Hardware
code must add its own centre before computing AcousTools holograms.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np


AXIS_INDEX = {"x": 0, "y": 1, "z": 2}
PAT_UPDATE_BASE_HZ = 40_000


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


def _generate_chirp(spec: dict[str, Any], time_sec: np.ndarray) -> tuple[np.ndarray, dict[str, Any]]:
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


def _generate_multisine(spec: dict[str, Any], time_sec: np.ndarray) -> tuple[np.ndarray, dict[str, Any]]:
    frequencies = np.asarray(spec["frequencies_hz"], dtype=float)
    if frequencies.ndim != 1 or frequencies.size == 0 or np.any(frequencies <= 0):
        raise ValueError("frequencies_hz must be a non-empty array of positive values")
    rng = np.random.default_rng(int(spec.get("seed", 0)))
    phases = rng.uniform(0.0, 2.0 * math.pi, frequencies.size)
    acceleration_per_tone = float(spec["target_acceleration_mm_s2"]) / math.sqrt(frequencies.size)
    amplitudes = acceleration_per_tone / (2.0 * math.pi * frequencies) ** 2
    tone_cap = float(spec.get("tone_displacement_cap_mm", np.inf))
    amplitudes = np.minimum(amplitudes, tone_cap)
    signal = np.sum(
        amplitudes[:, None]
        * np.sin(2.0 * math.pi * frequencies[:, None] * time_sec[None, :] + phases[:, None]),
        axis=0,
    )
    return signal, {
        "frequencies_hz": frequencies.tolist(),
        "phases_rad": phases.tolist(),
        "tone_amplitudes_mm_before_global_scaling": amplitudes.tolist(),
    }


def _safety_scale(offset_mm: np.ndarray, sample_hz: float, limits: dict[str, Any]) -> tuple[np.ndarray, float]:
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


def generate_trajectory(spec: dict[str, Any], defaults: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    sample_hz = float(spec.get("sample_hz", defaults["sample_hz"]))
    duration_sec = float(spec.get("duration_sec", defaults["duration_sec"]))
    if sample_hz <= 0 or duration_sec <= 0:
        raise ValueError("sample_hz and duration_sec must be positive")
    divider = PAT_UPDATE_BASE_HZ / sample_hz
    if abs(divider - round(divider)) > 1e-9:
        raise ValueError(f"sample_hz={sample_hz:g} must divide the {PAT_UPDATE_BASE_HZ} Hz PAT base rate")

    count = max(3, int(round(duration_sec * sample_hz)))
    time_sec = np.arange(count, dtype=float) / sample_hz
    axis = str(spec.get("axis", "x")).lower()
    if axis not in AXIS_INDEX:
        raise ValueError(f"Unknown axis: {axis}")
    kind = str(spec["kind"]).lower()
    detail: dict[str, Any] = {}
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

    signal *= smooth_envelope(count, sample_hz, float(spec.get("ramp_sec", defaults["ramp_sec"])))
    offset_mm = np.zeros((count, 3), dtype=float)
    offset_mm[:, AXIS_INDEX[axis]] = signal
    offset_mm, scale = _safety_scale(offset_mm, sample_hz, defaults["safety_limits"])
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
        "safety_limits": defaults["safety_limits"],
        "metrics": metrics,
        "generation_detail": detail,
    }
    return offset_mm, metadata


def positions_about_center(offset_mm: np.ndarray, center_m: tuple[float, float, float]) -> np.ndarray:
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
    comparisons = (
        ("max_abs_offset_mm", "max_offset_mm"),
        ("max_speed_mm_s", "max_speed_mm_s"),
        ("max_acceleration_mm_s2", "max_acceleration_mm_s2"),
    )
    for metric_name, limit_name in comparisons:
        if observed[metric_name]["vector"] > float(limits[limit_name]) * (1.0 + 1e-9):
            raise ValueError(f"Exported trajectory exceeds {limit_name} after loading")
    metadata = dict(metadata)
    metadata["hardware_command_scale"] = float(command_scale)
    metadata["hardware_load_metrics"] = observed
    positions = positions_about_center(offset_mm, center_m)
    return [tuple(float(value) for value in row) for row in positions], sample_hz, metadata


def write_offset_log(path: Path, offset_mm: np.ndarray, sample_hz: float) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "offset_x_m", "offset_y_m", "offset_z_m"])
        for index, row in enumerate(offset_mm * 1e-3):
            writer.writerow([f"{index / sample_hz:.9f}", *(f"{value:.9f}" for value in row)])


def write_preview(path: Path, offset_mm: np.ndarray, metadata: dict[str, Any]) -> None:
    import matplotlib.pyplot as plt

    time_sec = np.arange(offset_mm.shape[0]) / float(metadata["sample_hz"])
    figure, axes = plt.subplots(2, 1, figsize=(16, 6), constrained_layout=True)
    colors = ("#0072B2", "#D55E00", "#009E73")
    for index, axis in enumerate(("x", "y", "z")):
        axes[0].plot(time_sec, offset_mm[:, index], color=colors[index], linewidth=0.8, label=axis)
    axes[0].set_ylabel("Command offset [mm]")
    axes[0].legend(ncol=3, loc="upper right")
    axes[0].grid(alpha=0.25)
    active = offset_mm[:, AXIS_INDEX[metadata["axis"]]]
    edge_order = 2 if active.size >= 3 else 1
    acceleration = np.gradient(
        np.gradient(active, 1.0 / metadata["sample_hz"], edge_order=edge_order),
        1.0 / metadata["sample_hz"],
        edge_order=edge_order,
    )
    axes[1].plot(time_sec, acceleration, color="#CC79A7", linewidth=0.7, label="command acceleration")
    limit = float(metadata["safety_limits"]["max_acceleration_mm_s2"])
    axes[1].axhline(limit, color="black", linestyle="--", linewidth=0.8, label="safety limit")
    axes[1].axhline(-limit, color="black", linestyle="--", linewidth=0.8)
    axes[1].set_xlabel("Time [s]")
    axes[1].set_ylabel("Acceleration [mm/s²]")
    axes[1].legend(loc="upper right")
    axes[1].grid(alpha=0.25)
    figure.suptitle(metadata["name"])
    figure.savefig(path, dpi=180)
    plt.close(figure)


def export_plan(plan_path: Path, output_dir: Path, include_disabled: bool, write_csv: bool) -> dict[str, Any]:
    plan = load_plan(plan_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    runs = []
    for spec in plan["experiments"]:
        if not bool(spec.get("enabled", True)) and not include_disabled:
            continue
        repeats = int(spec.get("repeats", 1))
        for repeat_index in range(repeats):
            run_spec = dict(spec)
            run_spec["name"] = f"{spec['name']}_r{repeat_index + 1:02d}" if repeats > 1 else str(spec["name"])
            if run_spec["kind"] == "multisine":
                run_spec["seed"] = int(spec.get("seed", 0)) + repeat_index
            offset_mm, metadata = generate_trajectory(run_spec, plan["defaults"])
            run_dir = output_dir / metadata["name"]
            run_dir.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(
                run_dir / "command_trajectory.npz",
                time_sec=np.arange(offset_mm.shape[0]) / metadata["sample_hz"],
                offset_mm=offset_mm,
                offset_m=offset_mm * 1e-3,
                sample_hz=np.asarray(metadata["sample_hz"]),
            )
            if write_csv:
                write_offset_log(run_dir / "command_offset_log.csv", offset_mm, metadata["sample_hz"])
            write_preview(run_dir / "trajectory_preview.png", offset_mm, metadata)
            (run_dir / "trajectory_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            runs.append({"name": metadata["name"], "directory": str(run_dir), "metadata": metadata})

    digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "source_plan": str(plan_path.resolve()),
        "source_plan_sha256": digest,
        "coordinate_convention": "offset_mm, offset_m, and command_offset_log.csv are offsets from the hardware levitation centre",
        "delay_feedforward_applied": False,
        "runs": runs,
    }
    (output_dir / "export_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=Path(__file__).with_name("high_frequency_identification_plan.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("hf_identification_export"))
    parser.add_argument("--include-disabled", action="store_true", help="Also export diagnostic runs marked enabled=false")
    parser.add_argument("--write-csv", action="store_true", help="Write large command_offset_log.csv files in addition to NPZ")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = export_plan(args.plan.resolve(), args.output_dir.resolve(), args.include_disabled, args.write_csv)
    print(f"Exported {len(manifest['runs'])} runs to {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
