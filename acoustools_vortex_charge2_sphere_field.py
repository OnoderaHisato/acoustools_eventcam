#!/usr/bin/env python3
"""Simulate and plot the acoustic field of a focus+vortex hologram around the sphere.

The particle is the 10 mm diameter EPS sphere used by
``hologram_optimization_package`` (radius ``r_a = 5 mm``), placed at the PAT
origin, which is also where the focus of the hologram sits.

All pressure values come from the standard AcousTools two-board piston model
``acoustools.Utilities.propagate`` with the current ``acoustools.Constants``.
No hardware is opened.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import torch
from matplotlib.patches import Circle

import acoustools
import acoustools.Constants as Constants
from acoustools.Utilities import BOARD_POSITIONS

from acoustools_send_phase_npy import load_phase_map
from acoustools_visualize_phase_field import phase_boards, simulate_pressure


BOARD_SIDE = 16
BOARD_SIZE = BOARD_SIDE * BOARD_SIDE
SPHERE_RADIUS_M = 0.005
DEFAULT_LATERAL_MM = 20.0
DEFAULT_AXIAL_MM = 20.0
DEFAULT_RESOLUTION = 401
DEFAULT_CHUNK = 2048
SURFACE_THETA = 181
SURFACE_PHI = 361
RING_SAMPLES = 720


def plane_points(plane: str, horizontal_m: np.ndarray, vertical_m: np.ndarray):
    """Return the flattened XYZ samples of a centre plane and its mesh shape."""
    h_grid, v_grid = np.meshgrid(horizontal_m, vertical_m, indexing="xy")
    zeros = np.zeros(h_grid.size, dtype=np.float32)
    h_flat = h_grid.reshape(-1).astype(np.float32, copy=False)
    v_flat = v_grid.reshape(-1).astype(np.float32, copy=False)
    if plane == "xy":
        xyz = np.column_stack((h_flat, v_flat, zeros))
    elif plane == "xz":
        xyz = np.column_stack((h_flat, zeros, v_flat))
    elif plane == "yz":
        xyz = np.column_stack((zeros, h_flat, v_flat))
    else:
        raise ValueError(f"Unsupported plane: {plane}")
    return xyz, h_grid.shape


def sphere_surface_points(radius_m: float):
    """Return sphere-surface samples plus the theta/phi grids used to build them."""
    theta = np.linspace(0.0, np.pi, SURFACE_THETA)
    phi = np.linspace(-np.pi, np.pi, SURFACE_PHI)
    theta_grid, phi_grid = np.meshgrid(theta, phi, indexing="ij")
    x = radius_m * np.sin(theta_grid) * np.cos(phi_grid)
    y = radius_m * np.sin(theta_grid) * np.sin(phi_grid)
    z = radius_m * np.cos(theta_grid)
    xyz = np.column_stack(
        (x.reshape(-1), y.reshape(-1), z.reshape(-1))
    ).astype(np.float32)
    return xyz, theta, phi, theta_grid.shape


def ring_points(radius_m: float, samples: int = RING_SAMPLES):
    """Return a closed circle of samples in the z = 0 plane."""
    phi = np.linspace(-np.pi, np.pi, samples, endpoint=False)
    xyz = np.column_stack(
        (
            radius_m * np.cos(phi),
            radius_m * np.sin(phi),
            np.zeros_like(phi),
        )
    ).astype(np.float32)
    return xyz, phi


AZIMUTHAL_ORDERS = tuple(range(-6, 7))


def winding_number(ring_pressure: np.ndarray) -> float:
    """Topological charge of the pressure phase around a closed ring.

    Only meaningful for a travelling (single-handedness) field.  For a standing
    azimuthal pattern the phase merely steps by +/-pi at every amplitude null, so
    the result depends on rounding; use `azimuthal_spectrum` to tell the two
    cases apart.
    """
    phase = np.angle(ring_pressure)
    steps = np.angle(np.exp(1j * np.diff(np.append(phase, phase[0]))))
    return float(np.sum(steps) / (2.0 * np.pi))


def azimuthal_spectrum(ring_pressure: np.ndarray, phi: np.ndarray) -> dict:
    """Decompose p(phi) into exp(1j*m*phi) modes on a uniformly sampled ring.

    A single-handedness vortex of charge m puts essentially all of the energy in
    one order; a standing pattern splits it equally between +m and -m.
    """
    basis = np.exp(-1j * np.outer(np.asarray(AZIMUTHAL_ORDERS), phi))
    coefficients = basis @ ring_pressure / len(phi)
    magnitude = np.abs(coefficients)
    total = float(np.sum(magnitude**2))
    dominant = int(AZIMUTHAL_ORDERS[int(np.argmax(magnitude))])
    return {
        "orders": [int(order) for order in AZIMUTHAL_ORDERS],
        "coefficient_magnitude_pa": [float(value) for value in magnitude],
        "dominant_order": dominant,
        "energy_fraction": {
            str(order): float(magnitude[index] ** 2 / total)
            if total > 0.0
            else 0.0
            for index, order in enumerate(AZIMUTHAL_ORDERS)
        },
        "chirality_contrast": (
            float(
                (magnitude[AZIMUTHAL_ORDERS.index(2)] - magnitude[AZIMUTHAL_ORDERS.index(-2)])
                / max(
                    magnitude[AZIMUTHAL_ORDERS.index(2)]
                    + magnitude[AZIMUTHAL_ORDERS.index(-2)],
                    np.finfo(np.float64).tiny,
                )
            )
        ),
    }


def surface_solid_angle_weights(theta: np.ndarray, phi: np.ndarray) -> np.ndarray:
    """Trapezoidal solid-angle weights for the (theta, phi) surface grid."""
    d_theta = np.gradient(theta)
    d_phi = np.gradient(phi)
    return np.outer(np.sin(theta) * d_theta, d_phi)


def compute_fields(hologram: torch.Tensor, args) -> dict:
    lateral_m = np.linspace(
        -args.lateral_mm * 1e-3, args.lateral_mm * 1e-3, args.resolution, dtype=np.float32
    )
    axial_m = np.linspace(
        -args.axial_mm * 1e-3, args.axial_mm * 1e-3, args.resolution, dtype=np.float32
    )

    planes: dict[str, np.ndarray] = {}
    axes_mm: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for plane, horizontal, vertical in (
        ("xy", lateral_m, lateral_m),
        ("xz", lateral_m, axial_m),
        ("yz", lateral_m, axial_m),
    ):
        print(f"[SIM] plane {plane.upper()}", flush=True)
        xyz, shape = plane_points(plane, horizontal, vertical)
        planes[plane] = simulate_pressure(
            hologram, xyz, chunk_size=args.chunk_size
        ).reshape(shape)
        axes_mm[plane] = (
            horizontal.astype(np.float64) * 1e3,
            vertical.astype(np.float64) * 1e3,
        )

    print("[SIM] sphere surface", flush=True)
    surface_xyz, theta, phi, surface_shape = sphere_surface_points(args.sphere_radius_m)
    surface = simulate_pressure(
        hologram, surface_xyz, chunk_size=args.chunk_size
    ).reshape(surface_shape)

    rings: dict[str, dict] = {}
    ring_radii_mm = sorted({1.0, 2.5, args.sphere_radius_m * 1e3, 10.0})
    for radius_mm in ring_radii_mm:
        xyz, ring_phi = ring_points(radius_mm * 1e-3)
        pressure = simulate_pressure(hologram, xyz, chunk_size=args.chunk_size)
        rings[f"{radius_mm:g}mm"] = {
            "radius_mm": float(radius_mm),
            "phi_rad": ring_phi,
            "pressure": pressure,
            "winding_number": winding_number(pressure),
            "azimuthal_spectrum": azimuthal_spectrum(pressure, ring_phi),
            "magnitude_min_pa": float(np.abs(pressure).min()),
            "magnitude_max_pa": float(np.abs(pressure).max()),
        }

    print("[SIM] profiles", flush=True)
    radial_mm = np.linspace(0.0, args.lateral_mm, 401)
    radial: dict[str, np.ndarray] = {}
    for azimuth_deg in (0.0, 45.0, 90.0, 135.0):
        angle = np.deg2rad(azimuth_deg)
        xyz = np.column_stack(
            (
                radial_mm * 1e-3 * np.cos(angle),
                radial_mm * 1e-3 * np.sin(angle),
                np.zeros_like(radial_mm),
            )
        ).astype(np.float32)
        radial[f"{azimuth_deg:g}deg"] = simulate_pressure(
            hologram, xyz, chunk_size=args.chunk_size
        )

    # The z axis is the vortex core, so an on-axis profile alone is just numerical
    # noise; sample a parallel line on the sphere radius as well.
    axial_mm = np.linspace(-args.axial_mm, args.axial_mm, 401)
    axial_profiles: dict[str, np.ndarray] = {}
    for label, offset_mm in (
        ("r0", 0.0),
        (f"r{args.sphere_radius_m * 1e3:g}_phi0", args.sphere_radius_m * 1e3),
    ):
        axial_xyz = np.column_stack(
            (
                np.full_like(axial_mm, offset_mm * 1e-3),
                np.zeros_like(axial_mm),
                axial_mm * 1e-3,
            )
        ).astype(np.float32)
        axial_profiles[label] = simulate_pressure(
            hologram, axial_xyz, chunk_size=args.chunk_size
        )
    axial_profile = axial_profiles["r0"]

    origin = simulate_pressure(
        hologram, np.zeros((1, 3), dtype=np.float32), chunk_size=args.chunk_size
    )[0]

    return {
        "planes": planes,
        "axes_mm": axes_mm,
        "surface": surface,
        "theta": theta,
        "phi": phi,
        "rings": rings,
        "radial_mm": radial_mm,
        "radial": radial,
        "axial_mm": axial_mm,
        "axial_profile": axial_profile,
        "axial_profiles": axial_profiles,
        "origin_pressure": origin,
    }


def add_sphere_outline(ax, radius_mm: float) -> None:
    ax.add_patch(
        Circle(
            (0.0, 0.0),
            radius_mm,
            fill=False,
            edgecolor="white",
            linestyle="--",
            linewidth=1.6,
            alpha=0.95,
        )
    )
    ax.plot(0.0, 0.0, marker="+", color="white", markersize=9, markeredgewidth=1.3)


def load_helical_signature(source_path: Path) -> np.ndarray | None:
    """Read the generator's recorded helical signature, if it sits next to the npy."""
    metadata_path = source_path.with_name(source_path.stem + "_metadata.json")
    if not metadata_path.is_file():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    values = payload.get("helical_signature_rad")
    if values is None or len(values) != 2 * BOARD_SIZE:
        return None
    return np.asarray(values, dtype=np.float64)


