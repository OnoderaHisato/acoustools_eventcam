"""
Full Eq. (22) implementation: force-matching term PLUS the Lyapunov
stability term v_i*Re[lambda_i(q)]. stability_check.py showed the pure
force-matching solution (optimize.py / optimize_v2.py) is NOT a stable trap
(positive Jacobian eigenvalues = repulsive). This script fixes that by
penalizing the diagonal of the force Jacobian dF_i/dX_i (a very good proxy
for the eigenvalues here, since stability_check.py's Jacobians were
essentially diagonal -- off-diagonal terms ~1e-6 to 1e-8 vs ~0.8 on-diagonal)
directly inside the training loop via finite differences, differentiable
w.r.t. the transducer phases (each finite-difference term is itself a
differentiable force evaluation).

    min_q  wx(Fx-Fextx)^2 + wy(Fy-Fexty)^2 + wz(Fz-Fextz)^2
           + vx*Re[dFx/dX] + vy*Re[dFy/dY] + vz*Re[dFz/dZ]
"""
import json
import numpy as np
import torch
import sys
sys.path.insert(0, '.')
from arf_torch import ARFModel
from particle_parameters import PARTICLE_DENSITY_KG_M3

from acoustools.Utilities import TRANSDUCERS, create_points, add_lev_sig, DTYPE, device
from acoustools.Solvers import wgs
import acoustools.Constants as Constants

torch.manual_seed(0)
np.random.seed(0)

k = Constants.k
p_ref = Constants.P_ref
transducer_radius = Constants.radius
rho_0 = Constants.p_0
c_0 = Constants.c_0
c_p = Constants.c_p

r_a = 0.005
rho_p = PARTICLE_DENSITY_KG_M3
g = 9.81
V = 4 / 3 * np.pi * r_a ** 3
mass = rho_p * V
weight_force = mass * g
ka = k * r_a

transducer_pos = TRANSDUCERS.detach().cpu().numpy().real
n_max = 12  # reduced from 14 for speed (7x model evals/iter); already converged by n_max=10
delta = 0.0003  # 0.3mm finite-difference step (~lambda/30)

print("Building 7 ARF models (center +/- delta along x,y,z) ...")
centers = {
    "0": np.array([0., 0., 0.]),
    "+x": np.array([delta, 0., 0.]), "-x": np.array([-delta, 0., 0.]),
    "+y": np.array([0., delta, 0.]), "-y": np.array([0., -delta, 0.]),
    "+z": np.array([0., 0., delta]), "-z": np.array([0., 0., -delta]),
}
models = {k_: ARFModel(transducer_pos, c, n_max, k, transducer_radius, p_ref,
                        rho_0, c_0, rho_p, c_p, r_a, device=device)
          for k_, c in centers.items()}
print("Done.")

Fext = torch.tensor([0.0, 0.0, weight_force], dtype=torch.float32, device=device)

p0 = create_points(1, 1, x=0.0, y=0.0, z=0.0)
x_focus = wgs(p0)
x_trap = add_lev_sig(x_focus.clone()).squeeze().detach()
theta_init = torch.angle(x_trap).to(torch.float32)


def forces_and_jacobian_diag(x):
    Fx0, Fy0, Fz0 = models["0"].force(x)
    Fx_px, Fy_px, Fz_px = models["+x"].force(x)
    Fx_mx, Fy_mx, Fz_mx = models["-x"].force(x)
    Fx_py, Fy_py, Fz_py = models["+y"].force(x)
    Fx_my, Fy_my, Fz_my = models["-y"].force(x)
    Fx_pz, Fy_pz, Fz_pz = models["+z"].force(x)
    Fx_mz, Fy_mz, Fz_mz = models["-z"].force(x)

    dFxdX = (Fx_px - Fx_mx) / (2 * delta)
    dFydY = (Fy_py - Fy_my) / (2 * delta)
    dFzdZ = (Fz_pz - Fz_mz) / (2 * delta)
    return (Fx0, Fy0, Fz0), (dFxdX, dFydY, dFzdZ)


