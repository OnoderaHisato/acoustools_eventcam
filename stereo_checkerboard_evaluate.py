#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Evaluate a synchronized stereo event recording with a checkerboard.

The ordinary ``stereo_process_recording.py`` follows one event centroid.  This
tool instead detects every checkerboard corner in synchronized event-count
images, triangulates all corners, and evaluates the known adjacent-corner
spacing.  It is intended for an independent check of an existing stereo
calibration; it never modifies that calibration or the raw recording.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
import zipfile
from pathlib import Path
from typing import Any, Iterable

import cv2
import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

import eventcam_checkerboard_calibration_capture as checkerboard_capture


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CALIBRATION = (
    SCRIPT_DIR
    / "stereo_checkerboard_calib_extrinsics_20260805"
    / "stereo_calibration_square7p12_extrinsics_final.npz"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Detect a checkerboard in synchronized stereo event data and "
            "evaluate its known square spacing in reconstructed 3D."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "run_dir",
        type=Path,
        help="Run folder created by stereo_eventcam_record_sync.py.",
    )
    parser.add_argument(
        "--stereo-calibration",
        type=Path,
        default=DEFAULT_CALIBRATION,
        help="Existing stereo calibration NPZ to evaluate.",
    )
    parser.add_argument(
        "--square-mm",
        type=float,
        default=5.341125,
        help="Physical distance between adjacent checkerboard corners.",
    )
    parser.add_argument("--pattern-cols", type=int, default=9)
    parser.add_argument("--pattern-rows", type=int, default=6)
    parser.add_argument(
        "--window-us-list",
        default="10000,20000,30000,40000",
        help="Comma-separated synchronized accumulation windows.",
    )
    parser.add_argument(
        "--preferred-window-us",
        type=int,
        default=10_000,
        help="Window length searched first; short windows reduce motion blur.",
    )
    parser.add_argument(
        "--window-hop-us",
        type=int,
        default=5_000,
        help="Candidate start-time spacing.",
    )
    parser.add_argument(
        "--candidate-count",
        type=int,
        default=40,
        help=(
            "Candidates retained per window length. Half are high-activity "
            "windows and half provide temporal coverage."
        ),
    )
    parser.add_argument(
        "--min-events-per-camera",
        type=int,
        default=1_000,
        help="Minimum events in each camera for a candidate window.",
    )
    parser.add_argument(
        "--render-modes",
        default="abs,on,off",
        help="Comma-separated event image modes tried for each window.",
    )
    parser.add_argument(
        "--scale-percentile",
        type=float,
        default=99.5,
        help="Contrast percentile for event-count images.",
    )
    parser.add_argument(
        "--max-candidate-windows",
        type=int,
        default=160,
        help="Maximum synchronized windows tested.",
    )
    parser.add_argument(
        "--max-samples",
        type=int,
        default=4,
        help="Maximum non-overlapping paired detections retained.",
    )
    parser.add_argument(
        "--max-epipolar-rms-px",
        type=float,
        default=0.5,
        help="Advisory threshold for symmetric epipolar RMS.",
    )
    parser.add_argument(
        "--max-square-error-percent",
        type=float,
        default=1.0,
        help="Advisory threshold for absolute mean square-size error.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output folder; defaults to RUN_DIR/checkerboard_evaluation.",
    )
    return parser.parse_args()


def parse_positive_ints(text: str, name: str) -> list[int]:
    values: list[int] = []
    for item in str(text).split(","):
        item = item.strip()
        if not item:
            continue
        value = int(item)
        if value <= 0:
            raise SystemExit(f"{name} values must be positive: {text}")
        values.append(value)
    if not values:
        raise SystemExit(f"{name} did not contain a value.")
    return sorted(set(values))


def parse_render_modes(text: str) -> list[str]:
    allowed = {"abs", "on", "off", "signed"}
    modes = [item.strip().lower() for item in str(text).split(",") if item.strip()]
    invalid = [mode for mode in modes if mode not in allowed]
    if invalid or not modes:
        raise SystemExit(
            f"--render-modes must use {sorted(allowed)}; observed={modes!r}"
        )
    return list(dict.fromkeys(modes))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding=encoding)
    os.replace(temporary, path)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False),
    )


def json_number(value: float) -> float | None:
    value = float(value)
    return value if math.isfinite(value) else None


def npz_scalar(path: Path, key: str, default: Any) -> Any:
    with np.load(path, allow_pickle=False) as data:
        if key not in data.files:
            return default
        value = np.asarray(data[key])
        return value.item() if value.ndim == 0 else default


def load_events(path: Path) -> np.ndarray:
    """Memory-map an uncompressed events.npy member, with a safe fallback."""
    with zipfile.ZipFile(path) as archive:
        try:
            member = archive.getinfo("events.npy")
        except KeyError as exc:
            raise RuntimeError(f"events.npy is missing from {path}") from exc
        if member.compress_type != zipfile.ZIP_STORED:
            print(f"[WARN] {path.name} is compressed; loading events into memory.")
            with np.load(path, allow_pickle=False) as data:
                return np.asarray(data["events"])
        header_offset = int(member.header_offset)

    with path.open("rb") as handle:
        handle.seek(header_offset)
        local_header = handle.read(30)
        if len(local_header) != 30 or local_header[:4] != b"PK\x03\x04":
            raise RuntimeError(f"Invalid ZIP local header in {path}")
        name_length = int.from_bytes(local_header[26:28], "little")
        extra_length = int.from_bytes(local_header[28:30], "little")
        handle.seek(name_length + extra_length, os.SEEK_CUR)
        version = np.lib.format.read_magic(handle)
        if version == (1, 0):
            shape, fortran_order, dtype = np.lib.format.read_array_header_1_0(handle)
        elif version == (2, 0):
            shape, fortran_order, dtype = np.lib.format.read_array_header_2_0(handle)
        else:
            raise RuntimeError(f"Unsupported events.npy format {version} in {path}")
        array_offset = int(handle.tell())

    return np.memmap(
        path,
        dtype=dtype,
        mode="r",
        offset=array_offset,
        shape=shape,
        order="F" if fortran_order else "C",
    )


