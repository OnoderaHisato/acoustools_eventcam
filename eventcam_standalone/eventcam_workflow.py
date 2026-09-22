#!/usr/bin/env python3
"""Portable camera-only calibration, synchronized recording and particle tracking.

Only the standard library is imported until an action is executed. --dry-run
does not import a camera SDK, open hardware, start subprocesses or write files.
All relative paths are anchored to this directory, independent of the shell cwd.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROCESS_KEYS = {
    "window_us", "hop_us", "dt_us", "max_interp_gap_sec", "max_step_px", "roi",
    "left_mask_roi", "right_mask_roi", "tracking_method", "threshold_count",
    "min_events", "min_area", "min_mass", "polarity", "max_time_gap_sec",
    "max_reprojection_error_px", "render_overlay",
}
CONFIG_KEYS = {
    "session_dir", "left_serial", "right_serial", "hw_sync", "square_mm",
    "sensor_width", "sensor_height", "mono_duration_sec", "stereo_pose_count",
    "stereo_duration_sec", "min_stereo_pairs", "exclude_pairs", "record_duration_sec",
    "record_delta_t_us", "start_delay_sec", "processing",
}
STAGES = (
    "board", "capture-left", "calibrate-left", "capture-right", "calibrate-right",
    "capture-stereo", "calibrate-stereo", "record", "process",
)


@dataclass
class Step:
    name: str
    command: list[str]
    required: tuple[Path, ...] = ()
    fresh: tuple[Path, ...] = ()


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def positive(value, name: str, *, zero: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value) or (value < 0 if zero else value <= 0):
        raise ValueError(f"Invalid {name}: {value}")


def validate_config(config: dict) -> Path:
    if set(config) != CONFIG_KEYS:
        raise ValueError(f"Config keys differ: {sorted(set(config) ^ CONFIG_KEYS)}")
    if not isinstance(config["processing"], dict) or set(config["processing"]) != PROCESS_KEYS:
        raise ValueError("processing keys must match workflow_config.json")
    for side in ("left", "right"):
        serial = config[f"{side}_serial"]
        if not isinstance(serial, str) or not serial.strip():
            raise ValueError("Camera serials must be nonempty strings (keep leading zeros)")
    if config["left_serial"] == config["right_serial"]:
        raise ValueError("Left/right serials must differ")
    if config["hw_sync"] != "left-master":
        raise ValueError("This workflow requires left-master hardware synchronization")
    positive(config["square_mm"], "square_mm")
    if not math.isclose(float(config["square_mm"]), 7.12, rel_tol=0, abs_tol=1e-9):
        raise ValueError("The supplied 10x7 checkerboard workflow requires 7.12 mm squares")
    session_text = config["session_dir"]
    if not isinstance(session_text, str) or not session_text.strip() or Path(session_text).is_absolute():
        raise ValueError("session_dir must be a relative path inside this toolkit")
    session = (ROOT / session_text).resolve()
    if not session.is_relative_to(ROOT) or session == ROOT:
        raise ValueError("session_dir must stay inside this toolkit")
    for name in ("sensor_width", "sensor_height", "stereo_pose_count", "min_stereo_pairs", "record_delta_t_us"):
        positive(config[name], name)
        if not isinstance(config[name], int):
            raise ValueError(f"{name} must be an integer")
    if config["min_stereo_pairs"] < 10 or config["stereo_pose_count"] < config["min_stereo_pairs"]:
        raise ValueError("Require stereo_pose_count >= min_stereo_pairs >= 10")
    for name in ("mono_duration_sec", "stereo_duration_sec"):
        positive(config[name], name)
    # 0 means the operator ends the saved interval with a second Enter.
    positive(config["record_duration_sec"], "record_duration_sec", zero=True)
    positive(config["start_delay_sec"], "start_delay_sec", zero=True)
    if not isinstance(config["exclude_pairs"], str):
        raise ValueError("exclude_pairs must be a comma-separated string")
    p = config["processing"]
    for name in ("window_us", "hop_us", "dt_us", "threshold_count", "min_events", "min_area"):
        positive(p[name], name)
    for name in ("window_us", "hop_us", "threshold_count", "min_events", "min_area", "min_mass"):
        positive(p[name], name, zero=(name == "min_mass"))
        if not isinstance(p[name], int):
            raise ValueError(f"{name} must be an integer")
    for name in ("max_interp_gap_sec", "max_step_px", "max_time_gap_sec", "max_reprojection_error_px"):
        positive(p[name], name, zero=True)
    if not isinstance(p["render_overlay"], bool):
        raise ValueError("render_overlay must be a JSON boolean")
    if p["tracking_method"] not in {"event_weighted", "component_center", "hough_circle", "distance_transform_center", "min_enclosing_circle", "fit_ellipse", "ransac_circle"}:
        raise ValueError("Unknown tracking_method")
    if p["polarity"] not in {"all", "on", "off"}:
        raise ValueError("Unknown polarity")
    for name in ("roi", "left_mask_roi", "right_mask_roi"):
        text = p[name]
        if not isinstance(text, str):
            raise ValueError(f"{name} must be a string")
        if not text and name != "roi":
            continue
        values = [int(v.strip()) for v in text.split(",")]
        if len(values) != 4:
            raise ValueError(f"{name} must contain x0,y0,x1,y1")
        x0, y0, x1, y1 = values
        if not (0 <= x0 < x1 <= config["sensor_width"] and 0 <= y0 < y1 <= config["sensor_height"]):
            raise ValueError(f"{name} lies outside the sensor")
    return session


def command(script: str, *args) -> list[str]:
    path = ROOT / script
    if not path.is_file():
        raise ValueError(f"Missing bundled script: {path}")
    return [sys.executable, str(path), *(str(v) for v in args)]


def build_plan(config: dict, action: str, run_name: str | None, resume: bool = False) -> tuple[list[Step], Path]:
    session = validate_config(config)
    if action == "process" and not run_name:
        raise ValueError("process requires --run NAME (a directory under session/recordings)")
    name = run_name or ("run_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", name):
        raise ValueError("--run must be a short directory name using letters, numbers, _ or -")
    run = session / "recordings" / name
    board = ROOT / "checkerboard_10x7_normal.png"
    cal = session / "calibration"
    stereo_cal = cal / "stereo_calibration.npz"
    serials = ["--left-serial", config["left_serial"], "--right-serial", config["right_serial"]]
    square = ["--square-mm", "7.12", "--require-square-mm", "7.12"]
    steps = {"board": Step("board", [])}
    for side in ("left", "right"):
        captures = session / f"mono_{side}"
        output = cal / f"{side}_intrinsics.npz"
        steps[f"capture-{side}"] = Step(f"capture-{side}", command(
            "eventcam_checkerboard_calibration_capture.py", "--serial", config[f"{side}_serial"],
            "--output-dir", captures, "--checkerboard-image", board, *square,
            "--duration-sec", config["mono_duration_sec"], "--frame-window-us", 20000,
            "--frame-window-us-list", "10000,20000,30000,40000", "--render-mode", "all", "--candidate-count", 4,
        ), (board,), (captures,))
        steps[f"calibrate-{side}"] = Step(f"calibrate-{side}", command(
            "single_camera_calibrate.py", "--images", captures / "calib_images", "--camera-serial", config[f"{side}_serial"],
            *square, "--output", output, "--debug-dir", cal / f"{side}_debug",
        ), (captures / "calib_images",), (output, output.with_suffix(".csv"), cal / f"{side}_debug"))
    captures = session / "stereo_poses"
    steps["capture-stereo"] = Step("capture-stereo", command(
        "stereo_checkerboard_sync_capture.py", *serials, "--output-dir", captures,
        "--checkerboard-image", board, "--square-mm", 7.12, "--pose-count", config["stereo_pose_count"],
        "--hw-sync", "left-master", "--duration-sec", config["stereo_duration_sec"],
        "--frame-window-us-list", "10000,20000,30000,40000",
        "--sensor-width", config["sensor_width"], "--sensor-height", config["sensor_height"],
    ), (board,), (captures,))
    left_k, right_k = cal / "left_intrinsics.npz", cal / "right_intrinsics.npz"
    left_images, right_images = captures / "left" / "calib_images", captures / "right" / "calib_images"
    steps["calibrate-stereo"] = Step("calibrate-stereo", command(
        "stereo_camera_calibrate.py", "--left-images", left_images, "--right-images", right_images,
        "--left-intrinsics", left_k, "--right-intrinsics", right_k, *serials, *square,
        "--min-pairs", config["min_stereo_pairs"], "--exclude-pairs", config["exclude_pairs"],
        "--output", stereo_cal, "--debug-dir", cal / "stereo_debug",
    ), (left_k, right_k, left_images, right_images), (stereo_cal, stereo_cal.with_suffix(".csv"), cal / "stereo_debug"))
    steps["record"] = Step("record", command(
        "stereo_eventcam_record_sync.py", *serials, "--run-dir", run, "--hw-sync", "left-master",
        "--start-trigger", "enter",
        "--duration-sec", config["record_duration_sec"], "--delta-t-us", config["record_delta_t_us"],
        "--start-delay-sec", config["start_delay_sec"], "--npz-compression", "none", "--max-events", 0,
        "--stereo-calibration", stereo_cal, "--sensor-width", config["sensor_width"], "--sensor-height", config["sensor_height"],
        "--note", "Camera-only standalone workflow; no external actuator control",
    ), (stereo_cal,), (run,))
    process_args = []
    for key, value in config["processing"].items():
        if isinstance(value, bool):
            if value:
                process_args.append("--" + key.replace("_", "-"))
        else:
            process_args.extend(["--" + key.replace("_", "-"), value])
    if resume:
        process_args.append("--resume")
    steps["process"] = Step("process", command(
        "stereo_process_recording.py", run, "--stereo-calibration", run / "calibration" / "stereo_calibration.npz", *process_args,
    ), (run / "stereo_recording_manifest.json", run / "recording_config.json",
        run / "left" / "left_events.npz", run / "right" / "right_events.npz", run / "calibration" / "stereo_calibration.npz"))
    sequence = STAGES if action == "all" else (("record", "process") if action == "record-process" else (action,))
    return [steps[n] for n in sequence], run


def require_fresh(paths: tuple[Path, ...]) -> None:
    for path in paths:
        if path.exists():
            raise ValueError(f"Refusing to overwrite/reuse existing output: {path}. Choose a new session/run.")


def generate_board() -> None:
    import cv2
    import numpy as np
    pixels, margin = 80, 80
    board = np.full((7 * pixels + 2 * margin, 10 * pixels + 2 * margin), 255, np.uint8)
    for y in range(7):
        for x in range(10):
            board[margin+y*pixels:margin+(y+1)*pixels, margin+x*pixels:margin+(x+1)*pixels] = 0 if (x+y) % 2 == 0 else 255
    path = ROOT / "checkerboard_10x7_normal.png"
    if path.exists():
        if not np.array_equal(cv2.imread(str(path), cv2.IMREAD_GRAYSCALE), board):
            raise ValueError(f"An unrelated checkerboard already exists: {path}")
    elif not cv2.imwrite(str(path), board):
        raise RuntimeError(f"Could not write {path}")
    print(f"Board: {path}\nDisplay without blinking; measure each displayed square as 7.12 mm. PNG pixels alone do not set physical size.", flush=True)


def validate_calibration(path: Path, config: dict) -> dict:
    import numpy as np
    with np.load(path, allow_pickle=False) as data:
        for side in ("left", "right"):
            if str(data[f"{side}_camera_serial"].item()) != config[f"{side}_serial"]:
                raise ValueError(f"{side} camera serial differs from calibration")
            matrix = np.asarray(data[f"{side}_camera_matrix"])
            if matrix.shape != (3, 3) or not np.isfinite(matrix).all() or matrix[0, 0] <= 0 or matrix[1, 1] <= 0:
                raise ValueError(f"Invalid {side} camera matrix")
            if not np.isfinite(data[f"{side}_dist_coeffs"]).all():
                raise ValueError(f"Invalid {side} distortion")
        if not np.array_equal(data["image_size"].reshape(-1), [config["sensor_width"], config["sensor_height"]]):
            raise ValueError("Calibration image size differs from configured sensor size")
        if not math.isclose(float(data["square_size_mm"]), 7.12, rel_tol=0, abs_tol=1e-9):
            raise ValueError("Calibration square size is not 7.12 mm")
        rotation, translation = data["R"], data["T"]
        if rotation.shape != (3, 3) or translation.size != 3 or not np.isfinite(translation).all() or not np.isfinite(rotation).all():
            raise ValueError("Invalid stereo R/T")
        baseline = float(np.linalg.norm(translation))
        if baseline <= 0 or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-5) or not np.isclose(np.linalg.det(rotation), 1, atol=1e-5):
            raise ValueError("Invalid stereo rotation or zero baseline")
        rms = float(data["stereo_rms"]) if "stereo_rms" in data else None
    return {"sha256": sha256(path), "baseline_mm": baseline, "stereo_rms_px": rms}


def validate_recording(run: Path, config: dict) -> None:
    manifest = read_json(run / "stereo_recording_manifest.json")
    sync = manifest.get("hw_sync", {})
    if sync.get("mode") != "left-master" or sync.get("verified") is not True:
        raise ValueError("Recording does not have verified left-master synchronization")
    for side, role in (("left", "master"), ("right", "slave")):
        if manifest.get(f"{side}_serial") != config[f"{side}_serial"]:
            raise ValueError(f"Recorded {side} serial differs from config")
        result = manifest.get(side, {})
        meta = result.get("meta", {})
        if result.get("ok") is not True or not meta.get("capture_interval_complete") or meta.get("event_limit_reached") or meta.get("sync_role") != role:
            raise ValueError(f"Incomplete/invalid {side} recording")
        if not (run / side / f"{side}_events.npz").is_file():
            raise ValueError(f"Missing {side} events")


def run_processing(step: Step, run: Path, config: dict, resume: bool) -> None:
    validate_recording(run, config)
    captured = read_json(run / "recording_config.json")
    info = validate_calibration(run / "calibration" / "stereo_calibration.npz", config)
    if info["sha256"] != captured["calibration_sha256"]:
        raise ValueError("Recording calibration snapshot has changed")
    signature = {"processing": config["processing"], "calibration_sha256": info["sha256"]}
    checkpoint = run / "standalone_processing.json"
    outputs_exist = any((run / name).exists() for name in ("left/event_tracking", "right/event_tracking", "stereo_3d"))
    if checkpoint.exists() or outputs_exist:
        if not resume or not checkpoint.exists():
            raise ValueError("Processing output already exists; use --resume for the same settings, or a separate copied run for changed settings")
        saved = read_json(checkpoint)
        if saved.get("signature") != signature:
            raise ValueError("Resume settings/calibration differ; preserve this run and use a separate analysis copy")
    write_json(checkpoint, {"signature": signature, "status": "running"})
    try:
        subprocess.run(step.command, check=True, cwd=ROOT)
    except BaseException:
        write_json(checkpoint, {"signature": signature, "status": "interrupted_or_failed"})
        raise
    write_json(checkpoint, {"signature": signature, "status": "complete"})


def execute(plan: list[Step], run: Path, config: dict, *, resume: bool, all_steps: bool) -> None:
    # Detect output collisions for the entire selected sequence before any camera opens.
    for step in plan:
        require_fresh(step.fresh)
    for step in plan:
        print(f"\n[{step.name}]", flush=True)
        if step.name == "board":
            generate_board()
            continue
        for path in step.required:
            if not path.exists():
                raise ValueError(f"Missing input: {path}")
        if step.name == "process":
            run_processing(step, run, config, resume)
            continue
        if step.name == "record":
            calibration = step.required[0]
            info = validate_calibration(calibration, config)
            print(f"Calibration: {info}", flush=True)
            if all_steps:
                answer = input("Inspect calibration reports, replace checkerboard with the measurement target, then press Enter (Q=stop): ")
                if answer.strip().lower() == "q":
                    print("Stopped before recording. Resume with record or record-process when ready.")
                    return
        for path in step.fresh:
            path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(step.command, check=True, cwd=ROOT)
        if step.name == "record":
            validate_recording(run, config)
            destination = run / "calibration" / "stereo_calibration.npz"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(calibration, destination)
            write_json(run / "recording_config.json", {
                "config": config, "calibration": "calibration/stereo_calibration.npz",
                "calibration_sha256": info["sha256"], "coordinate_frame": "left_camera_mm",
                "external_actuator_control": False,
            })
        if step.name == "calibrate-stereo":
            print(f"Calibration validation: {validate_calibration(step.fresh[0], config)}", flush=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=(*STAGES, "record-process", "all", "doctor", "devices"))
    parser.add_argument("--config", type=Path, default=ROOT / "workflow_config.json")
    parser.add_argument("--run", help="Run directory name inside SESSION/recordings; mandatory for process")
    parser.add_argument("--dry-run", action="store_true", help="Print commands only; no SDK import, hardware, subprocess or file writes")
    parser.add_argument("--resume", action="store_true", help="Resume process with matching settings")
    parser.add_argument("--sdk", action="store_true", help="doctor only: import SDK, without enumerating/opening cameras")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        if args.resume and args.action != "process":
            raise ValueError("--resume is only available with process")
        if args.sdk and args.action != "doctor":
            raise ValueError("--sdk is only available with doctor")
        if args.action in {"doctor", "devices"}:
            cmd = command("check_environment.py", *(["--sdk"] if args.sdk else [])) if args.action == "doctor" else command("stereo_eventcam_record_sync.py", "--list-devices")
            print(subprocess.list2cmdline(cmd), flush=True)
            if not args.dry_run:
                subprocess.run(cmd, check=True, cwd=ROOT)
            return 0
        config = read_json(args.config)
        plan, run = build_plan(config, args.action, args.run, args.resume)
        for step in plan:
            print(f"{step.name}: {subprocess.list2cmdline(step.command) if step.command else 'generate/check local checkerboard PNG'}", flush=True)
        if args.dry_run:
            print("DRY RUN: no files written, subprocesses started, or cameras opened. Future input files were not required.")
            return 0
        execute(plan, run, config, resume=args.resume, all_steps=args.action == "all")
        return 0
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted; preserved existing files.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
