#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Interactive linked WebGL viewer for three-axis stereo stage-accuracy results.

The default local-browser view links:

* transformation-free distance error,
* stage-frame residual components,
* planned logical XYZ capture positions, and
* a scrollable all-capture role/coordinate list.

Its true-scale 3D scene supports unrestricted rotation, zoom and pan while
retaining spatial grids.  The legacy Matplotlib window remains available via
``--native`` and for headless PNG verification.  Both views are read-only and
never open a stage or camera.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import mimetypes
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from typing import Any
from urllib.parse import urlparse
import webbrowser

import numpy as np


SUMMARY_NAME = "stereo_3axis_stage_accuracy_summary.json"
WEB_ASSET_DIR = Path(__file__).resolve().with_name(
    "stereo_3axis_stage_accuracy_viewer"
)
THREE_VENDOR_DIR = Path(__file__).resolve().with_name("stereo_3d_viewer") / "vendor"
CAD_MODEL_ENDPOINT = "/cad/model.obj"
CAD_MATERIAL_ENDPOINT = "/cad/model.mtl"
AXES = ("x", "y", "z")
AXIS_COLORS = {
    "x": "#1f77b4",
    "y": "#ff7f0e",
    "z": "#2ca02c",
    "multi": "#d62728",
    "return": "#7f7f7f",
}
ROLE_STYLES = {
    "single_axis": {
        "short": "Axis",
        "label": "Single-axis",
        "edge": "#111111",
        "marker": "o",
    },
    "depth": {
        "short": "Depth",
        "label": "Depth",
        "edge": "#cc33aa",
        "marker": "s",
    },
    "diagonal": {
        "short": "Diag",
        "label": "Diagonal",
        "edge": "#00a6a6",
        "marker": "D",
    },
    "other": {
        "short": "Valid",
        "label": "Other validation",
        "edge": "#7f7f7f",
        "marker": "^",
    },
}

