#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Monitor processed event-camera NPZ files from auto measurements.

Default behavior is health-check only.  Trajectory extraction is NOT run unless
``--track`` is explicitly passed, for example:

    .\venv\Scripts\python.exe .\eventcam_auto_npz_monitor.py --new-only --track

This script watches ``proc_eventcam_auto`` by default and validates newly
created ``*.npz`` files.  It is intentionally dependency-light: polling is
enough here and avoids requiring watchdog.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import math
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import numpy as np


DEFAULT_ROOT = "proc_eventcam_auto"
DEFAULT_LOG_CSV = "eventcam_auto_npz_monitor_log.csv"
DEFAULT_TRACKING_SUBDIR = "event_tracking_npz_auto"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Watch proc_eventcam_auto for NPZ files. By default this only checks health; add --track to run trajectory extraction.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("root", nargs="?", default=DEFAULT_ROOT, help="Directory to scan/watch.")
    parser.add_argument("--interval-sec", type=float, default=2.0, help="Polling interval in watch mode.")
    parser.add_argument("--stable-sec", type=float, default=1.0, help="Require file size to be stable for this long before reading.")
    parser.add_argument("--once", action="store_true", help="Scan once and exit instead of watching continuously.")
    parser.add_argument("--new-only", action="store_true", help="In watch mode, ignore NPZ files already present at startup.")
    parser.add_argument("--min-events", type=int, default=1000, help="Warn if an NPZ has fewer events than this.")
    parser.add_argument("--min-duration-sec", type=float, default=0.05, help="Warn if event timestamp span is shorter than this.")
    parser.add_argument(
        "--expected-duration-ratio",
        type=float,
        default=0.8,
        help="Warn if event span is shorter than this ratio of expected_duration_sec from summary JSON.",
    )
    parser.add_argument("--log-csv", default=DEFAULT_LOG_CSV, help="Append monitor results to this CSV. Empty string disables logging.")
    parser.add_argument("--track", action="store_true", help="Run eventcam_npz_track.py after a readable NPZ is found.")
    parser.add_argument(
        "--tracking-method",
        action="append",
        default=[],
        choices=[
            "event_weighted",
            "component_center",
            "hough_circle",
            "distance_transform_center",
            "min_enclosing_circle",
            "fit_ellipse",
            "ransac_circle",
        ],
        help="Tracking method to run. Can be repeated. Default with --track is event_weighted.",
    )
    parser.add_argument("--track-output-subdir", default=DEFAULT_TRACKING_SUBDIR, help="Subdirectory created next to each NPZ for tracking outputs.")
    parser.add_argument("--track-on-warn", action="store_true", help="Run tracking even when the NPZ health check has WARN status.")
    parser.add_argument("--track-window-us", type=int, default=300, help="Tracking integration window passed to eventcam_npz_track.py.")
    parser.add_argument("--track-hop-us", type=int, default=100, help="Tracking hop passed to eventcam_npz_track.py.")
    parser.add_argument("--track-dt-us", type=int, default=100, help="Tracking interpolation dt passed to eventcam_npz_track.py.")
    parser.add_argument("--track-t-end-sec", type=float, default=0.0, help="Tracking end time. 0 uses all events.")
    parser.add_argument("--track-roi", default="0,0,1280,720", help="Tracking ROI x0,y0,x1,y1.")
    parser.add_argument("--track-min-events", type=int, default=20, help="Tracking min-events.")
    parser.add_argument("--track-threshold-count", type=int, default=1, help="Tracking pixel threshold.")
    parser.add_argument("--track-min-area", type=int, default=5, help="Tracking connected-component min area.")
    parser.add_argument("--track-min-mass", type=int, default=30, help="Tracking connected-component min mass.")
    parser.add_argument("--track-scale-px-per-mm", type=float, default=8.4615, help="Pixel/mm scale passed to tracking.")
    parser.add_argument("--track-center-mode", choices=["none", "first", "mean"], default="first", help="Tracking mm-output center mode.")
    parser.add_argument("--track-no-y-invert", action="store_true", help="Do not pass --y-invert to tracking.")
    return parser.parse_args()


