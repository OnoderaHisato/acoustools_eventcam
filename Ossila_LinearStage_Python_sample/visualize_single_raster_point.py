import json
import numpy as np
import matplotlib.pyplot as plt
import os
import random

def prompt_filename():
    fn = input("Enter raster handyscope JSON filename: ").strip()
    while not os.path.exists(fn):
        print(f"File '{fn}' does not exist. Please try again.")
        fn = input("Enter raster handyscope JSON filename: ").strip()
    return fn

def main():
    fn = prompt_filename()
    with open(fn, 'r') as f:
        data = json.load(f)
    mic_sens = float(data["microphone_sensitivity_mV_per_Pa"])
    raster_points = data["raster_points"]
    idx = random.randint(0, len(raster_points) - 1)
    pt = raster_points[idx]
    handyscope = pt["handyscope"]
    t = np.array(handyscope["time"])
    ch1 = np.array(handyscope["ch1_voltage"])
    ch2 = np.array(handyscope["ch2_voltage"])
    ch2_mV = ch2 * 1000.0
    ch2_Pa = ch2_mV / mic_sens
    print(f"Randomly selected raster point index: {idx}")
    print(f"Grid Index: {pt['relative_grid_index']}, Global Pos: {pt['global_relative_position']}")
    print(f"ch1 pk-pk: {np.max(ch1) - np.min(ch1):.4f} V")
    print(f"ch2 pk-pk: {np.max(ch2) - np.min(ch2):.4f} V, {np.max(ch2_Pa) - np.min(ch2_Pa):.4f} Pa")
    plt.figure(figsize=(10,6))
    plt.plot(t, ch1, label='Ch1 (V)')
    plt.plot(t, ch2, label='Ch2 (V)')
    plt.plot(t, ch2_Pa, label='Ch2 (Pa)', alpha=0.7)
    plt.xlabel('Time (s)')
    plt.ylabel('Signal')
    plt.legend()
    plt.title('Random Raster Point: Ch1 & Ch2')
    plt.tight_layout()
    plt.show()

if __name__ == "__main__":
    main()
