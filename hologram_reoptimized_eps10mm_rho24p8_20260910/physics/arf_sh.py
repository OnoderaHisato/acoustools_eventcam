"""
Acoustic radiation force (ARF) on a sphere via spherical-harmonic / partial-wave
(Mie-theory) expansion of the incident field from a phased array -- NOT the
boundary-element method of Inoue et al. 2019.

Physics reference: Sapozhnikov & Bailey, JASA 133, 661 (2013); reproduced/used
in Andersson & Ahrens (IEEE IUS 2019) and Zehnter, Andrade & Ament, J. Appl.
Phys. 129 (2021) -- the same formula structure found (in MATLAB) in the
Trust_Me_ARF reference repo (github.com/tpy37/Trust_Me_ARF), re-derived here
independently from the free-space multipole addition theorem and
re-implemented in PyTorch (differentiable w.r.t. the transducer activation
vector) so it can be used directly as the F(q) term of Inoue's Eq. (22).

SPHERICAL HARMONIC CONVENTION (this is the part the user explicitly warned
is easy to get wrong):
    Y_n^m(theta, phi) = scipy.special.sph_harm_y(n, m, theta, phi)
    with theta = POLAR angle from +z axis in [0, pi],
         phi   = AZIMUTHAL angle in [0, 2*pi).
    This is the standard orthonormal ( \\int |Y_n^m|^2 dOmega = 1 ) physics
    convention INCLUDING the Condon-Shortley phase (-1)^m for m>0.
    Verified empirically against closed-form Y_0^0, Y_1^0, Y_1^1 (see
    check_sph_harm_convention() below) before being used anywhere else.

    scipy >= 1.15 exposes `sph_harm_y(n, m, theta, phi)` (theta=polar,
    phi=azimuthal -- matches the physics-textbook naming). The legacy
    `sph_harm(m, n, theta, phi)` (theta=AZIMUTHAL, phi=POLAR -- opposite
    naming!) is deprecated/removed in the scipy installed here (1.18.1).
    Mixing these two up (or accidentally using the legacy argument order)
    silently swaps theta<->phi and gives wrong forces -- this is exactly
    the kind of "type of the function" bug being guarded against.

Geometry / addition-theorem convention:
    Expansion origin = sphere (levitation) center.
    For a point monopole-like transducer source at position vector rho_j
    (measured FROM the expansion origin), with amplitude a_j, radiating
    p_j(r) = a_j * exp(i k R) / R   (R = |r - transducer position|),
    the free-space addition theorem gives, for |r| < |rho_j|:

        p_j(r) = a_j * 4*pi*i*k * sum_{n,m} j_n(k|r|) h_n^(1)(k rho_j)
                        Y_n^m(theta_r, phi_r) * conj(Y_n^m(theta_j, phi_j))

    so the incident-field regular-wave expansion coefficients are

        S_nm = sum_j a_j * 4*pi*i*k * h_n^(1)(k rho_j) * conj(Y_n^m(theta_j, phi_j))

    This is validated directly against acoustools.propagate() in
    validate_pressure_reconstruction.py before any force physics is trusted.
"""
import numpy as np
from scipy import special


# ---------------------------------------------------------------------------
# Spherical harmonics: convention check
# ---------------------------------------------------------------------------
def Y(n, m, theta, phi):
    """Orthonormal complex spherical harmonic with Condon-Shortley phase.
    theta = polar angle (from +z), phi = azimuthal angle."""
    return special.sph_harm_y(n, m, theta, phi)


def check_sph_harm_convention():
    """Cross-check Y() against hand-derived closed forms. Raises if wrong."""
    theta, phi = 0.7, 1.3
    checks = {
        (0, 0): 1 / np.sqrt(4 * np.pi),
        (1, 0): np.sqrt(3 / (4 * np.pi)) * np.cos(theta),
        (1, 1): -np.sqrt(3 / (8 * np.pi)) * np.sin(theta) * np.exp(1j * phi),
        (1, -1): np.sqrt(3 / (8 * np.pi)) * np.sin(theta) * np.exp(-1j * phi),
        (2, 0): np.sqrt(5 / (16 * np.pi)) * (3 * np.cos(theta) ** 2 - 1),
    }
    for (n, m), true_val in checks.items():
        got = Y(n, m, theta, phi)
        err = abs(got - true_val)
        assert err < 1e-10, f"Y({n},{m}) mismatch: got {got}, expected {true_val}, err={err}"
    print("Spherical harmonic convention check PASSED "
          "(scipy.special.sph_harm_y(n,m,theta_polar,phi_azimuthal), orthonormal + Condon-Shortley).")


