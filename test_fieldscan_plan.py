"""Hardware-free tests for the 15 V field-mapping session (fieldscan_15V): 7 surfaces x a/b w scans."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

import numpy as np

from stereo_acoustools_3d_recording_core import safe_run_dir_base

CAMPAIGN = Path("fieldscan_15V")
PLAN = CAMPAIGN / "fieldscan_15V_plan.json"
EXPORT = CAMPAIGN / "export_fieldscan"
DEFAULT_OUTPUT_ROOT = Path(
    r"C:\Users\digit\Documents\scripts\python\eventcam_control"
    r"\stereo_acoustools_3d_records_V15"
)
SURFACES = {  # name -> (plane, radius, out-of-plane axis, offset)
    "XZ_R28": ("XZ", 28.0, "y", 0.0),
    "YZ_R28": ("YZ", 28.0, "x", 0.0),
    "XY_R28": ("XY", 28.0, "z", 0.0),
    "XZ_R24yp8": ("XZ", 24.0, "y", 8.0),
    "XZ_R24ym8": ("XZ", 24.0, "y", -8.0),
    "XZ_R20yp16": ("XZ", 20.0, "y", 16.0),
    "XZ_R20ym16": ("XZ", 20.0, "y", -16.0),
}
AXIS = {"x": 0, "y": 1, "z": 2}


def plan() -> dict:
    return json.loads(PLAN.read_text(encoding="utf-8"))


def scans() -> list[dict]:
    return [entry for entry in plan()["experiments"] if entry["name"].startswith("wscan_")]


def export_matches_plan() -> bool:
    manifest = EXPORT / "export_manifest.json"
    return manifest.is_file() and json.loads(manifest.read_text(encoding="utf-8")).get(
        "source_plan_sha256"
    ) == hashlib.sha256(PLAN.read_bytes()).hexdigest()


class FieldScanPlanTests(unittest.TestCase):
    def test_plan_holds_the_kchecks_and_the_fourteen_scans_under_the_scaleup_limits(self) -> None:
        document = plan()
        self.assertEqual(
            [entry["name"] for entry in document["experiments"]],
            ["kcheck_x_S105_6jumps", "kcheck_y_S105_6jumps", "kcheck_z_S105_6jumps"]
            + [f"wscan_{surface}_{side}" for surface in SURFACES for side in "ab"],
        )
        scaleup = json.loads(Path("scaleup_20260924/scaleup_plan.json").read_text(encoding="utf-8"))
        self.assertEqual(document["defaults"], scaleup["defaults"])
        for entry in scans():
            self.assertNotIn("safety_limits", entry)   # no per-run raise: the approved limits apply

    def test_scans_are_pinned_and_describe_their_surface(self) -> None:
        for entry in scans():
            surface, side = entry["name"][len("wscan_"):].rsplit("_", 1)
            plane, radius, axis, offset = SURFACES[surface]
            design = entry["feedforward_design"]
            self.assertEqual((design["plane"], design["plane_name"]), (plane, surface))
            self.assertEqual((design["radius_mm"], design["plane_offset_axis"]), (radius, axis))
            self.assertEqual(design["plane_offset_mm"], offset)
            self.assertEqual(design["direction"], "counter-clockwise" if side == "a" else "clockwise")
            self.assertFalse(design["command_differs_from_reference"])
            with np.load(CAMPAIGN / entry["source_npz"]) as data:
                positions = np.ascontiguousarray(np.asarray(data["positions_mm"], dtype=np.float64))
            self.assertEqual(hashlib.sha256(positions.tobytes()).hexdigest(), entry["positions_sha256"])
            # the surface is held exactly between the 0.5 s moves at both ends (the first issue of
            # the offset scans swept the out-of-plane axis between 0 and the offset at 1 Hz instead)
            middle = positions[5000:-5000, AXIS[axis]]
            self.assertEqual(float(np.max(np.abs(middle - offset))), 0.0, entry["name"])
            self.assertEqual(float(np.max(np.linalg.norm(positions[[0, -1]], axis=1))), 0.0)

    def test_the_xz_r28_scan_is_the_scaleup_file(self) -> None:
        for side in "ab":
            name = f"wscan_XZ_R28_{side}.npz"
            self.assertEqual(
                (CAMPAIGN / "source" / name).read_bytes(),
                Path("scaleup_20260924/source", name).read_bytes(),
            )

    def test_run_directories_keep_the_surface_names(self) -> None:
        for index, entry in enumerate(plan()["experiments"]):
            shape = "step_response_identification" if entry["kind"] == "staircase" else "feedforward_validation"
            label = f"{index:03d}_{entry['name']}_scale100_V15"
            base = safe_run_dir_base(DEFAULT_OUTPUT_ROOT, shape, label, "20260924_010203")
            self.assertEqual(base, f"{shape}_{label}_20260924_010203", entry["name"])

    @unittest.skipUnless(export_matches_plan(), "the export is older than the plan")
    def test_export_reproduces_the_supplied_commands_bitwise(self) -> None:
        for entry in scans():
            exported = np.load(EXPORT / entry["name"] / "command_trajectory.npz")["offset_mm"]
            with np.load(CAMPAIGN / entry["source_npz"]) as supplied:
                np.testing.assert_array_equal(exported, supplied["positions_mm"])


if __name__ == "__main__":
    unittest.main()
