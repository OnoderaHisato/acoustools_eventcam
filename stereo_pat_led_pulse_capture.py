#!/usr/bin/env python3
"""Capture a stationary PAT LED pulse train with synchronized stereo cameras."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import torch

from acoustools.Levitator import LevitatorController
from acoustools.Utilities import add_lev_sig

from acoustools_eventcam_sync import (
    compute_holograms_for_positions,
    mute_sync_transducer,
    prepare_message_from_holograms,
)
from stereo_acoustools_3d_recording_core import remove_failed_run, wait_for_capture_marker
import stereo_eventcam_record_sync as stereo_record


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left-serial", default="00000508")
    parser.add_argument("--right-serial", default="00000509")
    parser.add_argument("--frame-rate-hz", type=int, default=1000)
    parser.add_argument("--pulse-ms", type=float, default=100.0)
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--capture-duration-sec", type=float, default=2.0)
    parser.add_argument("--camera-start-delay-sec", type=float, default=0.5)
    parser.add_argument("--output-root", type=Path, default=Path(".tmp"))
    parser.add_argument("--keep-failed-capture", action="store_true")
    return parser.parse_args()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    args = parse_args()
    if args.frame_rate_hz <= 0 or args.pulse_ms <= 0 or args.cycles <= 0:
        raise SystemExit("Frame rate, pulse width, and cycle count must be positive.")
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = args.output_root.resolve()
    run_dir = (output_root / f"pat_led_pulse_test_{stamp}").resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    camera_run = run_dir / "stereo_recording"
    marker_path = run_dir / "capture_start_marker.json"

    lev = LevitatorController(ids=(101, 3))
    recorder: subprocess.Popen[Any] | None = None
    muted_hologram: torch.Tensor | None = None
    capture_succeeded = False
    try:
        base_hologram = compute_holograms_for_positions([(0.0, 0.0, 0.0)])[0]
        muted_hologram = mute_sync_transducer(base_hologram)
        lev.levitate(muted_hologram)
        time.sleep(0.5)
        set_frame_rate_return = lev.set_frame_rate(int(args.frame_rate_hz))
        frames_per_level = max(1, int(round(args.frame_rate_hz * args.pulse_ms * 1e-3)))
        holograms: list[torch.Tensor] = []
        for _ in range(int(args.cycles)):
            holograms.extend([base_hologram] * frames_per_level)
            holograms.extend([muted_hologram] * frames_per_level)
        phases, amplitudes, geometry_count = prepare_message_from_holograms(
            lev, holograms, permute=True
        )

        command = [
            sys.executable,
            str(Path(stereo_record.__file__).resolve()),
            "--run-dir", str(camera_run),
            "--left-serial", str(args.left_serial),
            "--right-serial", str(args.right_serial),
            "--duration-sec", f"{args.capture_duration_sec:.6f}",
            "--delta-t-us", "1000",
            "--start-delay-sec", f"{args.camera_start_delay_sec:.6f}",
            "--hw-sync", "left-master",
            "--capture-start-marker", str(marker_path),
            "--no-preview",
            "--note", "stationary PAT sync LED pulse train ROI test",
        ]
        print("[CAPTURE] " + subprocess.list2cmdline(command), flush=True)
        recorder = subprocess.Popen(command)
        marker = wait_for_capture_marker(recorder, marker_path, timeout_sec=20.0)
        marker_seen_perf_ns = time.perf_counter_ns()
        pat_send_start_perf_ns = time.perf_counter_ns()
        pat_send_start_wall_ns = time.time_ns()
        lev.send_message(
            phases,
            amplitudes,
            0,
            int(geometry_count),
            sleep_ms=0,
            loop=True,
            num_loops=1,
        )
        pat_send_end_perf_ns = time.perf_counter_ns()
        lev.levitate(muted_hologram)
        if recorder.wait(timeout=max(30.0, args.capture_duration_sec + 20.0)) != 0:
            raise RuntimeError(f"Stereo recorder failed with code {recorder.returncode}.")
        recorder = None

        camera_elapsed_at_marker_sec = (
            int(marker.get("camera_ts_at_marker_us", marker["capture_start_ts_us"]))
            - int(marker["capture_start_ts_us"])
        ) * 1e-6
        expected_t_recording_sec = camera_elapsed_at_marker_sec + (
            pat_send_start_perf_ns - int(marker["pc_start_perf_ns"])
        ) * 1e-9
        result = {
            "schema_version": 1,
            "run_dir": str(run_dir),
            "capture_marker": marker,
            "capture_marker_seen_perf_ns": marker_seen_perf_ns,
            "pat_send_start_perf_ns": pat_send_start_perf_ns,
            "pat_send_start_wall_ns": pat_send_start_wall_ns,
            "pat_send_end_perf_ns": pat_send_end_perf_ns,
            "pat_send_call_duration_sec": (pat_send_end_perf_ns - pat_send_start_perf_ns) * 1e-9,
            "expected_t_recording_sec": expected_t_recording_sec,
            "frame_rate_requested_hz": float(args.frame_rate_hz),
            "set_frame_rate_return": str(set_frame_rate_return),
            "frames_per_level": frames_per_level,
            "pulse_ms": float(args.pulse_ms),
            "cycles": int(args.cycles),
            "geometry_count": int(geometry_count),
        }
        write_json(run_dir / "pat_led_pulse_timing.json", result)
        capture_succeeded = True
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    finally:
        if recorder is not None and recorder.poll() is None:
            try:
                recorder.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                recorder.terminate()
                recorder.wait(timeout=5.0)
        if muted_hologram is not None:
            try:
                lev.levitate(muted_hologram)
                off_phase = add_lev_sig(torch.zeros_like(muted_hologram))
                phases, amplitudes, _ = prepare_message_from_holograms(
                    lev, [off_phase], permute=True
                )
                lev.send_message(
                    phases, amplitudes, 0, 1, sleep_ms=0, loop=False, num_loops=1
                )
            except Exception:
                pass
        if not capture_succeeded and run_dir.exists() and not args.keep_failed_capture:
            try:
                remove_failed_run(run_dir, output_root)
                print(f"[CAPTURE] Failed pulse-test data permanently deleted: {run_dir}")
            except Exception as exc:
                print(f"[CAPTURE] WARNING: could not delete failed pulse-test data: {exc}")
        print("[PAT] LED pulse test shutdown complete.")


if __name__ == "__main__":
    raise SystemExit(main())
