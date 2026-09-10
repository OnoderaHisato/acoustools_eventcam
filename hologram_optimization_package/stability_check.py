"""
Eq. (22)'s second term (the Lyapunov stability term) requires the eigenvalues
of the force Jacobian M^-1 grad(F(0)) to have non-positive real parts -- i.e.
the point must be a genuine restoring-force trap, not merely a force-balance
point. My optimize.py only implemented the force-matching term (as the user
asked to focus on: "try different weights for Fx, Fy, Fz"), so this script
checks, post-hoc, whether the converged solutions happen to be stable traps
by finite-differencing F(q_fixed, X) w.r.t. small positional offsets X from
the trap center, for the FIXED (already-optimized) phase pattern.
"""
import json
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
n_max = 12  # slightly reduced for speed (6 extra model builds); already converged at n_max=10
transducer_pos = TRANSDUCERS.detach().cpu().numpy().real

with open("optimize_results.json") as f:
    data = json.load(f)
print(f"Evaluating fixed phases at rho_p={rho_p} kg/m^3; "
      f"source optimization rho_p={data['meta']['rho_p']} kg/m^3 (not re-optimized).")

delta = 0.0003  # 0.3mm perturbation (small vs array scale, ~lambda/30)

for label, res in data["results"].items():
    x_real = np.array(res["x_real"])
    x_imag = np.array(res["x_imag"])
    x = torch.tensor(x_real + 1j * x_imag, dtype=torch.complex64)

    F0 = np.array(res["final_force"])
    J = np.zeros((3, 3))  # J[i,j] = dF_i/dX_j
    for j, axis in enumerate(['x', 'y', 'z']):
        for sign in [+1, -1]:
            center = np.zeros(3)
            center[j] = sign * delta
            model = ARFModel(transducer_pos, center, n_max, k, transducer_radius, p_ref,
                              rho_0, c_0, rho_p, c_p, r_a)
            Fx, Fy, Fz = model.force(x)
            F = np.array([Fx.item(), Fy.item(), Fz.item()])
            J[:, j] += sign * F / (2 * delta)

    eigvals = np.linalg.eigvals(J)
    print(f"\n=== {label} ===")
    print("Force Jacobian dF_i/dX_j (N/m):")
    print(J)
    print("Eigenvalues of Jacobian:", eigvals)
    print("Re[eigenvalues]:", eigvals.real)
    stable = np.all(eigvals.real <= 1e-6 * np.abs(eigvals).max())
    print(f"=> {'STABLE (restoring force, Lyapunov condition satisfied)' if stable else 'NOT STABLE (at least one non-negative real eigenvalue -- not a true 3D trap at this point)'}")