def strip_known_npz_suffix(stem: str) -> str:
    for suffix in (
        "_syncled_masked_events",
        "_syncled_events",
        "_masked_events",
        "_events",
    ):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def sidecar_paths(npz_path: Path) -> Dict[str, Path]:
    base = strip_known_npz_suffix(npz_path.stem)
    return {
        "led_sync": npz_path.with_name(f"{base}_led_sync.json"),
        "timestamps": npz_path.with_name(f"{base}_timestamps.csv"),
    }


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def summary_path_from_led_json(led_json: Dict[str, Any]) -> Path | None:
    raw_path = led_json.get("raw_path")
    if not raw_path:
        return None
    raw = Path(str(raw_path))
    return raw.with_name(f"{raw.stem}_eventcam_summary.json")


def ideal_log_from_led_json(led_json: Dict[str, Any]) -> Path | None:
    summary_path = summary_path_from_led_json(led_json)
    if summary_path is None:
        return None
    summary_json = load_json(summary_path)
    extra_meta = summary_json.get("extra_meta") if isinstance(summary_json.get("extra_meta"), dict) else {}
    ideal_log = extra_meta.get("ideal_log") if extra_meta else None
    if not ideal_log:
        return None
    path = Path(str(ideal_log))
    return path if path.exists() else None


def file_is_stable(path: Path, stable_sec: float) -> bool:
    try:
        size0 = path.stat().st_size
        time.sleep(max(0.0, float(stable_sec)))
        size1 = path.stat().st_size
        return size0 == size1 and size1 > 0
    except OSError:
        return False


def scalar_to_int(value: Any, default: int = -1) -> int:
    try:
        return int(np.asarray(value).item())
    except Exception:
        return int(default)


def summarize_npz(npz_path: Path, min_events: int, min_duration_sec: float, expected_duration_ratio: float) -> Dict[str, Any]:
    warnings: List[str] = []
    errors: List[str] = []
    stats: Dict[str, Any] = {
        "checked_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "path": str(npz_path),
        "status": "OK",
        "events": 0,
        "duration_sec": 0.0,
        "event_rate_mev_s": 0.0,
        "t_min_us": "",
        "t_max_us": "",
        "bbox": "",
        "polarity_pos": "",
        "polarity_neg": "",
        "sync_ts_us": "",
        "led_threshold": "",
        "led_peak_count": "",
        "expected_duration_sec": "",
        "raw_total_events": "",
        "raw_stop_ok": "",
        "trigger_event_count": "",
        "tracking_status": "",
        "tracking_outputs": "",
        "messages": "",
    }

    sidecars = sidecar_paths(npz_path)
    led_json = load_json(sidecars["led_sync"])
    summary_json: Dict[str, Any] = {}
    summary_path = summary_path_from_led_json(led_json)
    if summary_path is not None:
        summary_json = load_json(summary_path)

    if not sidecars["led_sync"].exists():
        warnings.append("missing_led_sync_json")
    if not sidecars["timestamps"].exists():
        warnings.append("missing_timestamps_csv")
    if summary_path is None or not summary_path.exists():
        warnings.append("missing_raw_summary_json")

    try:
        with np.load(npz_path, allow_pickle=False) as payload:
            if "events" not in payload.files:
                errors.append("missing_events_array")
                events = None
            else:
                events = payload["events"]
            stats["sync_ts_us"] = scalar_to_int(payload["sync_ts_us"]) if "sync_ts_us" in payload.files else ""
    except Exception as exc:
        events = None
        errors.append(f"npz_read_failed:{exc}")

    if events is not None:
        try:
            names = set(events.dtype.names or [])
            required = {"x", "y", "p", "t"}
            if not required.issubset(names):
                errors.append(f"events_dtype_missing:{','.join(sorted(required - names))}")
            count = int(events.size)
            stats["events"] = count
            if count < int(min_events):
                warnings.append(f"low_event_count:{count}")
            if count > 0 and required.issubset(names):
                t = events["t"].astype(np.int64, copy=False)
                x = events["x"].astype(np.int64, copy=False)
                y = events["y"].astype(np.int64, copy=False)
                p = events["p"].astype(np.int64, copy=False)
                t_min = int(np.min(t))
                t_max = int(np.max(t))
                duration_sec = max(0.0, (t_max - t_min) * 1e-6)
                stats["t_min_us"] = t_min
                stats["t_max_us"] = t_max
                stats["duration_sec"] = duration_sec
                stats["event_rate_mev_s"] = (count / duration_sec / 1_000_000.0) if duration_sec > 0 else 0.0
                stats["bbox"] = f"x[{int(np.min(x))},{int(np.max(x))}] y[{int(np.min(y))},{int(np.max(y))}]"
                stats["polarity_pos"] = int(np.count_nonzero(p > 0))
                stats["polarity_neg"] = int(np.count_nonzero(p <= 0))
                if duration_sec < float(min_duration_sec):
                    warnings.append(f"short_duration:{duration_sec:.3f}s")
        except Exception as exc:
            errors.append(f"event_stats_failed:{exc}")

    sync_result = led_json.get("sync_result") if isinstance(led_json.get("sync_result"), dict) else {}
    if sync_result:
        stats["led_threshold"] = sync_result.get("threshold", "")
        stats["led_peak_count"] = sync_result.get("peak_count", "")
        led_sync_ts = sync_result.get("sync_ts_us")
        if stats["sync_ts_us"] == "" and led_sync_ts is not None:
            stats["sync_ts_us"] = led_sync_ts
        try:
            if int(sync_result.get("peak_count", 0)) <= 0:
                warnings.append("led_peak_count_zero")
        except Exception:
            warnings.append("led_peak_count_invalid")
    else:
        warnings.append("missing_sync_result")

    extra_meta = summary_json.get("extra_meta") if isinstance(summary_json.get("extra_meta"), dict) else {}
    if summary_json:
        stats["raw_total_events"] = summary_json.get("total_events", "")
        stats["raw_stop_ok"] = summary_json.get("raw_stop_ok", "")
        stats["trigger_event_count"] = summary_json.get("trigger_event_count", "")
        if summary_json.get("raw_stop_ok") is False:
            errors.append("raw_stop_not_ok")
    expected_duration = extra_meta.get("expected_duration_sec") if extra_meta else None
    if expected_duration is not None:
        try:
            expected_duration_f = float(expected_duration)
            stats["expected_duration_sec"] = expected_duration_f
            duration_sec = float(stats["duration_sec"] or 0.0)
            if duration_sec > 0 and duration_sec < expected_duration_f * float(expected_duration_ratio):
                warnings.append(f"duration_lt_expected_ratio:{duration_sec:.3f}/{expected_duration_f:.3f}s")
        except Exception:
            warnings.append("expected_duration_invalid")

    if errors:
        stats["status"] = "ERROR"
    elif warnings:
        stats["status"] = "WARN"
    stats["messages"] = ";".join(errors + warnings)
    return stats


