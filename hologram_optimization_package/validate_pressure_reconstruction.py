"""
Step 0 validation (before any force physics): confirm that the S_nm beam-shape
coefficients built from transducer positions/activations via the multipole
addition theorem (arf_sh.build_Snm_basis_matrix + transducer_amplitudes)
reconstruct the SAME complex pressure field that acoustools.propagate()
computes, at points near an arbitrary trap center. If this doesn't match to
high precision, nothing downstream (Gor'kov check, force) can be trusted.
"""
import numpy as np
import torch
import sys
sys.path.insert(0, '.')
from arf_sh import (build_Snm_basis_matrix, transducer_amplitudes, reconstruct_pressure,
                     NMIndex)

from acoustools.Utilities import TRANSDUCERS, create_points, propagate, DTYPE, device
import acoustools.Constants as Constants

k = Constants.k
p_ref = Constants.P_ref
transducer_radius = Constants.radius

transducer_pos = TRANSDUCERS.detach().cpu().numpy().real  # (512,3)

# Random-ish phase pattern (not focused), off-center trap point, to stress-test
torch.manual_seed(0)
x_t = torch.exp(1j * torch.rand(TRANSDUCERS.shape[0], 1) * 2 * np.pi).to(DTYPE)
x_np = x_t.squeeze(1).detach().cpu().numpy()

center = np.array([0.003, -0.002, 0.01])  # 10mm off origin, well inside array

n_max = 14
C, nm, r_j, theta_j, phi_j = build_Snm_basis_matrix(transducer_pos, center, n_max, k, transducer_radius)
a = transducer_amplitudes(x_np, transducer_pos, center, k, transducer_radius, p_ref)
S = a @ C

wavelength = 2 * np.pi / k
print(f"wavelength = {wavelength*1000:.4f} mm, lambda/10 = {wavelength/10*1000:.4f} mm")

# Field points on a small sphere of radius lambda/10 around center (as user requested),
# PLUS a few points well inside min(r_j) to respect addition-theorem validity r < r_j
min_rj = r_j.min()
print(f"min transducer distance to center: {min_rj*1000:.2f} mm")

r_eval = wavelength / 10
n_test = 50
rng = np.random.default_rng(1)
dirs = rng.normal(size=(n_test, 3))
dirs /= np.linalg.norm(dirs, axis=1, keepdims=True)
test_points_rel = dirs * r_eval  # relative to center
test_points_abs = test_points_rel + center[None, :]

P_reconstructed = reconstruct_pressure(S, nm, test_points_rel, k)

# acoustools ground truth
pts_t = torch.tensor(test_points_abs.T[None, :, :], dtype=DTYPE, device=device)  # (1,3,N)
P_acoustools = propagate(x_t, pts_t).squeeze(0).detach().cpu().numpy()

err = np.abs(P_reconstructed - P_acoustools)
rel_err = err / np.abs(P_acoustools)
print(f"\nr_eval = lambda/10 = {r_eval*1000:.4f} mm, n_max={n_max}")
print(f"max |P_reconstructed - P_acoustools| = {err.max():.6e} Pa")
print(f"max relative error = {rel_err.max():.6e}")
print(f"mean relative error = {rel_err.mean():.6e}")
print("sample values (reconstructed vs acoustools):")
for i in range(5):
    print(f"  {P_reconstructed[i]:.4f}  vs  {P_acoustools[i]:.4f}")

assert rel_err.max() < 1e-4, "Pressure reconstruction FAILED -- SH/geometry convention is wrong."
print("\nPASSED: S_nm addition-theorem reconstruction matches acoustools.propagate() to <1e-4 relative error.")

# Also test convergence in n_max (should need fewer terms at small r_eval)
print("\nConvergence vs n_max at r_eval=lambda/10:")
for nmax_test in [2, 4, 6, 8, 10, 14]:
    C2, nm2, *_ = build_Snm_basis_matrix(transducer_pos, center, nmax_test, k, transducer_radius)
    S2 = a @ C2
    P2 = reconstruct_pressure(S2, nm2, test_points_rel, k)
    relerr2 = np.abs(P2 - P_acoustools) / np.abs(P_acoustools)
    print(f"  n_max={nmax_test:2d}: max rel err = {relerr2.max():.3e}")
