"""Hardware-free tests for the large single-axis steps and the default checkpoint mode (2026-09-20)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock
import unittest

import numpy as np

import acoustools_stereo_eventcam_3d_recording_auto as auto_record
import hf_identification_trajectory as hf
from stereo_acoustools_3d_hf_common import load_hf_export_runs, prepare_hf_trajectory


CAMPAIGN_DIR = Path("large_step_20260920")
OFFICIAL_PLAN = CAMPAIGN_DIR / "large_step_plan.json"
OFFICIAL_EXPORT = CAMPAIGN_DIR / "export_large_step"
EXPECTED_ORDER = [
    "largestep_XL12", "largestep_XL15", "largestep_XL18", "largestep_XL20",
    "largestep_YL12", "largestep_YL15", "largestep_YL18", "largestep_YL20",
]
# name -> (axis, step in mm, tier)
EXPECTED_DESIGN = {
    "largestep_XL12": ("x", 1.2, "D"), "largestep_XL15": ("x", 1.5, "E"),
    "largestep_XL18": ("x", 1.8, "F"), "largestep_XL20": ("x", 2.0, "G"),
    "largestep_YL12": ("y", 1.2, "D"), "largestep_YL15": ("y", 1.5, "E"),
    "largestep_YL18": ("y", 1.8, "F"), "largestep_YL20": ("y", 2.0, "G"),
}
DEFAULT_OUTPUT_ROOT = Path(
    r"C:\Users\digit\Documents\scripts\python\eventcam_control"
    r"\stereo_acoustools_3d_records_large_step"
)


def reference_build(step, axis="x", hold=0.4, cycles=3, init_hold=0.5, final_hold=0.5, fs=1e4):
    """Inline copy of build() from the analysis-side make_large_step_trajectory.py."""
    seq = [1, 0, -1, 0] * cycles
    n0, nh, nf = int(init_hold * fs), int(hold * fs), int(final_hold * fs)
    u = np.zeros((n0 + nh * (len(seq) - 1) + nf, 3))
    i = "xyz".index(axis)
    for k, a in enumerate(seq):
        u[n0 + k * nh:, i] = a * step
    return u


def official_export_matches_plan() -> bool:
    manifest_path = OFFICIAL_EXPORT / "export_manifest.json"
    if not manifest_path.is_file():
        return False
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return manifest.get("source_plan_sha256") == hashlib.sha256(OFFICIAL_PLAN.read_bytes()).hexdigest()


class LargeStepPlanTests(unittest.TestCase):
    def test_official_specs_are_bitwise_equal_to_the_reference_generator(self) -> None:
        plan = hf.load_plan(OFFICIAL_PLAN)
        self.assertEqual([spec["name"] for spec in plan["experiments"]], EXPECTED_ORDER)
        self.assertEqual(plan["measurement_family"], "step_response_identification")
        for spec in plan["experiments"]:
            axis, step, tier = EXPECTED_DESIGN[spec["name"]]
            with self.subTest(name=spec["name"]):
                offset_mm, metadata = hf.generate_trajectory(spec, plan["defaults"])
                self.assertTrue(np.array_equal(offset_mm, reference_build(step, axis)))
                self.assertEqual(offset_mm.shape, (54000, 3))
                self.assertEqual(metadata["axis"], axis)
                self.assertEqual(metadata["step_response_tier"], tier)
                self.assertAlmostEqual(metadata["duration_sec"], 5.4)
                detail = metadata["generation_detail"]
                self.assertEqual(detail["n_holds"], 13)
                self.assertEqual(detail["n_jumps"], 12)
                self.assertAlmostEqual(detail["max_step_mm"], step, places=12)
                # The command never jumps straight from +S to -S.
                self.assertEqual(detail["step_sizes_mm"], [round(step, 6)])
                self.assertLess(detail["safety"]["escape_margin_fraction"], 1.0)
                # The limits are pinned just above the designed step.
                limit = spec["safety_limits"]["max_step_mm"]
                self.assertGreater(limit, step)
                self.assertLess(limit, step + 0.1)

    def test_a_larger_step_than_the_pinned_limit_is_rejected(self) -> None:
        plan = hf.load_plan(OFFICIAL_PLAN)
        spec = dict(plan["experiments"][0], amplitudes_mm=[1.5])
        with self.assertRaisesRegex(ValueError, "max_step_mm|max_offset_mm"):
            hf.generate_trajectory(spec, plan["defaults"])

    def test_run_directories_are_not_shortened(self) -> None:
        from stereo_acoustools_3d_recording_core import safe_run_dir_base

        plan = hf.load_plan(OFFICIAL_PLAN)
        for index, spec in enumerate(plan["experiments"]):
            for scale_tag in ("scale100", "scale050"):
                with self.subTest(name=spec["name"], scale=scale_tag):
                    label = f"{index:03d}_{spec['name']}_{scale_tag}"
                    base = safe_run_dir_base(
                        DEFAULT_OUTPUT_ROOT, "step_response_identification", label, "20260920_230000"
                    )
                    self.assertEqual(base, f"step_response_identification_{label}_20260920_230000")

    @unittest.skipUnless(official_export_matches_plan(), "export absent or older than the plan")
    def test_official_export_loads_for_hardware(self) -> None:
        _path, runs = load_hf_export_runs(OFFICIAL_EXPORT)
        self.assertEqual([run.name for run in runs], EXPECTED_ORDER)
        by_name = {run.name: run for run in runs}
        trajectory = prepare_hf_trajectory(
            by_name["largestep_XL20"], center_m=(0.0, 0.0, 0.0), command_scale=1.0
        )
        self.assertEqual(len(trajectory.positions), 54000)
        self.assertEqual(trajectory.run_label, "003_largestep_XL20_scale100")
        # Centre, +2 mm and -2 mm: three holograms for the whole run.
        self.assertEqual(len(set(trajectory.positions)), 3)
        self.assertTrue(
            np.allclose(np.asarray(trajectory.positions) * 1e3, reference_build(2.0, "x"), atol=1e-12)
        )


class LargeStepAutoEntryTests(unittest.TestCase):
    def _export(self, root: Path) -> Path:
        def spec(name: str, axis: str, step: float) -> dict:
            return {
                "name": name,
                "kind": "staircase",
                "tier": "D",
                "axis": axis,
                "hold_sec": 0.01,
                "initial_hold_sec": 0.02,
                "final_hold_sec": 0.02,
                "amplitudes_mm": [step],
                "directions": [1, -1],
                "repeats_within_run": 1,
                "enabled": True,
            }

        plan = {
            "schema_version": 1,
            "defaults": {
                "sample_hz": 10000,
                "duration_sec": 0.1,
                "ramp_sec": 0.0,
                "safety_limits": {
                    "max_offset_mm": 2.05,
                    "max_step_mm": 2.05,
                    "max_speed_mm_s": 100.0,
                    "max_acceleration_mm_s2": 28000.0,
                },
            },
            "experiments": [spec("xl12", "x", 1.2), spec("xl15", "x", 1.5)],
        }
        (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        with mock.patch.object(
            hf, "write_preview", side_effect=lambda path, *_a, **_k: path.write_bytes(b"p")
        ):
            hf.export_plan(root / "plan.json", root / "export", False, False)
        return root / "export"

    def _run(self, argv: list[str]):
        events = mock.Mock()
        session = SimpleNamespace(current_pos=(0.0, 0.0, 0.0))
        playback = SimpleNamespace(name="first-run-playback")
        events.open_hw.return_value = session
        events.precompute.return_value = playback
        events.record.side_effect = lambda *_a, **_k: (0, None)
        with (
            mock.patch.object(auto_record, "open_recording_hardware_session", events.open_hw),
            mock.patch.object(auto_record, "shutdown_recording_hardware_session", events.close_hw),
            mock.patch.object(auto_record, "precompute_hologram_playback", events.precompute),
            mock.patch.object(auto_record, "run_recording", events.record),
        ):
            result = auto_record.main(argv)
        return result, events, playback

    def test_dry_run_and_missing_ack_never_open_hardware(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export(root)
            base = ["--hf-export-dir", str(export_dir), "--output-dir", str(root / "records")]
            for name, argv in {
                "dry run": base + ["--dry-run"],
                "no ack": base,
                "keep going": base + ["--acknowledge-step-response-risk", "--keep-going"],
            }.items():
                with self.subTest(case=name):
                    result, events, _playback = self._run(argv)
                    self.assertEqual(result, 0 if name == "dry run" else 2)
                    events.open_hw.assert_not_called()
                    events.record.assert_not_called()

    def test_prompt_on_capture_failure_keeps_the_interactive_retry(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export(root)
            output_root = root / "records"
            result, events, playback = self._run(
                [
                    "--hf-export-dir", str(export_dir),
                    "--output-dir", str(output_root),
                    "--acknowledge-step-response-risk",
                    "--unattended-after-first-checkpoint",
                    "--prompt-on-capture-failure",
                ]
            )
            self.assertEqual(result, 0)
            first, second = events.record.call_args_list
            # Only the first run stops for the operator.
            self.assertFalse(first.args[0].no_preview)
            self.assertTrue(first.kwargs["prompt_before_capture"])
            self.assertIs(first.kwargs["precomputed_playback"], playback)
            self.assertTrue(second.args[0].no_preview)
            self.assertFalse(second.kwargs["prompt_before_capture"])
            # None keeps the recording core's own "re-record? (Y/n)" prompt.
            for call in events.record.call_args_list:
                self.assertIsNone(call.kwargs["automatic_capture_retries"])
            session = json.loads(
                next(output_root.glob("auto_recording_session_*.json")).read_text(encoding="utf-8")
            )
            self.assertTrue(session["unattended_after_first_checkpoint"])
            self.assertTrue(session["prompt_on_capture_failure"])
            self.assertIsNone(session["automatic_capture_retry_limit"])

    def test_automatic_retries_still_available_and_mutually_exclusive(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export(root)
            base = [
                "--hf-export-dir", str(export_dir),
                "--output-dir", str(root / "records"),
                "--acknowledge-step-response-risk",
            ]
            result, events, _playback = self._run(
                base + ["--unattended-after-first-checkpoint", "--automatic-capture-retries", "3"]
            )
            self.assertEqual(result, 0)
            for call in events.record.call_args_list:
                self.assertEqual(call.kwargs["automatic_capture_retries"], 3)
            rejected = {
                "prompt without unattended": base + ["--prompt-on-capture-failure"],
                "prompt with automatic retries": base
                + [
                    "--unattended-after-first-checkpoint",
                    "--prompt-on-capture-failure",
                    "--automatic-capture-retries", "2",
                ],
            }
            for name, argv in rejected.items():
                with self.subTest(case=name):
                    result, events, _playback = self._run(argv)
                    self.assertEqual(result, 2)
                    events.open_hw.assert_not_called()

    def test_confirm_each_run_still_stops_before_every_run(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export(root)
            result, events, _playback = self._run(
                [
                    "--hf-export-dir", str(export_dir),
                    "--output-dir", str(root / "records"),
                    "--acknowledge-step-response-risk",
                ]
            )
            self.assertEqual(result, 0)
            for call in events.record.call_args_list:
                self.assertFalse(call.args[0].no_preview)
                self.assertTrue(call.kwargs["prompt_before_capture"])
                self.assertIsNone(call.kwargs["automatic_capture_retries"])


if __name__ == "__main__":
    unittest.main()
