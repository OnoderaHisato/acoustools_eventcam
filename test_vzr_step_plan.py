"""Hardware-free tests for the step-type vzr records (multi-axis staircase, 2026-09-19)."""

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


CAMPAIGN_DIR = Path("vzr_step_20260919")
OFFICIAL_PLAN = CAMPAIGN_DIR / "vzr_step_plan.json"
OFFICIAL_EXPORT = CAMPAIGN_DIR / "export_vzr_step"
EXPECTED_ORDER = [
    "vzrstep_XS1", "vzrstep_XS2", "vzrstep_XS3",
    "vzrstep_YS1", "vzrstep_YS2", "vzrstep_YS3",
]
EXPECTED_DESIGN = {
    "vzrstep_XS1": ("x", 0.6, 0.4, "C"),
    "vzrstep_XS2": ("x", 0.8, 0.5, "C"),
    "vzrstep_XS3": ("x", 1.0, 0.6, "D"),
    "vzrstep_YS1": ("y", 0.6, 0.4, "C"),
    "vzrstep_YS2": ("y", 0.8, 0.5, "C"),
    "vzrstep_YS3": ("y", 1.0, 0.6, "D"),
}
DEFAULT_OUTPUT_ROOT = Path(
    r"C:\Users\digit\Documents\scripts\python\eventcam_control"
    r"\stereo_acoustools_3d_records_vzr_step"
)
# Same block as the analysis-side make_vzr_step_trajectory.py: (horizontal, z) signs.
REFERENCE_SEQ = [(1, 0), (1, 1), (0, 0), (-1, -1), (-1, 0), (0, 0), (1, -1), (0, 0), (-1, 1), (0, 0)]


def reference_build(sh, sz, horiz="x", hold=0.3, blocks=3, init_hold=0.5, final_hold=0.5, fs=10000.0):
    """Inline copy of build() from the analysis-side generator (stagger_ms=0)."""
    ih = "xyz".index(horiz)
    n0 = int(round(init_hold * fs))
    nh = int(round(hold * fs))
    nf = int(round(final_hold * fs))
    seq = REFERENCE_SEQ * blocks
    u = np.zeros((n0 + nh * (len(seq) - 1) + nf, 3))
    for k, (a, b) in enumerate(seq):
        lo = n0 + k * nh
        u[lo:, ih] = a * sh
        u[lo:, 2] = b * sz
    return u


def official_export_matches_plan() -> bool:
    manifest_path = OFFICIAL_EXPORT / "export_manifest.json"
    if not manifest_path.is_file():
        return False
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return manifest.get("source_plan_sha256") == hashlib.sha256(OFFICIAL_PLAN.read_bytes()).hexdigest()


def _defaults(**limits) -> dict:
    safety = {
        "max_offset_mm": 1.2,
        "max_step_mm": 1.2,
        "max_speed_mm_s": 100.0,
        "max_acceleration_mm_s2": 28000.0,
    }
    safety.update(limits)
    return {"sample_hz": 10000, "duration_sec": 1.0, "ramp_sec": 0.0, "safety_limits": safety}


def _spec(levels, **extra) -> dict:
    spec = {
        "name": "xz_steps",
        "kind": "staircase",
        "tier": "C",
        "hold_sec": 0.01,
        "initial_hold_sec": 0.02,
        "final_hold_sec": 0.03,
        "level_sequence_mm": levels,
        "enabled": True,
    }
    spec.update(extra)
    return spec


SMALL_BLOCK = [[0.8, 0.0, 0.0], [0.8, 0.0, 0.5], [0.0, 0.0, 0.0], [-0.8, 0.0, -0.5], [0.0, 0.0, 0.0]]


