#!/usr/bin/env python3
"""
Single-camera calibration from accumulated event-frame images.

Expected board:
- 10 x 7 squares
- 9 x 6 internal corners

Example:
    python single_camera_calibrate.py \
        --images calib_images \
        --square-mm 6.42 \
        --output silky_single_calibration.npz

Dependencies:
    pip install numpy opencv-python
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import cv2
import numpy as np


PATTERN_SIZE = (9, 6)  # internal corners: columns, rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--images", type=Path, required=True,
                        help="Folder containing PNG/JPG calibration frames")
    parser.add_argument(
        "--camera-serial",
        default="",
        help="Camera serial saved in the output NPZ for left/right provenance checks.",
    )
    parser.add_argument("--square-mm", type=float, required=True,
                        help="Actual displayed square width in millimetres")
    parser.add_argument(
        "--require-square-mm",
        type=float,
        default=0.0,
        help=(
            "Safety lock for a known checkerboard. When positive, calibration "
            "stops unless --square-mm matches this value."
        ),
    )
    parser.add_argument("--output", type=Path,
                        default=Path("silky_single_calibration.npz"))
    parser.add_argument("--debug-dir", type=Path,
                        default=Path("calibration_debug"))
    return parser.parse_args()


def collect_images(folder: Path) -> list[Path]:
    paths: list[Path] = []
    for suffix in ("*.png", "*.jpg", "*.jpeg", "*.bmp", "*.tif", "*.tiff"):
        paths.extend(folder.glob(suffix))
    return sorted(set(paths))


def main() -> int:
    args = parse_args()

    if args.square_mm <= 0:
        print("ERROR: --square-mm must be positive.", file=sys.stderr)
        return 2
    if args.require_square_mm > 0 and not np.isclose(
        args.square_mm,
        args.require_square_mm,
        rtol=0.0,
        atol=1e-9,
    ):
        print(
            "ERROR: checkerboard square-size safety lock failed: "
            f"--square-mm={args.square_mm:.12g}, "
            f"required={args.require_square_mm:.12g}.",
            file=sys.stderr,
        )
        return 2

    image_paths = collect_images(args.images)
    if not image_paths:
        print(f"ERROR: No images found in {args.images}", file=sys.stderr)
        return 2

    args.debug_dir.mkdir(parents=True, exist_ok=True)

    # Physical coordinates on the board plane, in millimetres.
    objp = np.zeros((PATTERN_SIZE[0] * PATTERN_SIZE[1], 3), np.float32)
    objp[:, :2] = (
        np.mgrid[0:PATTERN_SIZE[0], 0:PATTERN_SIZE[1]]
        .T.reshape(-1, 2)
        * args.square_mm
    )

    object_points: list[np.ndarray] = []
    image_points: list[np.ndarray] = []
    accepted_paths: list[Path] = []
    image_size: tuple[int, int] | None = None

    detector_flags = cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY

    print(f"Found {len(image_paths)} candidate images.")
    for path in image_paths:
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            print(f"[SKIP] cannot read: {path.name}")
            continue

        current_size = (image.shape[1], image.shape[0])
        if image_size is None:
            image_size = current_size
        elif current_size != image_size:
            print(f"[SKIP] size mismatch: {path.name} {current_size} != {image_size}")
            continue

        found, corners = cv2.findChessboardCornersSB(
            image, PATTERN_SIZE, flags=detector_flags
        )

        debug = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        if found:
            object_points.append(objp.copy())
            image_points.append(corners.astype(np.float32))
            accepted_paths.append(path)
            cv2.drawChessboardCorners(debug, PATTERN_SIZE, corners, found)
            print(f"[OK]   {path.name}")
        else:
            print(f"[FAIL] {path.name}")

        cv2.imwrite(str(args.debug_dir / path.name), debug)

    n = len(accepted_paths)
    print(f"\nUsable views: {n}/{len(image_paths)}")
    if n < 10:
        print("ERROR: fewer than 10 usable views. Capture more varied poses.",
              file=sys.stderr)
        return 3
    if image_size is None:
        return 3

    (
        rms,
        camera_matrix,
        dist_coeffs,
        rvecs,
        tvecs,
        std_intrinsics,
        std_extrinsics,
        per_view_errors,
    ) = cv2.calibrateCameraExtended(
        object_points,
        image_points,
        image_size,
        None,
        None,
    )

    per_view_errors = np.asarray(per_view_errors).reshape(-1)

    np.savez(
        args.output,
        camera_matrix=camera_matrix,
        dist_coeffs=dist_coeffs,
        image_size=np.asarray(image_size, dtype=np.int32),
        square_size_mm=np.asarray(args.square_mm, dtype=np.float64),
        pattern_size=np.asarray(PATTERN_SIZE, dtype=np.int32),
        rms=np.asarray(rms, dtype=np.float64),
        per_view_errors=per_view_errors,
        accepted_images=np.asarray([str(p) for p in accepted_paths]),
        camera_serial=np.asarray(str(args.camera_serial)),
    )

    report_path = args.output.with_suffix(".csv")
    with report_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["image", "per_view_rms_px"])
        for path, err in zip(accepted_paths, per_view_errors):
            writer.writerow([path.name, f"{float(err):.6f}"])

    print("\n=== Calibration result ===")
    print(f"Overall RMS [px]: {rms:.6f}")
    print("Camera matrix K:")
    print(camera_matrix)
    print("Distortion coefficients:")
    print(dist_coeffs.ravel())
    print(f"Saved: {args.output}")
    print(f"Per-view report: {report_path}")
    print(f"Detection overlays: {args.debug_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
