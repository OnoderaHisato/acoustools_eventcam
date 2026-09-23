#!/usr/bin/env python3
"""Link the PAT supply log (pwr01_logger.py) to recorded runs, and judge drops and steadiness.

Used in two places:
  * acoustools_stereo_eventcam_3d_recording_auto.py (--psu-log): when a run finishes, the rows
    of the supply log that cover the run are copied into <run>/supply_log.csv and a summary is
    written to pipeline_manifest.json ("supply") and to the session JSON. Nothing here talks to
    the supply; it only reads the CSV the logger process is appending to.
  * psu_log_report.py: a session-level figure and summary (current against minutes since
    sound on, drops, and whether the current has become steady).

A PAT board fuse trip leaves the supply output ON while the current collapses, so drops are
detected from the current itself, not only from the output state.
"""
from __future__ import annotations

import csv
import datetime as dt
import math
from pathlib import Path
from typing import Any

# A reading counts as a drop when the current falls below this fraction of the median of the
# preceding window while the output is still ON (a fuse trip on a PAT board looks like this).
DROP_FRACTION = 0.5
DROP_WINDOW_S = 30.0
# Gaps in the log longer than this are reported (logger stalled or the USB link failed).
GAP_S = 5.0


def load_psu_rows(path: Path) -> list[dict[str, Any]]:
    """Parse a pwr01_logger.py CSV. A half-written last line (the logger is still appending) is ignored."""
    rows: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for raw in csv.DictReader(handle):
            try:
                t = float(raw["time_unix_s"])
            except (TypeError, ValueError, KeyError):
                continue
            row: dict[str, Any] = {"t": t, "error": raw.get("error") or ""}
            if not raw.get("voltage_V") and not row["error"]:
                continue  # a line the logger is still writing (every complete row has values or an error)
            try:
                row["V"] = float(raw["voltage_V"]) if raw.get("voltage_V") else None
                row["I"] = float(raw["current_A"]) if raw.get("current_A") else None
            except ValueError:
                continue
            on = raw.get("output_on", "")
            row["on"] = None if on in ("", None) else on == "1"
            rows.append(row)
    return rows


def _valid(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("V") is not None and row.get("I") is not None]