def plot_element_phases(
    path: Path,
    hologram: torch.Tensor,
    title: str,
    signature_rad: np.ndarray | None = None,
) -> None:
    """Total element phase, plus the helical signature alone when it is known.

    The focus term dominates the wrapped total phase, so the opposite winding of
    the two boards is only legible once the focus is taken back out.
    """
    rows: list[tuple[str, np.ndarray, np.ndarray]] = [
        ("total hologram phase", *phase_boards(hologram))
    ]
    if signature_rad is not None:
        wrapped = np.angle(np.exp(1j * signature_rad))
        rows.append(
            (
                "helical signature only (focus removed)",
                wrapped[:BOARD_SIZE].reshape(BOARD_SIDE, BOARD_SIDE).T,
                wrapped[BOARD_SIZE:].reshape(BOARD_SIDE, BOARD_SIDE).T,
            )
        )

    fig, axes = plt.subplots(
        len(rows),
        2,
        figsize=(10.6, 4.7 * len(rows)),
        squeeze=False,
        constrained_layout=True,
    )
    image = None
    for row, (row_label, top, bottom) in enumerate(rows):
        for column, (values, board_label) in enumerate(
            (
                (top, "Top board, elements 0--255 (z = +118.25 mm)"),
                (bottom, "Bottom board, elements 256--511 (z = -118.25 mm)"),
            )
        ):
            ax = axes[row, column]
            image = ax.imshow(
                values,
                origin="lower",
                extent=(-7.5, 7.5, -7.5, 7.5),
                interpolation="nearest",
                cmap="twilight",
                vmin=-np.pi,
                vmax=np.pi,
            )
            ax.set_title(f"{board_label}\n{row_label}", fontsize=9)
            ax.set_xlabel("element x index")
            ax.set_ylabel("element y index")
    fig.colorbar(image, ax=axes, label="phase (rad)", shrink=0.9)
    fig.suptitle(f"Transducer phases\n{title}", fontsize=12)
    fig.savefig(path, dpi=180)
    plt.close(fig)


