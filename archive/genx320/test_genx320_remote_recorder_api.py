"""Smoke test for the GenX320 remote recorder API.

This verifies the same start/log/stop shape that an AcousTools integration
will use, without moving the PAT.
"""

from __future__ import annotations

import argparse
import time

from genx320_remote_eventcam_recorder import GenX320RemoteEventCameraRecorder


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--pi", required=True, help="SSH target, e.g. eventcamera@192.168.100.132")
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--save-dir", default="./rec_eventcam_genx320/api_smoke")
    parser.add_argument("--postprocess-dir", default="./proc_eventcam_genx320")
    parser.add_argument("--fps", type=float, default=1000.0)
    parser.add_argument("--video-fps", type=float, default=60.0)
    parser.add_argument("--accumulation-us", type=int, default=1000)
    parser.add_argument("--name", default="genx320_api_smoke")
    parser.add_argument("--no-npz", action="store_true")
    parser.add_argument("--kill-existing-viewer", action="store_true", help="Stop metavision_viewer on the Pi before recording.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    recorder = GenX320RemoteEventCameraRecorder(
        pi=args.pi,
        save_dir=args.save_dir,
        postprocess_output_dir=args.postprocess_dir,
        postprocess_fps=args.fps,
        postprocess_video_fps=args.video_fps,
        postprocess_accumulation_us=args.accumulation_us,
        export_filtered_events_npz=not args.no_npz,
        kill_existing_viewer=args.kill_existing_viewer,
    )
    try:
        recorder.start_recording(args.name, extra_meta={"api_smoke_seconds": args.seconds})
        recorder.log_event("API_SMOKE_START")
        time.sleep(max(0.0, args.seconds))
        recorder.log_event("API_SMOKE_STOP_REQUEST")
        summary = recorder.stop_recording()
    finally:
        recorder.close()

    if summary is None:
        raise SystemExit("No summary returned.")
    print("RAW:", summary.raw_path)
    print("Summary:", summary.summary_json)
    print("Events:", summary.total_events)
    print("First/last ts us:", summary.first_event_ts_us, summary.last_event_ts_us)


if __name__ == "__main__":
    main()
