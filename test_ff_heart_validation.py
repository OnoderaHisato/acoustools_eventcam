#!/usr/bin/env python3
"""Hardware-free tests for imported feedforward-validation commands (FF heart)."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock
import unittest

import numpy as np

import acoustools_stereo_eventcam_3d_recording_auto as auto_record
import hf_identification_trajectory as hf
from stereo_acoustools_3d_hf_common import (
    is_feedforward_validation_metadata,
    load_hf_export_runs,
    prepare_hf_trajectory,
)


CAMPAIGN_DIR = Path("ff_heart_20260918")
OFFICIAL_PLAN = CAMPAIGN_DIR / "ff_heart_plan.json"
OFFICIAL_EXPORT = CAMPAIGN_DIR / "export_ff_heart"
OFFICIAL_SOURCES = [
    CAMPAIGN_DIR / "source" / "heart_s7_f10_OFF.npz",
    CAMPAIGN_DIR / "source" / "heart_s7_f10_A_delay.npz",
    CAMPAIGN_DIR / "source" / "heart_s7_f10_C_delay_inverse.npz",
    CAMPAIGN_DIR / "source" / "heart_s7_f10_OT_ident_nodelay.npz",
    CAMPAIGN_DIR / "source" / "heart_s7_f10_A_delay03.npz",
    CAMPAIGN_DIR / "source" / "heart_s7_f10_C_delay03_inverse_k918.npz",
    CAMPAIGN_DIR / "source" / "heart_s7_f5_OFF.npz",
    CAMPAIGN_DIR / "source" / "heart_s7_f7_OFF.npz",
    CAMPAIGN_DIR / "source" / "heart_s7_f1_OFF.npz",
    CAMPAIGN_DIR / "source" / "heart_s7_f2_OFF.npz",
    CAMPAIGN_DIR / "source" / "heart_s7_f7_OFFW.npz",
    CAMPAIGN_DIR / "source" / "heart_s7_f7_CW.npz",
    CAMPAIGN_DIR / "source" / "heart_s7_f7_C_delay_inverse.npz",
    CAMPAIGN_DIR / "source" / "heart_s7_f10_OFFW.npz",
    CAMPAIGN_DIR / "source" / "heart_s7_f10_CW.npz",
    CAMPAIGN_DIR / "source" / "wscan_XZ_a.npz",
    CAMPAIGN_DIR / "source" / "wscan_XZ_b.npz",
    CAMPAIGN_DIR / "source" / "wscan_YZ_a.npz",
    CAMPAIGN_DIR / "source" / "wscan_YZ_b.npz",
    CAMPAIGN_DIR / "source" / "wscan_XY_a.npz",
    CAMPAIGN_DIR / "source" / "wscan_XY_b.npz",
    CAMPAIGN_DIR / "source" / "wscan_XZyp4_a.npz",
    CAMPAIGN_DIR / "source" / "wscan_XZyp4_b.npz",
    CAMPAIGN_DIR / "source" / "wscan_XZym4_a.npz",
    CAMPAIGN_DIR / "source" / "wscan_XZym4_b.npz",
    CAMPAIGN_DIR / "source" / "heart_s7_f7_OFFWxz.npz",
    CAMPAIGN_DIR / "source" / "heart_s7_f7_CWxz.npz",
    CAMPAIGN_DIR / "source" / "heart_s7_f7_OFFW2.npz",
    CAMPAIGN_DIR / "source" / "heart_s7_f7_CW2.npz",
]
EXPECTED_ORDER = [
    "kcheck_x_S105_6jumps",
    "kcheck_z_S105_6jumps",
    "ffheart_s7_f10_OFF",
    "ffheart_s7_f10_A_delay",
    "ffheart_s7_f10_C_delay_inverse",
    "ffheart_OT_ident_nodelay",
    "ffheart_A_delay03",
    "ffheart_C_delay03_inv_k918",
    "ffheart_s7_f5_OFF",
    "ffheart_s7_f7_OFF",
    "ffheart_s7_f1_OFF",
    "ffheart_s7_f2_OFF",
    "kcheck_y_S105_6jumps",
    "ffheart_s7_f1_OFF_yz",
    "ffheart_s7_f2_OFF_yz",
    "ffheart_s7_f5_OFF_yz",
    "ffheart_s7_f7_OFF_yz",
    "ffheart_s7_f10_OFF_yz",
    "ffheart_s7_f7_OFFW",
    "ffheart_s7_f7_CW",
    "ffheart_s7_f7_C_delay_inv",
    "ffheart_s7_f10_OFFW",
    "ffheart_s7_f10_CW",
    "wscan_XZ_a",
    "wscan_XZ_b",
    "wscan_YZ_a",
    "wscan_YZ_b",
    "wscan_XY_a",
    "wscan_XY_b",
    "wscan_XZyp4_a",
    "wscan_XZyp4_b",
    "wscan_XZym4_a",
    "wscan_XZym4_b",
    "ffheart_s7_f7_OFFWxz",
    "ffheart_s7_f7_CWxz",
    "ffheart_s7_f7_OFFW2",
    "ffheart_s7_f7_CW2",
]
# The w-field surface scans (2026-09-20): spirals of 14 s, u = r, in these planes.
SCAN_AXES = {
    "wscan_XZ_a": "xz", "wscan_XZ_b": "xz",
    "wscan_YZ_a": "yz", "wscan_YZ_b": "yz",
    "wscan_XY_a": "xy", "wscan_XY_b": "xy",
    "wscan_XZyp4_a": "xyz", "wscan_XZyp4_b": "xyz",
    "wscan_XZym4_a": "xyz", "wscan_XZym4_b": "xyz",
}
# w-compensated hearts (2026-09-20) and the 7 Hz C: name -> (max |u - r| mm, acceleration limit).
# The 10 Hz W runs exceed the plan's 100,000 mm/s^2 and carry their own 110,000 limit.
W_RUNS = {
    "ffheart_s7_f7_OFFW": (0.5640, 100000.0),
    "ffheart_s7_f7_CW": (0.8338, 100000.0),
    "ffheart_s7_f7_C_delay_inv": (0.4792, 100000.0),
    "ffheart_s7_f10_OFFW": (0.3926, 110000.0),
    "ffheart_s7_f10_CW": (0.8858, 110000.0),
    # Stage 2 (2026-09-21): the xz pair leaves the out-of-plane y uncompensated.
    "ffheart_s7_f7_OFFWxz": (0.3229, 100000.0),
    "ffheart_s7_f7_CWxz": (0.6977, 100000.0),
    "ffheart_s7_f7_OFFW2": (0.4652, 100000.0),
    "ffheart_s7_f7_CW2": (0.7703, 100000.0),
}
W_XZ_ONLY = {"ffheart_s7_f7_OFFWxz", "ffheart_s7_f7_CWxz"}
# Same heart without compensation at a lower loop frequency: far below the speed and
# acceleration limits by design (name -> loop frequency in Hz).
SLOW_OFF_RUNS = {
    "ffheart_s7_f1_OFF": 1.0, "ffheart_s7_f2_OFF": 2.0,
    "ffheart_s7_f5_OFF": 5.0, "ffheart_s7_f7_OFF": 7.0,
    "ffheart_s7_f1_OFF_yz": 1.0, "ffheart_s7_f2_OFF_yz": 2.0,
    "ffheart_s7_f5_OFF_yz": 5.0, "ffheart_s7_f7_OFF_yz": 7.0,
}
# The 1 Hz and 2 Hz commands last 14 s, every other heart 8 s.
LONG_RUNS = {"ffheart_s7_f1_OFF", "ffheart_s7_f2_OFF", "ffheart_s7_f1_OFF_yz", "ffheart_s7_f2_OFF_yz"} | set(SCAN_AXES)


def official_export_matches_plan() -> bool:
    """The export is regenerated by the batch script when the plan changes; skip until then."""
    import hashlib

    manifest_path = OFFICIAL_EXPORT / "export_manifest.json"
    if not manifest_path.is_file():
        return False
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return manifest.get("source_plan_sha256") == hashlib.sha256(OFFICIAL_PLAN.read_bytes()).hexdigest()


# Recorded on 2026-09-18 under this name; its folder name is shortened (see naming_note).
LEGACY_SHORTENED_RUNS = {"ffheart_s7_f10_C_delay_inverse"}
DEFAULT_OUTPUT_ROOT = Path(
    r"C:\Users\digit\Documents\scripts\python\eventcam_control"
    r"\stereo_acoustools_3d_records_ff_heart"
)
LIMITS = {
    "max_offset_mm": 2.0,
    "max_speed_mm_s": 400.0,
    "max_acceleration_mm_s2": 100000.0,
    "max_endpoint_offset_mm": 0.01,
    "max_command_reference_distance_mm": 0.5,
}


def _small_command(samples: int = 500, sample_hz: float = 10000.0, lead: float = 0.0):
    """A ramped 1 mm circle in XZ and a command that leads it by ``lead`` radians."""
    t = np.arange(samples) / sample_hz
    envelope = np.sin(np.pi * np.arange(samples) / (samples - 1)) ** 2
    theta = 2.0 * np.pi * 40.0 * t
    reference = np.zeros((samples, 3))
    reference[:, 0] = np.sin(theta) * envelope
    reference[:, 2] = np.cos(theta) * envelope
    command = np.zeros((samples, 3))
    command[:, 0] = np.sin(theta + lead) * envelope
    command[:, 2] = np.cos(theta + lead) * envelope
    return t, command, reference


def _write_source(path: Path, *, lead: float = 0.0, **overrides) -> np.ndarray:
    t, command, reference = _small_command(lead=lead)
    arrays = {
        "positions_mm": command,
        "reference_mm": reference,
        "time_pat_sec": t,
        "time_cam_expected_sec": t * (1.0 + 182e-6) + 0.0008,
        "sample_hz": 10000.0,
        "params": json.dumps({"scale_mm": 1.0, "tau0_ms": 0.8}),
    }
    arrays.update(overrides)
    np.savez_compressed(path, **arrays)
    return np.asarray(arrays["positions_mm"], dtype=float)


def _imported_spec(name: str, source: str, design: str = "OFF", **extra) -> dict:
    spec = {
        "name": name,
        "kind": "imported",
        "source_npz": source,
        "extra_time_keys": ["time_pat_sec", "time_cam_expected_sec"],
        "campaign": "ff_test",
        "measurement_family": hf.FEEDFORWARD_VALIDATION_FAMILY,
        "condition": design,
        "feedforward_design": {
            "design": design,
            "command_differs_from_reference": design != "OFF",
        },
        "enabled": True,
    }
    spec.update(extra)
    return spec


def _defaults(**overrides) -> dict:
    defaults = {
        "sample_hz": 10000,
        "duration_sec": 0.05,
        "ramp_sec": 0.0,
        "safety_limits": dict(LIMITS),
    }
    defaults.update(overrides)
    return defaults


class ImportedCommandGenerationTests(unittest.TestCase):
    def test_imported_command_is_numerically_unchanged_and_keeps_reference(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = _write_source(root / "a.npz", lead=0.2)
            spec = _imported_spec("ff_a", "a.npz", "A_delay", _plan_dir=str(root))
            offset, metadata = hf.generate_trajectory(spec, _defaults())
            self.assertTrue(np.array_equal(offset, command))
            self.assertEqual(metadata["kind"], "imported")
            self.assertEqual(metadata["axis"], "xz")
            self.assertEqual(metadata["axes"], ["x", "z"])
            self.assertEqual(metadata["samples"], 500)
            self.assertEqual(metadata["safety_scale_applied"], 1.0)
            self.assertEqual(metadata["safety_policy"], "reject_above_limits")
            detail = metadata["generation_detail"]
            self.assertEqual(detail["positions_sha256"], hf.array_sha256(command))
            self.assertTrue(detail["reference_available"])
            self.assertEqual(detail["source_params"]["tau0_ms"], 0.8)
            self.assertAlmostEqual(
                detail["max_command_reference_distance_mm"], 2.0 * np.sin(0.1), delta=1e-3
            )
            self.assertEqual(metadata["feedforward_design"]["design"], "A_delay")
            self.assertNotIn("_plan_dir", json.dumps(metadata))

    def test_swap_axes_plays_the_reviewed_command_in_another_plane(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = _write_source(root / "a.npz", lead=0.2)
            spec = _imported_spec(
                "ff_a_yz", "a.npz", "A_delay", _plan_dir=str(root),
                positions_sha256=hf.array_sha256(command), swap_axes=["x", "y"],
            )
            offset, metadata = hf.generate_trajectory(spec, _defaults())
            self.assertTrue(np.array_equal(offset, command[:, [1, 0, 2]]))
            self.assertEqual(metadata["axis"], "yz")
            detail = metadata["generation_detail"]
            self.assertEqual(detail["swap_axes"], ["x", "y"])
            self.assertEqual(detail["source_positions_sha256"], hf.array_sha256(command))
            self.assertEqual(detail["positions_sha256"], hf.array_sha256(offset))
            # The reference moves with the command, so |u - r| is unchanged.
            imported = hf.load_imported_command(spec)
            self.assertTrue(np.all(imported.reference_mm[:, 0] == 0.0))
            self.assertAlmostEqual(
                detail["max_command_reference_distance_mm"], 2.0 * np.sin(0.1), delta=1e-3
            )
            # The pin still protects the source file.
            with self.assertRaisesRegex(ValueError, "not the reviewed command"):
                hf.generate_trajectory(dict(spec, positions_sha256="0" * 64), _defaults())
            for bad in (["x"], ["x", "x"], ["x", "w"], ["x", "y", "z"]):
                with self.subTest(swap_axes=bad):
                    with self.assertRaisesRegex(ValueError, "swap_axes"):
                        hf.generate_trajectory(dict(spec, swap_axes=bad), _defaults())
            # Exported and loaded for hardware: the recorded hash is that of the swapped export.
            plan = {"schema_version": 1, "defaults": _defaults(), "experiments": [
                {key: value for key, value in spec.items() if key != "_plan_dir"}
            ]}
            (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            with mock.patch.object(
                hf, "write_preview", side_effect=lambda path, *_a, **_k: path.write_bytes(b"p")
            ):
                hf.export_plan(root / "plan.json", root / "export", False, False)
            npz_path = root / "export" / "ff_a_yz" / "command_trajectory.npz"
            positions, _hz, _metadata = hf.load_trajectory_for_hardware(npz_path, (0.0, 0.0, 0.0))
            self.assertTrue(np.allclose(np.asarray(positions) * 1e3, command[:, [1, 0, 2]], atol=1e-12))
            reference = hf.load_reference_for_hardware(npz_path, (0.0, 0.0, 0.0))
            self.assertTrue(np.all(np.asarray(reference)[:, 0] == 0.0))

    def test_pinned_content_hash_must_match(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = _write_source(root / "a.npz")
            good = _imported_spec(
                "ff", "a.npz", _plan_dir=str(root), positions_sha256=hf.array_sha256(command)
            )
            _offset, metadata = hf.generate_trajectory(good, _defaults())
            self.assertTrue(metadata["generation_detail"]["positions_sha256_pinned"])
            bad = dict(good, positions_sha256="0" * 64)
            with self.assertRaisesRegex(ValueError, "not the reviewed command"):
                hf.generate_trajectory(bad, _defaults())

    def test_imported_command_is_rejected_not_rescaled_above_any_limit(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_source(root / "lead.npz", lead=0.2)
            spec = _imported_spec("ff", "lead.npz", "A_delay", _plan_dir=str(root))
            cases = {
                "never rescaled": {"max_speed_mm_s": 100.0},
                "departs .* from its reference": {"max_command_reference_distance_mm": 0.1},
                "max_offset_mm": {"max_offset_mm": 0.5},
            }
            for message, change in cases.items():
                with self.subTest(change=change):
                    limits = dict(LIMITS, **change)
                    with self.assertRaisesRegex(ValueError, message):
                        hf.generate_trajectory(spec, _defaults(safety_limits=limits))
            t, command, reference = _small_command()
            command = command + np.array([0.05, 0.0, 0.0])
            _write_source(root / "offset.npz", positions_mm=command)
            with self.assertRaisesRegex(ValueError, "starts or ends"):
                hf.generate_trajectory(
                    _imported_spec("ff", "offset.npz", _plan_dir=str(root)), _defaults()
                )

    def test_bad_sources_are_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_source(root / "ok.npz")
            _t, command, reference = _small_command()
            broken = command.copy()
            broken[10, 0] = np.nan
            _write_source(root / "nan.npz", positions_mm=broken)
            _write_source(root / "shape.npz", positions_mm=command[:, :2])
            _write_source(root / "ref.npz", reference_mm=reference[:-1])
            _write_source(root / "rate.npz", sample_hz=8000.0)
            _write_source(root / "time.npz", time_pat_sec=np.arange(3, dtype=float))
            cases = [
                (_imported_spec("ff", "missing.npz"), "source_npz not found"),
                (_imported_spec("ff", ""), "require source_npz"),
                (_imported_spec("ff", "nan.npz"), "non-finite"),
                (_imported_spec("ff", "shape.npz"), r"shape \(samples>=3, 3\)"),
                (_imported_spec("ff", "ref.npz"), "shape differs"),
                (_imported_spec("ff", "rate.npz"), "differs from the plan"),
                (_imported_spec("ff", "time.npz"), "one value per sample"),
                (_imported_spec("ff", "ok.npz", positions_key="nope"), "has no array"),
                (_imported_spec("ff", "ok.npz", duration_sec=0.06), "requires 600"),
            ]
            for spec, message in cases:
                with self.subTest(message=message):
                    spec["_plan_dir"] = str(root)
                    with self.assertRaisesRegex(ValueError, message):
                        hf.generate_trajectory(spec, _defaults())

    def test_per_experiment_safety_limits_override_plan_defaults(self) -> None:
        defaults = _defaults(duration_sec=1.0)
        step = {
            "name": "C0_x_staircase_check",
            "kind": "staircase",
            "tier": "C",
            "axis": "x",
            "hold_sec": 0.01,
            "amplitudes_mm": [1.05],
            "directions": [1, -1, 1],
            "repeats_within_run": 1,
            "safety_limits": {
                "max_offset_mm": 1.05,
                "max_step_mm": 1.05,
                "max_speed_mm_s": 1.0,
                "max_acceleration_mm_s2": 1.0,
            },
        }
        offset, metadata = hf.generate_trajectory(step, defaults)
        self.assertEqual(metadata["safety_limits"], step["safety_limits"])
        self.assertEqual(metadata["generation_detail"]["n_jumps"], 6)
        self.assertEqual(
            metadata["generation_detail"]["levels_mm"],
            [0.0, 1.05, 0.0, -1.05, 0.0, 1.05, 0.0],
        )
        self.assertEqual(float(np.max(np.abs(offset))), 1.05)
        too_large = dict(step, amplitudes_mm=[1.06])
        with self.assertRaisesRegex(ValueError, "exceeds"):
            hf.generate_trajectory(too_large, defaults)
        with self.assertRaisesRegex(ValueError, "non-empty object"):
            hf.generate_trajectory(dict(step, safety_limits={}), defaults)


class ImportedExportTests(unittest.TestCase):
    def _export(self, root: Path) -> Path:
        _write_source(root / "off.npz")
        _write_source(root / "lead.npz", lead=0.2)
        plan = {
            "schema_version": 1,
            "campaign": "ff_test",
            "measurement_family": hf.FEEDFORWARD_VALIDATION_FAMILY,
            "defaults": _defaults(),
            "experiments": [
                {
                    "name": "kcheck_x",
                    "kind": "staircase",
                    "tier": "C",
                    "axis": "x",
                    "hold_sec": 0.01,
                    "amplitudes_mm": [1.05],
                    "directions": [1, -1, 1],
                    "repeats_within_run": 1,
                    "safety_limits": {
                        "max_offset_mm": 1.05,
                        "max_step_mm": 1.05,
                        "max_speed_mm_s": 100.0,
                        "max_acceleration_mm_s2": 28000.0,
                    },
                    "enabled": True,
                },
                _imported_spec("ff_off", "off.npz", "OFF"),
                _imported_spec("ff_a", "lead.npz", "A_delay"),
            ],
        }
        plan_path = root / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        export_dir = root / "export"

        def fake_preview(path: Path, *_args: object) -> None:
            path.write_bytes(b"preview")

        with mock.patch.object(hf, "write_preview", side_effect=fake_preview):
            hf.export_plan(plan_path, export_dir, include_disabled=False, write_csv=False)
        return export_dir

    def test_export_keeps_reference_and_time_arrays_and_writes_csv(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export(root)
            with np.load(export_dir / "ff_a" / "command_trajectory.npz") as data:
                self.assertEqual(
                    sorted(data.files),
                    sorted(
                        [
                            "time_sec", "offset_mm", "offset_m", "sample_hz",
                            "reference_mm", "time_pat_sec", "time_cam_expected_sec",
                        ]
                    ),
                )
                exported = np.asarray(data["offset_mm"])
                reference = np.asarray(data["reference_mm"])
            with np.load(root / "lead.npz") as source:
                self.assertTrue(np.array_equal(exported, source["positions_mm"]))
                self.assertTrue(np.array_equal(reference, source["reference_mm"]))
            self.assertTrue((export_dir / "ff_a" / "command_offset_log.csv").is_file())
            manifest = json.loads(
                (export_dir / "export_manifest.json").read_text(encoding="utf-8")
            )
            self.assertIs(manifest["delay_feedforward_applied"], False)
            _path, runs = load_hf_export_runs(export_dir)
            self.assertEqual([run.name for run in runs], ["kcheck_x", "ff_off", "ff_a"])
            self.assertFalse(is_feedforward_validation_metadata(runs[0].metadata))
            self.assertTrue(is_feedforward_validation_metadata(runs[2].metadata))

    def test_prepared_trajectory_carries_command_and_scaled_reference(self) -> None:
        with TemporaryDirectory() as temporary:
            export_dir = self._export(Path(temporary))
            _path, runs = load_hf_export_runs(export_dir)
            by_name = {run.name: run for run in runs}
            centre = (0.001, -0.002, 0.003)
            lead = prepare_hf_trajectory(by_name["ff_a"], center_m=centre, command_scale=0.5)
            self.assertEqual(lead.shape_name, "feedforward_validation")
            self.assertEqual(len(lead.reference_positions), len(lead.positions))
            positions = np.asarray(lead.positions)
            reference = np.asarray(lead.reference_positions)
            self.assertTrue(np.allclose(positions[:, 1], -0.002))
            self.assertTrue(np.allclose(reference[:, 1], -0.002))
            _t, _command, source_reference = _small_command()
            self.assertTrue(
                np.allclose(
                    reference, np.asarray(centre) + 0.5e-3 * source_reference, atol=1e-12
                )
            )
            self.assertGreater(float(np.max(np.abs(positions - reference))), 5e-5)
            source = lead.trajectory_source
            self.assertTrue(source["imported_command"])
            self.assertTrue(source["reference_trajectory_logged"])
            self.assertTrue(source["command_contains_offline_feedforward"])
            self.assertIs(source["delay_feedforward_applied"], False)
            self.assertEqual(lead.parameters["feedforward_design"]["design"], "A_delay")
            off = prepare_hf_trajectory(by_name["ff_off"], center_m=centre, command_scale=1.0)
            self.assertFalse(off.trajectory_source["command_contains_offline_feedforward"])
            self.assertEqual(off.positions, off.reference_positions)
            step = prepare_hf_trajectory(by_name["kcheck_x"], center_m=centre, command_scale=1.0)
            self.assertIsNone(step.reference_positions)
            self.assertEqual(step.shape_name, "step_response_identification")

    def test_hardware_loader_detects_a_modified_export(self) -> None:
        with TemporaryDirectory() as temporary:
            export_dir = self._export(Path(temporary))
            npz_path = export_dir / "ff_a" / "command_trajectory.npz"
            with np.load(npz_path) as data:
                arrays = {key: np.asarray(data[key]) for key in data.files}
            arrays["offset_mm"] = arrays["offset_mm"] * 0.999
            np.savez_compressed(npz_path, **arrays)
            with self.assertRaisesRegex(ValueError, "no longer matches"):
                hf.load_trajectory_for_hardware(npz_path, (0.0, 0.0, 0.0))

    def test_loader_rejects_incomplete_feedforward_metadata(self) -> None:
        with TemporaryDirectory() as temporary:
            export_dir = self._export(Path(temporary))
            metadata_path = export_dir / "ff_a" / "trajectory_metadata.json"
            original = json.loads(metadata_path.read_text(encoding="utf-8"))
            broken_cases = {
                "requires feedforward_design": lambda m: m.pop("feedforward_design"),
                "must be imported commands": lambda m: m.update(kind="chirp"),
                "source_sha256 and positions_sha256": lambda m: m["generation_detail"].pop(
                    "positions_sha256"
                ),
                "must not be rescaled": lambda m: m.update(safety_scale_applied=0.9),
            }
            for message, mutate in broken_cases.items():
                with self.subTest(message=message):
                    metadata = json.loads(json.dumps(original))
                    mutate(metadata)
                    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, message):
                        load_hf_export_runs(export_dir)


class OfficialFfHeartPlanTests(unittest.TestCase):
    def test_official_plan_structure(self) -> None:
        plan = hf.load_plan(OFFICIAL_PLAN)
        self.assertEqual(plan["measurement_family"], hf.FEEDFORWARD_VALIDATION_FAMILY)
        self.assertEqual([spec["name"] for spec in plan["experiments"]], EXPECTED_ORDER)
        limits = plan["defaults"]["safety_limits"]
        self.assertEqual(limits["max_command_reference_distance_mm"], 1.0)
        self.assertEqual(limits["max_endpoint_offset_mm"], 0.01)
        checks = [spec for spec in plan["experiments"] if spec["kind"] == "staircase"]
        imported = [spec for spec in plan["experiments"] if spec["kind"] != "staircase"]
        self.assertEqual([spec["axis"] for spec in checks], ["x", "z", "y"])
        for spec in checks:
            self.assertEqual(spec["name"], f"kcheck_{spec['axis']}_S105_6jumps")
            self.assertEqual(spec["tier"], "C")
            self.assertEqual(spec["safety_limits"]["max_step_mm"], 1.05)
            offset, metadata = hf.generate_trajectory(spec, plan["defaults"])
            self.assertEqual(metadata["duration_sec"], 14.0)
            self.assertEqual(metadata["generation_detail"]["n_jumps"], 6)
            self.assertEqual(metadata["measurement_family"], "step_response_identification")
            self.assertEqual(float(np.max(np.abs(offset))), 1.05)
            self.assertEqual(float(np.max(np.abs(offset[:, hf.AXIS_INDEX[spec["axis"]]]))), 1.05)
        designs = [spec["feedforward_design"]["design"] for spec in imported]
        self.assertEqual(
            designs,
            [
                "OFF",
                "A_delay",
                "C_delay_inverse",
                "OT_ident_nodelay",
                "A_delay03",
                "C_delay03_inverse_k918",
            ]
            + ["OFF"] * 9
            + ["OFFW", "CW", "C_delay_inverse", "OFFW", "CW"]
            + ["WSCAN"] * len(SCAN_AXES)
            + ["OFFWxz", "CWxz", "OFFW2", "CW2"],
        )
        for spec in imported:
            if spec["name"] in W_RUNS and W_RUNS[spec["name"]][1] != 100000.0:
                # Only the acceleration limit is raised; every other limit is the plan default.
                raised = dict(plan["defaults"]["safety_limits"], max_acceleration_mm_s2=110000.0)
                self.assertEqual(spec["safety_limits"], raised)
            else:
                self.assertNotIn("safety_limits", spec)
            is_yz = spec["name"].endswith("_yz")
            self.assertEqual(spec.get("swap_axes"), ["x", "y"] if is_yz else None)
            self.assertEqual(spec.get("duration_sec"), 14.0 if spec["name"] in LONG_RUNS else None)
            if is_yz:
                # Same reviewed source file and the same pin as the XZ run.
                twin = next(item for item in imported if item["name"] == spec["name"][:-3])
                self.assertEqual(spec["source_npz"], twin["source_npz"])
                self.assertEqual(spec["positions_sha256"], twin["positions_sha256"])
        for name, loop_hz in SLOW_OFF_RUNS.items():
            spec = next(item for item in plan["experiments"] if item["name"] == name)
            self.assertEqual(spec["feedforward_design"]["loop_frequency_hz"], loop_hz)
            self.assertIn(f"{loop_hz:g} Hz", spec["reference"]["trajectory"])
        for spec in imported:
            self.assertEqual(spec["kind"], "imported")
            self.assertEqual(len(spec["positions_sha256"]), 64)
            # Only the uncompensated designs (OFF, and the WSCAN surveys) command u = r.
            self.assertEqual(
                spec["feedforward_design"]["command_differs_from_reference"],
                spec["feedforward_design"]["design"] not in ("OFF", "WSCAN"),
            )

    @unittest.skipUnless(all(path.is_file() for path in OFFICIAL_SOURCES), "source NPZs absent")
    def test_official_sources_match_pins_and_limits(self) -> None:
        plan = hf.load_plan(OFFICIAL_PLAN)
        expected_distance = {
            "OFF": 0.0,
            "A_delay": 0.5958,
            "C_delay_inverse": 0.7130,
            "OT_ident_nodelay": 0.2871,
            "A_delay03": 0.2235,
            "C_delay03_inverse_k918": 0.4546,
        }
        for spec in (item for item in plan["experiments"] if item["kind"] == "imported"):
            with self.subTest(name=spec["name"]):
                run_spec = dict(spec, _plan_dir=str(CAMPAIGN_DIR.resolve()))
                offset, metadata = hf.generate_trajectory(run_spec, plan["defaults"])
                samples = 140000 if spec["name"] in LONG_RUNS else 80000
                self.assertEqual(offset.shape, (samples, 3))
                detail = metadata["generation_detail"]
                if spec["name"].endswith("_yz"):
                    self.assertEqual(metadata["axis"], "yz")
                    self.assertTrue(np.all(offset[:, 0] == 0.0))
                    # The pin is the content of the reviewed source; the export is its x/y swap.
                    self.assertEqual(detail["source_positions_sha256"], spec["positions_sha256"])
                    self.assertEqual(detail["positions_sha256"], hf.array_sha256(offset))
                    twin_spec = dict(run_spec, name=spec["name"][:-3])
                    del twin_spec["swap_axes"]
                    twin, _twin_metadata = hf.generate_trajectory(twin_spec, plan["defaults"])
                    self.assertTrue(np.array_equal(offset, twin[:, [1, 0, 2]]))
                elif spec["name"] in SCAN_AXES:
                    # Spiral survey: starts and ends at the centre, and u = r everywhere.
                    self.assertEqual(metadata["axis"], SCAN_AXES[spec["name"]])
                    self.assertEqual(detail["positions_sha256"], spec["positions_sha256"])
                    self.assertTrue(np.array_equal(offset[0], np.zeros(3)))
                    self.assertTrue(np.array_equal(offset[-1], np.zeros(3)))
                    radius = np.linalg.norm(offset, axis=1).max()
                    self.assertAlmostEqual(radius, 8.856 if "y" in spec["name"][7:] else 9.2, delta=1e-3)
                else:
                    # w is three-dimensional, so the W commands also move in y.
                    has_y = spec["feedforward_design"]["design"] in ("OFFW", "CW", "OFFW2", "CW2")
                    if spec["name"] in W_XZ_ONLY:
                        self.assertEqual(spec["feedforward_design"]["w_axes"], "xz")
                        self.assertFalse(has_y)
                    self.assertEqual(metadata["axis"], "xyz" if has_y else "xz")
                    self.assertEqual(bool(np.any(offset[:, 1] != 0.0)), has_y)
                    self.assertEqual(detail["positions_sha256"], spec["positions_sha256"])
                design = spec["feedforward_design"]["design"]
                self.assertAlmostEqual(
                    detail["max_command_reference_distance_mm"],
                    0.0 if design == "WSCAN"
                    else W_RUNS[spec["name"]][0] if spec["name"] in W_RUNS
                    else expected_distance[design],
                    delta=1e-3,
                )
                self.assertLess(detail["max_endpoint_offset_mm"], 0.001)
                fractions = metadata["safety_limit_fractions"]
                for fraction in fractions.values():
                    self.assertLess(fraction, 1.0)
                if spec["name"] in SLOW_OFF_RUNS:
                    # Same path as the 10 Hz heart; speed scales with f and acceleration with f^2.
                    ratio = SLOW_OFF_RUNS[spec["name"]] / 10.0
                    self.assertGreater(fractions["max_offset_mm"], 0.85)
                    self.assertAlmostEqual(fractions["max_speed_mm_s"], 0.9312 * ratio, delta=2e-3)
                    self.assertAlmostEqual(
                        fractions["max_acceleration_mm_s2"], 0.9340 * ratio**2, delta=2e-3
                    )
                elif spec["name"] in SCAN_AXES:
                    # Slow on purpose, so only the offset is near its limit.
                    self.assertGreater(fractions["max_offset_mm"], 0.85)
                    self.assertLess(fractions["max_speed_mm_s"], 0.2)
                    self.assertLess(fractions["max_acceleration_mm_s2"], 0.05)
                elif spec["name"] in W_RUNS:
                    accel_limit = W_RUNS[spec["name"]][1]
                    self.assertEqual(metadata["safety_limits"]["max_acceleration_mm_s2"], accel_limit)
                    peak = metadata["metrics"]["max_acceleration_mm_s2"]["vector"]
                    # The 10 Hz W runs really need the raised limit; the 7 Hz runs do not.
                    self.assertEqual(peak > 100000.0, accel_limit > 100000.0)
                else:
                    for fraction in fractions.values():
                        self.assertGreater(fraction, 0.85)

    def test_official_run_directories_keep_the_full_design_name(self) -> None:
        # Analysis scripts find runs by "_<design>_" in the directory name, so new official
        # runs must not be shortened to a hash under the default output folder of this PC.
        # C is the documented exception: it keeps the name it was recorded under.
        from stereo_acoustools_3d_recording_core import safe_run_dir_base

        plan = hf.load_plan(OFFICIAL_PLAN)
        stamp = "20260918_220000"
        for index, spec in enumerate(plan["experiments"]):
            for scale_tag in ("scale100", "scale050"):
                with self.subTest(name=spec["name"], scale=scale_tag):
                    label = f"{index:03d}_{spec['name']}_{scale_tag}"
                    shape = (
                        "step_response_identification"
                        if spec["kind"] == "staircase"
                        else "feedforward_validation"
                    )
                    base = safe_run_dir_base(DEFAULT_OUTPUT_ROOT, shape, label, stamp)
                    if spec["name"] in LEGACY_SHORTENED_RUNS:
                        self.assertNotEqual(base, f"{shape}_{label}_{stamp}")
                        self.assertTrue(
                            base.startswith("feedforward_validation_004_ffheart_s7_f10_C_delay_in_")
                        )
                        continue
                    self.assertEqual(base, f"{shape}_{label}_{stamp}")
                    if spec["kind"] == "imported" and spec["name"] not in SCAN_AXES:
                        # The analysis finds designs by these prefixes of the design name.
                        design = spec["feedforward_design"]["design"]
                        prefix = "_" + "_".join(design.split("_")[:2])
                        self.assertIn(prefix, base)

    @unittest.skipUnless(official_export_matches_plan(), "export absent or older than the plan")
    def test_official_export_loads_and_prepares_reference(self) -> None:
        _path, runs = load_hf_export_runs(OFFICIAL_EXPORT)
        self.assertEqual([run.name for run in runs], EXPECTED_ORDER)
        by_name = {run.name: run for run in runs}
        trajectory = prepare_hf_trajectory(
            by_name["ffheart_s7_f10_A_delay"], center_m=(0.0, 0.0, 0.0), command_scale=1.0
        )
        self.assertEqual(len(trajectory.positions), 80000)
        self.assertEqual(len(trajectory.reference_positions), 80000)
        distance = np.linalg.norm(
            np.asarray(trajectory.positions) - np.asarray(trajectory.reference_positions), axis=1
        )
        self.assertAlmostEqual(float(distance.max()) * 1e3, 0.5958, delta=1e-3)
        self.assertEqual(trajectory.run_label, "003_ffheart_s7_f10_A_delay_scale100")


class FfAutoEntryTests(unittest.TestCase):
    _export = ImportedExportTests._export

    def _run(self, argv: list[str], record_results=None):
        events = mock.Mock()
        session = SimpleNamespace(current_pos=(0.0, 0.0, 0.0))
        playback = SimpleNamespace(name="first-run-playback")
        events.open_hw.return_value = session
        events.precompute.return_value = playback
        if record_results is None:
            events.record.side_effect = lambda *_a, **_k: (0, None)
        else:
            events.record.side_effect = list(record_results)
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
            result, events, _playback = self._run(
                ["--hf-export-dir", str(export_dir), "--dry-run"]
            )
            self.assertEqual(result, 0)
            events.open_hw.assert_not_called()
            events.record.assert_not_called()

    def test_hardware_runs_are_rejected_before_open_without_required_acks(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export(root)
            hf_root = root / "chirp"
            hf_root.mkdir()
            chirp_plan = {
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
            (hf_root / "plan.json").write_text(json.dumps(chirp_plan), encoding="utf-8")
            with mock.patch.object(
                hf, "write_preview", side_effect=lambda path, *_a: path.write_bytes(b"p")
            ):
                hf.export_plan(hf_root / "plan.json", hf_root / "export", False, True)
            ff = ["--acknowledge-ff-validation-protocol"]
            step = ["--acknowledge-step-response-risk"]
            base = ["--hf-export-dir", str(export_dir), "--output-dir", str(root / "records")]
            cases = {
                "no ack": base + ["--label", "ff_off"],
                "hf ack is not the ff ack": base
                + ["--label", "ff_off", "--acknowledge-hf-retention-and-visibility"],
                "sandwich step needs its own ack": base + ff,
                "keep going": base + ff + step + ["--keep-going"],
                "mixed with a chirp export": base
                + ["--hf-export-dir", str(hf_root / "export")]
                + ff + step + ["--acknowledge-hf-retention-and-visibility"],
                "unattended with confirm": base + ff + step
                + ["--unattended-after-first-checkpoint", "--confirm-each-run"],
            }
            for name, argv in cases.items():
                with self.subTest(case=name):
                    result, events, _playback = self._run(argv)
                    self.assertEqual(result, 2)
                    events.open_hw.assert_not_called()
                    events.record.assert_not_called()
            self.assertFalse((root / "records").exists())

    def test_every_run_has_a_forced_checkpoint_by_default(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export(root)
            output_root = root / "records"
            result, events, _playback = self._run(
                [
                    "--hf-export-dir", str(export_dir),
                    "--output-dir", str(output_root),
                    "--acknowledge-ff-validation-protocol",
                    "--acknowledge-step-response-risk",
                    "--no-preview",
                ]
            )
            self.assertEqual(result, 0)
            events.open_hw.assert_called_once()
            events.precompute.assert_not_called()
            calls = events.record.call_args_list
            self.assertEqual(
                [call.kwargs["automation_metadata"]["label"] for call in calls],
                ["kcheck_x", "ff_off", "ff_a"],
            )
            self.assertEqual(
                [call.kwargs["automation_metadata"]["mode"] for call in calls],
                [
                    "step_response_identification",
                    hf.FEEDFORWARD_VALIDATION_FAMILY,
                    hf.FEEDFORWARD_VALIDATION_FAMILY,
                ],
            )
            for call in calls:
                self.assertFalse(call.args[0].no_preview)
                self.assertTrue(call.kwargs["prompt_before_capture"])
                self.assertIsNone(call.kwargs["automatic_capture_retries"])
                self.assertFalse(call.kwargs["automation_metadata"]["delay_feedforward_applied"])
            self.assertEqual(
                calls[2].kwargs["automation_metadata"]["feedforward_design"]["design"], "A_delay"
            )
            self.assertIsNotNone(calls[2].kwargs["prepared_trajectory"].reference_positions)
            session = json.loads(
                next(output_root.glob("auto_recording_session_*.json")).read_text(encoding="utf-8")
            )
            self.assertEqual(session["trajectory_mode"], hf.FEEDFORWARD_VALIDATION_FAMILY)
            self.assertEqual(session["status"], "complete")

    def test_unattended_sandwich_sequence_repeats_labels_in_order(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export(root)
            sequence = [
                "kcheck_x=1.0", "ff_off=1.0", "kcheck_x=1.0", "ff_a=0.5",
                "kcheck_x=1.0", "ff_a=1.0", "kcheck_x=1.0",
            ]
            argv = ["--hf-export-dir", str(export_dir), "--output-dir", str(root / "records")]
            for request in sequence:
                argv += ["--hf-run", request]
            argv += [
                "--acknowledge-ff-validation-protocol",
                "--acknowledge-step-response-risk",
                "--unattended-after-first-checkpoint",
            ]
            result, events, playback = self._run(argv)
            self.assertEqual(result, 0)
            names = [name for name, _a, _k in events.mock_calls]
            self.assertLess(names.index("precompute"), names.index("open_hw"))
            calls = events.record.call_args_list
            self.assertEqual(
                [call.kwargs["prepared_trajectory"].run_label for call in calls],
                [
                    "000_kcheck_x_scale100", "001_ff_off_scale100", "000_kcheck_x_scale100",
                    "002_ff_a_scale050", "000_kcheck_x_scale100", "002_ff_a_scale100",
                    "000_kcheck_x_scale100",
                ],
            )
            first, *later = calls
            self.assertFalse(first.args[0].no_preview)
            self.assertTrue(first.kwargs["prompt_before_capture"])
            self.assertIs(first.kwargs["precomputed_playback"], playback)
            for call in later:
                self.assertTrue(call.args[0].no_preview)
                self.assertFalse(call.kwargs["prompt_before_capture"])
                self.assertIsNone(call.kwargs["precomputed_playback"])
            for call in calls:
                self.assertEqual(call.kwargs["automatic_capture_retries"], 2)
            half = np.asarray(calls[3].kwargs["prepared_trajectory"].positions)
            full = np.asarray(calls[5].kwargs["prepared_trajectory"].positions)
            self.assertTrue(np.allclose(half, 0.5 * full, atol=1e-12))

    def test_dry_run_warns_when_a_run_directory_name_will_be_shortened(self) -> None:
        import contextlib
        import io

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_source(root / "off.npz")
            long_name = "ff_" + "x" * 90
            plan = {
                "schema_version": 1,
                "defaults": _defaults(),
                "experiments": [
                    _imported_spec("ff_off", "off.npz", "OFF"),
                    _imported_spec(long_name, "off.npz", "OFF"),
                ],
            }
            (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            with mock.patch.object(
                hf, "write_preview", side_effect=lambda path, *_a: path.write_bytes(b"p")
            ):
                hf.export_plan(root / "plan.json", root / "export", False, False)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result, events, _playback = self._run(
                    [
                        "--hf-export-dir", str(root / "export"),
                        "--output-dir", str(root / "records"),
                        "--dry-run",
                    ]
                )
            self.assertEqual(result, 0)
            events.open_hw.assert_not_called()
            warnings = [line for line in output.getvalue().splitlines() if "[AUTO][WARN]" in line]
            self.assertEqual(len(warnings), 1)
            self.assertIn(f"001_{long_name}_scale100", warnings[0])
            self.assertNotIn("000_ff_off", warnings[0])

    def test_capture_failure_reasons_are_written_to_the_session_json(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export(root)
            events = mock.Mock()
            session = SimpleNamespace(
                current_pos=(0.0, 0.0, 0.0),
                last_capture_report={
                    "failures": ["attempt 1: LED detection failed: PAT-start LED was not detected"],
                    "led_warnings": [],
                },
            )
            events.record.side_effect = lambda *_a, **_k: (2, None)
            with (
                mock.patch.object(auto_record, "open_recording_hardware_session", return_value=session),
                mock.patch.object(auto_record, "shutdown_recording_hardware_session"),
                mock.patch.object(auto_record, "run_recording", events.record),
            ):
                result = auto_record.main(
                    [
                        "--hf-export-dir", str(export_dir),
                        "--output-dir", str(root / "records"),
                        "--label", "ff_off",
                        "--acknowledge-ff-validation-protocol",
                    ]
                )
            self.assertEqual(result, 2)
            saved = json.loads(
                next((root / "records").glob("auto_recording_session_*.json")).read_text(encoding="utf-8")
            )
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(
                saved["runs"][0]["capture_report"]["failures"],
                ["attempt 1: LED detection failed: PAT-start LED was not detected"],
            )

    def test_failed_run_stops_the_session(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = self._export(root)
            result, events, _playback = self._run(
                [
                    "--hf-export-dir", str(export_dir),
                    "--output-dir", str(root / "records"),
                    "--acknowledge-ff-validation-protocol",
                    "--acknowledge-step-response-risk",
                ],
                record_results=[(0, None), (2, None)],
            )
            self.assertEqual(result, 2)
            self.assertEqual(events.record.call_count, 2)
            events.close_hw.assert_called_once()


if __name__ == "__main__":
    unittest.main()
