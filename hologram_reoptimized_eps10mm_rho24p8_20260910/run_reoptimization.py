"""Hardware-free v3-style reoptimization; writes only to a NEW output folder."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.dont_write_bytecode = True
sys.path[:0] = [str(HERE / "dependencies"), str(HERE / "physics"), str(PROJECT)]

import numpy as np
import scipy
import torch
from acoustools.Utilities import TRANSDUCERS
from acoustools_send_phase_npy import load_phase_map
from arf_sh import check_sph_harm_convention, force_from_Snm, scattering_coefficient
from arf_torch import ARFModel

LABEL = "w=(1.0, 1.0, 1.0)"
NPY_NAME = "phase_acoustools_optimized_eps10mm_rho24p8_v3_xyz_complex64.npy"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save_json(path, data):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def source_audit():
    records = []
    for snapshot in sorted((HERE / "source_snapshot").iterdir()):
        source = (PROJECT / snapshot.name if snapshot.name.startswith("phase_acoustools_")
                  else PROJECT / "hologram_optimization_package" / snapshot.name)
        record = {"source": str(source), "snapshot": str(snapshot),
                  "sha256": sha256(snapshot), "source_sha256": sha256(source)}
        if record["sha256"] != record["source_sha256"]:
            raise RuntimeError(f"Source/snapshot changed: {source}")
        records.append(record)
    for name in ("arf_sh.py", "arf_torch.py"):
        if sha256(HERE / "physics" / name) != sha256(HERE / "source_snapshot" / name):
            raise RuntimeError(f"Physics copy changed: {name}")
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=HERE / "results")
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--learning-rate", type=float, default=0.03)
    args = parser.parse_args()
    if args.iterations < 1 or args.learning_rate <= 0:
        parser.error("iterations and learning rate must be positive")
    output = args.output_dir.resolve()
    if not output.is_relative_to(HERE) or output == HERE:
        parser.error("output directory must be a new child of this run folder")
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()

    def log(message):
        print(message, flush=True)
        with (output / "run.log").open("a", encoding="utf-8") as stream:
            stream.write(message + "\n")

    sources_before = source_audit()
    torch.set_num_threads(2)
    torch.manual_seed(0)
    np.random.seed(0)
    check_sph_harm_convention()
    snapshot = HERE / "source_snapshot"
    source = json.loads((snapshot / "optimize_results_v3_stable.json").read_text())
    field_meta = json.loads((snapshot / "pressure_field.json").read_text())["meta"]
    # Keep the historical wavelength; never change installed AcousTools globals.
    k = 2 * math.pi / field_meta["wavelength"]
    c_0 = field_meta["wavelength"] * 40000.0
    rho_p, rho_0, c_p, radius, gravity = 24.8, 1.2, 1052.0, 0.005, 9.81
    p_ref, transducer_radius = 3.4, 0.0045
    mass = rho_p * 4 / 3 * math.pi * radius ** 3
    weight = mass * gravity
    delta, n_max = 0.0003, 12
    board = TRANSDUCERS.detach().cpu().numpy().real.copy()
    assert board.shape == (512, 3)
    assert np.all(board[:256, 2] > 0) and np.all(board[256:, 2] < 0)
    np.testing.assert_allclose(np.abs(board[:, 2]), field_meta["board_z"], atol=1e-7, rtol=0)
    np.save(output / "transducer_positions_acoustools.npy", board)
    old = source["results"][LABEL]
    old_x = (np.asarray(old["x_real"]) + 1j*np.asarray(old["x_imag"])).astype(np.complex64)
    np.testing.assert_array_equal(
        old_x, np.load(snapshot / "phase_acoustools_optimized_eps10mm_v3_xyz_complex64.npy",
                       allow_pickle=False).reshape(-1))

    def model(center, order=n_max, density=rho_p, dtype=torch.complex64):
        return ARFModel(board, np.asarray(center), order, k, transducer_radius,
                        p_ref, rho_0, c_0, density, c_p, radius, dtype=dtype)

    def build_models(step=delta, order=n_max, dtype=torch.complex64):
        offsets = np.eye(3) * step
        return [model(np.zeros(3), order, dtype=dtype)] + [
            model(sign * offsets[axis], order, dtype=dtype)
            for axis in range(3) for sign in (1, -1)]

    def evaluate(x, models, step=delta):
        forces = torch.stack([torch.stack(m.force(x)) for m in models])
        jacobian = torch.stack([(forces[1+2*j] - forces[2+2*j]) / (2*step)
                                for j in range(3)], dim=1)
        return forces[0], jacobian

    reference_profiles = json.loads((snapshot / "force_profiles.json").read_text())["profiles"][LABEL]
    origin_index = int(np.argmin(np.abs(reference_profiles["offsets"])))
    reference_force = np.asarray([reference_profiles["F_vs_z"][a][origin_index]
                                  for a in ("Fx", "Fy", "Fz")])
    with torch.no_grad():
        replay_force = np.asarray([v.item() for v in model([0, 0, 0], density=40.0).force(
            torch.from_numpy(old_x))])
    replay_error = float(np.linalg.norm(replay_force-reference_force) / np.linalg.norm(reference_force))
    if replay_error > 1e-3:
        raise RuntimeError(f"Historical force replay mismatch: {replay_error}")
    log(f"Historical 40 kg/m3 force replay relative error = {replay_error:.6g}")
    log(f"New density={rho_p} kg/m3; mass={mass*1e6:.8f} mg; target Fz={weight*1e6:.8f} uN")
    log("Building seven n_max=12 models; warm start = original saved v3 XYZ phases")
    models = build_models()
    target = torch.tensor([0.0, 0.0, weight], dtype=torch.float32)
    theta = torch.angle(torch.from_numpy(old_x)).clone().requires_grad_(True)
    optimizer = torch.optim.Adam([theta], lr=args.learning_rate)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=args.iterations, eta_min=args.learning_rate*1e-3)
    history, best = [], None
    for iteration in range(args.iterations + 1):
        optimizer.zero_grad()
        x = torch.exp(1j * theta.to(torch.complex64))
        force, jacobian = evaluate(x, models)
        diag = torch.diagonal(jacobian)
        force_loss = torch.sum((force-target)**2) / weight**2
        stability_loss = torch.sum(diag) / (weight/0.01)
        loss = force_loss + stability_loss
        f = force.detach().numpy().copy()
        j = jacobian.detach().numpy().copy()
        eig = np.linalg.eigvals(j)
        restoring = bool(np.all(eig.real < 0))
        score = float(force_loss.detach())
        record = {"iteration": iteration, "loss": float(loss.detach()),
                  "force_loss": score, "force_N": f.tolist(),
                  "jacobian_N_per_m": j.tolist(), "restoring": restoring}
        history.append(record)
        if not np.isfinite(record["loss"]):
            raise RuntimeError("Non-finite optimization loss")
        # Save the exact activation that produced this force, BEFORE opt.step().
        if restoring and (best is None or score < best["force_loss"]):
            best = {**record, "activation": x.detach().numpy().copy()}
        if iteration % 25 == 0 or iteration == args.iterations:
            log(f"it={iteration:4d}: Fz={f[2]*1e6:.6f} uN, "
                f"force_error={math.sqrt(score)*100:.5f}%, "
                f"Jdiag={np.diag(j)}, restoring={restoring}")
        if iteration < args.iterations:
            loss.backward()
            optimizer.step()
            scheduler.step()
    if best is None:
        save_json(output / "failed_history.json", history)
        raise RuntimeError("No restoring-force candidate found; no NPY exported")

    saved = best.pop("activation").astype(np.complex64).reshape(1, 512, 1)
    npy_path = output / NPY_NAME
    with npy_path.open("xb") as stream:
        np.save(stream, saved, allow_pickle=False)
    loaded, info = load_phase_map(npy_path)
    np.testing.assert_array_equal(saved, loaded.numpy())
    with torch.no_grad():
        replay_f, replay_j = evaluate(loaded.reshape(-1), models)
    np.testing.assert_allclose(replay_f.numpy(), best["force_N"], rtol=0, atol=1e-12)
    np.testing.assert_allclose(replay_j.numpy(), best["jacobian_N_per_m"], rtol=0, atol=1e-10)
    log(f"Saved/reloaded NPY at iteration {best['iteration']}; evaluating full Jacobian and convergence")
    validations = []
    for order, step in ((10, delta), (12, delta), (16, delta), (16, delta/2)):
        precise_models = build_models(step, order, torch.complex128)
        with torch.no_grad():
            f, j = evaluate(loaded.reshape(-1), precise_models, step)
        f, j = f.numpy(), j.numpy()
        eig = np.linalg.eigvals(j)
        item = {"n_max": order, "delta_m": step, "dtype": "complex128",
                "force_N": f.tolist(), "jacobian_N_per_m": j.tolist(),
                "jacobian_eigenvalues_real": eig.real.tolist(),
                "jacobian_eigenvalues_imag": eig.imag.tolist(),
                "net_force_N": (f-np.array([0, 0, weight])).tolist(),
                "force_error_relative_to_mg": float(np.linalg.norm(f-[0, 0, weight])/weight),
                "restoring": bool(np.all(eig.real < 0))}
        validations.append(item)
        log(f"validate n={order}, delta={step*1e3:.2f} mm: "
            f"Fz={f[2]*1e6:.6f} uN, eig={eig}")
    # Independent NumPy summation check of the copied PyTorch force code.
    center_model = precise_models[0]
    activation128 = loaded.numpy().reshape(-1).astype(np.complex128)
    numpy_force = np.asarray(force_from_Snm(
        activation128 @ center_model.M.numpy(), center_model.nm,
        lambda n: scattering_coefficient(n, k, radius, rho_0, c_0, rho_p, c_p),
        16, k, rho_0, c_0))
    numpy_error = float(np.linalg.norm(numpy_force-np.asarray(validations[-1]["force_N"]))/weight)
    if numpy_error > 1e-8:
        raise RuntimeError("NumPy and PyTorch force summations disagree")
    convergence = float(np.linalg.norm(np.asarray(validations[0]["force_N"])-
                                       np.asarray(validations[2]["force_N"]))/weight)
    passed = (all(v["restoring"] and v["force_error_relative_to_mg"] < 0.01
                  for v in validations) and convergence < 1e-3)
    if source_audit() != sources_before:
        raise RuntimeError("Source files changed during execution")
    meta = {"rho_p": rho_p, "r_a": radius, "mass_kg": mass, "weight_force": weight,
            "g": gravity, "rho_0": rho_0, "c_0": c_0, "c_p": c_p,
            "k": k, "frequency_hz": 40000, "p_ref": p_ref,
            "transducer_radius": transducer_radius, "n_max": n_max, "delta": delta,
            "force_weights": [1, 1, 1], "stability_weights": [1, 1, 1],
            "initialization": "saved 40 kg/m3 v3 XYZ activation",
            "method": "original v3 force + diagonal-Jacobian objective; full-J candidate check",
            "iterations": args.iterations, "learning_rate": args.learning_rate,
            "source_reference_force_replay_relative_error": replay_error,
            "source_conditions_note": "343 m/s inferred from saved wavelength; rho0=1.2, "
                "P_ref=3.4 and transducer radius=4.5mm reproduce old force/pressure, "
                "but were not all recorded in original result metadata",
            "numpy_version": np.__version__, "scipy_version": scipy.__version__,
            "torch_version": torch.__version__, "python_version": sys.version,
            "script_sha256": sha256(__file__), "source_files": sources_before,
            "elapsed_seconds": time.monotonic()-started, "hardware_opened": False,
            "original_files_unchanged": True}
    result = {"meta": meta, "results": {LABEL: {
        **best, "x_real": saved.real.reshape(-1).tolist(),
        "x_imag": saved.imag.reshape(-1).tolist(), "history": history}},
        "validation": {"checks": validations, "numpy_force_relative_error": numpy_error,
                       "n10_vs_n16_force_difference_relative_to_mg": convergence,
                       "passed_force_1percent_and_restoring": passed,
                       "saved_activation_force_pair_replayed": True,
                       "physical_levitation_validated": False}}
    save_json(output / "optimization_result.json", result)
    save_json(output / (npy_path.stem + "_metadata.json"), {
        "source_result": "optimization_result.json", "source_result_sha256": sha256(output / "optimization_result.json"),
        "npy": npy_path.name, "sha256": info.sha256,
        "shape": list(info.shape), "dtype": info.dtype,
        "amplitude_min": info.amplitude_min, "amplitude_max": info.amplitude_max,
        "ordering": "AcousTools: 0..255 top, 256..511 bottom; sender permutes for OpenMPD",
        "extra_signature_applied": False, "amplitude_normalized": False,
        "rho_p": rho_p, "c_0": c_0, "optimization_rerun": True,
        "force_revalidated": True, "hardware_opened": False,
        "validation_passed": passed,
        "note": "Model candidate, not experimentally validated. Phase-only, no voltage setting."})
    log(f"DONE: validation_passed={passed}; NPY={npy_path}; SHA256={info.sha256}")
    if not passed:
        raise SystemExit("Candidate exported for diagnosis but fails the specified numerical acceptance checks")


if __name__ == "__main__":
    main()
