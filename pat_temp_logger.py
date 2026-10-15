#!/usr/bin/env python3
"""Log the PAT board temperatures from DS18B20 sensors read by an Arduino (USB serial, read only).

    python pat_temp_logger.py --list                       # serial ports; which one would be used
    python pat_temp_logger.py --once                       # one reading of every sensor, then exit
    python pat_temp_logger.py --output pat_temp.csv        # log until Ctrl+C
    python pat_temp_logger.py --output pat_temp.csv --stop-file pat_temp.stop   # stops when the file appears

The Arduino runs ds18b20_serial_logger/ds18b20_serial_logger.ino. It reads every sensor on its 1-Wire
bus once a second and prints ``T,<cycle>,<millis>,<ROM id>,<temp C>,<status>``. This logger adds the PC
time when a line arrives, the sensor name and group from the sensor map (pat_temp_sensors.json) and a
per-sensor offset, and appends one CSV row per sensor and reading. The only thing ever sent to the
Arduino is ``s`` (rescan the bus).

**The PAT boards themselves are FTDI FT232H serial devices (VID 0x0403, PID 0x6014).** The port search
never picks an FTDI port, and an FT232H port is refused even when named with --port, so the logger can
never open a PAT controller.

When no reading arrives for --stale-s seconds, a row with only an ``error`` is written, so a gap is
visible in the log; a lost port is reopened every 5 s. Used by acoustools_stereo_eventcam_3d_recording_auto.py
(--pat-temp-log) through pat_temp_logging.ps1, the same way as the supply (pwr01_logger.py) and room
(tempodisc_logger.py) loggers.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

BAUD = 115200
SKETCH_BANNER = "# ds18b20_serial_logger"
DEFAULT_SENSOR_MAP = Path(__file__).resolve().with_name("pat_temp_sensors.json")

FTDI_VID = 0x0403
FT232H_PID = 0x6014  # the PAT controller boards
# USB-serial chips found on Arduino boards and their clones (FTDI is deliberately not here).
ARDUINO_VIDS = {
    0x2341: "Arduino",
    0x2A03: "Arduino (arduino.org)",
    0x1A86: "CH340 (Arduino clone)",
    0x10C4: "CP210x (Arduino clone)",
    0x239A: "Adafruit",
    0x2E8A: "Raspberry Pi (Pico)",
}

CSV_COLUMNS = [
    "time_iso", "time_unix_s", "port", "cycle", "arduino_ms", "rom", "name", "group",
    "temp_raw_C", "offset_C", "temp_C", "status", "error", "event",
]
# ``event`` column: port_opened (the board kept running), port_opened_reset (it restarted when the port was
# opened), port_lost (the port failed; the next open follows). Readings have no event.
EVENT_OPENED = "port_opened"
EVENT_OPENED_RESET = "port_opened_reset"
EVENT_LOST = "port_lost"


# --------------------------------------------------------------------------------------------- lines


def parse_line(text: str) -> dict[str, Any] | None:
    """One line from the sketch -> a dict (``kind`` = reading / banner / sensors / rom / parasite / info); None if unknown."""
    line = text.strip()
    if not line:
        return None
    if line.startswith("T,"):
        parts = line.split(",")
        if len(parts) != 6:
            return None
        try:
            cycle, millis = int(parts[1]), int(parts[2])
            temperature = float(parts[4])
        except ValueError:
            return None
        rom = parts[3].strip().upper()
        if len(rom) != 16 or any(ch not in "0123456789ABCDEF" for ch in rom):
            return None
        return {"kind": "reading", "cycle": cycle, "arduino_ms": millis, "rom": rom,
                "temp_raw_C": temperature, "status": parts[5].strip() or "ok"}
    if line.startswith(SKETCH_BANNER):
        return {"kind": "banner", "text": line[2:]}
    if line.startswith("# sensors,"):
        try:
            return {"kind": "sensors", "count": int(line.split(",", 1)[1])}
        except ValueError:
            return None
    if line.startswith("# rom,"):
        parts = line.split(",")
        if len(parts) == 3:
            return {"kind": "rom", "index": parts[1].strip(), "rom": parts[2].strip().upper()}
        return None
    if line.startswith("# parasite_power,"):
        return {"kind": "parasite", "value": line.split(",", 1)[1].strip() == "1"}
    if line.startswith("#"):
        return {"kind": "info", "text": line[1:].strip()}
    return None


# --------------------------------------------------------------------------------------- sensor map


@dataclass
class SensorInfo:
    name: str
    group: str = ""
    offset_C: float = 0.0
    note: str = ""


def load_sensor_map(path: Path | None) -> dict[str, SensorInfo]:
    """``{"sensors": {"28FF...": {"name": ..., "group": "top", "offset_C": -0.12, "note": ...}}}`` -> by ROM id."""
    if path is None:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    sensors = data.get("sensors", {}) if isinstance(data, dict) else {}
    result: dict[str, SensorInfo] = {}
    for rom, item in sensors.items():
        key = str(rom).replace(":", "").replace("-", "").strip().upper()
        if len(key) != 16:
            raise ValueError(f"sensor map: {rom!r} is not a 16-hex-digit ROM id")
        item = item or {}
        result[key] = SensorInfo(
            name=str(item.get("name") or key),
            group=str(item.get("group") or ""),
            offset_C=float(item.get("offset_C") or 0.0),
            note=str(item.get("note") or ""),
        )
    return result


def sensor_for(rom: str, sensor_map: dict[str, SensorInfo]) -> SensorInfo:
    return sensor_map.get(rom) or SensorInfo(name=rom)


# ---------------------------------------------------------------------------------------- the ports


def describe_port(port: Any) -> str:
    vid = getattr(port, "vid", None)
    pid = getattr(port, "pid", None)
    ids = "" if vid is None else f" {vid:04X}:{(pid or 0):04X}"
    return f"{port.device}{ids} {getattr(port, 'description', '')}".rstrip()


def is_pat_controller(port: Any) -> bool:
    return getattr(port, "vid", None) == FTDI_VID and getattr(port, "pid", None) == FT232H_PID


def candidate_ports(ports: list[Any]) -> list[Any]:
    """Arduino-like ports only; never an FTDI port (the PAT boards are FTDI FT232H)."""
    return [p for p in ports if getattr(p, "vid", None) in ARDUINO_VIDS]


def list_ports() -> list[Any]:
    from serial.tools import list_ports as serial_list_ports

    return sorted(serial_list_ports.comports(), key=lambda p: p.device)


def choose_port(requested: str, ports: list[Any]) -> str:
    """The port to open: ``auto`` = the only Arduino-like port; a named port is checked against the PAT boards."""
    if requested and requested.lower() != "auto":
        for port in ports:
            if port.device.upper() == requested.upper():
                if is_pat_controller(port):
                    raise RuntimeError(f"{describe_port(port)} is an FTDI FT232H (a PAT controller board); refused")
                return port.device
        return requested  # not listed (yet): try it; the handshake will tell
    found = candidate_ports(ports)
    if not found:
        raise RuntimeError("no Arduino-like serial port found (FTDI ports are never used: the PAT boards are FTDI)")
    if len(found) > 1:
        raise RuntimeError("several Arduino-like ports: " + ", ".join(p.device for p in found) + "; give --port")
    return found[0].device


# ------------------------------------------------------------------------------------- the stream


@dataclass
class StreamState:
    banner: str = ""
    sensor_count: int | None = None
    roms: list[str] = field(default_factory=list)
    parasite_power: bool | None = None
    info: list[str] = field(default_factory=list)
    # True when the sketch announced itself before we asked: it restarted when the port was opened.
    reset_on_open: bool | None = None


def handshake(link: Any, state: StreamState, timeout_s: float = 6.0, clock: Any = time,
              pending: list[dict[str, Any]] | None = None) -> bool:
    """Wait for the sketch to identify itself (opening the port resets an Uno; it then prints its banner).

    Without a reset (the port is opened without toggling DTR) the sketch is already running: readings
    arrive but no sensor list. Then ``s`` (rescan) is sent once, at the first reading or after 2.5 s of
    silence. Readings seen meanwhile go to ``pending``. True when the banner, a sensor list or a reading
    was seen.
    """
    start = clock.time()
    asked = False
    identified = False
    got_reading = False
    while clock.time() - start < timeout_s:
        if not asked and not identified and (got_reading or clock.time() - start >= 2.5):
            link.write(b"s")
            asked = True
        raw = link.readline()
        if not raw:
            continue
        event = parse_line(raw.decode("ascii", errors="replace"))
        if event is None:
            continue
        apply_event(state, event)
        if event["kind"] == "reading":
            if pending is not None:
                pending.append(event)
            got_reading = True
            continue
        if event["kind"] in {"banner", "sensors", "rom"}:
            if not identified and state.reset_on_open is None:
                # a banner or sensor list nobody asked for: the board has just started
                state.reset_on_open = not asked and not got_reading
            identified = True
        if event["kind"] == "rom" and state.sensor_count is not None and len(state.roms) >= state.sensor_count:
            return True
        if event["kind"] == "sensors" and event["count"] == 0:
            return True
    return identified or got_reading


def apply_event(state: StreamState, event: dict[str, Any]) -> None:
    kind = event["kind"]
    if kind == "banner":
        state.banner = event["text"]
    elif kind == "sensors":
        state.sensor_count = event["count"]
        state.roms = []
    elif kind == "rom":
        if event["rom"] not in state.roms:
            state.roms.append(event["rom"])
    elif kind == "parasite":
        state.parasite_power = event["value"]
    elif kind == "info":
        state.info.append(event["text"])


def reading_row(event: dict[str, Any], now: float, port: str, sensor_map: dict[str, SensorInfo]) -> dict[str, str]:
    info = sensor_for(event["rom"], sensor_map)
    ok = event["status"] == "ok"
    return {
        "time_iso": dt.datetime.fromtimestamp(now).isoformat(timespec="milliseconds"),
        "time_unix_s": f"{now:.3f}",
        "port": port,
        "cycle": str(event["cycle"]),
        "arduino_ms": str(event["arduino_ms"]),
        "rom": event["rom"],
        "name": info.name,
        "group": info.group,
        "temp_raw_C": f"{event['temp_raw_C']:.4f}",
        "offset_C": f"{info.offset_C:.4f}",
        "temp_C": f"{event['temp_raw_C'] + info.offset_C:.4f}" if ok else "",
        "status": event["status"],
        "error": "",
    }


def is_new_reading(event: dict[str, Any], last_cycle: dict[str, int]) -> bool:
    """False for a repeat of the sensor's last written reading (same cycle); a restart (lower cycle) is new."""
    if last_cycle.get(event["rom"]) == event["cycle"]:
        return False
    last_cycle[event["rom"]] = event["cycle"]
    return True


