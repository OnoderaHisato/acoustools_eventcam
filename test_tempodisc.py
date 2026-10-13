"""Hardware-free tests for the Tempo Disc environment log (tempodisc_ble.py, tempodisc_logger.py,
env_run_link.py, the sensor columns of thermal_prompt.py and --env-log in the auto entry).

No Bluetooth is used: advertisements are fed as bytes (the packets were recorded from the lab
sensor F9EE9FFC on 2026-10-13).
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import NamedTuple
from unittest import mock
import unittest

import acoustools_stereo_eventcam_3d_recording_auto as auto_record
import env_run_link
import tempodisc_ble as ble
import tempodisc_logger
import thermal_prompt
from test_thermal_hold_test import SOUND_ON_SEC, FakeClock, write_export

MAIN = bytes.fromhex("1b640e10177000f0022427710100")       # v27, 100 %, 3600 s, 6000, 24.0 C, 54.8 %, 1009.7 hPa
SCAN = bytes.fromhex("277503490000000001520225000000000018002a0000000000")
ADDRESS = "F9:EE:9F:FC:F9:75"


class DecodeTests(unittest.TestCase):
    def test_the_lab_sensor_packet(self) -> None:
        reading = ble.decode_main_packet(MAIN, received_unix_s=5.0, address=ADDRESS, name="F9EE9FFC", rssi=-63)
        self.assertEqual((reading.version, reading.model), (27, "Tempo Disc THPD"))
        self.assertEqual((reading.battery_percent, reading.log_interval_s, reading.log_count), (100, 3600, 6000))
        self.assertEqual((reading.temperature_C, reading.humidity_percent, reading.pressure_hPa), (24.0, 54.8, 1009.7))
        self.assertAlmostEqual(reading.dew_point_C, 14.33, delta=0.1)

    def test_negative_temperature_and_the_scan_response_is_not_a_reading(self) -> None:
        cold = bytearray(MAIN)
        cold[6:8] = (-123).to_bytes(2, "big", signed=True)
        self.assertEqual(ble.decode_main_packet(bytes(cold)).temperature_C, -12.3)
        self.assertIsNone(ble.decode_main_packet(SCAN))
        self.assertIsNone(ble.decode_main_packet(MAIN[:8]))

    def test_dew_point(self) -> None:
        self.assertAlmostEqual(ble.dew_point_c(25.0, 60.0), 16.7, delta=0.1)
        self.assertAlmostEqual(ble.dew_point_c(20.0, 100.0), 20.0, delta=0.01)
        self.assertIsNone(ble.dew_point_c(20.0, 0.0))

    def test_device_matching(self) -> None:
        self.assertTrue(ble.matches_device("F9EE9FFC", ADDRESS, None))
        self.assertTrue(ble.matches_device("f9:ee:9f:fc", ADDRESS, ""))
        self.assertTrue(ble.matches_device("", "00:11:22:33:44:55", None))
        self.assertFalse(ble.matches_device("F9EE9FFC", "00:11:22:33:44:55", "other"))


class RawAdv(NamedTuple):
    adv: object
    scan: object


def winrt_event(data: bytes, rssi: int = -60):
    section = SimpleNamespace(company_id=ble.BLUE_MAESTRO_COMPANY_ID, data=data)
    return SimpleNamespace(advertisement=SimpleNamespace(manufacturer_data=[section]), raw_signal_strength_in_dbm=rssi)


class ListenerTests(unittest.TestCase):
    def test_the_scan_response_is_kept_and_attached(self) -> None:
        got = []
        listener = ble.TempoListener("F9EE9FFC", on_reading=got.append)
        self.assertIsNone(listener.handle(ADDRESS, "F9EE9FFC", -60, {ble.BLUE_MAESTRO_COMPANY_ID: SCAN}))
        reading = listener.handle(ADDRESS, "F9EE9FFC", -60, {ble.BLUE_MAESTRO_COMPANY_ID: MAIN})
        self.assertEqual(reading.scan_response_hex, SCAN.hex())
        self.assertEqual(len(got), 1)
        self.assertIsNone(listener.handle("00:00:00:00:00:01", "x", -60, {ble.BLUE_MAESTRO_COMPANY_ID: MAIN}))

    def test_windows_merged_packets_are_read_separately(self) -> None:
        """bleak on Windows hands the advertisement and the scan response together, both under 0x0133.

        The merged dict holds only the scan response; the reading must come from the advertisement
        event, once per new advertisement (a scan response alone repeats the old advertisement event).
        """
        got = []
        listener = ble.TempoListener("F9EE9FFC", on_reading=got.append)
        device = SimpleNamespace(address=ADDRESS, name="F9EE9FFC")
        first = winrt_event(MAIN)
        scan = winrt_event(SCAN)

        def callback(raw):
            merged = {ble.BLUE_MAESTRO_COMPANY_ID: SCAN if raw.scan is not None else MAIN}
            listener._callback(device, SimpleNamespace(local_name="F9EE9FFC", rssi=-60,
                                                       manufacturer_data=merged, platform_data=(None, raw)))

        callback(RawAdv(first, None))
        callback(RawAdv(first, scan))      # scan response: same advertisement event, no new reading
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].temperature_C, 24.0)
        newer = bytearray(MAIN)
        newer[6:8] = (239).to_bytes(2, "big")
        callback(RawAdv(winrt_event(bytes(newer)), scan))
        self.assertEqual([r.temperature_C for r in got], [24.0, 23.9])
        self.assertEqual(got[1].scan_response_hex, SCAN.hex())


class SinkTests(unittest.TestCase):
    def test_changes_repeats_and_silence(self) -> None:
        with TemporaryDirectory() as temporary:
            clock = FakeClock(1000.0)
            path = Path(temporary) / "env.csv"
            sink = tempodisc_logger.CsvSink(path, interval_s=10.0, stale_s=60.0, clock=clock)
            reading = ble.decode_main_packet(MAIN, received_unix_s=1000.0, address=ADDRESS)
            sink.on_reading(reading)           # new -> row
            sink.on_reading(reading)           # same -> no row
            clock.now = 1005.0
            sink.tick()                        # < interval -> no row
            clock.now = 1010.5
            sink.tick()                        # interval -> repeated row
            clock.now = 1075.0
            sink.tick()                        # 75 s silence -> error row
            sink.close()
            with path.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 3)
            self.assertEqual([r["temperature_C"] for r in rows[:2]], ["24.0", "24.0"])
            self.assertEqual(rows[1]["received_unix_s"], "1000.000")
            self.assertIn("no advertisement", rows[2]["error"])

    def test_bad_arguments(self) -> None:
        for argv in ([], ["--output", "x.csv", "--interval", "-1"], ["--once", "--timeout", "0"]):
            with self.subTest(argv=argv), mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                tempodisc_logger.parse_args(argv)


def write_env_log(path: Path, start: float, seconds: int, step: int = 10, temp=lambda k: 24.0, gap=()) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tempodisc_logger.CSV_COLUMNS)
        writer.writeheader()
        for k in range(0, seconds, step):
            t = start + k
            if any(a <= k < b for a, b in gap):
                continue
            reading = ble.decode_main_packet(MAIN, received_unix_s=t, address=ADDRESS)
            reading.temperature_C = temp(k)
            writer.writerow(tempodisc_logger.reading_row(reading, t))


class RunLinkTests(unittest.TestCase):
    def test_rows_around_a_run_go_into_the_run_folder(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "env_log.csv"
            write_env_log(log, 1000.0, 600, temp=lambda k: 24.0 + k / 1000.0)
            run_dir = root / "run"
            run_dir.mkdir()
            summary = env_run_link.attach_environment_record(run_dir, log, 1200.0, 1260.0)
            with (run_dir / "environment_log.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 19)            # 1140 .. 1320 every 10 s with the 60 s margin
            self.assertEqual(summary["pressure_hPa_mean"], 1009.7)
            self.assertAlmostEqual(summary["temperature_C_min"], 24.1, places=3)
            self.assertFalse(summary["stale"])

    def test_silence_is_stale_and_a_missing_log_never_raises(self) -> None:
        with TemporaryDirectory() as temporary:
            log = Path(temporary) / "env.csv"
            write_env_log(log, 1000.0, 1200, gap=[(300, 1200)])
            summary = env_run_link.attach_environment_record(None, log, 1500.0, 1700.0)
            self.assertTrue(summary["stale"])
            self.assertIsNone(env_run_link.latest_reading(log, 1700.0))
            self.assertEqual(env_run_link.latest_reading(log, 1300.0)["age_s"], 10.0)
            self.assertIn("error", env_run_link.attach_environment_record(None, Path(temporary) / "none.csv", 0.0, 1.0))


class ThermalNoteTests(unittest.TestCase):
    def test_a_note_carries_the_latest_sensor_reading(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "env.csv"
            write_env_log(log, SOUND_ON_SEC - 100.0, 400)
            clock = FakeClock(SOUND_ON_SEC + 155.0)
            notes = thermal_prompt.ThermalNotes(
                csv_path=root / "thermal.csv", plan=[(0.0, 5.0)], timeout_sec=60.0,
                sound_on_wall_sec=SOUND_ON_SEC, clock=clock, env_log=log,
                reader=lambda *_: "41.2 39.8",
            )
            with mock.patch("builtins.print"):
                record = notes.ask("scheduled", 3, "kcheck_x", None)
            self.assertEqual((record["T_top_PAT_C"], record["T_room_C"]), (41.2, None))
            self.assertEqual((record["sensor_T_C"], record["sensor_RH_percent"], record["sensor_pressure_hPa"]),
                             (24.0, 54.8, 1009.7))
            self.assertEqual(record["sensor_age_s"], 5.0)   # the row at +150 s
            with (root / "thermal.csv").open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["sensor_pressure_hPa"], "1009.7")

    def test_without_the_sensor_the_columns_stay_empty(self) -> None:
        with TemporaryDirectory() as temporary:
            notes = thermal_prompt.ThermalNotes(
                csv_path=Path(temporary) / "thermal.csv", plan=[(0.0, 5.0)], timeout_sec=60.0,
                sound_on_wall_sec=SOUND_ON_SEC, clock=FakeClock(SOUND_ON_SEC), reader=lambda *_: "41 40 24 50",
            )
            with mock.patch("builtins.print"):
                record = notes.ask("start", 1, "kx", None)
            self.assertNotIn("sensor_T_C", record)
            with (Path(temporary) / "thermal.csv").open(encoding="utf-8-sig", newline="") as handle:
                self.assertEqual(next(csv.DictReader(handle))["sensor_T_C"], "")


class AutoEntryTests(unittest.TestCase):
    def test_each_run_gets_its_environment_rows_and_summary(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = write_export(root)
            log = root / "env_log_x.csv"
            write_env_log(log, SOUND_ON_SEC - 100.0, 600)
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
                "--env-log", str(log),
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
                self.assertEqual(manifest["environment"]["pressure_hPa_mean"], 1009.7)
                self.assertFalse(manifest["environment"]["stale"])
                self.assertTrue((run_dir / "environment_log.csv").is_file())
            session_json = json.loads(next(output_root.glob("auto_recording_session_*.json")).read_text(encoding="utf-8"))
            self.assertEqual(session_json["env_log"], str(log.resolve()))
            self.assertEqual(session_json["psu_log"], "")
            self.assertTrue(all("environment" in run for run in session_json["runs"]))


if __name__ == "__main__":
    unittest.main()
