#!/usr/bin/env python3
"""Two-stage "physics-first" SINDy for stereo 3D trajectories.

Motivation: in the existing SINDYc pipeline (sindy_stereo3d_full.py /
sindy_stereo3d_full_tune.py) the state is the trap offset e = measured - command.
When trajectory tracking is tight, e stays confined to a narrow band, so the
sin(2k*e) harmonics and low-order e-power terms become nearly collinear and the
control-coupling terms (u, u_dot, u_ddot) are strongly correlated with e itself.
This is the classic closed-loop identification problem: a tightly-tracking
controller hides the plant's own nonlinearity from the identification data.

This module implements the two-stage alternative discussed with the user:

  Stage A (free / unforced fit): on segments where the commanded trajectory is
  held (locally) constant, u_dot = u_ddot = 0 exactly, so the true dynamics
  collapse to e_ddot = f(e, e_dot) with no control terms at all. Fitting f on
  many such segments -- ideally released from a wide spread of offsets -- gives
  a well-conditioned, noise-robust estimate of the intrinsic restoring force,
  damping, and bias, unconfounded by tracking-induced collinearity with u.

  Stage B (residual control coupling): with f(e, e_dot) fixed from Stage A,
  the moving-trajectory data is used to regress the *residual* acceleration
  (measured minus Stage-A's prediction) against only the remaining
  u / u_dot / u_ddot (and cross) terms. This is a much lower-dimensional,
  better-conditioned regression than fitting everything jointly.

This is purely additive: it imports build_candidate_library / build_weak_matrix /
sparse_regression from sindy_stereo3d_full.py and load_trial_full / discover_trials
from sindy_stereo3d_full_tune.py / sindy_stereo3d_data.py without modifying them.

Usage:
    python3 sindy_stereo3d_free_response.py detect --base-dir Acoutools_eventcam
    python3 sindy_stereo3d_free_response.py fit --base-dir Acoutools_eventcam --out-dir OUT
    python3 sindy_stereo3d_free_response.py self-test
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from sindy_stereo3d_data import DT_RAW, Trial, discover_trials
from sindy_stereo3d_full import (
    K_WAVE,
    FeatureSpec,
    SparseFit,
    build_candidate_library,
    build_weak_matrix,
    build_weak_test_function,
    format_equation,
    sparse_regression,
)
from sindy_stereo3d_full_tune import load_trial_full

AXES = ("x", "y", "z")
CONTROL_KINDS = {"u_linear", "u_dot_linear", "u_ddot_linear", "e_u_product", "e_ud_product", "v_u_product"}


def split_library(tier: str, axis: int) -> tuple[list[FeatureSpec], list[FeatureSpec]]:
    """Split one axis's candidate library into (free, control) feature lists."""
    full = build_candidate_library(tier, axis)
    free = [feature for feature in full if feature.kind not in CONTROL_KINDS]
    control = [feature for feature in full if feature.kind in CONTROL_KINDS]
    return free, control


def detect_hold_segments(
    trial: Trial, vel_tol_mm_s: float, min_hold_sec: float, sample_dt: float
) -> list[tuple[int, int]]:
    """Contiguous index ranges where the commanded velocity stays below vel_tol_mm_s."""
    speed = np.linalg.norm(trial.u_dot, axis=1)
    slow = speed < vel_tol_mm_s
    min_len = max(1, int(round(min_hold_sec / sample_dt)))
    segments: list[tuple[int, int]] = []
    start: int | None = None
    for index, ok in enumerate(slow):
        if ok and start is None:
            start = index
        elif not ok and start is not None:
            if index - start >= min_len:
                segments.append((start, index))
            start = None
    if start is not None and len(slow) - start >= min_len:
        segments.append((start, len(slow)))
    return segments


def slice_trial(trial: Trial, start: int, end: int) -> Trial:
    return replace(
        trial,
        e=trial.e[start:end],
        ctrl=trial.ctrl[start:end],
        e_dot=trial.e_dot[start:end],
        u_dot=trial.u_dot[start:end],
        u_ddot=trial.u_ddot[start:end],
    )


def usable_segments(
    trial: Trial, half_width: int, vel_tol_mm_s: float, min_hold_sec: float, sample_dt: float
) -> list[Trial]:
    min_len = 2 * half_width + 2
    result: list[Trial] = []
    for start, end in detect_hold_segments(trial, vel_tol_mm_s, min_hold_sec, sample_dt):
        if end - start >= min_len:
            result.append(slice_trial(trial, start, end))
    return result


