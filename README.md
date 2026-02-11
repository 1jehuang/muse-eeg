# Muse S EEG Streaming

Stream and visualize data from an [InteraXon Muse S](https://choosemuse.com/)
(Gen 1) EEG headband on Linux over Bluetooth Low Energy.

For in-depth protocol documentation, see **[PROTOCOL.md](PROTOCOL.md)**.

## Signals

| Signal | Channels | Rate | Notes |
|--------|----------|------|-------|
| **EEG** | TP9, AF7, AF8, TP10 (+2 AUX) | 256 Hz | 12-bit, values in µV |
| **Accelerometer** | X, Y, Z | 52 Hz | Raw int16 |
| **Gyroscope** | X, Y, Z | 52 Hz | Raw int16 |
| **Telemetry** | Battery, temperature | ~0.3 Hz | |

**No PPG** on Gen 1 (that's Muse 2 / Muse S Gen 2 only).

## Quick Start

```bash
cd ~/muse-eeg
source .venv/bin/activate

# 1. Turn on Muse S — wait for blinking white LED

# 2. Find it
python discover.py

# 3. Visualize (opens GUI window, logs to muse_data/)
python visualize.py -a 00:55:DA:B3:81:73

# Or use the desktop launcher: "Muse S EEG"
```

## Scripts

| Script | Description |
|--------|-------------|
| `discover.py` | Scan for nearby Muse devices |
| `visualize.py` | Real-time GUI with scrolling waveforms + CSV logging |
| `stream.py` | CLI streaming with optional CSV export |
| `raw_connect.py` | Low-level BLE debugging |
| `export.py` | Convert recorded `.bin` files to CSV |
| `launch.sh` | Shell wrapper for visualize.py |

## Visualizer Options

```bash
python visualize.py -a ADDRESS      # known MAC address
python visualize.py                  # auto-discover
python visualize.py --no-log        # disable CSV recording
```

Features:
- 4 EEG channel waveforms (5-second scrolling window)
- Accelerometer + Gyroscope plots
- Battery level display
- Packets/sec counter
- Auto-reconnect on disconnect
- CSV logging to `muse_data/session_TIMESTAMP/`

## Data Format

Sessions are saved to `muse_data/session_YYYYMMDD_HHMMSS/`:

**eeg.csv**: One row per 12-sample packet
```
timestamp,channel,s0,s1,s2,s3,s4,s5,s6,s7,s8,s9,s10,s11
1770773103.600671,TP10,-414.06,-847.17,-0.49,...
```

**accel.csv / gyro.csv**: One row per sample
```
timestamp,x,y,z
1770773103.612345,9078,-1471,13634
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| No devices found | Power cycle: off (hold 3s), wait, on (hold until blinking) |
| Stuck on "Connecting" | Muse went to sleep. Power cycle it. |
| Only control characteristic | This is normal on first connect — the code handles the two-phase reconnect automatically |
| Permission denied | `sudo usermod -aG bluetooth $USER` then re-login |
| Railed/flat EEG signals | Headband not on head, or poor electrode contact |

## Dependencies

- Python 3.10+
- bleak (BLE)
- PyQt6 + pyqtgraph (visualization)
- numpy, scipy, pandas
