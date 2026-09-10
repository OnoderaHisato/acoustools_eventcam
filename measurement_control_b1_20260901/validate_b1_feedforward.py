#!/usr/bin/env python3
"""Local validation of inverse2nd_feedforward against the offline simulation.

Run from the project root (needs Acoutools_eventcam/ data and
feedforward_inverse_sim.py).  Checks, per orig long_random run:
  1. numpy-only SG derivatives match scipy.signal.savgol_filter (interior)
  2. the module's u_ff matches feedforward_inverse_sim design C + clip
  3. linear-superposition evaluation reproduces the predicted improvement
  4. safety: max |u-r|, clip fraction, max speed / acceleration of u vs r
"""

import pathlib
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "Acoutools_eventcam"))

import os

os.chdir(ROOT / "Acoutools_eventcam")

from scipy.signal import savgol_filter

from feedforward_inverse_sim import (
    DT,
    PHYS_RING,
    T_SKIP,
    T_TAIL,
    clip_correction,
    design,
    load_runs,
    plant,
    rms,
    simulate,
)
from inverse2nd_feedforward import apply_inverse2nd_feedforward, savgol_deriv

FS = 1.0 / DT
PHYS_AXES = tuple(PHYS_RING[a] for a in range(3))


def main():
    # --- 1. SG implementation check
    rng = np.random.default_rng(0)
    x = np.cumsum(rng.standard_normal(5000)) * 0.01
    for deriv in (1, 2):
        mine = savgol_deriv(x, 51, 3, deriv, DT)
        ref = savgol_filter(x, 51, 3, deriv=deriv, delta=DT)
        err = np.abs(mine[100:-100] - ref[100:-100]).max()
        scale = np.abs(ref[100:-100]).max()
        print(f"SG deriv={deriv}: max interior diff {err:.3e} (signal scale {scale:.3e})")
        assert err < 1e-8 * max(scale, 1.0), "SG mismatch"

    runs = [r for r in load_runs() if r[1] == "stereo_acoustools_3d_records_auto"]
    print(f"runs: {len(runs)}")
    G = {a: plant(*PHYS_RING[a]) for a in range(3)}
    pooled0 = [[], [], []]
    pooledC = [[], [], []]
    worst_u_diff = 0.0
    for name, fam, m, r in runs:
        n = len(m)
        sl = slice(int(T_SKIP / DT), n - int(T_TAIL / DT))
        # reference design C (scipy path, mm)
        uref = np.stack([design("C", r[:, a], PHYS_RING[a]) for a in range(3)], 1)
        uref, clipfrac_ref, cmax_ref = clip_correction(uref, r)
        # module under test (metres in/out)
        u_list, stats = apply_inverse2nd_feedforward(
            [tuple(row) for row in (r * 1e-3)], sample_hz=FS, phys_axes=PHYS_AXES,
            max_offset_mm=1.0, limit_strategy="pointwise_clip", sg_window_ms=5.0,
        )
        u = np.asarray(u_list) * 1e3
        interior = slice(200, n - 200)
        udiff = np.abs(u[interior] - uref[interior]).max()
        worst_u_diff = max(worst_u_diff, udiff)
        # improvement via linear superposition
        e0 = m - r
        impr = []
        for a in range(3):
            eff = e0[:, a] + simulate(G[a], u[:, a] - r[:, a])
            pooled0[a].append(e0[sl, a])
            pooledC[a].append(eff[sl])
            impr.append(100 * (1 - rms(eff[sl]) / rms(e0[sl, a])))
        # safety: speed / accel of u vs r. SG(5 ms) derivatives, because the
        # locally reconstructed r is piecewise linear (interpolated ideal log)
        # and raw double differences only measure that artifact.  The machine
        # generates r analytically smooth at 10 kHz.
        def kin(v):
            v1 = savgol_filter(v, 51, 3, deriv=1, delta=DT, axis=0)
            v2 = savgol_filter(v, 51, 3, deriv=2, delta=DT, axis=0)
            return np.linalg.norm(v1, axis=1).max(), np.linalg.norm(v2, axis=1).max()

        sp_r, ac_r = kin(r)
        sp_u, ac_u = kin(u)
        print(
            f"{name[:44]:44s} u-diff {udiff:.2e} mm | max|u-r| {stats['observed_max_offset_mm']:.3f} "
            f"(clip {100 * stats['clipped_fraction']:.2f}%) | impr {impr[0]:+.1f}/{impr[1]:+.1f}/{impr[2]:+.1f}% "
            f"| speed {sp_r:.0f}->{sp_u:.0f} mm/s | accel {ac_r / 1e3:.1f}->{ac_u / 1e3:.1f} km/s^2"
        )
    pooled_impr = [
        100 * (1 - rms(np.concatenate(pooledC[a])) / rms(np.concatenate(pooled0[a])))
        for a in range(3)
    ]
    print(f"\nworst |u_module - u_sim| interior: {worst_u_diff:.2e} mm")
    print(
        "pooled improvement x/y/z: "
        + "/".join(f"{v:+.1f}%" for v in pooled_impr)
        + "  (offline prediction ~ +26/+28/+43%)"
    )


if __name__ == "__main__":
    main()
