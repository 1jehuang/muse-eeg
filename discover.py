#!/usr/bin/env python3
"""Scan for nearby Muse S devices over BLE."""

import asyncio
import sys
from muse_discovery import find_muse_devices


async def main():
    timeout = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    devices = await find_muse_devices(timeout=timeout)

    if not devices:
        print("\nNo Muse devices found. Make sure your Muse S is:")
        print("  1. Powered on")
        print("  2. Not connected to another device (phone app, etc.)")
        print("  3. In pairing/discoverable mode")
        sys.exit(1)

    print(f"\nFound {len(devices)} device(s):")
    for i, d in enumerate(devices, 1):
        print(f"  {i}. {d.name}  addr={d.address}  rssi={d.rssi} dBm")


if __name__ == "__main__":
    asyncio.run(main())
