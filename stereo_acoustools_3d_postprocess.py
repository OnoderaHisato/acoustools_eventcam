#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run deferred stereo particle tracking, 3D triangulation, and ideal comparison."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from stereo_acoustools_3d_common import PROCESSING_CONFIG_FIELDS
from stereo_acoustools_3d_postprocess_core import (
    processing_status,
    run_postprocess,
    validate_run_dir,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Postprocess one or more completed AcousTools stereo capture directories. "
            "No camera or PAT connection is opened."
        )
    )
    parser.add_argument("run_dirs", nargs="+", type=Path)
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="Continue with later run directories if one postprocess job fails.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Skip already-complete run directories and reuse a completed left/right "
            "tracking stage when its input and processing settings match."
        ),
    )
    suppress = argparse.SUPPRESS
    parser.add_argument("--window-us", type=int, default=suppress)
    parser.add_argument("--hop-us", type=int, default=suppress)
    parser.add_argument("--dt-us", type=float, default=suppress)
    parser.add_argument("--max-interp-gap-sec", type=float, default=suppress)
    parser.add_argument("--max-step-px", type=float, default=suppress)
    parser.add_argument("--roi", default=suppress)
    parser.add_argument("--tracking-method", default=suppress)
    parser.add_argument("--threshold-count", type=int, default=suppress)
    parser.add_argument("--min-events", type=int, default=suppress)
    parser.add_argument("--min-area", type=int, default=suppress)
    parser.add_argument("--min-mass", type=int, default=suppress)
    parser.add_argument("--polarity", choices=["all", "on", "off"], default=suppress)
    parser.add_argument("--max-time-gap-sec", type=float, default=suppress)
    parser.add_argument("--max-reprojection-error-px", type=float, default=suppress)
    parser.add_argument("--render-overlay", action="store_true", default=suppress)
    parser.add_argument("--auto-time-search-sec", type=float, default=suppress)
    parser.add_argument("--auto-time-step-sec", type=float, default=suppress)
    parser.add_argument("--refine-led-time", action="store_true", default=suppress)
    parser.add_argument("--camera-to-pat-transform", type=Path, default=suppress)
    tokens = list(sys.argv[1:] if argv is None else argv)
    # Compatibility with the short-lived wrapper syntax used before this
    # script owned postprocessing directly.
    tokens = [token for token in tokens if token != "--pipeline-args"]
    return parser.parse_args(tokens)


def main() -> int:
    args = parse_args()
    override_names = set(PROCESSING_CONFIG_FIELDS) | {"camera_to_pat_transform"}
    overrides = {name: value for name, value in vars(args).items() if name in override_names}
    failures = 0
    for index, requested_run_dir in enumerate(args.run_dirs, start=1):
        try:
            run_dir = validate_run_dir(requested_run_dir)
            previous_status = processing_status(run_dir)
            if args.resume and previous_status == "complete":
                stereo_npz = run_dir / "stereo_recording" / "stereo_3d" / "stereo_3d_points.npz"
                comparison_summary = (
                    run_dir / "ideal_comparison_3d" / "stereo_ideal_comparison_summary.json"
                )
                if stereo_npz.exists() and comparison_summary.exists():
                    print(
                        f"\n[POSTPROCESS] Job {index}/{len(args.run_dirs)}: {run_dir} "
                        "(already complete; skipped by --resume)",
                        flush=True,
                    )
                    continue
                print(
                    f"\n[POSTPROCESS][WARN] {run_dir} is marked complete but a final "
                    "artifact is missing; processing it again.",
                    flush=True,
                )
            print(
                f"\n[POSTPROCESS] Job {index}/{len(args.run_dirs)}: {run_dir} "
                f"(previous status={previous_status})",
                flush=True,
            )
            run_postprocess(run_dir, overrides, resume=args.resume)
        except Exception as exc:
            failures += 1
            print(f"[POSTPROCESS][ERROR] {requested_run_dir}: {exc}", file=sys.stderr, flush=True)
            if not args.keep_going:
                return 1
    if failures:
        print(f"[POSTPROCESS] Finished with {failures} failed job(s).", file=sys.stderr)
        return 1
    print(f"\n[POSTPROCESS] All {len(args.run_dirs)} job(s) completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
