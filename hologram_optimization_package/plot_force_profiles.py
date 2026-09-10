import json
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

with open("force_profiles.json") as f:
    data = json.load(f)
meta = data["meta"]
profiles = data["profiles"]
weight_force = meta["weight_force"]
r_a = meta["r_a"]

labels = list(profiles.keys())
colors = ["#1f77b4", "#d62728"]

fig, axes = plt.subplots(2, 3, figsize=(16, 8.5))

axis_force = [('x', 'Fx'), ('y', 'Fy'), ('z', 'Fz')]

for row, label in enumerate(labels):
    prof = profiles[label]
    offsets = np.array(prof["offsets"]) * 1000  # mm

    for col, (axis_name, force_name) in enumerate(axis_force):
        ax = axes[row, col]
        F = np.array(prof[f"F_vs_{axis_name}"][force_name]) * 1e6  # uN
        ax.plot(offsets, F, color=colors[row], linewidth=2)
        ax.axhline(0, color='gray', linewidth=0.7)
        ax.axvline(0, color='gray', linewidth=0.7)
        ax.axvspan(-r_a * 1000, r_a * 1000, color='orange', alpha=0.12, label='particle radius')
        if force_name == 'Fz':
            ax.axhline(weight_force * 1e6, color='k', linestyle=':', linewidth=1.2, label='target mg')
        ax.set_xlabel(f"{axis_name} (mm)")
        ax.set_ylabel(f"{force_name} (uN)")
        other_axes = [a for a in ['x', 'y', 'z'] if a != axis_name]
        ax.set_title(f"{label}\n{force_name}({axis_name}, {other_axes[0]}=0, {other_axes[1]}=0)")
        ax.legend(fontsize=7)

fig.suptitle("Force profile along each axis, holding the other two at 0 (z=0 = midplane between boards)\n"
             f"1cm sphere, rho_p={meta['rho_p']}kg/m^3, ka={meta['ka']:.3f}; "
             "shaded band = particle radius (5mm); dotted = target Fz=mg")
fig.tight_layout()
fig.savefig("force_profiles.png", dpi=150)
print("Saved force_profiles.png")
