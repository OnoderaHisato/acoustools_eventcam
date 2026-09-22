#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ハート軌道（取得側 generate_cycle_positions mode 4 と同じ式）の FF 指令列を生成する。

  r(t):  x = s·sin³θ,  z = s·(13cosθ − 5cos2θ − 2cos3θ − cos4θ)/13,  θ = 2π f t   （XZ 面、y = 0）
  OFF:   u = r
  A:     u(t) = r(t + τ0)                          純遅延の先読み（τ0 = 0.8 ms）
  C:     u(t) = r(t+τ0) + (r̈(t+τ0) + γ·ṙ(t+τ0)) / k   軸ごとの線形逆モデル（k, γ は同定値）

時間軸: 指令は PAT 時間（公称 10 kHz）で作る。実測ではトラップ事象がカメラ時間で
  t_cam = t_pat·(1 + ε) + τ0,  ε = +182 ppm
に現れる（HANDOFF「クロック差」）。評価用に各サンプルの t_cam を列に出す。
--exact-realtime を付けると、カメラ時間で厳密に f Hz になるよう PAT 側の周波数を f·(1+ε) にする。
"""
import argparse, json
import numpy as np
from pathlib import Path

EPS = 182e-6
K = {"x": 0.30, "y": 0.28, "z": 3.1}        # 線形剛性 [1/ms²]（ブロック最初の run の値。連続稼働による剛性低下に注意）
GAM = {"x": 0.040, "y": 0.036, "z": 0.024}  # 粘性減衰 [1/ms]

def heart(theta, s):
    x = s * np.sin(theta) ** 3
    z = s * (13 * np.cos(theta) - 5 * np.cos(2 * theta) - 2 * np.cos(3 * theta) - np.cos(4 * theta)) / 13.0
    return np.stack([x, np.zeros_like(x), z], 1)

def cardioid(theta, a):
    """OptiTrap 論文 Eq.(2) のカーディオイドを XZ 面に置いたもの（尖点が上、上下の中央を原点に合わせる）。幅 = 2.598a、経路長 = 8a。"""
    x = a * np.sin(theta) * (1 + np.cos(theta)); z = -a * np.cos(theta) * (1 + np.cos(theta)) + a - 0.125 * a
    return np.stack([x, np.zeros_like(x), z], 1)

SHAPES = {"heart": heart, "cardioid": cardioid}

def envelope(n, fs, ramp_sec):
    e = np.ones(n); nr = int(round(ramp_sec * fs))
    if nr > 0:
        r = 0.5 - 0.5 * np.cos(np.pi * np.arange(nr) / nr); e[:nr] = r; e[-nr:] = r[::-1]
    return e

def build(scale_mm=7.0, f_hz=10.0, duration=8.0, fs=10000.0, ramp=0.5, tau0_ms=0.8,
          exact_realtime=False, eps=EPS, shape="heart"):
    f_cmd = f_hz * (1 + eps) if exact_realtime else f_hz
    n = int(round(duration * fs)); t = np.arange(n) / fs                    # PAT 時間 [s]
    env = envelope(n, fs, ramp)
    def r_of(tt):  # 任意時刻の参照軌道（ランプ込み）
        e = np.interp(tt, t, env, left=0.0, right=0.0)
        return SHAPES[shape](2 * np.pi * f_cmd * tt, scale_mm) * e[:, None]
    tau = tau0_ms * 1e-3
    r  = r_of(t)
    rA = r_of(t + tau)
    # 微分は解析的に取れないので、先読み点で数値微分（1e-5 s の中心差分、ノイズ無しなので十分）
    h = 1e-5
    v = (r_of(t + tau + h) - r_of(t + tau - h)) / (2 * h) / 1e3          # mm/ms
    a = (r_of(t + tau + h) - 2 * rA + r_of(t + tau - h)) / h ** 2 / 1e6   # mm/ms²
    uC = rA.copy()
    for i, ax in enumerate("xyz"):
        uC[:, i] += (a[:, i] + GAM[ax] * v[:, i]) / K[ax]
    # OptiTrap 型: 逆モデルのみ、先読みなし（時間配分は等 θ のまま。この規模では力の上限が効かないので時間配分の最適化は不要）
    v0 = (r_of(t + h) - r_of(t - h)) / (2 * h) / 1e3; a0 = (r_of(t + h) - 2 * r + r_of(t - h)) / h ** 2 / 1e6; uOT = r.copy()
    for i, ax in enumerate("xyz"):
        uOT[:, i] += (a0[:, i] + GAM[ax] * v0[:, i]) / K[ax]
    t_cam = t * (1 + eps) + tau
    designs = {"OFF": r, "A_delay": rA, "C_delay_inverse": uC, "OT_ident_nodelay": uOT}
    metrics = {}
    for k_, u in designs.items():
        vel = np.gradient(u, 1 / fs, axis=0); acc = np.gradient(vel, 1 / fs, axis=0)
        metrics[k_] = {"max_abs_u_minus_r_mm": float(np.linalg.norm(u - r, axis=1).max()),
                       "max_speed_mm_s": float(np.linalg.norm(vel, axis=1).max()),
                       "max_accel_mm_s2": float(np.linalg.norm(acc, axis=1).max())}
    return t, t_cam, r, designs, metrics, f_cmd

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=float, default=7.0); ap.add_argument("--freq", type=float, default=10.0)
    ap.add_argument("--duration", type=float, default=8.0); ap.add_argument("--ramp", type=float, default=0.5)
    ap.add_argument("--tau0", type=float, default=0.8); ap.add_argument("--exact-realtime", action="store_true")
    ap.add_argument("--out", default="ff_heart/heart_s7_f10"); ap.add_argument("--shape", choices=list(SHAPES), default="heart")
    a = ap.parse_args()
    t, t_cam, r, designs, metrics, f_cmd = build(a.scale, a.freq, a.duration, 10000.0, a.ramp, a.tau0, a.exact_realtime, shape=a.shape)
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    for k_, u in designs.items():
        np.savez_compressed(f"{out}_{k_}.npz", positions_mm=u, reference_mm=r, time_pat_sec=t,
                            time_cam_expected_sec=t_cam, sample_hz=10000.0, design=k_,
                            params=json.dumps(dict(shape=a.shape, scale_mm=a.scale, f_hz=a.freq, f_cmd_hz=f_cmd, tau0_ms=a.tau0,
                                                   eps_ppm=EPS * 1e6, K=K, GAM=GAM, ramp_sec=a.ramp)))
        np.savetxt(f"{out}_{k_}.csv", np.column_stack([t, u, r, t_cam]), delimiter=",", fmt="%.6f",
                   header="time_pat_sec,ux_mm,uy_mm,uz_mm,rx_mm,ry_mm,rz_mm,time_cam_expected_sec", comments="")
    json.dump({"f_cmd_hz": f_cmd, "tau0_ms": a.tau0, "eps_ppm": EPS * 1e6, "K": K, "GAM": GAM, "metrics": metrics},
              open(f"{out}_metrics.json", "w"), indent=1)
    print(json.dumps({"f_cmd_hz": f_cmd, "metrics": metrics}, indent=1))

if __name__ == "__main__":
    main()
