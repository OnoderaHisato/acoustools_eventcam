#!/usr/bin/env python3
"""JSON trajectory resolution and preview helpers for automated stereo 3D runs."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Iterable

from acoustools_eventcam_sync import compute_pat_frame_rate_info, sanitize_filename
from acoustools_multitraj_no_eventcam import TrajectoryParameters, save_trajectory_preview, trajectory_stats
from acoustools_cusped3d import Cusped3DParameters, generate_cusped_3d_positions
from acoustools_random3d_no_eventcam import (
    Chirped3DParameters,
    ExtendedRandom3DParameters,
    Random3DParameters,
    generate_chirped_3d_positions,
    generate_extended_random_3d_positions,
    generate_random_3d_positions,
)
from stereo_acoustools_3d_common import PROCESSING_CONFIG_FIELDS
from stereo_acoustools_3d_recording_core import PreparedTrajectory, prepare_parametric_trajectory


DEFAULT_AUTO_JSON = Path("long_random_3d_patterns_initial.json")
DEFAULT_AUTO_OUTPUT_DIR = Path("stereo_acoustools_3d_records_auto")
DEFAULT_PREVIEW_DIR = Path("long_random_3d_patterns_initial_preview_10kHz")
RANDOM_3D_SHAPES = {"long_random_3d", "long_random_3d_stroke", "random_3d_stroke"}
EXTENDED_RANDOM_3D_SHAPES = {
    "extended_random_3d", "extended_multiband_random_3d", "multiband_random_3d",
}
CHIRPED_3D_SHAPES = {"chirped_3d", "three_axis_chirp", "3d_chirp"}
CUSPED_3D_SHAPES = {"cusped_3d", "cusp_3d", "3d_cusp"}
PARAMETRIC_SHAPE_MODES = {
    "z_axis_vibration": 1, "elliptical_orbit": 2, "ellipse_xz": 2,
    "diagonal_line": 3, "heart": 4, "heart_shape": 4,
    "rectangular_orbit": 5, "rectangle_xz": 5, "vertical_figure_eight": 6,
    "horizontal_infinity": 7, "single_slanted_line": 8, "s_shaped_orbit": 9,
    "x_axis_vibration": 10, "figure_eight_3d": 12, "toroidal_helix": 13,
    "trefoil_knot": 14, "lissajous_3d": 15,
}
RANDOM_PARAMETER_FIELDS = {field.name for field in fields(Random3DParameters)}
EXTENDED_PARAMETER_FIELDS = {field.name for field in fields(ExtendedRandom3DParameters)}
CHIRPED_PARAMETER_FIELDS = {field.name for field in fields(Chirped3DParameters)}
CUSPED_PARAMETER_FIELDS = {field.name for field in fields(Cusped3DParameters)}
PARAMETRIC_PARAMETER_FIELDS = {
    "amp_x_mm", "amp_y_mm", "amp_z_mm", "amp_scale_mm",
    "helix_minor_radius_mm", "helix_turns", "steps_per_cycle",
    "frequency_hz", "loops", "max_speed_mm_s", "max_accel_mm_s2",
}


@dataclass(frozen=True)
class ParametricAutoParameters:
    amp_x_mm: float = 0.0
    amp_y_mm: float = 0.0
    amp_z_mm: float = 0.0
    amp_scale_mm: float = 0.0
    helix_minor_radius_mm: float = 0.0
    helix_turns: int = 3
    steps_per_cycle: int = 800
    frequency_hz: float = 10.0
    loops: int = 10
    max_speed_mm_s: float = 0.0
    max_accel_mm_s2: float = 0.0


@dataclass(frozen=True)
class AutoCondition:
    json_index: int
    label: str
    shape: str
    trajectory_kind: str
    params: Random3DParameters | ExtendedRandom3DParameters | Chirped3DParameters | Cusped3DParameters | ParametricAutoParameters
    generator_mode: int | None = None
    role: str = "train"
    risk_level: str = "standard"
    plan_group: str = ""
    source_plan: str = ""
    source_json_index: int = -1
    repeat: int = 1
    processing_config: dict[str, Any] | None = None


def _normalized_shape(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _safe_label(value: Any, json_index: int) -> str:
    text = sanitize_filename(str(value).strip(), max_len=48)
    return text or f"condition_{json_index:03d}"


def _typed_dataclass(cls: Any, values: dict[str, Any], json_index: int) -> Any:
    integer_fields = {
        "seed", "components", "chirps", "waypoints", "low_components",
        "mid_components", "high_components", "chirps_per_enabled_band",
        "helix_turns", "steps_per_cycle", "loops",
        "cusp_count", "out_of_plane_harmonic_multiple",
    }
    string_fields = {"sweep_law", "envelope", "curve", "plane"}
    try:
        converted = {
            field.name: (
                int(values[field.name]) if field.name in integer_fields
                else str(values[field.name]) if field.name in string_fields
                else float(values[field.name])
            )
            for field in fields(cls)
        }
        return cls(**converted)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"JSON entry #{json_index} contains invalid parameter types: {exc}") from exc


def _parse_processing_config(entry: dict[str, Any], json_index: int) -> dict[str, Any] | None:
    processing_config = entry.get("processing_config")
    if processing_config is not None and not isinstance(processing_config, dict):
        raise ValueError(f"JSON entry #{json_index} processing_config must be an object.")
    if isinstance(processing_config, dict):
        unknown = set(processing_config).difference(PROCESSING_CONFIG_FIELDS)
        if unknown:
            raise ValueError(
                f"JSON entry #{json_index} has unknown processing_config fields: {sorted(unknown)}"
            )
        return dict(processing_config)
    return None


def _load_plan_entries(
    json_path: Path,
    *,
    include_stack: tuple[Path, ...] = (),
    inherited_group: str = "",
) -> list[dict[str, Any]]:
    resolved = json_path.resolve()
    if resolved in include_stack:
        chain = " -> ".join(str(path) for path in (*include_stack, resolved))
        raise ValueError(f"Automatic trajectory JSON include cycle: {chain}")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Automatic trajectory JSON was not found: {resolved}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON at {resolved}: {exc}") from exc

    if isinstance(payload, list):
        entries: list[dict[str, Any]] = []
        for source_index, entry in enumerate(payload):
            if not isinstance(entry, dict):
                raise ValueError(f"JSON entry #{source_index} in {resolved} must be an object.")
            copied = dict(entry)
            copied.setdefault("_source_plan", str(resolved))
            copied.setdefault("_source_json_index", source_index)
            if inherited_group:
                copied.setdefault("_plan_group", inherited_group)
            entries.append(copied)
        return entries

    if not isinstance(payload, dict) or not isinstance(payload.get("includes"), list):
        raise ValueError(
            "Automatic trajectory JSON root must be a list or an object with an includes list."
        )
    includes = payload["includes"]
    combined: list[dict[str, Any]] = []
    for include_index, include in enumerate(includes):
        if isinstance(include, str):
            relative_path, enabled, group = include, True, inherited_group
        elif isinstance(include, dict):
            relative_path = str(include.get("path", "")).strip()
            enabled = include.get("enabled", True) is not False
            group = str(include.get("group", inherited_group)).strip()
        else:
            raise ValueError(f"Include #{include_index} in {resolved} must be a string or object.")
        if not enabled:
            continue
        if not relative_path:
            raise ValueError(f"Include #{include_index} in {resolved} has no path.")
        include_path = Path(relative_path)
        if not include_path.is_absolute():
            include_path = resolved.parent / include_path
        combined.extend(
            _load_plan_entries(
                include_path,
                include_stack=(*include_stack, resolved),
                inherited_group=group,
            )
        )
    if not combined:
        raise ValueError(f"No enabled included trajectory entries were found in {resolved}.")
    return combined


def load_auto_conditions(path: str | Path) -> tuple[Path, list[AutoCondition]]:
    json_path = Path(path)
    if not json_path.is_absolute():
        json_path = Path(__file__).resolve().parent / json_path
    json_path = json_path.resolve()
    payload = _load_plan_entries(json_path)

    conditions: list[AutoCondition] = []
    for json_index, entry in enumerate(payload):
        if entry.get("enabled", True) is False:
            continue
        shape = _normalized_shape(entry.get("shape", ""))
        raw_params = entry.get("params", {})
        if not isinstance(raw_params, dict):
            raise ValueError(f"JSON entry #{json_index} params must be an object.")
        if shape in RANDOM_3D_SHAPES:
            cls, allowed, kind, generator_mode = Random3DParameters, RANDOM_PARAMETER_FIELDS, "random_3d", None
        elif shape in EXTENDED_RANDOM_3D_SHAPES:
            cls, allowed, kind, generator_mode = ExtendedRandom3DParameters, EXTENDED_PARAMETER_FIELDS, "extended_random_3d", None
        elif shape in CHIRPED_3D_SHAPES:
            cls, allowed, kind, generator_mode = Chirped3DParameters, CHIRPED_PARAMETER_FIELDS, "chirped_3d", None
        elif shape in CUSPED_3D_SHAPES:
            cls, allowed, kind, generator_mode = Cusped3DParameters, CUSPED_PARAMETER_FIELDS, "cusped_3d", None
        elif shape in PARAMETRIC_SHAPE_MODES:
            cls, allowed, kind = ParametricAutoParameters, PARAMETRIC_PARAMETER_FIELDS, "parametric"
            generator_mode = PARAMETRIC_SHAPE_MODES[shape]
        else:
            raise ValueError(
                f"JSON entry #{json_index} has unsupported shape {entry.get('shape')!r}; "
                "use Long_Random_3D, Extended_Random_3D, Chirped_3D, Cusped_3D, "
                "or a documented parametric shape."
            )
        unknown = set(raw_params).difference(allowed | {"label"})
        if unknown:
            raise ValueError(f"JSON entry #{json_index} has unknown parameters: {sorted(unknown)}")
        values = asdict(cls())
        values.update({key: value for key, value in raw_params.items() if key in allowed})
        params = _typed_dataclass(cls, values, json_index)
        if kind == "parametric" and (
            params.max_speed_mm_s <= 0.0 or params.max_accel_mm_s2 <= 0.0
        ):
            raise ValueError(
                f"JSON entry #{json_index} parametric shapes require positive "
                "max_speed_mm_s and max_accel_mm_s2 safety limits."
            )
        repeat = int(entry.get("repeat", 1))
        if repeat < 1:
            raise ValueError(f"JSON entry #{json_index} repeat must be at least 1.")
        role = str(entry.get("role", "train")).strip().lower()
        if role not in {"train", "validation", "diagnostic"}:
            raise ValueError(
                f"JSON entry #{json_index} role must be train, validation, or diagnostic."
            )
        risk_level = str(entry.get("risk_level", "standard")).strip().lower()
        if risk_level not in {"standard", "challenge", "retention_boundary"}:
            raise ValueError(
                f"JSON entry #{json_index} risk_level must be standard, challenge, "
                "or retention_boundary."
            )
        if risk_level == "retention_boundary" and role != "diagnostic":
            raise ValueError(
                f"JSON entry #{json_index} retention_boundary conditions must use role=diagnostic."
            )
        conditions.append(AutoCondition(
            json_index=json_index,
            label=_safe_label(raw_params.get("label", entry.get("label", "")), json_index),
            shape=shape, trajectory_kind=kind, params=params, generator_mode=generator_mode,
            role=role, risk_level=risk_level, repeat=repeat,
            plan_group=str(entry.get("_plan_group", "")).strip(),
            source_plan=str(entry.get("_source_plan", json_path)),
            source_json_index=int(entry.get("_source_json_index", json_index)),
            processing_config=_parse_processing_config(entry, json_index),
        ))
    if not conditions:
        raise ValueError(f"No enabled automatic trajectory conditions were found in {json_path}.")
    return json_path, conditions


def select_conditions(
    conditions: Iterable[AutoCondition], *, start_index: int = 0,
    limit: int = 0, labels: Iterable[str] = (), roles: Iterable[str] = (),
) -> list[AutoCondition]:
    label_filter = {str(label).strip().lower() for label in labels if str(label).strip()}
    role_filter = {str(role).strip().lower() for role in roles if str(role).strip()}
    selected = [condition for condition in conditions
                if condition.json_index >= max(0, int(start_index))
                and (not label_filter or condition.label.lower() in label_filter)
                and (not role_filter or condition.role in role_filter)]
    return selected[: int(limit)] if int(limit) > 0 else selected


def _scale_to_limits(
    trajectory: PreparedTrajectory, centre: tuple[float, float, float],
    max_speed_mm_s: float, max_accel_mm_s2: float,
) -> PreparedTrajectory:
    scale = 1.0
    if max_speed_mm_s > 0.0 and trajectory.stats["max_speed_mm_s"] > max_speed_mm_s:
        scale = min(scale, max_speed_mm_s / trajectory.stats["max_speed_mm_s"])
    if max_accel_mm_s2 > 0.0 and trajectory.stats["max_accel_mm_s2"] > max_accel_mm_s2:
        scale = min(scale, max_accel_mm_s2 / trajectory.stats["max_accel_mm_s2"])
    if scale >= 1.0:
        stats = dict(trajectory.stats)
        stats["amplitude_scale_applied"] = 1.0
        return replace(trajectory, stats=stats)
    positions = [tuple(centre[axis] + (point[axis] - centre[axis]) * scale for axis in range(3))
                 for point in trajectory.positions]
    stats = trajectory_stats(
        positions, float(trajectory.rate.effective_hz or trajectory.rate.requested_hz),
        trajectory.closed_cycle,
    )
    stats["amplitude_scale_applied"] = scale
    return replace(trajectory, positions=positions, stats=stats)


def prepare_auto_trajectory(
    condition: AutoCondition, repeat_index: int = 0,
    centre: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> PreparedTrajectory:
    if repeat_index < 0 or repeat_index >= condition.repeat:
        raise ValueError("repeat_index is outside this condition's repeat range")
    repeat_tag = f"_r{repeat_index + 1:02d}" if condition.repeat > 1 else ""
    run_label = f"{condition.json_index:03d}_{condition.label}{repeat_tag}"
    if condition.trajectory_kind == "cusped_3d":
        params = condition.params
        rate = compute_pat_frame_rate_info(params.steps_per_cycle, params.frequency_hz)
        if not rate.is_supported:
            raise ValueError(
                f"{condition.label}: steps*frequency="
                f"{params.steps_per_cycle * params.frequency_hz:g} Hz is not an exact "
                "40 kHz PAT divider."
            )
        positions, stats = generate_cusped_3d_positions(params, centre)
        parameters = asdict(params)
        parameters["label"] = condition.label
        prepared = PreparedTrajectory(
            shape_name="cusped_3d", positions=positions, n_steps=len(positions),
            frequency_hz=params.frequency_hz, loops=params.loops, rate=rate,
            parameters=parameters, closed_cycle=True, stats=stats, run_label=run_label,
        )
        _require_non_degenerate(prepared, condition.label)
        return prepared

    if condition.trajectory_kind in {"random_3d", "extended_random_3d", "chirped_3d"}:
        params = condition.params
        rate = compute_pat_frame_rate_info(1, params.sample_hz)
        if not rate.is_supported:
            raise ValueError(
                f"{condition.label}: sample_hz={params.sample_hz:g} is not an exact 40 kHz PAT divider."
            )
        if condition.trajectory_kind == "random_3d":
            positions, stats = generate_random_3d_positions(params, centre)
            shape_name = "long_random_3d"
        elif condition.trajectory_kind == "extended_random_3d":
            positions, stats = generate_extended_random_3d_positions(params, centre)
            shape_name = "extended_random_3d"
        else:
            positions, stats = generate_chirped_3d_positions(params, centre)
            shape_name = "chirped_3d"
        parameters = asdict(params)
        parameters["label"] = condition.label
        prepared = PreparedTrajectory(
            shape_name=shape_name, positions=positions, n_steps=len(positions),
            frequency_hz=params.sample_hz / len(positions), loops=1, rate=rate,
            parameters=parameters, closed_cycle=False, stats=stats, run_label=run_label,
        )
        _require_non_degenerate(prepared, condition.label)
        return prepared

    params = condition.params
    rate = compute_pat_frame_rate_info(params.steps_per_cycle, params.frequency_hz)
    if not rate.is_supported:
        raise ValueError(
            f"{condition.label}: steps*frequency={params.steps_per_cycle * params.frequency_hz:g} Hz "
            "is not an exact 40 kHz PAT divider."
        )
    trajectory_params = TrajectoryParameters(
        amp_x_mm=params.amp_x_mm, amp_y_mm=params.amp_y_mm,
        amp_z_mm=params.amp_z_mm, amp_scale_mm=params.amp_scale_mm,
        helix_minor_radius_mm=params.helix_minor_radius_mm,
        helix_turns=params.helix_turns,
    )
    prepared = prepare_parametric_trajectory(
        int(condition.generator_mode), centre, trajectory_params,
        (params.steps_per_cycle, params.frequency_hz, params.loops, rate),
    )
    parameters = dict(prepared.parameters)
    parameters.update({"label": condition.label, "max_speed_mm_s": params.max_speed_mm_s,
                       "max_accel_mm_s2": params.max_accel_mm_s2})
    prepared = replace(prepared, parameters=parameters, run_label=run_label)
    prepared = _scale_to_limits(
        prepared, centre, params.max_speed_mm_s, params.max_accel_mm_s2
    )
    _require_non_degenerate(prepared, condition.label)
    return prepared


def _require_non_degenerate(trajectory: PreparedTrajectory, label: str) -> None:
    spans = [
        max(point[axis] for point in trajectory.positions)
        - min(point[axis] for point in trajectory.positions)
        for axis in range(3)
    ]
    if max(spans) <= 1.0e-9:
        raise ValueError(f"{label}: generated trajectory is degenerate (all axes are static).")


def automation_metadata(condition: AutoCondition, json_path: Path, repeat_index: int) -> dict[str, Any]:
    return {
        "schema_version": 2, "source_json": str(json_path.resolve()),
        "json_index": condition.json_index, "label": condition.label,
        "shape": condition.shape, "trajectory_kind": condition.trajectory_kind,
        "dataset_role": condition.role, "risk_level": condition.risk_level,
        "plan_group": condition.plan_group, "source_plan": condition.source_plan,
        "source_json_index": condition.source_json_index,
        "repeat_index": repeat_index, "repeat_number": repeat_index + 1,
        "repeat_count": condition.repeat,
    }


def write_preview_set(conditions: Iterable[AutoCondition], output_dir: str | Path) -> Path:
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    resolved: list[tuple[AutoCondition, PreparedTrajectory]] = []
    for condition in conditions:
        trajectory = prepare_auto_trajectory(condition)
        resolved.append((condition, trajectory))
        preview_path = output / f"{condition.json_index:02d}_{condition.label}.png"
        if not save_trajectory_preview(str(preview_path), trajectory.positions, (0.0, 0.0, 0.0),
                                       f"{condition.label}: {condition.shape}"):
            raise RuntimeError(f"Could not write trajectory preview: {preview_path}")

    fieldnames = [
        "json_index", "source_json_index", "plan_group", "source_plan", "label", "shape",
        "trajectory_kind", "dataset_role", "risk_level", "points", "duration_sec",
        "sample_hz", "x_min_mm", "x_max_mm", "y_min_mm", "y_max_mm", "z_min_mm",
        "z_max_mm", "frequency_spec", "sweep_law", "envelope", "curve", "cusp_count",
        "rounding", "cusp_to_peak_speed_ratio", "highest_geometric_harmonic",
        "highest_geometric_frequency_hz",
        "max_speed_mm_s", "max_accel_mm_s2", "amplitude_scale_applied",
    ]
    with (output / "trajectory_preview_summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for condition, trajectory in resolved:
            stats, bounds = trajectory.stats, trajectory.stats.get("bounds_mm", {})
            sample_hz = float(trajectory.rate.effective_hz or trajectory.rate.requested_hz)
            axis_chirps = stats.get("axis_chirps", {})
            frequency_spec = ";".join(
                f"{axis.upper()}:{values['f_start_hz']:g}->{values['f_end_hz']:g}Hz"
                for axis, values in axis_chirps.items()
                if float(values.get("amplitude_mm", 0.0)) > 0.0
            )
            if not frequency_spec and "base_frequency_hz" in stats:
                frequency_spec = (
                    f"base:{stats['base_frequency_hz']:g}Hz;"
                    f"highest_geometry:{stats['highest_geometric_frequency_hz']:g}Hz"
                )
            writer.writerow({
                "json_index": condition.json_index, "label": condition.label,
                "source_json_index": condition.source_json_index,
                "plan_group": condition.plan_group, "source_plan": condition.source_plan,
                "shape": condition.shape, "trajectory_kind": condition.trajectory_kind,
                "dataset_role": condition.role, "risk_level": condition.risk_level,
                "points": len(trajectory.positions),
                "duration_sec": len(trajectory.positions) * trajectory.loops / sample_hz,
                "sample_hz": sample_hz,
                "x_min_mm": bounds.get("x", {}).get("min", ""), "x_max_mm": bounds.get("x", {}).get("max", ""),
                "y_min_mm": bounds.get("y", {}).get("min", ""), "y_max_mm": bounds.get("y", {}).get("max", ""),
                "z_min_mm": bounds.get("z", {}).get("min", ""), "z_max_mm": bounds.get("z", {}).get("max", ""),
                "frequency_spec": frequency_spec,
                "sweep_law": stats.get("sweep_law", ""),
                "envelope": stats.get("envelope", ""),
                "curve": stats.get("curve", ""),
                "cusp_count": stats.get("cusp_count", ""),
                "rounding": stats.get("rounding", ""),
                "cusp_to_peak_speed_ratio": stats.get("cusp_to_peak_speed_ratio", ""),
                "highest_geometric_harmonic": stats.get("highest_geometric_harmonic", ""),
                "highest_geometric_frequency_hz": stats.get(
                    "highest_geometric_frequency_hz", ""
                ),
                "max_speed_mm_s": stats.get("max_speed_mm_s", ""),
                "max_accel_mm_s2": stats.get("max_accel_mm_s2", ""),
                "amplitude_scale_applied": stats.get("amplitude_scale_applied", 1.0),
            })
    _write_overview(resolved, output / "trajectory_overview.png")
    return output


def _write_overview(resolved: list[tuple[AutoCondition, PreparedTrajectory]], output_path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    columns = min(4, max(1, len(resolved)))
    rows = (len(resolved) + columns - 1) // columns
    figure = plt.figure(figsize=(4.2 * columns, 3.8 * rows), constrained_layout=True)
    for panel, (condition, trajectory) in enumerate(resolved, start=1):
        axis = figure.add_subplot(rows, columns, panel, projection="3d")
        sampled = trajectory.positions[::max(1, len(trajectory.positions) // 4000)]
        x_mm = [point[0] * 1e3 for point in sampled]
        y_mm = [point[1] * 1e3 for point in sampled]
        z_mm = [point[2] * 1e3 for point in sampled]
        axis.plot(x_mm, y_mm, z_mm, linewidth=0.65)
        axis.scatter(x_mm[:1], y_mm[:1], z_mm[:1], s=18, color="green")
        axis.scatter(x_mm[-1:], y_mm[-1:], z_mm[-1:], s=18, color="red")
        axis.set_title(f"{condition.json_index:02d} {condition.label}", fontsize=9)
        axis.set_xlabel("X [mm]"); axis.set_ylabel("Y [mm]"); axis.set_zlabel("Z [mm]")
        axis.view_init(elev=24, azim=-55)
    figure.suptitle("Automated stereo 3D trajectory set", fontsize=13)
    figure.savefig(output_path, dpi=160)
    plt.close(figure)
