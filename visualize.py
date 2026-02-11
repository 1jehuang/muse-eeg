#!/usr/bin/env python3
"""
Real-time visualization of all Muse S signals.

Uses numpy ring buffers and pyqtgraph for smooth rendering.

Usage:
    python visualize.py                        # auto-discover
    python visualize.py -a 00:55:DA:B3:81:73   # known address
    python visualize.py --no-log               # disable CSV logging
"""

import argparse
import asyncio
import csv
import datetime
import json
import os
import struct
import subprocess
import sys
import threading
import time

import numpy as np
from scipy.signal import welch, butter, filtfilt
import pyqtgraph as pg
from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QFont
from bleak import BleakClient, BleakScanner

# --- GATT UUIDs ---
CONTROL = "273e0001-4c4d-454d-96be-f03bac821358"
EEG_UUIDS = {
    "273e0003-4c4d-454d-96be-f03bac821358": "TP9",
    "273e0004-4c4d-454d-96be-f03bac821358": "AF7",
    "273e0005-4c4d-454d-96be-f03bac821358": "AF8",
    "273e0006-4c4d-454d-96be-f03bac821358": "TP10",
}
GYRO_UUID = "273e0009-4c4d-454d-96be-f03bac821358"
ACCEL_UUID = "273e000a-4c4d-454d-96be-f03bac821358"
TELEM_UUID = "273e000b-4c4d-454d-96be-f03bac821358"

EEG_RATE = 256
EEG_WINDOW = 5
EEG_BUF_LEN = EEG_RATE * EEG_WINDOW  # 1280
IMU_RATE = 52
IMU_WINDOW = 5
IMU_BUF_LEN = IMU_RATE * IMU_WINDOW  # 260

EEG_SCALE = 1000.0 / 2048.0
EEG_COLORS = ["#4fc3f7", "#81c784", "#ffb74d", "#e57373"]
IMU_COLORS = ["#ef5350", "#66bb6a", "#42a5f5"]

BANDS = {
    "delta": (0.5, 4),
    "theta": (4, 8),
    "alpha": (8, 13),
    "beta":  (13, 30),
    "gamma": (30, 50),
}
BAND_COLORS = ["#9575cd", "#4fc3f7", "#66bb6a", "#ffb74d", "#ef5350"]
BLINK_THRESHOLD = 150  # µV


def cmd(s: str) -> bytes:
    return bytes([len(s) + 1] + [ord(c) for c in s] + [0x0A])


def unpack_eeg(data: bytes) -> list[float]:
    samples = []
    for i in range(12):
        byte_idx = 2 + (i * 3) // 2
        if i % 2 == 0:
            if byte_idx + 1 < len(data):
                raw = (data[byte_idx] << 4) | (data[byte_idx + 1] >> 4)
            else:
                break
        else:
            if byte_idx + 1 < len(data):
                raw = ((data[byte_idx] & 0x0F) << 8) | data[byte_idx + 1]
            else:
                break
        samples.append((raw - 2048) * EEG_SCALE)
    return samples


def unpack_imu(data: bytes) -> list[list[float]]:
    samples = []
    for i in range(3):
        offset = 2 + i * 6
        if offset + 6 <= len(data):
            x, y, z = struct.unpack(">hhh", data[offset:offset + 6])
            samples.append([float(x), float(y), float(z)])
    return samples


class RingBuffer:
    """Fast numpy ring buffer — no copies on read."""

    __slots__ = ("_buf", "_len", "_idx")

    def __init__(self, capacity: int):
        self._buf = np.full(capacity, np.nan)
        self._len = capacity
        self._idx = 0

    def extend(self, values):
        n = len(values)
        if n == 0:
            return
        if n >= self._len:
            self._buf[:] = values[-self._len:]
            self._idx = 0
        else:
            end = self._idx + n
            if end <= self._len:
                self._buf[self._idx:end] = values
            else:
                first = self._len - self._idx
                self._buf[self._idx:] = values[:first]
                self._buf[:n - first] = values[first:]
            self._idx = end % self._len

    def get_ordered(self) -> np.ndarray:
        """Return data in time order (oldest first). Zero-copy when possible."""
        if self._idx == 0:
            return self._buf
        return np.concatenate((self._buf[self._idx:], self._buf[:self._idx]))


