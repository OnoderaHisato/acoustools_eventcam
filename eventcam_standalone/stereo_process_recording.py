#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run left/right 2D tracking and stereo triangulation for one recording run."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process a stereo_eventcam_record_sync.py run: track both NPZs, then triangulate 3D.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("run_dir", type=Path, help="Run folder created by stereo_eventcam_record_sync.py.")
    parser.add_argument(
        "--stereo-calibration",
        type=Path,
        required=True,
        help="Stereo calibration NPZ.",
    )
    parser.add_argument("--window-us", type=int, default=100, help="Tracking integration window.")
    parser.add_argument("--hop-us", type=int, default=50, help="Tracking hop.")
    parser.add_argument("--dt-us", type=float, default=50.0, help="Interpolated tracking dt.")
    parser.add_argument(
        "--max-interp-gap-sec",
        type=float,
        default=0.0,
        help="Do not interpolate a 2D track across a longer missing interval. 0 disables.",
    )
    parser.add_argument(
        "--max-step-px",
        type=float,
        default=0.0,
        help="Reject a raw 2D centroid jump larger than this. 0 disables.",
    )
    parser.add_argument("--t-start-sec", type=float, default=0.0, help="Tracking start time.")
    parser.add_argument("--t-end-sec", type=float, default=0.0, help="Tracking end time. 0 uses all events.")
    parser.add_argument("--roi", default="0,0,1280,720", help="Tracking ROI.")
    parser.add_argument("--left-mask-roi", default="", help="ROI excluded only from left-camera tracking.")
    parser.add_argument("--right-mask-roi", default="", help="ROI excluded only from right-camera tracking.")
    parser.add_argument("--tracking-method", default="event_weighted", help="Tracking method passed to eventcam_npz_track.py.")
    parser.add_argument("--threshold-count", type=int, default=1, help="Pixel event-count threshold.")
    parser.add_argument("--min-events", type=int, default=20, help="Minimum events in a bin.")
    parser.add_argument("--min-area", type=int, default=5, help="Minimum connected-component area.")
    parser.add_argument("--min-mass", type=int, default=30, help="Minimum connected-component mass.")
    parser.add_argument("--polarity", choices=["all", "on", "off"], default="all", help="Event polarity.")
    parser.add_argument(
        "--right-time-offset-sec",
        type=float,
        default=None,
        help=(
            "Right track time offset for triangulation. By default it is derived "
            "from synchronized NPZ timestamp origins; unsynchronized inputs default to 0 with a warning."
        ),
    )
    parser.add_argument("--max-time-gap-sec", type=float, default=0.001, help="Max time support gap for triangulation.")
    parser.add_argument(
        "--max-reprojection-error-px",
        type=float,
        default=0.0,
        help="Reject stereo pairs exceeding this reprojection error in either camera. 0 disables.",
    )
    parser.add_argument(
        "--skip-tracking",
        action="store_true",
        help=(
            "Reuse existing tracking CSV files for manual diagnostics. "
            "Strict three-axis stage analysis rejects this provenance."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Reuse each completed camera-tracking stage only when its source NPZ "
            "and tracking configuration match; otherwise regenerate that side."
        ),
    )
    parser.add_argument(
        "--skip-plot",
        action="store_true",
        help="Skip the per-recording 3D diagnostic plot (useful for batch analysis).",
    )
    parser.add_argument("--render-overlay", action="store_true", help="Render left/right event videos with raw tracking overlays after processing.")
    parser.add_argument("--overlay-fps", type=float, default=1000.0, help="Event render rate for overlay videos.")
    parser.add_argument("--overlay-video-fps", type=float, default=30.0, help="Playback FPS in overlay video headers.")
    parser.add_argument("--overlay-accumulation-us", type=int, default=1000, help="Event accumulation window per overlay frame.")
    parser.add_argument("--overlay-tracking", choices=["raw", "interp"], default="raw", help="Tracking CSV to overlay.")
    parser.add_argument("--overlay-trail-sec", type=float, default=0.020, help="Overlay trail length.")
    parser.add_argument("--overlay-crop", default="", help="Optional crop x0,y0,x1,y1 applied to both overlay videos.")
    parser.add_argument("--overlay-scale", type=float, default=1.0, help="Optional output scale after overlay crop.")
    args = parser.parse_args()
    if args.skip_tracking and args.resume:
        parser.error("--skip-tracking and --resume cannot be used together")
    return args


