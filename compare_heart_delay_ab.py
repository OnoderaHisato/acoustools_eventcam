#!/usr/bin/env python3
"""Aggregate postprocessed heart baseline/delay runs from one A/B session."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

import numpy as np


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_manifest", type=Path)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--include-first-cycle",
        action="store_true",
        help="Include initialization cycle; by default t_ideal < 1/f is excluded.",
    )
    return parser.parse_args()


def quantile(values: np.ndarray, probability: float) -> float:
    return float(np.quantile(np.asarray(values, dtype=float), probability))


def load_run_metrics(
    run_dir: Path,
    frequency_hz: float,
    exclude_first_cycle: bool,
) -> dict[str, Any]:
    pipeline = json.loads((run_dir / "pipeline_manifest.json").read_text(encoding="utf-8"))
    comparison_csv = run_dir / "ideal_comparison_3d" / "stereo_ideal_comparison.csv"
    summary_path = run_dir / "ideal_comparison_3d" / "stereo_ideal_comparison_summary.json"
    if not comparison_csv.is_file() or not summary_path.is_file():
        raise FileNotFoundError(f"Postprocess output is incomplete: {run_dir}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    rows: list[dict[str, str]] = []
    with comparison_csv.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            included = str(row.get("included_in_metrics", "")).lower() in {"1", "true", "yes"}
            if not included:
                continue
            ideal_time = float(row["t_ideal_sec"])
            if exclude_first_cycle and ideal_time < 1.0 / frequency_hz:
                continue
            rows.append(row)
    if not rows:
        raise ValueError(f"No comparison samples remain after filtering: {run_dir}")
    errors = {
        "x": np.asarray([float(row["error_x_mm"]) for row in rows]),
        "y": np.asarray([float(row["error_y_mm"]) for row in rows]),
        "z": np.asarray([float(row["error_z_mm"]) for row in rows]),
        "norm": np.asarray([float(row["error_norm_mm"]) for row in rows]),
    }
    metrics: dict[str, Any] = {"samples": len(rows)}
    for axis, values in errors.items():
        metrics[f"rmse_{axis}_mm"] = float(np.sqrt(np.mean(values**2)))
        metrics[f"p95_{axis}_mm"] = quantile(np.abs(values), 0.95)
    control = pipeline.get("control", {})
    return {
        "metrics": metrics,
        "spatial_alignment": summary.get("spatial_alignment", ""),
        "control_mode": control.get("mode", ""),
        "effective_tau_ms": 1000.0
        * float(control.get("feedforward", {}).get("effective_tau_sec", 0.0)),
        "limited_fraction": float(
            control.get("feedforward", {}).get("limited_fraction", 0.0)
        ),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_plot(path: Path, rows: list[dict[str, Any]]) -> None:
    import matplotlib.pyplot as plt

    pairs: dict[int, dict[str, float]] = {}
    for row in rows:
        pairs.setdefault(int(row["repeat_number"]), {})[str(row["condition"])] = float(
            row["rmse_norm_mm"]
        )
    figure, axis = plt.subplots(figsize=(7.2, 4.6), constrained_layout=True)
    for repeat, values in sorted(pairs.items()):
        if {"baseline", "delay"}.issubset(values):
            axis.plot(
                [0, 1],
                [values["baseline"], values["delay"]],
                marker="o",
                linewidth=1.3,
                label=f"repeat {repeat}",
            )
    axis.set_xticks([0, 1], ["Baseline", "Delay feedforward"])
    axis.set_ylabel("3D tracking RMSE [mm]")
    axis.grid(axis="y", alpha=0.3)
    axis.legend()
    figure.savefig(path, dpi=180)
    plt.close(figure)


def main() -> int:
    args = parse_args()
    session_path = args.session_manifest.resolve()
    session = json.loads(session_path.read_text(encoding="utf-8"))
    frequency_hz = float(session["heart_frequency_hz"])
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else session_path.parent / f"{session_path.stem}_comparison"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    trial_rows: list[dict[str, Any]] = []
    for run in session["runs"]:
        if int(run.get("exit_code", 1)) != 0:
            continue
        run_dir = Path(str(run["run_dir"])).resolve()
        loaded = load_run_metrics(
            run_dir,
            frequency_hz=frequency_hz,
            exclude_first_cycle=not bool(args.include_first_cycle),
        )
        if loaded["control_mode"] != run["condition"] and not (
            loaded["control_mode"] == "delay_feedforward" and run["condition"] == "delay"
        ):
            raise ValueError(f"Control condition mismatch in {run_dir}")
        trial_rows.append(
            {
                "sequence_index": int(run["sequence_index"]),
                "repeat_number": int(run["repeat_number"]),
                "condition": str(run["condition"]),
                **loaded["metrics"],
                "effective_tau_ms": loaded["effective_tau_ms"],
                "limited_fraction": loaded["limited_fraction"],
                "spatial_alignment": loaded["spatial_alignment"],
                "run_dir": str(run_dir),
            }
        )
    if not trial_rows:
        raise SystemExit("No successfully postprocessed A/B runs were found.")
    write_csv(output_dir / "heart_delay_ab_trials.csv", trial_rows)

    paired_rows: list[dict[str, Any]] = []
    for repeat in sorted({int(row["repeat_number"]) for row in trial_rows}):
        by_condition = {
            str(row["condition"]): row
            for row in trial_rows
            if int(row["repeat_number"]) == repeat
        }
        if not {"baseline", "delay"}.issubset(by_condition):
            continue
        baseline = float(by_condition["baseline"]["rmse_norm_mm"])
        delay = float(by_condition["delay"]["rmse_norm_mm"])
        paired_rows.append(
            {
                "repeat_number": repeat,
                "baseline_rmse_norm_mm": baseline,
                "delay_rmse_norm_mm": delay,
                "improvement_pct": 100.0 * (baseline - delay) / baseline,
            }
        )
    if not paired_rows:
        raise SystemExit("No complete baseline/delay pair was found.")
    write_csv(output_dir / "heart_delay_ab_pairs.csv", paired_rows)
    improvement = np.asarray([row["improvement_pct"] for row in paired_rows], dtype=float)
    summary = {
        "schema_version": 1,
        "source_session": str(session_path),
        "pairs": len(paired_rows),
        "first_cycle_excluded": not bool(args.include_first_cycle),
        "primary_error": "measured_position - original_heart_reference",
        "mean_baseline_rmse_norm_mm": float(
            np.mean([row["baseline_rmse_norm_mm"] for row in paired_rows])
        ),
        "mean_delay_rmse_norm_mm": float(
            np.mean([row["delay_rmse_norm_mm"] for row in paired_rows])
        ),
        "mean_paired_improvement_pct": float(np.mean(improvement)),
        "std_paired_improvement_pct": float(np.std(improvement, ddof=1))
        if len(improvement) > 1
        else 0.0,
        "interpretation": (
            "Empirical zero-shot A/B result for this apparatus and trajectory; "
            "not a deterministic error guarantee."
        ),
    }
    (output_dir / "heart_delay_ab_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    write_plot(output_dir / "heart_delay_ab_rmse.png", trial_rows)
    print(json.dumps(summary, indent=2))
    print(f"[A/B] Comparison written: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
