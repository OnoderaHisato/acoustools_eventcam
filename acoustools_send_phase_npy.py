#!/usr/bin/env python3
"""Validate and optionally send one precomputed AcousTools phase map to PAT.

Two input formats are accepted, both in AcousTools element order (first 256 top,
last 256 bottom):

* ``complex64`` of shape ``(1, 512, 1)`` -- phase from ``angle(a)``, per-element
  amplitude from ``abs(a)``, which must not exceed 1.
* real ``float32``/``float64`` radians of shape ``(1, 512)`` or ``(1, 512, 1)``
  -- phase only; AcousTools drives every element at amplitude 1.0.
"""

from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch


BOARD_IDS = (101, 3)
TRANSDUCERS_PER_BOARD = 256
TOTAL_TRANSDUCERS = TRANSDUCERS_PER_BOARD * len(BOARD_IDS)
EXPECTED_SHAPE = (1, TOTAL_TRANSDUCERS, 1)
ACCEPTED_REAL_SHAPES = ((1, TOTAL_TRANSDUCERS), (1, TOTAL_TRANSDUCERS, 1))
AMPLITUDE_TOLERANCE = 1e-5
CONFIRMATION_TEXT = "SEND"

COMPLEX_HOLOGRAM = "complex64 hologram (per-element amplitude taken from |a|)"
REAL_PHASE_RADIANS = "real phase in radians (every element driven at amplitude 1.0)"


@dataclass(frozen=True)
class PhaseMapInfo:
    path: Path
    sha256: str
    shape: tuple[int, ...]
    dtype: str
    amplitude_min: float
    amplitude_max: float
    phase_min_rad: float
    phase_max_rad: float
    source_format: str
    source_dtype: str
    source_shape: tuple[int, ...]
    raw_phase_min_rad: float
    raw_phase_max_rad: float
    wrapped_elements: int


def _load_complex_hologram(loaded: np.ndarray) -> tuple[np.ndarray, dict]:
    """Validate a complex64 hologram; amplitude is whatever the file encodes."""
    if tuple(loaded.shape) != EXPECTED_SHAPE:
        raise ValueError(
            f"Expected shape {EXPECTED_SHAPE} for board IDs {BOARD_IDS}, "
            f"got {tuple(loaded.shape)}."
        )
    if not np.isfinite(loaded).all():
        raise ValueError("Phase map contains NaN or infinity.")

    amplitude_max = float(np.abs(loaded).max())
    if amplitude_max > 1.0 + AMPLITUDE_TOLERANCE:
        raise ValueError(
            f"Complex magnitude exceeds 1 (maximum {amplitude_max:.9g}); "
            "refusing to overdrive the requested map."
        )

    # np.load may return read-only or memory-mapped storage.  Own the buffer so
    # torch and AcousTools cannot mutate the source array or its file.
    phase_array = np.array(loaded, dtype=np.complex64, copy=True, order="C")
    angles = np.angle(phase_array)
    return phase_array, {
        "source_format": COMPLEX_HOLOGRAM,
        "raw_phase_min_rad": float(angles.min()),
        "raw_phase_max_rad": float(angles.max()),
        "wrapped_elements": 0,
    }


def _load_real_phase_radians(loaded: np.ndarray) -> tuple[np.ndarray, dict]:
    """Validate a real radian phase array and lift it to a unit-amplitude hologram.

    AcousTools drives every element at amplitude 1.0 when it is handed a real
    tensor (``Levitator.levitate`` takes ``amp = torch.ones_like(hologram)``), so
    ``exp(1j * phi)`` reaches the boards as exactly the same command while keeping
    one complex code path for the validation and the visualisers.
    """
    if tuple(loaded.shape) not in ACCEPTED_REAL_SHAPES:
        raise ValueError(
            f"Expected one of {ACCEPTED_REAL_SHAPES} for a real radian phase array "
            f"on board IDs {BOARD_IDS}, got {tuple(loaded.shape)}."
        )
    if not np.isfinite(loaded).all():
        raise ValueError("Phase map contains NaN or infinity.")

    raw = np.asarray(loaded, dtype=np.float64)
    # Phase is defined modulo 2*pi, so unwrapped optimiser output is admissible;
    # exp(1j * phi) performs the wrap exactly and the count is reported.
    wrapped_elements = int(np.count_nonzero(np.abs(raw) > np.pi + 1e-12))
    phase_array = np.exp(1j * raw).astype(np.complex64).reshape(EXPECTED_SHAPE)
    return np.ascontiguousarray(phase_array), {
        "source_format": REAL_PHASE_RADIANS,
        "raw_phase_min_rad": float(raw.min()),
        "raw_phase_max_rad": float(raw.max()),
        "wrapped_elements": wrapped_elements,
    }