class MultiAxisStaircaseGenerationTests(unittest.TestCase):
    def test_official_specs_are_bitwise_equal_to_the_reference_generator(self) -> None:
        plan = hf.load_plan(OFFICIAL_PLAN)
        self.assertEqual([spec["name"] for spec in plan["experiments"]], EXPECTED_ORDER)
        for spec in plan["experiments"]:
            horiz, sh, sz, tier = EXPECTED_DESIGN[spec["name"]]
            with self.subTest(name=spec["name"]):
                offset_mm, metadata = hf.generate_trajectory(spec, plan["defaults"])
                expected = reference_build(sh, sz, horiz)
                self.assertEqual(offset_mm.shape, (97000, 3))
                self.assertTrue(np.array_equal(offset_mm, expected))
                self.assertFalse(np.any(np.signbit(offset_mm) & (offset_mm == 0.0)))
                self.assertEqual(metadata["axis"], horiz + "z")
                self.assertEqual(metadata["axes"], [horiz, "z"])
                self.assertEqual(metadata["step_response_tier"], tier)
                self.assertEqual(metadata["measurement_family"], "step_response_identification")
                self.assertEqual(metadata["campaign"], "vzr_step_20260919")
                self.assertAlmostEqual(metadata["duration_sec"], 9.7)
                self.assertEqual(metadata["safety_scale_applied"], 1.0)
                detail = metadata["generation_detail"]
                self.assertEqual(detail["n_holds"], 31)
                self.assertEqual(detail["n_jumps"], 30)
                self.assertEqual(
                    detail["jump_kind_counts"], {horiz: 6, horiz + "z": 18, "z": 6}
                )
                self.assertAlmostEqual(detail["max_step_mm"], float(np.hypot(sh, sz)), places=12)
                self.assertEqual(detail["jump_sample_indices"][:3], [5000, 8000, 11000])
                self.assertFalse(detail["safety"]["derivative_limits_applied"])
                self.assertLess(detail["safety"]["escape_margin_fraction"], 0.55)
                # Limits are pinned just above the designed jump.
                limit = spec["safety_limits"]["max_step_mm"]
                self.assertGreater(limit, detail["max_step_mm"])
                self.assertLess(limit, detail["max_step_mm"] + 0.01)

    def test_small_sequence_layout_and_repeats(self) -> None:
        offset_mm, metadata = hf.generate_trajectory(
            _spec(SMALL_BLOCK, repeats_within_run=2), _defaults()
        )
        detail = metadata["generation_detail"]
        # 11 holds: initial 200 samples, nine of 100, final 300
        self.assertEqual(offset_mm.shape[0], 200 + 9 * 100 + 300)
        self.assertEqual(detail["n_holds"], 11)
        self.assertEqual(detail["jump_kinds"], ["x", "z", "xz", "xz", "xz"] * 2)
        self.assertTrue(np.array_equal(offset_mm[199], [0.0, 0.0, 0.0]))
        self.assertTrue(np.array_equal(offset_mm[200], [0.8, 0.0, 0.0]))
        self.assertTrue(np.array_equal(offset_mm[300], [0.8, 0.0, 0.5]))
        self.assertTrue(np.array_equal(offset_mm[-1], [0.0, 0.0, 0.0]))
        self.assertEqual(len({tuple(row) for row in offset_mm.tolist()}), 4)
        self.assertEqual(detail["levels_xyz_mm"][1], [0.8, 0.0, 0.0])

    def test_simultaneous_jump_is_checked_by_its_vector_norm(self) -> None:
        # 0.85 mm allows the 0.8 mm and 0.5 mm single-axis jumps but not the 0.943 mm diagonal.
        with self.assertRaisesRegex(ValueError, "max_step_mm|max_offset_mm"):
            hf.generate_trajectory(_spec(SMALL_BLOCK), _defaults(max_offset_mm=0.85, max_step_mm=0.85))
        with self.assertRaisesRegex(ValueError, "max_step_mm"):
            hf.generate_trajectory(_spec(SMALL_BLOCK), _defaults(max_offset_mm=1.2, max_step_mm=0.85))
        hf.generate_trajectory(_spec(SMALL_BLOCK), _defaults(max_offset_mm=0.95, max_step_mm=0.95))

    def test_escape_boundary_applies_to_diagonal_jumps(self) -> None:
        levels = [[1.6, 0.0, 1.5], [0.0, 0.0, 0.0]]
        with self.assertRaisesRegex(ValueError, "escape boundary"):
            hf.generate_trajectory(_spec(levels), _defaults(max_offset_mm=3.0, max_step_mm=3.0))

    def test_invalid_level_sequences_are_rejected(self) -> None:
        cases = {
            "does not end at the centre": ([[0.5, 0.0, 0.0]], {}, "end at the centre"),
            "same level twice": ([[0.5, 0.0, 0.0], [0.5, 0.0, 0.0], [0.0, 0.0, 0.0]], {}, "twice in a row"),
            "starts at the centre": ([[0.0, 0.0, 0.0]], {}, "twice in a row"),
            "wrong shape": ([[0.5, 0.0], [0.0, 0.0]], {}, r"\[x, y, z\]"),
            "not finite": ([[float("nan"), 0.0, 0.0], [0.0, 0.0, 0.0]], {}, "finite"),
            "combined with amplitudes": (SMALL_BLOCK, {"amplitudes_mm": [0.5]}, "amplitudes_mm"),
            "combined with order seed": (SMALL_BLOCK, {"order_seed": 1}, "order_seed"),
            "axis mismatch": (SMALL_BLOCK, {"axis": "x"}, "does not match"),
            "zero repeats": (SMALL_BLOCK, {"repeats_within_run": 0}, "repeats_within_run"),
        }
        for name, (levels, extra, pattern) in cases.items():
            with self.subTest(case=name):
                with self.assertRaisesRegex(ValueError, pattern):
                    hf.generate_trajectory(_spec(levels, **extra), _defaults())
        _offset, metadata = hf.generate_trajectory(_spec(SMALL_BLOCK, axis="xz"), _defaults())
        self.assertEqual(metadata["axis"], "xz")

    def test_single_axis_staircase_is_unchanged(self) -> None:
        spec = {
            "name": "kcheck_like",
            "kind": "staircase",
            "tier": "C",
            "axis": "z",
            "hold_sec": 0.01,
            "amplitudes_mm": [1.05],
            "directions": [1, -1, 1],
            "enabled": True,
        }
        offset_mm, metadata = hf.generate_trajectory(spec, _defaults())
        expected = np.zeros((700, 3))
        expected[100:200, 2] = 1.05
        expected[300:400, 2] = -1.05
        expected[500:600, 2] = 1.05
        self.assertTrue(np.array_equal(offset_mm, expected))
        self.assertEqual(metadata["axis"], "z")
        self.assertNotIn("axes", metadata)
        self.assertEqual(metadata["generation_detail"]["levels_mm"], [0.0, 1.05, 0.0, -1.05, 0.0, 1.05, 0.0])

    def test_export_writes_csv_and_a_real_multi_axis_preview(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = {"schema_version": 1, "defaults": _defaults(), "experiments": [_spec(SMALL_BLOCK)]}
            (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            hf.export_plan(root / "plan.json", root / "export", False, False)
            run_dir = root / "export" / "xz_steps"
            self.assertGreater((run_dir / "trajectory_preview.png").stat().st_size, 1000)
            self.assertTrue((run_dir / "command_offset_log.csv").is_file())
            _path, runs = load_hf_export_runs(root / "export")
            trajectory = prepare_hf_trajectory(runs[0], center_m=(0.0, 0.0, 0.1), command_scale=0.5)
            positions = np.asarray(trajectory.positions)
            self.assertEqual(trajectory.shape_name, "step_response_identification")
            self.assertEqual(trajectory.parameters["axis"], "xz")
            self.assertAlmostEqual(float(positions[:, 0].max()), 0.4e-3, places=12)
            self.assertAlmostEqual(float(positions[:, 2].max()), 0.1 + 0.25e-3, places=12)
            self.assertTrue(trajectory.trajectory_source["intentional_discontinuous_command"])


class OfficialVzrStepPlanTests(unittest.TestCase):
    def test_run_directories_are_not_shortened(self) -> None:
        from stereo_acoustools_3d_recording_core import safe_run_dir_base

        plan = hf.load_plan(OFFICIAL_PLAN)
        stamp = "20260919_120000"
        for index, spec in enumerate(plan["experiments"]):
            for scale_tag in ("scale100", "scale050"):
                with self.subTest(name=spec["name"], scale=scale_tag):
                    label = f"{index:03d}_{spec['name']}_{scale_tag}"
                    base = safe_run_dir_base(
                        DEFAULT_OUTPUT_ROOT, "step_response_identification", label, stamp
                    )
                    self.assertEqual(base, f"step_response_identification_{label}_{stamp}")

    @unittest.skipUnless(official_export_matches_plan(), "export absent or older than the plan")
    def test_official_export_loads_for_hardware(self) -> None:
        _path, runs = load_hf_export_runs(OFFICIAL_EXPORT)
        self.assertEqual([run.name for run in runs], EXPECTED_ORDER)
        by_name = {run.name: run for run in runs}
        trajectory = prepare_hf_trajectory(
            by_name["vzrstep_XS2"], center_m=(0.0, 0.0, 0.0), command_scale=1.0
        )
        self.assertEqual(len(trajectory.positions), 97000)
        self.assertEqual(trajectory.run_label, "001_vzrstep_XS2_scale100")
        # centre, (+,0), (+,+), (-,-), (-,0), (+,-), (-,+): seven holograms per run
        self.assertEqual(len(set(trajectory.positions)), 7)
        self.assertTrue(
            np.allclose(
                np.asarray(trajectory.positions) * 1e3, reference_build(0.8, 0.5, "x"), rtol=0.0, atol=1e-12
            )
        )


class VzrStepAutoEntryTests(unittest.TestCase):
    def _export(self, root: Path) -> Path:
        specs = [
            _spec(SMALL_BLOCK, name="vzrstep_XS1"),
            _spec([[0.0, 0.8, 0.0], [0.0, 0.8, 0.5], [0.0, 0.0, 0.0]], name="vzrstep_YS2"),
        ]
        plan = {"schema_version": 1, "defaults": _defaults(), "experiments": specs}
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

    def test_dry_run_never_opens_hardware(self) -> None:
        with TemporaryDirectory() as temporary:
            export_dir = self._export(Path(temporary))
            result, events, _playback = self._run(["--hf-export-dir", str(export_dir), "--dry-run"])
            self.assertEqual(result, 0)
            events.open_hw.assert_not_called()
            events.record.assert_not_called()

    def test_hardware_runs_need_the_step_response_ack(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export(root)
            base = ["--hf-export-dir", str(export_dir), "--output-dir", str(root / "records")]
            cases = {
                "no ack": base,
                "vzr sine ack is not the step ack": base + ["--acknowledge-vzr-protocol"],
                "keep going": base + ["--acknowledge-step-response-risk", "--keep-going"],
            }
            for name, argv in cases.items():
                with self.subTest(case=name):
                    result, events, _playback = self._run(argv)
                    self.assertEqual(result, 2)
                    events.open_hw.assert_not_called()
                    events.record.assert_not_called()
            self.assertFalse((root / "records").exists())

    def test_repeated_labels_are_recorded_in_order_with_forced_checkpoints(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export(root)
            argv = ["--hf-export-dir", str(export_dir), "--output-dir", str(root / "records")]
            for request in ("vzrstep_XS1=1.0", "vzrstep_YS2=1.0", "vzrstep_YS2=0.5"):
                argv += ["--hf-run", request]
            argv += ["--acknowledge-step-response-risk", "--no-preview"]
            result, events, _playback = self._run(argv)
            self.assertEqual(result, 0)
            events.open_hw.assert_called_once()
            calls = events.record.call_args_list
            self.assertEqual(
                [call.kwargs["prepared_trajectory"].run_label for call in calls],
                ["000_vzrstep_XS1_scale100", "001_vzrstep_YS2_scale100", "001_vzrstep_YS2_scale050"],
            )
            for call in calls:
                self.assertFalse(call.args[0].no_preview)
                self.assertTrue(call.kwargs["prompt_before_capture"])
                self.assertIsNone(call.kwargs["automatic_capture_retries"])
                self.assertEqual(
                    call.kwargs["automation_metadata"]["mode"], "step_response_identification"
                )
            session = json.loads(
                next((root / "records").glob("auto_recording_session_*.json")).read_text(encoding="utf-8")
            )
            self.assertEqual(session["status"], "complete")

    def test_unattended_mode_keeps_only_the_first_checkpoint(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export(root)
            argv = [
                "--hf-export-dir", str(export_dir), "--output-dir", str(root / "records"),
                "--hf-run", "vzrstep_XS1=1.0", "--hf-run", "vzrstep_XS1=1.0",
                "--acknowledge-step-response-risk", "--unattended-after-first-checkpoint",
            ]
            result, events, playback = self._run(argv)
            self.assertEqual(result, 0)
            first, second = events.record.call_args_list
            self.assertTrue(first.kwargs["prompt_before_capture"])
            self.assertIs(first.kwargs["precomputed_playback"], playback)
            self.assertFalse(second.kwargs["prompt_before_capture"])
            self.assertTrue(second.args[0].no_preview)
            self.assertEqual(second.kwargs["automatic_capture_retries"], 2)


if __name__ == "__main__":
    unittest.main()
