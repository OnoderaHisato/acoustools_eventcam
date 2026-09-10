#!/usr/bin/env python3
"""Watch completed automated stereo recordings and run deferred 3D postprocessing."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Any

from stereo_acoustools_3d_auto_common import DEFAULT_AUTO_OUTPUT_DIR
from stereo_acoustools_3d_common import PROCESSING_CONFIG_FIELDS
from stereo_acoustools_3d_postprocess_core import run_postprocess


LOCK_NAME = ".stereo_auto_postprocess.lock"
DEFAULT_LOG_NAME = "stereo_acoustools_3d_auto_monitor_log.csv"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Watch completed stereo 3D recording manifests and run 2D tracking, "
            "stereo triangulation, and ideal comparison."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("root", nargs="?", type=Path, default=DEFAULT_AUTO_OUTPUT_DIR)
    parser.add_argument("--interval-sec", type=float, default=2.0)
    parser.add_argument("--stable-sec", type=float, default=1.0)
    parser.add_argument("--once", action="store_true", help="Scan once and exit.")
    parser.add_argument(
        "--new-only",
        action="store_true",
        help="Ignore run manifests already present when watch mode starts.",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Also retry runs whose processing status is failed or interrupted.",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Exit after the first postprocessing error instead of monitoring subsequent runs.",
    )
    parser.add_argument(
        "--log-csv",
        type=Path,
        default=None,
        help="Monitor CSV path; default is ROOT/stereo_acoustools_3d_auto_monitor_log.csv.",
    )
    parser.add_argument(
        "--stale-lock-sec",
        type=float,
        default=0.0,
        help="Replace a monitor lock older than this; 0 never replaces locks.",
    )

    suppress = argparse.SUPPRESS
    parser.add_argument("--window-us", type=int, default=suppress)
    parser.add_argument("--hop-us", type=int, default=suppress)
    parser.add_argument("--dt-us", type=float, default=suppress)
    parser.add_argument("--max-interp-gap-sec", type=float, default=suppress)
    parser.add_argument("--max-step-px", type=float, default=suppress)
    parser.add_argument("--roi", default=suppress)
    parser.add_argument("--tracking-method", default=suppress)
    parser.add_argument("--threshold-count", type=int, default=suppress)
    parser.add_argument("--min-events", type=int, default=suppress)
    parser.add_argument("--min-area", type=int, default=suppress)
    parser.add_argument("--min-mass", type=int, default=suppress)
    parser.add_argument("--polarity", choices=["all", "on", "off"], default=suppress)
    parser.add_argument("--max-time-gap-sec", type=float, default=suppress)
    parser.add_argument("--max-reprojection-error-px", type=float, default=suppress)
    parser.add_argument("--render-overlay", action="store_true", default=suppress)
    parser.add_argument("--auto-time-search-sec", type=float, default=suppress)
    parser.add_argument("--auto-time-step-sec", type=float, default=suppress)
    parser.add_argument("--refine-led-time", action="store_true", default=suppress)
    parser.add_argument("--camera-to-pat-transform", type=Path, default=suppress)
    return parser.parse_args(argv)


def processing_overrides(args: argparse.Namespace) -> dict[str, Any]:
    names = set(PROCESSING_CONFIG_FIELDS) | {"camera_to_pat_transform"}
    return {name: getattr(args, name) for name in names if hasattr(args, name)}


def discover_manifests(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.rglob("pipeline_manifest.json"), key=lambda path: (path.stat().st_mtime, str(path)))


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def candidate_status(manifest: dict[str, Any], retry_failed: bool) -> str:
    if manifest.get("capture_complete") is not True:
        return "not_ready"
    status = str(manifest.get("processing_status", "unknown"))
    if status == "pending":
        return "eligible"
    if retry_failed and status in {"failed", "interrupted"}:
        return "eligible"
    return status


def required_inputs(run_dir: Path) -> list[Path]:
    return [
        run_dir / "pipeline_manifest.json",
        run_dir / "capture_start_marker.json",
        run_dir / "pat_camera_timing.json",
        run_dir / "stereo_recording" / "stereo_recording_manifest.json",
        run_dir / "stereo_recording" / "left" / "left_events.npz",
        run_dir / "stereo_recording" / "right" / "right_events.npz",
    ]


def input_signature(run_dir: Path) -> tuple[tuple[str, int], ...] | None:
    signature: list[tuple[str, int]] = []
    for path in required_inputs(run_dir):
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size <= 0:
            return None
        signature.append((str(path), int(size)))
    return tuple(signature)


def inputs_are_stable(run_dir: Path, stable_sec: float) -> bool:
    first = input_signature(run_dir)
    if first is None:
        return False
    if stable_sec > 0:
        time.sleep(float(stable_sec))
    return first == input_signature(run_dir)


def acquire_lock(run_dir: Path, stale_lock_sec: float) -> Path | None:
    lock_path = run_dir / LOCK_NAME
    if lock_path.exists() and stale_lock_sec > 0:
        try:
            age_sec = time.time() - lock_path.stat().st_mtime
            if age_sec > stale_lock_sec:
                lock_path.unlink()
                print(f"[MONITOR] Removed stale lock ({age_sec:.0f}s): {lock_path}")
        except OSError:
            return None
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "pid": os.getpid(),
                "created_at": dt.datetime.now().isoformat(timespec="milliseconds"),
            },
            handle,
            indent=2,
        )
    return lock_path


def append_log(log_path: Path, row: dict[str, Any]) -> None:
    fieldnames = [
        "checked_at", "result", "run_dir", "label", "processing_before",
        "processing_after", "left_npz_bytes", "right_npz_bytes", "error",
    ]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not log_path.exists()
    with log_path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow({name: row.get(name, "") for name in fieldnames})


def process_manifest(
    manifest_path: Path,
    args: argparse.Namespace,
    log_path: Path,
) -> tuple[str, bool]:
    run_dir = manifest_path.parent.resolve()
    manifest = load_manifest(manifest_path)
    before = str(manifest.get("processing_status", "unknown"))
    if candidate_status(manifest, bool(args.retry_failed)) != "eligible":
        return "skip", False
    if not inputs_are_stable(run_dir, max(0.0, float(args.stable_sec))):
        return "wait", False
    lock_path = acquire_lock(run_dir, max(0.0, float(args.stale_lock_sec)))
    if lock_path is None:
        return "locked", False

    # Another monitor may have completed the run immediately before this lock was acquired.
    current = load_manifest(manifest_path)
    if candidate_status(current, bool(args.retry_failed)) != "eligible":
        try:
            lock_path.unlink()
        except OSError:
            pass
        return "skip", False

    automation = current.get("automation", {})
    label = str(automation.get("label", current.get("shape_name", ""))) if isinstance(automation, dict) else ""
    left_npz = run_dir / "stereo_recording" / "left" / "left_events.npz"
    right_npz = run_dir / "stereo_recording" / "right" / "right_events.npz"
    row = {
        "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "label": label,
        "processing_before": before,
        "left_npz_bytes": left_npz.stat().st_size,
        "right_npz_bytes": right_npz.stat().st_size,
    }
    print(f"[MONITOR] Processing {label or run_dir.name}: {run_dir}", flush=True)
    failed = False
    try:
        run_postprocess(run_dir, processing_overrides(args))
        row["result"] = "OK"
        row["error"] = ""
    except KeyboardInterrupt:
        row["result"] = "INTERRUPTED"
        row["error"] = "KeyboardInterrupt"
        failed = True
    except Exception as exc:
        row["result"] = "ERROR"
        row["error"] = repr(exc)
        failed = True
        print(f"[MONITOR][ERROR] {run_dir}: {exc}", flush=True)
    finally:
        after_manifest = load_manifest(manifest_path)
        row["processing_after"] = after_manifest.get("processing_status", "unknown")
        append_log(log_path, row)
        try:
            lock_path.unlink()
        except OSError:
            pass
    print(
        f"[MONITOR] {row['result']}: {label or run_dir.name} "
        f"({before} -> {row['processing_after']})",
        flush=True,
    )
    return "processed", failed


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if min(float(args.interval_sec), float(args.stable_sec), float(args.stale_lock_sec)) < 0:
        print("[MONITOR][ERROR] Time values must be non-negative.")
        return 2
    root = Path(args.root).resolve()
    log_path = Path(args.log_csv).resolve() if args.log_csv is not None else root / DEFAULT_LOG_NAME
    print(f"[MONITOR] root={root}", flush=True)
    print(f"[MONITOR] log={log_path}", flush=True)

    ignored_at_start = set(discover_manifests(root)) if args.new_only and not args.once else set()
    handled: set[Path] = set(ignored_at_start)
    waiting_reported: set[Path] = set()
    had_error = False
    try:
        while True:
            manifests = discover_manifests(root)
            for manifest_path in manifests:
                if manifest_path in handled:
                    continue
                result, failed = process_manifest(manifest_path, args, log_path)
                if result in {"processed", "skip"}:
                    handled.add(manifest_path)
                    waiting_reported.discard(manifest_path)
                elif result in {"wait", "locked"} and manifest_path not in waiting_reported:
                    print(f"[MONITOR] Waiting for complete/stable inputs: {manifest_path.parent}")
                    waiting_reported.add(manifest_path)
                if failed:
                    had_error = True
                    if args.stop_on_error:
                        return 1
            if args.once:
                return 1 if had_error else 0
            time.sleep(max(0.05, float(args.interval_sec)))
    except KeyboardInterrupt:
        print("\n[MONITOR] Stopped by user.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
