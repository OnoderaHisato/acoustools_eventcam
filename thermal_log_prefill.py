#!/usr/bin/env python3
"""Pre-fill the thermal log (thermal_log_template.csv) from an auto recording session.

One row per recorded run, with the columns the recorder knows already filled in:
record_start (the run directory time stamp), run_name, sound_on_since (PAT output start
of the session), supply_V (--supply-voltage-v) and a note with the minutes since sound
on. Current, temperatures, humidity and particle change stay empty for the operator.

When a supply log from pwr01_logger.py is available (psu_log_*.csv next to the session JSON,
or --psu-log), supply_V and supply_A are the mean measured values over each run, from the
run directory time stamp to the moment the run finished, and the note gives the current
right after sound on. last_trip_at is filled when the log shows the output switching off.

  python thermal_log_prefill.py stereo_acoustools_3d_records_thermal\\auto_recording_session_<time>.json
  python thermal_log_prefill.py stereo_acoustools_3d_records_thermal      # newest session in the folder

The output is written next to the session JSON as thermal_log_<session time>.csv and is
never overwritten (use --output to choose another name).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
from pathlib import Path

COLUMNS = [
    "record_start", "run_name", "sound_on_since", "last_trip_at", "supply_V", "supply_A",
    "T_top_board_C", "T_bottom_board_C", "T_air_gap_C", "T_room_C", "RH_percent",
    "particle_changed", "note",
]
RUN_STAMP = re.compile(r"_(\d{8}_\d{6})(?:_\d{3})?$")


def run_start(run_dir: str) -> str:
    match = RUN_STAMP.search(Path(run_dir).name)
    if not match:
        return ""
    return dt.datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M:%S")


def sound_on(session: dict) -> dt.datetime | None:
    wall_ns = session.get("active_output_started_wall_ns")
    return None if wall_ns is None else dt.datetime.fromtimestamp(int(wall_ns) / 1e9)


def load_psu_log(path: Path) -> list[dict[str, float | bool | None]]:
    """Read a pwr01_logger.py CSV into (time, V, I, output_on) samples; failed readings are skipped."""
    samples = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("error") or not row.get("voltage_V"):
                continue
            samples.append({
                "t": float(row["time_unix_s"]),
                "V": float(row["voltage_V"]),
                "I": float(row["current_A"]),
                "on": None if row.get("output_on", "") == "" else row["output_on"] == "1",
            })
    return samples


def psu_window(samples: list[dict], start: float, end: float) -> tuple[float, float, int] | None:
    inside = [sample for sample in samples if start <= sample["t"] <= end]
    if not inside:
        return None
    return (
        sum(sample["V"] for sample in inside) / len(inside),
        sum(sample["I"] for sample in inside) / len(inside),
        len(inside),
    )


def output_trips(samples: list[dict]) -> list[float]:
    """Times at which the logged output state went from ON to OFF."""
    trips, previous = [], None
    for sample in samples:
        if sample["on"] is None:
            continue
        if previous is True and sample["on"] is False:
            trips.append(sample["t"])
        previous = sample["on"]
    return trips


def rows_for_session(session: dict, psu: list[dict] | None = None) -> list[dict[str, str]]:
    started = sound_on(session)
    voltage = session.get("supply_voltage_V")
    trips = output_trips(psu) if psu else []
    rows = []
    for run in session.get("runs", []):
        if int(run.get("exit_code", 1)) != 0 or not run.get("run_dir"):
            continue
        record_start = run_start(run["run_dir"])
        note = []
        if "schedule_group" in run:
            note.append(f"group {run['schedule_group']}")
        if started is not None and record_start:
            minutes = (dt.datetime.strptime(record_start, "%Y-%m-%d %H:%M:%S") - started).total_seconds() / 60.0
            note.append(f"t+{minutes:.1f} min after sound on")
        row = {
            "record_start": record_start,
            "run_name": Path(run["run_dir"]).name,
            "sound_on_since": "" if started is None else started.strftime("%Y-%m-%d %H:%M:%S"),
            "supply_V": "" if voltage is None else f"{float(voltage):g}",
        }
        if psu and record_start:
            start = dt.datetime.strptime(record_start, "%Y-%m-%d %H:%M:%S").timestamp()
            finished = run.get("finished_at")
            end = dt.datetime.fromisoformat(finished).timestamp() if finished else start + 60.0
            window = psu_window(psu, start, end)
            if window is not None:
                mean_v, mean_i, count = window
                row["supply_V"] = f"{mean_v:.3f}"
                row["supply_A"] = f"{mean_i:.3f}"
                note.append(f"PSU mean of {count} reading(s) over the run")
            earlier = [t for t in trips if t <= end]
            if earlier:
                row["last_trip_at"] = dt.datetime.fromtimestamp(earlier[-1]).strftime("%Y-%m-%d %H:%M:%S")
        row["note"] = ", ".join(note)
        rows.append(row)
    if psu and started is not None and rows:
        first = [s for s in psu if s["t"] >= started.timestamp()]
        if first:
            rows[0]["note"] = (rows[0]["note"] + ", " if rows[0]["note"] else "") + (
                f"current right after sound on {first[0]['I']:.3f} A"
            )
    return rows


def find_psu_log(folder: Path, session: dict) -> Path | None:
    """The psu_log_*.csv in the folder whose samples overlap the session."""
    started = sound_on(session)
    for candidate in sorted(folder.glob("psu_log_*.csv"), reverse=True):
        samples = load_psu_log(candidate)
        if not samples:
            continue
        if started is None or samples[0]["t"] <= started.timestamp() + 600 <= samples[-1]["t"] + 600:
            return candidate
    return None


def newest_session(folder: Path) -> Path:
    sessions = sorted(folder.glob("auto_recording_session_*.json"))
    if not sessions:
        sys.exit(f"No auto_recording_session_*.json in {folder}")
    return sessions[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("session", type=Path, help="Session JSON, or a records folder (newest session is used)")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--psu-log", type=Path, default=None,
                        help="pwr01_logger.py CSV (default: a psu_log_*.csv next to the session)")
    args = parser.parse_args(argv)

    path = newest_session(args.session) if args.session.is_dir() else args.session
    session = json.loads(path.read_text(encoding="utf-8"))
    psu_path = args.psu_log or find_psu_log(path.parent, session)
    psu = load_psu_log(psu_path) if psu_path else None
    rows = rows_for_session(session, psu)
    stamp = path.stem.replace("auto_recording_session_", "")
    output = args.output or path.with_name(f"thermal_log_{stamp}.csv")
    if output.exists():
        sys.exit(f"{output} already exists; it is not overwritten (use --output)")
    # utf-8-sig so that Excel opens it with the right encoding.
    with output.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in COLUMNS})
    print(f"{len(rows)} run(s) from {path.name} -> {output}")
    if psu:
        print(f"supply_V / supply_A from {psu_path.name} ({len(psu)} reading(s)).")
        print("Fill in temperatures, RH and particle_changed by hand.")
    else:
        print("Fill in supply_A, temperatures, RH, particle_changed (and last_trip_at) by hand.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
