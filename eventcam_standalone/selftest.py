"""Targeted hardware-free checks for the portable camera-only bundle."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

import cv2
import numpy as np

if __package__:
    from . import eventcam_workflow as workflow
else:
    import eventcam_workflow as workflow

BUNDLE = Path(__file__).resolve().parent


def config():
    return workflow.read_json(BUNDLE / "workflow_config.json")


def make_fixture(root: Path, cfg: dict) -> Path:
    run = root / cfg["session_dir"] / "recordings" / "synthetic"
    calibration = run / "calibration" / "stereo_calibration.npz"
    calibration.parent.mkdir(parents=True)
    k = np.array([[1000., 0., 640.], [0., 1000., 360.], [0., 0., 1.]])
    np.savez(calibration, left_camera_matrix=k, right_camera_matrix=k,
             left_dist_coeffs=np.zeros(5), right_dist_coeffs=np.zeros(5),
             R=np.eye(3), T=np.array([[-120.], [0.], [0.]]), image_size=[1280, 720],
             square_size_mm=7.12, stereo_rms=0., left_camera_serial=cfg["left_serial"],
             right_camera_serial=cfg["right_serial"])
    manifest = {"left_serial": cfg["left_serial"], "right_serial": cfg["right_serial"],
                "hw_sync": {"mode": "left-master", "verified": True}}
    dtype = np.dtype([("x", "<u2"), ("y", "<u2"), ("p", "u1"), ("t", "<i8")])
    for side, role, cx in (("left", "master", 640), ("right", "slave", 440)):
        side_dir = run / side
        side_dir.mkdir()
        rows = [(cx + t // 2000 + dx, 360 + dy, 1, 1_000_000 + t)
                for t in range(0, 10000, 50) for _ in range(4)
                for dx in range(-1, 2) for dy in range(-1, 2)]
        np.savez(side_dir / f"{side}_events.npz", events=np.array(rows, dtype=dtype),
                 output_start_ts_us=1_000_000, output_end_ts_us=1_010_000, sync_ts_us=1_000_000,
                 hardware_synchronized=True, sync_mode_verified=True, sync_role=role,
                 sync_mode_applied=role, timestamp_domain_id="hardware-master:00000508",
                 capture_interval_complete=True, event_limit_reached=False)
        manifest[side] = {"ok": True, "meta": {"capture_interval_complete": True,
                          "event_limit_reached": False, "sync_role": role}}
    workflow.write_json(run / "stereo_recording_manifest.json", manifest)
    workflow.write_json(run / "recording_config.json", {"calibration_sha256": workflow.sha256(calibration)})
    return run


def isolated_copy(temporary: str):
    base = Path(temporary)
    bundle = base / "portable"
    bundle.mkdir()
    for source in BUNDLE.iterdir():
        if source.is_file() and source.suffix in {".py", ".json", ".txt", ".md", ".ps1"}:
            shutil.copy2(source, bundle / source.name)
    blocker = base / "import_guard"
    blocker.mkdir()
    (blocker / "sitecustomize.py").write_text(
        "import sys\n"
        "class Guard:\n"
        " def find_spec(self, fullname, path=None, target=None):\n"
        "  if fullname.split('.')[0] in {'acoustools','torch','metavision_core','metavision_hal','metavision_sdk_core','metavision_sdk_ui'}:\n"
        "   raise RuntimeError('Forbidden hardware/PAT dependency: '+fullname)\n"
        "sys.meta_path.insert(0, Guard())\n", encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(blocker), PYTHONDONTWRITEBYTECODE="1", MPLBACKEND="Agg")
    return bundle, env


def make_mono_fixture(bundle: Path, name: str) -> tuple[Path, np.ndarray]:
    """Write a synthetic single-camera NPZ with an exactly known centre trajectory."""
    run = bundle / "mono_records" / name
    run.mkdir(parents=True)
    dtype = np.dtype([("x", "<u2"), ("y", "<u2"), ("p", "u1"), ("t", "<i8")])
    start_us = 1_000_000
    span_us = 100_000
    step_us = 100
    times_us = np.arange(0, span_us, step_us)
    centres = np.stack(
        [300.0 + 60.0 * np.sin(2 * np.pi * 3.0 * times_us / 1e6),
         200.0 + 40.0 * np.sin(2 * np.pi * 2.0 * times_us / 1e6)],
        axis=1,
    )
    disc = [(dx, dy) for dx in range(-3, 4) for dy in range(-3, 4) if dx * dx + dy * dy <= 9]
    rows = [(int(cx) + dx, int(cy) + dy, 1, start_us + int(t))
            for t, (cx, cy) in zip(times_us, centres)
            for dx, dy in disc]
    np.savez_compressed(
        run / "mono_20260101_000000_events.npz",
        events=np.array(rows, dtype=dtype), sync_ts_us=-1,
        output_start_ts_us=start_us, output_end_ts_us=start_us + span_us,
        mask_rois=np.empty((0, 4), dtype=np.int32),
    )
    return run, centres


class StandaloneTests(unittest.TestCase):
    def test_config_rejects_bad_geometry_sync_and_paths(self):
        for key, value in (("hw_sync", "off"), ("square_mm", 7.1), ("session_dir", "../escape"),
                           ("left_serial", "00000509"), ("record_duration_sec", float("nan")),
                           ("record_delta_t_us", 1000.0)):
            cfg = config()
            cfg[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                workflow.validate_config(cfg)

    def test_dry_run_writes_nothing_and_calls_no_subprocess(self):
        with TemporaryDirectory() as tmp, mock.patch.object(workflow, "ROOT", Path(tmp)), mock.patch.object(workflow.subprocess, "run") as run:
            # Command existence checks use only a stub filename; no code is run.
            for source in BUNDLE.glob("*.py"):
                (Path(tmp) / source.name).write_text("", encoding="utf-8")
            cfg_path = Path(tmp) / "config.json"
            workflow.write_json(cfg_path, config())
            before = set(Path(tmp).iterdir())
            self.assertEqual(workflow.main(["all", "--config", str(cfg_path), "--dry-run"]), 0)
            run.assert_not_called()
            self.assertEqual(before, set(Path(tmp).iterdir()))

    def test_plan_uses_only_bundled_scripts_and_child_parser_options(self):
        plan, _ = workflow.build_plan(config(), "all", "check")
        self.assertEqual([s.name for s in plan], list(workflow.STAGES))
        for step in plan:
            if not step.command:
                continue
            source = Path(step.command[1])
            self.assertEqual(source.parent, BUNDLE)
            declared = {arg.value for node in ast.walk(ast.parse(source.read_text(encoding="utf-8-sig")))
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"
                        for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("--")}
            supplied = {arg for arg in step.command[2:] if arg.startswith("--")}
            self.assertFalse(supplied - declared, (step.name, supplied - declared))
        process = plan[-1].command
        self.assertNotIn("--left-mask-roi 600,0,1280,180", " ".join(process))
        self.assertIn("--hw-sync", plan[-2].command)

    def test_no_import_depends_on_parent_project(self):
        local = {p.stem for p in BUNDLE.glob("*.py")}
        permitted = set(sys.stdlib_module_names) | local | {"__future__", "numpy", "cv2", "matplotlib",
                     "metavision_core", "metavision_hal", "metavision_sdk_core", "metavision_sdk_ui"}
        for path in BUNDLE.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                for name in names:
                    self.assertIn(name.split(".")[0], permitted, (path, name))

    def test_output_collision_prevents_any_subprocess(self):
        with TemporaryDirectory() as tmp, mock.patch.object(workflow.subprocess, "run") as run:
            exists = Path(tmp) / "existing.npz"
            exists.write_bytes(b"preserve")
            with self.assertRaisesRegex(ValueError, "overwrite"):
                workflow.execute([workflow.Step("capture-left", ["unused"], fresh=(exists,))], Path(tmp), config(), resume=False, all_steps=False)
            run.assert_not_called()
            self.assertEqual(exists.read_bytes(), b"preserve")

    def test_generated_board_has_54_corners_and_no_overwrite(self):
        with TemporaryDirectory() as tmp, mock.patch.object(workflow, "ROOT", Path(tmp)):
            workflow.generate_board()
            path = Path(tmp) / "checkerboard_10x7_normal.png"
            image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            found, corners = cv2.findChessboardCornersSB(image, (9, 6))
            self.assertTrue(found)
            self.assertEqual(len(corners), 54)
            path.write_bytes(b"different")
            with self.assertRaises(ValueError):
                workflow.generate_board()

    def test_calibration_and_recording_identity_checks(self):
        with TemporaryDirectory() as tmp:
            cfg = config()
            run = make_fixture(Path(tmp), cfg)
            path = run / "calibration" / "stereo_calibration.npz"
            self.assertAlmostEqual(workflow.validate_calibration(path, cfg)["baseline_mm"], 120.)
            cfg["left_serial"] = "wrong"
            with self.assertRaises(ValueError):
                workflow.validate_calibration(path, cfg)
            with self.assertRaises(ValueError):
                workflow.validate_recording(run, cfg)

    def test_all_help_commands_work_in_isolated_copy_without_sdk_or_pat(self):
        with TemporaryDirectory() as tmp:
            bundle, env = isolated_copy(tmp)
            for source in bundle.glob("*.py"):
                if source.name == "eventcam_npz_storage.py":
                    continue  # Library only; no CLI.
                result = subprocess.run([sys.executable, "-B", str(source), "--help"], env=env,
                                        cwd=tmp, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, (source.name, result.stderr))
            result = subprocess.run([sys.executable, "-B", str(bundle / "eventcam_workflow.py"), "all", "--dry-run"],
                                    env=env, cwd=tmp, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((bundle / "data").exists())

    def test_isolated_synthetic_events_to_3d_and_resume_guards(self):
        with TemporaryDirectory() as tmp:
            bundle, env = isolated_copy(tmp)
            cfg = config()
            run = make_fixture(bundle, cfg)
            command = [sys.executable, "-B", str(bundle / "eventcam_workflow.py"), "process", "--run", "synthetic"]
            result = subprocess.run(command, env=env, cwd=tmp, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with np.load(run / "stereo_3d" / "stereo_3d_points.npz", allow_pickle=False) as data:
                points = data["points_left_cam_mm"][data["valid"]]
                self.assertGreater(points.shape[0], 80)
                np.testing.assert_allclose(points[:, 2], 600., atol=1e-6)
                np.testing.assert_allclose(points[:, 1], 0., atol=1e-6)
            self.assertTrue((run / "stereo_3d" / "stereo_3d_trajectory.png").is_file())
            result = subprocess.run(command, env=env, cwd=tmp, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("already exists", result.stderr)
            result = subprocess.run(command + ["--resume"], env=env, cwd=tmp, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("skipping extraction", result.stdout)
            cfg["processing"]["window_us"] = 300
            workflow.write_json(bundle / "workflow_config.json", cfg)
            result = subprocess.run(command + ["--resume"], env=env, cwd=tmp, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Resume settings", result.stderr)


    def test_mono_sample_extracts_known_trajectory_and_renders_video(self):
        with TemporaryDirectory() as tmp:
            bundle, env = isolated_copy(tmp)
            run, centres = make_mono_fixture(bundle, "synthetic")
            entry = str(bundle / "mono_sample.py")

            for action in ("track", "video"):
                result = subprocess.run(
                    [sys.executable, "-B", entry, action, "--run", "synthetic",
                     "--video-duration-sec", "0.1"],
                    env=env, cwd=tmp, capture_output=True, text=True, timeout=120,
                )
                self.assertEqual(result.returncode, 0, (action, result.stdout + result.stderr))

            track = np.genfromtxt(run / "event_tracking" / "event_centres_interp.csv",
                                  delimiter=",", names=True, encoding="utf-8")
            finite = np.isfinite(track["x_px"]) & np.isfinite(track["y_px"])
            self.assertGreater(int(finite.sum()), 900)
            expected_x = 300.0 + 60.0 * np.sin(2 * np.pi * 3.0 * track["t_sec"])
            expected_y = 200.0 + 40.0 * np.sin(2 * np.pi * 2.0 * track["t_sec"])
            # The synthetic disc sits on integer pixels, so 1 px is the exact bound.
            self.assertLessEqual(float(np.max(np.abs(track["x_px"][finite] - expected_x[finite]))), 1.0)
            self.assertLessEqual(float(np.max(np.abs(track["y_px"][finite] - expected_y[finite]))), 1.0)

            summary = json.loads((run / "event_tracking" / "event_tracking_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["missing_raw_points"], 0)
            self.assertEqual(summary["tracking_rejected_points"], 0)

            video = run / "mono_overlay.mp4"
            self.assertTrue(video.is_file())
            self.assertGreater(video.stat().st_size, 1024)

            # A second run must not overwrite either output.
            for action in ("track", "video"):
                result = subprocess.run(
                    [sys.executable, "-B", entry, action, "--run", "synthetic"],
                    env=env, cwd=tmp, capture_output=True, text=True, timeout=60,
                )
                self.assertNotEqual(result.returncode, 0, action)
                self.assertIn("Refusing to overwrite", result.stderr)

    def test_shared_interval_math_is_aligned_and_leads_the_slave(self):
        import stereo_eventcam_record_sync as recorder

        for delta_t_us in (100, 1_000, 10_000):
            for now_us in (0, 1, delta_t_us - 1, delta_t_us, 123_456_789):
                for lead_us in (0, 1, 500, 10_000, 2_000_000):
                    start = recorder.compute_common_interval_start(now_us, lead_us, delta_t_us)
                    self.assertEqual(start % delta_t_us, 0)
                    # Never less than ten slices, so the Slave always reads the
                    # shared value before the interval opens.
                    self.assertGreaterEqual(start - now_us, 10 * delta_t_us)
                    self.assertGreaterEqual(start - now_us, lead_us)
                    end = recorder.compute_common_interval_end(now_us, start, delta_t_us)
                    self.assertEqual(end % delta_t_us, 0)
                    self.assertGreater(end, start)
        # An immediate second Enter must still save at least one slice.
        start = recorder.compute_common_interval_start(1_000_000, 0, 1_000)
        self.assertGreaterEqual(recorder.compute_common_interval_end(1_000_000, start, 1_000) - start, 1_000)

    def test_manual_stop_requires_enter_trigger_and_hardware_sync(self):
        script = str(BUNDLE / "stereo_eventcam_record_sync.py")
        cases = (
            (["--duration-sec", "0"], "--start-trigger enter"),
            (["--duration-sec", "-1"], "zero or positive"),
            (["--duration-sec", "0", "--start-trigger", "enter", "--hw-sync", "off"], "hardware synchronization"),
        )
        for extra, expected in cases:
            with self.subTest(extra=extra):
                result = subprocess.run([sys.executable, "-B", script, *extra],
                                        cwd=BUNDLE, capture_output=True, text=True, timeout=60)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)

    def test_record_step_asks_the_operator_and_allows_manual_stop(self):
        plan, _ = workflow.build_plan(config(), "record", "check")
        record = plan[0].command
        self.assertIn("--start-trigger", record)
        self.assertEqual(record[record.index("--start-trigger") + 1], "enter")
        cfg = config()
        cfg["record_duration_sec"] = 0
        workflow.validate_config(cfg)  # 0 is the manual-stop setting, not an error.
        for bad in (-1.0, float("nan")):
            cfg["record_duration_sec"] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                workflow.validate_config(cfg)

    def test_mono_entry_points_never_reach_stereo_or_calibration(self):
        plain = subprocess.run(
            [sys.executable, "-B", str(BUNDLE / "mono_sample.py"), "all",
             "--run", "check", "--serial", "00000508", "--dry-run"],
            cwd=BUNDLE, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(plain.returncode, 0, plain.stderr)
        for forbidden in ("stereo", "calibration", "hw-sync", "left-serial", "right-serial"):
            self.assertNotIn(forbidden, plain.stdout, forbidden)
        for script in ("mono_eventcam_record.py", "eventcam_npz_track.py", "eventcam_npz_render_video.py"):
            self.assertIn(script, plain.stdout)


if __name__ == "__main__":
    unittest.main()
