#!/usr/bin/env python3
"""
Stream ALL data signals from a Muse S headband and print them in real time.

Signals captured:
  - EEG  : 4+2 channels @ 256 Hz  (TP9, AF7, AF8, TP10 + AUX_L, AUX_R)
  - IMU  : accelerometer + gyroscope
  - REF  : reference/DRL electrode
  - TELEM: battery, temperature

Usage:
    python stream.py                    # auto-discover, stream 60 s
    python stream.py -a XX:XX:XX:XX:XX:XX  # connect to known device
    python stream.py -d 0               # stream forever (Ctrl-C to stop)
    python stream.py --csv              # export to CSV files on exit
"""

import argparse
import asyncio
import csv
import datetime
import os
import signal
import sys
from collections import Counter
from bleak import BleakClient, BleakScanner

# GATT UUIDs
CONTROL = "273e0001-4c4d-454d-96be-f03bac821358"

EEG_UUIDS = {
    "273e0003-4c4d-454d-96be-f03bac821358": "TP9",
    "273e0004-4c4d-454d-96be-f03bac821358": "AF7",
    "273e0005-4c4d-454d-96be-f03bac821358": "AF8",
    "273e0006-4c4d-454d-96be-f03bac821358": "TP10",
    "273e0002-4c4d-454d-96be-f03bac821358": "AUX_L",
    "273e0007-4c4d-454d-96be-f03bac821358": "AUX_R",
}
OTHER_UUIDS = {
    "273e0008-4c4d-454d-96be-f03bac821358": "REF",
    "273e0009-4c4d-454d-96be-f03bac821358": "GYRO",
    "273e000a-4c4d-454d-96be-f03bac821358": "ACCEL",
    "273e000b-4c4d-454d-96be-f03bac821358": "TELEM",
}

ALL_SENSOR_UUIDS = {**EEG_UUIDS, **OTHER_UUIDS}

EEG_SCALE = 1000.0 / 2048.0  # 12-bit to microvolts


def cmd(s: str) -> bytes:
    return bytes([len(s) + 1] + [ord(c) for c in s] + [0x0A])


def unpack_eeg_samples(data: bytes) -> list[float]:
    """Unpack 12 x 12-bit EEG samples from a 20-byte packet (first 2 bytes = counter)."""
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


def unpack_imu_samples(data: bytes) -> list[list[float]]:
    """Unpack IMU (accel or gyro) samples from a 20-byte packet."""
    import struct
    samples = []
    for i in range(3):
        offset = 2 + i * 6
        if offset + 6 <= len(data):
            x, y, z = struct.unpack(">hhh", data[offset:offset + 6])
            samples.append([x, y, z])
    return samples


