#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Capture a resumable Ossila-stage/PAT stereo accuracy experiment.

Safety defaults:

* without ``--execute`` this command only validates and writes the plan;
* only the configured depth axis is opened;
* every move is an absolute hardware-coordinate move checked against soft limits;
* motion completion and position readback are required before camera recording;
* an exception or Ctrl+C stops and closes the stage without an automatic move.

The PAT visits each cuboid vertex in Gray-code order and remains stationary
during each stereo recording.  This separates static stereo accuracy from PAT
motion lag and camera time alignment.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

from stereo_stage_accuracy_common import (
    SCRIPT_DIR,
    configuration_fingerprint,
    experiment_motion_vectors,
    file_sha256,
    generate_plan,
    gray_cube_vertices,
    json_ready,
    load_stereo_calibration,
    load_json,
    plan_hash,
    processing_fingerprint,
    resolve_from_config,
    validate_config,
)


DEFAULT_CONFIG = SCRIPT_DIR / "stereo_stage_accuracy_config.json"
FLOAT_PATTERN = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan or capture a stereo depth/shape experiment using an Ossila stage and PAT.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Experiment JSON.")
    parser.add_argument("--output-root", type=Path, default=Path("stereo_stage_accuracy_records"))
    parser.add_argument("--session-dir", type=Path, default=None, help="Exact new/resume session.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Open hardware and execute motion. Without this flag only a dry plan is produced.",
    )
    parser.add_argument(
        "--list-stage-ports",
        action="store_true",
        help=(
            "List COM ports and USB serial identifiers without opening a port, "
            "creating a session, or sending any command."
        ),
    )
    parser.add_argument(
        "--probe-stage-identity",
        action="store_true",
        help=(
            "With --execute, open only the configured stage, verify it is stopped, "
            "print serial/length/acc/dec, and close without motion."
        ),
    )
    parser.add_argument(
        "--home-depth-axis",
        action="store_true",
        help="Home the one configured stage axis before its first absolute move.",
    )
    parser.add_argument("--yes", action="store_true", help="Skip the final start confirmation.")
    parser.add_argument("--no-preview", action="store_true", help="Skip the initial stereo preview.")
    parser.add_argument(
        "--trace-cube-preview",
        action="store_true",
        help="Slowly trace all 12 cuboid edges once with the PAT before capture.",
    )
    parser.add_argument("--process-each", action="store_true", help="Process each accepted capture immediately.")
    parser.add_argument("--continue-on-error", action="store_true", help="Continue after exhausted capture retries.")
    return parser.parse_args()


def list_stage_ports() -> int:
    """List serial-port identity only; this does not open a port."""
    try:
        from serial.tools import list_ports
    except Exception as exc:
        raise SystemExit(f"pyserial is required to list stage ports: {exc}") from exc
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports are currently detected.")
        return 0
    print("Detected serial ports (read-only enumeration):")
    for port in ports:
        print(
            f"  port={port.device} usb_serial={port.serial_number or '-'} "
            f"description={port.description or '-'}"
        )
        print(f"    hwid={port.hwid or '-'}")
    print(
        "Match usb_serial to Ossila config.md. If several stages are present, "
        "unplug/replug one USB cable at a time and repeat this command."
    )
    return 0


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(json_ready(payload), indent=2), encoding="utf-8")
    os.replace(temporary, path)