def weak_stencils(setting: dict[str, Any], sample_dt: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, int, int]:
    half_width = max(2, int(round(float(setting["weak_half_width_ms"]) * 1.0e-3 / sample_dt)))
    stride = max(1, int(round(float(setting["weak_stride_ms"]) * 1.0e-3 / sample_dt)))
    phi, dphi, d2phi = build_weak_test_function(half_width, int(setting["weak_polynomial_order"]), sample_dt)
    return phi, dphi, d2phi, half_width, stride


def _fit(design: np.ndarray, target: np.ndarray, setting: dict[str, Any]) -> SparseFit:
    return sparse_regression(
        design,
        target,
        method=str(setting["optimizer"]),
        alpha=float(setting["ridge_alpha"]),
        threshold=float(setting["threshold"]),
        sparsity=int(setting["sparsity"]),
    )


def fit_free_response(
    trials: Sequence[Trial],
    tier: str,
    setting: dict[str, Any],
    sample_dt: float,
    vel_tol_mm_s: float,
    min_hold_sec: float,
) -> tuple[list[SparseFit], list[list[FeatureSpec]], dict[str, int]]:
    """Stage A: fit e_ddot = f(e, e_dot) on segments where u is locally constant."""
    phi, dphi, d2phi, half_width, stride = weak_stencils(setting, sample_dt)
    fits: list[SparseFit] = []
    libraries: list[list[FeatureSpec]] = []
    coverage = {"trials_with_segments": 0, "total_segments": 0, "total_rows": 0}
    for axis in range(3):
        free_features, _ = split_library(tier, axis)
        libraries.append(free_features)
        design_blocks: list[np.ndarray] = []
        target_blocks: list[np.ndarray] = []
        for trial in trials:
            segments = usable_segments(trial, half_width, vel_tol_mm_s, min_hold_sec, sample_dt)
            if segments and axis == 0:
                coverage["trials_with_segments"] += 1
                coverage["total_segments"] += len(segments)
            for segment in segments:
                design, target = build_weak_matrix(segment, axis, free_features, phi, dphi, d2phi, sample_dt, stride)
                design_blocks.append(design)
                target_blocks.append(target)
        if not design_blocks:
            raise RuntimeError(
                "no hold segments (commanded speed < "
                f"{vel_tol_mm_s} mm/s sustained >= {min_hold_sec * 1000:.0f} ms, long enough for a "
                f"{2 * half_width + 2} sample weak window) were found in the supplied trials. Stage A needs "
                "dedicated step-response / release-and-catch recordings where the commanded target is held "
                "fixed -- see `detect` for a coverage report against the current dataset."
            )
        design = np.vstack(design_blocks)
        target = np.concatenate(target_blocks)
        coverage["total_rows"] += len(target)
        fits.append(_fit(design, target, setting))
    return fits, libraries, coverage


def fit_residual_control(
    trials: Sequence[Trial],
    tier: str,
    setting: dict[str, Any],
    sample_dt: float,
    free_fits: Sequence[SparseFit],
    free_libraries: Sequence[list[FeatureSpec]],
) -> tuple[list[SparseFit], list[list[FeatureSpec]]]:
    """Stage B: with f(e, e_dot) fixed, fit the residual against control-only terms."""
    phi, dphi, d2phi, half_width, stride = weak_stencils(setting, sample_dt)
    fits: list[SparseFit] = []
    libraries: list[list[FeatureSpec]] = []
    for axis in range(3):
        free_features = free_libraries[axis]
        _, control_features = split_library(tier, axis)
        libraries.append(control_features)
        combined = list(free_features) + list(control_features)
        design_blocks: list[np.ndarray] = []
        target_blocks: list[np.ndarray] = []
        for trial in trials:
            if trial.n <= 2 * half_width + 1:
                continue
            design, target = build_weak_matrix(trial, axis, combined, phi, dphi, d2phi, sample_dt, stride)
            free_part = design[:, : len(free_features)]
            control_part = design[:, len(free_features) :]
            predicted_free = free_part @ free_fits[axis].coefficients
            design_blocks.append(control_part)
            target_blocks.append(target - predicted_free)
        if not design_blocks:
            raise RuntimeError("no trials long enough for the requested weak window")
        design = np.vstack(design_blocks)
        target = np.concatenate(target_blocks)
        fits.append(_fit(design, target, setting))
    return fits, libraries


# ------------------------------------------------------------------------- #
# CLI
# ------------------------------------------------------------------------- #

