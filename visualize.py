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
import os
import struct
import sys
import threading
import time

import numpy as np
import pyqtgraph as pg
from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel
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
        client = BleakClient(self.address, timeout=15.0)
        try:
            await client.connect()
            await client.start_notify(CONTROL, self._ctrl_handler)
            await asyncio.sleep(0.1)
            await client.write_gatt_char(CONTROL, cmd("h"), response=False)
            await asyncio.sleep(0.3)
        finally:
            try:
                await client.disconnect()
            except:
                pass
        await asyncio.sleep(1.5)

    async def _connect_phase2(self) -> BleakClient:
        """Phase 2: reconnect and subscribe to all sensors."""
        client = BleakClient(self.address, timeout=15.0)
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
        await asyncio.sleep(0.3)
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
                        await self._connect_phase1()
                        break
                    except Exception as e:
                        self.data.status = f"Phase 1 failed: {e}"
                        wait = min(attempt * 2, 10)
                        await asyncio.sleep(wait)
                else:
                    self.data.status = "Could not connect. Power cycle Muse and restart."
                    return

                self.data.status = "Connecting (phase 2)..."
                client = await self._connect_phase2()
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
        def target():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._run())
            except Exception as e:
                self.data.status = f"Error: {e}"
                self.data.connected = False
        t = threading.Thread(target=target, daemon=True)
        t.start()
        return t

    def stop(self):
        self._stop = True


class MuseWindow(QMainWindow):
    def __init__(self, data: MuseData):
        super().__init__()
        self.data = data
        self.setWindowTitle("Muse S — Live Signals")
        self.resize(1200, 900)

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

        for ch, curve in self.eeg_curves.items():
            curve.setData(self.x_eeg, self.data.eeg[ch].get_ordered())

        for ax, curve in self.accel_curves.items():
            curve.setData(self.x_imu, self.data.accel[ax].get_ordered())

        for ax, curve in self.gyro_curves.items():
            curve.setData(self.x_imu, self.data.gyro[ax].get_ordered())


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
        print(f"Logging to: {data.logger.path}")
    ble = MuseBLE(address, data)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet("QMainWindow { background: #1e1e1e; } QLabel { color: #e0e0e0; }")

    window = MuseWindow(data)
    window.show()

    ble_thread = ble.run_in_thread()

    def on_close():
        ble.stop()
        ble_thread.join(timeout=5)
        if data.logger:
            data.logger.close()
            print(f"\nData saved to: {data.logger.path}")
            print(f"  EEG packets: {data.logger.eeg_count}")
            print(f"  IMU packets: {data.logger.imu_count}")

    app.aboutToQuit.connect(on_close)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
