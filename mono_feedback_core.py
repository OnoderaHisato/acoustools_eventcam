"""Hardware-independent primitives for mono event-camera position feedback.

The controller follows the form used by Bos et al.:

    C(s) = kp * (Hm(s) * (1 + tau_d s) + 1 / (tau_i s))

Positions are expressed in millimetres.  This module deliberately contains no
camera or OpenMPD imports so it can be tested without touching hardware.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import math
from pathlib import Path
from typing import Deque, Literal

import cv2
import numpy as np


DerivativeMode = Literal["measurement", "error"]


def trap_particle_separation_mm(*, trap_mm: float, measured_mm: float) -> float:
    """Return absolute trap-particle separation after validating inputs."""

    if not math.isfinite(trap_mm) or not math.isfinite(measured_mm):
        raise ValueError("trap_mm and measured_mm must be finite")
    return abs(float(trap_mm) - float(measured_mm))


def trap_particle_limit_exceeded(
    *, trap_mm: float, measured_mm: float, limit_mm: float
) -> bool:
    """Test the relative-position interlock with a small numeric tolerance."""

    if not math.isfinite(limit_mm) or limit_mm <= 0:
        raise ValueError("limit_mm must be positive")
    return trap_particle_separation_mm(trap_mm=trap_mm, measured_mm=measured_mm) > float(
        limit_mm
    ) + 1e-12


@dataclass(frozen=True)
class PidConfig:
    sample_hz: float = 2000.0
    kp: float = 0.05
    integral_hz: float = 0.0
    derivative_hz: float = 0.0
    moving_average_samples: int = 4
    derivative_mode: DerivativeMode = "measurement"
    max_trap_offset_mm: float = 0.20
    max_trap_particle_mm: float = 0.30
    max_slew_mm_s: float = 5.0
    integral_output_limit_mm: float = 0.05

    def validate(self) -> None:
        if not math.isfinite(self.sample_hz) or self.sample_hz <= 0:
            raise ValueError("sample_hz must be positive")
        if not math.isfinite(self.kp) or self.kp < 0:
            raise ValueError("kp must be finite and non-negative")
        if self.integral_hz < 0 or self.derivative_hz < 0:
            raise ValueError("integral_hz and derivative_hz must be non-negative")
        if self.moving_average_samples < 1:
            raise ValueError("moving_average_samples must be at least one")
        if self.derivative_mode not in {"measurement", "error"}:
            raise ValueError("derivative_mode must be measurement or error")
        if self.max_trap_offset_mm <= 0 or self.max_trap_particle_mm <= 0:
            raise ValueError("position safety limits must be positive")
        if self.max_slew_mm_s <= 0:
            raise ValueError("max_slew_mm_s must be positive")
        if self.integral_output_limit_mm < 0:
            raise ValueError("integral_output_limit_mm must be non-negative")

    @property
    def nominal_dt_sec(self) -> float:
        return 1.0 / self.sample_hz

    @property
    def integral_time_sec(self) -> float:
        return math.inf if self.integral_hz <= 0 else 1.0 / (2.0 * math.pi * self.integral_hz)

    @property
    def derivative_time_sec(self) -> float:
        return 0.0 if self.derivative_hz <= 0 else 1.0 / (2.0 * math.pi * self.derivative_hz)

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["nominal_dt_sec"] = self.nominal_dt_sec
        payload["integral_time_sec"] = (
            None if not math.isfinite(self.integral_time_sec) else self.integral_time_sec
        )
        payload["derivative_time_sec"] = self.derivative_time_sec
        return payload


@dataclass(frozen=True)
class ControlSample:
    reference_mm: float
    measured_mm: float
    raw_error_mm: float
    filtered_error_mm: float
    proportional_term_mm: float
    integral_term_mm: float
    derivative_term_mm: float
    unsaturated_trap_mm: float
    absolute_limited_trap_mm: float
    requested_trap_mm: float
    requested_trap_particle_mm: float
    trap_mm: float
    output_saturated: bool
    relative_limit_exceeded: bool
    slew_limited: bool

    @property
    def relative_saturated(self) -> bool:
        """Backward-compatible name for older analysis code."""

        return self.relative_limit_exceeded


class PaperPid1D:
    """Discrete 1-D implementation of the paper's filtered PID structure.

    The derivative defaults to the filtered measurement rather than the error.
    With a constant reference this is algebraically equivalent and avoids a
    derivative kick when the reference is changed later.
    """

    def __init__(self, config: PidConfig):
        config.validate()
        self.config = config
        n = int(config.moving_average_samples)
        self._error_window: Deque[float] = deque(maxlen=n)
        self._measurement_window: Deque[float] = deque(maxlen=n)
        self._previous_filtered_error: float | None = None
        self._previous_filtered_measurement: float | None = None
        self._integral_term_mm = 0.0
        self._last_trap_mm = 0.0

    def reset(self, *, trap_mm: float = 0.0) -> None:
        self._error_window.clear()
        self._measurement_window.clear()
        self._previous_filtered_error = None
        self._previous_filtered_measurement = None
        self._integral_term_mm = 0.0
        self._last_trap_mm = float(trap_mm)

    @property
    def last_trap_mm(self) -> float:
        return self._last_trap_mm

    @staticmethod
    def _clip(value: float, limit: float) -> float:
        return float(np.clip(float(value), -float(limit), float(limit)))

    def update(
        self,
        *,
        reference_mm: float,
        measured_mm: float,
        dt_sec: float | None = None,
    ) -> ControlSample:
        cfg = self.config
        dt = cfg.nominal_dt_sec if dt_sec is None else float(dt_sec)
        if not math.isfinite(dt) or dt <= 0:
            raise ValueError("dt_sec must be positive")
        if not math.isfinite(reference_mm) or not math.isfinite(measured_mm):
            raise ValueError("reference_mm and measured_mm must be finite")

        raw_error = float(reference_mm) - float(measured_mm)
        self._error_window.append(raw_error)
        self._measurement_window.append(float(measured_mm))
        filtered_error = float(np.mean(self._error_window))
        filtered_measurement = float(np.mean(self._measurement_window))

        derivative_term = 0.0
        tau_d = cfg.derivative_time_sec
        if tau_d > 0:
            if cfg.derivative_mode == "measurement":
                previous = self._previous_filtered_measurement
                if previous is not None:
                    derivative_term = -tau_d * (filtered_measurement - previous) / dt
            else:
                previous = self._previous_filtered_error
                if previous is not None:
                    derivative_term = tau_d * (filtered_error - previous) / dt

        old_integral = self._integral_term_mm
        if cfg.integral_hz > 0 and cfg.kp > 0:
            self._integral_term_mm += raw_error * dt / cfg.integral_time_sec
            pre_kp_limit = cfg.integral_output_limit_mm / cfg.kp
            self._integral_term_mm = self._clip(self._integral_term_mm, pre_kp_limit)

        proportional_term = filtered_error
        unsaturated = cfg.kp * (proportional_term + self._integral_term_mm + derivative_term)
        absolute_limited = self._clip(unsaturated, cfg.max_trap_offset_mm)
        output_saturated = not math.isclose(absolute_limited, unsaturated, rel_tol=0.0, abs_tol=1e-15)

        max_delta = cfg.max_slew_mm_s * dt
        requested_trap = float(
            np.clip(
                absolute_limited,
                self._last_trap_mm - max_delta,
                self._last_trap_mm + max_delta,
            )
        )
        requested_trap = self._clip(requested_trap, cfg.max_trap_offset_mm)
        slew_limited = not math.isclose(
            requested_trap, absolute_limited, rel_tol=0.0, abs_tol=1e-15
        )

        requested_separation = trap_particle_separation_mm(
            trap_mm=requested_trap, measured_mm=float(measured_mm)
        )
        relative_limit_exceeded = trap_particle_limit_exceeded(
            trap_mm=requested_trap,
            measured_mm=float(measured_mm),
            limit_mm=cfg.max_trap_particle_mm,
        )

        # A relative-separation limit is an interlock, not a command clamp.
        # Clamping the trap into ``measured +/- limit`` can reverse a restoring
        # command and create positive feedback.  Hold the last safe request;
        # the hardware runner observes the flag and aborts before sending.
        if relative_limit_exceeded:
            trap = self._last_trap_mm
        else:
            trap = requested_trap

        # Conditional integration: do not accumulate when saturation or slew
        # limiting would be driven farther in the same direction by the error.
        limited = output_saturated or relative_limit_exceeded or slew_limited
        if cfg.integral_hz > 0 and limited:
            if relative_limit_exceeded:
                self._integral_term_mm = old_integral
            else:
                pushes_positive = raw_error > 0 and unsaturated > trap
                pushes_negative = raw_error < 0 and unsaturated < trap
                if pushes_positive or pushes_negative:
                    self._integral_term_mm = old_integral

        self._previous_filtered_error = filtered_error
        self._previous_filtered_measurement = filtered_measurement
        self._last_trap_mm = trap
        return ControlSample(
            reference_mm=float(reference_mm),
            measured_mm=float(measured_mm),
            raw_error_mm=raw_error,
            filtered_error_mm=filtered_error,
            proportional_term_mm=proportional_term,
            integral_term_mm=float(self._integral_term_mm),
            derivative_term_mm=float(derivative_term),
            unsaturated_trap_mm=float(unsaturated),
            absolute_limited_trap_mm=float(absolute_limited),
            requested_trap_mm=float(requested_trap),
            requested_trap_particle_mm=float(requested_separation),
            trap_mm=trap,
            output_saturated=output_saturated,
            relative_limit_exceeded=relative_limit_exceeded,
            slew_limited=slew_limited,
        )


@dataclass(frozen=True)
class MonoAxisProjection:
    """Local image-plane projection of one PAT axis for the left camera."""

    axis: Literal["x", "y", "z"]
    predicted_origin_px: tuple[float, float]
    vector_px_per_mm: tuple[float, float]

    @property
    def scale_px_per_mm(self) -> float:
        return float(np.linalg.norm(np.asarray(self.vector_px_per_mm, dtype=float)))

    @property
    def unit_vector_px(self) -> np.ndarray:
        scale = self.scale_px_per_mm
        if scale <= 0:
            raise ValueError("projection scale must be positive")
        return np.asarray(self.vector_px_per_mm, dtype=float) / scale

    def displacement_mm(
        self,
        point_px: tuple[float, float],
        locked_origin_px: tuple[float, float],
    ) -> float:
        delta = np.asarray(point_px, dtype=float) - np.asarray(locked_origin_px, dtype=float)
        return float(np.dot(delta, self.unit_vector_px) / self.scale_px_per_mm)

    def to_dict(self) -> dict[str, object]:
        return {
            "axis": self.axis,
            "predicted_origin_px": list(self.predicted_origin_px),
            "vector_px_per_mm": list(self.vector_px_per_mm),
            "scale_px_per_mm": self.scale_px_per_mm,
            "unit_vector_px": self.unit_vector_px.tolist(),
        }


@dataclass(frozen=True)
class LockStabilityResult:
    """Robust recent-window diagnostics for the pre-control position lock."""

    origin_px: tuple[float, float]
    robust_std_mm: float
    p90_span_mm: float
    endpoint_offset_mm: float
    stable: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def assess_lock_stability(
    points_px: np.ndarray,
    projection: MonoAxisProjection,
    *,
    max_robust_std_mm: float,
    max_p90_span_mm: float,
    max_endpoint_offset_mm: float,
) -> LockStabilityResult:
    """Assess whether a consecutive recent centroid window is stationary.

    The robust standard deviation is MAD scaled by 1.4826.  The 5th--95th
    percentile span rejects motion without letting one event-centroid outlier
    dominate the decision.  The final point must also remain near the window
    median so control cannot start at an oscillation extreme.
    """

    points = np.asarray(points_px, dtype=float)
    if points.ndim != 2 or points.shape[1] != 2 or points.shape[0] < 2:
        raise ValueError("points_px must contain at least two (x, y) points")
    if not np.all(np.isfinite(points)):
        raise ValueError("points_px must be finite")
    limits = (max_robust_std_mm, max_p90_span_mm, max_endpoint_offset_mm)
    if any(not math.isfinite(value) or value <= 0 for value in limits):
        raise ValueError("lock stability limits must be positive")

    origin = np.median(points, axis=0)
    axis_mm = (points @ projection.unit_vector_px) / projection.scale_px_per_mm
    axis_median = float(np.median(axis_mm))
    robust_std = float(1.4826 * np.median(np.abs(axis_mm - axis_median)))
    p90_span = float(np.percentile(axis_mm, 95.0) - np.percentile(axis_mm, 5.0))
    endpoint_offset = abs(float(axis_mm[-1]) - axis_median)
    stable = bool(
        robust_std <= float(max_robust_std_mm)
        and p90_span <= float(max_p90_span_mm)
        and endpoint_offset <= float(max_endpoint_offset_mm)
    )
    return LockStabilityResult(
        origin_px=(float(origin[0]), float(origin[1])),
        robust_std_mm=robust_std,
        p90_span_mm=p90_span,
        endpoint_offset_mm=endpoint_offset,
        stable=stable,
    )


def load_left_camera_axis_projection(
    calibration_path: str | Path,
    camera_to_pat_path: str | Path,
    *,
    axis: Literal["x", "y", "z"] = "x",
    finite_difference_mm: float = 0.5,
) -> MonoAxisProjection:
    """Derive the local PAT-axis image direction from existing calibrations."""

    calibration_path = Path(calibration_path)
    camera_to_pat_path = Path(camera_to_pat_path)
    with np.load(calibration_path, allow_pickle=False) as calib:
        camera_matrix = np.asarray(calib["left_camera_matrix"], dtype=np.float64)
        distortion = np.asarray(calib["left_dist_coeffs"], dtype=np.float64)
    with np.load(camera_to_pat_path, allow_pickle=False) as registration:
        rotation_camera_to_pat = np.asarray(registration["R_camera_to_pat"], dtype=np.float64)
        translation_camera_to_pat = np.asarray(registration["t_camera_to_pat_mm"], dtype=np.float64).reshape(3)

    if rotation_camera_to_pat.shape != (3, 3):
        raise ValueError("R_camera_to_pat must be 3x3")
    axis_index = {"x": 0, "y": 1, "z": 2}[axis]
    step = float(finite_difference_mm)
    if step <= 0:
        raise ValueError("finite_difference_mm must be positive")

    def project_pat(point_pat_mm: np.ndarray) -> np.ndarray:
        point_camera_mm = rotation_camera_to_pat.T @ (point_pat_mm - translation_camera_to_pat)
        image, _ = cv2.projectPoints(
            point_camera_mm.reshape(1, 3),
            np.zeros(3),
            np.zeros(3),
            camera_matrix,
            distortion,
        )
        return image.reshape(2).astype(float)

    origin = np.zeros(3, dtype=float)
    plus = origin.copy()
    minus = origin.copy()
    plus[axis_index] = step
    minus[axis_index] = -step
    uv_origin = project_pat(origin)
    vector = (project_pat(plus) - project_pat(minus)) / (2.0 * step)
    if float(np.linalg.norm(vector)) <= 0:
        raise ValueError("calibration produced a zero image-axis projection")
    return MonoAxisProjection(
        axis=axis,
        predicted_origin_px=(float(uv_origin[0]), float(uv_origin[1])),
        vector_px_per_mm=(float(vector[0]), float(vector[1])),
    )


@dataclass(frozen=True)
class SecondOrderPlantConfig:
    natural_frequency_hz: float = 12.0
    damping_ratio: float = 0.014
    delay_sec: float = 0.0055
    disturbance_accel_mm_s2: float = 0.0


def simulate_closed_loop(
    controller: PaperPid1D,
    *,
    duration_sec: float,
    plant: SecondOrderPlantConfig = SecondOrderPlantConfig(),
    initial_position_mm: float = 0.10,
    reference_mm: float = 0.0,
) -> dict[str, np.ndarray]:
    """Deterministic Euler simulation used for hardware-free regression tests."""

    dt = controller.config.nominal_dt_sec
    count = max(2, int(round(float(duration_sec) / dt)))
    omega = 2.0 * math.pi * float(plant.natural_frequency_hz)
    delay_samples = max(0, int(round(float(plant.delay_sec) / dt)))
    command_delay: Deque[float] = deque([0.0] * (delay_samples + 1), maxlen=delay_samples + 1)
    t = np.arange(count, dtype=float) * dt
    x = np.zeros(count, dtype=float)
    velocity = np.zeros(count, dtype=float)
    trap = np.zeros(count, dtype=float)
    x[0] = float(initial_position_mm)
    controller.reset(trap_mm=0.0)
    for index in range(1, count):
        sample = controller.update(reference_mm=reference_mm, measured_mm=x[index - 1], dt_sec=dt)
        command_delay.append(sample.trap_mm)
        delayed_trap = float(command_delay[0])
        acceleration = (
            -2.0 * float(plant.damping_ratio) * omega * velocity[index - 1]
            - omega * omega * (x[index - 1] - delayed_trap)
            + float(plant.disturbance_accel_mm_s2)
        )
        velocity[index] = velocity[index - 1] + acceleration * dt
        x[index] = x[index - 1] + velocity[index] * dt
        trap[index] = sample.trap_mm
    return {"time_sec": t, "position_mm": x, "velocity_mm_s": velocity, "trap_mm": trap}
