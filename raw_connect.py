#!/usr/bin/env python3
"""
Direct BLE streaming from Muse S with proper connection sequence.

The Muse S requires:
  1. Connect, send halt, disconnect
  2. Reconnect (now all GATT characteristics are visible)
  3. Subscribe to each sensor channel
  4. Send preset + resume command
"""

import asyncio
from collections import Counter, defaultdict
from bleak import BleakClient

ADDRESS = "00:55:DA:B3:81:73"

CONTROL    = "273e0001-4c4d-454d-96be-f03bac821358"
AUX_L      = "273e0002-4c4d-454d-96be-f03bac821358"
TP9        = "273e0003-4c4d-454d-96be-f03bac821358"
AF7        = "273e0004-4c4d-454d-96be-f03bac821358"
AF8        = "273e0005-4c4d-454d-96be-f03bac821358"
TP10       = "273e0006-4c4d-454d-96be-f03bac821358"
AUX_R      = "273e0007-4c4d-454d-96be-f03bac821358"
REF_DRL    = "273e0008-4c4d-454d-96be-f03bac821358"
GYRO       = "273e0009-4c4d-454d-96be-f03bac821358"
ACCEL      = "273e000a-4c4d-454d-96be-f03bac821358"
TELEMETRY  = "273e000b-4c4d-454d-96be-f03bac821358"
PPG1       = "273e000f-4c4d-454d-96be-f03bac821358"
PPG2       = "273e0010-4c4d-454d-96be-f03bac821358"
PPG3       = "273e0011-4c4d-454d-96be-f03bac821358"
THERMO     = "273e0012-4c4d-454d-96be-f03bac821358"

EEG_UUIDS = {TP9: "TP9", AF7: "AF7", AF8: "AF8", TP10: "TP10", AUX_R: "AUX_R", AUX_L: "AUX_L", REF_DRL: "REF"}
IMU_UUIDS = {GYRO: "GYRO", ACCEL: "ACCEL"}
PPG_UUIDS = {PPG1: "PPG1", PPG2: "PPG2", PPG3: "PPG3"}
OTHER_UUIDS = {TELEMETRY: "TELEM", THERMO: "THERMO"}

ALL_SENSOR_UUIDS = {**EEG_UUIDS, **IMU_UUIDS, **PPG_UUIDS, **OTHER_UUIDS}

stats = Counter()


def cmd(s: str) -> bytes:
    return bytes([len(s) + 1] + [ord(c) for c in s] + [0x0A])


def make_handler(label: str):
    def handler(sender, data):
        stats[label] += 1
        n = stats[label]
        if n <= 3 or n % 500 == 0:
            print(f"  [{label:>6}] #{n:5d}  len={len(data)}  {data[:10].hex()}")
    return handler


def control_handler(sender, data):
    stats["CTRL"] += 1
    text = bytes(data[1:]).decode("ascii", errors="replace")
    print(f"  [  CTRL] {text.rstrip()}")


async def main():
    # Phase 1: Connect, halt, disconnect (forces GATT table refresh)
    print(f"Phase 1: Initial connect to {ADDRESS}...")
    client = BleakClient(ADDRESS, timeout=20.0)
    await client.connect()
    print(f"  Connected: {client.is_connected}")

    await client.start_notify(CONTROL, control_handler)
    await asyncio.sleep(0.2)

    await client.write_gatt_char(CONTROL, cmd("v6"), response=False)
    await asyncio.sleep(0.5)
    await client.write_gatt_char(CONTROL, cmd("s"), response=False)
    await asyncio.sleep(0.5)
    await client.write_gatt_char(CONTROL, cmd("h"), response=False)
    await asyncio.sleep(0.3)

    print("  Disconnecting...")
    await client.disconnect()
    await asyncio.sleep(1.5)

    # Phase 2: Reconnect — now all characteristics should be visible
    print(f"\nPhase 2: Reconnecting...")
    client = BleakClient(ADDRESS, timeout=20.0)
    await client.connect()
    print(f"  Connected: {client.is_connected}")

    available = {char.uuid for service in client.services for char in service.characteristics}
    print(f"  Characteristics found: {len(available)}")

    # Subscribe to control
    await client.start_notify(CONTROL, control_handler)

    # Subscribe to all sensor channels
    subscribed = []
    for uuid, label in ALL_SENSOR_UUIDS.items():
        if uuid in available:
            try:
                await client.start_notify(uuid, make_handler(label))
                subscribed.append(label)
            except Exception as e:
                print(f"  Could not subscribe to {label}: {e}")
        else:
            print(f"  {label} not available")

    print(f"  Subscribed: {subscribed}")

    # Set preset and start
    print("\nPhase 3: Starting stream...")
    await client.write_gatt_char(CONTROL, cmd("p21"), response=False)
    await asyncio.sleep(0.5)
    await client.write_gatt_char(CONTROL, cmd("d"), response=False)

    print("Streaming for 20 seconds...\n")
    await asyncio.sleep(20)

    # Halt and disconnect
    try:
        await client.write_gatt_char(CONTROL, cmd("h"), response=False)
        await asyncio.sleep(0.3)
    except:
        pass
    await client.disconnect()

    # Results
    print(f"\n{'='*50}")
    print("RESULTS")
    print(f"{'='*50}")
    for label, count in stats.most_common():
        print(f"  {label:>8}: {count:6d} packets")
    sensor_total = sum(v for k, v in stats.items() if k != "CTRL")
    print(f"  {'TOTAL':>8}: {sensor_total:6d} sensor packets")


if __name__ == "__main__":
    asyncio.run(main())
