# Muse S (Gen 1) — Reverse Engineering Notes

Everything we learned about connecting to and streaming data from the InteraXon
Muse S (Gen 1) EEG headband on Linux. Most of this is **not documented anywhere**
by InteraXon — it was figured out through trial, error, and reading open-source
libraries.

---

## Device Info

| Field | Value |
|-------|-------|
| Model | Muse S (Gen 1) |
| Our device name | Muse-8173 |
| MAC address | `00:55:DA:B3:81:73` |
| Hardware revision | RevE, v3.1 |
| Firmware | 1.2.13 |
| Bootloader | 1.2.3 |
| Serial | 2031-4HAK3 |
| Charging port | Micro-USB |
| Battery life | ~10 hours |
| Type | Consumer |

## Physical Controls

- **Power button**: Small recessed button on the right side of the headband.
  Hold ~2 seconds to turn on/off.

### LED Indicators

| LED | Meaning |
|-----|---------|
| Blinking white | Advertising over BLE (discoverable) |
| Steady white | Powered on, may think it's connected |
| Breathing white (while plugged in) | Charging |
| Blinking orange | Low battery, charging |
| Steady green | Fully charged |
| No light | Off or dead battery |

**Important**: The Muse will auto-sleep after a period of inactivity. When it
wakes back up, it may need a power cycle (off, wait 3s, back on) to start
advertising again.

---

## Bluetooth Low Energy (BLE) Protocol

### Service UUID

All Muse characteristics live under a single service:

```
0000fe8d-0000-1000-8000-00805f9b34fb    (Interaxon Inc.)
```

### The Two-Phase Connection Problem

**This is the most important thing to know.** On first BLE connection, the Muse S
only exposes ONE GATT characteristic:

```
273e0001-4c4d-454d-96be-f03bac821358    Control (write + notify)
```

All the sensor characteristics (EEG channels, IMU, etc.) are **hidden**. You
cannot subscribe to them because they literally don't exist in the GATT table.

**The fix**: You must connect, send a halt command, disconnect, wait ~1.5 seconds,
then reconnect. On the second connection, all 15 characteristics appear.

```python
# Phase 1: wake up the GATT table
client = BleakClient(address)
await client.connect()
await client.start_notify(CONTROL_UUID, handler)
await client.write_gatt_char(CONTROL_UUID, cmd("h"), response=False)
await client.disconnect()
await asyncio.sleep(1.5)

# Phase 2: now everything is visible
client = BleakClient(address)
await client.connect()
# All sensor characteristics now available!
```