def build_setting(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "downsample": args.downsample,
        "velocity_window_ms": args.velocity_window_ms,
        "velocity_polyorder": args.velocity_polyorder,
        "control_window_ms": args.control_window_ms,
        "control_polyorder": args.control_polyorder,
        "dc_calib_sec": args.dc_calib_sec,
        "weak_half_width_ms": args.weak_half_width_ms,
        "weak_polynomial_order": args.weak_polynomial_order,
        "weak_stride_ms": args.weak_stride_ms,
        "optimizer": args.optimizer,
        "ridge_alpha": args.ridge_alpha,
        "threshold": args.threshold,
        "sparsity": args.sparsity,
        "max_sec": 0.0,
    }


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-dir", type=Path, default=Path("Acoutools_eventcam"))
    parser.add_argument("--tier", default="core", choices=["core", "coupled", "full"])
    parser.add_argument("--downsample", type=int, default=1)
    parser.add_argument("--velocity-window-ms", type=float, default=1.1)
    parser.add_argument("--velocity-polyorder", type=int, default=3)
    parser.add_argument("--control-window-ms", type=float, default=1.1)
    parser.add_argument("--control-polyorder", type=int, default=3)
    parser.add_argument("--dc-calib-sec", type=float, default=4.0)
    parser.add_argument("--weak-half-width-ms", type=float, default=15.0)
    parser.add_argument("--weak-polynomial-order", type=int, default=5)
    parser.add_argument("--weak-stride-ms", type=float, default=2.5)
    parser.add_argument("--optimizer", default="stlsq", choices=["stlsq", "topk"])
    parser.add_argument("--ridge-alpha", type=float, default=1.0e-6)
    parser.add_argument("--threshold", type=float, default=0.01)
    parser.add_argument("--sparsity", type=int, default=6)
    parser.add_argument("--vel-tol-mm-s", type=float, default=5.0)
    parser.add_argument("--min-hold-sec", type=float, default=0.05)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_detect = sub.add_parser("detect", help="report hold-segment coverage of the existing dataset")
    add_common_args(p_detect)

    p_fit = sub.add_parser("fit", help="run the two-stage free-response fit")
    add_common_args(p_fit)
    p_fit.add_argument("--out-dir", type=Path, required=True)

    sub.add_parser("self-test", help="synthetic recovery check (no real data required)")

    return parser.parse_args()


def cmd_detect(args: argparse.Namespace) -> int:
    setting = build_setting(args)
    infos = discover_trials(args.base_dir)
    if not infos:
        raise SystemExit(f"no stereo 3D datasets found under {args.base_dir}")
    sample_dt = DT_RAW * int(setting["downsample"])
    _, _, _, half_width, _ = weak_stencils(setting, sample_dt)

    trials_with = 0
    total_segments = 0
    total_duration = 0.0
    for info in infos:
        trial = load_trial_full(info, setting)
        segments = usable_segments(trial, half_width, args.vel_tol_mm_s, args.min_hold_sec, sample_dt)
        if segments:
            trials_with += 1
            total_segments += len(segments)
            total_duration += sum(segment.n * sample_dt for segment in segments)

    print(
        f"{trials_with}/{len(infos)} trials contain >=1 usable hold segment "
        f"(commanded speed < {args.vel_tol_mm_s} mm/s sustained >= {args.min_hold_sec * 1000:.0f} ms, "
        f"long enough for a {2 * half_width + 2}-sample weak window)"
    )
    print(f"total segments: {total_segments}, total usable duration: {total_duration:.3f} s")
    if total_segments == 0:
        print(
            "Stage A has nothing to train on: every current trial is a continuously-moving commanded "
            "trajectory. Collect dedicated step-response / release-and-catch recordings (fixed target, "
            "large initial offset, let the particle settle) before running `fit`."
        )
    return 0


