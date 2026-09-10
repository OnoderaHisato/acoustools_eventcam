"""
Hologram optimization for levitating a 1cm-diameter (r_a=5mm), rho_p=24.8 kg/m^3
sphere in mid-air between acoustools' default two opposing 16x16 (256+256=512
element) 40kHz arrays -- using Inoue et al. 2019 Eq. (22)'s force-matching
objective, with F(q) computed via the spherical-harmonic / Mie partial-wave
method (validated in validate_gorkov.py), NOT the paper's boundary-element
method.

Since a sphere carries no controllable torque (Inoue et al., Sec. III A: "T is
always zero and therefore uncontrollable for a true sphere in this
formulation"), Eq. (22) reduces to just the force term over (Fx,Fy,Fz):

    min_q E(q) = wx*(Fx(q)-Fext_x)^2 + wy*(Fy(q)-Fext_y)^2 + wz*(Fz(q)-Fext_z)^2

Fext = (0, 0, m*g): the acoustic force must balance gravity.
Runs for weight vectors (wx,wy,wz) = (0,0,1) and (1,1,1) as requested.
"""
import json
import numpy as np
import torch

import sys
sys.path.insert(0, '.')
from arf_torch import ARFModel
from particle_parameters import PARTICLE_DENSITY_KG_M3

from acoustools.Utilities import TRANSDUCERS, create_points, propagate, DTYPE, device
from acoustools.Solvers import wgs
import acoustools.Constants as Constants

torch.manual_seed(0)
np.random.seed(0)

# ---------------------------------------------------------------------------
# Physical setup
# ---------------------------------------------------------------------------
k = Constants.k
p_ref = Constants.P_ref
transducer_radius = Constants.radius
rho_0 = Constants.p_0
c_0 = Constants.c_0
c_p = Constants.c_p  # 1052 m/s, EPS-like assumption (density overridden below per user spec)

r_a = 0.005          # 1cm DIAMETER sphere -> 5mm radius
rho_p = PARTICLE_DENSITY_KG_M3  # applies to both Mie scattering and mass
g = 9.81

V = 4 / 3 * np.pi * r_a ** 3
mass = rho_p * V
weight_force = mass * g
ka = k * r_a
wavelength = 2 * np.pi / k

print(f"Sphere: diameter=1cm (r_a={r_a*1000}mm), rho_p={rho_p} kg/m^3, V={V:.4e} m^3, "
      f"mass={mass*1e3:.4f} g")
print(f"ka = {ka:.4f}  (lambda={wavelength*1000:.4f}mm) -- NOT small, full Mie sum needed")
print(f"Target Fz to balance gravity: {weight_force:.6e} N ({weight_force*1e6:.3f} uN)")

center = np.array([0.0, 0.0, 0.0])  # midpoint between the two boards
transducer_pos = TRANSDUCERS.detach().cpu().numpy().real

n_max = 14
model = ARFModel(transducer_pos, center, n_max, k, transducer_radius, p_ref,
                  rho_0, c_0, rho_p, c_p, r_a, device=device)

Fext = torch.tensor([0.0, 0.0, weight_force], dtype=torch.float32, device=device)

# ---------------------------------------------------------------------------
# Warm start: acoustools WGS focus at the trap center (high-pressure focus,
# far better starting point than random phase for gradient descent)
# ---------------------------------------------------------------------------
p0 = create_points(1, 1, x=0.0, y=0.0, z=0.0)
x_focus = wgs(p0).squeeze().detach()  # (512,) complex64, |x|=1 (phase-only)
theta_init = torch.angle(x_focus).to(torch.float32)


def run_optimization(weights, iters=800, lr=0.03, label=""):
    wx, wy, wz = weights
    # scale-normalize the loss so Adam sees an O(1) objective regardless of
    # which components are weighted (mg^2 is the natural force^2 scale here)
    scale2 = weight_force ** 2

    theta = theta_init.clone().requires_grad_(True)
    opt = torch.optim.Adam([theta], lr=lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=iters, eta_min=lr * 1e-3)

    history = {"loss": [], "Fx": [], "Fy": [], "Fz": []}
    best = {"loss": float("inf")}
    for it in range(iters):
        opt.zero_grad()
        x = torch.exp(1j * theta.to(torch.complex64))
        Fx, Fy, Fz = model.force(x)
        loss = (wx * (Fx - Fext[0]) ** 2 + wy * (Fy - Fext[1]) ** 2
                + wz * (Fz - Fext[2]) ** 2) / scale2
        loss.backward()
        opt.step()
        sched.step()

        loss_val = loss.item()
        history["loss"].append(loss_val)
        history["Fx"].append(Fx.item())
        history["Fy"].append(Fy.item())
        history["Fz"].append(Fz.item())
        if loss_val < best["loss"]:
            best = {"loss": loss_val, "theta": theta.detach().clone(),
                     "Fx": Fx.item(), "Fy": Fy.item(), "Fz": Fz.item(), "it": it}
        if it % 100 == 0 or it == iters - 1:
            print(f"[{label}] iter {it:4d}  loss={loss_val:.6e}  "
                  f"Fx={Fx.item():.4e}  Fy={Fy.item():.4e}  Fz={Fz.item():.4e}")

    print(f"[{label}] BEST at iter {best['it']}: loss={best['loss']:.6e}  "
          f"Fx={best['Fx']:.4e}  Fy={best['Fy']:.4e}  Fz={best['Fz']:.4e}")
    x_best = torch.exp(1j * best["theta"].to(torch.complex64))
    return best["theta"], x_best, history, best


results = {}
for weights in [(0.0, 0.0, 1.0), (1.0, 1.0, 1.0)]:
    label = f"w={weights}"
    print(f"\n=== Optimizing for weight vector {weights} ===")
    theta_f, x_f, hist, best = run_optimization(weights, label=label)
    results[label] = {
        "weights": weights,
        "theta": theta_f.numpy().tolist(),
        "x_real": x_f.real.numpy().tolist(),
        "x_imag": x_f.imag.numpy().tolist(),
        "history": hist,
        "final_force": [best["Fx"], best["Fy"], best["Fz"]],
        "best_iter": best["it"],
    }
    print(f"Best force for {label}: Fx={best['Fx']:.4e}, Fy={best['Fy']:.4e}, "
          f"Fz={best['Fz']:.4e}  (target Fz={weight_force:.4e})")

with open("optimize_results.json", "w") as f:
    json.dump({
        "meta": {"r_a": r_a, "rho_p": rho_p, "c_p": c_p, "ka": ka, "n_max": n_max,
                  "weight_force": weight_force, "mass_g": mass * 1e3},
        "results": results,
    }, f, indent=1)
print("\nSaved optimize_results.json")
