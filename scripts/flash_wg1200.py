#!/usr/bin/env python3
"""
WeatherXM WG1200 Non-Destructive Flashing & Rollback Utility.
=============================================================
Allows flashing DeskOS into OTA slot 0 (0x420000) while keeping
stock WeatherXM firmware (0x20000), certificates (0xD000), and
partition table (0xC000) completely intact.

Commands:
  python3 scripts/flash_wg1200.py --restore-partitions [-p PORT]
    Restores the stock partition table at 0xC000 so the WeatherXM Web Flasher works.

  python3 scripts/flash_wg1200.py --ota [-p PORT] [--bin PATH]
    Flashes DeskOS to ota_0 (0x420000) and activates it via otadata (0x13000).
    Leaves bootloader, factory firmware, certs, and partition table untouched.

  python3 scripts/flash_wg1200.py --rollback [-p PORT]
    Erases otadata (0x13000), immediately restoring factory WeatherXM firmware.

  python3 scripts/flash_wg1200.py --status [-p PORT]
    Reads device flash to report partition layout and active boot slot.
"""

from __future__ import annotations

import argparse
import binascii
import glob
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STOCK_WXM_PARTITIONS_CSV = ROOT / "partitions_wg1200.csv"
STOCK_WXM_PARTITIONS_BIN_CANDIDATES = [
    ROOT / ".pio" / "build" / "wg1200" / "partitions.bin",
    Path("/Users/manos/Documents/mesh/WG1400/wg1200-firmware/.pio/build/wg1200_release/partitions.bin"),
    ROOT / "build" / "partition_table" / "partition-table.bin",
]

DEFAULT_DESKOS_BIN_CANDIDATES = [
    ROOT / ".pio" / "build" / "wg1200" / "firmware.bin",
    ROOT / "build" / "meshcore_deskos_d1l.bin",
]

DEFAULT_OTADATA_BIN_CANDIDATES = [
    ROOT / ".pio" / "build" / "wg1200" / "ota_data_initial.bin",
    ROOT / "build" / "ota_data_initial.bin",
]


def resolve_existing_path(explicit: Path | None, candidates: list[Path]) -> Path:
    if explicit and explicit.is_file():
        return explicit
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]

PARTITION_TABLE_OFFSET = 0xC000
PARTITION_TABLE_SIZE = 0x1000
OTADATA_OFFSET = 0x13000
OTADATA_SIZE = 0x2000
OTA_0_OFFSET = 0x420000
FACTORY_OFFSET = 0x20000


def find_serial_port() -> str | None:
    """Find the USB serial port for the connected WG1200."""
    patterns = [
        "/dev/cu.usbmodem*",
        "/dev/cu.usbserial*",
        "/dev/cu.wchusbserial*",
        "/dev/ttyUSB*",
        "/dev/ttyACM*",
    ]
    for pat in patterns:
        matches = sorted(glob.glob(pat))
        if matches:
            return matches[0]
    return None


def run_esptool(port: str, args: list[str]) -> subprocess.CompletedProcess:
    """Execute esptool command with standard flags."""
    cmd = [
        sys.executable,
        "-m",
        "esptool",
        "--chip",
        "esp32s3",
        "-p",
        port,
        "-b",
        "460800",
        *args,
    ]
    print(f"[flash_wg1200] Running: {' '.join(cmd)}")
    return subprocess.run(cmd, check=True)


def get_stock_partition_binary() -> Path:
    """Locate or compile the authentic stock partition table binary."""
    for cand in STOCK_WXM_PARTITIONS_BIN_CANDIDATES:
        if cand.is_file() and cand.stat().st_size >= 3072:
            return cand

    # If binary is not cached, compile from partitions_wg1200.csv
    gen_candidates = list(Path.home().glob(".platformio/**/gen_esp32part.py"))
    if not gen_candidates:
        raise FileNotFoundError(
            "Neither prebuilt partitions.bin nor gen_esp32part.py could be found."
        )
    gen_script = gen_candidates[0]
    out_tmp = Path(tempfile.gettempdir()) / "wg1200_stock_partitions.bin"
    subprocess.run(
        [
            sys.executable,
            str(gen_script),
            "--offset",
            "0xc000",
            str(STOCK_WXM_PARTITIONS_CSV),
            str(out_tmp),
        ],
        check=True,
    )
    return out_tmp


