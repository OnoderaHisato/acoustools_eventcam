"""Watch a running auto recording session from outside and notify when something goes wrong (2026-10-16).

The recorder can check itself (``--particle-check``, capture-failure alerts), but a session started before those
existed, or started without them, can be watched by this separate process instead. It only reads: the session
JSON (rewritten by the recorder after every run) and the run folders; it never touches the recorder, the PAT or
the cameras. For every new run it looks at

* the exit code (a failed run),
* the particle at the end of the run (particle_presence.py: ``lost`` / ``absent_at_start``),
* the PAT-start LED consistency warnings and the capture failures that were retried,
* supply events (output off, current drop), the PAT temperature Arduino restarts, a stale environment sensor,

and it notices a run that does not finish: the newest run folder still has no ``pipeline_manifest.json``
``--waiting-min`` minutes after it was created (a failed capture or a missing PAT-start LED makes the recorder ask
"同じ軌道を再計測しますか？ (Y/n)" on screen), and a stall: no new run for ``--stall-min`` minutes. Each finding is sent with session_notify.py (email, event file -> the
watcher Claude session, beep) and kept in ``live_watch_<session stamp>.json`` next to the session JSON. The
watch ends by itself when the session status is no longer ``running``.

    python live_session_watch.py                      # newest session in stereo_acoustools_3d_records_V15c
    python live_session_watch.py --session <json> --stall-min 6
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import particle_presence
import session_notify

ROOT = Path(__file__).resolve().parent
DEFAULT_RECORDS = ROOT / "stereo_acoustools_3d_records_V15c"


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def read_session(path: Path) -> dict[str, Any] | None:
    for _ in range(5):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            time.sleep(0.5)  # the recorder replaces the file atomically; retry a half-read
    return None


def findings_for_run(number: int, run: dict[str, Any], reference: dict[str, Any] | None,
                     check_particle: bool) -> tuple[list[tuple[str, str]], dict[str, Any] | None]:
    """[(event, summary)] for one finished run, and its particle check record (if made)."""
    label = run.get("label", "?")
    out: list[tuple[str, str]] = []
    if int(run.get("exit_code", 0)) != 0:
        out.append(("run_failed", f"Run {number} ({label}) failed with exit code {run.get('exit_code')}: "
                                  f"{str(run.get('error') or '')[:120]}"))
    report = run.get("capture_report") or {}
    if report.get("led_warnings"):
        out.append(("led_warning", f"Run {number} ({label}): PAT-start LED looks inconsistent: "
                                   f"{'; '.join(map(str, report['led_warnings']))[:160]}"))
    if report.get("failures") and int(run.get("exit_code", 0)) == 0:
        out.append(("capture_retried", f"Run {number} ({label}) needed {len(report['failures'])} capture retry(s): "
                                       f"{str(report['failures'][-1])[:120]}"))
    for event in (run.get("supply") or {}).get("events", []) or []:
        if event.get("kind") in ("output_off", "current_drop"):
            out.append(("supply_event", f"Run {number} ({label}): power supply {event.get('kind')} {event}"[:200]))
    pat = run.get("pat_temperature") or {}
    if pat.get("interrupted"):
        out.append(("pat_temp_interrupted", f"Run {number} ({label}): the temperature/LED Arduino restarted or its "
                                            "port was lost; the lighting and the start LED may have stopped."))
    env = run.get("environment") or {}
    if env.get("stale") or env.get("error"):
        out.append(("environment_stale", f"Run {number} ({label}): no recent Tempo Disc reading."))
    presence = None
    run_dir = str(run.get("run_dir") or "")
    if check_particle and int(run.get("exit_code", 1)) == 0 and run_dir and Path(run_dir).is_dir():
        presence = particle_presence.check_run(Path(run_dir), reference)
        if presence["verdict"] in ("lost", "absent_at_start"):
            out.append(("particle_lost", f"Particle lost in run {number} ({label}) "
                                         f"[{particle_presence.describe(presence)}]. The running session does not stop: "
                                         f"stop it with Ctrl+C, replace the particle and resume with -StartAt."))
    return out, presence


def unfinished_run_folder(records: Path, runs: list[dict[str, Any]]) -> Path | None:
    """The newest run folder that is not among the recorded runs and has no pipeline_manifest.json yet."""
    known = {Path(str(r.get("run_dir") or "")).name for r in runs}
    folders = [p for p in records.iterdir() if p.is_dir() and "_scale" in p.name and p.name not in known]
    if not folders:
        return None
    newest = max(folders, key=lambda p: p.stat().st_ctime)
    if runs and runs[-1].get("finished_at"):
        try:
            if newest.stat().st_ctime < dt.datetime.fromisoformat(runs[-1]["finished_at"]).timestamp():
                return None            # an old leftover folder, not the run in progress
        except ValueError:
            pass
    return None if (newest / "pipeline_manifest.json").is_file() else newest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", type=Path, default=None, help="auto_recording_session_<stamp>.json (default: newest)")
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument("--interval-sec", type=float, default=20.0)
    parser.add_argument("--stall-min", type=float, default=6.0, help="notify when no run finished for this long")
    parser.add_argument("--waiting-min", type=float, default=3.0,
                        help="notify when the newest run folder is still unfinished this long after it was created")
    parser.add_argument("--no-particle-check", action="store_true")
    parser.add_argument("--notify-config", type=Path, default=session_notify.DEFAULT_CONFIG)
    parser.add_argument("--from-run", type=int, default=0,
                        help="check runs from this number on (default: only runs that finish after the watch starts)")
    args = parser.parse_args(argv)

    session_path = args.session
    if session_path is None:
        candidates = sorted(Path(args.records).glob("auto_recording_session_*.json"), key=os.path.getmtime)
        if not candidates:
            print("[WATCH] no session JSON found")
            return 2
        session_path = candidates[-1]
    session_path = Path(session_path)
    cfg = session_notify.load_config(args.notify_config)
    stamp = session_path.stem.replace("auto_recording_session_", "")
    out_path = session_path.with_name(f"live_watch_{stamp}.json")
    state: dict[str, Any] = {"session": str(session_path), "started_at": _now(), "findings": [], "particle": []}

    def save() -> None:
        tmp = out_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, out_path)

    def tell(event: str, summary: str, extra: dict[str, Any] | None = None) -> None:
        state["findings"].append({"event": event, "summary": summary, "at": _now(), **(extra or {})})
        save()
        session_notify.notify(event, summary, details={"session": str(session_path), **(extra or {})}, config=cfg)

    session = read_session(session_path) or {}
    seen = len(session.get("runs", [])) if args.from_run <= 0 else max(0, args.from_run - 1)
    reference: dict[str, Any] | None = None
    last_change = time.time()
    last_count = len(session.get("runs", []))
    stalled = False
    waiting_noted: set[str] = set()
    print(f"[WATCH] {session_path.name}: {last_count} run(s) so far; watching from run {seen + 1}. "
          f"Particle check {'off' if args.no_particle_check else 'on'}, stall {args.stall_min:g} min.", flush=True)
    save()
    while True:
        session = read_session(session_path)
        if session is None:
            time.sleep(args.interval_sec)
            continue
        runs = session.get("runs", [])
        if len(runs) != last_count:
            last_count = len(runs)
            last_change = time.time()
            if stalled:
                stalled = False
                print("[WATCH] progress again", flush=True)
        while seen < len(runs):
            seen += 1
            run = runs[seen - 1]
            try:
                found, presence = findings_for_run(seen, run, reference, not args.no_particle_check)
            except Exception as exc:  # noqa: BLE001 - keep watching
                found, presence = [("watch_error", f"Run {seen}: check failed: {exc!r}"[:200])], None
            if presence is not None:
                state["particle"].append({"run_number": seen, "label": run.get("label"),
                                          "verdict": presence["verdict"], "detail": particle_presence.describe(presence)})
                if presence["verdict"] == "present" and reference is None:
                    reference = presence
                print(f"[WATCH] run {seen} {run.get('label')}: particle {particle_presence.describe(presence)}", flush=True)
                save()
            for event, summary in found:
                print(f"[WATCH] {event}: {summary}", flush=True)
                tell(event, summary, {"run_number": seen, "label": run.get("label")})
        status = session.get("status")
        if status != "running":
            state["finished_at"] = _now()
            state["session_status"] = status
            save()
            print(f"[WATCH] session {status}; watch ends.", flush=True)
            return 0
        newest = unfinished_run_folder(session_path.parent, runs)
        if newest is not None and newest.name not in waiting_noted:
            age_min = (time.time() - newest.stat().st_ctime) / 60.0
            if age_min > args.waiting_min:
                waiting_noted.add(newest.name)
                tell("capture_waiting",
                     f"Run {len(runs) + 1} ({newest.name[:60]}) has not finished after {age_min:.0f} min: probably "
                     "the recorder asks on screen (stereo recording failed / PAT-start LED not detected, Y/n).",
                     {"run_number": len(runs) + 1, "run_folder": newest.name})
        if not stalled and time.time() - last_change > args.stall_min * 60.0:
            stalled = True
            last_label = runs[-1].get("label") if runs else "-"
            tell("session_stall", f"No run finished for {args.stall_min:g} min (last: run {len(runs)} {last_label}). "
                                  "The recorder may be waiting for an answer on screen (Y/n, Enter) or hung.",
                 {"run_number": len(runs)})
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    sys.exit(main())