def detect_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Output OFF transitions, current drops with the output ON, and gaps in the log."""
    events: list[dict[str, Any]] = []
    last_on = None
    previous_t = None
    valid_history: list[dict[str, Any]] = []
    in_drop = False
    for row in rows:
        if previous_t is not None and row["t"] - previous_t > GAP_S:
            events.append({"t": previous_t, "kind": "log_gap", "seconds": round(row["t"] - previous_t, 1)})
        previous_t = row["t"]
        if row.get("error"):
            events.append({"t": row["t"], "kind": "read_error", "detail": row["error"][:120]})
            continue
        if row.get("on") is not None:
            if last_on is True and row["on"] is False:
                events.append({"t": row["t"], "kind": "output_off"})
            if last_on is False and row["on"] is True:
                events.append({"t": row["t"], "kind": "output_on"})
            last_on = row["on"]
        if row.get("I") is None:
            continue
        window = [r["I"] for r in valid_history if row["t"] - r["t"] <= DROP_WINDOW_S]
        if len(window) >= 3 and last_on is not False:
            reference = sorted(window)[len(window) // 2]
            dropped = reference > 0.2 and row["I"] < DROP_FRACTION * reference
            if dropped and not in_drop:
                events.append({
                    "t": row["t"], "kind": "current_drop",
                    "from_A": round(reference, 4), "to_A": round(row["I"], 4),
                })
            in_drop = dropped
        valid_history.append(row)
        valid_history = [r for r in valid_history if row["t"] - r["t"] <= DROP_WINDOW_S]
    return events


def summarize(rows: list[dict[str, Any]], sound_on_unix: float | None = None) -> dict[str, Any]:
    valid = _valid(rows)
    summary: dict[str, Any] = {"samples": len(rows), "valid_samples": len(valid)}
    if rows:
        summary["first_time"] = dt.datetime.fromtimestamp(rows[0]["t"]).isoformat(timespec="seconds")
        summary["last_time"] = dt.datetime.fromtimestamp(rows[-1]["t"]).isoformat(timespec="seconds")
    if valid:
        voltages = [row["V"] for row in valid]
        currents = [row["I"] for row in valid]
        mean_i = sum(currents) / len(currents)
        summary.update({
            "voltage_mean_V": round(sum(voltages) / len(voltages), 4),
            "voltage_min_V": round(min(voltages), 4),
            "voltage_max_V": round(max(voltages), 4),
            "current_mean_A": round(mean_i, 4),
            "current_min_A": round(min(currents), 4),
            "current_max_A": round(max(currents), 4),
            "current_std_A": round(math.sqrt(sum((c - mean_i) ** 2 for c in currents) / len(currents)), 5),
        })
    states = [row["on"] for row in rows if row.get("on") is not None]
    if states:
        summary["output_on_all"] = all(states)
    if sound_on_unix is not None and rows:
        summary["minutes_since_sound_on_start"] = round((rows[0]["t"] - sound_on_unix) / 60.0, 2)
        summary["minutes_since_sound_on_end"] = round((rows[-1]["t"] - sound_on_unix) / 60.0, 2)
    events = detect_events(rows)
    summary["events"] = [
        {**event, "time": dt.datetime.fromtimestamp(event["t"]).isoformat(timespec="seconds")}
        for event in events
    ]
    summary["suspicious"] = any(e["kind"] in {"output_off", "current_drop", "log_gap"} for e in events)
    return summary


def attach_supply_record(
    run_dir: Path | None,
    psu_log: Path,
    start_unix: float,
    end_unix: float,
    sound_on_unix: float | None = None,
    margin_s: float = 5.0,
) -> dict[str, Any]:
    """Copy the supply rows that cover a run into the run folder and return their summary.

    Never raises: a missing or unreadable log gives {"error": ...} so a recording is not
    affected by the supply logger. With run_dir=None (a failed run whose folder was removed)
    only the summary is returned.
    """
    try:
        rows = [
            row for row in load_psu_rows(psu_log)
            if start_unix - margin_s <= row["t"] <= end_unix + margin_s
        ]
        summary = summarize(rows, sound_on_unix)
        summary["source_log"] = str(Path(psu_log).resolve())
        summary["window"] = {
            "start": dt.datetime.fromtimestamp(start_unix).isoformat(timespec="milliseconds"),
            "end": dt.datetime.fromtimestamp(end_unix).isoformat(timespec="milliseconds"),
            "margin_s": margin_s,
        }
        if run_dir is None or not Path(run_dir).is_dir():
            return summary
        target = Path(run_dir) / "supply_log.csv"
        with Path(psu_log).open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            fieldnames = reader.fieldnames or []
            kept = []
            for raw in reader:
                try:
                    t = float(raw.get("time_unix_s") or "nan")
                except ValueError:
                    continue
                if start_unix - margin_s <= t <= end_unix + margin_s:
                    kept.append(raw)
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(kept)
        summary["run_file"] = target.name
        return summary
    except Exception as exc:  # noqa: BLE001 - the supply record must never break a recording
        return {"error": repr(exc)[:300], "source_log": str(psu_log)}


def steadiness(
    rows: list[dict[str, Any]],
    sound_on_unix: float,
    window_min: float = 5.0,
    tolerance: float = 0.01,
    recent_min: float = 30.0,
) -> dict[str, Any]:
    """How steady the supply current is: drift over the last `recent_min` minutes, and when it settled.

    The current is averaged over `window_min`-minute bins (the logger samples every second).
    "Settled" is the first bin from which every later bin stays within `tolerance` of the mean of
    the last `window_min` * 2 minutes. Readings with the output OFF, read errors, and current drops
    (fuse trips) are excluded.
    """
    drops = [e["t"] for e in detect_events(rows) if e["kind"] == "current_drop"]
    valid = [
        row for row in _valid(rows)
        if row["t"] >= sound_on_unix and row.get("on") is not False
        and not any(0.0 <= row["t"] - t <= 60.0 for t in drops)
    ]
    if not valid:
        return {"bins": [], "note": "no readings after sound on"}
    bin_s = window_min * 60.0
    bins: dict[int, list[float]] = {}
    for row in valid:
        bins.setdefault(int((row["t"] - sound_on_unix) // bin_s), []).append(row["I"])
    series = [
        {"minute": round((index + 0.5) * window_min, 2), "current_A": round(sum(v) / len(v), 4), "samples": len(v)}
        for index, v in sorted(bins.items())
    ]
    result: dict[str, Any] = {"bin_minutes": window_min, "bins": series, "tolerance": tolerance}
    if len(series) >= 2:
        final = sum(b["current_A"] for b in series[-2:]) / 2.0
        settled = None
        for index in range(len(series)):
            if all(abs(b["current_A"] - final) <= tolerance * final for b in series[index:]):
                settled = series[index]["minute"] - window_min / 2.0
                break
        result["final_current_A"] = round(final, 4)
        result["settled_after_min"] = settled
        recent = [b for b in series if b["minute"] >= series[-1]["minute"] - recent_min]
        if len(recent) >= 2 and recent[0]["current_A"] > 0:
            result["drift_last_window_percent"] = round(
                100.0 * (recent[-1]["current_A"] - recent[0]["current_A"]) / recent[0]["current_A"], 2
            )
            result["drift_window_min"] = round(recent[-1]["minute"] - recent[0]["minute"], 2)
    return result
