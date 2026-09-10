"""Lightweight stereo 3D dataset discovery shared by batch identification jobs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np


DT_RAW = 1.0e-4
RECORDS_PREFIX = "stereo_acoustools_3d_records_auto"
GROUP_RE = re.compile(r"^(.+?_seed\d+)(?:_scale\d+)?_\d{8}_\d{6}$")
EXCLUDED_TRIALS = {
    "orig/long_random_3d_003_vertical_rich_10s_seed2404_20260807_185751",
}


@dataclass
class Trial:
    label: str
    group: str
    scale: str
    e: np.ndarray
    ctrl: np.ndarray
    e_dot: np.ndarray
    u_dot: np.ndarray
    u_ddot: np.ndarray
    dc: np.ndarray

    @property
    def n(self) -> int:
        return len(self.e)


def scale_variant(name: str) -> str:
    if name == RECORDS_PREFIX:
        return "orig"
    prefix = RECORDS_PREFIX + "_"
    if name.startswith(prefix):
        return name[len(prefix) :]
    raise ValueError(f"unrecognized records directory: {name}")


def trajectory_group(run_name: str) -> str:
    match = GROUP_RE.match(run_name)
    return match.group(1) if match else run_name


def discover_trials(base_dir: Path) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    pattern = f"{RECORDS_PREFIX}*/*/sindy_dataset_3d/state_control.npz"
    for npz_path in sorted(base_dir.glob(pattern)):
        run_dir = npz_path.parent.parent
        scale = scale_variant(run_dir.parent.name)
        label = f"{scale}/{run_dir.name}"
        if label in EXCLUDED_TRIALS:
            continue
        meta_path = npz_path.parent / "sindy_dataset_3d_meta.json"
        duration_sec = float(json.loads(meta_path.read_text())["duration_sec"]) if meta_path.exists() else 0.0
        found.append(
            {
                "npz": npz_path,
                "label": label,
                "scale": scale,
                "group": trajectory_group(run_dir.name),
                "duration_sec": duration_sec,
            }
        )
    return found


def block_mean(values: np.ndarray, factor: int) -> np.ndarray:
    n_blocks = len(values) // factor
    return values[: n_blocks * factor].reshape(n_blocks, factor, values.shape[1]).mean(axis=1)
