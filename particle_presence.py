"""Is the particle still levitated after a run? A quick check on the recorded events (2026-10-16).

Every plan command starts and ends at the centre, and the recording runs a few seconds past the end of the
motion (capture tail margin). The 10 kHz strobe illumination makes even a particle at rest give a dense blob of
events, on a dark background. So, per camera:

* pre window: the 0.55 s before the PAT-start LED (particle at the start point, the centre), and
* tail window: from 0.5 s after the motion ends to the end of the recording (particle back at the centre),

are binned into 4 px cells (3x3 summed). The brightest cell of the pre window is the particle; the tail window
is searched within +-3 cells (12 px) of it, or of the session's reference position when given. ``ratio`` =
tail peak / pre peak. Measured on past runs (PARTICLE_PRESENCE_JP.md): with the particle the ratio is 0.86-1.05,
after a drop or an escape it is 0.00-0.01 (the tail is either an event storm or empty), so 0.3 is a wide margin.

Verdicts: ``present`` (both cameras >= RATIO_PRESENT), ``lost`` (both below), ``uncertain`` (they disagree, a
window is too short, or a file cannot be read), ``absent_at_start`` (the pre blob is far weaker than the
session's reference: the particle was already gone before the run). Only the time windows are read from the
uncompressed NPZ (memory map), a few seconds per run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

BIN_PX = 4
RATIO_PRESENT = 0.3
ABSENT_AT_START_FRACTION = 0.2  # pre peak below this share of the session reference -> no particle at the start
SEARCH_CELLS = 3
REFERENCE_DISTANCE_PX = 40  # the pre blob farther than this from the session's reference: not the particle
MIN_WINDOW_SEC = 0.3


def _onset_and_motion(run_dir: Path) -> tuple[float, float]:
    timing = json.loads((run_dir / "pat_camera_timing.json").read_text(encoding="utf-8"))
    led = timing.get("pat_start_led") or {}
    onset = led.get("sync_t_recording_sec")
    if onset is None:
        onset = timing.get("pat_start_led_expected_onset_sec") or 0.0
    return float(onset), float(timing.get("expected_motion_duration_sec") or 0.0)


def _rate_image(events: Any, start_ts: int, t0: float, t1: float) -> np.ndarray:
    ts = events["t"]
    i = int(np.searchsorted(ts, start_ts + int(t0 * 1e6)))
    j = int(np.searchsorted(ts, start_ts + int(t1 * 1e6)))
    chunk = events[i:j]
    rows, cols = 720 // BIN_PX, 1280 // BIN_PX
    img = np.zeros((rows, cols))
    if len(chunk):
        y = np.clip(np.asarray(chunk["y"]) // BIN_PX, 0, rows - 1)
        x = np.clip(np.asarray(chunk["x"]) // BIN_PX, 0, cols - 1)
        np.add.at(img, (y, x), 1.0)
    img /= max(t1 - t0, 1e-6)
    padded = np.pad(img, 1)
    return sum(padded[1 + dy:1 + dy + rows, 1 + dx:1 + dx + cols] for dy in (-1, 0, 1) for dx in (-1, 0, 1))


def check_camera(run_dir: Path, side: str, reference_px: tuple[int, int] | None = None) -> dict[str, Any]:
    from stereo_acoustools_3d_quick_preview import mapped_events

    onset, motion = _onset_and_motion(run_dir)
    source = mapped_events(run_dir / "stereo_recording" / side / f"{side}_events.npz", 1280, 720)
    try:
        start_ts = int(source.output_start_ts_us)
        end_rel = (int(source.output_end_ts_us) - start_ts) / 1e6
        pre_window = (max(0.0, onset - 0.6), max(0.0, onset - 0.05))
        tail_window = (onset + motion + 0.5, end_rel - 0.05)
        result: dict[str, Any] = {
            "pre_window_s": [round(v, 3) for v in pre_window],
            "tail_window_s": [round(v, 3) for v in tail_window],
        }
        if min(pre_window[1] - pre_window[0], tail_window[1] - tail_window[0]) < MIN_WINDOW_SEC:
            result["verdict"] = "uncertain"
            result["reason"] = "window too short"
            return result
        pre = _rate_image(source.events, start_ts, *pre_window)
        tail = _rate_image(source.events, start_ts, *tail_window)
    finally:
        close = getattr(source, "close", None)
        if callable(close):
            close()
    # The particle is the brightest cell of the pre window (it is far denser than anything else).
    py, px = np.unravel_index(int(np.argmax(pre)), pre.shape)
    pre_peak = float(pre[py, px])
    if reference_px is not None:
        far = max(abs(px * BIN_PX - reference_px[0]), abs(py * BIN_PX - reference_px[1])) > REFERENCE_DISTANCE_PX
        if far:
            # Nothing at the usual place at the start: look for the tail blob where the particle should be.
            py, px = reference_px[1] // BIN_PX, reference_px[0] // BIN_PX
            pre_peak = float(pre[py, px])
    y0, x0 = max(0, py - SEARCH_CELLS), max(0, px - SEARCH_CELLS)
    window = tail[y0:py + SEARCH_CELLS + 1, x0:px + SEARCH_CELLS + 1]
    ty, tx = np.unravel_index(int(np.argmax(window)), window.shape)
    tail_peak = float(window[ty, tx])
    ratio = min(tail_peak / pre_peak, 99.0) if pre_peak > 0 else (99.0 if tail_peak > 0 else 0.0)
    result.update({
        "particle_px": [int(px * BIN_PX + BIN_PX // 2), int(py * BIN_PX + BIN_PX // 2)],
        "pre_peak_per_s": round(pre_peak, 1),
        "tail_peak_per_s": round(tail_peak, 1),
        "ratio": round(ratio, 3),
        "tail_events_per_s": round(float(tail.sum() / 9.0), 1),
        "verdict": "present" if ratio >= RATIO_PRESENT else "lost",
    })
    return result


def check_run(run_dir: Path, reference: dict[str, Any] | None = None) -> dict[str, Any]:
    """Both cameras; ``reference`` is the record of an earlier run with the particle present (same session)."""
    run_dir = Path(run_dir)
    cameras: dict[str, Any] = {}
    for side in ("left", "right"):
        ref_px = None
        if reference and reference.get("cameras", {}).get(side, {}).get("particle_px"):
            ref_px = tuple(reference["cameras"][side]["particle_px"])
        try:
            cameras[side] = check_camera(run_dir, side, ref_px)
        except Exception as exc:  # noqa: BLE001 - a check must never stop the recording
            cameras[side] = {"verdict": "uncertain", "reason": repr(exc)[:200]}
    verdicts = {c.get("verdict") for c in cameras.values()}
    if verdicts == {"present"}:
        verdict = "present"
    elif verdicts == {"lost"}:
        verdict = "lost"
    else:
        verdict = "uncertain"
    if reference and verdict != "uncertain":
        weak = []
        for side, cam in cameras.items():
            ref_peak = reference.get("cameras", {}).get(side, {}).get("pre_peak_per_s")
            if ref_peak and cam.get("pre_peak_per_s") is not None:
                weak.append(cam["pre_peak_per_s"] < ABSENT_AT_START_FRACTION * float(ref_peak))
        if weak and all(weak):
            verdict = "absent_at_start"
    return {"verdict": verdict, "cameras": cameras, "ratio_present": RATIO_PRESENT}


def describe(record: dict[str, Any]) -> str:
    parts = []
    for side, cam in record.get("cameras", {}).items():
        if "ratio" in cam:
            parts.append(f"{side} {cam['ratio']:.2f}")
        else:
            parts.append(f"{side} {cam.get('reason', '-')}")
    return f"{record.get('verdict')} (tail/pre {', '.join(parts)})"


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Check run folders for the particle at the end of the run.")
    parser.add_argument("runs", nargs="+", type=Path)
    args = parser.parse_args(argv)
    reference = None
    folder = None
    for run in args.runs:
        if run.parent != folder:          # a reference holds only within one recording folder
            folder, reference = run.parent, None
        record = check_run(run, reference)
        if reference is None and record["verdict"] == "present":
            reference = record
        print(f"{run.name}: {describe(record)}")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