def validate_recording_pair(
    left_npz: Path,
    right_npz: Path,
) -> tuple[int, int, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    for side, path in (("left", left_npz), ("right", right_npz)):
        values = {
            "camera_serial": str(npz_scalar(path, "camera_serial", "")),
            "hardware_synchronized": bool(
                npz_scalar(path, "hardware_synchronized", False)
            ),
            "sync_mode_verified": bool(npz_scalar(path, "sync_mode_verified", False)),
            "sync_mode_applied": str(npz_scalar(path, "sync_mode_applied", "")),
            "timestamp_domain_id": str(npz_scalar(path, "timestamp_domain_id", "")),
            "output_start_ts_us": int(npz_scalar(path, "output_start_ts_us", 0)),
            "output_end_ts_us": int(npz_scalar(path, "output_end_ts_us", 0)),
            "capture_interval_complete": bool(
                npz_scalar(path, "capture_interval_complete", False)
            ),
            "event_limit_reached": bool(npz_scalar(path, "event_limit_reached", False)),
        }
        if not values["hardware_synchronized"]:
            raise RuntimeError(f"{side} NPZ is not marked hardware synchronized.")
        if not values["sync_mode_verified"]:
            raise RuntimeError(f"{side} NPZ synchronization mode is not verified.")
        if not values["capture_interval_complete"]:
            raise RuntimeError(f"{side} NPZ did not complete the capture interval.")
        if values["event_limit_reached"]:
            raise RuntimeError(f"{side} NPZ was truncated by the event limit.")
        metadata[side] = values

    left = metadata["left"]
    right = metadata["right"]
    if left["timestamp_domain_id"] != right["timestamp_domain_id"]:
        raise RuntimeError("Left/right timestamp domains differ.")
    if {left["sync_mode_applied"], right["sync_mode_applied"]} != {
        "master",
        "slave",
    }:
        raise RuntimeError("Expected one master camera and one slave camera.")
    start = int(left["output_start_ts_us"])
    end = int(left["output_end_ts_us"])
    if (start, end) != (
        int(right["output_start_ts_us"]),
        int(right["output_end_ts_us"]),
    ):
        raise RuntimeError("Left/right saved camera-clock intervals differ.")
    if end <= start:
        raise RuntimeError(f"Empty shared interval [{start}, {end}).")
    return start, end, metadata


def load_calibration(path: Path) -> dict[str, Any]:
    required = (
        "left_camera_matrix",
        "left_dist_coeffs",
        "right_camera_matrix",
        "right_dist_coeffs",
        "image_size",
        "R",
        "T",
        "F",
    )
    with np.load(path, allow_pickle=False) as data:
        missing = [key for key in required if key not in data.files]
        if missing:
            raise RuntimeError(f"Calibration is missing fields: {missing}")
        calibration: dict[str, Any] = {
            "left_k": np.asarray(data["left_camera_matrix"], dtype=np.float64),
            "left_dist": np.asarray(data["left_dist_coeffs"], dtype=np.float64),
            "right_k": np.asarray(data["right_camera_matrix"], dtype=np.float64),
            "right_dist": np.asarray(data["right_dist_coeffs"], dtype=np.float64),
            "image_size": tuple(
                int(value) for value in np.asarray(data["image_size"]).reshape(-1)[:2]
            ),
            "R": np.asarray(data["R"], dtype=np.float64),
            "T": np.asarray(data["T"], dtype=np.float64).reshape(3, 1),
            "F": np.asarray(data["F"], dtype=np.float64),
            "calibration_square_mm": (
                float(np.asarray(data["square_size_mm"]).reshape(-1)[0])
                if "square_size_mm" in data.files
                else None
            ),
            "stereo_rms_px": (
                float(np.asarray(data["stereo_rms"]).reshape(-1)[0])
                if "stereo_rms" in data.files
                else None
            ),
            "accepted_pair_count": (
                int(np.asarray(data["accepted_pairs"]).shape[0])
                if "accepted_pairs" in data.files
                else None
            ),
            "left_camera_serial": (
                str(np.asarray(data["left_camera_serial"]).reshape(-1)[0])
                if "left_camera_serial" in data.files
                else ""
            ),
            "right_camera_serial": (
                str(np.asarray(data["right_camera_serial"]).reshape(-1)[0])
                if "right_camera_serial" in data.files
                else ""
            ),
        }
    return calibration


def temporal_and_high_score_indices(scores: np.ndarray, count: int) -> np.ndarray:
    usable = np.flatnonzero(scores >= 0)
    if usable.size <= count:
        return usable
    high_count = max(1, count // 2)
    high = usable[np.argsort(scores[usable])[::-1][:high_count]]
    temporal_count = max(1, count - high_count)
    temporal: list[int] = []
    for segment in np.array_split(usable, temporal_count):
        if segment.size:
            temporal.append(int(segment[np.argmax(scores[segment])]))
    selected = list(dict.fromkeys([*(int(v) for v in high), *temporal]))
    if len(selected) < count:
        for index in usable[np.argsort(scores[usable])[::-1]]:
            if int(index) not in selected:
                selected.append(int(index))
            if len(selected) >= count:
                break
    return np.asarray(selected[:count], dtype=np.int64)


def temporal_spread_order(rows: list[dict[str, int]]) -> list[dict[str, int]]:
    """Order candidates so early accepted samples cover the recording span.

    The first candidate retains the strongest joint event support.  Each next
    candidate is the one farthest in time from every already selected
    candidate, with event support used as the tie-breaker.  This avoids four
    good detections from one activity burst being mistaken for temporal
    coverage merely because their windows do not overlap.
    """
    if len(rows) < 2:
        return list(rows)
    pending = list(rows)
    first = max(pending, key=lambda row: int(row["joint_event_score"]))
    ordered = [first]
    pending.remove(first)
    while pending:
        centers = [
            (int(item["start_ts_us"]) + int(item["end_ts_us"])) / 2.0
            for item in ordered
        ]
        next_row = max(
            pending,
            key=lambda row: (
                min(
                    abs(
                        (int(row["start_ts_us"]) + int(row["end_ts_us"])) / 2.0
                        - center
                    )
                    for center in centers
                ),
                int(row["joint_event_score"]),
            ),
        )
        ordered.append(next_row)
        pending.remove(next_row)
    return ordered


def candidate_windows(
    left_events: np.ndarray,
    right_events: np.ndarray,
    start_us: int,
    end_us: int,
    window_sizes: Iterable[int],
    hop_us: int,
    minimum_events: int,
    candidate_count: int,
    preferred_window_us: int,
) -> list[dict[str, int]]:
    sizes = list(window_sizes)
    grids: dict[int, dict[str, np.ndarray]] = {}
    for window_us in sizes:
        latest = end_us - int(window_us)
        starts = np.arange(start_us, latest + 1, hop_us, dtype=np.int64)
        grids[int(window_us)] = {"starts": starts}

    for side, events in (("left", left_events), ("right", right_events)):
        timestamps = np.ascontiguousarray(events["t"], dtype=np.int64)
        for window_us, grid in grids.items():
            starts = grid["starts"]
            begins = np.searchsorted(timestamps, starts, side="left")
            ends = np.searchsorted(timestamps, starts + window_us, side="left")
            grid[f"{side}_begin"] = begins
            grid[f"{side}_end"] = ends
            grid[f"{side}_count"] = ends - begins
        del timestamps

    rows: list[dict[str, int]] = []
    for window_us, grid in grids.items():
        left_counts = grid["left_count"]
        right_counts = grid["right_count"]
        scores = np.minimum(left_counts, right_counts).astype(np.int64)
        scores[scores < minimum_events] = -1
        selected = temporal_and_high_score_indices(scores, candidate_count)
        for index in selected:
            rows.append(
                {
                    "start_ts_us": int(grid["starts"][index]),
                    "end_ts_us": int(grid["starts"][index] + window_us),
                    "frame_window_us": int(window_us),
                    "left_events": int(left_counts[index]),
                    "right_events": int(right_counts[index]),
                    "joint_event_score": int(scores[index]),
                    "left_begin": int(grid["left_begin"][index]),
                    "left_end": int(grid["left_end"][index]),
                    "right_begin": int(grid["right_begin"][index]),
                    "right_end": int(grid["right_end"][index]),
                }
            )
    ordered: list[dict[str, int]] = []
    for window_us in sorted(
        sizes, key=lambda value: (abs(value - preferred_window_us), value)
    ):
        same_size = [
            row for row in rows if int(row["frame_window_us"]) == int(window_us)
        ]
        ordered.extend(temporal_spread_order(same_size))
    return ordered


def symmetric_epipolar_distances(
    left_points: np.ndarray,
    right_points: np.ndarray,
    fundamental: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, float]:
    left_xy = np.asarray(left_points, dtype=np.float64).reshape(-1, 2)
    right_xy = np.asarray(right_points, dtype=np.float64).reshape(-1, 2)
    left_h = np.column_stack([left_xy, np.ones(left_xy.shape[0])])
    right_h = np.column_stack([right_xy, np.ones(right_xy.shape[0])])
    right_lines = (fundamental @ left_h.T).T
    left_lines = (fundamental.T @ right_h.T).T
    eps = np.finfo(np.float64).eps
    right_den = np.maximum(np.hypot(right_lines[:, 0], right_lines[:, 1]), eps)
    left_den = np.maximum(np.hypot(left_lines[:, 0], left_lines[:, 1]), eps)
    right_distance = np.abs(np.sum(right_lines * right_h, axis=1)) / right_den
    left_distance = np.abs(np.sum(left_lines * left_h, axis=1)) / left_den
    rms = float(
        np.sqrt(np.mean(np.concatenate([left_distance**2, right_distance**2])))
    )
    return left_distance, right_distance, rms


def align_right_corners(
    left_points: np.ndarray,
    right_points: np.ndarray,
    rows: int,
    cols: int,
    fundamental: np.ndarray,
    calibration: dict[str, Any] | None = None,
) -> tuple[np.ndarray, str, float]:
    grid = np.asarray(right_points, dtype=np.float64).reshape(rows, cols, 2)
    candidates = {
        "identity": grid,
        "flip_rows": grid[::-1, :, :],
        "flip_cols": grid[:, ::-1, :],
        "flip_rows_cols": grid[::-1, ::-1, :],
    }
    ranked: list[tuple[float, float, str, np.ndarray]] = []
    for name, candidate in candidates.items():
        flattened = np.ascontiguousarray(candidate.reshape(-1, 2))
        _, _, rms = symmetric_epipolar_distances(
            left_points, flattened, fundamental
        )
        geometry_penalty = 0.0
        if calibration is not None:
            try:
                reconstructed, left_reprojection, right_reprojection = (
                    triangulate_corners(left_points, flattened, calibration)
                )
                if (
                    not np.all(np.isfinite(reconstructed))
                    or np.any(reconstructed[:, 2] <= 0)
                ):
                    geometry_penalty = 1e9
                else:
                    grid3d = reconstructed.reshape(rows, cols, 3)
                    horizontal = np.linalg.norm(
                        grid3d[:, 1:, :] - grid3d[:, :-1, :], axis=2
                    ).reshape(-1)
                    vertical = np.linalg.norm(
                        grid3d[1:, :, :] - grid3d[:-1, :, :], axis=2
                    ).reshape(-1)
                    edges = np.concatenate([horizontal, vertical])
                    mean_edge = max(float(np.mean(edges)), 1e-12)
                    edge_cv = float(np.std(edges) / mean_edge)
                    plane_ratio = float(plane_rms_mm(reconstructed) / mean_edge)
                    reprojection_rms = float(
                        np.sqrt(
                            np.mean(
                                np.concatenate(
                                    [left_reprojection**2, right_reprojection**2]
                                )
                            )
                        )
                    )
                    geometry_penalty = (
                        5.0 * edge_cv + 5.0 * plane_ratio + 0.1 * reprojection_rms
                    )
            except (cv2.error, FloatingPointError, ValueError):
                geometry_penalty = 1e9
        ranked.append((rms + geometry_penalty, rms, name, flattened))
    _score, rms, name, points = min(ranked, key=lambda item: item[0])
    return points, name, float(rms)


def triangulate_corners(
    left_points: np.ndarray,
    right_points: np.ndarray,
    calibration: dict[str, Any],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    left_normalized = cv2.undistortPoints(
        np.asarray(left_points, dtype=np.float64).reshape(-1, 1, 2),
        calibration["left_k"],
        calibration["left_dist"],
    ).reshape(-1, 2)
    right_normalized = cv2.undistortPoints(
        np.asarray(right_points, dtype=np.float64).reshape(-1, 1, 2),
        calibration["right_k"],
        calibration["right_dist"],
    ).reshape(-1, 2)
    left_projection = np.column_stack(
        [np.eye(3, dtype=np.float64), np.zeros((3, 1), dtype=np.float64)]
    )
    right_projection = np.column_stack([calibration["R"], calibration["T"]])
    homogeneous = cv2.triangulatePoints(
        left_projection,
        right_projection,
        left_normalized.T,
        right_normalized.T,
    )
    points = (homogeneous[:3] / homogeneous[3]).T

    left_projected, _ = cv2.projectPoints(
        points,
        np.zeros((3, 1), dtype=np.float64),
        np.zeros((3, 1), dtype=np.float64),
        calibration["left_k"],
        calibration["left_dist"],
    )
    right_rvec, _ = cv2.Rodrigues(calibration["R"])
    right_projected, _ = cv2.projectPoints(
        points,
        right_rvec,
        calibration["T"],
        calibration["right_k"],
        calibration["right_dist"],
    )
    left_error = np.linalg.norm(
        left_projected.reshape(-1, 2)
        - np.asarray(left_points, dtype=np.float64).reshape(-1, 2),
        axis=1,
    )
    right_error = np.linalg.norm(
        right_projected.reshape(-1, 2)
        - np.asarray(right_points, dtype=np.float64).reshape(-1, 2),
        axis=1,
    )
    return points, left_error, right_error


def pnp_rms(
    image_points: np.ndarray,
    camera_matrix: np.ndarray,
    dist_coeffs: np.ndarray,
    rows: int,
    cols: int,
    square_mm: float,
) -> float:
    object_points = np.zeros((rows * cols, 3), dtype=np.float32)
    object_points[:, :2] = (
        np.mgrid[0:cols, 0:rows].T.reshape(-1, 2) * float(square_mm)
    )
    ok, rvec, tvec = cv2.solvePnP(
        object_points,
        np.asarray(image_points, dtype=np.float32).reshape(-1, 1, 2),
        camera_matrix,
        dist_coeffs,
    )
    if not ok:
        return float("nan")
    projected, _ = cv2.projectPoints(
        object_points, rvec, tvec, camera_matrix, dist_coeffs
    )
    residual = projected.reshape(-1, 2) - np.asarray(image_points).reshape(-1, 2)
    return float(np.sqrt(np.mean(np.sum(residual**2, axis=1))))


def edge_rows_for_sample(
    sample_id: int,
    points: np.ndarray,
    rows: int,
    cols: int,
    square_mm: float,
) -> list[dict[str, Any]]:
    grid = np.asarray(points, dtype=np.float64).reshape(rows, cols, 3)
    records: list[dict[str, Any]] = []
    for row in range(rows):
        for col in range(cols - 1):
            measured = float(np.linalg.norm(grid[row, col + 1] - grid[row, col]))
            records.append(
                {
                    "sample_id": sample_id,
                    "direction": "horizontal",
                    "row": row,
                    "col": col,
                    "measured_mm": measured,
                    "error_mm": measured - square_mm,
                    "error_percent": (measured / square_mm - 1.0) * 100.0,
                }
            )
    for row in range(rows - 1):
        for col in range(cols):
            measured = float(np.linalg.norm(grid[row + 1, col] - grid[row, col]))
            records.append(
                {
                    "sample_id": sample_id,
                    "direction": "vertical",
                    "row": row,
                    "col": col,
                    "measured_mm": measured,
                    "error_mm": measured - square_mm,
                    "error_percent": (measured / square_mm - 1.0) * 100.0,
                }
            )
    return records


def plane_rms_mm(points: np.ndarray) -> float:
    centered = np.asarray(points, dtype=np.float64) - np.mean(points, axis=0)
    _u, _s, vh = np.linalg.svd(centered, full_matrices=False)
    distances = centered @ vh[-1]
    return float(np.sqrt(np.mean(distances**2)))


def intervals_overlap(first: tuple[int, int], second: tuple[int, int]) -> bool:
    return max(first[0], second[0]) < min(first[1], second[1])


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def draw_detection_pair(
    output_path: Path,
    left_image: np.ndarray,
    right_image: np.ndarray,
    left_corners: np.ndarray,
    right_corners: np.ndarray,
    pattern_size: tuple[int, int],
) -> None:
    left_debug = cv2.cvtColor(left_image, cv2.COLOR_GRAY2BGR)
    right_debug = cv2.cvtColor(right_image, cv2.COLOR_GRAY2BGR)
    left_draw = np.ascontiguousarray(left_corners, dtype=np.float32).reshape(-1, 1, 2)
    right_draw = np.ascontiguousarray(right_corners, dtype=np.float32).reshape(-1, 1, 2)
    cv2.drawChessboardCorners(
        left_debug, pattern_size, left_draw, True
    )
    cv2.drawChessboardCorners(
        right_debug, pattern_size, right_draw, True
    )
    combined = np.hstack([left_debug, right_debug])
    cv2.imwrite(str(output_path), combined)


def plot_square_results(
    path: Path,
    edge_rows: list[dict[str, Any]],
    rows: int,
    cols: int,
    square_mm: float,
) -> None:
    horizontal = np.asarray(
        [row["measured_mm"] for row in edge_rows if row["direction"] == "horizontal"]
    )
    vertical = np.asarray(
        [row["measured_mm"] for row in edge_rows if row["direction"] == "vertical"]
    )
    horizontal_map = np.full((rows, cols - 1), np.nan)
    vertical_map = np.full((rows - 1, cols), np.nan)
    for direction, shape, target in (
        ("horizontal", horizontal_map.shape, horizontal_map),
        ("vertical", vertical_map.shape, vertical_map),
    ):
        for row_index in range(shape[0]):
            for col_index in range(shape[1]):
                values = [
                    item["error_mm"]
                    for item in edge_rows
                    if item["direction"] == direction
                    and item["row"] == row_index
                    and item["col"] == col_index
                ]
                if values:
                    target[row_index, col_index] = float(np.mean(values))

    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8), constrained_layout=True)
    axes[0].boxplot(
        [horizontal, vertical], tick_labels=["9-corner axis", "6-corner axis"]
    )
    axes[0].axhline(square_mm, color="black", linestyle="--", label="Nominal")
    axes[0].set_ylabel("Reconstructed adjacent-corner distance [mm]")
    axes[0].set_title("Square-size distribution")
    axes[0].grid(alpha=0.25)
    axes[0].legend()
    limit = float(
        max(
            np.nanmax(np.abs(horizontal_map)),
            np.nanmax(np.abs(vertical_map)),
            1e-6,
        )
    )
    for axis, values, title in (
        (axes[1], horizontal_map, "Pattern-column edge error [mm]"),
        (axes[2], vertical_map, "Pattern-row edge error [mm]"),
    ):
        image = axis.imshow(values, cmap="coolwarm", vmin=-limit, vmax=limit)
        axis.set_title(title)
        axis.set_xlabel("Board column")
        axis.set_ylabel("Board row")
        fig.colorbar(image, ax=axis, shrink=0.82)
    fig.savefig(path, dpi=180)
    plt.close(fig)


def plot_corner_errors(
    path: Path,
    samples: list[dict[str, Any]],
    rows: int,
    cols: int,
) -> None:
    epipolar = np.mean(
        np.stack([sample["corner_epipolar_px"] for sample in samples]), axis=0
    ).reshape(rows, cols)
    left_reprojection = np.mean(
        np.stack([sample["left_reprojection_px"] for sample in samples]), axis=0
    ).reshape(rows, cols)
    right_reprojection = np.mean(
        np.stack([sample["right_reprojection_px"] for sample in samples]), axis=0
    ).reshape(rows, cols)
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.5), constrained_layout=True)
    for axis, values, title in (
        (axes[0], epipolar, "Symmetric epipolar distance [px]"),
        (axes[1], left_reprojection, "Left triangulation reprojection [px]"),
        (axes[2], right_reprojection, "Right triangulation reprojection [px]"),
    ):
        image = axis.imshow(values, cmap="viridis")
        axis.set_title(title)
        axis.set_xlabel("Board column")
        axis.set_ylabel("Board row")
        fig.colorbar(image, ax=axis, shrink=0.82)
    fig.savefig(path, dpi=180)
    plt.close(fig)


