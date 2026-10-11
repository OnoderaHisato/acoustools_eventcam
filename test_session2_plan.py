"""Session 2 on 2026-10-11 (session2_20261011/session2_plan.json): pinned commands, limits and run names."""
import json
import unittest
from pathlib import Path

import numpy as np

from hf_identification_trajectory import array_sha256

ROOT = Path(__file__).resolve().parent
CAMPAIGN = ROOT / "session2_20261011"
PLAN = CAMPAIGN / "session2_plan.json"


@unittest.skipUnless(PLAN.is_file(), "session 2 plan not present")
class Session2PlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = json.loads(PLAN.read_text(encoding="utf-8"))
        cls.exps = cls.plan["experiments"]
        cls.by_name = {e["name"]: e for e in cls.exps}

    def test_counts_and_design_dirs(self):
        dirs = [e.get("feedforward_design", {}).get("design_dir") for e in self.exps]
        self.assertEqual(len(self.exps), 57)
        self.assertEqual(dirs.count("ff_diverse"), 37)
        self.assertEqual(dirs.count("ff_nn_v15c"), 6)
        self.assertEqual(len(self.by_name), len(self.exps))
        priorities = [e["feedforward_design"].get("priority") for e in self.exps
                      if e.get("feedforward_design", {}).get("design_dir") == "ff_diverse"]
        self.assertEqual(priorities.count("A"), 29)
        self.assertEqual(priorities.count("B"), 8)

    def test_commands_pinned_and_within_limits(self):
        limits = self.plan["defaults"]["safety_limits"]
        for e in self.exps:
            if e["kind"] != "imported":
                continue
            with np.load(CAMPAIGN / e["source_npz"], allow_pickle=False) as z:
                pos = np.array(z["positions_mm"], dtype=float)
                ref = np.array(z["reference_mm"], dtype=float)
                hz = float(z["sample_hz"])
            self.assertEqual(array_sha256(pos), e["positions_sha256"], e["name"])
            vel = np.gradient(pos, 1.0 / hz, axis=0)
            acc = np.gradient(vel, 1.0 / hz, axis=0)
            self.assertLessEqual(np.linalg.norm(pos, axis=1).max(), limits["max_offset_mm"], e["name"])
            self.assertLessEqual(np.linalg.norm(vel, axis=1).max(), limits["max_speed_mm_s"], e["name"])
            self.assertLessEqual(np.linalg.norm(acc, axis=1).max(), limits["max_acceleration_mm_s2"], e["name"])
            self.assertLessEqual(np.linalg.norm(pos - ref, axis=1).max(),
                                 limits["max_command_reference_distance_mm"], e["name"])
            self.assertLessEqual(np.linalg.norm(pos[[0, -1]], axis=1).max(),
                                 limits["max_endpoint_offset_mm"], e["name"])

    def test_run_names_fit_the_folder_name(self):
        # 27 characters is the longest label that keeps the _V15 tag under stereo_acoustools_3d_records_V15c.
        for name in self.by_name:
            self.assertLessEqual(len(name), 27, name)
        for prefix in ("random_", "eight_v_", "infinity_", "rect14x10_", "tri14_", "helix_", "aug_"):
            self.assertTrue(any(n.startswith(prefix) for n in self.by_name), prefix)

    def test_compensation_pairs_exist(self):
        for shape in ("heart_s7_f10", "heart_s7_f7", "cardioid_a5p4_f10", "cardioid_a10_f10",
                      "lissajous_s15_f10", "circle_s20_f10"):
            self.assertIn(f"{shape}_OFF", self.by_name)
            nn = self.by_name[f"{shape}_C_nn_M1z"]
            self.assertTrue(nn["feedforward_design"]["command_differs_from_reference"])
            self.assertEqual(nn["campaign"], "session2_20261011")


if __name__ == "__main__":
    unittest.main()