def plot_planes(path: Path, data: dict, radius_mm: float, title: str) -> None:
    planes = data["planes"]
    axes_mm = data["axes_mm"]
    layout = (
        ("xy", "XY plane, z = 0", "x (mm)", "y (mm)"),
        ("xz", "XZ plane, y = 0", "x (mm)", "z (mm)"),
        ("yz", "YZ plane, x = 0", "y (mm)", "z (mm)"),
    )
    magnitudes = np.concatenate([np.abs(planes[name]).ravel() for name, *_ in layout])
    vmax = float(np.percentile(magnitudes, 99.5))

    fig, axes = plt.subplots(2, 3, figsize=(16.2, 9.4), constrained_layout=True)
    magnitude_image = None
    phase_image = None
    for column, (name, plane_title, xlabel, ylabel) in enumerate(layout):
        horizontal_mm, vertical_mm = axes_mm[name]
        extent = (
            float(horizontal_mm[0]),
            float(horizontal_mm[-1]),
            float(vertical_mm[0]),
            float(vertical_mm[-1]),
        )
        magnitude_image = axes[0, column].imshow(
            np.abs(planes[name]),
            origin="lower",
            extent=extent,
            cmap="inferno",
            vmin=0.0,
            vmax=vmax,
        )
        phase_image = axes[1, column].imshow(
            np.angle(planes[name]),
            origin="lower",
            extent=extent,
            cmap="twilight",
            vmin=-np.pi,
            vmax=np.pi,
        )
        for row, prefix in ((0, "|p|"), (1, "arg(p)")):
            ax = axes[row, column]
            ax.set_title(f"{prefix}, {plane_title}", fontsize=11)
            ax.set_xlabel(xlabel)
            ax.set_ylabel(ylabel)
            ax.set_aspect("equal")
            add_sphere_outline(ax, radius_mm)

    fig.colorbar(
        magnitude_image,
        ax=axes[0, :],
        label="pressure magnitude |p| (Pa, clipped at 99.5 pct)",
        shrink=0.86,
    )
    fig.colorbar(
        phase_image,
        ax=axes[1, :],
        label="pressure phase arg(p) (rad)",
        shrink=0.86,
    )
    fig.suptitle(
        "Acoustic field around the 10 mm EPS sphere "
        f"(dashed circle, r = {radius_mm:.1f} mm)\n{title}",
        fontsize=13,
    )
    fig.savefig(path, dpi=170)
    plt.close(fig)