def load_phase_map(path: Path) -> tuple[torch.Tensor, PhaseMapInfo]:
    """Load a two-board phase map without touching hardware.

    Accepts either a complex64 hologram of shape ``(1, 512, 1)`` or a real
    radian phase array of shape ``(1, 512)`` / ``(1, 512, 1)``.  Both are handed
    on as complex64; see ``_load_real_phase_radians`` for why that is equivalent.
    """
    resolved = path.resolve(strict=True)
    loaded = np.load(resolved, allow_pickle=False)

    if loaded.dtype == np.dtype(np.complex64):
        phase_array, source = _load_complex_hologram(loaded)
    elif np.issubdtype(loaded.dtype, np.floating):
        phase_array, source = _load_real_phase_radians(loaded)
    else:
        raise ValueError(
            f"Expected dtype complex64 or a real floating phase in radians, "
            f"got {loaded.dtype}. Refusing an implicit phase-format conversion."
        )

    amplitudes = np.abs(phase_array)
    angles = np.angle(phase_array)
    info = PhaseMapInfo(
        path=resolved,
        sha256=hashlib.sha256(resolved.read_bytes()).hexdigest(),
        shape=tuple(phase_array.shape),
        dtype=str(phase_array.dtype),
        amplitude_min=float(amplitudes.min()),
        amplitude_max=float(amplitudes.max()),
        phase_min_rad=float(angles.min()),
        phase_max_rad=float(angles.max()),
        source_dtype=str(loaded.dtype),
        source_shape=tuple(loaded.shape),
        **source,
    )
    return torch.from_numpy(phase_array), info


def print_phase_map_info(info: PhaseMapInfo) -> None:
    print(f"[CHECK] file: {info.path}")
    print(f"[CHECK] SHA-256: {info.sha256}")
    print(f"[CHECK] source: shape {info.source_shape} | dtype {info.source_dtype}")
    print(f"[CHECK] interpreted as: {info.source_format}")
    print(f"[CHECK] sent as: shape {info.shape} | dtype {info.dtype}")
    print(
        "[CHECK] complex magnitude: "
        f"{info.amplitude_min:.9g} .. {info.amplitude_max:.9g}"
    )
    print(
        "[CHECK] phase range: "
        f"{info.phase_min_rad:.9g} .. {info.phase_max_rad:.9g} rad"
    )
    if info.source_format == REAL_PHASE_RADIANS:
        print(
            "[CHECK] source phase range: "
            f"{info.raw_phase_min_rad:.9g} .. {info.raw_phase_max_rad:.9g} rad"
        )
        if info.wrapped_elements:
            print(
                f"[WARN] {info.wrapped_elements} of {TOTAL_TRANSDUCERS} values lay "
                "outside [-pi, pi] and were wrapped modulo 2*pi."
            )
        print(
            "[WARN] Real radian input carries no amplitude; all "
            f"{TOTAL_TRANSDUCERS} elements will be driven at amplitude 1.0."
        )
    print(f"[CHECK] PAT board IDs: {BOARD_IDS}")
    print("[CHECK] ordering: AcousTools -> OpenMPD permutation enabled")


def send_and_hold(hologram: torch.Tensor) -> None:
    """Connect, activate the phase map, and guarantee an OFF/disconnect attempt."""
    # Importing the controller is deliberately delayed until after validation
    # and the final operator checkpoint.
    from acoustools.Levitator import LevitatorController

    controller = None
    try:
        controller = LevitatorController(ids=BOARD_IDS)
        controller.levitate(hologram, permute=True)
        print("[PAT] Phase map sent. Output is active.")
        try:
            input("[PAT] Press Enter to turn all transducers off and disconnect: ")
        except EOFError:
            print("[PAT] Standard input closed; shutting down immediately.")
    finally:
        if controller is not None:
            try:
                controller.turn_off()
                print("[PAT] All transducers turned off.")
            finally:
                controller.disconnect()
                print("[PAT] AcousTools/OpenMPD disconnected.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase_npy",
        nargs="?",
        type=Path,
        default=Path("phase_acoustools_regular_complex64.npy"),
        help=(
            "AcousTools-order phase map: complex64 (1, 512, 1), or real radians "
            "(1, 512) / (1, 512, 1) (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="connect to board IDs (101, 3) and activate the validated phase map",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        hologram, info = load_phase_map(args.phase_npy)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"[ERROR] {exc}") from exc

    print_phase_map_info(info)
    if not args.send:
        print("[DRY-RUN] Validation complete. Hardware was not opened.")
        print("[DRY-RUN] Add --send to reach the final operator checkpoint.")
        return 0

    print("[WARN] The supplied phase map will energize all 512 transducers.")
    try:
        confirmation = input(
            f"Type {CONFIRMATION_TEXT} to connect and activate this exact map: "
        ).strip()
    except EOFError:
        confirmation = ""
    if confirmation != CONFIRMATION_TEXT:
        print("[ABORT] Confirmation did not match; hardware was not opened.")
        return 2

    send_and_hold(hologram)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