def run_command(command: list[str]) -> None:
    print(" ".join(command), flush=True)
    subprocess.run(command, check=True)


TRACKING_RESUME_OUTPUTS = (
    "event_centres_raw.csv",
    "event_centres_interp.csv",
    "event_centres_interp.npy",
    "event_tracking_meta.json",
    "event_tracking_summary.json",
)


def _roi_values(text: str) -> list[int]:
    values = [int(value.strip()) for value in str(text).split(",") if value.strip()]
    if len(values) != 4:
        raise ValueError(f"Expected x0,y0,x1,y1 ROI, got {text!r}")
    return values


def tracking_resume_config(
    args: argparse.Namespace,
    input_npz: Path,
    mask_roi: str,
) -> dict[str, object]:
    mask_text = str(mask_roi).strip()
    return {
        "input_npz": str(input_npz.resolve()),
        "input_size_bytes": input_npz.stat().st_size,
        "input_mtime_ns": input_npz.stat().st_mtime_ns,
        "window_us": int(args.window_us),
        "hop_us": int(args.hop_us),
        "dt_us": float(args.dt_us),
        "max_interp_gap_sec": float(args.max_interp_gap_sec),
        "max_step_px": float(args.max_step_px),
        "t_start_sec": float(args.t_start_sec),
        "t_end_sec": float(args.t_end_sec),
        "roi": _roi_values(str(args.roi)),
        "mask_rois": [_roi_values(mask_text)] if mask_text else [],
        "tracking_method": str(args.tracking_method),
        "threshold_count": int(args.threshold_count),
        "min_events": int(args.min_events),
        "min_area": int(args.min_area),
        "min_mass": int(args.min_mass),
        "polarity": str(args.polarity),
    }