def print_result(stats: Dict[str, Any], root: Path) -> None:
    try:
        rel = Path(str(stats["path"])).resolve().relative_to(root.resolve())
    except Exception:
        rel = Path(str(stats["path"]))
    status = str(stats["status"])
    count = int(stats.get("events") or 0)
    duration = float(stats.get("duration_sec") or 0.0)
    rate = float(stats.get("event_rate_mev_s") or 0.0)
    msg = str(stats.get("messages") or "")
    print(
        f"[{status}] {rel} | events={count:,} | duration={duration:.3f}s | "
        f"rate={rate:.2f} Mev/s | sync={stats.get('sync_ts_us', '')} | "
        f"led_peak={stats.get('led_peak_count', '')}",
        flush=True,
    )
    if msg:
        print(f"      {msg}", flush=True)


def append_csv(log_csv: Path, stats: Dict[str, Any]) -> None:
    fieldnames = [
        "checked_at",
        "status",
        "path",
        "events",
        "duration_sec",
        "event_rate_mev_s",
        "t_min_us",
        "t_max_us",
        "bbox",
        "polarity_pos",
        "polarity_neg",
        "sync_ts_us",
        "led_threshold",
        "led_peak_count",
        "expected_duration_sec",
        "raw_total_events",
        "raw_stop_ok",
        "trigger_event_count",
        "tracking_status",
        "tracking_outputs",
        "messages",
    ]
    log_csv.parent.mkdir(parents=True, exist_ok=True)
    write_header = not log_csv.exists()
    with log_csv.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow({key: stats.get(key, "") for key in fieldnames})


