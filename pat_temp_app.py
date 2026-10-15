#!/usr/bin/env python3
"""PAT temperature monitor: a desktop window for the DS18B20 sensors on the PAT boards (Tkinter + matplotlib).

    .\\venv\\Scripts\\python.exe pat_temp_app.py                          # read the Arduino (port auto)
    .\\venv\\Scripts\\python.exe pat_temp_app.py --port COM12
    .\\venv\\Scripts\\python.exe pat_temp_app.py --follow <pat_temp_log.csv>  # show a log being written (or a finished one)

Live mode opens the Arduino that runs ds18b20_serial_logger.ino (never an FTDI port: the PAT boards are
FTDI FT232H; see pat_temp_logger.py) and can write the same CSV as pat_temp_logger.py ("記録開始").
Only one program can hold a serial port: during a measurement the batch script runs pat_temp_logger.py,
so open its pat_temp_log_*.csv with "ログを開く" (or --follow) instead. The window only shows readings;
the measurement never waits for it.

The cards show the top / bottom PAT means (sensor map groups), their difference, and every sensor with
its change over the last 10 minutes (to see when the boards have settled). Click a sensor card to
emphasise its line; hover over the graph for the values at that time.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any

import matplotlib

matplotlib.use("TkAgg")
matplotlib.rcParams["font.family"] = ["Yu Gothic", "BIZ UDGothic", "Meiryo", "sans-serif"]
import matplotlib.dates as mdates  # noqa: E402
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

import pat_temp_logger as logger  # noqa: E402

# Warm neutral page, charcoal header with an amber rule; cards are white with a coloured left edge.
PAGE = "#f3f1ec"
HEADER = "#22262a"
HEADER_RULE = "#e8a33d"
HEADER_TEXT = "#f5f3ee"
HEADER_MUTED = "#a9a59b"
CARD = "#ffffff"
CARD_EDGE = "#e1e0d9"
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
CRITICAL = "#d03b3b"
WARNING = "#b07a00"
# Series colours: fixed order, one per sensor (the order of the sensor map, then unmapped sensors as
# they appear). A sensor keeps its colour whatever else is shown.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
GROUP_LABEL = {"top": "上PAT", "bottom": "下PAT"}
RANGES = [("10分", 600), ("1時間", 3600), ("6時間", 6 * 3600), ("24時間", 24 * 3600), ("全部", None)]
SLOPE_WINDOW_S = 600.0
MAX_PLOT_POINTS = 3000
FONT = "Yu Gothic UI"


# --------------------------------------------------------------------------------------- data model


class SensorSeries:
    def __init__(self, rom: str, name: str, group: str, slot: int) -> None:
        self.rom, self.name, self.group, self.slot = rom, name, group, slot
        self.t: list[float] = []
        self.v: list[float] = []
        self.status = ""
        self.last_t: float | None = None

    @property
    def colour(self) -> str:
        return SERIES[self.slot % len(SERIES)]

    def add(self, t: float, value: float | None, status: str) -> None:
        self.status = status
        self.last_t = t
        if value is not None:
            self.t.append(t)
            self.v.append(value)

    @property
    def latest(self) -> float | None:
        return self.v[-1] if self.v and self.status == "ok" else None

    def window(self, seconds: float | None, end: float | None = None) -> tuple[list[float], list[float]]:
        if not self.t:
            return [], []
        if seconds is None:
            return self.t, self.v
        start = (self.t[-1] if end is None else end) - seconds
        index = _first_at_or_after(self.t, start)
        return self.t[index:], self.v[index:]

    def slope_per_10min(self, end: float | None = None) -> float | None:
        """Least-squares slope over the last 10 min in °C per 10 min (None with less than 2 min of data)."""
        ts, vs = self.window(SLOPE_WINDOW_S, end)
        if len(ts) < 3 or ts[-1] - ts[0] < 120.0:
            return None
        n = len(ts)
        mean_t = sum(ts) / n
        mean_v = sum(vs) / n
        sxx = sum((t - mean_t) ** 2 for t in ts)
        if sxx <= 0:
            return None
        sxy = sum((t - mean_t) * (v - mean_v) for t, v in zip(ts, vs))
        return sxy / sxx * 600.0

    def value_at(self, t: float) -> float | None:
        """The last value at or before ``t`` (within 5 s)."""
        index = _first_at_or_after(self.t, t + 1e-9) - 1
        if index < 0 or t - self.t[index] > 5.0:
            return None
        return self.v[index]


def _first_at_or_after(values: list[float], x: float) -> int:
    lo, hi = 0, len(values)
    while lo < hi:
        mid = (lo + hi) // 2
        if values[mid] < x:
            lo = mid + 1
        else:
            hi = mid
    return lo


class Model:
    """All sensors, ordered by the sensor map first (stable colours), then as they appear."""

    def __init__(self, sensor_map: dict[str, logger.SensorInfo]) -> None:
        self.sensor_map = sensor_map
        self.map_order = list(sensor_map)
        self.sensors: dict[str, SensorSeries] = {}
        self.unmapped = 0

    def sensor(self, rom: str, name: str = "", group: str = "") -> SensorSeries:
        if rom not in self.sensors:
            info = self.sensor_map.get(rom)
            if info is not None:
                slot = self.map_order.index(rom)
                name, group = info.name, info.group
            else:
                slot = len(self.map_order) + self.unmapped
                self.unmapped += 1
            self.sensors[rom] = SensorSeries(rom, name or rom, group, slot)
        return self.sensors[rom]

    def ordered(self) -> list[SensorSeries]:
        return sorted(self.sensors.values(), key=lambda s: s.slot)

    def add_row(self, row: dict[str, str]) -> SensorSeries | None:
        if row.get("error") or not row.get("rom"):
            return None
        try:
            t = float(row["time_unix_s"])
        except (KeyError, ValueError):
            return None
        status = row.get("status") or "ok"
        try:
            value = float(row["temp_C"]) if status == "ok" and row.get("temp_C") else None
        except ValueError:
            value = None
        series = self.sensor(row["rom"], row.get("name", ""), row.get("group", ""))
        series.add(t, value, status)
        return series

    def group_latest(self, group: str) -> float | None:
        values = [s.latest for s in self.sensors.values() if s.group == group and s.latest is not None]
        return None if not values else sum(values) / len(values)

    def group_slope(self, group: str) -> float | None:
        values = [s.slope_per_10min() for s in self.sensors.values() if s.group == group]
        values = [v for v in values if v is not None]
        return None if not values else sum(values) / len(values)

    def last_time(self) -> float | None:
        times = [s.last_t for s in self.sensors.values() if s.last_t is not None]
        return max(times) if times else None


def thin(ts: list[float], vs: list[float], limit: int = MAX_PLOT_POINTS) -> tuple[list[float], list[float]]:
    if len(ts) <= limit:
        return ts, vs
    step = (len(ts) + limit - 1) // limit
    xs, ys = ts[::step], vs[::step]
    if xs[-1] != ts[-1]:
        xs, ys = xs + [ts[-1]], ys + [vs[-1]]
    return xs, ys


def format_slope(slope: float | None) -> str:
    return "10分の変化 --" if slope is None else f"10分の変化 {slope:+.2f} °C"


# ------------------------------------------------------------------------------------------ sources


class SerialThread(threading.Thread):
    """Reads the Arduino; rows (the logger's CSV rows) and messages go to a queue for the Tk thread."""

    def __init__(self, port: str, sensor_map: dict[str, logger.SensorInfo], out: queue.Queue) -> None:
        super().__init__(daemon=True)
        self.port = port
        self.sensor_map = sensor_map
        self.out = out
        self.stop_event = threading.Event()
        self.rescan_requested = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            link = None
            try:
                name = logger.choose_port(self.port, logger.list_ports())
                link = logger.open_link(name)
                state = logger.StreamState()
                pending: list[dict[str, Any]] = []
                if not logger.handshake(link, state, pending=pending):
                    raise RuntimeError(f"{name}: ds18b20_serial_logger が応答しません（スケッチを書き込みましたか）")
                self.out.put(("state", {"port": name, "state": state}))
                for event in pending:
                    self.out.put(("row", logger.reading_row(event, time.time(), name, self.sensor_map)))
                while not self.stop_event.is_set():
                    if self.rescan_requested.is_set():
                        self.rescan_requested.clear()
                        link.write(b"s")
                    raw = link.readline()
                    if not raw:
                        continue
                    event = logger.parse_line(raw.decode("ascii", errors="replace"))
                    if event is None:
                        continue
                    logger.apply_event(state, event)
                    if event["kind"] == "reading":
                        self.out.put(("row", logger.reading_row(event, time.time(), name, self.sensor_map)))
                    elif event["kind"] in {"rom", "sensors", "parasite", "banner"}:
                        self.out.put(("state", {"port": name, "state": state}))
            except Exception as exc:  # noqa: BLE001 - not plugged in, busy, or lost: report and retry
                text = str(exc)
                if "PermissionError" in repr(exc) or "Access is denied" in text or "アクセスが拒否" in text:
                    text = "ポートを別のプログラムが使っています（計測中なら「ログを開く」で表示してください）"
                self.out.put(("error", text))
                self.stop_event.wait(5.0)
            finally:
                if link is not None:
                    try:
                        link.close()
                    except Exception:  # noqa: BLE001
                        pass


class FollowThread(threading.Thread):
    """Tails a pat_temp_logger.py CSV (the rows already there first, then new ones)."""

    def __init__(self, path: Path, out: queue.Queue) -> None:
        super().__init__(daemon=True)
        self.path = path
        self.out = out
        self.stop_event = threading.Event()
        self.rescan_requested = threading.Event()

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
                self.out.put(("error", f"ログ: {exc}"))
                self.stop_event.wait(2.0)
                continue
            pending += chunk
            lines = pending.split("\n")
            pending = lines.pop()  # a half-written last line waits for the next read
            rows = []
            for line in lines:
                line = line.rstrip("\r")
                if not line:
                    continue
                fields = next(csv.reader([line]))
                if header is None:
                    header = fields
                    continue
                rows.append(dict(zip(header, fields)))
            if rows:
                self.out.put(("rows", rows))
            self.stop_event.wait(1.0)


# ---------------------------------------------------------------------------------------------- app


class App:
    def __init__(self, root: tk.Tk, port: str, sensor_map_path: Path | None, follow: Path | None) -> None:
        self.root = root
        self.port = port
        self.sensor_map_path = sensor_map_path
        self.sensor_map = logger.load_sensor_map(sensor_map_path)
        self.queue: queue.Queue = queue.Queue()
        self.model = Model(self.sensor_map)
        self.mode = "live"
        self.selected: str | None = None
        self.range_s: float | None = 3600
        self.source_thread: Any = None
        self.record_handle = None
        self.record_writer = None
        self.record_path: Path | None = None
        self.plot_dirty = True
        self.last_draw = 0.0
        self.sensor_cards: dict[str, dict[str, tk.Widget]] = {}
        self.hover_artists: list[Any] = []

        root.title("PAT 温度モニター")
        root.configure(bg=PAGE)
        root.geometry("1040x780")
        root.minsize(800, 600)
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
        header = tk.Frame(self.root, bg=HEADER, padx=20, pady=12)
        header.pack(fill="x")
        tk.Frame(self.root, bg=HEADER_RULE, height=3).pack(fill="x")
        left = tk.Frame(header, bg=HEADER)
        left.pack(side="left")
        tk.Label(left, text="PAT 温度モニター", bg=HEADER, fg=HEADER_TEXT, font=(FONT, 19, "bold")).pack(anchor="w")
        self.source_var = tk.StringVar(value="接続待ち…")
        tk.Label(left, textvariable=self.source_var, bg=HEADER, fg=HEADER_MUTED, font=(FONT, 10)).pack(anchor="w")
        right = tk.Frame(header, bg=HEADER)
        right.pack(side="right")
        self.age_var = tk.StringVar(value="")
        self.warn_var = tk.StringVar(value="")
        tk.Label(right, textvariable=self.age_var, bg=HEADER, fg=HEADER_TEXT, font=(FONT, 11)).pack(anchor="e")
        tk.Label(right, textvariable=self.warn_var, bg=HEADER, fg=HEADER_RULE, font=(FONT, 10)).pack(anchor="e")

        summary = tk.Frame(self.root, bg=PAGE, padx=14, pady=10)
        summary.pack(fill="x")
        self.summary_cards: dict[str, dict[str, tk.Label]] = {}
        for column, (key, title) in enumerate((("top", "上PAT（平均）"), ("bottom", "下PAT（平均）"), ("diff", "上 − 下"))):
            summary.columnconfigure(column, weight=1, uniform="summary")
            card = tk.Frame(summary, bg=CARD, highlightthickness=1, highlightbackground=CARD_EDGE)
            card.grid(row=0, column=column, sticky="nsew", padx=6)
            inner = tk.Frame(card, bg=CARD, padx=16, pady=10)
            inner.pack(fill="both", expand=True)
            tk.Label(inner, text=title, bg=CARD, fg=INK_2, font=(FONT, 11)).pack(anchor="w")
            line = tk.Frame(inner, bg=CARD)
            line.pack(anchor="w")
            value = tk.Label(line, text="--", bg=CARD, fg=INK, font=(FONT, 30, "bold"))
            value.pack(side="left")
            tk.Label(line, text=" °C", bg=CARD, fg=MUTED, font=(FONT, 12)).pack(side="left", anchor="s", pady=(0, 8))
            note = tk.Label(inner, text="", bg=CARD, fg=MUTED, font=(FONT, 10))
            note.pack(anchor="w")
            self.summary_cards[key] = {"value": value, "note": note}

        self.sensor_frame = tk.Frame(self.root, bg=PAGE, padx=14, pady=0)
        self.sensor_frame.pack(fill="x")
        for column in range(4):
            self.sensor_frame.columnconfigure(column, weight=1, uniform="sensor")
        self.empty_label = tk.Label(self.sensor_frame, text="センサーの値を待っています", bg=PAGE, fg=MUTED, font=(FONT, 11))
        self.empty_label.grid(row=0, column=0, columnspan=4, sticky="w", padx=6, pady=4)

        footer = tk.Frame(self.root, bg=PAGE, padx=20, pady=8)
        footer.pack(side="bottom", fill="x")
        graph_box = tk.Frame(self.root, bg=SURFACE, highlightthickness=1, highlightbackground=CARD_EDGE, padx=8, pady=8)
        graph_box.pack(fill="both", expand=True, padx=20, pady=(10, 4))
        bar = tk.Frame(graph_box, bg=SURFACE)
        bar.pack(fill="x")
        tk.Label(bar, text="温度 [°C]", bg=SURFACE, fg=INK, font=(FONT, 12, "bold")).pack(side="left")
        tk.Label(bar, text="  カードを押すとその線を強調、グラフにマウスを重ねるとその時刻の値", bg=SURFACE, fg=MUTED,
                 font=(FONT, 9)).pack(side="left")
        self.range_buttons: dict[str, tk.Button] = {}
        for label, seconds in reversed(RANGES):
            button = tk.Button(bar, text=label, relief="flat", font=(FONT, 10), padx=9, bd=0,
                               command=lambda s=seconds, l=label: self.set_range(s, l))
            button.pack(side="right", padx=2)
            self.range_buttons[label] = button
        self.figure = Figure(figsize=(8, 3.4), dpi=100, facecolor=SURFACE)
        self.figure.subplots_adjust(left=0.07, right=0.985, top=0.9, bottom=0.13)
        self.axes = self.figure.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.figure, master=graph_box)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.canvas.mpl_connect("motion_notify_event", self.on_hover)
        self.canvas.mpl_connect("axes_leave_event", lambda _e: self.clear_hover(redraw=True))

        self.record_button = tk.Button(footer, text="● 記録開始", command=self.toggle_record, font=(FONT, 11),
                                       bg=HEADER, fg=HEADER_TEXT, activebackground="#3a4046",
                                       activeforeground=HEADER_TEXT, relief="flat", padx=14, bd=0)
        self.record_button.pack(side="left")
        for text, command in (("ログを開く…", self.open_log), ("ライブに戻る", self.start_live),
                              ("センサーを探し直す", self.rescan)):
            tk.Button(footer, text=text, command=command, font=(FONT, 11), relief="flat", padx=10, bd=0,
                      bg="#e6e3dc", activebackground="#d8d4ca").pack(side="left", padx=(8, 0))
        self.status_var = tk.StringVar(value="")
        tk.Label(footer, textvariable=self.status_var, bg=PAGE, fg=INK_2, font=(FONT, 10)).pack(side="right")
        self.set_range(3600, "1時間")

    def ensure_card(self, series: SensorSeries) -> None:
        if series.rom in self.sensor_cards:
            return
        self.empty_label.grid_remove()
        card = tk.Frame(self.sensor_frame, bg=CARD, highlightthickness=1, highlightbackground=CARD_EDGE, cursor="hand2")
        edge = tk.Frame(card, bg=series.colour, width=6)
        edge.pack(side="left", fill="y")
        inner = tk.Frame(card, bg=CARD, padx=12, pady=8)
        inner.pack(side="left", fill="both", expand=True)
        top = tk.Frame(inner, bg=CARD)
        top.pack(fill="x")
        name = tk.Label(top, text=series.name, bg=CARD, fg=INK, font=(FONT, 11, "bold"))
        name.pack(side="left")
        badge_text = GROUP_LABEL.get(series.group, series.group or "未登録")
        badge = tk.Label(top, text=badge_text, bg="#efece6", fg=INK_2, font=(FONT, 9), padx=6)
        badge.pack(side="right")
        value = tk.Label(inner, text="--", bg=CARD, fg=INK, font=(FONT, 24, "bold"))
        value.pack(anchor="w")
        status = tk.Label(inner, text="", bg=CARD, fg=MUTED, font=(FONT, 9))
        status.pack(anchor="w")
        span = tk.Label(inner, text="", bg=CARD, fg=MUTED, font=(FONT, 9))
        span.pack(anchor="w")
        for widget in (card, edge, inner, top, name, badge, value, status, span):
            widget.bind("<Button-1>", lambda _e, rom=series.rom: self.select(rom))
        self.sensor_cards[series.rom] = {"card": card, "value": value, "status": status, "span": span}
        self.layout_cards()

    def layout_cards(self) -> None:
        for index, series in enumerate(self.model.ordered()):
            widgets = self.sensor_cards.get(series.rom)
            if widgets is not None:
                widgets["card"].grid(row=index // 4, column=index % 4, sticky="nsew", padx=6, pady=4)

    # ---------- sources ----------
    def stop_source(self) -> None:
        if self.source_thread is not None:
            self.source_thread.stop_event.set()
            self.source_thread = None
        self.queue = queue.Queue()

    def reset_data(self) -> None:
        self.model = Model(self.sensor_map)
        for widgets in self.sensor_cards.values():
            widgets["card"].destroy()
        self.sensor_cards = {}
        self.empty_label.grid()
        self.selected = None
        self.warn_var.set("")
        self.plot_dirty = True

    def start_live(self) -> None:
        self.stop_source()
        self.reset_data()
        self.mode = "live"
        self.source_thread = SerialThread(self.port, self.sensor_map, self.queue)
        self.source_thread.start()
        self.record_button.configure(state="normal")
        self.source_var.set(f"ライブ（Arduino、ポート {self.port}）  接続待ち…")
        self.status_var.set("")

    def start_follow(self, path: Path) -> None:
        if self.record_handle is not None:
            self.toggle_record()
        self.stop_source()
        self.reset_data()
        self.mode = "follow"
        self.source_thread = FollowThread(Path(path), self.queue)
        self.source_thread.start()
        self.record_button.configure(state="disabled")
        self.source_var.set(f"ログ表示: {Path(path).name}")
        self.status_var.set("")
        self.set_range(None, "全部")

    def open_log(self) -> None:
        path = filedialog.askopenfilename(title="PAT 温度のログ（pat_temp_log_*.csv）", initialdir=str(Path.cwd()),
                                          filetypes=[("CSV", "*.csv"), ("すべて", "*.*")])
        if path:
            self.start_follow(Path(path))

    def rescan(self) -> None:
        if self.mode == "live" and self.source_thread is not None:
            self.source_thread.rescan_requested.set()
            self.status_var.set("センサーを探し直しています")

    # ---------- incoming data ----------
    def poll(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "row":
                    self.on_rows([payload])
                    if self.record_writer is not None:
                        self.record_writer.writerow(payload)
                        self.record_handle.flush()
                elif kind == "rows":
                    self.on_rows(payload)
                elif kind == "state":
                    self.on_state(payload)
                elif kind == "error":
                    self.status_var.set(payload)
        except queue.Empty:
            pass
        if self.plot_dirty and time.time() - self.last_draw >= 1.0:
            self.redraw()
        self.root.after(300, self.poll)

    def on_rows(self, rows: list[dict[str, str]]) -> None:
        changed = False
        for row in rows:
            series = self.model.add_row(row)
            if series is not None:
                self.ensure_card(series)
                changed = True
            elif row.get("error"):
                self.warn_var.set(f"⚠ {row['error'][:60]}")
        if changed:
            self.plot_dirty = True
            self.update_cards()

    def on_state(self, payload: dict[str, Any]) -> None:
        state: logger.StreamState = payload["state"]
        count = "?" if state.sensor_count is None else state.sensor_count
        self.source_var.set(f"ライブ（Arduino {payload['port']}）  {state.banner or 'ds18b20_serial_logger'}  センサー {count} 個")
        if state.parasite_power:
            self.warn_var.set("⚠ 寄生給電になっています（3 本線でつないでください）")
        unmapped = [rom for rom in state.roms if rom not in self.sensor_map]
        if unmapped and self.sensor_map:
            self.status_var.set(f"対応表に無いセンサー: {', '.join(unmapped)}")
        elif unmapped:
            self.status_var.set("対応表（pat_temp_sensors.json）が無いので ROM ID で表示しています")
        else:
            self.status_var.set("")

    # ---------- display ----------
    def update_cards(self) -> None:
        for key in ("top", "bottom"):
            value = self.model.group_latest(key)
            widgets = self.summary_cards[key]
            widgets["value"].configure(text="--" if value is None else f"{value:.2f}")
            members = [s for s in self.model.sensors.values() if s.group == key]
            valid = sum(1 for s in members if s.latest is not None)
            widgets["note"].configure(text=f"{format_slope(self.model.group_slope(key))}  （有効 {valid}／{len(members)} 個）",
                                      fg=MUTED if valid == len(members) else CRITICAL)
        top, bottom = self.model.group_latest("top"), self.model.group_latest("bottom")
        diff = self.summary_cards["diff"]
        diff["value"].configure(text="--" if top is None or bottom is None else f"{top - bottom:+.2f}")
        diff["note"].configure(text="上下の平均の差")
        for series in self.model.ordered():
            widgets = self.sensor_cards.get(series.rom)
            if widgets is None:
                continue
            latest = series.latest
            widgets["value"].configure(text="--" if latest is None else f"{latest:.2f} °C")
            if series.status and series.status != "ok":
                text = {"disconnected": "⚠ 切断", "power_on_reset": "⚠ 電源リセット（85 °C）"}.get(series.status, f"⚠ {series.status}")
                widgets["status"].configure(text=text, fg=CRITICAL)
            else:
                widgets["status"].configure(text=format_slope(series.slope_per_10min()), fg=MUTED)
            _, vs = series.window(self.range_s)
            widgets["span"].configure(text="" if not vs else f"最高 {max(vs):.2f}  最低 {min(vs):.2f}")

    def tick(self) -> None:
        last = self.model.last_time()
        if last is not None:
            if self.mode == "live":
                age = time.time() - last
                self.age_var.set(f"最終受信 {age:.0f} 秒前" if age < 10 else f"⚠ 最終受信 {age:.0f} 秒前（届いていません）")
            else:
                self.age_var.set("最終の行 " + dt.datetime.fromtimestamp(last).strftime("%m/%d %H:%M:%S"))
        if self.record_path is not None:
            self.status_var.set(f"記録中: {self.record_path.name}")
        if self.mode == "live" and int(time.time()) % 10 == 0:
            self.plot_dirty = True
        self.root.after(1000, self.tick)

    def select(self, rom: str) -> None:
        self.selected = None if self.selected == rom else rom
        for key, widgets in self.sensor_cards.items():
            widgets["card"].configure(highlightbackground=INK if key == self.selected else CARD_EDGE,
                                      highlightthickness=2 if key == self.selected else 1)
        self.plot_dirty = True

    def set_range(self, seconds: float | None, label: str) -> None:
        self.range_s = seconds
        for name, button in self.range_buttons.items():
            chosen = name == label
            button.configure(bg=HEADER if chosen else "#ebe8e1", fg=HEADER_TEXT if chosen else INK_2,
                             activebackground="#3a4046" if chosen else "#d8d4ca")
        self.plot_dirty = True
        self.update_cards()

    def plot_end(self) -> float | None:
        last = self.model.last_time()
        if last is None:
            return None
        return max(time.time(), last) if self.mode == "live" else last

    def redraw(self) -> None:
        self.plot_dirty = False
        self.last_draw = time.time()
        self.hover_artists = []
        ax = self.axes
        ax.clear()
        ax.set_facecolor(SURFACE)
        ax.grid(True, color=GRID, linewidth=0.8)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(AXIS)
        ax.tick_params(colors=MUTED, labelsize=9)
        end = self.plot_end()
        drawn = []
        lows, highs = [], []
        for series in self.model.ordered():
            ts, vs = series.window(self.range_s, end)
            if not ts:
                continue
            ts, vs = thin(ts, vs)
            emphasised = self.selected is None or self.selected == series.rom
            xs = [dt.datetime.fromtimestamp(t) for t in ts]
            ax.plot(xs, vs, color=series.colour, linewidth=2.6 if self.selected == series.rom else 1.8,
                    alpha=1.0 if emphasised else 0.25, label=series.name, solid_capstyle="round")
            ax.plot(xs[-1:], vs[-1:], "o", color=series.colour, markersize=6, alpha=1.0 if emphasised else 0.25,
                    markeredgecolor=SURFACE, markeredgewidth=1.5)
            lows.append(min(vs))
            highs.append(max(vs))
            drawn.append((series, xs[-1], vs[-1]))
        if drawn:
            low, high = min(lows), max(highs)
            pad = max((high - low) * 0.12, 0.1)
            ax.set_ylim(low - pad, high + pad)
            end_dt = dt.datetime.fromtimestamp(end)
            if self.range_s is not None:
                start_dt = end_dt - dt.timedelta(seconds=self.range_s)
            else:
                first = min(s.t[0] for s in self.model.sensors.values() if s.t)
                start_dt = min(dt.datetime.fromtimestamp(first), end_dt - dt.timedelta(minutes=5))
            ax.set_xlim(start_dt, end_dt + (end_dt - start_dt) * 0.02)
            locator = mdates.AutoDateLocator()
            ax.xaxis.set_major_locator(locator)
            ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
            if len(drawn) >= 2:
                legend = ax.legend(loc="lower left", bbox_to_anchor=(0.0, 1.0), ncol=min(len(drawn), 4), frameon=False,
                                   fontsize=9, labelcolor=INK_2, handlelength=1.6, borderaxespad=0.2)
                for handle in getattr(legend, "legend_handles", None) or getattr(legend, "legendHandles", []):
                    handle.set_alpha(1.0)
        else:
            ax.text(0.5, 0.5, "データ待ち", ha="center", va="center", transform=ax.transAxes, color=MUTED)
        self.canvas.draw_idle()

    # ---------- hover ----------
    def clear_hover(self, redraw: bool = False) -> None:
        for artist in self.hover_artists:
            try:
                artist.remove()
            except (ValueError, NotImplementedError):
                pass
        self.hover_artists = []
        if redraw:
            self.canvas.draw_idle()

    def on_hover(self, event: Any) -> None:
        if event.inaxes is not self.axes or event.xdata is None or not self.model.sensors:
            return
        self.clear_hover()
        t = mdates.num2date(event.xdata).timestamp()
        lines = []
        for series in self.model.ordered():
            value = series.value_at(t)
            if value is not None:
                lines.append(f"{series.name}  {value:.2f} °C")
        if not lines:
            self.canvas.draw_idle()
            return
        when = dt.datetime.fromtimestamp(t).strftime("%H:%M:%S")
        guide = self.axes.axvline(event.xdata, color=AXIS, linewidth=1)
        box = self.axes.annotate(
            "\n".join([when] + lines), xy=(event.xdata, event.ydata), xytext=(12, 12), textcoords="offset points",
            fontsize=9, color=INK, va="bottom",
            ha="right" if event.x > self.canvas.get_tk_widget().winfo_width() * 0.6 else "left",
            bbox={"boxstyle": "round,pad=0.5", "fc": CARD, "ec": CARD_EDGE},
        )
        self.hover_artists = [guide, box]
        self.canvas.draw_idle()

    # ---------- recording ----------
    def toggle_record(self) -> None:
        if self.record_handle is None:
            default = f"pat_temp_log_app_{dt.datetime.now():%Y%m%d_%H%M%S}.csv"
            path = filedialog.asksaveasfilename(title="記録の保存先", initialfile=default, defaultextension=".csv",
                                                filetypes=[("CSV", "*.csv")])
            if not path:
                return
            self.record_path = Path(path)
            new = not self.record_path.exists() or self.record_path.stat().st_size == 0
            self.record_handle = self.record_path.open("a", encoding="utf-8", newline="")
            self.record_writer = csv.DictWriter(self.record_handle, fieldnames=logger.CSV_COLUMNS)
            if new:
                self.record_writer.writeheader()
            self.record_button.configure(text="■ 記録停止", bg=CRITICAL, activebackground="#b03030")
        else:
            self.record_handle.close()
            messagebox.showinfo("記録停止", f"保存しました:\n{self.record_path}")
            self.record_handle = self.record_writer = None
            self.record_path = None
            self.record_button.configure(text="● 記録開始", bg=HEADER, activebackground="#3a4046")
            self.status_var.set("記録を止めました")

    def close(self) -> None:
        if self.record_handle is not None:
            self.record_handle.close()
        self.stop_source()
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", default="auto", help="Arduino port (default auto; FTDI ports are never used)")
    parser.add_argument("--sensor-map", type=Path, default=None,
                        help=f"sensor map JSON (default {logger.DEFAULT_SENSOR_MAP.name} if present)")
    parser.add_argument("--follow", type=Path, help="show this pat_temp_logger.py CSV instead of reading the Arduino")
    args = parser.parse_args(argv)
    if args.sensor_map is None and logger.DEFAULT_SENSOR_MAP.is_file():
        args.sensor_map = logger.DEFAULT_SENSOR_MAP
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on scaled Windows displays
    except Exception:  # noqa: BLE001
        pass
    root = tk.Tk()
    App(root, args.port, args.sensor_map, args.follow)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
