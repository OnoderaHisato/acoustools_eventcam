#!/usr/bin/env python3
"""B1 random-trajectory A/B overlay figures: OFF vs ON side by side.

Per OFF/ON pair from the b1_ff_ab_session_*.json, draws
  - full-run x/y/z time series (reference r dashed, measured m), 2 columns
  - 2-s zoom around the worst OFF error window (same window for both columns)
  - 3D trajectory panels
using ideal_comparison_3d/stereo_ideal_comparison.npz from each run.

Usage:
    python3 analyze_b1_ff_overlay.py <session.json> [--records-dir DIR]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401

plt.rcParams.update({"font.size": 13, "font.family": "BIZ UDGothic",
                     "axes.titlesize": 14, "axes.labelsize": 13, "legend.fontsize": 11})

AXES = "xyz"
C_R, C_OFF, C_ON = "#555555", "#1f77b4", "#208870"
T_SKIP, T_TAIL = 0.5, 0.05


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("session_json", type=Path)
    p.add_argument("--records-dir", type=Path, default=None)
    p.add_argument("--out-dir", type=Path, default=None)
    return p.parse_args()


def load_run(run_dir: Path):
    npz = np.load(run_dir / "ideal_comparison_3d" / "stereo_ideal_comparison.npz")
    t = np.asarray(npz["t_recording_sec"], dtype=float)
    m = np.asarray(npz["measured_pat_mm"], dtype=float)
    r = np.asarray(npz["ideal_pat_mm"], dtype=float)
    return t - t[0], m, r


def rms(x):
    return float(np.sqrt(np.mean(np.asarray(x) ** 2)))


def main() -> int:
    args = parse_args()
    session = json.loads(args.session_json.read_text(encoding="utf-8"))
    records_dir = (args.records_dir or args.session_json.parent).resolve()
    out_dir = (args.out_dir or records_dir / "b1_ab_analysis").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    by_key = {}
    for entry in session.get("runs", []):
        if int(entry.get("exit_code", 1)) != 0 or not entry.get("run_dir"):
            continue
        name = Path(str(entry["run_dir"]).replace("\\", "/")).name
        run_dir = records_dir / name
        if not (run_dir / "ideal_comparison_3d" / "stereo_ideal_comparison.npz").exists():
            print(f"[WARN] missing postprocess output, skipped: {name}")
            continue
        by_key[(str(entry["label"]), int(entry["pair_number"]), str(entry["condition"]))] = name

    pairs = sorted({(k[0], k[1]) for k in by_key})
    for label, pair in pairs:
        off_name = by_key.get((label, pair, "baseline"))
        on_name = by_key.get((label, pair, "inverse2nd"))
        if off_name is None or on_name is None:
            print(f"[WARN] unpaired: {label} pair {pair}")
            continue
        t_off, m_off, r_off = load_run(records_dir / off_name)
        t_on, m_on, r_on = load_run(records_dir / on_name)
        cols = [("OFF（補正なし u = r）", t_off, m_off, r_off, C_OFF),
                ("ON（SINDy 逆モデル補正）", t_on, m_on, r_on, C_ON)]
        stats = {}
        for ttl, t, m, r, col in cols:
            mask = (t >= T_SKIP) & (t <= t[-1] - T_TAIL)
            stats[ttl] = [rms((m - r)[mask, a]) for a in range(3)]

        # zoom window from worst OFF error
        mask0 = (t_off >= T_SKIP) & (t_off <= t_off[-1] - T_TAIL)
        e = np.linalg.norm((m_off - r_off), axis=1) * mask0
        k = np.convolve(e, np.ones(2000) / 2000, "same")
        ic = int(np.argmax(k))
        dt = float(np.median(np.diff(t_off)))
        half = int(1.0 / dt)
        w0 = max(0, ic - half)
        t_zoom = (t_off[w0], min(t_off[-1], t_off[w0] + 2.0))

        for tag, window in (("full", None), ("zoom", t_zoom)):
            fig, axs = plt.subplots(3, 2, figsize=(20, 9), sharex=True, sharey="row")
            for c, (ttl, t, m, r, col) in enumerate(cols):
                sel = slice(None) if window is None else \
                    slice(*np.searchsorted(t, window))
                for a in range(3):
                    ax = axs[a, c]
                    ax.plot(t[sel], r[sel, a], color=C_R, lw=1.6, ls="--", label="指令軌道 r")
                    ax.plot(t[sel], m[sel, a], color=col, lw=1.1, label="観測 m")
                    ax.grid(alpha=0.3)
                    if c == 0:
                        ax.set_ylabel(f"{AXES[a]} [mm]")
                    ax.text(0.995, 0.03, f"rms(m−r) = {stats[ttl][a]:.3f} mm",
                            transform=ax.transAxes, ha="right", va="bottom",
                            fontsize=11, color=col)
                axs[0, c].set_title(ttl)
                axs[0, c].legend(loc="upper right", ncol=2)
                axs[2, c].set_xlabel("t [s]")
            sub = "全区間" if window is None else f"{window[0]:.1f}–{window[1]:.1f} s 拡大"
            fig.suptitle(f"{label}  ペア {pair}  位置（{sub}）", fontsize=16)
            fig.tight_layout(rect=[0, 0, 1, 0.96])
            fig.savefig(out_dir / f"{label}_p{pair:02d}_{tag}.png", dpi=150)
            plt.close(fig)

        # 3D panels
        fig = plt.figure(figsize=(19, 9))
        for c, (ttl, t, m, r, col) in enumerate(cols):
            ax = fig.add_subplot(1, 2, c + 1, projection="3d")
            sel = slice(*np.searchsorted(t, (T_SKIP, t[-1] - T_TAIL)))
            ax.plot(r[sel, 0], r[sel, 1], r[sel, 2], color=C_R, lw=0.7, ls="--",
                    alpha=0.7, label="指令軌道 r")
            ax.plot(m[sel, 0], m[sel, 1], m[sel, 2], color=col, lw=0.8, alpha=0.85,
                    label="観測 m")
            rng = np.array([[r[sel, a].min(), r[sel, a].max()] for a in range(3)])
            ax.set_box_aspect(rng[:, 1] - rng[:, 0])
            ax.set_xlabel("x [mm]", labelpad=10)
            ax.set_ylabel("y [mm]", labelpad=10)
            ax.set_zlabel("z [mm]", labelpad=10)
            ax.set_title(ttl, pad=15)
            ax.legend(loc="upper left", fontsize=11)
        fig.suptitle(f"{label}  ペア {pair}  3D 軌道", fontsize=16)
        fig.tight_layout(rect=[0, 0, 1, 0.95])
        fig.savefig(out_dir / f"{label}_p{pair:02d}_3d.png", dpi=150)
        plt.close(fig)
        print(f"[B1-ff] figures written for {label} pair {pair}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