def plot_sphere_surface(path: Path, data: dict, radius_mm: float, title: str) -> None:
    surface = data["surface"]
    theta_deg = np.rad2deg(data["theta"])
    phi_deg = np.rad2deg(data["phi"])
    equator = data["rings"][f"{radius_mm:g}mm"]
    equator_phi_deg = np.rad2deg(equator["phi_rad"])
    equator_pressure = equator["pressure"]

    fig, axes = plt.subplots(2, 3, figsize=(19.2, 9.0), constrained_layout=True)

    magnitude_image = axes[0, 0].pcolormesh(
        phi_deg, theta_deg, np.abs(surface), cmap="inferno", shading="auto"
    )
    axes[0, 0].set_title(f"|p| on the sphere surface (r = {radius_mm:.1f} mm)")
    fig.colorbar(magnitude_image, ax=axes[0, 0], label="|p| (Pa)")

    phase_image = axes[0, 1].pcolormesh(
        phi_deg,
        theta_deg,
        np.angle(surface),
        cmap="twilight",
        shading="auto",
        vmin=-np.pi,
        vmax=np.pi,
    )
    axes[0, 1].set_title("arg(p) on the sphere surface")
    fig.colorbar(phase_image, ax=axes[0, 1], label="arg(p) (rad)")

    for ax in (axes[0, 0], axes[0, 1]):
        ax.set_xlabel("azimuth phi = atan2(y, x) (deg)")
        ax.set_ylabel("polar angle theta from +z (deg)")
        ax.invert_yaxis()
        ax.axhline(90.0, color="cyan", linewidth=0.9, alpha=0.8)
        ax.set_xticks([-180, -90, 0, 90, 180])

    axes[1, 0].plot(equator_phi_deg, np.abs(equator_pressure), color="crimson")
    axes[1, 0].set_title("|p| on the equator of the sphere (theta = 90 deg)")
    axes[1, 0].set_xlabel("azimuth phi (deg)")
    axes[1, 0].set_ylabel("|p| (Pa)")
    axes[1, 0].set_xticks([-180, -90, 0, 90, 180])
    axes[1, 0].grid(alpha=0.3)

    unwrapped = np.unwrap(np.angle(equator_pressure))
    axes[1, 1].plot(equator_phi_deg, unwrapped, color="navy")
    spectrum = equator["azimuthal_spectrum"]
    axes[1, 1].set_title(
        "unwrapped arg(p) on the equator | "
        f"dominant m = {spectrum['dominant_order']:+d}, "
        f"chirality contrast = {spectrum['chirality_contrast']:+.3f}"
    )
    axes[1, 1].set_xlabel("azimuth phi (deg)")
    axes[1, 1].set_ylabel("unwrapped arg(p) (rad)")
    axes[1, 1].set_xticks([-180, -90, 0, 90, 180])
    axes[1, 1].grid(alpha=0.3)

    orders = np.asarray(spectrum["orders"])
    coefficients = np.asarray(spectrum["coefficient_magnitude_pa"])
    colours = ["crimson" if abs(order) == 2 else "steelblue" for order in orders]
    axes[0, 2].bar(orders, coefficients, color=colours)
    axes[0, 2].set_title(
        f"azimuthal modes of p(phi) on the equator (r = {radius_mm:.1f} mm)"
    )
    axes[0, 2].set_xlabel("azimuthal order m in exp(1j*m*phi)")
    axes[0, 2].set_ylabel("|c_m| (Pa)")
    axes[0, 2].set_xticks(orders)
    axes[0, 2].grid(alpha=0.3, axis="y")

    ring_radii = [ring["radius_mm"] for ring in data["rings"].values()]
    contrasts = [
        ring["azimuthal_spectrum"]["chirality_contrast"] for ring in data["rings"].values()
    ]
    order = np.argsort(ring_radii)
    axes[1, 2].plot(
        np.asarray(ring_radii)[order],
        np.asarray(contrasts)[order],
        marker="o",
        color="darkorange",
    )
    axes[1, 2].axhline(0.0, color="black", linewidth=0.8)
    axes[1, 2].axvline(
        radius_mm, color="black", linestyle="--", linewidth=1.1, label="sphere surface"
    )
    axes[1, 2].set_ylim(-1.1, 1.1)
    axes[1, 2].set_title("chirality contrast (|c_+2| - |c_-2|) / (|c_+2| + |c_-2|)")
    axes[1, 2].set_xlabel("ring radius in the z = 0 plane (mm)")
    axes[1, 2].set_ylabel("contrast")
    axes[1, 2].legend(fontsize=9)
    axes[1, 2].grid(alpha=0.3)

    fig.suptitle(f"Acoustic pressure sampled on the particle surface\n{title}", fontsize=13)
    fig.savefig(path, dpi=170)
    plt.close(fig)


