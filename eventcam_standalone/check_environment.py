#!/usr/bin/env python3
"""Check Python/library imports without opening or enumerating a camera."""
from __future__ import annotations

import argparse
import importlib
import platform
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", action="store_true", help="Also import OpenEB/HAL/UI bindings; no device discovery")
    args = parser.parse_args()
    print(f"Python: {sys.executable}\nVersion: {platform.python_version()}\nToolkit: {Path(__file__).resolve().parent}")
    failures = 0
    for name in ("numpy", "cv2", "matplotlib"):
        try:
            module = importlib.import_module(name)
            print(f"[OK] {name}: {getattr(module, '__version__', '?')} ({module.__file__})")
        except Exception as exc:
            failures += 1
            print(f"[FAIL] {name}: {exc}")
    if args.sdk:
        try:
            from eventcam_scale_calibration_capture import import_metavision
            import_metavision()
            print("[OK] OpenEB event I/O, HAL, Core and UI bindings imported. Cameras were not enumerated/opened.")
        except (Exception, SystemExit) as exc:
            # import_metavision() reports a missing SDK with SystemExit, which is a
            # BaseException and would otherwise bypass this handler and its guidance.
            failures += 1
            print(f"[FAIL] OpenEB: {exc}\nSee SETUP_OPENEB_JP.md (DLL, plugin, Python ABI and HDF5 troubleshooting).")
    else:
        print("OpenEB not checked; use --sdk to test imports. Offline NPZ processing needs only the three libraries above.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
