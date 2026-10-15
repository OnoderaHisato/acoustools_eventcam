"""Hardware-free tests for the PAT board temperature log (pat_temp_logger.py, pat_temp_run_link.py, the
PAT sensor columns of thermal_prompt.py and --pat-temp-log in the auto entry).

No serial port is opened: the Arduino is a fake object that returns the lines the sketch
ds18b20_serial_logger.ino prints.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock
import unittest

import acoustools_stereo_eventcam_3d_recording_auto as auto_record
import pat_temp_logger as logger
import pat_temp_run_link as link
import thermal_prompt
from test_thermal_hold_test import SOUND_ON_SEC, FakeClock, write_export

ROM_TOP = "28FF641E8C160401"
ROM_BOTTOM = "28FF0A2B8D16050C"
ROM_SPARE = "2861640A1B2C3D4E"
SKETCH_START = [
    "# ds18b20_serial_logger v1",
    "# sensors,3",
    "# parasite_power,0",
    f"# rom,0,{ROM_TOP}",
    f"# rom,1,{ROM_BOTTOM}",
    f"# rom,2,{ROM_SPARE}",
]


def cycle_lines(cycle: int, top: float, bottom: float, spare_status: str = "ok") -> list[str]:
    ms = 1000 * cycle
    spare_temp = "-127.0000" if spare_status == "disconnected" else "25.5000"
    return [
        f"T,{cycle},{ms},{ROM_TOP},{top:.4f},ok",
        f"T,{cycle},{ms},{ROM_BOTTOM},{bottom:.4f},ok",
        f"T,{cycle},{ms},{ROM_SPARE},{spare_temp},{spare_status}",
    ]


class FakeLink:
    """Returns one line per readline() and moves the clock by ``step`` each call (0.5 s when idle)."""

    def __init__(self, lines: list[str], clock: FakeClock, step: float = 0.01) -> None:
        self.lines = list(lines)
        self.clock = clock
        self.step = step
        self.written: list[bytes] = []
        self.closed = False

    def readline(self) -> bytes:
        if self.lines:
            self.clock.now += self.step
            return (self.lines.pop(0) + "\r\n").encode("ascii")
        self.clock.now += 0.5
        return b""

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def close(self) -> None:
        self.closed = True


def port(device: str, vid: int | None, pid: int | None, description: str = "") -> SimpleNamespace:
    return SimpleNamespace(device=device, vid=vid, pid=pid, description=description)


PORTS = [port("COM12", 0x2341, 0x0043, "Arduino Uno"), port("COM5", 0x0403, 0x6014, "USB Serial Port"),
         port("COM6", 0x0403, 0x6014, "USB Serial Port")]


def write_map(path: Path) -> Path:
    path.write_text(json.dumps({"sensors": {
        ROM_TOP: {"name": "top_ft232h", "group": "top", "offset_C": -0.25},
        ROM_BOTTOM: {"name": "bottom_ft232h", "group": "bottom"},
    }}), encoding="utf-8")
    return path


class LineTests(unittest.TestCase):
    def test_the_sketch_lines(self) -> None:
        self.assertEqual(logger.parse_line(f"T,7,7000,{ROM_TOP.lower()},38.0625,ok\r\n"),
                         {"kind": "reading", "cycle": 7, "arduino_ms": 7000, "rom": ROM_TOP,
                          "temp_raw_C": 38.0625, "status": "ok"})
        self.assertEqual(logger.parse_line("# sensors,4"), {"kind": "sensors", "count": 4})
        self.assertEqual(logger.parse_line(f"# rom,0,{ROM_TOP}")["rom"], ROM_TOP)
        self.assertTrue(logger.parse_line("# parasite_power,1")["value"])
        self.assertEqual(logger.parse_line("# ds18b20_serial_logger v1")["kind"], "banner")
        for bad in ("", "T,1,2,3", f"T,x,1,{ROM_TOP},1.0,ok", "T,1,1,NOTAROM,1.0,ok", "garbage"):
            with self.subTest(bad=bad):
                self.assertIsNone(logger.parse_line(bad))


class PortTests(unittest.TestCase):
    def test_auto_takes_the_arduino_and_never_an_ftdi_port(self) -> None:
        self.assertEqual(logger.choose_port("auto", PORTS), "COM12")
        self.assertEqual(logger.candidate_ports(PORTS), [PORTS[0]])
        with self.assertRaisesRegex(RuntimeError, "no Arduino"):
            logger.choose_port("auto", PORTS[1:])
        with self.assertRaisesRegex(RuntimeError, "several"):
            logger.choose_port("auto", PORTS + [port("COM13", 0x1A86, 0x7523)])

    def test_a_pat_board_is_refused_even_by_name(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "PAT controller"):
            logger.choose_port("com5", PORTS)
        self.assertEqual(logger.choose_port("COM12", PORTS), "COM12")


class SensorMapTests(unittest.TestCase):
    def test_map_and_bad_rom(self) -> None:
        with TemporaryDirectory() as temporary:
            sensors = logger.load_sensor_map(write_map(Path(temporary) / "map.json"))
            self.assertEqual((sensors[ROM_TOP].name, sensors[ROM_TOP].group, sensors[ROM_TOP].offset_C),
                             ("top_ft232h", "top", -0.25))
            bad = Path(temporary) / "bad.json"
            bad.write_text(json.dumps({"sensors": {"28FF": {}}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "ROM id"):
                logger.load_sensor_map(bad)
        example = Path(__file__).with_name("pat_temp_sensors.example.json")
        self.assertEqual(len(logger.load_sensor_map(example)), 4)


class LoggerTests(unittest.TestCase):
    def test_rows_names_offsets_status_and_silence(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            clock = FakeClock(1000.0)
            lines = SKETCH_START + cycle_lines(1, 38.0, 36.5) + cycle_lines(2, 38.0625, 36.5, "disconnected")
            fake = FakeLink(lines, clock)
            args = logger.parse_args(["--output", str(root / "pat.csv"), "--sensor-map",
                                      str(write_map(root / "map.json")), "--stale-s", "3", "--duration", "8"])
            with mock.patch("builtins.print"):
                self.assertEqual(logger.log_loop(args, opener=lambda _p: fake, port_lister=lambda: PORTS,
                                                 clock=clock, sleep=clock.sleep), 0)
            with (root / "pat.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            readings = [r for r in rows if r["rom"]]
            self.assertEqual(len(readings), 6)
            # the board announced itself without being asked: it restarted when the port was opened
            self.assertEqual([r["event"] for r in rows if r["event"]], ["port_opened_reset"])
            first = readings[0]
            self.assertEqual((first["port"], first["name"], first["group"], first["temp_raw_C"], first["temp_C"]),
                             ("COM12", "top_ft232h", "top", "38.0000", "37.7500"))
            spare = [r for r in readings if r["rom"] == ROM_SPARE]
            self.assertEqual((spare[0]["name"], spare[0]["temp_C"]), (ROM_SPARE, "25.5000"))
            self.assertEqual((spare[1]["status"], spare[1]["temp_C"]), ("disconnected", ""))
            self.assertTrue(any("no reading" in r["error"] for r in rows))
            self.assertTrue(fake.closed)
            meta = json.loads((root / "pat.csv.meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["roms"], [ROM_TOP, ROM_BOTTOM, ROM_SPARE])
            self.assertEqual(meta["unmapped_roms"], [ROM_SPARE])
            self.assertEqual(fake.written, [])        # nothing sent when the sketch answered at once

    def test_a_silent_port_gets_a_rescan_then_fails_and_is_retried(self) -> None:
        with TemporaryDirectory() as temporary:
            clock = FakeClock(1000.0)
            opened = []

            def opener(name):
                opened.append(FakeLink([], clock))
                return opened[-1]

            args = logger.parse_args(["--output", str(Path(temporary) / "pat.csv"), "--duration", "20"])
            args.sensor_map = None
            with mock.patch("builtins.print"):
                logger.log_loop(args, opener=opener, port_lister=lambda: PORTS, clock=clock, sleep=clock.sleep)
            self.assertGreaterEqual(len(opened), 2)
            self.assertEqual(opened[0].written, [b"s"])
            with (Path(temporary) / "pat.csv").open(encoding="utf-8", newline="") as handle:
                first = next(csv.DictReader(handle))
            self.assertIn("no answer", first["error"])
            self.assertEqual(first["event"], "port_lost")

    def test_once(self) -> None:
        clock = FakeClock(0.0)
        with TemporaryDirectory() as temporary:
            args = logger.parse_args(["--once", "--sensor-map", str(write_map(Path(temporary) / "m.json"))])
            fake = FakeLink(SKETCH_START + cycle_lines(1, 38.0, 36.5), clock)
            with mock.patch("builtins.print") as printed:
                self.assertEqual(logger.once(args, opener=lambda _p: fake, port_lister=lambda: PORTS, clock=clock), 0)
            text = "\n".join(str(call.args[0]) for call in printed.call_args_list if call.args)
            self.assertIn("top_ft232h [top]: 37.7500 degC", text)
            self.assertIn("not in the sensor map", text)
            bad = FakeLink(SKETCH_START[:2] + [f"# rom,0,{ROM_TOP}", f"T,1,1,{ROM_TOP},-127.0000,disconnected"], clock)
            args.sensor_map = None
            with mock.patch("builtins.print"), mock.patch("sys.stderr"):
                self.assertEqual(logger.once(args, opener=lambda _p: bad, port_lister=lambda: PORTS, clock=clock), 1)

    def test_a_running_sketch_is_asked_for_its_sensor_list(self) -> None:
        """No reset on open: readings come first, then the list after the rescan request."""
        clock = FakeClock(0.0)

        class RunningLink(FakeLink):
            def write(self, data: bytes) -> None:
                super().write(data)
                if data == b"s":
                    self.lines.extend(SKETCH_START)

        link = RunningLink(cycle_lines(5, 38.0, 36.5)[:1], clock)
        state = logger.StreamState()
        pending: list = []
        self.assertTrue(logger.handshake(link, state, clock=clock, pending=pending))
        self.assertEqual(link.written, [b"s"])
        self.assertEqual((state.sensor_count, state.roms), (3, [ROM_TOP, ROM_BOTTOM, ROM_SPARE]))
        self.assertEqual([event["rom"] for event in pending], [ROM_TOP])
        self.assertFalse(state.reset_on_open)

    def test_a_reopen_without_reset_is_logged_as_port_opened(self) -> None:
        with TemporaryDirectory() as temporary:
            clock = FakeClock(1000.0)

            class RunningLink(FakeLink):
                def write(self, data: bytes) -> None:
                    super().write(data)
                    if data == b"s":
                        self.lines.extend(SKETCH_START + cycle_lines(8, 38.0, 36.5))

            link = RunningLink(cycle_lines(7, 38.0, 36.5)[:1], clock)
            args = logger.parse_args(["--output", str(Path(temporary) / "pat.csv"), "--duration", "4"])
            args.sensor_map = None
            with mock.patch("builtins.print"):
                logger.log_loop(args, opener=lambda _p: link, port_lister=lambda: PORTS, clock=clock, sleep=clock.sleep)
            with (Path(temporary) / "pat.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([r["event"] for r in rows if r["event"]], ["port_opened"])

    def test_a_repeated_reading_is_written_once_but_a_restart_is_kept(self) -> None:
        with TemporaryDirectory() as temporary:
            clock = FakeClock(1000.0)
            lines = SKETCH_START + cycle_lines(5, 38.0, 36.5) + cycle_lines(5, 38.0, 36.5) + cycle_lines(1, 37.0, 36.0)
            args = logger.parse_args(["--output", str(Path(temporary) / "pat.csv"), "--duration", "5"])
            args.sensor_map = None
            with mock.patch("builtins.print"):
                logger.log_loop(args, opener=lambda _p: FakeLink(lines, clock), port_lister=lambda: PORTS,
                                clock=clock, sleep=clock.sleep)
            with (Path(temporary) / "pat.csv").open(encoding="utf-8", newline="") as handle:
                cycles = [r["cycle"] for r in csv.DictReader(handle) if r["rom"] == ROM_TOP]
            self.assertEqual(cycles, ["5", "1"])

    def test_bad_arguments(self) -> None:
        for argv in ([], ["--output", "x.csv", "--stale-s", "0"], ["--once", "--list"]):
            with self.subTest(argv=argv), mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                logger.parse_args(argv)


def write_pat_log(path: Path, start: float, seconds: int, top=lambda k: 38.0, gap=(), restart_at=None,
                  events=(), columns=None) -> None:
    """One reading per second; ``restart_at`` makes the cycle counter start again (Arduino restart),
    ``events`` = [(second, event)] adds port event rows, ``columns`` writes an older column set."""
    sensors = logger.load_sensor_map(write_map(path.with_name("map.json")))
    fieldnames = columns or logger.CSV_COLUMNS
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for k in range(seconds):
            for second, name in events:
                if second == k:
                    writer.writerow(logger.event_row(start + k - 0.5, "COM12", name))
            if any(a <= k < b for a, b in gap):
                continue
            cycle = k if restart_at is None or k < restart_at else k - restart_at + 1
            for event in (logger.parse_line(line) for line in cycle_lines(cycle, top(k) + 0.25, 36.5)):
                writer.writerow(logger.reading_row(event, start + k, "COM12", sensors))


class RunLinkTests(unittest.TestCase):
    def test_rows_around_a_run_and_the_summary(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "pat.csv"
            write_pat_log(log, 1000.0, 300, top=lambda k: 38.0 + k / 100.0)
            run_dir = root / "run"
            run_dir.mkdir()
            summary = link.attach_pat_temperature_record(run_dir, log, 1100.0, 1160.0)
            with (run_dir / "pat_temperature_log.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 3 * 71)          # 1095 .. 1165 s with the 5 s margin
            top = summary["sensors"][ROM_TOP]
            self.assertEqual((top["name"], top["group"], top["first_C"], top["last_C"]), ("top_ft232h", "top", 38.95, 39.65))
            self.assertEqual(summary["bottom_mean_C"], 36.5)
            self.assertEqual(summary["sensors"][ROM_SPARE]["group"], "")
            self.assertFalse(summary["stale"])
            self.assertEqual(summary["run_file"], "pat_temperature_log.csv")
            self.assertEqual((summary["arduino_restarts"], summary["port_events"], summary["interrupted"]),
                             ([], [], False))

    def test_a_restart_or_port_event_inside_the_run_marks_it_interrupted(self) -> None:
        with TemporaryDirectory() as temporary:
            log = Path(temporary) / "pat.csv"
            write_pat_log(log, 1000.0, 300, restart_at=130, events=[(128, "port_lost"), (130, "port_opened_reset")],
                          gap=[(128, 130)])
            inside = link.attach_pat_temperature_record(None, log, 1100.0, 1160.0)
            self.assertTrue(inside["interrupted"])
            self.assertEqual(len(inside["arduino_restarts"]), 1)
            self.assertEqual([e["event"] for e in inside["port_events"]], ["port_lost", "port_opened_reset"])
            before = link.attach_pat_temperature_record(None, log, 1200.0, 1260.0)   # restart outside the window
            self.assertFalse(before["interrupted"])
            self.assertEqual(before["arduino_restarts"], [])

    def test_a_plain_reopen_without_reset_is_recorded_but_not_an_interruption(self) -> None:
        with TemporaryDirectory() as temporary:
            log = Path(temporary) / "pat.csv"
            write_pat_log(log, 1000.0, 100, events=[(50, "port_opened")])
            summary = link.attach_pat_temperature_record(None, log, 1040.0, 1060.0)
            self.assertEqual([e["event"] for e in summary["port_events"]], ["port_opened"])
            self.assertFalse(summary["interrupted"])

    def test_a_silence_row_marks_the_run_interrupted(self) -> None:
        with TemporaryDirectory() as temporary:
            log = Path(temporary) / "pat.csv"
            write_pat_log(log, 1000.0, 100)
            with log.open("a", encoding="utf-8", newline="") as handle:
                csv.DictWriter(handle, fieldnames=logger.CSV_COLUMNS).writerow(
                    logger.error_row(1050.2, "COM12", "no reading for 10 s"))
            summary = link.attach_pat_temperature_record(None, log, 1040.0, 1060.0)
            self.assertTrue(summary["interrupted"])
            self.assertEqual(len(summary["reading_gaps"]), 1)

    def test_an_older_log_without_the_event_column_is_read(self) -> None:
        with TemporaryDirectory() as temporary:
            log = Path(temporary) / "pat.csv"
            write_pat_log(log, 1000.0, 100, columns=[c for c in logger.CSV_COLUMNS if c != "event"])
            summary = link.attach_pat_temperature_record(None, log, 1040.0, 1060.0)
            self.assertEqual(summary["bottom_mean_C"], 36.5)
            self.assertFalse(summary["interrupted"])

    def test_latest_reading_silence_and_a_missing_log(self) -> None:
        with TemporaryDirectory() as temporary:
            log = Path(temporary) / "pat.csv"
            write_pat_log(log, 1000.0, 200, gap=[(100, 200)])
            latest = link.latest_reading(log, 1110.0)
            self.assertEqual((latest["top_C"], latest["bottom_C"], latest["age_s"]), (38.0, 36.5, 11.0))
            self.assertEqual(latest["values"]["top_ft232h"], 38.0)
            self.assertIsNone(link.latest_reading(log, 1200.0))
            self.assertTrue(link.attach_pat_temperature_record(None, log, 1150.0, 1190.0)["stale"])
            self.assertIn("error", link.attach_pat_temperature_record(None, Path(temporary) / "none.csv", 0.0, 1.0))


class ThermalNoteTests(unittest.TestCase):
    def make_notes(self, root: Path, answer: str | None) -> thermal_prompt.ThermalNotes:
        log = root / "pat.csv"
        write_pat_log(log, SOUND_ON_SEC - 100.0, 400)
        return thermal_prompt.ThermalNotes(
            csv_path=root / "thermal.csv", plan=[(0.0, 5.0)], timeout_sec=60.0,
            sound_on_wall_sec=SOUND_ON_SEC, clock=FakeClock(SOUND_ON_SEC + 155.5),
            pat_temp_log=log, reader=lambda *_: answer,
        )

    def test_a_typed_note_keeps_both_methods_apart(self) -> None:
        with TemporaryDirectory() as temporary:
            notes = self.make_notes(Path(temporary), "41.2 39.8")
            with mock.patch("builtins.print"):
                record = notes.ask("scheduled", 3, "kcheck_x", None)
            self.assertEqual((record["T_top_PAT_C"], record["pat_sensor_top_C"], record["pat_sensor_bottom_C"]),
                             (41.2, 38.0, 36.5))
            self.assertEqual(record["pat_sensor_age_s"], 0.5)
            self.assertIn("top_ft232h=38.0", record["pat_sensor_values"])
            with (Path(temporary) / "thermal.csv").open(encoding="utf-8-sig", newline="") as handle:
                self.assertEqual(next(csv.DictReader(handle))["pat_sensor_bottom_C"], "36.5")

    def test_no_answer_still_stores_the_sensor_values(self) -> None:
        for answer in (None, ""):
            with self.subTest(answer=answer), TemporaryDirectory() as temporary:
                notes = self.make_notes(Path(temporary), answer)
                with mock.patch("builtins.print"):
                    record = notes.ask("scheduled", 3, "kcheck_x", None)
                self.assertIsNone(record["T_top_PAT_C"])
                self.assertEqual(record["pat_sensor_top_C"], 38.0)
                self.assertFalse(notes.is_due())

    def test_without_the_sensor_no_answer_stores_nothing(self) -> None:
        with TemporaryDirectory() as temporary:
            notes = thermal_prompt.ThermalNotes(
                csv_path=Path(temporary) / "thermal.csv", plan=[(0.0, 5.0)], timeout_sec=60.0,
                sound_on_wall_sec=SOUND_ON_SEC, clock=FakeClock(SOUND_ON_SEC), reader=lambda *_: None,
            )
            with mock.patch("builtins.print"):
                self.assertIsNone(notes.ask("start", 1, "kx", None))


class AutoEntryTests(unittest.TestCase):
    def run_two(self, root: Path, log: Path) -> tuple[list[Path], dict]:
        export_dir = write_export(root)
        output_root = root / "rec"
        clock = FakeClock(SOUND_ON_SEC + 20.0)
        run_dirs = []

        def record(*_args, **_kwargs):
            run_dir = output_root / f"run_{len(run_dirs)}"
            run_dir.mkdir(parents=True)
            (run_dir / "pipeline_manifest.json").write_text("{}", encoding="utf-8")
            run_dirs.append(run_dir)
            clock.now += 60.0
            return 0, run_dir

        events = mock.Mock()
        events.open_hw.return_value = SimpleNamespace(current_pos=(0.0, 0.0, 0.0),
                                                      active_output_started_wall_ns=int(SOUND_ON_SEC * 1e9))
        events.precompute.return_value = SimpleNamespace(name="first")
        argv = [
            "--hf-export-dir", str(export_dir), "--output-dir", str(output_root),
            "--hf-run", "kx=1", "--hf-run", "ky=1",
            "--acknowledge-step-response-risk", "--unattended-after-first-checkpoint",
            "--pat-temp-log", str(log),
        ]
        with mock.patch.object(auto_record, "open_recording_hardware_session", events.open_hw), \
                mock.patch.object(auto_record, "shutdown_recording_hardware_session", events.close_hw), \
                mock.patch.object(auto_record, "precompute_hologram_playback", events.precompute), \
                mock.patch.object(auto_record, "run_recording", side_effect=record), \
                mock.patch.object(auto_record, "time", clock), \
                mock.patch("builtins.print"):
            self.assertEqual(auto_record.main(argv), 0)
        session_json = json.loads(next(output_root.glob("auto_recording_session_*.json")).read_text(encoding="utf-8"))
        return run_dirs, session_json

    def test_a_restart_during_a_run_is_listed_in_the_session(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "pat_temp_log_x.csv"
            # runs cover sound-on +20..+80 s and +80..+140 s; the Arduino restarts at +110 s
            write_pat_log(log, SOUND_ON_SEC - 100.0, 600, restart_at=210)
            run_dirs, session_json = self.run_two(root, log)
            manifests = [json.loads((d / "pipeline_manifest.json").read_text(encoding="utf-8")) for d in run_dirs]
            self.assertEqual([m["pat_temperature"]["interrupted"] for m in manifests], [False, True])
            listed = session_json["pat_temp_interrupted_runs"]
            self.assertEqual([(item["run_number"], item["label"]) for item in listed], [(2, "ky")])
            self.assertEqual(len(listed[0]["arduino_restarts"]), 1)

    def test_each_run_gets_its_pat_temperature_rows_and_summary(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = write_export(root)
            log = root / "pat_temp_log_x.csv"
            write_pat_log(log, SOUND_ON_SEC - 100.0, 600)
            output_root = root / "rec"
            clock = FakeClock(SOUND_ON_SEC + 20.0)
            run_dirs = []

            def record(*_args, **_kwargs):
                run_dir = output_root / f"run_{len(run_dirs)}"
                run_dir.mkdir(parents=True)
                (run_dir / "pipeline_manifest.json").write_text("{}", encoding="utf-8")
                run_dirs.append(run_dir)
                clock.now += 60.0
                return 0, run_dir

            events = mock.Mock()
            session = SimpleNamespace(current_pos=(0.0, 0.0, 0.0),
                                      active_output_started_wall_ns=int(SOUND_ON_SEC * 1e9))
            events.open_hw.return_value = session
            events.precompute.return_value = SimpleNamespace(name="first")
            argv = [
                "--hf-export-dir", str(export_dir), "--output-dir", str(output_root),
                "--hf-run", "kx=1", "--hf-run", "ky=1",
                "--acknowledge-step-response-risk", "--unattended-after-first-checkpoint",
                "--pat-temp-log", str(log),
            ]
            with mock.patch.object(auto_record, "open_recording_hardware_session", events.open_hw), \
                    mock.patch.object(auto_record, "shutdown_recording_hardware_session", events.close_hw), \
                    mock.patch.object(auto_record, "precompute_hologram_playback", events.precompute), \
                    mock.patch.object(auto_record, "run_recording", side_effect=record), \
                    mock.patch.object(auto_record, "time", clock):
                self.assertEqual(auto_record.main(argv), 0)
            self.assertEqual(len(run_dirs), 2)
            for run_dir in run_dirs:
                manifest = json.loads((run_dir / "pipeline_manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["pat_temperature"]["top_mean_C"], 38.0)
                self.assertFalse(manifest["pat_temperature"]["stale"])
                self.assertTrue((run_dir / "pat_temperature_log.csv").is_file())
            session_json = json.loads(next(output_root.glob("auto_recording_session_*.json")).read_text(encoding="utf-8"))
            self.assertEqual(session_json["pat_temp_log"], str(log.resolve()))
            self.assertTrue(all("pat_temperature" in run for run in session_json["runs"]))


if __name__ == "__main__":
    unittest.main()