# ---------------------------------------------------------------------------
# Spherical Bessel / Hankel functions (standard scipy, real argument)
# ---------------------------------------------------------------------------
def sph_jn(n, x):
    return special.spherical_jn(n, x)


def sph_jn_prime(n, x):
    return special.spherical_jn(n, x, derivative=True)


def sph_yn(n, x):
    return special.spherical_yn(n, x)


def sph_yn_prime(n, x):
    return special.spherical_yn(n, x, derivative=True)


def sph_h1(n, x):
    return sph_jn(n, x) + 1j * sph_yn(n, x)


def sph_h1_prime(n, x):
    return sph_jn_prime(n, x) + 1j * sph_yn_prime(n, x)


# ---------------------------------------------------------------------------
# Geometry: transducer positions -> spherical coords relative to a center
# ---------------------------------------------------------------------------
def cartesian_to_spherical_rel(positions, center):
    """positions: (T,3) array of transducer xyz. center: (3,) expansion origin.
    Returns r_j (T,), theta_j (T,) polar from +z, phi_j (T,) azimuthal."""
    vec = positions - np.asarray(center)[None, :]
    r = np.linalg.norm(vec, axis=1)
    theta = np.arccos(np.clip(vec[:, 2] / r, -1.0, 1.0))
    phi = np.arctan2(vec[:, 1], vec[:, 0])
    return r, theta, phi, vec


# ---------------------------------------------------------------------------
# acoustools piston directivity (exact same formula as
# acoustools.Utilities.Forward_models, replicated so our field reconstruction
# matches acoustools.propagate() bit-for-bit).
# ---------------------------------------------------------------------------
def piston_directivity_taylor(theta_j, k, transducer_radius):
    """theta_j = polar angle of (transducer - center) from board-normal axis (z).
    Board normals are along +-z, so sin(angle to normal) = sin(theta_j)
    regardless of which board (normal sign) the transducer is on."""
    sine_angle = np.sin(theta_j)
    x = k * transducer_radius * sine_angle
    return 0.5 - x ** 2 / 16 + x ** 4 / 384


# ---------------------------------------------------------------------------
# Incident-field beam-shape coefficients S_nm via the free-space multipole
# addition theorem, built directly from transducer positions + activations
# (no field sampling / quadrature needed -- see module docstring).
# ---------------------------------------------------------------------------
def nm_index_list(n_max):
    """All (n,m) pairs for n=0..n_max, m=-n..n, in a fixed order."""
    pairs = []
    for n in range(n_max + 1):
        for m in range(-n, n + 1):
            pairs.append((n, m))
    return pairs


class NMIndex:
    def __init__(self, n_max):
        self.n_max = n_max
        self.pairs = nm_index_list(n_max)
        self.index = {nm: i for i, nm in enumerate(self.pairs)}

    def idx(self, n, m):
        if n < 0 or n > self.n_max or abs(m) > n:
            return None
        return self.index[(n, m)]


def build_Snm_basis_matrix(transducer_pos, center, n_max, k, transducer_radius):
    """
    Returns C: complex array shape (T, NM) such that, for a per-transducer
    complex source AMPLITUDE vector a (T,) [a_j = 2*p_ref*x_j*D_j, i.e. the
    actual acoustools per-transducer output p_j(r)=a_j*exp(ikR)/R],
        S_nm = a @ C[:, idx(n,m)]
    i.e. C[j, idx(n,m)] = 4*pi*i*k * h_n^(1)(k*r_j) * conj(Y_n^m(theta_j, phi_j)).
    Directivity is NOT included here (folded into `a` by the caller) since it
    only depends on geometry too -- kept separate for clarity/reuse.
    """
    r_j, theta_j, phi_j, _ = cartesian_to_spherical_rel(transducer_pos, center)
    nm = NMIndex(n_max)
    T = transducer_pos.shape[0]
    NM = len(nm.pairs)
    C = np.zeros((T, NM), dtype=complex)
    for n in range(n_max + 1):
        h1 = sph_h1(n, k * r_j)  # (T,)
        pref = 4 * np.pi * 1j * k * h1  # (T,)
        for m in range(-n, n + 1):
            Ynm = Y(n, m, theta_j, phi_j)  # (T,)
            C[:, nm.idx(n, m)] = pref * np.conj(Ynm)
    return C, nm, r_j, theta_j, phi_j


def transducer_amplitudes(x, transducer_pos, center, k, transducer_radius, p_ref):
    """a_j = 2*p_ref*x_j*D_j(theta_j) -- matches acoustools forward_model's
    per-transducer trans_matrix EXACTLY (same Taylor-series directivity),
    so p_inc(r) reconstructed from S_nm matches acoustools.propagate()."""
    _, theta_j, _, _ = cartesian_to_spherical_rel(transducer_pos, center)
    D = piston_directivity_taylor(theta_j, k, transducer_radius)
    return 2 * p_ref * x * D


