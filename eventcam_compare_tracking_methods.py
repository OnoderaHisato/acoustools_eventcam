#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compare event-camera tracking outputs by smoothness-oriented metrics."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import numpy as np


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare tracking-method directories containing event_centres_interp.csv.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("root", help="Directory containing method subdirectories, or one tracking output directory.")
    parser.add_argument("--output", default="", help="Output CSV path. Empty writes <root>/tracking_smoothness_metrics.csv.")
    parser.add_argument(
        "--cutoff-hz",
        type=float,
        default=100.0,
        help="Frequency cutoff for high-frequency power ratio.",
    )
    parser.add_argument(
        "--methods",
        nargs="*",
        default=[],
        help="Optional method subdirectory names to compare. Empty auto-detects.",
    )
    return parser.parse_args()


def load_interp_csv(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    data = np.genfromtxt(path, delimiter=",", names=True, dtype=float, encoding="utf-8")
    names = set(data.dtype.names or [])
    required = {"t_sec", "x_px", "y_px"}
    if not required.issubset(names):
        raise SystemExit(f"{path} must contain columns {sorted(required)}")
    return (
        np.atleast_1d(data["t_sec"]).astype(float),
        np.atleast_1d(data["x_px"]).astype(float),
        np.atleast_1d(data["y_px"]).astype(float),
    )


def finite_segments(valid: np.ndarray) -> list[slice]:
    segments: list[slice] = []
    start: int | None = None
    for i, ok in enumerate(valid):
        if ok and start is None:
            start = i
        elif not ok and start is not None:
            if i - start > 0:
                segments.append(slice(start, i))
            start = None
    if start is not None:
        segments.append(slice(start, len(valid)))
    return segments


def percentile(values: np.ndarray, q: float) -> float:
    values = values[np.isfinite(values)]
    if values.size == 0:
        return float("nan")
    return float(np.percentile(values, q))


def summarize_values(prefix: str, values: np.ndarray) -> dict[str, float]:
    return {
        f"median_{prefix}": percentile(values, 50),
        f"p95_{prefix}": percentile(values, 95),
        f"p99_{prefix}": percentile(values, 99),
        f"max_{prefix}": percentile(values, 100),
    }


def high_frequency_power_ratio(t: np.ndarray, x: np.ndarray, y: np.ndarray, valid: np.ndarray, cutoff_hz: float) -> float:
    segments = finite_segments(valid)
    best = max(segments, key=lambda s: s.stop - s.start, default=None)
    if best is None or best.stop - best.start < 8:
        return float("nan")

    t_seg = t[best]
    x_seg = x[best]
    y_seg = y[best]
    dt = float(np.median(np.diff(t_seg)))
    if not np.isfinite(dt) or dt <= 0:
        return float("nan")

    # Remove linear trend so slow drift does not dominate the spectrum.
    n = x_seg.size
    idx = np.arange(n, dtype=float)
    x_d = x_seg - np.polyval(np.polyfit(idx, x_seg, deg=1), idx)
    y_d = y_seg - np.polyval(np.polyfit(idx, y_seg, deg=1), idx)
    window = np.hanning(n)
    freqs = np.fft.rfftfreq(n, d=dt)
    px = np.abs(np.fft.rfft(x_d * window)) ** 2
    py = np.abs(np.fft.rfft(y_d * window)) ** 2
    power = px + py
    if power.size <= 1:
        return float("nan")
    power[0] = 0.0
    total = float(power.sum())
    if total <= 0:
        return float("nan")
    return float(power[freqs >= float(cutoff_hz)].sum() / total)


def max_nan_gap_sec(t: np.ndarray, valid: np.ndarray) -> float:
    if t.size == 0:
        return float("nan")
    invalid_segments = finite_segments(~valid)
    if not invalid_segments:
        return 0.0
    gaps = []
    for seg in invalid_segments:
        if seg.stop - seg.start <= 0:
            continue
        if seg.stop < t.size:
            gaps.append(float(t[seg.stop] - t[seg.start]))
        elif seg.start > 0:
            gaps.append(float(t[seg.stop - 1] - t[seg.start - 1]))
    return max(gaps) if gaps else 0.0


def metrics_for_file(method: str, path: Path, cutoff_hz: float) -> dict[str, object]:
    t, x, y = load_interp_csv(path)
    valid = np.isfinite(t) & np.isfinite(x) & np.isfinite(y)
    rows: dict[str, object] = {
        "method": method,
        "path": str(path),
        "points": int(t.size),
        "valid_points": int(valid.sum()),
        "valid_ratio": float(valid.sum() / max(t.size, 1)),
        "max_nan_gap_sec": max_nan_gap_sec(t, valid),
    }

    step_values: list[np.ndarray] = []
    second_values: list[np.ndarray] = []
    third_values: list[np.ndarray] = []
    for seg in finite_segments(valid):
        xs = x[seg]
        ys = y[seg]
        if xs.size >= 2:
            step_values.append(np.hypot(np.diff(xs), np.diff(ys)))
        if xs.size >= 3:
            second_values.append(np.hypot(np.diff(xs, n=2), np.diff(ys, n=2)))
        if xs.size >= 4:
            third_values.append(np.hypot(np.diff(xs, n=3), np.diff(ys, n=3)))

    steps = np.concatenate(step_values) if step_values else np.array([], dtype=float)
    seconds = np.concatenate(second_values) if second_values else np.array([], dtype=float)
    thirds = np.concatenate(third_values) if third_values else np.array([], dtype=float)
    rows.update(summarize_values("step_px", steps))
    rows.update(summarize_values("second_diff_px", seconds))
    rows.update(summarize_values("third_diff_px", thirds))
    rows["high_freq_power_ratio"] = high_frequency_power_ratio(t, x, y, valid, cutoff_hz)
    return rows


def discover_inputs(root: Path, methods: list[str]) -> list[tuple[str, Path]]:
    if methods:
        return [(method, root / method / "event_centres_interp.csv") for method in methods]
    direct = root / "event_centres_interp.csv"
    if direct.exists():
        return [(root.name, direct)]
    inputs = []
    for child in sorted(root.iterdir()):
        candidate = child / "event_centres_interp.csv"
        if child.is_dir() and candidate.exists():
            inputs.append((child.name, candidate))
    if not inputs:
        raise SystemExit(f"No event_centres_interp.csv found under {root}")
    return inputs


def format_row(row: dict[str, object], fieldnames: list[str]) -> dict[str, object]:
    out: dict[str, object] = {}
    for name in fieldnames:
        value = row.get(name, "")
        if isinstance(value, float):
            out[name] = "" if not np.isfinite(value) else f"{value:.9g}"
        else:
            out[name] = value
    return out


def main() -> None:
    args = parse_args()
    root = Path(args.root).resolve()
    if not root.exists():
        raise SystemExit(f"Root not found: {root}")

    rows = [metrics_for_file(method, path, float(args.cutoff_hz)) for method, path in discover_inputs(root, args.methods)]
    fieldnames = [
        "method",
        "points",
        "valid_points",
        "valid_ratio",
        "max_nan_gap_sec",
        "median_step_px",
        "p95_step_px",
        "p99_step_px",
        "max_step_px",
        "median_second_diff_px",
        "p95_second_diff_px",
        "p99_second_diff_px",
        "max_second_diff_px",
        "median_third_diff_px",
        "p95_third_diff_px",
        "p99_third_diff_px",
        "max_third_diff_px",
        "high_freq_power_ratio",
        "path",
    ]
    output = Path(args.output).resolve() if args.output else root / "tracking_smoothness_metrics.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(format_row(row, fieldnames))

    print(f"Output: {output}")
    for row in rows:
        print(
            f"{row['method']}: valid={row['valid_ratio']:.4f}, "
            f"p99_step={row['p99_step_px']:.4g}px, "
            f"p99_second={row['p99_second_diff_px']:.4g}px, "
            f"hf={row['high_freq_power_ratio']:.4g}"
        )


if __name__ == "__main__":
    main()
