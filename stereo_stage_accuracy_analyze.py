#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Process and analyse an Ossila-stage/PAT stereo accuracy session.

Stereo points are triangulated in the left camera frame.  In PAT evaluation
mode a separately registered rigid camera-to-PAT transform is held fixed and
only its translation is composed with each capture's Ossila readback.  The
stage-session cube is then a verification set, never a coordinate fit.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from stereo_stage_accuracy_common import (
    SCRIPT_DIR,
    calibration_baseline_mm,
    calibration_focal_px,
    cube_edge_pairs,
    cube_station_enabled,
    experiment_motion_vectors,
    file_sha256,
    fit_line_against_stage,
    generate_plan,
    json_ready,
    kabsch,
    load_camera_to_pat_transform_json,
    load_json,
    load_stereo_calibration,
    numerical_triangulation_covariance,
    numeric_triplet,
    plan_hash,
    processing_fingerprint,
    point_to_stereo_baseline_line,
    robust_static_point,
    similarity_scale,
    simple_depth_limit_mm,
    simple_depth_sigma_mm,
    transform_points,
    triangulation_angle_deg,
    validate_config,
)


DEFAULT_CONFIG = SCRIPT_DIR / "stereo_stage_accuracy_config.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process captures and quantify stage-axis depth and cuboid accuracy.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("session_dir", type=Path, nargs="?", help="Session from stereo_stage_accuracy_capture.py.")
    parser.add_argument("--config", type=Path, default=None, help="Override session config.")
    parser.add_argument("--skip-processing", action="store_true", help="Use only existing stereo 3D NPZ files.")
    parser.add_argument("--force-processing", action="store_true", help="Re-run tracking/triangulation.")
    parser.add_argument("--continue-on-error", action="store_true", help="Continue after one processing failure.")
    parser.add_argument(
        "--minimum-valid-samples",
        type=int,
        default=None,
        help="Override robust 3D samples required per static capture.",
    )
    parser.add_argument(
        "--theory-only",
        action="store_true",
        help="Print the parallel-stereo theoretical table without a capture session.",
    )
    parser.add_argument("--z-min-mm", type=float, default=200.0, help="Theory-only range start.")
    parser.add_argument("--z-max-mm", type=float, default=1200.0, help="Theory-only range end.")
    parser.add_argument("--z-step-mm", type=float, default=100.0, help="Theory-only range step.")
    parser.add_argument("--output-dir", type=Path, default=None, help="Analysis output directory.")
    return parser.parse_args()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(json_ready(payload), indent=2), encoding="utf-8")
    temporary.replace(path)


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: (
                        ""
                        if value is None
                        or (isinstance(value, (float, np.floating)) and not math.isfinite(float(value)))
                        else value
                    )
                    for key, value in row.items()
                }
            )


def percentile(values: Iterable[float], q: float) -> float:
    array = np.asarray(list(values), dtype=np.float64)
    array = array[np.isfinite(array)]
    return float(np.percentile(array, q)) if array.size else float("nan")


def rms(values: Iterable[float]) -> float:
    array = np.asarray(list(values), dtype=np.float64)
    array = array[np.isfinite(array)]
    return float(np.sqrt(np.mean(array * array))) if array.size else float("nan")


def median(values: Iterable[float]) -> float:
    array = np.asarray(list(values), dtype=np.float64)
    array = array[np.isfinite(array)]
    return float(np.median(array)) if array.size else float("nan")


def resolve_config_path(args: argparse.Namespace) -> Path:
    if args.config is not None:
        return args.config.resolve()
    if args.session_dir is not None:
        session_config = args.session_dir.resolve() / "session_config.json"
        if session_config.exists():
            return session_config
    return DEFAULT_CONFIG.resolve()


def processing_command(config: dict[str, Any], calibration: Path, run_dir: Path) -> list[str]:
    tracking = config["tracking"]
    command = [
        sys.executable,
        str(SCRIPT_DIR / "stereo_process_recording.py"),
        str(run_dir),
        "--stereo-calibration",
        str(calibration),
        "--window-us",
        str(int(tracking["window_us"])),
        "--hop-us",
        str(int(tracking["hop_us"])),
        "--dt-us",
        str(float(tracking["dt_us"])),
        "--roi",
        str(tracking["roi"]),
        "--tracking-method",
        str(tracking["tracking_method"]),
        "--threshold-count",
        str(int(tracking["threshold_count"])),
        "--min-events",
        str(int(tracking["min_events"])),
        "--min-area",
        str(int(tracking["min_area"])),
        "--min-mass",
        str(int(tracking["min_mass"])),
        "--polarity",
        str(tracking["polarity"]),
        "--max-time-gap-sec",
        str(float(tracking.get("max_time_gap_sec", 0.001))),
    ]
    explicit_offset = tracking.get("right_time_offset_sec")
    if explicit_offset is not None:
        command.extend(["--right-time-offset-sec", str(float(explicit_offset))])
    return command


def process_samples(
    *,
    args: argparse.Namespace,
    config: dict[str, Any],
    calibration: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
) -> None:
    failures: list[str] = []
    expected_fingerprint = processing_fingerprint(config, calibration)
    for sample in manifest["samples"]:
        run_text = str(sample.get("accepted_run_dir", "")).strip()
        if not run_text:
            continue
        run_dir = Path(run_text)
        output = run_dir / "stereo_3d" / "stereo_3d_points.npz"
        stored_fingerprint = str(sample.get("processing_fingerprint", ""))
        if (
            output.exists()
            and not args.force_processing
            and (args.skip_processing or stored_fingerprint == expected_fingerprint)
        ):
            sample["stereo_3d_npz"] = str(output.resolve())
            sample["processing_status"] = (
                "existing_unverified"
                if args.skip_processing and stored_fingerprint != expected_fingerprint
                else "processed"
            )
            continue
        if args.skip_processing:
            failures.append(str(sample["sample_id"]))
            continue
        command = processing_command(config, calibration, run_dir)
        print("[PROCESS] " + " ".join(command), flush=True)
        sample["processing_status"] = "processing"
        atomic_write_json(manifest_path, manifest)
        completed = subprocess.run(command, check=False)
        if completed.returncode == 0 and output.exists():
            sample["stereo_3d_npz"] = str(output.resolve())
            sample["processing_fingerprint"] = expected_fingerprint
            sample["processing_status"] = "processed"
            sample["status"] = "processed"
            atomic_write_json(manifest_path, manifest)
        else:
            failures.append(str(sample["sample_id"]))
            sample["processing_status"] = "failed"
            sample["processing_error"] = f"return code {completed.returncode}"
            atomic_write_json(manifest_path, manifest)
            if not args.continue_on_error:
                raise SystemExit(
                    f"Processing failed for {sample['sample_id']}; "
                    "use --continue-on-error to analyse the remaining captures."
                )
    if failures:
        print(f"[WARN] samples without usable processing output: {', '.join(failures)}")


def build_sample_rows(
    manifest: dict[str, Any],
    config: dict[str, Any],
    minimum_valid_samples: int,
    calibration: dict[str, np.ndarray],
) -> list[dict[str, Any]]:
    analysis = config["analysis"]
    pat_center = numeric_triplet(config["target"]["pat_center_mm"], "target.pat_center_mm")
    rows: list[dict[str, Any]] = []
    for sample in manifest["samples"]:
        npz_text = str(sample.get("stereo_3d_npz", "")).strip()
        if not npz_text and sample.get("accepted_run_dir"):
            candidate = Path(sample["accepted_run_dir"]) / "stereo_3d" / "stereo_3d_points.npz"
            if candidate.exists():
                npz_text = str(candidate.resolve())
        stage_arrival = sample.get("stage_arrival", {})
        command_expected = np.asarray(
            sample["expected_relative_pat_mm"],
            dtype=np.float64,
        )
        pat_target = np.asarray(sample["pat_target_mm"], dtype=np.float64)
        row: dict[str, Any] = {
            "sample_id": str(sample["sample_id"]),
            "pass_name": str(sample["pass_name"]),
            "pass_direction": str(sample["pass_direction"]),
            "cycle_index": int(sample.get("cycle_index", -1)),
            "station_index": int(sample["station_index"]),
            "stage_global_mm": float(sample["stage_command_global_mm"]),
            "stage_delta_mm": float(sample["stage_delta_from_reference_mm"]),
            "stage_command_global_mm": float(sample["stage_command_global_mm"]),
            "stage_command_delta_mm": float(sample["stage_delta_from_reference_mm"]),
            "kind": str(sample["kind"]),
            "vertex_id": str(sample.get("vertex_id", "")),
            "vertex_signs": sample.get("vertex_signs"),
            "expected_mm": command_expected.copy(),
            "command_expected_mm": command_expected,
            "pat_target_mm": pat_target,
            "local_offset_mm": pat_target - pat_center,
            "data_role": (
                "registration"
                if str(sample["pass_direction"]) == "registration"
                and int(sample.get("cycle_index", -1)) == -1
                else "evaluation"
            ),
            "stage_readback_global_mm": float(stage_arrival.get("readback_global_mm", float("nan"))),
            "stage_command_hardware_mm": float(
                stage_arrival.get("command_hardware_mm", float("nan"))
            ),
            "stage_readback_hardware_mm": float(
                stage_arrival.get("readback_hardware_mm", float("nan"))
            ),
            "stage_readback_error_mm": float(stage_arrival.get("readback_error_mm", float("nan"))),
            "stereo_3d_npz": npz_text,
            "usable": False,
            "reason": "",
        }
        if not npz_text or not Path(npz_text).exists():
            row["reason"] = "missing stereo_3d_points.npz"
            rows.append(row)
            continue
        try:
            stats = robust_static_point(
                Path(npz_text),
                trim_start_sec=float(analysis.get("trim_start_sec", 0.05)),
                trim_end_sec=float(analysis.get("trim_end_sec", 0.05)),
                outlier_floor_mm=float(analysis.get("sample_outlier_floor_mm", 0.5)),
                mad_factor=float(analysis.get("sample_outlier_mad_factor", 6.0)),
            )
        except Exception as exc:
            row["reason"] = f"could not read 3D track: {exc}"
            rows.append(row)
            continue
        row.update(stats)
        center = np.asarray(stats["center_mm"], dtype=np.float64)
        row["center_camera_mm"] = center.copy()
        if np.all(np.isfinite(center)):
            baseline_metrics = point_to_stereo_baseline_line(
                center,
                calibration,
            )
            row["camera_baseline_perpendicular_range_mm"] = float(
                baseline_metrics["perpendicular_distance_mm"]
            )
            row["camera_baseline_foot_left_camera_mm"] = np.asarray(
                baseline_metrics["foot_left_camera_mm"],
                dtype=np.float64,
            )
        else:
            row["camera_baseline_perpendicular_range_mm"] = float("nan")
            row["camera_baseline_foot_left_camera_mm"] = np.full(3, np.nan)
        if float(stats["raw_valid_fraction"]) < float(
            analysis["minimum_sample_valid_fraction"]
        ):
            row["reason"] = (
                f"raw valid fraction {float(stats['raw_valid_fraction']):.3f} "
                f"< {float(analysis['minimum_sample_valid_fraction']):.3f}"
            )
        elif int(stats["robust_samples"]) < minimum_valid_samples:
            row["reason"] = f"only {stats['robust_samples']} robust samples"
        elif not np.all(np.isfinite(center)):
            row["reason"] = "non-finite robust centre"
        else:
            row["usable"] = True
        rows.append(row)
    return rows


def prepare_readback_truth(
    rows: list[dict[str, Any]],
    config: dict[str, Any],
) -> float:
    """Replace commanded stage offsets with offsets from the stage readback."""
    registration_readbacks = [
        float(row["stage_readback_global_mm"])
        for row in rows
        if row["data_role"] == "registration"
        and math.isfinite(float(row["stage_readback_global_mm"]))
    ]
    if not registration_readbacks:
        raise SystemExit(
            "The dedicated registration pass has no finite stage readback. "
            "Commanded positions are not accepted as a substitute for stage truth."
        )
    reference_readback = median(registration_readbacks)
    _, _, stage_vector = experiment_motion_vectors(config)
    for row in rows:
        readback = float(row["stage_readback_global_mm"])
        if not math.isfinite(readback):
            row["usable"] = False
            prior = str(row.get("reason", "")).strip()
            row["reason"] = (
                f"{prior}; missing finite stage readback"
                if prior
                else "missing finite stage readback"
            )
            row["stage_truth_delta_mm"] = float("nan")
            continue
        truth_delta = readback - reference_readback
        row["stage_truth_delta_mm"] = truth_delta
        row["expected_mm"] = (
            np.asarray(row["local_offset_mm"], dtype=np.float64)
            + stage_vector * truth_delta
        )
    return reference_readback


