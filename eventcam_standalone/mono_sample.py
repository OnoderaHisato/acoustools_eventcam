#!/usr/bin/env python3
"""Single-camera sample: record, extract particle centres, render an overlay video.

No stereo, no hardware synchronization, no stereo calibration and no 3D. Only the
standard library is imported until an action runs, and --dry-run neither imports a
camera SDK nor starts a subprocess nor writes a file. All paths are anchored to
this directory, independent of the shell working directory.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RECORDS = ROOT / "mono_records"
TRACK_DIR_NAME = "event_tracking"
INTERP_CSV = "event_centres_interp.csv"
VIDEO_NAME = "mono_overlay.mp4"
ACTIONS = ("devices", "record", "track", "video", "all")


def command(script: str, *args) -> list[str]:
    path = ROOT / script
    if not path.is_file():
        raise ValueError(f"Missing bundled script: {path}")
    return [sys.executable, str(path), *(str(value) for value in args)]


def run_directory(name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", name or ""):
        raise ValueError("--run must be a short directory name using letters, numbers, _ or -")
    return (RECORDS / name).resolve()


def find_npz(run: Path) -> Path:
    matches = sorted(run.glob("*_events.npz"))
    if not matches:
        raise ValueError(f"No *_events.npz in {run}. Record first, or check the run name.")
    if len(matches) > 1:
        raise ValueError(f"More than one *_events.npz in {run}; keep one recording per run directory.")
    return matches[0]


def record_command(run: Path, args: argparse.Namespace) -> list[str]:
    return command(
        "mono_eventcam_record.py",
        "--serial", args.serial,
        "--run-dir", run,
        "--duration-sec", args.duration_sec,
        "--delta-t-us", args.delta_t_us,
        "--npz-delta-t-us", args.npz_delta_t_us,
        "--note", "Standalone single-camera sample; no synchronization or actuator control",
    )


def track_command(npz: Path, run: Path, args: argparse.Namespace) -> list[str]:
    extra: list[str] = []
    if args.roi:
        extra += ["--roi", args.roi]
    if args.mask_roi:
        extra += ["--mask-roi", args.mask_roi]
    return command(
        "eventcam_npz_track.py", npz,
        "--output-dir", run / TRACK_DIR_NAME,
        "--window-us", args.window_us,
        "--hop-us", args.hop_us,
        "--dt-us", args.dt_us,
        "--max-interp-gap-sec", args.max_interp_gap_sec,
        "--max-step-px", args.max_step_px,
        "--tracking-method", args.tracking_method,
        "--threshold-count", args.threshold_count,
        "--min-events", args.min_events,
        "--min-area", args.min_area,
        "--min-mass", args.min_mass,
        "--polarity", args.polarity,
        *extra,
    )


def video_command(npz: Path, run: Path, args: argparse.Namespace) -> list[str]:
    extra: list[str] = []
    track_csv = run / TRACK_DIR_NAME / INTERP_CSV
    if track_csv.exists():
        extra += ["--tracking-csv", track_csv]
    return command(
        "eventcam_npz_render_video.py", npz,
        "--output", run / VIDEO_NAME,
        "--fps", args.video_fps,
        "--video-fps", args.video_playback_fps,
        "--accumulation-us", args.video_accumulation_us,
        "--duration-sec", args.video_duration_sec,
        "--draw-time",
        *extra,
    )


def require_fresh(path: Path) -> None:
    if path.exists():
        raise ValueError(f"Refusing to overwrite existing output: {path}. Choose a new --run name.")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--run", default="", help="Run directory name under mono_records/. Required except for devices.")
    parser.add_argument("--serial", default="", help="Camera serial. Empty opens the first detected camera.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands only; no SDK import, subprocess or file writes.")

    capture = parser.add_argument_group("capture")
    capture.add_argument("--duration-sec", type=float, default=2.0, help="Auto-stop duration after Enter starts the recording. 0 means a second Enter stops it.")
    capture.add_argument("--delta-t-us", type=int, default=10_000, help="Live event slice duration during capture.")
    capture.add_argument("--npz-delta-t-us", type=int, default=1_000, help="RAW read slice duration when exporting NPZ.")

    track = parser.add_argument_group("tracking")
    track.add_argument("--window-us", type=int, default=200, help="Event integration window.")
    track.add_argument("--hop-us", type=int, default=100, help="Step between centroid estimates.")
    track.add_argument("--dt-us", type=float, default=100.0, help="Uniform interpolation dt.")
    track.add_argument("--max-interp-gap-sec", type=float, default=0.005, help="Do not interpolate across longer gaps.")
    track.add_argument("--max-step-px", type=float, default=15.0, help="Reject a raw centroid jump larger than this.")
    track.add_argument("--tracking-method", default="event_weighted", help="Centre estimator.")
    track.add_argument("--threshold-count", type=int, default=1, help="Pixel event-count threshold.")
    track.add_argument("--min-events", type=int, default=20, help="Minimum events in a bin.")
    track.add_argument("--min-area", type=int, default=5, help="Minimum connected-component area.")
    track.add_argument("--min-mass", type=int, default=30, help="Minimum summed event count in the component.")
    track.add_argument("--polarity", choices=["all", "on", "off"], default="all", help="Event polarity.")
    track.add_argument("--roi", default="", help="Tracking ROI x0,y0,x1,y1. Empty uses the full sensor.")
    track.add_argument("--mask-roi", default="", help="Region excluded from tracking, x0,y0,x1,y1.")

    video = parser.add_argument_group("video")
    video.add_argument("--video-fps", type=float, default=2000.0, help="Rendered frames per second of event time.")
    video.add_argument("--video-playback-fps", type=float, default=30.0, help="Playback FPS written into the video header.")
    video.add_argument("--video-accumulation-us", type=int, default=1_000, help="Event accumulation per rendered frame.")
    video.add_argument("--video-duration-sec", type=float, default=1.0, help="Duration to render. 0 renders everything.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        if args.action == "devices":
            cmd = command("mono_eventcam_record.py", "--list-devices")
            print(subprocess.list2cmdline(cmd), flush=True)
            if not args.dry_run:
                subprocess.run(cmd, check=True, cwd=ROOT)
            return 0

        run = run_directory(args.run)
        stages = ("record", "track", "video") if args.action == "all" else (args.action,)

        if args.dry_run:
            placeholder = run / "mono_<timestamp>_events.npz"
            npz = placeholder
            if "record" not in stages and run.exists():
                npz = find_npz(run)
            for stage in stages:
                builder = {"record": lambda: record_command(run, args),
                           "track": lambda: track_command(npz, run, args),
                           "video": lambda: video_command(npz, run, args)}[stage]
                print(f"{stage}: {subprocess.list2cmdline(builder())}", flush=True)
            print("DRY RUN: no files written, subprocesses started, or cameras opened.")
            return 0

        if "record" in stages:
            require_fresh(run)
        if "track" in stages:
            require_fresh(run / TRACK_DIR_NAME)
        if "video" in stages:
            require_fresh(run / VIDEO_NAME)

        for stage in stages:
            print(f"\n[{stage}]", flush=True)
            if stage == "record":
                subprocess.run(record_command(run, args), check=True, cwd=ROOT)
                continue
            npz = find_npz(run)
            if stage == "track":
                subprocess.run(track_command(npz, run, args), check=True, cwd=ROOT)
            else:
                if not (run / TRACK_DIR_NAME / INTERP_CSV).exists():
                    print("[video] No tracking CSV yet; rendering events without an overlay.", flush=True)
                subprocess.run(video_command(npz, run, args), check=True, cwd=ROOT)

        print(f"\nRun directory: {run}")
        return 0
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted; preserved existing files.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
