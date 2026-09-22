#!/usr/bin/env python3
"""Hardware-free tests for the vzr identification (multi-axis sine) acquisition path."""

from __future__ import annotations

import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock
import unittest

import numpy as np

import acoustools_stereo_eventcam_3d_recording_auto as auto_record
import hf_identification_trajectory as hf
from stereo_acoustools_3d_hf_common import (
    is_vzr_metadata,
    load_hf_export_runs,
    prepare_hf_trajectory,
)


CAMPAIGN_DIR = Path("vzr_identification_20260917")
OFFICIAL_PLAN = CAMPAIGN_DIR / "vzr_identification_plan.json"
OFFICIAL_EXPORT = CAMPAIGN_DIR / "export_vzr_identification"
EXPECTED_ORDER = [
    "vzr_L1x_x0.4_57Hz_only",
    "vzr_L1z_z0.15_187Hz_only",
    "vzr_L1_x0.4_57Hz_z0.15_187Hz",
    "vzr_L2_x0.5_57Hz_z0.20_187Hz",
    "vzr_L3_x0.6_57Hz_z0.20_187Hz",
    "vzr_L4_x0.6_57Hz_z0.25_187Hz",
    "vzr_Y1y_y0.5_55Hz_only",
    "vzr_Y1_y0.5_55Hz_z0.20_187Hz",
    "vzr_Y2_y0.6_55Hz_z0.20_187Hz",
    "vzr_ALT_x0.7_50Hz_z0.20_187Hz",
]


def reference_build(components, sample_hz=10000.0, duration_sec=8.0, ramp_sec=0.5, chirp=None):
    """Verbatim numerical core of make_vzr_trajectory.py from the 2026-09-17 plan."""
    n = int(round(duration_sec * sample_hz))
    t = np.arange(n) / sample_hz
    env = np.ones(n)
    nr = int(round(ramp_sec * sample_hz))
    if nr > 0:
        r = 0.5 - 0.5 * np.cos(np.pi * np.arange(nr) / nr)
        env[:nr] = r
        env[-nr:] = r[::-1]
    pos = np.zeros((n, 3))
    for ax, A, f, ph in components:
        i = "xyz".index(ax)
        if chirp and ax in chirp:
            f1, f2 = chirp[ax]
            phase = 2 * np.pi * (f1 * t + 0.5 * (f2 - f1) / duration_sec * t * t)
        else:
            phase = 2 * np.pi * f * t
        pos[:, i] += A * np.sin(phase + ph)
    pos *= env[:, None]
    return pos


def _defaults(**overrides):
    defaults = {
        "sample_hz": 10000,
        "duration_sec": 1.0,
        "ramp_sec": 0.1,
        "safety_limits": {
            "max_offset_mm": 0.75,
            "max_speed_mm_s": 400.0,
            "max_acceleration_mm_s2": 400000.0,
        },
    }
    defaults.update(overrides)
    return defaults


