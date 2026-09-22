#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Triangulate stereo 3D points from left/right event tracking CSV files.

Inputs are normally the ``event_centres_interp.csv`` files produced by
``eventcam_npz_track.py`` for the left and right event-camera NPZs.

The output coordinate system is the left OpenCV camera coordinate system:
  X: image-right direction
  Y: image-down direction
  Z: camera-forward direction
Units are millimetres because the stereo calibration T vector is in mm.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Triangulate 3D trajectory from paired 2D event-camera tracking CSVs.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--left-track", type=Path, required=True, help="Left event_centres_interp.csv.")
    parser.add_argument("--right-track", type=Path, required=True, help="Right event_centres_interp.csv.")
    parser.add_argument(
        "--stereo-calibration",
        type=Path,
        required=True,
        help="Stereo calibration NPZ.",
    )
    parser.add_argument("--output-dir", default="", help="Output folder. Empty uses left-track parent/stereo_3d.")
    parser.add_argument("--right-time-offset-sec", type=float, default=0.0, help="Add this offset to right t_sec before pairing.")
    parser.add_argument("--max-time-gap-sec", type=float, default=0.001, help="Reject points farther than this from right track support.")
    parser.add_argument("--min-z-mm", type=float, default=0.0, help="Reject points with Z below this. 0 disables.")
    parser.add_argument("--max-z-mm", type=float, default=0.0, help="Reject points with Z above this. 0 disables.")
    parser.add_argument(
        "--max-reprojection-error-px",
        type=float,
        default=0.0,
        help="Reject a pair when either camera reprojection error exceeds this. 0 disables.",
    )
    parser.add_argument("--output-name", default="stereo_3d_points", help="Output basename without extension.")
    return parser.parse_args()