def cmd_restore_partitions(port: str) -> int:
    """Restore the stock WeatherXM partition table to 0xC000."""
    part_bin = get_stock_partition_binary()
    print(f"\n=======================================================")
    print(f" Restoring Stock WG1200 Partition Table to Flash")
    print(f" Binary source: {part_bin}")
    print(f" Target offset: 0x{PARTITION_TABLE_OFFSET:X}")
    print(f" Port:          {port}")
    print(f"=======================================================\n")

    run_esptool(port, ["write_flash", f"0x{PARTITION_TABLE_OFFSET:X}", str(part_bin)])
    print("\n[SUCCESS] Stock partition table restored to 0xC000.")
    print("The WeatherXM Web Flasher will now recognize the device partition layout.\n")
    return 0


def cmd_rollback(port: str) -> int:
    """Erase otadata sector to revert active boot slot to factory firmware."""
    print(f"\n=======================================================")
    print(f" Rolling Back to Factory WeatherXM Firmware")
    print(f" Erasing otadata at 0x{OTADATA_OFFSET:X} (size 0x{OTADATA_SIZE:X})")
    print(f" Port: {port}")
    print(f"=======================================================\n")

    run_esptool(
        port,
        ["erase_region", f"0x{OTADATA_OFFSET:X}", f"0x{OTADATA_SIZE:X}"],
    )
    print("\n[SUCCESS] otadata erased. ESP32-S3 bootloader will boot factory WeatherXM firmware.\n")
    return 0


def cmd_flash_ota(port: str, app_bin: Path | None = None) -> int:
    """Flash DeskOS into ota_0 without touching factory firmware, certs, or partition table."""
    resolved_app_bin = resolve_existing_path(app_bin, DEFAULT_DESKOS_BIN_CANDIDATES)
    if not resolved_app_bin.is_file():
        print(f"Error: Application binary not found: {resolved_app_bin}", file=sys.stderr)
        return 1

    otadata_bin = resolve_existing_path(None, DEFAULT_OTADATA_BIN_CANDIDATES)
    if not otadata_bin.is_file():
        # Generate clean 8KB otadata with seq=1 pointing to ota_0
        otadata_bytes = bytearray(b"\xFF" * 8192)
        seq = 1
        crc = binascii.crc32(struct.pack("<I", seq), 0xFFFFFFFF) % (1 << 32)
        otadata_bytes[0:4] = struct.pack("<I", seq)
        otadata_bytes[28:32] = struct.pack("<I", crc)
        temp_otadata = Path(tempfile.gettempdir()) / "wg1200_ota_initial.bin"
        temp_otadata.write_bytes(otadata_bytes)
        otadata_bin = temp_otadata

    print(f"\n=======================================================")
    print(f" Non-Destructive WG1200 DeskOS Installation")
    print(f" Port:             {port}")
    print(f" App Binary:       {resolved_app_bin} -> 0x{OTA_0_OFFSET:X} (ota_0)")
    print(f" Otadata:          {otadata_bin} -> 0x{OTADATA_OFFSET:X}")
    print(f"-------------------------------------------------------")
    print(f" [PROTECTED] Bootloader (0x0):         UNTOUCHED")
    print(f" [PROTECTED] Partition Table (0xC000): UNTOUCHED")
    print(f" [PROTECTED] esp_secure_cert (0xD000): UNTOUCHED")
    print(f" [PROTECTED] factory firmware (0x20000): UNTOUCHED")
    print(f" [PROTECTED] nvs_key (0xC20000):       UNTOUCHED")
    print(f" [PROTECTED] spiffs (0xC21000):        UNTOUCHED")
    print(f"=======================================================\n")

    run_esptool(
        port,
        [
            "write_flash",
            f"0x{OTADATA_OFFSET:X}",
            str(otadata_bin),
            f"0x{OTA_0_OFFSET:X}",
            str(resolved_app_bin),
        ],
    )
    print("\n[SUCCESS] DeskOS flashed to ota_0 (0x420000).")
    print("Device will boot into DeskOS. Factory WeatherXM firmware remains untouched.\n")
    return 0


