"""Record a levitated particle (number, diameters, calipers, source, times) for a recording session.

The paper plan (PAPER_SESSION_PLAN_20261014.md section 4) wants, per particle: its number (0, 1, 2, 3; 0' when a
dropped one was replaced), when it was placed and removed, three caliper diameters (0.1 mm) with their mean and
spread, the calipers (model, resolution, who measured), where the particle came from and how it looked, how it
was stored between sessions, and the runs it dropped in.

The auto recording process rewrites its session JSON after every run, so this tool never edits that file while
the session is running. It writes a sidecar next to it, ``particles_<session stamp>.json`` (one list, appended
to), and the session's ``particles`` (typed during the session with ``--particle-prompts``) get the sidecar's fields folded
in when the session has ended (merge happens
here when the status is no longer ``running``, and ``--merge`` repeats it later). Which run used which particle
follows from ``placed_at`` / ``removed_at`` and the ``particle_changes`` times already in the session JSON.

Examples::

    # before the session (the session JSON does not exist yet): write the sidecar for the next session stamp
    python particle_record.py --output-dir stereo_acoustools_3d_records_V15c --particle 0 \
        --diameters 1.5,1.6,1.5 --calipers "Mitutoyo CD-15APX, 0.01 mm" --measured-by Onodera \
        --source "bag A, lot 2026-09" --appearance "clean" --placed-at now

    # after the session: add the removal time and merge into the session JSON
    python particle_record.py --session stereo_acoustools_3d_records_V15c/auto_recording_session_20261016_090000.json \
        --particle 0 --removed-at now --storage "petri dish, lid on" --merge
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

SIDECAR_PREFIX = "particles_"
PENDING_STAMP = "pending"


def parse_diameters(text: str) -> dict[str, Any]:
    values = [float(v) for v in str(text).replace(";", ",").split(",") if v.strip()]
    if len(values) != 3:
        raise ValueError("--diameters takes exactly three values, e.g. 1.5,1.6,1.5")
    for v in values:
        if not 0.2 <= v <= 10.0:
            raise ValueError(f"diameter {v} mm is outside 0.2..10 mm")
    return {
        "values_mm": [round(v, 2) for v in values],
        "mean_mm": round(sum(values) / 3.0, 3),
        "spread_mm": round(max(values) - min(values), 3),
    }


def parse_time(text: str | None) -> str | None:
    if text is None or not str(text).strip():
        return None
    if str(text).strip().lower() == "now":
        return dt.datetime.now().isoformat(timespec="seconds")
    try:
        return dt.datetime.fromisoformat(str(text).strip()).isoformat(timespec="seconds")
    except ValueError as exc:
        raise ValueError(f"time must be ISO (2026-10-16T09:30:00) or 'now': {text}") from exc


def session_stamp(session_path: Path) -> str:
    name = session_path.stem
    prefix = "auto_recording_session_"
    return name[len(prefix):] if name.startswith(prefix) else name


def atomic_write(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_sidecar(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must hold a JSON list")
    return data


def upsert(records: list[dict[str, Any]], record: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per particle number: new fields are merged into an existing entry, others added."""
    for existing in records:
        if existing.get("particle") == record["particle"]:
            for key, value in record.items():
                if value is None or value == "" or value == []:
                    continue
                if key == "dropped_runs":
                    existing.setdefault("dropped_runs", [])
                    existing["dropped_runs"] = list(dict.fromkeys(existing["dropped_runs"] + list(value)))
                elif key == "diameters":
                    existing.setdefault("diameters", []).append(value)
                else:
                    existing[key] = value
            existing["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
            return records
    record = {k: v for k, v in record.items() if v not in (None, "", [])}
    if "diameters" in record:
        record["diameters"] = [record["diameters"]]
    record["recorded_at"] = dt.datetime.now().isoformat(timespec="seconds")
    records.append(record)
    return records


def merge_records(existing: list[dict[str, Any]], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold sidecar records into the session's particles (those typed during the session stay).

    Same particle number -> the last entry with it gets the new fields; diameters and dropped runs are added
    without duplicates. A number not in the session is appended.
    """
    for record in records:
        target = next((e for e in reversed(existing) if e.get("particle") == record.get("particle")), None)
        if target is None:
            existing.append(json.loads(json.dumps(record)))
            continue
        for key, value in record.items():
            if value is None or value == "" or value == []:
                continue
            if key == "diameters":
                have = target.setdefault("diameters", [])
                for d in value:
                    if d not in have:
                        have.append(d)
            elif key == "dropped_runs":
                target["dropped_runs"] = list(dict.fromkeys(list(target.get("dropped_runs", [])) + list(value)))
            elif key in ("recorded_at",):
                continue
            else:
                target[key] = value
    return existing


def merge_into_session(session_path: Path, records: list[dict[str, Any]], force: bool = False) -> bool:
    """Fold the sidecar's list into the session JSON unless the session is still running."""
    session = json.loads(session_path.read_text(encoding="utf-8"))
    if session.get("status") == "running" and not force:
        print(f"[PARTICLE] {session_path.name} is still running; the sidecar is kept, merge later with --merge.")
        return False
    session["particles"] = merge_records(list(session.get("particles") or []), records)
    atomic_write(session_path, session)
    print(f"[PARTICLE] {len(records)} particle record(s) merged into {session_path.name}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--session", type=Path, help="auto_recording_session_<stamp>.json of the session")
    where.add_argument("--output-dir", type=Path,
                       help="recording folder; the record goes to particles_pending.json there until a session "
                            "JSON exists (--session then adopts it)")
    parser.add_argument("--particle", required=True, help="particle number: 0, 1, 2, 3, or 0' for a replacement")
    parser.add_argument("--diameters", default="", help="three caliper diameters in mm, e.g. 1.5,1.6,1.5")
    parser.add_argument("--measured-when", default="", choices=("", "before", "after"),
                        help="whether the diameters were measured before or after use")
    parser.add_argument("--calipers", default="", help="calipers model and resolution, e.g. 'Mitutoyo CD-15APX, 0.01 mm'")
    parser.add_argument("--measured-by", default="")
    parser.add_argument("--source", default="", help="bag / lot / purchase date")
    parser.add_argument("--appearance", default="", help="chips, deposits, static cling, ...")
    parser.add_argument("--photo", default="", help="path of a photo next to a ruler, if taken")
    parser.add_argument("--placed-at", default="", help="ISO time or 'now'")
    parser.add_argument("--removed-at", default="", help="ISO time or 'now'")
    parser.add_argument("--storage", default="", help="how it was kept between sessions")
    parser.add_argument("--dropped-run", action="append", default=[], help="run name (or number) where it fell; repeatable")
    parser.add_argument("--note", default="")
    parser.add_argument("--merge", action="store_true", help="also merge the sidecar into the session JSON (when not running)")
    parser.add_argument("--force-merge", action="store_true", help="merge even while the session is running (not recommended)")
    args = parser.parse_args(argv)

    try:
        record: dict[str, Any] = {
            "particle": str(args.particle).strip(),
            "diameters": None if not args.diameters else {
                **parse_diameters(args.diameters),
                "measured_when": args.measured_when or None,
                "measured_at": dt.datetime.now().isoformat(timespec="seconds"),
            },
            "calipers": args.calipers,
            "measured_by": args.measured_by,
            "source": args.source,
            "appearance": args.appearance,
            "photo": args.photo,
            "placed_at": parse_time(args.placed_at),
            "removed_at": parse_time(args.removed_at),
            "storage": args.storage,
            "dropped_runs": list(args.dropped_run),
            "note": args.note,
        }
        if record["diameters"] is not None:
            record["diameters"] = {k: v for k, v in record["diameters"].items() if v is not None}
    except ValueError as exc:
        print(f"[PARTICLE][ERROR] {exc}")
        return 2

    if args.session is not None:
        session_path = Path(args.session)
        if not session_path.is_file():
            print(f"[PARTICLE][ERROR] session JSON not found: {session_path}")
            return 2
        folder = session_path.parent
        sidecar = folder / f"{SIDECAR_PREFIX}{session_stamp(session_path)}.json"
        pending = folder / f"{SIDECAR_PREFIX}{PENDING_STAMP}.json"
        records = load_sidecar(sidecar)
        if not records and pending.is_file():
            records = load_sidecar(pending)
            pending.rename(pending.with_suffix(".adopted.json"))
            print(f"[PARTICLE] adopted {pending.name} for session {session_stamp(session_path)}")
    else:
        folder = Path(args.output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        session_path = None
        sidecar = folder / f"{SIDECAR_PREFIX}{PENDING_STAMP}.json"
        records = load_sidecar(sidecar)

    records = upsert(records, record)
    atomic_write(sidecar, records)
    print(f"[PARTICLE] particle {record['particle']} recorded in {sidecar}")
    if session_path is not None and (args.merge or args.force_merge or args.removed_at):
        merge_into_session(session_path, records, force=args.force_merge)
    return 0


if __name__ == "__main__":
    sys.exit(main())