def load_track_csv(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if not path.exists():
        raise SystemExit(f"Track CSV not found: {path}")
    data = np.genfromtxt(path, delimiter=",", names=True, dtype=float, encoding="utf-8")
    if data.size == 0:
        raise SystemExit(f"Track CSV is empty: {path}")
    data = np.atleast_1d(data)
    names = set(data.dtype.names or [])
    required = {"t_sec", "x_px", "y_px"}
    missing = required - names
    if missing:
        raise SystemExit(f"{path} missing columns: {sorted(missing)}")
    return (
        np.asarray(data["t_sec"], dtype=float),
        np.asarray(data["x_px"], dtype=float),
        np.asarray(data["y_px"], dtype=float),
    )


def finite_interp_with_support(
    t_src: np.ndarray,
    y_src: np.ndarray,
    t_dst: np.ndarray,
    max_gap_sec: float,
) -> tuple[np.ndarray, np.ndarray]:
    mask = np.isfinite(t_src) & np.isfinite(y_src)
    if mask.sum() < 2:
        return np.full_like(t_dst, np.nan, dtype=float), np.zeros_like(t_dst, dtype=bool)
    t_valid = t_src[mask]
    y_valid = y_src[mask]
    order = np.argsort(t_valid, kind="stable")
    t_valid = t_valid[order]
    y_valid = y_valid[order]
    out = np.interp(t_dst, t_valid, y_valid)
    supported = (t_dst >= t_valid[0]) & (t_dst <= t_valid[-1])
    if max_gap_sec > 0:
        nearest_left = np.searchsorted(t_valid, t_dst, side="right") - 1
        nearest_right = nearest_left + 1
        nearest_left = np.clip(nearest_left, 0, len(t_valid) - 1)
        nearest_right = np.clip(nearest_right, 0, len(t_valid) - 1)
        nearest_dt = np.minimum(np.abs(t_dst - t_valid[nearest_left]), np.abs(t_dst - t_valid[nearest_right]))
        supported &= nearest_dt <= float(max_gap_sec)
    out[~supported] = np.nan
    return out, supported


def load_stereo_calibration(path: Path) -> dict[str, np.ndarray]:
    if not path.exists():
        raise SystemExit(f"Stereo calibration NPZ not found: {path}")
    data = np.load(path, allow_pickle=False)
    required = ["left_camera_matrix", "left_dist_coeffs", "right_camera_matrix", "right_dist_coeffs", "R", "T"]
    missing = [key for key in required if key not in data.files]
    if missing:
        raise SystemExit(f"Stereo calibration missing keys: {missing}")
    return {key: np.asarray(data[key], dtype=np.float64) for key in required}


def triangulate_points(
    left_xy: np.ndarray,
    right_xy: np.ndarray,
    calib: dict[str, np.ndarray],
) -> np.ndarray:
    left_k = calib["left_camera_matrix"]
    left_dist = calib["left_dist_coeffs"]
    right_k = calib["right_camera_matrix"]
    right_dist = calib["right_dist_coeffs"]
    R = calib["R"]
    T = calib["T"].reshape(3, 1)

    left_norm = cv2.undistortPoints(left_xy.reshape(-1, 1, 2), left_k, left_dist).reshape(-1, 2)
    right_norm = cv2.undistortPoints(right_xy.reshape(-1, 1, 2), right_k, right_dist).reshape(-1, 2)

    p_left = np.hstack([np.eye(3), np.zeros((3, 1))]).astype(np.float64)
    p_right = np.hstack([R, T]).astype(np.float64)
    points_h = cv2.triangulatePoints(p_left, p_right, left_norm.T, right_norm.T)
    points_3d = (points_h[:3, :] / points_h[3:4, :]).T
    return points_3d.astype(np.float64)


def reprojection_errors(
    points_3d: np.ndarray,
    left_xy: np.ndarray,
    right_xy: np.ndarray,
    calib: dict[str, np.ndarray],
) -> tuple[np.ndarray, np.ndarray]:
    left_projected, _ = cv2.projectPoints(
        points_3d,
        np.zeros(3, dtype=np.float64),
        np.zeros(3, dtype=np.float64),
        calib["left_camera_matrix"],
        calib["left_dist_coeffs"],
    )
    right_rvec, _ = cv2.Rodrigues(calib["R"])
    right_projected, _ = cv2.projectPoints(
        points_3d,
        right_rvec,
        calib["T"].reshape(3),
        calib["right_camera_matrix"],
        calib["right_dist_coeffs"],
    )
    left_error = np.linalg.norm(left_projected.reshape(-1, 2) - left_xy, axis=1)
    right_error = np.linalg.norm(right_projected.reshape(-1, 2) - right_xy, axis=1)
    return left_error, right_error


def write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    left_t, left_x, left_y = load_track_csv(args.left_track)
    right_t, right_x, right_y = load_track_csv(args.right_track)
    right_t_aligned = right_t + float(args.right_time_offset_sec)
    right_x_i, support_x = finite_interp_with_support(right_t_aligned, right_x, left_t, float(args.max_time_gap_sec))
    right_y_i, support_y = finite_interp_with_support(right_t_aligned, right_y, left_t, float(args.max_time_gap_sec))

    valid = (
        np.isfinite(left_t)
        & np.isfinite(left_x)
        & np.isfinite(left_y)
        & np.isfinite(right_x_i)
        & np.isfinite(right_y_i)
        & support_x
        & support_y
    )

    points_3d = np.full((left_t.size, 3), np.nan, dtype=np.float64)
    left_reprojection_error_px = np.full(left_t.size, np.nan, dtype=np.float64)
    right_reprojection_error_px = np.full(left_t.size, np.nan, dtype=np.float64)
    if valid.sum() >= 1:
        left_xy = np.column_stack([left_x[valid], left_y[valid]]).astype(np.float64)
        right_xy = np.column_stack([right_x_i[valid], right_y_i[valid]]).astype(np.float64)
        calibration = load_stereo_calibration(args.stereo_calibration)
        triangulated = triangulate_points(left_xy, right_xy, calibration)
        points_3d[valid] = triangulated
        left_error, right_error = reprojection_errors(
            triangulated, left_xy, right_xy, calibration
        )
        left_reprojection_error_px[valid] = left_error
        right_reprojection_error_px[valid] = right_error

    z_valid = np.isfinite(points_3d[:, 2])
    if args.min_z_mm > 0:
        valid &= points_3d[:, 2] >= float(args.min_z_mm)
    if args.max_z_mm > 0:
        valid &= points_3d[:, 2] <= float(args.max_z_mm)
    if args.max_reprojection_error_px > 0:
        valid &= (
            left_reprojection_error_px <= float(args.max_reprojection_error_px)
        ) & (
            right_reprojection_error_px <= float(args.max_reprojection_error_px)
        )
    valid &= z_valid

    out_dir = Path(args.output_dir) if str(args.output_dir).strip() else (args.left_track.parent / "stereo_3d")
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{args.output_name}.csv"
    npz_path = out_dir / f"{args.output_name}.npz"
    summary_path = out_dir / f"{args.output_name}_summary.json"

    rows: list[dict[str, object]] = []
    for i in range(left_t.size):
        is_valid = bool(valid[i])
        rows.append(
            {
                "index": i,
                "t_sec": f"{left_t[i]:.9f}" if np.isfinite(left_t[i]) else "",
                "left_x_px": f"{left_x[i]:.6f}" if np.isfinite(left_x[i]) else "",
                "left_y_px": f"{left_y[i]:.6f}" if np.isfinite(left_y[i]) else "",
                "right_x_px": f"{right_x_i[i]:.6f}" if np.isfinite(right_x_i[i]) else "",
                "right_y_px": f"{right_y_i[i]:.6f}" if np.isfinite(right_y_i[i]) else "",
                "x_left_cam_mm": f"{points_3d[i, 0]:.6f}" if is_valid else "",
                "y_left_cam_mm": f"{points_3d[i, 1]:.6f}" if is_valid else "",
                "z_left_cam_mm": f"{points_3d[i, 2]:.6f}" if is_valid else "",
                "left_reprojection_error_px": (
                    f"{left_reprojection_error_px[i]:.6f}"
                    if np.isfinite(left_reprojection_error_px[i]) else ""
                ),
                "right_reprojection_error_px": (
                    f"{right_reprojection_error_px[i]:.6f}"
                    if np.isfinite(right_reprojection_error_px[i]) else ""
                ),
                "valid": int(is_valid),
            }
        )
    write_csv(
        csv_path,
        rows,
        [
            "index",
            "t_sec",
            "left_x_px",
            "left_y_px",
            "right_x_px",
            "right_y_px",
            "x_left_cam_mm",
            "y_left_cam_mm",
            "z_left_cam_mm",
            "left_reprojection_error_px",
            "right_reprojection_error_px",
            "valid",
        ],
    )

    np.savez_compressed(
        npz_path,
        t_sec=left_t,
        left_xy_px=np.column_stack([left_x, left_y]),
        right_xy_px=np.column_stack([right_x_i, right_y_i]),
        points_left_cam_mm=points_3d,
        left_reprojection_error_px=left_reprojection_error_px,
        right_reprojection_error_px=right_reprojection_error_px,
        valid=valid,
        stereo_calibration=str(args.stereo_calibration.resolve()),
        stereo_calibration_sha256=np.asarray(
            file_sha256(args.stereo_calibration.resolve())
        ),
        right_time_offset_sec=np.asarray(float(args.right_time_offset_sec), dtype=np.float64),
    )

    valid_points = points_3d[valid]
    summary = {
        "left_track": str(args.left_track.resolve()),
        "right_track": str(args.right_track.resolve()),
        "stereo_calibration": str(args.stereo_calibration.resolve()),
        "output_csv": str(csv_path.resolve()),
        "output_npz": str(npz_path.resolve()),
        "input_points": int(left_t.size),
        "valid_points": int(valid.sum()),
        "right_time_offset_sec": float(args.right_time_offset_sec),
        "max_time_gap_sec": float(args.max_time_gap_sec),
        "max_reprojection_error_px": float(args.max_reprojection_error_px),
        "coordinate_system": "left OpenCV camera coordinates, units=mm; X right, Y down, Z forward",
    }
    if valid_points.size:
        valid_left_reprojection = left_reprojection_error_px[valid]
        valid_right_reprojection = right_reprojection_error_px[valid]
        summary["reprojection_error_px"] = {
            "left_rms": float(np.sqrt(np.mean(valid_left_reprojection ** 2))),
            "left_max": float(np.max(valid_left_reprojection)),
            "right_rms": float(np.sqrt(np.mean(valid_right_reprojection ** 2))),
            "right_max": float(np.max(valid_right_reprojection)),
        }
        summary["x_left_cam_mm"] = {
            "min": float(np.nanmin(valid_points[:, 0])),
            "mean": float(np.nanmean(valid_points[:, 0])),
            "max": float(np.nanmax(valid_points[:, 0])),
        }
        summary["y_left_cam_mm"] = {
            "min": float(np.nanmin(valid_points[:, 1])),
            "mean": float(np.nanmean(valid_points[:, 1])),
            "max": float(np.nanmax(valid_points[:, 1])),
        }
        summary["z_left_cam_mm"] = {
            "min": float(np.nanmin(valid_points[:, 2])),
            "mean": float(np.nanmean(valid_points[:, 2])),
            "max": float(np.nanmax(valid_points[:, 2])),
        }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"Output CSV: {csv_path}")
    print(f"Output NPZ: {npz_path}")
    print(f"Summary: {summary_path}")
    print(f"Valid 3D points: {int(valid.sum())}/{int(left_t.size)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
