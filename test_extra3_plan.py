"""Extra measurement 3 on 2026-10-12 (extra3_20261012/extra3_plan.json): pinned commands, limits, xy steps."""
import json
import unittest
from pathlib import Path

import numpy as np

from hf_identification_trajectory import array_sha256, generate_trajectory

ROOT = Path(__file__).resolve().parent
CAMPAIGN = ROOT / "extra3_20261012"
PLAN = CAMPAIGN / "extra3_plan.json"


@unittest.skipUnless(PLAN.is_file(), "extra measurement 3 plan not present")
class Extra3PlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = json.loads(PLAN.read_text(encoding="utf-8"))
        cls.by_name = {e["name"]: e for e in cls.plan["experiments"]}

    def test_counts(self):
        exps = self.plan["experiments"]
        self.assertEqual(len(exps), 28)
        self.assertEqual(len(self.by_name), 28)
        dirs = [e.get("feedforward_design", {}).get("design_dir") for e in exps]
        self.assertEqual(dirs.count("ff_extra3"), 24)
        for name in self.by_name:
            self.assertLessEqual(len(name), 27, name)

    def test_imported_pinned_and_within_limits(self):
        limits = self.plan["defaults"]["safety_limits"]
        for e in self.plan["experiments"]:
            if e["kind"] != "imported":
                continue
            with np.load(CAMPAIGN / e["source_npz"], allow_pickle=False) as z:
                pos = np.array(z["positions_mm"], dtype=float)
                hz = float(z["sample_hz"])
            self.assertEqual(array_sha256(pos), e["positions_sha256"], e["name"])
            vel = np.gradient(pos, 1.0 / hz, axis=0)
            acc = np.gradient(vel, 1.0 / hz, axis=0)
            self.assertLessEqual(np.linalg.norm(pos, axis=1).max(), limits["max_offset_mm"], e["name"])
            self.assertLessEqual(np.linalg.norm(vel, axis=1).max(), limits["max_speed_mm_s"], e["name"])
            self.assertLessEqual(np.linalg.norm(acc, axis=1).max(), limits["max_acceleration_mm_s2"], e["name"])

    def test_diagonal_staircase_moves_x_and_y_together(self):
        for name, sign in (("kdiag_p45_S105_6jumps", 1.0), ("kdiag_m45_S105_6jumps", -1.0)):
            offset, metadata = generate_trajectory(self.by_name[name], self.plan["defaults"])
            offset = np.asarray(offset)
            self.assertEqual(metadata["axis"], "xy")
            self.assertEqual(offset.shape, (140000, 3))
            self.assertTrue(np.all(offset[:, 2] == 0.0))
            np.testing.assert_allclose(offset[:, 1], sign * offset[:, 0], atol=1e-15)
            jumps = np.flatnonzero(np.any(np.diff(offset, axis=0) != 0.0, axis=1))
            self.assertEqual(len(jumps), 6)
            self.assertAlmostEqual(float(np.linalg.norm(offset, axis=1).max()), 1.05, places=12)


if __name__ == "__main__":
    unittest.main()
