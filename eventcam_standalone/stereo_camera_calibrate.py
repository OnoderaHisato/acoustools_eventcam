#!/usr/bin/env python3
"""Stereo calibration from paired checkerboard images and two intrinsic NPZs."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import cv2
import numpy as np


PATTERN_SIZE = (9, 6)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Estimate stereo extrinsics R,T from paired checkerboard images.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--left-images", type=Path, required=True, help="Folder containing left camera calibration images.")
    parser.add_argument("--right-images", type=Path, required=True, help="Folder containing right camera calibration images.")
    parser.add_argument("--left-intrinsics", type=Path, required=True, help="Left single-camera calibration NPZ.")
    parser.add_argument("--right-intrinsics", type=Path, required=True, help="Right single-camera calibration NPZ.")
    parser.add_argument("--left-serial", default="", help="Left camera serial saved in the output NPZ.")
    parser.add_argument("--right-serial", default="", help="Right camera serial saved in the output NPZ.")
    parser.add_argument("--square-mm", type=float, default=0.0, help="Checkerboard square size. 0 reads left intrinsics NPZ.")
    parser.add_argument(
        "--require-square-mm",
        type=float,
        default=0.0,
        help=(
            "Safety lock for a known checkerboard. When positive, calibration "
            "stops unless the selected and both intrinsic square sizes match it."
        ),
    )
    parser.add_argument("--output", type=Path, default=Path("stereo_calibration.npz"), help="Output stereo NPZ.")
    parser.add_argument("--debug-dir", type=Path, default=Path("stereo_calibration_debug"), help="Detection overlay folder.")
    parser.add_argument("--min-pairs", type=int, default=10, help="Minimum usable stereo pairs.")
    parser.add_argument("--pair-by", choices=["name", "sorted"], default="name", help="How left/right images are paired.")
    parser.add_argument(
        "--exclude-pairs",
        default="",
        help="Comma-separated pair IDs/stems to exclude without deleting source images.",
    )
    return parser.parse_args()


def collect_images(folder: Path) -> list[Path]:
    paths: list[Path] = []
    for suffix in ("*.png", "*.jpg", "*.jpeg", "*.bmp", "*.tif", "*.tiff"):
        paths.extend(folder.glob(suffix))
    return sorted(set(paths))


def pair_images(left_dir: Path, right_dir: Path, mode: str) -> list[tuple[Path, Path, str]]:
    left = collect_images(left_dir)
    right = collect_images(right_dir)
    if mode == "sorted":
        n = min(len(left), len(right))
        return [(left[i], right[i], f"pair_{i + 1:03d}") for i in range(n)]

    right_by_name = {p.name: p for p in right}
    pairs = []
    for lp in left:
        rp = right_by_name.get(lp.name)
        if rp is not None:
            pairs.append((lp, rp, lp.stem))
    return pairs


def load_intrinsics(
    path: Path,
) -> tuple[np.ndarray, np.ndarray, tuple[int, int], float | None, str]:
    data = np.load(path, allow_pickle=False)
    camera_matrix = np.asarray(data["camera_matrix"], dtype=np.float64)
    dist_coeffs = np.asarray(data["dist_coeffs"], dtype=np.float64)
    image_size = tuple(int(v) for v in np.asarray(data["image_size"]).reshape(-1)[:2])
    square_mm = float(data["square_size_mm"]) if "square_size_mm" in data.files else None
    serial = str(np.asarray(data["camera_serial"]).reshape(-1)[0]) if "camera_serial" in data.files else ""
    return camera_matrix, dist_coeffs, image_size, square_mm, serial


def detect_corners(path: Path) -> tuple[bool, np.ndarray | None, np.ndarray]:
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise RuntimeError(f"Could not read image: {path}")
    flags = cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY
    found, corners = cv2.findChessboardCornersSB(image, PATTERN_SIZE, flags=flags)
    return bool(found), None if corners is None else corners.astype(np.float32), image


def mean_reprojection_error(
    object_points: list[np.ndarray],
    image_points: list[np.ndarray],
    rvecs: tuple[np.ndarray, ...] | list[np.ndarray],
    tvecs: tuple[np.ndarray, ...] | list[np.ndarray],
    camera_matrix: np.ndarray,
    dist_coeffs: np.ndarray,
) -> np.ndarray:
    errors = []
    for objp, imgp, rvec, tvec in zip(object_points, image_points, rvecs, tvecs):
        projected, _ = cv2.projectPoints(objp, rvec, tvec, camera_matrix, dist_coeffs)
        diff = projected.reshape(-1, 2) - imgp.reshape(-1, 2)
        errors.append(float(np.sqrt(np.mean(np.sum(diff * diff, axis=1)))))
    return np.asarray(errors, dtype=np.float64)


def symmetric_epipolar_rms(
    left_points: np.ndarray,
    right_points: np.ndarray,
    fundamental: np.ndarray,
) -> float:
    left_xy = np.asarray(left_points, dtype=np.float64).reshape(-1, 2)
    right_xy = np.asarray(right_points, dtype=np.float64).reshape(-1, 2)
    left_h = np.column_stack([left_xy, np.ones(left_xy.shape[0])])
    right_h = np.column_stack([right_xy, np.ones(right_xy.shape[0])])
    right_lines = (fundamental @ left_h.T).T
    left_lines = (fundamental.T @ right_h.T).T
    right_den = np.maximum(
        np.hypot(right_lines[:, 0], right_lines[:, 1]),
        np.finfo(np.float64).eps,
    )
    left_den = np.maximum(
        np.hypot(left_lines[:, 0], left_lines[:, 1]),
        np.finfo(np.float64).eps,
    )
    right_distance = np.abs(np.sum(right_lines * right_h, axis=1)) / right_den
    left_distance = np.abs(np.sum(left_lines * left_h, axis=1)) / left_den
    return float(np.sqrt(np.mean(np.concatenate([left_distance**2, right_distance**2]))))


def main() -> int:
    args = parse_args()
    if args.min_pairs < 1:
        print("ERROR: --min-pairs must be positive.", file=sys.stderr)
        return 2
    if bool(args.left_serial) != bool(args.right_serial):
        print("ERROR: pass both --left-serial and --right-serial, or neither.", file=sys.stderr)
        return 2
    if args.left_serial and args.left_serial == args.right_serial:
        print("ERROR: left and right camera serials must differ.", file=sys.stderr)
        return 2

    left_k, left_dist, left_size, left_square_mm, left_intrinsic_serial = load_intrinsics(
        args.left_intrinsics
    )
    right_k, right_dist, right_size, right_square_mm, right_intrinsic_serial = load_intrinsics(
        args.right_intrinsics
    )
    if left_size != right_size:
        print(f"ERROR: image_size mismatch: left={left_size}, right={right_size}", file=sys.stderr)
        return 2
    for side, requested, intrinsic in (
        ("left", str(args.left_serial), left_intrinsic_serial),
        ("right", str(args.right_serial), right_intrinsic_serial),
    ):
        if requested and intrinsic and requested != intrinsic:
            print(
                f"ERROR: {side} intrinsic camera_serial={intrinsic!r}, "
                f"but --{side}-serial={requested!r}.",
                file=sys.stderr,
            )
            return 2

    square_mm = float(args.square_mm or left_square_mm or right_square_mm or 0.0)
    if square_mm <= 0:
        print("ERROR: --square-mm must be positive or present in intrinsics NPZ.", file=sys.stderr)
        return 2
    known_square_sizes = [
        ("selected", square_mm),
        ("left intrinsics", left_square_mm),
        ("right intrinsics", right_square_mm),
    ]
    if args.require_square_mm > 0:
        for label, value in known_square_sizes:
            if value is not None and not np.isclose(
                float(value),
                args.require_square_mm,
                rtol=0.0,
                atol=1e-9,
            ):
                print(
                    "ERROR: checkerboard square-size safety lock failed: "
                    f"{label}={float(value):.12g}, "
                    f"required={args.require_square_mm:.12g}.",
                    file=sys.stderr,
                )
                return 2
    if (
        left_square_mm is not None
        and right_square_mm is not None
        and not np.isclose(left_square_mm, right_square_mm, rtol=0.0, atol=1e-9)
    ):
        print(
            "ERROR: intrinsic checkerboard square sizes differ: "
            f"left={left_square_mm:.12g}, right={right_square_mm:.12g}.",
            file=sys.stderr,
        )
        return 2

    objp = np.zeros((PATTERN_SIZE[0] * PATTERN_SIZE[1], 3), np.float32)
    objp[:, :2] = np.mgrid[0 : PATTERN_SIZE[0], 0 : PATTERN_SIZE[1]].T.reshape(-1, 2) * square_mm

    pairs = pair_images(args.left_images, args.right_images, args.pair_by)
    if not pairs:
        print("ERROR: no image pairs found.", file=sys.stderr)
        return 2

    args.debug_dir.mkdir(parents=True, exist_ok=True)
    excluded_pairs = {
        item.strip()
        for item in str(args.exclude_pairs).split(",")
        if item.strip()
    }
    object_points: list[np.ndarray] = []
    left_points: list[np.ndarray] = []
    right_points: list[np.ndarray] = []
    accepted: list[tuple[str, str, str]] = []
    rows: list[dict[str, object]] = []

    for left_path, right_path, pair_id in pairs:
        left_found, left_corners, left_image = detect_corners(left_path)
        right_found, right_corners, right_image = detect_corners(right_path)

        left_debug = cv2.cvtColor(left_image, cv2.COLOR_GRAY2BGR)
        right_debug = cv2.cvtColor(right_image, cv2.COLOR_GRAY2BGR)
        if left_found and left_corners is not None:
            cv2.drawChessboardCorners(left_debug, PATTERN_SIZE, left_corners, left_found)
        if right_found and right_corners is not None:
            cv2.drawChessboardCorners(right_debug, PATTERN_SIZE, right_corners, right_found)
        cv2.imwrite(str(args.debug_dir / f"{pair_id}_left.png"), left_debug)
        cv2.imwrite(str(args.debug_dir / f"{pair_id}_right.png"), right_debug)

        excluded = pair_id in excluded_pairs
        usable = (
            left_found
            and right_found
            and left_corners is not None
            and right_corners is not None
            and not excluded
        )
        rows.append(
            {
                "pair_id": pair_id,
                "left_image": left_path.name,
                "right_image": right_path.name,
                "left_found": int(left_found),
                "right_found": int(right_found),
                "excluded": int(excluded),
                "accepted": int(usable),
            }
        )
        if usable:
            object_points.append(objp.copy())
            left_points.append(left_corners)
            right_points.append(right_corners)
            accepted.append((pair_id, str(left_path), str(right_path)))
            print(f"[OK]   {pair_id}: {left_path.name} / {right_path.name}")
        else:
            reason = "excluded" if excluded else f"left={left_found} right={right_found}"
            print(f"[FAIL] {pair_id}: {reason}")

    if len(accepted) < args.min_pairs:
        print(f"ERROR: usable stereo pairs {len(accepted)} < {args.min_pairs}", file=sys.stderr)
        return 3

    flags = cv2.CALIB_FIX_INTRINSIC
    rms, left_k_out, left_dist_out, right_k_out, right_dist_out, R, T, E, F = cv2.stereoCalibrate(
        object_points,
        left_points,
        right_points,
        left_k.copy(),
        left_dist.copy(),
        right_k.copy(),
        right_dist.copy(),
        left_size,
        flags=flags,
    )

    R1, R2, P1, P2, Q, roi1, roi2 = cv2.stereoRectify(
        left_k,
        left_dist,
        right_k,
        right_dist,
        left_size,
        R,
        T,
        flags=cv2.CALIB_ZERO_DISPARITY,
        alpha=0,
    )

    # Per-camera reprojection errors with fixed intrinsics and each board pose.
    left_error_values: list[float] = []
    right_error_values: list[float] = []
    for obj, lp, rp in zip(object_points, left_points, right_points):
        ok_l, rvec_l, tvec_l = cv2.solvePnP(obj, lp, left_k, left_dist)
        ok_r, rvec_r, tvec_r = cv2.solvePnP(obj, rp, right_k, right_dist)
        if ok_l:
            left_projected, _ = cv2.projectPoints(obj, rvec_l, tvec_l, left_k, left_dist)
            left_diff = left_projected.reshape(-1, 2) - lp.reshape(-1, 2)
            left_error_values.append(
                float(np.sqrt(np.mean(np.sum(left_diff * left_diff, axis=1))))
            )
        else:
            left_error_values.append(float("nan"))
        if ok_r:
            right_projected, _ = cv2.projectPoints(obj, rvec_r, tvec_r, right_k, right_dist)
            right_diff = right_projected.reshape(-1, 2) - rp.reshape(-1, 2)
            right_error_values.append(
                float(np.sqrt(np.mean(np.sum(right_diff * right_diff, axis=1))))
            )
        else:
            right_error_values.append(float("nan"))
    left_errors = np.asarray(left_error_values, dtype=np.float64)
    right_errors = np.asarray(right_error_values, dtype=np.float64)
    epipolar_errors = np.asarray(
        [
            symmetric_epipolar_rms(left_point, right_point, F)
            for left_point, right_point in zip(left_points, right_points)
        ],
        dtype=np.float64,
    )
    row_by_pair_id = {str(row["pair_id"]): row for row in rows}
    for index, (pair_id, _left_path, _right_path) in enumerate(accepted):
        row = row_by_pair_id[pair_id]
        row["left_pnp_rms_px"] = f"{float(left_errors[index]):.9f}"
        row["right_pnp_rms_px"] = f"{float(right_errors[index]):.9f}"
        row["symmetric_epipolar_rms_px"] = f"{float(epipolar_errors[index]):.9f}"

    np.savez(
        args.output,
        left_camera_matrix=left_k,
        left_dist_coeffs=left_dist,
        right_camera_matrix=right_k,
        right_dist_coeffs=right_dist,
        image_size=np.asarray(left_size, dtype=np.int32),
        square_size_mm=np.asarray(square_mm, dtype=np.float64),
        pattern_size=np.asarray(PATTERN_SIZE, dtype=np.int32),
        stereo_rms=np.asarray(rms, dtype=np.float64),
        R=R,
        T=T,
        E=E,
        F=F,
        R1=R1,
        R2=R2,
        P1=P1,
        P2=P2,
        Q=Q,
        roi1=np.asarray(roi1, dtype=np.int32),
        roi2=np.asarray(roi2, dtype=np.int32),
        accepted_pairs=np.asarray(accepted),
        left_per_view_errors=left_errors,
        right_per_view_errors=right_errors,
        symmetric_epipolar_rms_px=epipolar_errors,
        left_camera_serial=np.asarray(str(args.left_serial)),
        right_camera_serial=np.asarray(str(args.right_serial)),
    )

    report_path = args.output.with_suffix(".csv")
    with report_path.open("w", newline="", encoding="utf-8") as csv_file:
        fieldnames = [
            "pair_id",
            "left_image",
            "right_image",
            "left_found",
            "right_found",
            "excluded",
            "accepted",
            "left_pnp_rms_px",
            "right_pnp_rms_px",
            "symmetric_epipolar_rms_px",
        ]
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    baseline_mm = float(np.linalg.norm(T))
    print("\n=== Stereo calibration result ===")
    print(f"Usable pairs: {len(accepted)}/{len(pairs)}")
    print(f"Stereo RMS [px]: {rms:.6f}")
    print(
        "Median symmetric epipolar RMS [px]: "
        f"{float(np.median(epipolar_errors)):.6f}"
    )
    print(f"Baseline [mm]: {baseline_mm:.6f}")
    print("R:")
    print(R)
    print("T [mm]:")
    print(T.ravel())
    print(f"Saved: {args.output}")
    print(f"Pair report: {report_path}")
    print(f"Detection overlays: {args.debug_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
