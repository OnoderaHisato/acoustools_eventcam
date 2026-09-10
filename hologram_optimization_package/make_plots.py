import json
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

with open("optimize_results.json") as f:
    data = json.load(f)

meta = data["meta"]
results = data["results"]
weight_force = meta["weight_force"]

fig, axes = plt.subplots(2, 3, figsize=(15, 8))
labels = list(results.keys())
colors = ["#1f77b4", "#d62728"]

for i, label in enumerate(labels):
    h = results[label]["history"]
    it = np.arange(len(h["loss"]))
    c = colors[i]

    axes[0, 0].semilogy(it, np.array(h["loss"]) + 1e-18, color=c, label=label)
    axes[0, 1].plot(it, np.array(h["Fz"]) * 1e6, color=c, label=label)
    axes[0, 2].plot(it, np.array(h["Fx"]) * 1e6, color=c, linestyle="-", label=f"{label} Fx")
    axes[0, 2].plot(it, np.array(h["Fy"]) * 1e6, color=c, linestyle="--", label=f"{label} Fy")

axes[0, 0].set_title("Loss (normalized) vs iteration")
axes[0, 0].set_xlabel("iteration")
axes[0, 0].set_ylabel("loss")
axes[0, 0].legend(fontsize=8)

axes[0, 1].axhline(weight_force * 1e6, color="k", linestyle=":", label="target (mg)")
axes[0, 1].set_title("Fz vs iteration")
axes[0, 1].set_xlabel("iteration")
axes[0, 1].set_ylabel("Fz (uN)")
axes[0, 1].legend(fontsize=8)

axes[0, 2].axhline(0, color="k", linestyle=":")
axes[0, 2].set_title("Fx, Fy vs iteration")
axes[0, 2].set_xlabel("iteration")
axes[0, 2].set_ylabel("Force (uN)")
axes[0, 2].legend(fontsize=7)

# Final phase patterns on top/bottom board
import torch
import sys
sys.path.insert(0, '.')
from acoustools.Utilities import TOP_BOARD, BOTTOM_BOARD, TRANSDUCERS

top_n = TOP_BOARD.shape[0]
tx = TOP_BOARD[:, 0].numpy()
ty = TOP_BOARD[:, 1].numpy()
bx = BOTTOM_BOARD[:, 0].numpy()
by = BOTTOM_BOARD[:, 1].numpy()

for i, label in enumerate(labels):
    x_real = np.array(results[label]["x_real"])
    x_imag = np.array(results[label]["x_imag"])
    phase = np.angle(x_real + 1j * x_imag)
    phase_top = phase[:top_n]
    phase_bot = phase[top_n:]

    ax = axes[1, i]
    sc = ax.scatter(tx * 1000, ty * 1000, c=phase_top, cmap="twilight", vmin=-np.pi, vmax=np.pi, s=40)
    ax.set_title(f"Top board phase, {label}")
    ax.set_xlabel("x (mm)")
    ax.set_ylabel("y (mm)")
    ax.set_aspect("equal")
    plt.colorbar(sc, ax=ax, label="phase (rad)")

# Force vector bar comparison in final panel
ax = axes[1, 2]
width = 0.35
xpos = np.arange(3)
for i, label in enumerate(labels):
    F = np.array(results[label]["final_force"]) * 1e6
    ax.bar(xpos + i * width, F, width, label=label, color=colors[i])
ax.axhline(weight_force * 1e6, color="k", linestyle=":", label="target Fz=mg")
ax.set_xticks(xpos + width / 2)
ax.set_xticklabels(["Fx", "Fy", "Fz"])
ax.set_ylabel("Force (uN)")
ax.set_title("Final converged force by weight vector")
ax.legend(fontsize=8)

fig.suptitle(f"Spherical-harmonic/Mie ARF hologram optimization (Eq. 22 force term)\n"
             f"1cm sphere, rho_p={meta['rho_p']} kg/m^3, ka={meta['ka']:.3f}, "
             f"n_max={meta['n_max']}, target Fz=mg={weight_force*1e6:.2f} uN")
fig.tight_layout()
fig.savefig("optimization_results.png", dpi=150)
print("Saved optimization_results.png")
