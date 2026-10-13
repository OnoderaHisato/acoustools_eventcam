#!/usr/bin/env python3
"""Tempo Disc monitor: a desktop window in the style of the bmLogger phone app (Tkinter + matplotlib).

    .\\venv\\Scripts\\python.exe tempodisc_app.py                       # listen to F9EE9FFC over Bluetooth
    .\\venv\\Scripts\\python.exe tempodisc_app.py --device ""           # any Tempo Disc in range
    .\\venv\\Scripts\\python.exe tempodisc_app.py --follow <env_log.csv> # show a log being written (or a finished one)

Live mode listens to the BLE advertisement (no connection, the sensor is not changed) and can write
the same CSV as tempodisc_logger.py ("記録開始"). During a measurement the batch script already runs
tempodisc_logger.py; then open its env_log_*.csv with "ログを開く" (or --follow) so that there is a
single source of the numbers. The window only shows readings; the measurement never waits for it.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import matplotlib

matplotlib.use("TkAgg")
matplotlib.rcParams["font.family"] = ["Yu Gothic", "BIZ UDGothic", "Meiryo", "sans-serif"]
import matplotlib.dates as mdates  # noqa: E402
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

from tempodisc_ble import DEFAULT_DEVICE, TempoListener, TempoReading  # noqa: E402
from tempodisc_logger import CSV_COLUMNS, reading_row  # noqa: E402

# Colours close to the app: deep blue header, white cards, one accent colour per quantity.
BG = "#eef2f7"
HEADER = "#0b3d6e"
HEADER_TEXT = "#ffffff"
CARD = "#ffffff"
MUTED = "#6b7a8c"
TEXT = "#14212e"
QUANTITIES = [
    # key, label, unit, colour, digits
    ("temperature_C", "温度", "°C", "#e4572e", 1),
    ("humidity_percent", "湿度", "%RH", "#2e86de", 1),
    ("dew_point_C", "露点", "°C", "#17a589", 1),
    ("pressure_hPa", "気圧", "hPa", "#8e44ad", 1),
]
RANGES = [("10分", 600), ("1時間", 3600), ("6時間", 6 * 3600), ("24時間", 24 * 3600), ("全部", None)]
FONT = "Yu Gothic UI"


class Series:
    """Readings over time (one point per change or per row of a followed log)."""

    def __init__(self) -> None:
        self.t: list[float] = []
        self.values: dict[str, list[float | None]] = {key: [] for key, *_ in QUANTITIES}

    def add(self, t: float, values: dict[str, float | None]) -> None:
        self.t.append(t)
        for key in self.values:
            self.values[key].append(values.get(key))

    def window(self, key: str, seconds: float | None) -> tuple[list[dt.datetime], list[float]]:
        start = None if seconds is None or not self.t else self.t[-1] - seconds
        xs, ys = [], []
        for t, value in zip(self.t, self.values[key]):
            if value is None or (start is not None and t < start):
                continue
            xs.append(dt.datetime.fromtimestamp(t))
            ys.append(value)
        return xs, ys


def row_values(row: dict[str, str]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for key, *_ in QUANTITIES:
        try:
            out[key] = float(row[key]) if row.get(key) else None
        except ValueError:
            out[key] = None
    return out


class BleThread(threading.Thread):
    """Runs the bleak scanner in its own asyncio loop; readings go to a queue for the Tk thread."""

    def __init__(self, device: str, out: queue.Queue) -> None:
        super().__init__(daemon=True)
        self.device = device
        self.out = out
        self.stop_event = threading.Event()

    def run(self) -> None:
        listener = TempoListener(self.device, on_reading=lambda r: self.out.put(("reading", r)))
        while not self.stop_event.is_set():
            try:
                asyncio.run(listener.run(self.stop_event.is_set))
            except Exception as exc:  # noqa: BLE001 - Bluetooth off: report and retry
                self.out.put(("error", f"Bluetooth: {exc}"))
                self.stop_event.wait(5.0)


class FollowThread(threading.Thread):
    """Tails a tempodisc_logger.py CSV (rows already there first, then new ones)."""

    def __init__(self, path: Path, out: queue.Queue) -> None:
        super().__init__(daemon=True)
        self.path = path
        self.out = out
        self.stop_event = threading.Event()

    def run(self) -> None:
        position = 0
        header: list[str] | None = None
        pending = ""
        while not self.stop_event.is_set():
            try:
                with self.path.open(encoding="utf-8", newline="") as handle:
                    handle.seek(position)
                    chunk = handle.read()
                    position = handle.tell()
            except OSError as exc:
                self.out.put(("error", f"log: {exc}"))
                self.stop_event.wait(2.0)
                continue
            pending += chunk
            lines = pending.split("\n")
            pending = lines.pop()  # a half-written last line waits for the next read
            for line in lines:
                line = line.rstrip("\r")
                if not line:
                    continue
                fields = next(csv.reader([line]))
                if header is None:
                    header = fields
                    continue
                self.out.put(("row", dict(zip(header, fields))))
            self.stop_event.wait(1.0)


class App:
    def __init__(self, root: tk.Tk, device: str, follow: Path | None) -> None:
        self.root = root
        self.device = device
        self.queue: queue.Queue = queue.Queue()
        self.series = Series()
        self.latest: dict[str, float | None] = {}
        self.latest_meta: dict[str, str] = {}
        self.last_heard: float | None = None
        self.selected = "temperature_C"
        self.range_s: float | None = 3600
        self.source_thread: threading.Thread | None = None
        self.record_handle = None
        self.record_writer = None
        self.record_path: Path | None = None
        self.last_key: str | None = None
        self.plot_dirty = True

        root.title("Tempo Disc モニター")
        root.configure(bg=BG)
        root.geometry("980x720")
        root.minsize(760, 560)
        self.build()
        if follow is not None:
            self.start_follow(follow)
        else:
            self.start_live()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(200, self.poll)
        root.after(1000, self.tick)

    # ---------- layout ----------
    def build(self) -> None:
        header = tk.Frame(self.root, bg=HEADER, padx=18, pady=12)
        header.pack(fill="x")
        self.title_var = tk.StringVar(value=self.device or "Tempo Disc")
        self.model_var = tk.StringVar(value="受信待ち…")
        self.status_var = tk.StringVar(value="")
        left = tk.Frame(header, bg=HEADER)
        left.pack(side="left")
        tk.Label(left, textvariable=self.title_var, bg=HEADER, fg=HEADER_TEXT, font=(FONT, 20, "bold")).pack(anchor="w")
        tk.Label(left, textvariable=self.model_var, bg=HEADER, fg="#b8cde3", font=(FONT, 11)).pack(anchor="w")
        right = tk.Frame(header, bg=HEADER)
        right.pack(side="right")
        self.battery_var = tk.StringVar(value="電池 --")
        self.signal_var = tk.StringVar(value="電波 --")
        self.age_var = tk.StringVar(value="")
        for var in (self.battery_var, self.signal_var, self.age_var):
            tk.Label(right, textvariable=var, bg=HEADER, fg=HEADER_TEXT, font=(FONT, 11)).pack(anchor="e")

        tiles = tk.Frame(self.root, bg=BG, padx=12, pady=12)
        tiles.pack(fill="x")
        self.tile_widgets: dict[str, dict[str, tk.Widget]] = {}
        for column, (key, label, unit, colour, _digits) in enumerate(QUANTITIES):
            tiles.columnconfigure(column, weight=1, uniform="tile")
            card = tk.Frame(tiles, bg=CARD, padx=14, pady=10, highlightthickness=3, highlightbackground=CARD,
                            cursor="hand2")
            card.grid(row=0, column=column, sticky="nsew", padx=6)
            stripe = tk.Frame(card, bg=colour, height=4)
            stripe.pack(fill="x", pady=(0, 6))
            name = tk.Label(card, text=label, bg=CARD, fg=MUTED, font=(FONT, 12))
            name.pack(anchor="w")
            value = tk.Label(card, text="--", bg=CARD, fg=colour, font=(FONT, 34, "bold"))
            value.pack(anchor="w")
            unit_label = tk.Label(card, text=unit, bg=CARD, fg=MUTED, font=(FONT, 11))
            unit_label.pack(anchor="w")
            span = tk.Label(card, text="最高 --  最低 --", bg=CARD, fg=MUTED, font=(FONT, 10))
            span.pack(anchor="w", pady=(4, 0))
            for widget in (card, stripe, name, value, unit_label, span):
                widget.bind("<Button-1>", lambda _e, k=key: self.select(k))
            self.tile_widgets[key] = {"card": card, "value": value, "span": span}

        footer = tk.Frame(self.root, bg=BG, padx=18, pady=8)
        footer.pack(side="bottom", fill="x")
        graph_box = tk.Frame(self.root, bg=CARD, padx=8, pady=8)
        graph_box.pack(fill="both", expand=True, padx=18, pady=(0, 4))
        bar = tk.Frame(graph_box, bg=CARD)
        bar.pack(fill="x")
        self.graph_title = tk.Label(bar, text="", bg=CARD, fg=TEXT, font=(FONT, 12, "bold"))
        self.graph_title.pack(side="left")
        self.range_buttons: dict[str, tk.Button] = {}
        for label, seconds in reversed(RANGES):
            button = tk.Button(bar, text=label, relief="flat", font=(FONT, 10), padx=8,
                               command=lambda s=seconds, l=label: self.set_range(s, l))
            button.pack(side="right", padx=2)
            self.range_buttons[label] = button
        self.figure = Figure(figsize=(8, 3.4), dpi=100)
        self.figure.subplots_adjust(left=0.08, right=0.98, top=0.95, bottom=0.14)
        self.axes = self.figure.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.figure, master=graph_box)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        self.record_button = tk.Button(footer, text="● 記録開始", command=self.toggle_record, font=(FONT, 11),
                                       bg="#c0392b", fg="white", activebackground="#a93226", relief="flat", padx=12)
        self.record_button.pack(side="left")
        tk.Button(footer, text="ログを開く…", command=self.open_log, font=(FONT, 11), relief="flat", padx=10).pack(side="left", padx=8)
        tk.Button(footer, text="ライブに戻る", command=self.start_live, font=(FONT, 11), relief="flat", padx=10).pack(side="left")
        tk.Label(footer, textvariable=self.status_var, bg=BG, fg=MUTED, font=(FONT, 10)).pack(side="right")
        self.select(self.selected)
        self.set_range(3600, "1時間")

    # ---------- sources ----------
    def stop_source(self) -> None:
        if self.source_thread is not None:
            self.source_thread.stop_event.set()
            self.source_thread = None
        self.queue = queue.Queue()

    def reset_data(self) -> None:
        self.series = Series()
        self.latest = {}
        self.latest_meta = {}
        self.last_heard = None
        self.last_key = None
        self.plot_dirty = True

    def start_live(self) -> None:
        self.stop_source()
        self.reset_data()
        self.mode = "live"
        self.source_thread = BleThread(self.device, self.queue)
        self.source_thread.start()
        self.record_button.configure(state="normal")
        self.status_var.set(f"ライブ（Bluetooth、{self.device or 'すべての Tempo Disc'} を受信中）")

    def start_follow(self, path: Path) -> None:
        if self.record_handle is not None:
            self.toggle_record()
        self.stop_source()
        self.reset_data()
        self.mode = "follow"
        self.source_thread = FollowThread(Path(path), self.queue)
        self.source_thread.start()
        self.record_button.configure(state="disabled")
        self.status_var.set(f"ログ表示: {Path(path).name}")
        self.set_range(None, "全部")

    def open_log(self) -> None:
        start = Path.cwd()
        path = filedialog.askopenfilename(title="Tempo Disc のログ（env_log_*.csv）", initialdir=str(start),
                                          filetypes=[("CSV", "*.csv"), ("すべて", "*.*")])
        if path:
            self.start_follow(Path(path))

    # ---------- incoming data ----------
    def poll(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "reading":
                    self.on_reading(payload)
                elif kind == "row":
                    self.on_row(payload)
                elif kind == "error":
                    self.status_var.set(payload)
        except queue.Empty:
            pass
        if self.plot_dirty:
            self.redraw()
        self.root.after(300, self.poll)

    def on_reading(self, reading: TempoReading) -> None:
        values = {key: getattr(reading, key) for key, *_ in QUANTITIES}
        self.latest = values
        self.latest_meta = {
            "name": reading.name or reading.address, "model": f"{reading.model}（v{reading.version}）",
            "battery": str(reading.battery_percent), "rssi": "" if reading.rssi is None else str(reading.rssi),
            "interval": str(reading.log_interval_s), "address": reading.address,
        }
        self.last_heard = reading.received_unix_s
        if reading.raw_hex != self.last_key:
            self.last_key = reading.raw_hex
            self.series.add(reading.received_unix_s, values)
            self.plot_dirty = True
            if self.record_writer is not None:
                self.record_writer.writerow(reading_row(reading, time.time()))
                self.record_handle.flush()
        self.update_tiles()

    def on_row(self, row: dict[str, str]) -> None:
        if row.get("error") or not row.get("temperature_C"):
            return
        try:
            t = float(row.get("received_unix_s") or row["time_unix_s"])
        except (KeyError, ValueError):
            return
        values = row_values(row)
        key = row.get("received_unix_s") or row.get("time_unix_s")  # repeated rows share the reception time
        self.latest = values
        self.latest_meta = {
            "name": row.get("name") or row.get("address", ""),
            "model": f"Tempo Disc（v{row.get('version', '?')}）",
            "battery": row.get("battery_percent", ""), "rssi": row.get("rssi_dBm", ""),
            "interval": row.get("log_interval_s", ""), "address": row.get("address", ""),
        }
        self.last_heard = t
        if key != self.last_key:
            self.last_key = key
            self.series.add(t, values)
            self.plot_dirty = True
        self.update_tiles()

    # ---------- display ----------
    def update_tiles(self) -> None:
        meta = self.latest_meta
        self.title_var.set(meta.get("name") or self.device or "Tempo Disc")
        self.model_var.set(f"{meta.get('model', '')}  {meta.get('address', '')}  記録間隔 {meta.get('interval', '?')} s")
        self.battery_var.set(f"電池 {meta.get('battery', '--')} %")
        rssi = meta.get("rssi", "")
        self.signal_var.set(f"電波 {rssi} dBm" if rssi else "電波 --")
        for key, _label, _unit, _colour, digits in QUANTITIES:
            value = self.latest.get(key)
            widgets = self.tile_widgets[key]
            widgets["value"].configure(text="--" if value is None else f"{value:.{digits}f}")
            _, ys = self.series.window(key, self.range_s)
            if ys:
                widgets["span"].configure(text=f"最高 {max(ys):.{digits}f}  最低 {min(ys):.{digits}f}")

    def tick(self) -> None:
        if self.last_heard is not None:
            age = time.time() - self.last_heard
            if self.mode == "live":
                self.age_var.set(f"最終受信 {age:.0f} 秒前" if age < 120 else f"最終受信 {age / 60:.0f} 分前（届いていません）")
            else:
                self.age_var.set("最終の行 " + dt.datetime.fromtimestamp(self.last_heard).strftime("%m/%d %H:%M:%S"))
        if self.mode == "live" and int(time.time()) % 10 == 0:
            self.plot_dirty = True  # let the time axis move on while the value is unchanged
        if self.record_path is not None:
            self.status_var.set(f"記録中: {self.record_path.name}")
        self.root.after(1000, self.tick)

    def select(self, key: str) -> None:
        self.selected = key
        for name, widgets in self.tile_widgets.items():
            colour = next(c for k, _l, _u, c, _d in QUANTITIES if k == name)
            widgets["card"].configure(highlightbackground=colour if name == key else CARD)
        self.plot_dirty = True

    def set_range(self, seconds: float | None, label: str) -> None:
        self.range_s = seconds
        for name, button in self.range_buttons.items():
            button.configure(bg=HEADER if name == label else "#e3e8ef", fg="white" if name == label else TEXT)
        self.plot_dirty = True
        self.update_tiles()

    def redraw(self) -> None:
        self.plot_dirty = False
        key, label, unit, colour, _digits = next(q for q in QUANTITIES if q[0] == self.selected)
        self.graph_title.configure(text=f"{label} [{unit}]")
        xs, ys = self.series.window(key, self.range_s)
        ax = self.axes
        ax.clear()
        ax.grid(True, color="#e3e8ef")
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        if xs:
            ax.plot(xs, ys, color=colour, linewidth=1.8, drawstyle="steps-post")
            ax.fill_between(xs, ys, min(ys), color=colour, alpha=0.08, step="post")
            ax.plot(xs[-1:], ys[-1:], "o", color=colour)
            span = max(ys) - min(ys)
            pad = max(span * 0.15, 0.05 if key != "pressure_hPa" else 0.2)
            ax.set_ylim(min(ys) - pad, max(ys) + pad)
            # time axis: the chosen range up to now (live) or to the last row; the whole log for "全部"
            end = dt.datetime.fromtimestamp(max(time.time() if self.mode == "live" else 0.0, self.series.t[-1]))
            if self.range_s is not None:
                start = end - dt.timedelta(seconds=self.range_s)
            else:
                start = min(xs[0], end - dt.timedelta(minutes=5))
            ax.set_xlim(start, end + (end - start) * 0.02)
            locator = mdates.AutoDateLocator()
            ax.xaxis.set_major_locator(locator)
            ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        else:
            ax.text(0.5, 0.5, "データ待ち", ha="center", va="center", transform=ax.transAxes, color=MUTED)
        self.canvas.draw_idle()

    # ---------- recording ----------
    def toggle_record(self) -> None:
        if self.record_handle is None:
            default = f"env_log_app_{dt.datetime.now():%Y%m%d_%H%M%S}.csv"
            path = filedialog.asksaveasfilename(title="記録の保存先", initialfile=default, defaultextension=".csv",
                                                filetypes=[("CSV", "*.csv")])
            if not path:
                return
            self.record_path = Path(path)
            new = not self.record_path.exists() or self.record_path.stat().st_size == 0
            self.record_handle = self.record_path.open("a", encoding="utf-8", newline="")
            self.record_writer = csv.DictWriter(self.record_handle, fieldnames=CSV_COLUMNS)
            if new:
                self.record_writer.writeheader()
            self.record_button.configure(text="■ 記録停止", bg="#34495e", activebackground="#2c3e50")
        else:
            self.record_handle.close()
            messagebox.showinfo("記録停止", f"保存しました:\n{self.record_path}")
            self.record_handle = self.record_writer = None
            self.record_path = None
            self.record_button.configure(text="● 記録開始", bg="#c0392b", activebackground="#a93226")
            self.status_var.set("記録を止めました")

    def close(self) -> None:
        if self.record_handle is not None:
            self.record_handle.close()
        self.stop_source()
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", default=DEFAULT_DEVICE, help=f"sensor name or address part (default {DEFAULT_DEVICE}; '' = any)")
    parser.add_argument("--follow", type=Path, help="show this tempodisc_logger.py CSV instead of listening")
    args = parser.parse_args(argv)
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on scaled Windows displays
    except Exception:  # noqa: BLE001
        pass
    root = tk.Tk()
    App(root, args.device, args.follow)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
