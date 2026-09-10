#!/usr/bin/env python3
"""Distributed nested-CV tuning and evaluation for stereo 3D Full SINDy.

Typical workflow::

    python sindy_stereo3d_full_tune.py prepare --run-dir full_sindy_run
    python sindy_stereo3d_full_tune.py run-task --run-dir full_sindy_run --task-index 0
    python sindy_stereo3d_full_tune.py aggregate --run-dir full_sindy_run
    python sindy_stereo3d_full_tune.py evaluate --run-dir full_sindy_run

On Miyabi, ``run-task`` reads ``PBS_ARRAY_INDEX`` when ``--task-index`` is
omitted.  Every array element evaluates exactly one hyperparameter setting in
one outer fold; the held-out outer trajectory group is never inspected.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from scipy.signal import savgol_filter

from sindy_stereo3d_data import (
    DT_RAW,
    Trial,
    block_mean,
    discover_trials,
)
from sindy_stereo3d_full import (
    AXES,
    FeatureSpec,
    SparseFit,
    build_candidate_library,
    build_weak_matrix,
    build_weak_test_function,
    correlation_mask,
    format_equation,
    normalized_weak_rmse,
    regression_diagnostics,
    simulate_full_model,
    stability_selection,
)


DEFAULT_CONFIG = Path(__file__).with_name("full_sindy_search_config.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="create deterministic CV task manifest")
    prepare.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    prepare.add_argument("--base-dir", type=Path, default=Path("./Acoutools_eventcam"))
    prepare.add_argument("--run-dir", type=Path, required=True)

    task = subparsers.add_parser("run-task", help="evaluate one inner-CV hyperparameter task")
    task.add_argument("--run-dir", type=Path, required=True)
    task.add_argument("--task-index", type=int)
    task.add_argument("--force", action="store_true")

    aggregate = subparsers.add_parser("aggregate", help="select settings using inner-CV only")
    aggregate.add_argument("--run-dir", type=Path, required=True)
    aggregate.add_argument("--allow-incomplete", action="store_true")

    evaluate = subparsers.add_parser("evaluate", help="run held-out outer CV and fit deploy model")
    evaluate.add_argument("--run-dir", type=Path, required=True)
    evaluate.add_argument("--force", action="store_true")

    describe = subparsers.add_parser("describe-library", help="print candidate counts and names")
    describe.add_argument("--tier", choices=("core", "coupled", "full"), default="full")
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=True)
        handle.write("\n")
    temporary.replace(path)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def canonical_key(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def setting_id(setting: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_key(setting).encode("utf-8")).hexdigest()[:12]


def generate_settings(config: dict[str, Any]) -> list[dict[str, Any]]:
    search = config["search"]
    space: dict[str, list[Any]] = search["space"]
    requested = int(search["n_settings"])
    rng = np.random.default_rng(int(search["seed"]))
    settings: list[dict[str, Any]] = []
    seen: set[str] = set()

    def append(setting: dict[str, Any]) -> None:
        merged = dict(search.get("defaults", {}))
        merged.update(setting)
        # Remove conditionally inactive dimensions so random search does not
        # spend Miyabi jobs on numerically identical configurations.
        if merged.get("optimizer") == "topk":
            merged["threshold"] = 0.0
        elif merged.get("optimizer") == "stlsq":
            merged["sparsity"] = 0
        if int(merged.get("bootstrap_repeats", 0)) == 0:
            merged["stability_threshold"] = 0.0
        key = canonical_key(merged)
        if key not in seen:
            seen.add(key)
            settings.append(merged)

    for anchor in search.get("anchors", []):
        append(anchor)
    maximum_unique = math.prod(len(values) for values in space.values())
    attempts = 0
    while len(settings) < requested and len(settings) < maximum_unique and attempts < requested * 100:
        sampled = {name: values[int(rng.integers(len(values)))] for name, values in space.items()}
        append(sampled)
        attempts += 1
    if len(settings) < requested:
        print(f"warning: generated only {len(settings)} unique settings", file=sys.stderr)
    return settings


def eligible_outer_groups(base_dir: Path, selection: dict[str, Any]) -> set[str]:
    """Trajectory groups whose shortest member trial is long enough for outer_rollout_sec
    of free-running simulation past the rollout_start_sec warm-up point (see rollout_ratio).
    Short groups are still used for training whenever another group is held out -- they are
    only excluded from *being* the held-out outer/inner validation fold, where a too-short
    trial would otherwise report a spurious guaranteed-diverged rollout regardless of model
    quality (steps < 2 in rollout_ratio)."""
    min_duration = float(selection["rollout_start_sec"]) + float(selection["outer_rollout_sec"])
    durations: dict[str, float] = {}
    for info in discover_trials(base_dir):
        group = str(info["group"])
        duration = float(info.get("duration_sec", 0.0))
        durations[group] = min(durations.get(group, math.inf), duration)
    return {group for group, duration in durations.items() if duration >= min_duration}


def prepare_manifest(args: argparse.Namespace) -> int:
    config = read_json(args.config)
    infos = discover_trials(args.base_dir)
    if not infos:
        raise SystemExit(f"no stereo 3D datasets found under {args.base_dir}")
    all_groups = sorted({str(info["group"]) for info in infos})
    eligible = eligible_outer_groups(args.base_dir, config["selection"])
    groups = [group for group in all_groups if group in eligible]
    excluded_groups = sorted(set(all_groups) - eligible)
    if not groups:
        raise SystemExit("no trajectory groups meet the outer-fold rollout duration threshold")
    if excluded_groups:
        print(
            f"{len(excluded_groups)}/{len(all_groups)} trajectory groups are too short for "
            f"outer-fold rollout evaluation (still used for training, never held out): "
            f"{', '.join(excluded_groups)}"
        )
    settings = generate_settings(config)
    tasks: list[dict[str, Any]] = []
    for outer_fold, outer_group in enumerate(groups):
        for config_index, setting in enumerate(settings):
            tasks.append(
                {
                    "task_index": len(tasks),
                    "outer_fold": outer_fold,
                    "outer_group": outer_group,
                    "config_index": config_index,
                    "setting_id": setting_id(setting),
                }
            )
    args.run_dir.mkdir(parents=True, exist_ok=False)
    (args.run_dir / "tasks").mkdir()
    manifest = {
        "schema_version": 1,
        "base_dir": str(args.base_dir.resolve()),
        "config_source": str(args.config.resolve()),
        "groups": groups,
        "excluded_short_groups": excluded_groups,
        "trial_labels": [str(info["label"]) for info in infos],
        "settings": settings,
        "tasks": tasks,
    }
    write_json(args.run_dir / "manifest.json", manifest)
    write_json(args.run_dir / "search_config.json", config)
    (args.run_dir / "task_count.txt").write_text(f"{len(tasks)}\n", encoding="ascii")
    print(f"Prepared {len(settings)} settings x {len(groups)} outer folds = {len(tasks)} tasks")
    print(f"Manifest: {args.run_dir / 'manifest.json'}")
    print(f"PBS array range: 0-{len(tasks) - 1}")
    return 0


def _odd_window(window_ms: float, sample_dt: float, polynomial_order: int, length: int) -> int:
    requested = max(polynomial_order + 2, int(round(window_ms * 1.0e-3 / sample_dt)))
    if requested % 2 == 0:
        requested += 1
    maximum = length if length % 2 == 1 else length - 1
    window = min(requested, maximum)
    if window <= polynomial_order:
        raise ValueError(f"trajectory too short for Savitzky-Golay order {polynomial_order}")
    return window


def load_trial_full(info: dict[str, object], setting: dict[str, Any]) -> Trial:
    downsample = int(setting["downsample"])
    sample_dt = DT_RAW * downsample
    data = np.load(Path(info["npz"]), allow_pickle=False)
    if abs(float(data["dt"]) - DT_RAW) > DT_RAW * 0.05:
        raise ValueError(f"unexpected native dt in {info['npz']}: {float(data['dt'])}")
    measured = block_mean(np.asarray(data["measured_pat_mm"], dtype=float), downsample)
    ideal = block_mean(np.asarray(data["ideal_pat_mm"], dtype=float), downsample)
    max_sec = float(setting.get("max_sec", 0.0))
    if max_sec > 0:
        length = min(len(measured), int(max_sec / sample_dt))
        measured, ideal = measured[:length], ideal[:length]
    measured = measured - measured[0]
    ideal = ideal - ideal[0]
    error = measured - ideal

    velocity_order = int(setting["velocity_polyorder"])
    control_order = int(setting["control_polyorder"])
    velocity_window = _odd_window(float(setting["velocity_window_ms"]), sample_dt, velocity_order, len(error))
    control_window = _odd_window(float(setting["control_window_ms"]), sample_dt, control_order, len(error))
    error_velocity = savgol_filter(error, velocity_window, velocity_order, deriv=1, delta=sample_dt, axis=0)
    control_velocity = savgol_filter(ideal, control_window, control_order, deriv=1, delta=sample_dt, axis=0)
    control_acceleration = savgol_filter(ideal, control_window, control_order, deriv=2, delta=sample_dt, axis=0)
    calibration_count = min(len(error), max(1, int(float(setting["dc_calib_sec"]) / sample_dt)))
    dc = np.mean(error[:calibration_count], axis=0)
    return Trial(
        label=str(info["label"]),
        group=str(info["group"]),
        scale=str(info["scale"]),
        e=error,
        ctrl=ideal,
        e_dot=error_velocity,
        u_dot=control_velocity,
        u_ddot=control_acceleration,
        dc=dc,
    )


def build_problem(base_dir: Path, setting: dict[str, Any]) -> tuple[
    list[Trial],
    list[str],
    list[list[FeatureSpec]],
    list[dict[str, tuple[np.ndarray, np.ndarray]]],
    float,
]:
    infos = discover_trials(base_dir)
    sample_dt = DT_RAW * int(setting["downsample"])
    trials_all = [load_trial_full(info, setting) for info in infos]
    libraries = [build_candidate_library(str(setting["library_tier"]), axis) for axis in range(3)]
    half_width = max(2, int(round(float(setting["weak_half_width_ms"]) * 1.0e-3 / sample_dt)))
    stride = max(1, int(round(float(setting["weak_stride_ms"]) * 1.0e-3 / sample_dt)))
    min_len = 2 * half_width + 1
    trials = [trial for trial in trials_all if trial.n > min_len]
    skipped = [trial.label for trial in trials_all if trial.n <= min_len]
    if skipped:
        print(
            f"skip {len(skipped)} trial(s) shorter than weak window "
            f"(half_width={half_width} samples @ downsample={setting['downsample']}): {', '.join(skipped)}"
        )
    groups = sorted({trial.group for trial in trials})
    phi, dphi, d2phi = build_weak_test_function(
        half_width,
        int(setting["weak_polynomial_order"]),
        sample_dt,
    )
    per_axis_group: list[dict[str, tuple[np.ndarray, np.ndarray]]] = []
    for axis in range(3):
        grouped: dict[str, list[tuple[np.ndarray, np.ndarray]]] = defaultdict(list)
        for trial in trials:
            grouped[trial.group].append(
                build_weak_matrix(
                    trial,
                    axis,
                    libraries[axis],
                    phi,
                    dphi,
                    d2phi,
                    sample_dt,
                    stride,
                )
            )
        combined: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        for group, entries in grouped.items():
            combined[group] = (
                np.vstack([entry[0] for entry in entries]),
                np.concatenate([entry[1] for entry in entries]),
            )
        per_axis_group.append(combined)
    return trials, groups, libraries, per_axis_group, sample_dt


def combine_groups(
    grouped: dict[str, tuple[np.ndarray, np.ndarray]], groups: Sequence[str]
) -> tuple[np.ndarray, np.ndarray]:
    return (
        np.vstack([grouped[group][0] for group in groups]),
        np.concatenate([grouped[group][1] for group in groups]),
    )


def fit_axes(
    grouped_by_axis: list[dict[str, tuple[np.ndarray, np.ndarray]]],
    libraries: list[list[FeatureSpec]],
    train_groups: Sequence[str],
    setting: dict[str, Any],
    seed: int,
) -> tuple[list[SparseFit], list[dict[str, Any]]]:
    fits: list[SparseFit] = []
    diagnostics: list[dict[str, Any]] = []
    correlation_threshold = setting.get("correlation_threshold")
    for axis in range(3):
        grouped = grouped_by_axis[axis]
        full_design, full_target = combine_groups(grouped, train_groups)
        allowed, decisions = correlation_mask(full_design, libraries[axis], correlation_threshold)
        designs = {group: grouped[group][0] for group in train_groups}
        targets = {group: grouped[group][1] for group in train_groups}
        fit = stability_selection(
            designs,
            targets,
            train_groups,
            method=str(setting["optimizer"]),
            alpha=float(setting["ridge_alpha"]),
            threshold=float(setting["threshold"]),
            sparsity=int(setting["sparsity"]),
            allowed=allowed,
            repeats=int(setting["bootstrap_repeats"]),
            frequency_threshold=float(setting["stability_threshold"]),
            seed=seed + axis,
        )
        axis_diagnostics: dict[str, Any] = regression_diagnostics(full_design, allowed)
        axis_diagnostics.update(
            axis=AXES[axis],
            candidates=len(libraries[axis]),
            allowed=int(np.sum(allowed)),
            selected=int(np.sum(fit.support)),
            correlation_pruning=decisions,
        )
        diagnostics.append(axis_diagnostics)
        fits.append(fit)
    return fits, diagnostics


def validation_trials(trials: Sequence[Trial], group: str, limit: int) -> list[Trial]:
    selected = [trial for trial in trials if trial.group == group]
    selected.sort(key=lambda trial: (trial.scale != "orig", trial.scale, trial.label))
    return selected[:limit] if limit > 0 else selected


def rollout_ratio(
    trial: Trial,
    libraries: list[list[FeatureSpec]],
    fits: list[SparseFit],
    sample_dt: float,
    selection: dict[str, Any],
    horizon_sec: float,
) -> tuple[float, bool, float, float]:
    start = min(int(float(selection["rollout_start_sec"]) / sample_dt), trial.n - 2)
    steps = min(int(horizon_sec / sample_dt), trial.n - start - 1)
    if steps < 2:
        return math.inf, True, math.nan, math.nan
    prediction = simulate_full_model(
        trial,
        libraries,
        fits,
        start,
        steps,
        sample_dt,
        float(selection["divergence_mm"]),
    )
    true_error = trial.e[start : start + steps + 1]
    baseline = float(np.sqrt(np.mean(np.sum((true_error - trial.dc) ** 2, axis=1))))
    if prediction is None:
        return float(selection["divergence_penalty"]), True, math.nan, baseline
    rmse = float(np.sqrt(np.mean(np.sum((true_error - prediction) ** 2, axis=1))))
    return rmse / max(baseline, 1.0e-12), False, rmse, baseline


def run_inner_cv_task(
    task: dict[str, Any],
    setting: dict[str, Any],
    base_dir: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    trials, groups, libraries, grouped_by_axis, sample_dt = build_problem(base_dir, setting)
    outer_group = str(task["outer_group"])
    if outer_group not in groups:
        raise ValueError(f"outer group not in current data: {outer_group}")
    outer_train = [group for group in groups if group != outer_group]
    selection = config["selection"]
    eligible = eligible_outer_groups(base_dir, selection)
    inner_candidates = [group for group in outer_train if group in eligible]
    fold_records: list[dict[str, Any]] = []
    weak_scores: list[float] = []
    rollout_scores: list[float] = []
    selected_counts: list[int] = []
    diverged_count = 0

    for inner_fold, inner_group in enumerate(inner_candidates):
        inner_train = [group for group in outer_train if group != inner_group]
        seed = int(config["search"]["seed"]) + int(task["task_index"]) * 101 + inner_fold * 7
        fits, diagnostics = fit_axes(grouped_by_axis, libraries, inner_train, setting, seed)
        fold_weak: list[float] = []
        for axis in range(3):
            design, target = grouped_by_axis[axis][inner_group]
            fold_weak.append(normalized_weak_rmse(design, target, fits[axis].coefficients))
            selected_counts.append(int(np.sum(fits[axis].support)))
        selected_trials = validation_trials(
            trials,
            inner_group,
            int(selection["rollout_trials_per_group"]),
        )
        fold_rollout: list[float] = []
        for trial in selected_trials:
            ratio, diverged, _, _ = rollout_ratio(
                trial,
                libraries,
                fits,
                sample_dt,
                selection,
                float(selection["inner_rollout_sec"]),
            )
            fold_rollout.append(ratio)
            diverged_count += int(diverged)
        weak_scores.extend(fold_weak)
        rollout_scores.extend(fold_rollout)
        fold_records.append(
            {
                "inner_group": inner_group,
                "weak_nrmse": fold_weak,
                "rollout_ratio": fold_rollout,
                "diagnostics": diagnostics,
            }
        )

    weak_score = float(np.median(weak_scores))
    rollout_score = float(np.median(rollout_scores)) if rollout_scores else 0.0
    mean_terms = float(np.mean(selected_counts))
    score = (
        float(selection["weak_weight"]) * math.log1p(min(weak_score, float(selection["score_cap"])))
        + float(selection["rollout_weight"]) * math.log1p(min(rollout_score, float(selection["score_cap"])))
        + float(selection["complexity_weight"]) * mean_terms
    )
    return {
        "schema_version": 1,
        "task": task,
        "setting": setting,
        "score": score,
        "weak_nrmse_median": weak_score,
        "rollout_ratio_median": rollout_score,
        "mean_selected_terms": mean_terms,
        "diverged_rollouts": diverged_count,
        "inner_folds": fold_records,
    }


def run_task(args: argparse.Namespace) -> int:
    manifest = read_json(args.run_dir / "manifest.json")
    config = read_json(args.run_dir / "search_config.json")
    task_index = args.task_index
    if task_index is None:
        value = os.environ.get("PBS_ARRAY_INDEX") or os.environ.get("PBS_ARRAYID")
        if value is None:
            raise SystemExit("provide --task-index or run inside a PBS array job")
        task_index = int(value)
    tasks = manifest["tasks"]
    if task_index < 0 or task_index >= len(tasks):
        raise SystemExit(f"task index outside 0-{len(tasks) - 1}: {task_index}")
    output = args.run_dir / "tasks" / f"task_{task_index:06d}.json"
    if output.exists() and not args.force:
        print(f"Already complete: {output}")
        return 0
    task = tasks[task_index]
    setting = manifest["settings"][int(task["config_index"])]
    print(
        f"Task {task_index}/{len(tasks) - 1}: outer={task['outer_group']} "
        f"setting={task['setting_id']} tier={setting['library_tier']} optimizer={setting['optimizer']}"
    )
    result = run_inner_cv_task(task, setting, Path(manifest["base_dir"]), config)
    write_json(output, result)
    print(
        f"score={result['score']:.6g} weak={result['weak_nrmse_median']:.4g} "
        f"rollout={result['rollout_ratio_median']:.4g} terms={result['mean_selected_terms']:.2f}"
    )
    return 0


def load_task_results(run_dir: Path) -> list[dict[str, Any]]:
    return [read_json(path) for path in sorted((run_dir / "tasks").glob("task_*.json"))]


def aggregate_results(args: argparse.Namespace) -> int:
    manifest = read_json(args.run_dir / "manifest.json")
    results = load_task_results(args.run_dir)
    expected = len(manifest["tasks"])
    if len(results) != expected and not args.allow_incomplete:
        raise SystemExit(f"only {len(results)}/{expected} tasks are complete; use --allow-incomplete to inspect")
    if not results:
        raise SystemExit("no completed tasks")
    by_outer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_setting: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        by_outer[str(result["task"]["outer_group"])].append(result)
        by_setting[str(result["task"]["setting_id"])].append(result)

    selected_outer: list[dict[str, Any]] = []
    ranking_rows: list[dict[str, Any]] = []
    for outer_group, records in sorted(by_outer.items()):
        records.sort(key=lambda record: (float(record["score"]), float(record["mean_selected_terms"])))
        best = records[0]
        selected_outer.append(
            {
                "outer_fold": int(best["task"]["outer_fold"]),
                "outer_group": outer_group,
                "task_index": int(best["task"]["task_index"]),
                "setting_id": str(best["task"]["setting_id"]),
                "score": float(best["score"]),
                "setting": best["setting"],
            }
        )
        for rank, record in enumerate(records, start=1):
            ranking_rows.append(
                {
                    "outer_group": outer_group,
                    "rank": rank,
                    "task_index": record["task"]["task_index"],
                    "setting_id": record["task"]["setting_id"],
                    "score": record["score"],
                    "weak_nrmse_median": record["weak_nrmse_median"],
                    "rollout_ratio_median": record["rollout_ratio_median"],
                    "mean_selected_terms": record["mean_selected_terms"],
                    "diverged_rollouts": record["diverged_rollouts"],
                }
            )

    complete_settings = [
        records for records in by_setting.values() if len(records) == len(manifest["groups"])
    ]
    deployment: dict[str, Any] | None = None
    if complete_settings:
        averaged = sorted(
            complete_settings,
            key=lambda records: (
                float(np.mean([record["score"] for record in records])),
                float(np.mean([record["mean_selected_terms"] for record in records])),
            ),
        )
        records = averaged[0]
        deployment = {
            "setting_id": records[0]["task"]["setting_id"],
            "mean_inner_score": float(np.mean([record["score"] for record in records])),
            "setting": records[0]["setting"],
        }
    selected = {
        "schema_version": 1,
        "completed_tasks": len(results),
        "expected_tasks": expected,
        "outer_folds": selected_outer,
        "deployment": deployment,
    }
    write_json(args.run_dir / "selected_settings.json", selected)
    write_csv(args.run_dir / "inner_cv_ranking.csv", ranking_rows)
    print(f"Selected settings for {len(selected_outer)}/{len(manifest['groups'])} outer folds")
    if deployment:
        print(f"Deployment setting: {deployment['setting_id']} mean score={deployment['mean_inner_score']:.6g}")
    else:
        print("Deployment setting unavailable until every setting has all outer-fold results")
    return 0


def serialize_fit(axis: int, library: Sequence[FeatureSpec], fit: SparseFit) -> dict[str, Any]:
    selected = []
    for feature, coefficient, frequency in zip(library, fit.coefficients, fit.selection_frequency):
        if abs(coefficient) > 1.0e-14:
            selected.append(
                {
                    "feature": feature.to_dict(),
                    "coefficient": float(coefficient),
                    "selection_frequency": float(frequency),
                }
            )
    return {
        "axis": AXES[axis],
        "equation": format_equation(axis, library, fit),
        "selected_terms": selected,
        "nonzero_selection_frequencies": [
            {"feature": feature.name, "selection_frequency": float(frequency)}
            for feature, frequency in zip(library, fit.selection_frequency)
            if frequency > 0.0
        ],
        "candidate_count": len(library),
    }


def evaluate_fold(
    fold_record: dict[str, Any],
    base_dir: Path,
    config: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    setting = fold_record["setting"]
    trials, groups, libraries, grouped_by_axis, sample_dt = build_problem(base_dir, setting)
    held_out = str(fold_record["outer_group"])
    train_groups = [group for group in groups if group != held_out]
    seed = int(config["search"]["seed"]) + int(fold_record["outer_fold"]) * 100003
    fits, diagnostics = fit_axes(grouped_by_axis, libraries, train_groups, setting, seed)
    weak_scores = [
        normalized_weak_rmse(
            grouped_by_axis[axis][held_out][0],
            grouped_by_axis[axis][held_out][1],
            fits[axis].coefficients,
        )
        for axis in range(3)
    ]
    selection = config["selection"]
    metrics: list[dict[str, Any]] = []
    for trial in validation_trials(trials, held_out, 0):
        ratio, diverged, rmse, baseline = rollout_ratio(
            trial,
            libraries,
            fits,
            sample_dt,
            selection,
            float(selection["outer_rollout_sec"]),
        )
        metrics.append(
            {
                "outer_fold": fold_record["outer_fold"],
                "held_out_group": held_out,
                "label": trial.label,
                "scale": trial.scale,
                "rollout_rmse_over_no_model": ratio,
                "rollout_rmse_mm": rmse,
                "no_model_rmse_mm": baseline,
                "diverged": diverged,
                "weak_nrmse_x": weak_scores[0],
                "weak_nrmse_y": weak_scores[1],
                "weak_nrmse_z": weak_scores[2],
            }
        )
    model = {
        "outer_fold": fold_record["outer_fold"],
        "held_out_group": held_out,
        "setting_id": fold_record["setting_id"],
        "setting": setting,
        "weak_nrmse": dict(zip(AXES, weak_scores)),
        "diagnostics": diagnostics,
        "equations": [serialize_fit(axis, libraries[axis], fits[axis]) for axis in range(3)],
    }
    return model, metrics


def fit_deployment_model(
    deployment: dict[str, Any],
    base_dir: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    setting = deployment["setting"]
    _, groups, libraries, grouped_by_axis, _ = build_problem(base_dir, setting)
    fits, diagnostics = fit_axes(
        grouped_by_axis,
        libraries,
        groups,
        setting,
        int(config["search"]["seed"]) + 9000001,
    )
    return {
        "setting_id": deployment["setting_id"],
        "selection_basis": "mean nested inner-CV score; all groups used only for final coefficient refit",
        "setting": setting,
        "training_groups": groups,
        "diagnostics": diagnostics,
        "equations": [serialize_fit(axis, libraries[axis], fits[axis]) for axis in range(3)],
    }


def save_evaluation_plot(path: Path, metrics: list[dict[str, Any]]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    by_group: dict[str, list[float]] = defaultdict(list)
    for row in metrics:
        by_group[str(row["held_out_group"])].append(float(row["rollout_rmse_over_no_model"]))
    names = list(sorted(by_group))
    values = [float(np.median(by_group[name])) for name in names]
    figure, axis = plt.subplots(figsize=(max(9.0, len(names) * 1.3), 5.0))
    bars = axis.bar(range(len(names)), values, color="#2563eb")
    axis.axhline(1.0, color="#111827", linestyle="--", linewidth=1.2, label="No-model error")
    axis.set_xticks(range(len(names)), [name.replace("long_random_3d_", "") for name in names], rotation=25, ha="right")
    axis.set_ylabel("Rollout RMSE / no-model RMSE")
    axis.set_title("Nested outer-CV Full SINDy rollout")
    axis.grid(axis="y", linestyle=":", alpha=0.45)
    axis.legend()
    for bar, value in zip(bars, values):
        axis.text(bar.get_x() + bar.get_width() / 2.0, value, f"{value:.2f}", ha="center", va="bottom")
    figure.tight_layout()
    figure.savefig(path, dpi=180)
    plt.close(figure)


def evaluate_selected(args: argparse.Namespace) -> int:
    output = args.run_dir / "outer_cv_models.json"
    if output.exists() and not args.force:
        print(f"Already complete: {output}")
        return 0
    manifest = read_json(args.run_dir / "manifest.json")
    config = read_json(args.run_dir / "search_config.json")
    selected = read_json(args.run_dir / "selected_settings.json")
    if int(selected["completed_tasks"]) != int(selected["expected_tasks"]):
        raise SystemExit("refusing outer evaluation: hyperparameter task set is incomplete")
    expected_groups = set(manifest["groups"])
    selected_groups = {fold["outer_group"] for fold in selected["outer_folds"]}
    if selected_groups != expected_groups:
        raise SystemExit("outer-fold selection is incomplete; finish tasks before unbiased evaluation")
    models: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    for fold in sorted(selected["outer_folds"], key=lambda item: int(item["outer_fold"])):
        print(f"Evaluating outer fold {fold['outer_fold']}: {fold['outer_group']}")
        model, fold_metrics = evaluate_fold(fold, Path(manifest["base_dir"]), config)
        models.append(model)
        metrics.extend(fold_metrics)
    write_json(output, {"schema_version": 1, "models": models})
    write_csv(args.run_dir / "outer_cv_metrics.csv", metrics)
    save_evaluation_plot(args.run_dir / "outer_cv_rollout.png", metrics)

    deployment = selected.get("deployment")
    if deployment is not None:
        final_model = fit_deployment_model(deployment, Path(manifest["base_dir"]), config)
        write_json(args.run_dir / "final_model.json", final_model)
        equations = "\n".join(item["equation"] for item in final_model["equations"])
        (args.run_dir / "final_equations.txt").write_text(equations + "\n", encoding="utf-8")
    valid = [float(row["rollout_rmse_over_no_model"]) for row in metrics if not row["diverged"]]
    summary = {
        "runs": len(metrics),
        "diverged": int(sum(bool(row["diverged"]) for row in metrics)),
        "median_rollout_rmse_over_no_model": float(np.median(valid)) if valid else math.nan,
        "improves_over_no_model": bool(valid and np.median(valid) < 1.0),
    }
    write_json(args.run_dir / "outer_cv_summary.json", summary)
    print(json.dumps(summary, indent=2))
    return 0


def describe_library(tier: str) -> int:
    for axis in range(3):
        library = build_candidate_library(tier, axis)
        print(f"[{AXES[axis]}] {tier}: {len(library)} candidates")
        for index, feature in enumerate(library):
            print(f"  {index:3d} {feature.name}")
    return 0


def main() -> int:
    args = parse_args()
    if args.command == "prepare":
        return prepare_manifest(args)
    if args.command == "run-task":
        return run_task(args)
    if args.command == "aggregate":
        return aggregate_results(args)
    if args.command == "evaluate":
        return evaluate_selected(args)
    if args.command == "describe-library":
        return describe_library(args.tier)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