class MultitoneGenerationTests(unittest.TestCase):
    def test_multitone_reproduces_reference_generator(self) -> None:
        spec = {
            "name": "ref",
            "kind": "multitone",
            "components": [
                {"axis": "x", "amplitude_mm": 0.6, "frequency_hz": 57.0},
                {"axis": "z", "amplitude_mm": 0.2, "frequency_hz": 187.0},
            ],
        }
        offset, metadata = hf.generate_trajectory(spec, _defaults())
        expected = reference_build(
            [("x", 0.6, 57.0, 0.0), ("z", 0.2, 187.0, 0.7)],
            duration_sec=1.0,
            ramp_sec=0.1,
        )
        self.assertEqual(offset.shape, expected.shape)
        self.assertLess(float(np.max(np.abs(offset - expected))), 1e-12)
        # Default phases follow the reference sequence 0.0, 0.7, 2.1, 3.5.
        phases = [c["phase_rad"] for c in metadata["generation_detail"]["components"]]
        self.assertEqual(phases, [0.0, 0.7])

    def test_multitone_exact_amplitudes_axes_and_zero_endpoints(self) -> None:
        spec = {
            "name": "l3",
            "kind": "multitone",
            "components": [
                {"axis": "x", "amplitude_mm": 0.6, "frequency_hz": 57.0, "phase_rad": 0.0},
                {"axis": "z", "amplitude_mm": 0.2, "frequency_hz": 187.0, "phase_rad": 0.7},
            ],
        }
        offset, metadata = hf.generate_trajectory(spec, _defaults())
        self.assertEqual(metadata["kind"], "multitone")
        self.assertEqual(metadata["axis"], "xz")
        self.assertEqual(metadata["axes"], ["x", "z"])
        self.assertEqual(metadata["safety_scale_applied"], 1.0)
        self.assertEqual(metadata["safety_policy"], "reject_above_limits")
        self.assertAlmostEqual(float(np.max(np.abs(offset[:, 0]))), 0.6, delta=1e-3)
        self.assertAlmostEqual(float(np.max(np.abs(offset[:, 2]))), 0.2, delta=1e-3)
        self.assertTrue(np.all(offset[:, 1] == 0.0))
        self.assertTrue(np.allclose(offset[[0, -1]], 0.0, atol=1e-15))
        self.assertEqual(
            metadata["generation_detail"]["per_axis_command_amplitude_mm"],
            {"x": 0.6, "z": 0.2},
        )
        self.assertLess(metadata["safety_limit_fractions"]["max_acceleration_mm_s2"], 1.0)

    def test_multitone_rejects_command_above_limits_instead_of_scaling(self) -> None:
        spec = {
            "name": "too_fast",
            "kind": "multitone",
            "components": [{"axis": "z", "amplitude_mm": 0.25, "frequency_hz": 187.0}],
        }
        limits = {"max_offset_mm": 0.75, "max_speed_mm_s": 400.0, "max_acceleration_mm_s2": 300000.0}
        with self.assertRaisesRegex(ValueError, "never rescaled"):
            hf.generate_trajectory(spec, _defaults(safety_limits=limits))
        # The same command passes with the plan limits and keeps its amplitude.
        offset, _metadata = hf.generate_trajectory(spec, _defaults())
        self.assertAlmostEqual(float(np.max(np.abs(offset[:, 2]))), 0.25, delta=1e-3)

    def test_multitone_component_validation(self) -> None:
        base = {"name": "bad", "kind": "multitone"}
        cases = [
            ({}, "non-empty components"),
            ({"components": []}, "non-empty components"),
            (
                {"axis": "x", "components": [{"axis": "x", "amplitude_mm": 0.1, "frequency_hz": 10.0}]},
                "remove the top-level axis",
            ),
            ({"components": [{"axis": "w", "amplitude_mm": 0.1, "frequency_hz": 10.0}]}, "unknown axis"),
            ({"components": [{"axis": "x", "amplitude_mm": 0.0, "frequency_hz": 10.0}]}, "positive and finite"),
            ({"components": [{"axis": "x", "amplitude_mm": 0.1}]}, "frequency_hz or both"),
            (
                {"components": [{"axis": "x", "amplitude_mm": 0.1, "frequency_hz": 10.0, "f_start_hz": 1.0, "f_end_hz": 2.0}]},
                "frequency_hz or both",
            ),
            ({"components": [{"axis": "x", "amplitude_mm": 0.1, "frequency_hz": 5000.0}]}, "below Nyquist"),
        ]
        for extra, message in cases:
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(ValueError, message):
                    hf.generate_trajectory({**base, **extra}, _defaults())

    def test_multitone_chirp_component_matches_reference_formula(self) -> None:
        spec = {
            "name": "chirp",
            "kind": "multitone",
            "components": [
                {"axis": "x", "amplitude_mm": 0.3, "f_start_hz": 40.0, "f_end_hz": 55.0},
                {"axis": "z", "amplitude_mm": 0.2, "frequency_hz": 187.0},
            ],
        }
        offset, metadata = hf.generate_trajectory(spec, _defaults())
        expected = reference_build(
            [("x", 0.3, 0.0, 0.0), ("z", 0.2, 187.0, 0.7)],
            duration_sec=1.0,
            ramp_sec=0.1,
            chirp={"x": (40.0, 55.0)},
        )
        self.assertLess(float(np.max(np.abs(offset - expected))), 1e-12)
        component = metadata["generation_detail"]["components"][0]
        self.assertTrue(component["chirp"])
        self.assertEqual((component["f_start_hz"], component["f_end_hz"]), (40.0, 55.0))


