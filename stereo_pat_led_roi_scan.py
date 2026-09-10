#!/usr/bin/env python3
"""Rank spatial ROIs that contain a localized PAT-start event burst."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("events_npz", type=Path)
    parser.add_argument("--expected-sec", type=float, required=True)
    parser.add_argument("--search-before-sec", type=float, default=0.005)
    parser.add_argument("--search-after-sec", type=float, default=0.100)
    parser.add_argument("--bin-us", type=int, default=500)
    parser.add_argument("--tile-px", type=int, default=40)
    parser.add_argument("--sensor-width", type=int, default=1280)
    parser.add_argument("--sensor-height", type=int, default=720)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def scan(
    events_npz: Path,
    *,
    expected_sec: float,
    search_before_sec: float,
    search_after_sec: float,
    bin_us: int,
    tile_px: int,
    sensor_width: int,
    sensor_height: int,
    top: int,
    output_dir: Path,
) -> dict[str, Any]:
    events_npz = events_npz.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    with np.load(events_npz, allow_pickle=False) as data:
        events = np.asarray(data["events"])
        output_start_ts_us = int(data["output_start_ts_us"])
        side = str(np.asarray(data["side"]).item()) if "side" in data else events_npz.parent.name
        serial = str(np.asarray(data["camera_serial"]).item()) if "camera_serial" in data else ""

    start_sec = max(0.0, float(expected_sec) - float(search_before_sec))
    end_sec = float(expected_sec) + float(search_after_sec)
    start_ts_us = output_start_ts_us + int(np.floor(start_sec * 1e6))
    end_ts_us = output_start_ts_us + int(np.ceil(end_sec * 1e6))
    selected = events[(events["t"] >= start_ts_us) & (events["t"] < end_ts_us)]
    if selected.size == 0:
        raise RuntimeError("No events in the requested PAT-start search interval.")

    nx = int(np.ceil(sensor_width / tile_px))
    ny = int(np.ceil(sensor_height / tile_px))
    n_time = max(1, int(np.ceil((end_ts_us - start_ts_us) / bin_us)))
    tile_x = np.clip(selected["x"].astype(np.int64) // tile_px, 0, nx - 1)
    tile_y = np.clip(selected["y"].astype(np.int64) // tile_px, 0, ny - 1)
    tile_index = tile_y * nx + tile_x
    time_index = np.clip((selected["t"].astype(np.int64) - start_ts_us) // bin_us, 0, n_time - 1)
    combined = tile_index * n_time + time_index
    counts = np.bincount(combined, minlength=nx * ny * n_time).reshape(nx * ny, n_time)

    baseline = np.median(counts, axis=1)
    mad = np.median(np.abs(counts - baseline[:, None]), axis=1)
    peak_index = np.argmax(counts, axis=1)
    peak = counts[np.arange(nx * ny), peak_index]
    excess = peak - baseline
    robust_sigma = np.maximum(1.0, 1.4826 * mad)
    score = excess / robust_sigma
    total = np.sum(counts, axis=1)
    order = np.lexsort((-total, -peak, -score))

    candidates: list[dict[str, Any]] = []
    for flat in order[: max(1, int(top))]:
        ty, tx = divmod(int(flat), nx)
        peak_t_sec = start_sec + int(peak_index[flat]) * bin_us * 1e-6
        candidates.append({
            "roi": [
                tx * tile_px,
                ty * tile_px,
                min(sensor_width, (tx + 1) * tile_px),
                min(sensor_height, (ty + 1) * tile_px),
            ],
            "score": float(score[flat]),
            "peak_count": int(peak[flat]),
            "baseline_median": float(baseline[flat]),
            "baseline_mad": float(mad[flat]),
            "total_count": int(total[flat]),
            "peak_t_recording_sec": float(peak_t_sec),
            "delta_from_expected_sec": float(peak_t_sec - expected_sec),
        })

    result: dict[str, Any] = {
        "schema_version": 1,
        "events_npz": str(events_npz),
        "side": side,
        "camera_serial": serial,
        "expected_t_recording_sec": float(expected_sec),
        "search_interval_sec": [float(start_sec), float(end_sec)],
        "bin_us": int(bin_us),
        "tile_px": int(tile_px),
        "selected_event_count": int(selected.size),
        "candidates": candidates,
    }
    json_path = output_dir / f"{side}_led_roi_scan_tile{tile_px}_bin{bin_us}.json"
    result["report_json"] = str(json_path)
    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        heat = excess.reshape(ny, nx)
        fig, axis = plt.subplots(figsize=(12, 6))
        image = axis.imshow(
            heat,
            origin="upper",
            extent=[0, sensor_width, sensor_height, 0],
            interpolation="nearest",
            cmap="magma",
            aspect="auto",
        )
        for candidate in candidates[:5]:
            x0, y0, x1, y1 = candidate["roi"]
            axis.add_patch(plt.Rectangle((x0, y0), x1 - x0, y1 - y0, fill=False, color="cyan"))
        axis.set_title(f"{side} maximum event-count excess; {bin_us} us bins")
        axis.set_xlabel("x [px]")
        axis.set_ylabel("y [px]")
        fig.colorbar(image, ax=axis, label="peak - median events/tile/bin")
        fig.tight_layout()
        plot_path = output_dir / f"{side}_led_roi_scan_tile{tile_px}_bin{bin_us}.png"
        fig.savefig(plot_path, dpi=160)
        plt.close(fig)
        result["report_plot"] = str(plot_path)
        json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        result["report_plot"] = ""

    return result


def main() -> int:
    args = parse_args()
    result = scan(
        args.events_npz,
        expected_sec=args.expected_sec,
        search_before_sec=args.search_before_sec,
        search_after_sec=args.search_after_sec,
        bin_us=args.bin_us,
        tile_px=args.tile_px,
        sensor_width=args.sensor_width,
        sensor_height=args.sensor_height,
        top=args.top,
        output_dir=args.output_dir,
    )
    print(json.dumps(result["candidates"][:10], indent=2, ensure_ascii=False))
    print(f"Report: {result['report_json']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
