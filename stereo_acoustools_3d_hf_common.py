#!/usr/bin/env python3
"""High-frequency identification export adapter for stereo PAT recording."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

from acoustools_eventcam_sync import compute_pat_frame_rate_info
from acoustools_multitraj_no_eventcam import trajectory_stats
from hf_identification_trajectory import (
    FEEDFORWARD_VALIDATION_FAMILY,
    VZR_IDENTIFICATION_FAMILY,
    load_reference_for_hardware,
    load_trajectory_for_hardware,
)
from stereo_acoustools_3d_recording_core import PreparedTrajectory


PLAN_B_CAMPAIGN = "plan_b_20260825"
PLAN_B_FAMILIES = frozenset(
    {
        "plan_b1_small_amplitude_multisine",
        "plan_b2_z_line_source",
        "plan_b3_drift_source",
    }
)


def is_vzr_metadata(metadata: dict[str, Any] | None) -> bool:
    """True for the 2026-09-17 vzr identification family (multi-axis sine runs)."""
    if not isinstance(metadata, dict):
        return False
    return str(metadata.get("measurement_family", "")).strip() == VZR_IDENTIFICATION_FAMILY


def is_feedforward_validation_metadata(metadata: dict[str, Any] | None) -> bool:
    """True for imported feedforward-validation commands (FF heart, 2026-09-18)."""
    if not isinstance(metadata, dict):
        return False
    return (
        str(metadata.get("measurement_family", "")).strip()
        == FEEDFORWARD_VALIDATION_FAMILY
    )


@dataclass(frozen=True)
class HfExportRun:
    """One already-exported identification command and its immutable inputs."""

    export_index: int
    name: str
    directory: Path
    metadata: dict[str, Any]
    command_npz: Path
    metadata_json: Path
    offset_csv: Path
    preview_png: Path
    repeat: int = 1
    processing_config: dict[str, Any] | None = None

    @property
    def label(self) -> str:
        return self.name

    @property
    def json_index(self) -> int:
        return self.export_index


@dataclass(frozen=True)
class HfRunSelection:
    """One requested export/scale pair; duplicate exports are allowed."""

    run: HfExportRun
    command_scale: float

    @property
    def repeat(self) -> int:
        return 1

    @property
    def processing_config(self) -> dict[str, Any] | None:
        return self.run.processing_config

    @property
    def label(self) -> str:
        return self.run.label

    @property
    def json_index(self) -> int:
        return self.run.json_index

    @property
    def export_index(self) -> int:
        return self.run.export_index

    @property
    def name(self) -> str:
        return self.run.name

    @property
    def metadata(self) -> dict[str, Any]:
        return self.run.metadata

    @property
    def preview_png(self) -> Path:
        return self.run.preview_png


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_hf_export_runs(export_dir: str | Path) -> tuple[Path, list[HfExportRun]]:
    root = Path(export_dir).resolve()
    manifest_path = root / "export_manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"High-frequency export manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if int(manifest.get("schema_version", 0)) != 1:
        raise ValueError("Only high-frequency export schema_version=1 is supported")
    if manifest.get("delay_feedforward_applied") is not False:
        raise ValueError("Identification export must explicitly disable delay feedforward")
    raw_runs = manifest.get("runs")
    if not isinstance(raw_runs, list) or not raw_runs:
        raise ValueError("High-frequency export manifest contains no runs")

    runs: list[HfExportRun] = []
    names: set[str] = set()
    for export_index, raw in enumerate(raw_runs):
        if not isinstance(raw, dict):
            raise ValueError(f"High-frequency export run #{export_index} is not an object")
        name = str(raw.get("name", "")).strip()
        if not name or Path(name).name != name:
            raise ValueError(f"Invalid high-frequency export run name: {name!r}")
        if name.lower() in names:
            raise ValueError(f"Duplicate high-frequency export run name: {name}")
        names.add(name.lower())
        run_dir = (root / name).resolve()
        try:
            run_dir.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"High-frequency export run escapes its root: {run_dir}") from exc
        command_npz = run_dir / "command_trajectory.npz"
        metadata_json = run_dir / "trajectory_metadata.json"
        offset_csv = run_dir / "command_offset_log.csv"
        preview_png = run_dir / "trajectory_preview.png"
        missing = [
            path.name
            for path in (command_npz, metadata_json, offset_csv, preview_png)
            if not path.is_file()
        ]
        if missing:
            raise ValueError(f"{name}: missing exported input(s): {', '.join(missing)}")
        metadata = json.loads(metadata_json.read_text(encoding="utf-8"))
        if str(metadata.get("name", "")) != name:
            raise ValueError(f"{name}: trajectory_metadata.json name does not match directory")
        if not bool(metadata.get("enabled", True)):
            raise ValueError(f"{name}: disabled diagnostic commands cannot be recorded")
        if str(metadata.get("kind", "")).lower() == "staircase":
            family = str(metadata.get("measurement_family", "")).strip()
            tier = str(metadata.get("step_response_tier", "")).strip().upper()
            if family == "step_response_identification":
                if tier not in {"A", "B", "C", "D", "E", "F", "G", "H"}:
                    raise ValueError(
                        f"{name}: staircase metadata requires tier A through H"
                    )
                escape_probe = bool(metadata.get("escape_boundary_probe_enabled", False))
                if escape_probe != (tier == "H"):
                    raise ValueError(
                        f"{name}: escape-boundary probe metadata is valid only for tier H, "
                        "and every tier-H export must enable it"
                    )
            elif family == "plan_b2_z_line_source":
                if str(metadata.get("campaign", "")) != PLAN_B_CAMPAIGN:
                    raise ValueError(f"{name}: Plan B2 staircase campaign metadata is missing")
                if tier:
                    raise ValueError(f"{name}: Plan B2 staircase must not use a step-response tier")
                safety = metadata.get("generation_detail", {}).get("safety", {})
                if bool(safety.get("escape_boundary_exceeded", True)):
                    raise ValueError(f"{name}: Plan B2 staircase must remain below escape")
            else:
                raise ValueError(
                    f"{name}: staircase metadata has unsupported measurement family {family!r}"
                )
        kind = str(metadata.get("kind", "")).lower()
        if kind == "multitone":
            axes = metadata.get("axes")
            components = metadata.get("generation_detail", {}).get("components")
            if (
                not isinstance(axes, list)
                or not axes
                or not isinstance(components, list)
                or not components
            ):
                raise ValueError(
                    f"{name}: multitone metadata requires axes and generation_detail.components"
                )
            if float(metadata.get("safety_scale_applied", 0.0)) != 1.0:
                raise ValueError(f"{name}: multitone exports must not be rescaled at generation")
        if is_vzr_metadata(metadata) and kind not in {"multitone", "static"}:
            raise ValueError(
                f"{name}: vzr identification exports must be multitone or static commands"
            )
        if kind == "imported":
            detail = metadata.get("generation_detail", {})
            if not detail.get("source_sha256") or not detail.get("positions_sha256"):
                raise ValueError(
                    f"{name}: imported metadata requires source_sha256 and positions_sha256"
                )
            if float(metadata.get("safety_scale_applied", 0.0)) != 1.0:
                raise ValueError(f"{name}: imported exports must not be rescaled at generation")
        if is_feedforward_validation_metadata(metadata):
            if kind != "imported":
                raise ValueError(
                    f"{name}: feedforward validation exports must be imported commands"
                )
            design = metadata.get("feedforward_design")
            if not isinstance(design, dict) or not str(design.get("design", "")).strip():
                raise ValueError(
                    f"{name}: feedforward validation metadata requires feedforward_design.design"
                )
            if not metadata.get("generation_detail", {}).get("reference_available"):
                raise ValueError(
                    f"{name}: feedforward validation requires the reference trajectory"
                )
        campaign = str(metadata.get("campaign", "")).strip()
        family = str(metadata.get("measurement_family", "")).strip()
        if campaign == PLAN_B_CAMPAIGN and family not in PLAN_B_FAMILIES:
            raise ValueError(f"{name}: unsupported Plan B measurement family {family!r}")
        if family in PLAN_B_FAMILIES and campaign != PLAN_B_CAMPAIGN:
            raise ValueError(f"{name}: Plan B family requires campaign={PLAN_B_CAMPAIGN}")
        drive_scale = float(metadata.get("drive_amplitude_scale", 1.0))
        if not math.isfinite(drive_scale) or not 0.0 < drive_scale <= 1.0:
            raise ValueError(f"{name}: drive_amplitude_scale must be in (0, 1]")
        runs.append(
            HfExportRun(
                export_index=export_index,
                name=name,
                directory=run_dir,
                metadata=metadata,
                command_npz=command_npz,
                metadata_json=metadata_json,
                offset_csv=offset_csv,
                preview_png=preview_png,
            )
        )
    return manifest_path, runs


def select_hf_runs(
    runs: Iterable[HfExportRun],
    *,
    start_index: int = 0,
    limit: int = 0,
    labels: Iterable[str] = (),
) -> list[HfExportRun]:
    if start_index < 0:
        raise ValueError("start_index must be non-negative")
    label_filter = {str(label).strip().lower() for label in labels if str(label).strip()}
    selected = [
        run
        for run in runs
        if run.export_index >= int(start_index)
        and (not label_filter or run.name.lower() in label_filter)
    ]
    if int(limit) > 0:
        selected = selected[: int(limit)]
    if label_filter:
        found = {run.name.lower() for run in selected}
        missing = sorted(label_filter - found)
        if missing:
            raise ValueError(f"Unknown or excluded high-frequency label(s): {', '.join(missing)}")
    return selected


def hf_run_selections(
    runs: Iterable[HfExportRun], command_scale: float
) -> list[HfRunSelection]:
    scale = float(command_scale)
    if not math.isfinite(scale) or not 0.0 < scale <= 1.0:
        raise ValueError("HF command scale must be in the interval (0, 1]")
    return [HfRunSelection(run=run, command_scale=scale) for run in runs]


def resolve_hf_run_requests(
    runs: Iterable[HfExportRun], requests: Iterable[str]
) -> list[HfRunSelection]:
    """Resolve ordered, repeatable ``LABEL=SCALE`` HF run requests."""
    by_name = {run.name.lower(): run for run in runs}
    selected: list[HfRunSelection] = []
    for raw_request in requests:
        request = str(raw_request).strip()
        if "=" not in request:
            raise ValueError(f"HF run request must use LABEL=SCALE: {request!r}")
        label, raw_scale = request.rsplit("=", 1)
        label = label.strip()
        if not label:
            raise ValueError(f"HF run request has an empty label: {request!r}")
        run = by_name.get(label.lower())
        if run is None:
            raise ValueError(f"Unknown high-frequency label in --hf-run: {label}")
        try:
            scale = float(raw_scale)
        except ValueError as exc:
            raise ValueError(f"Invalid HF command scale in --hf-run: {request!r}") from exc
        if not math.isfinite(scale) or not 0.0 < scale <= 1.0:
            raise ValueError(f"HF command scale must be in (0, 1]: {request!r}")
        selected.append(HfRunSelection(run=run, command_scale=scale))
    if not selected:
        raise ValueError("At least one --hf-run LABEL=SCALE request is required")
    return selected


def prepare_hf_trajectory(
    run: HfExportRun,
    *,
    center_m: tuple[float, float, float],
    command_scale: float,
) -> PreparedTrajectory:
    positions, sample_hz, loader_metadata = load_trajectory_for_hardware(
        run.command_npz,
        center_m=center_m,
        command_scale=command_scale,
    )
    rate = compute_pat_frame_rate_info(1, sample_hz)
    if not rate.is_supported:
        raise ValueError(
            f"{run.name}: sample_hz={sample_hz:g} is not an exact 40 kHz PAT divider"
        )
    stats = trajectory_stats(positions, sample_hz, False)
    metrics = loader_metadata["hardware_load_metrics"]
    stats["identification_command_metrics"] = metrics
    stats["hardware_command_scale"] = float(command_scale)
    if "hardware_load_safety" in loader_metadata:
        stats["identification_command_safety"] = loader_metadata["hardware_load_safety"]
    scale_tag = int(round(float(command_scale) * 100.0))
    is_staircase = str(loader_metadata.get("kind", "")).lower() == "staircase"
    measurement_family = str(loader_metadata.get("measurement_family", "")).strip()
    is_step_response = is_staircase and measurement_family == "step_response_identification"
    is_plan_b = str(loader_metadata.get("campaign", "")) == PLAN_B_CAMPAIGN
    is_vzr = is_vzr_metadata(loader_metadata)
    is_feedforward_validation = is_feedforward_validation_metadata(loader_metadata)
    is_multitone = str(loader_metadata.get("kind", "")).lower() == "multitone"
    is_imported = str(loader_metadata.get("kind", "")).lower() == "imported"
    reference_positions = (
        load_reference_for_hardware(
            run.command_npz, center_m=center_m, command_scale=command_scale
        )
        if is_imported
        else None
    )
    drive_amplitude_scale = float(loader_metadata.get("drive_amplitude_scale", 1.0))
    source_kind = (
        "step_response_identification"
        if is_step_response
        else measurement_family or "high_frequency_identification"
    )
    trajectory_source = {
        "kind": source_kind,
        "name": run.name,
        "export_index": run.export_index,
        "command_scale": float(command_scale),
        "drive_amplitude_scale": drive_amplitude_scale,
        "delay_feedforward_applied": False,
        "levitation_center_m": [float(value) for value in center_m],
        "source_command_npz_sha256": _sha256(run.command_npz),
        "source_metadata_sha256": _sha256(run.metadata_json),
        "loader_metadata": loader_metadata,
    }
    if is_staircase:
        trajectory_source["intentional_discontinuous_command"] = True
    if is_step_response:
        trajectory_source["step_response_tier"] = loader_metadata["step_response_tier"]
    for field in (
        "campaign",
        "measurement_family",
        "condition",
        "note",
        "escalation_rank",
        "response_gate",
        "particle_id",
        "operator_checkpoint",
        "requires_operator_context",
        "reference",
        "feedforward_design",
    ):
        if field in loader_metadata:
            trajectory_source[field] = loader_metadata[field]
    if is_imported:
        design = loader_metadata.get("feedforward_design", {})
        trajectory_source["imported_command"] = True
        trajectory_source["reference_trajectory_logged"] = reference_positions is not None
        # delay_feedforward_applied=False means the acquisition side added nothing.
        # An imported command may still contain feedforward designed offline.
        trajectory_source["command_contains_offline_feedforward"] = bool(
            isinstance(design, dict) and design.get("command_differs_from_reference", False)
        )
    parameters = {
        "identification_input": True,
        "name": run.name,
        "kind": loader_metadata["kind"],
        "axis": loader_metadata["axis"],
        "sample_hz": sample_hz,
        "duration_sec": loader_metadata["duration_sec"],
        "command_scale": float(command_scale),
        "drive_amplitude_scale": drive_amplitude_scale,
        "delay_feedforward_applied": False,
    }
    if is_staircase:
        parameters["intentional_discontinuous_command"] = True
    if is_multitone:
        parameters["axes"] = [str(axis) for axis in loader_metadata.get("axes", [])]
        parameters["components"] = list(
            loader_metadata.get("generation_detail", {}).get("components", [])
        )
    if is_imported:
        parameters["axes"] = [str(axis) for axis in loader_metadata.get("axes", [])]
        parameters["imported_command"] = True
        parameters["reference_trajectory_logged"] = reference_positions is not None
        if "feedforward_design" in loader_metadata:
            parameters["feedforward_design"] = loader_metadata["feedforward_design"]
    if is_step_response:
        parameters["measurement_family"] = "step_response_identification"
        parameters["step_response_tier"] = loader_metadata["step_response_tier"]
    elif measurement_family:
        parameters["measurement_family"] = measurement_family
    return PreparedTrajectory(
        shape_name=(
            "step_response_identification"
            if is_step_response
            else "plan_b_identification" if is_plan_b
            else "vzr_identification" if is_vzr
            else "feedforward_validation" if is_feedforward_validation
            else "hf_identification"
        ),
        positions=positions,
        n_steps=len(positions),
        frequency_hz=sample_hz / len(positions),
        loops=1,
        rate=rate,
        parameters=parameters,
        closed_cycle=False,
        stats=stats,
        run_label=f"{run.export_index:03d}_{run.name}_scale{scale_tag:03d}",
        source_artifacts={
            "command_trajectory.npz": run.command_npz,
            "trajectory_metadata.json": run.metadata_json,
            "command_offset_log.csv": run.offset_csv,
            "command_trajectory_preview.png": run.preview_png,
        },
        trajectory_source=trajectory_source,
        reference_positions=reference_positions,
    )


def hf_automation_metadata(
    run: HfExportRun,
    export_manifest: Path,
    command_scale: float,
) -> dict[str, Any]:
    is_staircase = str(run.metadata.get("kind", "")).lower() == "staircase"
    measurement_family = str(run.metadata.get("measurement_family", "")).strip()
    is_step_response = is_staircase and measurement_family == "step_response_identification"
    payload = {
        "schema_version": 1,
        "mode": (
            "step_response_identification"
            if is_step_response
            else measurement_family or "high_frequency_identification"
        ),
        "export_manifest": str(export_manifest.resolve()),
        "export_index": run.export_index,
        "label": run.name,
        "repeat_index": 0,
        "repeat_number": 1,
        "repeat_count": 1,
        "command_scale": float(command_scale),
        "drive_amplitude_scale": float(run.metadata.get("drive_amplitude_scale", 1.0)),
        "delay_feedforward_applied": False,
    }
    if is_step_response:
        payload["step_response_tier"] = run.metadata["step_response_tier"]
        payload["intentional_discontinuous_command"] = True
    elif is_staircase:
        payload["intentional_discontinuous_command"] = True
    for field in (
        "campaign",
        "measurement_family",
        "condition",
        "note",
        "escalation_rank",
        "response_gate",
        "particle_id",
        "operator_checkpoint",
        "requires_operator_context",
        "reference",
        "feedforward_design",
    ):
        if field in run.metadata:
            payload[field] = run.metadata[field]
    return payload