def cmd_status(port: str) -> int:
    """Read partition table and otadata from device to display status."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_part = Path(tmpdir) / "part.bin"
        tmp_ota = Path(tmpdir) / "otadata.bin"

        run_esptool(
            port,
            [
                "read_flash",
                f"0x{PARTITION_TABLE_OFFSET:X}",
                f"0x{PARTITION_TABLE_SIZE:X}",
                str(tmp_part),
            ],
        )
        run_esptool(
            port,
            [
                "read_flash",
                f"0x{OTADATA_OFFSET:X}",
                f"0x{OTADATA_SIZE:X}",
                str(tmp_ota),
            ],
        )

        part_data = tmp_part.read_bytes()
        ota_data = tmp_ota.read_bytes()

        # Parse partitions
        print(f"\n--- Detected Partitions on Flash (0xC000) ---")
        record_size = 32
        for offset in range(0, len(part_data), record_size):
            record = part_data[offset : offset + record_size]
            if len(record) < record_size:
                break
            if record[0:2] != b"\xaa\x50":
                break
            pos, size = struct.unpack("<II", record[4:12])
            name = record[12:28].split(b"\x00")[0].decode("ascii", errors="ignore")
            print(f"  - {name:<16} offset=0x{pos:06X} size=0x{size:06X} ({size // 1024} KB)")

        # Parse active slot from otadata
        sec0 = ota_data[0:4096]
        sec1 = ota_data[4096:8192]
        seq0, = struct.unpack("<I", sec0[0:4])
        crc0, = struct.unpack("<I", sec0[28:32])
        valid0 = (seq0 != 0xFFFFFFFF) and (crc0 == binascii.crc32(struct.pack("<I", seq0), 0xFFFFFFFF) % (1 << 32))

        seq1, = struct.unpack("<I", sec1[0:4])
        crc1, = struct.unpack("<I", sec1[28:32])
        valid1 = (seq1 != 0xFFFFFFFF) and (crc1 == binascii.crc32(struct.pack("<I", seq1), 0xFFFFFFFF) % (1 << 32))

        active_slot = "factory (WeatherXM)"
        if valid0 and not valid1:
            active_slot = f"ota_0 (DeskOS) [seq={seq0}]" if seq0 % 2 == 1 else f"ota_1 [seq={seq0}]"
        elif not valid0 and valid1:
            active_slot = f"ota_0 (DeskOS) [seq={seq1}]" if seq1 % 2 == 1 else f"ota_1 [seq={seq1}]"
        elif valid0 and valid1:
            seq = max(seq0, seq1)
            active_slot = f"ota_0 (DeskOS) [seq={seq}]" if seq % 2 == 1 else f"ota_1 [seq={seq}]"

        print(f"\nActive Boot Slot: {active_slot}\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="WeatherXM WG1200 Non-Destructive Flash & Recovery Utility"
    )
    parser.add_argument(
        "-p", "--port", help="Serial port of WG1200 (auto-detected if omitted)"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--restore-partitions",
        action="store_true",
        help="Restore stock WeatherXM partition table to 0xC000",
    )
    group.add_argument(
        "--ota",
        action="store_true",
        help="Flash DeskOS to ota_0 (0x420000) preserving stock factory firmware",
    )
    group.add_argument(
        "--rollback",
        action="store_true",
        help="Erase otadata (0x13000) to return immediately to factory WeatherXM",
    )
    group.add_argument(
        "--status",
        action="store_true",
        help="Inspect flash partitions and active boot slot on connected device",
    )
    parser.add_argument(
        "--bin",
        type=Path,
        default=None,
        help="Path to DeskOS application binary (auto-detects .pio or build/ output if omitted)",
    )

    args = parser.parse_args()

    port = args.port or find_serial_port()
    if not port:
        print("Error: No serial port specified and no WG1200 device auto-detected.", file=sys.stderr)
        return 1

    try:
        if args.restore_partitions:
            return cmd_restore_partitions(port)
        elif args.rollback:
            return cmd_rollback(port)
        elif args.ota:
            return cmd_flash_ota(port, args.bin)
        elif args.status:
            return cmd_status(port)
    except subprocess.CalledProcessError as exc:
        print(f"Error: esptool command failed with exit code {exc.returncode}", file=sys.stderr)
        return exc.returncode
    return 0



if __name__ == "__main__":
    sys.exit(main())
