#!/usr/bin/env python3
"""Build a centre-focus + charge-2 acoustic vortex hologram for the two-board PAT.

The hologram is the product of two factors on every transducer:

1. ``acoustools.Solvers.kd_solver`` phase conjugation for a focus at the PAT
   origin ``(0, 0, 0)``.
2. A helical signature ``exp(1j * s * m * phi)`` with ``m = 2`` and the lab-frame
   azimuth ``phi = atan2(y, x)`` of the transducer.  The per-board sign ``s`` is
   set by ``--convention``.

Sign convention (lab frame, right-hand rule about +z, time factor exp(-i*omega*t)):
``s = +1`` puts the orbital angular momentum along +z, ``s = -1`` along -z.

* ``counter_rotating`` (default): bottom board ``s = +1``, top board ``s = -1``.
  The two boards carry opposite chirality *in the lab frame*; the bottom board is
  right-handed about its own emission direction (+z) and the top board is then
  also right-handed about its own emission direction (-z).
* ``co_rotating``: both boards ``s = +1``.  Same chirality in the lab frame, which
  is what the built-in ``add_lev_sig(mode='Vortex')`` signature does at ``m = 1``;
  relative to each board's own emission direction the two boards are opposite.

No hardware is opened.  This script only writes an ``.npy`` hologram plus a
metadata JSON next to it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch

import acoustools
import acoustools.Constants as Constants
from acoustools.Solvers import kd_solver
from acoustools.Utilities import BOARD_POSITIONS, DTYPE, create_points, transducers


BOARD_SIDE = 16
BOARD_SIZE = BOARD_SIDE * BOARD_SIDE
TOP_SLICE = slice(0, BOARD_SIZE)
BOTTOM_SLICE = slice(BOARD_SIZE, 2 * BOARD_SIZE)

CONVENTIONS: dict[str, dict[str, int]] = {
    # sign of the helical charge applied to each board, lab frame (+z right hand)
    "counter_rotating": {"bottom": +1, "top": -1},
    "co_rotating": {"bottom": +1, "top": +1},
}
DEFAULT_CONVENTION = "counter_rotating"
DEFAULT_CHARGE = 2


def build_board() -> torch.Tensor:
    """Return the standard AcousTools two-board geometry (top first, bottom last)."""
    board = transducers(BOARD_SIDE, BOARD_POSITIONS)
    if board.shape[0] != 2 * BOARD_SIZE:
        raise RuntimeError(f"Expected 512 transducers, got {board.shape[0]}")
    top_z = board[TOP_SLICE, 2]
    bottom_z = board[BOTTOM_SLICE, 2]
    if not bool((top_z > 0).all()) or not bool((bottom_z < 0).all()):
        raise RuntimeError(
            "Unexpected board ordering: elements 0--255 must be the top board."
        )
    return board


def focus_hologram(board: torch.Tensor) -> torch.Tensor:
    """Phase-conjugating focus at the PAT origin, shape (1, 512, 1)."""
    points = create_points(1, 1, x=0.0, y=0.0, z=0.0)
    return kd_solver(points, board)


def helical_signature(
    board: torch.Tensor,
    *,
    charge: int,
    bottom_sign: int,
    top_sign: int,
) -> np.ndarray:
    """Per-transducer helical phase ``s * m * atan2(y, x)`` in radians."""
    positions = board.detach().cpu().numpy().real.astype(np.float64)
    azimuth = np.arctan2(positions[:, 1], positions[:, 0])
    signs = np.empty(positions.shape[0], dtype=np.float64)
    signs[TOP_SLICE] = float(top_sign)
    signs[BOTTOM_SLICE] = float(bottom_sign)
    return signs * float(charge) * azimuth


def compose_hologram(
    focus: torch.Tensor,
    signature_rad: np.ndarray,
) -> np.ndarray:
    """Apply the helical signature to the focus hologram, returning complex64."""
    focus_flat = focus.detach().cpu().numpy().reshape(-1).astype(np.complex128)
    magnitude = np.abs(focus_flat)
    phase = np.angle(focus_flat) + signature_rad
    combined = magnitude * np.exp(1j * phase)
    return combined.reshape(1, -1, 1).astype(np.complex64)


def board_phase_grid(hologram: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return top/bottom 16x16 phase maps with x horizontal and y vertical."""
    flat = hologram.reshape(-1)
    phase = np.angle(flat)
    top = phase[TOP_SLICE].reshape(BOARD_SIDE, BOARD_SIDE).T
    bottom = phase[BOTTOM_SLICE].reshape(BOARD_SIDE, BOARD_SIDE).T
    return top, bottom


