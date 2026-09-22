#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面外 y の外乱を「共振で増幅された応答」から逆算して補償指令を作り直す（7 Hz、ハート）。

  2026-09-20 の OFF と OFFW は、y の指令だけが既知の量 Δu_y = −w_slow,y だけ違う。応答の差から伝達関数を実験的に求める:
      G_y(n) = (M_OFFW,y − M_OFF,y) / Δu_y(n)
  補償なしの y の揺れは本当のずれ W_y が G_y で増幅されたものなので  W_y(n) = M_OFF,y(n) / G_y(n)。
  共振帯では応答が約 10 倍に増幅されるので、ゆっくり回して測るより 10 倍よく見え、位置に貼りついた測定の偏りの影響を受けにくい。
  波数 4 以下（なだらかな成分）は、ゆっくり測った値が効いていた（y <35 Hz が 0.13 → 0.04）のでそのまま使う。
  出力: heart_s7_f7_OFFW2 / heart_s7_f7_CW2（x, z は前回と同じ、y だけ差し替え）。
"""
import sys, json
import numpy as np
from pathlib import Path
HERE = Path(__file__).resolve().parent; FF = HERE.parent / "ff_heart"; sys.path.insert(0, str(HERE)); import joint_w_estimate as J
a = J.a; MIN_PROBE = 0.012          # 伝達関数を信用する最小の |Δu_y|（片側振幅 [mm]）

def main(root):
    root = Path(root); ks = json.load(open(root / "kcheck_summary.json")); g = lambda pat: J.load(next(root.glob(pat)), ks)
    OFF, OFFW = g("*f7_OFF_scale100_20260920*"), g("*f7_OFFW_*"); wf = np.load(FF / "w_field_heart_xz.npz", allow_pickle=True); Wc = wf["Wc"].copy(); nmax = int(wf["nmax"])
    Wy = Wc[:, 1].copy(); used = []
    for n in range(5, nmax + 1):
        du = -Wc[n, 1]
        if abs(du) < MIN_PROBE: Wy[n] = 0.0; continue
        G = (OFFW["M"][n, 1] - OFF["M"][n, 1]) / du
        if abs(G) < 1.0: Wy[n] = 0.0; continue          # 共振より下で |G| < 1 は物理的にありえない = その波数は測れていない
        Wy[n] = OFF["M"][n, 1] / G; used.append((n, abs(G), 2 * abs(Wc[n, 1]), 2 * abs(Wy[n])))
    print(" n   |G_y|   ゆっくり測った |w_y|   応答から逆算した |W_y| [mm]"); [print(f"{n:2d}  {G:6.2f}       {ws:.3f}                {wd:.3f}") for n, G, ws, wd in used]
    Wc2 = Wc.copy(); Wc2[:, 1] = Wy; np.savez(HERE / "w_heart_xz_ydynamic.npz", Wc=Wc2, nmax=nmax)
    def w_of(theta): n = np.arange(nmax + 1)[:, None, None]; return 2 * np.real((Wc2[:, None, :] * np.exp(1j * n * theta[None, :, None])).sum(0))
    for src, out, tau in (("heart_s7_f7_OFF", "heart_s7_f7_OFFW2", 0.0), ("heart_s7_f7_C_delay_inverse", "heart_s7_f7_CW2", 0.8e-3)):
        d = np.load(FF / f"{src}.npz", allow_pickle=True); u = d["positions_mm"].astype(float); r = d["reference_mm"].astype(float); t = d["time_pat_sec"]; fs = 1e4
        env = np.ones(len(t)); nr = int(0.5 * fs); ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(nr) / nr); env[:nr] = ramp; env[-nr:] = ramp[::-1]
        u2 = u - w_of(2 * np.pi * 7.0 * (t + tau)) * env[:, None]; v = np.gradient(u2, 1 / fs, axis=0); ac = np.gradient(v, 1 / fs, axis=0)
        p = json.loads(str(d["params"])); p.update(w_compensation=dict(axes="xyz", y_source="dynamic (OFF/OFFW pair 2026-09-20)", nmax=nmax))
        np.savez_compressed(FF / f"{out}.npz", positions_mm=u2, reference_mm=r, time_pat_sec=t, time_cam_expected_sec=d["time_cam_expected_sec"], sample_hz=1e4, design=out.split("_f7_")[1], params=json.dumps(p))
        np.savetxt(FF / f"{out}.csv", np.column_stack([t, u2, r, d["time_cam_expected_sec"]]), delimiter=",", fmt="%.6f", header="time_pat_sec,ux_mm,uy_mm,uz_mm,rx_mm,ry_mm,rz_mm,time_cam_expected_sec", comments="")
        print(f"{out:22} 端点 {np.abs(u2[[0, -1]]).max():.4f}  最大オフセット {np.linalg.norm(u2, axis=1).max():.2f} mm  max v {np.linalg.norm(v, axis=1).max():.0f} mm/s  max a {np.linalg.norm(ac, axis=1).max() / 1e3:.1f}k mm/s²  max|u−r| {np.linalg.norm(u2 - r, axis=1).max():.3f} mm  y 振幅 {np.ptp(u2[:, 1]):.2f} mm")

if __name__ == "__main__":
    main(sys.argv[1])
