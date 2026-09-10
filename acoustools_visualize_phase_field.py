#!/usr/bin/env python3
"""Visualize a two-board AcousTools phase map and simulated pressure slices."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Callable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import torch

from acoustools.Utilities import BOARD_POSITIONS, DTYPE, propagate

from acoustools_send_phase_npy import load_phase_map


BOARD_SIDE = 16
DEFAULT_RESOLUTION = 241
DEFAULT_LATERAL_MM = 40.0
DEFAULT_AXIAL_MM = 60.0
DEFAULT_CHUNK_SIZE = 4096


def make_plane_coordinates(
    plane: str,
    horizontal_m: np.ndarray,
    vertical_m: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return mesh coordinates and flattened XYZ positions for a centre plane."""
    horizontal_grid, vertical_grid = np.meshgrid(
        horizontal_m, vertical_m, indexing="xy"
    )
    zeros = np.zeros(horizontal_grid.size, dtype=np.float32)
    h_flat = horizontal_grid.reshape(-1).astype(np.float32, copy=False)
    v_flat = vertical_grid.reshape(-1).astype(np.float32, copy=False)
    if plane == "xy":
        xyz = np.column_stack((h_flat, v_flat, zeros))
    elif plane == "xz":
        xyz = np.column_stack((h_flat, zeros, v_flat))
    elif plane == "yz":
        xyz = np.column_stack((zeros, h_flat, v_flat))
    else:
        raise ValueError(f"Unsupported plane: {plane}")
    return horizontal_grid, vertical_grid, xyz