def create_or_resume_session(
    args: argparse.Namespace,
    config: dict[str, Any],
    config_path: Path,
    resolved: dict[str, Any],
    planned: list[dict[str, Any]],
) -> tuple[Path, Path, dict[str, Any]]:
    if args.session_dir is not None:
        session_dir = args.session_dir.resolve()
    else:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = str(config.get("name", "stereo_stage_accuracy"))
        session_dir = (args.output_root / f"{name}_{stamp}").resolve()
    manifest_path = session_dir / "stereo_stage_accuracy_manifest.json"
    if (
        bool(getattr(args, "execute", False))
        and not bool(getattr(args, "probe_stage_identity", False))
        and not manifest_path.is_file()
    ):
        raise SystemExit(
            "A full --execute run must reuse an existing reviewed dry-plan session. "
            "First run without --execute, then copy the exact --session-dir command "
            "printed by the dry plan."
        )
    session_dir.mkdir(parents=True, exist_ok=True)
    expected_hash = plan_hash(planned)
    expected_fingerprint = configuration_fingerprint(config, resolved)
    if manifest_path.exists():
        manifest = load_json(manifest_path)
        if str(manifest.get("plan_hash", "")) != expected_hash:
            raise SystemExit(
                "The existing session plan differs from the selected config. "
                "Resume with the original config or choose a new session directory."
            )
        try:
            actual_manifest_plan_hash = plan_hash(manifest["samples"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SystemExit(f"The existing session sample plan is invalid: {exc}") from exc
        if actual_manifest_plan_hash != expected_hash:
            raise SystemExit(
                "The existing session sample list no longer matches its immutable "
                "plan hash. Do not resume this session."
            )
        if manifest.get("configuration_fingerprint") != expected_fingerprint:
            raise SystemExit(
                "The stage/camera/config/calibration fingerprint differs from this session. "
                "For physical safety and provenance, start a new session directory."
            )
        session_calibration = Path(str(manifest.get("stereo_calibration", "")))
        if (
            not session_calibration.exists()
            or file_sha256(session_calibration)
            != str(expected_fingerprint["stereo_calibration_sha256"])
        ):
            raise SystemExit(
                "The immutable session calibration copy is missing or has changed. "
                "Do not resume this session."
            )
        session_stage_config = Path(str(manifest.get("stage_config_copy", "")))
        if (
            not session_stage_config.exists()
            or file_sha256(session_stage_config)
            != str(expected_fingerprint["stage_config_sha256"])
        ):
            raise SystemExit(
                "The immutable session stage-config copy is missing or has changed. "
                "Do not resume this session."
            )
        if str(resolved.get("evaluation_frame", "camera_relative")) == "pat":
            session_transform = Path(
                str(manifest.get("camera_to_pat_transform", ""))
            )
            expected_transform_hash = str(
                expected_fingerprint.get("camera_to_pat_transform_sha256", "")
            )
            if (
                not session_transform.is_file()
                or not expected_transform_hash
                or file_sha256(session_transform) != expected_transform_hash
            ):
                raise SystemExit(
                    "The immutable session camera-to-PAT transform copy is missing "
                    "or has changed. Do not resume this session."
                )
        print(f"[RESUME] {session_dir}")
        return session_dir, manifest_path, manifest

    inputs_dir = session_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    session_calibration = inputs_dir / "stereo_calibration.npz"
    session_stage_config = inputs_dir / "ossila_stage_config.md"
    shutil.copy2(Path(resolved["stereo_calibration"]), session_calibration)
    shutil.copy2(Path(resolved["stage_config"]), session_stage_config)
    session_transform: Path | None = None
    if str(resolved.get("evaluation_frame", "camera_relative")) == "pat":
        session_transform = inputs_dir / "camera_to_pat_transform.json"
        shutil.copy2(
            Path(resolved["camera_to_pat_transform"]),
            session_transform,
        )
    manifest = {
        "schema_version": 2,
        "name": str(config.get("name", "stereo_stage_accuracy")),
        "session_dir": str(session_dir),
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "status": "planned" if not args.execute else "initializing",
        "config_source": str(config_path),
        "stage_config_source": str(resolved["stage_config"]),
        "stage_config_copy": str(session_stage_config),
        "stage_axis": str(resolved["stage_axis"]),
        "stage_serial": str(resolved["stage_serial"]),
        "stage_direction": int(resolved["stage_direction"]),
        "stereo_calibration_source": str(resolved["stereo_calibration"]),
        "stereo_calibration": str(session_calibration),
        "evaluation_frame": str(
            resolved.get("evaluation_frame", "camera_relative")
        ),
        "camera_to_pat_transform_source": (
            str(resolved["camera_to_pat_transform"])
            if session_transform is not None
            else None
        ),
        "camera_to_pat_transform": (
            str(session_transform) if session_transform is not None else None
        ),
        "configuration_fingerprint": expected_fingerprint,
        "coordinate_system": (
            "Stereo triangulation is stored in the left OpenCV camera frame "
            "(X right, Y down, Z forward), mm. Final evaluation_frame selects "
            "PAT coordinates with a per-readback moving-camera translation, or "
            "the legacy reference-relative camera evaluation."
        ),
        "plan_hash": expected_hash,
        "samples": planned,
    }
    atomic_write_json(session_dir / "session_config.json", config)
    atomic_write_json(manifest_path, manifest)
    return session_dir, manifest_path, manifest


def update_manifest(
    manifest_path: Path,
    manifest: dict[str, Any],
    *,
    status: str | None = None,
) -> None:
    manifest["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    if status is not None:
        manifest["status"] = status
    atomic_write_json(manifest_path, manifest)


def print_plan(config: dict[str, Any], samples: list[dict[str, Any]], resolved: dict[str, Any]) -> None:
    stage = config["stage"]
    configured_positions = [
        float(value) for value in stage["positions_global_mm"]
    ]
    scan_order = str(
        config["experiment"].get("scan_order", "ascending_global")
    ).strip().lower()
    positions = (
        configured_positions
        if scan_order == "configured"
        else sorted(configured_positions)
    )
    direction = int(resolved["stage_direction"])
    datum = float(stage["datum_mm"])
    raw = [datum + direction * value for value in positions]
    counts: dict[str, int] = {}
    for sample in samples:
        counts[str(sample["kind"])] = counts.get(str(sample["kind"]), 0) + 1
    print("=== Stereo stage accuracy plan ===")
    print(
        f"Stage logical axis={resolved['stage_axis'].upper()} "
        f"USB serial={resolved['stage_serial']} direction={direction:+d}"
    )
    print(
        f"Forward global stations ({scan_order}) [mm]: "
        + ", ".join(f"{value:g}" for value in positions)
    )
    print(
        "Hardware targets [mm]: "
        + ", ".join(f"{value:g}" for value in raw)
    )
    print(
        f"Soft limits [mm]: [{float(stage['hardware_min_mm']):g}, "
        f"{float(stage['hardware_max_mm']):g}]"
    )
    reporting = config.get("distance_reporting", {})
    intended_reference = float(
        reporting.get(
            "intended_reference_hardware_mm",
            datum + direction * float(stage["reference_global_mm"]),
        )
    )
    intended_sweep = [
        float(value)
        for value in reporting.get("intended_sweep_hardware_mm", [intended_reference])
    ]
    actual_reference = datum + direction * float(stage["reference_global_mm"])
    print(
        f"Intended hardware sweep [mm]: "
        f"{', '.join(f'{value:g}' for value in intended_sweep)}; "
        f"intended reference={intended_reference:g}"
    )
    if (
        not math.isclose(actual_reference, intended_reference, rel_tol=0.0, abs_tol=1e-9)
        or any(
            not any(
                math.isclose(
                    intended,
                    actual,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                for actual in raw
            )
            for intended in intended_sweep
        )
    ):
        print(
            "[PLAN][NOTICE] The executable targets do not yet cover the intended "
            "hardware sweep. Check datum, station order, soft limits, and the "
            "verified home/end-switch clearance before execution."
        )
    print(f"Samples: {len(samples)} ({', '.join(f'{key}={value}' for key, value in counts.items())})")
    moving_body, physical_vector, apparent_vector = experiment_motion_vectors(config)
    print(
        f"Moving body={moving_body}; physical motion per +global mm in PAT frame: "
        f"{physical_vector.tolist()}"
    )
    print(
        "Stationary target apparent motion per +global mm in PAT frame: "
        f"{apparent_vector.tolist()}"
    )
    evaluation_frame = str(
        resolved.get("evaluation_frame", "camera_relative")
    )
    print(f"Analysis evaluation frame: {evaluation_frame}")
    if evaluation_frame == "pat":
        transform_data = resolved["camera_to_pat_data"]
        print(
            "Camera-to-PAT transform: "
            f"{resolved['camera_to_pat_transform']} "
            f"(registered stage readback="
            f"{float(transform_data['stage_hardware_readback_mm']):.6f} mm; "
            "translation will be updated from every capture readback)"
        )
    if str(config["target"].get("mode", "pat")).lower() == "pat":
        centre = np.asarray(config["target"]["pat_center_mm"], dtype=np.float64)
        zero = np.asarray(
            config["target"]["acoustools_zero_in_pat_mm"],
            dtype=np.float64,
        )
        print(
            f"PAT centre [mm]: {centre.tolist()}; "
            f"AcousTools command [mm]: {(centre - zero).tolist()}; "
            f"AcousTools [0,0,0] maps to PAT {zero.tolist()} mm"
        )


class SafeOssilaAxis:
    """Strict frame-level single-axis adapter around the supplied port finder."""

    def __init__(self, config: dict[str, Any], resolved: dict[str, Any]):
        sample_dir = Path(resolved["sample_dir"])
        if str(sample_dir) not in sys.path:
            sys.path.insert(0, str(sample_dir))
        try:
            from ossila_stage.stage import OssilaStage
        except ModuleNotFoundError as exc:
            if exc.name == "serial":
                raise RuntimeError(
                    "pyserial is required for Ossila control. Run "
                    f"{sys.executable} -m pip install pyserial"
                ) from exc
            raise
        self.config = config["stage"]
        self.axis = str(resolved["stage_axis"])
        self.direction = int(resolved["stage_direction"])
        self.datum_mm = float(self.config["datum_mm"])
        self.hardware_min_mm = float(self.config["hardware_min_mm"])
        self.hardware_max_mm = float(self.config["hardware_max_mm"])
        self.command_timeout_sec = float(self.config["command_timeout_sec"])
        self.usb_serial = str(resolved["stage_serial"])
        self.reported_length_mm: float | None = None
        self.recovery_target_global_mm: float | None = None
        try:
            self.device = OssilaStage(
                serial_number=self.usb_serial,
                direction=self.direction,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Could not open Ossila stage USB serial {self.usb_serial!r}: "
                f"{exc}. Close or disconnect Ossila Motion Control Console and "
                "any other program using the stage COM port, then retry."
            ) from exc
        self.device.ser.write_timeout = self.command_timeout_sec
        self.closed = False
        self.protocol_log: list[dict[str, Any]] = []
        self.receive_buffer = bytearray()
        try:
            self.device.ser.reset_input_buffer()
            self.device.ser.reset_output_buffer()
        except Exception:
            self.device.close()
            self.closed = True
            raise

    def hardware_from_global(self, global_mm: float) -> float:
        return self.datum_mm + self.direction * float(global_mm)

    def global_from_hardware(self, hardware_mm: float) -> float:
        return (float(hardware_mm) - self.datum_mm) / self.direction

    def within_soft_limits(
        self,
        hardware_mm: float,
        *,
        readback_allowance_mm: float = 0.0,
    ) -> bool:
        """Check a command strictly, or a measured readback with allowance."""
        allowance = max(0.0, float(readback_allowance_mm))
        value = float(hardware_mm)
        return (
            self.hardware_min_mm - allowance
            <= value
            <= self.hardware_max_mm + allowance
        )

    @staticmethod
    def _frame_parts(frame: str, expected_name: str | None = None) -> list[str]:
        text = str(frame).strip()
        if not (text.startswith("<") and text.endswith(">")):
            raise RuntimeError(f"Invalid Ossila frame delimiters: {frame!r}")
        parts = text[1:-1].strip().split()
        if not parts:
            raise RuntimeError(f"Empty Ossila frame: {frame!r}")
        if expected_name is not None and parts[0].lower() != expected_name.lower():
            raise RuntimeError(
                f"Expected Ossila <{expected_name} ...>, received {frame!r}"
            )
        return parts

    @staticmethod
    def _float_token(token: str, field: str) -> float:
        if not FLOAT_PATTERN.fullmatch(str(token)):
            raise RuntimeError(f"Invalid Ossila {field} float: {token!r}")
        value = float(token)
        if not math.isfinite(value):
            raise RuntimeError(f"Non-finite Ossila {field}: {token!r}")
        return value

    @staticmethod
    def normalize_reported_length_mm(
        raw_length: float,
        expected_length_mm: float,
    ) -> tuple[float, str]:
        """Normalize known firmware length units only when locked by expected travel."""
        raw = float(raw_length)
        expected = float(expected_length_mm)
        if math.isclose(raw, expected, abs_tol=0.5):
            return raw, "raw_mm"
        scaled = raw / 1000.0
        if math.isclose(scaled, expected, abs_tol=0.5):
            return scaled, "raw_um_normalized_to_mm"
        raise RuntimeError(
            f"Configured travel is {expected:.3f} mm but <length?> reports raw "
            f"{raw:.6f}; neither raw mm nor raw/1000 matches. Refusing motion."
        )

    def _exchange(
        self,
        command: str,
        *,
        expected_names: set[str],
        timeout_sec: float | None = None,
    ) -> str:
        """Write one command and return one complete expected ``<...>`` frame."""
        if self.closed:
            raise RuntimeError("Ossila serial connection is closed.")
        serial_port = self.device.ser
        timeout = self.command_timeout_sec if timeout_sec is None else float(timeout_sec)
        try:
            serial_port.reset_input_buffer()
            self.receive_buffer.clear()
            serial_port.write(f"<{command.lower()}>".encode("ascii"))
        except Exception as exc:
            raise RuntimeError(f"Ossila write failed for {command!r}: {exc}") from exc
        return self._read_expected_frame(
            expected_names=expected_names,
            timeout_sec=timeout,
            command_label=command,
        )

    def _read_expected_frame(
        self,
        *,
        expected_names: set[str],
        timeout_sec: float,
        command_label: str,
    ) -> str:
        """Read an expected frame without sending another command.

        Ossila ``<home>`` first returns ``<homing>`` and later emits ``<home>``
        asynchronously.  This reader preserves any complete frames received in
        the same serial chunk so the completion frame cannot be discarded.
        """
        if self.closed:
            raise RuntimeError("Ossila serial connection is closed.")
        serial_port = self.device.ser
        timeout = float(timeout_sec)
        deadline = time.monotonic() + timeout
        buffer = self.receive_buffer
        ignored: list[str] = []
        while time.monotonic() < deadline:
            while True:
                start = buffer.find(b"<")
                if start < 0:
                    buffer.clear()
                    break
                if start > 0:
                    del buffer[:start]
                end = buffer.find(b">", 1)
                if end < 0:
                    break
                raw = bytes(buffer[: end + 1])
                del buffer[: end + 1]
                frame = raw.decode("ascii", errors="strict")
                parts = self._frame_parts(frame)
                name = parts[0].lower()
                if name == "invalid":
                    raise RuntimeError(
                        f"Ossila rejected {command_label!r}: {frame}"
                    )
                if (
                    name == "pos"
                    and len(parts) >= 2
                    and parts[1].lower() == "unknown"
                ):
                    raise RuntimeError(
                        f"Ossila refused {command_label!r}: {frame}. "
                        "Home the axis first."
                    )
                if name in expected_names:
                    self.protocol_log.append(
                        {
                            "command": command_label,
                            "response": frame,
                            "ignored_frames": ignored,
                        }
                    )
                    self.protocol_log = self.protocol_log[-200:]
                    return frame
                ignored.append(frame)
            try:
                waiting = int(getattr(serial_port, "in_waiting", 0) or 0)
                chunk = serial_port.read(max(1, min(256, waiting)))
            except Exception as exc:
                raise RuntimeError(
                    f"Ossila read failed for {command_label!r}: {exc}"
                ) from exc
            if not chunk:
                continue
            buffer.extend(chunk)
        raise TimeoutError(
            f"Timed out waiting for {sorted(expected_names)} after Ossila "
            f"{command_label!r}; "
            f"ignored={ignored!r}, partial={bytes(buffer)!r}"
        )

    def query_float(self, name: str) -> tuple[float, str]:
        frame = self._exchange(f"{name}?", expected_names={name.lower()})
        parts = self._frame_parts(frame, name)
        if len(parts) != 2:
            raise RuntimeError(f"Expected two fields in Ossila {name}? response: {frame!r}")
        return self._float_token(parts[1], name), frame

    def raw_position_mm(self) -> tuple[float, str]:
        return self.query_float("pos")

    def global_position_mm(self) -> tuple[float, float, str]:
        raw, response = self.raw_position_mm()
        return self.global_from_hardware(raw), raw, response

    def query_status(self) -> dict[str, Any]:
        frame = self._exchange("status?", expected_names={"status"})
        parts = self._frame_parts(frame, "status")
        if len(parts) != 5:
            raise RuntimeError(
                "Expected <status speed position home end>, "
                f"received {frame!r}"
            )
        speed = self._float_token(parts[1], "status speed")
        position = self._float_token(parts[2], "status position")
        flags: list[bool] = []
        for label, token in (("home", parts[3]), ("end", parts[4])):
            if token not in {"0", "1"}:
                raise RuntimeError(f"Invalid Ossila status {label} flag: {frame!r}")
            flags.append(token == "1")
        return {
            "frame": frame,
            "speed_mm_s": speed,
            "position_hardware_mm": position,
            "home_switch": flags[0],
            "end_switch": flags[1],
        }

    def query_posmode(self) -> tuple[str, str]:
        frame = self._exchange("posmode?", expected_names={"posmode"})
        parts = self._frame_parts(frame, "posmode")
        if len(parts) != 2 or parts[1].lower() not in {"relative", "absolute"}:
            raise RuntimeError(f"Invalid Ossila posmode response: {frame!r}")
        return parts[1].lower(), frame

    def query_text(self, name: str) -> tuple[str, str]:
        frame = self._exchange(f"{name}?", expected_names={name.lower()})
        parts = self._frame_parts(frame, name)
        if len(parts) < 2:
            raise RuntimeError(f"Empty Ossila {name}? response: {frame!r}")
        return " ".join(parts[1:]), frame

    def query_alarms(self) -> tuple[list[str], str]:
        frame = self._exchange("alarms?", expected_names={"alarms"})
        parts = self._frame_parts(frame, "alarms")
        values = parts[1:]
        if len(values) == 1 and values[0].lower() in {"0", "none", "clear", "ok"}:
            values = []
        return values, frame

    def wait_until_stopped(self, *, timeout_sec: float | None = None) -> list[dict[str, Any]]:
        timeout = (
            float(self.config["motion_timeout_sec"])
            if timeout_sec is None
            else float(timeout_sec)
        )
        poll_sec = max(0.01, float(self.config.get("status_poll_sec", 0.05)))
        started = time.monotonic()
        stable_zero_count = 0
        responses: list[dict[str, Any]] = []
        while time.monotonic() - started <= timeout:
            status = self.query_status()
            responses.append(status)
            if abs(float(status["speed_mm_s"])) <= 1e-9:
                stable_zero_count += 1
                if stable_zero_count >= 2:
                    return responses
            else:
                stable_zero_count = 0
            time.sleep(poll_sec)
        raise TimeoutError(f"Ossila motion exceeded {timeout:.3f} s.")

    def require_clear_alarms(self) -> str:
        alarms, frame = self.query_alarms()
        if alarms and bool(self.config["require_clear_alarms"]):
            raise RuntimeError(f"Ossila reports active alarms: {alarms!r} ({frame})")
        return frame

    def require_absolute_mode(self) -> str:
        mode, frame = self.query_posmode()
        if mode != "absolute":
            raise RuntimeError(
                "Ossila stage is not homed (posmode is relative). "
                "After clearing the complete HOME and HOME-to-reference paths, "
                "rerun with the explicit homing option for the calling workflow "
                "(--home-depth-axis for the single-axis workflow, or "
                "--home-axes AXIS... for the three-axis workflow)."
            )
        return frame

    def preflight(
        self,
        *,
        require_absolute: bool,
        allow_end_switch_for_homing: bool = False,
        allow_outside_soft_limit_for_homing: bool = False,
    ) -> dict[str, Any]:
        device_text, device_frame = self.query_text("device")
        firmware_text, firmware_frame = self.query_text("firmware")
        stage_serial, serial_frame = self.query_text("serial")
        length_raw, length_frame = self.query_float("length")
        speed_mm_s, speed_frame = self.query_float("speed")
        acceleration, acceleration_frame = self.query_float("acc")
        deceleration, deceleration_frame = self.query_float("dec")
        alarms, alarms_frame = self.query_alarms()
        posmode, posmode_frame = self.query_posmode()
        status = self.query_status()
        expected_device = str(
            self.config.get("expected_device_response", "")
        ).strip()
        if not expected_device:
            raise RuntimeError(
                "stage.expected_device_response is empty. Copy the exact product "
                "code from the identity probe before any motion."
            )
        if device_text != expected_device:
            raise RuntimeError(
                f"Stage <device?> mismatch: expected {expected_device!r}, "
                f"got {device_text!r}."
            )
        expected_length = float(self.config["expected_travel_mm"])
        length_mm, length_interpretation = self.normalize_reported_length_mm(
            length_raw,
            expected_length,
        )
        self.reported_length_mm = length_mm
        if self.hardware_max_mm > length_mm or self.hardware_min_mm < 0:
            raise RuntimeError("Configured soft limits exceed the stage-reported travel.")
        expected_serial = str(self.config.get("expected_stage_serial_response", "")).strip()
        if not expected_serial:
            raise RuntimeError(
                "stage.expected_stage_serial_response is empty. Copy the serial "
                "reported for the physically labelled depth stage into the config "
                "before any motion."
            )
        if stage_serial != expected_serial:
            raise RuntimeError(
                f"Stage <serial?> mismatch: expected {expected_serial!r}, got {stage_serial!r}."
            )
        expected_acceleration = self.config.get("expected_acceleration_mm_s2")
        expected_deceleration = self.config.get("expected_deceleration_mm_s2")
        if expected_acceleration is None or expected_deceleration is None:
            raise RuntimeError(
                "Expected Ossila acceleration/deceleration are unset. Read the "
                "physically approved Motion Console values into the config first."
            )
        settings_tolerance = float(self.config["settings_match_tolerance_mm_s2"])
        if not math.isclose(
            acceleration,
            float(expected_acceleration),
            abs_tol=settings_tolerance,
        ):
            raise RuntimeError(
                f"Ossila acceleration mismatch: expected {float(expected_acceleration):.6f}, "
                f"got {acceleration:.6f} mm/s^2."
            )
        if not math.isclose(
            deceleration,
            float(expected_deceleration),
            abs_tol=settings_tolerance,
        ):
            raise RuntimeError(
                f"Ossila deceleration mismatch: expected {float(expected_deceleration):.6f}, "
                f"got {deceleration:.6f} mm/s^2."
            )
        if alarms and bool(self.config["require_clear_alarms"]):
            raise RuntimeError(f"Ossila reports active alarms: {alarms!r}")
        if abs(float(status["speed_mm_s"])) > 1e-9:
            self.emergency_stop()
            raise RuntimeError("Ossila stage was already moving during preflight.")
        current_hardware = float(status["position_hardware_mm"])
        readback_allowance = float(self.config["readback_tolerance_mm"])
        if (
            current_hardware < -readback_allowance
            or current_hardware > length_mm + readback_allowance
        ):
            raise RuntimeError(
                f"Ossila status position {current_hardware:.6f} mm is outside "
                f"reported travel [0, {length_mm:.6f}] mm even with "
                f"+/-{readback_allowance:.6f} mm readback allowance."
            )
        if (
            not self.within_soft_limits(
                current_hardware,
                readback_allowance_mm=readback_allowance,
            )
            and not allow_outside_soft_limit_for_homing
        ):
            raise RuntimeError(
                f"Current Ossila position {current_hardware:.6f} mm is outside "
                f"experiment soft limits [{self.hardware_min_mm:.6f}, "
                f"{self.hardware_max_mm:.6f}] mm with "
                f"+/-{readback_allowance:.6f} mm readback allowance. "
                "Explicit homing is required "
                "for this axis; after clearing both paths, use --home-depth-axis "
                "in the single-axis workflow or --home-axes AXIS... in the "
                "three-axis workflow."
            )
        if bool(status["end_switch"]) and not allow_end_switch_for_homing:
            raise RuntimeError(
                "Ossila end switch is active. Inspect the setup and home the axis before use."
            )
        if require_absolute and posmode != "absolute":
            raise RuntimeError(
                "Ossila posmode is relative (not homed); absolute goto is unavailable."
            )
        return {
            "usb_serial": self.usb_serial,
            "device": device_text,
            "device_response": device_frame,
            "firmware": firmware_text,
            "firmware_response": firmware_frame,
            "stage_serial": stage_serial,
            "serial_response": serial_frame,
            "length_mm": length_mm,
            "length_raw_response_value": length_raw,
            "length_unit_interpretation": length_interpretation,
            "length_response": length_frame,
            "stored_speed_mm_s": speed_mm_s,
            "speed_response": speed_frame,
            "acceleration_mm_s2": acceleration,
            "acceleration_response": acceleration_frame,
            "deceleration_mm_s2": deceleration,
            "deceleration_response": deceleration_frame,
            "alarms": alarms,
            "alarms_response": alarms_frame,
            "posmode": posmode,
            "posmode_response": posmode_frame,
            "status": status,
        }

    def authorize_recovery_move(self, global_mm: float) -> None:
        """Authorize one post-home move from hardware 0 into the soft-limit interval."""
        hardware_mm = self.hardware_from_global(global_mm)
        if not self.within_soft_limits(hardware_mm):
            raise RuntimeError("Recovery target must be inside configured soft limits.")
        self.recovery_target_global_mm = float(global_mm)

    def move_global(self, global_mm: float) -> dict[str, Any]:
        hardware_mm = self.hardware_from_global(global_mm)
        if not self.within_soft_limits(hardware_mm):
            raise RuntimeError(
                f"Refusing move outside soft limits: global={global_mm:.6f}, "
                f"hardware={hardware_mm:.6f}, limits="
                f"[{self.hardware_min_mm:.6f}, {self.hardware_max_mm:.6f}] mm"
            )
        self.require_absolute_mode()
        self.require_clear_alarms()
        departure_global, departure_hardware, departure_response = (
            self.global_position_mm()
        )
        readback_allowance = float(self.config["readback_tolerance_mm"])
        if self.reported_length_mm is not None and not (
            -readback_allowance
            <= departure_hardware
            <= self.reported_length_mm + readback_allowance
        ):
            raise RuntimeError(
                f"Current hardware position {departure_hardware:.6f} mm is outside "
                "the preflight-reported travel even with "
                f"+/-{readback_allowance:.6f} mm readback allowance."
            )
        recovery_authorized = (
            self.recovery_target_global_mm is not None
            and math.isclose(
                float(global_mm),
                self.recovery_target_global_mm,
                abs_tol=1e-9,
            )
        )
        if not self.within_soft_limits(
            departure_hardware,
            readback_allowance_mm=readback_allowance,
        ):
            if not recovery_authorized:
                raise RuntimeError(
                    f"Refusing move from current position {departure_hardware:.6f} mm "
                    f"outside soft limits [{self.hardware_min_mm:.6f}, "
                    f"{self.hardware_max_mm:.6f}] mm with "
                    f"+/-{readback_allowance:.6f} mm readback allowance."
                )
        self.recovery_target_global_mm = None
        speed_value = float(self.config["speed_mm_s"])
        try:
            command_response = self._exchange(
                f"goto {hardware_mm} {speed_value}",
                expected_names={"goto"},
            )
            command_parts = self._frame_parts(command_response, "goto")
            if len(command_parts) != 3:
                raise RuntimeError(f"Unexpected Ossila goto echo: {command_response!r}")
            echoed_position = self._float_token(command_parts[1], "goto position")
            echoed_speed = self._float_token(command_parts[2], "goto speed")
            if not math.isclose(echoed_position, hardware_mm, abs_tol=1e-6):
                raise RuntimeError(f"Ossila goto position echo mismatch: {command_response!r}")
            if not math.isclose(echoed_speed, speed_value, abs_tol=1e-6):
                raise RuntimeError(f"Ossila goto speed echo mismatch: {command_response!r}")
            status_responses = self.wait_until_stopped()
        except Exception:
            self.emergency_stop()
            raise
        if bool(status_responses[-1]["end_switch"]):
            self.emergency_stop()
            raise RuntimeError("Ossila end switch became active during an in-range move.")
        settle_sec = float(self.config.get("settle_sec", 0.0))
        if settle_sec > 0:
            time.sleep(settle_sec)
        readback_global, readback_raw, readback_response = self.global_position_mm()
        readback_error = readback_global - float(global_mm)
        tolerance = float(self.config["readback_tolerance_mm"])
        if abs(readback_error) > tolerance:
            self.stop()
            raise RuntimeError(
                f"Stage readback error {readback_error:+.6f} mm exceeds "
                f"{tolerance:.6f} mm; stop command sent."
            )
        return {
            "command_global_mm": float(global_mm),
            "command_hardware_mm": hardware_mm,
            "departure_global_mm": departure_global,
            "departure_hardware_mm": departure_hardware,
            "departure_response": departure_response,
            "post_home_recovery_move": recovery_authorized,
            "goto_response": str(command_response),
            "last_status": status_responses[-1] if status_responses else {},
            "readback_global_mm": readback_global,
            "readback_hardware_mm": readback_raw,
            "readback_error_mm": readback_error,
            "readback_response": readback_response,
            "arrived_at": dt.datetime.now().isoformat(timespec="milliseconds"),
        }

    def home(self) -> dict[str, Any]:
        self.require_clear_alarms()
        response = self._exchange("home", expected_names={"homing", "home"})
        response_name = self._frame_parts(response)[0].lower()
        completion_response = response
        try:
            if response_name == "homing":
                completion_response = self._read_expected_frame(
                    expected_names={"home"},
                    timeout_sec=float(self.config["motion_timeout_sec"]),
                    command_label="home completion",
                )
        except Exception:
            self.emergency_stop()
            raise
        mode, mode_response = self.query_posmode()
        if mode != "absolute":
            self.emergency_stop()
            raise RuntimeError("Ossila homing stopped but posmode is not absolute.")
        global_mm, raw_mm, raw_response = self.global_position_mm()
        tolerance = float(self.config["home_readback_tolerance_mm"])
        if abs(raw_mm) > tolerance:
            self.emergency_stop()
            raise RuntimeError(
                f"Ossila home readback {raw_mm:.6f} mm exceeds {tolerance:.6f} mm."
            )
        final_status = self.query_status()
        if abs(float(final_status["speed_mm_s"])) > 1e-9:
            self.emergency_stop()
            raise RuntimeError("Ossila homing completion was received while still moving.")
        status_position = float(final_status["position_hardware_mm"])
        if abs(status_position) > tolerance:
            self.emergency_stop()
            raise RuntimeError(
                f"Ossila home status position {status_position:.6f} mm exceeds "
                f"{tolerance:.6f} mm."
            )
        if bool(final_status["end_switch"]):
            self.emergency_stop()
            raise RuntimeError(
                "Ossila end switch is active after HOME completion."
            )
        self.require_clear_alarms()
        return {
            "home_response": str(response),
            "home_completion_response": str(completion_response),
            "last_status": final_status,
            "verified_status": final_status,
            "home_switch_active_after_completion": bool(
                final_status["home_switch"]
            ),
            "posmode": mode,
            "posmode_response": mode_response,
            "readback_global_mm": global_mm,
            "readback_hardware_mm": raw_mm,
            "readback_response": raw_response,
        }

    def device_snapshot(
        self,
        preflight_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if preflight_data is not None:
            result.update(preflight_data)
        else:
            try:
                result.update(self.preflight(require_absolute=False))
            except Exception as exc:
                result["preflight_error"] = str(exc)
        try:
            global_mm, raw_mm, raw_response = self.global_position_mm()
            result.update(
                {
                    "position_global_mm": global_mm,
                    "position_hardware_mm": raw_mm,
                    "position_response": raw_response,
                }
            )
        except Exception as exc:
            result["position_error"] = str(exc)
        result["protocol_log_tail"] = self.protocol_log[-20:]
        return result

    def emergency_stop(self) -> bool:
        if self.closed:
            return False
        errors: list[str] = []
        try:
            self._exchange("stop", expected_names={"stop"}, timeout_sec=0.5)
            self.wait_until_stopped(timeout_sec=2.0)
            return True
        except Exception as exc:
            errors.append(f"stop: {exc}")
            try:
                self._exchange("hardstop", expected_names={"hardstop"}, timeout_sec=0.5)
                self.wait_until_stopped(timeout_sec=2.0)
                return True
            except Exception as hard_exc:
                errors.append(f"hardstop: {hard_exc}")
        print(
            "[STAGE][EMERGENCY] Software stop could not be verified: "
            + " | ".join(errors)
            + ". Disconnect the accessible stage power immediately.",
            file=sys.stderr,
        )
        return False

    def stop(self) -> None:
        self.emergency_stop()

    def close(self) -> None:
        if self.closed:
            return
        try:
            self.device.close()
        finally:
            self.closed = True


class PatTarget:
    def __init__(self, config: dict[str, Any]):
        target = config["target"]
        try:
            from acoustools.Levitator import LevitatorController
            from acoustools_eventcam_sync import compute_holograms_for_positions, mute_sync_transducer
        except Exception as exc:
            raise RuntimeError(f"Could not import AcousTools/PAT control: {exc}") from exc
        self.target_config = target
        self.compute_holograms = compute_holograms_for_positions
        self.mute_sync_transducer = mute_sync_transducer
        self.controller: Any | None = None
        self.current_hologram: Any | None = None
        self.acoustools_zero_in_pat_mm = np.asarray(
            target["acoustools_zero_in_pat_mm"],
            dtype=np.float64,
        )
        self.current_mm = np.asarray(target["pat_center_mm"], dtype=np.float64)
        try:
            self.controller = LevitatorController(
                ids=tuple(int(item) for item in target["controller_ids"])
            )
            self.current_hologram = self._hologram(self.current_mm)
            self.controller.levitate(self.current_hologram)
            time.sleep(max(0.2, float(target.get("settle_sec", 0.0))))
        except BaseException:
            self.turn_off()
            raise

    def _hologram(self, position_mm: np.ndarray) -> Any:
        position_m = tuple(
            (
                (np.asarray(position_mm, dtype=np.float64) - self.acoustools_zero_in_pat_mm)
                * 1e-3
            ).tolist()
        )
        return self.mute_sync_transducer(self.compute_holograms([position_m])[0])

    def move_to_mm(self, target_mm: list[float]) -> None:
        from pat_stereo_grid_capture import move_particle

        destination = np.asarray(target_mm, dtype=np.float64)
        start_m = tuple(
            ((self.current_mm - self.acoustools_zero_in_pat_mm) * 1e-3).tolist()
        )
        end_m = tuple(
            ((destination - self.acoustools_zero_in_pat_mm) * 1e-3).tolist()
        )
        current_m, hologram = move_particle(
            self.controller,
            start_m,
            end_m,
            float(self.target_config["transfer_step_mm"]),
            float(self.target_config["transfer_dwell_sec"]),
        )
        self.current_mm = (
            np.asarray(current_m, dtype=np.float64) * 1e3
            + self.acoustools_zero_in_pat_mm
        )
        self.current_hologram = hologram
        settle_sec = float(self.target_config.get("settle_sec", 0.0))
        if settle_sec > 0:
            time.sleep(settle_sec)

    def turn_off(self) -> bool:
        if self.controller is None:
            return True
        off_sent = False
        errors: list[str] = []
        for attempt in range(2):
            try:
                self.controller.turn_off()
                off_sent = True
                break
            except BaseException as exc:
                errors.append(f"turn_off attempt {attempt + 1}: {exc}")
        disconnected = False
        try:
            self.controller.disconnect()
            disconnected = True
        except BaseException as exc:
            errors.append(f"disconnect: {exc}")
        self.controller = None
        if not (off_sent or disconnected):
            print(
                "[PAT][EMERGENCY] Could not verify transducer OFF/disconnect: "
                + " | ".join(errors)
                + ". Disconnect the accessible PAT power immediately.",
                file=sys.stderr,
            )
            return False
        if errors:
            print("[PAT][WARN] cleanup recovered after: " + " | ".join(errors))
        return True


class ManualTarget:
    def __init__(self, config: dict[str, Any]):
        self.current_mm = np.asarray(config["target"]["pat_center_mm"], dtype=np.float64)

    def move_to_mm(self, target_mm: list[float]) -> None:
        self.current_mm = np.asarray(target_mm, dtype=np.float64)
        input(
            "[MANUAL TARGET] Place the marker at PAT/local "
            f"{self.current_mm.tolist()} mm, wait for settling, then press Enter: "
        )

    def turn_off(self) -> bool:
        return True


def trace_cube_wireframe(target_driver: PatTarget, config: dict[str, Any]) -> None:
    """Trace all 12 edges; repeated connecting edges avoid any diagonal jump."""
    center = np.asarray(config["target"]["pat_center_mm"], dtype=np.float64)
    half = np.asarray(config["target"]["cube_half_extent_mm"], dtype=np.float64)
    lookup = {tuple(signs): center + half * np.asarray(signs) for signs in gray_cube_vertices()}
    a = (-1, -1, -1)
    b = (+1, -1, -1)
    c = (+1, +1, -1)
    d = (-1, +1, -1)
    e = (-1, -1, +1)
    f = (+1, -1, +1)
    g = (+1, +1, +1)
    h = (-1, +1, +1)
    path = [a, b, c, d, a, e, f, g, h, e, a, b, f, g, c, d, h]
    print("[PAT] tracing the 12 cuboid edges slowly (some connecting edges repeat)")
    for signs in path:
        target_driver.move_to_mm(lookup[signs].tolist())
    target_driver.move_to_mm(center.tolist())


def run_initial_preview(config: dict[str, Any]) -> bool:
    from stereo_eventcam_record_sync import run_preview

    camera = config["camera"]
    preview = camera.get("preview", {})
    return run_preview(
        left_serial=str(camera["left_serial"]),
        right_serial=str(camera["right_serial"]),
        sensor_width=int(camera.get("sensor_width", 1280)),
        sensor_height=int(camera.get("sensor_height", 720)),
        delta_t_us=int(preview.get("delta_t_us", 5000)),
        display_scale=float(preview.get("scale", 0.5)),
        point_size=int(preview.get("point_size", 1)),
        reopen_wait_sec=float(camera.get("camera_reopen_wait_sec", 1.0)),
    )


def run_camera_stream_preflight(
    config: dict[str, Any],
    calibration_path: Path,
    session_dir: Path,
) -> dict[str, Any]:
    """Open and stream both cameras through the real paired recorder before motion."""
    camera = config["camera"]
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    attempt_dir = session_dir / "camera_preflight_attempts" / stamp
    run_dir = attempt_dir / "recording"
    attempt_dir.mkdir(parents=True, exist_ok=False)
    duration_sec = max(0.05, min(float(camera["record_sec"]), 0.2))
    command = [
        sys.executable,
        str(SCRIPT_DIR / "stereo_eventcam_record_sync.py"),
        "--left-serial",
        str(camera["left_serial"]),
        "--right-serial",
        str(camera["right_serial"]),
        "--run-dir",
        str(run_dir),
        "--duration-sec",
        f"{duration_sec:.9g}",
        "--delta-t-us",
        str(int(camera["delta_t_us"])),
        "--start-delay-sec",
        f"{float(camera['start_delay_sec']):.9g}",
        "--hw-sync",
        str(camera["hw_sync"]),
        "--hw-sync-timeout-sec",
        f"{float(camera['hw_sync_timeout_sec']):.9g}",
        "--sensor-width",
        str(int(camera.get("sensor_width", 1280))),
        "--sensor-height",
        str(int(camera.get("sensor_height", 720))),
        "--max-events",
        "0",
        "--npz-compression",
        "none",
        "--stereo-calibration",
        str(calibration_path),
        "--note",
        "mandatory pre-motion stereo camera stream/synchronization preflight",
        "--no-preview",
    ]
    timeout_sec = max(
        60.0,
        float(camera["hw_sync_timeout_sec"])
        + float(camera["start_delay_sec"])
        + 45.0,
    )
    started_at = dt.datetime.now().isoformat(timespec="milliseconds")
    try:
        completed = subprocess.run(
            command,
            cwd=SCRIPT_DIR,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        command_result = {
            "command": command,
            "started_at": started_at,
            "finished_at": dt.datetime.now().isoformat(timespec="milliseconds"),
            "timeout_sec": timeout_sec,
            "returncode": int(completed.returncode),
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        command_result = {
            "command": command,
            "started_at": started_at,
            "finished_at": dt.datetime.now().isoformat(timespec="milliseconds"),
            "timeout_sec": timeout_sec,
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "error": "camera stream preflight timed out",
        }
        atomic_write_json(attempt_dir / "command_result.json", command_result)
        raise RuntimeError(
            f"Stereo camera stream preflight timed out after {timeout_sec:.1f} s; "
            "no apparatus motion was commanded."
        ) from exc
    atomic_write_json(attempt_dir / "command_result.json", command_result)
    if completed.returncode != 0:
        raise RuntimeError(
            "Stereo camera stream/synchronization preflight failed before motion. "
            f"See {attempt_dir / 'command_result.json'}"
        )

    recording_manifest_path = run_dir / "stereo_recording_manifest.json"
    if not recording_manifest_path.is_file():
        raise RuntimeError(
            "Stereo camera preflight returned success without its recording manifest."
        )
    recording_manifest = load_json(recording_manifest_path)
    configured_serials = {
        "left": str(camera["left_serial"]).strip(),
        "right": str(camera["right_serial"]).strip(),
    }
    for side, expected_serial in configured_serials.items():
        if str(recording_manifest.get(f"{side}_serial", "")).strip() != expected_serial:
            raise RuntimeError(
                f"Camera preflight {side} serial does not match the configured role."
            )
        result = recording_manifest.get(side, {})
        meta = result.get("meta", {}) if isinstance(result, dict) else {}
        if not bool(result.get("ok")):
            raise RuntimeError(f"Camera preflight {side} worker did not complete successfully.")
        if not bool(meta.get("capture_interval_complete")):
            raise RuntimeError(
                f"Camera preflight {side} did not complete the requested stream interval."
            )
        if str(meta.get("serial", "")).strip() != expected_serial:
            raise RuntimeError(f"Camera preflight {side} worker serial mismatch.")
    if (
        str(camera["hw_sync"]) != "off"
        and not bool(recording_manifest.get("hw_sync", {}).get("verified"))
    ):
        raise RuntimeError(
            "The configured hardware Master/Slave synchronization was not verified."
        )
    reopen_wait_sec = float(camera.get("camera_reopen_wait_sec", 1.0))
    if reopen_wait_sec > 0:
        time.sleep(reopen_wait_sec)
    return {
        "attempt_dir": str(attempt_dir),
        "recording_manifest": str(recording_manifest_path),
        "configured_camera_serials": configured_serials,
        "duration_sec": duration_sec,
        "hardware_sync_mode": str(camera["hw_sync"]),
        "hardware_sync_verified": bool(
            recording_manifest.get("hw_sync", {}).get("verified")
        ),
        "left_total_events": int(
            recording_manifest["left"]["meta"].get("total_events", 0)
        ),
        "right_total_events": int(
            recording_manifest["right"]["meta"].get("total_events", 0)
        ),
        "checked_at": dt.datetime.now().isoformat(timespec="milliseconds"),
    }


def preflight_capture_software(
    config: dict[str, Any],
    calibration_path: Path,
    session_dir: Path,
) -> dict[str, Any]:
    """Validate inputs, exact discovery, and paired camera streaming before motion."""
    required_scripts = [
        SCRIPT_DIR / "stereo_eventcam_record_sync.py",
        SCRIPT_DIR / "stereo_process_recording.py",
    ]
    missing_scripts = [str(path) for path in required_scripts if not path.is_file()]
    if missing_scripts:
        raise RuntimeError(f"Required capture scripts are missing: {missing_scripts}")
    calibration = load_stereo_calibration(calibration_path)

    if str(config["target"].get("mode", "pat")).lower() == "pat":
        try:
            from acoustools.Levitator import LevitatorController  # noqa: F401
            from acoustools_eventcam_sync import compute_holograms_for_positions  # noqa: F401
            from pat_stereo_grid_capture import move_particle, turn_off_pat  # noqa: F401
        except Exception as exc:
            raise RuntimeError(f"PAT software preflight failed: {exc}") from exc

    try:
        from stereo_eventcam_record_sync import list_devices

        devices = [str(device).strip() for device in list_devices()]
    except Exception as exc:
        raise RuntimeError(f"Stereo camera software/discovery preflight failed: {exc}") from exc
    camera = config["camera"]
    configured = {
        "left": str(camera["left_serial"]),
        "right": str(camera["right_serial"]),
    }
    if configured["left"] == configured["right"]:
        raise RuntimeError("Left and right camera serials must differ.")
    missing_cameras = [
        f"{side}:{serial}"
        for side, serial in configured.items()
        if serial.strip() not in devices
    ]
    if missing_cameras:
        raise RuntimeError(
            "Configured event cameras were not discovered: "
            f"{missing_cameras}; discovered={devices}"
        )
    stream_preflight = run_camera_stream_preflight(
        config,
        calibration_path,
        session_dir,
    )
    return {
        "calibration": str(calibration_path),
        "calibration_baseline_mm": float(np.linalg.norm(calibration["T"])),
        "camera_devices": devices,
        "configured_camera_serials": configured,
        "stream_preflight": stream_preflight,
        "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


def build_record_command(
    config: dict[str, Any],
    calibration: Path,
    run_dir: Path,
    sample: dict[str, Any],
) -> list[str]:
    camera = config["camera"]
    note = (
        f"stage accuracy {sample['sample_id']}; pass={sample['pass_name']}; "
        f"stage_global_mm={float(sample['stage_command_global_mm']):.6f}; "
        f"kind={sample['kind']}; vertex={sample['vertex_id']}"
    )
    command = [
        sys.executable,
        str(SCRIPT_DIR / "stereo_eventcam_record_sync.py"),
        "--left-serial",
        str(camera["left_serial"]),
        "--right-serial",
        str(camera["right_serial"]),
        "--run-dir",
        str(run_dir),
        "--duration-sec",
        str(float(camera["record_sec"])),
        "--delta-t-us",
        str(int(camera["delta_t_us"])),
        "--start-delay-sec",
        str(float(camera.get("start_delay_sec", 1.0))),
        "--hw-sync",
        str(camera.get("hw_sync", "left-master")),
        "--hw-sync-timeout-sec",
        str(float(camera.get("hw_sync_timeout_sec", 10.0))),
        "--npz-compression",
        str(camera.get("npz_compression", "none")),
        "--stereo-calibration",
        str(calibration),
        "--note",
        note,
        "--no-preview",
    ]
    max_events = int(camera.get("max_events", 0))
    if max_events > 0:
        command.extend(["--max-events", str(max_events)])
    return command


def read_capture_counts(run_dir: Path) -> tuple[int, int]:
    counts: list[int] = []
    for side in ("left", "right"):
        meta_path = run_dir / side / f"{side}_recording_meta.json"
        if not meta_path.exists():
            counts.append(0)
            continue
        try:
            payload = load_json(meta_path)
        except SystemExit:
            counts.append(0)
            continue
        counts.append(int(payload.get("total_events", 0) or 0))
    return counts[0], counts[1]


def process_recording(config: dict[str, Any], calibration: Path, run_dir: Path) -> None:
    tracking = config["tracking"]
    command = [
        sys.executable,
        str(SCRIPT_DIR / "stereo_process_recording.py"),
        str(run_dir),
        "--stereo-calibration",
        str(calibration),
        "--window-us",
        str(int(tracking["window_us"])),
        "--hop-us",
        str(int(tracking["hop_us"])),
        "--dt-us",
        str(float(tracking["dt_us"])),
        "--roi",
        str(tracking["roi"]),
        "--tracking-method",
        str(tracking["tracking_method"]),
        "--threshold-count",
        str(int(tracking["threshold_count"])),
        "--min-events",
        str(int(tracking["min_events"])),
        "--min-area",
        str(int(tracking["min_area"])),
        "--min-mass",
        str(int(tracking["min_mass"])),
        "--polarity",
        str(tracking["polarity"]),
        "--max-time-gap-sec",
        str(float(tracking.get("max_time_gap_sec", 0.001))),
    ]
    explicit_offset = tracking.get("right_time_offset_sec")
    if explicit_offset is not None:
        command.extend(["--right-time-offset-sec", str(float(explicit_offset))])
    print("[PROCESS] " + " ".join(command), flush=True)
    subprocess.run(command, check=True)


def capture_sample(
    *,
    config: dict[str, Any],
    calibration: Path,
    session_dir: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    sample: dict[str, Any],
    stage_arrival: dict[str, Any],
    stage_driver: SafeOssilaAxis,
    process_each: bool,
) -> bool:
    camera = config["camera"]
    attempts_this_run = int(camera["max_capture_attempts"])
    minimum_events = int(camera.get("minimum_events_per_camera", 1))
    sample_root = session_dir / "captures" / str(sample["sample_id"])
    sample_root.mkdir(parents=True, exist_ok=True)
    used_indices = [
        int(item.get("attempt", 0))
        for item in sample.get("attempts", [])
        if int(item.get("attempt", 0)) > 0
    ]
    for candidate in sample_root.glob("attempt_*"):
        try:
            used_indices.append(int(candidate.name.rsplit("_", 1)[-1]))
        except ValueError:
            continue
    first_attempt_index = max(used_indices, default=0) + 1
    sample["stage_arrival"] = stage_arrival
    sample["target_settled_at"] = dt.datetime.now().isoformat(timespec="milliseconds")
    update_manifest(manifest_path, manifest)

    for local_attempt in range(1, attempts_this_run + 1):
        attempt_index = first_attempt_index + local_attempt - 1
        run_dir = sample_root / f"attempt_{attempt_index:02d}"
        requested_stage = float(sample["stage_command_global_mm"])
        before_status = stage_driver.query_status()
        before_global, before_hardware, before_response = (
            stage_driver.global_position_mm()
        )
        before_error = before_global - requested_stage
        readback_tolerance = float(
            config["stage"]["readback_tolerance_mm"]
        )
        if abs(float(before_status["speed_mm_s"])) > 1e-9:
            raise RuntimeError(
                "Stage was moving immediately before camera recording."
            )
        if abs(before_error) > readback_tolerance:
            raise RuntimeError(
                "Stage readback immediately before recording differs from the "
                f"command by {before_error:+.6f} mm."
            )
        if bool(before_status["home_switch"]) or bool(
            before_status["end_switch"]
        ):
            raise RuntimeError(
                "A stage limit switch was active immediately before recording."
            )
        stage_before_capture = {
            "command_global_mm": requested_stage,
            "command_hardware_mm": stage_driver.hardware_from_global(
                requested_stage
            ),
            "readback_global_mm": before_global,
            "readback_hardware_mm": before_hardware,
            "readback_error_mm": before_error,
            "readback_response": before_response,
            "status": before_status,
            "checked_at": dt.datetime.now().isoformat(
                timespec="milliseconds"
            ),
        }
        sample["stage_arrival"] = stage_before_capture
        command = build_record_command(config, calibration, run_dir, sample)
        print(
            f"[CAPTURE] {sample['sample_id']} {sample['pass_name']} "
            f"stage={float(sample['stage_command_global_mm']):+.3f} "
            f"{sample['kind']} {sample['vertex_id']} "
            f"attempt={local_attempt}/{attempts_this_run}",
            flush=True,
        )
        started_at = dt.datetime.now().isoformat(timespec="milliseconds")
        attempt = {
            "attempt": attempt_index,
            "run_dir": str(run_dir.resolve()),
            "started_at": started_at,
            "finished_at": "",
            "return_code": None,
            "left_events": 0,
            "right_events": 0,
            "accepted": False,
            "status": "recording",
        }
        sample.setdefault("attempts", []).append(attempt)
        sample["status"] = "recording"
        sample["capture_status"] = "recording"
        update_manifest(manifest_path, manifest)
        completed = subprocess.run(command, check=False)
        after_status = stage_driver.query_status()
        after_global, after_hardware, after_response = (
            stage_driver.global_position_mm()
        )
        after_error = after_global - requested_stage
        stage_drift = after_global - before_global
        stage_after_capture = {
            "command_global_mm": requested_stage,
            "command_hardware_mm": stage_driver.hardware_from_global(
                requested_stage
            ),
            "readback_global_mm": after_global,
            "readback_hardware_mm": after_hardware,
            "readback_error_mm": after_error,
            "readback_response": after_response,
            "status": after_status,
            "drift_from_before_mm": stage_drift,
            "checked_at": dt.datetime.now().isoformat(
                timespec="milliseconds"
            ),
        }
        stage_stable = (
            abs(float(after_status["speed_mm_s"])) <= 1e-9
            and not bool(after_status["home_switch"])
            and not bool(after_status["end_switch"])
            and abs(after_error) <= readback_tolerance
            and abs(stage_drift) <= readback_tolerance
        )
        left_events, right_events = read_capture_counts(run_dir)
        accepted = (
            completed.returncode == 0
            and stage_stable
            and left_events >= minimum_events
            and right_events >= minimum_events
        )
        attempt.update(
            {
                "finished_at": dt.datetime.now().isoformat(timespec="milliseconds"),
                "return_code": int(completed.returncode),
                "left_events": left_events,
                "right_events": right_events,
                "stage_before_capture": stage_before_capture,
                "stage_after_capture": stage_after_capture,
                "stage_stable": stage_stable,
                "accepted": accepted,
                "status": "accepted" if accepted else "failed",
            }
        )
        if accepted:
            sample["status"] = "captured"
            sample["capture_status"] = "captured"
            sample["processing_status"] = "pending"
            sample["accepted_run_dir"] = str(run_dir.resolve())
            update_manifest(manifest_path, manifest)
            print(f"[CAPTURE][OK] left={left_events:,} right={right_events:,}")
            if process_each:
                sample["processing_status"] = "processing"
                update_manifest(manifest_path, manifest)
                try:
                    process_recording(config, calibration, run_dir)
                    output = run_dir / "stereo_3d" / "stereo_3d_points.npz"
                    if not output.exists():
                        raise RuntimeError(
                            f"Processing returned without expected output: {output}"
                        )
                    sample["stereo_3d_npz"] = str(output.resolve())
                    sample["processing_fingerprint"] = processing_fingerprint(
                        config,
                        calibration,
                    )
                    sample["processing_status"] = "processed"
                    sample["status"] = "processed"
                    update_manifest(manifest_path, manifest)
                except Exception:
                    sample["processing_status"] = "failed"
                    sample["status"] = "captured"
                    update_manifest(manifest_path, manifest)
                    raise
            return True
        sample["status"] = "retry_pending" if local_attempt < attempts_this_run else "failed"
        sample["capture_status"] = sample["status"]
        update_manifest(manifest_path, manifest)
        print(
            f"[CAPTURE][FAIL] return={completed.returncode} "
            f"left={left_events:,} right={right_events:,} "
            f"stage_stable={stage_stable} drift={stage_drift:+.6f} mm"
        )
        wait_sec = float(camera.get("camera_reopen_wait_sec", 1.0))
        if wait_sec > 0:
            time.sleep(wait_sec)
    return False


def confirm_motion_authority(
    args: argparse.Namespace,
    resolved: dict[str, Any],
) -> bool:
    """Ask only after the stage has been verified stopped; no motion occurs here."""
    if args.home_depth_axis:
        print()
        print("Homing safety check:")
        print(f"  - only axis {resolved['stage_axis'].upper()} will be homed;")
        print("  - homing may travel outside the configured experiment soft-limit interval;")
        print("  - the complete path to the HOME switch is visibly clear;")
        print("  - cables are slack and the accessible power disconnect is within reach.")
        answer = input(
            f"Type HOME {resolved['stage_axis'].upper()} to authorize this homing move: "
        ).strip()
        return answer == f"HOME {resolved['stage_axis'].upper()}"
    if args.yes:
        return True
    print()
    print("Physical safety check:")
    print("  - the selected stage axis has been homed;")
    print("  - the entire listed travel is clear of collisions and cable strain;")
    print("  - the emergency power disconnect is accessible;")
    return input("Type RUN to authorize motion: ").strip() == "RUN"


def best_effort_stage_stop(stage_driver: SafeOssilaAxis) -> bool:
    """Keep cleanup progressing even if a second Ctrl+C arrives during stop."""
    try:
        return bool(stage_driver.emergency_stop())
    except BaseException as exc:
        print(
            "[STAGE][EMERGENCY] Stop path was interrupted or failed: "
            f"{exc}. Disconnect the accessible stage power immediately.",
            file=sys.stderr,
        )
        return False


def probe_stage_identity(
    config: dict[str, Any],
    resolved: dict[str, Any],
) -> dict[str, Any]:
    """Read identity/settings only. No home, goto, PAT, or camera command is sent."""
    stage_driver: SafeOssilaAxis | None = None
    try:
        stage_driver = SafeOssilaAxis(config, resolved)
        status = stage_driver.query_status()
        if abs(float(status["speed_mm_s"])) > 1e-9:
            stopped = best_effort_stage_stop(stage_driver)
            raise RuntimeError(
                "Stage was moving when the identity probe connected; "
                f"stop_verified={stopped}. Inspect it before any experiment."
            )
        device, device_response = stage_driver.query_text("device")
        firmware, firmware_response = stage_driver.query_text("firmware")
        internal_serial, serial_response = stage_driver.query_text("serial")
        length_raw, length_response = stage_driver.query_float("length")
        length_mm, length_interpretation = (
            stage_driver.normalize_reported_length_mm(
                length_raw,
                float(config["stage"]["expected_travel_mm"]),
            )
        )
        acceleration, acceleration_response = stage_driver.query_float("acc")
        deceleration, deceleration_response = stage_driver.query_float("dec")
        posmode, posmode_response = stage_driver.query_posmode()
        alarms, alarms_response = stage_driver.query_alarms()
        return {
            "usb_serial_from_axis_config": str(resolved["stage_serial"]),
            "configured_axis": str(resolved["stage_axis"]),
            "device": device,
            "device_response": device_response,
            "firmware": firmware,
            "firmware_response": firmware_response,
            "internal_serial": internal_serial,
            "serial_response": serial_response,
            "length_mm": length_mm,
            "length_raw_response_value": length_raw,
            "length_unit_interpretation": length_interpretation,
            "length_response": length_response,
            "acceleration_mm_s2": acceleration,
            "acceleration_response": acceleration_response,
            "deceleration_mm_s2": deceleration,
            "deceleration_response": deceleration_response,
            "posmode": posmode,
            "posmode_response": posmode_response,
            "alarms": alarms,
            "alarms_response": alarms_response,
            "status": status,
        }
    finally:
        if stage_driver is not None:
            try:
                stage_driver.close()
            except BaseException as exc:
                print(f"[STAGE][WARN] probe serial close failed: {exc}", file=sys.stderr)


def main() -> int:
    args = parse_args()
    if bool(getattr(args, "list_stage_ports", False)):
        if any(
            (
                args.execute,
                args.probe_stage_identity,
                args.home_depth_axis,
                args.trace_cube_preview,
            )
        ):
            raise SystemExit(
                "--list-stage-ports cannot be combined with hardware-control options."
            )
        return list_stage_ports()
    config_path = args.config.resolve()
    config = load_json(config_path)
    resolved = validate_config(config, config_path)
    planned = generate_plan(config)
    session_dir, manifest_path, manifest = create_or_resume_session(
        args,
        config,
        config_path,
        resolved,
        planned,
    )
    print_plan(config, manifest["samples"], resolved)
    print(f"Session: {session_dir}")
    if args.probe_stage_identity and not args.execute:
        raise SystemExit("--probe-stage-identity opens the stage and therefore requires --execute.")
    if not args.execute:
        if manifest.get("cleanup_verified") is False:
            print(
                "[SAFETY LOCK] This existing session records an unverified prior "
                "hardware cleanup. Preserve it and choose a fresh --session-dir.",
                file=sys.stderr,
            )
            return 2
        update_manifest(manifest_path, manifest, status="planned")
        execute_command = subprocess.list2cmdline(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--config",
                str(config_path),
                "--session-dir",
                str(session_dir),
                "--execute",
            ]
        )
        print("[DRY PLAN] Hardware was not opened.")
        print("[NEXT] After reviewing this exact plan, reuse the same session:")
        print(execute_command)
        return 0
    if args.probe_stage_identity:
        print("[STAGE][PROBE] read-only identity/settings query; no motion commands")
        try:
            probe = probe_stage_identity(config, resolved)
        except Exception as exc:
            update_manifest(manifest_path, manifest, status="stage_probe_failed")
            print(f"[STAGE][PROBE][ERROR] {exc}", file=sys.stderr)
            return 2
        manifest["stage_identity_probe"] = probe
        update_manifest(manifest_path, manifest, status="stage_identity_probed")
        print(json.dumps(json_ready(probe), indent=2))
        print(
            "Copy device, internal_serial, acceleration_mm_s2, and "
            "deceleration_mm_s2 into the four execution-lock fields, then "
            "create a fresh dry plan."
        )
        return 0

    if args.home_depth_axis and args.yes:
        raise SystemExit(
            "--yes cannot be combined with --home-depth-axis. "
            "Homing always requires the dedicated physical-path confirmation."
        )
    pending = [
        sample
        for sample in manifest["samples"]
        if sample.get("capture_status") != "captured"
        and sample.get("status") not in {"captured", "processed"}
    ]
    cleanup_was_not_verified = manifest.get("cleanup_verified") is False
    if cleanup_was_not_verified:
        print(
            "[SAFETY LOCK] The previous run did not verify PAT OFF/stage shutdown. "
            "Inspect the apparatus and disconnect accessible power if needed. This "
            "session is intentionally not resumable; preserve it for analysis and "
            "create a fresh reviewed dry-plan session.",
            file=sys.stderr,
        )
        return 2
    if not pending:
        update_manifest(manifest_path, manifest, status="captured")
        print("All planned samples are already captured.")
        return 0
    missing_execution_locks: list[str] = []
    if not str(config["stage"].get("expected_stage_serial_response", "")).strip():
        missing_execution_locks.append("stage.expected_stage_serial_response")
    if not str(config["stage"].get("expected_device_response", "")).strip():
        missing_execution_locks.append("stage.expected_device_response")
    for key in (
        "expected_acceleration_mm_s2",
        "expected_deceleration_mm_s2",
    ):
        if config["stage"].get(key) is None:
            missing_execution_locks.append(f"stage.{key}")
    if missing_execution_locks:
        raise SystemExit(
            "Execution is locked until these physical-stage values are set: "
            f"{', '.join(missing_execution_locks)}. Copy the exact product code, "
            "internal serial, and approved acceleration/deceleration from the "
            "physically identified camera stage/Motion Console, then repeat the dry plan."
        )

    stage_driver: SafeOssilaAxis | None = None
    target_driver: PatTarget | ManualTarget | None = None
    completed_normally = False
    captured_count = 0
    interrupted = False
    stop_verified = False
    calibration = Path(str(manifest["stereo_calibration"]))
    reference_global = float(config["stage"]["reference_global_mm"])
    last_stage_command: float | None = None

    try:
        # This atomic marker is written before opening any hardware. If the
        # process is killed so finally cannot run, the session remains locked.
        manifest["cleanup_verified"] = False
        manifest["cleanup_errors"] = []
        update_manifest(
            manifest_path,
            manifest,
            status="run_active_cleanup_required",
        )
        stage_driver = SafeOssilaAxis(config, resolved)
        initial_status = stage_driver.query_status()
        manifest["stage_initial_status"] = initial_status
        if abs(float(initial_status["speed_mm_s"])) > 1e-9:
            stop_verified = best_effort_stage_stop(stage_driver)
            raise RuntimeError(
                "The stage was moving when this process connected. A stop was "
                "attempted; inspect the apparatus before starting a new session."
            )
        mandatory_preflight = stage_driver.preflight(
            require_absolute=not args.home_depth_axis,
            allow_end_switch_for_homing=bool(args.home_depth_axis),
            allow_outside_soft_limit_for_homing=bool(args.home_depth_axis),
        )
        manifest["stage_device_before"] = stage_driver.device_snapshot(mandatory_preflight)
        update_manifest(manifest_path, manifest)
        print(
            "[STAGE][PREFLIGHT] "
            f"internal serial={mandatory_preflight['stage_serial']!r}, "
            f"travel={mandatory_preflight['length_mm']:.3f} mm, "
            f"stored acc/dec={mandatory_preflight['acceleration_mm_s2']:.3f}/"
            f"{mandatory_preflight['deceleration_mm_s2']:.3f} mm/s^2, "
            f"command speed={float(config['stage']['speed_mm_s']):.3f} mm/s"
        )
        current_hardware = float(
            mandatory_preflight["status"]["position_hardware_mm"]
        )
        reference_hardware = stage_driver.hardware_from_global(reference_global)
        if args.home_depth_axis:
            print(
                "[STAGE][PATH TO CLEAR] "
                f"current {current_hardware:.3f} mm -> HOME 0 mm -> "
                f"reference {reference_hardware:.3f} mm"
            )
        else:
            print(
                "[STAGE][PATH TO CLEAR] "
                f"current {current_hardware:.3f} mm -> "
                f"reference {reference_hardware:.3f} mm"
            )

        print("[PREFLIGHT] validating capture software, calibration, and camera discovery")
        manifest["software_camera_preflight"] = preflight_capture_software(
            config,
            calibration,
            session_dir,
        )
        update_manifest(manifest_path, manifest)

        if not confirm_motion_authority(args, resolved):
            update_manifest(manifest_path, manifest, status="user_aborted")
            print("Aborted after read-only preflight and before any commanded motion.")
            return 1

        if str(config["target"].get("mode", "pat")).lower() == "pat":
            print(f"[PAT] connecting controllers {config['target']['controller_ids']}")
            target_driver = PatTarget(config)
        else:
            print("[TARGET] manual positioning mode")
            target_driver = ManualTarget(config)

        preview_enabled = bool(config["camera"].get("preview", {}).get("enabled", True))
        if preview_enabled and not args.no_preview:
            if not run_initial_preview(config):
                update_manifest(manifest_path, manifest, status="preview_aborted")
                print("[PREVIEW] aborted before any stage motion.")
                return 1

        if args.home_depth_axis:
            print(f"[STAGE] homing only axis {resolved['stage_axis'].upper()}")
            manifest["stage_home"] = stage_driver.home()
            manifest["stage_preflight_after_home"] = stage_driver.preflight(
                require_absolute=True,
                allow_outside_soft_limit_for_homing=True,
            )
            stage_driver.authorize_recovery_move(reference_global)
            update_manifest(manifest_path, manifest)

        print(f"[STAGE] move to reference global {reference_global:+.3f} mm")
        reference_arrival = stage_driver.move_global(reference_global)
        last_stage_command = reference_global
        manifest["stage_reference_arrival"] = reference_arrival
        update_manifest(manifest_path, manifest)

        if args.trace_cube_preview:
            if not isinstance(target_driver, PatTarget):
                raise RuntimeError("--trace-cube-preview requires target.mode='pat'.")
            trace_cube_wireframe(target_driver, config)

        update_manifest(manifest_path, manifest, status="capturing")
        for sample in pending:
            requested_stage = float(sample["stage_command_global_mm"])
            if last_stage_command is None or not math.isclose(
                requested_stage, last_stage_command, abs_tol=1e-9
            ):
                print(f"[STAGE] move global {requested_stage:+.3f} mm")
                stage_arrival = stage_driver.move_global(requested_stage)
                last_stage_command = requested_stage
            else:
                global_readback, raw_readback, response = stage_driver.global_position_mm()
                error = global_readback - requested_stage
                if abs(error) > float(config["stage"]["readback_tolerance_mm"]):
                    raise RuntimeError(
                        f"Stage drift/readback error {error:+.6f} mm exceeds tolerance."
                    )
                stage_arrival = {
                    "command_global_mm": requested_stage,
                    "command_hardware_mm": stage_driver.hardware_from_global(requested_stage),
                    "readback_global_mm": global_readback,
                    "readback_hardware_mm": raw_readback,
                    "readback_error_mm": error,
                    "readback_response": response,
                    "arrived_at": dt.datetime.now().isoformat(timespec="milliseconds"),
                }

            target_driver.move_to_mm(list(sample["pat_target_mm"]))
            ok = capture_sample(
                config=config,
                calibration=calibration,
                session_dir=session_dir,
                manifest_path=manifest_path,
                manifest=manifest,
                sample=sample,
                stage_arrival=stage_arrival,
                stage_driver=stage_driver,
                process_each=bool(args.process_each),
            )
            if not ok and not args.continue_on_error:
                update_manifest(manifest_path, manifest, status="capture_failed")
                print("Capture stopped. Resume with --session-dir after correcting the problem.")
                return 2

        captured_count = sum(
            sample.get("capture_status") == "captured"
            or sample.get("status") in {"captured", "processed"}
            for sample in manifest["samples"]
        )
        completed_normally = captured_count == len(manifest["samples"])
        manifest["capture_complete"] = bool(completed_normally)
        manifest["cleanup_verified"] = False
        manifest["cleanup_errors"] = []
        update_manifest(
            manifest_path,
            manifest,
            status=(
                "capture_complete_cleanup_pending"
                if completed_normally
                else "captured_partial_cleanup_pending"
            ),
        )
        return 0 if completed_normally else 2
    except KeyboardInterrupt:
        interrupted = True
        if stage_driver is not None:
            stop_verified = best_effort_stage_stop(stage_driver)
        update_manifest(manifest_path, manifest, status="interrupted")
        print("\n[STOP] interrupted. Stop was attempted before session I/O; no return move.")
        return 130
    except Exception as exc:
        if stage_driver is not None:
            stop_verified = best_effort_stage_stop(stage_driver)
        update_manifest(manifest_path, manifest, status="error")
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    finally:
        cleanup_errors: list[str] = []
        try:
            if stage_driver is not None and not completed_normally and not stop_verified:
                stop_verified = best_effort_stage_stop(stage_driver)
                if not stop_verified:
                    cleanup_errors.append("stage stop could not be verified")
            if completed_normally:
                if target_driver is not None and bool(config["target"].get("return_to_center", True)):
                    target_driver.move_to_mm(list(config["target"]["pat_center_mm"]))
                    print("[PAT/TARGET] returned to configured centre.")
                if stage_driver is not None and bool(
                    config["stage"].get("return_to_reference_on_success", True)
                ):
                    stage_driver.move_global(reference_global)
                    print("[STAGE] returned to reference.")
        except BaseException as exc:
            cleanup_errors.append(f"return motion: {type(exc).__name__}: {exc}")
            if isinstance(exc, KeyboardInterrupt):
                interrupted = True
            if stage_driver is not None:
                stop_verified = best_effort_stage_stop(stage_driver)
            print(
                f"[WARN] normal return/cleanup motion failed: {exc}",
                file=sys.stderr,
            )
        finally:
            if target_driver is not None:
                try:
                    if not target_driver.turn_off():
                        cleanup_errors.append(
                            "PAT OFF/disconnect could not be verified; disconnect PAT power"
                        )
                except BaseException as exc:
                    cleanup_errors.append(
                        f"PAT cleanup: {type(exc).__name__}: {exc}"
                    )
                    print(
                        "[PAT][EMERGENCY] Cleanup raised unexpectedly: "
                        f"{exc}. Disconnect PAT power immediately.",
                        file=sys.stderr,
                    )
            if stage_driver is not None:
                try:
                    if not interrupted and not cleanup_errors:
                        final_snapshot = stage_driver.device_snapshot()
                        manifest["stage_device_after"] = final_snapshot
                        snapshot_errors = {
                            key: final_snapshot[key]
                            for key in ("preflight_error", "position_error")
                            if final_snapshot.get(key)
                        }
                        if snapshot_errors:
                            raise RuntimeError(
                                "final stage snapshot was not fully verified: "
                                f"{snapshot_errors}"
                            )
                except BaseException as exc:
                    cleanup_errors.append(
                        f"final stage state verification: {type(exc).__name__}: {exc}"
                    )
                    stop_verified = best_effort_stage_stop(stage_driver)
                finally:
                    try:
                        stage_driver.close()
                    except BaseException as exc:
                        cleanup_errors.append(
                            f"stage serial close: {type(exc).__name__}: {exc}"
                        )
                        print(
                            f"[STAGE][WARN] serial close failed: {exc}",
                            file=sys.stderr,
                        )
        manifest["cleanup_verified"] = not cleanup_errors
        manifest["cleanup_completed_at"] = dt.datetime.now().isoformat(
            timespec="milliseconds"
        )
        manifest["cleanup_errors"] = cleanup_errors
        final_status: str | None = None
        if cleanup_errors:
            final_status = (
                "capture_complete_cleanup_failed"
                if completed_normally
                else f"{str(manifest.get('status', 'run'))}_cleanup_failed"
            )
            print(
                "[SAFETY][CLEANUP FAILED] "
                + " | ".join(cleanup_errors)
                + ". Inspect the apparatus and disconnect accessible power if needed.",
                file=sys.stderr,
            )
        elif completed_normally:
            final_status = "captured"
        elif str(manifest.get("status", "")) == "captured_partial_cleanup_pending":
            final_status = "captured_partial"
        try:
            update_manifest(manifest_path, manifest, status=final_status)
        except BaseException as exc:
            cleanup_errors.append(
                f"final manifest write: {type(exc).__name__}: {exc}"
            )
            manifest["cleanup_verified"] = False
            print(
                f"[SAFETY][WARN] Could not persist final cleanup state: {exc}",
                file=sys.stderr,
            )
        if completed_normally and not cleanup_errors:
            print(
                f"[DONE] captured {captured_count}/{len(manifest['samples'])} samples; "
                "PAT OFF and stage shutdown verified."
            )
            print(
                "Next: "
                f"{sys.executable} {SCRIPT_DIR / 'stereo_stage_accuracy_analyze.py'} "
                f"{session_dir}"
            )
        if interrupted and not cleanup_errors:
            try:
                print(
                    f"Resume: {sys.executable} {Path(__file__).name} "
                    f"--config {config_path} --session-dir {session_dir} --execute"
                )
            except BaseException:
                pass
        if cleanup_errors:
            return 130 if interrupted else 2


if __name__ == "__main__":
    if os.name == "nt":
        import multiprocessing as mp

        mp.freeze_support()
    raise SystemExit(main())
