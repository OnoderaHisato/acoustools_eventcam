#!/usr/bin/env python3
"""Log the room temperature, humidity and air pressure from a Blue Maestro Tempo Disc (BLE, listen only).

    python tempodisc_logger.py --once                      # wait for one reading of F9EE9FFC and print it
    python tempodisc_logger.py --list                      # every Tempo Disc in range (10 s)
    python tempodisc_logger.py --output env.csv            # log until Ctrl+C
    python tempodisc_logger.py --output env.csv --stop-file env.stop   # stops when the file appears

The PC never connects to the sensor; it only listens to the advertisement (tempodisc_ble.py), so the
sensor settings and its own memory are not touched. Close the bmLogger app while logging: a sensor
connected to the phone may stop advertising.

A row is written when the advertised reading changes, and otherwise every --interval seconds (the
latest advertisement is repeated, with its own reception time). When nothing has been heard for
--stale-s seconds, a row with only an ``error`` is written, so a gap is visible in the log. The sensor
updates the advertised value at its own logging interval (set in bmLogger); the column
``log_interval_s`` shows it.

Used by acoustools_stereo_eventcam_3d_recording_auto.py (--env-log) through env_logging.ps1, the same
way as the supply logger (pwr01_logger.py).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import sys
import time
from pathlib import Path

from tempodisc_ble import DEFAULT_DEVICE, TempoListener, TempoReading, scan_for

CSV_COLUMNS = [
    "time_iso", "time_unix_s", "received_unix_s", "address", "name", "rssi_dBm", "version",
    "battery_percent", "log_interval_s", "log_count", "temperature_C", "humidity_percent",
    "pressure_hPa", "dew_point_C", "raw_hex", "scan_response_hex", "error",
]


def reading_row(reading: TempoReading, now: float) -> dict[str, str]:
    def fmt(value, digits: int = 1) -> str:
        return "" if value is None else f"{value:.{digits}f}"

    return {
        "time_iso": dt.datetime.fromtimestamp(now).isoformat(timespec="milliseconds"),
        "time_unix_s": f"{now:.3f}",
        "received_unix_s": f"{reading.received_unix_s:.3f}",
        "address": reading.address,
        "name": reading.name,
        "rssi_dBm": "" if reading.rssi is None else str(reading.rssi),
        "version": str(reading.version),
        "battery_percent": str(reading.battery_percent),
        "log_interval_s": str(reading.log_interval_s),
        "log_count": str(reading.log_count),
        "temperature_C": fmt(reading.temperature_C),
        "humidity_percent": fmt(reading.humidity_percent),
        "pressure_hPa": fmt(reading.pressure_hPa),
        "dew_point_C": fmt(reading.dew_point_C, 2),
        "raw_hex": reading.raw_hex,
        "scan_response_hex": reading.scan_response_hex,
        "error": "",
    }


def error_row(now: float, message: str) -> dict[str, str]:
    row = {key: "" for key in CSV_COLUMNS}
    row.update({
        "time_iso": dt.datetime.fromtimestamp(now).isoformat(timespec="milliseconds"),
        "time_unix_s": f"{now:.3f}",
        "error": message[:200],
    })
    return row


class CsvSink:
    """Decides which readings become rows (change or --interval), and writes them line by line."""

    def __init__(self, path: Path, interval_s: float, stale_s: float, clock=time) -> None:
        self.path = Path(path)
        self.interval_s = float(interval_s)
        self.stale_s = float(stale_s)
        self.clock = clock
        self.latest: TempoReading | None = None
        self.last_written_key: str | None = None
        self.last_row_at: float | None = None
        self.last_heard_at: float = clock.time()
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

    def on_reading(self, reading: TempoReading) -> None:
        self.latest = reading
        self.last_heard_at = reading.received_unix_s
        key = reading.raw_hex
        if key != self.last_written_key:
            self._write_latest(self.clock.time())

    def _write_latest(self, now: float) -> None:
        self.write(reading_row(self.latest, now))
        self.last_written_key = self.latest.raw_hex
        self.last_row_at = now

    def tick(self) -> None:
        """Called about every 0.2 s: repeat the latest reading every interval; report silence."""
        now = self.clock.time()
        if now - self.last_heard_at >= self.stale_s:
            self.write(error_row(now, f"no advertisement for {now - self.last_heard_at:.0f} s"))
            self.last_heard_at = now  # report again after another stale_s
            return
        if self.latest is not None and self.interval_s > 0 and (
            self.last_row_at is None or now - self.last_row_at >= self.interval_s
        ):
            self._write_latest(now)

    def close(self) -> None:
        self.handle.close()


async def log_loop(args: argparse.Namespace) -> int:
    sink = CsvSink(args.output, args.interval, args.stale_s)
    meta = {
        "logger": "tempodisc_logger.py",
        "device": args.device,
        "started": dt.datetime.now().isoformat(timespec="seconds"),
        "interval_s": args.interval,
        "stale_s": args.stale_s,
        "columns": CSV_COLUMNS,
        "note": "BLE advertisement only (no connection). Dew point is calculated (Magnus).",
    }
    Path(str(args.output) + ".meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    end = None if args.duration is None else time.monotonic() + float(args.duration)
    stop_file = None if args.stop_file is None else Path(args.stop_file)

    def should_stop() -> bool:
        return (end is not None and time.monotonic() >= end) or (stop_file is not None and stop_file.exists())

    listener = TempoListener(args.device, on_reading=sink.on_reading)
    print(f"[ENV] Logging {args.device or 'any Tempo Disc'} to {args.output}", flush=True)
    try:
        while not should_stop():
            try:
                from bleak import BleakScanner

                async with BleakScanner(listener._callback, scanning_mode="active"):
                    while not should_stop():
                        sink.tick()
                        await asyncio.sleep(0.2)
            except Exception as exc:  # noqa: BLE001 - Bluetooth off or adapter busy: note it and retry
                sink.write(error_row(time.time(), f"scanner: {exc!r}"))
                print(f"[ENV][WARN] scanner stopped: {exc!r}; retrying in 5 s", flush=True)
                for _ in range(25):
                    if should_stop():
                        break
                    await asyncio.sleep(0.2)
    finally:
        sink.close()
        print(f"[ENV] {sink.rows} rows written to {args.output}", flush=True)
    return 0


def print_reading(reading: TempoReading) -> None:
    age = time.time() - reading.received_unix_s
    humidity = "-" if reading.humidity_percent is None else f"{reading.humidity_percent:.1f} %"
    pressure = "-" if reading.pressure_hPa is None else f"{reading.pressure_hPa:.1f} hPa"
    dew = "-" if reading.dew_point_C is None else f"{reading.dew_point_C:.1f} degC"
    print(
        f"{reading.name or '?'} {reading.address} {reading.model} (v{reading.version}) RSSI {reading.rssi} dBm, "
        f"battery {reading.battery_percent} %, logging every {reading.log_interval_s} s ({reading.log_count} records)\n"
        f"  temperature {reading.temperature_C:.1f} degC, humidity {humidity}, pressure {pressure}, dew point {dew}"
        f"  (heard {age:.1f} s ago)",
        flush=True,
    )


async def once(args: argparse.Namespace) -> int:
    found: list[TempoReading] = []
    listener = TempoListener(args.device, on_reading=found.append)
    end = time.monotonic() + float(args.timeout)
    try:
        await listener.run(lambda: bool(found) or time.monotonic() >= end)
    except Exception as exc:  # noqa: BLE001
        print(f"[ENV][ERROR] Bluetooth scan failed: {exc!r}", file=sys.stderr)
        return 1
    if not found:
        print(f"[ENV][ERROR] no advertisement from {args.device or 'a Tempo Disc'} in {args.timeout:g} s "
              "(sensor out of range, battery, Bluetooth off, or the phone app is connected)", file=sys.stderr)
        return 1
    print_reading(found[0])
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", default=DEFAULT_DEVICE,
                        help=f"Sensor name or (part of) its address (default {DEFAULT_DEVICE}; '' = any Tempo Disc).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Wait for one reading, print it, exit (0 found, 1 not).")
    mode.add_argument("--list", action="store_true", help="Listen 10 s and list every Tempo Disc in range.")
    parser.add_argument("--timeout", type=float, default=30.0, help="--once: seconds to wait (default 30).")
    parser.add_argument("--output", type=Path, help="CSV to append to.")
    parser.add_argument("--interval", type=float, default=10.0,
                        help="Repeat the latest reading every this many seconds when it has not changed (default 10; 0 = changes only).")
    parser.add_argument("--stale-s", type=float, default=120.0,
                        help="Write an error row after this long without an advertisement (default 120).")
    parser.add_argument("--stop-file", type=Path, help="Stop when this file appears.")
    parser.add_argument("--duration", type=float, help="Stop after this many seconds.")
    args = parser.parse_args(argv)
    if not (args.once or args.list) and args.output is None:
        parser.error("give --output (or --once / --list)")
    if args.interval < 0 or args.stale_s <= 0 or args.timeout <= 0:
        parser.error("--interval must be >= 0, --stale-s and --timeout > 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.once:
            return asyncio.run(once(args))
        if args.list:
            readings = asyncio.run(scan_for(10.0, ""))
            for reading in readings:
                print_reading(reading)
            if not readings:
                print("[ENV] no Tempo Disc heard in 10 s")
            return 0 if readings else 1
        return asyncio.run(log_loop(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