def error_row(now: float, port: str, message: str, event: str = "") -> dict[str, str]:
    row = {key: "" for key in CSV_COLUMNS}
    row.update({
        "time_iso": dt.datetime.fromtimestamp(now).isoformat(timespec="milliseconds"),
        "time_unix_s": f"{now:.3f}",
        "port": port,
        "error": message[:200],
        "event": event,
    })
    return row


def event_row(now: float, port: str, event: str) -> dict[str, str]:
    """A row that only marks a port event (no temperature, no error)."""
    return error_row(now, port, "", event)


class CsvSink:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.rows = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists() or self.path.stat().st_size == 0
        self.handle = self.path.open("a", encoding="utf-8", newline="")
        self.writer = csv.DictWriter(self.handle, fieldnames=CSV_COLUMNS)
        if new:
            self.writer.writeheader()
            self.handle.flush()

    def write(self, row: dict[str, str]) -> None:
        self.writer.writerow(row)
        self.handle.flush()
        self.rows += 1

    def close(self) -> None:
        self.handle.close()


def open_link(port: str) -> Any:
    """Open the port without toggling DTR/RTS, so that the Arduino is not reset.

    The same Arduino may also drive the PAT start marker LED and the illumination strobe
    (Arduino/pat_sync_strobe_temp); a reset would stop the strobe for about a second. Without a reset the
    sketch prints nothing on its own, so handshake() asks for a rescan ("s") and reads the sensor list.
    """
    import serial

    link = serial.Serial()
    link.port = port
    link.baudrate = BAUD
    link.timeout = 0.5
    link.dtr = False
    link.rts = False
    link.open()
    link.reset_input_buffer()  # lines buffered before the open would get the open time as their time stamp
    return link


