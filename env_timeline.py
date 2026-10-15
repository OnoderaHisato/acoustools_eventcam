"""One-minute table of the environment for a recording session (2026-10-16).

The loggers write fine logs during a session: Tempo Disc (room temperature, humidity, air pressure, dew point;
env_log_*.csv, about every 10 s), DS18B20 on the PAT boards (pat_temp_log_*.csv, every 1 s per sensor) and the
power supply (psu_log_*.csv, every 1 s). This module folds the three into one row per minute,
``env_timeline_<session stamp>.csv`` next to the session JSON, with the runs that were recorded in each minute,
so the analysis can line the environment up with the run table without reading three logs.

The auto recorder writes it at the end of every session (``env_timeline_csv`` in the session JSON). For an
older session, or to redo it::

    python env_timeline.py --session stereo_acoustools_3d_records_V15c/auto_recording_session_<stamp>.json

Columns: minute_start (local ISO), minutes_after_sound_on, room_T_C, RH_percent, pressure_hPa, dew_point_C
(means over the minute), top_pat_C / bottom_pat_C (mean of the sensors of each board), supply_V, supply_A,
supply_A_max, the number of readings behind each (env_n, pat_n, psu_n; 0 = no reading in that minute) and runs
(``number:label`` of every run that overlaps the minute).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

COLUMNS = (
    "minute_start", "minutes_after_sound_on",
    "room_T_C", "RH_percent", "pressure_hPa", "dew_point_C", "env_n",
    "top_pat_C", "bottom_pat_C", "pat_n",
    "supply_V", "supply_A", "supply_A_max", "psu_n",
    "runs",
)


def _mean(values: list[float]) -> float | None:
    return None if not values else sum(values) / len(values)


def _rounded(value: float | None, digits: int) -> Any:
    return "" if value is None or not math.isfinite(value) else round(value, digits)


def _load(loader, path: Path | None) -> list[dict[str, Any]]:
    if path is None or not Path(path).is_file():
        return []
    try:
        return loader(Path(path))
    except Exception as exc:  # noqa: BLE001 - one unreadable log must not lose the others
        print(f"[TIMELINE][WARN] could not read {path}: {exc}", flush=True)
        return []


def build_timeline(
    out_csv: Path,
    *,
    start_unix: float,
    end_unix: float,
    env_log: Path | None = None,
    pat_temp_log: Path | None = None,
    psu_log: Path | None = None,
    sound_on_unix: float | None = None,
    runs: list[dict[str, Any]] | None = None,
    bin_sec: float = 60.0,
) -> dict[str, Any]:
    """Write the per-minute table; returns a short summary (rows, minutes with each source)."""
    from env_run_link import load_env_rows
    from pat_temp_run_link import load_pat_rows
    from psu_run_link import load_psu_rows

    if not end_unix > start_unix:
        raise ValueError("end must be after start")
    first = math.floor(start_unix / bin_sec) * bin_sec
    count = int(math.ceil((end_unix - first) / bin_sec))
    bins: list[dict[str, list]] = [
        {"T": [], "RH": [], "P": [], "Td": [], "top": [], "bottom": [], "V": [], "A": []} for _ in range(count)
    ]

    def index(t: float) -> int | None:
        k = int((t - first) // bin_sec)
        return k if 0 <= k < count else None

    for row in _load(load_env_rows, env_log):
        k = index(row["t"])
        if k is None or row.get("error"):
            continue
        for key, name in (("T", "temperature_C"), ("RH", "humidity_percent"), ("P", "pressure_hPa"), ("Td", "dew_point_C")):
            if row.get(name) is not None:
                bins[k][key].append(float(row[name]))
    for row in _load(load_pat_rows, pat_temp_log):
        k = index(row["t"])
        if k is None or row.get("error") or row.get("temp_C") is None:
            continue
        if row.get("group") in ("top", "bottom"):
            bins[k][row["group"]].append(float(row["temp_C"]))
    for row in _load(load_psu_rows, psu_log):
        k = index(row["t"])
        if k is None or row.get("V") is None or row.get("I") is None:
            continue
        bins[k]["V"].append(float(row["V"]))
        bins[k]["A"].append(float(row["I"]))

    run_marks: list[list[str]] = [[] for _ in range(count)]
    for run in runs or []:
        s, e = run.get("start_unix"), run.get("end_unix")
        if s is None or e is None:
            continue
        a, b = index(max(float(s), first)), index(min(float(e), first + count * bin_sec - 1e-6))
        if a is None or b is None:
            continue
        for k in range(a, b + 1):
            run_marks[k].append(f"{run.get('number')}:{run.get('label')}")

    out_csv = Path(out_csv)
    tmp = out_csv.with_suffix(".csv.tmp")
    seen = {"env": 0, "pat": 0, "psu": 0}
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS))
        writer.writeheader()
        for k, b in enumerate(bins):
            t0 = first + k * bin_sec
            env_n = max(len(b["T"]), len(b["RH"]))
            pat_n = len(b["top"]) + len(b["bottom"])
            psu_n = len(b["A"])
            seen["env"] += env_n > 0
            seen["pat"] += pat_n > 0
            seen["psu"] += psu_n > 0
            writer.writerow({
                "minute_start": dt.datetime.fromtimestamp(t0).isoformat(timespec="seconds"),
                "minutes_after_sound_on": "" if sound_on_unix is None else round((t0 - sound_on_unix) / 60.0, 2),
                "room_T_C": _rounded(_mean(b["T"]), 2),
                "RH_percent": _rounded(_mean(b["RH"]), 1),
                "pressure_hPa": _rounded(_mean(b["P"]), 2),
                "dew_point_C": _rounded(_mean(b["Td"]), 2),
                "env_n": env_n,
                "top_pat_C": _rounded(_mean(b["top"]), 3),
                "bottom_pat_C": _rounded(_mean(b["bottom"]), 3),
                "pat_n": pat_n,
                "supply_V": _rounded(_mean(b["V"]), 3),
                "supply_A": _rounded(_mean(b["A"]), 4),
                "supply_A_max": _rounded(max(b["A"]) if b["A"] else None, 4),
                "psu_n": psu_n,
                "runs": ";".join(run_marks[k]),
            })
    os.replace(tmp, out_csv)
    return {"csv": str(out_csv), "minutes": count, "minutes_with_env": seen["env"],
            "minutes_with_pat_temp": seen["pat"], "minutes_with_psu": seen["psu"]}


def runs_from_session(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Start and end of each recorded run from the auto session JSON."""
    sound_on_ns = session.get("active_output_started_wall_ns")
    out = []
    for number, run in enumerate(session.get("runs", []), start=1):
        if int(run.get("exit_code", 1)) != 0:
            continue
        end = run.get("finished_at")
        since = run.get("seconds_since_sound_on_at_run_start")
        try:
            end_unix = dt.datetime.fromisoformat(end).timestamp() if end else None
        except ValueError:
            end_unix = None
        start_unix = None if since is None or sound_on_ns is None else sound_on_ns / 1e9 + float(since)
        if start_unix is None and end_unix is not None:
            start_unix = end_unix - 60.0
        out.append({"number": number, "label": run.get("label", ""), "start_unix": start_unix, "end_unix": end_unix})
    return out


