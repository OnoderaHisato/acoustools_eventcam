"""Smoke test for the GenX320 HTTP recorder API."""

from __future__ import annotations

import argparse
import time

from genx320_http_eventcam_recorder import GenX320HttpEventCameraRecorder


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--base-url", required=True, help="Example: http://192.168.50.2:8080")
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--save-dir", default="./rec_eventcam_genx320_http/api_smoke")
    parser.add_argument("--postprocess-dir", default="./proc_eventcam_genx320_http")
    parser.add_argument("--fps", type=float, default=1000.0)
    parser.add_argument("--video-fps", type=float, default=60.0)
    parser.add_argument("--accumulation-us", type=int, default=1000)
    parser.add_argument("--name", default="genx320_http_api_smoke")
    parser.add_argument("--no-npz", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    recorder = GenX320HttpEventCameraRecorder(
        base_url=args.base_url,
        save_dir=args.save_dir,
        postprocess_output_dir=args.postprocess_dir,
        postprocess_fps=args.fps,
        postprocess_video_fps=args.video_fps,
        postprocess_accumulation_us=args.accumulation_us,
        export_filtered_events_npz=not args.no_npz,
    )
    recorder.start_recording(args.name, extra_meta={"http_api_smoke_seconds": args.seconds})
    recorder.log_event("HTTP_API_SMOKE_START")
    time.sleep(max(0.0, args.seconds))
    recorder.log_event("HTTP_API_SMOKE_STOP_REQUEST")
    summary = recorder.stop_recording()
    print("RAW:", summary.raw_path)
    print("Summary:", summary.summary_json)
    print("Events:", summary.total_events)
    print("First/last ts us:", summary.first_event_ts_us, summary.last_event_ts_us)


if __name__ == "__main__":
    main()
