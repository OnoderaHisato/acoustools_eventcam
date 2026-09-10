#!/usr/bin/env python3
"""Render a phase-map field with AcousTools' built-in ``Visualise`` function."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from acoustools.Utilities import propagate_abs, propagate_phase
from acoustools.Visualiser import ABC, Visualise

from acoustools_send_phase_npy import load_phase_map
from acoustools_visualize_phase_field import phase_boards


DEFAULT_SIZE_MM = 40.0
DEFAULT_RESOLUTION = 240
DEFAULT_DEPTH = 2


def save_element_phase_maps(
    output_path: Path,
    top_phase: np.ndarray,
    bottom_phase: np.ndarray,
    source_name: str,
) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(10.8, 4.8), constrained_layout=True)
    image = None
    for ax, values, title in (
        (axes[0], top_phase, "Top: elements 0--255 (board 101)"),
        (axes[1], bottom_phase, "Bottom: elements 256--511 (board 3)"),
    ):
        image = ax.imshow(
            values,
            origin="lower",
            interpolation="nearest",
            cmap="twilight",
            vmin=-np.pi,
            vmax=np.pi,
        )
        ax.set_title(title)
        ax.set_xlabel("Element x index")
        ax.set_ylabel("Element y index")
    if image is not None:
        fig.colorbar(image, ax=axes, label="Phase (rad)", shrink=0.88)
    fig.suptitle(f"Element phases (AcousTools order)\n{source_name}")
    fig.savefig(output_path, dpi=180)
    plt.close(fig)


def render_builtin_visualiser(
    output_path: Path,
    hologram,
    *,
    size_m: float,
    resolution: int,
    depth: int,
    phase: bool,
    source_name: str,
) -> None:
    planes = ("xy", "xz", "yz")
    corners = [ABC(size_m, plane=plane) for plane in planes]
    a_values = [corner[0] for corner in corners]
    b_values = [corner[1] for corner in corners]
    c_values = [corner[2] for corner in corners]
    titles = [
        "XY at z = 0",
        "XZ at y = 0",
        "YZ at x = 0",
    ]
    colour_function = propagate_phase if phase else propagate_abs
    cmap = "twilight" if phase else "inferno"
    label = "Acoustic pressure phase (rad)" if phase else "Pressure magnitude |p| (Pa)"

    plt.figure(figsize=(15.8, 5.2))
    kwargs = {
        "A": a_values,
        "B": b_values,
        "C": c_values,
        "activation": hologram,
        "colour_functions": [colour_function] * len(planes),
        "colour_function_args": [{} for _ in planes],
        "res": (resolution, resolution),
        "cmaps": [cmap] * len(planes),
        "show": False,
        "depth": depth,
        "link_ax": "all",
        "arrangement": (1, 3),
        "titles": titles,
        "clr_labels": [label] * len(planes),
    }
    if phase:
        kwargs["vmin"] = -np.pi
        kwargs["vmax"] = np.pi
    fig = Visualise(**kwargs)
    field_name = "pressure phase" if phase else "pressure magnitude"
    fig.set_size_inches(15.8, 5.2)
    fig.suptitle(
        f"AcousTools Visualiser.Visualise: {field_name}\n"
        f"{source_name} | each plane +/-{size_m * 1e3:.1f} mm",
        fontsize=13,
    )
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.90))
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
    parser.add_argument("--size-mm", type=float, default=DEFAULT_SIZE_MM)
    parser.add_argument("--resolution", type=int, default=DEFAULT_RESOLUTION)
    parser.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.size_mm <= 0:
        raise SystemExit("[ERROR] --size-mm must be positive")
    if args.resolution < 4:
        raise SystemExit("[ERROR] --resolution must be at least 4")
    if args.depth < 0:
        raise SystemExit("[ERROR] --depth must be non-negative")
    divisor = 2 ** max(args.depth, 0)
    if args.resolution % divisor != 0:
        raise SystemExit(
            f"[ERROR] --resolution must be divisible by 2**depth ({divisor})"
        )

    hologram, source_info = load_phase_map(args.phase_npy)
    output_dir = (
        args.output_dir
        if args.output_dir is not None
        else source_info.path.with_name(source_info.path.stem + "_acoustools_visualiser")
    ).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    top_phase, bottom_phase = phase_boards(hologram)
    element_path = output_dir / "element_phase_maps.png"
    pressure_path = output_dir / "acoustools_visualiser_pressure.png"
    field_phase_path = output_dir / "acoustools_visualiser_pressure_phase.png"

    save_element_phase_maps(
        element_path,
        top_phase,
        bottom_phase,
        source_info.path.name,
    )
    print("[VIS] AcousTools Visualise(): pressure magnitude", flush=True)
    render_builtin_visualiser(
        pressure_path,
        hologram,
        size_m=args.size_mm * 1e-3,
        resolution=args.resolution,
        depth=args.depth,
        phase=False,
        source_name=source_info.path.name,
    )
    print("[VIS] AcousTools Visualise(): pressure phase", flush=True)
    render_builtin_visualiser(
        field_phase_path,
        hologram,
        size_m=args.size_mm * 1e-3,
        resolution=args.resolution,
        depth=args.depth,
        phase=True,
        source_name=source_info.path.name,
    )

    metadata = {
        "source_npy": str(source_info.path),
        "source_sha256": source_info.sha256,
        "field_renderer": "acoustools.Visualiser.Visualise",
        "pressure_function": "acoustools.Utilities.propagate_abs",
        "pressure_phase_function": "acoustools.Utilities.propagate_phase",
        "planes": ["xy@z=0", "xz@y=0", "yz@x=0"],
        "half_extent_mm": float(args.size_mm),
        "resolution": [int(args.resolution), int(args.resolution)],
        "visualiser_depth": int(args.depth),
        "hardware_opened": False,
        "outputs": [str(element_path), str(pressure_path), str(field_phase_path)],
    }
    metadata_path = output_dir / "acoustools_visualiser_metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[DONE] {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
