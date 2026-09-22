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


def write_led_case(
    path: Path,
    *,
    background_per_bin: int = 25,
    led_onset_us: int | None = 150_000,
    led_burst: tuple[int, ...] = (300, 150, 60),
    noise_spike_us: int | None = None,
    noise_spike_count: int = 47,
    duration_us: int = 200_000,
) -> None:
    """ROI activity like the real recordings: steady background, optional LED burst, optional noise bin."""
    start = 1_000_000
    bin_us = 100
    rng = np.random.default_rng(7)
    times: list[np.ndarray] = []
    for index in range(duration_us // bin_us):
        count = background_per_bin
        offset = index * bin_us
        if led_onset_us is not None:
            burst_index = (offset - led_onset_us) // bin_us
            if 0 <= burst_index < len(led_burst):
                count = led_burst[burst_index]
        if noise_spike_us is not None and offset == noise_spike_us:
            count = noise_spike_count
        times.append(start + offset + np.sort(rng.integers(0, bin_us, count)))
    t = np.concatenate(times)
    events = np.empty(t.size, dtype=EVENT_DTYPE)
    events["x"] = 5
    events["y"] = 7
    events["p"] = 1
    events["t"] = t
    np.savez(
        path,
        events=events,
        output_start_ts_us=np.asarray(start),
        output_end_ts_us=np.asarray(start + duration_us),
        side=np.asarray("left"),
        camera_serial=np.asarray("test"),
        hardware_synchronized=np.asarray(True),
        timestamp_domain_id=np.asarray("shared-test-clock"),
    )


class StereoPatStartLedTests(unittest.TestCase):
    ROI = (0, 0, 20, 20)

    def _detect(self, path: Path, out: Path, **kwargs):
        return detect_led_sync_npz(
            path,
            self.ROI,
            bin_us=100,
            threshold=47,
            output_dir=out,
            expected_t_recording_sec=0.0,
            search_half_window_sec=0.2,
            **kwargs,
        )

    def test_genuine_onset_is_accepted_and_reports_its_margin_and_residual(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tmp_path = Path(temporary)
            write_led_case(tmp_path / "left_events.npz")
            result = self._detect(
                tmp_path / "left_events.npz",
                tmp_path / "report",
                expected_onset_t_recording_sec=0.142,
                onset_tolerance_sec=0.1,
                min_peak_to_threshold_ratio=1.5,
            )
            self.assertTrue(result["detected"])
            self.assertTrue(result["threshold_crossed"])
            self.assertEqual(result["rejection_reasons"], [])
            self.assertAlmostEqual(result["sync_t_recording_sec"], 0.150, delta=0.0001)
            self.assertAlmostEqual(result["onset_residual_sec"], 0.008, delta=0.0002)
            self.assertGreater(result["peak_to_threshold_ratio"], 6.0)

    def test_isolated_noise_bin_far_from_the_predicted_start_is_rejected(self) -> None:
        # The 2026-09-18 false detections: no LED in view, one noise bin reaching the threshold
        # more than a second before the real PAT start.
        with tempfile.TemporaryDirectory() as temporary:
            tmp_path = Path(temporary)
            write_led_case(tmp_path / "left_events.npz", led_onset_us=None, noise_spike_us=30_000)
            with self.assertRaisesRegex(RuntimeError, "candidate was rejected.*predicted PAT start"):
                self._detect(
                    tmp_path / "left_events.npz",
                    tmp_path / "report",
                    expected_onset_t_recording_sec=0.170,
                    onset_tolerance_sec=0.1,
                )
            # Without the new checks the same recording is accepted, as it was before.
            legacy = self._detect(tmp_path / "left_events.npz", tmp_path / "legacy")
            self.assertTrue(legacy["detected"])
            self.assertAlmostEqual(legacy["sync_t_recording_sec"], 0.030, delta=0.0001)
            # Report-only use (the recording core's default): accepted, but the numbers that
            # expose the false detection are in the result.
            reported = self._detect(
                tmp_path / "left_events.npz",
                tmp_path / "reported",
                expected_onset_t_recording_sec=0.170,
            )
            self.assertTrue(reported["detected"])
            self.assertEqual(reported["rejection_reasons"], [])
            self.assertAlmostEqual(reported["onset_residual_sec"], -0.140, delta=0.0002)
            self.assertAlmostEqual(reported["peak_to_threshold_ratio"], 1.0, delta=0.01)

    def test_peak_that_barely_reaches_the_threshold_is_rejected_as_too_dim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tmp_path = Path(temporary)
            write_led_case(tmp_path / "left_events.npz", led_burst=(50, 40))
            with self.assertRaisesRegex(RuntimeError, "candidate was rejected.*too dim"):
                self._detect(
                    tmp_path / "left_events.npz",
                    tmp_path / "report",
                    expected_onset_t_recording_sec=0.150,
                    onset_tolerance_sec=0.1,
                    min_peak_to_threshold_ratio=1.5,
                )
            import json as _json

            report = _json.loads((tmp_path / "report" / "left_pat_start_led.json").read_text(encoding="utf-8"))
            self.assertFalse(report["detected"])
            self.assertTrue(report["threshold_crossed"])
            self.assertEqual(len(report["rejection_reasons"]), 1)

    def test_missing_led_is_still_reported_as_not_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tmp_path = Path(temporary)
            write_led_case(tmp_path / "left_events.npz", led_onset_us=None)
            with self.assertRaisesRegex(RuntimeError, "was not detected"):
                self._detect(
                    tmp_path / "left_events.npz",
                    tmp_path / "report",
                    expected_onset_t_recording_sec=0.150,
                    onset_tolerance_sec=0.1,
                    min_peak_to_threshold_ratio=1.5,
                )

    def test_invalid_acceptance_settings_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tmp_path = Path(temporary)
            write_led_case(tmp_path / "left_events.npz")
            with self.assertRaisesRegex(ValueError, "onset_tolerance_sec"):
                self._detect(tmp_path / "left_events.npz", tmp_path / "a", onset_tolerance_sec=-0.1)
            with self.assertRaisesRegex(ValueError, "min_peak_to_threshold_ratio"):
                self._detect(tmp_path / "left_events.npz", tmp_path / "b", min_peak_to_threshold_ratio=0.5)

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
