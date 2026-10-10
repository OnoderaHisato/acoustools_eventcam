"""Temperature notes typed into a running auto recording session (cooling remeasurement, 2026-10-10).

The operator measures the top and bottom PAT surface temperatures, the room temperature and the
humidity by hand. Instead of filling in a sheet and guessing when, the recording asks for them:

* at the start (after the particle check) and at the end of the session,
* every N minutes after sound on (``--thermal-prompt-every`` "0:5" = every 5 min; "0:5,92:10" switches
  to every 10 min from minute 92). A scheduled session asks while it waits for the next run and gives
  up 10 s before that run is due; a session without a schedule asks between runs and gives up after
  ``--thermal-prompt-timeout-sec`` (default 180 s). Once the operator starts typing, the prompt waits
  for Enter regardless of the deadline (a scheduled run may then start a little late). The particle is
  held at the centre (PAT on) while it asks.

One line answers: ``top bottom room humidity [note]`` -- numbers, ``-`` for a value not measured, any
text after the numbers is kept as a note. An empty line or no answer records nothing; the note is then
asked again at the next chance. Every answer is appended to the session JSON (``thermal_notes``) and to
``thermal_log_<session time>.csv`` next to it, together with the latest supply voltage and current from
the PSU log.
"""

from __future__ import annotations

import csv
import datetime as dt
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

FIELDS = ("T_top_PAT_C", "T_bottom_PAT_C", "T_room_C", "RH_percent")
FIELD_LABELS = ("上PAT [°C]", "下PAT [°C]", "室温 [°C]", "湿度 [%]")
CSV_COLUMNS = (
    "recorded_at", "minutes_after_sound_on", "kind", "next_run_number", "next_label",
    *FIELDS, "supply_V", "supply_A", "note",
)
SCHEDULED_MIN_WAIT_SEC = 20.0  # a scheduled wait shorter than this is not used for a prompt
SCHEDULED_LEAD_SEC = 10.0  # stop asking this long before a scheduled run is due


def parse_interval_plan(text: str) -> list[tuple[float, float]]:
    """``"0:5,92:10"`` -> [(0.0, 5.0), (92.0, 10.0)]: from minute 0 every 5 min, from minute 92 every 10."""
    plan: list[tuple[float, float]] = []
    for item in str(text).split(","):
        item = item.strip()
        if not item:
            continue
        start_text, _, every_text = item.partition(":")
        try:
            start, every = float(start_text), float(every_text)
        except ValueError as exc:
            raise ValueError(f"thermal prompt plan item {item!r} is not START_MIN:EVERY_MIN") from exc
        if not (math.isfinite(start) and math.isfinite(every)) or start < 0 or every <= 0:
            raise ValueError(f"thermal prompt plan item {item!r} needs START_MIN >= 0 and EVERY_MIN > 0")
        plan.append((start, every))
    if not plan:
        raise ValueError("thermal prompt plan is empty")
    plan.sort()
    if plan[0][0] != 0.0:
        raise ValueError("thermal prompt plan must start at minute 0")
    return plan


def interval_min_at(plan: list[tuple[float, float]], minute: float) -> float:
    every = plan[0][1]
    for start, value in plan:
        if minute >= start:
            every = value
    return every


def latest_mark_min(plan: list[tuple[float, float]], minute: float) -> float:
    """The latest prompt mark at or before ``minute`` (marks: each START, then every EVERY until the next START)."""
    mark = 0.0
    for start, every in plan:
        if minute < start:
            break
        mark = start + every * math.floor((minute - start) / every + 1e-9)
    return mark


def parse_thermal_line(text: str | None) -> tuple[dict[str, float | None], str] | None:
    """``"41.2 39.8 24.1 45 fan loud"`` -> values and note; None for no answer.

    Up to four leading tokens are values (a number or ``-``); the first other token starts the note.
    """
    if text is None or not text.strip():
        return None
    tokens = text.split()
    values: dict[str, float | None] = {name: None for name in FIELDS}
    index = 0
    while index < len(tokens) and index < len(FIELDS):
        token = tokens[index].replace(",", ".")
        if token == "-":
            index += 1
            continue
        try:
            value = float(token)
        except ValueError:
            break
        if not math.isfinite(value):
            break
        values[FIELDS[index]] = value
        index += 1
    note = " ".join(tokens[index:])
    if all(value is None for value in values.values()) and not note:
        return None
    return values, note


def read_line_with_deadline(prompt: str, deadline_wall: float | None, clock: Any = time) -> str | None:
    """Read one line from the console; None when the deadline passes before anything is typed.

    Once a character has been typed the deadline no longer applies: the line is finished with Enter.
    """
    print(prompt, end="", flush=True)
    if deadline_wall is None:
        try:
            return input()
        except EOFError:
            return None
    if os.name == "nt":
        import msvcrt

        buffer: list[str] = []
        while True:
            if not buffer and clock.time() >= deadline_wall:
                print("\n[THERMAL] No answer in time; continuing.", flush=True)
                return None
            if not msvcrt.kbhit():
                time.sleep(0.05)
                continue
            char = msvcrt.getwche()
            if char in ("\r", "\n"):
                print(flush=True)
                return "".join(buffer)
            if char == "\x03":
                raise KeyboardInterrupt
            if char in ("\x00", "\xe0"):
                msvcrt.getwch()  # function / arrow key: ignore the second code
                continue
            if char == "\x08":
                if buffer:
                    buffer.pop()
                    sys.stdout.write(" \x08")
                    sys.stdout.flush()
                continue
            buffer.append(char)
    import select

    while True:
        remaining = deadline_wall - clock.time()
        if remaining <= 0:
            print("\n[THERMAL] No answer in time; continuing.", flush=True)
            return None
        ready, _, _ = select.select([sys.stdin], [], [], min(0.5, remaining))
        if ready:
            line = sys.stdin.readline()
            return None if line == "" else line.rstrip("\n")


