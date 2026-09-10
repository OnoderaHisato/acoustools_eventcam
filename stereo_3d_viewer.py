#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Serve an interactive Three.js viewer for stereo/PAT 3D NPZ or CSV data."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import mimetypes
import socket
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import numpy as np


SCRIPT_DIR = Path(__file__).resolve().parent
STATIC_DIR = SCRIPT_DIR / "stereo_3d_viewer"
VENDOR_ASSETS = {
    "three.module.js",
    "OrbitControls.js",
    "OBJLoader.js",
    "MTLLoader.js",
    "lucide.min.js",
    "LICENSE.three.txt",
    "LICENSE.lucide.txt",
}
CAD_MODEL_ENDPOINT = "/cad/model.obj"
CAD_MATERIAL_ENDPOINT = "/cad/model.mtl"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open a drag/zoom interactive 3D viewer for stereo reconstruction or PAT registration data.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input", type=Path, required=True, help="3D NPZ or correspondence CSV.")
    parser.add_argument(
        "--stereo-calibration",
        type=Path,
        default=None,
        help="Optional stereo calibration NPZ used to show camera-centre geometry.",
    )
    parser.add_argument(
        "--cad-obj",
        type=Path,
        default=None,
        help="Optional OBJ rig model to overlay on absolute PAT-coordinate data.",
    )
    parser.add_argument(
        "--cad-config",
        type=Path,
        default=None,
        help="Optional JSON defining OBJ units and the CAD-to-PAT rigid placement.",
    )
    parser.add_argument("--port", type=int, default=8765, help="Preferred local port; the next free port is used if occupied.")
    parser.add_argument("--bind", default="127.0.0.1", help="HTTP bind address.")
    parser.add_argument("--max-points", type=int, default=100000, help="Maximum points per displayed series.")
    parser.add_argument("--no-open", action="store_true", help="Do not open the default browser automatically.")
    return parser.parse_args()


def downsample(points: np.ndarray, time_values: np.ndarray, max_points: int) -> tuple[np.ndarray, np.ndarray]:
    if max_points <= 0 or points.shape[0] <= max_points:
        return points, time_values
    indices = np.linspace(0, points.shape[0] - 1, max_points, dtype=np.int64)
    return points[indices], time_values[indices]


