#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""位置依存の外乱 w(x, y, z) を面で測るための、ゆっくりした走査の指令列（補償なし、14 s）。

  面内を渦巻きでなぞる: 半径 R(t) = Rmax·(1 − cos(2πt/T))/2（0 → Rmax → 0）、角速度 1.5 回/s。Rmax = 9.2 mm（ハート 7 mm の全域を含み、上限 9.5 mm 以内）。
  速さは最大 87 mm/s。実測の w は周期 2.5 mm 以下の成分を持たないので、80 Hz の共振に当たる成分（この速さでは周期 1.1 mm 以下）が無く、
  粒子は w をそのままなぞる（ゲイン ≈ 1）。渦の間隔は最大 1.4 mm で、行きと帰りが別の位相を通るので実効 0.7 mm（w の最短周期 2.5 mm の半分以下）。
  各面で a / b の 2 本: a は反時計回り、b は時計回りで半周ずらす。同じ場所を逆向きに通るので、w が「位置だけの関数か、進む向きにも依るか」を検定できる。
  面: XZ（y=0）、YZ（x=0）、XY（z=0）、XZ を y = ±4 mm にずらした 2 面（3 次元の補間用。Rmax = 8 mm）。
"""
import json
import numpy as np
from pathlib import Path
HERE = Path(__file__).resolve().parent; FS = 1e4; DUR = 14.0; EPS = 182e-6; FROT = 1.5
PLANES = {"XZ": (0, 2, None, 0.0, 9.2), "YZ": (1, 2, None, 0.0, 9.2), "XY": (0, 1, None, 0.0, 9.2), "XZyp4": (0, 2, 1, +4.0, 8.0), "XZym4": (0, 2, 1, -4.0, 8.0)}
VARIANTS = {"a": (+1, 0.0), "b": (-1, np.pi)}

def build(plane, variant):
    i1, i2, i3, off, rmax = PLANES[plane]; sgn, ph = VARIANTS[variant]; n = int(DUR * FS); t = np.arange(n) / FS
    R = rmax * 0.5 * (1 - np.cos(2 * np.pi * t / DUR)); ang = sgn * 2 * np.pi * FROT * t + ph
    u = np.zeros((n, 3)); u[:, i1] = R * np.cos(ang); u[:, i2] = R * np.sin(ang)
    if i3 is not None: u[:, i3] = off * np.minimum(1, 0.5 * (1 - np.cos(np.pi * np.minimum(t, DUR - t) / 0.5)))
    u[-1] = 0.0
    return t, u

def main():
    for plane in PLANES:
        for v in VARIANTS:
            t, u = build(plane, v); vel = np.gradient(u, 1 / FS, axis=0); acc = np.gradient(vel, 1 / FS, axis=0); name = f"wscan_{plane}_{v}"
            np.savez_compressed(HERE / f"{name}.npz", positions_mm=u, reference_mm=u, time_pat_sec=t, time_cam_expected_sec=t * (1 + EPS) + 0.8e-3, sample_hz=FS, design="OFF",
                                params=json.dumps(dict(shape="spiral", plane=plane, variant=v, rotation_hz=FROT, direction=VARIANTS[v][0], rmax_mm=PLANES[plane][4], offset_mm=PLANES[plane][3], duration_sec=DUR)))
            np.savetxt(HERE / f"{name}.csv", np.column_stack([t, u, u, t * (1 + EPS) + 0.8e-3]), delimiter=",", fmt="%.6f", header="time_pat_sec,ux_mm,uy_mm,uz_mm,rx_mm,ry_mm,rz_mm,time_cam_expected_sec", comments="")
            print(f"{name:16} 端点 {np.abs(u[[0, -1]]).max():.4f}  中心からの最大 {np.linalg.norm(u, axis=1).max():.2f} mm  max v {np.linalg.norm(vel, axis=1).max():.0f} mm/s  max a {np.linalg.norm(acc, axis=1).max() / 1e3:.2f}k mm/s²")

if __name__ == "__main__":
    main()
