#!/usr/bin/env python3
"""
WeatherXM WG1200 Non-Destructive Flashing & Rollback Utility.
=============================================================
Installs DeskOS into an OTA slot (ota_0 at 0x420000 or ota_1 at 0x820000)
while keeping the stock WeatherXM bootloader (0x0), partition table (0xC000),
device credentials (esp_secure_cert at 0xD000), factory firmware (0x20000),
nvs_key and spiffs intact. Only otadata and the chosen OTA slot are written;
every write goes through an allow-list of flash regions.

Safety rules:
- The serial port is never auto-detected; pass -p/--port explicitly.
- Every write command requires the device MAC (--expected-mac or the
  WG1200_EXPECTED_MAC environment variable) and checks it, the chip and the
  16 MB flash size before touching flash.
- Before any write, 0x0-0x20000 (bootloader, partition table, esp_secure_cert,
  nvs, otadata, phy_init) is backed up with a SHA-256 manifest.
- The DeskOS image must be a WG1200 DeskOS build. On devices with Secure Boot
  enabled (all production WG1200s) it must carry a Secure Boot V2 signature
  block bound to the image; an unsigned image would never boot.
- The running slot is never overwritten, and a slot holding WeatherXM
  firmware is only overwritten with --overwrite-weatherxm-slot.

Commands:
  python3 scripts/flash_wg1200.py -p PORT --status
    Reports device identity, partition layout, boot slot and slot contents.

  python3 scripts/flash_wg1200.py -p PORT --expected-mac MAC --ota [--bin PATH] [--slot {0,1}]
    Installs DeskOS into a free or DeskOS-owned non-running slot, then activates it.

  python3 scripts/flash_wg1200.py -p PORT --expected-mac MAC --rollback
    Boots the WeatherXM firmware again: the WeatherXM OTA slot that was
    running before DeskOS when it can be identified, otherwise factory.

  python3 scripts/flash_wg1200.py -p PORT --expected-mac MAC --switch-slot {factory,ota_0,ota_1}
    Switches the boot slot without reflashing (the slot must hold an app).

  python3 scripts/flash_wg1200.py -p PORT --expected-mac MAC --restore-partitions
    Rewrites the stock partition table at 0xC000 if the device's differs.
"""

from __future__ import annotations

import argparse
import binascii
import hashlib
import os
import re
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STOCK_WXM_PARTITIONS_CSV = ROOT / "partitions_wg1200.csv"
DEFAULT_DESKOS_BIN = ROOT / ".pio" / "build" / "wg1200" / "firmware.bin"
DEFAULT_BACKUP_ROOT = Path.home() / "deskos-wg1200-backups"
EXPECTED_MAC_ENV = "WG1200_EXPECTED_MAC"

PARTITION_TABLE_OFFSET = 0xC000
PARTITION_TABLE_SIZE = 0x1000
OTADATA_OFFSET = 0x13000
OTADATA_SIZE = 0x2000
OTA_0_OFFSET = 0x420000
OTA_1_OFFSET = 0x820000
FACTORY_OFFSET = 0x20000
APP_SLOT_SIZE = 0x400000
SECURE_CERT_OFFSET = 0xD000
SECURE_CERT_SIZE = 0x2000
BACKUP_OFFSET = 0x0
BACKUP_SIZE = 0x20000
EXPECTED_FLASH_MB = 16

SLOT_OFFSETS = {"factory": FACTORY_OFFSET, "ota_0": OTA_0_OFFSET, "ota_1": OTA_1_OFFSET}

# The only flash regions this tool may write.
WRITABLE_REGIONS = {
    "partition_table": (PARTITION_TABLE_OFFSET, PARTITION_TABLE_SIZE),
    "otadata": (OTADATA_OFFSET, OTADATA_SIZE),
    "ota_0": (OTA_0_OFFSET, APP_SLOT_SIZE),
    "ota_1": (OTA_1_OFFSET, APP_SLOT_SIZE),
}

ESP_IMAGE_MAGIC = 0xE9
ESP32S3_CHIP_ID = 9
APP_DESC_OFFSET = 0x20
APP_DESC_MAGIC = 0xABCD5432
APP_HEADER_READ_SIZE = 0x100
DESKOS_PROJECT_NAME = "meshcore_deskos_d1l"
WG1200_BUILD_MARKER = b"weatherxm_wg1200"
SECURE_BOOT_SECTOR_SIZE = 0x1000
SECURE_BOOT_V2_MAGIC = b"\xe7\x02"