def cmd_fit(args: argparse.Namespace) -> int:
    setting = build_setting(args)
    infos = discover_trials(args.base_dir)
    if not infos:
        raise SystemExit(f"no stereo 3D datasets found under {args.base_dir}")
    sample_dt = DT_RAW * int(setting["downsample"])
    trials = [load_trial_full(info, setting) for info in infos]

    free_fits, free_libraries, coverage = fit_free_response(
        trials, args.tier, setting, sample_dt, args.vel_tol_mm_s, args.min_hold_sec
    )
    print(
        f"Stage A: {coverage['trials_with_segments']} trials / {coverage['total_segments']} segments / "
        f"{coverage['total_rows']} weak-form rows"
    )
    for axis in range(3):
        print(f"  [free] {AXES[axis]}: {format_equation(axis, free_libraries[axis], free_fits[axis])}")

    residual_fits, control_libraries = fit_residual_control(
        trials, args.tier, setting, sample_dt, free_fits, free_libraries
    )
    print("Stage B (residual control coupling):")
    for axis in range(3):
        print(f"  [ctrl] {AXES[axis]}: {format_equation(axis, control_libraries[axis], residual_fits[axis])}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "tier": args.tier,
        "setting": setting,
        "coverage": coverage,
        "free_response": [
            {
                "axis": AXES[axis],
                "features": [feature.to_dict() for feature in free_libraries[axis]],
                "coefficients": free_fits[axis].coefficients.tolist(),
                "support": free_fits[axis].support.tolist(),
            }
            for axis in range(3)
        ],
        "control_coupling": [
            {
                "axis": AXES[axis],
                "features": [feature.to_dict() for feature in control_libraries[axis]],
                "coefficients": residual_fits[axis].coefficients.tolist(),
                "support": residual_fits[axis].support.tolist(),
            }
            for axis in range(3)
        ],
    }
    out_path = args.out_dir / "free_response_model.json"
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"wrote {out_path}")
    return 0


def _rk4_second_order(rhs, e0: float, edot0: float, dt: float, n_steps: int) -> tuple[np.ndarray, np.ndarray]:
    e = np.empty(n_steps)
    edot = np.empty(n_steps)
    e[0], edot[0] = e0, edot0
    for i in range(1, n_steps):
        t0 = (i - 1) * dt

        def deriv(t: float, state: np.ndarray) -> np.ndarray:
            ee, vv = state
            return np.array(rhs(t, ee, vv))

        state = np.array([e[i - 1], edot[i - 1]])
        k1 = deriv(t0, state)
        k2 = deriv(t0 + dt / 2, state + dt / 2 * k1)
        k3 = deriv(t0 + dt / 2, state + dt / 2 * k2)
        k4 = deriv(t0 + dt, state + dt * k3)
        state = state + dt / 6 * (k1 + 2 * k2 + 2 * k3 + k4)
        e[i], edot[i] = state
    return e, edot


