#!/usr/bin/env python3
"""
Replay and export a previously recorded Muse S binary session.

Usage:
    python export.py muse_data/muse_20250210_143000.bin          # print summary
    python export.py muse_data/muse_20250210_143000.bin --csv    # export CSVs
"""

import argparse
import csv
import datetime
import os
import sys

from muse_raw_stream import MuseRawStream
from muse_realtime_decoder import MuseRealtimeDecoder


def main():
    p = argparse.ArgumentParser(description="Export recorded Muse binary data")
    p.add_argument("binfile", help="Path to .bin recording")
    p.add_argument("--csv", action="store_true", help="Export to CSV files")
    p.add_argument("--outdir", default=None, help="Output directory (default: same as input)")
    args = p.parse_args()

    if not os.path.isfile(args.binfile):
        print(f"File not found: {args.binfile}")
        sys.exit(1)

    stream = MuseRawStream(args.binfile)
    decoder = MuseRealtimeDecoder()

    eeg_rows = []
    ppg_rows = []
    imu_rows = []
    hr_values = []

    print(f"Reading {args.binfile} …")
    info = stream.get_file_info()
    print(f"  Packets : {info['packet_count']}")
    print(f"  Size    : {info['file_size_mb']:.2f} MB")

    for ts, packet in stream.read_all():
        decoded = decoder.decode(packet, ts)

        if decoded.eeg:
            row = {"timestamp": ts.isoformat()}
            for ch, samples in decoded.eeg.items():
                row[ch] = samples[0] if samples else None
            eeg_rows.append(row)

        if decoded.ppg:
            for s in decoded.ppg.get("samples", []):
                ppg_rows.append({"timestamp": ts.isoformat(), "ppg": s})

        if decoded.imu:
            accel = decoded.imu.get("accel", [0, 0, 0])
            gyro = decoded.imu.get("gyro", [0, 0, 0])
            imu_rows.append({
                "timestamp": ts.isoformat(),
                "ax": accel[0], "ay": accel[1], "az": accel[2],
                "gx": gyro[0], "gy": gyro[1], "gz": gyro[2],
            })

        if decoded.heart_rate:
            hr_values.append({"timestamp": ts.isoformat(), "bpm": decoded.heart_rate})

    stats = decoder.get_stats()
    print(f"\nDecoded:")
    print(f"  EEG samples : {stats['eeg_samples']}")
    print(f"  PPG samples : {stats['ppg_samples']}")
    print(f"  IMU samples : {stats['imu_samples']}")
    print(f"  Errors      : {stats['decode_errors']}")
    if stats["last_heart_rate"]:
        print(f"  Last HR     : {stats['last_heart_rate']:.0f} BPM")

    if args.csv:
        out_dir = args.outdir or os.path.dirname(args.binfile) or "."
        os.makedirs(out_dir, exist_ok=True)
        base = os.path.splitext(os.path.basename(args.binfile))[0]

        for name, rows in [("eeg", eeg_rows), ("ppg", ppg_rows),
                           ("imu", imu_rows), ("hr", hr_values)]:
            if not rows:
                continue
            path = os.path.join(out_dir, f"{base}_{name}.csv")
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            print(f"  → {path}  ({len(rows)} rows)")


if __name__ == "__main__":
    main()
