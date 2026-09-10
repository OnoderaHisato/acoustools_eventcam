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

    def test_escape_boundary_crossing_requires_tier_h_opt_in_and_hard_cap(self) -> None:
        defaults = {
            "sample_hz": 1000,
            "duration_sec": 1.0,
            "ramp_sec": 0.0,
            "safety_limits": {
                "max_offset_mm": 2.31,
                "max_step_mm": 2.31,
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
        with self.assertRaisesRegex(ValueError, "escape boundary"):
            hf.generate_trajectory({**base, "tier": "G"}, defaults)
        with self.assertRaisesRegex(ValueError, "require allow_escape_boundary_probe"):
            hf.generate_trajectory({**base, "tier": "H"}, defaults)
        offset, metadata = hf.generate_trajectory(
            {**base, "tier": "H", "allow_escape_boundary_probe": True}, defaults
        )
        self.assertAlmostEqual(np.max(np.abs(offset[:, 0])), 2.15)
        self.assertTrue(metadata["generation_detail"]["safety"]["escape_boundary_exceeded"])
        with self.assertRaisesRegex(ValueError, "hard probe cap"):
            hf.generate_trajectory(
                {
                    **base,
                    "tier": "H",
                    "allow_escape_boundary_probe": True,
                    "amplitudes_mm": [2.31],
                },
                defaults,
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

    def test_tier_b_requires_tier_a_survival_ack(self) -> None:
        with TemporaryDirectory() as temporary:
            export_dir = self._export_step_plan(Path(temporary), tier="B")
            with mock.patch.object(auto_record, "open_recording_hardware_session") as open_hw:
                result = auto_record.main(
                    [
                        "--hf-export-dir",
                        str(export_dir),
                        "--acknowledge-step-response-risk",
                    ]
                )
            self.assertEqual(result, 2)
            open_hw.assert_not_called()

    def test_tier_c_requires_tier_b_survival_ack(self) -> None:
        with TemporaryDirectory() as temporary:
            export_dir = self._export_step_plan(Path(temporary), tier="C")
            with mock.patch.object(auto_record, "open_recording_hardware_session") as open_hw:
                result = auto_record.main(
                    [
                        "--hf-export-dir",
                        str(export_dir),
                        "--acknowledge-step-response-risk",
                    ]
                )
            self.assertEqual(result, 2)
            open_hw.assert_not_called()

    def test_tier_d_requires_tier_c_survival_ack(self) -> None:
        with TemporaryDirectory() as temporary:
            export_dir = self._export_step_plan(Path(temporary), tier="D")
            with mock.patch.object(auto_record, "open_recording_hardware_session") as open_hw:
                result = auto_record.main(
                    [
                        "--hf-export-dir",
                        str(export_dir),
                        "--acknowledge-step-response-risk",
                    ]
                )
            self.assertEqual(result, 2)
            open_hw.assert_not_called()

    def test_tier_d_allows_multiple_runs_after_tier_c_ack(self) -> None:
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
                        "--acknowledge-step-response-tier-c-survived",
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

    def test_tier_c_allows_multiple_runs_after_tier_b_ack(self) -> None:
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
                        "--acknowledge-step-response-tier-b-survived",
                        "--no-preview",
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(record.call_count, 2)

    def test_tier_d_single_run_proceeds_after_tier_c_ack(self) -> None:
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
                        "--acknowledge-step-response-tier-c-survived",
                        "--no-preview",
                    ]
                )
            self.assertEqual(result, 0)
            record.assert_called_once()

    def test_tier_e_requires_tier_d_survival_ack(self) -> None:
        with TemporaryDirectory() as temporary:
            export_dir = self._export_step_plan(Path(temporary), tier="E")
            with mock.patch.object(auto_record, "open_recording_hardware_session") as open_hw:
                result = auto_record.main(
                    [
                        "--hf-export-dir",
                        str(export_dir),
                        "--acknowledge-step-response-risk",
                    ]
                )
            self.assertEqual(result, 2)
            open_hw.assert_not_called()

    def test_tier_e_allows_multiple_runs_after_tier_d_ack(self) -> None:
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
                        "--acknowledge-step-response-tier-d-survived",
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

    def test_tier_e_single_run_proceeds_after_tier_d_ack(self) -> None:
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
                        "--acknowledge-step-response-tier-d-survived",
                        "--no-preview",
                    ]
                )
            self.assertEqual(result, 0)
            record.assert_called_once()

    def test_tier_f_requires_tier_e_survival_ack(self) -> None:
        with TemporaryDirectory() as temporary:
            export_dir = self._export_step_plan(Path(temporary), tier="F")
            with mock.patch.object(auto_record, "open_recording_hardware_session") as open_hw:
                result = auto_record.main(
                    [
                        "--hf-export-dir",
                        str(export_dir),
                        "--acknowledge-step-response-risk",
                    ]
                )
            self.assertEqual(result, 2)
            open_hw.assert_not_called()

    def test_tier_g_requires_tier_f_survival_ack(self) -> None:
        with TemporaryDirectory() as temporary:
            export_dir = self._export_step_plan(Path(temporary), tier="G")
            with mock.patch.object(auto_record, "open_recording_hardware_session") as open_hw:
                result = auto_record.main(
                    [
                        "--hf-export-dir",
                        str(export_dir),
                        "--acknowledge-step-response-risk",
                    ]
                )
            self.assertEqual(result, 2)
            open_hw.assert_not_called()

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
                        "--acknowledge-step-response-tier-g-survived",
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
                        "--acknowledge-step-response-tier-g-survived",
                        "--acknowledge-step-response-escape-boundary-probe",
                    ]
                )
            self.assertEqual(result, 2)
            open_hw.assert_not_called()

    def test_single_tier_h_run_proceeds_after_all_boundary_acks(self) -> None:
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
                        "--acknowledge-step-response-tier-g-survived",
                        "--acknowledge-step-response-escape-boundary-probe",
                        "--no-preview",
                    ]
                )
            self.assertEqual(result, 0)
            record.assert_called_once()
            self.assertFalse(record.call_args.args[0].no_preview)
            self.assertTrue(record.call_args.kwargs["prompt_before_capture"])

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
