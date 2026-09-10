#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compare a triangulated stereo particle trajectory with an AcousTools ideal log.

The ideal log is expressed in the AcousTools/PAT frame and metres. Stereo
triangulation is expressed in the left-camera frame and millimetres. A fixed
camera-to-PAT registration is therefore preferred. When it is unavailable,
``--spatial-alignment fit-run`` estimates a rigid camera-to-PAT transform from
the measured run itself; that mode evaluates trajectory shape/repeatability,
not absolute camera-to-PAT placement accuracy.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare stereo 3D measurements with a target_x/y/z ideal log.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("stereo_npz", type=Path, help="stereo_3d_points.npz from stereo triangulation.")
    parser.add_argument("ideal_log", type=Path, help="AcousTools *_ideal_log.csv (positions in metres).")
    parser.add_argument("--output-dir", type=Path, default=None, help="Output directory.")
    parser.add_argument("--camera-to-pat-transform", type=Path, default=None, help="Fixed camera_to_pat_transform.npz.")
    parser.add_argument(
        "--spatial-alignment",
        choices=["auto", "fixed", "fit-run"],
        default="auto",
        help="Use a fixed registration or estimate a rigid transform from this trajectory.",
    )
    parser.add_argument(
        "--time-offset-sec",
        type=float,
        default=0.0,
        help="Measured-recording time at which ideal-log t=0 occurs.",
    )
    parser.add_argument(
        "--auto-time-search-sec",
        type=float,
        default=0.02,
        help="Refine time offset within +/- this range. 0 disables refinement.",
    )
    parser.add_argument("--auto-time-step-sec", type=float, default=0.0001, help="Time-offset search step.")
    parser.add_argument("--fit-trim-fraction", type=float, default=0.05, help="Fraction trimmed from each residual tail during run-fit refinement.")
    parser.add_argument("--max-error-mm", type=float, default=0.0, help="Exclude 3D errors above this from reported metrics. 0 disables.")
    parser.add_argument("--allow-calibration-mismatch", action="store_true", help="Allow fixed transform and stereo points made with different calibrations.")
    return parser.parse_args()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def npz_text(data: np.lib.npyio.NpzFile, key: str) -> str:
    if key not in data.files:
        return ""
    value = np.asarray(data[key])
    return str(value.reshape(-1)[0]) if value.size else ""


def load_ideal_log(path: Path) -> tuple[np.ndarray, np.ndarray]:
    data = np.genfromtxt(path, delimiter=",", names=True, dtype=float, encoding="utf-8")
    data = np.atleast_1d(data)
    names = set(data.dtype.names or ())
    required = {"time", "target_x", "target_y", "target_z"}
    if data.size == 0 or not required.issubset(names):
        raise SystemExit(f"Ideal log must contain {sorted(required)} and at least one row: {path}")
    t = np.asarray(data["time"], dtype=np.float64)
    xyz_mm = np.column_stack([data["target_x"], data["target_y"], data["target_z"]]).astype(np.float64) * 1000.0
    finite = np.isfinite(t) & np.all(np.isfinite(xyz_mm), axis=1)
    t, xyz_mm = t[finite], xyz_mm[finite]
    order = np.argsort(t, kind="stable")
    t, xyz_mm = t[order], xyz_mm[order]
    unique = np.r_[True, np.diff(t) > 0.0]
    t, xyz_mm = t[unique], xyz_mm[unique]
    if len(t) < 2:
        raise SystemExit("Ideal log requires at least two distinct finite timestamps.")
    return t, xyz_mm


def load_stereo(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.lib.npyio.NpzFile]:
    data = np.load(path, allow_pickle=False)
    if "points_left_cam_mm" not in data or "t_sec" not in data:
        raise SystemExit("Stereo NPZ must contain points_left_cam_mm and t_sec.")
    points = np.asarray(data["points_left_cam_mm"], dtype=np.float64)
    t = np.asarray(data["t_sec"], dtype=np.float64)
    valid = np.asarray(data["valid"], dtype=bool) if "valid" in data else np.ones(len(t), dtype=bool)
    if points.shape != (len(t), 3) or valid.shape != (len(t),):
        raise SystemExit(f"Invalid stereo array shapes: t={t.shape}, points={points.shape}, valid={valid.shape}")
    valid &= np.isfinite(t) & np.all(np.isfinite(points), axis=1)
    return t, points, valid, data


