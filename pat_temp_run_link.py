"""Link the PAT board temperature log (pat_temp_logger.py: DS18B20 via Arduino) to recorded runs.

Used by acoustools_stereo_eventcam_3d_recording_auto.py (--pat-temp-log) in the same way as
psu_run_link.py and env_run_link.py: when a run finishes, the rows that cover it are copied into
<run>/pat_temperature_log.csv and a summary goes to pipeline_manifest.json ("pat_temperature") and to the
session JSON. The temperature notes (thermal_prompt.py) also take the latest reading of each group
(top / bottom PAT). Only the CSV is read; nothing here opens a serial port, and nothing here may stop a
recording.
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from typing import Any

# The sensors are read once a second; a sensor not heard for this long is "stale".
STALE_S = 30.0
GROUPS = ("top", "bottom")
# Port events of pat_temp_logger.py that mean the Arduino (illumination PWM, start LED) may have stopped.
INTERRUPTING_EVENTS = ("port_lost", "port_opened_reset")


def load_pat_rows(path: Path) -> list[dict[str, Any]]:
    """Parse a pat_temp_logger.py CSV. Error rows are kept (``error``); a half-written last line is dropped."""
    rows: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for raw in csv.DictReader(handle):
            try:
                t = float(raw["time_unix_s"])
            except (TypeError, ValueError, KeyError):
                continue
            event = raw.get("event") or ""
            if raw.get("error"):
                rows.append({"t": t, "error": raw["error"], "event": event})
                continue
            if not raw.get("rom"):
                if event:
                    rows.append({"t": t, "error": "", "event": event})
                continue
            try:
                cycle = int(raw["cycle"]) if raw.get("cycle") else None
            except ValueError:
                cycle = None
            try:
                temperature = float(raw["temp_C"]) if raw.get("temp_C") else None
            except ValueError:
                continue
            rows.append({
                "t": t, "error": "", "event": "", "cycle": cycle,
                "rom": raw["rom"], "name": raw.get("name") or raw["rom"],
                "group": raw.get("group") or "", "status": raw.get("status") or "",
                "temp_C": temperature if raw.get("status") == "ok" else None,
            })
    return rows


def _ok(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if not row.get("error") and row.get("temp_C") is not None]


def _mean(values: list[float]) -> float | None:
    return None if not values else sum(values) / len(values)


def latest_reading(path: Path | None, now_unix: float, max_age_s: float = STALE_S) -> dict[str, Any] | None:
    """The newest valid reading of each sensor heard within ``max_age_s`` and the group means.

    ``{"top_C": ..., "bottom_C": ..., "values": {name: temp}, "age_s": oldest age used}``; None if none.
    """
    if path is None:
        return None
    try:
        rows = [row for row in _ok(load_pat_rows(path)) if row["t"] <= now_unix]
    except Exception:  # noqa: BLE001 - an unreadable log must not stop the recording
        return None
    newest: dict[str, dict[str, Any]] = {}
    for row in rows:
        if now_unix - row["t"] > max_age_s:
            continue
        if row["rom"] not in newest or row["t"] >= newest[row["rom"]]["t"]:
            newest[row["rom"]] = row
    if not newest:
        return None
    result: dict[str, Any] = {}
    for group in GROUPS:
        mean = _mean([row["temp_C"] for row in newest.values() if row["group"] == group])
        result[f"{group}_C"] = None if mean is None else round(mean, 3)
    result["values"] = {row["name"]: round(row["temp_C"], 3) for row in sorted(newest.values(), key=lambda r: r["name"])}
    result["age_s"] = round(max(now_unix - row["t"] for row in newest.values()), 1)
    return result


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t).isoformat(timespec="milliseconds")


def arduino_restarts(rows: list[dict[str, Any]]) -> list[float]:
    """Times at which the sketch's cycle counter went back: the Arduino restarted (reset, USB, power).

    The counter grows by one per reading from power-on and is not reset by a bus rescan.
    """
    previous: dict[str, int] = {}
    restarts: list[float] = []
    for row in sorted((r for r in rows if r.get("rom") and r.get("cycle") is not None), key=lambda r: r["t"]):
        last = previous.get(row["rom"])
        if last is not None and row["cycle"] < last and not any(abs(row["t"] - t) < 1.0 for t in restarts):
            restarts.append(row["t"])
        previous[row["rom"]] = row["cycle"]
    return restarts


def summarize(rows: list[dict[str, Any]], end_unix: float | None = None) -> dict[str, Any]:
    ok = _ok(rows)
    sensor_rows = [row for row in rows if row.get("rom")]
    error_rows = [row for row in rows if row.get("error")]
    summary: dict[str, Any] = {
        "samples": len(sensor_rows),
        "valid_samples": len(ok),
        "error_rows": len(error_rows),
        "bad_status": sorted({row["status"] for row in sensor_rows if row["status"] != "ok"}),
    }
    # Interruptions: the illumination PWM and the start LED run on the same Arduino, so a restart (or a
    # port lost / reopened, or no readings for a while) inside the run window may have stopped them.
    restarts = arduino_restarts(rows)
    port_events = [{"time": _iso(row["t"]), "event": row["event"]} for row in rows if row.get("event")]
    gaps = [{"time": _iso(row["t"]), "error": row["error"]} for row in error_rows
            if not row.get("event") and str(row["error"]).startswith("no reading")]
    summary["arduino_restarts"] = [_iso(t) for t in restarts]
    summary["port_events"] = port_events
    summary["reading_gaps"] = gaps
    # A plain "port_opened" (the logger started, the board kept running) does not stop the LEDs.
    harmful = [e for e in port_events if e["event"] in INTERRUPTING_EVENTS]
    summary["interrupted"] = bool(restarts or harmful or gaps)
    sensors: dict[str, dict[str, Any]] = {}
    for rom in sorted({row["rom"] for row in sensor_rows}):
        mine = [row for row in ok if row["rom"] == rom]
        first = next(row for row in sensor_rows if row["rom"] == rom)
        item: dict[str, Any] = {"name": first["name"], "group": first["group"], "samples": len(mine)}
        if mine:
            values = [row["temp_C"] for row in mine]
            item.update({
                "mean_C": round(sum(values) / len(values), 3),
                "min_C": round(min(values), 3),
                "max_C": round(max(values), 3),
                "first_C": round(mine[0]["temp_C"], 3),
                "last_C": round(mine[-1]["temp_C"], 3),
            })
        sensors[rom] = item
    summary["sensors"] = sensors
    for group in GROUPS:
        means = [item["mean_C"] for item in sensors.values() if item["group"] == group and "mean_C" in item]
        if means:
            summary[f"{group}_mean_C"] = round(sum(means) / len(means), 3)
    if ok and end_unix is not None:
        last = max(row["t"] for row in ok)
        summary["last_heard_before_end_s"] = round(end_unix - last, 1)
        summary["stale"] = end_unix - last > STALE_S
    else:
        summary["stale"] = True
    return summary


def attach_pat_temperature_record(
    run_dir: Path | None,
    pat_temp_log: Path,
    start_unix: float,
    end_unix: float,
    margin_s: float = 5.0,
) -> dict[str, Any]:
    """Copy the temperature rows around a run into the run folder and return their summary. Never raises.

    With run_dir=None (a failed run whose folder was removed) only the summary is returned.
    """
    try:
        rows = [row for row in load_pat_rows(pat_temp_log)
                if start_unix - margin_s <= row["t"] <= end_unix + margin_s]
        summary = summarize(rows, end_unix)
        summary["source_log"] = str(Path(pat_temp_log).resolve())
        summary["window"] = {
            "start": dt.datetime.fromtimestamp(start_unix).isoformat(timespec="milliseconds"),
            "end": dt.datetime.fromtimestamp(end_unix).isoformat(timespec="milliseconds"),
            "margin_s": margin_s,
        }
        if run_dir is None or not Path(run_dir).is_dir():
            return summary
        with Path(pat_temp_log).open(encoding="utf-8", newline="") as source:
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
        target = Path(run_dir) / "pat_temperature_log.csv"
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(kept)
        summary["run_file"] = target.name
        return summary
    except Exception as exc:  # noqa: BLE001 - the temperature record must never break a recording
        return {"error": repr(exc)[:300], "source_log": str(pat_temp_log)}