class OfficialVzrPlanTests(unittest.TestCase):
    def test_official_plan_order_durations_family_and_limits(self) -> None:
        plan = hf.load_plan(OFFICIAL_PLAN)
        self.assertEqual(plan["measurement_family"], hf.VZR_IDENTIFICATION_FAMILY)
        self.assertEqual(plan["campaign"], "vzr_identification_20260917")
        limits = plan["defaults"]["safety_limits"]
        self.assertEqual(limits["max_speed_mm_s"], 400.0)
        self.assertEqual(limits["max_acceleration_mm_s2"], 400000.0)
        names = [spec["name"] for spec in plan["experiments"]]
        self.assertEqual(names, EXPECTED_ORDER)
        for spec in plan["experiments"]:
            with self.subTest(name=spec["name"]):
                self.assertEqual(spec["kind"], "multitone")
                self.assertTrue(spec["enabled"])
                self.assertEqual(spec["measurement_family"], hf.VZR_IDENTIFICATION_FAMILY)
                self.assertEqual(int(spec.get("repeats", 1)), 1)
                is_single = spec["name"].endswith("_only")
                self.assertEqual(len(spec["components"]), 1 if is_single else 2)
                self.assertEqual(spec.get("duration_sec", plan["defaults"]["duration_sec"]), 4.0 if is_single else 8.0)
                if not is_single:
                    axes = [c["axis"] for c in spec["components"]]
                    self.assertEqual(axes[1], "z")
                    self.assertIn(axes[0], ("x", "y"))
                    self.assertEqual(spec["components"][1]["frequency_hz"], 187.0)
                    self.assertEqual(spec["components"][1]["phase_rad"], 0.7)
                offset, metadata = hf.generate_trajectory(spec, plan["defaults"])
                self.assertEqual(metadata["ramp_sec"], 0.5)
                self.assertEqual(metadata["sample_hz"], 10000.0)
                self.assertEqual(metadata["safety_scale_applied"], 1.0)
                metrics = metadata["metrics"]
                self.assertLessEqual(metrics["max_speed_mm_s"]["vector"], 400.0)
                self.assertLessEqual(metrics["max_acceleration_mm_s2"]["vector"], 400000.0)
                self.assertLessEqual(metrics["max_abs_offset_mm"]["vector"], 0.75)
                self.assertTrue(np.allclose(offset[[0, -1]], 0.0, atol=1e-15))
                for component in spec["components"]:
                    index = hf.AXIS_INDEX[component["axis"]]
                    self.assertAlmostEqual(
                        float(np.max(np.abs(offset[:, index]))),
                        component["amplitude_mm"],
                        delta=1e-3,
                    )

    def test_official_plan_matches_reference_generator_for_every_run(self) -> None:
        plan = hf.load_plan(OFFICIAL_PLAN)
        for spec in plan["experiments"]:
            with self.subTest(name=spec["name"]):
                components = [
                    (c["axis"], c["amplitude_mm"], c["frequency_hz"], c["phase_rad"])
                    for c in spec["components"]
                ]
                expected = reference_build(
                    components,
                    duration_sec=float(spec.get("duration_sec", plan["defaults"]["duration_sec"])),
                    ramp_sec=0.5,
                )
                offset, _metadata = hf.generate_trajectory(spec, plan["defaults"])
                self.assertEqual(offset.shape, expected.shape)
                self.assertLess(float(np.max(np.abs(offset - expected))), 1e-12)

    @unittest.skipUnless((OFFICIAL_EXPORT / "export_manifest.json").is_file(), "export not generated")
    def test_official_export_loads_as_vzr_and_prepares_multi_axis_positions(self) -> None:
        _manifest, runs = load_hf_export_runs(OFFICIAL_EXPORT)
        self.assertEqual([run.name for run in runs], EXPECTED_ORDER)
        for run in runs:
            self.assertTrue(is_vzr_metadata(run.metadata))
            self.assertTrue(run.offset_csv.is_file())
        by_name = {run.name: run for run in runs}
        trajectory = prepare_hf_trajectory(
            by_name["vzr_L3_x0.6_57Hz_z0.20_187Hz"],
            center_m=(0.001, -0.002, 0.003),
            command_scale=1.0,
        )
        self.assertEqual(trajectory.shape_name, "vzr_identification")
        self.assertEqual(trajectory.parameters["axes"], ["x", "z"])
        self.assertEqual(len(trajectory.parameters["components"]), 2)
        self.assertEqual(trajectory.trajectory_source["kind"], hf.VZR_IDENTIFICATION_FAMILY)
        self.assertEqual(trajectory.trajectory_source["reference"]["stage"], "L3")
        self.assertFalse(trajectory.trajectory_source["delay_feedforward_applied"])
        positions = np.asarray(trajectory.positions, dtype=float)
        self.assertEqual(positions.shape, (80000, 3))
        self.assertAlmostEqual(float(np.max(positions[:, 0] - 0.001)), 0.6e-3, delta=1e-6)
        self.assertAlmostEqual(float(np.max(positions[:, 2] - 0.003)), 0.2e-3, delta=1e-6)
        self.assertTrue(np.allclose(positions[:, 1], -0.002))


