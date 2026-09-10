#!/usr/bin/env python3
"""Hardware-free tests for the Plan B acquisition integration."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock
import unittest

import torch

import acoustools_stereo_eventcam_3d_recording_auto as auto_record
import hf_identification_trajectory as hf
from stereo_acoustools_3d_hf_common import load_hf_export_runs, prepare_hf_trajectory
from stereo_acoustools_3d_recording_core import scale_hologram_drive_amplitude


ROOT = Path("plan_b_20260825")


class PlanBMeasurementTests(unittest.TestCase):
    def test_plan_b3_labels_and_active_warmup_targets_match_context_protocol(self) -> None:
        _manifest, runs = load_hf_export_runs(ROOT / "export_B3_drift_source")
        self.assertEqual(
            [run.name for run in runs],
            [
                "40_shield_off_warm00_static_60s",
                "41_shield_off_warm05_static_60s",
                "42_shield_off_warm10_static_60s",
                "43_shield_on_warm05_static_60s",
                "44_shield_on_hvac_off_warm05_static_60s",
                "45_shield_off_hvac_off_warm05_static_60s",
            ],
        )
        self.assertEqual(
            [run.metadata["nominal_active_warmup_minutes"] for run in runs],
            [0, 5, 10, 5, 5, 5],
        )

    def test_plan_counts_rank_order_and_repeat_specific_hold(self) -> None:
        b1 = hf.load_plan(ROOT / "B1_small_amplitude_multisine_plan.json")
        expanded = []
        for spec in b1["experiments"]:
            expanded.extend([spec] * int(spec.get("repeats", 1)))
        self.assertEqual(len(expanded), 30)
        ranks = [
            int(spec["escalation_rank"])
            for spec in b1["experiments"]
            if "escalation_rank" in spec
        ]
        self.assertEqual(ranks, sorted(ranks))

        _manifest, runs = load_hf_export_runs(
            ROOT / "export_B1_small_amplitude_multisine"
        )
        by_name = {run.name: run for run in runs}
        self.assertEqual(
            by_name["13_z_multisine_rung05.0um_r01"].metadata["response_gate"],
            "hold",
        )
        self.assertEqual(
            by_name["13_z_multisine_rung05.0um_r02"].metadata["response_gate"],
            "go",
        )

    def test_plan_b2_staircase_and_drive_scale_load(self) -> None:
        _manifest, runs = load_hf_export_runs(ROOT / "export_B2_z_line_source")
        by_name = {run.name: run for run in runs}
        run = by_name["21_driveC_z_staircase_ringdown"]
        self.assertEqual(run.metadata["measurement_family"], "plan_b2_z_line_source")
        self.assertNotIn("step_response_tier", run.metadata)
        trajectory = prepare_hf_trajectory(
            run, center_m=(0.0, 0.0, 0.0), command_scale=1.0
        )
        self.assertEqual(trajectory.trajectory_source["drive_amplitude_scale"], 0.7)
        self.assertEqual(trajectory.shape_name, "plan_b_identification")
        self.assertTrue(trajectory.parameters["intentional_discontinuous_command"])

    def test_drive_amplitude_scaling_preserves_phase_and_scales_magnitude(self) -> None:
        hologram = torch.tensor([1 + 0j, 1j, -1 + 0j], dtype=torch.complex64)
        scaled = scale_hologram_drive_amplitude(hologram, 0.7)
        self.assertTrue(torch.allclose(torch.angle(scaled), torch.angle(hologram)))
        self.assertTrue(torch.allclose(torch.abs(scaled), torch.full((3,), 0.7)))
        self.assertTrue(torch.allclose(torch.abs(hologram), torch.ones(3)))

    def test_plan_b1_multi_rank_hardware_selection_is_rejected_before_open(self) -> None:
        with mock.patch.object(auto_record, "open_recording_hardware_session") as open_hw:
            result = auto_record.main(
                [
                    "--hf-export-dir",
                    str(ROOT / "export_B1_small_amplitude_multisine"),
                    "--acknowledge-plan-b-protocol",
                    "--acknowledge-hf-retention-and-visibility",
                ]
            )
        self.assertEqual(result, 2)
        open_hw.assert_not_called()

    def test_plan_b3_requires_context_before_hardware_open(self) -> None:
        with mock.patch.object(auto_record, "open_recording_hardware_session") as open_hw:
            result = auto_record.main(
                [
                    "--hf-export-dir",
                    str(ROOT / "export_B3_drift_source"),
                    "--label",
                    "40_shield_off_warm00_static_60s",
                    "--acknowledge-plan-b-protocol",
                ]
            )
        self.assertEqual(result, 2)
        open_hw.assert_not_called()

    def test_plan_b3_context_accepts_explicit_temperature_unavailable_reason(self) -> None:
        with TemporaryDirectory() as temporary:
            context_path = Path(temporary) / "context.json"
            context_path.write_text(
                json.dumps(
                    {
                        "runs": {
                            "40_shield_off_warm00_static_60s": {
                                "actual_warmup_minutes": 0,
                                "shield_state": "off",
                                "hvac_state": "on",
                                "temperature_unavailable_reason": "no thermometer",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            session = SimpleNamespace(current_pos=(0.0, 0.0, 0.0))
            playback = SimpleNamespace(
                holograms=[torch.ones((1, 4), dtype=torch.complex64)],
                drive_amplitude_scale=1.0,
            )
            with (
                mock.patch.object(
                    auto_record,
                    "precompute_hologram_playback",
                    return_value=playback,
                ) as precompute,
                mock.patch.object(
                    auto_record, "open_recording_hardware_session", return_value=session
                ) as open_hw,
                mock.patch.object(auto_record, "shutdown_recording_hardware_session"),
                mock.patch.object(
                    auto_record, "run_recording", return_value=(0, None)
                ) as record,
            ):
                result = auto_record.main(
                    [
                        "--hf-export-dir",
                        str(ROOT / "export_B3_drift_source"),
                        "--label",
                        "40_shield_off_warm00_static_60s",
                        "--plan-b-context-json",
                        str(context_path),
                        "--output-dir",
                        str(Path(temporary) / "records"),
                        "--acknowledge-plan-b-protocol",
                    ]
                )
            self.assertEqual(result, 0)
            precompute.assert_called_once()
            open_hw.assert_called_once()
            self.assertIs(
                open_hw.call_args.kwargs["initial_hologram"],
                playback.holograms[0],
            )
            self.assertIs(
                record.call_args.kwargs["precomputed_playback"], playback
            )
            self.assertEqual(
                record.call_args.kwargs["active_warmup_target_minutes"], 0.0
            )


if __name__ == "__main__":
    unittest.main()
