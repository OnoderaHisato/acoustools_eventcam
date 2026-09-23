#!/usr/bin/env python3
"""Read-only logger for the Kikusui PWR-01 series DC supply (PWR801L), over USB or LAN.

Records the measured output voltage and current of the PAT supply to a CSV file while a
measurement session runs, so that the thermal log no longer depends on reading the panel.

    python pwr01_logger.py --usb --once                           # find the PWR801L on USB, one reading
    python pwr01_logger.py --usb --output psu.csv --interval 1
    python pwr01_logger.py --usb --output psu.csv --stop-file psu.stop   # stops when the file appears
    python pwr01_logger.py --resource USB0::0x0B3E::0x104A::<serial>::INSTR --once
    python pwr01_logger.py --host 192.168.98.50 --once            # LAN (SCPI-RAW, port 5025)

USB needs a VISA library (KI-VISA, NI-VISA or Keysight VISA); it installs the USBTMC driver
(PWR-01 Interface Manual p.23), and the Python package pyvisa talks to it. The PWR801L
shows up as USB0::0x0B3E::0x104A::<serial>::INSTR. LAN needs nothing extra.

Protocol: MEAS:ALL? returns "<current>,<voltage>" in NR3; the supply updates voltage and
current alternately every 25 ms. Commands are LF terminated.

Safety: the logger never changes the supply. Only the queries in READ_ONLY_COMMANDS are sent,
plus SYST:COMM:RLST LOC, which hands the front panel back after each reading (a remote query
puts the PWR-01 into remote mode, which locks every key except LOCAL). It refuses to start
when the communication watchdog (OUTP:PROT:WDOG) is enabled, because a watchdog turns the
output off when polling stops.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import socket
import sys
import time
from pathlib import Path

DEFAULT_PORT = 5025
KIKUSUI_VID = 0x0B3E
PWR01_PIDS = {0x1049: "PWR-01 400 W", 0x104A: "PWR-01 800 W (PWR801L)",
              0x104B: "PWR-01 1200 W", 0x1055: "PWR-01 2000 W"}
# Everything the logger may send. Nothing here changes the output.
READ_ONLY_COMMANDS = frozenset({
    "*IDN?",
    "MEAS:ALL?",
    "VOLT?",
    "CURR?",
    "OUTP?",
    "OUTP:PROT:WDOG?",
    "SYST:ERR?",
    "SYST:COMM:RLST LOC",
})
CSV_COLUMNS = [
    "time_iso", "time_unix_s", "voltage_V", "current_A", "power_W",
    "output_on", "voltage_set_V", "current_set_A", "error",
]


class SocketTransport:
    """SCPI-RAW over TCP (LAN)."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout_s: float = 3.0) -> None:
        self.host, self.port, self.timeout_s = host, int(port), float(timeout_s)
        self._sock: socket.socket | None = None
        self._buffer = b""

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def open(self) -> None:
        self.close()
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
        self._sock.settimeout(self.timeout_s)
        self._buffer = b""

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def write(self, command: str) -> None:
        if self._sock is None:
            self.open()
        assert self._sock is not None
        self._sock.sendall(command.encode("ascii") + b"\n")

    def read(self) -> str:
        assert self._sock is not None
        while b"\n" not in self._buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("the supply closed the connection")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line.decode("ascii", errors="replace").strip()


class HighSessionHandle(RuntimeError):
    """The VISA library handed out a session number it cannot use in this process."""


# KI-VISA 5.5.0.275 (x64) fails with VI_ERROR_INV_OBJECT whenever a session number is 2^31 or
# larger (checked on 2026-09-24: 100 % of such sessions fail, 100 % of smaller ones work, with
# the router and with kivisa32.dll directly). The number is fixed for the life of a process and
# random between processes, so a VISA logger restarts itself in a fresh process until it gets a
# usable one (see main()).
VISA_SESSION_LIMIT = 2 ** 31
HIGH_SESSION_EXIT_CODE = 75
VISA_ATTEMPTS = 20