def cmd_self_test() -> int:
    """Synthetic recovery check: simulate a known e_ddot = f(e, e_dot) - u_ddot - c*u_dot
    system, split into (a) constant-command release trials and (b) moving-command
    trials, and verify the two-stage fit recovers the known coefficients. The
    u_ddot coefficient must come back at exactly -1.0 (it is forced by e = x - u,
    not a free parameter), which is the sharpest correctness check available.
    """
    rng = np.random.default_rng(0)
    dt = 2.0e-3

    c1_sin = -1.364e5  # sin(2k*e_z) restoring coefficient  (~ small-signal stiffness 2e5 1/s^2)
    c3_drag = -0.5  # |e_dot_z| * e_dot_z coefficient
    c4_bias = -9810.0  # constant bias (z axis only)
    c6_udot = -40.0  # u_dot_z coefficient (Stage B)
    c7_uddot = -1.0  # u_ddot_z coefficient (Stage B, physically exact)

    def rhs(t: float, e: float, edot: float, u: float = 0.0, udot: float = 0.0, uddot: float = 0.0) -> tuple[float, float]:
        eddot = c1_sin * math.sin(2.0 * K_WAVE * e) + c3_drag * abs(edot) * edot + c4_bias + c6_udot * udot + c7_uddot * uddot
        return edot, eddot

    def make_trial(label: str, e_arr: np.ndarray, edot_arr: np.ndarray, u_arr: np.ndarray, udot_arr: np.ndarray, uddot_arr: np.ndarray) -> Trial:
        n = len(e_arr)
        e3 = np.zeros((n, 3))
        edot3 = np.zeros((n, 3))
        u3 = np.zeros((n, 3))
        ud3 = np.zeros((n, 3))
        udd3 = np.zeros((n, 3))
        e3[:, 2], edot3[:, 2] = e_arr, edot_arr
        u3[:, 2], ud3[:, 2], udd3[:, 2] = u_arr, udot_arr, uddot_arr
        return Trial(label=label, group=label, scale="synthetic", e=e3, ctrl=u3, e_dot=edot3, u_dot=ud3, u_ddot=udd3, dc=np.zeros(3))

    # Stage-A trials: released from a wide spread of offsets, target held constant
    # (u = u_dot = u_ddot = 0 exactly), so the true dynamics reduce to e_ddot = f(e, e_dot).
    n_steps_a = 180
    free_trials = []
    offsets = np.concatenate([rng.uniform(0.5, 4.0, 8), rng.uniform(-4.0, -0.5, 8)])
    for i, e0 in enumerate(offsets):
        e_arr, edot_arr = _rk4_second_order(lambda t, e, edot: rhs(t, e, edot), e0, 0.0, dt, n_steps_a)
        zeros = np.zeros(n_steps_a)
        free_trials.append(make_trial(f"synthetic_release_{i}", e_arr, edot_arr, zeros, zeros, zeros))

    # Stage-B trials: smooth moving command, full dynamics (control coupling active).
    n_steps_b = 1500
    ctrl_trials = []
    for i, (amp, freq, phase) in enumerate([(3.0, 3.0, 0.0), (2.0, 5.0, 0.7), (4.0, 1.5, 2.1)]):
        omega = 2.0 * math.pi * freq
        t = np.arange(n_steps_b) * dt
        u_arr = amp * np.sin(omega * t + phase)
        udot_arr = amp * omega * np.cos(omega * t + phase)
        uddot_arr = -amp * omega * omega * np.sin(omega * t + phase)

        def moving_rhs(tt: float, e: float, edot: float, amp=amp, omega=omega, phase=phase) -> tuple[float, float]:
            return rhs(tt, e, edot, amp * math.sin(omega * tt + phase), amp * omega * math.cos(omega * tt + phase), -amp * omega * omega * math.sin(omega * tt + phase))

        e_arr, edot_arr = _rk4_second_order(moving_rhs, 0.0, 0.0, dt, n_steps_b)
        ctrl_trials.append(make_trial(f"synthetic_move_{i}", e_arr, edot_arr, u_arr, udot_arr, uddot_arr))

    setting = {
        "weak_half_width_ms": 15.0,
        "weak_polynomial_order": 5,
        "weak_stride_ms": 2.0,
        "optimizer": "stlsq",
        "ridge_alpha": 1.0e-8,
        "threshold": 0.01,
        "sparsity": 6,
    }

    free_fits, free_libraries, coverage = fit_free_response(
        free_trials, "core", setting, dt, vel_tol_mm_s=1.0, min_hold_sec=0.0
    )
    residual_fits, control_libraries = fit_residual_control(
        ctrl_trials, "core", setting, dt, free_fits, free_libraries
    )

    def find(features: Sequence[FeatureSpec], kind: str) -> int | None:
        for index, feature in enumerate(features):
            if feature.kind == kind and feature.exponents and feature.exponents[0] == 2:
                return index
        for index, feature in enumerate(features):
            if feature.kind == kind:
                return index
        return None

    checks = []
    idx = find(free_libraries[2], "sin_e")
    checks.append(("sin(2k*e_z) [Stage A]", c1_sin, free_fits[2].coefficients[idx], 0.15))
    idx = find(free_libraries[2], "quadratic_drag")
    checks.append(("|e_dot_z|*e_dot_z [Stage A]", c3_drag, free_fits[2].coefficients[idx], 0.30))
    idx = find(free_libraries[2], "constant")
    checks.append(("bias_z [Stage A]", c4_bias, free_fits[2].coefficients[idx], 0.15))
    idx = find(control_libraries[2], "u_dot_linear")
    checks.append(("u_dot_z [Stage B]", c6_udot, residual_fits[2].coefficients[idx], 0.35))
    idx = find(control_libraries[2], "u_ddot_linear")
    checks.append(("u_ddot_z [Stage B, exact -1.0 expected]", c7_uddot, residual_fits[2].coefficients[idx], 0.15))

    print(f"Stage A coverage: {coverage['trials_with_segments']} trials / {coverage['total_segments']} segments")
    all_ok = True
    for name, truth, recovered, tol in checks:
        rel_error = abs(recovered - truth) / max(abs(truth), 1.0e-9)
        ok = rel_error <= tol
        all_ok &= ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name:42s} truth={truth:12.4g}  recovered={recovered:12.4g}  rel_err={rel_error:6.2%} (tol {tol:.0%})")

    print("SELF-TEST " + ("PASSED" if all_ok else "FAILED"))
    return 0 if all_ok else 1


def main() -> int:
    args = parse_args()
    if args.command == "detect":
        return cmd_detect(args)
    if args.command == "fit":
        return cmd_fit(args)
    if args.command == "self-test":
        return cmd_self_test()
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
