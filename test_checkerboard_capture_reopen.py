"""Hardware-free test of the R (reopen camera) key in eventcam_checkerboard_calibration_capture.py."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import eventcam_checkerboard_calibration_capture as cap

EVENT_DTYPE = np.dtype([("x", "<u2"), ("y", "<u2"), ("p", "u1"), ("t", "<i8")])


class FakeKey:
    KEY_ESCAPE, KEY_Q, KEY_ENTER, KEY_KP_ENTER, KEY_R = "esc", "q", "enter", "kpenter", "r"


class FakeAction:
    PRESS = "press"


class FakeBaseWindow:
    class RenderMode:
        BGR = 0


class FakeFrameGenerator:
    def __init__(self, **kwargs):
        pass

    def set_output_callback(self, callback):
        pass

    def process_events(self, events):
        pass


class FakeHardware:
    """A camera whose stream yields 2 ms slices, a window, and scripted key presses."""

    def __init__(self, script: dict[int, str]):
        self.script = script
        self.polls = 0
        self.opened: list[str] = []
        self.raw_log_calls: list[str] = []
        self.window = None
        hardware = self

        class Stream:
            def log_raw_data(self, path):
                hardware.raw_log_calls.append("log")
                Path(path).write_bytes(b"")

            def stop_log_raw_data(self):
                hardware.raw_log_calls.append("stop")

        class Device:
            def get_i_events_stream(self):
                return Stream()

        class Iterator:
            def get_size(self):
                return (720, 1280)

            def __iter__(self):
                for _ in range(10_000):
                    time.sleep(0.002)
                    yield np.zeros(5, dtype=EVENT_DTYPE)

        class EventsIterator:
            @staticmethod
            def from_device(**kwargs):
                return Iterator()

        class Window:
            def __init__(self, **kwargs):
                self.closed = False
                self.callback = None
                hardware.window = self

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def set_keyboard_callback(self, callback):
                self.callback = callback

            def show_async(self, frame):
                pass

            def should_close(self):
                return self.closed

            def set_close_flag(self):
                self.closed = True

        class EventLoop:
            @staticmethod
            def poll_and_dispatch():
                hardware.polls += 1
                key = hardware.script.get(hardware.polls)
                if key:
                    hardware.window.callback(key, 0, FakeAction.PRESS, 0)

        self.metavision = (EventsIterator, object(), object(), FakeFrameGenerator, mock.Mock(Dark=0),
                           EventLoop, FakeBaseWindow, Window, FakeAction, FakeKey)
        self.device_class = Device

    def open_event_camera(self, initiate_device, metavision_hal, serial):
        self.opened.append(serial)
        return self.device_class()


class ReopenKeyTest(unittest.TestCase):
    def test_r_reopens_the_camera_but_not_while_recording(self) -> None:
        # R before any pose -> reopen; Enter starts a 0.05 s pose; R right after is ignored
        # while recording; R at poll 200 (pose done) -> reopen; Q finishes.
        hardware = FakeHardware({5: "r", 20: "enter", 21: "r", 200: "r", 300: "q"})
        with tempfile.TemporaryDirectory() as temporary:
            argv = ["capture", "--serial", "00000508", "--output-dir", temporary,
                    "--duration-sec", "0.05", "--camera-reopen-wait-sec", "0"]
            no_corners = {"selected_found_corners": False, "selected_calibration_image": "", "candidates": []}
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(cap.scale_capture, "import_metavision", return_value=hardware.metavision), \
                    mock.patch.object(cap.scale_capture, "open_event_camera", side_effect=hardware.open_event_camera), \
                    mock.patch.object(cap, "write_pose_images_from_events", return_value=no_corners):
                cap.main()
            manifest = json.loads((Path(temporary) / "capture_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["pose_count"], 1)
        self.assertEqual(manifest["camera_refresh_count"], 2)
        self.assertEqual(hardware.opened, ["00000508"] * 3)
        self.assertEqual(hardware.raw_log_calls, ["log", "stop"])

    def test_negative_reopen_wait_is_rejected(self) -> None:
        with mock.patch.object(sys, "argv", ["capture", "--camera-reopen-wait-sec", "-1"]):
            with self.assertRaises(SystemExit):
                cap.main()


if __name__ == "__main__":
    unittest.main()