def plot_sphere_3d(path: Path, data: dict, radius_mm: float, title: str) -> None:
    surface = data["surface"]
    theta_grid, phi_grid = np.meshgrid(data["theta"], data["phi"], indexing="ij")
    x = radius_mm * np.sin(theta_grid) * np.cos(phi_grid)
    y = radius_mm * np.sin(theta_grid) * np.sin(phi_grid)
    z = radius_mm * np.cos(theta_grid)

    magnitude = np.abs(surface)
    normalised = (magnitude - magnitude.min()) / max(
        float(magnitude.max() - magnitude.min()), np.finfo(np.float32).eps
    )
    colours = plt.get_cmap("inferno")(normalised)

    fig = plt.figure(figsize=(14.0, 5.4))
    for index, (elevation, azimuth, view_label) in enumerate(
        ((22.0, -60.0, "oblique"), (88.0, -90.0, "from +z (top)")), start=1
    ):
        ax = fig.add_subplot(1, 3, index, projection="3d")
        ax.plot_surface(
            x,
            y,
            z,
            facecolors=colours,
            rstride=2,
            cstride=2,
            linewidth=0,
            antialiased=False,
            shade=False,
        )
        ax.set_title(f"|p| on the sphere, view {view_label}", fontsize=10)
        ax.set_xlabel("x (mm)")
        ax.set_ylabel("y (mm)")
        ax.set_zlabel("z (mm)")
        ax.set_box_aspect((1.0, 1.0, 1.0))
        ax.view_init(elev=elevation, azim=azimuth)
        if elevation > 60.0:
            # Looking straight down +z: the z axis degenerates into label clutter.
            ax.set_zticks([])
            ax.set_zlabel("")

    ax_bar = fig.add_subplot(1, 3, 3)
    ax_bar.axis("off")
    mappable = plt.cm.ScalarMappable(
        cmap="inferno",
        norm=plt.Normalize(vmin=float(magnitude.min()), vmax=float(magnitude.max())),
    )
    fig.colorbar(mappable, ax=ax_bar, fraction=0.35, label="|p| (Pa)")
    ax_bar.text(
        0.0,
        0.5,
        (
            f"sphere radius {radius_mm:.1f} mm\n"
            f"|p| min {magnitude.min():.1f} Pa\n"
            f"|p| max {magnitude.max():.1f} Pa\n"
            f"|p| mean {magnitude.mean():.1f} Pa"
        ),
        fontsize=10,
        va="center",
    )
    fig.suptitle(f"Pressure magnitude on the 10 mm EPS sphere\n{title}", fontsize=12)
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.90))
    fig.savefig(path, dpi=170)
    plt.close(fig)


