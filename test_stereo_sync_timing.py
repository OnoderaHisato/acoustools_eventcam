from __future__ import annotations

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
