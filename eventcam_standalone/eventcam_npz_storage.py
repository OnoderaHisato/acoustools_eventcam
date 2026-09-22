#!/usr/bin/env python3
"""Low-overhead, atomic NPZ storage helpers for event-camera recordings."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import numpy as np


NPZ_COMPRESSION_CHOICES = ("none", "compressed")


def concatenate_event_chunks(
    chunks: list[np.ndarray],
    *,
    dtype: np.dtype[Any],
) -> tuple[np.ndarray, float]:
    """Combines chunks and immediately releases their references."""
    started = time.perf_counter()
    if not chunks:
        events = np.empty(0, dtype=dtype)
    elif len(chunks) == 1:
        events = chunks.pop()
    else:
        events = np.concatenate(chunks)
        chunks.clear()
    return events, time.perf_counter() - started


def save_npz_atomic(
    path: Path,
    *,
    compression: str,
    arrays: dict[str, Any],
) -> dict[str, Any]:
    """Writes a complete NPZ beside the target and atomically replaces it."""
    if compression not in NPZ_COMPRESSION_CHOICES:
        raise ValueError(
            f"Unknown NPZ compression mode {compression!r}; "
            f"expected one of {NPZ_COMPRESSION_CHOICES}"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the atomic staging name short. Deep stereo run directories can sit
    # close to the legacy Windows MAX_PATH limit even when the final NPZ name
    # itself is valid.
    temp_path = path.with_name(f".events.{os.getpid()}.tmp")
    saver = np.savez_compressed if compression == "compressed" else np.savez
    started = time.perf_counter()
    try:
        with temp_path.open("wb") as stream:
            saver(stream, **arrays)
            stream.flush()
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    return {
        "npz_compression": compression,
        "npz_save_sec": float(time.perf_counter() - started),
        "npz_size_bytes": int(path.stat().st_size),
    }
