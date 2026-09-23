#!/usr/bin/env python3
"""Tests for bounded HF command generation and stereo recording integration."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest import mock
import unittest

import numpy as np

import acoustools_stereo_eventcam_3d_recording_auto as auto_record
import hf_identification_trajectory as hf
from stereo_acoustools_3d_hf_common import (
    load_hf_export_runs,
    prepare_hf_trajectory,
)
from stereo_acoustools_3d_recording_core import copy_prepared_trajectory_artifacts


class HighFrequencyIdentificationTests(unittest.TestCase):
    def _write_small_plan(self, root: Path) -> Path:
        plan = {
            "schema_version": 1,
            "defaults": {
                "sample_hz": 10000,
                "duration_sec": 0.02,
                "ramp_sec": 0.002,
                "safety_limits": {
                    "max_offset_mm": 0.15,
                    "max_speed_mm_s": 100.0,
                    "max_acceleration_mm_s2": 28000.0,
                },
            },
            "experiments": [
                {
                    "name": "x_chirp_test",
                    "kind": "chirp",
                    "axis": "x",
                    "f_start_hz": 40.0,
                    "f_end_hz": 130.0,
                    "displacement_cap_mm": 0.12,
                    "target_acceleration_mm_s2": 20000.0,
                    "enabled": True,
                }
            ],
        }
        path = root / "plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        return path

    def _export_small_plan(self, root: Path) -> Path:
        export_dir = root / "export"
        plan_path = self._write_small_plan(root)

        def fake_preview(path: Path, *_args: object) -> None:
            path.write_bytes(b"preview")

        with mock.patch.object(hf, "write_preview", side_effect=fake_preview):
            hf.export_plan(plan_path, export_dir, include_disabled=False, write_csv=True)
        return export_dir

    def _export_step_plan(
        self, root: Path, *, tier: str = "A", repeats: int = 1
    ) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        plan = {
            "schema_version": 1,
            "defaults": {
                "sample_hz": 1000,
                "duration_sec": 1.0,
                "ramp_sec": 0.0,
                "safety_limits": {
                    "max_offset_mm": 0.5,
                    "max_step_mm": 0.5,
                    # Deliberately tiny: derivative limits do not describe an
                    # intentional discontinuous trap command.
                    "max_speed_mm_s": 1.0,
                    "max_acceleration_mm_s2": 1.0,
                },
            },
            "experiments": [
                {
                    "name": f"{tier}0_x_staircase_test",
                    "kind": "staircase",
                    "tier": tier,
                    "axis": "x",
                    "hold_sec": 0.01,
                    "initial_hold_sec": 0.02,
                    "final_hold_sec": 0.01,
                    "amplitudes_mm": [0.4],
                    "directions": [1],
                    "repeats_within_run": 1,
                    "repeats": repeats,
                    "order_seed": 1,
                    "enabled": True,
                }
            ],
        }
        if tier == "H":
            plan["experiments"][0]["allow_escape_boundary_probe"] = True
        plan_path = root / "step_plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        export_dir = root / "step_export"

        def fake_preview(path: Path, *_args: object) -> None:
            path.write_bytes(b"step-preview")

        with mock.patch.object(hf, "write_preview", side_effect=fake_preview):
            hf.export_plan(plan_path, export_dir, include_disabled=False, write_csv=False)
        return export_dir

    def test_official_plan_respects_all_vector_safety_limits(self) -> None:
        plan = hf.load_plan(Path("high_frequency_identification_plan.json"))
        enabled_count = 0
        for spec in plan["experiments"]:
            if not spec.get("enabled", True):
                continue
            enabled_count += int(spec.get("repeats", 1))
            offset, metadata = hf.generate_trajectory(spec, plan["defaults"])
            limits = metadata["safety_limits"]
            metrics = metadata["metrics"]
            self.assertLessEqual(
                metrics["max_abs_offset_mm"]["vector"], limits["max_offset_mm"] + 1e-9
            )
            self.assertLessEqual(
                metrics["max_speed_mm_s"]["vector"], limits["max_speed_mm_s"] + 1e-9
            )
            self.assertLessEqual(
                metrics["max_acceleration_mm_s2"]["vector"],
                limits["max_acceleration_mm_s2"] + 1e-6,
            )
            self.assertTrue(np.allclose(offset[[0, -1]], 0.0, atol=1e-12))
        self.assertEqual(enabled_count, 12)

    def test_hardware_loader_adds_centre_and_rechecks_half_scale(self) -> None:
        with TemporaryDirectory() as temporary:
            export_dir = self._export_small_plan(Path(temporary))
            manifest, runs = load_hf_export_runs(export_dir)
            trajectory = prepare_hf_trajectory(
                runs[0], center_m=(0.001, -0.002, 0.003), command_scale=0.5
            )

    def test_official_step_plan_has_three_safe_tier_a_runs(self) -> None:
        plan = hf.load_plan(Path("step_response_identification_plan.json"))
        enabled = [spec for spec in plan["experiments"] if spec.get("enabled", True)]
        self.assertEqual([spec["tier"] for spec in enabled], ["A", "A", "A"])
        self.assertEqual([spec["axis"] for spec in enabled], ["x", "y", "z"])
        for spec in enabled:
            offset, metadata = hf.generate_trajectory(spec, plan["defaults"])
            detail = metadata["generation_detail"]
            self.assertEqual(offset.shape, (68000, 3))
            self.assertEqual(metadata["duration_sec"], 6.8)
            self.assertEqual(metadata["measurement_family"], "step_response_identification")
            self.assertEqual(metadata["step_response_tier"], "A")
            self.assertFalse(metadata["derivative_metrics_interpretable"])
            self.assertEqual(detail["n_holds"], 17)
            self.assertEqual(detail["n_jumps"], 16)
            self.assertAlmostEqual(detail["max_step_mm"], 0.4)
            self.assertLess(detail["safety"]["escape_margin_fraction"], 0.2)
            self.assertTrue(np.allclose(offset[0], 0.0))
            self.assertTrue(np.allclose(offset[-1], 0.0))

    def test_official_step_plan_all_tiers_match_escalation_protocol(self) -> None:
        plan = hf.load_plan(Path("step_response_identification_plan.json"))
        expected = {
            "A": {"runs": 3, "duration": 6.8, "jumps": 16, "max_step": 0.4},
            "B": {"runs": 6, "duration": 9.2, "jumps": 24, "max_step": 0.7},
            "C": {"runs": 6, "duration": 11.6, "jumps": 32, "max_step": 1.05},
        }
        expanded_runs = {tier: 0 for tier in expected}
        for spec in plan["experiments"]:
            tier = spec["tier"]
            offset, metadata = hf.generate_trajectory(spec, plan["defaults"])
            detail = metadata["generation_detail"]
            expanded_runs[tier] += int(spec.get("repeats", 1))
            self.assertAlmostEqual(metadata["duration_sec"], expected[tier]["duration"])
            self.assertEqual(detail["n_jumps"], expected[tier]["jumps"])
            self.assertAlmostEqual(detail["max_step_mm"], expected[tier]["max_step"])
            self.assertLess(detail["safety"]["escape_margin_fraction"], 0.5)
            self.assertTrue(np.allclose(offset[[0, -1]], 0.0))
        self.assertEqual(
            expanded_runs,
            {tier: values["runs"] for tier, values in expected.items()},
        )

    def test_tier_b_step_plan_exports_only_six_ladder_runs(self) -> None:
        plan_path = Path("step_response_identification_tier_b_plan.json")
        plan = hf.load_plan(plan_path)
        self.assertEqual(len(plan["experiments"]), 3)
        self.assertEqual([spec["tier"] for spec in plan["experiments"]], ["B", "B", "B"])
        self.assertEqual([spec["axis"] for spec in plan["experiments"]], ["x", "y", "z"])
        self.assertTrue(all(spec.get("enabled", True) for spec in plan["experiments"]))

        with TemporaryDirectory() as temporary:
            manifest = hf.export_plan(
                plan_path,
                Path(temporary) / "tier_b_export",
                include_disabled=False,
                write_csv=False,
            )
        names = [run["name"] for run in manifest["runs"]]
        self.assertEqual(
            names,
            [
                "B0_x_staircase_ladder_r01",
                "B0_x_staircase_ladder_r02",
                "B1_y_staircase_ladder_r01",
                "B1_y_staircase_ladder_r02",
                "B2_z_staircase_ladder_r01",
                "B2_z_staircase_ladder_r02",
            ],
        )
        for run in manifest["runs"]:
            metadata = run["metadata"]
            detail = metadata["generation_detail"]
            self.assertEqual(metadata["step_response_tier"], "B")
            self.assertEqual(metadata["duration_sec"], 9.2)
            self.assertEqual(detail["n_jumps"], 24)
            self.assertAlmostEqual(detail["max_step_mm"], 0.7)
            self.assertLess(detail["safety"]["escape_margin_fraction"], 0.5)

    def test_tier_c_step_plan_exports_only_six_nonlinear_runs(self) -> None:
        plan_path = Path("step_response_identification_tier_c_plan.json")
        plan = hf.load_plan(plan_path)
        self.assertEqual([spec["tier"] for spec in plan["experiments"]], ["C", "C", "C"])
        with TemporaryDirectory() as temporary:
            manifest = hf.export_plan(
                plan_path,
                Path(temporary) / "tier_c_export",
                include_disabled=False,
                write_csv=False,
            )
        self.assertEqual(len(manifest["runs"]), 6)
        for run in manifest["runs"]:
            metadata = run["metadata"]
            detail = metadata["generation_detail"]
            self.assertEqual(metadata["step_response_tier"], "C")
            self.assertEqual(metadata["duration_sec"], 11.6)
            self.assertEqual(detail["n_jumps"], 32)
            self.assertAlmostEqual(detail["max_step_mm"], 1.05)
            self.assertLess(detail["safety"]["escape_margin_fraction"], 0.5)

    def test_tier_d_step_plan_stays_below_escape_boundary(self) -> None:
        plan_path = Path("step_response_identification_tier_d_plan.json")
        plan = hf.load_plan(plan_path)
        self.assertEqual([spec["tier"] for spec in plan["experiments"]], ["D", "D", "D"])
        with TemporaryDirectory() as temporary:
            manifest = hf.export_plan(
                plan_path,
                Path(temporary) / "tier_d_export",
                include_disabled=False,
                write_csv=False,
            )
        self.assertEqual(len(manifest["runs"]), 6)
        for run in manifest["runs"]:
            metadata = run["metadata"]
            detail = metadata["generation_detail"]
            self.assertEqual(metadata["step_response_tier"], "D")
            self.assertEqual(metadata["duration_sec"], 11.6)
            self.assertEqual(detail["n_jumps"], 32)
            self.assertAlmostEqual(detail["max_step_mm"], 1.25)
            self.assertGreater(detail["max_step_mm"], detail["force_peak_offset_mm"])
            self.assertLess(detail["safety"]["escape_margin_fraction"], 0.6)

    def test_tier_e_step_plan_uses_one_second_holds_below_escape_boundary(self) -> None:
        plan_path = Path("step_response_identification_tier_e_plan.json")
        plan = hf.load_plan(plan_path)
        self.assertEqual([spec["tier"] for spec in plan["experiments"]], ["E", "E", "E"])
        with TemporaryDirectory() as temporary:
            manifest = hf.export_plan(
                plan_path,
                Path(temporary) / "tier_e_export",
                include_disabled=False,
                write_csv=False,
            )
        self.assertEqual(len(manifest["runs"]), 6)
        for run in manifest["runs"]:
            metadata = run["metadata"]
            detail = metadata["generation_detail"]
            self.assertEqual(metadata["step_response_tier"], "E")
            self.assertEqual(metadata["duration_sec"], 18.0)
            self.assertEqual(metadata["samples"], 180000)
            self.assertEqual(detail["hold_sec"], 1.0)
            self.assertEqual(detail["final_hold_sec"], 1.0)
            self.assertEqual(detail["n_jumps"], 16)
            self.assertAlmostEqual(detail["max_step_mm"], 1.7)
            self.assertGreater(detail["max_step_mm"], detail["force_peak_offset_mm"])
            self.assertLess(detail["safety"]["escape_margin_fraction"], 0.8)

    def test_tier_f_g_h_plans_form_ordered_escape_boundary_escalation(self) -> None:
        expected = {
            "F": {
                "plan": "step_response_identification_tier_f_plan.json",
                "runs": 3,
                "duration": 10.0,
                "jumps": 8,
                "max_step": 1.9,
                "probe": False,
            },
            "G": {
                "plan": "step_response_identification_tier_g_plan.json",
                "runs": 6,
                "duration": 8.0,
                "jumps": 6,
                "max_step": 2.1,
                "probe": False,
            },
            "H": {
                "plan": "step_response_identification_tier_h_plan.json",
                "runs": 6,
                "duration": 8.0,
                "jumps": 6,
                "max_step": 2.3,
                "probe": True,
            },
        }
        for tier, values in expected.items():
            plan_path = Path(values["plan"])
            plan = hf.load_plan(plan_path)
            self.assertTrue(all(spec["tier"] == tier for spec in plan["experiments"]))
            with TemporaryDirectory() as temporary:
                manifest = hf.export_plan(
                    plan_path,
                    Path(temporary) / f"tier_{tier.lower()}_export",
                    include_disabled=False,
                    write_csv=False,
                )
            self.assertEqual(len(manifest["runs"]), values["runs"])
            for run in manifest["runs"]:
                metadata = run["metadata"]
                detail = metadata["generation_detail"]
                safety = detail["safety"]
                self.assertEqual(metadata["step_response_tier"], tier)
                self.assertEqual(metadata["duration_sec"], values["duration"])
                self.assertEqual(detail["n_jumps"], values["jumps"])
                self.assertAlmostEqual(detail["max_step_mm"], values["max_step"])
                self.assertEqual(
                    metadata["escape_boundary_probe_enabled"], values["probe"]
                )
                self.assertEqual(
                    safety["escape_boundary_exceeded"], values["probe"]
                )
                target_levels = [
                    abs(value) for value in detail["levels_mm"] if value != 0.0
                ]
                self.assertEqual(target_levels, sorted(target_levels))

    def test_escape_boundary_crossing_is_reported_and_capped_at_the_hard_limit(self) -> None:
        """2026-09-24: the gate is the 3.2 mm hard cap; lambda/4 is reported, not enforced."""
        defaults = {
            "sample_hz": 1000,
            "duration_sec": 1.0,
            "ramp_sec": 0.0,
            "safety_limits": {
                "max_offset_mm": 3.4,
                "max_step_mm": 3.4,
                "max_speed_mm_s": 1.0,
                "max_acceleration_mm_s2": 1.0,
            },
        }
        base = {
            "name": "escape_probe",
            "kind": "staircase",
            "axis": "x",
            "hold_sec": 0.01,
            "initial_hold_sec": 0.02,
            "amplitudes_mm": [2.15],
            "directions": [1],
        }
        # A jump past lambda/4 no longer stops the export; it is flagged in the metadata.
        offset, metadata = hf.generate_trajectory({**base, "tier": "G"}, defaults)
        safety = metadata["generation_detail"]["safety"]
        self.assertAlmostEqual(np.max(np.abs(offset[:, 0])), 2.15)
        self.assertTrue(safety["escape_boundary_exceeded"])
        self.assertAlmostEqual(safety["escape_offset_mm"], hf.ESCAPE_OFFSET_MM)
        self.assertAlmostEqual(safety["staircase_max_jump_limit_mm"], hf.STAIRCASE_MAX_JUMP_MM)
        # Tier H keeps its own opt-in.
        with self.assertRaisesRegex(ValueError, "require allow_escape_boundary_probe"):
            hf.generate_trajectory({**base, "tier": "H"}, defaults)
        # The scale-up steps (2.5 and 3.0 mm) pass; beyond the 3.2 mm cap they do not.
        for amplitude in (2.5, 3.0):
            offset, _metadata = hf.generate_trajectory(
                {**base, "tier": "G", "amplitudes_mm": [amplitude]}, defaults
            )
            self.assertAlmostEqual(np.max(np.abs(offset[:, 0])), amplitude)
        with self.assertRaisesRegex(ValueError, "exceeds the hard cap"):
            hf.generate_trajectory(
                {**base, "tier": "G", "amplitudes_mm": [3.3]}, defaults
            )

    def test_step_hardware_loader_uses_jump_limits_and_keeps_exact_steps(self) -> None:
        with TemporaryDirectory() as temporary:
            export_dir = self._export_step_plan(Path(temporary))
            _manifest, runs = load_hf_export_runs(export_dir)
            self.assertTrue((runs[0].directory / "command_offset_log.csv").is_file())
            trajectory = prepare_hf_trajectory(
                runs[0], center_m=(0.001, -0.002, 0.003), command_scale=1.0
            )
            self.assertEqual(trajectory.shape_name, "step_response_identification")
            self.assertEqual(trajectory.rate.requested_hz, 1000)
            self.assertEqual(trajectory.trajectory_source["step_response_tier"], "A")
            self.assertTrue(trajectory.trajectory_source["intentional_discontinuous_command"])
            x_mm = (np.asarray(trajectory.positions)[:, 0] - 0.001) * 1000.0
            self.assertEqual(set(np.round(x_mm, 9)), {0.0, 0.4})
            safety = trajectory.stats["identification_command_safety"]
            self.assertAlmostEqual(safety["max_step_mm"], 0.4)
            self.assertFalse(safety["derivative_limits_applied"])

    def test_step_generation_rejects_jump_above_configured_limit(self) -> None:
        defaults = {
            "sample_hz": 1000,
            "duration_sec": 1.0,
            "ramp_sec": 0.0,
            "safety_limits": {
                "max_offset_mm": 1.0,
                "max_step_mm": 0.3,
                "max_speed_mm_s": 1.0,
                "max_acceleration_mm_s2": 1.0,
            },
        }
        spec = {
            "name": "A0_unsafe",
            "kind": "staircase",
            "tier": "A",
            "axis": "x",
            "hold_sec": 0.01,
            "initial_hold_sec": 0.02,
            "amplitudes_mm": [0.4],
            "directions": [1],
        }
        with self.assertRaisesRegex(ValueError, "exceeds max_step_mm"):
            hf.generate_trajectory(spec, defaults)

            self.assertTrue(manifest.is_file())
            self.assertEqual(trajectory.positions[0], (0.001, -0.002, 0.003))
            self.assertEqual(trajectory.positions[-1], (0.001, -0.002, 0.003))
            self.assertEqual(trajectory.rate.requested_hz, 10000)
            self.assertEqual(trajectory.trajectory_source["command_scale"], 0.5)
            self.assertFalse(trajectory.trajectory_source["delay_feedforward_applied"])
            self.assertEqual(
                set(trajectory.source_artifacts),
                {
                    "command_trajectory.npz",
                    "trajectory_metadata.json",
                    "command_offset_log.csv",
                    "command_trajectory_preview.png",
                },
            )

    def test_hf_dry_run_never_opens_hardware(self) -> None:
        with TemporaryDirectory() as temporary:
            export_dir = self._export_small_plan(Path(temporary))
            with mock.patch.object(auto_record, "open_recording_hardware_session") as open_hw:
                result = auto_record.main(
                    ["--hf-export-dir", str(export_dir), "--hf-command-scale", "0.5", "--dry-run"]
                )
            self.assertEqual(result, 0)
            open_hw.assert_not_called()

    def test_step_dry_run_never_opens_hardware_or_requires_ack(self) -> None:
        with TemporaryDirectory() as temporary:
            export_dir = self._export_step_plan(Path(temporary))
            with mock.patch.object(auto_record, "open_recording_hardware_session") as open_hw:
                result = auto_record.main(
                    ["--hf-export-dir", str(export_dir), "--dry-run"]
                )
            self.assertEqual(result, 0)
            open_hw.assert_not_called()

    def test_step_hardware_run_requires_dedicated_ack(self) -> None:
        with TemporaryDirectory() as temporary:
            export_dir = self._export_step_plan(Path(temporary))
            with mock.patch.object(auto_record, "open_recording_hardware_session") as open_hw:
                result = auto_record.main(
                    ["--hf-export-dir", str(export_dir), "--no-preview"]
                )
            self.assertEqual(result, 2)
            open_hw.assert_not_called()

    def test_step_run_forces_preview_and_enter_checkpoint(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export_step_plan(root)
            output_root = root / "records"
            hardware_session = SimpleNamespace(current_pos=(0.0, 0.0, 0.0))
            with (
                mock.patch.object(
                    auto_record,
                    "open_recording_hardware_session",
                    return_value=hardware_session,
                ),
                mock.patch.object(auto_record, "shutdown_recording_hardware_session"),
                mock.patch.object(
                    auto_record,
                    "run_recording",
                    return_value=(0, output_root / "recorded"),
                ) as record,
            ):
                result = auto_record.main(
                    [
                        "--hf-export-dir",
                        str(export_dir),
                        "--output-dir",
                        str(output_root),
                        "--acknowledge-step-response-risk",
                        "--no-preview",
                    ]
                )
            self.assertEqual(result, 0)
            self.assertFalse(record.call_args.args[0].no_preview)
            self.assertTrue(record.call_args.kwargs["prompt_before_capture"])
            self.assertEqual(
                record.call_args.kwargs["automation_metadata"]["mode"],
                "step_response_identification",
            )

    def test_tiers_b_to_g_need_only_step_response_risk_ack(self) -> None:
        for tier in "BCDEFG":
            with self.subTest(tier=tier), TemporaryDirectory() as temporary:
                root = Path(temporary)
                export_dir = self._export_step_plan(root, tier=tier)
                output_root = root / "records"
                hardware_session = SimpleNamespace(current_pos=(0.0, 0.0, 0.0))
                with (
                    mock.patch.object(
                        auto_record,
                        "open_recording_hardware_session",
                        return_value=hardware_session,
                    ) as open_hw,
                    mock.patch.object(auto_record, "shutdown_recording_hardware_session"),
                    mock.patch.object(
                        auto_record,
                        "run_recording",
                        return_value=(0, output_root / f"{tier.lower()}_r01"),
                    ) as record,
                ):
                    result = auto_record.main(
                        [
                            "--hf-export-dir",
                            str(export_dir),
                            "--output-dir",
                            str(output_root),
                            "--acknowledge-step-response-risk",
                            "--no-preview",
                        ]
                    )
                self.assertEqual(result, 0)
                open_hw.assert_called_once()
                record.assert_called_once()
                self.assertFalse(record.call_args.args[0].no_preview)
                self.assertTrue(record.call_args.kwargs["prompt_before_capture"])

    def test_upper_tiers_still_require_step_response_risk_ack(self) -> None:
        for tier in "EH":
            with self.subTest(tier=tier), TemporaryDirectory() as temporary:
                export_dir = self._export_step_plan(Path(temporary), tier=tier)
                with mock.patch.object(
                    auto_record, "open_recording_hardware_session"
                ) as open_hw:
                    result = auto_record.main(
                        [
                            "--hf-export-dir",
                            str(export_dir),
                            "--acknowledge-step-response-escape-boundary-probe",
                        ]
                    )
                self.assertEqual(result, 2)
                open_hw.assert_not_called()

    def test_removed_tier_survival_flags_are_rejected(self) -> None:
        for tier in "abcdefg":
            with self.subTest(tier=tier):
                with (
                    mock.patch("sys.stderr"),
                    self.assertRaises(SystemExit),
                ):
                    auto_record.parse_args(
                        [f"--acknowledge-step-response-tier-{tier}-survived"]
                    )

    def test_tier_d_allows_multiple_runs(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export_step_plan(
                root, tier="D", repeats=2
            )
            output_root = root / "records"
            hardware_session = SimpleNamespace(current_pos=(0.0, 0.0, 0.0))
            with (
                mock.patch.object(
                    auto_record,
                    "open_recording_hardware_session",
                    return_value=hardware_session,
                ) as open_hw,
                mock.patch.object(auto_record, "shutdown_recording_hardware_session") as close_hw,
                mock.patch.object(
                    auto_record,
                    "run_recording",
                    side_effect=[
                        (0, output_root / "d_r01"),
                        (0, output_root / "d_r02"),
                    ],
                ) as record,
            ):
                result = auto_record.main(
                    [
                        "--hf-export-dir",
                        str(export_dir),
                        "--output-dir",
                        str(output_root),
                        "--acknowledge-step-response-risk",
                        "--no-preview",
                    ]
                )
            self.assertEqual(result, 0)
            open_hw.assert_called_once()
            close_hw.assert_called_once_with(hardware_session)
            self.assertEqual(record.call_count, 2)
            for call in record.call_args_list:
                self.assertFalse(call.args[0].no_preview)
                self.assertTrue(call.kwargs["prompt_before_capture"])

    def test_tier_c_allows_multiple_runs(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export_step_plan(root, tier="C", repeats=2)
            output_root = root / "records"
            hardware_session = SimpleNamespace(current_pos=(0.0, 0.0, 0.0))
            with (
                mock.patch.object(
                    auto_record,
                    "open_recording_hardware_session",
                    return_value=hardware_session,
                ),
                mock.patch.object(auto_record, "shutdown_recording_hardware_session"),
                mock.patch.object(
                    auto_record,
                    "run_recording",
                    side_effect=[
                        (0, output_root / "c_r01"),
                        (0, output_root / "c_r02"),
                    ],
                ) as record,
            ):
                result = auto_record.main(
                    [
                        "--hf-export-dir",
                        str(export_dir),
                        "--output-dir",
                        str(output_root),
                        "--acknowledge-step-response-risk",
                        "--no-preview",
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(record.call_count, 2)

    def test_tier_d_single_run_proceeds(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export_step_plan(root, tier="D")
            output_root = root / "records"
            hardware_session = SimpleNamespace(current_pos=(0.0, 0.0, 0.0))
            with (
                mock.patch.object(
                    auto_record,
                    "open_recording_hardware_session",
                    return_value=hardware_session,
                ),
                mock.patch.object(auto_record, "shutdown_recording_hardware_session"),
                mock.patch.object(
                    auto_record,
                    "run_recording",
                    return_value=(0, output_root / "d_r01"),
                ) as record,
            ):
                result = auto_record.main(
                    [
                        "--hf-export-dir",
                        str(export_dir),
                        "--output-dir",
                        str(output_root),
                        "--acknowledge-step-response-risk",
                        "--no-preview",
                    ]
                )
            self.assertEqual(result, 0)
            record.assert_called_once()

    def test_tier_e_allows_multiple_runs(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export_step_plan(
                root, tier="E", repeats=2
            )
            output_root = root / "records"
            hardware_session = SimpleNamespace(current_pos=(0.0, 0.0, 0.0))
            with (
                mock.patch.object(
                    auto_record,
                    "open_recording_hardware_session",
                    return_value=hardware_session,
                ) as open_hw,
                mock.patch.object(
                    auto_record, "shutdown_recording_hardware_session"
                ) as close_hw,
                mock.patch.object(
                    auto_record,
                    "run_recording",
                    side_effect=[
                        (0, output_root / "e_r01"),
                        (0, output_root / "e_r02"),
                    ],
                ) as record,
            ):
                result = auto_record.main(
                    [
                        "--hf-export-dir",
                        str(export_dir),
                        "--output-dir",
                        str(output_root),
                        "--acknowledge-step-response-risk",
                        "--no-preview",
                    ]
                )
            self.assertEqual(result, 0)
            open_hw.assert_called_once()
            close_hw.assert_called_once_with(hardware_session)
            self.assertEqual(record.call_count, 2)
            for call in record.call_args_list:
                self.assertFalse(call.args[0].no_preview)
                self.assertTrue(call.kwargs["prompt_before_capture"])

    def test_tier_e_single_run_proceeds(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export_step_plan(root, tier="E")
            output_root = root / "records"
            hardware_session = SimpleNamespace(current_pos=(0.0, 0.0, 0.0))
            with (
                mock.patch.object(
                    auto_record,
                    "open_recording_hardware_session",
                    return_value=hardware_session,
                ),
                mock.patch.object(auto_record, "shutdown_recording_hardware_session"),
                mock.patch.object(
                    auto_record,
                    "run_recording",
                    return_value=(0, output_root / "e_r01"),
                ) as record,
            ):
                result = auto_record.main(
                    [
                        "--hf-export-dir",
                        str(export_dir),
                        "--output-dir",
                        str(output_root),
                        "--acknowledge-step-response-risk",
                        "--no-preview",
                    ]
                )
            self.assertEqual(result, 0)
            record.assert_called_once()

    def test_tier_h_requires_dedicated_escape_ack_and_one_run(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            one_export = self._export_step_plan(root / "one", tier="H")
            with mock.patch.object(auto_record, "open_recording_hardware_session") as open_hw:
                result = auto_record.main(
                    [
                        "--hf-export-dir",
                        str(one_export),
                        "--acknowledge-step-response-risk",
                    ]
                )
            self.assertEqual(result, 2)
            open_hw.assert_not_called()

            two_export = self._export_step_plan(root / "two", tier="H", repeats=2)
            with mock.patch.object(auto_record, "open_recording_hardware_session") as open_hw:
                result = auto_record.main(
                    [
                        "--hf-export-dir",
                        str(two_export),
                        "--acknowledge-step-response-risk",
                        "--acknowledge-step-response-escape-boundary-probe",
                    ]
                )
            self.assertEqual(result, 2)
            open_hw.assert_not_called()

    def test_single_tier_h_run_proceeds_after_escape_ack(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export_step_plan(root, tier="H")
            output_root = root / "records"
            hardware_session = SimpleNamespace(current_pos=(0.0, 0.0, 0.0))
            with (
                mock.patch.object(
                    auto_record,
                    "open_recording_hardware_session",
                    return_value=hardware_session,
                ),
                mock.patch.object(auto_record, "shutdown_recording_hardware_session"),
                mock.patch.object(
                    auto_record,
                    "run_recording",
                    return_value=(0, output_root / "h_probe"),
                ) as record,
            ):
                result = auto_record.main(
                    [
                        "--hf-export-dir",
                        str(export_dir),
                        "--output-dir",
                        str(output_root),
                        "--acknowledge-step-response-risk",
                        "--acknowledge-step-response-escape-boundary-probe",
                        "--no-preview",
                    ]
                )
            self.assertEqual(result, 0)
            record.assert_called_once()
            self.assertFalse(record.call_args.args[0].no_preview)
            self.assertTrue(record.call_args.kwargs["prompt_before_capture"])

    def _run_auto_with_mock_hardware(
        self,
        argv: list[str],
        *,
        record_results: list[tuple[int, Path | None]] | None = None,
    ) -> tuple[int, mock.Mock, mock.Mock, mock.Mock]:
        """Run the auto entry point with PAT, cameras and holograms mocked."""
        events = mock.Mock()
        hardware_session = SimpleNamespace(current_pos=(0.0, 0.0, 0.0))
        playback = SimpleNamespace(name="first-run-playback")
        events.open_hw.return_value = hardware_session
        events.precompute.return_value = playback
        if record_results is None:
            events.record.side_effect = lambda *_a, **_k: (0, None)
        else:
            events.record.side_effect = list(record_results)
        with (
            mock.patch.object(
                auto_record, "open_recording_hardware_session", events.open_hw
            ),
            mock.patch.object(
                auto_record, "shutdown_recording_hardware_session", events.close_hw
            ),
            mock.patch.object(
                auto_record, "precompute_hologram_playback", events.precompute
            ),
            mock.patch.object(auto_record, "run_recording", events.record),
        ):
            result = auto_record.main(argv)
        return result, events, hardware_session, playback

    def test_multiple_export_dirs_record_in_given_order(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self._export_step_plan(root / "first", tier="A")
            second = self._export_step_plan(root / "second", tier="B")
            output_root = root / "records"
            result, events, _session, _playback = self._run_auto_with_mock_hardware(
                [
                    "--hf-export-dir", str(second),
                    "--hf-export-dir", str(first),
                    "--output-dir", str(output_root),
                    "--acknowledge-step-response-risk",
                ]
            )
            self.assertEqual(result, 0)
            events.open_hw.assert_called_once()
            labels = [
                call.kwargs["automation_metadata"]["label"]
                for call in events.record.call_args_list
            ]
            self.assertEqual(labels, ["B0_x_staircase_test", "A0_x_staircase_test"])
            manifests = [
                Path(call.kwargs["automation_metadata"]["export_manifest"])
                for call in events.record.call_args_list
            ]
            self.assertEqual(
                manifests,
                [
                    (second / "export_manifest.json").resolve(),
                    (first / "export_manifest.json").resolve(),
                ],
            )
            session_json = json.loads(
                next(output_root.glob("auto_recording_session_*.json")).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(len(session_json["source_export_manifests"]), 2)
            self.assertFalse(session_json["unattended_after_first_checkpoint"])
            for call in events.record.call_args_list:
                self.assertIsNone(call.kwargs["automatic_capture_retries"])
                self.assertTrue(call.kwargs["prompt_before_capture"])

    def test_multiple_export_dirs_reject_duplicate_names_and_index_selectors(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self._export_step_plan(root / "first", tier="A")
            duplicate = self._export_step_plan(root / "duplicate", tier="A")
            other = self._export_step_plan(root / "other", tier="B")
            for argv in (
                ["--hf-export-dir", str(first), "--hf-export-dir", str(duplicate)],
                ["--hf-export-dir", str(first), "--hf-export-dir", str(first)],
                ["--hf-export-dir", str(first), "--hf-export-dir", str(other), "--limit", "1"],
            ):
                with self.subTest(argv=argv):
                    result, events, _session, _playback = self._run_auto_with_mock_hardware(
                        argv + ["--acknowledge-step-response-risk"]
                    )
                    self.assertEqual(result, 2)
                    events.open_hw.assert_not_called()

    def test_unattended_series_checks_only_the_first_run(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self._export_step_plan(root / "first", tier="A", repeats=2)
            second = self._export_step_plan(root / "second", tier="E")
            output_root = root / "records"
            result, events, session, playback = self._run_auto_with_mock_hardware(
                [
                    "--hf-export-dir", str(first),
                    "--hf-export-dir", str(second),
                    "--output-dir", str(output_root),
                    "--acknowledge-step-response-risk",
                    "--unattended-after-first-checkpoint",
                ]
            )
            self.assertEqual(result, 0)
            # First-run holograms are ready before PAT output starts.
            call_names = [name for name, _args, _kwargs in events.mock_calls]
            self.assertLess(call_names.index("precompute"), call_names.index("open_hw"))
            events.precompute.assert_called_once()
            events.open_hw.assert_called_once_with()
            events.close_hw.assert_called_once_with(session)
            calls = events.record.call_args_list
            self.assertEqual(len(calls), 3)
            self.assertEqual(
                [call.kwargs["automation_metadata"]["label"] for call in calls],
                [
                    "A0_x_staircase_test_r01",
                    "A0_x_staircase_test_r02",
                    "E0_x_staircase_test",
                ],
            )
            first_call, *later_calls = calls
            self.assertFalse(first_call.args[0].no_preview)
            self.assertTrue(first_call.kwargs["prompt_before_capture"])
            self.assertIs(first_call.kwargs["precomputed_playback"], playback)
            self.assertTrue(
                first_call.kwargs["automation_metadata"]["operator_checkpoint_before_capture"]
            )
            for call in later_calls:
                self.assertTrue(call.args[0].no_preview)
                self.assertFalse(call.kwargs["prompt_before_capture"])
                self.assertIsNone(call.kwargs["precomputed_playback"])
                self.assertFalse(
                    call.kwargs["automation_metadata"]["operator_checkpoint_before_capture"]
                )
            for call in calls:
                self.assertEqual(call.kwargs["automatic_capture_retries"], 2)
                self.assertTrue(
                    call.kwargs["automation_metadata"]["unattended_after_first_checkpoint"]
                )
            session_json = json.loads(
                next(output_root.glob("auto_recording_session_*.json")).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(session_json["status"], "complete")
            self.assertTrue(session_json["unattended_after_first_checkpoint"])
            self.assertEqual(session_json["automatic_capture_retry_limit"], 2)

    def test_unattended_series_stops_when_a_run_fails(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export_step_plan(root, tier="C", repeats=3)
            output_root = root / "records"
            result, events, session, _playback = self._run_auto_with_mock_hardware(
                [
                    "--hf-export-dir", str(export_dir),
                    "--output-dir", str(output_root),
                    "--acknowledge-step-response-risk",
                    "--unattended-after-first-checkpoint",
                    "--automatic-capture-retries", "0",
                ],
                record_results=[(0, output_root / "c_r01"), (2, None)],
            )
            self.assertEqual(result, 2)
            self.assertEqual(events.record.call_count, 2)
            for call in events.record.call_args_list:
                self.assertEqual(call.kwargs["automatic_capture_retries"], 0)
            events.close_hw.assert_called_once_with(session)
            session_json = json.loads(
                next(output_root.glob("auto_recording_session_*.json")).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(session_json["status"], "failed")

    def test_unattended_series_rejects_unsupported_selections(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            step_export = self._export_step_plan(root / "step", tier="A")
            tier_h_export = self._export_step_plan(root / "tier_h", tier="H")
            (root / "hf").mkdir()
            hf_export = self._export_small_plan(root / "hf")
            unattended = ["--unattended-after-first-checkpoint"]
            risk = ["--acknowledge-step-response-risk"]
            cases = {
                "non-step export": ["--hf-export-dir", str(hf_export)] + unattended,
                "mixed step and non-step": [
                    "--hf-export-dir", str(step_export),
                    "--hf-export-dir", str(hf_export),
                ] + unattended + risk,
                "tier H": [
                    "--hf-export-dir", str(tier_h_export),
                    "--acknowledge-step-response-escape-boundary-probe",
                ] + unattended + risk,
                "confirm each run": ["--hf-export-dir", str(step_export), "--confirm-each-run"]
                + unattended + risk,
                "preview each run": ["--hf-export-dir", str(step_export), "--preview-each-run"]
                + unattended + risk,
                "retries without unattended": [
                    "--hf-export-dir", str(step_export), "--automatic-capture-retries", "1",
                ] + risk,
                "retries out of range": [
                    "--hf-export-dir", str(step_export), "--automatic-capture-retries", "11",
                ] + unattended + risk,
                "missing risk ack": ["--hf-export-dir", str(step_export)] + unattended,
                "keep going": ["--hf-export-dir", str(step_export), "--keep-going"]
                + unattended + risk,
            }
            for name, argv in cases.items():
                with self.subTest(case=name):
                    result, events, _session, _playback = self._run_auto_with_mock_hardware(argv)
                    self.assertEqual(result, 2)
                    events.open_hw.assert_not_called()
                    events.precompute.assert_not_called()
                    events.record.assert_not_called()

    def test_unattended_dry_run_never_opens_hardware_or_precomputes(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self._export_step_plan(root / "first", tier="A")
            second = self._export_step_plan(root / "second", tier="E")
            result, events, _session, _playback = self._run_auto_with_mock_hardware(
                [
                    "--hf-export-dir", str(first),
                    "--hf-export-dir", str(second),
                    "--unattended-after-first-checkpoint",
                    "--dry-run",
                ]
            )
            self.assertEqual(result, 0)
            events.open_hw.assert_not_called()
            events.precompute.assert_not_called()
            events.record.assert_not_called()

    def test_two_scales_of_same_export_run_in_one_auto_session(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export_small_plan(root)
            output_root = root / "records"
            hardware_session = SimpleNamespace(current_pos=(0.0, 0.0, 0.0))
            run_dirs = [output_root / "half", output_root / "full"]
            with (
                mock.patch.object(
                    auto_record,
                    "open_recording_hardware_session",
                    return_value=hardware_session,
                ) as open_hw,
                mock.patch.object(auto_record, "shutdown_recording_hardware_session") as close_hw,
                mock.patch.object(
                    auto_record,
                    "run_recording",
                    side_effect=[(0, run_dirs[0]), (0, run_dirs[1])],
                ) as record,
            ):
                result = auto_record.main(
                    [
                        "--hf-export-dir",
                        str(export_dir),
                        "--output-dir",
                        str(output_root),
                        "--hf-run",
                        "x_chirp_test=0.5",
                        "--hf-run",
                        "x_chirp_test=1.0",
                        "--acknowledge-hf-retention-and-visibility",
                        "--no-preview",
                    ]
                )

            self.assertEqual(result, 0)
            open_hw.assert_called_once_with()
            close_hw.assert_called_once_with(hardware_session)
            self.assertEqual(record.call_count, 2)
            scales = [
                call.kwargs["prepared_trajectory"].trajectory_source["command_scale"]
                for call in record.call_args_list
            ]
            self.assertEqual(scales, [0.5, 1.0])
            for call in record.call_args_list:
                self.assertEqual(call.args[0].capture_tail_margin_sec, 2.0)

    def test_recording_parser_default_tail_margin_is_two_seconds(self) -> None:
        args = auto_record.parse_args([])
        self.assertEqual(args.capture_tail_margin_sec, 2.0)

    def test_full_scale_dynamic_run_requires_explicit_retention_ack(self) -> None:
        with TemporaryDirectory() as temporary:
            export_dir = self._export_small_plan(Path(temporary))
            with mock.patch.object(auto_record, "open_recording_hardware_session") as open_hw:
                result = auto_record.main(["--hf-export-dir", str(export_dir), "--no-preview"])
            self.assertEqual(result, 2)
            open_hw.assert_not_called()

    def test_exact_command_artifacts_are_copied_and_hashed(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.npz"
            source.write_bytes(b"exact-command")
            run_dir = root / "run"
            run_dir.mkdir()

            result = copy_prepared_trajectory_artifacts(
                run_dir, {"command_trajectory.npz": source}
            )

            copied = run_dir / "command_trajectory.npz"
            self.assertEqual(copied.read_bytes(), source.read_bytes())
            self.assertEqual(result["command_trajectory.npz"]["size_bytes"], len(b"exact-command"))
            self.assertEqual(len(result["command_trajectory.npz"]["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