class VisaTransport:
    """USBTMC (or any VISA resource) through pyvisa and an installed VISA library."""

    def __init__(self, resource: str, timeout_s: float = 3.0) -> None:
        self.resource, self.timeout_s = resource, float(timeout_s)
        self._manager = None
        self._instrument = None

    @property
    def address(self) -> str:
        return self.resource

    def open(self) -> None:
        self.close()
        pyvisa = import_pyvisa()
        self._manager = pyvisa.ResourceManager()
        if int(self._manager.session) >= VISA_SESSION_LIMIT:
            raise HighSessionHandle(f"resource manager session {self._manager.session}")
        instrument = self._manager.open_resource(self.resource)
        if int(instrument.session) >= VISA_SESSION_LIMIT:
            instrument.close()
            raise HighSessionHandle(f"instrument session {instrument.session}")
        instrument.timeout = int(self.timeout_s * 1000)
        instrument.read_termination = "\n"
        instrument.write_termination = "\n"
        self._instrument = instrument

    def close(self) -> None:
        if self._instrument is not None:
            try:
                self._instrument.close()
            finally:
                self._instrument = None

    def write(self, command: str) -> None:
        if self._instrument is None:
            self.open()
        self._instrument.write(command)

    def read(self) -> str:
        return str(self._instrument.read()).strip()


def load_windows_visa_environment() -> list[str]:
    """Copy the VISA variables the installer put in the machine environment into this process.

    A VISA library installed after the parent program (a terminal, the Claude app) started is
    invisible to it until that program restarts: VXIPNPPATH and the IVI/VISA PATH entries are
    missing, and the VISA resource manager then fails with VI_ERROR_INV_OBJECT. Reading the
    machine environment from the registry fixes that without a restart. Returns what was added.
    """
    if sys.platform != "win32":
        return []
    import os  # noqa: PLC0415
    import winreg  # noqa: PLC0415

    added: list[str] = []
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
        )
    except OSError:
        return added
    with key:
        def value(name: str) -> str | None:
            try:
                return str(winreg.QueryValueEx(key, name)[0])
            except OSError:
                return None

        for name in ("VXIPNPPATH", "VXIPNPPATH64", "IVIROOTDIR32", "IVIROOTDIR64"):
            machine = value(name)
            if machine and not os.environ.get(name):
                os.environ[name] = machine
                added.append(name)
        current = os.environ.get("PATH", "").lower().split(";")
        missing = [
            entry for entry in (value("Path") or "").split(";")
            if entry and any(tag in entry for tag in ("IVI Foundation", "VISA"))
            and entry.lower() not in current
        ]
        if missing:
            os.environ["PATH"] = ";".join(missing) + ";" + os.environ.get("PATH", "")
            added.append("PATH")
    return added


def import_pyvisa():
    load_windows_visa_environment()
    try:
        import pyvisa  # noqa: PLC0415 - optional dependency, only needed for USB
    except ImportError as exc:
        raise RuntimeError(
            "USB needs the Python package pyvisa (.\\venv\\Scripts\\python.exe -m pip install pyvisa) "
            "and a VISA library such as KI-VISA or NI-VISA, which installs the USBTMC driver."
        ) from exc
    return pyvisa


def find_usb_resources() -> list[str]:
    """VISA resource strings of every Kikusui PWR-01 on USB."""
    pyvisa = import_pyvisa()
    try:
        manager = pyvisa.ResourceManager()
        session = getattr(manager, "session", 0)
        if isinstance(session, int) and session >= VISA_SESSION_LIMIT:
            raise HighSessionHandle(f"resource manager session {session}")
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            "No VISA library was found. Install KI-VISA (Kikusui), NI-VISA or Keysight VISA; "
            "it also installs the USBTMC driver the PWR801L needs on USB."
        ) from exc
    try:
        resources = manager.list_resources("USB?*::INSTR")
    except Exception as exc:  # noqa: BLE001 - pyvisa raises VisaIOError, including "nothing found"
        if "RSRC_NFOUND" in str(exc):
            return []
        raise RuntimeError(
            f"The VISA library could not list USB instruments ({exc}). If it was installed just "
            "now, restart this terminal or the PC so that its settings are loaded."
        ) from exc
    found = []
    for resource in resources:
        parts = resource.upper().split("::")
        if len(parts) >= 3 and parts[1] in {f"0X{KIKUSUI_VID:04X}", str(KIKUSUI_VID)}:
            pid = int(parts[2], 0)
            if pid in PWR01_PIDS:
                found.append(resource)
    return found


class Pwr01Client:
    """Sends only the read-only commands above, over any transport with write/read."""

    def __init__(self, transport) -> None:
        self.transport = transport

    def connect(self) -> None:
        self.transport.open()

    def close(self) -> None:
        self.transport.close()

    def _send(self, command: str) -> None:
        if command not in READ_ONLY_COMMANDS:
            raise ValueError(f"refusing to send a command outside the read-only list: {command!r}")
        self.transport.write(command)

    def query(self, command: str) -> str:
        if not command.endswith("?"):
            raise ValueError(f"not a query: {command!r}")
        self._send(command)
        return self.transport.read()

    def release_panel(self) -> None:
        """Return the front panel to local operation (no reply is expected)."""
        self._send("SYST:COMM:RLST LOC")


