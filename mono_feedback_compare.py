"""Compare two mono-feedback runs using their lightweight control logs."""

from __future__ import annotations

import argparse
import csv
import datetime
import json
from pathlib import Path

import numpy as np


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare mono X baseline and feedback runs.")
    parser.add_argument("baseline", type=Path)
    parser.add_argument("feedback", type=Path)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def load_run(path: Path) -> tuple[dict[str, object], dict[str, np.ndarray]]:
    run_dir = path if path.is_dir() else path.parent
    csv_path = path if path.is_file() else run_dir / "control_log.csv"
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"Run manifest does not exist: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not csv_path.exists():
        raise SystemExit(
            "Run has no control_log.csv and cannot be compared.\n"
            f"Run: {run_dir}\n"
            f"status={manifest.get('status')!r}\n"
            f"abort_reason={manifest.get('abort_reason')!r}\n"
            f"control_samples={manifest.get('control_samples')!r}\n"
            "Reacquire a run that completes particle lock and records control samples."
        )
    rows = list(csv.DictReader(csv_path.open(newline="", encoding="utf-8")))
    if not rows:
        raise SystemExit(
            f"Run control_log.csv is empty and cannot be compared: {csv_path}\n"
            f"status={manifest.get('status')!r}, abort_reason={manifest.get('abort_reason')!r}"
        )

    def column(name: str) -> np.ndarray:
        return np.asarray(
            [float(row[name]) if str(row.get(name, "")).strip() else np.nan for row in rows],
            dtype=float,
        )

    data = {
        "sample": column("sample"),
        "measured_x_mm": column("measured_x_mm"),
        "trap_x_mm": column("trap_x_mm"),
        "valid": column("valid"),
        "sent": column("sent"),
        "send_us": column("send_us"),
        "processing_us": column("processing_us"),
    }
    data["time_sec"] = data["sample"] / float(manifest["pid"]["sample_hz"])
    return manifest, data


def summarize(manifest: dict[str, object], data: dict[str, np.ndarray]) -> dict[str, object]:
    x = data["measured_x_mm"]
    valid = np.isfinite(x)
    x_valid = x[valid]
    send = data["send_us"][data["sent"] > 0]
    processing = data["processing_us"][np.isfinite(data["processing_us"])]
    return {
        "run_status": manifest.get("status"),
        "abort_reason": manifest.get("abort_reason"),
        "pid": manifest.get("pid"),
        "samples": int(x.size),
        "valid_fraction": float(valid.mean()) if valid.size else 0.0,
        "position_rms_mm": float(np.sqrt(np.mean(x_valid**2))) if x_valid.size else None,
        "position_std_mm": float(np.std(x_valid)) if x_valid.size else None,
        "position_p95_abs_mm": float(np.percentile(np.abs(x_valid), 95)) if x_valid.size else None,
        "position_max_abs_mm": float(np.max(np.abs(x_valid))) if x_valid.size else None,
        "send_count": int(send.size),
        "send_p95_us": float(np.percentile(send, 95)) if send.size else None,
        "processing_p95_us": float(np.percentile(processing, 95)) if processing.size else None,
    }


def main() -> int:
    args = parse_args()
    baseline_manifest, baseline_data = load_run(args.baseline)
    feedback_manifest, feedback_data = load_run(args.feedback)
    baseline_summary = summarize(baseline_manifest, baseline_data)
    feedback_summary = summarize(feedback_manifest, feedback_data)
    baseline_rms = baseline_summary["position_rms_mm"]
    feedback_rms = feedback_summary["position_rms_mm"]
    ratio = None
    if baseline_rms not in {None, 0.0} and feedback_rms is not None:
        ratio = float(feedback_rms) / float(baseline_rms)

    if args.output_dir is None:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = Path("mono_feedback_records") / f"comparison_{stamp}"
    else:
        output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=False)
    payload = {
        "baseline": baseline_summary,
        "feedback": feedback_summary,
        "feedback_over_baseline_rms": ratio,
        "rms_reduction_percent": None if ratio is None else 100.0 * (1.0 - ratio),
    }
    (output_dir / "comparison.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=False)
    for label, data in (("baseline", baseline_data), ("feedback", feedback_data)):
        axes[0].plot(data["time_sec"], data["measured_x_mm"], lw=0.8, label=label)
        axes[1].plot(data["time_sec"], data["trap_x_mm"], lw=0.8, label=label)
    axes[0].set_ylabel("measured X [mm]")
    axes[1].set_ylabel("trap X [mm]")
    axes[1].set_xlabel("time [s]")
    for axis in axes:
        axis.grid(ls=":", alpha=0.5)
        axis.legend()
    figure.tight_layout()
    figure.savefig(output_dir / "comparison.png", dpi=160)
    plt.close(figure)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"Output: {output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