def aggregate_reference_vertices(
    rows: list[dict[str, Any]],
    reference_global_mm: float,
    registration_pass_name: str,
) -> tuple[np.ndarray, np.ndarray, list[str], list[str]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if (
            row["usable"]
            and row["kind"] == "vertex"
            and row["data_role"] == "registration"
            and math.isclose(float(row["stage_global_mm"]), reference_global_mm, abs_tol=1e-9)
        ):
            grouped[str(row["vertex_id"])].append(row)
    source: list[np.ndarray] = []
    target: list[np.ndarray] = []
    labels: list[str] = []
    sample_ids: list[str] = []
    for label in sorted(grouped):
        members = grouped[label]
        source.append(np.median(np.vstack([item["expected_mm"] for item in members]), axis=0))
        target.append(np.median(np.vstack([item["center_mm"] for item in members]), axis=0))
        labels.append(label)
        sample_ids.extend(str(item["sample_id"]) for item in members)
    if len(source) != 8:
        raise SystemExit(
            "Dedicated registration requires all eight usable cuboid vertices. "
            f"Only {len(source)} unique vertices were usable in pass "
            f"{registration_pass_name!r}; evaluation passes are never used to fill gaps."
        )
    source_array = np.vstack(source)
    target_array = np.vstack(target)
    rank = int(np.linalg.matrix_rank(source_array - source_array.mean(axis=0)))
    if rank < 3:
        raise SystemExit(
            "Usable reference vertices do not span 3D. Capture non-coplanar cuboid vertices."
        )
    return source_array, target_array, labels, sample_ids


def apply_reference_registration(
    rows: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    reference = float(
        config["analysis"].get(
            "reference_stage_global_mm",
            config["stage"]["reference_global_mm"],
        )
    )
    registration_pass_name = str(config["analysis"]["registration_pass_name"])
    reference_readback = prepare_readback_truth(rows, config)
    reference_hardware_readbacks = [
        float(row.get("stage_readback_hardware_mm", float("nan")))
        for row in rows
        if row["data_role"] == "registration"
        and math.isfinite(float(row.get("stage_readback_hardware_mm", float("nan"))))
    ]
    source, target, labels, sample_ids = aggregate_reference_vertices(
        rows,
        reference,
        registration_pass_name,
    )
    rotation, translation = kabsch(source, target)
    reference_predicted = transform_points(source, rotation, translation)
    reference_residuals = np.linalg.norm(target - reference_predicted, axis=1)
    scale = similarity_scale(source, target, rotation)
    moving_body, physical_stage_vector, stage_vector = experiment_motion_vectors(config)
    axis_camera_raw = rotation @ stage_vector
    axis_camera_scale = float(np.linalg.norm(axis_camera_raw))
    axis_camera = axis_camera_raw / axis_camera_scale

    for row in rows:
        if not row["usable"]:
            continue
        predicted = rotation @ row["expected_mm"] + translation
        residual = np.asarray(row["center_mm"], dtype=np.float64) - predicted
        axial_error = float(np.dot(residual, axis_camera))
        cross_vector = residual - axial_error * axis_camera
        row.update(
            {
                "predicted_camera_mm": predicted,
                "residual_camera_mm": residual,
                "predicted_evaluation_mm": predicted,
                "residual_evaluation_mm": residual,
                "residual_3d_mm": float(np.linalg.norm(residual)),
                "axial_error_mm": axial_error,
                "cross_axis_error_mm": float(np.linalg.norm(cross_vector)),
                "measured_axial_coordinate_mm": float(
                    np.dot(np.asarray(row["center_mm"]) - translation, axis_camera)
                ),
            }
        )
        command_predicted = rotation @ row["command_expected_mm"] + translation
        command_residual = (
            np.asarray(row["center_mm"], dtype=np.float64) - command_predicted
        )
        command_axial_error = float(np.dot(command_residual, axis_camera))
        row.update(
            {
                "command_predicted_camera_mm": command_predicted,
                "command_residual_camera_mm": command_residual,
                "command_residual_evaluation_mm": command_residual,
                "command_residual_3d_mm": float(np.linalg.norm(command_residual)),
                "command_axial_error_mm": command_axial_error,
            }
        )
    return {
        "evaluation_frame": "camera_relative",
        "registration_source": "stage_session_kabsch",
        "reference_stage_global_mm": reference,
        "reference_stage_readback_global_mm": reference_readback,
        "reference_stage_readback_hardware_mm": median(reference_hardware_readbacks),
        "registration_pass_name": registration_pass_name,
        "reference_vertex_ids": labels,
        "reference_sample_ids": sample_ids,
        "rotation_expected_to_left_camera": rotation,
        "translation_left_camera_mm": translation,
        "rotation_expected_to_evaluation": rotation,
        "translation_evaluation_mm": translation,
        "self_registered_reference_target_left_camera_mm": translation,
        "self_registered_reference_camera_z_mm": float(translation[2]),
        "self_registered_reference_slant_range_mm": float(np.linalg.norm(translation)),
        "stage_axis_left_camera": axis_camera,
        "stage_axis_evaluation": axis_camera,
        "moving_body": moving_body,
        "moving_body_translation_per_global_mm_in_pat": physical_stage_vector,
        "apparent_target_translation_per_global_mm_in_pat": stage_vector,
        "stage_scale_from_config_mm_per_global_mm": axis_camera_scale,
        "similarity_scale_diagnostic": scale,
        "reference_fit_rms_mm": rms(reference_residuals),
        "reference_fit_max_mm": float(np.max(reference_residuals)),
    }


def apply_pat_frame_registration(
    rows: list[dict[str, Any]],
    config: dict[str, Any],
    transform_data: dict[str, Any],
    stage_direction: int,
) -> dict[str, Any]:
    """Evaluate points in physical PAT coordinates without fitting this session.

    The saved transform is the camera pose at its registration readback.  The
    stage carries the camera, so its PAT-frame translation is composed for every
    capture from the actual global readback.
    """
    moving_body, physical_stage_vector, apparent_stage_vector = (
        experiment_motion_vectors(config)
    )
    if moving_body != "camera":
        raise SystemExit(
            "PAT-frame stage evaluation currently requires "
            "experiment.moving_body='camera'."
        )
    stage_vector_norm = float(np.linalg.norm(physical_stage_vector))
    stage_axis_pat = physical_stage_vector / stage_vector_norm
    rotation = np.asarray(transform_data["R_camera_to_pat"], dtype=np.float64)
    translation_at_registration = np.asarray(
        transform_data["t_camera_to_pat_mm"],
        dtype=np.float64,
    )
    datum = float(config["stage"]["datum_mm"])
    direction = int(stage_direction)
    transform_hardware = float(transform_data["stage_hardware_readback_mm"])
    transform_global = (transform_hardware - datum) / direction
    pat_center = numeric_triplet(
        config["target"]["pat_center_mm"],
        "target.pat_center_mm",
    )

    for row in rows:
        readback = float(row["stage_readback_global_mm"])
        if not math.isfinite(readback):
            row["usable"] = False
            prior = str(row.get("reason", "")).strip()
            row["reason"] = (
                f"{prior}; missing finite stage readback"
                if prior
                else "missing finite stage readback"
            )
            row["stage_truth_delta_mm"] = float("nan")
            continue
        truth_delta = readback - transform_global
        row["stage_truth_delta_mm"] = truth_delta
        if not row["usable"]:
            continue
        center_camera = np.asarray(row["center_camera_mm"], dtype=np.float64)
        current_translation = (
            translation_at_registration
            + physical_stage_vector * truth_delta
        )
        measured_pat = rotation @ center_camera + current_translation
        expected_pat = np.asarray(row["pat_target_mm"], dtype=np.float64)
        residual_pat = measured_pat - expected_pat
        axial_error = float(np.dot(residual_pat, stage_axis_pat))
        cross_vector = residual_pat - axial_error * stage_axis_pat

        command_delta = (
            float(row["stage_command_global_mm"]) - transform_global
        )
        command_translation = (
            translation_at_registration
            + physical_stage_vector * command_delta
        )
        measured_pat_from_command = rotation @ center_camera + command_translation
        command_residual_pat = measured_pat_from_command - expected_pat
        command_axial_error = float(
            np.dot(command_residual_pat, stage_axis_pat)
        )
        row.update(
            {
                "center_mm": measured_pat,
                "center_pat_mm": measured_pat,
                "expected_mm": expected_pat,
                "command_expected_mm": expected_pat.copy(),
                "predicted_evaluation_mm": expected_pat,
                "residual_evaluation_mm": residual_pat,
                "predicted_pat_mm": expected_pat,
                "residual_pat_mm": residual_pat,
                "residual_3d_mm": float(np.linalg.norm(residual_pat)),
                "axial_error_mm": axial_error,
                "cross_axis_error_mm": float(np.linalg.norm(cross_vector)),
                "measured_axial_coordinate_mm": float(
                    np.dot(measured_pat - pat_center, stage_axis_pat)
                ),
                "camera_to_pat_translation_current_mm": current_translation,
                "command_measured_pat_mm": measured_pat_from_command,
                "command_residual_evaluation_mm": command_residual_pat,
                "command_residual_pat_mm": command_residual_pat,
                "command_residual_3d_mm": float(
                    np.linalg.norm(command_residual_pat)
                ),
                "command_axial_error_mm": command_axial_error,
            }
        )

    registration_rows = [
        row
        for row in rows
        if row["usable"]
        and row["data_role"] == "registration"
        and row["kind"] == "vertex"
    ]
    verification_by_vertex: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in registration_rows:
        verification_by_vertex[str(row["vertex_id"])].append(row)
    verification_expected: list[np.ndarray] = []
    verification_measured: list[np.ndarray] = []
    verification_sample_ids: list[str] = []
    for label in sorted(verification_by_vertex):
        members = verification_by_vertex[label]
        verification_expected.append(
            np.median(
                np.vstack([np.asarray(item["pat_target_mm"]) for item in members]),
                axis=0,
            )
        )
        verification_measured.append(
            np.median(
                np.vstack([np.asarray(item["center_pat_mm"]) for item in members]),
                axis=0,
            )
        )
        verification_sample_ids.extend(str(item["sample_id"]) for item in members)
    verification_residuals = np.asarray([], dtype=np.float64)
    verification: dict[str, Any] = {
        "role": "verification_only_not_used_to_fit_evaluation_coordinates",
        "available": bool(verification_expected),
        "unique_vertices": len(verification_expected),
        "sample_ids": verification_sample_ids,
    }
    if verification_expected:
        expected_array = np.vstack(verification_expected)
        measured_array = np.vstack(verification_measured)
        verification_residuals = np.linalg.norm(
            measured_array - expected_array,
            axis=1,
        )
        verification.update(
            {
                "rms_mm": rms(verification_residuals),
                "max_mm": float(np.max(verification_residuals)),
                "residual_vectors_pat_mm": measured_array - expected_array,
            }
        )
        if (
            len(verification_expected) >= 4
            and np.linalg.matrix_rank(
                expected_array - expected_array.mean(axis=0)
            )
            >= 3
        ):
            drift_rotation, drift_translation = kabsch(
                expected_array,
                measured_array,
            )
            verification.update(
                {
                    "diagnostic_fit_rotation_pat_to_measured_pat": drift_rotation,
                    "diagnostic_fit_translation_pat_mm": drift_translation,
                    "diagnostic_similarity_scale": similarity_scale(
                        expected_array,
                        measured_array,
                        drift_rotation,
                    ),
                }
            )

    transform_pose_pat_in_camera = rotation.T @ (
        pat_center - translation_at_registration
    )
    registration_readbacks = [
        float(row["stage_readback_global_mm"])
        for row in rows
        if row["data_role"] == "registration"
        and math.isfinite(float(row["stage_readback_global_mm"]))
    ]
    registration_hardware_readbacks = [
        float(row["stage_readback_hardware_mm"])
        for row in rows
        if row["data_role"] == "registration"
        and math.isfinite(float(row["stage_readback_hardware_mm"]))
    ]
    session_reference_global = median(registration_readbacks)
    if not math.isfinite(session_reference_global):
        session_reference_global = transform_global
    session_reference_translation = (
        translation_at_registration
        + physical_stage_vector
        * (session_reference_global - transform_global)
    )
    reference_pat_in_camera = rotation.T @ (
        pat_center - session_reference_translation
    )
    return {
        "evaluation_frame": "pat",
        "registration_source": "external_fixed_camera_to_pat",
        "registration_samples_used_only_for_verification": True,
        "reference_stage_global_mm": float(
            config["stage"]["reference_global_mm"]
        ),
        "reference_stage_readback_global_mm": median(registration_readbacks),
        "reference_stage_readback_hardware_mm": median(
            registration_hardware_readbacks
        ),
        "transform_stage_readback_hardware_mm": transform_hardware,
        "transform_stage_readback_global_mm": transform_global,
        "camera_to_pat_transform_path": str(transform_data["transform_path"]),
        "camera_to_pat_transform_sha256": str(
            transform_data["transform_sha256"]
        ),
        "camera_to_pat_stereo_calibration_sha256": str(
            transform_data["stereo_calibration_sha256"]
        ),
        "camera_to_pat_registration_quality": transform_data["quality"],
        "R_camera_to_pat": rotation,
        "t_camera_to_pat_at_registration_mm": translation_at_registration,
        "rotation_expected_to_evaluation": np.eye(3),
        "translation_evaluation_mm": np.zeros(3),
        "stage_axis_evaluation": stage_axis_pat,
        # Compatibility aliases used by the common aggregation/plot code.
        "rotation_expected_to_left_camera": np.eye(3),
        "translation_left_camera_mm": np.zeros(3),
        "stage_axis_left_camera": stage_axis_pat,
        "external_registered_reference_target_left_camera_mm": (
            reference_pat_in_camera
        ),
        "transform_pose_target_left_camera_mm": transform_pose_pat_in_camera,
        "reference_camera_z_mm": float(reference_pat_in_camera[2]),
        "reference_camera_slant_range_mm": float(
            np.linalg.norm(reference_pat_in_camera)
        ),
        "moving_body": moving_body,
        "moving_body_translation_per_global_mm_in_pat": physical_stage_vector,
        "apparent_target_translation_per_global_mm_in_pat": apparent_stage_vector,
        "stage_scale_from_config_mm_per_global_mm": stage_vector_norm,
        "verification": verification,
        "reference_fit_rms_mm": (
            rms(verification_residuals)
            if verification_residuals.size
            else float("nan")
        ),
        "reference_fit_max_mm": (
            float(np.max(verification_residuals))
            if verification_residuals.size
            else float("nan")
        ),
        "similarity_scale_diagnostic": float(
            verification.get("diagnostic_similarity_scale", float("nan"))
        ),
    }


def centre_for_pass_station(
    members: list[dict[str, Any]],
    registration: dict[str, Any],
    calibration: dict[str, np.ndarray],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    if not members:
        return None
    planned_centre_rows = [
        row for row in members if str(row["kind"]).startswith("center")
    ]
    centre_rows = [row for row in planned_centre_rows if row["usable"]]
    first = members[0]
    translation = np.asarray(
        registration.get(
            "translation_evaluation_mm",
            registration["translation_left_camera_mm"],
        )
    )
    axis_evaluation = np.asarray(
        registration.get(
            "stage_axis_evaluation",
            registration["stage_axis_left_camera"],
        )
    )
    rotation_camera_to_evaluation = np.asarray(
        registration.get("R_camera_to_pat", np.eye(3)),
        dtype=np.float64,
    )
    nan_vector = np.full(3, np.nan)
    if centre_rows:
        measured = np.median(
            np.vstack([np.asarray(row["center_mm"]) for row in centre_rows]),
            axis=0,
        )
        measured_camera = np.median(
            np.vstack(
                [
                    np.asarray(row.get("center_camera_mm", row["center_mm"]))
                    for row in centre_rows
                ]
            ),
            axis=0,
        )
        predicted = np.median(
            np.vstack(
                [
                    np.asarray(
                        row.get(
                            "predicted_evaluation_mm",
                            row.get("predicted_camera_mm"),
                        )
                    )
                    for row in centre_rows
                ]
            ),
            axis=0,
        )
        residual = measured - predicted
        axial_errors = [float(row["axial_error_mm"]) for row in centre_rows]
        command_axial_errors = [
            float(row["command_axial_error_mm"]) for row in centre_rows
        ]
        cross_errors = [float(row["cross_axis_error_mm"]) for row in centre_rows]
        scatter_values = [float(row["scatter_p95_mm"]) for row in centre_rows]
        measured_axial_values = [
            float(row["measured_axial_coordinate_mm"]) for row in centre_rows
        ]
        truth_deltas = [float(row["stage_truth_delta_mm"]) for row in centre_rows]
        worst_index = int(np.argmax(np.abs(axial_errors)))
        signed_worst_axial_error = axial_errors[worst_index]
    else:
        measured = nan_vector.copy()
        measured_camera = nan_vector.copy()
        predicted = nan_vector.copy()
        residual = nan_vector.copy()
        axial_errors = []
        command_axial_errors = []
        cross_errors = []
        scatter_values = []
        measured_axial_values = []
        truth_deltas = []
        signed_worst_axial_error = float("nan")

    readbacks = [
        float(row["stage_readback_global_mm"])
        for row in members
        if math.isfinite(float(row["stage_readback_global_mm"]))
    ]
    hardware_commands = [
        float(row.get("stage_command_hardware_mm", float("nan")))
        for row in members
        if math.isfinite(float(row.get("stage_command_hardware_mm", float("nan"))))
    ]
    hardware_readbacks = [
        float(row.get("stage_readback_hardware_mm", float("nan")))
        for row in members
        if math.isfinite(float(row.get("stage_readback_hardware_mm", float("nan"))))
    ]
    readback_errors = [
        float(row["stage_readback_error_mm"])
        for row in members
        if math.isfinite(float(row["stage_readback_error_mm"]))
    ]
    by_kind: dict[str, list[float]] = defaultdict(list)
    for row in centre_rows:
        by_kind[str(row["kind"])].append(float(row["axial_error_mm"]))
    pre_post_drift = float("nan")
    if by_kind.get("center_pre") and by_kind.get("center_post"):
        pre_post_drift = median(by_kind["center_post"]) - median(by_kind["center_pre"])
    expected_captures = (
        2
        if bool(config["experiment"]["center_before_and_after"])
        else len(planned_centre_rows)
    )
    usable_captures = len(centre_rows)
    capture_fraction = (
        usable_captures / expected_captures if expected_captures else 0.0
    )
    if np.all(np.isfinite(measured_camera)):
        baseline_metrics = point_to_stereo_baseline_line(
            measured_camera,
            calibration,
        )
    else:
        baseline_metrics = {
            "perpendicular_distance_mm": float("nan"),
            "foot_left_camera_mm": nan_vector.copy(),
            "foot_from_left_optical_center_mm": float("nan"),
            "foot_fraction_of_left_to_right_baseline": float("nan"),
            "foot_is_between_optical_centers": False,
            "baseline_length_mm": float("nan"),
        }
    result: dict[str, Any] = {
        "pass_name": str(first["pass_name"]),
        "pass_direction": str(first["pass_direction"]),
        "cycle_index": int(first.get("cycle_index", -1)),
        "stage_global_mm": float(first["stage_global_mm"]),
        "stage_delta_mm": float(first["stage_delta_mm"]),
        "stage_truth_delta_median_mm": median(truth_deltas),
        "center_source": "individual_center_captures",
        "center_expected_captures": expected_captures,
        "center_usable_captures": usable_captures,
        "center_capture_fraction": capture_fraction,
        "evaluation_frame": str(registration["evaluation_frame"]),
        "center_evaluation_mm": measured,
        "center_pat_mm": (
            measured
            if str(registration["evaluation_frame"]) == "pat"
            else nan_vector.copy()
        ),
        "center_camera_mm": measured_camera,
        "camera_z_mm": float(measured_camera[2]),
        "camera_slant_range_mm": float(np.linalg.norm(measured_camera)),
        "camera_baseline_perpendicular_range_mm": float(
            baseline_metrics["perpendicular_distance_mm"]
        ),
        "camera_baseline_foot_left_camera_mm": np.asarray(
            baseline_metrics["foot_left_camera_mm"],
            dtype=np.float64,
        ),
        "camera_baseline_foot_from_left_mm": float(
            baseline_metrics["foot_from_left_optical_center_mm"]
        ),
        "camera_baseline_foot_fraction": float(
            baseline_metrics["foot_fraction_of_left_to_right_baseline"]
        ),
        "camera_baseline_foot_between_centers": bool(
            baseline_metrics["foot_is_between_optical_centers"]
        ),
        "stereo_baseline_length_mm": float(
            baseline_metrics["baseline_length_mm"]
        ),
        "predicted_evaluation_mm": predicted,
        "center_residual_evaluation_mm": residual,
        "center_axial_error_mm": signed_worst_axial_error,
        "center_axial_bias_mm": median(axial_errors),
        "center_axial_error_rms_mm": rms(axial_errors),
        "center_axial_error_p95_abs_mm": percentile(np.abs(axial_errors), 95),
        "center_axial_error_max_abs_mm": (
            float(np.max(np.abs(axial_errors))) if axial_errors else float("nan")
        ),
        "center_command_axial_error_max_abs_mm": (
            float(np.max(np.abs(command_axial_errors)))
            if command_axial_errors
            else float("nan")
        ),
        "center_cross_axis_error_mm": median(cross_errors),
        "center_cross_axis_error_max_mm": (
            float(np.max(cross_errors)) if cross_errors else float("nan")
        ),
        "center_pre_post_axial_drift_mm": pre_post_drift,
        "measured_axial_coordinate_mm": median(measured_axial_values),
        "static_scatter_p95_median_mm": median(scatter_values),
        "stage_readback_global_median_mm": median(readbacks),
        "stage_command_hardware_median_mm": median(hardware_commands),
        "stage_readback_hardware_median_mm": median(hardware_readbacks),
        "stage_readback_error_max_abs_mm": (
            float(np.max(np.abs(readback_errors))) if readback_errors else float("nan")
        ),
        "theory": {},
    }
    coverage = float(config["analysis"].get("theory_coverage_sigma", 1.96))
    if np.all(np.isfinite(measured_camera)):
        for pixel_sigma in config["analysis"].get("pixel_sigma_scenarios", [0.25]):
            propagated = numerical_triangulation_covariance(
                measured_camera,
                calibration,
                per_camera_pixel_sigma=float(pixel_sigma),
            )
            covariance = np.asarray(propagated["covariance_mm2"])
            axis_camera = (
                rotation_camera_to_evaluation.T @ axis_evaluation
            )
            axial_sigma = math.sqrt(
                max(0.0, float(axis_camera @ covariance @ axis_camera))
            )
            result["theory"][str(float(pixel_sigma))] = {
                "pixel_sigma_per_camera": float(pixel_sigma),
                "sigma_axis_mm": axial_sigma,
                "coverage_axis_mm": coverage * axial_sigma,
                "sigma_camera_z_mm": float(propagated["sigma_xyz_mm"][2]),
                "left_u_px": float(propagated["observation_px"][0]),
                "left_v_px": float(propagated["observation_px"][1]),
                "right_u_px": float(propagated["observation_px"][2]),
                "right_v_px": float(propagated["observation_px"][3]),
                "estimator_scope": "one triangulated tracking window",
            }
        result["triangulation_angle_deg"] = triangulation_angle_deg(
            measured_camera,
            calibration,
        )
    else:
        result["triangulation_angle_deg"] = float("nan")
    return result


def build_pass_centres(
    rows: list[dict[str, Any]],
    registration: dict[str, Any],
    calibration: dict[str, np.ndarray],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, float], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if str(row["data_role"]) == "registration":
            continue
        grouped[(str(row["pass_name"]), float(row["stage_global_mm"]))].append(row)
    centres: list[dict[str, Any]] = []
    for key in sorted(grouped, key=lambda item: (item[0], item[1])):
        centre = centre_for_pass_station(
            grouped[key],
            registration,
            calibration,
            config,
        )
        if centre is not None:
            centres.append(centre)
    return centres


def aggregate_cube_station(
    station_rows: list[dict[str, Any]],
    registration: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    by_vertex: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in station_rows:
        if row["usable"] and row["kind"] == "vertex":
            by_vertex[str(row["vertex_id"])].append(row)
    measured_by_vertex: dict[str, np.ndarray] = {}
    expected_by_vertex: dict[str, np.ndarray] = {}
    scatter_values: list[float] = []
    for label, members in by_vertex.items():
        measured_by_vertex[label] = np.median(
            np.vstack([np.asarray(item["center_mm"]) for item in members]),
            axis=0,
        )
        expected_by_vertex[label] = np.median(
            np.vstack([np.asarray(item["expected_mm"]) for item in members]),
            axis=0,
        )
        scatter_values.extend(float(item["scatter_p95_mm"]) for item in members)

    valid_fraction = len(measured_by_vertex) / 8.0
    rotation = np.asarray(
        registration.get(
            "rotation_expected_to_evaluation",
            registration["rotation_expected_to_left_camera"],
        )
    )
    translation = np.asarray(
        registration.get(
            "translation_evaluation_mm",
            registration["translation_left_camera_mm"],
        )
    )
    vertex_errors: list[float] = []
    for label, measured in measured_by_vertex.items():
        predicted = rotation @ expected_by_vertex[label] + translation
        vertex_errors.append(float(np.linalg.norm(measured - predicted)))

    edge_rows: list[dict[str, Any]] = []
    axis_names = ("x", "y", "z")
    first = station_rows[0] if station_rows else {}
    for left, right, axis in cube_edge_pairs():
        if left not in measured_by_vertex or right not in measured_by_vertex:
            continue
        measured_length = float(
            np.linalg.norm(measured_by_vertex[left] - measured_by_vertex[right])
        )
        expected_length = float(
            np.linalg.norm(expected_by_vertex[left] - expected_by_vertex[right])
        )
        edge_rows.append(
            {
                "pass_name": str(first.get("pass_name", "")),
                "pass_direction": str(first.get("pass_direction", "")),
                "cycle_index": int(first.get("cycle_index", -1)),
                "left": left,
                "right": right,
                "axis": axis_names[axis],
                "expected_length_mm": expected_length,
                "measured_length_mm": measured_length,
                "error_mm": measured_length - expected_length,
                "error_percent": 100.0 * (measured_length / expected_length - 1.0),
            }
        )

    procrustes_rms = float("nan")
    affine_matrix = np.full((3, 3), np.nan)
    affine_singular_values = np.full(3, np.nan)
    axis_scales = np.full(3, np.nan)
    affine_volume_scale = float("nan")
    maximum_axis_cosine = float("nan")
    if len(measured_by_vertex) >= 4:
        labels = sorted(measured_by_vertex)
        expected = np.vstack([expected_by_vertex[label] for label in labels])
        measured = np.vstack([measured_by_vertex[label] for label in labels])
        local_expected = expected - expected.mean(axis=0)
        local_measured = measured - measured.mean(axis=0)
        if np.linalg.matrix_rank(local_expected) >= 3:
            local_rotation, local_translation = kabsch(expected, measured)
            local_predicted = transform_points(expected, local_rotation, local_translation)
            procrustes_rms = rms(np.linalg.norm(measured - local_predicted, axis=1))
            row_mapping, _, _, _ = np.linalg.lstsq(local_expected, local_measured, rcond=None)
            affine_matrix = row_mapping.T
            affine_singular_values = np.linalg.svd(affine_matrix, compute_uv=False)
            axis_scales = np.linalg.norm(affine_matrix, axis=0)
            affine_volume_scale = float(np.linalg.det(affine_matrix))
            normalised_columns = affine_matrix / np.maximum(axis_scales, 1e-12)
            cosines = [
                abs(float(np.dot(normalised_columns[:, left], normalised_columns[:, right])))
                for left, right in ((0, 1), (0, 2), (1, 2))
            ]
            maximum_axis_cosine = max(cosines)

    edge_errors = [float(item["error_mm"]) for item in edge_rows]
    axis_edge_mean_error = {
        axis: median(item["error_mm"] for item in edge_rows if item["axis"] == axis)
        for axis in axis_names
    }
    return {
        "pass_name": str(first.get("pass_name", "")),
        "pass_direction": str(first.get("pass_direction", "")),
        "cycle_index": int(first.get("cycle_index", -1)),
        "valid_unique_vertices": len(measured_by_vertex),
        "valid_vertex_fraction": valid_fraction,
        "vertex_rmse_mm": rms(vertex_errors),
        "vertex_p95_mm": percentile(vertex_errors, 95),
        "vertex_max_mm": float(np.max(vertex_errors)) if vertex_errors else float("nan"),
        "edge_count": len(edge_rows),
        "edge_error_rms_mm": rms(edge_errors),
        "edge_error_max_abs_mm": (
            float(np.max(np.abs(edge_errors))) if edge_errors else float("nan")
        ),
        "axis_edge_mean_error_mm": axis_edge_mean_error,
        "local_rigid_shape_rms_mm": procrustes_rms,
        "affine_matrix": affine_matrix,
        "affine_singular_values": affine_singular_values,
        "axis_scales": axis_scales,
        "affine_volume_scale": affine_volume_scale,
        "maximum_axis_cosine": maximum_axis_cosine,
        "static_scatter_p95_median_mm": median(scatter_values),
        "edge_rows": edge_rows,
        "measured_by_vertex": measured_by_vertex,
        "expected_by_vertex": expected_by_vertex,
    }


def expected_evaluation_passes(config: dict[str, Any]) -> list[dict[str, Any]]:
    passes: list[dict[str, Any]] = []
    for cycle_index in range(int(config["experiment"]["scan_cycles"])):
        for direction in ("forward", "reverse"):
            passes.append(
                {
                    "pass_name": f"cycle{cycle_index + 1:02d}_{direction}",
                    "pass_direction": direction,
                    "cycle_index": cycle_index,
                }
            )
    return passes


def build_pass_results(
    sample_rows: list[dict[str, Any]],
    pass_centres: list[dict[str, Any]],
    registration: dict[str, Any],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    grouped_samples: dict[tuple[str, float], list[dict[str, Any]]] = defaultdict(list)
    grouped_centres = {
        (str(row["pass_name"]), float(row["stage_global_mm"])): row
        for row in pass_centres
    }
    for row in sample_rows:
        if str(row["data_role"]) == "registration":
            continue
        grouped_samples[(str(row["pass_name"]), float(row["stage_global_mm"]))].append(row)

    acceptance = config["analysis"]["acceptance"]
    cube_config = config["experiment"].get("cube_positions_global_mm", "all")
    positions = sorted(float(value) for value in config["stage"]["positions_global_mm"])
    reference = float(config["stage"]["reference_global_mm"])
    results: list[dict[str, Any]] = []
    for pass_spec in expected_evaluation_passes(config):
        pass_name = str(pass_spec["pass_name"])
        for stage_global in positions:
            members = grouped_samples.get((pass_name, stage_global), [])
            centre = grouped_centres.get((pass_name, stage_global))
            if centre is None:
                centre = {
                    **pass_spec,
                    "stage_global_mm": stage_global,
                    "stage_delta_mm": stage_global - reference,
                    "stage_truth_delta_median_mm": float("nan"),
                    "center_expected_captures": (
                        2
                        if bool(config["experiment"]["center_before_and_after"])
                        else 0
                    ),
                    "center_usable_captures": 0,
                    "center_capture_fraction": 0.0,
                    "center_camera_mm": np.full(3, np.nan),
                    "center_pat_mm": np.full(3, np.nan),
                    "camera_z_mm": float("nan"),
                    "center_axial_error_mm": float("nan"),
                    "center_axial_bias_mm": float("nan"),
                    "center_axial_error_rms_mm": float("nan"),
                    "center_axial_error_p95_abs_mm": float("nan"),
                    "center_axial_error_max_abs_mm": float("nan"),
                    "center_command_axial_error_max_abs_mm": float("nan"),
                    "center_cross_axis_error_mm": float("nan"),
                    "center_cross_axis_error_max_mm": float("nan"),
                    "center_pre_post_axial_drift_mm": float("nan"),
                    "measured_axial_coordinate_mm": float("nan"),
                    "theory": {},
                }
            cube = aggregate_cube_station(members, registration, config)
            cube_required = cube_station_enabled(stage_global, cube_config)
            usable_scatter = [
                float(row["scatter_p95_mm"])
                for row in members
                if row["usable"] and math.isfinite(float(row["scatter_p95_mm"]))
            ]
            scatter_median = median(usable_scatter)
            failure_reasons: list[str] = []
            if int(centre["center_expected_captures"]) < 1:
                failure_reasons.append("no planned centre captures")
            if int(centre["center_usable_captures"]) < 1:
                failure_reasons.append("no usable centre capture")
            if float(centre["center_capture_fraction"]) < float(
                acceptance["minimum_center_capture_fraction_per_pass"]
            ):
                failure_reasons.append("centre capture fraction")
            center_max = float(centre["center_axial_error_max_abs_mm"])
            if (
                not math.isfinite(center_max)
                or center_max > float(acceptance["max_abs_center_axial_error_mm"])
            ):
                failure_reasons.append("centre axial error")
            if cube_required:
                if (
                    int(cube["valid_unique_vertices"]) != 8
                    or float(cube["valid_vertex_fraction"])
                    < float(acceptance["minimum_vertex_fraction"])
                ):
                    failure_reasons.append("eight cube vertices not complete")
                if int(cube["edge_count"]) != 12:
                    failure_reasons.append("twelve cube edges not complete")
                if (
                    not math.isfinite(float(cube["vertex_rmse_mm"]))
                    or float(cube["vertex_rmse_mm"])
                    > float(acceptance["max_cube_vertex_rmse_mm"])
                ):
                    failure_reasons.append("cube vertex RMSE")
                if (
                    not math.isfinite(float(cube["edge_error_max_abs_mm"]))
                    or float(cube["edge_error_max_abs_mm"])
                    > float(acceptance["max_abs_edge_error_mm"])
                ):
                    failure_reasons.append("cube edge error")
            if (
                not math.isfinite(scatter_median)
                or scatter_median
                > float(acceptance["max_median_static_scatter_p95_mm"])
            ):
                failure_reasons.append("static scatter")

            public_cube = {
                key: value
                for key, value in cube.items()
                if key
                not in {
                    "edge_rows",
                    "measured_by_vertex",
                    "expected_by_vertex",
                    "static_scatter_p95_median_mm",
                }
            }
            result = {
                **centre,
                **public_cube,
                "pass_name": pass_name,
                "pass_direction": str(pass_spec["pass_direction"]),
                "cycle_index": int(pass_spec["cycle_index"]),
                "stage_global_mm": stage_global,
                "stage_delta_mm": stage_global - reference,
                "cube_required": cube_required,
                "planned_capture_count": len(members),
                "usable_capture_count": sum(bool(row["usable"]) for row in members),
                "static_scatter_p95_median_mm": scatter_median,
                "success": not failure_reasons,
                "failure_reasons": failure_reasons,
                "_edge_rows": cube["edge_rows"],
                "_measured_by_vertex": cube["measured_by_vertex"],
                "_expected_by_vertex": cube["expected_by_vertex"],
            }
            results.append(result)
    return results


def build_station_rows(
    pass_results: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    grouped: dict[float, list[dict[str, Any]]] = defaultdict(list)
    for row in pass_results:
        grouped[float(row["stage_global_mm"])].append(row)
    acceptance = config["analysis"]["acceptance"]
    stations: list[dict[str, Any]] = []

    for stage_global in sorted(grouped):
        members = grouped[stage_global]
        successful = [row for row in members if bool(row["success"])]
        success_fraction = len(successful) / len(members) if members else 0.0
        failure_reasons: list[str] = []
        minimum_success = float(acceptance["minimum_successful_pass_fraction"])
        if success_fraction < minimum_success:
            failure_reasons.append(
                f"successful pass fraction {len(successful)}/{len(members)} "
                f"< {minimum_success:.3f}"
            )

        by_direction: dict[str, list[float]] = defaultdict(list)
        for item in members:
            coordinate = float(item["measured_axial_coordinate_mm"])
            if math.isfinite(coordinate):
                by_direction[str(item["pass_direction"])].append(coordinate)
        hysteresis = float("nan")
        if by_direction.get("forward") and by_direction.get("reverse"):
            hysteresis = median(by_direction["forward"]) - median(by_direction["reverse"])

        representative = max(
            members,
            key=lambda item: (
                float(item["vertex_rmse_mm"])
                if math.isfinite(float(item["vertex_rmse_mm"]))
                else float("-inf")
            ),
        )
        all_edge_rows = [
            edge for item in members for edge in item.get("_edge_rows", [])
        ]
        centre_signed = [
            float(item["center_axial_error_mm"])
            for item in members
            if math.isfinite(float(item["center_axial_error_mm"]))
        ]
        centre_max = [
            float(item["center_axial_error_max_abs_mm"])
            for item in members
            if math.isfinite(float(item["center_axial_error_max_abs_mm"]))
        ]
        centre_cross = [
            float(item["center_cross_axis_error_mm"])
            for item in members
            if math.isfinite(float(item["center_cross_axis_error_mm"]))
        ]
        camera_z = [
            float(item["camera_z_mm"])
            for item in members
            if math.isfinite(float(item["camera_z_mm"]))
        ]
        camera_slant_range = [
            float(item.get("camera_slant_range_mm", float("nan")))
            for item in members
            if math.isfinite(float(item.get("camera_slant_range_mm", float("nan"))))
        ]
        camera_baseline_range = [
            float(
                item.get(
                    "camera_baseline_perpendicular_range_mm",
                    float("nan"),
                )
            )
            for item in members
            if math.isfinite(
                float(
                    item.get(
                        "camera_baseline_perpendicular_range_mm",
                        float("nan"),
                    )
                )
            )
        ]
        pat_centres = [
            np.asarray(item.get("center_pat_mm", np.full(3, np.nan)))
            for item in members
            if np.all(
                np.isfinite(
                    np.asarray(
                        item.get("center_pat_mm", np.full(3, np.nan))
                    )
                )
            )
        ]
        pat_center_median = (
            np.median(np.vstack(pat_centres), axis=0)
            if pat_centres
            else np.full(3, np.nan)
        )
        hardware_commands = [
            float(item.get("stage_command_hardware_median_mm", float("nan")))
            for item in members
            if math.isfinite(
                float(item.get("stage_command_hardware_median_mm", float("nan")))
            )
        ]
        hardware_readbacks = [
            float(item.get("stage_readback_hardware_median_mm", float("nan")))
            for item in members
            if math.isfinite(
                float(item.get("stage_readback_hardware_median_mm", float("nan")))
            )
        ]
        truth_delta = [
            float(item["stage_truth_delta_median_mm"])
            for item in members
            if math.isfinite(float(item["stage_truth_delta_median_mm"]))
        ]
        theory_aggregate: dict[str, dict[str, Any]] = {}
        for pixel_sigma in config["analysis"].get("pixel_sigma_scenarios", [0.25]):
            key = str(float(pixel_sigma))
            axis_values = [
                float(item["theory"][key]["coverage_axis_mm"])
                for item in members
                if key in item.get("theory", {})
            ]
            theory_aggregate[key] = {
                "pixel_sigma_per_camera": float(pixel_sigma),
                "coverage_axis_mm": median(axis_values),
                "estimator_scope": "one triangulated tracking window",
            }
        pass_scatter = [
            float(item["static_scatter_p95_median_mm"])
            for item in members
            if math.isfinite(float(item["static_scatter_p95_median_mm"]))
        ]
        vertex_fractions = [float(item["valid_vertex_fraction"]) for item in members]
        vertex_rmse_values = [
            float(item["vertex_rmse_mm"])
            for item in members
            if math.isfinite(float(item["vertex_rmse_mm"]))
        ]
        vertex_p95_values = [
            float(item["vertex_p95_mm"])
            for item in members
            if math.isfinite(float(item["vertex_p95_mm"]))
        ]
        vertex_max_values = [
            float(item["vertex_max_mm"])
            for item in members
            if math.isfinite(float(item["vertex_max_mm"]))
        ]
        edge_rms_values = [
            float(item["edge_error_rms_mm"])
            for item in members
            if math.isfinite(float(item["edge_error_rms_mm"]))
        ]
        edge_max_values = [
            float(item["edge_error_max_abs_mm"])
            for item in members
            if math.isfinite(float(item["edge_error_max_abs_mm"]))
        ]
        failed_passes = [
            {
                "pass_name": str(item["pass_name"]),
                "failure_reasons": list(item["failure_reasons"]),
            }
            for item in members
            if not bool(item["success"])
        ]
        station = {
            "stage_global_mm": stage_global,
            "stage_delta_mm": float(members[0]["stage_delta_mm"]),
            "stage_truth_delta_median_mm": median(truth_delta),
            "stage_command_hardware_median_mm": median(hardware_commands),
            "stage_readback_hardware_median_mm": median(hardware_readbacks),
            "camera_z_median_mm": median(camera_z),
            "camera_slant_range_median_mm": median(camera_slant_range),
            "camera_baseline_perpendicular_range_median_mm": median(
                camera_baseline_range
            ),
            "center_pat_median_mm": pat_center_median,
            "planned_passes": len(members),
            "successful_passes": len(successful),
            "successful_pass_fraction": success_fraction,
            "failed_passes": failed_passes,
            "pass_centres": sum(
                int(item["center_usable_captures"]) > 0 for item in members
            ),
            "center_axial_bias_mm": median(centre_signed),
            "center_axial_error_rms_mm": rms(centre_signed),
            "center_axial_error_p95_abs_mm": percentile(np.abs(centre_signed), 95),
            "center_axial_error_max_abs_mm": (
                max(centre_max) if centre_max else float("nan")
            ),
            "center_command_axial_error_max_abs_mm": max(
                (
                    float(item["center_command_axial_error_max_abs_mm"])
                    for item in members
                    if math.isfinite(
                        float(item["center_command_axial_error_max_abs_mm"])
                    )
                ),
                default=float("nan"),
            ),
            "center_cross_axis_median_mm": median(centre_cross),
            "center_pre_post_axial_drift_max_abs_mm": max(
                (
                    abs(float(item["center_pre_post_axial_drift_mm"]))
                    for item in members
                    if math.isfinite(float(item["center_pre_post_axial_drift_mm"]))
                ),
                default=float("nan"),
            ),
            "forward_minus_reverse_axial_mm": hysteresis,
            "valid_unique_vertices": min(
                int(item["valid_unique_vertices"]) for item in members
            ),
            "valid_vertex_fraction": min(vertex_fractions),
            "vertex_rmse_mm": max(vertex_rmse_values, default=float("nan")),
            "vertex_p95_mm": max(vertex_p95_values, default=float("nan")),
            "vertex_max_mm": max(vertex_max_values, default=float("nan")),
            "edge_count": min(int(item["edge_count"]) for item in members),
            "edge_error_rms_mm": max(edge_rms_values, default=float("nan")),
            "edge_error_max_abs_mm": max(edge_max_values, default=float("nan")),
            "axis_edge_mean_error_mm": representative["axis_edge_mean_error_mm"],
            "local_rigid_shape_rms_mm": max(
                (
                    float(item["local_rigid_shape_rms_mm"])
                    for item in members
                    if math.isfinite(float(item["local_rigid_shape_rms_mm"]))
                ),
                default=float("nan"),
            ),
            "affine_matrix": representative["affine_matrix"],
            "affine_singular_values": representative["affine_singular_values"],
            "axis_scales": representative["axis_scales"],
            "affine_volume_scale": representative["affine_volume_scale"],
            "maximum_axis_cosine": representative["maximum_axis_cosine"],
            "static_scatter_p95_median_mm": (
                max(pass_scatter) if pass_scatter else float("nan")
            ),
            "theory": theory_aggregate,
            "pass": not failure_reasons,
            "failure_reasons": failure_reasons,
            "_edge_rows": all_edge_rows,
            "_measured_by_vertex": representative["_measured_by_vertex"],
            "_expected_by_vertex": representative["_expected_by_vertex"],
            "_pass_results": members,
        }
        stations.append(station)
    return stations


def accepted_sampled_span(
    station_rows: list[dict[str, Any]],
    reference_global_mm: float,
) -> dict[str, Any]:
    by_position = {float(row["stage_global_mm"]): row for row in station_rows}
    if reference_global_mm not in by_position:
        return {"available": False, "reason": "reference station missing"}
    if not bool(by_position[reference_global_mm]["pass"]):
        return {"available": False, "reason": "reference station failed acceptance"}

    positions = sorted(by_position)
    negative = [value for value in reversed(positions) if value < reference_global_mm]
    positive = [value for value in positions if value > reference_global_mm]
    accepted = [reference_global_mm]
    for side in (negative, positive):
        for value in side:
            if not bool(by_position[value]["pass"]):
                break
            accepted.append(value)
    accepted = sorted(accepted)
    camera_z = [
        float(by_position[value]["camera_z_median_mm"])
        for value in accepted
        if math.isfinite(float(by_position[value]["camera_z_median_mm"]))
    ]
    camera_slant_range = [
        float(by_position[value].get("camera_slant_range_median_mm", float("nan")))
        for value in accepted
        if math.isfinite(
            float(by_position[value].get("camera_slant_range_median_mm", float("nan")))
        )
    ]
    camera_baseline_range = [
        float(
            by_position[value].get(
                "camera_baseline_perpendicular_range_median_mm",
                float("nan"),
            )
        )
        for value in accepted
        if math.isfinite(
            float(
                by_position[value].get(
                    "camera_baseline_perpendicular_range_median_mm",
                    float("nan"),
                )
            )
        )
    ]
    hardware_readback = [
        float(
            by_position[value].get(
                "stage_readback_hardware_median_mm",
                float("nan"),
            )
        )
        for value in accepted
        if math.isfinite(
            float(
                by_position[value].get(
                    "stage_readback_hardware_median_mm",
                    float("nan"),
                )
            )
        )
    ]
    return {
        "available": True,
        "minimum_stage_global_mm": min(accepted),
        "maximum_stage_global_mm": max(accepted),
        "minimum_stage_delta_mm": min(accepted) - reference_global_mm,
        "maximum_stage_delta_mm": max(accepted) - reference_global_mm,
        "accepted_sampled_stations": accepted,
        "sampled_station_spacing_mm": sorted(
            {
                round(right - left, 9)
                for left, right in zip(positions, positions[1:])
            }
        ),
        "minimum_camera_z_mm": min(camera_z) if camera_z else float("nan"),
        "maximum_camera_z_mm": max(camera_z) if camera_z else float("nan"),
        "minimum_camera_slant_range_mm": (
            min(camera_slant_range) if camera_slant_range else float("nan")
        ),
        "maximum_camera_slant_range_mm": (
            max(camera_slant_range) if camera_slant_range else float("nan")
        ),
        "minimum_camera_baseline_perpendicular_range_mm": (
            min(camera_baseline_range) if camera_baseline_range else float("nan")
        ),
        "maximum_camera_baseline_perpendicular_range_mm": (
            max(camera_baseline_range) if camera_baseline_range else float("nan")
        ),
        "minimum_stage_hardware_readback_mm": (
            min(hardware_readback) if hardware_readback else float("nan")
        ),
        "maximum_stage_hardware_readback_mm": (
            max(hardware_readback) if hardware_readback else float("nan")
        ),
        "interpretation": (
            "This is a reference-registered relative-accuracy span. Camera Z and "
            "slant range are observed stereo estimates, not independent absolute "
            "distance truth. Bounds apply only to the listed sampled stations; "
            "accuracy between stations was not measured and is not implied."
        ),
    }


def distance_interpretation(
    registration: dict[str, Any],
    stations: list[dict[str, Any]],
    config: dict[str, Any],
    calibration: dict[str, np.ndarray],
) -> dict[str, Any]:
    """Separate observed metric coordinates from an independent absolute truth claim."""
    evaluation_frame = str(
        registration.get("evaluation_frame", "camera_relative")
    )
    if evaluation_frame == "pat":
        reference_point = np.asarray(
            registration[
                "external_registered_reference_target_left_camera_mm"
            ],
            dtype=np.float64,
        )
        reference_point_role = (
            "left-camera coordinate of the configured PAT target centre, "
            "obtained from the external camera-to-PAT registration"
        )
    else:
        reference_point = np.asarray(
            registration["self_registered_reference_target_left_camera_mm"],
            dtype=np.float64,
        )
        reference_point_role = (
            "self-estimated left-camera coordinate of the configured PAT target "
            "centre; observable/registration nuisance parameter, not ground truth"
        )
    reference_baseline = point_to_stereo_baseline_line(
        reference_point,
        calibration,
    )
    finite_z = [
        float(item["camera_z_median_mm"])
        for item in stations
        if math.isfinite(float(item["camera_z_median_mm"]))
    ]
    finite_slant = [
        float(item.get("camera_slant_range_median_mm", float("nan")))
        for item in stations
        if math.isfinite(
            float(item.get("camera_slant_range_median_mm", float("nan")))
        )
    ]
    finite_baseline_range = [
        float(
            item.get(
                "camera_baseline_perpendicular_range_median_mm",
                float("nan"),
            )
        )
        for item in stations
        if math.isfinite(
            float(
                item.get(
                    "camera_baseline_perpendicular_range_median_mm",
                    float("nan"),
                )
            )
        )
    ]
    reporting = config.get("distance_reporting", {})
    independent = reporting.get("independent_absolute_reference", {})
    return {
        "absolute_accuracy_available": False,
        "absolute_accuracy_reason": (
            "No independent camera-to-PAT distance truth is configured. The reference "
            "camera-to-PAT registration is also derived from stereo measurements, so "
            "a constant depth bias can be absorbed by its translation."
        ),
        "valid_accuracy_claim": (
            "Ossila-readback-referenced relative displacement, linearity, repeatability, "
            "and cuboid shape at the sampled stations."
        ),
        "reference_point_role": reference_point_role,
        "reference_target_left_camera_mm": reference_point,
        "reference_camera_z_estimate_mm": float(reference_point[2]),
        "reference_optical_center_slant_range_estimate_mm": float(
            np.linalg.norm(reference_point)
        ),
        "reference_pat_origin_to_stereo_baseline_estimate_mm": float(
            reference_baseline["perpendicular_distance_mm"]
        ),
        "reference_baseline_foot_left_camera_mm": np.asarray(
            reference_baseline["foot_left_camera_mm"],
            dtype=np.float64,
        ),
        "reference_baseline_foot_from_left_optical_center_mm": float(
            reference_baseline["foot_from_left_optical_center_mm"]
        ),
        "reference_baseline_foot_fraction": float(
            reference_baseline["foot_fraction_of_left_to_right_baseline"]
        ),
        "reference_baseline_foot_between_optical_centers": bool(
            reference_baseline["foot_is_between_optical_centers"]
        ),
        "stereo_baseline_length_mm": float(
            reference_baseline["baseline_length_mm"]
        ),
        "observed_camera_z_estimate_min_mm": min(finite_z) if finite_z else float("nan"),
        "observed_camera_z_estimate_max_mm": max(finite_z) if finite_z else float("nan"),
        "observed_optical_center_slant_range_estimate_min_mm": (
            min(finite_slant) if finite_slant else float("nan")
        ),
        "observed_optical_center_slant_range_estimate_max_mm": (
            max(finite_slant) if finite_slant else float("nan")
        ),
        "observed_pat_origin_to_stereo_baseline_estimate_min_mm": (
            min(finite_baseline_range) if finite_baseline_range else float("nan")
        ),
        "observed_pat_origin_to_stereo_baseline_estimate_max_mm": (
            max(finite_baseline_range) if finite_baseline_range else float("nan")
        ),
        "intended_reference_stage_hardware_mm": float(
            reporting.get(
                "intended_reference_hardware_mm",
                registration.get("reference_stage_readback_hardware_mm", float("nan")),
            )
        ),
        "intended_sweep_stage_hardware_mm": [
            float(value)
            for value in reporting.get("intended_sweep_hardware_mm", [])
        ],
        "independent_reference_configured": bool(independent.get("available", False)),
        "distance_definitions": {
            "camera_z": "signed left OpenCV optical-axis coordinate Z",
            "optical_center_slant_range": "sqrt(X^2 + Y^2 + Z^2) from the left optical centre",
            "stereo_baseline_perpendicular_range": (
                "shortest distance from the PAT target point to the calibrated "
                "infinite line through the left and right optical centres"
            ),
            "relative_stage_axis": (
                "projection of PAT-frame residual along the configured physical "
                "stage axis; stage readback composes the moving camera translation"
                if evaluation_frame == "pat"
                else "projection after the self-registration, with zero at the reference station"
            ),
        },
    }


def stage_line_diagnostic(pass_centres: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [
        item
        for item in pass_centres
        if math.isfinite(float(item["stage_truth_delta_median_mm"]))
        and np.all(np.isfinite(np.asarray(item["center_camera_mm"])))
    ]
    if len(usable) < 2:
        return {"available": False}
    stage = np.asarray(
        [float(item["stage_truth_delta_median_mm"]) for item in usable],
        dtype=np.float64,
    )
    if np.unique(np.round(stage, decimals=9)).size < 2:
        return {"available": False}
    points = np.vstack([np.asarray(item["center_camera_mm"]) for item in usable])
    result = fit_line_against_stage(stage, points)
    result["available"] = True
    result["truth_source"] = "Ossila position readback relative to registration-pass median"
    result["stage_truth_delta_mm"] = stage
    result["pass_centres_camera_mm"] = points
    evaluation_points = np.vstack(
        [
            np.asarray(
                item.get("center_evaluation_mm", item["center_camera_mm"])
            )
            for item in usable
        ]
    )
    result["evaluation_frame"] = str(
        usable[0].get("evaluation_frame", "camera_relative")
    )
    result["pass_centres_evaluation_mm"] = evaluation_points
    result["evaluation_coordinate_diagnostic"] = fit_line_against_stage(
        stage,
        evaluation_points,
    )
    if result["evaluation_frame"] == "pat":
        result["evaluation_coordinate_interpretation"] = (
            "After composing the camera motion, a stationary PAT target should "
            "have zero slope. The raw camera-coordinate fit above retains the "
            "observed apparent stage motion."
        )
    command_stage = np.asarray(
        [float(item["stage_delta_mm"]) for item in usable],
        dtype=np.float64,
    )
    if np.unique(np.round(command_stage, decimals=9)).size >= 2:
        result["command_coordinate_diagnostic"] = fit_line_against_stage(
            command_stage,
            points,
        )
    return result


def flatten_sample_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for row in rows:
        center_camera = np.asarray(
            row.get("center_camera_mm", row.get("center_mm", np.full(3, np.nan)))
        )
        center_pat = np.asarray(row.get("center_pat_mm", np.full(3, np.nan)))
        expected_pat = np.asarray(
            row.get("pat_target_mm", np.full(3, np.nan))
        )
        residual_evaluation = np.asarray(
            row.get(
                "residual_evaluation_mm",
                row.get("residual_camera_mm", np.full(3, np.nan)),
            )
        )
        residual_pat = np.asarray(
            row.get("residual_pat_mm", np.full(3, np.nan))
        )
        flattened.append(
            {
                "sample_id": row["sample_id"],
                "pass_name": row["pass_name"],
                "pass_direction": row["pass_direction"],
                "cycle_index": row["cycle_index"],
                "data_role": row["data_role"],
                "station_index": row["station_index"],
                "stage_global_mm": row["stage_global_mm"],
                "stage_delta_mm": row["stage_delta_mm"],
                "stage_truth_delta_mm": row.get("stage_truth_delta_mm", float("nan")),
                "kind": row["kind"],
                "vertex_id": row["vertex_id"],
                "usable": row["usable"],
                "reason": row["reason"],
                "evaluation_frame": (
                    "pat" if np.all(np.isfinite(center_pat)) else "camera_relative"
                ),
                "camera_x_mm": center_camera[0],
                "camera_y_mm": center_camera[1],
                "camera_z_mm": center_camera[2],
                "camera_slant_range_mm": float(np.linalg.norm(center_camera)),
                "pat_x_mm": center_pat[0],
                "pat_y_mm": center_pat[1],
                "pat_z_mm": center_pat[2],
                "expected_pat_x_mm": expected_pat[0],
                "expected_pat_y_mm": expected_pat[1],
                "expected_pat_z_mm": expected_pat[2],
                "camera_baseline_perpendicular_range_mm": row.get(
                    "camera_baseline_perpendicular_range_mm",
                    float("nan"),
                ),
                "axial_error_mm": row.get("axial_error_mm", float("nan")),
                "command_axial_error_mm": row.get(
                    "command_axial_error_mm",
                    float("nan"),
                ),
                "cross_axis_error_mm": row.get("cross_axis_error_mm", float("nan")),
                "residual_3d_mm": row.get("residual_3d_mm", float("nan")),
                "residual_evaluation_x_mm": residual_evaluation[0],
                "residual_evaluation_y_mm": residual_evaluation[1],
                "residual_evaluation_z_mm": residual_evaluation[2],
                "residual_pat_x_mm": residual_pat[0],
                "residual_pat_y_mm": residual_pat[1],
                "residual_pat_z_mm": residual_pat[2],
                "robust_samples": row.get("robust_samples", 0),
                "raw_valid_samples": row.get("raw_valid_samples", 0),
                "raw_valid_fraction": row.get("raw_valid_fraction", 0.0),
                "scatter_rms_mm": row.get("scatter_rms_mm", float("nan")),
                "scatter_p95_mm": row.get("scatter_p95_mm", float("nan")),
                "stage_readback_global_mm": row["stage_readback_global_mm"],
                "stage_command_hardware_mm": row.get(
                    "stage_command_hardware_mm",
                    float("nan"),
                ),
                "stage_readback_hardware_mm": row.get(
                    "stage_readback_hardware_mm",
                    float("nan"),
                ),
                "stage_readback_error_mm": row["stage_readback_error_mm"],
                "stereo_3d_npz": row["stereo_3d_npz"],
            }
        )
    return flattened


def flatten_station_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for row in rows:
        axis_scales = np.asarray(row.get("axis_scales", np.full(3, np.nan)))
        axis_edges = row.get("axis_edge_mean_error_mm", {})
        center_pat = np.asarray(
            row.get("center_pat_median_mm", np.full(3, np.nan))
        )
        flattened.append(
            {
                "stage_global_mm": row["stage_global_mm"],
                "stage_delta_mm": row["stage_delta_mm"],
                "stage_truth_delta_median_mm": row[
                    "stage_truth_delta_median_mm"
                ],
                "stage_command_hardware_median_mm": row.get(
                    "stage_command_hardware_median_mm",
                    float("nan"),
                ),
                "stage_readback_hardware_median_mm": row.get(
                    "stage_readback_hardware_median_mm",
                    float("nan"),
                ),
                "camera_z_median_mm": row["camera_z_median_mm"],
                "camera_slant_range_median_mm": row.get(
                    "camera_slant_range_median_mm",
                    float("nan"),
                ),
                "camera_baseline_perpendicular_range_median_mm": row.get(
                    "camera_baseline_perpendicular_range_median_mm",
                    float("nan"),
                ),
                "center_pat_x_median_mm": center_pat[0],
                "center_pat_y_median_mm": center_pat[1],
                "center_pat_z_median_mm": center_pat[2],
                "pass": row["pass"],
                "failure_reasons": "; ".join(row["failure_reasons"]),
                "planned_passes": row["planned_passes"],
                "successful_passes": row["successful_passes"],
                "successful_pass_fraction": row["successful_pass_fraction"],
                "pass_centres": row["pass_centres"],
                "center_axial_bias_mm": row["center_axial_bias_mm"],
                "center_axial_error_rms_mm": row["center_axial_error_rms_mm"],
                "center_axial_error_p95_abs_mm": row["center_axial_error_p95_abs_mm"],
                "center_axial_error_max_abs_mm": row["center_axial_error_max_abs_mm"],
                "center_command_axial_error_max_abs_mm": row[
                    "center_command_axial_error_max_abs_mm"
                ],
                "center_cross_axis_median_mm": row["center_cross_axis_median_mm"],
                "center_pre_post_axial_drift_max_abs_mm": row[
                    "center_pre_post_axial_drift_max_abs_mm"
                ],
                "forward_minus_reverse_axial_mm": row["forward_minus_reverse_axial_mm"],
                "valid_unique_vertices": row["valid_unique_vertices"],
                "valid_vertex_fraction": row["valid_vertex_fraction"],
                "vertex_rmse_mm": row["vertex_rmse_mm"],
                "vertex_p95_mm": row["vertex_p95_mm"],
                "edge_count": row["edge_count"],
                "edge_error_rms_mm": row["edge_error_rms_mm"],
                "edge_error_max_abs_mm": row["edge_error_max_abs_mm"],
                "edge_x_mean_error_mm": axis_edges.get("x", float("nan")),
                "edge_y_mean_error_mm": axis_edges.get("y", float("nan")),
                "edge_z_mean_error_mm": axis_edges.get("z", float("nan")),
                "local_rigid_shape_rms_mm": row["local_rigid_shape_rms_mm"],
                "affine_scale_x": axis_scales[0],
                "affine_scale_y": axis_scales[1],
                "affine_scale_z": axis_scales[2],
                "affine_volume_scale": row["affine_volume_scale"],
                "maximum_axis_cosine": row["maximum_axis_cosine"],
                "static_scatter_p95_median_mm": row["static_scatter_p95_median_mm"],
            }
        )
    return flattened


def flatten_pass_results(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for row in rows:
        center_pat = np.asarray(
            row.get("center_pat_mm", np.full(3, np.nan))
        )
        flattened.append(
            {
                "pass_name": row["pass_name"],
                "pass_direction": row["pass_direction"],
                "cycle_index": row["cycle_index"],
                "stage_global_mm": row["stage_global_mm"],
                "stage_delta_mm": row["stage_delta_mm"],
                "stage_truth_delta_median_mm": row[
                    "stage_truth_delta_median_mm"
                ],
                "stage_command_hardware_median_mm": row.get(
                    "stage_command_hardware_median_mm",
                    float("nan"),
                ),
                "stage_readback_hardware_median_mm": row.get(
                    "stage_readback_hardware_median_mm",
                    float("nan"),
                ),
                "camera_z_mm": row.get("camera_z_mm", float("nan")),
                "camera_slant_range_mm": row.get(
                    "camera_slant_range_mm",
                    float("nan"),
                ),
                "camera_baseline_perpendicular_range_mm": row.get(
                    "camera_baseline_perpendicular_range_mm",
                    float("nan"),
                ),
                "center_pat_x_mm": center_pat[0],
                "center_pat_y_mm": center_pat[1],
                "center_pat_z_mm": center_pat[2],
                "success": row["success"],
                "failure_reasons": "; ".join(row["failure_reasons"]),
                "center_expected_captures": row["center_expected_captures"],
                "center_usable_captures": row["center_usable_captures"],
                "center_capture_fraction": row["center_capture_fraction"],
                "center_axial_error_max_abs_mm": row[
                    "center_axial_error_max_abs_mm"
                ],
                "center_command_axial_error_max_abs_mm": row[
                    "center_command_axial_error_max_abs_mm"
                ],
                "center_pre_post_axial_drift_mm": row[
                    "center_pre_post_axial_drift_mm"
                ],
                "valid_unique_vertices": row["valid_unique_vertices"],
                "valid_vertex_fraction": row["valid_vertex_fraction"],
                "vertex_rmse_mm": row["vertex_rmse_mm"],
                "edge_count": row["edge_count"],
                "edge_error_max_abs_mm": row["edge_error_max_abs_mm"],
                "static_scatter_p95_median_mm": row[
                    "static_scatter_p95_median_mm"
                ],
            }
        )
    return flattened


def flatten_edge_rows(stations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for station in stations:
        for edge in station["_edge_rows"]:
            result.append(
                {
                    "stage_global_mm": station["stage_global_mm"],
                    "camera_z_median_mm": station["camera_z_median_mm"],
                    **edge,
                }
            )
    return result


def make_summary_plot(
    path: Path,
    pass_centres: list[dict[str, Any]],
    stations: list[dict[str, Any]],
    config: dict[str, Any],
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(3, 1, figsize=(10, 12), constrained_layout=True)
    colours = {"forward": "tab:blue", "reverse": "tab:orange"}
    for direction in ("forward", "reverse"):
        members = [item for item in pass_centres if item["pass_direction"] == direction]
        if not members:
            continue
        axes[0].scatter(
            [item["stage_truth_delta_median_mm"] for item in members],
            [item["measured_axial_coordinate_mm"] for item in members],
            label=direction,
            color=colours[direction],
        )
    all_deltas = [
        float(item["stage_truth_delta_median_mm"])
        for item in pass_centres
        if math.isfinite(float(item["stage_truth_delta_median_mm"]))
    ]
    if all_deltas:
        low, high = min(all_deltas), max(all_deltas)
        evaluation_frame = str(
            config["analysis"].get("evaluation_frame", "camera_relative")
        ).strip().lower()
        _, _, apparent_stage_vector = experiment_motion_vectors(config)
        stage_vector_norm = float(np.linalg.norm(apparent_stage_vector))
        expected_low = 0.0 if evaluation_frame == "pat" else low * stage_vector_norm
        expected_high = 0.0 if evaluation_frame == "pat" else high * stage_vector_norm
        axes[0].plot(
            [low, high],
            [expected_low, expected_high],
            "k--",
            label="expected",
        )
    axes[0].set_xlabel("Stage readback delta from registration reference [mm]")
    axes[0].set_ylabel(
        "Measured PAT stage-axis offset [mm]"
        if str(config["analysis"].get("evaluation_frame", "")).lower() == "pat"
        else "Measured coordinate along registered stage axis [mm]"
    )
    axes[0].grid(True, alpha=0.3)
    axes[0].legend()

    for direction in ("forward", "reverse"):
        members = [item for item in pass_centres if item["pass_direction"] == direction]
        if members:
            axes[1].scatter(
                [item["camera_z_mm"] for item in members],
                [item["center_axial_error_mm"] for item in members],
                label=direction,
                color=colours[direction],
            )
    threshold = float(config["analysis"]["acceptance"]["max_abs_center_axial_error_mm"])
    axes[1].axhline(+threshold, color="tab:red", linestyle="--", label="acceptance")
    axes[1].axhline(-threshold, color="tab:red", linestyle="--")
    axes[1].axhline(0.0, color="black", linewidth=0.8)
    axes[1].set_xlabel("Observed stereo-estimated left-camera Z [mm]")
    axes[1].set_ylabel(
        "PAT stage-axis residual [mm]"
        if str(config["analysis"].get("evaluation_frame", "")).lower() == "pat"
        else "Registered stage-axis error [mm]"
    )
    axes[1].grid(True, alpha=0.3)
    axes[1].legend()

    x = [
        item.get(
            "camera_baseline_perpendicular_range_median_mm",
            float("nan"),
        )
        for item in stations
    ]
    axes[2].plot(x, [item["vertex_rmse_mm"] for item in stations], "o-", label="vertex RMSE")
    axes[2].plot(
        x,
        [item["edge_error_max_abs_mm"] for item in stations],
        "s-",
        label="max |edge error|",
    )
    axes[2].plot(
        x,
        [item["static_scatter_p95_median_mm"] for item in stations],
        "^-",
        label="worst pass-median static scatter P95",
    )
    axes[2].set_xlabel("Observed PAT-to-stereo-baseline perpendicular range [mm]")
    axes[2].set_ylabel("Error [mm]")
    axes[2].grid(True, alpha=0.3)
    axes[2].legend()
    figure.suptitle("Stereo stage/cuboid accuracy")
    figure.savefig(path, dpi=180)
    plt.close(figure)


def make_cube_plot(
    path: Path,
    stations: list[dict[str, Any]],
    registration: dict[str, Any],
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    available = [item for item in stations if len(item["_measured_by_vertex"]) >= 4]
    if not available:
        return
    picks = [available[0], available[len(available) // 2], available[-1]]
    unique: list[dict[str, Any]] = []
    for item in picks:
        if item not in unique:
            unique.append(item)
    rotation = np.asarray(registration["rotation_expected_to_evaluation"])
    translation = np.asarray(registration["translation_evaluation_mm"])
    figure = plt.figure(figsize=(6 * len(unique), 6))
    edge_pairs = cube_edge_pairs()
    for index, station in enumerate(unique, start=1):
        axis = figure.add_subplot(1, len(unique), index, projection="3d")
        measured_expected_frame = {
            label: rotation.T @ (point - translation)
            for label, point in station["_measured_by_vertex"].items()
        }
        truth = station["_expected_by_vertex"]
        for left, right, _ in edge_pairs:
            if left in truth and right in truth:
                pair = np.vstack([truth[left], truth[right]])
                axis.plot(pair[:, 0], pair[:, 1], pair[:, 2], "k--", alpha=0.5)
            if left in measured_expected_frame and right in measured_expected_frame:
                pair = np.vstack([measured_expected_frame[left], measured_expected_frame[right]])
                axis.plot(pair[:, 0], pair[:, 1], pair[:, 2], color="tab:blue")
        if measured_expected_frame:
            points = np.vstack(list(measured_expected_frame.values()))
            axis.scatter(points[:, 0], points[:, 1], points[:, 2], color="tab:blue", label="measured")
        axis.set_title(
            f"stage {station['stage_global_mm']:+g} mm\n"
            f"camera Z {station['camera_z_median_mm']:.1f} mm"
        )
        axis.set_xlabel("PAT X [mm]")
        axis.set_ylabel("PAT Y [mm]")
        axis.set_zlabel("PAT Z [mm]")
        axis.set_box_aspect((1, 1, 1))
    figure.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(figure)


def theory_summary(
    config: dict[str, Any],
    calibration: dict[str, np.ndarray],
) -> dict[str, Any]:
    focal = calibration_focal_px(calibration)
    baseline = calibration_baseline_mm(calibration)
    tolerance = float(config["analysis"]["acceptance"]["max_abs_center_axial_error_mm"])
    coverage = float(config["analysis"].get("theory_coverage_sigma", 1.96))
    scenarios = []
    for pixel_sigma in config["analysis"].get("pixel_sigma_scenarios", [0.25]):
        scenarios.append(
            {
                "pixel_sigma_per_camera": float(pixel_sigma),
                "disparity_sigma_px": math.sqrt(2.0) * float(pixel_sigma),
                "coverage_sigma": coverage,
                "simple_parallel_limit_mm": simple_depth_limit_mm(
                    tolerance,
                    focal_px=focal,
                    baseline_mm=baseline,
                    per_camera_pixel_sigma=float(pixel_sigma),
                    coverage_sigma=coverage,
                ),
            }
        )
    return {
        "raw_intrinsic_mean_fx_px": focal,
        "optical_centre_baseline_mm": baseline,
        "focal_times_baseline_px_mm": focal * baseline,
        "stereo_rms_px": (
            float(np.asarray(calibration["stereo_rms"]).item())
            if "stereo_rms" in calibration
            else None
        ),
        "acceptance_tolerance_mm": tolerance,
        "note": (
            "The simple limit uses Z=fB/d and random pixel noise only. "
            "Use the per-station numerical Jacobian for this convergent camera pair. "
            "Both predictions describe one triangulated tracking window; measured "
            "capture centres are robust medians of many correlated windows, so their "
            "uncertainties must not be compared as identical estimators."
        ),
        "theory_estimator_scope": "one triangulated tracking window",
        "measured_estimator_scope": "one static capture robust median",
        "scenarios": scenarios,
    }


def run_theory_only(
    args: argparse.Namespace,
    config: dict[str, Any],
    calibration_path: Path,
) -> int:
    if args.z_step_mm <= 0 or args.z_max_mm < args.z_min_mm:
        raise SystemExit("Theory range requires z-step > 0 and z-max >= z-min.")
    calibration = load_stereo_calibration(calibration_path)
    summary = theory_summary(config, calibration)
    focal = float(summary["raw_intrinsic_mean_fx_px"])
    baseline = float(summary["optical_centre_baseline_mm"])
    z_min_mm = float(args.z_min_mm)
    z_max_mm = float(args.z_max_mm)
    z_step_mm = float(args.z_step_mm)
    # Keep a grid-aligned endpoint, but never emit a station above z_max when
    # the requested interval is not an integer number of steps.
    z_values = np.arange(
        z_min_mm,
        z_max_mm + z_step_mm * 1e-9,
        z_step_mm,
    )
    z_values = z_values[z_values <= z_max_mm + z_step_mm * 1e-9]
    rows: list[dict[str, Any]] = []
    scenarios = [float(value) for value in config["analysis"]["pixel_sigma_scenarios"]]
    for z_mm in z_values:
        row: dict[str, Any] = {
            "z_mm": float(z_mm),
            "sensitivity_mm_per_disparity_px": float(z_mm * z_mm / (focal * baseline)),
        }
        for sigma in scenarios:
            row[f"sigma_z_px_{sigma:g}_mm"] = simple_depth_sigma_mm(
                float(z_mm),
                focal_px=focal,
                baseline_mm=baseline,
                per_camera_pixel_sigma=sigma,
            )
        rows.append(row)
    output_dir = args.output_dir.resolve() if args.output_dir else Path.cwd()
    output_dir.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0]) if rows else ["z_mm"]
    csv_path = output_dir / "stereo_depth_theory.csv"
    json_path = output_dir / "stereo_depth_theory_summary.json"
    write_csv(csv_path, rows, fieldnames)
    atomic_write_json(json_path, summary)
    print(
        f"Raw fx(mean)={focal:.3f} px, baseline={baseline:.3f} mm, "
        f"fB={focal * baseline:.1f} px mm"
    )
    print("Simple 95%-style limits for the configured absolute-error tolerance:")
    for scenario in summary["scenarios"]:
        print(
            f"  per-camera sigma={scenario['pixel_sigma_per_camera']:.6g} px -> "
            f"Z~{scenario['simple_parallel_limit_mm']:.1f} mm"
        )
    print(f"Table: {csv_path}")
    print(
        "Caution: this is the parallel-stereo random-noise approximation. "
        "The session analysis uses the calibrated convergent geometry and a numerical Jacobian."
    )
    return 0


def _write_markdown_report_legacy(
    path: Path,
    *,
    summary: dict[str, Any],
    stations: list[dict[str, Any]],
    config: dict[str, Any],
) -> None:
    registration = summary["registration"]
    accurate = summary["maximum_accurate_range"]
    line = summary["stage_line_diagnostic"]
    theory = summary["theory"]
    lines = [
        "# ステレオ・リニアステージ精度評価",
        "",
        "## 結論",
        "",
    ]
    if accurate.get("available"):
        lines.extend(
            [
                (
                    f"設定した全判定条件を連続して満たしたステージ範囲は "
                    f"`{accurate['minimum_stage_global_mm']:.3f} .. "
                    f"{accurate['maximum_stage_global_mm']:.3f} mm` です。"
                ),
                (
                    f"その区間で実測された左カメラ Z は "
                    f"`{accurate['minimum_camera_z_mm']:.3f} .. "
                    f"{accurate['maximum_camera_z_mm']:.3f} mm` です。"
                ),
            ]
        )
    else:
        lines.append(f"連続合格範囲を確定できませんでした: {accurate.get('reason', 'unknown')}")
    lines.extend(
        [
            "",
            "これは設定した閾値に対する結果です。カメラ光学中心からの絶対距離真値を"
            "別の測長器で与えていない場合、厳密には「ステージ相対変位追従精度」です。",
            "",
            "## 座標と登録",
            "",
            (
                "左 OpenCV カメラ座標は X=画像右、Y=画像下、Z=前方です。"
                "PAT Y と camera Z は同一視せず、基準距離の8頂点から求めた"
                "剛体回転でステージ軸をカメラ座標へ写しています。"
            ),
            (
                "登録後のステージ軸（左カメラ座標）: `"
                + ", ".join(
                    f"{value:+.6f}" for value in registration["stage_axis_left_camera"]
                )
                + "`"
            ),
            (
                f"基準立方体の rigid-fit RMS: `{registration['reference_fit_rms_mm']:.6f} mm`、"
                f"scale診断: `{registration['similarity_scale_diagnostic']:.8f}`"
            ),
        ]
    )
    if line.get("available"):
        lines.extend(
            [
                "",
                "## ステージ中心走査の診断",
                "",
                (
                    f"全距離の中心点から求めた実測軸スケールは "
                    f"`{line['scale_mm_per_mm']:.8f} mm/mm`、"
                    f"直線fit残差RMSは `{line['residual_rms_mm']:.6f} mm` です。"
                ),
                (
                    "この直線fitは診断値であり、距離別の合否判定には使っていません"
                    "（評価データ自身で誤差を消さないためです）。"
                ),
            ]
        )
    acceptance = config["analysis"]["acceptance"]
    lines.extend(
        [
            "",
            "## 判定条件",
            "",
            f"- 中心軸方向の最大絶対誤差 ≤ `{acceptance['max_abs_center_axial_error_mm']} mm`",
            f"- 8頂点RMSE ≤ `{acceptance['max_cube_vertex_rmse_mm']} mm`",
            f"- 12辺の最大絶対誤差 ≤ `{acceptance['max_abs_edge_error_mm']} mm`",
            f"- 静止点scatter P95中央値 ≤ `{acceptance['max_median_static_scatter_p95_mm']} mm`",
            f"- 有効頂点率 ≥ `{acceptance['minimum_vertex_fraction']}`",
            "",
            "## 距離別結果",
            "",
            "| stage [mm] | camera Z [mm] | axial max [mm] | vertex RMS [mm] | edge max [mm] | 判定 |",
            "|---:|---:|---:|---:|---:|:---:|",
        ]
    )
    for station in stations:
        lines.append(
            f"| {station['stage_global_mm']:.3f} | "
            f"{station['camera_z_median_mm']:.3f} | "
            f"{station['center_axial_error_max_abs_mm']:.4f} | "
            f"{station['vertex_rmse_mm']:.4f} | "
            f"{station['edge_error_max_abs_mm']:.4f} | "
            f"{'PASS' if station['pass'] else 'FAIL'} |"
        )
    lines.extend(
        [
            "",
            "## 理論値",
            "",
            (
                f"簡易式には生intrinsicの平均 `fx={theory['raw_intrinsic_mean_fx_px']:.3f} px` と "
                f"光学中心基線 `B={theory['optical_centre_baseline_mm']:.3f} mm` を使用しています。"
            ),
            "現在のカメラは収束配置なので、距離別CSV/JSONの理論値は実際の"
            "undistort＋triangulationを数値微分した値です。理論値は画素ランダム誤差のみで、"
            "校正バイアス、PAT平衡位置、ステージ取付誤差を含みません。",
            "",
            "## 出力",
            "",
            "- `sample_measurements.csv`: 1 captureごとの静止点・残差",
            "- `station_accuracy.csv`: 距離ごとの中心・立方体・合否",
            "- `cube_edges.csv`: 12辺ごとの長さ誤差",
            "- `stereo_stage_accuracy_summary.json`: 全数値と登録行列",
            "- `stereo_stage_accuracy_summary.png`: 距離依存グラフ",
            "- `cube_shape_comparison.png`: 近・中・遠の立方体比較",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_markdown_report_pre_absolute_distance_fix(
    path: Path,
    *,
    summary: dict[str, Any],
    stations: list[dict[str, Any]],
    config: dict[str, Any],
) -> None:
    registration = summary["registration"]
    sampled_span = summary["maximum_accurate_range"]
    line = summary["stage_line_diagnostic"]
    theory = summary["theory"]
    acceptance = config["analysis"]["acceptance"]
    lines = [
        "# ステレオ・リニアステージ精度評価",
        "",
        "## 結果",
        "",
    ]
    if sampled_span.get("available"):
        lines.extend(
            [
                (
                    "合格した隣接測定点の範囲は "
                    f"`{sampled_span['minimum_stage_global_mm']:.3f} .. "
                    f"{sampled_span['maximum_stage_global_mm']:.3f} mm`、"
                    "その点群で観測した左カメラ Z は "
                    f"`{sampled_span['minimum_camera_z_mm']:.3f} .. "
                    f"{sampled_span['maximum_camera_z_mm']:.3f} mm` です。"
                ),
                (
                    "これは列挙した測定点だけの結果です。測定点間を連続的に"
                    "保証するものではありません。"
                ),
            ]
        )
    else:
        lines.append(
            "基準点から連なる合格測定点を確定できませんでした: "
            f"{sampled_span.get('reason', 'unknown')}"
        )
    lines.extend(
        [
            "",
            "## 座標・真値・登録",
            "",
            (
                "3D出力は左OpenCVカメラ座標（X=右、Y=下、Z=前方、mm）です。"
                "PAT Yとcamera Zを同一視せず、専用registration passの8頂点だけで"
                "剛体変換を1回求めています。評価passは登録に使用していません。"
            ),
            (
                "登録後のステージ軸（左カメラ座標）: `"
                + ", ".join(
                    f"{float(value):+.6f}"
                    for value in registration["stage_axis_left_camera"]
                )
                + "`"
            ),
            (
                f"registration rigid-fit RMS: "
                f"`{registration['reference_fit_rms_mm']:.6f} mm`、"
                f"scale診断: `{registration['similarity_scale_diagnostic']:.8f}`"
            ),
            (
                "各captureの真値にはcommand値ではなく、registration passの"
                "readback中央値に対するOssila position readback差を使用しています。"
                "command基準の誤差は別の総合診断列として残しています。"
            ),
        ]
    )
    if line.get("available"):
        lines.extend(
            [
                "",
                "## ステージ中心線診断",
                "",
                (
                    f"readback差に対する実測軸スケールは "
                    f"`{line['scale_mm_per_mm']:.8f} mm/mm`、"
                    f"直線残差RMSは `{line['residual_rms_mm']:.6f} mm` です。"
                ),
            ]
        )
    lines.extend(
        [
            "",
            "## 合否条件",
            "",
            (
                f"- 各passのcenter pre/post個別誤差の最大絶対値 ≤ "
                f"`{acceptance['max_abs_center_axial_error_mm']} mm`"
            ),
            (
                f"- 各passで8頂点・12辺が揃い、頂点RMSE ≤ "
                f"`{acceptance['max_cube_vertex_rmse_mm']} mm`"
            ),
            (
                f"- 各passの辺長最大絶対誤差 ≤ "
                f"`{acceptance['max_abs_edge_error_mm']} mm`"
            ),
            (
                f"- 各passの静止scatter P95中央値 ≤ "
                f"`{acceptance['max_median_static_scatter_p95_mm']} mm`"
            ),
            (
                f"- stationの成功pass率 ≥ "
                f"`{acceptance['minimum_successful_pass_fraction']}`"
            ),
            "",
            "## 測定点別結果",
            "",
            (
                "| stage command [mm] | camera Z [mm] | 成功pass | "
                "axial worst [mm] | vertex RMS worst [mm] | "
                "edge worst [mm] | 判定 |"
            ),
            "|---:|---:|---:|---:|---:|---:|:---:|",
        ]
    )
    for station in stations:
        lines.append(
            f"| {station['stage_global_mm']:.3f} | "
            f"{station['camera_z_median_mm']:.3f} | "
            f"{station['successful_passes']}/{station['planned_passes']} | "
            f"{station['center_axial_error_max_abs_mm']:.4f} | "
            f"{station['vertex_rmse_mm']:.4f} | "
            f"{station['edge_error_max_abs_mm']:.4f} | "
            f"{'PASS' if station['pass'] else 'FAIL'} |"
        )
    lines.extend(
        [
            "",
            "## 理論値の扱い",
            "",
            (
                f"単純式では平均 `fx={theory['raw_intrinsic_mean_fx_px']:.3f} px`、"
                f"光学中心間隔 `B={theory['optical_centre_baseline_mm']:.3f} mm` "
                "を用います。実配置は収束ステレオなので、測定点別には実校正を"
                "通した数値Jacobianも計算しています。"
            ),
            (
                "理論σは1 tracking windowの三角測量、実測中心は多数windowの"
                "robust medianです。相関を推定していないため、両者を同一推定量の"
                "σとして直接比較しません。"
            ),
            "",
            "## 出力",
            "",
            "- `sample_measurements.csv`: capture単位の有効率・readback真値・残差",
            "- `pass_accuracy.csv`: 往復・cycle・station単位の合否根拠",
            "- `station_accuracy.csv`: station単位の成功pass率とworst値",
            "- `cube_edges.csv`: pass別12辺の長さ誤差",
            "- `stereo_stage_accuracy_summary.json`: 登録・理論・全集計",
            "- `stereo_stage_accuracy_summary.png`: 距離依存グラフ",
            "- `cube_shape_comparison.png`: 代表passの六面体比較",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_markdown_report(
    path: Path,
    *,
    summary: dict[str, Any],
    stations: list[dict[str, Any]],
    config: dict[str, Any],
) -> None:
    """Write the active report with explicit relative/absolute claim boundaries."""
    registration = summary["registration"]
    sampled_span = summary["accepted_relative_accuracy_span"]
    distance = summary["distance_interpretation"]
    line = summary["stage_line_diagnostic"]
    theory = summary["theory"]
    acceptance = config["analysis"]["acceptance"]
    pat_mode = str(summary.get("evaluation_frame", "")) == "pat"
    if pat_mode:
        registration_description = (
            "27点PAT登録のcamera-to-PAT変換を固定して使用しています。"
            "専用8頂点passはverificationだけに使い、変換の再fitには使っていません。"
            "各captureでは登録時と取得時のOssila readback差から、移動したカメラの"
            "並進を合成しています。PAT座標の物理ステージ軸は `"
            + ", ".join(
                f"{float(value):+.6f}"
                for value in registration["stage_axis_evaluation"]
            )
            + "` です。"
        )
        registration_metric_description = (
            f"verification 8頂点の固定変換に対するRMSは "
            f"`{registration['reference_fit_rms_mm']:.6f} mm`、"
            f"診断用similarity scaleは "
            f"`{registration['similarity_scale_diagnostic']:.8f}` です。"
        )
        truth_description = (
            "PAT位置真値は各 `pat_target_mm` です。カメラ移動補正にはcommand値ではなく、"
            "transformの登録時readbackと各capture直前のOssila readbackの差を使っています。"
        )
    else:
        registration_description = (
            "専用registration passの8頂点だけで剛体変換を求め、評価passは"
            "登録に使用していません。登録後のステージ軸（左カメラ座標）は `"
            + ", ".join(
                f"{float(value):+.6f}"
                for value in registration["stage_axis_left_camera"]
            )
            + "` です。"
        )
        registration_metric_description = (
            f"基準立方体のrigid-fit RMSは "
            f"`{registration['reference_fit_rms_mm']:.6f} mm`、"
            f"similarity scale診断は "
            f"`{registration['similarity_scale_diagnostic']:.8f}` です。"
        )
        truth_description = (
            "各captureの相対真値には、command値ではなくregistration passの"
            "readback中央値に対するOssila readback差を使っています。"
        )
    lines = [
        "# ステレオ・リニアステージ精度評価",
        "",
        "## 結論",
        "",
    ]
    if sampled_span.get("available"):
        lines.extend(
            [
                (
                    "基準登録後の相対誤差条件に連続して合格した標本点は、global座標で "
                    f"`{sampled_span['minimum_stage_global_mm']:.3f} .. "
                    f"{sampled_span['maximum_stage_global_mm']:.3f} mm` です。"
                ),
                (
                    "その標本点で観測されたステレオ推定の左カメラZは "
                    f"`{sampled_span['minimum_camera_z_mm']:.3f} .. "
                    f"{sampled_span['maximum_camera_z_mm']:.3f} mm`、"
                    "光学中心からの直線距離は "
                    f"`{sampled_span['minimum_camera_slant_range_mm']:.3f} .. "
                    f"{sampled_span['maximum_camera_slant_range_mm']:.3f} mm` です。"
                ),
                (
                    "PAT点から校正済みステレオベースライン直線までの垂線距離推定は "
                    f"`{sampled_span['minimum_camera_baseline_perpendicular_range_mm']:.3f} .. "
                    f"{sampled_span['maximum_camera_baseline_perpendicular_range_mm']:.3f} mm` "
                    "です。"
                ),
                (
                    "これはOssila readback差を基準にした相対精度の結果です。"
                    "標本点間の連続範囲を保証するものではありません。"
                ),
            ]
        )
    else:
        lines.append(
            "基準点から連続する合格標本点を確定できませんでした: "
            f"{sampled_span.get('reason', 'unknown')}"
        )
    lines.extend(
        [
            "",
            "## 絶対距離に関する注意",
            "",
            (
                "**絶対距離精度は判定していません。** "
                + str(distance["absolute_accuracy_reason"])
            ),
            (
                "基準PAT中心について登録から得た左カメラ座標は `"
                + ", ".join(
                    f"{float(value):+.6f}"
                    for value in distance["reference_target_left_camera_mm"]
                )
                + " mm`、Zは "
                f"`{distance['reference_camera_z_estimate_mm']:.6f} mm`、"
                "光学中心からの直線距離は "
                f"`{distance['reference_optical_center_slant_range_estimate_mm']:.6f} mm` "
                "です。これらは観測値であり、独立な真値ではありません。"
            ),
            (
                "今回定義する最近点の `D0`、すなわちPAT原点から左右光学中心を通る"
                "校正済みベースライン直線までの垂線距離推定は "
                f"`{distance['reference_pat_origin_to_stereo_baseline_estimate_mm']:.6f} mm` "
                "です。垂線の足は左光学中心からベースライン方向へ "
                f"`{distance['reference_baseline_foot_from_left_optical_center_mm']:.6f} mm` "
                "の位置です。"
            ),
            (
                "左OpenCVカメラ座標はX=右、Y=下、Z=前方です。camera Z、"
                "光学中心からの直線距離 `sqrt(X²+Y²+Z²)`、ステージ軸方向距離は、"
                "軸が完全に一致するときだけ同じになります。"
            ),
            "",
            "## 座標登録とステージ真値",
            "",
            registration_description,
            registration_metric_description,
            truth_description,
        ]
    )
    if line.get("available"):
        lines.extend(
            [
                "",
                "## ステージ中心線診断",
                "",
                (
                    "readback差に対する実測軸スケールは "
                    f"`{line['scale_mm_per_mm']:.8f} mm/mm`、"
                    f"直線残差RMSは `{line['residual_rms_mm']:.6f} mm` です。"
                ),
            ]
        )
    lines.extend(
        [
            "",
            "## 合格条件",
            "",
            (
                "- 各passのcenter pre/post個別誤差の最大絶対値 ≤ "
                f"`{acceptance['max_abs_center_axial_error_mm']} mm`"
            ),
            (
                "- 各passで8頂点・12辺が揃い、頂点RMSE ≤ "
                f"`{acceptance['max_cube_vertex_rmse_mm']} mm`"
            ),
            (
                "- 各passの辺長最大絶対誤差 ≤ "
                f"`{acceptance['max_abs_edge_error_mm']} mm`"
            ),
            (
                "- 各passの静止scatter P95中央値 ≤ "
                f"`{acceptance['max_median_static_scatter_p95_mm']} mm`"
            ),
            (
                "- stationの成功pass率 ≥ "
                f"`{acceptance['minimum_successful_pass_fraction']}`"
            ),
            "",
            "## 測定点別結果",
            "",
            (
                "| Ossila readback [mm] | global [mm] | baseline垂線距離推定 [mm] | "
                "camera Z推定 [mm] | 左光学中心距離推定 [mm] | 成功pass | "
                "axial worst [mm] | vertex RMS worst [mm] | edge worst [mm] | 判定 |"
            ),
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|",
        ]
    )
    for station in stations:
        lines.append(
            f"| {station['stage_readback_hardware_median_mm']:.3f} | "
            f"{station['stage_global_mm']:.3f} | "
            f"{station['camera_baseline_perpendicular_range_median_mm']:.3f} | "
            f"{station['camera_z_median_mm']:.3f} | "
            f"{station['camera_slant_range_median_mm']:.3f} | "
            f"{station['successful_passes']}/{station['planned_passes']} | "
            f"{station['center_axial_error_max_abs_mm']:.4f} | "
            f"{station['vertex_rmse_mm']:.4f} | "
            f"{station['edge_error_max_abs_mm']:.4f} | "
            f"{'PASS' if station['pass'] else 'FAIL'} |"
        )
    lines.extend(
        [
            "",
            "## 理論値の扱い",
            "",
            (
                f"単純式には平均 `fx={theory['raw_intrinsic_mean_fx_px']:.3f} px` と"
                f"光学中心間基線 `B={theory['optical_centre_baseline_mm']:.3f} mm` を"
                "使います。実配置は収束ステレオなので、測定点別には実校正を通した"
                "数値Jacobianも計算しています。"
            ),
            (
                "理論値は1 tracking windowの三角測量、実測中心は多数windowの"
                "robust medianです。相関を仮定していないため、同一推定量のσとして"
                "直接比較しません。"
            ),
            "",
            "## 出力",
            "",
            "- `sample_measurements.csv`: capture単位の有効率、hardware readback、観測距離、残差",
            "- `pass_accuracy.csv`: 往復・cycle・station単位の合否根拠",
            "- `station_accuracy.csv`: station単位の成功pass率とworst値",
            "- `cube_edges.csv`: pass別12辺の長さ誤差",
            "- `stereo_stage_accuracy_summary.json`: 登録、距離の解釈、理論値、全集計",
            "- `stereo_stage_accuracy_summary.png`: 距離依存グラフ",
            "- `cube_shape_comparison.png`: 代表passの六面体比較",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    if args.skip_processing and args.force_processing:
        raise SystemExit("--skip-processing and --force-processing are mutually exclusive.")
    if args.minimum_valid_samples is not None and args.minimum_valid_samples < 1:
        raise SystemExit("--minimum-valid-samples must be at least 1.")
    config_path = resolve_config_path(args)
    config = load_json(config_path)
    if (
        not args.theory_only
        and args.session_dir is not None
        and str(
            config.get("analysis", {}).get(
                "evaluation_frame",
                "camera_relative",
            )
        ).strip().lower()
        == "pat"
    ):
        candidate_manifest_path = (
            args.session_dir.resolve()
            / "stereo_stage_accuracy_manifest.json"
        )
        if candidate_manifest_path.is_file():
            candidate_manifest = load_json(candidate_manifest_path)
            immutable_transform = str(
                candidate_manifest.get("camera_to_pat_transform", "")
            ).strip()
            if immutable_transform:
                config["analysis"]["camera_to_pat_transform"] = (
                    immutable_transform
                )
    resolved = validate_config(config, config_path)
    if args.theory_only:
        return run_theory_only(args, config, Path(resolved["stereo_calibration"]))
    if args.session_dir is None:
        raise SystemExit("session_dir is required unless --theory-only is used.")

    session_dir = args.session_dir.resolve()
    manifest_path = session_dir / "stereo_stage_accuracy_manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"Session manifest not found: {manifest_path}")
    manifest = load_json(manifest_path)
    stored_plan_hash = str(manifest.get("plan_hash", ""))
    try:
        manifest_plan_hash = plan_hash(manifest["samples"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"Session manifest has an invalid sample plan: {exc}") from exc
    expected_plan_hash = plan_hash(generate_plan(config))
    if not stored_plan_hash or manifest_plan_hash != stored_plan_hash:
        raise SystemExit(
            "Session manifest samples no longer match its stored plan hash; "
            "analysis was refused."
        )
    if expected_plan_hash != stored_plan_hash:
        raise SystemExit(
            "The analysis config generates a different physical sample plan from "
            "the captured session; use the session config or a plan-compatible override."
        )
    calibration_path = Path(manifest.get("stereo_calibration", resolved["stereo_calibration"])).resolve()
    expected_calibration_hash = str(
        manifest.get("configuration_fingerprint", {}).get(
            "stereo_calibration_sha256",
            "",
        )
    )
    if expected_calibration_hash and file_sha256(calibration_path) != expected_calibration_hash:
        raise SystemExit(
            "The immutable session calibration copy no longer matches the capture "
            "fingerprint; analysis was refused."
        )
    transform_data: dict[str, Any] | None = None
    if str(resolved["evaluation_frame"]) == "pat":
        session_transform_text = str(
            manifest.get("camera_to_pat_transform", "")
        ).strip()
        if not session_transform_text:
            raise SystemExit(
                "This PAT-frame analysis requires the immutable camera-to-PAT "
                "transform copied into the capture session."
            )
        session_transform = Path(session_transform_text).resolve()
        expected_transform_hash = str(
            manifest.get("configuration_fingerprint", {}).get(
                "camera_to_pat_transform_sha256",
                "",
            )
        )
        if (
            not expected_transform_hash
            or not session_transform.is_file()
            or file_sha256(session_transform) != expected_transform_hash
        ):
            raise SystemExit(
                "The immutable session camera-to-PAT transform is missing or no "
                "longer matches the capture fingerprint; analysis was refused."
            )
        transform_data = load_camera_to_pat_transform_json(session_transform)
        if (
            str(transform_data["stereo_calibration_sha256"])
            != file_sha256(calibration_path)
        ):
            raise SystemExit(
                "The immutable camera-to-PAT transform and session stereo "
                "calibration have different SHA-256 values."
            )
    calibration = load_stereo_calibration(calibration_path)
    process_samples(
        args=args,
        config=config,
        calibration=calibration_path,
        manifest_path=manifest_path,
        manifest=manifest,
    )
    minimum_valid_samples = (
        int(args.minimum_valid_samples)
        if args.minimum_valid_samples is not None
        else int(config["analysis"]["minimum_valid_samples"])
    )
    sample_rows = build_sample_rows(
        manifest,
        config,
        minimum_valid_samples,
        calibration,
    )
    if str(resolved["evaluation_frame"]) == "pat":
        assert transform_data is not None
        registration = apply_pat_frame_registration(
            sample_rows,
            config,
            transform_data,
            int(resolved["stage_direction"]),
        )
    else:
        registration = apply_reference_registration(sample_rows, config)
    usable = sum(bool(row["usable"]) for row in sample_rows)
    print(f"[ANALYSE] usable static captures: {usable}/{len(sample_rows)}")
    evaluation_rows = [
        row for row in sample_rows if str(row["data_role"]) == "evaluation"
    ]
    pass_centres = build_pass_centres(
        evaluation_rows,
        registration,
        calibration,
        config,
    )
    pass_results = build_pass_results(
        evaluation_rows,
        pass_centres,
        registration,
        config,
    )
    stations = build_station_rows(pass_results, config)
    reference = float(registration["reference_stage_global_mm"])
    accurate_range = accepted_sampled_span(stations, reference)
    distance_summary = distance_interpretation(
        registration,
        stations,
        config,
        calibration,
    )
    line_diagnostic = stage_line_diagnostic(pass_centres)
    theory = theory_summary(config, calibration)

    summary = {
        "schema_version": 3,
        "session_dir": str(session_dir),
        "stereo_calibration": str(calibration_path),
        "input_samples": len(sample_rows),
        "usable_static_captures": usable,
        "minimum_valid_samples_per_capture": minimum_valid_samples,
        "minimum_sample_valid_fraction": float(
            config["analysis"]["minimum_sample_valid_fraction"]
        ),
        "registration_samples_are_excluded_from_evaluation": True,
        "evaluation_frame": str(registration["evaluation_frame"]),
        "registration_samples_used_only_for_verification": bool(
            registration.get(
                "registration_samples_used_only_for_verification",
                False,
            )
        ),
        "registration": registration,
        "stage_line_diagnostic": line_diagnostic,
        "accepted_relative_accuracy_span": accurate_range,
        "maximum_accurate_range": accurate_range,
        "maximum_accurate_range_deprecated": (
            "Use accepted_relative_accuracy_span. This compatibility alias is not "
            "an independent absolute-distance accuracy claim."
        ),
        "distance_interpretation": distance_summary,
        "acceptance": config["analysis"]["acceptance"],
        "theory": theory,
        "passes": [
            {
                key: value
                for key, value in result.items()
                if not key.startswith("_")
            }
            for result in pass_results
        ],
        "stations": [
            {
                key: value
                for key, value in station.items()
                if not key.startswith("_")
            }
            for station in stations
        ],
    }
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else session_dir / "analysis"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    flat_samples = flatten_sample_rows(sample_rows)
    sample_fields = list(flat_samples[0]) if flat_samples else ["sample_id"]
    flat_stations = flatten_station_rows(stations)
    station_fields = list(flat_stations[0]) if flat_stations else ["stage_global_mm"]
    flat_passes = flatten_pass_results(pass_results)
    pass_fields = list(flat_passes[0]) if flat_passes else ["pass_name"]
    flat_edges = flatten_edge_rows(stations)
    edge_fields = (
        list(flat_edges[0])
        if flat_edges
        else [
            "stage_global_mm",
            "camera_z_median_mm",
            "left",
            "right",
            "axis",
            "expected_length_mm",
            "measured_length_mm",
            "error_mm",
            "error_percent",
        ]
    )
    write_csv(output_dir / "sample_measurements.csv", flat_samples, sample_fields)
    write_csv(output_dir / "pass_accuracy.csv", flat_passes, pass_fields)
    write_csv(output_dir / "station_accuracy.csv", flat_stations, station_fields)
    write_csv(output_dir / "cube_edges.csv", flat_edges, edge_fields)
    atomic_write_json(output_dir / "stereo_stage_accuracy_summary.json", summary)
    make_summary_plot(
        output_dir / "stereo_stage_accuracy_summary.png",
        pass_centres,
        stations,
        config,
    )
    make_cube_plot(
        output_dir / "cube_shape_comparison.png",
        stations,
        registration,
    )
    write_markdown_report(
        output_dir / "stereo_stage_accuracy_report.md",
        summary=summary,
        stations=stations,
        config=config,
    )
    manifest["analysis_output_dir"] = str(output_dir)
    manifest["analysis_summary"] = str(
        (output_dir / "stereo_stage_accuracy_summary.json").resolve()
    )
    manifest["analysis_processing_fingerprint"] = processing_fingerprint(
        config,
        calibration_path,
    )
    manifest["analysis_skip_processing"] = bool(args.skip_processing)
    atomic_write_json(manifest_path, manifest)

    print(f"Analysis: {output_dir}")
    if accurate_range.get("available"):
        print(
            "[RESULT][RELATIVE] accepted sampled-station span "
            f"{accurate_range['minimum_stage_global_mm']:+.3f} .. "
            f"{accurate_range['maximum_stage_global_mm']:+.3f} mm; "
            "observed stereo-estimated camera Z "
            f"{accurate_range['minimum_camera_z_mm']:.3f} .. "
            f"{accurate_range['maximum_camera_z_mm']:.3f} mm; "
            "PAT-to-baseline perpendicular estimate "
            f"{accurate_range['minimum_camera_baseline_perpendicular_range_mm']:.3f} .. "
            f"{accurate_range['maximum_camera_baseline_perpendicular_range_mm']:.3f} mm"
        )
        print(
            "[RESULT][ABSOLUTE] unavailable: no independent camera-to-PAT distance "
            "truth is configured; the reference translation is self-estimated."
        )
        print("[RESULT] No accuracy claim is made between sampled stations.")
    else:
        print(f"[RESULT] no accepted sampled span: {accurate_range.get('reason')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
