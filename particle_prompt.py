"""Particle records typed into a running auto recording session (paper sessions, 2026-10-16).

The paper plan (PAPER_SESSION_PLAN_20261014.md section 4) wants each particle documented: number (0, 1, 2, 3;
0' for a replacement after a drop), when it was placed and removed, the runs it carried, three caliper
diameters (0.1 mm) with mean and spread, before and after use, the calipers, who measured, and where the
particle came from. With ``--particle-prompts`` the recorder asks at the moments the operator is at the PAT:

* at the start, right after the PAT output is on: the particle about to be levitated
  (``number d1 d2 d3 [note]``; the number may be left out when the runner knows it from the runlist);
* at a particle change, after the new particle is confirmed: first the after-use diameters of the particle that
  was taken out, then the new particle;
* at the end, after the particle was collected (the end-of-session hold ended with Enter): its after-use
  diameters.

An empty Enter skips a question; nothing typed within the time limit skips it too (the session goes on). The
answers go straight into the session JSON under ``particles`` (one entry per particle, in the order used),
each run record gets ``particle``, and ``particle_record.py`` can still add or correct fields afterwards.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any, Callable

import thermal_prompt

DEFAULT_TIMEOUT_SEC = 600.0
MIN_DIAMETER_MM = 0.2
MAX_DIAMETER_MM = 10.0


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _float(token: str) -> float | None:
    try:
        value = float(token.replace(",", "."))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_particle_line(text: str | None, expected_label: str | None, want_label: bool = True) -> dict[str, Any] | None:
    """``"0 1.5 1.6 1.5 note"`` -> {"particle": "0", "diameters_mm": [1.5, 1.6, 1.5], "note": "note"}.

    The particle number comes first; it may be left out when ``expected_label`` is known (then three numbers
    are the diameters). A label that is not a plain number (``0'``) is always taken as the label. Returns None
    for an empty line; raises ValueError for anything else that does not fit (the caller asks again).
    """
    if text is None or not text.strip():
        return None
    tokens = text.split()
    numbers: list[float] = []
    for token in tokens:
        value = _float(token)
        if value is None:
            break
        numbers.append(value)
    label: str | None = None
    rest_start = len(numbers)
    if want_label:
        if not numbers:                       # e.g. "0'" or a typo: the first token is the label
            label = tokens[0]
            rest_start = 1
            numbers = []
            for token in tokens[1:]:
                value = _float(token)
                if value is None:
                    break
                numbers.append(value)
            rest_start = 1 + len(numbers)
        elif len(numbers) in (1, 4):          # "1" or "1 1.5 1.6 1.5": the first number is the particle
            label = tokens[0]
            numbers = numbers[1:]
        elif expected_label is None:
            raise ValueError("start with the particle number, e.g. '0 1.5 1.6 1.5'")
        if label is None:
            label = expected_label
    if len(numbers) not in (0, 3):
        raise ValueError("give three diameters in mm (or none), e.g. '1.5 1.6 1.5'")
    for value in numbers:
        if not MIN_DIAMETER_MM <= value <= MAX_DIAMETER_MM:
            raise ValueError(f"diameter {value} mm is outside {MIN_DIAMETER_MM}..{MAX_DIAMETER_MM} mm")
    note = " ".join(tokens[rest_start:]).strip()
    return {"particle": label, "diameters_mm": numbers, "note": note}


def diameter_record(values: list[float], when: str) -> dict[str, Any]:
    return {
        "values_mm": [round(v, 3) for v in values],
        "mean_mm": round(sum(values) / len(values), 3),
        "spread_mm": round(max(values) - min(values), 3),
        "measured_when": when,
        "measured_at": _now(),
    }


class ParticlePrompts:
    """Keeps ``session['particles']`` and asks the operator; the caller writes the session JSON."""

    def __init__(
        self,
        session: dict[str, Any],
        *,
        calipers: str = "",
        measured_by: str = "",
        source: str = "",
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        reader: Callable[[str, float | None], str | None] | None = None,
        clock: Any = None,
    ) -> None:
        import time

        self.session = session
        self.static = {k: v for k, v in (("calipers", calipers), ("measured_by", measured_by),
                                         ("source", source)) if str(v).strip()}
        self.timeout_sec = float(timeout_sec)
        self.reader = reader or (lambda prompt, deadline: thermal_prompt.read_line_with_deadline(prompt, deadline))
        self.clock = clock or time
        self.session.setdefault("particles", [])
        self.current: dict[str, Any] | None = None

    # ---- asking -----------------------------------------------------------------------------------------
    def _ask(self, prompt: str, expected_label: str | None, want_label: bool) -> dict[str, Any] | None:
        deadline = self.clock.time() + self.timeout_sec if self.timeout_sec > 0 else None
        for _attempt in range(3):
            try:
                text = self.reader(prompt, deadline)
            except EOFError:
                return None
            try:
                return parse_particle_line(text, expected_label, want_label)
            except ValueError as exc:
                print(f"[PARTICLE] {exc}; try again (empty Enter skips).", flush=True)
        return None

    def _new_entry(self, label: str, placed_before_run: int, answer: dict[str, Any] | None) -> dict[str, Any]:
        entry: dict[str, Any] = {"particle": str(label), "placed_before_run": int(placed_before_run),
                                 "placed_at": None, "runs": [], "diameters": [], **self.static}
        if answer:
            if answer["diameters_mm"]:
                entry["diameters"].append(diameter_record(answer["diameters_mm"], "before"))
            if answer["note"]:
                entry["note"] = answer["note"]
        self.session["particles"].append(entry)
        self.current = entry
        return entry

    def start(self, expected_label: str | None, first_run: int = 1) -> dict[str, Any]:
        hint = f" (number {expected_label} from the runlist may be left out)" if expected_label else ""
        print(
            "\n[PARTICLE] The particle for this session: type 'number d1 d2 d3 [note]' with the three caliper "
            f"diameters in mm{hint}. Empty Enter skips.",
            flush=True,
        )
        answer = self._ask("[PARTICLE] particle, diameters: ", expected_label, True)
        label = (answer or {}).get("particle") or expected_label or "?"
        return self._new_entry(label, first_run, answer)

    def change(self, expected_label: str | None, before_run: int, changed_at: str) -> dict[str, Any]:
        old = self.current
        if old is not None:
            old["removed_at"] = changed_at
            print(f"\n[PARTICLE] Particle {old['particle']} was taken out. Its diameters after use "
                  "'d1 d2 d3 [note]' (empty Enter skips):", flush=True)
            answer = self._ask(f"[PARTICLE] particle {old['particle']} after: ", old["particle"], False)
            if answer and answer["diameters_mm"]:
                old["diameters"].append(diameter_record(answer["diameters_mm"], "after"))
            if answer and answer["note"]:
                old["note"] = (old.get("note", "") + " / " + answer["note"]).strip(" /")
        hint = f" (number {expected_label} may be left out)" if expected_label else ""
        print(f"[PARTICLE] The new particle: 'number d1 d2 d3 [note]'{hint}.", flush=True)
        answer = self._ask("[PARTICLE] new particle, diameters: ", expected_label, True)
        label = (answer or {}).get("particle") or expected_label or "?"
        entry = self._new_entry(label, before_run, answer)
        entry["placed_at"] = changed_at
        return entry

    def mark_placed(self, at: str | None = None) -> None:
        if self.current is not None and not self.current.get("placed_at"):
            self.current["placed_at"] = at or _now()

    def note_run(self, run_number: int, label: str, exit_code: int) -> str | None:
        if self.current is None:
            return None
        if int(exit_code) == 0:
            self.current["runs"].append({"run_number": int(run_number), "label": label})
            self.current["first_run"] = self.current["runs"][0]["label"]
            self.current["last_run"] = label
        return self.current["particle"]

    def finish(self, collected: bool, ended_at: str | None = None) -> None:
        """At the end of the session: when the particle was collected, ask its after-use diameters."""
        old = self.current
        if old is None:
            return
        old["removed_at"] = ended_at or _now()
        if not collected:
            old["removal"] = "PAT turned off without a collection hold (the particle fell)"
            return
        old["removal"] = "collected during the end-of-session hold"
        print(f"\n[PARTICLE] Particle {old['particle']} collected. Its diameters after use 'd1 d2 d3 [note]' "
              "(empty Enter skips):", flush=True)
        answer = self._ask(f"[PARTICLE] particle {old['particle']} after: ", old["particle"], False)
        if answer and answer["diameters_mm"]:
            old["diameters"].append(diameter_record(answer["diameters_mm"], "after"))
        if answer and answer["note"]:
            old["note"] = (old.get("note", "") + " / " + answer["note"]).strip(" /")
