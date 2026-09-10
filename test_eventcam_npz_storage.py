from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

import eventcam_npz_storage as storage


EVENT_DTYPE = np.dtype([("x", "<u2"), ("y", "<u2"), ("p", "u1"), ("t", "<i8")])


class EventcamNpzStorageTests(unittest.TestCase):
    def test_single_chunk_is_reused_without_concatenation_copy(self) -> None:
        chunk = np.zeros(3, dtype=EVENT_DTYPE)
        chunks = [chunk]
        events, _ = storage.concatenate_event_chunks(chunks, dtype=EVENT_DTYPE)
        self.assertIs(events, chunk)
        self.assertEqual(chunks, [])

    def test_multiple_chunks_are_combined_and_released(self) -> None:
        first = np.zeros(2, dtype=EVENT_DTYPE)
        second = np.zeros(2, dtype=EVENT_DTYPE)
        first["t"] = [1, 2]
        second["t"] = [3, 4]
        chunks = [first, second]
        events, _ = storage.concatenate_event_chunks(chunks, dtype=EVENT_DTYPE)
        np.testing.assert_array_equal(events["t"], [1, 2, 3, 4])
        self.assertEqual(chunks, [])

    def test_atomic_npz_is_compatible_in_both_compression_modes(self) -> None:
        events = np.zeros(10, dtype=EVENT_DTYPE)
        events["t"] = np.arange(10)
        with tempfile.TemporaryDirectory() as temp_dir:
            for compression in storage.NPZ_COMPRESSION_CHOICES:
                path = Path(temp_dir) / f"events_{compression}.npz"
                stats = storage.save_npz_atomic(
                    path,
                    compression=compression,
                    arrays={
                        "events": events,
                        "output_start_ts_us": np.asarray(0, dtype=np.int64),
                        "npz_compression": np.asarray(compression),
                    },
                )
                self.assertTrue(path.exists())
                self.assertGreater(stats["npz_size_bytes"], 0)
                self.assertGreaterEqual(stats["npz_save_sec"], 0)
                with np.load(path, allow_pickle=False) as loaded:
                    np.testing.assert_array_equal(loaded["events"], events)
                    self.assertEqual(str(loaded["npz_compression"]), compression)
                self.assertEqual(list(Path(temp_dir).glob("*.tmp")), [])

    def test_atomic_npz_uses_short_staging_name_near_windows_path_limit(self) -> None:
        events = np.zeros(2, dtype=EVENT_DTYPE)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            filename = "left_events.npz"
            component_length = 253 - len(str(root)) - 1 - len(filename) - 1
            side_dir = root / ("r" * component_length)
            side_dir.mkdir()
            path = side_dir / filename

            storage.save_npz_atomic(
                path,
                compression="none",
                arrays={"events": events},
            )

            self.assertEqual(len(str(path.resolve())), 253)
            self.assertTrue(path.exists())
            self.assertEqual(list(side_dir.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
