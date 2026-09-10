"""
Force profile along each axis, for the FIXED (already-optimized, stable-trap)
phase pattern from optimize_results_v3_stable.json:
    Fx(x, y=0, z=0), Fy(x=0, y, z=0), Fz(x=0, y=0, z)
i.e. sweep one coordinate at a time, holding the other two at 0 (z=0 = the
midplane / "half-height" between the two opposing boards).
"""
import json
import time
import numpy as np
import torch
import sys
sys.path.insert(0, '.')
from arf_torch import ARFModel
from particle_parameters import PARTICLE_DENSITY_KG_M3

from acoustools.Utilities import TRANSDUCERS
import acoustools.Constants as Constants

k = Constants.k
p_ref = Constants.P_ref
transducer_radius = Constants.radius
rho_0 = Constants.p_0
c_0 = Constants.c_0
c_p = Constants.c_p
r_a = 0.005
rho_p = PARTICLE_DENSITY_KG_M3
n_max = 10  # confirmed converged at n_max=10 for ka=3.66 (matches n_max=18 exactly)
transducer_pos = TRANSDUCERS.detach().cpu().numpy().real

with open("optimize_results_v3_stable.json") as f:
    data = json.load(f)
V = 4 / 3 * np.pi * r_a ** 3
mass = rho_p * V
weight_force = mass * 9.81
# Evaluation conditions must not inherit the old optimization density/weight.
evaluation_meta = {
    **data["meta"],
    "r_a": r_a, "rho_p": rho_p, "c_p": c_p, "ka": k * r_a,
    "n_max": n_max, "mass_g": mass * 1e3, "weight_force": weight_force,
    "rho_0": rho_0, "c_0": c_0, "k": k,
    "p_ref": p_ref, "transducer_radius": transducer_radius,
}
print(f"Evaluating fixed phases at rho_p={rho_p} kg/m^3, mg={weight_force*1e6:.3f} uN; "
      f"source optimization rho_p={data['meta']['rho_p']} kg/m^3 (not re-optimized).")

sweep_range = 0.006  # +/- 6mm (~0.7 lambda), covers well past the particle radius (5mm)
n_points = 61
offsets = np.linspace(-sweep_range, sweep_range, n_points)

profiles = {}
for label, res in data["results"].items():
    x_real = np.array(res["x_real"]); x_imag = np.array(res["x_imag"])
    x = torch.tensor(x_real + 1j * x_imag, dtype=torch.complex64)

    profiles[label] = {"offsets": offsets.tolist()}
    t0 = time.time()
    for axis_idx, axis_name in enumerate(['x', 'y', 'z']):
        Fx_list, Fy_list, Fz_list = [], [], []
        for off in offsets:
            center = np.zeros(3)
            center[axis_idx] = off
            model = ARFModel(transducer_pos, center, n_max, k, transducer_radius, p_ref,
                              rho_0, c_0, rho_p, c_p, r_a)
            Fx, Fy, Fz = model.force(x)
            Fx_list.append(Fx.item()); Fy_list.append(Fy.item()); Fz_list.append(Fz.item())
        profiles[label][f"F_vs_{axis_name}"] = {"Fx": Fx_list, "Fy": Fy_list, "Fz": Fz_list}
        print(f"[{label}] swept along {axis_name}: {time.time()-t0:.1f}s elapsed")

with open("force_profiles.json", "w") as f:
    json.dump({"meta": evaluation_meta, "source_optimization_meta": data["meta"],
               "profiles": profiles}, f, indent=1)
print("Saved force_profiles.json")