def plot_profiles(path: Path, data: dict, radius_mm: float, title: str) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(13.4, 5.0), constrained_layout=True)

    # Symmetry makes several azimuths coincide exactly, so vary the dash pattern
    # and width instead of relying on colour alone.
    styles = (
        ("-", 3.2, 0.55),
        ("--", 2.2, 0.85),
        (":", 1.8, 1.0),
        ("-.", 1.4, 1.0),
    )
    for (label, pressure), (dash, width, alpha) in zip(data["radial"].items(), styles):
        axes[0].plot(
            data["radial_mm"],
            np.abs(pressure),
            label=f"phi = {label}",
            linestyle=dash,
            linewidth=width,
            alpha=alpha,
        )
    axes[0].axvline(
        radius_mm, color="black", linestyle="--", linewidth=1.2, label="sphere surface"
    )
    axes[0].set_title("|p| along radial lines in the z = 0 plane")
    axes[0].set_xlabel("radius from centre (mm)")
    axes[0].set_ylabel("|p| (Pa)")
    axes[0].legend(fontsize=9)
    axes[0].grid(alpha=0.3)

    for label, pressure in data["axial_profiles"].items():
        readable = (
            "on the z axis (x = y = 0, vortex core)"
            if label == "r0"
            else f"parallel line at x = {radius_mm:g} mm, y = 0"
        )
        axes[1].semilogy(data["axial_mm"], np.abs(pressure), label=readable)
    axes[1].axvspan(-radius_mm, radius_mm, color="grey", alpha=0.22, label="sphere extent")
    axes[1].set_title("|p| along z (log scale)")
    axes[1].set_xlabel("z (mm)")
    axes[1].set_ylabel("|p| (Pa)")
    axes[1].legend(fontsize=9)
    axes[1].grid(alpha=0.3, which="both")

    fig.suptitle(f"Pressure profiles through the trap centre\n{title}", fontsize=12)
    fig.savefig(path, dpi=170)
    plt.close(fig)


