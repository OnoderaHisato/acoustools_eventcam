"""Compare an exported NPY with the package's saved complex XZ fields; no hardware."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import torch

import acoustools.Constants as constants
from acoustools.Utilities import DTYPE, TRANSDUCERS, device, propagate
from acoustools_send_phase_npy import load_phase_map


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def simulate(hologram: torch.Tensor, xyz: np.ndarray, k: float) -> np.ndarray:
    values = []
    with torch.inference_mode():
        for start in range(0, xyz.shape[1], 4096):
            points = torch.tensor(
                xyz[:, start:start + 4096][None, :, :], dtype=DTYPE, device=device
            )
            pressure = propagate(
                hologram, points, board=TRANSDUCERS, k=k,
                p_ref=constants.P_ref, transducer_radius=constants.radius,
            )
            values.append(pressure.detach().cpu().numpy().reshape(-1))
    return np.concatenate(values)


def metrics(calculated: np.ndarray, reference: np.ndarray) -> dict:
    difference = calculated - reference
    mag_difference = np.abs(calculated) - np.abs(reference)
    norm = float(np.linalg.norm(reference))
    peak = float(np.abs(reference).max())
    if norm == 0 or peak == 0:
        raise ValueError("Saved pressure field has no nonzero reference values")
    return {
        "complex_relative_l2": float(np.linalg.norm(difference) / norm),
        "magnitude_relative_l2": float(np.linalg.norm(mag_difference) / norm),
        "max_complex_error_pa": float(np.abs(difference).max()),
        "max_complex_error_relative_to_reference_peak": float(np.abs(difference).max() / peak),
        "reference_peak_pa": peak,
        "calculated_peak_pa": float(np.abs(calculated).max()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase_npy", type=Path)
    parser.add_argument("--package-dir", type=Path, default=Path("hologram_optimization_package"))
    parser.add_argument("--label", default="w=(1.0, 1.0, 1.0)")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    solution_path = args.package_dir / "optimize_results_v3_stable.json"
    field_path = args.package_dir / "pressure_field.json"
    sources = [args.phase_npy.resolve(), solution_path.resolve(), field_path.resolve()]
    hashes = {str(path): sha256(path) for path in sources}
    solution = json.loads(solution_path.read_text(encoding="utf-8"))
    field_data = json.loads(field_path.read_text(encoding="utf-8"))
    selected = solution["results"][args.label]
    expected = (np.array(selected["x_real"]) + 1j * np.array(selected["x_imag"])).astype(np.complex64)
    hologram, info = load_phase_map(args.phase_npy)
    exported = hologram.numpy().reshape(-1)
    np.testing.assert_array_equal(exported, expected)
    board_z = TRANSDUCERS.detach().cpu().numpy()[:, 2]
    if not np.allclose(np.abs(board_z), field_data["meta"]["board_z"], rtol=0, atol=1e-7):
        raise ValueError("Saved and current board separation differ")
    hologram = hologram.to(device)
    saved_wavelength = float(field_data["meta"]["wavelength"])
    saved_k = float(2 * np.pi / saved_wavelength)
    saved_c = saved_wavelength * constants.f
    cases = {"current_defaults": float(constants.k), "saved_wavelength": saved_k}
    summary = {
        "input_npy": str(info.path), "source_sha256": hashes,
        "selected_result": args.label,
        "activation": {
            "exact_complex64_match": True, "shape": list(hologram.shape),
            "top_max_abs_complex_difference": float(np.abs(exported[:256] - expected[:256]).max()),
            "bottom_max_abs_complex_difference": float(np.abs(exported[256:] - expected[256:]).max()),
            "ordering": "AcousTools; top 0..255, bottom 256..511",
        },
        "simulation": {
            "function": "acoustools.Utilities.propagate",
            "current_sound_speed_m_per_s": float(constants.c_0),
            "saved_sound_speed_inferred_from_wavelength_and_40khz": saved_c,
            "wavenumber_rad_per_m": cases,
            "p_ref_both_cases": float(constants.P_ref),
            "transducer_radius_m_both_cases": float(constants.radius),
            "board_z_m": [float(board_z[0]), float(board_z[256])],
            "fitted_gain_or_phase": False,
            "note": "Only k changes in saved_wavelength replay. Source P_ref/radius are not recorded; the current values are retained without fitting.",
        },
        "fields": {}, "hardware_opened": False, "force_validated": False,
    }
    args.output_dir.mkdir(parents=True, exist_ok=False)
    arrays = {}
    fig, axes = plt.subplots(2, 3, figsize=(14, 8), layout="constrained")
    for row, name in enumerate(("wide", "zoom")):
        saved = field_data["results"][args.label][name]
        xs, zs = np.array(saved["xs"]), np.array(saved["zs"])
        xx, zz = np.meshgrid(xs, zs, indexing="ij")
        xyz = np.stack([xx.ravel(), np.zeros(xx.size), zz.ravel()])
        reference = np.array(saved["P_real"]) + 1j * np.array(saved["P_imag"])
        if reference.shape != xx.shape or not np.isfinite(reference).all():
            raise ValueError(f"Invalid reference grid: {name}")
        fields = {"package_saved": reference}
        stats = {}
        for case, k in cases.items():
            print(f"[COMPARE] {name}: {case}", flush=True)
            fields[case] = simulate(hologram, xyz, k).reshape(xx.shape)
            stats[case] = metrics(fields[case], reference)
        summary["fields"][name] = {"grid_shape_x_z": list(xx.shape), **stats}
        arrays[name + "_xs_m"], arrays[name + "_zs_m"] = xs, zs
        common_max = max(float(np.abs(p).max()) for p in fields.values())
        for col, (case, pressure) in enumerate(fields.items()):
            arrays[name + "_" + case] = pressure
            ax = axes[row, col]
            mesh = ax.pcolormesh(zs * 1000, xs * 1000, np.abs(pressure),
                                 shading="auto", cmap="inferno", vmin=0, vmax=common_max)
            if case == "package_saved":
                title = "Package saved field"
            else:
                c = constants.c_0 if case == "current_defaults" else saved_c
                error = stats[case]["magnitude_relative_l2"] * 100
                title = f"NPY, c = {c:.0f} m/s\nMagnitude relative L2 error = {error:.5g}%"
            ax.set_title(title, fontsize=10)
            ax.set_xlabel("z (mm), board-to-board axis")
            ax.set_ylabel("x (mm)")
            ax.set_aspect("equal")
            if name == "zoom":
                ax.add_patch(plt.Circle((0, 0), field_data["meta"]["r_a"] * 1000,
                                       fill=False, edgecolor="cyan", linestyle="--"))
                ax.plot(0, 0, "c+", markersize=6)
        fig.colorbar(mesh, ax=list(axes[row]), label="Pressure magnitude (Pa)", shrink=0.8)
    fig.suptitle("Saved optimization package vs exported NPY\n"
                 "Identical XZ grids and row color scales; no gain/phase alignment", fontsize=13)
    fig.savefig(args.output_dir / "package_vs_npy_pressure_xz.png", dpi=180)
    plt.close(fig)
    np.savez_compressed(args.output_dir / "package_vs_npy_pressure_xz.npz", **arrays)
    summary["saved_wavelength_replay_passed_relative_l2_1e_minus4"] = all(
        item["saved_wavelength"]["complex_relative_l2"] < 1e-4
        for item in summary["fields"].values()
    )
    for path in sources:
        if sha256(path) != hashes[str(path)]:
            raise RuntimeError(f"Source changed during comparison: {path}")
    (args.output_dir / "comparison_metadata.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