def _load_json_object(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _tracking_outputs_complete(track_dir: Path) -> bool:
    return all(
        (track_dir / name).is_file() and (track_dir / name).stat().st_size > 0
        for name in TRACKING_RESUME_OUTPUTS
    )


def _legacy_tracking_metadata_matches(
    track_dir: Path,
    expected: dict[str, object],
) -> bool:
    """Validate outputs written before resume checkpoints were introduced."""
    meta = _load_json_object(track_dir / "event_tracking_meta.json")
    summary = _load_json_object(track_dir / "event_tracking_summary.json")
    if meta is None or summary is None:
        return False
    recorded_input = Path(str(meta.get("input_npz", ""))).resolve()
    expected_input = Path(str(expected["input_npz"])).resolve()
    expected_end_us = (
        None
        if float(expected["t_end_sec"]) <= 0
        else round(float(expected["t_end_sec"]) * 1_000_000)
    )
    checks = (
        recorded_input == expected_input,
        int(meta.get("window_us", -1)) == expected["window_us"],
        int(meta.get("hop_us", -1)) == expected["hop_us"],
        float(meta.get("dt_unified_us", -1.0)) == expected["dt_us"],
        meta.get("roi") == expected["roi"],
        meta.get("mask_rois", []) == expected["mask_rois"],
        str(meta.get("tracking_method", "")) == expected["tracking_method"],
        str(meta.get("polarity", "")) == expected["polarity"],
        int(meta.get("min_mass", -1)) == expected["min_mass"],
        int(summary.get("threshold_count", -1)) == expected["threshold_count"],
        int(summary.get("min_events", -1)) == expected["min_events"],
        int(summary.get("min_area", -1)) == expected["min_area"],
        int(meta.get("analysis_start_us", -1))
        == round(float(expected["t_start_sec"]) * 1_000_000),
        meta.get("analysis_end_us") == expected_end_us,
    )
    return all(checks)


def write_tracking_resume_checkpoint(
    track_dir: Path,
    config: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "schema_version": 1,
        "created_at": dt.datetime.now().isoformat(timespec="milliseconds"),
        "config": config,
        "outputs": {
            name: {"size_bytes": (track_dir / name).stat().st_size}
            for name in TRACKING_RESUME_OUTPUTS
        },
    }
    atomic_write_json(track_dir / "tracking_resume_checkpoint.json", payload)


def tracking_can_resume(
    track_dir: Path,
    expected: dict[str, object],
) -> tuple[bool, str]:
    if not _tracking_outputs_complete(track_dir):
        return False, "required tracking outputs are incomplete"
    checkpoint = _load_json_object(track_dir / "tracking_resume_checkpoint.json")
    if checkpoint is not None:
        if checkpoint.get("config") != expected:
            return False, "resume checkpoint configuration does not match"
        recorded_outputs = checkpoint.get("outputs")
        if not isinstance(recorded_outputs, dict):
            return False, "resume checkpoint output inventory is invalid"
        for name in TRACKING_RESUME_OUTPUTS:
            entry = recorded_outputs.get(name)
            if (
                not isinstance(entry, dict)
                or int(entry.get("size_bytes", -1)) != (track_dir / name).stat().st_size
            ):
                return False, f"resume checkpoint does not match {name}"
        return True, "validated resume checkpoint"
    if _legacy_tracking_metadata_matches(track_dir, expected):
        return True, "validated legacy tracking metadata"
    return False, "existing outputs do not match the requested tracking configuration"


def npz_scalar(data: Any, key: str, default: Any) -> Any:
    if key not in data.files:
        return default
    value = np.asarray(data[key])
    return value.item() if value.ndim == 0 else default


def load_timing_metadata(path: Path) -> dict[str, object]:
    with np.load(path, allow_pickle=False) as data:
        events = np.asarray(data["events"]) if "events" in data.files else np.empty(0)
        fallback_start = int(events["t"][0]) if events.size and events.dtype.names and "t" in events.dtype.names else 0
        return {
            "output_start_ts_us": int(npz_scalar(data, "output_start_ts_us", fallback_start)),
            "output_end_ts_us": int(npz_scalar(data, "output_end_ts_us", fallback_start)),
            "sync_ts_us": int(npz_scalar(data, "sync_ts_us", -1)),
            "sync_role": str(npz_scalar(data, "sync_role", "")),
            "sync_mode_applied": str(npz_scalar(data, "sync_mode_applied", "")),
            "sync_mode_verified": bool(npz_scalar(data, "sync_mode_verified", False)),
            "hardware_synchronized": bool(npz_scalar(data, "hardware_synchronized", False)),
            "timestamp_domain_id": str(npz_scalar(data, "timestamp_domain_id", "")),
            "capture_interval_complete": bool(npz_scalar(data, "capture_interval_complete", False)),
            "event_limit_reached": bool(npz_scalar(data, "event_limit_reached", False)),
            "has_hardware_sync_metadata": "hardware_synchronized" in data.files,
        }


def load_manifest_sync_status(run_dir: Path) -> dict[str, object]:
    path = run_dir / "stereo_recording_manifest.json"
    if not path.exists():
        return {"requested": False, "verified": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"requested": False, "verified": None}
    hw_sync = payload.get("hw_sync", {})
    return {
        "requested": str(hw_sync.get("mode", "off")) != "off",
        "verified": hw_sync.get("verified") if "verified" in hw_sync else None,
    }


def resolve_right_time_offset_sec(
    *,
    explicit_offset_sec: float | None,
    left_meta: dict[str, object],
    right_meta: dict[str, object],
    manifest_sync_status: dict[str, object],
) -> tuple[float, str]:
    if explicit_offset_sec is not None:
        return float(explicit_offset_sec), "explicit command-line value"

    roles = {
        str(left_meta.get("sync_mode_applied") or left_meta.get("sync_role") or ""),
        str(right_meta.get("sync_mode_applied") or right_meta.get("sync_role") or ""),
    }
    left_domain = str(left_meta.get("timestamp_domain_id") or "")
    right_domain = str(right_meta.get("timestamp_domain_id") or "")
    same_declared_domain = bool(left_domain and left_domain == right_domain and left_domain != "unsynchronized")
    sync_marker_matches = (
        int(left_meta.get("sync_ts_us", -1)) >= 0
        and int(left_meta.get("sync_ts_us", -1)) == int(right_meta.get("sync_ts_us", -1))
    )
    new_metadata_present = bool(
        left_meta.get("has_hardware_sync_metadata") and right_meta.get("has_hardware_sync_metadata")
    )
    common_window_matches = (
        int(left_meta["output_start_ts_us"]) == int(right_meta["output_start_ts_us"])
        and int(left_meta["output_end_ts_us"]) == int(right_meta["output_end_ts_us"])
        and int(left_meta["sync_ts_us"]) == int(left_meta["output_start_ts_us"])
        and int(right_meta["sync_ts_us"]) == int(right_meta["output_start_ts_us"])
    )
    verified_new_recording = (
        new_metadata_present
        and bool(left_meta.get("hardware_synchronized"))
        and bool(right_meta.get("hardware_synchronized"))
        and bool(left_meta.get("sync_mode_verified"))
        and bool(right_meta.get("sync_mode_verified"))
        and bool(left_meta.get("capture_interval_complete"))
        and bool(right_meta.get("capture_interval_complete"))
        and not bool(left_meta.get("event_limit_reached"))
        and not bool(right_meta.get("event_limit_reached"))
        and same_declared_domain
        and roles == {"master", "slave"}
        and sync_marker_matches
        and common_window_matches
        and manifest_sync_status.get("verified") is not False
    )
    legacy_synchronized_recording = (
        not new_metadata_present
        and roles == {"master", "slave"}
        and bool(manifest_sync_status.get("requested"))
    )
    synchronized = bool(verified_new_recording or legacy_synchronized_recording or sync_marker_matches)

    if not synchronized:
        if manifest_sync_status.get("requested"):
            raise SystemExit(
                "Hardware synchronization was requested, but the NPZ/manifest metadata does not verify "
                "a shared timestamp domain. Refusing automatic stereo time alignment."
            )
        return 0.0, "unsynchronized inputs; no reliable automatic correction is possible"

    if left_domain and right_domain and left_domain != right_domain:
        raise SystemExit(
            "The NPZ files claim hardware synchronization but use different timestamp domains: "
            f"left={left_domain!r}, right={right_domain!r}"
        )

    left_start = int(left_meta["output_start_ts_us"])
    right_start = int(right_meta["output_start_ts_us"])
    offset_sec = (right_start - left_start) / 1_000_000.0
    return offset_sec, "derived from the synchronized NPZ output_start_ts_us values"


def main() -> int:
    args = parse_args()
    run_dir = args.run_dir.resolve()
    left_npz = run_dir / "left" / "left_events.npz"
    right_npz = run_dir / "right" / "right_events.npz"
    if not left_npz.exists():
        raise SystemExit(f"Left NPZ not found: {left_npz}")
    if not right_npz.exists():
        raise SystemExit(f"Right NPZ not found: {right_npz}")

    left_timing = load_timing_metadata(left_npz)
    right_timing = load_timing_metadata(right_npz)
    right_time_offset_sec, offset_source = resolve_right_time_offset_sec(
        explicit_offset_sec=args.right_time_offset_sec,
        left_meta=left_timing,
        right_meta=right_timing,
        manifest_sync_status=load_manifest_sync_status(run_dir),
    )
    print(f"Right time offset: {right_time_offset_sec:.9f} s ({offset_source})")
    if "unsynchronized" in offset_source:
        print(
            "[WARN] Left/right timestamps are not hardware synchronized. "
            "Use --right-time-offset-sec only if an independent calibration provides the offset."
        )

    script_dir = Path(__file__).resolve().parent
    python = sys.executable
    track_script = script_dir / "eventcam_npz_track.py"
    triangulate_script = script_dir / "stereo_triangulate_tracks.py"
    plot_script = script_dir / "stereo_plot_3d_points.py"
    render_script = script_dir / "eventcam_npz_render_video.py"
    left_track_dir = run_dir / "left" / "event_tracking"
    right_track_dir = run_dir / "right" / "event_tracking"
    tracking_modes = {
        "left": "generated_from_raw_events",
        "right": "generated_from_raw_events",
    }

    if not args.skip_tracking:
        common = [
            "--bin-us",
            str(int(args.window_us)),
            "--window-us",
            str(int(args.window_us)),
            "--hop-us",
            str(int(args.hop_us)),
            "--dt-us",
            str(float(args.dt_us)),
            "--max-interp-gap-sec",
            str(float(args.max_interp_gap_sec)),
            "--max-step-px",
            str(float(args.max_step_px)),
            "--t-start-sec",
            str(float(args.t_start_sec)),
            "--t-end-sec",
            str(float(args.t_end_sec)),
            "--roi",
            str(args.roi),
            "--tracking-method",
            str(args.tracking_method),
            "--threshold-count",
            str(int(args.threshold_count)),
            "--min-events",
            str(int(args.min_events)),
            "--min-area",
            str(int(args.min_area)),
            "--min-mass",
            str(int(args.min_mass)),
            "--polarity",
            str(args.polarity),
        ]
        left_command = [python, str(track_script), str(left_npz), "--output-dir", str(left_track_dir), *common]
        right_command = [python, str(track_script), str(right_npz), "--output-dir", str(right_track_dir), *common]
        if str(args.left_mask_roi).strip():
            left_command.extend(["--mask-roi", str(args.left_mask_roi)])
        if str(args.right_mask_roi).strip():
            right_command.extend(["--mask-roi", str(args.right_mask_roi)])
        tracking_jobs = (
            ("left", left_npz, left_track_dir, str(args.left_mask_roi), left_command),
            ("right", right_npz, right_track_dir, str(args.right_mask_roi), right_command),
        )
        for side, input_npz, track_dir, mask_roi, command in tracking_jobs:
            resume_config = tracking_resume_config(args, input_npz, mask_roi)
            reusable, reason = (
                tracking_can_resume(track_dir, resume_config)
                if args.resume
                else (False, "resume disabled")
            )
            if reusable:
                print(
                    f"[RESUME] {side} tracking: {reason}; skipping extraction.",
                    flush=True,
                )
                tracking_modes[side] = "reused_validated_tracking"
                if not (track_dir / "tracking_resume_checkpoint.json").exists():
                    write_tracking_resume_checkpoint(track_dir, resume_config)
                continue
            if args.resume and track_dir.exists():
                print(
                    f"[RESUME] {side} tracking cannot be reused ({reason}); regenerating.",
                    flush=True,
                )
            run_command(command)
            if not _tracking_outputs_complete(track_dir):
                raise RuntimeError(
                    f"{side.capitalize()} tracking command returned without complete outputs"
                )
            write_tracking_resume_checkpoint(track_dir, resume_config)

    left_track = left_track_dir / "event_centres_interp.csv"
    right_track = right_track_dir / "event_centres_interp.csv"
    if not left_track.exists():
        raise SystemExit(f"Left tracking CSV not found: {left_track}")
    if not right_track.exists():
        raise SystemExit(f"Right tracking CSV not found: {right_track}")

    run_command(
        [
            python,
            str(triangulate_script),
            "--left-track",
            str(left_track),
            "--right-track",
            str(right_track),
            "--stereo-calibration",
            str(args.stereo_calibration),
            "--output-dir",
            str(run_dir / "stereo_3d"),
            "--right-time-offset-sec",
            str(float(right_time_offset_sec)),
            "--max-time-gap-sec",
            str(float(args.max_time_gap_sec)),
            "--max-reprojection-error-px",
            str(float(args.max_reprojection_error_px)),
        ]
    )
    output_npz = run_dir / "stereo_3d" / "stereo_3d_points.npz"
    source_paths = {
        "stereo_process_recording.py": Path(__file__).resolve(),
        "eventcam_npz_track.py": track_script.resolve(),
        "stereo_triangulate_tracks.py": triangulate_script.resolve(),
    }
    provenance: dict[str, object] = {
        "schema_version": 1,
        "created_at": dt.datetime.now().isoformat(timespec="milliseconds"),
        "run_dir": str(run_dir),
        "raw_inputs": {
            "left_events_npz": str(left_npz),
            "left_events_sha256": file_sha256(left_npz),
            "right_events_npz": str(right_npz),
            "right_events_sha256": file_sha256(right_npz),
        },
        "stereo_calibration": str(args.stereo_calibration.resolve()),
        "stereo_calibration_sha256": file_sha256(
            args.stereo_calibration.resolve()
        ),
        "tracking": {
            "window_us": int(args.window_us),
            "hop_us": int(args.hop_us),
            "dt_us": float(args.dt_us),
            "max_interp_gap_sec": float(args.max_interp_gap_sec),
            "max_step_px": float(args.max_step_px),
            "t_start_sec": float(args.t_start_sec),
            "t_end_sec": float(args.t_end_sec),
            "roi": str(args.roi),
            "left_mask_roi": str(args.left_mask_roi),
            "right_mask_roi": str(args.right_mask_roi),
            "tracking_method": str(args.tracking_method),
            "threshold_count": int(args.threshold_count),
            "min_events": int(args.min_events),
            "min_area": int(args.min_area),
            "min_mass": int(args.min_mass),
            "polarity": str(args.polarity),
            "max_time_gap_sec": float(args.max_time_gap_sec),
            "max_reprojection_error_px": float(args.max_reprojection_error_px),
            "right_time_offset_requested_sec": (
                float(args.right_time_offset_sec)
                if args.right_time_offset_sec is not None
                else None
            ),
            "right_time_offset_resolved_sec": float(
                right_time_offset_sec
            ),
            "right_time_offset_source": str(offset_source),
        },
        "tracking_artifacts": {
            "mode": (
                "reused_existing_csv"
                if args.skip_tracking
                else ("per_side_resume" if args.resume else "generated_from_raw_events")
            ),
            "per_side_mode": tracking_modes,
            "left_interp_csv": str(left_track),
            "left_interp_csv_sha256": file_sha256(left_track),
            "right_interp_csv": str(right_track),
            "right_interp_csv_sha256": file_sha256(right_track),
        },
        "source_sha256": {
            name: file_sha256(path) for name, path in source_paths.items()
        },
        "output_npz": str(output_npz),
        "output_npz_sha256": file_sha256(output_npz),
        "diagnostic_plot_requested": not bool(args.skip_plot),
        "postprocessing_complete": False,
    }
    processing_manifest = run_dir / "stereo_3d" / "processing_manifest.json"
    # Commit core provenance immediately after triangulation.  Optional
    # visualisation failure must not orphan an otherwise valid 3D result.
    atomic_write_json(processing_manifest, provenance)

    if not args.skip_plot:
        run_command(
            [
                python,
                str(plot_script),
                str(run_dir / "stereo_3d" / "stereo_3d_points.npz"),
            ]
        )
    if args.render_overlay:
        tracking_name = "event_centres_raw.csv" if args.overlay_tracking == "raw" else "event_centres_interp.csv"
        overlay_common = [
            "--fps",
            str(float(args.overlay_fps)),
            "--video-fps",
            str(float(args.overlay_video_fps)),
            "--accumulation-us",
            str(int(args.overlay_accumulation_us)),
            "--duration-sec",
            "0",
            "--trail-sec",
            str(float(args.overlay_trail_sec)),
            "--draw-time",
        ]
        if args.overlay_crop:
            overlay_common.extend(["--crop", str(args.overlay_crop)])
        if args.overlay_scale != 1.0:
            overlay_common.extend(["--scale", str(float(args.overlay_scale))])
        run_command(
            [
                python,
                str(render_script),
                str(left_npz),
                "--output",
                str(run_dir / "left" / f"left_events_{args.overlay_tracking}_overlay.mp4"),
                "--tracking-csv",
                str(left_track_dir / tracking_name),
                *overlay_common,
            ]
        )
        run_command(
            [
                python,
                str(render_script),
                str(right_npz),
                "--output",
                str(run_dir / "right" / f"right_events_{args.overlay_tracking}_overlay.mp4"),
                "--tracking-csv",
                str(right_track_dir / tracking_name),
                *overlay_common,
            ]
        )
    provenance["postprocessing_complete"] = True
    provenance["postprocessing_completed_at"] = dt.datetime.now().isoformat(
        timespec="milliseconds"
    )
    atomic_write_json(processing_manifest, provenance)
    print(f"Stereo 3D output: {run_dir / 'stereo_3d'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
