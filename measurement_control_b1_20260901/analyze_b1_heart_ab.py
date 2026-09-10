#!/usr/bin/env python3
"""B1 heart A/B: does the heart render cleanly with the inverse-2nd feedforward ON?

Reads the b1_heart_ab_session_*.json and, per run, the postprocessed
ideal_comparison_3d/stereo_ideal_comparison.npz.  Reports rms(m - r) per axis
and draws the direct visual answer: measured trajectory in the heart (x-z)
plane over the reference heart, OFF and ON side by side per pair.

Usage:
    python3 analyze_b1_heart_ab.py <session.json> [--records-dir DIR]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams.update({"font.size": 13, "font.family": "BIZ UDGothic",
                     "axes.titlesize": 14, "axes.labelsize": 13, "legend.fontsize": 11})

AXES = "xyz"
C_R, C_OFF, C_ON = "#555555", "#1f77b4", "#208870"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("session_json", type=Path)
    p.add_argument("--records-dir", type=Path, default=None)
    p.add_argument("--t-skip", type=float, default=0.15,
                   help="Skip this many seconds (transient; 1.5 cycles at 10 Hz).")
    p.add_argument("--t-tail", type=float, default=0.02)
    p.add_argument("--out-dir", type=Path, default=None)
    return p.parse_args()


def load_run(run_dir: Path):
    npz = np.load(run_dir / "ideal_comparison_3d" / "stereo_ideal_comparison.npz")
    t = np.asarray(npz["t_recording_sec"], dtype=float)
    m = np.asarray(npz["measured_pat_mm"], dtype=float)
    r = np.asarray(npz["ideal_pat_mm"], dtype=float)
    return t - t[0], m, r


def metrics(t, m, r, t_skip, t_tail):
    mask = (t >= t_skip) & (t <= t[-1] - t_tail)
    err = m[mask] - r[mask]
    vec = np.linalg.norm(err, axis=1)
    out = {"n": int(mask.sum()), "duration_sec": float(t[-1]),
           "rms_vec_mm": float(np.sqrt(np.mean(vec ** 2))),
           "max_vec_mm": float(vec.max())}
    for a in range(3):
        out[f"rms_{AXES[a]}_mm"] = float(np.sqrt(np.mean(err[:, a] ** 2)))
    return out, mask


def main() -> int:
    args = parse_args()
    session = json.loads(args.session_json.read_text(encoding="utf-8"))
    records_dir = (args.records_dir or args.session_json.parent).resolve()
    out_dir = (args.out_dir or records_dir / "b1_heart_analysis").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    runs = {}
    for entry in session.get("runs", []):
        if int(entry.get("exit_code", 1)) != 0 or not entry.get("run_dir"):
            continue
        name = Path(str(entry["run_dir"]).replace("\\", "/")).name
        run_dir = records_dir / name
        if not (run_dir / "ideal_comparison_3d" / "stereo_ideal_comparison.npz").exists():
            print(f"[WARN] missing postprocess output, skipped: {name}")
            continue
        t, m, r = load_run(run_dir)
        st, mask = metrics(t, m, r, args.t_skip, args.t_tail)
        runs[(int(entry["repeat_number"]), str(entry["condition"]))] = {
            "name": name, "t": t, "m": m, "r": r, "mask": mask, "metrics": st,
            "sequence_index": int(entry["sequence_index"]),
        }
        print(name, {k: round(v, 3) for k, v in st.items() if isinstance(v, float)})

    pairs = sorted({k[0] for k in runs})
    if not pairs:
        raise SystemExit("No postprocessed heart runs found.")

    # ---- main visual: x-z plane, OFF | ON columns, one row per pair --------
    fig, axs = plt.subplots(len(pairs), 2, figsize=(11, 5.2 * len(pairs)),
                            squeeze=False, sharex=True, sharey=True)
    for i, pair in enumerate(pairs):
        for c, cond in enumerate(("baseline", "inverse2nd")):
            ax = axs[i, c]
            run = runs.get((pair, cond))
            if run is None:
                ax.set_axis_off()
                continue
            t, m, r, mask, st = run["t"], run["m"], run["r"], run["mask"], run["metrics"]
            col = C_OFF if cond == "baseline" else C_ON
            ax.plot(r[:, 0], r[:, 2], color=C_R, lw=2.0, ls="--", label="参照ハート r")
            ax.plot(m[mask, 0], m[mask, 2], color=col, lw=0.9, alpha=0.85,
                    label="観測 m（全周回）")
            ax.set_aspect("equal")
            ax.grid(alpha=0.3)
            ttl = "OFF（補正なし u = r）" if cond == "baseline" else "ON（SINDy 逆モデル補正）"
            ax.set_title(f"ペア {pair}  {ttl}")
            ax.text(0.99, 0.01,
                    f"rms(m−r): x {st['rms_x_mm']:.3f} / z {st['rms_z_mm']:.3f} mm\n"
                    f"ベクトル {st['rms_vec_mm']:.3f} mm",
                    transform=ax.transAxes, ha="right", va="bottom", fontsize=11, color=col)
            ax.set_xlabel("x [mm]")
            if c == 0:
                ax.set_ylabel("z [mm]")
            ax.legend(loc="upper left", fontsize=10)
    fig.suptitle("ハート軌道 7 mm・10 Hz  OFF/ON 比較（x–z 平面、全周回重ね描き）", fontsize=15)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(out_dir / "heart_xz_off_on.png", dpi=160)
    plt.close(fig)

    # ---- time series x/z, OFF vs ON columns, one figure per pair -----------
    for pair in pairs:
        fig, axs = plt.subplots(2, 2, figsize=(16, 7), sharex=True, sharey="row")
        for c, cond in enumerate(("baseline", "inverse2nd")):
            run = runs.get((pair, cond))
            if run is None:
                continue
            t, m, r, st = run["t"], run["m"], run["r"], run["metrics"]
            col = C_OFF if cond == "baseline" else C_ON
            for a_i, a in enumerate((0, 2)):
                ax = axs[a_i, c]
                ax.plot(t, r[:, a], color=C_R, lw=1.6, ls="--", label="参照 r")
                ax.plot(t, m[:, a], color=col, lw=1.1, label="観測 m")
                ax.axvspan(0, args.t_skip, color="gold", alpha=0.15)
                ax.grid(alpha=0.3)
                if c == 0:
                    ax.set_ylabel(f"{AXES[a]} [mm]")
                ax.text(0.995, 0.03, f"rms(m−r) = {st[f'rms_{AXES[a]}_mm']:.3f} mm",
                        transform=ax.transAxes, ha="right", va="bottom", fontsize=11, color=col)
            ttl = "OFF（補正なし）" if cond == "baseline" else "ON（SINDy 逆モデル補正）"
            axs[0, c].set_title(ttl)
            axs[0, c].legend(loc="upper right", ncol=2, fontsize=10)
            axs[1, c].set_xlabel("t [s]")
        fig.suptitle(f"ハート軌道 ペア {pair}  時系列（黄 = 評価から除外した助走区間）", fontsize=15)
        fig.tight_layout(rect=[0, 0, 1, 0.95])
        fig.savefig(out_dir / f"heart_timeseries_pair{pair}.png", dpi=160)
        plt.close(fig)

    # ---- summary ------------------------------------------------------------
    lines = ["# B1 ハート軌道 OFF/ON 結果", "",
             f"7 mm・10 Hz・10 周回（1 s）。評価窓 t ≥ {args.t_skip} s。",
             "", "| ペア | 軸 | OFF rms [mm] | ON rms [mm] | 改善率 [%] |", "|--|--|--:|--:|--:|"]
    summary = {"t_skip": args.t_skip, "pairs": {}}
    for pair in pairs:
        off = runs.get((pair, "baseline"))
        on = runs.get((pair, "inverse2nd"))
        if off is None or on is None:
            continue
        summary["pairs"][pair] = {"off": off["metrics"], "on": on["metrics"], "impr_pct": {}}
        for key in [f"rms_{a}_mm" for a in AXES] + ["rms_vec_mm"]:
            o, n = off["metrics"][key], on["metrics"][key]
            impr = 100.0 * (1.0 - n / o) if o > 0 else float("nan")
            summary["pairs"][pair]["impr_pct"][key] = impr
            lines.append(f"| {pair} | {key.replace('rms_', '').replace('_mm', '')} "
                         f"| {o:.4f} | {n:.4f} | {impr:+.1f} |")
    (out_dir / "heart_summary.json").write_text(
        json.dumps(summary, indent=1, ensure_ascii=False, default=float), encoding="utf-8")
    (out_dir / "HEART_AB_SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"[B1-heart] Analysis written to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