def timeline_for_session(session_path: Path, session: dict[str, Any] | None = None,
                         end_unix: float | None = None) -> dict[str, Any]:
    session_path = Path(session_path)
    if session is None:
        session = json.loads(session_path.read_text(encoding="utf-8"))
    stamp = session_path.stem.replace("auto_recording_session_", "")
    sound_on_ns = session.get("active_output_started_wall_ns")
    start = dt.datetime.fromisoformat(session["started_at"]).timestamp()
    if end_unix is None:
        end_text = session.get("hardware_session_closed_at") or session.get("finished_at")
        end_unix = dt.datetime.fromisoformat(end_text).timestamp() if end_text else start + 60.0

    def path_of(key: str) -> Path | None:
        value = str(session.get(key) or "")
        return Path(value) if value else None

    return build_timeline(
        session_path.with_name(f"env_timeline_{stamp}.csv"),
        start_unix=start, end_unix=max(end_unix, start + 1.0),
        env_log=path_of("env_log"), pat_temp_log=path_of("pat_temp_log"), psu_log=path_of("psu_log"),
        sound_on_unix=None if sound_on_ns is None else sound_on_ns / 1e9,
        runs=runs_from_session(session),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", type=Path, required=True, help="auto_recording_session_<stamp>.json")
    args = parser.parse_args(argv)
    summary = timeline_for_session(args.session)
    print(f"[TIMELINE] {summary['csv']}: {summary['minutes']} min (env {summary['minutes_with_env']}, "
          f"PAT temp {summary['minutes_with_pat_temp']}, supply {summary['minutes_with_psu']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
