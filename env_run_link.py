"""Link the room-environment log (tempodisc_logger.py: temperature, humidity, pressure) to recorded runs.

Used by acoustools_stereo_eventcam_3d_recording_auto.py (--env-log) in the same way as psu_run_link.py:
when a run finishes, the rows that cover it are copied into <run>/environment_log.csv and a summary
goes to pipeline_manifest.json ("environment") and to the session JSON. The temperature notes
(thermal_prompt.py) also take the latest reading. Only the CSV is read; nothing here touches Bluetooth,
and nothing here may stop a recording.
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from typing import Any

QUANTITIES = ("temperature_C", "humidity_percent", "pressure_hPa", "dew_point_C")
# A run is "stale" when the sensor was last heard this long before the run ended.
STALE_S = 300.0


def load_env_rows(path: Path) -> list[dict[str, Any]]:
    """Parse a tempodisc_logger.py CSV. Rows with an error and a half-written last line are kept out."""
    rows: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for raw in csv.DictReader(handle):
            try:
                t = float(raw["time_unix_s"])
                received = float(raw.get("received_unix_s") or t)
            except (TypeError, ValueError, KeyError):
                continue
            if raw.get("error") or not raw.get("temperature_C"):
                rows.append({"t": t, "received": None, "error": raw.get("error") or "incomplete"})
                continue
            row: dict[str, Any] = {"t": t, "received": received, "error": ""}
            try:
                for name in QUANTITIES:
                    row[name] = float(raw[name]) if raw.get(name) else None
                row["battery_percent"] = int(raw["battery_percent"]) if raw.get("battery_percent") else None
            except ValueError:
                continue
            rows.append(row)
    return rows


def _valid(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if not row.get("error")]


def latest_reading(path: Path | None, now_unix: float, max_age_s: float = STALE_S) -> dict[str, Any] | None:
    """The newest reading heard within ``max_age_s`` of ``now_unix`` (None if none, or the log is unreadable)."""
    if path is None:
        return None
    try:
        rows = [row for row in _valid(load_env_rows(path)) if row["received"] <= now_unix + 1.0]
    except Exception:  # noqa: BLE001 - an unreadable log must not stop the recording
        return None
    if not rows:
        return None
    row = max(rows, key=lambda r: r["received"])
    age = now_unix - row["received"]
    if age > max_age_s:
        return None
    return {**{name: row.get(name) for name in QUANTITIES}, "age_s": round(age, 1)}


def summarize(rows: list[dict[str, Any]], end_unix: float | None = None) -> dict[str, Any]:
    valid = _valid(rows)
    summary: dict[str, Any] = {"samples": len(rows), "valid_samples": len(valid),
                               "errors": len(rows) - len(valid)}
    for name in QUANTITIES:
        values = [row[name] for row in valid if row.get(name) is not None]
        if values:
            summary[f"{name}_mean"] = round(sum(values) / len(values), 3)
            summary[f"{name}_min"] = round(min(values), 3)
            summary[f"{name}_max"] = round(max(values), 3)
    batteries = [row["battery_percent"] for row in valid if row.get("battery_percent") is not None]
    if batteries:
        summary["battery_percent"] = batteries[-1]
    if valid and end_unix is not None:
        last_heard = max(row["received"] for row in valid)
        summary["last_heard_before_end_s"] = round(end_unix - last_heard, 1)
        summary["stale"] = end_unix - last_heard > STALE_S
    else:
        summary["stale"] = True
    return summary


def attach_environment_record(
    run_dir: Path | None,
    env_log: Path,
    start_unix: float,
    end_unix: float,
    margin_s: float = 60.0,
) -> dict[str, Any]:
    """Copy the environment rows around a run into the run folder and return their summary. Never raises.

    The margin is wider than for the supply (60 s): the sensor updates its advertisement only every
    few tens of seconds, so a short run may otherwise contain no reading at all. With run_dir=None
    (a failed run whose folder was removed) only the summary is returned.
    """
    try:
        rows = [row for row in load_env_rows(env_log) if start_unix - margin_s <= row["t"] <= end_unix + margin_s]
        summary = summarize(rows, end_unix)
        summary["source_log"] = str(Path(env_log).resolve())
        summary["window"] = {
            "start": dt.datetime.fromtimestamp(start_unix).isoformat(timespec="milliseconds"),
            "end": dt.datetime.fromtimestamp(end_unix).isoformat(timespec="milliseconds"),
            "margin_s": margin_s,
        }
        if run_dir is None or not Path(run_dir).is_dir():
            return summary
        with Path(env_log).open(encoding="utf-8", newline="") as source:
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
        target = Path(run_dir) / "environment_log.csv"
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(kept)
        summary["run_file"] = target.name
        return summary
    except Exception as exc:  # noqa: BLE001 - the environment record must never break a recording
        return {"error": repr(exc)[:300], "source_log": str(env_log)}
