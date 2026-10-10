#!/usr/bin/env python3
"""Free space on C: by deleting record folders that are fully backed up on the SSD (D:).

For each target, every file under it on C: must exist on D: at the same relative path with the same
size; if even one file is missing or differs, that target is left untouched. Without --delete
nothing is removed (the default only reports). Run it yourself: the deletion is permanent on C:
(the SSD copy stays).

Targets (choose on the command line):
  ff_heart   stereo_acoustools_3d_records_ff_heart (whole folder, 18 V, 2026-09-18..21)
  steps      stereo_acoustools_3d_records_step_response_2s, _large_step, _vzr_step (whole folders)
  v15_0924   the run folders of stereo_acoustools_3d_records_V15 recorded on 2026-09-24
             (the 14 mm session; session JSONs and supply logs are kept)
  v15_0923   the same for 2026-09-23 (kcheck and the five scanned planes at 15 V)
  v15_0925   the same for 2026-09-25 (scale-up parts A/B, retries, field-scan rounds 1/2)

  python delete_backed_up_records.py ff_heart steps v15_0924            # check only
  python delete_backed_up_records.py ff_heart steps v15_0924 --delete   # check, then delete
"""
from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SSD = Path("D:/")
V15_DAYS = {"v15_0923": "20260923", "v15_0924": "20260924", "v15_0925": "20260925"}
TARGETS = ["ff_heart", "steps", *V15_DAYS]


def targets(name: str) -> list[Path]:
    if name == "ff_heart":
        return [ROOT / "stereo_acoustools_3d_records_ff_heart"]
    if name == "steps":
        return [ROOT / f"stereo_acoustools_3d_records_{n}" for n in ("step_response_2s", "large_step", "vzr_step")]
    if name in V15_DAYS:
        records = ROOT / "stereo_acoustools_3d_records_V15"
        tag = f"_V15_{V15_DAYS[name]}_"
        return sorted(p for p in records.iterdir() if p.is_dir() and tag in p.name)
    raise SystemExit(f"unknown target {name!r}")


def check(path: Path) -> tuple[int, int, list[str]]:
    """(files, bytes, problems) comparing path on C: with the same relative path on D:."""
    copy = SSD / path.relative_to(ROOT)
    count = total = 0
    problems: list[str] = []
    for dirpath, _dirs, names in os.walk(path):
        for name in names:
            src = Path(dirpath) / name
            dst = copy / src.relative_to(path)
            size = src.stat().st_size
            count += 1
            total += size
            if not dst.is_file():
                problems.append(f"missing on SSD: {dst}")
            elif dst.stat().st_size != size:
                problems.append(f"size differs: {dst} ({dst.stat().st_size} vs {size})")
    if count == 0:
        problems.append("nothing to delete (empty or absent)")
    return count, total, problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="+", choices=TARGETS)
    ap.add_argument("--delete", action="store_true", help="delete the targets that passed the check")
    args = ap.parse_args()
    if not SSD.is_dir():
        raise SystemExit("D: (the SSD) is not available")

    passed: list[tuple[Path, int]] = []
    for name in args.targets:
        paths = targets(name)
        results = [(p, *check(p)) for p in paths if p.exists()]
        bad = [r for r in results if r[3]]
        files = sum(r[1] for r in results)
        size = sum(r[2] for r in results)
        if not results or bad:
            print(f"[KEEP] {name}: {len(results)} folder(s), {files} files, {size / 1e9:.1f} GB - not deleted:")
            for p, _c, _t, problems in (bad or []):
                print(f"   {p.name}: {problems[0]}" + (f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""))
            continue
        print(f"[OK]   {name}: {len(results)} folder(s), {files} files, {size / 1e9:.1f} GB - every file is on the SSD")
        passed.extend((p, t) for p, _c, t, _pr in results)

    freed = sum(t for _p, t in passed)
    if not args.delete:
        print(f"Check only. With --delete these would free {freed / 1e9:.1f} GB on C:.")
        return 0
    for path, _t in passed:
        shutil.rmtree(path)
    usage = shutil.disk_usage(ROOT)
    print(f"Deleted {len(passed)} folder(s), {freed / 1e9:.1f} GB. C: free now {usage.free / 1e9:.0f} GB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
