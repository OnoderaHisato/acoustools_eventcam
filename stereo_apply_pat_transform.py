#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Apply a camera-to-PAT rigid registration to reconstructed stereo points."""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path

import numpy as np


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transform stereo 3D points from the left-camera frame into PAT coordinates.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input_npz", type=Path, help="stereo_3d_points.npz.")
    parser.add_argument("--transform", type=Path, required=True, help="camera_to_pat_transform.npz.")
    parser.add_argument("--output", type=Path, default=None, help="Output NPZ. Defaults to <input>_pat.npz.")
    parser.add_argument("--relative-start", action="store_true", help="Also save PAT displacement relative to the initial stable median.")
    parser.add_argument("--start-window-sec", type=float, default=0.05, help="Initial time span used by --relative-start.")
    parser.add_argument(
        "--allow-calibration-mismatch",
        action="store_true",
        help="Override the check that the 3D points and PAT transform use the same stereo calibration.",
    )
    return parser.parse_args()


def transform_points(points: np.ndarray, rotation: np.ndarray, translation: np.ndarray) -> np.ndarray:
    output = np.full_like(points, np.nan, dtype=np.float64)
    finite = np.all(np.isfinite(points), axis=1)
    output[finite] = (rotation @ points[finite].T).T + translation
    return output


def npz_text(data: np.lib.npyio.NpzFile, key: str) -> str:
    if key not in data.files:
        return ""
    value = np.asarray(data[key])
    return str(value.reshape(-1)[0]) if value.size else ""


def write_csv(
    path: Path,
    t_sec: np.ndarray,
    camera: np.ndarray,
    pat: np.ndarray,
    valid: np.ndarray,
    relative: np.ndarray | None,
) -> None:
    fieldnames = [
        "index",
        "t_sec",
        "camera_x_mm",
        "camera_y_mm",
        "camera_z_mm",
        "pat_x_mm",
        "pat_y_mm",
        "pat_z_mm",
        "valid",
    ]
    if relative is not None:
        fieldnames.extend(["pat_dx_mm", "pat_dy_mm", "pat_dz_mm"])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for index in range(camera.shape[0]):
            is_valid = bool(valid[index])
            row: dict[str, object] = {
                "index": index,
                "t_sec": f"{float(t_sec[index]):.9f}" if np.isfinite(t_sec[index]) else "",
                "camera_x_mm": f"{camera[index, 0]:.9f}" if is_valid else "",
                "camera_y_mm": f"{camera[index, 1]:.9f}" if is_valid else "",
                "camera_z_mm": f"{camera[index, 2]:.9f}" if is_valid else "",
                "pat_x_mm": f"{pat[index, 0]:.9f}" if is_valid else "",
                "pat_y_mm": f"{pat[index, 1]:.9f}" if is_valid else "",
                "pat_z_mm": f"{pat[index, 2]:.9f}" if is_valid else "",
                "valid": int(is_valid),
            }
            if relative is not None:
                row.update(
                    {
                        "pat_dx_mm": f"{relative[index, 0]:.9f}" if is_valid else "",
                        "pat_dy_mm": f"{relative[index, 1]:.9f}" if is_valid else "",
                        "pat_dz_mm": f"{relative[index, 2]:.9f}" if is_valid else "",
                    }
                )
            writer.writerow(row)


