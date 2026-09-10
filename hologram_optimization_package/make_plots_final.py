import json
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import sys
sys.path.insert(0, '.')
from acoustools.Utilities import TOP_BOARD, BOTTOM_BOARD

with open("optimize_results_v3_stable.json") as f:
    data = json.load(f)
meta = data["meta"]
results = data["results"]
weight_force = meta["weight_force"]

top_n = TOP_BOARD.shape[0]
tx, ty = TOP_BOARD[:, 0].numpy() * 1000, TOP_BOARD[:, 1].numpy() * 1000
bx, by = BOTTOM_BOARD[:, 0].numpy() * 1000, BOTTOM_BOARD[:, 1].numpy() * 1000

labels = list(results.keys())
fig, axes = plt.subplots(2, 4, figsize=(20, 9))

for i, label in enumerate(labels):
    r = results[label]
    x_real = np.array(r["x_real"]); x_imag = np.array(r["x_imag"])
    phase = np.angle(x_real + 1j * x_imag)
    phase_top, phase_bot = phase[:top_n], phase[top_n:]

    ax = axes[i, 0]
    sc = ax.scatter(tx, ty, c=phase_top, cmap="twilight", vmin=-np.pi, vmax=np.pi, s=60, edgecolors='k', linewidths=0.3)
    ax.set_title(f"TOP board hologram (phase)\n{label}")
    ax.set_xlabel("x (mm)"); ax.set_ylabel("y (mm)"); ax.set_aspect("equal")
    plt.colorbar(sc, ax=ax, label="phase (rad)")

    ax = axes[i, 1]
    sc = ax.scatter(bx, by, c=phase_bot, cmap="twilight", vmin=-np.pi, vmax=np.pi, s=60, edgecolors='k', linewidths=0.3)
    ax.set_title(f"BOTTOM board hologram (phase)\n{label}")
    ax.set_xlabel("x (mm)"); ax.set_ylabel("y (mm)"); ax.set_aspect("equal")
    plt.colorbar(sc, ax=ax, label="phase (rad)")

    ax = axes[i, 2]
    F = np.array(r["final_force"]) * 1e6  # uN
    bars = ax.bar(["Fx", "Fy", "Fz"], F, color=["#1f77b4", "#2ca02c", "#d62728"])
    ax.axhline(weight_force * 1e6, color="k", linestyle=":", linewidth=1.5, label=f"target mg={weight_force*1e6:.2f}uN")
    for b, val in zip(bars, F):
        ax.text(b.get_x() + b.get_width() / 2, val, f"{val:.4f}", ha='center',
                va='bottom' if val >= 0 else 'top', fontsize=9)
    ax.set_ylabel("Force (uN)")
    ax.set_title(f"Converged force, {label}\n(stable={r['stable']})")
    ax.legend(fontsize=8)

    ax = axes[i, 3]
    J = r["final_jacobian_diag"]
    bars = ax.bar(["dFx/dx", "dFy/dy", "dFz/dz"], J, color=["#1f77b4", "#2ca02c", "#d62728"])
    for b, val in zip(bars, J):
        ax.text(b.get_x() + b.get_width() / 2, val, f"{val:.4f}", ha='center',
                va='bottom' if val >= 0 else 'top', fontsize=9)
    ax.axhline(0, color="k", linewidth=0.8)
    ax.set_ylabel("N/m")
    ax.set_title(f"Stability eigenvalues (all <0 = trapped)\n{label}")

fig.suptitle(f"Acoustic hologram + resulting force/stability -- 1cm sphere, rho_p={meta['rho_p']}kg/m^3, "
             f"ka={meta['ka']:.3f}, target Fz=mg={weight_force*1e6:.3f}uN\n"
             f"(full Eq.22: force-matching + Lyapunov stability term, SH/Mie ARF)")
fig.tight_layout()
fig.savefig("final_hologram_and_forces.png", dpi=150)
print("Saved final_hologram_and_forces.png")

for label in labels:
    r = results[label]
    print(f"\n{label}:")
    print(f"  Fx = {r['final_force'][0]*1e6:.6f} uN")
    print(f"  Fy = {r['final_force'][1]*1e6:.6f} uN")
    print(f"  Fz = {r['final_force'][2]*1e6:.6f} uN   (target = {weight_force*1e6:.6f} uN)")
    print(f"  stable = {r['stable']}, Jacobian diag = {r['final_jacobian_diag']}")
