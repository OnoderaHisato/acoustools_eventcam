"""Hardware-free tests for the temperature prompts typed into a running auto recording (2026-10-10)."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
import unittest

import thermal_prompt
from test_thermal_hold_test import SOUND_ON_SEC, FakeClock, run_main, write_export


class ParsingTests(unittest.TestCase):
    def test_values_dashes_and_note(self) -> None:
        values, note = thermal_prompt.parse_thermal_line("41.2 39,8 - 45 fan loud")
        self.assertEqual(values, {"T_top_PAT_C": 41.2, "T_bottom_PAT_C": 39.8, "T_room_C": None, "RH_percent": 45.0})
        self.assertEqual(note, "fan loud")
        values, note = thermal_prompt.parse_thermal_line("41 40")
        self.assertEqual((values["T_room_C"], values["RH_percent"], note), (None, None, ""))

    def test_empty_or_missing_answer_records_nothing(self) -> None:
        for text in (None, "", "   ", "- - - -"):
            self.assertIsNone(thermal_prompt.parse_thermal_line(text), text)
        values, note = thermal_prompt.parse_thermal_line("smell of hot board")
        self.assertTrue(all(v is None for v in values.values()))
        self.assertEqual(note, "smell of hot board")

    def test_interval_plan(self) -> None:
        plan = thermal_prompt.parse_interval_plan("92:10, 0:5")
        self.assertEqual(plan, [(0.0, 5.0), (92.0, 10.0)])
        self.assertEqual(thermal_prompt.interval_min_at(plan, 91.9), 5.0)
        self.assertEqual(thermal_prompt.interval_min_at(plan, 92.0), 10.0)
        for bad in ("", "5:5", "0:0", "0-5", "0:x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                thermal_prompt.parse_interval_plan(bad)

    @unittest.skipUnless(thermal_prompt.os.name == "nt", "console reader uses msvcrt on Windows")
    def test_typing_cancels_the_deadline_until_enter(self) -> None:
        class Clock:
            now = 0.0

            def time(self):
                return self.now

        clock = Clock()
        keys = iter("41.5 40\r")
        # The deadline passes right after the first key; the reader must keep going until Enter.
        def getwche():
            clock.now += 100.0
            return next(keys)

        fake = mock.Mock(kbhit=mock.Mock(return_value=True), getwche=getwche)
        with mock.patch.dict(sys.modules, {"msvcrt": fake}), mock.patch("builtins.print"):
            self.assertEqual(thermal_prompt.read_line_with_deadline("> ", 10.0, clock), "41.5 40")
        # Nothing typed before the deadline: gives up.
        clock.now = 0.0
        idle = mock.Mock(kbhit=mock.Mock(return_value=False))

        def tick(_):
            clock.now += 5.0

        with mock.patch.dict(sys.modules, {"msvcrt": idle}), mock.patch("builtins.print"), \
                mock.patch.object(thermal_prompt.time, "sleep", side_effect=tick):
            self.assertIsNone(thermal_prompt.read_line_with_deadline("> ", 10.0, clock))

    def test_marks_follow_the_sound_on_clock(self) -> None:
        plan = thermal_prompt.parse_interval_plan("0:5,92:10")
        marks = {m: thermal_prompt.latest_mark_min(plan, m) for m in (0, 4.9, 5, 89.9, 91.9, 92, 101.9, 102, 115)}
        self.assertEqual(marks, {0: 0, 4.9: 0, 5: 5, 89.9: 85, 91.9: 90, 92: 92, 101.9: 92, 102: 102, 115: 112})


class SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.export_dir = write_export(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def argv(self, runs: list[str], *extra: str) -> list[str]:
        argv = ["--hf-export-dir", str(self.export_dir), "--output-dir", str(self.root / "rec")]
        for name in runs:
            argv += ["--hf-run", f"{name}=1"]
        return argv + ["--acknowledge-step-response-risk", "--unattended-after-first-checkpoint",
                       "--prompt-on-capture-failure", *extra]

    def session(self) -> dict:
        return json.loads(next((self.root / "rec").glob("auto_recording_session_*.json")).read_text(encoding="utf-8"))

    def test_scheduled_session_asks_during_the_waits_without_delaying_runs(self) -> None:
        clock = FakeClock(SOUND_ON_SEC + 120.0)
        asked: list[tuple[str, float | None, float]] = []
        answers = iter(["40 38 24 50 start", "41 39 24 50", "42 40 24.5 51", "43 41 25 51 end"])

        def reader(prompt, deadline, clk):
            asked.append((prompt, deadline, clk.time()))
            return next(answers)

        argv = self.argv(["kx", "ky", "kz"] * 3, "--schedule-interval-sec", "300", "--schedule-group-size", "3",
                         "--thermal-prompt-every", "0:5", "--pat-board-gap-mm", "236.5-237")
        with mock.patch.object(thermal_prompt, "read_line_with_deadline", side_effect=reader):
            result, _events, starts = run_main(argv, clock=clock, run_seconds=50.0)
        self.assertEqual(result, 0)
        # The runs keep their schedule: group 0 after the check, groups 1 and 2 at 5 and 10 minutes.
        self.assertEqual([s - SOUND_ON_SEC for s in starts], [120, 170, 220, 300, 350, 400, 600, 650, 700])
        session = self.session()
        notes = session["thermal_notes"]
        self.assertEqual([n["kind"] for n in notes], ["start", "periodic", "periodic", "end"])
        self.assertEqual([n["next_run_number"] for n in notes], [1, 4, 7, None])
        # Periodic prompts give up 10 s before the scheduled run.
        self.assertEqual(asked[1][1] - SOUND_ON_SEC, 290.0)
        self.assertEqual(asked[2][1] - SOUND_ON_SEC, 590.0)
        self.assertEqual(notes[0]["T_top_PAT_C"], 40.0)
        self.assertEqual(notes[0]["note"], "start")
        self.assertEqual(session["pat_board_gap_mm"], "236.5-237")
        self.assertEqual(session["thermal_prompt_settings"]["timeout_sec"], 180.0)
        with open(session["thermal_log_csv"], encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 4)
        self.assertEqual(list(rows[0].keys()), list(thermal_prompt.CSV_COLUMNS))
        self.assertEqual(rows[2]["T_room_C"], "24.5")

    def test_unscheduled_session_asks_between_runs_and_retries_a_missed_note(self) -> None:
        clock = FakeClock(SOUND_ON_SEC)
        asked: list[float] = []
        answers = iter(["40 38 24 50", "", "41 39 24 50", "42 40 25 51", "43 41 25 52"])

        def reader(prompt, deadline, clk):
            asked.append(deadline - clk.time())
            return next(answers)

        argv = self.argv(["kx", "ky", "kz", "kx", "ky"], "--thermal-prompt-every", "0:1",
                         "--thermal-prompt-timeout-sec", "45")
        with mock.patch.object(thermal_prompt, "read_line_with_deadline", side_effect=reader):
            result, _events, starts = run_main(argv, clock=clock, run_seconds=50.0)
        self.assertEqual(result, 0)
        notes = self.session()["thermal_notes"]
        # Runs start at 0, 50, 100, 150, 200 s. Start note before run 1; nothing due at 50 s; the
        # 1-minute mark is due at 100 s but unanswered, so it is asked again at 150 s; the 3-minute
        # mark at 200 s; then the end.
        self.assertEqual([(n["kind"], n["next_run_number"]) for n in notes],
                         [("start", 1), ("periodic", 4), ("periodic", 5), ("end", None)])
        self.assertEqual(asked, [45.0, 45.0, 45.0, 45.0, 90.0])  # the end prompt waits twice as long

    def test_no_prompts_without_the_option_and_bad_plans_never_open_hardware(self) -> None:
        with mock.patch.object(thermal_prompt, "read_line_with_deadline") as reader:
            result, _events, _starts = run_main(self.argv(["kx", "ky"]), clock=FakeClock(SOUND_ON_SEC))
        self.assertEqual(result, 0)
        reader.assert_not_called()
        self.assertNotIn("thermal_notes", self.session())
        result, events, _starts = run_main(self.argv(["kx"], "--thermal-prompt-every", "5:5"))
        self.assertEqual(result, 2)
        events.open_hw.assert_not_called()


if __name__ == "__main__":
    unittest.main()