DATASET_COLORS = (
    "#6ea8fe",
    "#ffb74d",
    "#65d6ad",
    "#d995f2",
    "#ff7b86",
    "#8bd3e6",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Open a linked 2D/list/3D viewer for a completed three-axis "
            "stage-accuracy analysis."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "session_or_analysis_dir",
        type=Path,
        nargs="+",
        help=(
            "One or more session directories (or analysis_3axis directories). "
            "Multiple sessions are placed on one absolute mechanical Y axis."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional PNG snapshot of the initial viewer state.",
    )
    parser.add_argument(
        "--no-show",
        action="store_true",
        help="Render without opening a GUI; useful for automated verification.",
    )
    parser.add_argument(
        "--native",
        action="store_true",
        help="Use the legacy Matplotlib window instead of the WebGL viewer.",
    )
    parser.add_argument(
        "--bind",
        default="127.0.0.1",
        help="Address for the read-only local WebGL server.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Local WebGL server port; 0 selects a free port.",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Start the WebGL server without opening the default browser.",
    )
    parser.add_argument(
        "--d0-mm",
        type=float,
        default=None,
        help=(
            "Independent target-to-stereo-baseline perpendicular D0 label for "
            "this viewer only; it does not modify the analysis record."
        ),
    )
    parser.add_argument(
        "--cad-obj",
        type=Path,
        default=None,
        help="Optional camera-rig OBJ model to overlay in stage coordinates.",
    )
    parser.add_argument(
        "--cad-config",
        type=Path,
        default=None,
        help=(
            "Camera-frame CAD alignment JSON. Its transform is composed with "
            "the fitted camera-to-stage transform."
        ),
    )
    return parser.parse_args()


def resolve_summary(path: Path) -> Path:
    root = path.resolve()
    candidates = (
        root / SUMMARY_NAME,
        root / "analysis_3axis" / SUMMARY_NAME,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SystemExit(
        "Analysis summary not found. Expected one of:\n  "
        + "\n  ".join(str(candidate) for candidate in candidates)
    )


def load_summary(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid analysis summary {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"Analysis summary root must be an object: {path}")
    if not isinstance(value.get("captures"), list):
        raise SystemExit(f"Analysis summary has no captures list: {path}")
    if not isinstance(value.get("validation"), list):
        raise SystemExit(f"Analysis summary has no validation list: {path}")
    return value


def finite_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return result


def vector_from(row: dict[str, Any], prefix: str) -> np.ndarray:
    return np.asarray(
        [finite_float(row.get(f"{prefix}_{axis}_mm")) for axis in AXES],
        dtype=np.float64,
    )


def vector_text(value: np.ndarray, precision: int = 1) -> str:
    if not np.all(np.isfinite(value)):
        return "[n/a]"
    return "[" + ",".join(f"{item:.{precision}f}" for item in value) + "]"


def value_text(value: Any, precision: int = 3) -> str:
    number = finite_float(value)
    return f"{number:.{precision}f}" if math.isfinite(number) else "n/a"


def validation_family(
    capture: dict[str, Any],
    validation: dict[str, Any] | None,
) -> str:
    source = validation or capture
    explicit = str(source.get("validation_family", "")).strip().lower()
    if explicit in ROLE_STYLES:
        return explicit
    data_role = str(source.get("data_role", "")).strip().lower()
    trajectory = str(source.get("trajectory", "")).strip().lower()
    if data_role == "validation_axis" or trajectory == "axis":
        return "single_axis"
    if data_role == "validation_depth" or trajectory == "depth":
        return "depth"
    if data_role == "validation_diagonal" or trajectory == "diagonal":
        return "diagonal"
    return "other"


def purpose_key(
    capture: dict[str, Any],
    validation: dict[str, Any] | None,
    return_row: dict[str, Any] | None,
    orientation_ids: set[str],
) -> tuple[str, str]:
    capture_id = str(capture.get("capture_id", ""))
    data_role = str(capture.get("data_role", "")).strip().lower()
    if capture_id in orientation_ids:
        return "orientation_fit", "OriFit"
    if data_role == "registration_center_pre":
        return "orientation_reference_pre", "OriPre"
    if data_role == "registration_center_post":
        return "orientation_reference_post", "OriPost"
    if data_role == "validation_center_pre":
        return "pass_reference", "RefPre"
    if return_row is not None or data_role == "validation_center_post":
        return "return_drift", "Return"
    if validation is not None:
        family = validation_family(capture, validation)
        return family, ROLE_STYLES[family]["short"]
    return "other", "Other"


def display_group(
    purpose: str,
    validation: dict[str, Any] | None,
) -> str:
    """Return the 3D visibility group without changing analysis semantics."""
    if purpose == "single_axis":
        dominant_axis = str((validation or {}).get("dominant_axis", "")).lower()
        if dominant_axis in AXES:
            return f"axis_{dominant_axis}"
        return "axis_other"
    if purpose in {"depth", "diagonal"}:
        return purpose
    if purpose == "orientation_fit" or purpose.startswith("orientation_reference_"):
        return "orientation"
    if purpose in {"pass_reference", "return_drift"}:
        return "reference_return"
    return "other"


def _json_vector(row: dict[str, Any], prefix: str) -> list[float | None]:
    values: list[float | None] = []
    for axis in AXES:
        value = finite_float(row.get(f"{prefix}_{axis}_mm"))
        values.append(value if math.isfinite(value) else None)
    return values


def camera_geometry_in_stage(
    summary: dict[str, Any],
    independent_d0_mm: float | None,
) -> dict[str, Any] | None:
    """Express the stereo optical centres and baseline foot in stage XYZ."""
    calibration_text = str(summary.get("stereo_calibration", "")).strip()
    if not calibration_text:
        return None
    calibration_path = Path(calibration_text)
    if not calibration_path.is_file():
        return None
    from stereo_3d_viewer import load_camera_geometry

    camera_geometry = load_camera_geometry(calibration_path)
    orientation = summary.get("orientation", {})
    if camera_geometry is None or not isinstance(orientation, dict):
        return None
    try:
        rotation = np.asarray(
            orientation["rotation_camera_to_stage"],
            dtype=np.float64,
        ).reshape(3, 3)
        translation = np.asarray(
            orientation["translation_stage_mm"],
            dtype=np.float64,
        ).reshape(3)
        right_camera = np.asarray(
            camera_geometry["right_centre_left_cam_mm"],
            dtype=np.float64,
        ).reshape(3)
    except (KeyError, TypeError, ValueError):
        return None
    if not np.all(np.isfinite(rotation)) or not np.all(np.isfinite(translation)):
        return None
    left_stage = translation
    right_stage = rotation @ right_camera + translation
    baseline = right_stage - left_stage
    baseline_norm = float(np.linalg.norm(baseline))
    if not math.isfinite(baseline_norm) or baseline_norm <= 1e-9:
        return None
    baseline_unit = baseline / baseline_norm
    reference_target = np.zeros(3, dtype=np.float64)
    foot = left_stage + baseline_unit * float(
        np.dot(reference_target - left_stage, baseline_unit)
    )
    fitted_d0 = float(np.linalg.norm(reference_target - foot))

    origin_estimates = []
    for capture in summary.get("captures", []):
        target = _json_vector(capture, "plan_target")
        estimate = finite_float(
            capture.get("stereo_baseline_distance_estimate_mm")
        )
        if (
            target == [0.0, 0.0, 0.0]
            and math.isfinite(estimate)
            and str(capture.get("capture_id", "")) != "q0041"
        ):
            origin_estimates.append(estimate)
    capture_median = (
        float(np.median(np.asarray(origin_estimates, dtype=np.float64)))
        if origin_estimates
        else None
    )
    independent = (
        float(independent_d0_mm)
        if independent_d0_mm is not None
        and math.isfinite(float(independent_d0_mm))
        else None
    )
    return {
        "left_optical_centre_stage_mm": left_stage.tolist(),
        "right_optical_centre_stage_mm": right_stage.tolist(),
        "baseline_foot_from_stage_origin_mm": foot.tolist(),
        "baseline_mm": baseline_norm,
        "fitted_transform_d0_mm": fitted_d0,
        "capture_median_d0_mm": capture_median,
        "capture_count_for_median": len(origin_estimates),
        "independent_d0_mm": independent,
        "stereo_minus_independent_mm": (
            capture_median - independent
            if capture_median is not None and independent is not None
            else None
        ),
        "reference_target_stage_mm": reference_target.tolist(),
        "note": (
            "Stereo values use the fitted camera-to-stage transform and are "
            "diagnostic, not independent ground truth."
        ),
    }


def load_stage_cad_model(
    summary: dict[str, Any],
    obj_path: Path | None,
    config_path: Path | None,
) -> tuple[dict[str, Any] | None, Path | None, Path | None]:
    """Load a camera-frame CAD model and compose it into stage coordinates."""
    if obj_path is None:
        if config_path is not None:
            raise SystemExit("--cad-config requires --cad-obj.")
        return None, None, None
    from stereo_3d_viewer import load_cad_model, load_camera_geometry

    calibration_text = str(summary.get("stereo_calibration", "")).strip()
    calibration_path = Path(calibration_text) if calibration_text else None
    camera_geometry = load_camera_geometry(calibration_path)
    spec, resolved_obj, material_path = load_cad_model(
        obj_path,
        config_path,
        camera_geometry,
    )
    if spec is None:
        return None, resolved_obj, material_path
    if spec.get("frame") != "camera":
        raise SystemExit(
            "The three-axis accuracy viewer currently requires a CAD config "
            "with frame='camera'. A PAT-frame placement is not valid for the "
            "new stage-only experiment."
        )
    orientation = summary.get("orientation", {})
    try:
        camera_to_stage_rotation = np.asarray(
            orientation["rotation_camera_to_stage"],
            dtype=np.float64,
        ).reshape(3, 3)
        camera_to_stage_translation = np.asarray(
            orientation["translation_stage_mm"],
            dtype=np.float64,
        ).reshape(3)
        obj_to_camera_rotation = np.asarray(
            spec["rotation_matrix_row_major"],
            dtype=np.float64,
        ).reshape(3, 3)
        obj_to_camera_translation = np.asarray(
            spec["translation_mm"],
            dtype=np.float64,
        ).reshape(3)
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(
            "CAD overlay requires the fitted camera-to-stage rigid transform."
        ) from exc
    obj_to_stage_rotation = camera_to_stage_rotation @ obj_to_camera_rotation
    obj_to_stage_translation = (
        camera_to_stage_rotation @ obj_to_camera_translation
        + camera_to_stage_translation
    )
    mechanical_alignment: dict[str, Any] | None = None
    raw_cad_config: dict[str, Any] = {}
    if config_path is not None:
        try:
            parsed_config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Unable to re-read CAD config: {exc}") from exc
        if isinstance(parsed_config, dict):
            raw_cad_config = parsed_config
    raw_alignment = raw_cad_config.get("stage_front_plane_alignment")
    if raw_alignment is not None:
        if not isinstance(raw_alignment, dict):
            raise SystemExit(
                "CAD stage_front_plane_alignment must be a JSON object."
            )

        def alignment_vector(field: str) -> np.ndarray:
            raw_value = raw_alignment.get(field)
            if not isinstance(raw_value, list) or len(raw_value) != 3:
                raise SystemExit(
                    f"CAD stage_front_plane_alignment.{field} must contain "
                    "three numeric values."
                )
            try:
                value = np.asarray(raw_value, dtype=np.float64).reshape(3)
            except (TypeError, ValueError) as exc:
                raise SystemExit(
                    f"CAD stage_front_plane_alignment.{field} must be numeric."
                ) from exc
            if not np.all(np.isfinite(value)):
                raise SystemExit(
                    f"CAD stage_front_plane_alignment.{field} must be finite."
                )
            return value

        plane_point_obj = alignment_vector("source_plane_point_obj")
        plane_normal_obj = alignment_vector("source_plane_normal_obj")
        target_stage = alignment_vector("target_stage_point_mm")
        normal_norm = float(np.linalg.norm(plane_normal_obj))
        if normal_norm <= 1e-9:
            raise SystemExit(
                "CAD stage_front_plane_alignment source plane normal is zero."
            )
        plane_normal_obj /= normal_norm
        axis_name = str(raw_alignment.get("stage_axis", "y")).strip().lower()
        if axis_name not in AXES:
            raise SystemExit(
                "CAD stage_front_plane_alignment.stage_axis must be x, y, or z."
            )
        axis_index = AXES.index(axis_name)
        try:
            distance_mm = float(
                raw_alignment["front_to_target_distance_mm"]
            )
            front_side_sign = int(raw_alignment.get("front_side_sign", -1))
            maximum_angle_deg = float(
                raw_alignment.get("maximum_axis_misalignment_deg", 2.0)
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SystemExit(
                "CAD front-plane distance, sign, and angle limit must be numeric."
            ) from exc
        if (
            not math.isfinite(distance_mm)
            or distance_mm <= 0.0
            or front_side_sign not in {-1, 1}
            or not math.isfinite(maximum_angle_deg)
            or not 0.0 <= maximum_angle_deg < 90.0
        ):
            raise SystemExit(
                "Invalid CAD stage front-plane alignment distance/sign/angle."
            )
        scale = float(spec["mm_per_unit"])
        plane_point_before = (
            scale * (obj_to_stage_rotation @ plane_point_obj)
            + obj_to_stage_translation
        )
        plane_normal_stage = obj_to_stage_rotation @ plane_normal_obj
        plane_normal_stage /= np.linalg.norm(plane_normal_stage)
        target_axis_unit = np.zeros(3, dtype=np.float64)
        target_axis_unit[axis_index] = -float(front_side_sign)
        normal_dot = float(np.dot(plane_normal_stage, target_axis_unit))
        misalignment_deg = math.degrees(
            math.acos(float(np.clip(normal_dot, -1.0, 1.0)))
        )
        if misalignment_deg > maximum_angle_deg:
            raise SystemExit(
                "CAD front-plane normal is not aligned with the requested "
                f"stage {axis_name.upper()} direction: {misalignment_deg:.3f} "
                f"deg > {maximum_angle_deg:.3f} deg."
            )
        desired_axis_coordinate = (
            target_stage[axis_index] + front_side_sign * distance_mm
        )
        translation_adjustment = (
            desired_axis_coordinate - plane_point_before[axis_index]
        )
        obj_to_stage_translation[axis_index] += translation_adjustment
        plane_point_after = (
            scale * (obj_to_stage_rotation @ plane_point_obj)
            + obj_to_stage_translation
        )
        adjustment_vector = np.zeros(3, dtype=np.float64)
        adjustment_vector[axis_index] = translation_adjustment
        perpendicular_distance = abs(
            float(np.dot(target_stage - plane_point_after, plane_normal_stage))
        )
        mechanical_alignment = {
            "method": (
                "camera-derived rotation with stage-axis translation constrained "
                "by an independent case-front measurement"
            ),
            "source_plane_point_obj": plane_point_obj.tolist(),
            "source_plane_normal_obj": plane_normal_obj.tolist(),
            "target_stage_point_mm": target_stage.tolist(),
            "stage_axis": axis_name,
            "front_side_sign": front_side_sign,
            "front_to_target_distance_mm": distance_mm,
            "plane_point_stage_before_constraint_mm": (
                plane_point_before.tolist()
            ),
            "stage_translation_adjustment_mm": adjustment_vector.tolist(),
            "stage_axis_translation_adjustment_mm": translation_adjustment,
            "plane_point_stage_after_constraint_mm": plane_point_after.tolist(),
            "plane_normal_stage": plane_normal_stage.tolist(),
            "axis_misalignment_deg": misalignment_deg,
            "perpendicular_distance_after_constraint_mm": (
                perpendicular_distance
            ),
            "measurement_method": str(
                raw_alignment.get("measurement_method", "")
            ),
            "measurement_note": str(
                raw_alignment.get("measurement_note", "")
            ),
            "standard_uncertainty_mm": raw_alignment.get(
                "standard_uncertainty_mm"
            ),
            "instrument": str(raw_alignment.get("instrument", "")),
            "measured_at": str(raw_alignment.get("measured_at", "")),
        }
    result = dict(spec)
    result.update(
        {
            "source_frame": "camera",
            "frame": "stage",
            "alignment_status": (
                "Camera CAD alignment composed with fitted camera-to-stage R,t"
            ),
            "rotation_matrix_row_major": (
                obj_to_stage_rotation.reshape(-1).tolist()
            ),
            "translation_mm": obj_to_stage_translation.tolist(),
            "model_url": CAD_MODEL_ENDPOINT,
            "material_url": (
                CAD_MATERIAL_ENDPOINT if material_path is not None else ""
            ),
            "visible_by_default": True,
            "placement_warning": (
                (
                    "Rotation uses the stereo-derived camera-to-stage fit; "
                    "stage-axis translation is constrained by the configured "
                    "case-front mechanical measurement."
                    if mechanical_alignment is not None
                    else
                    "Placement uses the stereo-derived camera-to-stage fit. "
                    "D0 is shown as an independent diagnostic and is not "
                    "forced into the CAD transform."
                )
            ),
        }
    )
    if mechanical_alignment is not None:
        result["mechanical_front_alignment"] = mechanical_alignment
        result["alignment_status"] = (
            f"Camera/CAD orientation + case front at stage "
            f"{mechanical_alignment['stage_axis'].upper()}="
            f"{mechanical_alignment['plane_point_stage_after_constraint_mm'][axis_index]:.3f} "
            "mm"
        )
    return result, resolved_obj, material_path


def build_web_payload(
    summary: dict[str, Any],
    summary_path: Path,
    *,
    independent_d0_mm: float | None = None,
    cad_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a finite, browser-oriented payload without changing analysis data."""
    captures = sorted(
        [dict(row) for row in summary["captures"]],
        key=lambda row: int(row.get("sequence_index", 0)),
    )
    validation_by_id = {
        str(row["capture_id"]): dict(row)
        for row in summary["validation"]
    }
    return_by_id = {
        str(row["capture_id"]): dict(row)
        for row in summary.get("return_drift_captures", [])
    }
    orientation_ids = {
        str(value)
        for value in summary.get("orientation", {}).get(
            "orientation_capture_ids",
            [],
        )
    }

    grouped: dict[tuple[float, float, float], list[str]] = {}
    true_positions: dict[str, list[float | None]] = {}
    for capture in captures:
        capture_id = str(capture["capture_id"])
        target = _json_vector(capture, "plan_target")
        true_positions[capture_id] = target
        if all(value is not None for value in target):
            key = tuple(round(float(value), 6) for value in target)
            grouped.setdefault(key, []).append(capture_id)

    # The fan-out is display-only. It is deliberately large enough for reliable
    # WebGL hit testing; every detail readout continues to show the true target.
    display_positions = {
        capture_id: list(target)
        for capture_id, target in true_positions.items()
    }
    duplicate_sizes = {capture_id: 1 for capture_id in true_positions}
    for key, capture_ids in grouped.items():
        count = len(capture_ids)
        for capture_id in capture_ids:
            duplicate_sizes[capture_id] = count
        if count <= 1:
            continue
        for index, capture_id in enumerate(capture_ids):
            capacity = 10
            ring = index // capacity
            ring_start = ring * capacity
            ring_count = min(capacity, count - ring_start)
            position_in_ring = index - ring_start
            radius = 1.8 * (ring + 1)
            angle = 2.0 * math.pi * position_in_ring / ring_count + 0.35 * (
                ring % 2
            )
            display_positions[capture_id] = [
                key[0] + radius * math.cos(angle),
                key[1],
                key[2] + radius * math.sin(angle),
            ]

    web_captures: list[dict[str, Any]] = []
    for capture in captures:
        capture_id = str(capture["capture_id"])
        validation = validation_by_id.get(capture_id)
        return_row = return_by_id.get(capture_id)
        purpose, purpose_short = purpose_key(
            capture,
            validation,
            return_row,
            orientation_ids,
        )
        web_captures.append(
            {
                "capture_id": capture_id,
                "sequence_index": int(capture.get("sequence_index", 0)),
                "role": str(capture.get("role", "")),
                "data_role": str(capture.get("data_role", "")),
                "trajectory": str(capture.get("trajectory", "")),
                "reference_id": str(capture.get("reference_id", "")),
                "direction": str(capture.get("direction", "")),
                "cycle": capture.get("cycle"),
                "usable": bool(capture.get("usable", False)),
                "reason": str(capture.get("reason", "")),
                "purpose": purpose,
                "purpose_short": purpose_short,
                "display_group": display_group(purpose, validation),
                "target": true_positions[capture_id],
                "display_target": display_positions[capture_id],
                "duplicate_count": duplicate_sizes[capture_id],
                "stage_readback": _json_vector(capture, "stage_readback"),
                "absolute_distance_label_mm": capture.get(
                    "absolute_distance_label_mm"
                ),
                "hardware": _json_vector(capture, "stage_hardware"),
                "camera": _json_vector(capture, "camera"),
                "measured_stage": _json_vector(capture, "measured_stage"),
                "validation": validation,
                "return_drift": return_row,
                "orientation_fit": capture_id in orientation_ids,
            }
        )

    payload = {
        "schema_version": 1,
        "input": str(summary_path),
        "session_dir": summary.get("session_dir"),
        "captures": web_captures,
        "orientation": summary.get("orientation", {}),
        "axis_colors": AXIS_COLORS,
        "role_styles": ROLE_STYLES,
        "coordinate_mapping": {
            "logical": ["X", "Y", "Z"],
            "three_world": ["X", "Z", "Y"],
            "note": (
                "Logical Z is vertical in the WebGL world. Display fan-out "
                "never changes true coordinates or analysis values."
            ),
        },
    }
    camera_geometry = camera_geometry_in_stage(summary, independent_d0_mm)
    if camera_geometry is not None:
        payload["camera_geometry_stage"] = camera_geometry
    if cad_model is not None:
        payload["cad_model"] = cad_model
    return payload


def _dataset_label(
    summary: dict[str, Any],
    summary_path: Path,
) -> str:
    session_text = str(summary.get("session_dir", "")).strip()
    if session_text:
        label = Path(session_text).name
        if label:
            return label
    if summary_path.parent.name == "analysis_3axis":
        return summary_path.parent.parent.name
    return summary_path.parent.name or summary_path.stem


def _unique_dataset_id(label: str, used: set[str]) -> str:
    base = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in label
    ).strip("_") or "dataset"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _absolute_y_placement(
    summary: dict[str, Any],
    summary_path: Path,
) -> dict[str, Any]:
    labels = summary.get("absolute_distance_labels")
    if not isinstance(labels, dict) or not bool(labels.get("available")):
        raise SystemExit(
            "Combined 3D placement requires an available "
            f"absolute_distance_reference: {summary_path}"
        )
    if str(labels.get("stage_axis", "")).strip().lower() != "y":
        raise SystemExit(
            "Combined 3D placement currently requires stage_axis='y': "
            f"{summary_path}"
        )
    try:
        distance_mm = float(labels["reference_distance_mm"])
        sign = float(labels.get("sign", 1.0))
        reference_command = [
            float(value)
            for value in labels["reference_command_global_mm"]
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(
            "Invalid absolute-distance placement metadata in "
            f"{summary_path}"
        ) from exc
    if (
        not math.isfinite(distance_mm)
        or sign != 1.0
        or len(reference_command) != 3
        or not all(math.isfinite(value) for value in reference_command)
    ):
        raise SystemExit(
            "Combined 3D placement requires finite metadata and sign=+1 in "
            f"{summary_path}"
        )
    return {
        "reference_distance_mm": distance_mm,
        "reference_command_global_mm": reference_command,
        "stage_axis": "y",
        "sign": sign,
        "definition": str(labels.get("definition", "")),
        "moving_body": str(labels.get("moving_body", "")),
        "standard_uncertainty_mm": labels.get(
            "combined_standard_uncertainty_mm",
            labels.get("standard_uncertainty_mm"),
        ),
    }


def _absolute_y_vector(
    vector: list[float | None],
    placement: dict[str, Any],
) -> list[float | None]:
    if len(vector) != 3 or vector[1] is None:
        return list(vector)
    reference_y = float(placement["reference_command_global_mm"][1])
    return [
        vector[0],
        float(placement["reference_distance_mm"])
        + float(placement["sign"]) * (float(vector[1]) - reference_y),
        vector[2],
    ]


def shift_stage_cad_to_absolute_y(
    cad_model: dict[str, Any],
    placement: dict[str, Any],
) -> dict[str, Any]:
    """Move one local-stage CAD placement into the shared absolute-Y frame."""
    shifted = copy.deepcopy(cad_model)
    y_offset = (
        float(placement["reference_distance_mm"])
        - float(placement["reference_command_global_mm"][1])
    )
    translation = list(shifted.get("translation_mm", []))
    if len(translation) == 3:
        translation[1] = float(translation[1]) + y_offset
        shifted["translation_mm"] = translation
    alignment = shifted.get("mechanical_front_alignment")
    if isinstance(alignment, dict):
        for field in (
            "target_stage_point_mm",
            "plane_point_stage_before_constraint_mm",
            "plane_point_stage_after_constraint_mm",
        ):
            vector = alignment.get(field)
            if isinstance(vector, list) and len(vector) == 3:
                vector[1] = float(vector[1]) + y_offset
        after = alignment.get("plane_point_stage_after_constraint_mm")
        if isinstance(after, list) and len(after) == 3:
            shifted["alignment_status"] = (
                "Camera/CAD at common mechanical origin; case front at "
                f"absolute Y={float(after[1]):.3f} mm"
            )
    shifted["absolute_y_translation_mm"] = y_offset
    shifted["placement_warning"] = (
        str(shifted.get("placement_warning", "")).rstrip()
        + " The model is translated into the combined absolute mechanical-Y "
        "frame."
    ).strip()
    return shifted


def build_combined_web_payload(
    summaries: list[tuple[dict[str, Any], Path]],
    *,
    cad_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Combine independently analysed sessions on one mechanical Y axis."""
    if len(summaries) < 2:
        raise ValueError("Combined payload requires at least two summaries.")

    used_ids: set[str] = set()
    datasets: list[dict[str, Any]] = []
    combined_captures: list[dict[str, Any]] = []
    placements: list[dict[str, Any]] = []
    definitions: set[tuple[str, str]] = set()

    for index, (summary, summary_path) in enumerate(summaries):
        placement = _absolute_y_placement(summary, summary_path)
        placements.append(placement)
        definitions.add(
            (placement["definition"], placement["moving_body"])
        )
        label = _dataset_label(summary, summary_path)
        dataset_id = _unique_dataset_id(label, used_ids)
        color = DATASET_COLORS[index % len(DATASET_COLORS)]
        child = build_web_payload(summary, summary_path)
        capture_ids = {
            str(capture["capture_id"]) for capture in child["captures"]
        }
        transformed: list[dict[str, Any]] = []
        for original in child["captures"]:
            capture = copy.deepcopy(original)
            original_id = str(capture["capture_id"])
            local_reference_id = str(capture.get("reference_id", ""))
            capture["original_capture_id"] = original_id
            capture["capture_id"] = f"{dataset_id}:{original_id}"
            capture["local_reference_id"] = local_reference_id
            capture["reference_id"] = (
                f"{dataset_id}:{local_reference_id}"
                if local_reference_id in capture_ids
                else ""
            )
            capture["dataset_id"] = dataset_id
            capture["dataset_label"] = label
            capture["dataset_color"] = color
            capture["reference_distance_mm"] = placement[
                "reference_distance_mm"
            ]
            capture["local_target"] = list(capture["target"])
            capture["local_display_target"] = list(
                capture["display_target"]
            )
            capture["target"] = _absolute_y_vector(
                capture["local_target"], placement
            )
            capture["display_target"] = _absolute_y_vector(
                capture["local_display_target"], placement
            )
            capture["local_stage_readback"] = list(
                capture["stage_readback"]
            )
            capture["absolute_stage_readback"] = _absolute_y_vector(
                capture["local_stage_readback"], placement
            )
            transformed.append(capture)
        combined_captures.extend(transformed)
        absolute_y = [
            float(capture["target"][1])
            for capture in transformed
            if capture["target"][1] is not None
        ]
        datasets.append(
            {
                "id": dataset_id,
                "label": label,
                "color": color,
                "input": str(summary_path),
                "session_dir": summary.get("session_dir"),
                "reference_distance_mm": placement[
                    "reference_distance_mm"
                ],
                "standard_uncertainty_mm": placement[
                    "standard_uncertainty_mm"
                ],
                "capture_count": len(transformed),
                "absolute_y_range_mm": (
                    [min(absolute_y), max(absolute_y)]
                    if absolute_y
                    else [None, None]
                ),
            }
        )

    if len(definitions) != 1:
        raise SystemExit(
            "The selected summaries use incompatible absolute-distance "
            "definitions or moving bodies."
        )

    primary_summary, _ = summaries[0]
    payload: dict[str, Any] = {
        "schema_version": 2,
        "input": " + ".join(dataset["label"] for dataset in datasets),
        "session_dir": None,
        "datasets": datasets,
        "captures": combined_captures,
        "orientation": primary_summary.get("orientation", {}),
        "axis_colors": AXIS_COLORS,
        "role_styles": ROLE_STYLES,
        "coordinate_mapping": {
            "logical": ["X", "absolute mechanical Y", "Z"],
            "three_world": ["X", "Z", "absolute mechanical Y"],
            "mode": "absolute_mechanical_distance_y",
            "note": (
                "Each local target is placed at reference_distance_mm + "
                "(local Y - reference-command Y). X and Z retain their "
                "stage coordinates; display fan-out does not change analysis "
                "values."
            ),
        },
    }
    camera_geometry = camera_geometry_in_stage(primary_summary, None)
    if camera_geometry is not None:
        payload["camera_geometry_stage"] = camera_geometry
    if cad_model is not None:
        payload["cad_model"] = shift_stage_cad_to_absolute_y(
            cad_model,
            placements[0],
        )
    return payload


def make_web_handler(
    payload: dict[str, Any],
    cad_obj_path: Path | None = None,
    cad_material_path: Path | None = None,
) -> type[BaseHTTPRequestHandler]:
    payload_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")

    class ViewerHandler(BaseHTTPRequestHandler):
        def log_message(self, format_string: str, *args: Any) -> None:
            print(f"[WEB] {self.address_string()} {format_string % args}")

        def _send(
            self,
            status: int,
            content_type: str,
            body: bytes,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            path = urlparse(self.path).path
            if path == "/api/data":
                self._send(
                    200,
                    "application/json; charset=utf-8",
                    payload_bytes,
                )
                return
            if path in {"/", "/index.html"}:
                file_path = WEB_ASSET_DIR / "index.html"
            elif path in {"/app.js", "/styles.css"}:
                file_path = WEB_ASSET_DIR / path.lstrip("/")
            elif path == "/vendor/three.module.js":
                file_path = THREE_VENDOR_DIR / "three.module.js"
            elif path == "/vendor/OrbitControls.js":
                file_path = THREE_VENDOR_DIR / "OrbitControls.js"
            elif path == "/vendor/OBJLoader.js":
                file_path = THREE_VENDOR_DIR / "OBJLoader.js"
            elif path == "/vendor/MTLLoader.js":
                file_path = THREE_VENDOR_DIR / "MTLLoader.js"
            elif path == CAD_MODEL_ENDPOINT and cad_obj_path is not None:
                file_path = cad_obj_path
            elif (
                path == CAD_MATERIAL_ENDPOINT
                and cad_material_path is not None
            ):
                file_path = cad_material_path
            else:
                self._send(404, "text/plain; charset=utf-8", b"Not found")
                return
            if not file_path.is_file():
                self._send(
                    500,
                    "text/plain; charset=utf-8",
                    f"Missing viewer asset: {file_path}".encode("utf-8"),
                )
                return
            mime, _ = mimetypes.guess_type(file_path.name)
            content_type = (
                "text/plain"
                if file_path.suffix.lower() in {".obj", ".mtl"}
                else mime or "application/octet-stream"
            )
            if content_type.startswith(("text/", "application/javascript")):
                content_type += "; charset=utf-8"
            self._send(200, content_type, file_path.read_bytes())

    return ViewerHandler


def serve_web_viewer(
    payload: dict[str, Any],
    *,
    bind: str,
    port: int,
    open_browser: bool,
    cad_obj_path: Path | None = None,
    cad_material_path: Path | None = None,
) -> None:
    missing = [
        path
        for path in (
            WEB_ASSET_DIR / "index.html",
            WEB_ASSET_DIR / "app.js",
            WEB_ASSET_DIR / "styles.css",
            THREE_VENDOR_DIR / "three.module.js",
            THREE_VENDOR_DIR / "OrbitControls.js",
            THREE_VENDOR_DIR / "OBJLoader.js",
            THREE_VENDOR_DIR / "MTLLoader.js",
        )
        if not path.is_file()
    ]
    if missing:
        raise SystemExit(
            "WebGL viewer assets are missing:\n  "
            + "\n  ".join(str(path) for path in missing)
        )
    server = ThreadingHTTPServer(
        (bind, port),
        make_web_handler(payload, cad_obj_path, cad_material_path),
    )
    host_for_url = "127.0.0.1" if bind in {"0.0.0.0", "::"} else bind
    url = f"http://{host_for_url}:{server.server_address[1]}/"
    print(f"WebGL viewer: {url}")
    print(
        "Controls: left-drag=free rotate, wheel=zoom, right-drag=pan, "
        "point/list click=lock selection, Esc=unlock"
    )
    print("Press Ctrl+C in this console to close the viewer.")
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nViewer stopped.")
    finally:
        server.server_close()


class AccuracyViewer:
    def __init__(self, summary: dict[str, Any], summary_path: Path, plt: Any) -> None:
        self.summary = summary
        self.summary_path = summary_path
        self.plt = plt
        self.captures = sorted(
            [dict(row) for row in summary["captures"]],
            key=lambda row: int(row.get("sequence_index", 0)),
        )
        self.capture_by_id = {
            str(row["capture_id"]): row for row in self.captures
        }
        self.validation = [dict(row) for row in summary["validation"]]
        self.validation_by_id = {
            str(row["capture_id"]): row for row in self.validation
        }
        self.return_by_id = {
            str(row["capture_id"]): row
            for row in summary.get("return_drift_captures", [])
        }
        self.orientation_ids = {
            str(value)
            for value in summary.get("orientation", {}).get(
                "orientation_capture_ids",
                [],
            )
        }
        self.capture_ids = [str(row["capture_id"]) for row in self.captures]
        self.selected_id = (
            str(self.validation[0]["capture_id"])
            if self.validation
            else self.capture_ids[0]
        )
        self.selection_locked = False
        self.table_offset = 0
        self.table_page_rows = 18
        self.table_hit_rows: list[tuple[float, str]] = []
        self.artist_capture: dict[Any, str] = {}
        self.axis_artists: dict[Any, list[Any]] = {}
        (
            self.stage_true_positions,
            self.stage_display_positions,
            self.stage_duplicate_group_sizes,
        ) = self._build_stage_display_positions()

        self.figure = plt.figure(figsize=(16.5, 9.2), constrained_layout=True)
        grid = self.figure.add_gridspec(
            2,
            3,
            width_ratios=(1.25, 1.15, 1.05),
            height_ratios=(1.35, 1.0),
        )
        self.stage_axis = self.figure.add_subplot(grid[0, 0:2], projection="3d")
        self.distance_axis = self.figure.add_subplot(grid[1, 0])
        self.residual_axis = self.figure.add_subplot(grid[1, 1])
        self.table_axis = self.figure.add_subplot(grid[:, 2])
        self.figure.suptitle(
            "Stereo three-axis stage accuracy — linked capture inspector",
            fontsize=14,
        )

        self._draw_stage_positions()
        self._draw_distance()
        self._draw_residual()
        self._create_selection_artists()
        self._draw_table()
        self._connect_events()
        self.select(self.selected_id, locked=False)
        self.figure.text(
            0.5,
            0.004,
            "Hover: temporary selection   Click: lock   Esc/right-click: unlock   "
            "↑/↓: previous/next capture   Mouse wheel over list: scroll",
            ha="center",
            va="bottom",
            fontsize=8,
            color="#444444",
        )

    def _build_stage_display_positions(
        self,
    ) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], dict[str, int]]:
        """Fan out exact duplicate targets in X/Z for display and hit testing only."""
        true_positions: dict[str, np.ndarray] = {}
        grouped: dict[tuple[float, float, float], list[str]] = {}
        for capture in self.captures:
            capture_id = str(capture["capture_id"])
            target = vector_from(capture, "plan_target")
            true_positions[capture_id] = target
            if not np.all(np.isfinite(target)):
                continue
            key = tuple(float(value) for value in np.round(target, 6))
            grouped.setdefault(key, []).append(capture_id)

        display_positions = {
            capture_id: value.copy()
            for capture_id, value in true_positions.items()
        }
        group_sizes: dict[str, int] = {
            capture_id: 1 for capture_id in true_positions
        }
        for key, capture_ids in grouped.items():
            count = len(capture_ids)
            for capture_id in capture_ids:
                group_sizes[capture_id] = count
            if count <= 1:
                continue
            true = np.asarray(key, dtype=np.float64)
            capacity = 10
            for index, capture_id in enumerate(capture_ids):
                ring = index // capacity
                ring_start = ring * capacity
                ring_count = min(capacity, count - ring_start)
                position_in_ring = index - ring_start
                radius = 0.60 * (ring + 1)
                phase = 0.35 * (ring % 2)
                angle = (
                    2.0 * math.pi * position_in_ring / ring_count
                    + phase
                )
                display = true.copy()
                display[0] += radius * math.cos(angle)
                display[2] += radius * math.sin(angle)
                display_positions[capture_id] = display
        return true_positions, display_positions, group_sizes

    def _register_artist(self, artist: Any, capture_id: str) -> None:
        self.artist_capture[artist] = capture_id
        self.axis_artists.setdefault(artist.axes, []).append(artist)

    def _style_for_validation(
        self,
        capture_id: str,
    ) -> tuple[dict[str, str], str]:
        validation = self.validation_by_id[capture_id]
        family = validation_family(self.capture_by_id[capture_id], validation)
        axis = str(validation.get("dominant_axis", "multi")).lower()
        return ROLE_STYLES[family], axis if axis in AXIS_COLORS else "multi"

    def _dual_legend(self, axis: Any, first_title: str) -> None:
        from matplotlib.lines import Line2D

        observed_axes = set()
        used_roles = []
        for row in self.validation:
            name = str(row.get("dominant_axis", "multi")).lower()
            observed_axes.add(name)
            role = validation_family(
                self.capture_by_id[str(row["capture_id"])],
                row,
            )
            if role not in used_roles:
                used_roles.append(role)
        used_axes = [
            name
            for name in ("x", "y", "z", "multi", "return")
            if name in observed_axes
        ]
        axis_handles = [
            Line2D(
                [],
                [],
                linestyle="",
                marker="o",
                markersize=7,
                markerfacecolor=AXIS_COLORS.get(name, AXIS_COLORS["multi"]),
                markeredgecolor="white",
                label=name.upper(),
            )
            for name in used_axes
        ]
        first = axis.legend(
            handles=axis_handles,
            title=first_title,
            loc="upper left",
            fontsize=8,
            title_fontsize=8,
        )
        axis.add_artist(first)
        role_handles = [
            Line2D(
                [],
                [],
                linestyle="",
                marker=ROLE_STYLES[name]["marker"],
                markersize=7,
                markerfacecolor="white",
                markeredgecolor=ROLE_STYLES[name]["edge"],
                markeredgewidth=1.7,
                label=ROLE_STYLES[name]["label"],
            )
            for name in used_roles
        ]
        axis.legend(
            handles=role_handles,
            title="Family (edge/shape)",
            loc="upper right",
            fontsize=8,
            title_fontsize=8,
        )

    def _draw_distance(self) -> None:
        axis = self.distance_axis
        for row in self.validation:
            capture_id = str(row["capture_id"])
            style, dominant = self._style_for_validation(capture_id)
            artist = axis.scatter(
                [finite_float(row.get("stage_displacement_norm_mm"))],
                [finite_float(row.get("distance_norm_error_mm"))],
                s=58,
                marker=style["marker"],
                facecolor=AXIS_COLORS[dominant],
                edgecolor=style["edge"],
                linewidth=1.5,
                alpha=0.9,
                picker=6,
            )
            self._register_artist(artist, capture_id)
        axis.axhline(0.0, color="black", linewidth=1)
        axis.set_xlabel("Stage displacement norm from each pass reference [mm]")
        axis.set_ylabel("||Δcamera|| − ||Δstage|| [mm]")
        axis.set_title(
            "Transformation-free distance error (all validation captures)",
            fontsize=11,
        )
        axis.grid(True, alpha=0.3)
        self._dual_legend(axis, "Dominant stage axis (fill)")
        axis.text(
            0.01,
            0.01,
            "x is a reference-relative displacement norm, not global Y.",
            transform=axis.transAxes,
            fontsize=7.5,
            color="#444444",
        )

    def _draw_residual(self) -> None:
        axis = self.residual_axis
        for row in self.validation:
            capture_id = str(row["capture_id"])
            style, _ = self._style_for_validation(capture_id)
            x = finite_float(row.get("stage_displacement_norm_mm"))
            for component in AXES:
                artist = axis.scatter(
                    [x],
                    [finite_float(row.get(f"residual_stage_{component}_mm"))],
                    s=48,
                    marker=style["marker"],
                    facecolor=AXIS_COLORS[component],
                    edgecolor=style["edge"],
                    linewidth=1.4,
                    alpha=0.9,
                    picker=6,
                )
                self._register_artist(artist, capture_id)
        axis.axhline(0.0, color="black", linewidth=1)
        axis.set_xlabel("Stage displacement norm [mm]")
        axis.set_ylabel("Stage-frame residual [mm]")
        axis.set_title("Residual XYZ components", fontsize=11)
        axis.grid(True, alpha=0.3)
        self._dual_legend(axis, "Residual component (fill)")

    def _capture_3d_style(
        self,
        capture: dict[str, Any],
    ) -> tuple[str, str, str, float]:
        capture_id = str(capture["capture_id"])
        validation = self.validation_by_id.get(capture_id)
        return_row = self.return_by_id.get(capture_id)
        key, _ = purpose_key(
            capture,
            validation,
            return_row,
            self.orientation_ids,
        )
        if validation is not None:
            style, dominant = self._style_for_validation(capture_id)
            return (
                style["marker"],
                AXIS_COLORS[dominant],
                style["edge"],
                50,
            )
        if key == "orientation_fit":
            return "^", "#9467bd", "#4b286d", 46
        if key in {"pass_reference", "orientation_reference_pre"}:
            return "P", "white", "#d62728", 52
        if key in {"return_drift", "orientation_reference_post"}:
            return "X", "#d62728", "#7f0000", 52
        return "o", "#bdbdbd", "#666666", 35

    def _draw_stage_positions(self) -> None:
        axis = self.stage_axis
        targets = []
        duplicate_centres_drawn: set[tuple[float, float, float]] = set()
        for capture in self.captures:
            capture_id = str(capture["capture_id"])
            true_target = self.stage_true_positions[capture_id]
            display_target = self.stage_display_positions[capture_id]
            if not np.all(np.isfinite(true_target)):
                continue
            targets.append(true_target)
            duplicate_count = self.stage_duplicate_group_sizes[capture_id]
            if duplicate_count > 1:
                key = tuple(float(value) for value in np.round(true_target, 6))
                if key not in duplicate_centres_drawn:
                    duplicate_centres_drawn.add(key)
                    axis.scatter(
                        [true_target[0]],
                        [true_target[1]],
                        [true_target[2]],
                        marker="+",
                        s=34,
                        color="#777777",
                        linewidth=1.0,
                        alpha=0.7,
                        zorder=1,
                    )
                axis.plot(
                    [true_target[0], display_target[0]],
                    [true_target[1], display_target[1]],
                    [true_target[2], display_target[2]],
                    color="#999999",
                    linestyle=":",
                    linewidth=0.7,
                    alpha=0.55,
                    zorder=1,
                )
            marker, face, edge, size = self._capture_3d_style(capture)
            artist = axis.scatter(
                [display_target[0]],
                [display_target[1]],
                [display_target[2]],
                marker=marker,
                s=size,
                facecolor=face,
                edgecolor=edge,
                linewidth=1.2,
                alpha=0.82,
                picker=6,
            )
            self._register_artist(artist, capture_id)
        axis.set_xlabel("logical X [mm]")
        axis.set_ylabel("logical Y [mm]")
        axis.set_zlabel("logical Z [mm]")
        axis.set_title(
            "All planned capture targets — hover/click to link q ID and results",
            fontsize=12,
        )
        if targets:
            # Independent display aspect keeps the small X/Z excursions
            # selectable beside the much longer Y range. True values remain
            # in the axis coordinates and details panel.
            axis.set_box_aspect((1.25, 2.15, 1.10))
        axis.view_init(elev=22, azim=-58)
        axis.text2D(
            0.01,
            0.01,
            "Exact duplicate logical coordinates are fanned out in display X/Z "
            "only; grey dotted lines point to the true coordinate. "
            "Display aspect is independently scaled.",
            transform=axis.transAxes,
            fontsize=7.5,
            color="#444444",
        )

    def _create_selection_artists(self) -> None:
        self.distance_selection = self.distance_axis.scatter(
            [],
            [],
            s=190,
            facecolors="none",
            edgecolors="#ffd000",
            linewidths=3.0,
            zorder=20,
        )
        self.residual_selection = self.residual_axis.scatter(
            [],
            [],
            s=150,
            facecolors="none",
            edgecolors="#ffd000",
            linewidths=2.8,
            zorder=20,
        )
        self.stage_selection = self.stage_axis.scatter(
            [],
            [],
            [],
            s=170,
            facecolors="none",
            edgecolors="#ffd000",
            linewidths=3.0,
            zorder=20,
        )
        self.stage_reference = self.stage_axis.scatter(
            [],
            [],
            [],
            marker="*",
            s=130,
            facecolors="#fff2a8",
            edgecolors="#111111",
            linewidths=1.3,
            zorder=19,
        )
        (self.stage_reference_line,) = self.stage_axis.plot(
            [],
            [],
            [],
            color="#ffd000",
            linewidth=2.0,
            alpha=0.9,
            zorder=18,
        )
        self.annotations = {}
        for axis in (self.distance_axis, self.residual_axis):
            annotation = axis.annotate(
                "",
                xy=(0.0, 0.0),
                xytext=(10, 10),
                textcoords="offset points",
                bbox={"boxstyle": "round,pad=0.3", "fc": "#fffbe6", "ec": "#555555"},
                arrowprops={"arrowstyle": "->", "color": "#555555"},
                fontsize=8,
                zorder=30,
            )
            annotation.set_visible(False)
            self.annotations[axis] = annotation

    def _purpose_for(self, capture_id: str) -> tuple[str, str]:
        return purpose_key(
            self.capture_by_id[capture_id],
            self.validation_by_id.get(capture_id),
            self.return_by_id.get(capture_id),
            self.orientation_ids,
        )

    def _table_line(self, capture_id: str) -> str:
        capture = self.capture_by_id[capture_id]
        _, short = self._purpose_for(capture_id)
        target = vector_from(capture, "plan_target")
        reference = str(capture.get("reference_id", ""))
        return (
            f"{capture_id:>5} {short:<7} "
            f"g={vector_text(target, 0):<16} ref={reference or '-'}"
        )

    def _details_text(self, capture_id: str) -> str:
        capture = self.capture_by_id[capture_id]
        validation = self.validation_by_id.get(capture_id)
        return_row = self.return_by_id.get(capture_id)
        key, short = self._purpose_for(capture_id)
        target = vector_from(capture, "plan_target")
        stage = vector_from(capture, "stage_readback")
        hardware = vector_from(capture, "stage_hardware")
        reference_id = str(capture.get("reference_id", ""))
        reference_capture = self.capture_by_id.get(reference_id)
        reference_target = (
            vector_from(reference_capture, "plan_target")
            if reference_capture is not None
            else np.full(3, np.nan)
        )
        lines = [
            f"{capture_id}  {short}  ({key})",
            f"target logical g:  {vector_text(target, 3)} mm",
            f"stage readback g: {vector_text(stage, 3)} mm",
            f"hardware h:      {vector_text(hardware, 3)} mm",
            f"reference: {reference_id or 'none'}  "
            f"g={vector_text(reference_target, 3)} mm",
            f"direction/cycle: {capture.get('direction', '')} / "
            f"{capture.get('cycle', '')}",
        ]
        if validation is not None:
            delta_stage = vector_from(validation, "delta_stage")
            delta_camera = vector_from(validation, "delta_camera")
            residual = vector_from(validation, "residual_stage")
            lines.extend(
                [
                    f"family/group: {validation_family(capture, validation)} / "
                    f"{validation.get('summary_group', '')}",
                    f"Δstage:  {vector_text(delta_stage, 3)} mm",
                    f"Δcamera: {vector_text(delta_camera, 3)} mm",
                    f"residual stage XYZ: {vector_text(residual, 3)} mm",
                    "norm stage/camera/error: "
                    f"{value_text(validation.get('stage_displacement_norm_mm'))} / "
                    f"{value_text(validation.get('camera_displacement_norm_mm'))} / "
                    f"{value_text(validation.get('distance_norm_error_mm'))} mm",
                    "axial/lateral/3D: "
                    f"{value_text(validation.get('axial_error_mm'))} / "
                    f"{value_text(validation.get('lateral_error_mm'))} / "
                    f"{value_text(validation.get('error_3d_mm'))} mm",
                ]
            )
        elif return_row is not None:
            lines.extend(
                [
                    "return camera/stage/residual: "
                    f"{value_text(return_row.get('camera_return_shift_3d_mm'))} / "
                    f"{value_text(return_row.get('stage_return_offset_3d_mm'))} / "
                    f"{value_text(return_row.get('return_residual_3d_mm'))} mm",
                    "This point is in return_drift.csv, not validation_accuracy.csv.",
                ]
            )
        elif capture_id in self.orientation_ids:
            lines.append(
                "Used to fit camera→stage R,t; not an independent validation point."
            )
        else:
            lines.append("Reference/context capture; see measurements.csv.")
        return "\n".join(lines)

    def _ensure_table_visible(self, capture_id: str) -> None:
        index = self.capture_ids.index(capture_id)
        if index < self.table_offset:
            self.table_offset = index
        elif index >= self.table_offset + self.table_page_rows:
            self.table_offset = index - self.table_page_rows + 1
        maximum = max(0, len(self.capture_ids) - self.table_page_rows)
        self.table_offset = min(max(0, self.table_offset), maximum)

    def _draw_table(self) -> None:
        axis = self.table_axis
        axis.clear()
        axis.set_axis_off()
        axis.set_xlim(0.0, 1.0)
        axis.set_ylim(0.0, 1.0)
        axis.set_title(
            "All captures — role, target, reference",
            fontsize=11,
            loc="left",
        )
        start = self.table_offset
        stop = min(len(self.capture_ids), start + self.table_page_rows)
        visible = self.capture_ids[start:stop]
        axis.text(
            0.01,
            0.955,
            f"rows {start + 1}–{stop} / {len(self.capture_ids)}",
            fontsize=8,
            color="#555555",
            va="top",
        )
        self.table_hit_rows = []
        y_start = 0.915
        step = 0.026
        for local_index, capture_id in enumerate(visible):
            y = y_start - local_index * step
            selected = capture_id == self.selected_id
            axis.text(
                0.01,
                y,
                self._table_line(capture_id),
                family="monospace",
                fontsize=7.4,
                va="center",
                color="#111111",
                bbox=(
                    {
                        "boxstyle": "round,pad=0.15",
                        "facecolor": "#fff2a8",
                        "edgecolor": "#d4a800",
                    }
                    if selected
                    else None
                ),
            )
            self.table_hit_rows.append((y, capture_id))
        axis.axhline(0.425, color="#bbbbbb", linewidth=0.8)
        axis.text(
            0.01,
            0.405,
            self._details_text(self.selected_id),
            family="monospace",
            fontsize=7.8,
            va="top",
            ha="left",
            linespacing=1.35,
        )

    def _update_selection_artists(self, capture_id: str) -> None:
        validation = self.validation_by_id.get(capture_id)
        if validation is None:
            self.distance_selection.set_offsets(np.empty((0, 2)))
            self.residual_selection.set_offsets(np.empty((0, 2)))
        else:
            x = finite_float(validation.get("stage_displacement_norm_mm"))
            self.distance_selection.set_offsets(
                np.asarray(
                    [[x, finite_float(validation.get("distance_norm_error_mm"))]]
                )
            )
            self.residual_selection.set_offsets(
                np.asarray(
                    [
                        [x, finite_float(validation.get(f"residual_stage_{axis}_mm"))]
                        for axis in AXES
                    ]
                )
            )
        capture = self.capture_by_id[capture_id]
        target = self.stage_display_positions[capture_id]
        reference_id = str(capture.get("reference_id", ""))
        reference = self.capture_by_id.get(reference_id)
        reference_target = (
            self.stage_display_positions[reference_id]
            if reference is not None
            else np.full(3, np.nan)
        )
        if np.all(np.isfinite(target)):
            self.stage_selection._offsets3d = (
                [target[0]],
                [target[1]],
                [target[2]],
            )
        else:
            self.stage_selection._offsets3d = ([], [], [])
        if (
            np.all(np.isfinite(reference_target))
            and reference_id != capture_id
        ):
            self.stage_reference._offsets3d = (
                [reference_target[0]],
                [reference_target[1]],
                [reference_target[2]],
            )
            self.stage_reference_line.set_data_3d(
                [reference_target[0], target[0]],
                [reference_target[1], target[1]],
                [reference_target[2], target[2]],
            )
        else:
            self.stage_reference._offsets3d = ([], [], [])
            self.stage_reference_line.set_data_3d([], [], [])

    def _show_annotation(self, event: Any, capture_id: str) -> None:
        for annotation in self.annotations.values():
            annotation.set_visible(False)
        annotation = self.annotations.get(event.inaxes)
        if annotation is None or event.xdata is None or event.ydata is None:
            return
        capture = self.capture_by_id[capture_id]
        annotation.xy = (event.xdata, event.ydata)
        annotation.set_text(
            f"{capture_id}  g={vector_text(vector_from(capture, 'plan_target'), 1)}"
        )
        annotation.set_visible(True)

    def select(
        self,
        capture_id: str,
        *,
        locked: bool | None,
        event: Any | None = None,
    ) -> None:
        if capture_id not in self.capture_by_id:
            return
        self.selected_id = capture_id
        if locked is not None:
            self.selection_locked = locked
        self._ensure_table_visible(capture_id)
        self._update_selection_artists(capture_id)
        self._draw_table()
        if event is not None:
            self._show_annotation(event, capture_id)
        else:
            for annotation in self.annotations.values():
                annotation.set_visible(False)
        self.figure.canvas.draw_idle()

    def _artist_at_event(self, event: Any) -> str | None:
        if event.inaxes is None:
            return None
        for artist in reversed(self.axis_artists.get(event.inaxes, [])):
            contains, _ = artist.contains(event)
            if contains:
                return self.artist_capture[artist]
        return None

    def _on_motion(self, event: Any) -> None:
        if self.selection_locked:
            return
        capture_id = self._artist_at_event(event)
        if capture_id is None:
            return
        self.select(capture_id, locked=False, event=event)

    def _on_click(self, event: Any) -> None:
        if event.button == 3:
            self.selection_locked = False
            return
        if event.button != 1:
            return
        if event.inaxes is self.table_axis and event.ydata is not None:
            if self.table_hit_rows:
                y, capture_id = min(
                    self.table_hit_rows,
                    key=lambda item: abs(item[0] - event.ydata),
                )
                if abs(y - event.ydata) <= 0.014:
                    self.select(capture_id, locked=True)
                    return
        capture_id = self._artist_at_event(event)
        if capture_id is not None:
            self.select(capture_id, locked=True, event=event)

    def _on_scroll(self, event: Any) -> None:
        if event.inaxes is not self.table_axis:
            return
        delta = -3 if event.button == "up" else 3
        maximum = max(0, len(self.capture_ids) - self.table_page_rows)
        self.table_offset = min(max(0, self.table_offset + delta), maximum)
        self._draw_table()
        self.figure.canvas.draw_idle()

    def _on_key(self, event: Any) -> None:
        if event.key in {"escape", "esc"}:
            self.selection_locked = False
            return
        if event.key not in {"up", "down", "home", "end"}:
            return
        index = self.capture_ids.index(self.selected_id)
        if event.key == "up":
            index = max(0, index - 1)
        elif event.key == "down":
            index = min(len(self.capture_ids) - 1, index + 1)
        elif event.key == "home":
            index = 0
        elif event.key == "end":
            index = len(self.capture_ids) - 1
        self.select(self.capture_ids[index], locked=True)

    def _connect_events(self) -> None:
        canvas = self.figure.canvas
        canvas.mpl_connect("motion_notify_event", self._on_motion)
        canvas.mpl_connect("button_press_event", self._on_click)
        canvas.mpl_connect("scroll_event", self._on_scroll)
        canvas.mpl_connect("key_press_event", self._on_key)


def main() -> int:
    args = parse_args()
    summary_paths = [
        resolve_summary(path) for path in args.session_or_analysis_dir
    ]
    summaries = [load_summary(path) for path in summary_paths]
    summary_path = summary_paths[0]
    summary = summaries[0]

    if len(summaries) > 1 and (args.native or args.no_show):
        raise SystemExit(
            "Multiple-session viewing is available in the WebGL viewer only; "
            "omit --native and --no-show."
        )
    if len(summaries) > 1 and args.d0_mm is not None:
        raise SystemExit(
            "--d0-mm is a single-session diagnostic. Combined placement uses "
            "each summary's absolute_distance_reference instead."
        )

    if not args.native and not args.no_show:
        cad_obj_path = (
            args.cad_obj.resolve() if args.cad_obj is not None else None
        )
        cad_config_path = (
            args.cad_config.resolve() if args.cad_config is not None else None
        )
        cad_model, cad_obj_path, cad_material_path = load_stage_cad_model(
            summary,
            cad_obj_path,
            cad_config_path,
        )
        if len(summaries) > 1:
            payload = build_combined_web_payload(
                list(zip(summaries, summary_paths)),
                cad_model=cad_model,
            )
        else:
            payload = build_web_payload(
                summary,
                summary_path,
                independent_d0_mm=args.d0_mm,
                cad_model=cad_model,
            )
        serve_web_viewer(
            payload,
            bind=args.bind,
            port=args.port,
            open_browser=not args.no_open,
            cad_obj_path=cad_obj_path,
            cad_material_path=cad_material_path,
        )
        return 0

    import matplotlib

    if args.no_show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    viewer = AccuracyViewer(summary, summary_path, plt)
    if args.output is not None:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        viewer.figure.savefig(output, dpi=160)
        print(f"Saved viewer snapshot: {output}")
    if not args.no_show:
        plt.show()
    else:
        plt.close(viewer.figure)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