def build_metadata(data: dict, source_info, args, outputs: list[Path]) -> dict:
    surface = data["surface"]
    magnitude = np.abs(surface)
    weights = surface_solid_angle_weights(data["theta"], data["phi"])
    weighted_mean = float(np.sum(magnitude * weights) / np.sum(weights))
    theta_index, phi_index = np.unravel_index(int(np.argmax(magnitude)), magnitude.shape)
    return {
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "source_npy": str(source_info.path),
        "source_sha256": source_info.sha256,
        "simulation": "acoustools.Utilities.propagate (two-board piston model)",
        "acoustools_path": str(acoustools.__file__),
        "acoustics": {
            "speed_of_sound_m_s": float(Constants.c_0),
            "wavelength_mm": float(Constants.wavelength * 1e3),
            "wavenumber_rad_m": float(Constants.k),
            "p_ref": float(Constants.P_ref),
            "board_z_mm": [
                float(BOARD_POSITIONS * 1e3),
                float(-BOARD_POSITIONS * 1e3),
            ],
        },
        "sphere": {
            "radius_mm": float(args.sphere_radius_m * 1e3),
            "diameter_mm": float(2.0 * args.sphere_radius_m * 1e3),
            "centre_mm": [0.0, 0.0, 0.0],
            "ka": float(Constants.k * args.sphere_radius_m),
            "material": "EPS, same particle as hologram_optimization_package (r_a = 5 mm)",
        },
        "focus_position_mm": [0.0, 0.0, 0.0],
        "grid": {
            "resolution": int(args.resolution),
            "lateral_extent_mm": [-float(args.lateral_mm), float(args.lateral_mm)],
            "axial_extent_mm": [-float(args.axial_mm), float(args.axial_mm)],
            "surface_theta_samples": int(SURFACE_THETA),
            "surface_phi_samples": int(SURFACE_PHI),
        },
        "pressure": {
            "origin_pa": float(np.abs(data["origin_pressure"])),
            "sphere_surface_min_pa": float(magnitude.min()),
            "sphere_surface_max_pa": float(magnitude.max()),
            "sphere_surface_mean_pa": float(magnitude.mean()),
            "sphere_surface_solid_angle_weighted_mean_pa": weighted_mean,
            "sphere_surface_max_at": {
                "theta_deg": float(np.rad2deg(data["theta"][theta_index])),
                "phi_deg": float(np.rad2deg(data["phi"][phi_index])),
            },
            "axial_lines": {
                label: {
                    "max_pa": float(np.abs(pressure).max()),
                    "max_z_mm": float(
                        data["axial_mm"][int(np.argmax(np.abs(pressure)))]
                    ),
                    "mean_pa": float(np.abs(pressure).mean()),
                }
                for label, pressure in data["axial_profiles"].items()
            },
        },
        "azimuthal_analysis": {
            name: {
                "radius_mm": ring["radius_mm"],
                "magnitude_min_pa": ring["magnitude_min_pa"],
                "magnitude_max_pa": ring["magnitude_max_pa"],
                "phase_winding_number": ring["winding_number"],
                "dominant_order": ring["azimuthal_spectrum"]["dominant_order"],
                "energy_fraction_m_plus_2": ring["azimuthal_spectrum"][
                    "energy_fraction"
                ]["2"],
                "energy_fraction_m_minus_2": ring["azimuthal_spectrum"][
                    "energy_fraction"
                ]["-2"],
                "chirality_contrast": ring["azimuthal_spectrum"]["chirality_contrast"],
                "spectrum": ring["azimuthal_spectrum"],
            }
            for name, ring in data["rings"].items()
        },
        "winding_number_caveat": (
            "phase_winding_number is only meaningful for a single-handedness field; "
            "for a standing azimuthal pattern read chirality_contrast instead "
            "(+1 = pure m=+2, 0 = equal +2/-2 standing pattern, -1 = pure m=-2)"
        ),
        "outputs": [str(path) for path in outputs],
        "hardware_opened": False,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase_npy",
        nargs="?",
        type=Path,
        default=Path("phase_acoustools_focus_vortex_m2_counter_rotating_complex64.npy"),
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--lateral-mm", type=float, default=DEFAULT_LATERAL_MM)
    parser.add_argument("--axial-mm", type=float, default=DEFAULT_AXIAL_MM)
    parser.add_argument("--resolution", type=int, default=DEFAULT_RESOLUTION)
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK)
    parser.add_argument("--sphere-radius-m", type=float, default=SPHERE_RADIUS_M)
    parser.add_argument("--title", type=str)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.resolution < 3:
        raise SystemExit("[ERROR] --resolution must be at least 3")
    if args.lateral_mm <= 0 or args.axial_mm <= 0:
        raise SystemExit("[ERROR] field extents must be positive")
    if args.sphere_radius_m <= 0:
        raise SystemExit("[ERROR] --sphere-radius-m must be positive")

    hologram, source_info = load_phase_map(args.phase_npy)
    output_dir = (
        args.output_dir
        if args.output_dir is not None
        else Path("visualizations") / "acoustools_vortex_charge2" / source_info.path.stem
    ).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    title = args.title or source_info.path.name
    data = compute_fields(hologram, args)
    radius_mm = float(args.sphere_radius_m * 1e3)

    element_path = output_dir / "element_phase_maps.png"
    planes_path = output_dir / "pressure_planes_with_sphere.png"
    surface_path = output_dir / "pressure_on_sphere_surface.png"
    sphere3d_path = output_dir / "sphere_3d_pressure.png"
    profiles_path = output_dir / "profiles_radial_axial.png"
    npz_path = output_dir / "field_data.npz"
    metadata_path = output_dir / "field_metadata.json"

    print("[PLOT] element phases", flush=True)
    plot_element_phases(
        element_path, hologram, title, load_helical_signature(source_info.path)
    )
    print("[PLOT] centre planes with sphere outline", flush=True)
    plot_planes(planes_path, data, radius_mm, title)
    print("[PLOT] sphere surface maps", flush=True)
    plot_sphere_surface(surface_path, data, radius_mm, title)
    print("[PLOT] sphere 3D render", flush=True)
    plot_sphere_3d(sphere3d_path, data, radius_mm, title)
    print("[PLOT] radial and axial profiles", flush=True)
    plot_profiles(profiles_path, data, radius_mm, title)

    ring_arrays = {}
    for name, ring in data["rings"].items():
        key = name.replace(".", "p")
        ring_arrays[f"ring_{key}_phi_rad"] = ring["phi_rad"]
        ring_arrays[f"ring_{key}_pressure"] = ring["pressure"]
    np.savez_compressed(
        npz_path,
        xy_pressure=data["planes"]["xy"],
        xz_pressure=data["planes"]["xz"],
        yz_pressure=data["planes"]["yz"],
        lateral_mm=data["axes_mm"]["xy"][0],
        axial_mm_axis=data["axes_mm"]["xz"][1],
        surface_pressure=data["surface"],
        surface_theta_rad=data["theta"],
        surface_phi_rad=data["phi"],
        radial_mm=data["radial_mm"],
        axial_mm=data["axial_mm"],
        **{f"axial_{name}": values for name, values in data["axial_profiles"].items()},
        **{f"radial_{name}": values for name, values in data["radial"].items()},
        **ring_arrays,
    )

    outputs = [
        element_path,
        planes_path,
        surface_path,
        sphere3d_path,
        profiles_path,
        npz_path,
    ]
    metadata = build_metadata(data, source_info, args, outputs)
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"[RESULT] |p| at origin: {metadata['pressure']['origin_pa']:.4f} Pa")
    print(
        "[RESULT] sphere surface |p|: "
        f"{metadata['pressure']['sphere_surface_min_pa']:.2f} .. "
        f"{metadata['pressure']['sphere_surface_max_pa']:.2f} Pa"
    )
    for ring in metadata["azimuthal_analysis"].values():
        print(
            f"[RESULT] ring r = {ring['radius_mm']:g} mm (z = 0): "
            f"dominant m = {ring['dominant_order']:+d}, "
            f"E(+2) = {ring['energy_fraction_m_plus_2']:.3f}, "
            f"E(-2) = {ring['energy_fraction_m_minus_2']:.3f}, "
            f"chirality contrast = {ring['chirality_contrast']:+.3f}"
        )
    print(f"[DONE] {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