class ThermalNotes:
    """Asks for temperatures at the right moments and keeps the records (session JSON and CSV)."""

    def __init__(
        self,
        csv_path: Path,
        plan: list[tuple[float, float]],
        timeout_sec: float,
        sound_on_wall_sec: float | None,
        psu_log: Path | None = None,
        clock: Any = time,
        reader: Callable[..., str | None] | None = None,
    ) -> None:
        self.csv_path = Path(csv_path)
        self.plan = plan
        self.timeout_sec = float(timeout_sec)
        self.sound_on_wall_sec = sound_on_wall_sec
        self.psu_log = psu_log
        self.clock = clock
        self.reader = reader
        self.records: list[dict[str, Any]] = []
        self.covered_mark_min: float | None = None  # the latest mark a stored note stands for

    def minutes(self, wall: float) -> float | None:
        if self.sound_on_wall_sec is None:
            return None
        return round((wall - self.sound_on_wall_sec) / 60.0, 2)

    def mark_at(self, wall: float) -> float:
        return latest_mark_min(self.plan, (wall - self.sound_on_wall_sec) / 60.0)

    def is_due(self, at_wall: float | None = None) -> bool:
        """True when no stored note stands for the latest mark at ``at_wall`` (default now).

        For a scheduled wait ``at_wall`` is the time the next run is due, so the 5-minute note is
        taken in the wait before the 5-minute run (and counts as the 5-minute note).
        """
        if self.covered_mark_min is None:
            return True
        if self.sound_on_wall_sec is None:
            return False
        reference = self.clock.time() if at_wall is None else float(at_wall)
        return self.mark_at(reference) > self.covered_mark_min + 1e-9

    def latest_supply(self) -> tuple[float | None, float | None]:
        if self.psu_log is None:
            return None, None
        try:
            from psu_run_link import load_psu_rows

            rows = [r for r in load_psu_rows(self.psu_log) if r.get("V") is not None and r.get("I") is not None]
        except Exception:  # noqa: BLE001 - an unreadable log must not stop the recording
            return None, None
        if not rows:
            return None, None
        return rows[-1]["V"], rows[-1]["I"]

    def ask(
        self,
        kind: str,
        next_run_number: int | None,
        next_label: str,
        deadline_wall: float | None,
        covers_wall: float | None = None,
    ) -> dict | None:
        """Ask once; returns the stored record, or None when nothing was entered.

        ``covers_wall`` (the start time of the next scheduled run) lets a note taken in the wait
        before a mark stand for that mark.
        """
        now = self.clock.time()
        minute = self.minutes(now)
        when = "" if minute is None else f"音を出してから {minute:.1f} 分、"
        upcoming = "" if next_run_number is None else f"次は run {next_run_number}（{next_label}）。"
        limit = "" if deadline_wall is None else f"{max(0.0, deadline_wall - now):.0f} 秒以内に"
        print(
            f"\n[THERMAL] 温度の記録（{kind}）: {when}{upcoming}\n"
            f"[THERMAL] {limit}「{' '.join(FIELD_LABELS)}」の順に空白区切りで入力してEnter"
            "（測れない値は -、数字のあとの文はメモ、空のEnterは記録なし。打ち始めたら Enter まで待ちます）。"
            "PAT は粒子を中央で保持しています。",
            flush=True,
        )
        reader = self.reader or read_line_with_deadline
        parsed = parse_thermal_line(reader("[THERMAL] > ", deadline_wall, self.clock))
        if parsed is None:
            return None
        values, note = parsed
        answered = self.clock.time()
        supply_v, supply_a = self.latest_supply()
        record = {
            "recorded_at": dt.datetime.fromtimestamp(answered).isoformat(timespec="seconds"),
            "minutes_after_sound_on": self.minutes(answered),
            "kind": kind,
            "next_run_number": next_run_number,
            "next_label": next_label,
            **values,
            "supply_V": supply_v,
            "supply_A": supply_a,
            "note": note,
        }
        self.records.append(record)
        if self.sound_on_wall_sec is not None:
            mark = self.mark_at(max(answered, answered if covers_wall is None else float(covers_wall)))
            self.covered_mark_min = mark if self.covered_mark_min is None else max(self.covered_mark_min, mark)
        else:
            self.covered_mark_min = 0.0
        self.write_csv()
        shown = ", ".join(f"{label}={'-' if values[name] is None else values[name]}"
                          for name, label in zip(FIELDS, FIELD_LABELS))
        print(f"[THERMAL] 記録しました: {shown}" + (f"、メモ: {note}" if note else ""), flush=True)
        return record

    def write_csv(self) -> None:
        temporary = self.csv_path.with_suffix(".csv.tmp")
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
            writer.writeheader()
            for record in self.records:
                writer.writerow({key: ("" if record.get(key) is None else record[key]) for key in CSV_COLUMNS})
        os.replace(temporary, self.csv_path)
