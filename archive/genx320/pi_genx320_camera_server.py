#!/usr/bin/env python3
"""Headless GenX320 capture helper for Raspberry Pi 5.

This script is intended to run on the Raspberry Pi that has the
Prophesee GenX320 Raspberry Pi 5 Starter Kit attached. Windows can call it
through SSH, then copy the resulting RAW/JSON files back with scp.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


def run_command(command: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, text=True, capture_output=True)


def current_overlays_text() -> str:
    completed = run_command(["dtoverlay", "-l"])
    return (completed.stdout or "") + (completed.stderr or "")


def configure_runtime_environment() -> None:
    """Apply the V4L2 runtime variables recommended by Prophesee."""
    os.environ.setdefault("PSEE_VAR_V4L2_BSIZE", "1")
    os.environ.setdefault("V4L2_HEAP", "vidbuf_cached")


def find_v4l_setup_script() -> Path | None:
    home = Path.home()
    candidates = [
        Path("/home/eventcamera/rp5_setup_v4l.sh"),
        home / "rp5_setup_v4l.sh",
        home / "rpi-sensor-drivers" / "rp5_setup_v4l.sh",
        Path.cwd() / "rp5_setup_v4l.sh",
        Path.cwd() / "rpi-sensor-drivers" / "rp5_setup_v4l.sh",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def setup_v4l() -> dict[str, Any]:
    """Load the GenX320 overlay and run Prophesee's V4L setup script if present."""
    steps: list[dict[str, Any]] = []

    overlays_before = current_overlays_text()
    steps.append({"command": "dtoverlay -l", "returncode": 0, "stdout": overlays_before, "stderr": ""})
    if "genx320" not in overlays_before:
        overlay = run_command(["sudo", "dtoverlay", "genx320"])
        steps.append(
            {
                "command": "sudo dtoverlay genx320",
                "returncode": overlay.returncode,
                "stdout": overlay.stdout,
                "stderr": overlay.stderr,
            }
        )
    else:
        steps.append(
            {
                "command": "sudo dtoverlay genx320",
                "returncode": 0,
                "stdout": "",
                "stderr": "skipped: genx320 overlay already loaded",
            }
        )

    setup_script = find_v4l_setup_script()
    if setup_script is not None:
        setup = subprocess.run(
            [str(setup_script)],
            text=True,
            capture_output=True,
            cwd=str(setup_script.parent),
            env=os.environ.copy(),
        )
        steps.append(
            {
                "command": str(setup_script),
                "returncode": setup.returncode,
                "stdout": setup.stdout,
                "stderr": setup.stderr,
            }
        )
    else:
        steps.append(
            {
                "command": "rp5_setup_v4l.sh",
                "returncode": None,
                "stdout": "",
                "stderr": "setup script not found",
            }
        )

    return {"steps": steps}


def get_status() -> dict[str, Any]:
    commands = {
        "uname": ["uname", "-r"],
        "overlays": ["dtoverlay", "-l"],
        "video_devices": ["bash", "-lc", "ls -1 /dev/video* 2>/dev/null || true"],
        "v4l_devices": ["bash", "-lc", "v4l2-ctl --list-devices 2>/dev/null || true"],
        "dkms": ["bash", "-lc", "dkms status 2>/dev/null || true"],
        "genx_modules": ["bash", "-lc", "lsmod | grep -i -E 'genx|psee' || true"],
    }
    status: dict[str, Any] = {}
    for name, command in commands.items():
        completed = run_command(command)
        status[name] = {
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }
    return status


def import_metavision() -> tuple[Any, Any]:
    configure_runtime_environment()
    try:
        import metavision_hal
        from metavision_core.event_io import EventsIterator
        from metavision_core.event_io.raw_reader import initiate_device
    except Exception as exc:
        raise SystemExit(f"Metavision Python bindings are not available on the Pi: {exc}") from exc

    return metavision_hal, EventsIterator, initiate_device


def list_devices() -> dict[str, Any]:
    metavision_hal, _, _ = import_metavision()
    return {"devices": [str(device) for device in metavision_hal.DeviceDiscovery.list()]}


def make_output_paths(output_dir: Path, basename: str) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = output_dir / f"{basename}_{stamp}"
    return stem.with_suffix(".raw"), stem.with_suffix(".json")