def write_meta(path: Path, args: argparse.Namespace, port: str, state: StreamState,
               sensor_map: dict[str, SensorInfo]) -> None:
    meta = {
        "logger": "pat_temp_logger.py",
        "port": port,
        "started": getattr(args, "_started", dt.datetime.now().isoformat(timespec="seconds")),
        "sketch": state.banner,
        "sensor_count": state.sensor_count,
        "roms": state.roms,
        "parasite_power": state.parasite_power,
        "sensor_map_file": "" if args.sensor_map is None else str(Path(args.sensor_map).resolve()),
        "sensor_map": {rom: vars(info) for rom, info in sensor_map.items()},
        "unmapped_roms": [rom for rom in state.roms if rom not in sensor_map],
        "stale_s": args.stale_s,
        "columns": CSV_COLUMNS,
        "note": "temp_C = temp_raw_C + offset_C (offset from the sensor map). DS18B20, 12 bit (0.0625 C).",
    }
    Path(str(path) + ".meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


def log_loop(
    args: argparse.Namespace,
    opener: Callable[[str], Any] = open_link,
    port_lister: Callable[[], list[Any]] = list_ports,
    clock: Any = time,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    sensor_map = load_sensor_map(args.sensor_map)
    sink = CsvSink(args.output)
    args._started = dt.datetime.now().isoformat(timespec="seconds")
    end = None if args.duration is None else clock.time() + float(args.duration)
    stop_file = None if args.stop_file is None else Path(args.stop_file)

    def should_stop() -> bool:
        return (end is not None and clock.time() >= end) or (stop_file is not None and stop_file.exists())

    port_name = str(args.port)
    last_cycle: dict[str, int] = {}
    print(f"[PATTEMP] Logging DS18B20 temperatures to {args.output}", flush=True)
    try:
        while not should_stop():
            link = None
            try:
                port_name = choose_port(args.port, port_lister())
                link = opener(port_name)
                state = StreamState()
                pending: list[dict[str, Any]] = []
                if not handshake(link, state, clock=clock, pending=pending):
                    raise RuntimeError(f"{port_name}: no answer from ds18b20_serial_logger (sketch uploaded?)")
                write_meta(args.output, args, port_name, state, sensor_map)
                sink.write(event_row(clock.time(), port_name,
                                     EVENT_OPENED_RESET if state.reset_on_open else EVENT_OPENED))
                print(f"[PATTEMP] {port_name}: {state.banner or 'sketch'}; sensors {state.sensor_count}: "
                      + ", ".join(f"{rom} ({sensor_for(rom, sensor_map).name})" for rom in state.roms), flush=True)
                last_data = clock.time()
                for event in pending:
                    if is_new_reading(event, last_cycle):
                        sink.write(reading_row(event, clock.time(), port_name, sensor_map))
                while not should_stop():
                    raw = link.readline()
                    now = clock.time()
                    if raw:
                        event = parse_line(raw.decode("ascii", errors="replace"))
                        if event is None:
                            continue
                        apply_event(state, event)
                        if event["kind"] == "reading" and is_new_reading(event, last_cycle):
                            sink.write(reading_row(event, now, port_name, sensor_map))
                            last_data = now
                        elif event["kind"] == "rom":
                            write_meta(args.output, args, port_name, state, sensor_map)
                    if now - last_data >= args.stale_s:
                        sink.write(error_row(now, port_name, f"no reading for {now - last_data:.0f} s"))
                        last_data = now
            except Exception as exc:  # noqa: BLE001 - port lost or not there: note it and retry
                sink.write(error_row(clock.time(), port_name, f"serial: {exc!r}", EVENT_LOST))
                print(f"[PATTEMP][WARN] {exc!r}; retrying in 5 s", flush=True)
                for _ in range(25):
                    if should_stop():
                        break
                    sleep(0.2)
            finally:
                if link is not None:
                    try:
                        link.close()
                    except Exception:  # noqa: BLE001
                        pass
    finally:
        sink.close()
        print(f"[PATTEMP] {sink.rows} rows written to {args.output}", flush=True)
    return 0


def once(args: argparse.Namespace, opener: Callable[[str], Any] = open_link,
         port_lister: Callable[[], list[Any]] = list_ports, clock: Any = time) -> int:
    sensor_map = load_sensor_map(args.sensor_map)
    try:
        port_name = choose_port(args.port, port_lister())
        link = opener(port_name)
    except Exception as exc:  # noqa: BLE001
        print(f"[PATTEMP][ERROR] {exc}", file=sys.stderr)
        return 1
    try:
        state = StreamState()
        pending: list[dict[str, Any]] = []
        if not handshake(link, state, clock=clock, pending=pending):
            print(f"[PATTEMP][ERROR] {port_name}: no answer from ds18b20_serial_logger (sketch uploaded? 115200 baud?)",
                  file=sys.stderr)
            return 1
        readings: dict[str, dict[str, Any]] = {event["rom"]: event for event in pending}
        deadline = clock.time() + float(args.timeout)
        while clock.time() < deadline:
            if state.roms and all(rom in readings for rom in state.roms):
                break
            if state.sensor_count == 0:
                break
            raw = link.readline()
            if not raw:
                continue
            event = parse_line(raw.decode("ascii", errors="replace"))
            if event is None:
                continue
            apply_event(state, event)
            if event["kind"] == "reading":
                readings[event["rom"]] = event
    finally:
        link.close()
    print(f"{port_name}: {state.banner or 'ds18b20_serial_logger'}, sensors on the bus {state.sensor_count}"
          + ("" if state.parasite_power in (None, False) else " (PARASITE POWER: use the 3-wire connection)"))
    good = 0
    for rom in state.roms or list(readings):
        info = sensor_for(rom, sensor_map)
        event = readings.get(rom)
        mapped = "" if rom in sensor_map else "  (not in the sensor map)"
        if event is None:
            print(f"  {rom} {info.name}: no reading{mapped}")
            continue
        if event["status"] != "ok":
            print(f"  {rom} {info.name}: {event['status']}{mapped}")
            continue
        good += 1
        offset = "" if info.offset_C == 0.0 else f" (raw {event['temp_raw_C']:.4f}, offset {info.offset_C:+.4f})"
        group = f" [{info.group}]" if info.group else ""
        print(f"  {rom} {info.name}{group}: {event['temp_raw_C'] + info.offset_C:.4f} degC{offset}{mapped}")
    if good == 0:
        print("[PATTEMP][ERROR] no valid temperature (wiring, 4.7 kOhm pull-up, sensors?)", file=sys.stderr)
        return 1
    return 0


def print_port_list(ports: list[Any]) -> int:
    if not ports:
        print("[PATTEMP] no serial ports")
        return 1
    found = candidate_ports(ports)
    for port in ports:
        if is_pat_controller(port):
            mark = "PAT controller (FTDI FT232H): never opened"
        elif getattr(port, "vid", None) == FTDI_VID:
            mark = "FTDI: never chosen automatically"
        elif port in found:
            mark = f"candidate ({ARDUINO_VIDS[port.vid]})"
        else:
            mark = ""
        print(f"  {describe_port(port)}  {mark}".rstrip())
    if len(found) == 1:
        print(f"[PATTEMP] --port auto would use {found[0].device}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", default="auto",
                        help="Serial port of the Arduino (default auto: the only Arduino-like port; FTDI ports are never used).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Read every sensor once, print, exit (0 = a valid reading).")
    mode.add_argument("--list", action="store_true", help="List the serial ports and which one --port auto would use.")
    parser.add_argument("--timeout", type=float, default=8.0, help="--once: seconds to wait for the readings (default 8).")
    parser.add_argument("--sensor-map", type=Path, default=None,
                        help=f"JSON with the name, group (top/bottom) and offset of each ROM id (default {DEFAULT_SENSOR_MAP.name} if present).")
    parser.add_argument("--output", type=Path, help="CSV to append to.")
    parser.add_argument("--stale-s", type=float, default=10.0,
                        help="Write an error row after this long without a reading (default 10).")
    parser.add_argument("--stop-file", type=Path, help="Stop when this file appears.")
    parser.add_argument("--duration", type=float, help="Stop after this many seconds.")
    args = parser.parse_args(argv)
    if not (args.once or args.list) and args.output is None:
        parser.error("give --output (or --once / --list)")
    if args.stale_s <= 0 or args.timeout <= 0:
        parser.error("--stale-s and --timeout must be > 0")
    if args.sensor_map is None and DEFAULT_SENSOR_MAP.is_file():
        args.sensor_map = DEFAULT_SENSOR_MAP
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.list:
            return print_port_list(list_ports())
        if args.once:
            return once(args)
        return log_loop(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
