#!/usr/bin/env python3
"""
WeatherXM WG1200 Non-Destructive Flashing & Rollback Utility.
=============================================================
Allows flashing DeskOS into OTA slot 0 (0x420000) or slot 1 (0x820000)
while keeping stock WeatherXM firmware (0x20000), certificates (0xD000),
and partition table (0xC000) completely intact.

Dual-Slot OTA Safety:
- DeskOS is installed to the non-active slot first.
- The active slot is never modified during flashing.
- Only after flashing and hash verification succeed is otadata (0x13000)
  updated to switch the boot slot to the new image.
- At any time, running `--rollback` or `--switch-slot factory` returns to
  the factory WeatherXM firmware.

Commands:
  python3 scripts/flash_wg1200.py --restore-partitions [-p PORT]
    Restores the stock partition table at 0xC000 so the WeatherXM Web Flasher works.

  python3 scripts/flash_wg1200.py --ota [-p PORT] [--bin PATH] [--slot {0,1}]
    Installs DeskOS into the non-active OTA slot, verifies write, then activates it.

  python3 scripts/flash_wg1200.py --switch-slot {factory,ota_0,ota_1} [-p PORT]
    Switches active boot slot without reflashing application binaries.

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
OTA_1_OFFSET = 0x820000
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


def calc_otadata_crc(seq: int) -> int:
    """Calculate CRC32 for an OTA sequence number as expected by ESP-IDF bootloader."""
    return binascii.crc32(struct.pack("<I", seq), 0xFFFFFFFF) % (1 << 32)


def read_otadata_from_device(port: str) -> bytearray:
    """Read the 8KB otadata partition from the device."""
    with tempfile.NamedTemporaryFile() as tf:
        run_esptool(
            port,
            ["read_flash", f"0x{OTADATA_OFFSET:X}", f"0x{OTADATA_SIZE:X}", tf.name],
        )
        tf.seek(0)
        data = tf.read()
        if len(data) == OTADATA_SIZE:
            return bytearray(data)
    return bytearray(b"\xFF" * OTADATA_SIZE)


def get_boot_state(otadata: bytearray) -> tuple[str, int | None, int | None]:
    """
    Parse otadata and return (active_slot, active_seq, active_sector):
      active_slot: 'factory', 'ota_0', or 'ota_1'
      active_seq: current highest valid sequence number, or None
      active_sector: sector index (0 or 1) that is currently active, or None
    """
    if len(otadata) < OTADATA_SIZE:
        return ("factory", None, None)

    sec0 = otadata[0:4096]
    sec1 = otadata[4096:8192]
    seq0, = struct.unpack("<I", sec0[0:4])
    crc0, = struct.unpack("<I", sec0[28:32])
    valid0 = (seq0 != 0xFFFFFFFF) and (crc0 == calc_otadata_crc(seq0))

    seq1, = struct.unpack("<I", sec1[0:4])
    crc1, = struct.unpack("<I", sec1[28:32])
    valid1 = (seq1 != 0xFFFFFFFF) and (crc1 == calc_otadata_crc(seq1))

    if valid0 and not valid1:
        slot = "ota_0" if seq0 % 2 == 1 else "ota_1"
        return (slot, seq0, 0)
    elif not valid0 and valid1:
        slot = "ota_0" if seq1 % 2 == 1 else "ota_1"
        return (slot, seq1, 1)
    elif valid0 and valid1:
        if seq0 >= seq1:
            slot = "ota_0" if seq0 % 2 == 1 else "ota_1"
            return (slot, seq0, 0)
        else:
            slot = "ota_0" if seq1 % 2 == 1 else "ota_1"
            return (slot, seq1, 1)

    return ("factory", None, None)


def compute_next_seq(active_seq: int | None, target_slot: int) -> int:
    """
    Compute smallest sequence number > active_seq that boots target_slot.
    target_slot 0 (ota_0) requires odd sequence numbers (1, 3, 5, ...).
    target_slot 1 (ota_1) requires even sequence numbers (2, 4, 6, ...).
    """
    if active_seq is None:
        return 1 if target_slot == 0 else 2
    candidate = active_seq + 1
    while (candidate - 1) % 2 != target_slot:
        candidate += 1
    return candidate


def build_otadata_binary(current_otadata: bytearray, target_sec: int, next_seq: int) -> bytearray:
    """
    Build an 8KB otadata binary updating the given sector with next_seq.
    The previous sector is preserved for ping-pong safety.
    """
    buf = bytearray(current_otadata) if len(current_otadata) == OTADATA_SIZE else bytearray(b"\xFF" * OTADATA_SIZE)
    start = target_sec * 4096
    buf[start : start + 4096] = b"\xFF" * 4096
    buf[start : start + 4] = struct.pack("<I", next_seq)
    buf[start + 24 : start + 28] = struct.pack("<I", 0xFFFFFFFF)  # ESP_OTA_IMG_UNDEFINED
    crc = calc_otadata_crc(next_seq)
    buf[start + 28 : start + 32] = struct.pack("<I", crc)
    return buf


def write_otadata_to_device(port: str, otadata_buf: bytearray) -> None:
    """Write an 8KB otadata buffer to 0x13000 on the device."""
    with tempfile.NamedTemporaryFile() as tf:
        tf.write(otadata_buf)
        tf.flush()
        run_esptool(port, ["write_flash", f"0x{OTADATA_OFFSET:X}", tf.name])


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


def cmd_switch_slot(port: str, target: str) -> int:
    """Switch active boot slot without reflashing application binaries."""
    if target == "factory":
        return cmd_rollback(port)

    target_slot = 0 if target == "ota_0" else 1
    current_otadata = read_otadata_from_device(port)
    active_slot, active_seq, active_sec = get_boot_state(current_otadata)

    next_seq = compute_next_seq(active_seq, target_slot)
    target_sec = 0 if active_sec is None else (1 - active_sec)

    print(f"\n=======================================================")
    print(f" Switching Active Boot Slot to {target}")
    print(f" Port:             {port}")
    print(f" Previous Slot:    {active_slot} (seq={active_seq})")
    print(f" Target Slot:      {target} [seq={next_seq}]")
    print(f" Writing Sector:   Sector {target_sec}")
    print(f"=======================================================\n")

    new_otadata = build_otadata_binary(current_otadata, target_sec, next_seq)
    write_otadata_to_device(port, new_otadata)

    print(f"[SUCCESS] Boot slot switched to {target} (seq={next_seq}). Device will boot {target} on reset.\n")
    return 0


def cmd_flash_ota(port: str, app_bin: Path | None = None, slot: int | None = None) -> int:
    """
    Install DeskOS into the non-active OTA slot (or specified slot), verify it,
    and only then update otadata to point to the newly flashed slot.
    """
    resolved_app_bin = resolve_existing_path(app_bin, DEFAULT_DESKOS_BIN_CANDIDATES)
    if not resolved_app_bin.is_file():
        print(f"Error: Application binary not found: {resolved_app_bin}", file=sys.stderr)
        return 1

    print(f"\n=======================================================")
    print(f" Non-Destructive WG1200 Dual-Slot OTA Installation")
    print(f" Port:             {port}")
    print(f" App Binary:       {resolved_app_bin}")
    print(f"-------------------------------------------------------")
    print(f" [PROTECTED] Bootloader (0x0):         UNTOUCHED")
    print(f" [PROTECTED] Partition Table (0xC000): UNTOUCHED")
    print(f" [PROTECTED] esp_secure_cert (0xD000): UNTOUCHED")
    print(f" [PROTECTED] factory firmware (0x20000): UNTOUCHED")
    print(f" [PROTECTED] nvs_key (0xC20000):       UNTOUCHED")
    print(f" [PROTECTED] spiffs (0xC21000):        UNTOUCHED")
    print(f"=======================================================\n")

    # Step 1: Read current boot state
    print("[1/3] Reading current boot state from device...")
    current_otadata = read_otadata_from_device(port)
    active_slot, active_seq, active_sec = get_boot_state(current_otadata)
    print(f"      Active Boot Slot: {active_slot} (seq={active_seq if active_seq is not None else 'None'})")

    # Step 2: Determine non-active target slot
    if slot is not None:
        target_slot = slot
    elif active_slot == "ota_0":
        target_slot = 1
    elif active_slot == "ota_1":
        target_slot = 0
    else:  # factory
        target_slot = 0

    target_slot_name = f"ota_{target_slot}"
    target_offset = OTA_0_OFFSET if target_slot == 0 else OTA_1_OFFSET

    print(f"\n[2/3] Flashing DeskOS into non-active slot: {target_slot_name} (0x{target_offset:X})...")
    print(f"      (Current slot '{active_slot}' remains active until verification succeeds)")

    run_esptool(
        port,
        [
            "write_flash",
            f"0x{target_offset:X}",
            str(resolved_app_bin),
        ],
    )
    print(f"\n[SUCCESS] Image successfully written and verified at 0x{target_offset:X} ({target_slot_name}).")

    # Step 3: Switch boot slot to newly flashed slot
    next_seq = compute_next_seq(active_seq, target_slot)
    target_sec = 0 if active_sec is None else (1 - active_sec)

    print(f"\n[3/3] Activating {target_slot_name} in otadata...")
    print(f"      Writing seq={next_seq} to Sector {target_sec} (0x{OTADATA_OFFSET + target_sec * 4096:X})")

    new_otadata = build_otadata_binary(current_otadata, target_sec, next_seq)
    write_otadata_to_device(port, new_otadata)

    print(f"\n=======================================================")
    print(f"[SUCCESS] DeskOS flashed and activated on {target_slot_name}!")
    print(f" Active slot:   {target_slot_name} [seq={next_seq}]")
    print(f" Previous slot: {active_slot}")
    print(f" Factory WeatherXM firmware remains untouched at 0x20000.")
    print(f" Instant rollback: python3 scripts/flash_wg1200.py --rollback")
    print(f"=======================================================\n")
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
        ota_data = bytearray(tmp_ota.read_bytes())

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

        active_slot, active_seq, active_sec = get_boot_state(ota_data)
        next_target = "ota_1" if active_slot == "ota_0" else "ota_0"
        next_offset = OTA_1_OFFSET if active_slot == "ota_0" else OTA_0_OFFSET

        print(f"\nActive Boot Slot:    {active_slot} (seq={active_seq if active_seq is not None else 'None'})")
        print(f"Active Sector:       {active_sec if active_sec is not None else 'None'}")
        print(f"Next Target on OTA:  {next_target} (0x{next_offset:X})\n")
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
        help="Flash DeskOS to non-active OTA slot preserving stock factory firmware",
    )
    group.add_argument(
        "--switch-slot",
        choices=["factory", "ota_0", "ota_1"],
        help="Switch active boot slot without reflashing application binaries",
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
        "--slot",
        type=int,
        choices=[0, 1],
        default=None,
        help="Explicitly choose OTA target slot (0 or 1). Defaults to non-active slot.",
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
        elif args.switch_slot:
            return cmd_switch_slot(port, args.switch_slot)
        elif args.ota:
            return cmd_flash_ota(port, args.bin, args.slot)
        elif args.status:
            return cmd_status(port)
    except subprocess.CalledProcessError as exc:
        print(f"Error: esptool command failed with exit code {exc.returncode}", file=sys.stderr)
        return exc.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
