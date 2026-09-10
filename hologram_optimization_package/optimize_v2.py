"""
optimize.py, v2: warm-start from the standard TWIN-TRAP configuration
(acoustools add_lev_sig -- +pi phase flip of one board, giving a pressure
NODE at the center, the textbook stable-trap field structure) instead of a
plain focus (pressure ANTI-node -- confirmed unstable by stability_check.py).
Everything else identical to optimize.py.
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

center = np.array([0.0, 0.0, 0.0])
transducer_pos = TRANSDUCERS.detach().cpu().numpy().real

n_max = 14
model = ARFModel(transducer_pos, center, n_max, k, transducer_radius, p_ref,
                  rho_0, c_0, rho_p, c_p, r_a, device=device)

Fext = torch.tensor([0.0, 0.0, weight_force], dtype=torch.float32, device=device)

# Warm start: WGS focus + standard twin-trap signature (pressure NODE at center)
p0 = create_points(1, 1, x=0.0, y=0.0, z=0.0)
x_focus = wgs(p0)
x_trap = add_lev_sig(x_focus.clone()).squeeze().detach()
theta_init = torch.angle(x_trap).to(torch.float32)

Fx0, Fy0, Fz0 = model.force(torch.exp(1j * theta_init.to(torch.complex64)))
print(f"Twin-trap warm start force (pre-optimization): Fx={Fx0.item():.4e} Fy={Fy0.item():.4e} Fz={Fz0.item():.4e}")
print(f"Target Fz = {weight_force:.4e} N")


def run_optimization(weights, iters=800, lr=0.03, label=""):
    wx, wy, wz = weights
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
    print(f"\n=== [twin-trap warm start] Optimizing for weight vector {weights} ===")
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

with open("optimize_results_v2_trap.json", "w") as f:
    json.dump({
        "meta": {"r_a": r_a, "rho_p": rho_p, "c_p": c_p, "ka": ka, "n_max": n_max,
                  "weight_force": weight_force, "mass_g": mass * 1e3,
                  "warm_start": "twin-trap (add_lev_sig)"},
        "results": results,
    }, f, indent=1)
print("\nSaved optimize_results_v2_trap.json")
