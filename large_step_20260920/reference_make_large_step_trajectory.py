#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""水平の飽和長 vxr（= 水平の力の上限 A_r = k_x / vxr）を決めるための大振幅ステップ指令。

  1 軸だけを 0 → +S → 0 → −S → 0 と動かす（1 ジャンプは S。+S → −S の 2S ジャンプはしない）。3 周 = 12 ジャンプ、ホールド 0.4 s、5.8 s。
  1 mm 以下のステップでは sin の飽和が弱く、ar と vxr は積 k_x = ar·vxr しか決まらない。1.5–2 mm で初めて分かれる。
  取得側は `staircase` 経路（振幅のみ検査、1 ジャンプ < 脱出境界 2.144 mm）。2 mm の水平ステップは 8 月、1.7 mm は 9/16 に保持済み。
  同定は ../vzr_step/vzr_step_fit.py（z を動かさない記録では水平の段だけ当てる）。
"""
import argparse, json
import numpy as np
from pathlib import Path
HERE = Path(__file__).resolve().parent
LEVELS = {"L12": 1.2, "L15": 1.5, "L18": 1.8, "L20": 2.0}

def build(s, axis="x", hold=0.4, cycles=3, init_hold=0.5, final_hold=0.5, fs=1e4):
    seq = [1, 0, -1, 0] * cycles; n0, nh, nf = int(init_hold * fs), int(hold * fs), int(final_hold * fs)
    u = np.zeros((n0 + nh * (len(seq) - 1) + nf, 3)); i = "xyz".index(axis)
    for k, a in enumerate(seq): u[n0 + k * nh:, i] = a * s
    assert np.abs(u[[0, -1]]).max() == 0; return u

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--out", type=Path, default=HERE / "commands"); g = ap.parse_args(); g.out.mkdir(parents=True, exist_ok=True)
    for axis in "xy":
        for lab, s in LEVELS.items():
            u = build(s, axis); name = f"vzrstep_{axis.upper()}{lab}_{axis}{s:g}_z0"      # エクスポータが vzrstep_* を読むので接頭辞を合わせる
            params = dict(horizontal_axis=axis, S_h_mm=s, S_z_mm=0.0, hold_sec=0.4, blocks=3, stagger_ms=0.0, purpose="horizontal_saturation_length",
                          n_jumps=int((np.abs(np.diff(u, axis=0)).sum(1) > 0).sum()))
            np.savez_compressed(g.out / f"{name}.npz", positions_mm=u, time_sec=np.arange(len(u)) / 1e4, sample_hz=1e4, params=json.dumps(params))
            print(f"{name:26} {len(u) / 1e4:4.1f} s  ジャンプ {params['n_jumps']}  1 ジャンプ {s} mm（脱出境界 2.144 mm の {100 * s / 2.144:.0f}%）")

if __name__ == "__main__":
    main()
