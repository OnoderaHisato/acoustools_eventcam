"""Blue Maestro Tempo Disc: decode the BLE advertisement and listen for it (no connection, no pairing).

The Tempo Disc broadcasts its latest reading in the manufacturer data of its advertisement
(company ID 0x0133, Blue Maestro). The PC only listens, so nothing on the sensor is changed and the
phone app (bmLogger) keeps working. Close bmLogger while the PC logs: a connected sensor may stop
advertising.

Layout of the advertisement (big-endian), checked against the THPD on 2026-10-13
(``1b640e10177000f0022427710100`` = v27, battery 100 %, log interval 3600 s, 6000 records,
24.0 degC, 54.8 %RH, 1009.7 hPa; not yet compared with the bmLogger display) and against the
open-source decoder ``bluemaestro-ble`` (struct ``!B4xhHH`` after the version byte):

    byte  0     version (13 = Tempo Disc T, 22/23 = THD, 27 = THPD)
    byte  1     battery [%]
    bytes 2-3   logging interval [s]
    bytes 4-5   number of stored records
    bytes 6-7   temperature x10 [degC], signed
    bytes 8-9   humidity x10 [%RH] (THD, THPD)
    bytes 10-11 pressure x10 [hPa] (THPD)

The scan response carries a second, longer block under the same company ID (daily highs/lows and
similar). Its layout is not documented here, so it is only kept as raw hex. The dew point is
calculated from temperature and humidity (Magnus formula), as the app does; the THPD does not send it.
"""

from __future__ import annotations

import asyncio
import math
import struct
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

BLUE_MAESTRO_COMPANY_ID = 0x0133
DEFAULT_DEVICE = "F9EE9FFC"  # name printed on the lab sensor (the back QR code); also the address prefix

# version -> (model name, has humidity, has pressure)
MODELS: dict[int, tuple[str, bool, bool]] = {
    13: ("Tempo Disc T", False, False),
    22: ("Tempo Disc THD", True, False),
    23: ("Tempo Disc THD", True, False),
    27: ("Tempo Disc THPD", True, True),
}
MAIN_PACKET_MIN_BYTES = 12


@dataclass
class TempoReading:
    received_unix_s: float
    address: str
    name: str
    rssi: int | None
    version: int
    model: str
    battery_percent: int
    log_interval_s: int
    log_count: int
    temperature_C: float
    humidity_percent: float | None
    pressure_hPa: float | None
    dew_point_C: float | None
    raw_hex: str
    scan_response_hex: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def dew_point_c(temperature_c: float, humidity_percent: float) -> float | None:
    """Magnus formula (Sonntag 1990 constants), good to about 0.1 degC between -45 and 60 degC."""
    if humidity_percent is None or not (0.0 < humidity_percent <= 100.0):
        return None
    a, b = 17.62, 243.12
    gamma = math.log(humidity_percent / 100.0) + a * temperature_c / (b + temperature_c)
    return round(b * gamma / (a - gamma), 2)


def is_main_packet(data: bytes) -> bool:
    """True for the advertisement block (version byte first), False for the scan response block."""
    return len(data) >= MAIN_PACKET_MIN_BYTES and len(data) <= 16 and data[0] in MODELS


def decode_main_packet(
    data: bytes,
    *,
    received_unix_s: float | None = None,
    address: str = "",
    name: str = "",
    rssi: int | None = None,
    scan_response_hex: str = "",
) -> TempoReading | None:
    """Decode the advertisement block; None when it is not a known Tempo Disc reading."""
    data = bytes(data)
    if not is_main_packet(data):
        return None
    version = data[0]
    model, has_humidity, has_pressure = MODELS[version]
    battery, interval, count, raw_t, raw_h, raw_p = struct.unpack_from(">BHHhHH", data, 1)
    temperature = raw_t / 10.0
    humidity = raw_h / 10.0 if has_humidity else None
    pressure = raw_p / 10.0 if has_pressure else None
    if not (-60.0 <= temperature <= 100.0):
        return None  # not a reading (some other Blue Maestro frame)
    if humidity is not None and not (0.0 <= humidity <= 100.0):
        humidity = None
    if pressure is not None and not (300.0 <= pressure <= 1200.0):
        pressure = None
    return TempoReading(
        received_unix_s=time.time() if received_unix_s is None else float(received_unix_s),
        address=address,
        name=name,
        rssi=rssi,
        version=version,
        model=model,
        battery_percent=int(battery),
        log_interval_s=int(interval),
        log_count=int(count),
        temperature_C=temperature,
        humidity_percent=humidity,
        pressure_hPa=pressure,
        dew_point_C=None if humidity is None else dew_point_c(temperature, humidity),
        raw_hex=data.hex(),
        scan_response_hex=scan_response_hex,
    )