def circular_difference(a: np.ndarray, b: np.ndarray) -> float:
    """Maximum absolute wrapped phase difference between two arrays."""
    diff = np.angle(np.exp(1j * (np.asarray(a) - np.asarray(b))))
    return float(np.max(np.abs(diff)))


def rotation_180_index(board: np.ndarray) -> np.ndarray:
    """Index that maps each transducer onto its 180-degree rotation about z."""
    return _match_positions(board, board * np.array([-1.0, -1.0, 1.0]))


def mirror_x_index(board: np.ndarray) -> np.ndarray:
    """Index that maps each transducer onto its mirror image across the yz plane."""
    return _match_positions(board, board * np.array([-1.0, 1.0, 1.0]))


def _match_positions(board: np.ndarray, targets: np.ndarray) -> np.ndarray:
    distance = np.linalg.norm(board[:, None, :] - targets[None, :, :], axis=2)
    index = np.argmin(distance, axis=0)
    if float(distance[index, np.arange(len(targets))].max()) > 1e-9:
        raise RuntimeError("Transducer lattice is not symmetric as expected.")
    return index


def symmetry_report(hologram: np.ndarray, board: torch.Tensor, charge: int) -> dict:
    """Analytic symmetry checks that do not depend on any field simulation."""
    positions = board.detach().cpu().numpy().real.astype(np.float64)
    flat = hologram.reshape(-1)
    phase = np.angle(flat)

    rot = rotation_180_index(positions)
    mir = mirror_x_index(positions)
    # exp(i m phi) is invariant under a 180 deg rotation when m is even.
    rotation_expected_rad = float(np.pi * (charge % 2))
    return {
        "rotation_180_about_z_max_wrapped_diff_rad": circular_difference(
            phase[rot], phase + rotation_expected_rad
        ),
        "rotation_180_expected_offset_rad": rotation_expected_rad,
        "mirror_x_top_vs_bottom_max_wrapped_diff_rad": circular_difference(
            phase[mir][TOP_SLICE], phase[BOTTOM_SLICE]
        ),
        "mirror_x_self_max_wrapped_diff_rad": circular_difference(phase[mir], phase),
    }


def element_winding_number(
    hologram: np.ndarray,
    board: torch.Tensor,
    *,
    ring_radius_m: float = 0.045,
    tolerance_m: float = 0.006,
) -> dict[str, float]:
    """Unwrapped phase winding of each board's outer element ring."""
    positions = board.detach().cpu().numpy().real.astype(np.float64)
    phase = np.angle(hologram.reshape(-1))
    radius = np.hypot(positions[:, 0], positions[:, 1])
    azimuth = np.arctan2(positions[:, 1], positions[:, 0])
    result: dict[str, float] = {}
    for name, board_slice in (("top", TOP_SLICE), ("bottom", BOTTOM_SLICE)):
        mask = np.zeros(len(phase), dtype=bool)
        mask[board_slice] = True
        mask &= np.abs(radius - ring_radius_m) <= tolerance_m
        order = np.argsort(azimuth[mask])
        ring_phase = phase[mask][order]
        unwrapped = np.unwrap(ring_phase)
        closing = np.angle(np.exp(1j * (ring_phase[0] - unwrapped[-1])))
        total = (unwrapped[-1] - unwrapped[0]) + closing
        result[f"{name}_ring_elements"] = float(mask.sum())
        result[f"{name}_ring_winding_number"] = float(total / (2.0 * np.pi))
    return result


