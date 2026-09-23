#!/usr/bin/env python3
"""Figure and summary of the PAT supply over a recording session: is it steady, did it drop?

    python psu_log_report.py stereo_acoustools_3d_records_V15            # newest session + its supply log
    python psu_log_report.py --psu-log <psu_log_*.csv> --session <auto_recording_session_*.json>

Writes psu_report_<session time>.png and .json next to the session JSON (never overwrites):
  * current and voltage against minutes since sound on (PAT output start), 1 s readings and
    5-minute means, with the recorded runs shaded;
  * events: output OFF, current drops with the output still ON (a PAT board fuse trip looks like
    this), read errors and gaps in the log;
  * steadiness: the current drift over the last 30 minutes and the time after which every
    5-minute mean stays within 1 % of the final level.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import psu_run_link as link

RUN_STAMP_FORMAT = "%Y%m%d_%H%M%S"


def newest(folder: Path, pattern: str) -> Path | None:
    found = sorted(folder.glob(pattern))
    return found[-1] if found else None


def session_psu_log(session: dict, folder: Path) -> Path | None:
    recorded = session.get("psu_log")
    if recorded and Path(recorded).is_file():
        return Path(recorded)
    started = session.get("active_output_started_wall_ns")
    for candidate in sorted(folder.glob("psu_log_*.csv"), reverse=True):
        rows = link.load_psu_rows(candidate)
        if rows and (started is None or rows[0]["t"] <= started / 1e9 + 600 <= rows[-1]["t"] + 600):
            return candidate
    return None


def run_spans(session: dict) -> list[tuple[float, float, str, bool]]:
    spans = []
    for run in session.get("runs", []):
        run_dir = run.get("run_dir") or ""
        finished = run.get("finished_at")
        if not finished:
            continue
        end = dt.datetime.fromisoformat(finished).timestamp()
        start = end - 45.0
        stamp = Path(run_dir).name[-15:] if run_dir else ""
        try:
            start = dt.datetime.strptime(stamp, RUN_STAMP_FORMAT).timestamp()
        except ValueError:
            pass
        spans.append((start, end, str(run.get("label", "")), int(run.get("exit_code", 1)) == 0))
    return spans


def make_report(psu_log: Path, session_path: Path | None, output_stem: Path) -> dict:
    import matplotlib  # noqa: PLC0415

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415

    rows = link.load_psu_rows(psu_log)
    if not rows:
        raise ValueError(f"no readings in {psu_log}")
    session = json.loads(session_path.read_text(encoding="utf-8")) if session_path else {}
    sound_on_ns = session.get("active_output_started_wall_ns")
    sound_on = sound_on_ns / 1e9 if sound_on_ns else rows[0]["t"]
    events = link.detect_events(rows)
    steady = link.steadiness(rows, sound_on)
    summary = {
        "psu_log": str(psu_log.resolve()),
        "session": "" if session_path is None else str(session_path.resolve()),
        "sound_on": dt.datetime.fromtimestamp(sound_on).isoformat(timespec="seconds"),
        "supply_voltage_V_requested": session.get("supply_voltage_V"),
        "whole_log": link.summarize(rows, sound_on),
        "steadiness": steady,
        "events": [
            {**e, "time": dt.datetime.fromtimestamp(e["t"]).isoformat(timespec="seconds"),
             "minute": round((e["t"] - sound_on) / 60.0, 2)}
            for e in events
        ],
    }

    minutes = [(row["t"] - sound_on) / 60.0 for row in rows if row.get("I") is not None]
    current = [row["I"] for row in rows if row.get("I") is not None]
    voltage = [row["V"] for row in rows if row.get("V") is not None]
    figure, (axis_i, axis_v) = plt.subplots(2, 1, figsize=(11, 6.5), sharex=True,
                                            gridspec_kw={"height_ratios": [3, 1]})
    for start, end, _label, ok in run_spans(session):
        for axis in (axis_i, axis_v):
            axis.axvspan((start - sound_on) / 60.0, (end - sound_on) / 60.0,
                         color="#9ecae1" if ok else "#fcbba1", alpha=0.35, lw=0)
    axis_i.plot(minutes, current, color="#636363", lw=0.6, label="1 s readings")
    if steady.get("bins"):
        axis_i.plot([b["minute"] for b in steady["bins"]], [b["current_A"] for b in steady["bins"]],
                    "o-", color="#08519c", lw=1.6, ms=4, label=f"{steady['bin_minutes']:g}-min mean")
    if steady.get("settled_after_min") is not None:
        axis_i.axvline(steady["settled_after_min"], color="#31a354", ls="--", lw=1.2,
                       label=f"settled (within {100 * steady['tolerance']:g} %) after "
                             f"{steady['settled_after_min']:.0f} min")
    colours = {"current_drop": "#de2d26", "output_off": "black", "log_gap": "#969696", "read_error": "#fd8d3c"}
    for event in events:
        axis_i.axvline((event["t"] - sound_on) / 60.0, color=colours.get(event["kind"], "red"), lw=1.0)
    axis_i.set_ylabel("current [A]")
    drift = steady.get("drift_last_window_percent")
    title = f"PAT supply, sound on {summary['sound_on']}"
    if drift is not None:
        title += f"  |  drift over the last {steady['drift_window_min']:.0f} min: {drift:+.2f} %"
    axis_i.set_title(title, fontsize=10)
    axis_i.legend(loc="lower right", fontsize=8)
    axis_v.plot(minutes[: len(voltage)], voltage, color="#756bb1", lw=0.8)
    axis_v.set_ylabel("voltage [V]")
    axis_v.set_xlabel("minutes since sound on (blue: recorded runs, red: failed runs)")
    for axis in (axis_i, axis_v):
        axis.grid(alpha=0.3)
    figure.tight_layout()
    figure.savefig(output_stem.with_suffix(".png"), dpi=130)
    plt.close(figure)
    output_stem.with_suffix(".json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", nargs="?", type=Path, help="records folder (newest session and its supply log)")
    parser.add_argument("--psu-log", type=Path, default=None)
    parser.add_argument("--session", type=Path, default=None)
    args = parser.parse_args(argv)
    session_path = args.session or (newest(args.folder, "auto_recording_session_*.json") if args.folder else None)
    session = json.loads(session_path.read_text(encoding="utf-8")) if session_path else {}
    psu_log = args.psu_log or (session_psu_log(session, args.folder or session_path.parent) if session_path else None)
    if psu_log is None:
        print("[PSU] no supply log found for this session", file=sys.stderr)
        return 1
    base = (session_path or psu_log).parent
    stamp = session_path.stem.replace("auto_recording_session_", "") if session_path else psu_log.stem
    output_stem = base / f"psu_report_{stamp}"
    if output_stem.with_suffix(".png").exists() or output_stem.with_suffix(".json").exists():
        print(f"[PSU] {output_stem}.png/.json already exist; not overwritten", file=sys.stderr)
        return 1
    summary = make_report(psu_log, session_path, output_stem)
    steady = summary["steadiness"]
    whole = summary["whole_log"]
    print(f"[PSU] report: {output_stem}.png")
    print(f"[PSU] current {whole.get('current_min_A')}..{whole.get('current_max_A')} A, "
          f"voltage {whole.get('voltage_min_V')}..{whole.get('voltage_max_V')} V")
    if steady.get("drift_last_window_percent") is not None:
        print(f"[PSU] drift over the last {steady['drift_window_min']:.0f} min: "
              f"{steady['drift_last_window_percent']:+.2f} %; settled after: {steady.get('settled_after_min')} min")
    alarms = [e for e in summary["events"] if e["kind"] in {"output_off", "current_drop", "log_gap"}]
    for event in alarms:
        print(f"[PSU][WARN] {event['kind']} at {event['time']} (t+{event['minute']} min): {event}")
    if not alarms:
        print("[PSU] no output-off, current drop or log gap")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
