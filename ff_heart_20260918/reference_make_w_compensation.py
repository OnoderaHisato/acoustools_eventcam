#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""位置依存の外乱 w を準静的な記録（1 Hz / 2 Hz の補償なし、XZ 面）から取り出し、10 Hz の指令から引いた指令列を作る。

  w(θ) = 実測のループ平均 − 所望（一定の時間ずれと平均オフセットを除く）。1 Hz と 2 Hz の複素スペクトルを平均し、波数 n ≤ NMAX だけ残す。
  トラップの実位置は u + w(u) なので、u′ = u − w(所望位置) とすれば u′ + w ≈ u。
  出力: heart_s7_f<Hz>_OFFW（r − w）、heart_s7_f<Hz>_CW（C − w）。y（面外）成分も引く。10 Hz は元の指令が 93k mm/s² で上限 100k に余裕が無く、
  w を足すと 104k–125k になるので 7 Hz（46k）で試す。7 Hz の共振帯は波数 9–13。

使い方: python3 make_w_compensation.py <root> [--nmax 12]
"""
import sys, re, json, argparse
import numpy as np
from pathlib import Path
from scipy.interpolate import interp1d
HERE = Path(__file__).resolve().parent; sys.path.insert(0, str(HERE)); import analyze_ff_final as a

def w_spectrum(run):
    d = np.load(run / "reference_comparison_3d" / "stereo_ideal_comparison.npz", allow_pickle=True)
    t, m, r = d["t_ideal_sec"].astype(float), d["measured_pat_mm"].astype(float), d["ideal_pat_mm"].astype(float)
    a.set_loop(run.name, t.max()); rfun = interp1d(t, r, axis=0, bounds_error=False, fill_value=(r[0], r[-1])); t = t + a.led_origin_late_sec(run)
    w = (t >= a.T0) & (t < a.T1) & np.isfinite(m).all(1); M, _ = a.fold(t[w], m[w]); R, _ = a.fold(t[w], rfun(t[w] / (1 + a.EPS))); _, _, e = a.best_lag(M, R)
    return np.fft.rfft(e, axis=0) / a.NB                     # 波数 n の複素振幅（片側、×2 で振幅）

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("root"); ap.add_argument("--nmax", type=int, default=13); ap.add_argument("--freq", type=int, default=7); g = ap.parse_args(); root = Path(g.root)
    runs = [r for r in sorted(root.glob("feedforward_validation_*_OFF_*")) if re.search(r"_f[12]_OFF_scale", r.name)]
    assert len(runs) >= 2, "XZ 面の 1 Hz / 2 Hz の補償なしが要る"; C = [w_spectrum(r)[: g.nmax + 1] for r in runs]
    agree = [float(np.abs(C[0][n] - C[1][n]).max() / max(np.abs(C[0][n]).max(), 1e-9)) for n in range(1, g.nmax + 1)]
    Wc = np.mean(C, 0); Wc[0] = 0
    def w_of(theta):                                          # θ [rad] → w [mm]（3 成分）
        n = np.arange(g.nmax + 1)[:, None, None]; return 2 * np.real((Wc[:, None, :] * np.exp(1j * n * theta[None, :, None])).sum(0))
    th = np.linspace(0, 2 * np.pi, 2000, endpoint=False); ww = w_of(th)
    print(f"w の rms [mm]: x {ww[:, 0].std():.3f}  y {ww[:, 1].std():.3f}  z {ww[:, 2].std():.3f}   最大 |w| {np.linalg.norm(ww, axis=1).max():.3f}   （n ≤ {g.nmax}、{len(runs)} 本の平均）")
    print("1 Hz と 2 Hz の食い違い（波数ごと、相対）:", np.round(agree, 2))
    np.savez(HERE / "w_field_heart_xz.npz", Wc=Wc, nmax=g.nmax, theta=th, w_mm=ww, source_runs=[r.name for r in runs])
    for src, out in ((f"heart_s7_f{g.freq}_OFF", f"heart_s7_f{g.freq}_OFFW"), (f"heart_s7_f{g.freq}_C_delay_inverse", f"heart_s7_f{g.freq}_CW")):
        d = np.load(HERE / f"{src}.npz", allow_pickle=True); u = d["positions_mm"].astype(float); r = d["reference_mm"].astype(float); t = d["time_pat_sec"]; fs = 1e4
        tau = 0.0 if src.endswith("OFF") else 0.8e-3; theta = 2 * np.pi * float(g.freq) * (t + tau)
        env = np.ones(len(t)); nr = int(0.5 * fs); ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(nr) / nr); env[:nr] = ramp; env[-nr:] = ramp[::-1]
        u2 = u - w_of(theta) * env[:, None]; v = np.gradient(u2, 1 / fs, axis=0); ac = np.gradient(v, 1 / fs, axis=0)
        p = json.loads(str(d["params"])); p.update(w_compensation=dict(nmax=g.nmax, source_runs=[r_.name for r_ in runs], note="u' = u - w(theta); w from quasi-static 1 Hz / 2 Hz OFF records"))
        np.savez_compressed(HERE / f"{out}.npz", positions_mm=u2, reference_mm=r, time_pat_sec=t, time_cam_expected_sec=d["time_cam_expected_sec"], sample_hz=1e4, design=out.split(f"_f{g.freq}_")[1], params=json.dumps(p))
        np.savetxt(HERE / f"{out}.csv", np.column_stack([t, u2, r, d["time_cam_expected_sec"]]), delimiter=",", fmt="%.6f", header="time_pat_sec,ux_mm,uy_mm,uz_mm,rx_mm,ry_mm,rz_mm,time_cam_expected_sec", comments="")
        print(f"{out:28} 端点 {np.abs(u2[[0, -1]]).max():.4f}  最大オフセット {np.linalg.norm(u2, axis=1).max():.2f} mm  max v {np.linalg.norm(v, axis=1).max():.0f} mm/s  max a {np.linalg.norm(ac, axis=1).max() / 1e3:.1f}k mm/s²  max|u−r| {np.linalg.norm(u2 - r, axis=1).max():.3f} mm")

if __name__ == "__main__":
    main()