def main() -> int:
    args = parse_args()
    input_path = args.input_npz.resolve()
    transform_path = args.transform.resolve()
    if not input_path.exists():
        raise SystemExit(f"Input NPZ not found: {input_path}")
    if not transform_path.exists():
        raise SystemExit(f"Transform NPZ not found: {transform_path}")

    source = np.load(input_path, allow_pickle=False)
    registration = np.load(transform_path, allow_pickle=False)
    if "points_left_cam_mm" not in source:
        raise SystemExit("Input NPZ does not contain points_left_cam_mm.")
    required = {"R_camera_to_pat", "t_camera_to_pat_mm"}
    missing = sorted(required.difference(registration.files))
    if missing:
        raise SystemExit(f"Transform NPZ is missing keys: {', '.join(missing)}")

    camera = np.asarray(source["points_left_cam_mm"], dtype=np.float64)
    rotation = np.asarray(registration["R_camera_to_pat"], dtype=np.float64)
    translation = np.asarray(registration["t_camera_to_pat_mm"], dtype=np.float64).reshape(3)
    if camera.ndim != 2 or camera.shape[1] != 3:
        raise SystemExit(f"points_left_cam_mm must have shape (N, 3); got {camera.shape}.")
    if rotation.shape != (3, 3):
        raise SystemExit(f"R_camera_to_pat must have shape (3, 3); got {rotation.shape}.")
    if not np.all(np.isfinite(rotation)) or not np.all(np.isfinite(translation)):
        raise SystemExit("Camera-to-PAT transform contains non-finite values.")
    orthogonality_error = float(np.linalg.norm(rotation.T @ rotation - np.eye(3), ord="fro"))
    determinant = float(np.linalg.det(rotation))
    if orthogonality_error > 1e-6 or not np.isclose(
        determinant,
        1.0,
        rtol=0.0,
        atol=1e-6,
    ):
        raise SystemExit(
            "R_camera_to_pat is not a proper rigid rotation: "
            f"orthogonality_error={orthogonality_error:.6g}, det={determinant:.9g}."
        )

    source_hash = npz_text(source, "stereo_calibration_sha256")
    transform_hash = npz_text(registration, "stereo_calibration_sha256")
    source_calibration = npz_text(source, "stereo_calibration")
    transform_calibration = npz_text(registration, "stereo_calibration")
    calibration_matches = True
    comparison = "not available"
    if source_hash and transform_hash:
        calibration_matches = source_hash == transform_hash
        comparison = "sha256"
    elif source_calibration and transform_calibration:
        calibration_matches = (
            os.path.normcase(str(Path(source_calibration).resolve()))
            == os.path.normcase(str(Path(transform_calibration).resolve()))
        )
        comparison = "resolved path"
    if not calibration_matches and not args.allow_calibration_mismatch:
        raise SystemExit(
            "Stereo-calibration mismatch between the 3D points and PAT transform "
            f"({comparison}). Reprocess with one calibration, or use "
            "--allow-calibration-mismatch only for a deliberate diagnostic."
        )
    valid = np.asarray(source["valid"], dtype=bool) if "valid" in source else np.all(np.isfinite(camera), axis=1)
    t_sec = np.asarray(source["t_sec"], dtype=np.float64) if "t_sec" in source else np.arange(camera.shape[0], dtype=float)
    pat = transform_points(camera, rotation, translation)
    valid &= np.all(np.isfinite(pat), axis=1)

    relative: np.ndarray | None = None
    start_origin: np.ndarray | None = None
    if args.relative_start:
        finite_t = t_sec[valid & np.isfinite(t_sec)]
        if finite_t.size:
            start_t = float(np.min(finite_t))
            start_mask = valid & (t_sec <= start_t + max(0.0, float(args.start_window_sec)))
        else:
            start_mask = valid.copy()
        if not start_mask.any():
            raise SystemExit("No valid points are available for the relative-start origin.")
        start_origin = np.nanmedian(pat[start_mask], axis=0)
        relative = pat - start_origin

    output_path = (
        args.output.resolve()
        if args.output is not None
        else input_path.with_name(f"{input_path.stem}_pat.npz")
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: source[key] for key in source.files}
    payload.update(
        {
            "points_pat_mm": pat,
            "valid": valid,
            "camera_to_pat_transform": np.asarray(str(transform_path)),
            "R_camera_to_pat": rotation,
            "t_camera_to_pat_mm": translation,
            "coordinate_convention_pat": np.asarray(
                "AcousTools PAT frame, units=mm; p_pat = R_camera_to_pat @ p_left_camera + t_camera_to_pat_mm"
            ),
        }
    )
    if relative is not None and start_origin is not None:
        payload["points_pat_relative_mm"] = relative
        payload["pat_relative_origin_mm"] = start_origin
    np.savez_compressed(output_path, **payload)
    csv_path = output_path.with_suffix(".csv")
    write_csv(csv_path, t_sec, camera, pat, valid, relative)

    valid_pat = pat[valid]
    summary = {
        "input_npz": str(input_path),
        "transform_npz": str(transform_path),
        "output_npz": str(output_path),
        "output_csv": str(csv_path),
        "valid_points": int(valid.sum()),
        "coordinate_formula": "p_pat_mm = R_camera_to_pat @ p_left_camera_mm + t_camera_to_pat_mm",
        "pat_bounds_mm": {
            "min": np.min(valid_pat, axis=0).tolist() if valid_pat.size else [],
            "max": np.max(valid_pat, axis=0).tolist() if valid_pat.size else [],
        },
        "relative_start_origin_mm": start_origin.tolist() if start_origin is not None else None,
        "rotation_orthogonality_error": orthogonality_error,
        "rotation_determinant": determinant,
        "stereo_calibration_match": calibration_matches,
        "stereo_calibration_comparison": comparison,
    }
    summary_path = output_path.with_name(f"{output_path.stem}_summary.json")
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"PAT NPZ: {output_path}")
    print(f"PAT CSV: {csv_path}")
    print(f"Summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
