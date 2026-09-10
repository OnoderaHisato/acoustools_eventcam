#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Process a PAT stereo grid session and estimate camera-to-PAT registration."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np

from pat_stereo_grid_capture import atomic_write_json, load_json
from stereo_stage_accuracy_common import (
    load_stereo_calibration,
    point_to_stereo_baseline_line,
)


SCRIPT_DIR = Path(__file__).resolve().parent


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Track/triangulate every PAT grid capture, aggregate static points, and fit camera-to-PAT registration.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("session_dir", type=Path, help="Session created by pat_stereo_grid_capture.py.")
    parser.add_argument("--skip-processing", action="store_true", help="Use existing stereo_3d_points.npz files.")
    parser.add_argument("--force-processing", action="store_true", help="Re-run tracking and triangulation even when output exists.")
    parser.add_argument("--process-only", action="store_true", help="Run per-point processing but do not fit the registration.")
    parser.add_argument("--continue-on-error", action="store_true", help="Continue when one point cannot be processed.")
    parser.add_argument("--minimum-valid-samples", type=int, default=20, help="Minimum robust 3D samples required per grid point.")
    parser.add_argument("--no-point-outlier-rejection", action="store_true", help="Keep all point correspondences in the final fit.")
    return parser.parse_args()


def processing_command(config: dict[str, Any], calibration: Path, run_dir: Path) -> list[str]:
    tracking = config["tracking"]
    return [
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
        "--right-time-offset-sec",
        str(float(tracking.get("right_time_offset_sec", 0.0))),
        "--max-time-gap-sec",
        str(float(tracking.get("max_time_gap_sec", 0.001))),
    ]


def process_points(
    session_dir: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    config: dict[str, Any],
    calibration: Path,
    args: argparse.Namespace,
) -> None:
    failures = 0
    for point in manifest["points"]:
        run_text = str(point.get("accepted_run_dir", "")).strip()
        if not run_text:
            print(f"[SKIP] {point['point_id']}: no accepted capture")
            continue
        run_dir = Path(run_text)
        output_npz = run_dir / "stereo_3d" / "stereo_3d_points.npz"
        if output_npz.exists() and not args.force_processing:
            print(f"[REUSE] {point['point_id']}: {output_npz}")
            point["status"] = "processed"
            point["stereo_3d_npz"] = str(output_npz.resolve())
            continue
        if args.skip_processing:
            print(f"[MISSING] {point['point_id']}: {output_npz}")
            failures += 1
            continue
        command = processing_command(config, calibration, run_dir)
        print(f"[PROCESS] {point['point_id']}", flush=True)
        completed = subprocess.run(command, check=False)
        if completed.returncode == 0 and output_npz.exists():
            point["status"] = "processed"
            point["stereo_3d_npz"] = str(output_npz.resolve())
        else:
            point["status"] = "processing_failed"
            point["processing_return_code"] = int(completed.returncode)
            failures += 1
            atomic_write_json(manifest_path, manifest)
            if not args.continue_on_error:
                raise SystemExit(
                    f"Processing failed at {point['point_id']}. "
                    "Fix it, then re-run this command; completed points will be reused."
                )
        atomic_write_json(manifest_path, manifest)
    if failures:
        print(f"[WARN] points without usable processing output: {failures}")


