#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vzr 同定用の多軸同時励起指令列を生成する（10 kHz、cos ランプ付き）。

設計原則（HANDOFF「vzr を取るための計測仕様」）:
  - 力モデルは Δ = トラップ − 粒子 にしか依存しない。粒子が追従してしまう低周波の
    大振幅は Δ を作らない（x 1.2 mm @15 Hz → Δr 0.04 mm）。
  - 各軸を共振の 0.5–0.75 倍で駆動して Δ を立てる:
      x: f0 ≈ 87 Hz → 45–65 Hz,   z: f0 ≈ 289 Hz → 150–210 Hz
  - x と z の周波数は非整数比（Δz と d_r の位相を独立にする）。
  - 指令→実トラップの純遅延 τ≈2 ms は同定側で u(t−τ) として扱う（補償不要）。
  - 脱出余裕: 物理 Δ で d_r ≤ 0.6 mm（力最大 1.07）、|Δz| ≤ 0.3 mm（力最大 1.1–1.5）。
    模擬では x 0.8 mm @57 Hz は脱出、0.6 mm は保持（HANDOFF 参照）。

使い方:
  python3 make_vzr_trajectory.py --out vzr_x0.6_57_z0.2_187 \
      --comp x 0.6 57 --comp z 0.2 187 --duration 8 --ramp 0.5
  → <out>.npz (positions_mm (N,3), time_sec, sample_hz, components) と <out>.csv
     取得側は json_3d と同じ (N,3) [mm] の位置列として送ればよい。
"""
import argparse, json
import numpy as np


def build(components, sample_hz=10000.0, duration_sec=8.0, ramp_sec=0.5,
          chirp=None):
    """components: [(axis, amp_mm, f_hz, phase_rad)], axis in 'xyz'.
    chirp: 任意 {axis: (f_start, f_end)} で線形チャープに置き換える。"""
    n = int(round(duration_sec * sample_hz))
    t = np.arange(n) / sample_hz
    env = np.ones(n)
    nr = int(round(ramp_sec * sample_hz))
    if nr > 0:
        r = 0.5 - 0.5 * np.cos(np.pi * np.arange(nr) / nr)
        env[:nr] = r
        env[-nr:] = r[::-1]          # 終端も滑らかに戻す（保持のため）
    pos = np.zeros((n, 3))
    for ax, A, f, ph in components:
        i = "xyz".index(ax)
        if chirp and ax in chirp:
            f1, f2 = chirp[ax]
            phase = 2 * np.pi * (f1 * t + 0.5 * (f2 - f1) / duration_sec * t * t)
        else:
            phase = 2 * np.pi * f * t
        pos[:, i] += A * np.sin(phase + ph)
    pos *= env[:, None]
    v = np.gradient(pos, 1 / sample_hz, axis=0)
    a = np.gradient(v, 1 / sample_hz, axis=0)
    metrics = {
        "max_abs_offset_mm": float(np.linalg.norm(pos, axis=1).max()),
        "max_speed_mm_s": float(np.linalg.norm(v, axis=1).max()),
        "max_acceleration_mm_s2": float(np.linalg.norm(a, axis=1).max()),
        "samples": n, "duration_sec": n / sample_hz, "sample_hz": sample_hz,
        "pat_divider": int(round(40000 / sample_hz)),
    }
    return t, pos, metrics


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--comp", nargs=3, action="append", metavar=("AXIS", "AMP_MM", "F_HZ"),
                    required=True, help="例: --comp x 0.6 57 --comp z 0.2 187")
    ap.add_argument("--chirp", nargs=3, action="append", metavar=("AXIS", "F1", "F2"),
                    help="その軸を線形チャープにする")
    ap.add_argument("--duration", type=float, default=8.0)
    ap.add_argument("--ramp", type=float, default=0.5)
    ap.add_argument("--sample-hz", type=float, default=10000.0)
    ap.add_argument("--phase", type=float, nargs="*", default=None,
                    help="成分ごとの位相 [rad]（省略時 0, 0.7, 2.1, ...）")
    a = ap.parse_args()
    comps = []
    for k, (ax, A, f) in enumerate(a.comp):
        ph = a.phase[k] if a.phase and k < len(a.phase) else [0.0, 0.7, 2.1, 3.5][k % 4]
        comps.append((ax, float(A), float(f), ph))
    chirp = {ax: (float(f1), float(f2)) for ax, f1, f2 in (a.chirp or [])}
    t, pos, m = build(comps, a.sample_hz, a.duration, a.ramp, chirp)
    np.savez_compressed(a.out + ".npz", positions_mm=pos, time_sec=t,
                        sample_hz=a.sample_hz, components=json.dumps(comps),
                        chirp=json.dumps(chirp), metrics=json.dumps(m))
    np.savetxt(a.out + ".csv", np.column_stack([t, pos]), delimiter=",",
               header="time_sec,x_mm,y_mm,z_mm", comments="", fmt="%.6f")
    print(json.dumps({"components": comps, "chirp": chirp, **m}, indent=1))


if __name__ == "__main__":
    main()
