#!/usr/bin/env python3
"""Tests for the memory-mapped lightweight stereo preview renderer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np

from stereo_acoustools_3d_quick_preview import (
    discover_runs,
    led_mask_from_manifest,
    mmap_stored_npy,
    preview_interval,
    render_events,
)


EVENT_DTYPE = np.dtype([("x", "<u2"), ("y", "<u2"), ("p", "<i2"), ("t", "<i8")])


class QuickStereoPreviewTests(unittest.TestCase):
    def test_uncompressed_events_member_is_mapped_in_place(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.npz"
            expected = np.array([(1, 2, 1, 100), (3, 4, 0, 200)], dtype=EVENT_DTYPE)
            np.savez(path, events=expected, output_start_ts_us=np.int64(100))

            mapped = mmap_stored_npy(path)

            self.assertIsInstance(mapped, np.memmap)
            np.testing.assert_array_equal(mapped, expected)
            mapped._mmap.close()

    def test_compressed_events_are_rejected_instead_of_fully_loaded(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "events_compressed.npz"
            np.savez_compressed(path, events=np.zeros(2, dtype=EVENT_DTYPE))
            with self.assertRaisesRegex(RuntimeError, "requires an uncompressed NPZ"):
                mmap_stored_npy(path)

    def test_discovers_only_run_directories(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "run_a"
            run.mkdir()
            (run / "pipeline_manifest.json").write_text("{}", encoding="utf-8")
            self.assertEqual(discover_runs([root]), [run.resolve()])

    def test_motion_window_uses_led_timing_and_manifest_duration(self) -> None:
        args = argparse.Namespace(
            full_recording=False,
            pre_roll_sec=0.25,
            post_roll_sec=0.25,
        )
        start, end, motion_start = preview_interval(
            {"expected_duration_sec": 8.0},
            {"ideal_start_in_recording_sec": 1.0},
            10.0,
            args,
        )
        self.assertEqual((start, end, motion_start), (0.75, 9.25, 1.0))

    def test_led_roi_is_masked_by_default(self) -> None:
        manifest = {"pat_start_led": {"side": "left", "roi": "6,0,10,3"}}
        self.assertEqual(led_mask_from_manifest(manifest, False), ("left", (6, 0, 10, 3)))
        self.assertEqual(led_mask_from_manifest(manifest, True), ("off", None))

    def test_render_masks_led_events_but_keeps_particle_events(self) -> None:
        events = np.array([(8, 1, 1, 100), (2, 5, 1, 100)], dtype=EVENT_DTYPE)
        frame = render_events(
            events,
            sensor_width=10,
            sensor_height=10,
            output_width=10,
            output_height=10,
            gain=255,
            point_size=1,
            mask_roi=(6, 0, 10, 3),
        )
        self.assertEqual(int(frame[1, 8].max()), 0)
        self.assertEqual(int(frame[5, 2, 2]), 255)


if __name__ == "__main__":
    unittest.main()
