#!/usr/bin/env python3
"""Aggregate the B1 OFF/ON feedforward A/B results after stereo postprocessing.

Input: the b1_ff_ab_session_*.json written by acoustools_stereo_b1_ff_ab.py and
a directory that contains the (postprocessed) run directories, each holding
ideal_comparison_3d/stereo_ideal_comparison.npz.

Per run: rms(measured - reference) per axis and vector norm over t >= 0.5 s
(0.05 s tail dropped), matching the offline simulation window.  Pairs OFF/ON
runs by (trajectory label, pair number) and reports paired improvements.

Usage:
    python compare_b1_ff_ab.py <session.json> [--records-dir DIR] [--t-skip 0.5]
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

import numpy as np

AXES = "xyz"
PREDICTED_IMPR = (26.0, 28.0, 43.0)  # % from feedforward_inverse_sim design C (ringdown physics)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_json", type=Path)
    parser.add_argument(
        "--records-dir",
        type=Path,
        default=None,
        help="Directory holding the run directories (default: the session JSON's directory).",
    )
    parser.add_argument("--t-skip", type=float, default=0.5, help="Skip this many seconds at the start.")
    parser.add_argument("--t-tail", type=float, default=0.05, help="Drop this many seconds at the end.")
    parser.add_argument("--escape-mm", type=float, default=2.0, help="Vector error above this flags a likely escape.")
    parser.add_argument("--out-dir", type=Path, default=None, help="Output directory (default: <records-dir>/b1_ab_analysis).")
    return parser.parse_args()


def run_metrics(npz_path: Path, t_skip: float, t_tail: float, escape_mm: float) -> dict:
    data = np.load(npz_path)
    t = np.asarray(data["t_recording_sec"], dtype=float)
    measured = np.asarray(data["measured_pat_mm"], dtype=float)
    ideal = np.asarray(data["ideal_pat_mm"], dtype=float)
    t_rel = t - t[0]
    mask = (t_rel >= t_skip) & (t_rel <= t_rel[-1] - t_tail)
    if mask.sum() < 100:
        raise ValueError(f"too few samples after windowing: {npz_path}")
    err = measured[mask] - ideal[mask]
    vec = np.linalg.norm(err, axis=1)
    dt_med = float(np.median(np.diff(t_rel)))
    coverage = len(t_rel) * dt_med / float(t_rel[-1] - t_rel[0] + dt_med)
    out = {
        "n_samples": int(mask.sum()),
        "duration_sec": float(t_rel[-1]),
        "coverage": coverage,
        "rms_vec_mm": float(np.sqrt(np.mean(vec**2))),
        "max_vec_mm": float(vec.max()),
        "escape_suspected": bool(vec.max() > escape_mm),
    }
    for a in range(3):
        out[f"rms_{AXES[a]}_mm"] = float(np.sqrt(np.mean(err[:, a] ** 2)))
    return out


def main() -> int:
    args = parse_args()
    session = json.loads(args.session_json.read_text(encoding="utf-8"))
    records_dir = (args.records_dir or args.session_json.parent).resolve()
    out_dir = (args.out_dir or records_dir / "b1_ab_analysis").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for run in session.get("runs", []):
        if int(run.get("exit_code", 1)) != 0 or not run.get("run_dir"):
            continue
        run_name = Path(str(run["run_dir"]).replace("\\", "/")).name
        run_dir = records_dir / run_name
        npz = run_dir / "ideal_comparison_3d" / "stereo_ideal_comparison.npz"
        if not npz.exists():
            print(f"[WARN] missing postprocess output, skipped: {npz}")
            continue
        try:
            metrics = run_metrics(npz, args.t_skip, args.t_tail, args.escape_mm)
        except ValueError as exc:
            print(f"[WARN] {exc}")
            continue
        rows.append(
            {
                "label": run["label"],
                "pair": int(run["pair_number"]),
                "condition": run["condition"],
                "sequence_index": int(run["sequence_index"]),
                "run_dir": run_name,
                **metrics,
            }
        )
    if not rows:
        raise SystemExit("No postprocessed runs found; run the stereo postprocess chain first.")

    fields = list(rows[0].keys())
    with (out_dir / "per_run_metrics.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    # Pair OFF/ON by (label, pair number)
    index = {(r["label"], r["pair"], r["condition"]): r for r in rows}
    pairs = []
    for r in rows:
        if r["condition"] != "baseline":
            continue
        on = index.get((r["label"], r["pair"], "inverse2nd"))
        if on is None:
            print(f"[WARN] unpaired baseline run: {r['label']} pair {r['pair']}")
            continue
        entry = {"label": r["label"], "pair": r["pair"]}
        for key in [f"rms_{a}_mm" for a in AXES] + ["rms_vec_mm"]:
            entry[f"off_{key}"] = r[key]
            entry[f"on_{key}"] = on[key]
            entry[f"impr_{key.replace('rms_', '').replace('_mm', '')}_pct"] = (
                100.0 * (1.0 - on[key] / r[key]) if r[key] > 0 else float("nan")
            )
        entry["off_escape"] = r["escape_suspected"]
        entry["on_escape"] = on["escape_suspected"]
        pairs.append(entry)
    if not pairs:
        raise SystemExit("No complete OFF/ON pairs found.")
    with (out_dir / "paired_improvements.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(pairs[0].keys()))
        writer.writeheader()
        writer.writerows(pairs)

    # Summary: paired statistics per axis + vector
    summary = {"n_pairs": len(pairs), "predicted_impr_pct_xyz": list(PREDICTED_IMPR)}
    lines = [
        "# B1 前置補正 ON/OFF 試験の結果集計",
        "",
        f"ペア数 {len(pairs)}（各ペア = 同一軌道・隣接収録の OFF/ON）。窓 t >= {args.t_skip} s。",
        "",
        "| 軸 | OFF rms [mm] | ON rms [mm] | 改善率 平均 [%] | 中央値 [%] | 最悪ペア [%] | 改善ペア数 | 事前予測 [%] |",
        "|--|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for key, pred in zip(list(AXES) + ["vec"], list(PREDICTED_IMPR) + [None]):
        impr = np.array([p[f"impr_{key}_pct"] for p in pairs], dtype=float)
        off = np.array([p[f"off_rms_{key}_mm"] for p in pairs], dtype=float)
        on = np.array([p[f"on_rms_{key}_mm"] for p in pairs], dtype=float)
        # paired t on the rms differences
        diff = off - on
        tval = float(diff.mean() / (diff.std(ddof=1) / math.sqrt(len(diff)))) if len(diff) > 1 else float("nan")
        summary[key] = {
            "off_rms_mean_mm": float(off.mean()),
            "on_rms_mean_mm": float(on.mean()),
            "impr_mean_pct": float(impr.mean()),
            "impr_median_pct": float(np.median(impr)),
            "impr_min_pct": float(impr.min()),
            "n_improved": int((impr > 0).sum()),
            "paired_t": tval,
        }
        lines.append(
            "| %s | %.4f | %.4f | %+.1f | %+.1f | %+.1f | %d/%d | %s |"
            % (
                key,
                off.mean(),
                on.mean(),
                impr.mean(),
                float(np.median(impr)),
                impr.min(),
                int((impr > 0).sum()),
                len(pairs),
                "-" if pred is None else f"{pred:.0f}",
            )
        )
    escapes_off = sum(bool(p["off_escape"]) for p in pairs)
    escapes_on = sum(bool(p["on_escape"]) for p in pairs)
    summary["escape_suspected"] = {"off": escapes_off, "on": escapes_on}
    lines += [
        "",
        f"脱出疑い（最大ベクトル誤差 > {args.escape_mm:g} mm）: OFF {escapes_off} / ON {escapes_on} ペア。",
        "",
        "OFF/ON ともに評価は measured − r（r = 元の指令軌道、ideal_log.csv）。ON の指令 u は評価に使わない。",
    ]
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    (out_dir / "B1_AB_SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))

    # Paired plot
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, axs = plt.subplots(1, 4, figsize=(13, 3.6), sharey=False)
        for i, key in enumerate(list(AXES) + ["vec"]):
            ax = axs[i]
            for p in pairs:
                ax.plot([0, 1], [p[f"off_rms_{key}_mm"], p[f"on_rms_{key}_mm"]], "o-", color="#1f77b4", alpha=0.5, ms=4)
            ax.set_xticks([0, 1])
            ax.set_xticklabels(["OFF", "ON"])
            ax.set_title(key)
            ax.grid(alpha=0.3)
            if i == 0:
                ax.set_ylabel("rms(m - r) [mm]")
        fig.suptitle("B1 feedforward A/B: paired rms per trajectory")
        fig.tight_layout()
        fig.savefig(out_dir / "paired_rms.png", dpi=160)
    except Exception as exc:  # matplotlib may be absent on the acquisition PC
        print(f"[WARN] plot skipped: {exc}")
    print(f"[B1] Analysis written to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