def interpolate_ideal(ideal_t: np.ndarray, ideal_xyz: np.ndarray, query_t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    supported = (query_t >= ideal_t[0]) & (query_t <= ideal_t[-1]) & np.isfinite(query_t)
    output = np.full((len(query_t), 3), np.nan, dtype=np.float64)
    for axis in range(3):
        output[supported, axis] = np.interp(query_t[supported], ideal_t, ideal_xyz[:, axis])
    return output, supported


def fit_rigid(source: np.ndarray, target: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if source.shape != target.shape or source.ndim != 2 or source.shape[1] != 3 or len(source) < 3:
        raise ValueError("Rigid fit requires matching Nx3 arrays with at least three points.")
    source_mean = np.mean(source, axis=0)
    target_mean = np.mean(target, axis=0)
    covariance = (source - source_mean).T @ (target - target_mean)
    u, _, vt = np.linalg.svd(covariance)
    rotation = vt.T @ u.T
    if np.linalg.det(rotation) < 0.0:
        vt[-1, :] *= -1.0
        rotation = vt.T @ u.T
    translation = target_mean - rotation @ source_mean
    return rotation, translation


def transform_points(points: np.ndarray, rotation: np.ndarray, translation: np.ndarray) -> np.ndarray:
    return (rotation @ points.T).T + translation.reshape(1, 3)


def trimmed_rigid_fit(source: np.ndarray, target: np.ndarray, trim_fraction: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    keep = np.ones(len(source), dtype=bool)
    rotation, translation = fit_rigid(source, target)
    trim_fraction = max(0.0, min(float(trim_fraction), 0.45))
    for _ in range(3):
        residual = np.linalg.norm(transform_points(source, rotation, translation) - target, axis=1)
        if trim_fraction <= 0.0:
            break
        threshold = float(np.quantile(residual, 1.0 - trim_fraction))
        new_keep = residual <= threshold
        if new_keep.sum() < 3 or np.array_equal(new_keep, keep):
            break
        keep = new_keep
        rotation, translation = fit_rigid(source[keep], target[keep])
    return rotation, translation, keep


def validate_fixed_transform(
    transform_path: Path,
    stereo_data: np.lib.npyio.NpzFile,
    allow_mismatch: bool,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    registration = np.load(transform_path, allow_pickle=False)
    required = {"R_camera_to_pat", "t_camera_to_pat_mm"}
    missing = required.difference(registration.files)
    if missing:
        raise SystemExit(f"Camera-to-PAT transform missing keys: {sorted(missing)}")
    rotation = np.asarray(registration["R_camera_to_pat"], dtype=np.float64)
    translation = np.asarray(registration["t_camera_to_pat_mm"], dtype=np.float64).reshape(3)
    if rotation.shape != (3, 3):
        raise SystemExit(f"R_camera_to_pat must be 3x3; got {rotation.shape}")
    determinant = float(np.linalg.det(rotation))
    orthogonality = float(np.linalg.norm(rotation.T @ rotation - np.eye(3), ord="fro"))
    if abs(determinant - 1.0) > 1e-6 or orthogonality > 1e-6:
        raise SystemExit("Camera-to-PAT transform rotation is not a proper rigid rotation.")
    points_hash = npz_text(stereo_data, "stereo_calibration_sha256")
    transform_hash = npz_text(registration, "stereo_calibration_sha256")
    matches = not (points_hash and transform_hash) or points_hash == transform_hash
    if not matches and not allow_mismatch:
        raise SystemExit(
            "Stereo calibration hash differs from the camera-to-PAT registration. "
            "Create a new registration for this calibration or explicitly pass --allow-calibration-mismatch."
        )
    return rotation, translation, {
        "transform_path": str(transform_path),
        "transform_sha256": file_sha256(transform_path),
        "calibration_hash_match": matches,
        "rotation_determinant": determinant,
        "rotation_orthogonality_error": orthogonality,
    }


def candidate_score(
    measured_t: np.ndarray,
    camera_points: np.ndarray,
    ideal_t: np.ndarray,
    ideal_xyz: np.ndarray,
    offset: float,
    mode: str,
    fixed_rotation: np.ndarray | None,
    fixed_translation: np.ndarray | None,
    trim_fraction: float,
) -> tuple[float, np.ndarray, np.ndarray, int]:
    target, supported = interpolate_ideal(ideal_t, ideal_xyz, measured_t - offset)
    if supported.sum() < 3:
        return math.inf, np.eye(3), np.zeros(3), int(supported.sum())
    source = camera_points[supported]
    target_supported = target[supported]
    if mode == "fit-run":
        rotation, translation, fit_keep = trimmed_rigid_fit(source, target_supported, trim_fraction)
        residual = transform_points(source, rotation, translation) - target_supported
        residual = residual[fit_keep]
    else:
        assert fixed_rotation is not None and fixed_translation is not None
        rotation, translation = fixed_rotation, fixed_translation
        residual = transform_points(source, rotation, translation) - target_supported
        if trim_fraction > 0.0 and len(residual) >= 10:
            norm = np.linalg.norm(residual, axis=1)
            residual = residual[norm <= np.quantile(norm, 1.0 - min(trim_fraction, 0.45))]
    score = float(np.sqrt(np.mean(np.sum(residual * residual, axis=1)))) if len(residual) else math.inf
    return score, rotation, translation, int(supported.sum())


def metric(values: np.ndarray) -> dict[str, float]:
    values = np.asarray(values, dtype=np.float64)
    return {
        "mean": float(np.mean(values)),
        "std": float(np.std(values)),
        "mae": float(np.mean(np.abs(values))),
        "rmse": float(np.sqrt(np.mean(values * values))),
        "p95_abs": float(np.quantile(np.abs(values), 0.95)),
        "max_abs": float(np.max(np.abs(values))),
    }


def save_csv(
    path: Path,
    t: np.ndarray,
    ideal_time: np.ndarray,
    measured: np.ndarray,
    target: np.ndarray,
    error: np.ndarray,
    included: np.ndarray,
) -> None:
    fields = [
        "t_recording_sec", "t_ideal_sec", "measured_x_mm", "measured_y_mm", "measured_z_mm",
        "ideal_x_mm", "ideal_y_mm", "ideal_z_mm", "error_x_mm", "error_y_mm", "error_z_mm",
        "error_norm_mm", "included_in_metrics",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index in range(len(t)):
            writer.writerow({
                "t_recording_sec": f"{t[index]:.9f}",
                "t_ideal_sec": f"{ideal_time[index]:.9f}",
                "measured_x_mm": f"{measured[index, 0]:.9f}",
                "measured_y_mm": f"{measured[index, 1]:.9f}",
                "measured_z_mm": f"{measured[index, 2]:.9f}",
                "ideal_x_mm": f"{target[index, 0]:.9f}",
                "ideal_y_mm": f"{target[index, 1]:.9f}",
                "ideal_z_mm": f"{target[index, 2]:.9f}",
                "error_x_mm": f"{error[index, 0]:.9f}",
                "error_y_mm": f"{error[index, 1]:.9f}",
                "error_z_mm": f"{error[index, 2]:.9f}",
                "error_norm_mm": f"{np.linalg.norm(error[index]):.9f}",
                "included_in_metrics": int(included[index]),
            })


def save_plots(
    output_dir: Path,
    t: np.ndarray,
    measured: np.ndarray,
    target: np.ndarray,
    error: np.ndarray,
    included: np.ndarray,
    mode_label: str,
) -> None:
    labels = ("X", "Y", "Z")
    colors = ("tab:blue", "tab:orange", "tab:green")
    fig, axes = plt.subplots(3, 1, figsize=(11, 8), sharex=True)
    for axis, label, color in zip(axes, labels, colors):
        index = labels.index(label)
        axis.plot(t, target[:, index], "--", color="black", linewidth=1.0, label="ideal")
        axis.plot(t, measured[:, index], color=color, linewidth=0.9, label="measured")
        axis.set_ylabel(f"{label} [mm]")
        axis.grid(True, linestyle=":", alpha=0.45)
        axis.legend(loc="upper right", fontsize=8)
    axes[-1].set_xlabel("recording time [s]")
    fig.suptitle(f"Stereo 3D measurement vs ideal ({mode_label})")
    fig.tight_layout()
    fig.savefig(output_dir / "ideal_comparison_time_xyz.png", dpi=180)
    plt.close(fig)

    fig, axes = plt.subplots(4, 1, figsize=(11, 9), sharex=True)
    for index, (label, color) in enumerate(zip(labels, colors)):
        axes[index].plot(t, error[:, index], color=color, linewidth=0.8)
        axes[index].axhline(0.0, color="black", linewidth=0.6)
        axes[index].set_ylabel(f"e{label} [mm]")
        axes[index].grid(True, linestyle=":", alpha=0.45)
    norm = np.linalg.norm(error, axis=1)
    axes[3].plot(t, norm, color="tab:red", linewidth=0.8)
    if not np.all(included):
        axes[3].scatter(t[~included], norm[~included], s=5, color="gray", label="excluded")
        axes[3].legend(fontsize=8)
    axes[3].set_ylabel("|e| [mm]")
    axes[3].set_xlabel("recording time [s]")
    axes[3].grid(True, linestyle=":", alpha=0.45)
    fig.suptitle("3D trajectory error")
    fig.tight_layout()
    fig.savefig(output_dir / "ideal_comparison_errors.png", dpi=180)
    plt.close(fig)

    fig = plt.figure(figsize=(14, 9), constrained_layout=True)
    grid = fig.add_gridspec(2, 2)
    ax3d = fig.add_subplot(grid[0, 0], projection="3d")
    ax3d.plot(*target.T, "--", color="black", linewidth=1.0, label="ideal")
    ax3d.plot(*measured.T, color="tab:cyan", linewidth=0.8, label="measured")
    ax3d.set_xlabel("PAT X [mm]")
    ax3d.set_ylabel("PAT Y [mm]")
    ax3d.set_zlabel("PAT Z [mm]")
    ax3d.legend(fontsize=8)
    for subplot, (a, b, title) in zip(
        (grid[0, 1], grid[1, 0], grid[1, 1]),
        ((0, 1, "XY"), (0, 2, "XZ"), (1, 2, "YZ")),
    ):
        axis = fig.add_subplot(subplot)
        axis.plot(target[:, a], target[:, b], "--", color="black", linewidth=1.0, label="ideal")
        axis.plot(measured[:, a], measured[:, b], color="tab:cyan", linewidth=0.8, label="measured")
        axis.set_xlabel(f"{labels[a]} [mm]")
        axis.set_ylabel(f"{labels[b]} [mm]")
        axis.set_title(f"{title} projection")
        axis.set_aspect("equal", adjustable="datalim")
        axis.grid(True, linestyle=":", alpha=0.45)
    fig.suptitle(f"3D trajectory comparison ({mode_label})")
    fig.savefig(output_dir / "ideal_comparison_trajectory_3d.png", dpi=180)
    plt.close(fig)


def main() -> int:
    args = parse_args()
    stereo_path = args.stereo_npz.resolve()
    ideal_path = args.ideal_log.resolve()
    if not stereo_path.exists() or not ideal_path.exists():
        raise SystemExit("Stereo NPZ and ideal log must both exist.")
    measured_t_all, camera_all, valid_all, stereo_data = load_stereo(stereo_path)
    ideal_t, ideal_xyz = load_ideal_log(ideal_path)
    measured_t = measured_t_all[valid_all]
    camera = camera_all[valid_all]
    if len(camera) < 3:
        raise SystemExit("At least three valid stereo points are required.")

    mode = str(args.spatial_alignment)
    if mode == "auto":
        mode = "fixed" if args.camera_to_pat_transform is not None else "fit-run"
    fixed_rotation = fixed_translation = None
    transform_meta: dict[str, Any] = {}
    if mode == "fixed":
        if args.camera_to_pat_transform is None:
            raise SystemExit("--spatial-alignment fixed requires --camera-to-pat-transform.")
        transform_path = args.camera_to_pat_transform.resolve()
        fixed_rotation, fixed_translation, transform_meta = validate_fixed_transform(
            transform_path, stereo_data, bool(args.allow_calibration_mismatch)
        )
    elif args.camera_to_pat_transform is not None:
        print("[WARN] --camera-to-pat-transform is ignored in fit-run mode.")

    initial_offset = float(args.time_offset_sec)
    search = max(0.0, float(args.auto_time_search_sec))
    step = float(args.auto_time_step_sec)
    if search > 0.0 and step <= 0.0:
        raise SystemExit("--auto-time-step-sec must be positive when auto search is enabled.")
    offsets = np.asarray([initial_offset])
    if search > 0.0:
        count = max(1, int(math.ceil(search / step)))
        offsets = initial_offset + np.arange(-count, count + 1, dtype=float) * step
    best: tuple[float, float, np.ndarray, np.ndarray, int] | None = None
    # Limit the search fit to a representative subset while retaining all data for final metrics.
    stride = max(1, int(math.ceil(len(camera) / 5000)))
    for offset in offsets:
        score, rotation, translation, support_count = candidate_score(
            measured_t[::stride], camera[::stride], ideal_t, ideal_xyz, float(offset), mode,
            fixed_rotation, fixed_translation, float(args.fit_trim_fraction),
        )
        candidate = (score, float(offset), rotation, translation, support_count)
        if best is None or candidate[0] < best[0]:
            best = candidate
    assert best is not None
    _, resolved_offset, rotation, translation, _ = best

    ideal_query_t = measured_t - resolved_offset
    target_all, support = interpolate_ideal(ideal_t, ideal_xyz, ideal_query_t)
    if support.sum() < 3:
        raise SystemExit("Fewer than three stereo points overlap the ideal-log time range.")
    if mode == "fit-run":
        rotation, translation, fit_keep = trimmed_rigid_fit(
            camera[support], target_all[support], float(args.fit_trim_fraction)
        )
    else:
        fit_keep = np.ones(int(support.sum()), dtype=bool)
    measured_pat_all = transform_points(camera, rotation, translation)
    t = measured_t[support]
    ideal_query_t = ideal_query_t[support]
    measured_pat = measured_pat_all[support]
    target = target_all[support]
    error = measured_pat - target
    error_norm = np.linalg.norm(error, axis=1)
    included = np.ones(len(error), dtype=bool)
    if float(args.max_error_mm) > 0.0:
        included &= error_norm <= float(args.max_error_mm)
    if not included.any():
        raise SystemExit("No points remain for metrics after --max-error-mm filtering.")

    output_dir = (args.output_dir or stereo_path.parent / "ideal_comparison_3d").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "stereo_ideal_comparison.csv"
    npz_path = output_dir / "stereo_ideal_comparison.npz"
    summary_path = output_dir / "stereo_ideal_comparison_summary.json"
    save_csv(csv_path, t, ideal_query_t, measured_pat, target, error, included)
    np.savez_compressed(
        npz_path,
        t_recording_sec=t,
        t_ideal_sec=ideal_query_t,
        measured_pat_mm=measured_pat,
        ideal_pat_mm=target,
        error_mm=error,
        included_in_metrics=included,
        resolved_time_offset_sec=np.asarray(resolved_offset),
        R_camera_to_pat=rotation,
        t_camera_to_pat_mm=translation,
        spatial_alignment=np.asarray(mode),
        source_stereo_npz=np.asarray(str(stereo_path)),
        source_ideal_log=np.asarray(str(ideal_path)),
    )
    metrics = {
        axis: metric(error[included, index]) for index, axis in enumerate(("x_mm", "y_mm", "z_mm"))
    }
    metrics["norm_mm"] = metric(error_norm[included])
    summary: dict[str, Any] = {
        "schema_version": 1,
        "stereo_npz": str(stereo_path),
        "stereo_npz_sha256": file_sha256(stereo_path),
        "ideal_log": str(ideal_path),
        "ideal_log_sha256": file_sha256(ideal_path),
        "output_csv": str(csv_path),
        "output_npz": str(npz_path),
        "spatial_alignment": mode,
        "interpretation": (
            "absolute PAT-frame comparison using an independent fixed camera-to-PAT registration"
            if mode == "fixed"
            else "per-run rigid-fit comparison; evaluates trajectory shape but not absolute pose"
        ),
        "time_offset_initial_sec": initial_offset,
        "time_offset_resolved_sec": resolved_offset,
        "time_search_half_range_sec": search,
        "time_search_step_sec": step,
        "overlap_points": int(len(error)),
        "metric_points": int(included.sum()),
        "excluded_by_max_error": int((~included).sum()),
        "max_error_mm_filter": float(args.max_error_mm),
        "metrics": metrics,
        "R_camera_to_pat": rotation.tolist(),
        "t_camera_to_pat_mm": translation.tolist(),
        "fit_points_retained": int(fit_keep.sum()),
        "transform": transform_meta,
    }
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    mode_label = "fixed registration" if mode == "fixed" else "per-run rigid fit"
    save_plots(output_dir, t, measured_pat, target, error, included, mode_label)
    print(f"Alignment: {mode_label}")
    print(f"Resolved ideal start in recording: {resolved_offset:.9f} s")
    print(f"3D error RMSE: {metrics['norm_mm']['rmse']:.6f} mm")
    print(f"Comparison CSV: {csv_path}")
    print(f"Summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
