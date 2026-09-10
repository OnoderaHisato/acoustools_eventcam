"""Windows-side HTTP recorder client for a Pi-hosted GenX320 camera."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass
class GenX320HttpRecordingSummary:
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
    timestamps_csv: Optional[str] = None
    trigger_events_csv: Optional[str] = None


class GenX320HttpEventCameraRecorder:
    def __init__(
        self,
        *,
        base_url: str,
        save_dir: str,
        remote_dir: str = "~/genx320_http_captures",
        setup_v4l: bool = True,
        postprocess_output_dir: str = "./proc_eventcam_genx320_http",
        postprocess_fps: float = 1000.0,
        postprocess_video_fps: float = 60.0,
        postprocess_accumulation_us: int = 1000,
        postprocess_write_video: bool = True,
        export_filtered_events_npz: bool = True,
        sync_led_roi: str = "",
        sync_led_bin_us: int = 100,
        sync_led_threshold: int = 0,
        sync_pre_roll_us: int = 0,
        sync_duration_us: int = 0,
        mask_led_roi: bool = False,
        mask_rois: Optional[list[str]] = None,
        kill_existing_viewer: bool = False,
        python_exe: str = sys.executable,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.save_dir = Path(save_dir).resolve()
        self.remote_dir = remote_dir
        self.setup_v4l = bool(setup_v4l)
        self.postprocess_output_dir = str(postprocess_output_dir)
        self.postprocess_fps = float(postprocess_fps)
        self.postprocess_video_fps = float(postprocess_video_fps)
        self.postprocess_accumulation_us = int(postprocess_accumulation_us)
        self.postprocess_write_video = bool(postprocess_write_video)
        self.export_filtered_events_npz = bool(export_filtered_events_npz)
        self.sync_led_roi = str(sync_led_roi or "")
        self.sync_led_bin_us = int(sync_led_bin_us)
        self.sync_led_threshold = int(sync_led_threshold)
        self.sync_pre_roll_us = int(sync_pre_roll_us)
        self.sync_duration_us = int(sync_duration_us)
        self.mask_led_roi = bool(mask_led_roi)
        self.mask_rois = list(mask_rois or [])
        self.kill_existing_viewer = bool(kill_existing_viewer)
        self.python_exe = python_exe
        self.save_dir.mkdir(parents=True, exist_ok=True)
        self.rec_start_pc_ns = 0
        self.rec_start_wall_ns = 0
        self.events_log: list[dict[str, Any]] = []
        self.extra_meta: dict[str, Any] = {}
        self.remote_started_summary: dict[str, Any] = {}

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 60.0) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def start_recording(self, safe_base: str, extra_meta: Optional[dict[str, Any]] = None) -> dict[str, str]:
        self.extra_meta = dict(extra_meta or {})
        self.events_log = []
        payload = {
            "basename": safe_base,
            "output_dir": self.remote_dir,
            "setup_v4l": self.setup_v4l,
            "kill_existing_viewer": self.kill_existing_viewer,
            "delta_t_us": 10_000,
        }
        response = self._request_json("POST", "/start", payload, timeout=90.0)
        if not response.get("ready"):
            raise RuntimeError(f"GenX320 HTTP server did not become ready: {response}")
        self.rec_start_pc_ns = int(time.perf_counter_ns())
        self.rec_start_wall_ns = int(time.time_ns())
        self.remote_started_summary = dict(response.get("summary") or {})
        return {
            "raw_path": str(self.remote_started_summary.get("raw_path") or ""),
            "summary_json": str(self.remote_started_summary.get("summary_path") or ""),
        }

    def log_event(self, name: str, pc_ns: Optional[int] = None, wall_ns: Optional[int] = None, note: str = "") -> None:
        self.events_log.append(
            {
                "event": str(name),
                "pc_ns": int(pc_ns if pc_ns is not None else time.perf_counter_ns()),
                "wall_ns": int(wall_ns if wall_ns is not None else time.time_ns()),
                "note": str(note or ""),
            }
        )

    def stop_recording(self) -> GenX320HttpRecordingSummary:
        rec_stop_pc_ns = int(time.perf_counter_ns())
        rec_stop_wall_ns = int(time.time_ns())
        response = self._request_json("POST", "/stop", {}, timeout=120.0)
        remote_summary = dict(response.get("summary") or {})
        remote_raw = str(remote_summary["raw_path"])
        remote_json = str(remote_summary["summary_path"])

        local_raw = self.save_dir / Path(remote_raw).name
        local_remote_json = self.save_dir / Path(remote_json).name
        self._download(remote_raw, local_raw)
        self._download(remote_json, local_remote_json)

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

        summary = GenX320HttpRecordingSummary(
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

    def _download(self, remote_path: str, local_path: Path) -> None:
        query = urllib.parse.urlencode({"path": remote_path})
        url = f"{self.base_url}/download?{query}"
        with urllib.request.urlopen(url, timeout=120.0) as resp, local_path.open("wb") as f:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)

    def _start_postprocess(self, summary: GenX320HttpRecordingSummary) -> None:
        sync_duration_us = max(0, self.sync_duration_us)
        if sync_duration_us <= 0:
            try:
                expected_duration_sec = float(self.extra_meta.get("expected_duration_sec") or 0.0)
            except Exception:
                expected_duration_sec = 0.0
            if expected_duration_sec > 0:
                sync_duration_us = int(round(expected_duration_sec * 1_000_000))

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
        if not self.postprocess_write_video:
            command.append("--no-video")
        if self.sync_led_roi:
            command.extend(
                [
                    "--sync-led-roi",
                    self.sync_led_roi,
                    "--sync-led-bin-us",
                    str(max(1, self.sync_led_bin_us)),
                    "--sync-led-threshold",
                    str(max(0, self.sync_led_threshold)),
                    "--sync-pre-roll-us",
                    str(max(0, self.sync_pre_roll_us)),
                    "--duration-us",
                    str(sync_duration_us),
                ]
            )
        if self.mask_led_roi:
            command.append("--mask-led-roi")
        for roi in self.mask_rois:
            if roi:
                command.extend(["--mask-roi", str(roi)])
        summary.postprocess_command = command
        subprocess.run(command, check=True)

    def close(self) -> None:
        return
