#!/usr/bin/env python3
"""Read-only identity probe for one Ossila Linear Stage.

This program never sends home, goto, move, run, stop, or hardstop.  It only
lists serial ports or queries one already-stopped stage for identification and
stored settings.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="List serial ports only.")
    group.add_argument("--port", help="COM port to query, for example COM4.")
    group.add_argument(
        "--usb-serial",
        help="USB serial to find with pyserial, for example E662608797387B2E.",
    )
    parser.add_argument("--timeout-sec", type=float, default=1.0)
    parser.add_argument(
        "--expected-travel-mm",
        type=float,
        default=200.0,
        help="Used only to annotate a known raw/1000 length response.",
    )
    return parser.parse_args()


def list_ports() -> int:
    from serial.tools import list_ports

    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports detected.")
        return 0
    for port in ports:
        print(
            f"port={port.device} usb_serial={port.serial_number or '-'} "
            f"description={port.description or '-'}"
        )
        print(f"  hwid={port.hwid or '-'}")
    return 0


def find_port(usb_serial: str) -> str:
    from serial.tools import list_ports

    matches = [
        port.device
        for port in list_ports.comports()
        if str(port.serial_number or "").strip() == usb_serial
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one port with USB serial {usb_serial!r}; found {matches!r}. "
            "Run with --list and check the cable/Windows device state."
        )
    return matches[0]


def read_frame(port: Any, command: str, timeout_sec: float) -> str:
    """Send one framed read-only command and return its complete <...> response."""
    port.reset_input_buffer()
    port.write(f"<{command}>".encode("ascii"))
    deadline = time.monotonic() + timeout_sec
    buffer = bytearray()
    while time.monotonic() < deadline:
        chunk = port.read(max(1, min(256, int(port.in_waiting or 0))))
        if not chunk:
            continue
        buffer.extend(chunk)
        start = buffer.find(b"<")
        end = buffer.find(b">", start + 1) if start >= 0 else -1
        if start >= 0 and end >= 0:
            return bytes(buffer[start : end + 1]).decode("ascii", errors="strict")
    raise TimeoutError(f"No complete response to <{command}> within {timeout_sec:.1f} s.")


def frame_parts(frame: str, expected_name: str) -> list[str]:
    text = frame.strip()
    if not (text.startswith("<") and text.endswith(">")):
        raise RuntimeError(f"Invalid response frame: {frame!r}")
    parts = text[1:-1].split()
    if not parts or parts[0].lower() != expected_name.lower():
        raise RuntimeError(f"Unexpected response to <{expected_name}?>: {frame!r}")
    return parts


def query_text(port: Any, name: str, timeout_sec: float) -> tuple[str, str]:
    frame = read_frame(port, f"{name}?", timeout_sec)
    parts = frame_parts(frame, name)
    return " ".join(parts[1:]), frame


def query_float(port: Any, name: str, timeout_sec: float) -> tuple[float, str]:
    text, frame = query_text(port, name, timeout_sec)
    try:
        value = float(text)
    except ValueError as exc:
        raise RuntimeError(f"Non-numeric {name}? response: {frame!r}") from exc
    if not math.isfinite(value):
        raise RuntimeError(f"Non-finite {name}? response: {frame!r}")
    return value, frame


def main() -> int:
    args = parse_args()
    if args.list:
        return list_ports()
    if args.timeout_sec <= 0:
        raise SystemExit("--timeout-sec must be positive.")
    if args.expected_travel_mm <= 0:
        raise SystemExit("--expected-travel-mm must be positive.")

    import serial

    selected_port = args.port or find_port(str(args.usb_serial).strip())
    with serial.Serial(
        selected_port,
        baudrate=9600,
        timeout=0.05,
        write_timeout=float(args.timeout_sec),
    ) as port:
        status_frame = read_frame(port, "status?", float(args.timeout_sec))
        status_parts = frame_parts(status_frame, "status")
        if len(status_parts) != 5:
            raise RuntimeError(f"Unexpected status response: {status_frame!r}")
        speed_mm_s = float(status_parts[1])
        if abs(speed_mm_s) > 1e-9:
            raise RuntimeError(
                f"Stage is moving at {speed_mm_s:g} mm/s. This probe sends no stop command; "
                "wait until it stops and retry."
            )

        device, device_frame = query_text(port, "device", args.timeout_sec)
        firmware, firmware_frame = query_text(port, "firmware", args.timeout_sec)
        internal_serial, serial_frame = query_text(port, "serial", args.timeout_sec)
        length_raw, length_frame = query_float(port, "length", args.timeout_sec)
        acceleration, acceleration_frame = query_float(port, "acc", args.timeout_sec)
        deceleration, deceleration_frame = query_float(port, "dec", args.timeout_sec)
        posmode, posmode_frame = query_text(port, "posmode", args.timeout_sec)
        alarms, alarms_frame = query_text(port, "alarms", args.timeout_sec)

    expected = float(args.expected_travel_mm)
    if math.isclose(length_raw, expected, abs_tol=0.5):
        length_mm, length_interpretation = length_raw, "raw_mm"
    elif math.isclose(length_raw / 1000.0, expected, abs_tol=0.5):
        length_mm, length_interpretation = length_raw / 1000.0, "raw_um_normalized_to_mm"
    else:
        length_mm, length_interpretation = None, "unrecognized_raw_unit"

    result = {
        "port": selected_port,
        "device": device,
        "device_response": device_frame,
        "firmware": firmware,
        "firmware_response": firmware_frame,
        "internal_serial": internal_serial,
        "serial_response": serial_frame,
        "length_raw_response_value": length_raw,
        "length_mm": length_mm,
        "length_unit_interpretation": length_interpretation,
        "acceleration_mm_s2": acceleration,
        "acceleration_response": acceleration_frame,
        "deceleration_mm_s2": deceleration,
        "deceleration_response": deceleration_frame,
        "posmode": posmode,
        "posmode_response": posmode_frame,
        "alarms": alarms.split() if alarms else [],
        "alarms_response": alarms_frame,
        "status": {
            "frame": status_frame,
            "speed_mm_s": speed_mm_s,
            "position_hardware_mm": float(status_parts[2]),
            "home_switch": status_parts[3] == "1",
            "end_switch": status_parts[4] == "1",
        },
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
