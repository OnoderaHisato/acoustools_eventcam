"""Remote GenX320 recorder with an EventCameraRecorder-like interface.

This is a bridge toward ``acoustools_eventcam_sync.py``: Windows controls the
experiment, while a Raspberry Pi records GenX320 RAW through SSH.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass
class GenX320RemoteRecordingSummary:
    raw_path: str
    summary_json: str
    rec_start_pc_ns: int
    rec_stop_pc_ns: int
    rec_start_wall_ns: int
    rec_stop_wall_ns: int
    sensor_width: int
    sensor_height: int
    total_slices: int
    total_events: int
    first_event_ts_us: Optional[int]
    last_event_ts_us: Optional[int]
    remote_raw_path: str
    remote_summary_json: str
    postprocess_command: Optional[list[str]] = None


class GenX320RemoteEventCameraRecorder:
    """Start/stop recorder for a Pi-hosted GenX320 camera.

    The public methods intentionally resemble ``EventCameraRecorder`` enough
    that an AcousTools variant can call ``start_recording()``, ``log_event()``,
    ``stop_recording()``, and ``close()`` at the same points as before.
    """

    def __init__(
        self,
        *,
        pi: str,
        save_dir: str,
        remote_dir: str = "~/genx320_captures",
        helper_local_path: str = "pi_genx320_camera_server.py",
        helper_remote_path: str = "~/pi_genx320_camera_server.py",
        setup_v4l: bool = True,
        deploy_helper: bool = True,
        remote_timeout_sec: float = 3600.0,
        postprocess_output_dir: str = "./proc_eventcam_genx320",
        postprocess_fps: float = 10000.0,
        postprocess_video_fps: float = 120.0,
        postprocess_accumulation_us: int = 100,
        export_filtered_events_npz: bool = True,
        sync_led_roi: str = "",
        mask_led_roi: bool = False,
        kill_existing_viewer: bool = False,
        python_exe: str = sys.executable,
    ) -> None:
        self.pi = pi
        self.save_dir = Path(save_dir).resolve()
        self.remote_dir = remote_dir
        self.helper_local_path = Path(helper_local_path).resolve()
        self.helper_remote_path = helper_remote_path
        self.setup_v4l = bool(setup_v4l)
        self.remote_timeout_sec = float(remote_timeout_sec)
        self.postprocess_output_dir = str(postprocess_output_dir)
        self.postprocess_fps = float(postprocess_fps)
        self.postprocess_video_fps = float(postprocess_video_fps)
        self.postprocess_accumulation_us = int(postprocess_accumulation_us)
        self.export_filtered_events_npz = bool(export_filtered_events_npz)
        self.sync_led_roi = str(sync_led_roi or "")
        self.mask_led_roi = bool(mask_led_roi)
        self.kill_existing_viewer = bool(kill_existing_viewer)
        self.python_exe = python_exe

        self.save_dir.mkdir(parents=True, exist_ok=True)
        self.proc: subprocess.Popen[str] | None = None
        self.stop_file = ""
        self.ready_file = ""
        self._stdout_lines: list[str] = []
        self.rec_start_pc_ns = 0
        self.rec_start_wall_ns = 0
        self.events_log: list[dict[str, Any]] = []
        self.extra_meta: dict[str, Any] = {}
        self.last_started: dict[str, str] = {}

        if deploy_helper:
            self.deploy_helper()

    def stop_remote_viewer(self) -> None:
        subprocess.run(["ssh", self.pi, "pkill -f metavision_viewer || true"], check=False)
        time.sleep(1.0)

    def deploy_helper(self) -> None:
        if not self.helper_local_path.exists():
            raise FileNotFoundError(self.helper_local_path)
        subprocess.run(["scp", str(self.helper_local_path), f"{self.pi}:{self.helper_remote_path}"], check=True)

    def start_recording(self, safe_base: str, extra_meta: Optional[dict[str, Any]] = None) -> dict[str, str]:
        if self.proc is not None and self.proc.poll() is None:
            raise RuntimeError("GenX320 remote recording is already active.")

        if self.kill_existing_viewer:
            self.stop_remote_viewer()

        self.extra_meta = dict(extra_meta or {})
        self.events_log = []
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.stop_file = f"/tmp/genx320_stop_{safe_base}_{stamp}"
        self.ready_file = f"/tmp/genx320_ready_{safe_base}_{stamp}.json"
        setup_arg = " --setup-v4l" if self.setup_v4l else ""
        command = (
            f"rm -f '{self.stop_file}' '{self.ready_file}'; "
            f"python3 {self.helper_remote_path} capture "
            f"--seconds {self.remote_timeout_sec:g} "
            f"--basename '{safe_base}' "
            f"--output-dir '{self.remote_dir}' "
            f"--stop-file '{self.stop_file}' "
            f"--ready-file '{self.ready_file}' "
            f"--ready-stdout "
            f"--stop-stdin "
            f"--include-status"
            f"{setup_arg}"
        )

        self.proc = subprocess.Popen(
            ["ssh", self.pi, command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        ready_payload = self._wait_for_ready_stdout()
        self.rec_start_pc_ns = int(time.perf_counter_ns())
        self.rec_start_wall_ns = int(time.time_ns())
        self.last_started = {
            "summary_json": str(ready_payload.get("summary_path") or ""),
            "raw_path": str(ready_payload.get("raw_path") or ""),
            "remote_stop_file": self.stop_file,
            "remote_ready_file": self.ready_file,
        }
        return dict(self.last_started)

    def _wait_for_ready_stdout(self, timeout_sec: float = 30.0) -> dict[str, Any]:
        if self.proc is None or self.proc.stdout is None:
            raise RuntimeError("Remote process is not running.")
        deadline = time.monotonic() + float(timeout_sec)
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                stdout, stderr = self.proc.communicate()
                raise RuntimeError(
                    "Remote GenX320 capture exited before becoming ready.\n"
                    f"STDERR:\n{stderr}\nSTDOUT:\n{stdout}"
                )
            line = self.proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            self._stdout_lines.append(line)
            if line.startswith("__GENX320_READY__"):
                return json.loads(line[len("__GENX320_READY__"):])
        raise TimeoutError(
            "Timed out waiting for remote GenX320 READY marker. "
            "If SSH password prompts are still appearing, configure SSH key auth first."
        )

    def log_event(self, name: str, pc_ns: Optional[int] = None, wall_ns: Optional[int] = None, note: str = "") -> None:
        self.events_log.append(
            {
                "event": str(name),
                "pc_ns": int(pc_ns if pc_ns is not None else time.perf_counter_ns()),
                "wall_ns": int(wall_ns if wall_ns is not None else time.time_ns()),
                "note": str(note or ""),
            }
        )

    def stop_recording(self) -> GenX320RemoteRecordingSummary | None:
        if self.proc is None:
            return None

        rec_stop_pc_ns = int(time.perf_counter_ns())
        rec_stop_wall_ns = int(time.time_ns())
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.write("STOP\n")
                self.proc.stdin.flush()
            except Exception:
                pass

        stdout, stderr = self.proc.communicate(timeout=max(10.0, self.remote_timeout_sec + 30.0))
        stdout = "".join(self._stdout_lines) + (stdout or "")
        self._stdout_lines = []
        proc = self.proc
        self.proc = None
        if proc.returncode != 0:
            raise RuntimeError(f"Remote GenX320 capture failed with code {proc.returncode}.\nSTDERR:\n{stderr}\nSTDOUT:\n{stdout}")

        json_lines = [line for line in stdout.splitlines() if line.strip() and not line.startswith("__GENX320_READY__")]
        if not json_lines:
            raise RuntimeError(f"Remote GenX320 capture produced no final JSON.\nSTDERR:\n{stderr}\nSTDOUT:\n{stdout}")
        remote_summary = json.loads("\n".join(json_lines))
        remote_raw = str(remote_summary["raw_path"])
        remote_json = str(remote_summary["summary_path"])
        subprocess.run(["scp", f"{self.pi}:{remote_raw}", str(self.save_dir)], check=True)
        subprocess.run(["scp", f"{self.pi}:{remote_json}", str(self.save_dir)], check=True)

        local_raw = self.save_dir / Path(remote_raw).name
        local_remote_json = self.save_dir / Path(remote_json).name
        local_summary = self.save_dir / f"{local_raw.stem}_eventcam_summary.json"
        sensor_size = remote_summary.get("sensor_size") or {}
        payload = {
            "raw_path": str(local_raw.resolve()),
            "summary_path": str(local_summary.resolve()),
            "remote_raw_path": remote_raw,
            "remote_summary_path": remote_json,
            "remote_summary_copy": str(local_remote_json.resolve()),
            "sensor_size": sensor_size,
            "started_at": dt.datetime.fromtimestamp(self.rec_start_wall_ns / 1e9).isoformat(timespec="seconds"),
            "stopped_at": dt.datetime.fromtimestamp(rec_stop_wall_ns / 1e9).isoformat(timespec="seconds"),
            "rec_start_pc_ns": self.rec_start_pc_ns,
            "rec_stop_pc_ns": rec_stop_pc_ns,
            "total_slices": int(remote_summary.get("total_slices") or 0),
            "total_events": int(remote_summary.get("total_events") or 0),
            "first_event_ts_us": remote_summary.get("first_event_ts_us"),
            "last_event_ts_us": remote_summary.get("last_event_ts_us"),
            "extra_meta": self.extra_meta,
            "control_events": self.events_log,
        }
        local_summary.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        summary = GenX320RemoteRecordingSummary(
            raw_path=str(local_raw.resolve()),
            summary_json=str(local_summary.resolve()),
            rec_start_pc_ns=self.rec_start_pc_ns,
            rec_stop_pc_ns=rec_stop_pc_ns,
            rec_start_wall_ns=self.rec_start_wall_ns,
            rec_stop_wall_ns=rec_stop_wall_ns,
            sensor_width=int(sensor_size.get("width") or 0),
            sensor_height=int(sensor_size.get("height") or 0),
            total_slices=int(remote_summary.get("total_slices") or 0),
            total_events=int(remote_summary.get("total_events") or 0),
            first_event_ts_us=remote_summary.get("first_event_ts_us"),
            last_event_ts_us=remote_summary.get("last_event_ts_us"),
            remote_raw_path=remote_raw,
            remote_summary_json=remote_json,
        )
        self._start_postprocess(summary)
        return summary

    def _start_postprocess(self, summary: GenX320RemoteRecordingSummary) -> None:
        command = [
            self.python_exe,
            "eventcam_raw_postprocess.py",
            summary.summary_json,
            "--output-dir",
            self.postprocess_output_dir,
            "--fps",
            f"{self.postprocess_fps:g}",
            "--video-fps",
            f"{self.postprocess_video_fps:g}",
            "--accumulation-us",
            str(max(0, self.postprocess_accumulation_us)),
        ]
        if self.export_filtered_events_npz:
            command.append("--export-filtered-events-npz")
        if self.sync_led_roi:
            command.extend(["--sync-led-roi", self.sync_led_roi])
        if self.mask_led_roi:
            command.append("--mask-led-roi")

        summary.postprocess_command = command
        subprocess.run(command, check=True)

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.stop_recording()
            except Exception:
                self.proc.kill()
                self.proc = None
