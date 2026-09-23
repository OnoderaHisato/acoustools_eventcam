"""Hardware-free tests for pwr01_logger.py (Kikusui PWR801L supply logger) and the thermal-log merge.

A fake PWR-01 answers the SCPI-RAW protocol on localhost, so the real socket transport, the
read-only command list, the watchdog refusal and the CSV output are all exercised.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import socket
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock
import unittest

import pwr01_logger as psu
import thermal_log_prefill


class FakePwr01:
    """Minimal SCPI-RAW server that behaves like a PWR801L at 15 V / 4.0 A."""

    def __init__(self, watchdog: int = 0) -> None:
        self.watchdog = watchdog
        self.received: list[str] = []
        self.output_on = True
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(4)
        self.port = self._server.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _answer(self, command: str) -> str | None:
        replies = {
            "*IDN?": "KIKUSUI,PWR801L,FAKE0001,1.00",
            "MEAS:ALL?": "+4.0123E+00,+1.5002E+01",
            "OUTP?": "1" if self.output_on else "0",
            "VOLT?": "+1.5000E+01",
            "CURR?": "+5.3000E+01",
            "OUTP:PROT:WDOG?": str(self.watchdog),
            "SYST:ERR?": '0,"No error"',
        }
        return replies.get(command)

    def _serve(self) -> None:
        self._server.settimeout(0.2)
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return  # the listening socket was closed
            threading.Thread(target=self._client, args=(connection,), daemon=True).start()

    def _client(self, connection: socket.socket) -> None:
        buffer = b""
        connection.settimeout(0.2)
        with connection:
            while not self._stop.is_set():
                try:
                    chunk = connection.recv(1024)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    command = line.decode().strip()
                    self.received.append(command)
                    reply = self._answer(command)
                    if reply is not None:
                        connection.sendall(reply.encode() + b"\n")

    def close(self) -> None:
        self._stop.set()
        self._server.close()


class ParsingTests(unittest.TestCase):
    def test_measurement_reply_is_current_then_voltage(self) -> None:
        self.assertEqual(psu.parse_measurement("+4.0E+00,+1.5E+01"), (15.0, 4.0))
        with self.assertRaises(ValueError):
            psu.parse_measurement("4.0")

    def test_only_read_only_commands_can_be_sent(self) -> None:
        client = psu.Pwr01Client(transport=mock.Mock())
        for command in ("VOLT 18", "OUTP OFF", "OUTP:PROT:WDOG 30", "*RST", "SYST:COMM:RLST REM"):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, "read-only"):
                client._send(command)
        client.transport.write.assert_not_called()
        self.assertNotIn("VOLT", {c.split()[0] for c in psu.READ_ONLY_COMMANDS if " " in c})


class FakeSupplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakePwr01()

    def tearDown(self) -> None:
        self.fake.close()

    def test_once_reads_one_sample_and_hands_the_panel_back(self) -> None:
        code = psu.main(["--host", "127.0.0.1", "--port", str(self.fake.port), "--once"])
        self.assertEqual(code, 0)
        sent = set(self.fake.received)
        self.assertTrue(sent <= psu.READ_ONLY_COMMANDS, sent - psu.READ_ONLY_COMMANDS)
        self.assertIn("MEAS:ALL?", sent)
        self.assertEqual(self.fake.received[-1], "SYST:COMM:RLST LOC")

    def test_logging_writes_csv_until_the_stop_file_appears(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            output, stop = root / "psu_log_test.csv", root / "psu.stop"
            args = ["--host", "127.0.0.1", "--port", str(self.fake.port), "--output", str(output),
                    "--interval", "0.1", "--settings-interval", "0.2", "--stop-file", str(stop)]
            worker = threading.Thread(target=psu.main, args=(args,))
            worker.start()
            time.sleep(0.8)
            stop.touch()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            with output.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertGreaterEqual(len(rows), 4)
            self.assertEqual(rows[0]["voltage_V"], "15.0020")
            self.assertEqual(rows[0]["current_A"], "4.0123")
            self.assertEqual(rows[0]["output_on"], "1")
            self.assertEqual(rows[0]["voltage_set_V"], "15.0000")
            meta = json.loads((root / "psu_log_test.csv.meta.json").read_text(encoding="utf-8"))
            self.assertIn("PWR801L", meta["instrument"])
            self.assertTrue(set(self.fake.received) <= psu.READ_ONLY_COMMANDS)

    def test_an_enabled_watchdog_stops_the_logger_before_it_starts(self) -> None:
        self.fake.close()
        self.fake = FakePwr01(watchdog=30)
        code = psu.main(["--host", "127.0.0.1", "--port", str(self.fake.port), "--once"])
        self.assertEqual(code, 2)
        self.assertNotIn("MEAS:ALL?", self.fake.received)


class UsbDiscoveryTests(unittest.TestCase):
    def test_only_kikusui_pwr01_resources_are_picked(self) -> None:
        manager = mock.Mock()
        manager.list_resources.return_value = (
            "USB0::0x0B3E::0x104A::AB123456::INSTR",   # PWR801L
            "USB0::0x0957::0x1796::MY999::INSTR",       # someone else's instrument
            "USB0::0x0B3E::0x9999::X1::INSTR",          # Kikusui, not a PWR-01
        )
        fake_pyvisa = SimpleNamespace(ResourceManager=lambda: manager)
        with mock.patch.object(psu, "import_pyvisa", return_value=fake_pyvisa):
            self.assertEqual(psu.find_usb_resources(), ["USB0::0x0B3E::0x104A::AB123456::INSTR"])

    def test_missing_visa_library_gives_an_actionable_error(self) -> None:
        def broken_manager():
            raise ValueError("Could not locate a VISA implementation")

        fake_pyvisa = SimpleNamespace(ResourceManager=broken_manager)
        with mock.patch.object(psu, "import_pyvisa", return_value=fake_pyvisa), \
                self.assertRaisesRegex(RuntimeError, "KI-VISA"):
            psu.find_usb_resources()


class KiVisaSessionWorkaroundTests(unittest.TestCase):
    """KI-VISA 5.5.0.275 (x64) cannot use session numbers >= 2^31 (found on 2026-09-24)."""

    def _fake_pyvisa(self, manager_session: int, instrument_session: int = 1234):
        instrument = mock.Mock(session=instrument_session)
        manager = mock.Mock(session=manager_session)
        manager.open_resource.return_value = instrument
        return SimpleNamespace(ResourceManager=lambda: manager), instrument

    def test_a_high_session_number_is_refused_before_any_command(self) -> None:
        for manager_session, instrument_session in ((2 ** 31, 1234), (1234, 3_000_000_000)):
            fake, instrument = self._fake_pyvisa(manager_session, instrument_session)
            transport = psu.VisaTransport("USB0::0x0B3E::0x104A::X::0::INSTR")
            with self.subTest(manager=manager_session, instrument=instrument_session), \
                    mock.patch.object(psu, "import_pyvisa", return_value=fake), \
                    self.assertRaises(psu.HighSessionHandle):
                transport.open()
            instrument.write.assert_not_called()

    def test_a_usable_session_opens_normally(self) -> None:
        fake, instrument = self._fake_pyvisa(723_068_552, 723_070_000)
        transport = psu.VisaTransport("USB0::0x0B3E::0x104A::X::0::INSTR")
        with mock.patch.object(psu, "import_pyvisa", return_value=fake):
            transport.open()
        self.assertEqual(instrument.read_termination, "\n")

    def test_usb_runs_are_retried_in_fresh_processes(self) -> None:
        codes = iter([psu.HIGH_SESSION_EXIT_CODE, psu.HIGH_SESSION_EXIT_CODE, 0])
        launched = []

        def popen(command):
            launched.append(command)
            return mock.Mock(wait=mock.Mock(return_value=next(codes)))

        with mock.patch("subprocess.Popen", side_effect=popen), mock.patch.object(psu.time, "sleep"):
            self.assertEqual(psu.main(["--usb", "--once"]), 0)
        self.assertEqual(len(launched), 3)
        self.assertTrue(all(command[-1] == "--visa-child" for command in launched))

    def test_other_failures_are_not_retried(self) -> None:
        with mock.patch("subprocess.Popen", return_value=mock.Mock(wait=mock.Mock(return_value=2))) as popen:
            self.assertEqual(psu.main(["--usb", "--once"]), 2)
        self.assertEqual(popen.call_count, 1)

    def test_lan_never_spawns_a_child(self) -> None:
        with mock.patch("subprocess.Popen") as popen, \
                mock.patch.object(psu, "run_logger", return_value=0):
            self.assertEqual(psu.main(["--host", "127.0.0.1", "--once"]), 0)
        popen.assert_not_called()


class ThermalLogMergeTests(unittest.TestCase):
    def test_supply_columns_come_from_the_log(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            sound_on = dt.datetime(2026, 9, 24, 20, 0, 0)
            run_start = sound_on + dt.timedelta(minutes=5)
            finished = run_start + dt.timedelta(seconds=40)
            session = {
                "active_output_started_wall_ns": int(sound_on.timestamp() * 1e9),
                "supply_voltage_V": 15.0,
                "runs": [{
                    "exit_code": 0,
                    "run_dir": str(root / f"step_response_identification_000_kcheck_x_S105_6jumps_scale100_V15_{run_start:%Y%m%d_%H%M%S}"),
                    "finished_at": finished.isoformat(timespec="milliseconds"),
                }],
            }
            session_path = root / f"auto_recording_session_{sound_on:%Y%m%d_%H%M%S}.json"
            session_path.write_text(json.dumps(session), encoding="utf-8")
            log = root / f"psu_log_{sound_on:%Y%m%d_%H%M%S}.csv"
            with log.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=psu.CSV_COLUMNS)
                writer.writeheader()
                t = sound_on - dt.timedelta(seconds=30)
                while t <= finished + dt.timedelta(seconds=30):
                    on = t >= sound_on
                    current = 4.05 if t < run_start else 4.00
                    writer.writerow({
                        "time_iso": t.isoformat(), "time_unix_s": f"{t.timestamp():.3f}",
                        "voltage_V": "15.0000", "current_A": f"{current if on else 0.9:.4f}",
                        "power_W": "", "output_on": "1", "voltage_set_V": "", "current_set_A": "",
                        "error": "",
                    })
                    t += dt.timedelta(seconds=1)
            self.assertEqual(thermal_log_prefill.main([str(root)]), 0)
            with (root / f"thermal_log_{sound_on:%Y%m%d_%H%M%S}.csv").open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["supply_V"], "15.000")
            self.assertEqual(row["supply_A"], "4.000")
            self.assertIn("current right after sound on 4.050 A", row["note"])
            self.assertIn("PSU mean of 41 reading(s)", row["note"])


if __name__ == "__main__":
    unittest.main()