class SafetyError(RuntimeError):
    """A safety check refused the requested operation."""


@dataclass
class DeviceIdentity:
    mac: str
    flash_mb: int | None
    secure_boot: bool | None


@dataclass
class AppInfo:
    kind: str  # "empty", "invalid" or "app"
    project: str = ""
    version: str = ""

    @property
    def is_deskos(self) -> bool:
        return self.kind == "app" and self.project == DESKOS_PROJECT_NAME

    @property
    def is_weatherxm(self) -> bool:
        return self.kind == "app" and self.project != DESKOS_PROJECT_NAME

    def describe(self) -> str:
        if self.kind != "app":
            return self.kind
        owner = "DeskOS" if self.is_deskos else "WeatherXM/other"
        return f"{owner} ({self.project} {self.version})"


def run_esptool(port: str, args: list[str], capture: bool = False) -> str:
    """Run esptool against the given port. Raises CalledProcessError on failure."""
    cmd = [sys.executable, "-m", "esptool", "--chip", "esp32s3", "-p", port, "-b", "460800", *args]
    print(f"[flash_wg1200] Running: {' '.join(cmd)}")
    if not capture:
        subprocess.run(cmd, check=True)
        return ""
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return result.stdout + result.stderr


def read_flash(port: str, offset: int, size: int) -> bytes:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "read.bin"
        run_esptool(port, ["read_flash", f"0x{offset:X}", f"0x{size:X}", str(path)])
        data = path.read_bytes()
    if len(data) != size:
        raise SafetyError(f"Read {len(data)} bytes at 0x{offset:X}, expected {size}")
    return data


def check_write_allowed(region: str, offset: int, size: int) -> None:
    """Refuse any write outside the named allow-listed region."""
    if region not in WRITABLE_REGIONS:
        raise SafetyError(f"Region {region!r} is not writable by this tool")
    start, length = WRITABLE_REGIONS[region]
    if size <= 0 or offset < start or offset + size > start + length:
        raise SafetyError(
            f"Write 0x{offset:X}+0x{size:X} falls outside {region} (0x{start:X}+0x{length:X})"
        )


def write_region(port: str, region: str, offset: int, data: bytes) -> None:
    check_write_allowed(region, offset, len(data))
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / f"{region}.bin"
        path.write_bytes(data)
        run_esptool(port, ["write_flash", f"0x{offset:X}", str(path)])


def normalize_mac(mac: str) -> str:
    cleaned = re.sub(r"[^0-9a-fA-F]", "", mac or "").lower()
    if len(cleaned) != 12:
        raise SafetyError(f"Invalid MAC address: {mac!r}")
    return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))


def parse_flash_id_output(text: str) -> tuple[str | None, int | None]:
    """MAC and flash size (MB) from esptool output; BASE MAC wins over an EUI-64 'MAC'."""
    macs = {}
    for label, value in re.findall(r"^\s*(BASE MAC|MAC):\s*([0-9a-fA-F:]+)\s*$", text, re.MULTILINE):
        if re.fullmatch(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}", value):
            macs.setdefault(label, value)
    raw_mac = macs.get("BASE MAC") or macs.get("MAC")
    size_match = re.search(r"Detected flash size:\s*(\d+)\s*MB", text)
    mac = normalize_mac(raw_mac) if raw_mac else None
    return mac, int(size_match.group(1)) if size_match else None


def parse_secure_boot(text: str) -> bool | None:
    match = re.search(r"Secure Boot:\s*(Enabled|Disabled)", text, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).lower() == "enabled"


def read_device_identity(port: str) -> DeviceIdentity:
    mac, flash_mb = parse_flash_id_output(run_esptool(port, ["flash_id"], capture=True))
    if mac is None:
        raise SafetyError("Could not read the device MAC from esptool flash_id")
    try:
        secure_boot = parse_secure_boot(run_esptool(port, ["get_security_info"], capture=True))
    except subprocess.CalledProcessError:
        secure_boot = None
    return DeviceIdentity(mac=mac, flash_mb=flash_mb, secure_boot=secure_boot)


def require_device(identity: DeviceIdentity, expected_mac: str | None) -> None:
    """Fail closed unless this is the intended 16 MB WG1200."""
    if not expected_mac:
        raise SafetyError(
            f"Write commands need --expected-mac (or {EXPECTED_MAC_ENV}); connected device MAC is {identity.mac}"
        )
    if normalize_mac(expected_mac) != identity.mac:
        raise SafetyError(f"Connected device MAC {identity.mac} does not match --expected-mac {expected_mac}")
    if identity.flash_mb != EXPECTED_FLASH_MB:
        raise SafetyError(f"Expected {EXPECTED_FLASH_MB} MB flash, device reports {identity.flash_mb} MB")


