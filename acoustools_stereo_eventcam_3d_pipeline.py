#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""All-in-one AcousTools stereo recording and 3D postprocessing entry point."""

from __future__ import annotations

import sys
from pathlib import Path

from stereo_acoustools_3d_common import (
    PROCESSING_CONFIG_FIELDS,
    apply_manifest_processing_config,
)
from stereo_acoustools_3d_postprocess_core import run_postprocess
from stereo_acoustools_3d_recording_core import (
    build_recording_parser,
    compute_led_search_half_window_sec,
    promote_capture_attempt,
    remap_attempt_result_paths,
    remove_capture_attempt,
    remove_failed_run,
    run_recording,
    wait_for_capture_marker,
)


def _run_process_only_compatibility(tokens: list[str]) -> int:
    index = tokens.index("--process-only")
    if index + 1 >= len(tokens):
        raise SystemExit("--process-only requires a pipeline run directory.")
    run_dir = Path(tokens[index + 1]).resolve()
    forwarded = tokens[:index] + tokens[index + 2 :]

    # Preserve the former pipeline CLI for existing commands. Only explicit
    # processing options override the settings stored in the run manifest.
    parser = build_recording_parser()
    parser.description = "Compatibility parser for deferred stereo postprocessing."
    parsed = parser.parse_args(forwarded)
    overrides = {
        field: getattr(parsed, field)
        for field in PROCESSING_CONFIG_FIELDS
        if any(
            token == "--" + field.replace("_", "-")
            or token.startswith("--" + field.replace("_", "-") + "=")
            for token in forwarded
        )
    }
    if any(token == "--camera-to-pat-transform" for token in forwarded):
        overrides["camera_to_pat_transform"] = parsed.camera_to_pat_transform
    return run_postprocess(run_dir, overrides)


def main(argv: list[str] | None = None) -> int:
    tokens = list(sys.argv[1:] if argv is None else argv)
    if "--process-only" in tokens:
        return _run_process_only_compatibility(tokens)

    # This option was previously required to opt into the full workflow. The
    # pipeline is now the explicitly full entry point, so accept and ignore it.
    tokens = [token for token in tokens if token != "--process-after-capture"]
    parser = build_recording_parser()
    parser.description = (
        "Record one synchronized AcousTools stereo trajectory, then run 2D tracking, "
        "3D triangulation, and ideal-log comparison."
    )
    args = parser.parse_args(tokens)
    args.entry_script = Path(__file__).name
    exit_code, run_dir = run_recording(args, postprocess_will_follow=True)
    if exit_code != 0 or run_dir is None:
        return exit_code
    return run_postprocess(run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
