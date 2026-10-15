"""Tests for the PAT temperature monitor window (pat_temp_app.py): the data model without a window, and a
short smoke test of the window in log-follow mode (no serial port is opened)."""

from __future__ import annotations

import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import tkinter as tk
import unittest

import pat_temp_app as app
import pat_temp_logger as logger
from test_pat_temp import ROM_BOTTOM, ROM_SPARE, ROM_TOP, cycle_lines, write_map


def rows_for(seconds: int, start: float = 1000.0, sensor_map=None) -> list[dict[str, str]]:
    rows = []
    for k in range(seconds):
        for event in (logger.parse_line(line) for line in cycle_lines(k, 30.0 + k / 60.0, 36.5)):
            rows.append(logger.reading_row(event, start + k, "COM12", sensor_map or {}))
    return rows


class ModelTests(unittest.TestCase):
    def test_colours_follow_the_map_then_appearance(self) -> None:
        with TemporaryDirectory() as temporary:
            sensors = logger.load_sensor_map(write_map(Path(temporary) / "map.json"))
        model = app.Model(sensors)
        for row in rows_for(2, sensor_map=sensors)[::-1]:   # spare and bottom appear first
            model.add_row(row)
        ordered = model.ordered()
        self.assertEqual([s.rom for s in ordered], [ROM_TOP, ROM_BOTTOM, ROM_SPARE])
        self.assertEqual([s.colour for s in ordered], app.SERIES[:3])
        self.assertEqual(ordered[0].name, "top_ft232h")

    def test_slope_group_means_and_status(self) -> None:
        with TemporaryDirectory() as temporary:
            sensors = logger.load_sensor_map(write_map(Path(temporary) / "map.json"))
        model = app.Model(sensors)
        for row in rows_for(900, sensor_map=sensors):
            model.add_row(row)
        top = model.sensors[ROM_TOP]
        self.assertAlmostEqual(top.slope_per_10min(), 10.0, places=6)    # 1 degC per minute (offset -0.25)
        self.assertAlmostEqual(model.group_latest("top"), 30.0 + 899 / 60.0 - 0.25, places=3)
        self.assertEqual(model.group_latest("bottom"), 36.5)
        self.assertAlmostEqual(top.value_at(1500.4), top.v[500], places=6)
        self.assertIsNone(top.value_at(900.0))
        model.add_row(logger.reading_row(logger.parse_line(f"T,9,9,{ROM_BOTTOM},-127.0000,disconnected"), 1900.0,
                                         "COM12", sensors))
        self.assertIsNone(model.group_latest("bottom"))
        self.assertIsNone(model.add_row({"time_unix_s": "1.0", "error": "no reading for 10 s"}))

    def test_short_data_has_no_slope_and_thinning_keeps_the_end(self) -> None:
        series = app.SensorSeries("R", "r", "", 0)
        for k in range(60):
            series.add(float(k), 20.0, "ok")
        self.assertIsNone(series.slope_per_10min())
        xs, ys = app.thin(list(range(10001)), list(range(10001)), limit=1000)
        self.assertLessEqual(len(xs), 1002)
        self.assertEqual(xs[-1], 10000)


class WindowTests(unittest.TestCase):
    def test_follow_mode_fills_the_cards_and_draws(self) -> None:
        with TemporaryDirectory() as temporary:
            root_dir = Path(temporary)
            sensor_map = write_map(root_dir / "map.json")
            sensors = logger.load_sensor_map(sensor_map)
            log = root_dir / "pat.csv"
            with log.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=logger.CSV_COLUMNS)
                writer.writeheader()
                writer.writerows(rows_for(300, sensor_map=sensors))
            try:
                root = tk.Tk()
            except tk.TclError as exc:  # pragma: no cover - no display
                self.skipTest(f"no Tk display: {exc}")
            root.withdraw()
            window = app.App(root, "auto", sensor_map, log)
            try:
                deadline = 50
                while not window.sensor_cards and deadline:
                    root.after(100)
                    root.update()
                    window.poll()
                    deadline -= 1
                window.redraw()
                self.assertEqual(set(window.sensor_cards), {ROM_TOP, ROM_BOTTOM, ROM_SPARE})
                self.assertEqual(window.summary_cards["bottom"]["value"].cget("text"), "36.50")
                self.assertEqual(window.summary_cards["diff"]["value"].cget("text"),
                                 f"{30.0 + 299 / 60.0 - 0.25 - 36.5:+.2f}")
                self.assertEqual(len(window.axes.get_lines()), 6)          # a line and an end marker per sensor
                window.select(ROM_TOP)
                window.set_range(600, "10分")
                window.redraw()
                self.assertEqual(window.selected, ROM_TOP)
                self.assertEqual(str(window.record_button.cget("state")), "disabled")
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