def parse_measurement(reply: str) -> tuple[float, float]:
    """MEAS:ALL? -> (voltage_V, current_A). The supply answers current first, then voltage."""
    parts = [part.strip() for part in reply.split(",")]
    if len(parts) != 2:
        raise ValueError(f"unexpected MEAS:ALL? reply: {reply!r}")
    current, voltage = float(parts[0]), float(parts[1])
    return voltage, current


def parse_bool(reply: str) -> bool:
    value = reply.strip().upper()
    if value in {"1", "ON"}:
        return True
    if value in {"0", "OFF"}:
        return False
    raise ValueError(f"unexpected boolean reply: {reply!r}")


def read_sample(client: Pwr01Client, include_settings: bool = True) -> dict:
    now = time.time()
    voltage, current = parse_measurement(client.query("MEAS:ALL?"))
    sample = {
        "time_iso": dt.datetime.fromtimestamp(now).isoformat(timespec="milliseconds"),
        "time_unix_s": f"{now:.3f}",
        "voltage_V": f"{voltage:.4f}",
        "current_A": f"{current:.4f}",
        "power_W": f"{voltage * current:.3f}",
        "output_on": "",
        "voltage_set_V": "",
        "current_set_A": "",
        "error": "",
    }
    if include_settings:
        sample["output_on"] = "1" if parse_bool(client.query("OUTP?")) else "0"
        sample["voltage_set_V"] = f"{float(client.query('VOLT?')):.4f}"
        sample["current_set_A"] = f"{float(client.query('CURR?')):.4f}"
    return sample


def check_watchdog(client: Pwr01Client) -> int:
    """Return the watchdog delay in seconds (0 = off, the factory default)."""
    return int(float(client.query("OUTP:PROT:WDOG?")))


def make_transport(args: argparse.Namespace):
    if args.host:
        return SocketTransport(args.host, args.port, args.timeout)
    resource = args.resource
    if not resource:
        found = find_usb_resources()
        if not found:
            raise RuntimeError(
                "No Kikusui PWR-01 was found on USB (VID 0x0B3E). Check the cable, that USB is "
                "enabled on the supply (CONFIG CF41 = ON), and that the VISA library sees it."
            )
        if len(found) > 1:
            raise RuntimeError(f"More than one PWR-01 on USB; pick one with --resource: {found}")
        resource = found[0]
    return VisaTransport(resource, args.timeout)


def open_client(transport) -> tuple[Pwr01Client, str]:
    client = Pwr01Client(transport)
    client.connect()
    identity = client.query("*IDN?")
    watchdog = check_watchdog(client)
    if watchdog != 0:
        client.release_panel()
        client.close()
        raise RuntimeError(
            f"the supply's communication watchdog is set to {watchdog} s (OUTP:PROT:WDOG). "
            "The output would switch off whenever polling stops, so the logger will not run. "
            "Turn the watchdog off on the supply first."
        )
    client.release_panel()
    return client, identity