def simulate_pressure(
    hologram: torch.Tensor,
    xyz_m: np.ndarray,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    propagate_function: Callable[..., torch.Tensor] = propagate,
) -> np.ndarray:
    """Run the AcousTools piston propagation model in bounded-memory chunks."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    values: list[np.ndarray] = []
    with torch.inference_mode():
        for start in range(0, len(xyz_m), chunk_size):
            xyz_chunk = xyz_m[start : start + chunk_size]
            points = torch.from_numpy(xyz_chunk.T.copy()).unsqueeze(0).to(DTYPE)
            pressure = propagate_function(hologram, points)
            values.append(
                pressure.detach().cpu().numpy().reshape(-1).astype(np.complex64)
            )
    return np.concatenate(values)


def phase_boards(hologram: torch.Tensor) -> tuple[np.ndarray, np.ndarray]:
    """Return top/bottom 16x16 phase maps with x horizontal and y vertical."""
    flat = hologram.detach().cpu().numpy().reshape(-1)
    if flat.size != 2 * BOARD_SIDE * BOARD_SIDE:
        raise ValueError(f"Expected 512 transducers, got {flat.size}")
    phase = np.angle(flat)
    top = phase[: BOARD_SIDE * BOARD_SIDE].reshape(BOARD_SIDE, BOARD_SIDE).T
    bottom = phase[BOARD_SIDE * BOARD_SIDE :].reshape(BOARD_SIDE, BOARD_SIDE).T
    return top, bottom


def plane_peak(
    pressure: np.ndarray,
    horizontal_mm: np.ndarray,
    vertical_mm: np.ndarray,
) -> dict[str, float]:
    magnitude = np.abs(pressure)
    index = np.unravel_index(int(np.argmax(magnitude)), magnitude.shape)
    return {
        "pressure_pa": float(magnitude[index]),
        "horizontal_mm": float(horizontal_mm[index[1]]),
        "vertical_mm": float(vertical_mm[index[0]]),
    }


def save_summary_figure(
    output_path: Path,
    source_name: str,
    top_phase: np.ndarray,
    bottom_phase: np.ndarray,
    pressure: dict[str, np.ndarray],
    axes_mm: dict[str, tuple[np.ndarray, np.ndarray]],
) -> None:
    fig, axes = plt.subplots(2, 4, figsize=(19.2, 9.6), constrained_layout=True)
    element_extent = (-7.5, 7.5, -7.5, 7.5)
    phase_images = []
    for ax, values, title in (
        (axes[0, 0], top_phase, "Top board phase (z = +118.25 mm)"),
        (axes[0, 1], bottom_phase, "Bottom board phase (z = -118.25 mm)"),
    ):
        image = ax.imshow(
            values,
            origin="lower",
            extent=element_extent,
            interpolation="nearest",
            cmap="twilight",
            vmin=-np.pi,
            vmax=np.pi,
        )
        phase_images.append(image)
        ax.set_title(title)
        ax.set_xlabel("Element x index")
        ax.set_ylabel("Element y index")
        ax.set_xticks([-7.5, -3.5, 0.5, 4.5, 7.5])
        ax.set_yticks([-7.5, -3.5, 0.5, 4.5, 7.5])
    fig.colorbar(
        phase_images[0],
        ax=[axes[0, 0], axes[0, 1]],
        label="Phase (rad)",
        shrink=0.85,
    )

    magnitudes = [np.abs(pressure[name]) for name in ("xy", "xz", "yz")]
    pressure_vmax = float(np.percentile(np.concatenate([x.ravel() for x in magnitudes]), 99.5))
    pressure_vmax = max(pressure_vmax, np.finfo(np.float32).eps)
    pressure_image = None
    field_layout = (
        (axes[0, 2], "xy", "|p|, XY at z = 0", "x (mm)", "y (mm)", False),
        (axes[1, 0], "xz", "|p|, XZ at y = 0", "x (mm)", "z (mm)", False),
        (axes[1, 2], "yz", "|p|, YZ at x = 0", "y (mm)", "z (mm)", False),
        (axes[0, 3], "xy", "arg(p), XY at z = 0", "x (mm)", "y (mm)", True),
        (axes[1, 1], "xz", "arg(p), XZ at y = 0", "x (mm)", "z (mm)", True),
        (axes[1, 3], "yz", "arg(p), YZ at x = 0", "y (mm)", "z (mm)", True),
    )
    phase_field_image = None
    for ax, plane, title, xlabel, ylabel, show_phase in field_layout:
        horizontal_mm, vertical_mm = axes_mm[plane]
        extent = (
            float(horizontal_mm[0]),
            float(horizontal_mm[-1]),
            float(vertical_mm[0]),
            float(vertical_mm[-1]),
        )
        if show_phase:
            image = ax.imshow(
                np.angle(pressure[plane]),
                origin="lower",
                extent=extent,
                aspect="auto",
                cmap="twilight",
                vmin=-np.pi,
                vmax=np.pi,
            )
            phase_field_image = image
        else:
            image = ax.imshow(
                np.abs(pressure[plane]),
                origin="lower",
                extent=extent,
                aspect="auto",
                cmap="inferno",
                vmin=0.0,
                vmax=pressure_vmax,
            )
            pressure_image = image
        ax.axhline(0.0, color="white", linewidth=0.5, alpha=0.55)
        ax.axvline(0.0, color="white", linewidth=0.5, alpha=0.55)
        ax.plot(0.0, 0.0, marker="+", color="cyan", markersize=8, markeredgewidth=1.2)
        ax.set_title(title)
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)

    if pressure_image is not None:
        fig.colorbar(
            pressure_image,
            ax=[axes[0, 2], axes[1, 0], axes[1, 2]],
            label="Pressure magnitude |p| (Pa; clipped at 99.5 percentile)",
            shrink=0.82,
        )
    if phase_field_image is not None:
        fig.colorbar(
            phase_field_image,
            ax=[axes[0, 3], axes[1, 1], axes[1, 3]],
            label="Acoustic pressure phase (rad)",
            shrink=0.82,
        )
    fig.suptitle(f"AcousTools phase map and simulated acoustic field\n{source_name}", fontsize=15)
    fig.savefig(output_path, dpi=180)
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase_npy",
        nargs="?",
        type=Path,
        default=Path("phase_acoustools_regular_complex64.npy"),
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--resolution", type=int, default=DEFAULT_RESOLUTION)
    parser.add_argument("--lateral-mm", type=float, default=DEFAULT_LATERAL_MM)
    parser.add_argument("--axial-mm", type=float, default=DEFAULT_AXIAL_MM)
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.resolution < 3:
        raise SystemExit("[ERROR] --resolution must be at least 3")
    if args.lateral_mm <= 0 or args.axial_mm <= 0:
        raise SystemExit("[ERROR] field extents must be positive")

    hologram, source_info = load_phase_map(args.phase_npy)
    output_dir = (
        args.output_dir
        if args.output_dir is not None
        else source_info.path.with_name(source_info.path.stem + "_visualization")
    ).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    lateral_m = np.linspace(
        -args.lateral_mm * 1e-3,
        args.lateral_mm * 1e-3,
        args.resolution,
        dtype=np.float32,
    )
    axial_m = np.linspace(
        -args.axial_mm * 1e-3,
        args.axial_mm * 1e-3,
        args.resolution,
        dtype=np.float32,
    )
    pressure: dict[str, np.ndarray] = {}
    axes_mm: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for plane, horizontal, vertical in (
        ("xy", lateral_m, lateral_m),
        ("xz", lateral_m, axial_m),
        ("yz", lateral_m, axial_m),
    ):
        print(f"[SIM] AcousTools propagate(): {plane.upper()} plane", flush=True)
        horizontal_grid, _, xyz_m = make_plane_coordinates(plane, horizontal, vertical)
        result = simulate_pressure(
            hologram,
            xyz_m,
            chunk_size=args.chunk_size,
        ).reshape(horizontal_grid.shape)
        pressure[plane] = result
        axes_mm[plane] = (horizontal.astype(np.float64) * 1e3, vertical.astype(np.float64) * 1e3)

    top_phase, bottom_phase = phase_boards(hologram)
    summary_path = output_dir / "phase_and_acoustic_field.png"
    save_summary_figure(
        summary_path,
        source_info.path.name,
        top_phase,
        bottom_phase,
        pressure,
        axes_mm,
    )

    np.savez_compressed(
        output_dir / "acoustic_field_slices.npz",
        xy_pressure=pressure["xy"],
        xz_pressure=pressure["xz"],
        yz_pressure=pressure["yz"],
        lateral_mm=axes_mm["xy"][0],
        axial_mm=axes_mm["xz"][1],
        top_phase_rad=top_phase,
        bottom_phase_rad=bottom_phase,
    )
    metadata = {
        "source_npy": str(source_info.path),
        "source_sha256": source_info.sha256,
        "simulation": "acoustools.Utilities.propagate (default two-board piston model)",
        "board_z_mm": [float(BOARD_POSITIONS * 1e3), float(-BOARD_POSITIONS * 1e3)],
        "resolution": int(args.resolution),
        "lateral_extent_mm": [-float(args.lateral_mm), float(args.lateral_mm)],
        "axial_extent_mm": [-float(args.axial_mm), float(args.axial_mm)],
        "origin_pressure_pa": {
            plane: float(np.abs(values[args.resolution // 2, args.resolution // 2]))
            for plane, values in pressure.items()
        },
        "plane_peak": {
            plane: plane_peak(values, *axes_mm[plane])
            for plane, values in pressure.items()
        },
        "outputs": {
            "summary_png": str(summary_path),
            "field_npz": str(output_dir / "acoustic_field_slices.npz"),
        },
        "hardware_opened": False,
    }
    metadata_path = output_dir / "simulation_metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[DONE] {summary_path}")
    print(f"[DONE] {metadata_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
