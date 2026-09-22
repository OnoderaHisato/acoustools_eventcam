from __future__ import annotations

import queue
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
from unittest import mock
import unittest

import numpy as np

import stereo_eventcam_record_sync as recorder
import stereo_process_recording as processor


class FakeMode:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeSyncFacility:
    def __init__(
        self,
        *,
        succeeds: bool = True,
        reported_mode: str | None = None,
        unreadable_mode: bool = False,
    ) -> None:
        self.succeeds = succeeds
        self.reported_mode = reported_mode
        self.unreadable_mode = unreadable_mode
        self.requested_mode = "standalone"

    def _set(self, mode: str) -> bool:
        self.requested_mode = mode
        return self.succeeds

    def set_mode_standalone(self) -> bool:
        return self._set("standalone")

    def set_mode_master(self) -> bool:
        return self._set("master")

    def set_mode_slave(self) -> bool:
        return self._set("slave")

    def get_mode(self) -> FakeMode:
        if self.unreadable_mode:
            raise TypeError("unregistered SyncMode enum")
        return FakeMode((self.reported_mode or self.requested_mode).upper())


class RecordedValue:
    """Stand-in for a multiprocessing.Value that remembers every assignment."""

    def __init__(self, initial: int = 0) -> None:
        self.history: list[int] = []
        self._value = int(initial)

    @property
    def value(self) -> int:
        return self._value

    @value.setter
    def value(self, new_value: int) -> None:
        self._value = int(new_value)
        self.history.append(int(new_value))


