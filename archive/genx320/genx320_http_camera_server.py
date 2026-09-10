#!/usr/bin/env python3
"""HTTP camera server for a Pi-hosted Prophesee GenX320.

Run this on the Raspberry Pi. Windows can then control capture without
starting ssh/scp for every recording.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def configure_runtime_environment() -> None:
    os.environ.setdefault("PSEE_VAR_V4L2_BSIZE", "1")
    os.environ.setdefault("V4L2_HEAP", "vidbuf_cached")


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True)


def current_overlays_text() -> str:
    completed = run_command(["dtoverlay", "-l"])
    return (completed.stdout or "") + (completed.stderr or "")


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
    configure_runtime_environment()
    steps: list[dict[str, Any]] = []
    overlays_before = current_overlays_text()
    steps.append({"command": "dtoverlay -l", "returncode": 0, "stdout": overlays_before, "stderr": ""})
    if "genx320" not in overlays_before:
        completed = run_command(["sudo", "dtoverlay", "genx320"])
        steps.append(
            {
                "command": "sudo dtoverlay genx320",
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
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

    script = find_v4l_setup_script()
    if script is not None:
        completed = subprocess.run(
            [str(script)],
            text=True,
            capture_output=True,
            cwd=str(script.parent),
            env=os.environ.copy(),
        )
        steps.append(
            {
                "command": str(script),
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        )
    else:
        steps.append({"command": "rp5_setup_v4l.sh", "returncode": None, "stdout": "", "stderr": "not found"})
    return {"steps": steps}


def get_status() -> dict[str, Any]:
    commands = {
        "uname": ["uname", "-r"],
        "overlays": ["dtoverlay", "-l"],
        "video_devices": ["bash", "-lc", "ls -1 /dev/video* 2>/dev/null || true"],
        "v4l_devices": ["bash", "-lc", "v4l2-ctl --list-devices 2>/dev/null || true"],
        "genx_modules": ["bash", "-lc", "lsmod | grep -i -E 'genx|psee' || true"],
    }
    payload: dict[str, Any] = {}
    for name, command in commands.items():
        completed = run_command(command)
        payload[name] = {
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }
    return payload


def stop_metavision_viewer() -> dict[str, Any]:
    completed = run_command(["pkill", "-x", "metavision_viewer"])
    if completed.returncode not in (0, 1):
        return {
            "command": "pkill -x metavision_viewer",
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    return {
        "command": "pkill -x metavision_viewer",
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "note": "returncode 1 means no viewer process was running",
    }


def import_metavision() -> tuple[Any, Any]:
    configure_runtime_environment()
    from metavision_core.event_io import EventsIterator
    from metavision_core.event_io.raw_reader import initiate_device

    return EventsIterator, initiate_device


class CaptureSession:
    def __init__(self, output_dir: Path, basename: str, serial: str, delta_t_us: int, setup_before_open: bool) -> None:
        self.output_dir = output_dir.expanduser()
        self.basename = basename
        self.serial = serial
        self.delta_t_us = int(delta_t_us)
        self.setup_before_open = bool(setup_before_open)
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.done_event = threading.Event()
        self.error: str | None = None
        self.thread: threading.Thread | None = None
        self.summary: dict[str, Any] = {}

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="GenX320HttpCapture", daemon=True)
        self.thread.start()

    def stop(self, timeout_sec: float = 20.0) -> dict[str, Any]:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=timeout_sec)
        if self.thread is not None and self.thread.is_alive():
            raise TimeoutError("Timed out waiting for capture thread to stop.")
        if self.error:
            raise RuntimeError(self.error)
        return self.summary

    def _run(self) -> None:
        device = None
        iterator = None
        events_stream = None
        try:
            if self.setup_before_open:
                setup_v4l()
            EventsIterator, initiate_device = import_metavision()
            self.output_dir.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            stem = self.output_dir / f"{self.basename}_{stamp}"
            raw_path = stem.with_suffix(".raw")
            summary_path = stem.with_suffix(".json")

            device = initiate_device(path=self.serial)
            events_stream = device.get_i_events_stream()
            if events_stream is None:
                raise RuntimeError("This device does not expose I_EventsStream.")
            iterator = EventsIterator.from_device(
                device=device,
                mode="delta_t",
                delta_t=self.delta_t_us,
                max_duration=None,
                relative_timestamps=False,
            )
            height, width = iterator.get_size()
            self.summary = {
                "raw_path": str(raw_path.resolve()),
                "summary_path": str(summary_path.resolve()),
                "started_at": dt.datetime.now().isoformat(timespec="seconds"),
                "stopped_at": None,
                "delta_t_us": self.delta_t_us,
                "serial": self.serial,
                "sensor_size": {"width": int(width), "height": int(height)},
                "total_slices": 0,
                "total_events": 0,
                "first_event_ts_us": None,
                "last_event_ts_us": None,
                "stop_reason": None,
            }
            events_stream.log_raw_data(str(raw_path.resolve()))
            self.ready_event.set()

            for events in iterator:
                self.summary["total_slices"] += 1
                count = int(events.size)
                self.summary["total_events"] += count
                if count:
                    if self.summary["first_event_ts_us"] is None:
                        self.summary["first_event_ts_us"] = int(events["t"][0])
                    self.summary["last_event_ts_us"] = int(events["t"][-1])
                if self.stop_event.is_set():
                    self.summary["stop_reason"] = "http_stop"
                    break
        except Exception as exc:
            self.error = str(exc)
            self.ready_event.set()
        finally:
            if events_stream is not None:
                try:
                    events_stream.stop_log_raw_data()
                except Exception as exc:
                    self.summary["stop_log_raw_data_error"] = str(exc)
            if self.summary:
                self.summary["stopped_at"] = dt.datetime.now().isoformat(timespec="seconds")
                summary_path_text = self.summary.get("summary_path")
                if summary_path_text:
                    Path(summary_path_text).write_text(json.dumps(self.summary, indent=2), encoding="utf-8")
            del iterator
            del device
            self.done_event.set()


class CameraState:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.lock = threading.Lock()
        self.session: CaptureSession | None = None
        self.last_summary: dict[str, Any] | None = None


def parse_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length <= 0:
        return {}
    data = handler.rfile.read(length)
    return json.loads(data.decode("utf-8"))


def write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    server_version = "GenX320HTTP/1.0"

    @property
    def state(self) -> CameraState:
        return self.server.state  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/status":
            with self.state.lock:
                active = self.state.session is not None and not self.state.session.done_event.is_set()
                last_summary = self.state.last_summary
            write_json(self, 200, {"active": active, "last_summary": last_summary, "pi_status": get_status()})
            return
        if parsed.path == "/last-summary":
            write_json(self, 200, {"last_summary": self.state.last_summary})
            return
        if parsed.path == "/download":
            query = urllib.parse.parse_qs(parsed.query)
            requested = query.get("path", [""])[0]
            path = Path(requested)
            if not path.exists() or not path.is_file():
                write_json(self, 404, {"error": f"file not found: {requested}"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.end_headers()
            with path.open("rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return
        write_json(self, 404, {"error": f"unknown endpoint: {parsed.path}"})

    def do_POST(self) -> None:
        if self.path == "/setup":
            write_json(self, 200, setup_v4l())
            return
        if self.path == "/start":
            body = parse_json_body(self)
            basename = str(body.get("basename") or "genx320_http")
            serial = str(body.get("serial") or "")
            delta_t_us = int(body.get("delta_t_us") or 10_000)
            output_dir = Path(str(body.get("output_dir") or self.state.output_dir))
            setup_before_open = bool(body.get("setup_v4l", True))
            kill_existing_viewer = bool(body.get("kill_existing_viewer", False))
            ready_timeout = float(body.get("ready_timeout_sec") or 30.0)
            with self.state.lock:
                if self.state.session is not None and not self.state.session.done_event.is_set():
                    write_json(self, 409, {"error": "capture already active"})
                    return
                session = CaptureSession(output_dir, basename, serial, delta_t_us, setup_before_open)
                self.state.session = session
            viewer_stop_result = None
            if kill_existing_viewer:
                viewer_stop_result = stop_metavision_viewer()
                time.sleep(0.5)
            session.start()
            if not session.ready_event.wait(timeout=ready_timeout):
                write_json(self, 504, {"error": "timed out waiting for camera ready"})
                return
            if session.error:
                with self.state.lock:
                    self.state.session = None
                write_json(self, 500, {"error": session.error})
                return
            write_json(self, 200, {"ready": True, "summary": session.summary, "viewer_stop_result": viewer_stop_result})
            return
        if self.path == "/stop":
            with self.state.lock:
                session = self.state.session
            if session is None:
                write_json(self, 409, {"error": "no active capture"})
                return
            try:
                summary = session.stop()
            except Exception as exc:
                with self.state.lock:
                    self.state.session = None
                write_json(self, 500, {"error": str(exc)})
                return
            with self.state.lock:
                self.state.last_summary = summary
                self.state.session = None
            write_json(self, 200, {"stopped": True, "summary": summary})
            return
        write_json(self, 404, {"error": f"unknown endpoint: {self.path}"})

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--output-dir", default="~/genx320_http_captures")
    parser.add_argument("--setup-on-start", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.setup_on_start:
        print(json.dumps(setup_v4l(), indent=2))
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.state = CameraState(Path(args.output_dir).expanduser())  # type: ignore[attr-defined]
    print(f"GenX320 HTTP camera server listening on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
