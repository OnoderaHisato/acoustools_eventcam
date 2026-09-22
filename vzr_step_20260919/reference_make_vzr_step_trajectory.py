#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ステップ型 vzr 指令列: 水平 1 軸（x か y）と z のジャンプを 1 記録に混ぜる。

1 ブロック = 10 ジャンプ（水平単独 2、z 単独 2、同時 6）。水平を h、鉛直を z として (h, z) の順に
  (0,0)→(+,0)→(+,+)→(0,0)→(−,−)→(−,0)→(0,0)→(+,−)→(0,0)→(−,+)→(0,0)
単独ジャンプが 5 パラメータ（ar, vxr, gam / az, vz, gamz）を、同時ジャンプが vzr を決める。全部が同じ記録
（同じ熱状態）に入るので、ドループによる固定値のずれが原理的に起きない。
ホールド中は指令一定なので、同定は純遅延 τ0 にもクロック差にも依らない（vzr_step_fit.py）。

取得側は `staircase` 経路（振幅のみ検査）で流す。滑らかな指令用の `imported` 経路は加速度 100k mm/s² で
落とすので、vzr に必要な |Δz| ≥ 0.1 mm（粒子加速度 ≥ 330k mm/s²）は滑らかな指令では作れない。
"""
import argparse, json
import numpy as np
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEQ = [(1, 0), (1, 1), (0, 0), (-1, -1), (-1, 0), (0, 0), (1, -1), (0, 0), (-1, 1), (0, 0)]
LEVELS = {"S1": (0.6, 0.4), "S2": (0.8, 0.5), "S3": (1.0, 0.6)}


def build(sh, sz, horiz="x", hold=0.3, blocks=3, init_hold=0.5, final_hold=0.5, fs=10000.0, stagger_ms=0.0):
    ih = "xyz".index(horiz); n0 = int(round(init_hold * fs)); nh = int(round(hold * fs)); nf = int(round(final_hold * fs))
    seq = SEQ * blocks; n = n0 + nh * (len(seq) - 1) + nf; u = np.zeros((n, 3)); ns = int(round(stagger_ms * 1e-3 * fs))
    jumps = []; prev = (0, 0)
    for k, (a, b) in enumerate(seq):
        lo = n0 + k * nh
        u[lo:, ih] = a * sh; u[lo + (ns if (a != prev[0] and b != prev[1]) else 0):, 2] = b * sz
        kind = "both" if (a != prev[0] and b != prev[1]) else ("h" if a != prev[0] else "z")
        jumps.append(dict(sample=int(lo), time_sec=lo / fs, kind=kind, to=[a * sh, b * sz])); prev = (a, b)
    assert np.abs(u[[0, -1]]).max() == 0.0
    return u, jumps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=HERE / "commands"); ap.add_argument("--hold", type=float, default=0.3)
    ap.add_argument("--blocks", type=int, default=3); ap.add_argument("--stagger-ms", type=float, default=0.0)
    a = ap.parse_args(); a.out.mkdir(parents=True, exist_ok=True); rows = []
    for horiz in "xy":
        for lab, (sh, sz) in LEVELS.items():
            u, jumps = build(sh, sz, horiz, a.hold, a.blocks, stagger_ms=a.stagger_ms)
            name = f"vzrstep_{horiz.upper()}{lab}_{horiz}{sh:g}_z{sz:g}" + (f"_stag{a.stagger_ms:g}ms" if a.stagger_ms else "")
            params = dict(horizontal_axis=horiz, S_h_mm=sh, S_z_mm=sz, hold_sec=a.hold, blocks=a.blocks, stagger_ms=a.stagger_ms,
                          n_jumps=len(jumps), n_both=sum(j["kind"] == "both" for j in jumps), sequence=SEQ, jumps=jumps)
            np.savez_compressed(a.out / f"{name}.npz", positions_mm=u, time_sec=np.arange(len(u)) / 1e4, sample_hz=1e4, params=json.dumps(params))
            step = np.linalg.norm(np.diff(u, axis=0), axis=1).max()
            rows.append(f"{name:34} {len(u) / 1e4:5.1f} s  ジャンプ {len(jumps)}（同時 {params['n_both']}）  最大ジャンプ {step:.3f} mm  最大オフセット {np.linalg.norm(u, axis=1).max():.3f} mm")
    print("\n".join(rows))


if __name__ == "__main__":
    main()