class StreamStallWatchdogTests(unittest.TestCase):
    SECOND = 1_000_000_000

    def test_only_a_silent_capturing_worker_counts_as_stalled(self) -> None:
        now = 100 * self.SECOND
        progress = {
            "left": (recorder.CAPTURE_PHASE_CAPTURING, now - 5 * self.SECOND),
            "right": (recorder.CAPTURE_PHASE_CAPTURING, now - self.SECOND // 100),
        }
        stalled = recorder.stalled_capture_sides(progress, now, 3.0)
        self.assertEqual(list(stalled), ["left"])
        self.assertAlmostEqual(stalled["left"], 5.0, places=6)

    def test_start_up_and_saving_phases_are_never_reported(self) -> None:
        now = 100 * self.SECOND
        long_ago = now - 60 * self.SECOND
        progress = {
            "waiting_for_sync": (recorder.CAPTURE_PHASE_STARTING, long_ago),
            "no_slice_yet": (recorder.CAPTURE_PHASE_CAPTURING, 0),
            "writing_npz": (recorder.CAPTURE_PHASE_SAVING, long_ago),
        }
        self.assertEqual(recorder.stalled_capture_sides(progress, now, 3.0), {})

    def test_zero_timeout_disables_the_watchdog(self) -> None:
        now = 100 * self.SECOND
        progress = {"left": (recorder.CAPTURE_PHASE_CAPTURING, now - 60 * self.SECOND)}
        self.assertEqual(recorder.stalled_capture_sides(progress, now, 0.0), {})

    def _progress(self, left_phase: int, left_age_sec: float, right_phase: int, right_age_sec: float):
        now = time.perf_counter_ns()
        return {
            "left": (SimpleNamespace(value=left_phase), SimpleNamespace(value=now - int(left_age_sec * self.SECOND))),
            "right": (SimpleNamespace(value=right_phase), SimpleNamespace(value=now - int(right_age_sec * self.SECOND))),
        }

    def test_collection_stops_within_seconds_when_one_stream_stalls(self) -> None:
        results_queue: queue.Queue = queue.Queue()
        abort_event = threading.Event()
        started = time.monotonic()
        results, stalled = recorder.collect_worker_results(
            results_queue,
            expected_sides=["right", "left"],
            progress=self._progress(recorder.CAPTURE_PHASE_CAPTURING, 10.0, recorder.CAPTURE_PHASE_CAPTURING, 0.0),
            abort_event=abort_event,
            deadline_monotonic=time.monotonic() + 30.0,
            stall_timeout_sec=3.0,
            poll_sec=0.01,
        )
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(results, [])
        self.assertEqual(list(stalled), ["left"])
        self.assertTrue(abort_event.is_set())

    def test_collection_returns_both_results_when_the_streams_are_healthy(self) -> None:
        results_queue: queue.Queue = queue.Queue()
        results_queue.put({"side": "right", "ok": True})
        results_queue.put({"side": "left", "ok": True})
        abort_event = threading.Event()
        results, stalled = recorder.collect_worker_results(
            results_queue,
            expected_sides=["right", "left"],
            progress=self._progress(recorder.CAPTURE_PHASE_SAVING, 10.0, recorder.CAPTURE_PHASE_SAVING, 10.0),
            abort_event=abort_event,
            deadline_monotonic=time.monotonic() + 30.0,
            stall_timeout_sec=3.0,
            poll_sec=0.01,
        )
        self.assertEqual([result["side"] for result in results], ["right", "left"])
        self.assertEqual(stalled, {})
        self.assertFalse(abort_event.is_set())

    def test_a_finished_side_is_not_watched_while_the_other_is_still_capturing(self) -> None:
        results_queue: queue.Queue = queue.Queue()
        results_queue.put({"side": "left", "ok": True})
        abort_event = threading.Event()
        # The left value is stale on purpose: its result has already arrived.
        results, stalled = recorder.collect_worker_results(
            results_queue,
            expected_sides=["right", "left"],
            progress=self._progress(recorder.CAPTURE_PHASE_CAPTURING, 10.0, recorder.CAPTURE_PHASE_CAPTURING, 0.0),
            abort_event=abort_event,
            deadline_monotonic=time.monotonic() + 0.2,
            stall_timeout_sec=3.0,
            poll_sec=0.01,
        )
        self.assertEqual([result["side"] for result in results], ["left"])
        self.assertEqual(stalled, {})
        self.assertFalse(abort_event.is_set())

    def test_worker_reports_capturing_then_saving_and_a_heartbeat_per_slice(self) -> None:
        class FakeIterator:
            def __init__(self) -> None:
                self.current = 0

            def __iter__(self):
                for index in range(400):
                    time.sleep(0.001)
                    self.current = index * 1000
                    events = np.zeros(3, dtype=recorder.EVENT_DTYPE)
                    events["t"] = self.current + np.arange(3)
                    yield events

            def get_current_time(self) -> int:
                return self.current

        iterator = FakeIterator()
        fake_events_iterator = SimpleNamespace(from_device=lambda **_kwargs: iterator)
        device = SimpleNamespace(get_i_camera_synchronization=lambda: None)
        phase = RecordedValue(recorder.CAPTURE_PHASE_STARTING)
        heartbeat = RecordedValue(0)
        results_queue: queue.Queue = queue.Queue()
        with TemporaryDirectory() as temporary:
            with (
                mock.patch.object(recorder, "import_metavision", return_value=(fake_events_iterator, None, None)),
                mock.patch.object(recorder.scale_capture, "open_event_camera", return_value=device),
                mock.patch.object(recorder, "get_sensor_size", return_value=(1280, 720)),
            ):
                recorder.record_worker(
                    side="left",
                    serial="test",
                    output_dir=str(Path(temporary) / "left"),
                    start_perf_ns=time.perf_counter_ns(),
                    start_delay_sec=0.0,
                    duration_sec=0.02,
                    delta_t_us=1000,
                    sensor_width=1280,
                    sensor_height=720,
                    max_events=0,
                    result_queue=results_queue,
                    sync_role="standalone",
                    capture_phase=phase,
                    last_slice_perf_ns=heartbeat,
                )
            result = results_queue.get_nowait()
            self.assertTrue(result["ok"], result["meta"].get("error"))
            self.assertEqual(
                phase.history, [recorder.CAPTURE_PHASE_CAPTURING, recorder.CAPTURE_PHASE_SAVING]
            )
            self.assertEqual(len(heartbeat.history), result["meta"]["total_slices"])
            self.assertEqual(heartbeat.history, sorted(heartbeat.history))
            self.assertGreater(heartbeat.value, 0)


class StereoSyncTimingTests(unittest.TestCase):
    def test_filter_events_uses_half_open_common_window(self) -> None:
        events = np.zeros(6, dtype=recorder.EVENT_DTYPE)
        events["t"] = [99, 100, 101, 199, 200, 201]
        filtered = recorder.filter_events_to_window(events, 100, 200)
        np.testing.assert_array_equal(filtered["t"], [100, 101, 199])

    def test_sync_mode_success_is_verified(self) -> None:
        facility = FakeSyncFacility()
        applied, readback_verified, verification = recorder.set_and_verify_sync_mode(
            side="left",
            i_sync=facility,
            sync_role="master",
        )
        self.assertEqual(applied, "master")
        self.assertTrue(readback_verified)
        self.assertEqual(verification, "setter_return_and_get_mode_readback")

    def test_sync_mode_false_return_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "failed to set"):
            recorder.set_and_verify_sync_mode(
                side="right",
                i_sync=FakeSyncFacility(succeeds=False),
                sync_role="slave",
            )

    def test_sync_mode_readback_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "camera reports"):
            recorder.set_and_verify_sync_mode(
                side="right",
                i_sync=FakeSyncFacility(reported_mode="standalone"),
                sync_role="slave",
            )

    def test_unregistered_vendor_mode_enum_uses_successful_setter(self) -> None:
        applied, readback_verified, verification = recorder.set_and_verify_sync_mode(
            side="right",
            i_sync=FakeSyncFacility(unreadable_mode=True),
            sync_role="slave",
        )
        self.assertEqual(applied, "slave")
        self.assertFalse(readback_verified)
        self.assertIn("setter_return_only", verification)

    def test_new_hardware_sync_common_origin_needs_zero_offset(self) -> None:
        common = {
            "sync_ts_us": 20_000,
            "output_start_ts_us": 20_000,
            "output_end_ts_us": 2_020_000,
            "sync_mode_verified": True,
            "hardware_synchronized": True,
            "timestamp_domain_id": "hardware-master:123",
            "capture_interval_complete": True,
            "event_limit_reached": False,
            "has_hardware_sync_metadata": True,
        }
        left = {**common, "sync_role": "master", "sync_mode_applied": "master"}
        right = {**common, "sync_role": "slave", "sync_mode_applied": "slave"}
        offset, source = processor.resolve_right_time_offset_sec(
            explicit_offset_sec=None,
            left_meta=left,
            right_meta=right,
            manifest_sync_status={"requested": True, "verified": True},
        )
        self.assertEqual(offset, 0.0)
        self.assertIn("derived", source)

    def test_legacy_hardware_sync_recovers_individual_origin_difference(self) -> None:
        left = {
            "sync_ts_us": -1,
            "output_start_ts_us": 1_000,
            "output_end_ts_us": 10_000,
            "sync_role": "master",
            "sync_mode_applied": "master",
            "timestamp_domain_id": "",
            "has_hardware_sync_metadata": False,
        }
        right = {
            "sync_ts_us": -1,
            "output_start_ts_us": 1_350,
            "output_end_ts_us": 10_350,
            "sync_role": "slave",
            "sync_mode_applied": "slave",
            "timestamp_domain_id": "",
            "has_hardware_sync_metadata": False,
        }
        offset, _ = processor.resolve_right_time_offset_sec(
            explicit_offset_sec=None,
            left_meta=left,
            right_meta=right,
            manifest_sync_status={"requested": True, "verified": None},
        )
        self.assertAlmostEqual(offset, 0.000350)

    def test_unverified_requested_hardware_sync_is_rejected(self) -> None:
        incomplete = {
            "sync_ts_us": -1,
            "output_start_ts_us": 0,
            "output_end_ts_us": 0,
            "sync_role": "master",
            "sync_mode_applied": "",
            "sync_mode_verified": False,
            "hardware_synchronized": False,
            "timestamp_domain_id": "hardware-master:123",
            "capture_interval_complete": False,
            "event_limit_reached": False,
            "has_hardware_sync_metadata": True,
        }
        with self.assertRaisesRegex(SystemExit, "does not verify"):
            processor.resolve_right_time_offset_sec(
                explicit_offset_sec=None,
                left_meta=incomplete,
                right_meta={**incomplete, "sync_role": "slave"},
                manifest_sync_status={"requested": True, "verified": False},
            )


if __name__ == "__main__":
    unittest.main()
