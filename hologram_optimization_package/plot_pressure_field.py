import json
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

with open("pressure_field.json") as f:
    data = json.load(f)
meta = data["meta"]
results = data["results"]
r_a = meta["r_a"]

label = "w=(1.0, 1.0, 1.0)"  # the fully-weighted, complete Eq.22 stable solution
field = results[label]

fig, axes = plt.subplots(1, 2, figsize=(15, 7))

for ax, name, title in [(axes[0], "wide", "Full array-to-array cavity"),
                         (axes[1], "zoom", "Zoomed around the sphere")]:
    d = field[name]
    xs = np.array(d["xs"]) * 1000  # mm
    zs = np.array(d["zs"]) * 1000
    P = np.array(d["P_real"]) + 1j * np.array(d["P_imag"])
    absP = np.abs(P)

    im = ax.pcolormesh(zs, xs, absP, shading='auto', cmap='inferno')
    plt.colorbar(im, ax=ax, label="|P| (Pa)")
    ax.set_xlabel("z (mm)  [board-to-board axis]")
    ax.set_ylabel("x (mm)")
    ax.set_title(f"{title}\n|P(x, y=0, z)|")
    ax.set_aspect('equal')

    if name == "zoom":
        circle = plt.Circle((0, 0), r_a * 1000, fill=False, edgecolor='cyan', linewidth=2, linestyle='--')
        ax.add_patch(circle)
        ax.plot(0, 0, 'c+', markersize=12, markeredgewidth=2)
    else:
        ax.axhline(0, color='cyan', linewidth=0.5, alpha=0.5)
        ax.axvline(0, color='cyan', linewidth=0.5, alpha=0.5)

fig.suptitle(f"XZ-plane pressure magnitude (y=0), stable trap hologram, {label}\n"
             f"1cm sphere at origin (dashed cyan circle = particle radius, {r_a*1000:.0f}mm)")
fig.tight_layout()
fig.savefig("pressure_field_xz.png", dpi=150)
print("Saved pressure_field_xz.png")