class DataLogger:
    def __init__(self, out_dir="muse_data"):
        os.makedirs(out_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_dir = os.path.join(out_dir, f"session_{stamp}")
        os.makedirs(self.session_dir, exist_ok=True)

        self.eeg_file = open(os.path.join(self.session_dir, "eeg.csv"), "w", newline="", buffering=1)
        self.eeg_writer = csv.writer(self.eeg_file)
        self.eeg_writer.writerow(["timestamp", "channel", "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9", "s10", "s11"])
        self.eeg_file.flush()

        self.accel_file = open(os.path.join(self.session_dir, "accel.csv"), "w", newline="", buffering=1)
        self.accel_writer = csv.writer(self.accel_file)
        self.accel_writer.writerow(["timestamp", "x", "y", "z"])
        self.accel_file.flush()

        self.gyro_file = open(os.path.join(self.session_dir, "gyro.csv"), "w", newline="", buffering=1)
        self.gyro_writer = csv.writer(self.gyro_file)
        self.gyro_writer.writerow(["timestamp", "x", "y", "z"])
        self.gyro_file.flush()

        self.eeg_count = 0
        self.imu_count = 0

    def log_eeg(self, channel: str, samples: list[float]):
        ts = time.time()
        self.eeg_writer.writerow([f"{ts:.6f}", channel] + [f"{s:.2f}" for s in samples])
        self.eeg_count += 1

    def log_accel(self, samples: list[list[float]]):
        ts = time.time()
        for s in samples:
            self.accel_writer.writerow([f"{ts:.6f}", s[0], s[1], s[2]])
        self.imu_count += 1

    def log_gyro(self, samples: list[list[float]]):
        ts = time.time()
        for s in samples:
            self.gyro_writer.writerow([f"{ts:.6f}", s[0], s[1], s[2]])

    def close(self):
        self.eeg_file.close()
        self.accel_file.close()
        self.gyro_file.close()

    @property
    def path(self):
        return self.session_dir


class FocusTracker:
    """Tracks niri window focus in a background thread."""

    def __init__(self, session_dir: str):
        self.session_dir = session_dir
        self.current_app = ""
        self.current_title = ""
        self.current_category = ""
        self._stop = False
        self._windows = {}
        self._last_focused_id = None

        self.focus_path = os.path.join(session_dir, "focus.csv")
        self._file = open(self.focus_path, "w", newline="", buffering=1)
        self._writer = csv.writer(self._file)
        self._writer.writerow(["timestamp", "window_id", "app_id", "title", "workspace_id", "category"])
        self._file.flush()

    @staticmethod
    def categorize(title: str, app_id: str) -> str:
        t = (title or "").lower()
        a = (app_id or "").lower()
        if any(k in t for k in ["chinese", "chin", "pinyin", "hanzi", "lesson", "dictation",
                                 "cumulative", "character-sheet", "textbook", "dialogue",
                                 "listening", "radical", "tone", "quiz"]):
            return "chinese_learning"
        if "firefox" in a or "chromium" in a:
            if any(k in t for k in ["github", "stackoverflow", "docs"]):
                return "coding_ref"
            if any(k in t for k in ["youtube", "reddit", "twitter", "news"]):
                return "media"
            return "browsing"
        if a in ("kitty", "foot", "footclient", "alacritty", "wezterm"):
            if any(k in t for k in ["code", "codex", "jcode", "claude", "nvim", "vim", "helix"]):
                return "coding"
            return "terminal"
        if "code" in a or "cursor" in a:
            return "coding"
        if "muse" in t or "eeg" in t:
            return "eeg_monitor"
        return "other"

    def _log(self, wid, app_id, title, workspace):
        cat = self.categorize(title, app_id)
        self.current_app = app_id or ""
        self.current_title = title or ""
        self.current_category = cat
        self._writer.writerow([f"{time.time():.6f}", wid, app_id, title, workspace, cat])

    def _run(self):
        try:
            r = subprocess.run(["niri", "msg", "-j", "focused-window"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                w = json.loads(r.stdout)
                self._log(w.get("id"), w.get("app_id"), w.get("title"), w.get("workspace_id"))
                self._last_focused_id = w.get("id")
        except:
            pass

        proc = subprocess.Popen(["niri", "msg", "-j", "event-stream"],
                                stdout=subprocess.PIPE, text=True)
        try:
            for line in proc.stdout:
                if self._stop:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if "WindowsChanged" in event:
                    for w in event["WindowsChanged"].get("windows", []):
                        self._windows[w["id"]] = w

                if "WindowFocusChanged" in event:
                    wid = event["WindowFocusChanged"].get("id")
                    if wid is not None and wid != self._last_focused_id:
                        self._last_focused_id = wid
                        w = self._windows.get(wid, {})
                        self._log(wid, w.get("app_id"), w.get("title"), w.get("workspace_id"))

                if "WindowOpenedOrChanged" in event:
                    w = event["WindowOpenedOrChanged"].get("window", {})
                    if w:
                        self._windows[w["id"]] = w
                        if w["id"] == self._last_focused_id and w.get("is_focused"):
                            self._log(w["id"], w.get("app_id"), w.get("title"), w.get("workspace_id"))
        finally:
            proc.terminate()
            self._file.close()

    def start(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        return t

    def stop(self):
        self._stop = True


class MuseData:
    def __init__(self):
        self.eeg = {ch: RingBuffer(EEG_BUF_LEN) for ch in ["TP9", "AF7", "AF8", "TP10"]}
        self.accel = {ax: RingBuffer(IMU_BUF_LEN) for ax in ["x", "y", "z"]}
        self.gyro = {ax: RingBuffer(IMU_BUF_LEN) for ax in ["x", "y", "z"]}
        self.battery = 0
        self.packet_count = 0
        self.connected = False
        self.status = "Disconnected"
        self.logger: DataLogger | None = None
        self.focus: FocusTracker | None = None


class MuseBLE:
    def __init__(self, address: str, data: MuseData):
        self.address = address
        self.data = data
        self._stop = False

    def _make_eeg_handler(self, ch_name):
        def handler(sender, raw):
            samples = unpack_eeg(raw)
            self.data.eeg[ch_name].extend(samples)
            self.data.packet_count += 1
            if self.data.logger:
                self.data.logger.log_eeg(ch_name, samples)
        return handler

    def _accel_handler(self, sender, raw):
        samples = unpack_imu(raw)
        for s in samples:
            self.data.accel["x"].extend([s[0]])
            self.data.accel["y"].extend([s[1]])
            self.data.accel["z"].extend([s[2]])
        if self.data.logger:
            self.data.logger.log_accel(samples)

    def _gyro_handler(self, sender, raw):
        samples = unpack_imu(raw)
        for s in samples:
            self.data.gyro["x"].extend([s[0]])
            self.data.gyro["y"].extend([s[1]])
            self.data.gyro["z"].extend([s[2]])
        if self.data.logger:
            self.data.logger.log_gyro(samples)

    def _telem_handler(self, sender, raw):
        if len(raw) >= 6:
            self.data.battery = int.from_bytes(raw[2:4], "big") / 100

    def _ctrl_handler(self, sender, data):
        pass

    async def _connect_phase1(self):
        """Phase 1: connect, halt, disconnect to wake up GATT table."""
        client = BleakClient(self.address, timeout=10.0)
        try:
            await client.connect()
            await client.start_notify(CONTROL, self._ctrl_handler)
            await asyncio.sleep(0.1)
            await client.write_gatt_char(CONTROL, cmd("h"), response=False)
            await asyncio.sleep(0.2)
        finally:
            try:
                await client.disconnect()
            except:
                pass
        await asyncio.sleep(1.0)

    async def _connect_and_stream(self) -> BleakClient:
        """Connect and start streaming. Does phase 1 only if needed."""
        client = BleakClient(self.address, timeout=10.0)
        await client.connect()

        available = {c.uuid for svc in client.services for c in svc.characteristics}
        eeg_available = any(uuid in available for uuid in EEG_UUIDS)

        if not eeg_available:
            self.data.status = "Waking GATT table..."
            await client.disconnect()
            await self._connect_phase1()
            client = BleakClient(self.address, timeout=10.0)
            await client.connect()
            available = {c.uuid for svc in client.services for c in svc.characteristics}

        await client.start_notify(CONTROL, self._ctrl_handler)

        for uuid, ch in EEG_UUIDS.items():
            if uuid in available:
                await client.start_notify(uuid, self._make_eeg_handler(ch))
        if ACCEL_UUID in available:
            await client.start_notify(ACCEL_UUID, self._accel_handler)
        if GYRO_UUID in available:
            await client.start_notify(GYRO_UUID, self._gyro_handler)
        if TELEM_UUID in available:
            await client.start_notify(TELEM_UUID, self._telem_handler)

        await client.write_gatt_char(CONTROL, cmd("p21"), response=False)
        await asyncio.sleep(0.2)
        await client.write_gatt_char(CONTROL, cmd("d"), response=False)
        return client

    async def _run(self):
        MAX_RETRIES = 5

        while not self._stop:
            client = None
            try:
                for attempt in range(1, MAX_RETRIES + 1):
                    if self._stop:
                        return

                    self.data.status = f"Connecting (attempt {attempt}/{MAX_RETRIES})..."

                    try:
                        client = await self._connect_and_stream()
                        break
                    except Exception as e:
                        self.data.status = f"Connect failed: {e}"
                        wait = min(attempt * 2, 10)
                        await asyncio.sleep(wait)
                else:
                    self.data.status = "Could not connect. Power cycle Muse and restart."
                    return

                self.data.connected = True
                self.data.status = "Streaming"

                while not self._stop:
                    if not client.is_connected:
                        raise ConnectionError("Device disconnected")
                    await asyncio.sleep(0.2)

            except Exception as e:
                self.data.connected = False
                self.data.status = f"Disconnected: {e} — reconnecting..."
                if client:
                    try:
                        await client.disconnect()
                    except:
                        pass
                    client = None
                await asyncio.sleep(3)
                continue

            finally:
                if client:
                    try:
                        await client.write_gatt_char(CONTROL, cmd("h"), response=False)
                        await asyncio.sleep(0.2)
                    except:
                        pass
                    try:
                        await client.disconnect()
                    except:
                        pass
                self.data.connected = False

        self.data.status = "Disconnected"

    def run_in_thread(self):
        self._loop = None
        def target():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._run())
            except Exception as e:
                self.data.status = f"Error: {e}"
                self.data.connected = False
        t = threading.Thread(target=target, daemon=True)
        t.start()
        return t

    def stop(self):
        self._stop = True

    def force_disconnect(self):
        """Force BLE disconnect via bluetoothctl as last resort."""
        try:
            subprocess.run(
                ["bluetoothctl", "disconnect", self.address],
                capture_output=True, timeout=3,
            )
        except:
            pass


class MuseWindow(QMainWindow):
    def __init__(self, data: MuseData):
        super().__init__()
        self.data = data
        self.setWindowTitle("Muse S — Live Signals")
        self.resize(1400, 1050)

        pg.setConfigOptions(antialias=False, background="#1e1e1e", foreground="#e0e0e0")
        pg.setConfigOption("useOpenGL", True)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        # Status bar
        status_layout = QHBoxLayout()
        self.status_label = QLabel("Connecting...")
        self.status_label.setFont(QFont("monospace", 11))
        self.status_label.setStyleSheet("color: #ffa726;")
        status_layout.addWidget(self.status_label)

        self.battery_label = QLabel("🔋 --")
        self.battery_label.setFont(QFont("monospace", 11))
        self.battery_label.setStyleSheet("color: #66bb6a;")
        self.battery_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        status_layout.addWidget(self.battery_label)

        self.pps_label = QLabel("0 pkt/s")
        self.pps_label.setFont(QFont("monospace", 11))
        self.pps_label.setStyleSheet("color: #90a4ae;")
        self.pps_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        status_layout.addWidget(self.pps_label)
        layout.addLayout(status_layout)

        # Focus bar
        self.focus_label = QLabel("🪟 —")
        self.focus_label.setFont(QFont("monospace", 10))
        self.focus_label.setStyleSheet("color: #b0bec5; padding: 2px 4px; background: #2a2a2a; border-radius: 3px;")
        layout.addWidget(self.focus_label)

        # Pre-compute x axes
        self.x_eeg = np.linspace(-EEG_WINDOW, 0, EEG_BUF_LEN)
        self.x_imu = np.linspace(-IMU_WINDOW, 0, IMU_BUF_LEN)

        # EEG plots
        eeg_channels = ["TP9", "AF7", "AF8", "TP10"]
        self.eeg_curves = {}
        for i, ch in enumerate(eeg_channels):
            pw = pg.PlotWidget(title=f"EEG — {ch}")
            pw.setLabel("left", "µV")
            pw.setYRange(-500, 500)
            pw.setXRange(-EEG_WINDOW, 0)
            pw.showGrid(x=True, y=True, alpha=0.15)
            pw.setMinimumHeight(130)
            pw.setDownsampling(auto=True, mode="peak")
            pw.setClipToView(True)
            curve = pw.plot(pen=pg.mkPen(EEG_COLORS[i], width=1.2))
            self.eeg_curves[ch] = curve
            layout.addWidget(pw)

        # --- Signal processing row ---
        sp_layout = QHBoxLayout()

        # Band power bar chart (TP9 average)
        self.band_plot = pg.PlotWidget(title="Band Power (TP9)")
        self.band_plot.setLabel("left", "dB")
        self.band_plot.setYRange(-10, 40)
        self.band_plot.showGrid(y=True, alpha=0.15)
        self.band_plot.setMinimumHeight(140)
        self.band_plot.getAxis("bottom").setTicks(
            [[(i, name) for i, name in enumerate(BANDS.keys())]]
        )
        self.band_bars = pg.BarGraphItem(
            x=list(range(len(BANDS))),
            height=[0] * len(BANDS),
            width=0.6,
            brushes=[pg.mkBrush(c) for c in BAND_COLORS],
        )
        self.band_plot.addItem(self.band_bars)
        sp_layout.addWidget(self.band_plot)

        # Alpha asymmetry gauge
        self.asym_plot = pg.PlotWidget(title="Alpha Asymmetry")
        self.asym_plot.setLabel("left", "ln(R)-ln(L)")
        self.asym_plot.setYRange(-2, 2)
        self.asym_plot.setXRange(-30, 0)
        self.asym_plot.showGrid(x=True, y=True, alpha=0.15)
        self.asym_plot.setMinimumHeight(140)
        self.asym_plot.addLine(y=0, pen=pg.mkPen("#555", width=1, style=Qt.PenStyle.DashLine))
        self.asym_curve = self.asym_plot.plot(pen=pg.mkPen("#ab47bc", width=2))
        self.asym_fill_pos = pg.FillBetweenItem(
            self.asym_curve, self.asym_plot.plot([0], [0], pen=pg.mkPen(None)),
            brush=pg.mkBrush(102, 187, 106, 60),
        )
        self.asym_plot.addItem(self.asym_fill_pos)
        self.asym_history = RingBuffer(30)  # 30 data points, ~1/sec
        sp_layout.addWidget(self.asym_plot)

        # Blink + stats panel
        stats_widget = QWidget()
        stats_layout = QVBoxLayout(stats_widget)
        stats_layout.setContentsMargins(8, 4, 8, 4)

        self.blink_label = QLabel("👁 Blinks: 0")
        self.blink_label.setFont(QFont("monospace", 14, QFont.Weight.Bold))
        self.blink_label.setStyleSheet("color: #4fc3f7;")
        self.blink_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stats_layout.addWidget(self.blink_label)

        self.blink_rate_label = QLabel("0/min")
        self.blink_rate_label.setFont(QFont("monospace", 11))
        self.blink_rate_label.setStyleSheet("color: #78909c;")
        self.blink_rate_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stats_layout.addWidget(self.blink_rate_label)

        self.asym_label = QLabel("α Asym: —")
        self.asym_label.setFont(QFont("monospace", 12))
        self.asym_label.setStyleSheet("color: #ab47bc;")
        self.asym_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stats_layout.addWidget(self.asym_label)

        self.dominant_label = QLabel("")
        self.dominant_label.setFont(QFont("monospace", 10))
        self.dominant_label.setStyleSheet("color: #78909c;")
        self.dominant_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stats_layout.addWidget(self.dominant_label)

        stats_widget.setMinimumHeight(140)
        stats_widget.setMaximumWidth(180)
        stats_widget.setStyleSheet("background: #2a2a2a; border-radius: 4px;")
        sp_layout.addWidget(stats_widget)
        layout.addLayout(sp_layout)

        # Blink detection state
        self.blink_count = 0
        self.blink_start_time = time.time()
        self._blink_filter_b, self._blink_filter_a = butter(4, [1 / (EEG_RATE/2), 10 / (EEG_RATE/2)], btype="band")
        self._sp_update_counter = 0

        # IMU row
        imu_layout = QHBoxLayout()

        self.accel_plot = pg.PlotWidget(title="Accelerometer")
        self.accel_plot.setLabel("left", "raw")
        self.accel_plot.showGrid(x=True, y=True, alpha=0.15)
        self.accel_plot.setXRange(-IMU_WINDOW, 0)
        self.accel_plot.setDownsampling(auto=True, mode="peak")
        self.accel_plot.setClipToView(True)
        self.accel_plot.addLegend(offset=(10, 10))
        self.accel_plot.setMinimumHeight(150)
        self.accel_curves = {}
        for i, ax in enumerate(["x", "y", "z"]):
            self.accel_curves[ax] = self.accel_plot.plot(
                pen=pg.mkPen(IMU_COLORS[i], width=1.2), name=ax.upper()
            )
        imu_layout.addWidget(self.accel_plot)

        self.gyro_plot = pg.PlotWidget(title="Gyroscope")
        self.gyro_plot.setLabel("left", "raw")
        self.gyro_plot.showGrid(x=True, y=True, alpha=0.15)
        self.gyro_plot.setXRange(-IMU_WINDOW, 0)
        self.gyro_plot.setDownsampling(auto=True, mode="peak")
        self.gyro_plot.setClipToView(True)
        self.gyro_plot.addLegend(offset=(10, 10))
        self.gyro_plot.setMinimumHeight(150)
        self.gyro_curves = {}
        for i, ax in enumerate(["x", "y", "z"]):
            self.gyro_curves[ax] = self.gyro_plot.plot(
                pen=pg.mkPen(IMU_COLORS[i], width=1.2), name=ax.upper()
            )
        imu_layout.addWidget(self.gyro_plot)
        layout.addLayout(imu_layout)

        # Update timer — 60 fps
        self.last_packet_count = 0
        self.last_pps_time = time.time()
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_plots)
        self.timer.start(16)  # ~60 fps

    def update_plots(self):
        self.status_label.setText(f"⬤ {self.data.status}")
        color = "#66bb6a" if self.data.connected else "#ffa726"
        self.status_label.setStyleSheet(f"color: {color};")

        if self.data.battery > 0:
            self.battery_label.setText(f"🔋 {self.data.battery:.0f}%")

        now = time.time()
        dt = now - self.last_pps_time
        if dt >= 1.0:
            pps = (self.data.packet_count - self.last_packet_count) / dt
            self.pps_label.setText(f"{pps:.0f} pkt/s")
            self.last_packet_count = self.data.packet_count
            self.last_pps_time = now

        # --- Signal processing (every 5th frame = ~12 Hz) ---
        self._sp_update_counter += 1
        if self._sp_update_counter % 5 == 0:
            self._update_signal_processing()

        if self.data.focus and self.data.focus.current_title:
            cat = self.data.focus.current_category
            cat_colors = {
                "chinese_learning": "#ff7043",
                "coding": "#42a5f5",
                "terminal": "#78909c",
                "browsing": "#ab47bc",
                "media": "#ec407a",
                "coding_ref": "#26a69a",
                "eeg_monitor": "#66bb6a",
            }
            c = cat_colors.get(cat, "#b0bec5")
            title = self.data.focus.current_title
            if len(title) > 60:
                title = title[:57] + "..."
            self.focus_label.setText(f"🪟 [{cat}] {self.data.focus.current_app} — {title}")
            self.focus_label.setStyleSheet(f"color: {c}; padding: 2px 4px; background: #2a2a2a; border-radius: 3px;")

        for ch, curve in self.eeg_curves.items():
            curve.setData(self.x_eeg, self.data.eeg[ch].get_ordered())

        for ax, curve in self.accel_curves.items():
            curve.setData(self.x_imu, self.data.accel[ax].get_ordered())

        for ax, curve in self.gyro_curves.items():
            curve.setData(self.x_imu, self.data.gyro[ax].get_ordered())

    def _update_signal_processing(self):
        """Compute band powers, blinks, and alpha asymmetry."""
        tp9 = self.data.eeg["TP9"].get_ordered()
        tp10 = self.data.eeg["TP10"].get_ordered()
        af7 = self.data.eeg["AF7"].get_ordered()
        af8 = self.data.eeg["AF8"].get_ordered()

        valid = ~np.isnan(tp9)
        if valid.sum() < 256:
            return

        # Band powers from TP9 (last 2 seconds)
        seg = tp9[valid][-512:]
        seg = seg - np.mean(seg)
        try:
            freqs, psd = welch(seg, fs=EEG_RATE, nperseg=min(256, len(seg)))
            heights = []
            for lo, hi in BANDS.values():
                mask = (freqs >= lo) & (freqs <= hi)
                power = np.mean(psd[mask]) if mask.any() else 1e-10
                heights.append(10 * np.log10(power + 1e-10))
            self.band_bars.setOpts(height=heights)
        except:
            pass

        # Blink detection from frontal average (last 1 second)
        valid_f = ~np.isnan(af7) & ~np.isnan(af8)
        if valid_f.sum() > 256:
            frontal = (af7[valid_f][-256:] + af8[valid_f][-256:]) / 2
            try:
                filtered = filtfilt(self._blink_filter_b, self._blink_filter_a, frontal)
                abs_sig = np.abs(filtered)
                from scipy.signal import find_peaks
                peaks, _ = find_peaks(abs_sig, height=BLINK_THRESHOLD, distance=int(EEG_RATE * 0.3))
                if len(peaks) > 0:
                    self.blink_count += len(peaks)
            except:
                pass

        elapsed = time.time() - self.blink_start_time
        rate = self.blink_count / elapsed * 60 if elapsed > 5 else 0
        self.blink_label.setText(f"👁 Blinks: {self.blink_count}")
        self.blink_rate_label.setText(f"{rate:.0f}/min" if elapsed > 5 else "measuring...")

        # Alpha asymmetry
        valid_lr = ~np.isnan(tp9) & ~np.isnan(tp10)
        if valid_lr.sum() > 512:
            left = tp9[valid_lr][-512:]
            right = tp10[valid_lr][-512:]
            left = left - np.mean(left)
            right = right - np.mean(right)
            try:
                fl, pl = welch(left, fs=EEG_RATE, nperseg=256)
                fr, pr = welch(right, fs=EEG_RATE, nperseg=256)
                alpha_mask = (fl >= 8) & (fl <= 13)
                la = np.mean(pl[alpha_mask]) if alpha_mask.any() else 1e-10
                ra = np.mean(pr[alpha_mask]) if alpha_mask.any() else 1e-10
                asym = float(np.log(ra + 1e-10) - np.log(la + 1e-10))
                self.asym_history.extend([asym])

                hist = self.asym_history.get_ordered()
                valid_h = ~np.isnan(hist)
                if valid_h.sum() > 1:
                    x_asym = np.linspace(-30, 0, len(hist))
                    self.asym_curve.setData(x_asym[valid_h], hist[valid_h])

                self.asym_label.setText(f"α Asym: {asym:+.2f}")
                if asym > 0.2:
                    self.dominant_label.setText("→ Right dominant")
                    self.dominant_label.setStyleSheet("color: #66bb6a;")
                elif asym < -0.2:
                    self.dominant_label.setText("← Left dominant")
                    self.dominant_label.setStyleSheet("color: #ef5350;")
                else:
                    self.dominant_label.setText("≈ Balanced")
                    self.dominant_label.setStyleSheet("color: #78909c;")
            except:
                pass


async def find_muse(timeout=10.0):
    print(f"Scanning for Muse devices ({timeout}s)...")
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for addr, (device, adv) in devices.items():
        if device.name and "Muse" in device.name:
            print(f"  Found: {device.name} ({addr})")
            return addr
    return None


def main():
    p = argparse.ArgumentParser(description="Muse S real-time visualizer")
    p.add_argument("-a", "--address", help="BLE MAC address")
    p.add_argument("--no-log", action="store_true", help="Disable CSV logging")
    args = p.parse_args()

    address = args.address
    if not address:
        address = asyncio.run(find_muse())
        if not address:
            print("No Muse found.")
            sys.exit(1)

    data = MuseData()
    if not args.no_log:
        data.logger = DataLogger()
        data.focus = FocusTracker(data.logger.session_dir)
        print(f"Logging to: {data.logger.path}")
    ble = MuseBLE(address, data)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet("QMainWindow { background: #1e1e1e; } QLabel { color: #e0e0e0; }")

    window = MuseWindow(data)
    window.show()

    ble_thread = ble.run_in_thread()
    if data.focus:
        data.focus.start()

    def on_close():
        ble.stop()
        if data.focus:
            data.focus.stop()
        ble_thread.join(timeout=3)
        if ble_thread.is_alive():
            ble.force_disconnect()
        else:
            ble.force_disconnect()
        if data.logger:
            data.logger.close()
            print(f"\nData saved to: {data.logger.path}")
            print(f"  EEG packets: {data.logger.eeg_count}")
            print(f"  IMU packets: {data.logger.imu_count}")

    app.aboutToQuit.connect(on_close)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
