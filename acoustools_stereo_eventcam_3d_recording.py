#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Interactive multi-trajectory AcousTools synchronized stereo 3D recorder.

The recording core still supports a single independent run.  This entry point
keeps one AcousTools/OpenMPD session open while the operator selects and records
multiple trajectories, matching the interactive loop of the legacy monocular
``acoustools_eventcam_sync.py`` workflow.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from stereo_acoustools_3d_recording_core import (
    build_recording_parser,
    open_recording_hardware_session,
    run_recording,
    shutdown_recording_hardware_session,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_recording_parser()
    parser.description = (
        "Interactively record one or more AcousTools 3D trajectories with "
        "synchronized stereo event cameras."
    )
    parser.add_argument(
        "--single-run",
        action="store_true",
        help="Preserve the previous behavior: record one trajectory and exit.",
    )
    return parser.parse_args(argv)


def prompt_record_another() -> bool:
    """Return whether another trajectory should be selected and recorded."""
    while True:
        try:
            answer = input("\n別の軌道を続けて計測しますか？ (Y/n): ").strip().lower()
        except EOFError:
            return False
        if answer in {"", "y", "yes", "はい"}:
            return True
        if answer in {"n", "no", "いいえ"}:
            return False
        print("[WARN] Yまたはnを入力してください。")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.entry_script = Path(__file__).name

    if args.single_run:
        exit_code, _ = run_recording(args)
        return int(exit_code)

    hardware_session = open_recording_hardware_session()
    overall_code = 0
    completed_runs = 0
    attempted_runs = 0
    try:
        while True:
            attempted_runs += 1
            print(f"\n=== Stereo interactive trajectory run {attempted_runs} ===")
            try:
                exit_code, _ = run_recording(
                    args,
                    hardware_session=hardware_session,
                )
            except SystemExit as exc:
                raw_code = exc.code if isinstance(exc.code, int) else 2
                exit_code = int(raw_code)
                message = str(exc)
                if message and message != str(raw_code):
                    print(f"[RECORDING][ERROR] {message}")

            if exit_code == 0:
                completed_runs += 1
            else:
                overall_code = int(exit_code)
                print(f"[RECORDING][WARN] This run did not complete (exit={exit_code}).")

            if exit_code == 130 or not prompt_record_another():
                break

        print(f"[RECORDING] Interactive session finished; completed runs={completed_runs}.")
        return overall_code
    except KeyboardInterrupt:
        print("\n[RECORDING] User requested exit. Shutting down the shared PAT session...")
        return 130
    finally:
        shutdown_recording_hardware_session(hardware_session)


if __name__ == "__main__":
    raise SystemExit(main())