class DataCollector:
    def __init__(self):
        self.eeg = {name: [] for name in EEG_UUIDS.values()}
        self.accel = []
        self.gyro = []
        self.stats = Counter()

    def export_csv(self, out_dir="muse_data"):
        os.makedirs(out_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        files = []

        # EEG: one file with all channels
        eeg_len = min(len(v) for v in self.eeg.values()) if all(self.eeg.values()) else 0
        if eeg_len > 0:
            path = os.path.join(out_dir, f"eeg_{stamp}.csv")
            with open(path, "w", newline="") as f:
                channels = list(self.eeg.keys())
                writer = csv.writer(f)
                writer.writerow(["timestamp"] + channels)
                for i in range(eeg_len):
                    ts = self.eeg[channels[0]][i][0]
                    row = [ts] + [self.eeg[ch][i][1] for ch in channels]
                    writer.writerow(row)
            files.append((path, eeg_len))

        for name, data in [("accel", self.accel), ("gyro", self.gyro)]:
            if data:
                path = os.path.join(out_dir, f"{name}_{stamp}.csv")
                with open(path, "w", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["timestamp", "x", "y", "z"])
                    writer.writerows(data)
                files.append((path, len(data)))

        return files


async def find_muse(timeout=10.0):
    print(f"Scanning for Muse devices ({timeout}s)...")
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for addr, (device, adv) in devices.items():
        if device.name and "Muse" in device.name:
            print(f"  Found: {device.name} ({addr}) rssi={adv.rssi}")
            return addr
    return None


async def stream(address: str, duration: int, export_csv: bool, quiet: bool):
    collector = DataCollector()
    stop_event = asyncio.Event()

    def handle_sigint():
        print("\nStopping...")
        stop_event.set()

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, handle_sigint)

    def make_eeg_handler(ch_name):
        def handler(sender, data):
            collector.stats[ch_name] += 1
            ts = datetime.datetime.now().isoformat()
            samples = unpack_eeg_samples(data)
            for s in samples:
                collector.eeg[ch_name].append((ts, s))
            n = collector.stats[ch_name]
            if not quiet and (n <= 2 or n % 200 == 0):
                print(f"  [{ch_name:>6}] #{n:5d}  samples={[f'{s:+7.1f}' for s in samples[:3]]} µV")
        return handler

    def accel_handler(sender, data):
        collector.stats["ACCEL"] += 1
        ts = datetime.datetime.now().isoformat()
        samples = unpack_imu_samples(data)
        for s in samples:
            collector.accel.append([ts] + s)
        n = collector.stats["ACCEL"]
        if not quiet and (n <= 2 or n % 200 == 0):
            print(f"  [ ACCEL] #{n:5d}  {samples[0] if samples else '?'}")

    def gyro_handler(sender, data):
        collector.stats["GYRO"] += 1
        ts = datetime.datetime.now().isoformat()
        samples = unpack_imu_samples(data)
        for s in samples:
            collector.gyro.append([ts] + s)
        n = collector.stats["GYRO"]
        if not quiet and (n <= 2 or n % 200 == 0):
            print(f"  [  GYRO] #{n:5d}  {samples[0] if samples else '?'}")

    def telem_handler(sender, data):
        collector.stats["TELEM"] += 1
        if collector.stats["TELEM"] <= 3:
            print(f"  [ TELEM] #{collector.stats['TELEM']}  raw={data.hex()}")

    def control_handler(sender, data):
        text = bytes(data[1:]).decode("ascii", errors="replace")
        if "bp" in text:
            print(f"  [  INFO] {text.strip()}")

    # Phase 1: initial connect to wake up GATT table
    print(f"Connecting to {address} (phase 1)...")
    client = BleakClient(address, timeout=20.0)
    await client.connect()

    await client.start_notify(CONTROL, control_handler)
    await asyncio.sleep(0.1)
    await client.write_gatt_char(CONTROL, cmd("v6"), response=False)
    await asyncio.sleep(0.3)
    await client.write_gatt_char(CONTROL, cmd("s"), response=False)
    await asyncio.sleep(0.3)
    await client.write_gatt_char(CONTROL, cmd("h"), response=False)
    await asyncio.sleep(0.2)
    await client.disconnect()
    await asyncio.sleep(1.5)

    # Phase 2: reconnect — all characteristics now visible
    print(f"Reconnecting (phase 2)...")
    client = BleakClient(address, timeout=20.0)
    await client.connect()

    available = {c.uuid for svc in client.services for c in svc.characteristics}
    await client.start_notify(CONTROL, control_handler)

    # Subscribe EEG channels
    for uuid, ch_name in EEG_UUIDS.items():
        if uuid in available:
            await client.start_notify(uuid, make_eeg_handler(ch_name))

    # Subscribe IMU
    accel_uuid = "273e000a-4c4d-454d-96be-f03bac821358"
    gyro_uuid = "273e0009-4c4d-454d-96be-f03bac821358"
    telem_uuid = "273e000b-4c4d-454d-96be-f03bac821358"

    if accel_uuid in available:
        await client.start_notify(accel_uuid, accel_handler)
    if gyro_uuid in available:
        await client.start_notify(gyro_uuid, gyro_handler)
    if telem_uuid in available:
        await client.start_notify(telem_uuid, telem_handler)

    # Start streaming
    print("Starting stream...")
    await client.write_gatt_char(CONTROL, cmd("p21"), response=False)
    await asyncio.sleep(0.3)
    await client.write_gatt_char(CONTROL, cmd("d"), response=False)

    dur_str = "∞" if duration == 0 else f"{duration}s"
    print(f"Streaming ({dur_str})... Ctrl-C to stop\n")

    if duration > 0:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=duration)
        except asyncio.TimeoutError:
            pass
    else:
        await stop_event.wait()

    # Halt and disconnect
    try:
        await client.write_gatt_char(CONTROL, cmd("h"), response=False)
        await asyncio.sleep(0.2)
    except:
        pass
    await client.disconnect()

    # Summary
    print(f"\n{'='*50}")
    print("SESSION SUMMARY")
    print(f"{'='*50}")
    for label, count in collector.stats.most_common():
        print(f"  {label:>8}: {count:6d} packets")

    eeg_samples = sum(len(v) for v in collector.eeg.values())
    print(f"  EEG total: {eeg_samples} samples across {len(EEG_UUIDS)} channels")
    print(f"  Accel: {len(collector.accel)} samples")
    print(f"  Gyro:  {len(collector.gyro)} samples")

    if export_csv:
        print("\nExporting CSV...")
        for path, count in collector.export_csv():
            print(f"  {path} ({count} rows)")


def main():
    p = argparse.ArgumentParser(description="Stream Muse S EEG data")
    p.add_argument("-a", "--address", help="BLE MAC address (skip discovery)")
    p.add_argument("-d", "--duration", type=int, default=60, help="Seconds (0=infinite)")
    p.add_argument("--csv", action="store_true", help="Export CSV on exit")
    p.add_argument("-q", "--quiet", action="store_true", help="Suppress per-packet output")
    args = p.parse_args()

    async def run():
        address = args.address
        if not address:
            address = await find_muse()
            if not address:
                print("No Muse found.")
                sys.exit(1)

        await stream(address, args.duration, args.csv, args.quiet)

    asyncio.run(run())


if __name__ == "__main__":
    main()
