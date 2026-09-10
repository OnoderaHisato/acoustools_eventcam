from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import numpy as np

from stereo_detect_pat_start_led import detect_led_sync_npz


EVENT_DTYPE = np.dtype([("x", "<u2"), ("y", "<u2"), ("p", "u1"), ("t", "<i8")])


def write_side(path: Path, *, with_led: bool) -> None:
    start = 1_000_000
    particle = np.empty(400, dtype=EVENT_DTYPE)
    particle["x"] = 640
    particle["y"] = 360
    particle["p"] = 1
    particle["t"] = start + np.arange(400) * 500
    chunks = [particle]
    if with_led:
        led = np.empty(240, dtype=EVENT_DTYPE)
        led["x"] = 5
        led["y"] = 7
        led["p"] = 1
        led["t"] = start + 50_000 + np.arange(240) % 300
        chunks.append(led)
    events = np.concatenate(chunks)
    events.sort(order="t")
    np.savez(
        path,
        events=events,
        output_start_ts_us=np.asarray(start),
        output_end_ts_us=np.asarray(start + 200_000),
        side=np.asarray(path.stem.split("_")[0]),
        camera_serial=np.asarray("test"),
        hardware_synchronized=np.asarray(True),
        timestamp_domain_id=np.asarray("shared-test-clock"),
    )


class StereoPatStartLedTests(unittest.TestCase):
    def test_one_side_led_detection_does_not_require_other_side(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tmp_path = Path(temporary)
            left = tmp_path / "left_events.npz"
            right = tmp_path / "right_events.npz"
            write_side(left, with_led=True)
            write_side(right, with_led=False)

            result = detect_led_sync_npz(
                left,
                (0, 0, 20, 20),
                bin_us=100,
                threshold=20,
                output_dir=tmp_path / "report",
                expected_t_recording_sec=0.05,
                search_half_window_sec=0.01,
            )

            self.assertEqual(result["side"], "left")
            self.assertLess(abs(result["sync_t_recording_sec"] - 0.05), 0.0001)
            with self.assertRaisesRegex(RuntimeError, "was not detected"):
                detect_led_sync_npz(
                    right,
                    (0, 0, 20, 20),
                    bin_us=100,
                    threshold=20,
                    output_dir=tmp_path / "right_report",
                    expected_t_recording_sec=0.05,
                    search_half_window_sec=0.01,
                )


if __name__ == "__main__":
    unittest.main()
