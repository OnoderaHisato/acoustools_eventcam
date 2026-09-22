#!/usr/bin/env python3
"""Pre-fill the thermal log (thermal_log_template.csv) from an auto recording session.

One row per recorded run, with the columns the recorder knows already filled in:
record_start (the run directory time stamp), run_name, sound_on_since (PAT output start
of the session), supply_V (--supply-voltage-v) and a note with the minutes since sound
on. Current, temperatures, humidity and particle change stay empty for the operator.

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


def rows_for_session(session: dict) -> list[dict[str, str]]:
    started = sound_on(session)
    voltage = session.get("supply_voltage_V")
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
        rows.append({
            "record_start": record_start,
            "run_name": Path(run["run_dir"]).name,
            "sound_on_since": "" if started is None else started.strftime("%Y-%m-%d %H:%M:%S"),
            "supply_V": "" if voltage is None else f"{float(voltage):g}",
            "note": ", ".join(note),
        })
    return rows


def newest_session(folder: Path) -> Path:
    sessions = sorted(folder.glob("auto_recording_session_*.json"))
    if not sessions:
        sys.exit(f"No auto_recording_session_*.json in {folder}")
    return sessions[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("session", type=Path, help="Session JSON, or a records folder (newest session is used)")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    path = newest_session(args.session) if args.session.is_dir() else args.session
    session = json.loads(path.read_text(encoding="utf-8"))
    rows = rows_for_session(session)
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
    print("Fill in supply_A, temperatures, RH, particle_changed (and last_trip_at) by hand.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