def write_outputs(
    hologram: np.ndarray,
    output_path: Path,
    metadata: dict,
) -> tuple[Path, Path]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    np.save(output_path, hologram, allow_pickle=False)
    digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
    metadata["sha256"] = digest
    metadata_path = output_path.with_name(output_path.stem + "_metadata.json")
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path, metadata_path


def default_output_path(convention: str, charge: int) -> Path:
    return Path(
        f"phase_acoustools_focus_vortex_m{charge}_{convention}_complex64.npy"
    )


def generate(convention: str, charge: int, output_path: Path) -> dict:
    if convention not in CONVENTIONS:
        raise SystemExit(f"[ERROR] unknown convention: {convention}")
    if charge == 0:
        raise SystemExit("[ERROR] --charge must be non-zero")

    signs = CONVENTIONS[convention]
    board = build_board()
    focus = focus_hologram(board)
    signature = helical_signature(
        board,
        charge=charge,
        bottom_sign=signs["bottom"],
        top_sign=signs["top"],
    )
    hologram = compose_hologram(focus, signature)

    magnitude = np.abs(hologram)
    phase = np.angle(hologram)
    metadata = {
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "output": output_path.name,
        "generator": (
            "acoustools.Solvers.kd_solver focus at origin "
            "* exp(1j * sign * charge * atan2(y, x))"
        ),
        "target_m": [0.0, 0.0, 0.0],
        "topological_charge": int(charge),
        "convention": convention,
        "board_charge_sign": {
            "top_elements_0_255": int(signs["top"]),
            "bottom_elements_256_511": int(signs["bottom"]),
        },
        "azimuth_definition": "phi = atan2(y, x) in PAT lab coordinates",
        "handedness_note": (
            "sign = +1 places the orbital angular momentum along +z "
            "(right-hand rule about +z, time factor exp(-i*omega*t))"
        ),
        "board": {
            "factory": "transducers(16, BOARD_POSITIONS)",
            "board_positions_m": float(BOARD_POSITIONS),
            "transducers": int(board.shape[0]),
            "order": "AcousTools; first 256 top (z>0), last 256 bottom (z<0)",
        },
        "acoustics": {
            "speed_of_sound_m_s": float(Constants.c_0),
            "frequency_hz": float(Constants.f),
            "wavelength_m": float(Constants.wavelength),
            "wavenumber_rad_m": float(Constants.k),
            "p_ref": float(Constants.P_ref),
            "transducer_radius_m": float(Constants.radius),
        },
        "shape": list(hologram.shape),
        "dtype": str(hologram.dtype),
        "magnitude_min": float(magnitude.min()),
        "magnitude_max": float(magnitude.max()),
        "phase_min_rad": float(phase.min()),
        "phase_max_rad": float(phase.max()),
        "helical_signature_rad": [float(value) for value in signature],
        "symmetry": symmetry_report(hologram, board, charge),
        "element_phase_winding": element_winding_number(hologram, board),
        "acoustools_path": str(acoustools.__file__),
        "hardware_opened": False,
    }
    npy_path, metadata_path = write_outputs(hologram, output_path, metadata)
    print(f"[GEN] convention={convention} charge={charge}")
    print(
        "[GEN] board sign: top={top:+d} bottom={bottom:+d}".format(
            top=signs["top"], bottom=signs["bottom"]
        )
    )
    print(f"[GEN] magnitude {magnitude.min():.9g} .. {magnitude.max():.9g}")
    print(f"[GEN] SHA-256 {metadata['sha256']}")
    print(f"[GEN] {npy_path}")
    print(f"[GEN] {metadata_path}")
    return metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--convention",
        choices=sorted(CONVENTIONS) + ["both"],
        default=DEFAULT_CONVENTION,
    )
    parser.add_argument("--charge", type=int, default=DEFAULT_CHARGE)
    parser.add_argument(
        "--output",
        type=Path,
        help="Explicit output .npy path (only with a single --convention).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    conventions = (
        sorted(CONVENTIONS) if args.convention == "both" else [args.convention]
    )
    if args.output is not None and len(conventions) != 1:
        raise SystemExit("[ERROR] --output requires a single --convention")
    for convention in conventions:
        output_path = args.output or default_output_path(convention, args.charge)
        generate(convention, args.charge, output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