def robust_static_point(
    npz_path: Path,
    *,
    trim_start_sec: float,
    trim_end_sec: float,
    outlier_floor_mm: float,
    mad_factor: float,
) -> dict[str, Any]:
    data = np.load(npz_path, allow_pickle=False)
    points = np.asarray(data["points_left_cam_mm"], dtype=np.float64)
    valid = np.asarray(data["valid"], dtype=bool)
    t_sec = np.asarray(data["t_sec"], dtype=np.float64)
    mask = valid & np.all(np.isfinite(points), axis=1) & np.isfinite(t_sec)
    if mask.any():
        first_t = float(np.nanmin(t_sec[mask]))
        last_t = float(np.nanmax(t_sec[mask]))
        mask &= t_sec >= first_t + max(0.0, float(trim_start_sec))
        mask &= t_sec <= last_t - max(0.0, float(trim_end_sec))
    samples = points[mask]
    if samples.size == 0:
        return {
            "center_mm": np.full(3, np.nan),
            "input_samples": int(points.shape[0]),
            "trimmed_valid_samples": 0,
            "robust_samples": 0,
            "sample_threshold_mm": float("nan"),
            "scatter_rms_mm": float("nan"),
            "scatter_p95_mm": float("nan"),
            "scatter_max_mm": float("nan"),
        }

    initial_center = np.nanmedian(samples, axis=0)
    radial = np.linalg.norm(samples - initial_center, axis=1)
    radial_median = float(np.nanmedian(radial))
    radial_mad = float(np.nanmedian(np.abs(radial - radial_median)))
    robust_sigma = 1.4826 * radial_mad
    threshold = max(
        float(outlier_floor_mm),
        radial_median + float(mad_factor) * robust_sigma,
    )
    keep = np.isfinite(radial) & (radial <= threshold)
    robust_samples = samples[keep]
    if robust_samples.size == 0:
        robust_samples = samples
    center = np.nanmedian(robust_samples, axis=0)
    scatter = np.linalg.norm(robust_samples - center, axis=1)
    return {
        "center_mm": center,
        "input_samples": int(points.shape[0]),
        "trimmed_valid_samples": int(samples.shape[0]),
        "robust_samples": int(robust_samples.shape[0]),
        "sample_threshold_mm": float(threshold),
        "scatter_rms_mm": float(np.sqrt(np.mean(scatter * scatter))) if scatter.size else float("nan"),
        "scatter_p95_mm": float(np.percentile(scatter, 95)) if scatter.size else float("nan"),
        "scatter_max_mm": float(np.max(scatter)) if scatter.size else float("nan"),
    }


