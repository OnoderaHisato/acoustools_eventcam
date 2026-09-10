"""Compare existing visualization outputs and model forces without hardware I/O."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PHYSICS_RUN = ROOT / "hologram_reoptimized_eps10mm_rho24p8_20260910"
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT), str(PHYSICS_RUN / "physics"), str(PHYSICS_RUN / "dependencies")]

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle
import numpy as np
import torch
import acoustools.Constants as constants
from acoustools.Utilities import TRANSDUCERS
from acoustools_send_phase_npy import load_phase_map
from arf_torch import ARFModel

NAMES = ("phase_13mg_5mmradius_w001", "A_previous_10mm_18V",
         "phase_13mg_5mmradius_w001_boards_swapped")
LABELS = ("13mg w001 (original order)", "A previous 10mm 18V", "13mg w001 (boards swapped)")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    report_path = HERE / "comparison.json"
    if report_path.exists():
        raise FileExistsError("Comparison already exists; preserve the previous outputs")
    source_hashes = {name: sha(ROOT / (name + ".npy")) for name in NAMES}
    holograms, fields, metadata = [], [], []
    for name in NAMES:
        h, info = load_phase_map(ROOT / (name + ".npy"))
        raw = np.load(ROOT / (name + ".npy"), allow_pickle=False)
        expected = (raw if np.iscomplexobj(raw) else np.exp(1j*raw).astype(np.complex64)).reshape(1, 512, 1)
        np.testing.assert_array_equal(h.numpy(), expected)
        meta = asdict(info)
        meta["path"] = str(info.path)
        if not np.iscomplexobj(raw):
            meta["max_circular_phase_roundtrip_error_rad"] = float(np.max(np.abs(np.angle(
                np.exp(1j*(np.angle(h.numpy().reshape(-1)).astype(np.float64)-raw.reshape(-1)))))))
        meta["active_top_elements"] = int(np.count_nonzero(np.abs(h.numpy().reshape(-1)[:256]) > 0))
        meta["active_bottom_elements"] = int(np.count_nonzero(np.abs(h.numpy().reshape(-1)[256:]) > 0))
        metadata.append(meta)
        holograms.append(h)
        with np.load(HERE / name / "acoustic_field_slices.npz", allow_pickle=False) as data:
            fields.append({key: data[key].copy() for key in data.files})
    expected_swap = torch.cat((holograms[0][:, 256:, :], holograms[0][:, :256, :]), dim=1)
    np.testing.assert_array_equal(holograms[2].numpy(), expected_swap.numpy())
    board = TRANSDUCERS.detach().cpu().numpy().real
    np.testing.assert_array_equal(board[:256, :2], board[256:, :2])
    for f in fields[1:]:
        np.testing.assert_array_equal(f["lateral_mm"], fields[0]["lateral_mm"])
        np.testing.assert_array_equal(f["axial_mm"], fields[0]["axial_mm"])

    vmax = float(np.percentile(np.concatenate([
        np.abs(f[p+"_pressure"]).ravel() for f in fields for p in ("xy", "xz", "yz")]), 99.5))
    fig, axes = plt.subplots(3, 3, figsize=(13, 12), constrained_layout=True)
    for row, (name, f) in enumerate(zip(LABELS, fields)):
        for col, plane in enumerate(("xy", "xz", "yz")):
            ax = axes[row, col]
            x = f["lateral_mm"]
            y = f["lateral_mm" if plane == "xy" else "axial_mm"]
            im = ax.imshow(np.abs(f[plane+"_pressure"]), origin="lower",
                           extent=[x[0], x[-1], y[0], y[-1]], vmin=0, vmax=vmax,
                           cmap="inferno", interpolation="nearest", aspect="equal")
            ax.add_patch(Circle((0, 0), 5, fill=False, ec="cyan", lw=1.1))
            ax.plot(0, 0, "+", color="cyan", ms=6)
            ax.set_title(name + "\n" + plane.upper(), fontsize=10)
            ax.set_xlabel(plane[0] + " (mm)")
            ax.set_ylabel(plane[1] + " (mm)")
    fig.colorbar(im, ax=axes, label="|p| (Pa), shared scale; clipped at pooled 99.5 percentile", shrink=.8)
    fig.suptitle("Same AcousTools incident-field model, c=346 m/s; cyan circle: 10mm sphere", fontsize=13)
    fig.savefig(HERE / "pressure_comparison.png", dpi=170)
    plt.close(fig)

    fig, axes = plt.subplots(3, 2, figsize=(9, 12), constrained_layout=True)
    for row, (name, f) in enumerate(zip(LABELS, fields)):
        for col, board_name in enumerate(("top", "bottom")):
            ax = axes[row, col]
            im = ax.imshow(f[board_name+"_phase_rad"], origin="lower", cmap="twilight",
                           vmin=-np.pi, vmax=np.pi, interpolation="nearest")
            ax.set_title(name + "\n" + board_name + (" (0-255)" if col == 0 else " (256-511)"), fontsize=10)
            ax.set_xlabel("Element x index")
            ax.set_ylabel("Element y index")
    fig.colorbar(im, ax=axes, label="Phase (rad)", shrink=.8)
    fig.savefig(HERE / "element_phase_comparison.png", dpi=170)
    plt.close(fig)

    mirror_errors = {}
    for plane in ("xy", "xz", "yz"):
        original = fields[0][plane+"_pressure"]
        predicted = original if plane == "xy" else original[::-1, :]
        swapped = fields[2][plane+"_pressure"]
        mirror_errors[plane] = {
            "complex_relative_l2": float(np.linalg.norm(swapped-predicted)/np.linalg.norm(predicted)),
            "max_complex_difference_Pa": float(np.max(np.abs(swapped-predicted)))}

    torch.set_num_threads(2)
    radius, rho_p, rho_0, c_p = .005, 24.8, 1.2, 1052.0
    mass = 4/3*np.pi*radius**3*rho_p
    weight = mass*9.81
    checks = []
    # Both sound speeds are explicit; the source optimizer's medium is unknown.
    for c0, order, step in ((346., 12, .0003), (346., 16, .0003),
                            (346., 16, .00015), (343., 16, .0003)):
        k = 2*np.pi*40000/c0
        centers = [np.zeros(3)] + [sign*step*np.eye(3)[axis]
                                    for axis in range(3) for sign in (1, -1)]
        models = [ARFModel(board, center, order, k, .0045, 3.4,
                           rho_0, c0, rho_p, c_p, radius, dtype=torch.complex128)
                  for center in centers]
        for name, h in zip(NAMES, holograms):
            with torch.no_grad():
                forces = np.asarray([[v.item() for v in m.force(h.reshape(-1))] for m in models])
            j = np.column_stack([(forces[1+2*a]-forces[2+2*a])/(2*step) for a in range(3)])
            eig = np.linalg.eigvals(j)
            item = {"name": name, "c_0": c0, "n_max": order, "delta_m": step,
                    "force_N": forces[0].tolist(), "net_Fz_N": float(forces[0, 2]-weight),
                    "Fz_over_mg": float(forces[0, 2]/weight),
                    "jacobian_N_per_m": j.tolist(), "eigenvalues_real": eig.real.tolist(),
                    "eigenvalues_imag": eig.imag.tolist(),
                    "all_eigenvalues_negative_real": bool(np.all(eig.real < 0))}
            checks.append(item)
            print(f"c={c0}, n={order}, delta={step*1e3:.2f}mm, {name}: "
                  f"F[uN]={forces[0]*1e6}, Fz/mg={item['Fz_over_mg']:.4f}, eig={eig}", flush=True)
    assert source_hashes == {name: sha(ROOT / (name + ".npy")) for name in NAMES}
    report = {"inputs": metadata, "swap_exact": True,
              "board_coordinates_xy_match_between_boards": True,
              "swapped_field_matches_z_mirror": mirror_errors,
              "source_generation_order_confirmed": False,
              "simulation": {"c_0": float(constants.c_0), "k": float(constants.k),
                             "p_ref": float(constants.P_ref), "transducer_radius": float(constants.radius),
                             "resolution": 241, "extent_mm": [-15, 15]},
              "force_model": {"implementation": str(PHYSICS_RUN / "physics" / "arf_torch.py"),
                              "rho_p": rho_p, "r_a": radius, "mass_kg": mass, "weight_N": weight,
                              "rho_0": rho_0, "c_p_assumed": c_p, "p_ref": 3.4,
                              "transducer_radius": .0045,
                              "note": "SH/Mie fluid-sphere approximation; directivity evaluated at sphere center. "
                                      "No experimental pressure/material calibration; no physical levitation proof."},
              "force_checks": checks, "source_files_unchanged": True, "hardware_opened": False}
    with report_path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    print("DONE:", HERE, flush=True)


if __name__ == "__main__":
    main()