def iter_npz(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    return sorted(root.rglob("*.npz"), key=lambda p: (p.stat().st_mtime, str(p)))


def tracking_methods_from_args(args: argparse.Namespace) -> List[str]:
    methods = [str(method).strip() for method in args.tracking_method if str(method).strip()]
    return methods or ["event_weighted"]


def run_tracking(npz_path: Path, args: argparse.Namespace) -> Tuple[str, str]:
    sidecars = sidecar_paths(npz_path)
    led_json = load_json(sidecars["led_sync"])
    ideal_log = ideal_log_from_led_json(led_json)
    methods = tracking_methods_from_args(args)
    output_paths: List[str] = []
    failures: List[str] = []

    for method in methods:
        out_dir = npz_path.parent / str(args.track_output_subdir)
        if len(methods) > 1:
            out_dir = out_dir / method
        cmd = [
            sys.executable,
            str(Path(__file__).resolve().parent / "eventcam_npz_track.py"),
            str(npz_path),
            "--output-dir",
            str(out_dir),
            "--window-us",
            str(int(args.track_window_us)),
            "--hop-us",
            str(int(args.track_hop_us)),
            "--dt-us",
            str(int(args.track_dt_us)),
            "--t-end-sec",
            f"{float(args.track_t_end_sec):g}",
            "--roi",
            str(args.track_roi),
            "--tracking-method",
            str(method),
            "--min-events",
            str(int(args.track_min_events)),
            "--threshold-count",
            str(int(args.track_threshold_count)),
            "--min-area",
            str(int(args.track_min_area)),
            "--min-mass",
            str(int(args.track_min_mass)),
            "--scale-px-per-mm",
            f"{float(args.track_scale_px_per_mm):g}",
            "--center-mode",
            str(args.track_center_mode),
        ]
        if not bool(args.track_no_y_invert):
            cmd.append("--y-invert")
        if ideal_log is not None:
            cmd.extend(["--ideal-log", str(ideal_log)])

        print(f"[TRACK] {method}: {npz_path.name}", flush=True)
        try:
            subprocess.run(cmd, check=True)
            output_paths.append(str(out_dir))
            print(f"[TRACK] {method}: done -> {out_dir}", flush=True)
        except Exception as exc:
            failures.append(f"{method}:{exc}")
            print(f"[TRACK][WARN] {method}: failed: {exc}", flush=True)

    if failures:
        return "ERROR", ";".join(failures)
    return "OK", ";".join(output_paths)


def process_npz(path: Path, root: Path, args: argparse.Namespace, log_csv: Path | None) -> Dict[str, Any] | None:
    if not file_is_stable(path, args.stable_sec):
        return None
    stats = summarize_npz(
        path,
        min_events=int(args.min_events),
        min_duration_sec=float(args.min_duration_sec),
        expected_duration_ratio=float(args.expected_duration_ratio),
    )
    print_result(stats, root)
    if args.track:
        if stats["status"] == "ERROR":
            stats["tracking_status"] = "SKIP_ERROR"
        elif stats["status"] == "WARN" and not args.track_on_warn:
            stats["tracking_status"] = "SKIP_WARN"
        else:
            tracking_status, tracking_outputs = run_tracking(path, args)
            stats["tracking_status"] = tracking_status
            stats["tracking_outputs"] = tracking_outputs
    if log_csv is not None:
        append_csv(log_csv, stats)
    return stats


def main() -> None:
    args = parse_args()
    root = Path(args.root)
    log_csv = Path(args.log_csv) if str(args.log_csv).strip() else None

    print(f"[MONITOR] root={root.resolve()}", flush=True)
    if log_csv is not None:
        print(f"[MONITOR] log_csv={log_csv.resolve()}", flush=True)

    seen: set[Path] = set()
    if args.new_only and not args.once:
        seen = {p.resolve() for p in iter_npz(root)}
        print(f"[MONITOR] new-only mode: ignoring {len(seen)} existing NPZ file(s).", flush=True)

    while True:
        found_any = False
        for path in iter_npz(root):
            resolved = path.resolve()
            if resolved in seen:
                continue
            found_any = True
            result = process_npz(path, root, args, log_csv)
            if result is not None:
                seen.add(resolved)
        if args.once:
            if not found_any:
                print("[MONITOR] No new NPZ files found.", flush=True)
            return
        time.sleep(max(0.2, float(args.interval_sec)))


if __name__ == "__main__":
    main()
