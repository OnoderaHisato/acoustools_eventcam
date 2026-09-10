#!/usr/bin/env python3
"""Manifest and calibration utilities isolated for the heart delay A/B test."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Mapping

import numpy as np


DEFAULT_CALIBRATION = Path(
    "stereo_checkerboard_calib_extrinsics_20260805/"
    "stereo_calibration_square7p12_extrinsics_final.npz"
)

DEFAULT_PROCESSING_CONFIG: dict[str, Any] = {
    "window_us": 200,
    "hop_us": 100,
    "dt_us": 100.0,
    "max_interp_gap_sec": 0.005,
    "max_step_px": 15.0,
    "roi": "0,0,1280,720",
    "tracking_method": "event_weighted",
    "threshold_count": 1,
    "min_events": 20,
    "min_area": 5,
    "min_mass": 30,
    "polarity": "all",
    "max_time_gap_sec": 0.001,
    "max_reprojection_error_px": 3.0,
    "render_overlay": False,
    "auto_time_search_sec": 0.02,
    "auto_time_step_sec": 0.0001,
    "refine_led_time": False,
}
PROCESSING_CONFIG_FIELDS = tuple(DEFAULT_PROCESSING_CONFIG)


def processing_config_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        field: getattr(args, field, default)
        for field, default in DEFAULT_PROCESSING_CONFIG.items()
    }


def apply_manifest_processing_config(
    args: argparse.Namespace,
    manifest: Mapping[str, Any],
    explicit_fields: set[str] | None = None,
) -> None:
    config = manifest.get("processing_config", {})
    if not isinstance(config, Mapping):
        return
    explicit_fields = explicit_fields or set()
    for field in PROCESSING_CONFIG_FIELDS:
        if field in config and field not in explicit_fields:
            setattr(args, field, config[field])


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def validate_calibration(path: Path, left_serial: str, right_serial: str) -> Path:
    path = path.resolve()
    if not path.exists():
        raise SystemExit(f"Stereo calibration not found: {path}")
    with np.load(path, allow_pickle=False) as data:
        required = {
            "left_camera_matrix",
            "left_dist_coeffs",
            "right_camera_matrix",
            "right_dist_coeffs",
            "R",
            "T",
        }
        missing = required.difference(data.files)
        if missing:
            raise SystemExit(f"Stereo calibration missing keys: {sorted(missing)}")
        calibrated_left = (
            str(np.asarray(data["left_camera_serial"]).item())
            if "left_camera_serial" in data
            else ""
        )
        calibrated_right = (
            str(np.asarray(data["right_camera_serial"]).item())
            if "right_camera_serial" in data
            else ""
        )
    if calibrated_left and calibrated_left != str(left_serial):
        raise SystemExit(
            f"Left camera serial mismatch: calibration={calibrated_left}, requested={left_serial}"
        )
    if calibrated_right and calibrated_right != str(right_serial):
        raise SystemExit(
            f"Right camera serial mismatch: calibration={calibrated_right}, requested={right_serial}"
        )
    return path
