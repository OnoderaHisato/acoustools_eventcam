"""
XZ-plane pressure field (y=0) around the trap, for the converged stable-trap
hologram, computed with acoustools.propagate directly (validated earlier to
agree with our SH/Mie reconstruction near the trap to <1%).
"""
import json
import numpy as np
import torch
import sys
sys.path.insert(0, '.')
from acoustools.Utilities import TRANSDUCERS, propagate, DTYPE, device, BOARD_POSITIONS
import acoustools.Constants as Constants

with open("optimize_results_v3_stable.json") as f:
    data = json.load(f)
meta = data["meta"]
r_a = meta["r_a"]
wavelength = 2 * np.pi / Constants.k

results = {}
for label, res in data["results"].items():
    x_real = np.array(res["x_real"]); x_imag = np.array(res["x_imag"])
    x = torch.tensor((x_real + 1j * x_imag).reshape(-1, 1), dtype=DTYPE, device=device)

    field_data = {}
    for name, (half_x, half_z, n_x, n_z) in [
        ("wide", (0.079, BOARD_POSITIONS, 161, 241)),
        ("zoom", (0.015, 0.015, 151, 151)),
    ]:
        xs = np.linspace(-half_x, half_x, n_x)
        zs = np.linspace(-half_z, half_z, n_z)
        Xg, Zg = np.meshgrid(xs, zs, indexing='ij')
        pts = np.stack([Xg.ravel(), np.zeros(Xg.size), Zg.ravel()], axis=0)  # (3,N)
        pts_t = torch.tensor(pts[None, :, :], dtype=DTYPE, device=device)  # (1,3,N)

        P = propagate(x, pts_t).squeeze(0).detach().cpu().numpy()
        P = P.reshape(Xg.shape)
        field_data[name] = {
            "xs": xs.tolist(), "zs": zs.tolist(),
            "P_real": P.real.tolist(), "P_imag": P.imag.tolist(),
        }
        print(f"{label} {name}: field computed, shape {P.shape}, max|P|={np.abs(P).max():.2f} Pa")

    results[label] = field_data

with open("pressure_field.json", "w") as f:
    json.dump({"meta": {"r_a": r_a, "wavelength": wavelength, "board_z": BOARD_POSITIONS}, "results": results}, f)
print("Saved pressure_field.json")