def matches_device(wanted: str, address: str, name: str | None) -> bool:
    """``wanted`` is empty (any Tempo Disc), the printed name (F9EE9FFC), or all/part of the address."""
    wanted = (wanted or "").replace(":", "").replace("-", "").upper()
    if not wanted:
        return True
    plain_address = (address or "").replace(":", "").replace("-", "").upper()
    return wanted in plain_address or wanted == (name or "").upper()


class TempoListener:
    """Collects decoded readings from BLE advertisements (bleak, active scan for the scan response).

    ``on_reading`` is called from the asyncio loop for every advertisement that decodes. The scan
    response block (same company ID, longer) is remembered per device and attached to the next reading.
    """

    def __init__(self, device: str = DEFAULT_DEVICE, on_reading: Callable[[TempoReading], None] | None = None) -> None:
        self.device = device
        self.on_reading = on_reading
        self.scan_responses: dict[str, str] = {}
        self.latest: dict[str, TempoReading] = {}
        self._last_adv: dict[str, Any] = {}

    def handle(self, address: str, name: str | None, rssi: int | None, manufacturer_data: dict[int, bytes],
               received_unix_s: float | None = None) -> TempoReading | None:
        data = manufacturer_data.get(BLUE_MAESTRO_COMPANY_ID)
        if data is None or not matches_device(self.device, address, name):
            return None
        data = bytes(data)
        if not is_main_packet(data):
            self.scan_responses[address] = data.hex()
            return None
        reading = decode_main_packet(
            data, received_unix_s=received_unix_s, address=address, name=name or "", rssi=rssi,
            scan_response_hex=self.scan_responses.get(address, ""),
        )
        if reading is not None:
            self.latest[address] = reading
            if self.on_reading is not None:
                self.on_reading(reading)
        return reading

    def _callback(self, device: Any, advertisement: Any) -> None:
        name = advertisement.local_name or device.name
        raw = getattr(advertisement, "platform_data", None)
        raw = raw[1] if isinstance(raw, tuple) and len(raw) == 2 else None
        if hasattr(raw, "adv") and hasattr(raw, "scan"):
            # Windows (bleak WinRT) merges the advertisement and the scan response into one dict, and
            # both carry company ID 0x0133, so the scan response would hide the reading. Read the two
            # packets separately; the advertisement is new only when its event object changes.
            if raw.scan is not None:
                for section in raw.scan.advertisement.manufacturer_data:
                    if section.company_id == BLUE_MAESTRO_COMPANY_ID:
                        self.handle(device.address, name, advertisement.rssi,
                                    {BLUE_MAESTRO_COMPANY_ID: bytes(section.data)})
            if raw.adv is not None and self._last_adv.get(device.address) is not raw.adv:
                self._last_adv[device.address] = raw.adv
                for section in raw.adv.advertisement.manufacturer_data:
                    if section.company_id == BLUE_MAESTRO_COMPANY_ID:
                        self.handle(device.address, name, raw.adv.raw_signal_strength_in_dbm,
                                    {BLUE_MAESTRO_COMPANY_ID: bytes(section.data)})
            return
        self.handle(device.address, name, advertisement.rssi, advertisement.manufacturer_data)

    async def run(self, stop: Callable[[], bool], poll_s: float = 0.2) -> None:
        """Scan until ``stop()`` is true. Raises when Bluetooth is unavailable."""
        from bleak import BleakScanner

        async with BleakScanner(self._callback, scanning_mode="active"):
            while not stop():
                await asyncio.sleep(poll_s)


async def scan_for(seconds: float, device: str = "") -> list[TempoReading]:
    """Listen for ``seconds`` and return the latest reading of every matching Tempo Disc."""
    listener = TempoListener(device)
    end = time.monotonic() + seconds
    await listener.run(lambda: time.monotonic() >= end)
    return list(listener.latest.values())