def run_logger(args: argparse.Namespace) -> int:
    transport = make_transport(args)
    client, identity = open_client(transport)
    print(f"[PSU] {identity} at {transport.address}; watchdog off", flush=True)
    if args.once:
        sample = read_sample(client)
        client.release_panel()
        client.close()
        print(
            f"[PSU] {sample['voltage_V']} V, {sample['current_A']} A, {sample['power_W']} W, "
            f"output {'ON' if sample['output_on'] == '1' else 'OFF'}, "
            f"set {sample['voltage_set_V']} V / {sample['current_set_A']} A"
        )
        return 0

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    new_file = not output.exists()
    stop_file = Path(args.stop_file) if args.stop_file else None
    deadline = time.monotonic() + args.duration if args.duration > 0 else None
    settings_every = max(1, int(round(args.settings_interval / args.interval)))
    meta_path = output.with_suffix(output.suffix + ".meta.json")
    meta_path.write_text(json.dumps({
        "instrument": identity,
        "address": transport.address,
        "interval_s": args.interval,
        "started_at": dt.datetime.now().isoformat(timespec="milliseconds"),
        "columns": CSV_COLUMNS,
        "note": "read-only: MEAS:ALL?, OUTP?, VOLT?, CURR?; the panel is returned to LOCAL after each reading",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    count = 0
    failures = 0
    with output.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        if new_file:
            writer.writeheader()
        next_time = time.monotonic()
        try:
            while True:
                if stop_file is not None and stop_file.exists():
                    print(f"[PSU] stop file found; {count} sample(s) written to {output}", flush=True)
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    print(f"[PSU] duration reached; {count} sample(s) written to {output}", flush=True)
                    break
                try:
                    sample = read_sample(client, include_settings=(count % settings_every == 0))
                    client.release_panel()
                    failures = 0
                except Exception as exc:  # noqa: BLE001 - keep logging through transient I/O errors
                    failures += 1
                    now = time.time()
                    sample = {column: "" for column in CSV_COLUMNS}
                    sample.update({
                        "time_iso": dt.datetime.fromtimestamp(now).isoformat(timespec="milliseconds"),
                        "time_unix_s": f"{now:.3f}",
                        "error": repr(exc)[:200],
                    })
                    print(f"[PSU][WARN] reading failed ({failures}): {exc}", flush=True)
                    try:
                        client.connect()
                    except Exception:  # noqa: BLE001
                        pass
                writer.writerow(sample)
                handle.flush()
                count += 1
                next_time += args.interval
                time.sleep(max(0.0, next_time - time.monotonic()))
        except KeyboardInterrupt:
            print(f"[PSU] stopped; {count} sample(s) written to {output}", flush=True)
        finally:
            try:
                client.release_panel()
            except Exception:  # noqa: BLE001
                pass
            client.close()
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--usb", action="store_true", help="find the PWR-01 on USB through VISA")
    where.add_argument("--resource", default="", help="VISA resource, e.g. USB0::0x0B3E::0x104A::<serial>::INSTR")
    where.add_argument("--host", default="", help="LAN: IP address or host name (SCPI-RAW)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="LAN SCPI-RAW port (always 5025 on the PWR-01)")
    parser.add_argument("--timeout", type=float, default=3.0, help="I/O timeout in seconds")
    parser.add_argument("--once", action="store_true", help="print one reading and exit (connection test)")
    parser.add_argument("--list", action="store_true", help="list the PWR-01 supplies VISA sees on USB and exit")
    parser.add_argument("--output", default="", help="CSV file to append to")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between readings (default 1)")
    parser.add_argument("--settings-interval", type=float, default=30.0,
                        help="seconds between reads of the output state and set points (default 30)")
    parser.add_argument("--duration", type=float, default=0.0, help="stop after this many seconds (0 = no limit)")
    parser.add_argument("--stop-file", default="", help="stop when this file appears")
    parser.add_argument("--visa-child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not (args.once or args.list) and not args.output:
        parser.error("--output is required unless --once or --list is given")
    if args.interval < 0.1:
        parser.error("--interval must be at least 0.1 s (the supply updates every 25 ms, alternating V and I)")
    return args


def run_visa_in_fresh_processes(argv: list[str]) -> int:
    """Run this script as a child until KI-VISA hands it usable session numbers."""
    import subprocess  # noqa: PLC0415

    command = [sys.executable, str(Path(__file__).resolve()), *argv, "--visa-child"]
    for attempt in range(1, VISA_ATTEMPTS + 1):
        child = subprocess.Popen(command)
        try:
            code = child.wait()
        except KeyboardInterrupt:
            code = child.wait()
        if code != HIGH_SESSION_EXIT_CODE:
            return code
        time.sleep(0.2)
    print(
        f"[PSU][ERROR] the VISA library gave an unusable session number {VISA_ATTEMPTS} times in a row",
        file=sys.stderr, flush=True,
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(raw_argv)
    if (args.usb or args.resource) and not args.visa_child:
        return run_visa_in_fresh_processes(raw_argv)
    try:
        if args.list:
            found = find_usb_resources()
            for resource in found:
                pid = int(resource.split("::")[2], 0)
                print(f"{resource}  ({PWR01_PIDS.get(pid, 'PWR-01')})")
            if not found:
                print("[PSU] no Kikusui PWR-01 found on USB")
            return 0 if found else 1
        return run_logger(args)
    except HighSessionHandle:
        if args.visa_child:
            # Leave without interpreter clean-up: pyvisa would otherwise try to close the unusable
            # session at exit and print a harmless but alarming VI_ERROR_INV_OBJECT traceback.
            sys.stdout.flush()
            sys.stderr.flush()
            import os  # noqa: PLC0415

            os._exit(HIGH_SESSION_EXIT_CODE)
        return HIGH_SESSION_EXIT_CODE
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[PSU][ERROR] {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
