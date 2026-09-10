"""Hardware-free parameter checks; do not import executable optimizer modules."""

import ast
import math
from pathlib import Path
from types import SimpleNamespace
import unittest

from hologram_optimization_package.particle_parameters import PARTICLE_DENSITY_KG_M3


PACKAGE = Path(__file__).resolve().parent / "hologram_optimization_package"
OPTIMIZERS = ("optimize.py", "optimize_v2.py", "optimize_v3_stable.py")
CONSUMERS = OPTIMIZERS + (
    "stability_check.py", "stability_check_v2.py", "force_profile.py",
    "validate_gorkov.py",
)


def parameter_assignments(filename, names, **extra):
    """Execute only named scalar/metadata assignments, never model or I/O code."""
    tree = ast.parse((PACKAGE / filename).read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, ast.Assign)
             and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
             and node.targets[0].id in names]
    namespace = {"np": SimpleNamespace(pi=math.pi),
                 "PARTICLE_DENSITY_KG_M3": PARTICLE_DENSITY_KG_M3, **extra}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, "exec"), namespace)
    return namespace


class HologramParticleParameterTests(unittest.TestCase):
    def test_requested_density(self):
        self.assertEqual(PARTICLE_DENSITY_KG_M3, 24.8)

    def test_all_material_calculations_use_shared_density(self):
        for filename in CONSUMERS:
            with self.subTest(filename=filename):
                tree = ast.parse((PACKAGE / filename).read_text(encoding="utf-8"))
                self.assertTrue(any(
                    isinstance(node, ast.ImportFrom)
                    and node.module == "particle_parameters"
                    and any(alias.name == "PARTICLE_DENSITY_KG_M3" for alias in node.names)
                    for node in tree.body))
                params = parameter_assignments(filename, {"rho_p"})
                self.assertEqual(params["rho_p"], 24.8)

    def test_optimizers_recompute_mass_and_weight(self):
        for filename in OPTIMIZERS:
            with self.subTest(filename=filename):
                params = parameter_assignments(
                    filename, {"rho_p", "r_a", "g", "V", "mass", "weight_force"})
                self.assertEqual(params["r_a"], 0.005)
                self.assertAlmostEqual(params["mass"] * 1e6, 12.985249634837814)
                self.assertAlmostEqual(params["weight_force"] * 1e6, 127.38529891775897)

    def test_force_profile_does_not_reuse_old_density_or_weight(self):
        old_meta = {"rho_p": 40.0, "weight_force": 205.46015954477252e-6,
                    "ka": 3.663664902, "n_max": 12, "c_p": 1052.0, "r_a": 0.005}
        original = old_meta.copy()
        params = parameter_assignments(
            "force_profile.py",
            {"rho_p", "r_a", "n_max", "V", "mass", "weight_force", "evaluation_meta"},
            data={"meta": old_meta}, k=726.3798042774566, c_p=1052.0,
            rho_0=1.2, c_0=346.0, p_ref=3.4, transducer_radius=0.0045)
        meta = params["evaluation_meta"]
        self.assertEqual(meta["rho_p"], 24.8)
        self.assertAlmostEqual(meta["weight_force"] * 1e6, 127.38529891775897)
        self.assertEqual(meta["n_max"], 10)
        self.assertAlmostEqual(meta["ka"], 726.3798042774566 * 0.005)
        self.assertEqual(old_meta, original)
        self.assertIsNot(meta, old_meta)


if __name__ == "__main__":
    unittest.main()