Nobody documents this. The `muselsl` library works around it by using `pygatt`
which does service discovery differently. The `amused` library tried to read
everything from the control characteristic (which doesn't work on Gen 1). We
figured this out by examining what characteristics appeared before and after
connection cycling.

### GATT Characteristics (after Phase 2 reconnect)

| UUID (short) | Full UUID | Name | Direction |
|-------------|-----------|------|-----------|
| `0001` | `273e0001-4c4d-454d-96be-f03bac821358` | **Control** | write + notify |
| `0002` | `273e0002-4c4d-454d-96be-f03bac821358` | EEG: AUX_L | notify |
| `0003` | `273e0003-4c4d-454d-96be-f03bac821358` | EEG: TP9 | notify |
| `0004` | `273e0004-4c4d-454d-96be-f03bac821358` | EEG: AF7 | notify |
| `0005` | `273e0005-4c4d-454d-96be-f03bac821358` | EEG: AF8 | notify |
| `0006` | `273e0006-4c4d-454d-96be-f03bac821358` | EEG: TP10 | notify |
| `0007` | `273e0007-4c4d-454d-96be-f03bac821358` | EEG: AUX_R | notify |
| `0008` | `273e0008-4c4d-454d-96be-f03bac821358` | Reference/DRL | notify |
| `0009` | `273e0009-4c4d-454d-96be-f03bac821358` | Gyroscope | notify |
| `000a` | `273e000a-4c4d-454d-96be-f03bac821358` | Accelerometer | notify |
| `000b` | `273e000b-4c4d-454d-96be-f03bac821358` | Telemetry | notify |

**Not present on Gen 1** (these are Muse 2 / Muse S Gen 2 only):

| UUID (short) | Name |
|-------------|------|
| `000f` | PPG channel 1 |
| `0010` | PPG channel 2 |
| `0011` | PPG channel 3 |
| `0012` | Thermistor |

### Control Commands

Commands are sent to `273e0001` as byte arrays. Format:
`[length+1, ...ascii_bytes..., 0x0A]`

Helper function:
```python
def cmd(s: str) -> bytes:
    return bytes([len(s) + 1] + [ord(c) for c in s] + [0x0A])
```

| Command | Bytes | Description |
|---------|-------|-------------|
| `v6` | `03 76 36 0a` | Request firmware version info |
| `s` | `02 73 0a` | Request device status (battery, serial, etc.) |
| `h` | `02 68 0a` | **Halt** — stop streaming |
| `d` | `02 64 0a` | **Resume** — start streaming |
| `p21` | `04 70 32 31 0a` | Set preset: basic EEG only |
| `p1034` | `06 70 31 30 33 34 0a` | Set preset: sleep mode |
| `p1035` | `06 70 31 30 33 35 0a` | Set preset: full sensors |
| `*1` | `03 2a 31 0a` | Reset (causes disconnect!) |

**Startup sequence**:
1. Subscribe to control characteristic notifications
2. Send `v6` (version) — optional, for device info
3. Send `s` (status) — optional, for battery level
4. Send `h` (halt) — stop any existing stream
5. Send `p21` (preset) — configure which sensors to enable
6. Send `d` (resume) — **start streaming**

### Control Response Format

Responses come as 20-byte notification packets on the control characteristic.
First byte is a length indicator, remaining bytes are ASCII JSON fragments.
Multi-packet responses must be concatenated:

```
Packet 1:  13 7b 22 61 70 22 3a 22 68 65 61 64 73 65 74 22 2c 00 00 00
           ^len  { " a  p  " :  " h  e  a  d  s  e  t  " ,  (padding)
```

#### Version response (`v6`)
```json
{
  "ap": "headset",
  "sp": "RevE",
  "tp": "consumer",
  "hw": "3.1",
  "bn": 27,
  "fw": "1.2.13",
  "bl": "1.2.3",
  "pv": 1,
  "rc": 0
}
```

#### Status response (`s`)
```json
{
  "hn": "Muse-8173",
  "sn": "2031-4HAK3",
  "ma": "00-55-da-b3-81-73",
  "id": "18473731 32313631 00290028",
  "bp": 55,
  "ts": 0,
  "ps": 32,
  "rc": 0
}
```

| Field | Meaning |
|-------|---------|
| `hn` | Hostname (device name) |
| `sn` | Serial number |
| `ma` | MAC address |
| `bp` | Battery percentage |
| `ts` | Timestamp (?) |
| `ps` | Unknown |
| `rc` | Return code (0 = OK) |

### Presets

| Preset | What it enables |
|--------|----------------|
| `p21` | Basic EEG (4 channels + 2 AUX + REF) — **recommended for Gen 1** |
| `p1034` | Sleep mode |
| `p1035` | Full sensors (EEG + PPG + IMU) — PPG won't work on Gen 1 |

For our Gen 1 device, `p21` is the right choice since PPG isn't available anyway.

---

## EEG Data

### Electrode Placement

```
         AF7    FPz*   AF8
          ●      ●      ●
         /  (forehead)   \
        /                 \
  TP9  ●                   ●  TP10
       (behind left ear)     (behind right ear)

  * FPz is the AUX channel — only useful with external electrode
```

### Channels

| Channel | Location | UUID (short) | Signal quality |
|---------|----------|-------------|----------------|
| **TP9** | Left temporal (behind ear) | `0003` | Good — firm skin contact |
| **AF7** | Left frontal (forehead) | `0004` | Good — prone to blink artifacts |
| **AF8** | Right frontal (forehead) | `0005` | Good — prone to blink artifacts |
| **TP10** | Right temporal (behind ear) | `0006` | Good — firm skin contact |
| AUX_L | Left auxiliary port | `0002` | Noise unless external electrode attached |
| AUX_R | Right auxiliary port | `0007` | Noise unless external electrode attached |

**Realistically 4 usable channels.** AUX_L and AUX_R are just noise without
external electrodes.

### EEG Packet Format

Each EEG notification is **20 bytes**:

```
[counter_hi] [counter_lo] [12 × 12-bit samples packed into 18 bytes]
```

- Bytes 0–1: Packet counter (big-endian uint16)
- Bytes 2–19: 12 EEG samples, each 12 bits, packed consecutively

**Unpacking 12-bit samples from packed bytes:**

```python
def unpack_eeg(data: bytes) -> list[float]:
    """Unpack 12 × 12-bit EEG samples from 20-byte packet."""
    samples = []
    for i in range(12):
        byte_idx = 2 + (i * 3) // 2
        if i % 2 == 0:
            raw = (data[byte_idx] << 4) | (data[byte_idx + 1] >> 4)
        else:
            raw = ((data[byte_idx] & 0x0F) << 8) | data[byte_idx + 1]
        # Convert 12-bit unsigned to microvolts
        # 2048 is the midpoint, scale factor is ~0.4883 µV/LSB
        samples.append((raw - 2048) * (1000.0 / 2048.0))
    return samples
```

### Sample Rate & Throughput

- **256 Hz** per channel
- 12 samples per packet → **~21.3 packets/sec per channel**
- 4 channels × 21.3 = **~85 EEG packets/sec** total
- Raw data rate: ~85 × 20 bytes = **~1.7 KB/sec** for EEG

### What the signals look like

| State | What you see |
|-------|-------------|
| Headband on table | Railed to ±500 µV or 50/60 Hz noise |
| On head, eyes open | Low-amplitude irregular waves (10–50 µV) |
| On head, eyes closed | **Alpha waves** (~10 Hz) appear on TP9/TP10 |
| Blinking | Large sharp spikes on AF7/AF8 (100–500 µV) |
| Jaw clench | Muscle artifact on all channels |

---

## IMU Data

### Packet Format

IMU notifications are **20 bytes**:

```
[counter_hi] [counter_lo] [3 × samples, each 6 bytes (3 × int16 big-endian)]
```

Each sample is 3 axes (X, Y, Z) as signed 16-bit big-endian integers:

```python
import struct

def unpack_imu(data: bytes) -> list[list[float]]:
    samples = []
    for i in range(3):
        offset = 2 + i * 6
        x, y, z = struct.unpack(">hhh", data[offset:offset + 6])
        samples.append([float(x), float(y), float(z)])
    return samples
```

### Sample Rates

- **Accelerometer**: ~52 Hz (3 samples per packet → ~17 packets/sec)
- **Gyroscope**: ~52 Hz (same)

### Scale Factors

From muselsl:
- Accelerometer: multiply by `6.10352e-05` to get g's
- Gyroscope: multiply by `0.0074768` to get degrees/sec

---

## Telemetry

Telemetry packets arrive infrequently (~1 per few seconds). Format varies but
bytes 2–3 contain battery level (big-endian uint16, divide by 100 for percent).

---

## Linux Setup

### Prerequisites

```bash
# Arch Linux
sudo pacman -S bluez bluez-utils

# Make sure bluetooth is running
sudo systemctl enable --now bluetooth
bluetoothctl show   # should show "Powered: yes"
```

### Python Environment

```bash
cd ~/muse-eeg
python -m venv .venv
source .venv/bin/activate
pip install bleak pyqtgraph PyQt6 numpy scipy pandas
```

### BLE Permissions

If you get permission errors, either:
- Run with `sudo`
- Add your user to the `bluetooth` group: `sudo usermod -aG bluetooth $USER`

---

## Libraries Evaluated

| Library | Status | Notes |
|---------|--------|-------|
| **bleak** | ✅ Works | Pure Python async BLE. Best option for Linux. |
| **amused** | ❌ Didn't work | Designed for Muse S but assumes all data on control char. Wrong for Gen 1. |
| **muselsl** | ⚠️ Partial | Scanner broken on Python 3.14. Muse class works but needs event loop fix. |
| **pygatt** | Not tested | Older, uses gatttool subprocess. muselsl's fallback. |
| **BlueMuse** | N/A | Windows only |

We ended up writing our own connection logic with bleak directly, using UUIDs
from muselsl's constants.

---

## Known Issues & Gotchas

1. **GATT table not populated on first connect** — Must do the two-phase
   connect-disconnect-reconnect dance. This is the #1 thing that will trip you up.

2. **Muse auto-sleeps** — After a period of no BLE activity, the Muse turns off.
   Sometimes needs a power cycle to wake up and start advertising again.

3. **muselsl scanner uses pexpect + bluetoothctl** — Breaks on newer Linux/Python
   versions. Just use bleak's `BleakScanner.discover()` instead.

4. **Python 3.14 event loop** — `asyncio.get_event_loop()` raises RuntimeError.
   Use `asyncio.new_event_loop()` / `asyncio.set_event_loop()` in threads,
   or `asyncio.run()` in main.

5. **No PPG on Gen 1** — The PPG characteristics (`273e000f/0010/0011`) simply
   don't exist. This is a Muse 2 / Muse S Gen 2 feature.

6. **`*1` reset command disconnects** — Sending the reset command causes the
   Muse to reboot, which drops the BLE connection and invalidates the GATT
   service cache. Don't use it during normal operation.

7. **Control response padding** — The 20-byte control notifications have trailing
   garbage/padding from previous messages. Parse only up to the length byte.

8. **CSV writer buffering** — Python's csv writer inherits file buffering. Use
   `buffering=1` (line buffered) when opening files for real-time logging, or
   data won't appear on disk until the buffer fills or the file closes.

---

## Project Files

```
~/muse-eeg/
├── .venv/                  # Python virtual environment
├── muse_data/              # Recorded sessions
│   └── session_YYYYMMDD_HHMMSS/
│       ├── eeg.csv         # EEG: timestamp, channel, 12 samples
│       ├── accel.csv       # Accelerometer: timestamp, x, y, z
│       └── gyro.csv        # Gyroscope: timestamp, x, y, z
├── discover.py             # Scan for Muse devices
├── stream.py               # CLI streaming with CSV export
├── visualize.py            # Real-time PyQt6/pyqtgraph visualizer + logging
├── raw_connect.py          # Low-level BLE debugging script
├── export.py               # Export .bin recordings to CSV
├── launch.sh               # Shell wrapper for visualizer
└── README.md               # Usage guide
```