def describe_app(header: bytes) -> AppInfo:
    """Classify the start of an app slot by its ESP image header and app descriptor."""
    if not header or header == b"\xff" * len(header):
        return AppInfo("empty")
    if len(header) < APP_DESC_OFFSET + 0x50 or header[0] != ESP_IMAGE_MAGIC:
        return AppInfo("invalid")
    (desc_magic,) = struct.unpack_from("<I", header, APP_DESC_OFFSET)
    if desc_magic != APP_DESC_MAGIC:
        return AppInfo("invalid")
    version = header[APP_DESC_OFFSET + 0x10 : APP_DESC_OFFSET + 0x30].split(b"\0")[0]
    project = header[APP_DESC_OFFSET + 0x30 : APP_DESC_OFFSET + 0x50].split(b"\0")[0]
    return AppInfo(
        "app",
        project=project.decode("ascii", errors="replace"),
        version=version.decode("ascii", errors="replace"),
    )


def read_slot_info(port: str, slot: str) -> AppInfo:
    return describe_app(read_flash(port, SLOT_OFFSETS[slot], APP_HEADER_READ_SIZE))


def has_secure_boot_v2_signature(image: bytes) -> bool:
    """True when the image ends with a Secure Boot V2 signature sector bound to it."""
    if len(image) < 2 * SECURE_BOOT_SECTOR_SIZE or len(image) % SECURE_BOOT_SECTOR_SIZE:
        return False
    sector = image[-SECURE_BOOT_SECTOR_SIZE:]
    if sector[:2] != SECURE_BOOT_V2_MAGIC:
        return False
    return sector[4:36] == hashlib.sha256(image[:-SECURE_BOOT_SECTOR_SIZE]).digest()


def validate_deskos_image(image: bytes, secure_boot: bool | None) -> list[str]:
    """Return the reasons this image must not be installed (empty when acceptable)."""
    errors = []
    if len(image) > APP_SLOT_SIZE:
        errors.append(f"image is {len(image)} bytes, larger than the 4 MB slot")
    if len(image) < APP_HEADER_READ_SIZE or image[0] != ESP_IMAGE_MAGIC:
        errors.append("not an ESP application image")
        return errors
    (chip_id,) = struct.unpack_from("<H", image, 12)
    if chip_id != ESP32S3_CHIP_ID:
        errors.append(f"image targets chip id {chip_id}, not ESP32-S3")
    info = describe_app(image[:APP_HEADER_READ_SIZE])
    if not info.is_deskos:
        errors.append(f"image is not DeskOS (project {info.project or 'unknown'!r})")
    if WG1200_BUILD_MARKER not in image:
        errors.append("image is not a WG1200 build (D1L firmware must not be installed on a WG1200)")
    if secure_boot is not False and not has_secure_boot_v2_signature(image):
        state = "enabled" if secure_boot else "unknown"
        errors.append(
            f"Secure Boot is {state} on the device and the image has no Secure Boot V2 "
            "signature bound to it; the bootloader would refuse it"
        )
    return errors


def calc_otadata_crc(seq: int) -> int:
    """Calculate CRC32 for an OTA sequence number as expected by ESP-IDF bootloader."""
    return binascii.crc32(struct.pack("<I", seq), 0xFFFFFFFF) % (1 << 32)


def _otadata_sector_seq(sector: bytes) -> int | None:
    (seq,) = struct.unpack("<I", sector[0:4])
    (crc,) = struct.unpack("<I", sector[28:32])
    if seq == 0xFFFFFFFF or crc != calc_otadata_crc(seq):
        return None
    return seq


def _slot_for_seq(seq: int) -> str:
    return "ota_0" if seq % 2 == 1 else "ota_1"


def get_boot_state(otadata: bytearray) -> tuple[str, int | None, int | None]:
    """
    Parse otadata and return (active_slot, active_seq, active_sector):
      active_slot: 'factory', 'ota_0', or 'ota_1'
      active_seq: current highest valid sequence number, or None
      active_sector: sector index (0 or 1) that is currently active, or None
    """
    if len(otadata) < OTADATA_SIZE:
        return ("factory", None, None)
    seqs = [_otadata_sector_seq(otadata[0:4096]), _otadata_sector_seq(otadata[4096:8192])]
    valid = [(seq, sec) for sec, seq in enumerate(seqs) if seq is not None]
    if not valid:
        return ("factory", None, None)
    seq, sec = max(valid, key=lambda item: (item[0], -item[1]))
    return (_slot_for_seq(seq), seq, sec)


