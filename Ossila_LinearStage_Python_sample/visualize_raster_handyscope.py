#!/usr/bin/env python3
"""
使い方:
    このスクリプトを実行すると、対話形式で
    1) JSON ファイル名
    2) 表示したい平面 (XY, XZ, YZ)
    を順に聞かれます。
"""

import json
import numpy as np
import matplotlib.pyplot as plt
import os

def prompt_filename():
    fn = input("Enter raster handyscope JSON filename: ").strip()
    while not os.path.exists(fn):
        print(f"File '{fn}' does not exist. Please try again.")
        fn = input("Enter raster handyscope JSON filename: ").strip()
    return fn

def prompt_plane():
    plane = input("Enter scan plane [XY, XZ, YZ] (default: XZ): ").strip().upper()
    if plane == "":
        return "XZ"
    while plane not in ("XY", "XZ", "YZ"):
        print("Invalid plane. Please enter one of: XY, XZ, YZ.")
        plane = input("Enter scan plane [XY, XZ, YZ] (default: XZ): ").strip().upper()
        if plane == "":
            return "XZ"
    return plane

def extract_grid_from_plane(raster_points, plane):
    a1, a2 = plane[0].lower(), plane[1].lower()
    grid_indices = [tuple(pt["relative_grid_index"][:2]) for pt in raster_points]
    i_vals = sorted({i for i, _ in grid_indices})
    j_vals = sorted({j for _, j in grid_indices})
    ix_map = {v: i for i, v in enumerate(i_vals)}
    iy_map = {v: j for j, v in enumerate(j_vals)}
    shape = (len(i_vals), len(j_vals))
    g1_mm = np.zeros(shape)
    g2_mm = np.zeros(shape)
    for pt in raster_points:
        i_idx, j_idx = pt["relative_grid_index"][:2]
        i = ix_map[i_idx]; j = iy_map[j_idx]
        pos = pt["global_relative_position"]
        g1_mm[i, j] = pos[a1]
        g2_mm[i, j] = pos[a2]
    return grid_indices, ix_map, iy_map, shape, g1_mm, g2_mm

def process_point_top_peaks(time, ch1, ch2, mic_sens, n_peaks=4, debug=False):
    t = np.array(time)
    v_mic = np.array(ch1); v_ref = np.array(ch2)
    pa = (v_mic * 1000.0) / mic_sens
    pa -= pa.mean(); v_ref -= v_ref.mean()
    n = len(t); dt = t[1] - t[0]
    freqs = np.fft.rfftfreq(n, dt)
    fft_ref = np.fft.rfft(v_ref); fft_mic = np.fft.rfft(pa)
    amp = np.abs(fft_mic) / n
    amp_no_dc = amp.copy(); amp_no_dc[0] = 0
    idxs = np.argpartition(amp_no_dc, -n_peaks)[-n_peaks:]
    idxs = idxs[np.argsort(amp_no_dc[idxs])[::-1]]
    freqs_peaks = freqs[idxs]; amps_peaks = amp[idxs]
    idx_max = idxs[0]
    phase_diff = np.angle(fft_mic[idx_max]) - np.angle(fft_ref[idx_max])
    phase_diff = np.arctan2(np.sin(phase_diff), np.cos(phase_diff))
    if debug:
        print(f"[DEBUG] FFT peaks freq: {freqs_peaks}")
        print(f"[DEBUG] FFT peaks amp: {amps_peaks}")
    return freqs_peaks, amps_peaks, phase_diff

def main():
    fn = prompt_filename()
    plane = prompt_plane()
    print(f"Selected plane: {plane}\n")

    with open(fn, 'r') as f:
        data = json.load(f)
    mic_sens = float(data["microphone_sensitivity_mV_per_Pa"])
    raster_points = data["raster_points"]

    _, ix_map, iy_map, shape, g1_mm, g2_mm = extract_grid_from_plane(raster_points, plane)

    n_peaks = 3
    amp_maps = [np.zeros(shape) for _ in range(n_peaks)]
    freq_maps = [np.zeros(shape) for _ in range(n_peaks)]
    phase_map = np.zeros(shape)

    for npt, pt in enumerate(raster_points):
        i_idx, j_idx = pt["relative_grid_index"][:2]
        i = ix_map[i_idx]; j = iy_map[j_idx]
        t = pt["handyscope"]["time"]
        ch1 = pt["handyscope"]["ch1_voltage"]
        ch2 = pt["handyscope"]["ch2_voltage"]
        debug = (npt == 0)
        freqs_peaks, amps_peaks, phase = process_point_top_peaks(
            t, ch1, ch2, mic_sens, n_peaks=n_peaks, debug=debug
        )
        for k in range(n_peaks):
            amp_maps[k][i, j] = amps_peaks[k]
            freq_maps[k][i, j] = freqs_peaks[k]
            if k == 0:
                phase_map[i, j] = phase

    # Amplitude maps
    fig, axs = plt.subplots(1, 3, figsize=(15, 5))
    for k, ax in enumerate(axs.flat):
        im = ax.pcolormesh(g1_mm.T, g2_mm.T, amp_maps[k].T, shading='auto')
        fig.colorbar(im, ax=ax, label='Amplitude (Pa)')
        ax.set_aspect('equal')                        # ← ここでアスペクト比を1:1に
        idx_flat = np.argmax(amp_maps[k])
        idx_2d = np.unravel_index(idx_flat, amp_maps[k].shape)
        dom_freq = freq_maps[k][idx_2d]
        ax.set_title(f'Peak {k+1}: {dom_freq:.1f} Hz')
        ax.set_xlabel(f'{plane[0]} (mm)')
        ax.set_ylabel(f'{plane[1]} (mm)')
    plt.suptitle(f'Top 3 FFT Amplitudes (Pa) [{plane} plane]')
    plt.tight_layout(rect=[0, 0, 1, 0.95])
    amp_png = os.path.splitext(fn)[0] + f'_{plane}_amplitude_top3.png'
    plt.savefig(amp_png)
    plt.show()

    # Phase map
    fig = plt.figure(figsize=(8, 6))
    ax = fig.add_subplot(1,1,1)
    im2 = ax.pcolormesh(g1_mm.T, g2_mm.T, phase_map.T, shading='auto')
    fig.colorbar(im2, ax=ax, label='Phase (rad)')
    ax.set_aspect('equal')                            # ← 同じく縦横比1:1
    ax.set_title(f'Phase (top peak, rel. to ref) [{plane} plane]')
    ax.set_xlabel(f'{plane[0]} (mm)')
    ax.set_ylabel(f'{plane[1]} (mm)')
    plt.tight_layout()
    phase_png = os.path.splitext(fn)[0] + f'_{plane}_phase_top1.png'
    plt.savefig(phase_png)
    plt.show()

    print(f"Amplitude plot saved as {amp_png}")
    print(f"Phase plot saved as   {phase_png}")

if __name__ == "__main__":
    main()
