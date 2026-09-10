"""
Differentiable (PyTorch, autograd-compatible w.r.t. transducer activation x)
version of the spherical-harmonic / Mie partial-wave acoustic radiation force,
validated in validate_gorkov.py against acoustools' Gor'kov force for r<<lambda.

All (n,m)-index bookkeeping and special-function evaluation (spherical
Bessel/Hankel/harmonics, which do NOT depend on the optimization variable x)
is precomputed once in numpy; only the final linear (S = x @ M) and bilinear
(force from S) steps -- which DO depend on x -- are done in torch, so
gradients w.r.t. x flow through correctly via PyTorch's complex autograd.
"""
import numpy as np
import torch

from arf_sh import (build_Snm_basis_matrix, cartesian_to_spherical_rel,
                     piston_directivity_taylor, scattering_coefficient, NMIndex)


class ARFModel:
    def __init__(self, transducer_pos, center, n_max, k, transducer_radius, p_ref,
                 rho_0, c_0, rho_p, c_p, r_a, device='cpu', dtype=torch.complex64):
        self.n_max = n_max
        self.k = k
        self.rho_0 = rho_0
        self.c_0 = c_0
        self.r_a = r_a
        self.device = device
        self.dtype = dtype

        C, nm, r_j, theta_j, phi_j = build_Snm_basis_matrix(
            transducer_pos, center, n_max, k, transducer_radius)
        self.nm = nm

        D = piston_directivity_taylor(theta_j, k, transducer_radius)  # (T,)
        w = 2 * p_ref * D  # (T,) real
        M = w[:, None] * C  # (T, NM) complex; S = x @ M

        self.M = torch.tensor(M, dtype=dtype, device=device)

        # Scattering coefficients c_n, n=0..n_max+1 (constants; not x-dependent)
        c_n = np.array([scattering_coefficient(n, k, r_a, rho_0, c_0, rho_p, c_p)
                         for n in range(n_max + 2)])
        Psi_n = 2j * (c_n[:-1] + np.conj(c_n[1:]) + 2 * c_n[:-1] * np.conj(c_n[1:]))  # n=0..n_max

        # Flatten the (n,m) loop (n=0..n_max-1, m=-n..n) into index arrays for
        # vectorized torch gather/compute.
        idx_nm, idx_n1_m1, idx_n_negm, idx_n1_negm1, idx_n1_m = [], [], [], [], []
        Anm_list, Bnm_list, Psi_idx = [], [], []
        for n in range(n_max):
            for m in range(-n, n + 1):
                idx_nm.append(nm.idx(n, m))
                idx_n1_m1.append(nm.idx(n + 1, m + 1))
                idx_n_negm.append(nm.idx(n, -m))
                idx_n1_negm1.append(nm.idx(n + 1, -m - 1))
                idx_n1_m.append(nm.idx(n + 1, m))
                Anm_list.append(np.sqrt((n + m + 1) * (n + m + 2) / ((2 * n + 1) * (2 * n + 3))))
                Bnm_list.append(-2 * np.sqrt((n + m + 1) * (n - m + 1) / ((2 * n + 1) * (2 * n + 3))))
                Psi_idx.append(n)

        self.idx_nm = torch.tensor(idx_nm, dtype=torch.long, device=device)
        self.idx_n1_m1 = torch.tensor(idx_n1_m1, dtype=torch.long, device=device)
        self.idx_n_negm = torch.tensor(idx_n_negm, dtype=torch.long, device=device)
        self.idx_n1_negm1 = torch.tensor(idx_n1_negm1, dtype=torch.long, device=device)
        self.idx_n1_m = torch.tensor(idx_n1_m, dtype=torch.long, device=device)
        self.Anm = torch.tensor(Anm_list, dtype=torch.float64, device=device)
        self.Bnm = torch.tensor(Bnm_list, dtype=torch.float64, device=device)
        self.Psi_n_full = torch.tensor(Psi_n, dtype=dtype, device=device)
        self.Psi_g = self.Psi_n_full[torch.tensor(Psi_idx, dtype=torch.long, device=device)]

        self.force_coeff = 1.0 / (8 * rho_0 * c_0 ** 2 * k ** 2)

    def force(self, x):
        """x: complex torch tensor, shape (T,) or (T,1). Returns Fx,Fy,Fz (real scalars, torch)."""
        x = x.reshape(-1).to(self.dtype)
        S = x @ self.M  # (NM,) complex

        Snm = S[self.idx_nm]
        Sn1m1 = S[self.idx_n1_m1]
        Snmn = S[self.idx_n_negm]
        Sn1mn1 = S[self.idx_n1_negm1]
        S1m = S[self.idx_n1_m]

        Gnm = Snm * torch.conj(Sn1m1) - Snmn * torch.conj(Sn1mn1)
        Hnm = Snm * torch.conj(Sn1m1) + Snmn * torch.conj(Sn1mn1)

        Anm_c = self.Anm.to(Snm.real.dtype)
        Bnm_c = self.Bnm.to(Snm.real.dtype)

        Fx_sum = torch.sum(self.Psi_g * Anm_c * Gnm)
        Fy_sum = torch.sum(self.Psi_g * Anm_c * Hnm)
        Fz_sum = torch.sum(self.Psi_g * Bnm_c * (Snm * torch.conj(S1m)))

        Fx = self.force_coeff * Fx_sum.real
        Fy = self.force_coeff * Fy_sum.imag
        Fz = self.force_coeff * Fz_sum.real
        return Fx, Fy, Fz
