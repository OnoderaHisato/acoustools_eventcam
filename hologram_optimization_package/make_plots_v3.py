import json
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

with open("optimize_results_v3_stable.json") as f:
    data = json.load(f)
meta = data["meta"]
results = data["results"]
weight_force = meta["weight_force"]

fig, axes = plt.subplots(2, 2, figsize=(12, 9))
colors = ["#1f77b4", "#d62728"]
labels = list(results.keys())

for i, label in enumerate(labels):
    h = results[label]["history"]
    it = np.arange(len(h["Fz"]))
    c = colors[i]
    axes[0, 0].plot(it, np.array(h["Fz"]) * 1e6, color=c, label=f"{label} Fz")
    axes[0, 1].plot(it, h["Jxx"], color=c, linestyle="-", label=f"{label} Jxx")
    axes[0, 1].plot(it, h["Jyy"], color=c, linestyle="--", label=f"{label} Jyy")
    axes[0, 1].plot(it, h["Jzz"], color=c, linestyle=":", label=f"{label} Jzz")
    best_it = results[label]["best_iter"]
    axes[0, 0].axvline(best_it, color=c, alpha=0.3)
    axes[0, 1].axvline(best_it, color=c, alpha=0.3)

axes[0, 0].axhline(weight_force * 1e6, color="k", linestyle=":", label="target mg")
axes[0, 0].set_title("Fz vs iteration (vertical line = chosen 'best' iterate)")
axes[0, 0].set_xlabel("iteration"); axes[0, 0].set_ylabel("Fz (uN)")
axes[0, 0].legend(fontsize=7)

axes[0, 1].axhline(0, color="gray", linewidth=0.8)
axes[0, 1].set_title("Force-Jacobian diagonal (~eigenvalues): <0 = stable")
axes[0, 1].set_xlabel("iteration"); axes[0, 1].set_ylabel("dF_i/dX_i (N/m)")
axes[0, 1].legend(fontsize=6, ncol=2)

# trade-off scatter: force error vs "instability margin" (max diag eigenvalue)
for i, label in enumerate(labels):
    h = results[label]["history"]
    Fz = np.array(h["Fz"])
    force_err_pct = np.abs(Fz - weight_force) / weight_force * 100
    max_eig = np.maximum.reduce([h["Jxx"], h["Jyy"], h["Jzz"]])
    axes[1, 0].plot(max_eig, force_err_pct, '.', color=colors[i], alpha=0.4, markersize=3, label=label)
axes[1, 0].axvline(0, color="k", linestyle=":", label="stability boundary")
axes[1, 0].set_xlabel("max diagonal Jacobian eigenvalue (N/m); <0 = stable")
axes[1, 0].set_ylabel("Fz error (%)")
axes[1, 0].set_title("Stability-vs-force-accuracy trade-off (Eq. 22)")
axes[1, 0].legend(fontsize=8)
axes[1, 0].set_yscale("log")

# Summary bar: final force + eigenvalues at chosen best iterate
ax = axes[1, 1]
x_pos = np.arange(3)
width = 0.35
for i, label in enumerate(labels):
    r = results[label]
    vals = r["final_jacobian_diag"]
    ax.bar(x_pos + i * width, vals, width, color=colors[i], label=label)
ax.axhline(0, color="k", linewidth=0.8)
ax.set_xticks(x_pos + width / 2)
ax.set_xticklabels(["Jxx (x-stability)", "Jyy (y-stability)", "Jzz (z-stability)"])
ax.set_ylabel("dF_i/dX_i (N/m)")
ax.set_title("Chosen 'best' iterate: all eigenvalues < 0 (stable)")
ax.legend(fontsize=8)

fig.suptitle("Full Eq. 22 (force-matching + Lyapunov stability term)\n"
             f"1cm sphere, rho_p={meta['rho_p']}kg/m^3, ka={meta['ka']:.3f}, "
             f"target Fz=mg={weight_force*1e6:.2f} uN")
fig.tight_layout()
fig.savefig("optimization_results_v3_stable.png", dpi=150)
print("Saved optimization_results_v3_stable.png")

for label in labels:
    r = results[label]
    print(f"\n{label}: final_force={r['final_force']}, "
          f"jacobian_diag={r['final_jacobian_diag']}, stable={r['stable']}")