def plot_board_3d(
    path: Path,
    points: np.ndarray,
    rows: int,
    cols: int,
) -> None:
    grid = np.asarray(points).reshape(rows, cols, 3)
    fig = plt.figure(figsize=(8, 7), constrained_layout=True)
    axis = fig.add_subplot(111, projection="3d")
    for row in range(rows):
        axis.plot(grid[row, :, 0], grid[row, :, 1], grid[row, :, 2], "o-", ms=3)
    for col in range(cols):
        axis.plot(grid[:, col, 0], grid[:, col, 1], grid[:, col, 2], "o-", ms=3)
    axis.set_xlabel("Left-camera X [mm]")
    axis.set_ylabel("Left-camera Y [mm]")
    axis.set_zlabel("Left-camera Z [mm]")
    axis.set_title("Triangulated checkerboard corners")
    try:
        axis.set_box_aspect(np.ptp(points, axis=0))
    except (AttributeError, ValueError):
        pass
    fig.savefig(path, dpi=180)
    plt.close(fig)


def metric_summary(values: np.ndarray, nominal: float) -> dict[str, Any]:
    values = np.asarray(values, dtype=np.float64)
    errors = values - nominal
    return {
        "count": int(values.size),
        "mean_mm": float(np.mean(values)),
        "median_mm": float(np.median(values)),
        "standard_deviation_mm": float(np.std(values)),
        "minimum_mm": float(np.min(values)),
        "maximum_mm": float(np.max(values)),
        "mean_error_mm": float(np.mean(errors)),
        "mean_error_percent": float((np.mean(values) / nominal - 1.0) * 100.0),
        "mean_absolute_error_mm": float(np.mean(np.abs(errors))),
        "rmse_mm": float(np.sqrt(np.mean(errors**2))),
    }


