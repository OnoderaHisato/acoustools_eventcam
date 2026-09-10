"""Per-axis second-order inverse (design C) trajectory pre-compensation.

Command equation, per axis a with plant  x'' = -w0^2 (x - u(t-tau)) - gamma x':

    u_a(t) = r_a(t+tau_a) + ( r_a''(t+tau_a) + gamma_a * r_a'(t+tau_a) ) / w0_a^2

The advance by tau_a cancels the identified command-to-trap delay; the
(r'' + gamma r') / w0^2 term cancels the second-order lag of the trap
response.  Derivatives use a Savitzky-Golay filter (numpy-only
implementation, validated against scipy.signal.savgol_filter).  The total
correction u - r is limited in vector norm, matching the offline simulation
that predicted the improvement (feedforward_inverse_sim.py, design C).
"""

from __future__ import annotations

import math
from typing import Sequence, Tuple

import numpy as np

Position = Tuple[float, float, float]

# (f0_hz, gamma_1_per_s, tau_sec) per axis x, y, z — ringdown + rollout values
# (the fixed-physics model of the 2026-08 stereo-3D analysis).
DEFAULT_PHYS_AXES: tuple[tuple[float, float, float], ...] = (
    (81.2, 31.4, 2.0e-3),
    (84.2, 36.2, 2.1e-3),
    (270.0, 23.8, 1.8e-3),
)


def savgol_deriv(
    x: np.ndarray, window: int, poly: int, deriv: int, delta: float, periodic: bool = False
) -> np.ndarray:
    """Savitzky-Golay derivative filter (numpy only).

    Non-periodic input uses edge-value padding; a periodic (closed-cycle)
    input wraps around so the derivative is continuous across the seam.
    """
    if window % 2 == 0 or window < poly + 2:
        raise ValueError("window must be odd and larger than poly+1")
    half = window // 2
    offsets = np.arange(-half, half + 1, dtype=float)
    design = np.vander(offsets, poly + 1, increasing=True)
    coeffs = np.linalg.pinv(design)[deriv] * math.factorial(deriv) / (delta ** deriv)
    if periodic:
        padded = np.concatenate([x[-half:], x, x[:half]])
    else:
        padded = np.concatenate([np.full(half, x[0]), x, np.full(half, x[-1])])
    # y[i] = sum_j coeffs[j] * x[i + offsets[j]]  (correlation, not convolution)
    return np.convolve(padded, coeffs[::-1], mode="valid")


def advance(x: np.ndarray, dt_advance: float, sample_hz: float, periodic: bool = False) -> np.ndarray:
    """x(t + dt_advance), fractional shift via linear interpolation.

    Non-periodic input holds the last value at the edge; a periodic input is
    shifted circularly (one sample of x covers exactly one cycle).
    """
    n = len(x)
    t = np.arange(n) / sample_hz
    if periodic:
        period = n / sample_hz
        xp = np.concatenate([t, [period]])
        fp = np.concatenate([x, [x[0]]])
        return np.interp(np.mod(t + dt_advance, period), xp, fp)
    return np.interp(t + dt_advance, t, x)