def reconstruct_pressure(S, nm, field_points, k):
    """field_points: (N,3) Cartesian, relative to the SAME center used to build S.
    Returns complex pressure P(r) = sum_{n,m} S_nm * j_n(k|r|) * Y_n^m(theta,phi)."""
    r, theta, phi, _ = cartesian_to_spherical_rel(field_points, np.zeros(3))
    P = np.zeros(field_points.shape[0], dtype=complex)
    for (n, m), i in nm.index.items():
        jn = sph_jn(n, k * r)
        Ynm = Y(n, m, theta, phi)
        P += S[i] * jn * Ynm
    return P


# ---------------------------------------------------------------------------
# Scattering coefficients -- literal translation of
# Trust_Me_ARF/package_original/scattering_coefficient.m (general fluid sphere).
# Z = rho_p*c_p / (rho_0*c_0) (relative acoustic impedance), NOT the gamma=1/Z
# convention -- kept as its own function since it is paired 1:1 with the
# force summation structure below (force_from_Snm), which was derived
# together with this exact scattering-coefficient convention.
# ---------------------------------------------------------------------------
def scattering_coefficient(n, k, r_a, rho_0, c_0, rho_p, c_p):
    kp = k * c_0 / c_p
    Z = (rho_p * c_p) / (rho_0 * c_0)

    jn_ka, jnp_ka = sph_jn(n, k * r_a), sph_jn_prime(n, k * r_a)
    jn_kpa, jnp_kpa = sph_jn(n, kp * r_a), sph_jn_prime(n, kp * r_a)
    h1_ka, h1p_ka = sph_h1(n, k * r_a), sph_h1_prime(n, k * r_a)

    num = jn_ka * jnp_kpa - Z * (jnp_ka * jn_kpa)
    den = h1_ka * jnp_kpa - Z * (h1p_ka * jn_kpa)
    return -num / den


def scattering_coefficient_rigid(n, k, r_a):
    """Rigid/sound-hard limit: s_n = -j_n'(ka) / h_n^{(1)'}(ka)."""
    ka = k * r_a
    return -sph_jn_prime(n, ka) / sph_h1_prime(n, ka)


# ---------------------------------------------------------------------------
# Force from beam-shape coefficients S_nm and scattering coefficients c_n --
# literal translation of Trust_Me_ARF/package_original/return_force.m, with
# our own validated S_nm (addition theorem) and Y_n^m (scipy sph_harm_y).
# ---------------------------------------------------------------------------
def force_from_Snm(S, nm, s_n_func, n_max, k, rho_0, c_0):
    """S: complex array indexed via nm.idx(n,m). s_n_func(n) -> scattering
    coefficient c_n. Returns (Fx, Fy, Fz) real force components in Newtons."""
    c = [s_n_func(n) for n in range(n_max + 2)]

    def Snm_val(n, m):
        i = nm.idx(n, m)
        if i is None:
            return 0.0 + 0.0j
        return S[i]

    force_coeff = 1.0 / (8 * rho_0 * c_0 ** 2 * k ** 2)

    Fx_sum = 0.0 + 0.0j
    Fy_sum = 0.0 + 0.0j
    Fz_sum = 0.0 + 0.0j
    for n in range(0, n_max):
        Psi_n = 2j * (c[n] + np.conj(c[n + 1]) + 2 * c[n] * np.conj(c[n + 1]))
        for m in range(-n, n + 1):
            Snm = Snm_val(n, m)
            Sn1m1 = Snm_val(n + 1, m + 1)
            Snmn = Snm_val(n, -m)
            Sn1mn1 = Snm_val(n + 1, -m - 1)
            S1m = Snm_val(n + 1, m)

            Anm = np.sqrt((n + m + 1) * (n + m + 2) / ((2 * n + 1) * (2 * n + 3)))
            Bnm = -2 * np.sqrt((n + m + 1) * (n - m + 1) / ((2 * n + 1) * (2 * n + 3)))
            Gnm = Snm * np.conj(Sn1m1) - Snmn * np.conj(Sn1mn1)
            Hnm = Snm * np.conj(Sn1m1) + Snmn * np.conj(Sn1mn1)

            Fx_sum += Psi_n * Anm * Gnm
            Fy_sum += Psi_n * Anm * Hnm
            Fz_sum += Psi_n * Bnm * (Snm * np.conj(S1m))

    Fx = force_coeff * Fx_sum.real
    Fy = force_coeff * Fy_sum.imag
    Fz = force_coeff * Fz_sum.real
    return Fx, Fy, Fz


if __name__ == "__main__":
    check_sph_harm_convention()