def kabsch(source: np.ndarray, target: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if source.shape != target.shape or source.ndim != 2 or source.shape[1] != 3:
        raise ValueError("source and target must both be Nx3 arrays.")
    if source.shape[0] < 3:
        raise ValueError("At least three correspondences are required.")
    source_center = source.mean(axis=0)
    target_center = target.mean(axis=0)
    source_zero = source - source_center
    target_zero = target - target_center
    u, _, vt = np.linalg.svd(source_zero.T @ target_zero)
    rotation = vt.T @ u.T
    if np.linalg.det(rotation) < 0:
        vt[-1, :] *= -1
        rotation = vt.T @ u.T
    translation = target_center - rotation @ source_center
    return rotation, translation


def transform_points(points: np.ndarray, rotation: np.ndarray, translation: np.ndarray) -> np.ndarray:
    return (rotation @ points.T).T + translation


def pat_origin_in_left_camera(
    rotation_camera_to_pat: np.ndarray,
    translation_camera_to_pat_mm: np.ndarray,
) -> np.ndarray:
    """Invert p_pat = R @ p_camera + t at p_pat=[0,0,0]."""
    rotation = np.asarray(rotation_camera_to_pat, dtype=np.float64)
    translation = np.asarray(translation_camera_to_pat_mm, dtype=np.float64).reshape(3)
    if rotation.shape != (3, 3):
        raise ValueError("rotation_camera_to_pat must have shape (3, 3).")
    return -rotation.T @ translation


def fit_similarity_scale(source: np.ndarray, target: np.ndarray, rotation: np.ndarray) -> float:
    source_zero = source - source.mean(axis=0)
    target_zero = target - target.mean(axis=0)
    rotated = (rotation @ source_zero.T).T
    denominator = float(np.sum(rotated * rotated))
    if denominator <= 0:
        return float("nan")
    return float(np.sum(rotated * target_zero) / denominator)


def rms(values: np.ndarray) -> float:
    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    return float(np.sqrt(np.mean(finite * finite))) if finite.size else float("nan")


def build_correspondences(
    manifest: dict[str, Any],
    config: dict[str, Any],
    minimum_valid_samples: int,
) -> list[dict[str, Any]]:
    registration = config.get("registration", {})
    rows: list[dict[str, Any]] = []
    for point in manifest["points"]:
        target = np.asarray(point["target_pat_mm"], dtype=np.float64)
        npz_text = str(point.get("stereo_3d_npz", "")).strip()
        if not npz_text and point.get("accepted_run_dir"):
            candidate = Path(point["accepted_run_dir"]) / "stereo_3d" / "stereo_3d_points.npz"
            if candidate.exists():
                npz_text = str(candidate.resolve())
        row: dict[str, Any] = {
            "point_id": str(point["point_id"]),
            "target_pat_mm": target,
            "stereo_3d_npz": npz_text,
            "usable": False,
            "reason": "",
        }
        if not npz_text or not Path(npz_text).exists():
            row["reason"] = "missing stereo_3d_points.npz"
            rows.append(row)
            continue
        stats = robust_static_point(
            Path(npz_text),
            trim_start_sec=float(registration.get("trim_start_sec", 0.05)),
            trim_end_sec=float(registration.get("trim_end_sec", 0.05)),
            outlier_floor_mm=float(registration.get("sample_outlier_floor_mm", 0.5)),
            mad_factor=float(registration.get("sample_outlier_mad_factor", 6.0)),
        )
        row.update(stats)
        center = np.asarray(stats["center_mm"], dtype=np.float64)
        if int(stats["robust_samples"]) < int(minimum_valid_samples):
            row["reason"] = f"only {stats['robust_samples']} robust samples"
        elif not np.all(np.isfinite(center)):
            row["reason"] = "non-finite camera point"
        else:
            row["usable"] = True
        rows.append(row)
    return rows


def choose_validation_mask(target: np.ndarray, every: int) -> np.ndarray:
    mask = np.zeros(target.shape[0], dtype=bool)
    if every <= 1 or target.shape[0] < 8:
        return mask
    mask[np.arange(target.shape[0]) % every == every - 1] = True
    train = target[~mask]
    if mask.sum() < 1 or train.shape[0] < 4 or np.linalg.matrix_rank(train - train.mean(axis=0)) < 3:
        mask[:] = False
    return mask


def fit_registration(
    camera: np.ndarray,
    target: np.ndarray,
    *,
    point_outlier_threshold_mm: float,
    reject_point_outliers: bool,
    validation_every: int,
) -> dict[str, Any]:
    if camera.shape[0] < 4:
        raise SystemExit("At least four usable grid points are required.")
    target_rank = int(np.linalg.matrix_rank(target - target.mean(axis=0)))
    if target_rank < 3:
        raise SystemExit("Usable PAT targets are coplanar. Capture points at multiple X, Y, and Z coordinates.")

    inlier = np.ones(camera.shape[0], dtype=bool)
    if reject_point_outliers and point_outlier_threshold_mm > 0:
        for _ in range(5):
            rotation, translation = kabsch(camera[inlier], target[inlier])
            residual = np.linalg.norm(transform_points(camera, rotation, translation) - target, axis=1)
            proposed = residual <= float(point_outlier_threshold_mm)
            if proposed.sum() < 4:
                break
            if np.linalg.matrix_rank(target[proposed] - target[proposed].mean(axis=0)) < 3:
                break
            if np.array_equal(proposed, inlier):
                break
            inlier = proposed

    validation_local = choose_validation_mask(target[inlier], int(validation_every))
    inlier_indices = np.flatnonzero(inlier)
    validation = np.zeros(camera.shape[0], dtype=bool)
    validation[inlier_indices[validation_local]] = True
    train = inlier & ~validation
    validation_summary: dict[str, Any] = {"enabled": False}
    if validation.any() and train.sum() >= 4:
        val_rotation, val_translation = kabsch(camera[train], target[train])
        val_residual = np.linalg.norm(
            transform_points(camera, val_rotation, val_translation) - target,
            axis=1,
        )
        validation_summary = {
            "enabled": True,
            "training_points": int(train.sum()),
            "validation_points": int(validation.sum()),
            "training_rms_mm": rms(val_residual[train]),
            "validation_rms_mm": rms(val_residual[validation]),
            "validation_max_mm": float(np.max(val_residual[validation])),
        }

    rotation, translation = kabsch(camera[inlier], target[inlier])
    transformed = transform_points(camera, rotation, translation)
    residual = np.linalg.norm(transformed - target, axis=1)
    scale = fit_similarity_scale(camera[inlier], target[inlier], rotation)
    return {
        "rotation": rotation,
        "translation_mm": translation,
        "transformed_mm": transformed,
        "residual_mm": residual,
        "inlier": inlier,
        "validation": validation,
        "target_rank": target_rank,
        "similarity_scale_diagnostic": scale,
        "validation_summary": validation_summary,
    }


def write_correspondence_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "point_id",
        "target_pat_x_mm",
        "target_pat_y_mm",
        "target_pat_z_mm",
        "camera_x_mm",
        "camera_y_mm",
        "camera_z_mm",
        "camera_in_pat_x_mm",
        "camera_in_pat_y_mm",
        "camera_in_pat_z_mm",
        "registration_residual_mm",
        "fit_inlier",
        "validation_point",
        "input_samples",
        "trimmed_valid_samples",
        "robust_samples",
        "sample_threshold_mm",
        "scatter_rms_mm",
        "scatter_p95_mm",
        "scatter_max_mm",
        "usable",
        "reason",
        "stereo_3d_npz",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            target = np.asarray(row["target_pat_mm"], dtype=float)
            camera = np.asarray(row.get("center_mm", np.full(3, np.nan)), dtype=float)
            transformed = np.asarray(row.get("transformed_mm", np.full(3, np.nan)), dtype=float)
            writer.writerow(
                {
                    "point_id": row["point_id"],
                    "target_pat_x_mm": f"{target[0]:.9f}",
                    "target_pat_y_mm": f"{target[1]:.9f}",
                    "target_pat_z_mm": f"{target[2]:.9f}",
                    "camera_x_mm": f"{camera[0]:.9f}" if np.isfinite(camera[0]) else "",
                    "camera_y_mm": f"{camera[1]:.9f}" if np.isfinite(camera[1]) else "",
                    "camera_z_mm": f"{camera[2]:.9f}" if np.isfinite(camera[2]) else "",
                    "camera_in_pat_x_mm": f"{transformed[0]:.9f}" if np.isfinite(transformed[0]) else "",
                    "camera_in_pat_y_mm": f"{transformed[1]:.9f}" if np.isfinite(transformed[1]) else "",
                    "camera_in_pat_z_mm": f"{transformed[2]:.9f}" if np.isfinite(transformed[2]) else "",
                    "registration_residual_mm": (
                        f"{float(row['registration_residual_mm']):.9f}"
                        if np.isfinite(float(row.get("registration_residual_mm", np.nan)))
                        else ""
                    ),
                    "fit_inlier": int(bool(row.get("fit_inlier", False))),
                    "validation_point": int(bool(row.get("validation_point", False))),
                    "input_samples": int(row.get("input_samples", 0)),
                    "trimmed_valid_samples": int(row.get("trimmed_valid_samples", 0)),
                    "robust_samples": int(row.get("robust_samples", 0)),
                    "sample_threshold_mm": row.get("sample_threshold_mm", ""),
                    "scatter_rms_mm": row.get("scatter_rms_mm", ""),
                    "scatter_p95_mm": row.get("scatter_p95_mm", ""),
                    "scatter_max_mm": row.get("scatter_max_mm", ""),
                    "usable": int(bool(row.get("usable", False))),
                    "reason": row.get("reason", ""),
                    "stereo_3d_npz": row.get("stereo_3d_npz", ""),
                }
            )


def write_registration_plot(
    path: Path,
    target: np.ndarray,
    transformed: np.ndarray,
    inlier: np.ndarray,
) -> None:
    import matplotlib.pyplot as plt

    figure = plt.figure(figsize=(12, 9))
    axes = [
        figure.add_subplot(2, 2, 1, projection="3d"),
        figure.add_subplot(2, 2, 2),
        figure.add_subplot(2, 2, 3),
        figure.add_subplot(2, 2, 4),
    ]
    ax3d = axes[0]
    ax3d.scatter(target[:, 0], target[:, 1], target[:, 2], c="#e45756", marker="s", label="PAT target")
    ax3d.scatter(
        transformed[:, 0],
        transformed[:, 1],
        transformed[:, 2],
        c=np.where(inlier, "#2a9d8f", "#f4a261"),
        marker="o",
        label="Camera transformed",
    )
    for expected, measured in zip(target, transformed):
        ax3d.plot(
            [expected[0], measured[0]],
            [expected[1], measured[1]],
            [expected[2], measured[2]],
            color="#777777",
            linewidth=0.7,
        )
    ax3d.set_xlabel("PAT X [mm]")
    ax3d.set_ylabel("PAT Y [mm]")
    ax3d.set_zlabel("PAT Z [mm]")
    ax3d.legend(loc="best")
    projections = [
        (0, 1, "X", "Y"),
        (0, 2, "X", "Z"),
        (1, 2, "Y", "Z"),
    ]
    for axis, (a, b, a_name, b_name) in zip(axes[1:], projections):
        axis.scatter(target[:, a], target[:, b], c="#e45756", marker="s", label="PAT target")
        axis.scatter(transformed[:, a], transformed[:, b], c="#2a9d8f", marker="o", label="Camera transformed")
        for expected, measured in zip(target, transformed):
            axis.plot([expected[a], measured[a]], [expected[b], measured[b]], color="#999999", linewidth=0.7)
        axis.set_xlabel(f"PAT {a_name} [mm]")
        axis.set_ylabel(f"PAT {b_name} [mm]")
        axis.set_aspect("equal", adjustable="box")
        axis.grid(True, alpha=0.25)
    figure.suptitle("PAT target vs. transformed stereo measurement")
    figure.tight_layout()
    figure.savefig(path, dpi=180)
    plt.close(figure)


def main() -> int:
    args = parse_args()
    session_dir = args.session_dir.resolve()
    manifest_path = session_dir / "pat_stereo_grid_manifest.json"
    config_path = session_dir / "session_config.json"
    if not manifest_path.exists() or not config_path.exists():
        raise SystemExit(f"Not a PAT stereo grid session: {session_dir}")
    manifest = load_json(manifest_path)
    config = load_json(config_path)
    calibration = Path(manifest["stereo_calibration"]).resolve()
    if not calibration.exists():
        raise SystemExit(f"Stereo calibration not found: {calibration}")

    process_points(session_dir, manifest_path, manifest, config, calibration, args)
    atomic_write_json(manifest_path, manifest)
    if args.process_only:
        print("Per-point processing is complete; registration fit was skipped.")
        return 0

    rows = build_correspondences(manifest, config, args.minimum_valid_samples)
    usable_rows = [row for row in rows if row["usable"]]
    if len(usable_rows) < 4:
        output = session_dir / "registration"
        output.mkdir(parents=True, exist_ok=True)
        write_correspondence_csv(output / "grid_correspondences.csv", rows)
        raise SystemExit(f"Only {len(usable_rows)} usable points; at least four non-coplanar points are required.")

    camera = np.vstack([row["center_mm"] for row in usable_rows]).astype(np.float64)
    target = np.vstack([row["target_pat_mm"] for row in usable_rows]).astype(np.float64)
    registration_config = config.get("registration", {})
    result = fit_registration(
        camera,
        target,
        point_outlier_threshold_mm=float(registration_config.get("point_outlier_threshold_mm", 2.0)),
        reject_point_outliers=not args.no_point_outlier_rejection,
        validation_every=int(registration_config.get("validation_every", 4)),
    )
    for index, row in enumerate(usable_rows):
        row["transformed_mm"] = result["transformed_mm"][index]
        row["registration_residual_mm"] = float(result["residual_mm"][index])
        row["fit_inlier"] = bool(result["inlier"][index])
        row["validation_point"] = bool(result["validation"][index])

    output_dir = session_dir / "registration"
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "grid_correspondences.csv"
    npz_path = output_dir / "camera_to_pat_transform.npz"
    json_path = output_dir / "camera_to_pat_transform.json"
    text_path = output_dir / "camera_to_pat_transform_summary.txt"
    plot_path = output_dir / "pat_camera_registration.png"
    write_correspondence_csv(csv_path, rows)

    inlier = np.asarray(result["inlier"], dtype=bool)
    residual = np.asarray(result["residual_mm"], dtype=float)
    rotation_camera_to_pat = np.asarray(result["rotation"], dtype=np.float64)
    translation_camera_to_pat = np.asarray(
        result["translation_mm"],
        dtype=np.float64,
    )
    pat_origin_left_camera = pat_origin_in_left_camera(
        rotation_camera_to_pat,
        translation_camera_to_pat,
    )
    pat_origin_slant_range = float(np.linalg.norm(pat_origin_left_camera))
    stereo_calibration_data = load_stereo_calibration(calibration)
    pat_origin_baseline = point_to_stereo_baseline_line(
        pat_origin_left_camera,
        stereo_calibration_data,
    )
    pat_origin_baseline_range = float(
        pat_origin_baseline["perpendicular_distance_mm"]
    )
    np.savez_compressed(
        npz_path,
        R_camera_to_pat=rotation_camera_to_pat,
        t_camera_to_pat_mm=translation_camera_to_pat,
        pat_origin_left_camera_mm=pat_origin_left_camera,
        pat_origin_left_camera_z_mm=np.asarray(pat_origin_left_camera[2]),
        pat_origin_left_camera_slant_range_mm=np.asarray(pat_origin_slant_range),
        pat_origin_to_stereo_baseline_perpendicular_range_mm=np.asarray(
            pat_origin_baseline_range
        ),
        pat_origin_baseline_foot_left_camera_mm=np.asarray(
            pat_origin_baseline["foot_left_camera_mm"],
            dtype=np.float64,
        ),
        stereo_right_optical_center_left_camera_mm=np.asarray(
            pat_origin_baseline["right_optical_center_mm"],
            dtype=np.float64,
        ),
        stereo_baseline_unit_left_camera=np.asarray(
            pat_origin_baseline["baseline_unit_left_camera"],
            dtype=np.float64,
        ),
        pat_origin_distance_role=np.asarray(
            "self-estimated from this stereo PAT registration; not independent ground truth"
        ),
        source_camera_mm=camera,
        target_pat_mm=target,
        transformed_camera_mm=np.asarray(result["transformed_mm"], dtype=np.float64),
        residual_mm=residual,
        inlier=inlier,
        validation=np.asarray(result["validation"], dtype=bool),
        point_ids=np.asarray([row["point_id"] for row in usable_rows]),
        stereo_calibration=np.asarray(str(calibration)),
        stereo_calibration_sha256=np.asarray(file_sha256(calibration)),
        session_dir=np.asarray(str(session_dir)),
        stage_hardware_readback_mm=np.asarray(
            float(manifest.get("stage_hardware_readback_mm", float("nan")))
        ),
        coordinate_convention=np.asarray(
            "p_pat_mm = R_camera_to_pat @ p_left_camera_mm + t_camera_to_pat_mm"
        ),
    )
    summary = {
        "session_dir": str(session_dir),
        "session_config": str(config_path),
        "session_config_sha256": file_sha256(config_path),
        "stage_hardware_readback_mm": manifest.get(
            "stage_hardware_readback_mm"
        ),
        "stage_reference": manifest.get("stage_reference"),
        "stereo_calibration": str(calibration),
        "stereo_calibration_sha256": file_sha256(calibration),
        "left_camera_serial": str(config["camera"]["left_serial"]),
        "right_camera_serial": str(config["camera"]["right_serial"]),
        "pat_controller_ids": list(config["pat"].get("controller_ids", [])),
        "pat_center_mm": list(config["pat"]["center_mm"]),
        "acoustools_zero_in_pat_mm": list(
            config["pat"]["acoustools_zero_in_pat_mm"]
        ),
        "transform_npz": str(npz_path.resolve()),
        "transform_summary_txt": str(text_path.resolve()),
        "correspondence_csv": str(csv_path.resolve()),
        "plot_png": str(plot_path.resolve()),
        "transform_formula": "p_pat_mm = R_camera_to_pat @ p_left_camera_mm + t_camera_to_pat_mm",
        "camera_coordinates": "left OpenCV camera frame: X right, Y down, Z forward; units mm",
        "pat_coordinates": "AcousTools target frame from the grid config; units mm",
        "R_camera_to_pat": np.asarray(result["rotation"]).tolist(),
        "t_camera_to_pat_mm": np.asarray(result["translation_mm"]).tolist(),
        "inverse_origin_formula": (
            "p_left_camera_of_pat_origin_mm = "
            "-R_camera_to_pat.T @ t_camera_to_pat_mm"
        ),
        "pat_origin_left_camera_mm": pat_origin_left_camera.tolist(),
        "pat_origin_left_camera_z_mm": float(pat_origin_left_camera[2]),
        "pat_origin_left_camera_slant_range_mm": pat_origin_slant_range,
        "pat_origin_to_stereo_baseline_perpendicular_range_mm": (
            pat_origin_baseline_range
        ),
        "pat_origin_baseline_foot_left_camera_mm": np.asarray(
            pat_origin_baseline["foot_left_camera_mm"]
        ).tolist(),
        "pat_origin_baseline_foot_from_left_optical_center_mm": float(
            pat_origin_baseline["foot_from_left_optical_center_mm"]
        ),
        "pat_origin_baseline_foot_fraction": float(
            pat_origin_baseline["foot_fraction_of_left_to_right_baseline"]
        ),
        "pat_origin_baseline_foot_between_optical_centers": bool(
            pat_origin_baseline["foot_is_between_optical_centers"]
        ),
        "stereo_right_optical_center_left_camera_mm": np.asarray(
            pat_origin_baseline["right_optical_center_mm"]
        ).tolist(),
        "stereo_baseline_length_mm": float(
            pat_origin_baseline["baseline_length_mm"]
        ),
        "pat_origin_distance_role": (
            "Self-estimated from the same stereo observations used for registration. "
            "It is a metric distance estimate, not independent absolute-distance truth."
        ),
        "usable_points": int(len(usable_rows)),
        "fit_inliers": int(inlier.sum()),
        "rejected_point_ids": [
            usable_rows[index]["point_id"]
            for index in np.flatnonzero(~inlier)
        ],
        "fit_rms_mm": rms(residual[inlier]),
        "fit_mean_mm": float(np.mean(residual[inlier])),
        "fit_max_mm": float(np.max(residual[inlier])),
        "similarity_scale_diagnostic": float(result["similarity_scale_diagnostic"]),
        "target_rank": int(result["target_rank"]),
        "validation": result["validation_summary"],
        "notes": [
            "The saved transform is rigid and does not apply the diagnostic similarity scale.",
            "This is an operational registration: static-particle equilibrium error is included in the residual.",
            "The reported PAT-origin camera Z/slant range is self-estimated and cannot reveal a constant stereo depth bias.",
            "Recalibrate after either camera or the PAT coordinate frame is physically moved.",
        ],
    }
    acceptance = registration_config.get("acceptance", {})
    quality_failures: list[str] = []
    minimum_usable_points = int(acceptance.get("minimum_usable_points", 4))
    minimum_inlier_fraction = float(acceptance.get("minimum_inlier_fraction", 0.0))
    maximum_fit_rms_mm = float(acceptance.get("maximum_fit_rms_mm", 1.0e300))
    maximum_fit_max_mm = float(acceptance.get("maximum_fit_max_mm", 1.0e300))
    maximum_validation_rms_mm = float(
        acceptance.get("maximum_validation_rms_mm", 1.0e300)
    )
    maximum_scale_error = float(
        acceptance.get("maximum_similarity_scale_error", 1.0e300)
    )
    inlier_fraction = float(inlier.sum() / max(1, len(usable_rows)))
    if len(usable_rows) < minimum_usable_points:
        quality_failures.append(
            f"usable_points={len(usable_rows)} < {minimum_usable_points}"
        )
    if inlier_fraction < minimum_inlier_fraction:
        quality_failures.append(
            f"inlier_fraction={inlier_fraction:.6f} < {minimum_inlier_fraction:.6f}"
        )
    if float(summary["fit_rms_mm"]) > maximum_fit_rms_mm:
        quality_failures.append(
            f"fit_rms_mm={summary['fit_rms_mm']:.6f} > {maximum_fit_rms_mm:.6f}"
        )
    if float(summary["fit_max_mm"]) > maximum_fit_max_mm:
        quality_failures.append(
            f"fit_max_mm={summary['fit_max_mm']:.6f} > {maximum_fit_max_mm:.6f}"
        )
    if (
        summary["validation"].get("enabled")
        and float(summary["validation"]["validation_rms_mm"]) > maximum_validation_rms_mm
    ):
        quality_failures.append(
            "validation_rms_mm="
            f"{summary['validation']['validation_rms_mm']:.6f} > "
            f"{maximum_validation_rms_mm:.6f}"
        )
    scale_error = abs(float(summary["similarity_scale_diagnostic"]) - 1.0)
    if scale_error > maximum_scale_error:
        quality_failures.append(
            f"similarity_scale_error={scale_error:.6f} > {maximum_scale_error:.6f}"
        )
    summary["quality"] = {
        "pass": not quality_failures,
        "failures": quality_failures,
        "inlier_fraction": inlier_fraction,
        "similarity_scale_error": scale_error,
        "acceptance": {
            "minimum_usable_points": minimum_usable_points,
            "minimum_inlier_fraction": minimum_inlier_fraction,
            "maximum_fit_rms_mm": maximum_fit_rms_mm,
            "maximum_fit_max_mm": maximum_fit_max_mm,
            "maximum_validation_rms_mm": maximum_validation_rms_mm,
            "maximum_similarity_scale_error": maximum_scale_error,
        },
    }
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    rotation_text = np.array2string(
        np.asarray(result["rotation"], dtype=float),
        precision=10,
        suppress_small=False,
    )
    translation_text = np.array2string(
        np.asarray(result["translation_mm"], dtype=float),
        precision=10,
        suppress_small=False,
    )
    pat_origin_text = np.array2string(
        pat_origin_left_camera,
        precision=10,
        suppress_small=False,
    )
    validation_text = (
        f"{summary['validation']['validation_rms_mm']:.9f} mm"
        if summary["validation"].get("enabled")
        else "disabled"
    )
    text_path.write_text(
        "\n".join(
            [
                "Camera-to-PAT rigid registration",
                "",
                f"Session: {session_dir}",
                f"Stereo calibration: {calibration}",
                (
                    "Ossila stage hardware readback at registration: "
                    f"{summary['stage_hardware_readback_mm']} mm"
                ),
                "",
                "Formula:",
                "  p_pat_mm = R_camera_to_pat @ p_left_camera_mm + t_camera_to_pat_mm",
                "",
                "R_camera_to_pat:",
                rotation_text,
                "",
                "t_camera_to_pat_mm:",
                translation_text,
                "",
                "PAT origin in left-camera coordinates (self-estimated):",
                pat_origin_text,
                f"PAT origin camera Z estimate: {pat_origin_left_camera[2]:.9f} mm",
                f"PAT origin optical-centre slant range estimate: {pat_origin_slant_range:.9f} mm",
                (
                    "PAT origin to stereo optical-centre baseline perpendicular "
                    f"range estimate (D0): {pat_origin_baseline_range:.9f} mm"
                ),
                (
                    "Perpendicular foot from left optical centre along baseline: "
                    f"{float(pat_origin_baseline['foot_from_left_optical_center_mm']):.9f} mm"
                ),
                "Role: metric stereo estimate; not independent absolute-distance truth.",
                "",
                f"Usable points: {summary['usable_points']}",
                f"Fit inliers: {summary['fit_inliers']}",
                f"Fit RMS: {summary['fit_rms_mm']:.9f} mm",
                f"Fit max: {summary['fit_max_mm']:.9f} mm",
                f"Validation RMS: {validation_text}",
                f"Similarity scale diagnostic: {summary['similarity_scale_diagnostic']:.12f}",
                f"Quality pass: {summary['quality']['pass']}",
                (
                    "Quality failures: "
                    + ("; ".join(summary["quality"]["failures"]) or "none")
                ),
                f"Rejected point IDs: {', '.join(summary['rejected_point_ids']) or 'none'}",
                "",
                "Camera frame: left OpenCV camera coordinates; X right, Y down, Z forward.",
                "PAT frame: AcousTools target coordinates from session_config.json.",
                "Units: millimetres.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    write_registration_plot(
        plot_path,
        target,
        np.asarray(result["transformed_mm"], dtype=float),
        inlier,
    )
    manifest["registration"] = summary
    manifest["status"] = (
        "registered"
        if summary["quality"]["pass"]
        else "registered_quality_failed"
    )
    atomic_write_json(manifest_path, manifest)

    print(f"Transform NPZ: {npz_path}")
    print(f"Transform JSON: {json_path}")
    print(f"Transform summary: {text_path}")
    print(f"Correspondences: {csv_path}")
    print(f"Fit RMS: {summary['fit_rms_mm']:.6f} mm")
    if summary["validation"].get("enabled"):
        print(f"Validation RMS: {summary['validation']['validation_rms_mm']:.6f} mm")
    print(f"Scale diagnostic: {summary['similarity_scale_diagnostic']:.9f}")
    print(f"Quality pass: {summary['quality']['pass']}")
    for failure in summary["quality"]["failures"]:
        print(f"[QUALITY][FAIL] {failure}")
    print(
        "Interactive view: "
        f"{sys.executable} {SCRIPT_DIR / 'stereo_3d_viewer.py'} --input {npz_path}"
    )
    return 0 if summary["quality"]["pass"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