def get_previous_slot(otadata: bytearray) -> str | None:
    """The slot recorded in the non-active otadata sector, i.e. the one booted before."""
    _, _, active_sec = get_boot_state(otadata)
    if active_sec is None:
        return None
    other = 1 - active_sec
    seq = _otadata_sector_seq(otadata[other * 4096 : (other + 1) * 4096])
    return _slot_for_seq(seq) if seq is not None else None


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


def build_boot_selection(current_otadata: bytearray, target: str) -> bytearray:
    """otadata that boots target ('factory' clears both sectors)."""
    if target == "factory":
        return bytearray(b"\xFF" * OTADATA_SIZE)
    _, active_seq, active_sec = get_boot_state(current_otadata)
    target_slot = 0 if target == "ota_0" else 1
    target_sec = 0 if active_sec is None else 1 - active_sec
    return build_otadata_binary(current_otadata, target_sec, compute_next_seq(active_seq, target_slot))


def choose_weatherxm_slot(otadata: bytearray, slots: dict[str, AppInfo]) -> str:
    """
    Pick the slot that returns the device to WeatherXM firmware: the OTA slot
    that ran before DeskOS if it still holds WeatherXM firmware, else the only
    WeatherXM OTA slot, else factory.
    """
    previous = get_previous_slot(otadata)
    if previous and slots.get(previous, AppInfo("empty")).is_weatherxm:
        return previous
    weatherxm_ota = [name for name in ("ota_0", "ota_1") if slots.get(name, AppInfo("empty")).is_weatherxm]
    if len(weatherxm_ota) == 1:
        return weatherxm_ota[0]
    return "factory"


def choose_install_slot(active_slot: str, slots: dict[str, AppInfo], requested: int | None,
                        overwrite_weatherxm: bool) -> str:
    """Pick the OTA slot to install DeskOS into, refusing unsafe choices."""
    candidates = [f"ota_{requested}"] if requested is not None else ["ota_0", "ota_1"]
    reasons = []
    for slot in candidates:
        info = slots[slot]
        if slot == active_slot:
            reasons.append(f"{slot} is the running slot")
        elif info.is_weatherxm and not overwrite_weatherxm:
            reasons.append(f"{slot} holds {info.describe()}")
        else:
            return slot
    raise SafetyError(
        "No safe slot for DeskOS: " + "; ".join(reasons)
        + ". Use --overwrite-weatherxm-slot only if that WeatherXM copy is not needed."
    )


