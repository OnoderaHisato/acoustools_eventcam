#!/usr/bin/env python3
"""Create low-cost side-by-side videos from stereo event NPZ recordings.

This intentionally does not run particle tracking, triangulation, or ideal
comparison.  For uncompressed NPZ files it memory-maps the stored events.npy
member in place and touches only the short accumulation windows used by the
low-frame-rate preview.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import struct
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np


DEFAULT_ROOT = Path("stereo_acoustools_3d_records_auto")
LOCAL_FILE_HEADER = struct.Struct("<IHHHHHIIIHH")
ZIP_LOCAL_FILE_SIGNATURE = 0x04034B50


@dataclass
class MappedEvents:
    path: Path
    events: np.memmap
    output_start_ts_us: int
    output_end_ts_us: int
    sensor_width: int
    sensor_height: int

    def close(self) -> None:
        mmap_handle = getattr(self.events, "_mmap", None)
        if mmap_handle is not None:
            mmap_handle.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Render lightweight side-by-side stereo event videos without particle tracking. "
            "Input may be one or more run directories, or a root containing runs."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("paths", nargs="*", type=Path, default=[DEFAULT_ROOT])
    parser.add_argument("--fps", type=float, default=5.0, help="Sampled video frames per second.")
    parser.add_argument(
        "--accumulation-ms",
        type=float,
        default=50.0,
        help="Event accumulation represented by each sampled frame.",
    )
    parser.add_argument("--scale", type=float, default=0.5, help="Per-camera output scale.")
    parser.add_argument("--pre-roll-sec", type=float, default=0.25)
    parser.add_argument("--post-roll-sec", type=float, default=0.25)
    parser.add_argument(
        "--full-recording",
        action="store_true",
        help="Render the whole common recording interval instead of the PAT-motion window.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of runs; 0 means all.")
    parser.add_argument("--start-index", type=int, default=0, help="Skip this many discovered runs.")
    parser.add_argument("--gain", type=float, default=70.0, help="Brightness per event count.")
    parser.add_argument("--point-size", type=int, default=1, choices=[1, 2, 3])
    parser.add_argument(
        "--keep-led-events",
        action="store_true",
        help="Do not mask the PAT-start LED ROI recorded in pipeline_manifest.json.",
    )
    parser.add_argument("--codec", default="mp4v", help="OpenCV FourCC codec.")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--stop-on-error", action="store_true")
    return parser.parse_args(argv)


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON root must be an object: {path}")
    return payload


def discover_runs(paths: Iterable[Path]) -> list[Path]:
    runs: set[Path] = set()
    for raw_path in paths:
        path = raw_path.resolve()
        if (path / "pipeline_manifest.json").exists():
            runs.add(path)
            continue
        if path.exists():
            runs.update(manifest.parent.resolve() for manifest in path.rglob("pipeline_manifest.json"))
    return sorted(runs, key=lambda run: run.name)


def _npy_header(reader: Any) -> tuple[tuple[int, ...], bool, np.dtype[Any]]:
    version = np.lib.format.read_magic(reader)
    if version == (1, 0):
        return np.lib.format.read_array_header_1_0(reader)
    if version == (2, 0):
        return np.lib.format.read_array_header_2_0(reader)
    if version == (3, 0):
        return np.lib.format.read_array_header_2_0(reader)
    raise RuntimeError(f"Unsupported NPY header version: {version}")


def mmap_stored_npy(npz_path: Path, member_name: str = "events.npy") -> np.memmap:
    """Memory-map a ZIP_STORED NPY member without extracting or loading it."""
    npz_path = npz_path.resolve()
    with zipfile.ZipFile(npz_path) as archive:
        try:
            info = archive.getinfo(member_name)
        except KeyError as exc:
            raise RuntimeError(f"{npz_path} does not contain {member_name}") from exc
        if info.compress_type != zipfile.ZIP_STORED:
            raise RuntimeError(
                f"Lightweight mmap preview requires an uncompressed NPZ: {npz_path}. "
                "This file's events.npy is compressed."
            )
        if info.flag_bits & 0x1:
            raise RuntimeError(f"Encrypted NPZ members are unsupported: {npz_path}")
        local_header_offset = int(info.header_offset)

    with npz_path.open("rb") as handle:
        handle.seek(local_header_offset)
        fixed = handle.read(LOCAL_FILE_HEADER.size)
        if len(fixed) != LOCAL_FILE_HEADER.size:
            raise RuntimeError(f"Truncated ZIP local header: {npz_path}")
        values = LOCAL_FILE_HEADER.unpack(fixed)
        signature, filename_length, extra_length = values[0], values[-2], values[-1]
        if signature != ZIP_LOCAL_FILE_SIGNATURE:
            raise RuntimeError(f"Invalid ZIP local header signature: {npz_path}")
        handle.seek(int(filename_length) + int(extra_length), 1)
        shape, fortran_order, dtype = _npy_header(handle)
        array_offset = handle.tell()

    if fortran_order:
        raise RuntimeError(f"Fortran-order event arrays are unsupported: {npz_path}")
    if len(shape) != 1:
        raise RuntimeError(f"Expected a one-dimensional events array in {npz_path}, got {shape}")
    required_fields = {"x", "y", "p", "t"}
    missing = required_fields.difference(dtype.names or ())
    if missing:
        raise RuntimeError(f"Event dtype in {npz_path} is missing fields: {sorted(missing)}")
    return np.memmap(
        npz_path,
        mode="r",
        dtype=dtype,
        offset=array_offset,
        shape=shape,
        order="C",
    )


def npz_scalar(npz_path: Path, name: str, default: int) -> int:
    with np.load(npz_path, allow_pickle=False) as payload:
        if name not in payload.files:
            return int(default)
        return int(np.asarray(payload[name]).item())


def mapped_events(npz_path: Path, width: int, height: int) -> MappedEvents:
    events = mmap_stored_npy(npz_path)
    if events.size == 0:
        raise RuntimeError(f"Events array is empty: {npz_path}")
    first_ts = int(events[0]["t"])
    last_ts = int(events[-1]["t"]) + 1
    return MappedEvents(
        path=npz_path.resolve(),
        events=events,
        output_start_ts_us=npz_scalar(npz_path, "output_start_ts_us", first_ts),
        output_end_ts_us=npz_scalar(npz_path, "output_end_ts_us", last_ts),
        sensor_width=int(width),
        sensor_height=int(height),
    )


def parse_roi(value: Any) -> tuple[int, int, int, int] | None:
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        return None
    if len(parts) != 4:
        return None
    try:
        x0, y0, x1, y1 = (int(part) for part in parts)
    except (TypeError, ValueError):
        return None
    return x0, y0, x1, y1


def led_mask_from_manifest(
    manifest: dict[str, Any], keep_led_events: bool
) -> tuple[str, tuple[int, int, int, int] | None]:
    if keep_led_events:
        return "off", None
    led = manifest.get("pat_start_led", {})
    if not isinstance(led, dict):
        return "off", None
    return str(led.get("side", "off")), parse_roi(led.get("roi"))


def sensor_size(recording_manifest: dict[str, Any]) -> tuple[int, int]:
    left = recording_manifest.get("left", {})
    meta = left.get("meta", {}) if isinstance(left, dict) else {}
    size = meta.get("sensor_size", {}) if isinstance(meta, dict) else {}
    return int(size.get("width", 1280)), int(size.get("height", 720))


def event_slice(source: MappedEvents, start_rel_us: int, end_rel_us: int) -> np.ndarray:
    absolute_start = int(source.output_start_ts_us) + int(start_rel_us)
    absolute_end = int(source.output_start_ts_us) + int(end_rel_us)
    timestamps = source.events["t"]
    left = int(np.searchsorted(timestamps, absolute_start, side="left"))
    right = int(np.searchsorted(timestamps, absolute_end, side="left"))
    return source.events[left:right]


def render_events(
    events: np.ndarray,
    sensor_width: int,
    sensor_height: int,
    output_width: int,
    output_height: int,
    gain: float,
    point_size: int,
    mask_roi: tuple[int, int, int, int] | None,
) -> np.ndarray:
    frame = np.zeros((output_height, output_width, 3), dtype=np.uint8)
    if events.size == 0:
        return frame
    x = events["x"].astype(np.int32, copy=False)
    y = events["y"].astype(np.int32, copy=False)
    valid = (x >= 0) & (x < sensor_width) & (y >= 0) & (y < sensor_height)
    if mask_roi is not None:
        x0, y0, x1, y1 = mask_roi
        valid &= ~((x >= x0) & (x < x1) & (y >= y0) & (y < y1))
    if not np.any(valid):
        return frame
    scaled_x = np.minimum((x[valid] * output_width) // sensor_width, output_width - 1)
    scaled_y = np.minimum((y[valid] * output_height) // sensor_height, output_height - 1)
    polarity = events["p"][valid]

    positive = np.zeros((output_height, output_width), dtype=np.uint16)
    negative = np.zeros_like(positive)
    on = polarity > 0
    if np.any(on):
        np.add.at(positive, (scaled_y[on], scaled_x[on]), 1)
    if np.any(~on):
        np.add.at(negative, (scaled_y[~on], scaled_x[~on]), 1)
    if point_size > 1:
        kernel = np.ones((point_size, point_size), dtype=np.uint8)
        positive = cv2.dilate(positive, kernel)
        negative = cv2.dilate(negative, kernel)
    positive_u8 = np.clip(positive.astype(np.float32) * float(gain), 0, 255).astype(np.uint8)
    negative_u8 = np.clip(negative.astype(np.float32) * float(gain), 0, 255).astype(np.uint8)
    frame[:, :, 2] = positive_u8
    frame[:, :, 0] = negative_u8
    frame[:, :, 1] = np.minimum(positive_u8, negative_u8) // 2
    return frame


def annotate_frame(
    frame: np.ndarray,
    side: str,
    recording_time_sec: float,
    motion_start_sec: float,
    event_count: int,
    roi_masked: bool,
) -> None:
    cv2.rectangle(frame, (0, 0), (frame.shape[1], 42), (0, 0, 0), -1)
    pat_time = recording_time_sec - motion_start_sec
    status = (
        f"{side.upper()}  rec={recording_time_sec:6.2f}s  "
        f"PAT={pat_time:+6.2f}s  events={event_count:,}"
    )
    if roi_masked:
        status += "  LED ROI masked"
    cv2.putText(frame, status, (10, 27), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (230, 230, 230), 1, cv2.LINE_AA)


def preview_interval(
    manifest: dict[str, Any],
    timing: dict[str, Any],
    recording_duration_sec: float,
    args: argparse.Namespace,
) -> tuple[float, float, float]:
    motion_start = float(timing.get("ideal_start_in_recording_sec", 0.0))
    if args.full_recording:
        return 0.0, recording_duration_sec, motion_start
    motion_duration = float(
        manifest.get(
            "expected_duration_sec",
            timing.get("expected_motion_duration_sec", recording_duration_sec),
        )
    )
    start = max(0.0, motion_start - float(args.pre_roll_sec))
    end = min(
        recording_duration_sec,
        motion_start + motion_duration + float(args.post_roll_sec),
    )
    return start, end, motion_start


def render_run(run_dir: Path, args: argparse.Namespace) -> Path:
    started = time.perf_counter()
    pipeline_manifest = load_json(run_dir / "pipeline_manifest.json")
    timing = load_json(run_dir / "pat_camera_timing.json")
    camera_dir = run_dir / "stereo_recording"
    recording_manifest = load_json(camera_dir / "stereo_recording_manifest.json")
    left_path = camera_dir / "left" / "left_events.npz"
    right_path = camera_dir / "right" / "right_events.npz"
    width, height = sensor_size(recording_manifest)
    left = mapped_events(left_path, width, height)
    right = mapped_events(right_path, width, height)

    common_start_ts = max(left.output_start_ts_us, right.output_start_ts_us)
    common_end_ts = min(left.output_end_ts_us, right.output_end_ts_us)
    if common_end_ts <= common_start_ts:
        raise RuntimeError(f"No common stereo timestamp interval: {run_dir}")
    # Recording-relative timing uses the common synchronized start. Current recordings use equal starts.
    if left.output_start_ts_us != right.output_start_ts_us:
        raise RuntimeError(
            f"Stereo output_start_ts_us differs: left={left.output_start_ts_us}, "
            f"right={right.output_start_ts_us}"
        )
    recording_duration_sec = (common_end_ts - common_start_ts) * 1e-6
    start_sec, end_sec, motion_start_sec = preview_interval(
        pipeline_manifest, timing, recording_duration_sec, args
    )

    if args.fps <= 0 or args.accumulation_ms <= 0 or args.scale <= 0:
        raise RuntimeError("--fps, --accumulation-ms, and --scale must be positive")
    period_us = int(round(1_000_000.0 / float(args.fps)))
    accumulation_us = int(round(float(args.accumulation_ms) * 1000.0))
    if accumulation_us > period_us:
        raise RuntimeError(
            "--accumulation-ms must not exceed the frame period in lightweight mode "
            f"({period_us / 1000.0:g} ms at {args.fps:g} fps)."
        )
    frame_count = max(1, int(np.ceil((end_sec - start_sec) * float(args.fps))))
    output_width = max(1, int(round(width * float(args.scale))))
    output_height = max(1, int(round(height * float(args.scale))))
    combined_size = (output_width * 2, output_height)

    output_dir = run_dir / "quick_stereo_preview"
    output_dir.mkdir(parents=True, exist_ok=True)
    fps_tag = f"{float(args.fps):g}".replace(".", "p")
    output_path = output_dir / f"stereo_lowrate_{fps_tag}fps.mp4"
    contact_path = output_dir / f"stereo_lowrate_{fps_tag}fps_contact_sheet.jpg"
    report_path = output_dir / f"stereo_lowrate_{fps_tag}fps_manifest.json"
    if output_path.exists() and not args.overwrite:
        left.close()
        right.close()
        print(f"[QUICK-PREVIEW] Skip existing: {output_path}")
        return output_path

    writer = cv2.VideoWriter(
        str(output_path),
        cv2.VideoWriter_fourcc(*str(args.codec)[:4]),
        float(args.fps),
        combined_size,
        True,
    )
    if not writer.isOpened():
        raise RuntimeError(f"Could not open video writer: {output_path}")

    led_side, led_roi = led_mask_from_manifest(
        pipeline_manifest, bool(args.keep_led_events)
    )
    contact_indices = {0, frame_count // 3, (2 * frame_count) // 3, frame_count - 1}
    contact_frames: list[np.ndarray] = []
    sampled_left_events = 0
    sampled_right_events = 0
    try:
        for frame_index in range(frame_count):
            frame_start_sec = start_sec + frame_index / float(args.fps)
            start_rel_us = int(round(frame_start_sec * 1_000_000.0))
            end_rel_us = min(
                int(round(end_sec * 1_000_000.0)),
                start_rel_us + accumulation_us,
            )
            left_events = event_slice(left, start_rel_us, end_rel_us)
            right_events = event_slice(right, start_rel_us, end_rel_us)
            sampled_left_events += int(left_events.size)
            sampled_right_events += int(right_events.size)
            left_frame = render_events(
                left_events, width, height, output_width, output_height,
                float(args.gain), int(args.point_size), led_roi if led_side == "left" else None,
            )
            right_frame = render_events(
                right_events, width, height, output_width, output_height,
                float(args.gain), int(args.point_size), led_roi if led_side == "right" else None,
            )
            annotate_frame(
                left_frame, "left", frame_start_sec, motion_start_sec,
                int(left_events.size), led_side == "left" and led_roi is not None,
            )
            annotate_frame(
                right_frame, "right", frame_start_sec, motion_start_sec,
                int(right_events.size), led_side == "right" and led_roi is not None,
            )
            combined = np.hstack((left_frame, right_frame))
            writer.write(combined)
            if frame_index in contact_indices:
                contact_frames.append(combined.copy())
    finally:
        writer.release()

    if contact_frames:
        cv2.imwrite(str(contact_path), np.vstack(contact_frames), [cv2.IMWRITE_JPEG_QUALITY, 90])
    elapsed = time.perf_counter() - started
    report = {
        "schema_version": 1,
        "created_at": dt.datetime.now().isoformat(timespec="milliseconds"),
        "purpose": "lightweight stereo field-of-view check; no particle tracking or triangulation",
        "run_dir": str(run_dir.resolve()),
        "left_npz": str(left.path),
        "right_npz": str(right.path),
        "events_access": "in-place mmap of ZIP_STORED events.npy",
        "left_total_events": int(left.events.size),
        "right_total_events": int(right.events.size),
        "left_sampled_events": sampled_left_events,
        "right_sampled_events": sampled_right_events,
        "sensor_size": [width, height],
        "per_camera_video_size": [output_width, output_height],
        "combined_video_size": list(combined_size),
        "fps": float(args.fps),
        "accumulation_ms": float(args.accumulation_ms),
        "preview_start_sec": start_sec,
        "preview_end_sec": end_sec,
        "motion_start_sec": motion_start_sec,
        "frame_count": frame_count,
        "led_mask_side": led_side,
        "led_mask_roi": led_roi,
        "video": str(output_path.resolve()),
        "contact_sheet": str(contact_path.resolve()),
        "elapsed_sec": elapsed,
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    left.close()
    right.close()
    print(
        f"[QUICK-PREVIEW] Complete: {run_dir.name} | frames={frame_count}, "
        f"elapsed={elapsed:.2f}s -> {output_path}",
        flush=True,
    )
    return output_path


def write_run_overview(runs: Iterable[Path], fps: float) -> Path | None:
    """Collect one mid-motion stereo frame per run into a small overview image."""
    run_list = list(runs)
    if not run_list or any(run.parent != run_list[0].parent for run in run_list):
        return None
    fps_tag = f"{float(fps):g}".replace(".", "p")
    panels: list[np.ndarray] = []
    full_contact_panels: list[np.ndarray] = []
    for run in run_list:
        contact_path = (
            run / "quick_stereo_preview" / f"stereo_lowrate_{fps_tag}fps_contact_sheet.jpg"
        )
        image = cv2.imread(str(contact_path), cv2.IMREAD_COLOR)
        if image is None or image.shape[0] < 4:
            continue
        frame_height = image.shape[0] // 4
        panel = image[2 * frame_height : 3 * frame_height].copy()
        panel = cv2.resize(panel, (640, 180), interpolation=cv2.INTER_AREA)
        cv2.rectangle(panel, (0, 0), (640, 25), (0, 0, 0), -1)
        cv2.putText(
            panel,
            run.name,
            (7, 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
        panels.append(panel)
        full_panel = cv2.resize(image, (640, 720), interpolation=cv2.INTER_AREA)
        cv2.rectangle(full_panel, (0, 0), (640, 25), (0, 0, 0), -1)
        cv2.putText(
            full_panel,
            run.name,
            (7, 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
        full_contact_panels.append(full_panel)
    if not panels:
        return None
    if len(panels) % 2:
        panels.append(np.zeros_like(panels[0]))
    rows = [np.hstack(panels[index : index + 2]) for index in range(0, len(panels), 2)]
    overview = np.vstack(rows)
    output_path = run_list[0].parent / f"quick_stereo_preview_overview_{fps_tag}fps.jpg"
    cv2.imwrite(str(output_path), overview, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if len(full_contact_panels) % 2:
        full_contact_panels.append(np.zeros_like(full_contact_panels[0]))
    full_rows = [
        np.hstack(full_contact_panels[index : index + 2])
        for index in range(0, len(full_contact_panels), 2)
    ]
    full_output_path = (
        run_list[0].parent / f"quick_stereo_preview_all_times_{fps_tag}fps.jpg"
    )
    cv2.imwrite(
        str(full_output_path),
        np.vstack(full_rows),
        [cv2.IMWRITE_JPEG_QUALITY, 92],
    )
    print(f"[QUICK-PREVIEW] Run overview: {output_path}", flush=True)
    print(f"[QUICK-PREVIEW] All-times overview: {full_output_path}", flush=True)
    return output_path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runs = discover_runs(args.paths)
    runs = runs[max(0, int(args.start_index)) :]
    if int(args.limit) > 0:
        runs = runs[: int(args.limit)]
    if not runs:
        print("[QUICK-PREVIEW][ERROR] No run directories with pipeline_manifest.json were found.")
        return 2
    print(
        f"[QUICK-PREVIEW] runs={len(runs)}, fps={args.fps:g}, "
        f"accumulation={args.accumulation_ms:g}ms, scale={args.scale:g}",
        flush=True,
    )
    failures = 0
    for run_dir in runs:
        try:
            render_run(run_dir, args)
        except KeyboardInterrupt:
            print("\n[QUICK-PREVIEW] Interrupted by user.")
            return 130
        except Exception as exc:
            failures += 1
            print(f"[QUICK-PREVIEW][ERROR] {run_dir}: {exc}", flush=True)
            if args.stop_on_error:
                return 1
    write_run_overview(runs, float(args.fps))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