def add_series(
    series: list[dict[str, Any]],
    *,
    series_id: str,
    label: str,
    frame: str,
    points: np.ndarray,
    time_values: np.ndarray | None,
    max_points: int,
    kind: str = "trajectory",
) -> None:
    array = np.asarray(points, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != 3:
        return
    if time_values is None or np.asarray(time_values).shape[0] != array.shape[0]:
        times = np.arange(array.shape[0], dtype=np.float64)
    else:
        times = np.asarray(time_values, dtype=np.float64)
    finite = np.all(np.isfinite(array), axis=1) & np.isfinite(times)
    array = array[finite]
    times = times[finite]
    if array.size == 0:
        return
    array, times = downsample(array, times, max_points)
    series.append(
        {
            "id": series_id,
            "label": label,
            "frame": frame,
            "kind": kind,
            "points": array.tolist(),
            "time": times.tolist(),
        }
    )


def npz_scalar_text(data: Any, key: str) -> str:
    if key not in data.files:
        return ""
    value = np.asarray(data[key])
    if value.size != 1:
        return ""
    item = value.reshape(-1)[0]
    if isinstance(item, bytes):
        return item.decode("utf-8", errors="replace").strip()
    return str(item).strip()


def load_camera_geometry(calibration_path: Path | None) -> dict[str, Any] | None:
    if calibration_path is None or not calibration_path.is_file():
        return None
    try:
        calibration = np.load(calibration_path, allow_pickle=False)
        rotation = np.asarray(calibration["R"], dtype=np.float64).reshape(3, 3)
        translation = np.asarray(calibration["T"], dtype=np.float64).reshape(3)
    except (OSError, KeyError, ValueError):
        return None
    right_centre = -rotation.T @ translation
    return {
        "source": str(calibration_path),
        "left_centre_mm": [0.0, 0.0, 0.0],
        "right_centre_left_cam_mm": right_centre.tolist(),
        "baseline_mm": float(np.linalg.norm(translation)),
        "right_from_left_rotation": rotation.tolist(),
        "units": "mm",
    }


def resolve_calibration_path(raw_path: str, source_path: Path) -> Path | None:
    if not raw_path:
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = source_path.parent / candidate
    return candidate.resolve()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def finite_float(value: Any, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise SystemExit(f"CAD config field {field_name!r} must be numeric.") from error
    if not np.isfinite(number):
        raise SystemExit(f"CAD config field {field_name!r} must be finite.")
    return number


def cad_vector(value: Any, size: int, field_name: str) -> list[float]:
    if not isinstance(value, list) or len(value) != size:
        raise SystemExit(f"CAD config field {field_name!r} must be a list of {size} numbers.")
    return [finite_float(item, field_name) for item in value]


def unit_vector(vector: np.ndarray, field_name: str) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(norm) or norm <= 1e-9:
        raise SystemExit(f"CAD alignment vector {field_name!r} has zero length.")
    return vector / norm


def rigid_basis(baseline: np.ndarray, forward: np.ndarray, field_name: str) -> np.ndarray:
    baseline_unit = unit_vector(baseline, f"{field_name} baseline")
    forward_orthogonal = forward - baseline_unit * float(np.dot(forward, baseline_unit))
    forward_unit = unit_vector(forward_orthogonal, f"{field_name} forward")
    vertical_unit = unit_vector(np.cross(forward_unit, baseline_unit), f"{field_name} vertical")
    return np.column_stack((baseline_unit, vertical_unit, forward_unit))


def camera_alignment_transform(
    raw_config: dict[str, Any],
    mm_per_unit: float,
    camera_geometry: dict[str, Any] | None,
) -> tuple[list[float], list[float], dict[str, Any], float]:
    if camera_geometry is None:
        raise SystemExit(
            "CAD frame 'camera' requires embedded stereo calibration or --stereo-calibration."
        )
    left_lens_obj = np.asarray(
        cad_vector(raw_config.get("left_lens_center_obj"), 3, "left_lens_center_obj"),
        dtype=np.float64,
    )
    right_lens_obj = np.asarray(
        cad_vector(raw_config.get("right_lens_center_obj"), 3, "right_lens_center_obj"),
        dtype=np.float64,
    )
    focus_obj = np.asarray(
        cad_vector(raw_config.get("focus_obj"), 3, "focus_obj"),
        dtype=np.float64,
    )
    try:
        right_centre_left_cam = np.asarray(
            camera_geometry["right_centre_left_cam_mm"],
            dtype=np.float64,
        ).reshape(3)
        right_from_left = np.asarray(
            camera_geometry["right_from_left_rotation"],
            dtype=np.float64,
        ).reshape(3, 3)
    except (KeyError, TypeError, ValueError) as error:
        raise SystemExit("Stereo calibration does not contain usable camera geometry.") from error
    nominal_left_mm = left_lens_obj * mm_per_unit
    nominal_right_mm = right_lens_obj * mm_per_unit
    nominal_baseline_mm = float(np.linalg.norm(nominal_right_mm - nominal_left_mm))
    stereo_baseline_mm = float(np.linalg.norm(right_centre_left_cam))
    fit_baseline_scale = raw_config.get("fit_baseline_scale", True)
    if not isinstance(fit_baseline_scale, bool):
        raise SystemExit("CAD config field 'fit_baseline_scale' must be boolean.")
    alignment_scale = stereo_baseline_mm / nominal_baseline_mm if fit_baseline_scale else 1.0
    effective_mm_per_unit = mm_per_unit * alignment_scale
    source_left_mm = left_lens_obj * effective_mm_per_unit
    source_right_mm = right_lens_obj * effective_mm_per_unit
    source_focus_mm = focus_obj * effective_mm_per_unit
    source_midpoint_mm = (source_left_mm + source_right_mm) * 0.5
    source_basis = rigid_basis(
        source_right_mm - source_left_mm,
        source_focus_mm - source_midpoint_mm,
        "CAD camera",
    )
    left_forward = np.array([0.0, 0.0, 1.0], dtype=np.float64)
    right_forward_in_left = right_from_left.T @ left_forward
    target_basis = rigid_basis(
        right_centre_left_cam,
        left_forward + right_forward_in_left,
        "stereo calibration",
    )
    rotation = target_basis @ source_basis.T
    translation = -(rotation @ source_left_mm)
    return (
        rotation.reshape(-1).tolist(),
        translation.tolist(),
        {
            "left_lens_center_obj": left_lens_obj.tolist(),
            "right_lens_center_obj": right_lens_obj.tolist(),
            "focus_obj": focus_obj.tolist(),
            "nominal_cad_lens_baseline_mm": nominal_baseline_mm,
            "effective_cad_lens_baseline_mm": float(np.linalg.norm(source_right_mm - source_left_mm)),
            "stereo_baseline_mm": stereo_baseline_mm,
            "baseline_scale": alignment_scale,
        },
        effective_mm_per_unit,
    )


def load_cad_model(
    obj_path: Path | None,
    config_path: Path | None,
    camera_geometry: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, Path | None, Path | None]:
    if obj_path is None:
        if config_path is not None:
            raise SystemExit("--cad-config requires --cad-obj.")
        return None, None, None
    if not obj_path.is_file():
        raise SystemExit(f"CAD OBJ not found: {obj_path}")
    if obj_path.suffix.lower() != ".obj":
        raise SystemExit("--cad-obj must reference an OBJ file.")

    raw_config: dict[str, Any] = {}
    if config_path is not None:
        if not config_path.is_file():
            raise SystemExit(f"CAD config not found: {config_path}")
        try:
            parsed = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SystemExit(f"Unable to read CAD config: {config_path}: {error}") from error
        if not isinstance(parsed, dict):
            raise SystemExit("CAD config root must be a JSON object.")
        raw_config = parsed

    mm_per_unit = finite_float(raw_config.get("mm_per_unit", 1.0), "mm_per_unit")
    if mm_per_unit <= 0.0:
        raise SystemExit("CAD config field 'mm_per_unit' must be positive.")
    frame = str(raw_config.get("frame", "cad")).strip().lower()
    if frame not in {"cad", "camera", "pat"}:
        raise SystemExit("CAD config field 'frame' must be 'cad', 'camera', or 'pat'.")
    camera_alignment: dict[str, Any] | None = None
    if frame == "camera":
        rotation, translation, camera_alignment, mm_per_unit = camera_alignment_transform(
            raw_config,
            mm_per_unit,
            camera_geometry,
        )
    else:
        rotation = cad_vector(
            raw_config.get("rotation_matrix_row_major", [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]),
            9,
            "rotation_matrix_row_major",
        )
        rotation_matrix = np.asarray(rotation, dtype=np.float64).reshape(3, 3)
        if not np.allclose(rotation_matrix.T @ rotation_matrix, np.eye(3), rtol=0.0, atol=1e-5):
            raise SystemExit("CAD rotation_matrix_row_major must be an orthogonal rigid transform.")
        if not np.isclose(np.linalg.det(rotation_matrix), 1.0, rtol=0.0, atol=1e-5):
            raise SystemExit("CAD rotation_matrix_row_major must preserve a right-handed coordinate system.")
        translation = cad_vector(raw_config.get("translation_mm", [0.0, 0.0, 0.0]), 3, "translation_mm")
    opacity = finite_float(raw_config.get("opacity", 0.3), "opacity")
    if not 0.0 <= opacity <= 1.0:
        raise SystemExit("CAD config field 'opacity' must be between 0 and 1.")

    material_path = obj_path.with_suffix(".mtl")
    if not material_path.is_file():
        material_path = None
    default_alignment = "Nominal left-camera alignment" if frame == "camera" else "CAD local frame"
    spec: dict[str, Any] = {
        "label": str(raw_config.get("label", obj_path.stem)),
        "source": str(obj_path),
        "config_source": str(config_path) if config_path is not None else "",
        "config_sha256": file_sha256(config_path) if config_path is not None else "",
        "model_url": CAD_MODEL_ENDPOINT,
        "material_url": CAD_MATERIAL_ENDPOINT if material_path is not None else "",
        "source_units": str(raw_config.get("source_units", "OBJ units")),
        "mm_per_unit": mm_per_unit,
        "frame": frame,
        "alignment_status": str(raw_config.get("alignment_status", default_alignment)),
        "left_camera_serial": str(raw_config.get("left_camera_serial", "")),
        "right_camera_serial": str(raw_config.get("right_camera_serial", "")),
        "rotation_matrix_row_major": rotation,
        "translation_mm": translation,
        "color": str(raw_config.get("color", "#5ca8ff")),
        "opacity": opacity,
        "visible_by_default": bool(raw_config.get("visible_by_default", False)),
    }
    if camera_alignment is not None:
        spec["camera_alignment"] = camera_alignment
    return spec, obj_path, material_path


def load_npz(
    path: Path,
    max_points: int,
    calibration_override: Path | None = None,
) -> dict[str, Any]:
    data = np.load(path, allow_pickle=False)
    series: list[dict[str, Any]] = []
    t_sec = np.asarray(data["t_sec"], dtype=np.float64) if "t_sec" in data else None
    valid = np.asarray(data["valid"], dtype=bool) if "valid" in data else None

    def masked(key: str) -> tuple[np.ndarray, np.ndarray | None]:
        points = np.asarray(data[key], dtype=np.float64)
        times = t_sec
        if valid is not None and valid.shape[0] == points.shape[0]:
            points = points[valid]
            if times is not None:
                times = times[valid]
        return points, times

    if "points_left_cam_mm" in data:
        points, times = masked("points_left_cam_mm")
        add_series(
            series,
            series_id="camera",
            label="Left camera coordinates",
            frame="Left OpenCV camera: X right, Y down, Z forward [mm]",
            points=points,
            time_values=times,
            max_points=max_points,
        )
    if "points_pat_mm" in data:
        points, times = masked("points_pat_mm")
        add_series(
            series,
            series_id="pat",
            label="PAT coordinates",
            frame="AcousTools PAT coordinates [mm]",
            points=points,
            time_values=times,
            max_points=max_points,
        )
    if "points_pat_relative_mm" in data:
        points, times = masked("points_pat_relative_mm")
        add_series(
            series,
            series_id="pat_relative",
            label="PAT displacement",
            frame="PAT displacement from initial stable median [mm]",
            points=points,
            time_values=times,
            max_points=max_points,
        )
    if "source_camera_mm" in data:
        add_series(
            series,
            series_id="registration_camera",
            label="Grid measured in camera frame",
            frame="Left OpenCV camera coordinates [mm]",
            points=np.asarray(data["source_camera_mm"], dtype=np.float64),
            time_values=None,
            max_points=max_points,
            kind="points",
        )
    if "transformed_camera_mm" in data:
        add_series(
            series,
            series_id="registration_measured_pat",
            label="Grid measurement transformed to PAT",
            frame="AcousTools PAT coordinates [mm]",
            points=np.asarray(data["transformed_camera_mm"], dtype=np.float64),
            time_values=None,
            max_points=max_points,
            kind="points",
        )
    if "target_pat_mm" in data:
        add_series(
            series,
            series_id="registration_target",
            label="PAT grid targets",
            frame="AcousTools PAT coordinates [mm]",
            points=np.asarray(data["target_pat_mm"], dtype=np.float64),
            time_values=None,
            max_points=max_points,
            kind="targets",
        )
    if not series:
        raise SystemExit(f"No supported 3D arrays found in {path}. Keys: {', '.join(data.files)}")
    calibration_path = calibration_override
    if calibration_path is None:
        calibration_path = resolve_calibration_path(npz_scalar_text(data, "stereo_calibration"), path)
    result = {
        "title": path.stem,
        "input": str(path),
        "format": "npz",
        "series": series,
        "units": "mm",
    }
    geometry = load_camera_geometry(calibration_path)
    if geometry is not None:
        result["camera_geometry"] = geometry
    return result


def parse_float(row: dict[str, str], key: str) -> float:
    try:
        return float(row.get(key, ""))
    except (TypeError, ValueError):
        return float("nan")


def load_csv(
    path: Path,
    max_points: int,
    calibration_override: Path | None = None,
) -> dict[str, Any]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit(f"CSV contains no rows: {path}")
    columns = set(rows[0])
    definitions = [
        (
            "camera",
            "Camera coordinates",
            "Left OpenCV camera coordinates [mm]",
            ("camera_x_mm", "camera_y_mm", "camera_z_mm"),
            "points",
        ),
        (
            "left_camera",
            "Left camera coordinates",
            "Left OpenCV camera: X right, Y down, Z forward [mm]",
            ("x_left_cam_mm", "y_left_cam_mm", "z_left_cam_mm"),
            "trajectory",
        ),
        (
            "pat",
            "PAT coordinates",
            "AcousTools PAT coordinates [mm]",
            ("pat_x_mm", "pat_y_mm", "pat_z_mm"),
            "trajectory",
        ),
        (
            "camera_in_pat",
            "Camera measurement transformed to PAT",
            "AcousTools PAT coordinates [mm]",
            ("camera_in_pat_x_mm", "camera_in_pat_y_mm", "camera_in_pat_z_mm"),
            "points",
        ),
        (
            "pat_target",
            "PAT grid targets",
            "AcousTools PAT coordinates [mm]",
            ("target_pat_x_mm", "target_pat_y_mm", "target_pat_z_mm"),
            "targets",
        ),
    ]
    time_values = np.asarray(
        [parse_float(row, "t_sec") for row in rows],
        dtype=np.float64,
    )
    if not np.isfinite(time_values).any():
        time_values = np.arange(len(rows), dtype=np.float64)
    series: list[dict[str, Any]] = []
    for series_id, label, frame, keys, kind in definitions:
        if not set(keys).issubset(columns):
            continue
        points = np.asarray(
            [[parse_float(row, key) for key in keys] for row in rows],
            dtype=np.float64,
        )
        add_series(
            series,
            series_id=series_id,
            label=label,
            frame=frame,
            points=points,
            time_values=time_values,
            max_points=max_points,
            kind=kind,
        )
    if not series:
        raise SystemExit(f"No supported XYZ column set found in {path}.")
    result = {
        "title": path.stem,
        "input": str(path),
        "format": "csv",
        "series": series,
        "units": "mm",
    }
    geometry = load_camera_geometry(calibration_override)
    if geometry is not None:
        result["camera_geometry"] = geometry
    return result


def load_dataset(
    path: Path,
    max_points: int,
    calibration_override: Path | None = None,
) -> dict[str, Any]:
    if path.suffix.lower() == ".npz":
        dataset = load_npz(path, max_points, calibration_override)
    elif path.suffix.lower() == ".csv":
        dataset = load_csv(path, max_points, calibration_override)
    else:
        raise SystemExit("--input must be an NPZ or CSV file.")
    dataset["series_count"] = len(dataset["series"])
    dataset["point_count"] = sum(len(item["points"]) for item in dataset["series"])
    return dataset


def find_free_port(bind: str, preferred: int) -> int:
    for port in range(int(preferred), int(preferred) + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((bind, port))
            except OSError:
                continue
            return port
    raise SystemExit(f"No free port found from {preferred} through {preferred + 99}.")


def make_handler(
    dataset_json: bytes,
    cad_obj_path: Path | None = None,
    cad_material_path: Path | None = None,
) -> type[BaseHTTPRequestHandler]:
    class ViewerHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            request_path = urlparse(self.path).path
            if request_path == "/api/data":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(dataset_json)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(dataset_json)
                return
            if request_path == "/health":
                body = b'{"status":"ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if request_path == CAD_MODEL_ENDPOINT:
                if cad_obj_path is None:
                    self.send_error(404)
                    return
                file_path = cad_obj_path
            elif request_path == CAD_MATERIAL_ENDPOINT:
                if cad_material_path is None:
                    self.send_error(404)
                    return
                file_path = cad_material_path
            if request_path.startswith("/vendor/"):
                static_name = Path(request_path).name
                if static_name not in VENDOR_ASSETS:
                    self.send_error(404)
                    return
                file_path = STATIC_DIR / "vendor" / static_name
            elif request_path not in {CAD_MODEL_ENDPOINT, CAD_MATERIAL_ENDPOINT}:
                static_name = "index.html" if request_path in {"/", "/index.html"} else request_path.lstrip("/")
                if static_name not in {"index.html", "app.js", "styles.css"}:
                    self.send_error(404)
                    return
                file_path = STATIC_DIR / static_name
            if not file_path.is_file():
                self.send_error(404)
                return
            body = file_path.read_bytes()
            content_type = (
                "text/plain; charset=utf-8"
                if file_path.suffix.lower() in {".obj", ".mtl"}
                else mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            )
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format_text: str, *values: Any) -> None:
            print("[HTTP] " + (format_text % values))

    return ViewerHandler


def main() -> int:
    args = parse_args()
    input_path = args.input.resolve()
    if not input_path.exists():
        raise SystemExit(f"Input not found: {input_path}")
    for asset in (
        "index.html",
        "app.js",
        "styles.css",
        "vendor/three.module.js",
        "vendor/OrbitControls.js",
        "vendor/OBJLoader.js",
        "vendor/MTLLoader.js",
        "vendor/lucide.min.js",
    ):
        if not (STATIC_DIR / asset).exists():
            raise SystemExit(f"Viewer asset not found: {STATIC_DIR / asset}")
    calibration_override = args.stereo_calibration.resolve() if args.stereo_calibration is not None else None
    cad_obj_path = args.cad_obj.resolve() if args.cad_obj is not None else None
    cad_config_path = args.cad_config.resolve() if args.cad_config is not None else None
    dataset = load_dataset(input_path, int(args.max_points), calibration_override)
    cad_model, cad_obj_path, cad_material_path = load_cad_model(
        cad_obj_path,
        cad_config_path,
        dataset.get("camera_geometry"),
    )
    if cad_model is not None:
        dataset["cad_model"] = cad_model
    dataset_json = json.dumps(dataset, separators=(",", ":")).encode("utf-8")
    port = find_free_port(str(args.bind), int(args.port))
    server = ThreadingHTTPServer(
        (str(args.bind), port),
        make_handler(dataset_json, cad_obj_path, cad_material_path),
    )
    url = f"http://{args.bind}:{port}/"
    print(f"Stereo 3D Viewer: {url}")
    print(f"Input: {input_path}")
    if cad_model is not None:
        print(f"CAD model: {cad_obj_path}")
        if cad_material_path is not None:
            print(f"CAD material: {cad_material_path}")
    print("Press Ctrl+C to stop.")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nViewer stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