def capture(args: argparse.Namespace) -> dict[str, Any]:
    configure_runtime_environment()
    if args.setup_v4l:
        setup_v4l()

    _, EventsIterator, initiate_device = import_metavision()

    raw_path, summary_path = make_output_paths(Path(args.output_dir).expanduser(), args.basename)
    device = None
    iterator = None
    events_stream = None
    summary: dict[str, Any] = {
        "raw_path": str(raw_path.resolve()),
        "summary_path": str(summary_path.resolve()),
        "started_at": dt.datetime.now().isoformat(timespec="seconds"),
        "stopped_at": None,
        "duration_sec_requested": float(args.seconds),
        "delta_t_us": int(args.delta_t_us),
        "serial": args.serial,
        "sensor_size": None,
        "total_slices": 0,
        "total_events": 0,
        "first_event_ts_us": None,
        "last_event_ts_us": None,
        "status_before_capture": get_status() if args.include_status else None,
    }
    stdin_stop_event = threading.Event()

    if args.stop_stdin:
        def _stdin_stop_loop() -> None:
            try:
                for line in sys.stdin:
                    if line.strip().upper() == "STOP":
                        stdin_stop_event.set()
                        return
            except Exception:
                return

        threading.Thread(target=_stdin_stop_loop, name="GenX320StdinStop", daemon=True).start()

    try:
        device = initiate_device(path=args.serial)
        events_stream = device.get_i_events_stream()
        if events_stream is None:
            raise RuntimeError("This device does not expose I_EventsStream.")

        iterator = EventsIterator.from_device(
            device=device,
            mode="delta_t",
            delta_t=args.delta_t_us,
            max_duration=None,
            relative_timestamps=False,
        )
        height, width = iterator.get_size()
        summary["sensor_size"] = {"width": int(width), "height": int(height)}

        events_stream.log_raw_data(str(raw_path.resolve()))
        ready_file = Path(args.ready_file).expanduser() if args.ready_file else None
        if ready_file is not None:
            ready_file.parent.mkdir(parents=True, exist_ok=True)
            ready_payload = {
                "raw_path": str(raw_path.resolve()),
                "summary_path": str(summary_path.resolve()),
                "started_at": summary["started_at"],
                "sensor_size": summary["sensor_size"],
            }
            ready_file.write_text(json.dumps(ready_payload), encoding="utf-8")
        if args.ready_stdout:
            ready_payload = {
                "raw_path": str(raw_path.resolve()),
                "summary_path": str(summary_path.resolve()),
                "started_at": summary["started_at"],
                "sensor_size": summary["sensor_size"],
            }
            print("__GENX320_READY__" + json.dumps(ready_payload), flush=True)
        deadline = time.monotonic() + float(args.seconds)
        stop_file = Path(args.stop_file).expanduser() if args.stop_file else None
        if stop_file is not None:
            try:
                stop_file.unlink()
            except FileNotFoundError:
                pass
        for events in iterator:
            summary["total_slices"] += 1
            count = int(events.size)
            summary["total_events"] += count
            if count:
                if summary["first_event_ts_us"] is None:
                    summary["first_event_ts_us"] = int(events["t"][0])
                summary["last_event_ts_us"] = int(events["t"][-1])
            if stop_file is not None and stop_file.exists():
                summary["stop_reason"] = "stop_file"
                break
            if stdin_stop_event.is_set():
                summary["stop_reason"] = "stdin"
                break
            if time.monotonic() >= deadline:
                summary["stop_reason"] = "duration"
                break
    finally:
        if events_stream is not None:
            try:
                events_stream.stop_log_raw_data()
            except Exception as exc:
                summary["stop_log_raw_data_error"] = str(exc)
        summary["stopped_at"] = dt.datetime.now().isoformat(timespec="seconds")
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        del iterator
        del device

    return summary


def launch_viewer(args: argparse.Namespace) -> None:
    configure_runtime_environment()
    if args.setup_v4l:
        setup_result = setup_v4l()
        print(json.dumps(setup_result, indent=2), file=sys.stderr)
    os.execvp("metavision_viewer", ["metavision_viewer"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Print Pi camera/driver status as JSON.")
    subparsers.add_parser("list-devices", help="List Metavision devices as JSON.")

    capture_parser = subparsers.add_parser("capture", help="Record a RAW file for a fixed duration.")
    capture_parser.add_argument("--seconds", type=float, default=5.0)
    capture_parser.add_argument("--delta-t-us", type=int, default=10_000)
    capture_parser.add_argument("--serial", default="")
    capture_parser.add_argument("--output-dir", default="~/genx320_captures")
    capture_parser.add_argument("--basename", default="genx320")
    capture_parser.add_argument("--setup-v4l", action="store_true")
    capture_parser.add_argument("--include-status", action="store_true")
    capture_parser.add_argument("--stop-file", default="", help="Stop when this file appears. Useful for SSH start/stop control.")
    capture_parser.add_argument("--ready-file", default="", help="Write JSON here after RAW logging has started.")
    capture_parser.add_argument("--ready-stdout", action="store_true", help="Print a READY marker to stdout after RAW logging starts.")
    capture_parser.add_argument("--stop-stdin", action="store_true", help="Stop when the word STOP is received on stdin.")

    viewer_parser = subparsers.add_parser("viewer", help="Launch metavision_viewer on the Pi desktop.")
    viewer_parser.add_argument("--setup-v4l", action="store_true")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "status":
        payload = get_status()
    elif args.command == "list-devices":
        payload = list_devices()
    elif args.command == "capture":
        payload = capture(args)
    elif args.command == "viewer":
        launch_viewer(args)
        return
    else:
        raise SystemExit(f"Unknown command: {args.command}")

    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