class VzrAutoEntryTests(unittest.TestCase):
    def _export_vzr_plan(self, root: Path, *, family: str = hf.VZR_IDENTIFICATION_FAMILY) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        plan = {
            "schema_version": 1,
            "campaign": "vzr_test",
            "measurement_family": family,
            "defaults": {
                "sample_hz": 10000,
                "duration_sec": 0.05,
                "ramp_sec": 0.005,
                "safety_limits": {
                    "max_offset_mm": 0.75,
                    "max_speed_mm_s": 400.0,
                    "max_acceleration_mm_s2": 400000.0,
                },
            },
            "experiments": [
                {
                    "name": "t_x_only",
                    "kind": "multitone",
                    "components": [{"axis": "x", "amplitude_mm": 0.4, "frequency_hz": 57.0}],
                    "campaign": "vzr_test",
                    "measurement_family": family,
                    "enabled": True,
                },
                {
                    "name": "t_xz",
                    "kind": "multitone",
                    "components": [
                        {"axis": "x", "amplitude_mm": 0.4, "frequency_hz": 57.0},
                        {"axis": "z", "amplitude_mm": 0.15, "frequency_hz": 187.0},
                    ],
                    "campaign": "vzr_test",
                    "measurement_family": family,
                    "enabled": True,
                },
            ],
        }
        plan_path = root / "vzr_plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        export_dir = root / "vzr_export"

        def fake_preview(path: Path, *_args: object) -> None:
            path.write_bytes(b"vzr-preview")

        with mock.patch.object(hf, "write_preview", side_effect=fake_preview):
            hf.export_plan(plan_path, export_dir, include_disabled=False, write_csv=False)
        return export_dir

    def _export_chirp_plan(self, root: Path) -> Path:
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
        plan_path = root / "chirp_plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        export_dir = root / "chirp_export"

        def fake_preview(path: Path, *_args: object) -> None:
            path.write_bytes(b"preview")

        with mock.patch.object(hf, "write_preview", side_effect=fake_preview):
            hf.export_plan(plan_path, export_dir, include_disabled=False, write_csv=True)
        return export_dir

    def test_multitone_export_always_writes_offset_csv_and_loads(self) -> None:
        with TemporaryDirectory() as temporary:
            export_dir = self._export_vzr_plan(Path(temporary))
            for name in ("t_x_only", "t_xz"):
                self.assertTrue((export_dir / name / "command_offset_log.csv").is_file())
            _manifest, runs = load_hf_export_runs(export_dir)
            self.assertEqual([run.name for run in runs], ["t_x_only", "t_xz"])
            self.assertEqual(runs[1].metadata["axis"], "xz")

    def test_loader_rejects_vzr_family_with_non_multitone_kind(self) -> None:
        with TemporaryDirectory() as temporary:
            export_dir = self._export_vzr_plan(Path(temporary))
            metadata_path = export_dir / "t_xz" / "trajectory_metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["kind"] = "chirp"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "multitone or static"):
                load_hf_export_runs(export_dir)

    def test_vzr_dry_run_never_opens_hardware(self) -> None:
        with TemporaryDirectory() as temporary:
            export_dir = self._export_vzr_plan(Path(temporary))
            with mock.patch.object(auto_record, "open_recording_hardware_session") as open_hw:
                result = auto_record.main(["--hf-export-dir", str(export_dir), "--dry-run"])
            self.assertEqual(result, 0)
            open_hw.assert_not_called()

    def test_vzr_hardware_run_requires_protocol_ack(self) -> None:
        with TemporaryDirectory() as temporary:
            export_dir = self._export_vzr_plan(Path(temporary))
            with mock.patch.object(auto_record, "open_recording_hardware_session") as open_hw:
                result = auto_record.main(
                    [
                        "--hf-export-dir",
                        str(export_dir),
                        "--output-dir",
                        str(Path(temporary) / "records"),
                        "--acknowledge-hf-retention-and-visibility",
                    ]
                )
            self.assertEqual(result, 2)
            open_hw.assert_not_called()
            self.assertFalse((Path(temporary) / "records").exists())

    def test_vzr_rejects_keep_going_unattended_and_mixed_exports(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export_vzr_plan(root)
            chirp_dir = self._export_chirp_plan(root)
            cases = [
                ["--hf-export-dir", str(export_dir), "--acknowledge-vzr-protocol", "--keep-going"],
                [
                    "--hf-export-dir",
                    str(export_dir),
                    "--acknowledge-vzr-protocol",
                    "--unattended-after-first-checkpoint",
                ],
                [
                    "--hf-export-dir",
                    str(export_dir),
                    "--hf-export-dir",
                    str(chirp_dir),
                    "--acknowledge-vzr-protocol",
                    "--acknowledge-hf-retention-and-visibility",
                ],
            ]
            for argv in cases:
                with self.subTest(argv=argv):
                    with mock.patch.object(
                        auto_record, "open_recording_hardware_session"
                    ) as open_hw:
                        result = auto_record.main(
                            argv + ["--output-dir", str(root / "records")]
                        )
                    self.assertEqual(result, 2)
                    open_hw.assert_not_called()

    def test_vzr_runs_force_preview_and_enter_without_hf_retention_ack(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export_vzr_plan(root)
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
                    return_value=(0, output_root / "recorded"),
                ) as record,
            ):
                result = auto_record.main(
                    [
                        "--hf-export-dir",
                        str(export_dir),
                        "--output-dir",
                        str(output_root),
                        "--acknowledge-vzr-protocol",
                        "--no-preview",
                    ]
                )
            self.assertEqual(result, 0)
            open_hw.assert_called_once()
            self.assertEqual(record.call_count, 2)
            for call in record.call_args_list:
                self.assertFalse(call.args[0].no_preview)
                self.assertTrue(call.kwargs["prompt_before_capture"])
                metadata = call.kwargs["automation_metadata"]
                self.assertEqual(metadata["mode"], hf.VZR_IDENTIFICATION_FAMILY)
                self.assertTrue(metadata["operator_checkpoint_before_capture"])
                self.assertFalse(metadata["delay_feedforward_applied"])
                self.assertEqual(metadata["command_scale"], 1.0)
            self.assertEqual(
                [call.kwargs["prepared_trajectory"].run_label for call in record.call_args_list],
                ["000_t_x_only_scale100", "001_t_xz_scale100"],
            )
            self.assertEqual(
                record.call_args_list[1].kwargs["prepared_trajectory"].shape_name,
                "vzr_identification",
            )
            sessions = sorted(output_root.glob("auto_recording_session_*.json"))
            self.assertEqual(len(sessions), 1)
            session = json.loads(sessions[0].read_text(encoding="utf-8"))
            self.assertEqual(session["trajectory_mode"], hf.VZR_IDENTIFICATION_FAMILY)
            self.assertEqual(session["status"], "complete")
            self.assertFalse(session["delay_feedforward_applied"])

    def test_vzr_label_subset_keeps_export_order(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export_vzr_plan(root)
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
                        "--label",
                        "t_xz",
                        "--label",
                        "t_x_only",
                        "--hf-command-scale",
                        "0.5",
                        "--acknowledge-vzr-protocol",
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(
                [call.kwargs["prepared_trajectory"].run_label for call in record.call_args_list],
                ["000_t_x_only_scale050", "001_t_xz_scale050"],
            )
            positions = np.asarray(
                record.call_args_list[1].kwargs["prepared_trajectory"].positions
            )
            self.assertAlmostEqual(float(np.max(np.abs(positions[:, 0]))), 0.2e-3, delta=2e-5)


if __name__ == "__main__":
    unittest.main()