def apply_inverse2nd_feedforward(
    positions: Sequence[Position],
    sample_hz: float,
    phys_axes: Sequence[tuple[float, float, float]] = DEFAULT_PHYS_AXES,
    max_offset_mm: float = 1.0,
    limit_strategy: str = "pointwise_clip",
    sg_window_ms: float = 5.0,
    sg_poly: int = 3,
    periodic: bool = False,
) -> tuple[list[Position], dict[str, float | str | list[float]]]:
    """Return the design-C command with a vector-norm safety limit.

    ``positions`` are metres, as used by the recording workflow.  The returned
    statistics report offsets in millimetres.  Pass ``periodic=True`` for a
    closed-cycle trajectory (one loop that the PAT repeats): derivatives and
    the tau advance then wrap around the seam instead of holding the edges.
    """
    r = np.asarray(positions, dtype=float)
    if r.ndim != 2 or r.shape[1] != 3:
        raise ValueError("positions must be an N x 3 sequence")
    if len(r) < 16:
        raise ValueError("at least 16 trajectory positions are required")
    if sample_hz <= 0 or max_offset_mm <= 0 or sg_window_ms <= 0:
        raise ValueError("sample_hz, max_offset_mm, and sg_window_ms must be positive")
    if len(phys_axes) != 3:
        raise ValueError("phys_axes must give (f0_hz, gamma, tau_sec) for x, y, z")
    if limit_strategy not in {"pointwise_clip", "global_scale"}:
        raise ValueError("limit_strategy must be 'pointwise_clip' or 'global_scale'")

    dt = 1.0 / float(sample_hz)
    window = int(round(sg_window_ms * 1.0e-3 * sample_hz))
    window += (window % 2 == 0)
    window = max(window, sg_poly + 2 + ((sg_poly + 2) % 2 == 0))
    if window > len(r):
        raise ValueError("trajectory shorter than the Savitzky-Golay window")

    u = np.empty_like(r)
    for axis in range(3):
        f0, gamma, tau = (float(v) for v in phys_axes[axis])
        if f0 <= 0 or gamma < 0 or tau < 0:
            raise ValueError(f"invalid physics for axis {axis}: {phys_axes[axis]}")
        w0_sq = (2.0 * math.pi * f0) ** 2
        r1 = savgol_deriv(r[:, axis], window, sg_poly, 1, dt, periodic)
        r2 = savgol_deriv(r[:, axis], window, sg_poly, 2, dt, periodic)
        u[:, axis] = (
            advance(r[:, axis], tau, sample_hz, periodic)
            + (advance(r2, tau, sample_hz, periodic) + gamma * advance(r1, tau, sample_hz, periodic))
            / w0_sq
        )

    offset = u - r
    norms = np.linalg.norm(offset, axis=1)
    raw_max_m = float(norms.max())
    max_offset_m = max_offset_mm * 1.0e-3
    global_scale = 1.0
    clipped = 0
    if limit_strategy == "global_scale":
        if raw_max_m > max_offset_m > 0.0:
            global_scale = max_offset_m / raw_max_m
        offset = offset * global_scale
        norms = norms * global_scale
    else:
        over = norms > max_offset_m
        clipped = int(over.sum())
        if clipped:
            scale = np.ones_like(norms)
            scale[over] = max_offset_m / norms[over]
            offset = offset * scale[:, None]
            norms = np.minimum(norms, max_offset_m)
    u = r + offset

    # AcousTools' create_points type-checks coordinates with `type(x) is float`
    # and silently leaves the point at (0,0,0) for numpy scalars, so the
    # command MUST be returned as builtin floats, never np.float64.
    command = [(float(row[0]), float(row[1]), float(row[2])) for row in u]

    stats: dict[str, float | str | list[float]] = {
        "enabled": 1.0,
        "design": "inverse2nd (C): u = r(t+tau) + (r'' + gamma r')(t+tau) / w0^2",
        "f0_hz": [float(p[0]) for p in phys_axes],
        "gamma_1_per_s": [float(p[1]) for p in phys_axes],
        "tau_sec": [float(p[2]) for p in phys_axes],
        "sg_window_samples": float(window),
        "sg_poly": float(sg_poly),
        "periodic": float(periodic),
        "max_offset_mm": float(max_offset_mm),
        "limit_strategy": limit_strategy,
        "raw_max_offset_mm": raw_max_m * 1.0e3,
        "global_scale_applied": float(global_scale),
        "observed_max_offset_mm": float(norms.max()) * 1.0e3,
        "rms_offset_mm": float(np.sqrt(np.mean(norms**2))) * 1.0e3,
        "clipped_points": float(clipped),
        "clipped_fraction": float(clipped / len(r)),
        "limited_points": float(len(r) if global_scale < 1.0 else clipped),
        "limited_fraction": float(1.0 if global_scale < 1.0 else clipped / len(r)),
    }
    return command, stats
