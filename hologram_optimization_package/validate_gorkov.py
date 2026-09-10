"""
Step 1 validation (the user's explicit request): for r << lambda/10, the
general spherical-harmonic / Mie partial-wave force must reduce to Gor'kov's
small-particle force. Ground truth = acoustools.Force.compute_force (its own
analytic Gor'kov gradient), using the SAME material properties, SAME forward
model, SAME field.

This also resolves the Fx/Fy overall-prefactor ambiguity found when
cross-checking the Silva formula against Trust_Me_ARF's return_force.m by
hand (Fz matched exactly; Fx/Fy had an unresolved factor of ~2). We compute
both components at an off-axis point and read off the calibration factor
empirically, then freeze it.
"""
import numpy as np
import torch
import sys
sys.path.insert(0, '.')
from arf_sh import (build_Snm_basis_matrix, transducer_amplitudes, scattering_coefficient,
                     force_from_Snm, NMIndex)
import arf_sh
from particle_parameters import PARTICLE_DENSITY_KG_M3

from acoustools.Utilities import TRANSDUCERS, create_points, DTYPE, device
from acoustools.Force import compute_force
import acoustools.Constants as Constants

k = Constants.k
p_ref = Constants.P_ref
transducer_radius = Constants.radius
rho_0 = Constants.p_0
c_0 = Constants.c_0
rho_p = PARTICLE_DENSITY_KG_M3  # same density in both SH/Mie and Gor'kov models
c_p = Constants.c_p     # 1052 m/s

transducer_pos = TRANSDUCERS.detach().cpu().numpy().real
wavelength = 2 * np.pi / k
print(f"lambda={wavelength*1000:.4f}mm, rho_0={rho_0}, c_0={c_0}, rho_p={rho_p}, c_p={c_p}")

torch.manual_seed(3)
x_t = torch.exp(1j * torch.rand(TRANSDUCERS.shape[0], 1) * 2 * np.pi).to(DTYPE)
x_np = x_t.squeeze(1).detach().cpu().numpy()

# Off-axis, off-center point so Fx,Fy,Fz are all generically nonzero
center = np.array([0.004, 0.003, 0.015])

r_a = wavelength / 100   # deeply r << lambda/10 as requested
ka = k * r_a
print(f"\nTest particle radius r_a = lambda/100 = {r_a*1000:.5f} mm, ka = {ka:.4f}")

n_max = 4  # tiny ka -> low order sufficient; will check convergence below
C, nm, r_j, *_ = build_Snm_basis_matrix(transducer_pos, center, n_max, k, transducer_radius)
a = transducer_amplitudes(x_np, transducer_pos, center, k, transducer_radius, p_ref)
S = a @ C


def s_n_func(n):
    return scattering_coefficient(n, k, r_a, rho_0, c_0, rho_p, c_p)


# raw (uncalibrated Fxy) force
pass
Fx_raw, Fy_raw, Fz = force_from_Snm(S, nm, s_n_func, n_max, k, rho_0, c_0)
print(f"\nSH-Mie force (raw, FXY_CALIBRATION=1): Fx={Fx_raw:.6e}, Fy={Fy_raw:.6e}, Fz={Fz:.6e} N")

# Ground truth: acoustools Gor'kov-gradient force, same particle/point/phases
V = 4 / 3 * np.pi * r_a ** 3
p_ac = create_points(1, 1, x=float(center[0]), y=float(center[1]), z=float(center[2]))
F_ac = compute_force(x_t, p_ac, V=V, particle_density=rho_p, particle_speed=c_p,
                      medium_density=rho_0, medium_speed=c_0, p_ref=p_ref,
                      transducer_radius=transducer_radius, k=k)
F_ac_np = F_ac.detach().cpu().numpy().squeeze()
print(f"acoustools Gor'kov force:              Fx={F_ac_np[0]:.6e}, Fy={F_ac_np[1]:.6e}, Fz={F_ac_np[2]:.6e} N")

print(f"\nFz ratio (SH/Gorkov) = {Fz/F_ac_np[2]:.6f}")
print(f"Fx ratio (SH_raw/Gorkov) = {Fx_raw/F_ac_np[0]:.6f}")
print(f"Fy ratio (SH_raw/Gorkov) = {Fy_raw/F_ac_np[1]:.6f}")

# Convergence check in n_max
print("\nConvergence of SH force vs n_max:")
for nmax_test in [1, 2, 3, 4, 6, 8]:
    C2, nm2, *_ = build_Snm_basis_matrix(transducer_pos, center, nmax_test, k, transducer_radius)
    S2 = a @ C2
    fx, fy, fz = force_from_Snm(S2, nm2, s_n_func, nmax_test, k, rho_0, c_0)
    print(f"  n_max={nmax_test}: Fx={fx:.6e} Fy={fy:.6e} Fz={fz:.6e}")