def run_optimization(weights, v_stab, iters=500, lr=0.03, label=""):
    wx, wy, wz = weights
    vx, vy, vz = v_stab
    scale2 = weight_force ** 2
    jac_scale = (weight_force / 0.01) ** 1  # N/m natural scale (~force/1cm), for normalizing stability term

    theta = theta_init.clone().requires_grad_(True)
    opt = torch.optim.Adam([theta], lr=lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=iters, eta_min=lr * 1e-3)

    history = {"loss": [], "Fx": [], "Fy": [], "Fz": [], "Jxx": [], "Jyy": [], "Jzz": []}
    best = {"loss": float("inf")}
    for it in range(iters):
        opt.zero_grad()
        x = torch.exp(1j * theta.to(torch.complex64))
        (Fx, Fy, Fz), (Jxx, Jyy, Jzz) = forces_and_jacobian_diag(x)

        force_loss = (wx * (Fx - Fext[0]) ** 2 + wy * (Fy - Fext[1]) ** 2
                      + wz * (Fz - Fext[2]) ** 2) / scale2
        stab_loss = (vx * Jxx + vy * Jyy + vz * Jzz) / jac_scale
        loss = force_loss + stab_loss
        loss.backward()
        opt.step()
        sched.step()

        loss_val = loss.item()
        history["loss"].append(loss_val)
        history["Fx"].append(Fx.item())
        history["Fy"].append(Fy.item())
        history["Fz"].append(Fz.item())
        history["Jxx"].append(Jxx.item())
        history["Jyy"].append(Jyy.item())
        history["Jzz"].append(Jzz.item())

        # track best by FORCE match quality among iterates with all-negative diag (stable-ish)
        stable_ok = (Jxx.item() < 0) and (Jyy.item() < 0) and (Jzz.item() < 0)
        score = force_loss.item() if stable_ok else force_loss.item() + 10.0
        if score < best["loss"]:
            best = {"loss": score, "theta": theta.detach().clone(),
                     "Fx": Fx.item(), "Fy": Fy.item(), "Fz": Fz.item(),
                     "Jxx": Jxx.item(), "Jyy": Jyy.item(), "Jzz": Jzz.item(),
                     "it": it, "stable": stable_ok}
        if it % 50 == 0 or it == iters - 1:
            print(f"[{label}] it {it:4d} loss={loss_val:.4e} force_loss={force_loss.item():.4e} "
                  f"Fz={Fz.item():.4e} Jxx={Jxx.item():.4f} Jyy={Jyy.item():.4f} Jzz={Jzz.item():.4f}")

    print(f"[{label}] BEST(stable-preferring) it={best['it']} stable={best['stable']} "
          f"Fx={best['Fx']:.4e} Fy={best['Fy']:.4e} Fz={best['Fz']:.4e} "
          f"Jxx={best['Jxx']:.4f} Jyy={best['Jyy']:.4f} Jzz={best['Jzz']:.4f}")
    x_best = torch.exp(1j * best["theta"].to(torch.complex64))
    return best["theta"], x_best, history, best


results = {}
# v_stab weights chosen after a quick scan: uniform penalty on all 3 axes
v_stab = (1.0, 1.0, 1.0)
for weights in [(0.0, 0.0, 1.0), (1.0, 1.0, 1.0)]:
    label = f"w={weights}"
    print(f"\n=== Full Eq.22 (force + stability) for weight vector {weights}, v_stab={v_stab} ===")
    theta_f, x_f, hist, best = run_optimization(weights, v_stab, label=label)
    results[label] = {
        "weights": weights, "v_stab": v_stab,
        "x_real": x_f.real.numpy().tolist(), "x_imag": x_f.imag.numpy().tolist(),
        "history": hist,
        "final_force": [best["Fx"], best["Fy"], best["Fz"]],
        "final_jacobian_diag": [best["Jxx"], best["Jyy"], best["Jzz"]],
        "stable": best["stable"], "best_iter": best["it"],
    }

with open("optimize_results_v3_stable.json", "w") as f:
    json.dump({"meta": {"r_a": r_a, "rho_p": rho_p, "c_p": c_p, "ka": ka, "n_max": n_max,
                         "weight_force": weight_force, "delta": delta},
                "results": results}, f, indent=1)
print("\nSaved optimize_results_v3_stable.json")
