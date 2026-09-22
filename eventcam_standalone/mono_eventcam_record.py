#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Record one event camera into a run directory and export a tracking-ready NPZ.

Single camera only: no stereo pairing, no hardware Master/Slave synchronization,
no PAT or actuator control. The exported NPZ uses the same keys the bundled
``eventcam_npz_track.py`` and ``eventcam_npz_render_video.py`` already read.

The camera handling itself is reused from ``eventcam_scale_calibration_capture``
rather than reimplemented, so this entry point shares the preview, RAW logging
and RAW-to-NPZ export that are already in use. Only the circle fit is dropped.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import eventcam_scale_calibration_capture as mono_capture

BASENAME = "mono"
MANIFEST_NAME = "mono_recording_manifest.json"


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--serial", default="", help="Camera serial. Empty opens the first detected camera.")
    parser.add_argument("--list-devices", action="store_true", help="List detected devices and exit.")
    parser.add_argument("--run-dir", default="", help="Run directory to create. Required unless --list-devices.")
    parser.add_argument(
        "--duration-sec",
        type=float,
        default=0.0,
        help="Auto-stop duration after Enter starts the recording. 0 means a second Enter stops it.",
    )
    parser.add_argument("--delta-t-us", type=int, default=10_000, help="Live event slice duration during capture.")
    parser.add_argument("--npz-delta-t-us", type=int, default=1_000, help="RAW read slice duration when exporting NPZ.")
    parser.add_argument("--preview-fps", type=float, default=25.0, help="Preview update FPS.")
    parser.add_argument("--preview-accumulation-us", type=int, default=10_000, help="Preview event accumulation time.")
    parser.add_argument("--post-stop-wait-sec", type=float, default=0.2, help="Wait after stopping RAW logging before reading it.")
    parser.add_argument("--note", default="", help="Free-form note stored in the manifest.")
    return parser.parse_args(argv)


def capture_namespace(args: argparse.Namespace, output_dir: str, *, list_devices: bool) -> SimpleNamespace:
    """Build exactly the attributes capture_raw() and export_raw_to_npz() read."""
    return SimpleNamespace(
        list_devices=list_devices,
        serial=args.serial,
        output_dir=output_dir,
        basename=BASENAME,
        delta_t_us=int(args.delta_t_us),
        duration_sec=float(args.duration_sec),
        post_stop_wait_sec=float(args.post_stop_wait_sec),
        preview_fps=float(args.preview_fps),
        preview_accumulation_us=int(args.preview_accumulation_us),
        npz_delta_t_us=int(args.npz_delta_t_us),
    )


def validate(args: argparse.Namespace) -> Path:
    if args.delta_t_us <= 0:
        raise ValueError("--delta-t-us must be positive")
    if args.npz_delta_t_us <= 0:
        raise ValueError("--npz-delta-t-us must be positive")
    if args.preview_fps <= 0:
        raise ValueError("--preview-fps must be positive")
    if args.preview_accumulation_us <= 0:
        raise ValueError("--preview-accumulation-us must be positive")
    if args.duration_sec < 0:
        raise ValueError("--duration-sec must be zero or positive")
    if args.post_stop_wait_sec < 0:
        raise ValueError("--post-stop-wait-sec must be zero or positive")
    if not str(args.run_dir).strip():
        raise ValueError("--run-dir is required unless --list-devices is used")
    run_dir = Path(args.run_dir).resolve()
    if run_dir.exists():
        raise ValueError(f"Refusing to reuse an existing run directory: {run_dir}. Choose a new name.")
    return run_dir


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        if args.list_devices:
            # capture_raw prints the device list and exits before touching output_dir.
            mono_capture.capture_raw(capture_namespace(args, ".", list_devices=True))
            return 0

        run_dir = validate(args)
        run_dir.mkdir(parents=True, exist_ok=False)
        capture = capture_namespace(args, str(run_dir), list_devices=False)

        if not args.serial:
            print(
                "[MONO] No --serial given; the first detected camera will be opened. "
                "Pass --serial to record a specific camera.",
                flush=True,
            )

        raw_path, summary_path, stats = mono_capture.capture_raw(capture)
        npz_path = mono_capture.export_raw_to_npz(raw_path, capture)

        manifest = {
            "schema_version": 1,
            "kind": "mono_event_recording",
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "camera_serial": args.serial,
            "stereo": False,
            "hardware_synchronized": False,
            "external_actuator_control": False,
            "raw": raw_path.name,
            "npz": npz_path.name,
            "capture_summary": summary_path.name,
            "settings": {
                "duration_sec": float(args.duration_sec),
                "delta_t_us": int(args.delta_t_us),
                "npz_delta_t_us": int(args.npz_delta_t_us),
                "preview_fps": float(args.preview_fps),
                "preview_accumulation_us": int(args.preview_accumulation_us),
            },
            "stats": stats,
            "note": args.note,
        }
        manifest_path = run_dir / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        summary_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        print(f"[MONO] Run directory: {run_dir}")
        print(f"[MONO] RAW: {raw_path.name}")
        print(f"[MONO] NPZ: {npz_path.name}")
        print(f"[MONO] Manifest: {manifest_path.name}")
        print(f"[MONO] Events: {stats.get('total_events', 0)}")
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"[ERROR] {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted; preserved existing files.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