def make_report(summary: dict[str, Any]) -> str:
    metrics = summary["square_metrics"]["all_edges"]
    horizontal = summary["square_metrics"]["horizontal"]
    vertical = summary["square_metrics"]["vertical"]
    quality = summary["quality"]
    lines = [
        "# ステレオ・チェッカーボード専用評価",
        "",
        f"- 判定: **{quality['status']}**",
        f"- 公称1マス: {summary['evaluation_square_mm']:.6f} mm",
        f"- 時間的に重ならない検出区間: {summary['non_overlapping_sample_count']} 区間",
        f"- 検出区間が分布する時間幅: {summary['sampling']['span_us'] / 1000.0:.3f} ms ({summary['sampling']['span_percent']:.3f}%)",
        f"- 対称エピポーラRMS: {summary['symmetric_epipolar_rms_px']:.6f} px",
        f"- 左右三角測量再投影RMS: {summary['left_reprojection_rms_px']:.6f} / {summary['right_reprojection_rms_px']:.6f} px",
        "",
        "## 距離評価",
        "",
        "| 対象 | 辺数 | 平均 [mm] | 平均誤差 [mm] | 平均誤差 [%] | RMSE [mm] |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for label, item in (
        ("全隣接辺", metrics),
        ("9交点方向（配列列方向）", horizontal),
        ("6交点方向（配列行方向）", vertical),
    ):
        lines.append(
            f"| {label} | {item['count']} | {item['mean_mm']:.6f} | "
            f"{item['mean_error_mm']:+.6f} | {item['mean_error_percent']:+.4f} | "
            f"{item['rmse_mm']:.6f} |"
        )
    lines.extend(["", "## 品質確認", ""])
    for check in quality["checks"]:
        marker = "OK" if check["passed"] else "要確認"
        lines.append(f"- {marker}: {check['message']}")
    lines.extend(
        [
            "",
            "## 解釈上の注意",
            "",
            "- 各サンプルには9交点方向48本・6交点方向45本、計93本の隣接辺があります。これらは同じ1枚のチェッカーボード上にあるため、93回の独立反復ではありません。",
            "- 9交点方向・6交点方向はチェッカーボード配列上の方向です。画像やカメラ座標の水平・垂直、XYZ軸を意味しません。",
            "- 時間的に重なるイベント積算窓は別サンプルとして採用していません。ただし、非重複でも時間的に近い区間同士は統計的に独立とは限りません。",
            "- エピポーラ誤差が大きい場合、距離誤差だけが小さくても校正が良いとは判断できません。",
            "- 校正後にカメラ相対姿勢が変わった場合と、撮影中のボード移動・イベント像の崩れは、この1記録だけでは分離できません。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    if args.square_mm <= 0:
        raise SystemExit("--square-mm must be positive.")
    if args.pattern_cols < 2 or args.pattern_rows < 2:
        raise SystemExit("Checkerboard pattern dimensions must both be at least 2.")
    if args.window_hop_us <= 0 or args.candidate_count <= 0:
        raise SystemExit("Window hop and candidate count must be positive.")
    if args.max_candidate_windows <= 0 or args.max_samples <= 0:
        raise SystemExit("Candidate/sample limits must be positive.")

    run_dir = args.run_dir.resolve()
    calibration_path = args.stereo_calibration.resolve()
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else run_dir / "checkerboard_evaluation"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    accepted_dir = output_dir / "accepted_detections"
    accepted_dir.mkdir(parents=True, exist_ok=True)
    left_npz = run_dir / "left" / "left_events.npz"
    right_npz = run_dir / "right" / "right_events.npz"
    for path in (left_npz, right_npz, calibration_path):
        if not path.exists():
            raise SystemExit(f"Required file not found: {path}")

    window_sizes = parse_positive_ints(args.window_us_list, "--window-us-list")
    render_modes = parse_render_modes(args.render_modes)
    start_us, end_us, recording_metadata = validate_recording_pair(
        left_npz, right_npz
    )
    calibration = load_calibration(calibration_path)
    width, height = calibration["image_size"]
    for side in ("left", "right"):
        calibration_serial = str(calibration[f"{side}_camera_serial"])
        recording_serial = str(recording_metadata[side]["camera_serial"])
        if calibration_serial and recording_serial and calibration_serial != recording_serial:
            raise SystemExit(
                f"{side} calibration serial {calibration_serial!r} does not match "
                f"recording serial {recording_serial!r}."
            )

    print(f"Run: {run_dir}")
    print(f"Calibration: {calibration_path}")
    print(f"Shared synchronized interval: [{start_us}, {end_us}) us")
    print("Memory-mapping event arrays...")
    left_events = load_events(left_npz)
    right_events = load_events(right_npz)
    print(f"Events: left={left_events.size:,}, right={right_events.size:,}")
    windows = candidate_windows(
        left_events,
        right_events,
        start_us,
        end_us,
        window_sizes,
        int(args.window_hop_us),
        int(args.min_events_per_camera),
        int(args.candidate_count),
        int(args.preferred_window_us),
    )[: int(args.max_candidate_windows)]
    print(f"Candidate windows: {len(windows)}")

    pattern_size = (int(args.pattern_cols), int(args.pattern_rows))
    detector_flags = cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY
    search_rows: list[dict[str, Any]] = []
    accepted_intervals: list[tuple[int, int]] = []
    samples: list[dict[str, Any]] = []
    edge_rows: list[dict[str, Any]] = []
    paired_detections = 0

    for candidate_index, window in enumerate(windows, start=1):
        left_window = left_events[window["left_begin"] : window["left_end"]]
        right_window = right_events[window["right_begin"] : window["right_end"]]
        accepted_this_window = False
        for mode in render_modes:
            left_image = checkerboard_capture.render_events(
                left_window,
                width,
                height,
                mode,
                float(args.scale_percentile),
            )
            right_image = checkerboard_capture.render_events(
                right_window,
                width,
                height,
                mode,
                float(args.scale_percentile),
            )
            left_found, left_corners = cv2.findChessboardCornersSB(
                left_image, pattern_size, flags=detector_flags
            )
            right_found, right_corners = cv2.findChessboardCornersSB(
                right_image, pattern_size, flags=detector_flags
            )
            row: dict[str, Any] = {
                "candidate_index": candidate_index,
                "start_ts_us": window["start_ts_us"],
                "end_ts_us": window["end_ts_us"],
                "window_us": window["frame_window_us"],
                "render_mode": mode,
                "left_events": window["left_events"],
                "right_events": window["right_events"],
                "left_found": int(bool(left_found)),
                "right_found": int(bool(right_found)),
                "both_found": int(bool(left_found and right_found)),
                "accepted_independent_sample": 0,
                "rejection_reason": "",
            }
            if not (left_found and right_found):
                search_rows.append(row)
                continue

            paired_detections += 1
            left_xy = np.asarray(left_corners, dtype=np.float64).reshape(-1, 2)
            right_xy, orientation, _alignment_rms = align_right_corners(
                left_xy,
                np.asarray(right_corners, dtype=np.float64).reshape(-1, 2),
                int(args.pattern_rows),
                int(args.pattern_cols),
                calibration["F"],
                calibration,
            )
            left_epi, right_epi, epi_rms = symmetric_epipolar_distances(
                left_xy, right_xy, calibration["F"]
            )
            interval = (int(window["start_ts_us"]), int(window["end_ts_us"]))
            if any(intervals_overlap(interval, existing) for existing in accepted_intervals):
                row["rejection_reason"] = "overlaps an accepted sample"
                row["corner_orientation"] = orientation
                row["symmetric_epipolar_rms_px"] = epi_rms
                search_rows.append(row)
                break

            points, left_reprojection, right_reprojection = triangulate_corners(
                left_xy, right_xy, calibration
            )
            if not np.all(np.isfinite(points)) or np.any(points[:, 2] <= 0):
                row["rejection_reason"] = "non-finite or non-positive triangulated depth"
                search_rows.append(row)
                break

            sample_id = len(samples) + 1
            corner_epipolar = np.sqrt((left_epi**2 + right_epi**2) / 2.0)
            sample_edges = edge_rows_for_sample(
                sample_id,
                points,
                int(args.pattern_rows),
                int(args.pattern_cols),
                float(args.square_mm),
            )
            edge_rows.extend(sample_edges)
            edge_values = np.asarray([item["measured_mm"] for item in sample_edges])
            sample = {
                "sample_id": sample_id,
                "start_ts_us": interval[0],
                "end_ts_us": interval[1],
                "window_us": int(window["frame_window_us"]),
                "render_mode": mode,
                "left_events": int(window["left_events"]),
                "right_events": int(window["right_events"]),
                "corner_orientation": orientation,
                "symmetric_epipolar_rms_px": epi_rms,
                "left_pnp_rms_px": pnp_rms(
                    left_xy,
                    calibration["left_k"],
                    calibration["left_dist"],
                    int(args.pattern_rows),
                    int(args.pattern_cols),
                    float(args.square_mm),
                ),
                "right_pnp_rms_px": pnp_rms(
                    right_xy,
                    calibration["right_k"],
                    calibration["right_dist"],
                    int(args.pattern_rows),
                    int(args.pattern_cols),
                    float(args.square_mm),
                ),
                "left_reprojection_rms_px": float(
                    np.sqrt(np.mean(left_reprojection**2))
                ),
                "right_reprojection_rms_px": float(
                    np.sqrt(np.mean(right_reprojection**2))
                ),
                "mean_depth_mm": float(np.mean(points[:, 2])),
                "plane_rms_mm": plane_rms_mm(points),
                "mean_square_mm": float(np.mean(edge_values)),
                "mean_square_error_mm": float(np.mean(edge_values) - args.square_mm),
                "mean_square_error_percent": float(
                    (np.mean(edge_values) / args.square_mm - 1.0) * 100.0
                ),
                "left_corners_px": left_xy,
                "right_corners_px": right_xy,
                "points_left_camera_mm": points,
                "corner_epipolar_px": corner_epipolar,
                "left_reprojection_px": left_reprojection,
                "right_reprojection_px": right_reprojection,
            }
            samples.append(sample)
            accepted_intervals.append(interval)
            row["accepted_independent_sample"] = 1
            row["corner_orientation"] = orientation
            row["symmetric_epipolar_rms_px"] = epi_rms
            search_rows.append(row)
            stem = f"sample_{sample_id:02d}_{interval[0]}_{mode}_{window['frame_window_us']}us"
            cv2.imwrite(str(accepted_dir / f"{stem}_left.png"), left_image)
            cv2.imwrite(str(accepted_dir / f"{stem}_right.png"), right_image)
            draw_detection_pair(
                accepted_dir / f"{stem}_corners.png",
                left_image,
                right_image,
                left_xy,
                right_xy,
                pattern_size,
            )
            print(
                f"[ACCEPT] sample={sample_id} t=[{interval[0]}, {interval[1]}) "
                f"mode={mode} epi={epi_rms:.3f}px square={np.mean(edge_values):.6f}mm"
            )
            accepted_this_window = True
            break
        if accepted_this_window and len(samples) >= int(args.max_samples):
            break

    search_fields = [
        "candidate_index",
        "start_ts_us",
        "end_ts_us",
        "window_us",
        "render_mode",
        "left_events",
        "right_events",
        "left_found",
        "right_found",
        "both_found",
        "accepted_independent_sample",
        "rejection_reason",
        "corner_orientation",
        "symmetric_epipolar_rms_px",
    ]
    write_csv(output_dir / "checkerboard_candidate_search.csv", search_rows, search_fields)
    if not samples:
        failure = {
            "schema_version": 1,
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "status": "no_paired_checkerboard_detection",
            "run_dir": str(run_dir),
            "stereo_calibration": str(calibration_path),
            "candidate_windows_available": len(windows),
            "tested_candidate_windows": len(
                {int(row["candidate_index"]) for row in search_rows}
            ),
            "tested_render_attempts": len(search_rows),
        }
        atomic_write_json(output_dir / "checkerboard_evaluation_summary.json", failure)
        print("No synchronized left/right checkerboard detection was found.")
        print(f"Candidate report: {output_dir / 'checkerboard_candidate_search.csv'}")
        return 3

    all_values = np.asarray([item["measured_mm"] for item in edge_rows])
    horizontal_values = np.asarray(
        [item["measured_mm"] for item in edge_rows if item["direction"] == "horizontal"]
    )
    vertical_values = np.asarray(
        [item["measured_mm"] for item in edge_rows if item["direction"] == "vertical"]
    )
    all_epi = np.concatenate([sample["corner_epipolar_px"] for sample in samples])
    left_reprojection = np.concatenate(
        [sample["left_reprojection_px"] for sample in samples]
    )
    right_reprojection = np.concatenate(
        [sample["right_reprojection_px"] for sample in samples]
    )
    epipolar_rms = float(np.sqrt(np.mean(all_epi**2)))
    left_reprojection_rms = float(np.sqrt(np.mean(left_reprojection**2)))
    right_reprojection_rms = float(np.sqrt(np.mean(right_reprojection**2)))
    all_metrics = metric_summary(all_values, float(args.square_mm))
    sampling_start_us = min(int(sample["start_ts_us"]) for sample in samples)
    sampling_end_us = max(int(sample["end_ts_us"]) for sample in samples)
    sampling_span_us = sampling_end_us - sampling_start_us
    recording_duration_us = end_us - start_us
    sampling_span_percent = sampling_span_us / recording_duration_us * 100.0
    checks = [
        {
            "name": "hardware_sync",
            "passed": True,
            "message": "左右NPZは同一ハードウェア時刻区間で同期しています。",
        },
        {
            "name": "sampling_coverage",
            "passed": len(samples) >= 2 and sampling_span_percent >= 20.0,
            "message": (
                f"非重複検出は{len(samples)}区間ですが、検出全体は"
                f"{sampling_span_us / 1000.0:.3f} ms "
                f"(収録時間の{sampling_span_percent:.3f}%)に分布しています。"
                + (
                    ""
                    if len(samples) >= 2 and sampling_span_percent >= 20.0
                    else "時間安定性の評価には収録中の離れた時刻での検出が必要です。"
                )
            ),
        },
        {
            "name": "epipolar_rms",
            "passed": epipolar_rms <= float(args.max_epipolar_rms_px),
            "message": (
                f"対称エピポーラRMS={epipolar_rms:.6f} px "
                f"(目安<={args.max_epipolar_rms_px:.3f} px)。"
            ),
        },
        {
            "name": "mean_square_error",
            "passed": abs(all_metrics["mean_error_percent"])
            <= float(args.max_square_error_percent),
            "message": (
                f"平均1マス誤差={all_metrics['mean_error_mm']:+.6f} mm "
                f"({all_metrics['mean_error_percent']:+.4f}%, "
                f"目安|誤差|<={args.max_square_error_percent:.3f}%)。"
            ),
        },
    ]
    quality_status = "PASS" if all(check["passed"] for check in checks) else "要確認"

    serializable_samples: list[dict[str, Any]] = []
    frame_rows: list[dict[str, Any]] = []
    corner_rows: list[dict[str, Any]] = []
    for sample in samples:
        public_sample = {
            key: value
            for key, value in sample.items()
            if not isinstance(value, np.ndarray)
        }
        serializable_samples.append(public_sample)
        frame_rows.append(public_sample)
        left_xy = sample["left_corners_px"]
        right_xy = sample["right_corners_px"]
        points = sample["points_left_camera_mm"]
        for index in range(int(args.pattern_cols) * int(args.pattern_rows)):
            corner_rows.append(
                {
                    "sample_id": sample["sample_id"],
                    "corner_index": index,
                    "row": index // int(args.pattern_cols),
                    "col": index % int(args.pattern_cols),
                    "left_x_px": left_xy[index, 0],
                    "left_y_px": left_xy[index, 1],
                    "right_x_px": right_xy[index, 0],
                    "right_y_px": right_xy[index, 1],
                    "x_left_camera_mm": points[index, 0],
                    "y_left_camera_mm": points[index, 1],
                    "z_left_camera_mm": points[index, 2],
                    "symmetric_epipolar_distance_px": sample["corner_epipolar_px"][index],
                    "left_reprojection_error_px": sample["left_reprojection_px"][index],
                    "right_reprojection_error_px": sample["right_reprojection_px"][index],
                }
            )

    summary: dict[str, Any] = {
        "schema_version": 1,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "left_events_npz": str(left_npz),
        "right_events_npz": str(right_npz),
        "stereo_calibration": str(calibration_path),
        "stereo_calibration_sha256": sha256_file(calibration_path),
        "calibration_metadata": {
            "training_square_mm": calibration["calibration_square_mm"],
            "stereo_rms_px": calibration["stereo_rms_px"],
            "accepted_pair_count": calibration["accepted_pair_count"],
            "left_camera_serial": calibration["left_camera_serial"],
            "right_camera_serial": calibration["right_camera_serial"],
            "baseline_mm": float(np.linalg.norm(calibration["T"])),
        },
        "recording_metadata": recording_metadata,
        "evaluation_square_mm": float(args.square_mm),
        "pattern_size_inner_corners": [int(args.pattern_cols), int(args.pattern_rows)],
        "shared_interval_us": [start_us, end_us],
        "candidate_windows_available": len(windows),
        "candidate_windows_tested": len(
            {int(row["candidate_index"]) for row in search_rows}
        ),
        "render_attempts_tested": len(search_rows),
        "paired_detections_including_overlaps": paired_detections,
        "non_overlapping_sample_count": len(samples),
        # Retained for compatibility with the initial schema. Non-overlap does
        # not imply statistical independence when samples are close in time.
        "independent_sample_count": len(samples),
        "sampling": {
            "first_sample_start_ts_us": sampling_start_us,
            "last_sample_end_ts_us": sampling_end_us,
            "span_us": sampling_span_us,
            "span_percent": sampling_span_percent,
            "recording_duration_us": recording_duration_us,
        },
        "samples": serializable_samples,
        "square_metrics": {
            "all_edges": all_metrics,
            "horizontal": metric_summary(horizontal_values, float(args.square_mm)),
            "vertical": metric_summary(vertical_values, float(args.square_mm)),
        },
        "symmetric_epipolar_rms_px": epipolar_rms,
        "left_reprojection_rms_px": left_reprojection_rms,
        "right_reprojection_rms_px": right_reprojection_rms,
        "quality": {"status": quality_status, "checks": checks},
    }
    atomic_write_json(output_dir / "checkerboard_evaluation_summary.json", summary)
    write_csv(
        output_dir / "checkerboard_samples.csv",
        frame_rows,
        [
            "sample_id",
            "start_ts_us",
            "end_ts_us",
            "window_us",
            "render_mode",
            "left_events",
            "right_events",
            "corner_orientation",
            "symmetric_epipolar_rms_px",
            "left_pnp_rms_px",
            "right_pnp_rms_px",
            "left_reprojection_rms_px",
            "right_reprojection_rms_px",
            "mean_depth_mm",
            "plane_rms_mm",
            "mean_square_mm",
            "mean_square_error_mm",
            "mean_square_error_percent",
        ],
    )
    write_csv(
        output_dir / "checkerboard_edges.csv",
        edge_rows,
        [
            "sample_id",
            "direction",
            "row",
            "col",
            "measured_mm",
            "error_mm",
            "error_percent",
        ],
    )
    write_csv(
        output_dir / "checkerboard_corners.csv",
        corner_rows,
        [
            "sample_id",
            "corner_index",
            "row",
            "col",
            "left_x_px",
            "left_y_px",
            "right_x_px",
            "right_y_px",
            "x_left_camera_mm",
            "y_left_camera_mm",
            "z_left_camera_mm",
            "symmetric_epipolar_distance_px",
            "left_reprojection_error_px",
            "right_reprojection_error_px",
        ],
    )
    plot_square_results(
        output_dir / "checkerboard_square_error.png",
        edge_rows,
        int(args.pattern_rows),
        int(args.pattern_cols),
        float(args.square_mm),
    )
    plot_corner_errors(
        output_dir / "checkerboard_corner_errors.png",
        samples,
        int(args.pattern_rows),
        int(args.pattern_cols),
    )
    plot_board_3d(
        output_dir / "checkerboard_reconstruction_3d.png",
        samples[0]["points_left_camera_mm"],
        int(args.pattern_rows),
        int(args.pattern_cols),
    )
    atomic_write_text(
        output_dir / "checkerboard_evaluation_report.md",
        make_report(summary),
        encoding="utf-8-sig",
    )

    print("\n=== Checkerboard evaluation ===")
    print(f"Non-overlapping samples: {len(samples)}")
    print(
        f"Sampling span: {sampling_span_us / 1000.0:.3f} ms "
        f"({sampling_span_percent:.3f}% of recording)"
    )
    print(f"Adjacent edges: {all_metrics['count']}")
    print(f"Nominal square [mm]: {args.square_mm:.6f}")
    print(f"Measured square [mm]: {all_metrics['mean_mm']:.6f}")
    print(
        f"Mean error: {all_metrics['mean_error_mm']:+.6f} mm "
        f"({all_metrics['mean_error_percent']:+.4f}%)"
    )
    print(f"Symmetric epipolar RMS [px]: {epipolar_rms:.6f}")
    print(
        "Triangulation reprojection RMS [px]: "
        f"left={left_reprojection_rms:.6f}, right={right_reprojection_rms:.6f}"
    )
    print(f"Quality: {quality_status}")
    print(f"Output: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
