#!/usr/bin/env python3
"""Re-render existing checkerboard calibration NPZ files with new parameters."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import eventcam_checkerboard_calibration_capture as capture


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reprocess checkerboard calibration NPZ files into calibration PNGs.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", type=Path, help="Capture output directory or directory containing *_events.npz files.")
    parser.add_argument("--output-dir", type=Path, default=None, help="Output directory. Empty overwrites calib_images/candidates in input.")
    parser.add_argument("--frame-window-us", type=int, default=20_000, help="Default accumulation window.")
    parser.add_argument("--frame-window-us-list", default="", help="Comma-separated windows to try.")
    parser.add_argument("--candidate-count", type=int, default=1, help="Candidate windows per NPZ and window size.")
    parser.add_argument("--render-mode", choices=["signed", "abs", "on", "off", "all"], default="off", help="Render mode.")
    parser.add_argument(
        "--exhaustive-candidates",
        action="store_true",
        help="Render and test every requested candidate instead of stopping at the first detected board.",
    )
    parser.add_argument("--scale-percentile", type=float, default=99.5, help="Contrast scaling percentile.")
    parser.add_argument("--min-events", type=int, default=200, help="Minimum events in a candidate window.")
    parser.add_argument("--clear-output", action="store_true", help="Remove existing calib_images/candidates/corner_debug first.")
    return parser.parse_args()


def find_npz_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    raw_dir = path / "raw"
    if raw_dir.exists():
        files = sorted(raw_dir.glob("*_events.npz"))
        if files:
            return files
    return sorted(path.rglob("*_events.npz"))


def summary_for_npz(npz_path: Path) -> Path | None:
    stem = npz_path.stem
    if stem.endswith("_events"):
        candidate = npz_path.with_name(stem[:-7] + ".json")
        if candidate.exists():
            return candidate
    candidates = sorted(npz_path.parent.glob("*.json"))
    return candidates[0] if candidates else None


def sensor_size_for_npz(npz_path: Path) -> dict[str, int]:
    summary_path = summary_for_npz(npz_path)
    if summary_path is not None:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
        sensor = payload.get("sensor_size")
        if isinstance(sensor, dict) and "width" in sensor and "height" in sensor:
            return {"width": int(sensor["width"]), "height": int(sensor["height"])}
    raise SystemExit(f"Could not determine sensor_size for {npz_path}. Keep the capture JSON beside the NPZ.")


def pose_index_for_npz(npz_path: Path, fallback: int) -> int:
    summary_path = summary_for_npz(npz_path)
    if summary_path is not None:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
        if "pose_index" in payload:
            return int(payload["pose_index"])
    return fallback


def main() -> None:
    args = parse_args()
    npz_files = find_npz_files(args.input)
    if not npz_files:
        raise SystemExit(f"No *_events.npz files found under {args.input}")

    out_dir = args.output_dir or (args.input if args.input.is_dir() else args.input.parent)
    if args.clear_output:
        for name in ("calib_images", "candidates", "corner_debug"):
            path = out_dir / name
            if path.exists():
                shutil.rmtree(path)

    capture.parse_int_list(args.frame_window_us_list, args.frame_window_us, "--frame-window-us-list")
    print(f"Reprocessing {len(npz_files)} NPZ files into {out_dir}")
    ok_count = 0
    skip_count = 0
    for i, npz_path in enumerate(npz_files, start=1):
        pose_index = pose_index_for_npz(npz_path, i)
        try:
            report = capture.write_pose_images(npz_path, pose_index, out_dir, args, sensor_size_for_npz(npz_path))
        except Exception as exc:
            skip_count += 1
            print(f"[SKIP] pose_{pose_index:03d}: {npz_path} ({exc})")
            continue
        if report["selected_found_corners"]:
            ok_count += 1
        status = "OK" if report["selected_found_corners"] else "FAIL"
        print(f"[{status}] pose_{pose_index:03d}: {report['selected_calibration_image']}")
    print(f"Usable selected images: {ok_count}/{len(npz_files) - skip_count} processed, skipped={skip_count}")
    print(f"Calibration images: {out_dir / 'calib_images'}")


if __name__ == "__main__":
    main()
