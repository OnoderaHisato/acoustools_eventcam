#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Detect the PAT-start LED onset in one side of a synchronized stereo NPZ pair."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

import numpy as np


def parse_roi(text: str) -> tuple[int, int, int, int]:
    try:
        values = tuple(int(part.strip()) for part in text.split(","))
    except ValueError as exc:
        raise ValueError(f"LED ROI must be x0,y0,x1,y1: {text!r}") from exc
    if len(values) != 4 or values[2] <= values[0] or values[3] <= values[1]:
        raise ValueError(f"LED ROI must satisfy x1>x0 and y1>y0: {text!r}")
    return values


def detect_led_sync_npz(
    npz_path: Path,
    roi: tuple[int, int, int, int],
    *,
    bin_us: int = 100,
    threshold: int = 0,
    output_dir: Path | None = None,
    expected_t_recording_sec: float | None = None,
    search_half_window_sec: float = 0.1,
) -> dict[str, Any]:
    """Detect one LED onset without requiring the LED in the other camera."""
    npz_path = npz_path.resolve()
    if bin_us <= 0:
        raise ValueError("bin_us must be positive")
    with np.load(npz_path, allow_pickle=False) as data:
        events = np.asarray(data["events"])
        if events.size == 0:
            raise RuntimeError(f"Event NPZ is empty: {npz_path}")
        output_start_ts_us = int(data["output_start_ts_us"]) if "output_start_ts_us" in data else int(events["t"][0])
        output_end_ts_us = int(data["output_end_ts_us"]) if "output_end_ts_us" in data else int(events["t"][-1]) + 1
        side = str(np.asarray(data["side"]).item()) if "side" in data else npz_path.parent.name
        serial = str(np.asarray(data["camera_serial"]).item()) if "camera_serial" in data else ""
        hardware_synchronized = bool(np.asarray(data["hardware_synchronized"]).item()) if "hardware_synchronized" in data else False
        timestamp_domain_id = str(np.asarray(data["timestamp_domain_id"]).item()) if "timestamp_domain_id" in data else ""

    x0, y0, x1, y1 = roi
    in_roi = (
        (events["x"] >= x0) & (events["x"] < x1)
        & (events["y"] >= y0) & (events["y"] < y1)
        & (events["t"] >= output_start_ts_us) & (events["t"] < output_end_ts_us)
    )
    roi_events = events[in_roi]
    n_bins = max(1, int(np.ceil((output_end_ts_us - output_start_ts_us) / bin_us)))
    if roi_events.size:
        indices = ((roi_events["t"].astype(np.int64) - output_start_ts_us) // bin_us).astype(np.int64)
        valid_indices = (indices >= 0) & (indices < n_bins)
        counts = np.bincount(indices[valid_indices], minlength=n_bins).astype(np.int64)
    else:
        counts = np.zeros(n_bins, dtype=np.int64)

    configured_threshold = int(threshold)
    if threshold <= 0:
        median = float(np.median(counts))
        mad = float(np.median(np.abs(counts - median)))
        threshold = int(np.ceil(median + max(10.0, 8.0 * mad)))
    search_start_index = 0
    search_end_index = n_bins
    if expected_t_recording_sec is not None and search_half_window_sec > 0.0:
        search_start_sec = max(0.0, float(expected_t_recording_sec) - float(search_half_window_sec))
        search_end_sec = float(expected_t_recording_sec) + float(search_half_window_sec)
        search_start_index = max(0, int(np.floor(search_start_sec * 1e6 / bin_us)))
        search_end_index = min(n_bins, int(np.ceil(search_end_sec * 1e6 / bin_us)) + 1)
        if search_end_index <= search_start_index:
            raise RuntimeError("PAT-start LED search window is outside the saved recording interval.")
    peak_index = search_start_index + int(np.argmax(counts[search_start_index:search_end_index]))
    detected = int(counts[peak_index]) >= int(threshold)

    onset_index = peak_index
    while detected and onset_index > search_start_index and int(counts[onset_index - 1]) >= int(threshold):
        onset_index -= 1
    onset_bin_start_ts_us = output_start_ts_us + onset_index * bin_us
    onset_bin_end_ts_us = min(output_end_ts_us, onset_bin_start_ts_us + bin_us)
    onset_events = roi_events[
        (roi_events["t"] >= onset_bin_start_ts_us)
        & (roi_events["t"] < onset_bin_end_ts_us)
    ]
    sync_ts_us = int(onset_events["t"].min()) if onset_events.size else int(onset_bin_start_ts_us)
    sync_t_recording_sec = (sync_ts_us - output_start_ts_us) * 1e-6

    output_dir = (output_dir or npz_path.parent / "pat_start_led").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"{side}_pat_start_led_bins.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["bin_index", "bin_start_ts_us", "t_recording_sec", "roi_event_count", "above_threshold"])
        for index, count in enumerate(counts):
            timestamp = output_start_ts_us + index * bin_us
            writer.writerow([
                index,
                timestamp,
                f"{(timestamp - output_start_ts_us) * 1e-6:.9f}",
                int(count),
                int(count >= threshold),
            ])

    plot_path = output_dir / f"{side}_pat_start_led_counts.png"
    plot_output = ""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        plot_indices = np.arange(search_start_index, search_end_index, dtype=int)
        plot_time = plot_indices * bin_us * 1e-6
        fig, axis = plt.subplots(figsize=(10, 4))
        axis.plot(plot_time, counts[plot_indices], linewidth=0.8, color="tab:blue", label="ROI events/bin")
        axis.axhline(threshold, color="tab:red", linestyle="--", linewidth=1.0, label="threshold")
        axis.axvline(
            sync_t_recording_sec,
            color="tab:green" if detected else "tab:orange",
            linewidth=1.2,
            label="detected onset" if detected else "candidate peak (below threshold)",
        )
        if expected_t_recording_sec is not None:
            axis.axvline(expected_t_recording_sec, color="black", linestyle=":", linewidth=1.0, label="PC estimate")
        axis.set_xlabel("recording time [s]")
        axis.set_ylabel(f"events / {bin_us} us")
        axis.set_title(f"PAT-start LED detection: {side} camera only, ROI={roi}")
        axis.grid(True, linestyle=":", alpha=0.4)
        axis.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(plot_path, dpi=160)
        plt.close(fig)
        plot_output = str(plot_path)
    except Exception:
        plot_output = ""

    result: dict[str, Any] = {
        "schema_version": 1,
        "input_npz": str(npz_path),
        "side": side,
        "camera_serial": serial,
        "hardware_synchronized": hardware_synchronized,
        "timestamp_domain_id": timestamp_domain_id,
        "roi": list(roi),
        "bin_us": int(bin_us),
        "configured_threshold": configured_threshold,
        "resolved_threshold": int(threshold),
        "detected": bool(detected),
        "expected_t_recording_sec": expected_t_recording_sec,
        "search_half_window_sec": float(search_half_window_sec),
        "search_bin_range": [search_start_index, search_end_index],
        "peak_bin_index": peak_index,
        "peak_count": int(counts[peak_index]),
        "onset_bin_index": onset_index,
        "sync_ts_us": sync_ts_us,
        "output_start_ts_us": output_start_ts_us,
        "sync_t_recording_sec": sync_t_recording_sec,
        "report_csv": str(csv_path),
        "report_plot": plot_output,
    }
    json_path = output_dir / f"{side}_pat_start_led.json"
    result["report_json"] = str(json_path)
    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    if not detected:
        raise RuntimeError(
            "PAT-start LED was not detected in the selected side/ROI: "
            f"side={side}, roi={roi}, peak_count={int(counts[peak_index])}, "
            f"threshold={threshold}, diagnostic={json_path}"
        )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect PAT start from an LED visible in only one synchronized stereo camera."
    )
    parser.add_argument("npz", type=Path)
    parser.add_argument("--roi", required=True, help="LED ROI x0,y0,x1,y1.")
    parser.add_argument("--bin-us", type=int, default=100)
    parser.add_argument("--threshold", type=int, default=0, help="Event count threshold. 0 selects automatically.")
    parser.add_argument("--expected-t-sec", type=float, default=None, help="Optional approximate PAT start in recording time.")
    parser.add_argument("--search-half-window-sec", type=float, default=0.1)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = detect_led_sync_npz(
        args.npz,
        parse_roi(args.roi),
        bin_us=int(args.bin_us),
        threshold=int(args.threshold),
        output_dir=args.output_dir,
        expected_t_recording_sec=args.expected_t_sec,
        search_half_window_sec=float(args.search_half_window_sec),
    )
    print(
        f"PAT-start LED: side={result['side']}, "
        f"t_recording={result['sync_t_recording_sec']:.9f} s, "
        f"peak={result['peak_count']}, threshold={result['resolved_threshold']}"
    )
    print(f"Report: {result['report_json']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