def backup_boot_region(port: str, identity: DeviceIdentity, backup_root: Path) -> Path:
    """Save 0x0-0x20000 with a SHA-256 manifest before any write."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    dest = backup_root / f"{identity.mac.replace(':', '')}_{stamp}"
    dest.mkdir(parents=True, exist_ok=False)
    data = read_flash(port, BACKUP_OFFSET, BACKUP_SIZE)
    region = dest / "flash_0x0_0x20000.bin"
    region.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    (dest / "SHA256SUMS").write_text(f"{digest}  {region.name}\n")
    print(f"[backup] 0x0-0x20000 saved to {region} (sha256 {digest})")
    cert = data[SECURE_CERT_OFFSET : SECURE_CERT_OFFSET + SECURE_CERT_SIZE]
    if cert == b"\xff" * SECURE_CERT_SIZE:
        print("[backup] WARNING: esp_secure_cert (0xD000) is blank on this device; "
              "its WeatherXM credentials are already missing.")
    return dest


def identify_target(port: str, expected_mac: str | None) -> DeviceIdentity:
    identity = read_device_identity(port)
    require_device(identity, expected_mac)
    return identity


def read_all_slots(port: str) -> dict[str, AppInfo]:
    return {slot: read_slot_info(port, slot) for slot in SLOT_OFFSETS}


def find_gen_esp32part() -> Path:
    roots = [os.environ.get("IDF_PATH"), str(Path.home() / ".platformio/packages/framework-espidf")]
    for root in roots:
        if root:
            candidate = Path(root) / "components/partition_table/gen_esp32part.py"
            if candidate.is_file():
                return candidate
    raise FileNotFoundError("gen_esp32part.py not found; set IDF_PATH")


def build_stock_partition_table() -> bytes:
    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir) / "partitions.bin"
        subprocess.run(
            [sys.executable, str(find_gen_esp32part()), "--offset", "0xc000",
             str(STOCK_WXM_PARTITIONS_CSV), str(out)],
            check=True,
        )
        return out.read_bytes()


def cmd_restore_partitions(port: str, expected_mac: str | None, backup_root: Path) -> int:
    """Restore the stock WeatherXM partition table to 0xC000 if it differs."""
    stock = build_stock_partition_table()
    identity = identify_target(port, expected_mac)
    current = read_flash(port, PARTITION_TABLE_OFFSET, PARTITION_TABLE_SIZE)
    if current[: len(stock)] == stock:
        print("[OK] Device already has the stock WG1200 partition table; nothing written.")
        return 0
    backup_boot_region(port, identity, backup_root)
    write_region(port, "partition_table", PARTITION_TABLE_OFFSET, stock)
    print("\n[SUCCESS] Stock partition table restored to 0xC000.\n")
    return 0


def cmd_switch_slot(port: str, target: str, expected_mac: str | None, backup_root: Path) -> int:
    """Switch the boot slot without reflashing; the target must hold an app."""
    identity = identify_target(port, expected_mac)
    info = read_slot_info(port, target)
    if info.kind != "app":
        raise SafetyError(f"{target} holds no valid app ({info.kind}); refusing to boot it")
    otadata = bytearray(read_flash(port, OTADATA_OFFSET, OTADATA_SIZE))
    backup_boot_region(port, identity, backup_root)
    write_region(port, "otadata", OTADATA_OFFSET, build_boot_selection(otadata, target))
    print(f"[SUCCESS] Boot slot switched to {target}: {info.describe()}")
    return 0


def cmd_rollback(port: str, expected_mac: str | None, backup_root: Path) -> int:
    """Boot WeatherXM firmware again, preferring the WeatherXM slot that ran before DeskOS."""
    identity = identify_target(port, expected_mac)
    slots = read_all_slots(port)
    otadata = bytearray(read_flash(port, OTADATA_OFFSET, OTADATA_SIZE))
    target = choose_weatherxm_slot(otadata, slots)
    if slots[target].kind != "app" or slots[target].is_deskos:
        raise SafetyError(f"No WeatherXM firmware found to return to ({target}: {slots[target].describe()})")
    if target == "factory":
        print("[rollback] No identifiable WeatherXM OTA slot; booting the factory image, "
              "which may be older than the firmware the unit last ran.")
    backup_boot_region(port, identity, backup_root)
    write_region(port, "otadata", OTADATA_OFFSET, build_boot_selection(otadata, target))
    print(f"[SUCCESS] Device will boot {target}: {slots[target].describe()}")
    return 0


def cmd_flash_ota(port: str, app_bin: Path, slot: int | None, expected_mac: str | None,
                  backup_root: Path, overwrite_weatherxm: bool) -> int:
    """Install DeskOS into a safe non-running slot, verify it, then activate it."""
    if not app_bin.is_file():
        raise SafetyError(f"Application binary not found: {app_bin}")
    image = app_bin.read_bytes()

    identity = identify_target(port, expected_mac)
    errors = validate_deskos_image(image, identity.secure_boot)
    if errors:
        raise SafetyError("Refusing image " + str(app_bin) + ":\n  - " + "\n  - ".join(errors))

    otadata = bytearray(read_flash(port, OTADATA_OFFSET, OTADATA_SIZE))
    active_slot, active_seq, _ = get_boot_state(otadata)
    slots = read_all_slots(port)
    for name, info in slots.items():
        print(f"      {name:<8} {info.describe()}")
    target = choose_install_slot(active_slot, slots, slot, overwrite_weatherxm)

    print("\n=======================================================")
    print(" Non-Destructive WG1200 DeskOS Installation")
    print(f" Port: {port}   MAC: {identity.mac}   Secure Boot: {identity.secure_boot}")
    print(f" App Binary: {app_bin}")
    print("-------------------------------------------------------")
    print(" [PROTECTED] Bootloader (0x0):         UNTOUCHED")
    print(" [PROTECTED] Partition Table (0xC000): UNTOUCHED")
    print(" [PROTECTED] esp_secure_cert (0xD000): UNTOUCHED")
    print(" [PROTECTED] factory firmware (0x20000): UNTOUCHED")
    print(" [PROTECTED] nvs_key (0xC20000):       UNTOUCHED")
    print(" [PROTECTED] spiffs (0xC21000):        UNTOUCHED")
    print("=======================================================\n")

    backup_boot_region(port, identity, backup_root)
    print(f"[1/2] Writing DeskOS to {target} (running slot {active_slot}, seq={active_seq})")
    write_region(port, target, SLOT_OFFSETS[target], image)
    print(f"[2/2] Activating {target}")
    write_region(port, "otadata", OTADATA_OFFSET, build_boot_selection(otadata, target))
    print(f"\n[SUCCESS] DeskOS installed and activated on {target}.")
    print(f" Return to WeatherXM: python3 scripts/flash_wg1200.py -p {port} --expected-mac {identity.mac} --rollback\n")
    return 0


def cmd_status(port: str) -> int:
    """Report identity, partition layout, boot slot and slot contents (read-only)."""
    identity = read_device_identity(port)
    part_data = read_flash(port, PARTITION_TABLE_OFFSET, PARTITION_TABLE_SIZE)
    otadata = bytearray(read_flash(port, OTADATA_OFFSET, OTADATA_SIZE))

    print(f"\nMAC: {identity.mac}   Flash: {identity.flash_mb} MB   Secure Boot: {identity.secure_boot}")
    print("\n--- Detected Partitions on Flash (0xC000) ---")
    for offset in range(0, len(part_data), 32):
        record = part_data[offset : offset + 32]
        if len(record) < 32 or record[0:2] != b"\xaa\x50":
            break
        pos, size = struct.unpack("<II", record[4:12])
        name = record[12:28].split(b"\x00")[0].decode("ascii", errors="ignore")
        print(f"  - {name:<16} offset=0x{pos:06X} size=0x{size:06X} ({size // 1024} KB)")

    active_slot, active_seq, active_sec = get_boot_state(otadata)
    print(f"\nActive Boot Slot:    {active_slot} (seq={active_seq}, sector={active_sec})")
    print(f"Previous Boot Slot:  {get_previous_slot(otadata)}")
    for name, info in read_all_slots(port).items():
        print(f"  {name:<8} {info.describe()}")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WeatherXM WG1200 Non-Destructive Flash & Recovery Utility")
    parser.add_argument("-p", "--port", required=True, help="Serial port of the WG1200 (never auto-detected)")
    parser.add_argument(
        "--expected-mac",
        default=os.environ.get(EXPECTED_MAC_ENV),
        help=f"MAC of the intended device; required for writes (default: ${EXPECTED_MAC_ENV})",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--restore-partitions", action="store_true",
                       help="Restore the stock WeatherXM partition table to 0xC000 if it differs")
    group.add_argument("--ota", action="store_true",
                       help="Install DeskOS into a safe non-running OTA slot and activate it")
    group.add_argument("--switch-slot", choices=["factory", "ota_0", "ota_1"],
                       help="Switch the boot slot without reflashing")
    group.add_argument("--rollback", action="store_true",
                       help="Boot WeatherXM firmware again (previous WeatherXM slot, else factory)")
    group.add_argument("--status", action="store_true",
                       help="Report device identity, partitions, boot slot and slot contents")
    parser.add_argument("--slot", type=int, choices=[0, 1], default=None,
                        help="Explicit OTA slot for --ota (still subject to the safety checks)")
    parser.add_argument("--bin", type=Path, default=DEFAULT_DESKOS_BIN,
                        help="DeskOS WG1200 image (Secure Boot signed for production units)")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_ROOT,
                        help="Where 0x0-0x20000 backups are written before any change")
    parser.add_argument("--overwrite-weatherxm-slot", action="store_true",
                        help="Allow --ota to replace an OTA slot that holds WeatherXM firmware")
    args = parser.parse_args(argv)

    try:
        if args.status:
            return cmd_status(args.port)
        if args.restore_partitions:
            return cmd_restore_partitions(args.port, args.expected_mac, args.backup_dir)
        if args.rollback:
            return cmd_rollback(args.port, args.expected_mac, args.backup_dir)
        if args.switch_slot:
            return cmd_switch_slot(args.port, args.switch_slot, args.expected_mac, args.backup_dir)
        return cmd_flash_ota(args.port, args.bin, args.slot, args.expected_mac,
                             args.backup_dir, args.overwrite_weatherxm_slot)
    except SafetyError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"Error: esptool command failed with exit code {exc.returncode}", file=sys.stderr)
        return exc.returncode


if __name__ == "__main__":
    sys.exit(main())
