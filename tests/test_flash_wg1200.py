import binascii
import hashlib
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FLASH_SCRIPT = ROOT / "scripts" / "flash_wg1200.py"

# Import helpers from flash_wg1200 directly
sys.path.insert(0, str(ROOT / "scripts"))
import flash_wg1200

MAC = "e8:06:90:9e:b5:b0"


def make_app(project, version="1.0", size=0x10000, wg1200=True, signed=False):
    """Synthetic ESP32-S3 app image with an app descriptor."""
    image = bytearray(b"\x00" * size)
    image[0] = flash_wg1200.ESP_IMAGE_MAGIC
    struct.pack_into("<H", image, 12, flash_wg1200.ESP32S3_CHIP_ID)
    desc = flash_wg1200.APP_DESC_OFFSET
    struct.pack_into("<I", image, desc, flash_wg1200.APP_DESC_MAGIC)
    image[desc + 0x10 : desc + 0x10 + len(version)] = version.encode()
    image[desc + 0x30 : desc + 0x30 + len(project)] = project.encode()
    if wg1200:
        image[0x200 : 0x200 + len(flash_wg1200.WG1200_BUILD_MARKER)] = flash_wg1200.WG1200_BUILD_MARKER
    image = bytes(image)
    if signed:
        image += b"\xff" * (-len(image) % 0x1000)
        sector = bytearray(b"\xff" * 0x1000)
        sector[0:4] = b"\xe7\x02\x00\x00"
        sector[4:36] = hashlib.sha256(image).digest()
        image += bytes(sector)
    return image


def deskos(**kwargs):
    return make_app(flash_wg1200.DESKOS_PROJECT_NAME, "1.10.0", **kwargs)


def weatherxm(version="0.8.34"):
    return make_app("wg1010_port", version, wg1200=False)


class FakeDevice:
    """Emulates the esptool commands flash_wg1200 issues against a 16 MB flash."""

    def __init__(self, secure_boot=True, flash_mb=16, mac=MAC):
        self.flash = bytearray(b"\xff" * (16 * 1024 * 1024))
        self.secure_boot = secure_boot
        self.flash_mb = flash_mb
        self.mac = mac
        self.writes = []
        self.flash[flash_wg1200.SECURE_CERT_OFFSET : flash_wg1200.SECURE_CERT_OFFSET + 4] = b"CERT"

    def put(self, offset, data):
        self.flash[offset : offset + len(data)] = data

    def run_esptool(self, port, args, capture=False):
        command = args[0]
        if command == "flash_id":
            return f"MAC:                {self.mac}\nDetected flash size: {self.flash_mb}MB\n"
        if command == "get_security_info":
            return f"Secure Boot: {'Enabled' if self.secure_boot else 'Disabled'}\n"
        if command == "read_flash":
            offset, size, path = int(args[1], 16), int(args[2], 16), args[3]
            Path(path).write_bytes(bytes(self.flash[offset : offset + size]))
            return ""
        if command == "write_flash":
            offset, data = int(args[1], 16), Path(args[2]).read_bytes()
            self.writes.append((offset, len(data)))
            self.put(offset, data)
            return ""
        raise AssertionError(f"unexpected esptool command {args}")


class TestFlashWG1200(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.backups = Path(self.tmp.name) / "backups"

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, device, *argv):
        with mock.patch.object(flash_wg1200, "run_esptool", device.run_esptool):
            return flash_wg1200.main(["-p", "/dev/cu.usbmodem-test", "--backup-dir", str(self.backups), *argv])

    def write_bin(self, data):
        path = Path(self.tmp.name) / "firmware.bin"
        path.write_bytes(data)
        return path

    def test_flash_script_help(self):
        res = subprocess.run(
            [sys.executable, str(FLASH_SCRIPT), "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("--restore-partitions", res.stdout)
        self.assertIn("--ota", res.stdout)
        self.assertIn("--switch-slot", res.stdout)
        self.assertIn("--rollback", res.stdout)
        self.assertIn("--status", res.stdout)
        self.assertIn("--expected-mac", res.stdout)

    def test_port_is_never_auto_detected(self):
        res = subprocess.run(
            [sys.executable, str(FLASH_SCRIPT), "--status"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("-p/--port", res.stderr)
        script_text = FLASH_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("glob", script_text)
        self.assertNotIn("/dev/cu.", script_text)
        self.assertNotIn("meshcore_deskos_d1l.bin", script_text)
        self.assertNotIn("/Users/", script_text)

    def test_otadata_crc_generation(self):
        seq = 1
        crc = flash_wg1200.calc_otadata_crc(seq)
        expected = binascii.crc32(struct.pack("<I", 1), 0xFFFFFFFF) % (1 << 32)
        self.assertEqual(crc, expected)

    def test_compute_next_seq(self):
        # Initial state (factory / None)
        self.assertEqual(flash_wg1200.compute_next_seq(None, 0), 1)  # ota_0
        self.assertEqual(flash_wg1200.compute_next_seq(None, 1), 2)  # ota_1

        # Current active is ota_0 (seq=1) -> switch to ota_1
        self.assertEqual(flash_wg1200.compute_next_seq(1, 1), 2)

        # Current active is ota_1 (seq=2) -> switch to ota_0
        self.assertEqual(flash_wg1200.compute_next_seq(2, 0), 3)

        # Overwrite same slot: current is ota_0 (seq=1) -> reflash ota_0
        self.assertEqual(flash_wg1200.compute_next_seq(1, 0), 3)

    def test_build_and_parse_boot_state(self):
        # Initial empty otadata
        empty = bytearray(b"\xFF" * 8192)
        slot, seq, sec = flash_wg1200.get_boot_state(empty)
        self.assertEqual(slot, "factory")
        self.assertIsNone(seq)
        self.assertIsNone(sec)
        self.assertIsNone(flash_wg1200.get_previous_slot(empty))

        # Build otadata pointing to ota_0 in sector 0
        ota0_data = flash_wg1200.build_otadata_binary(empty, target_sec=0, next_seq=1)
        slot, seq, sec = flash_wg1200.get_boot_state(ota0_data)
        self.assertEqual(slot, "ota_0")
        self.assertEqual(seq, 1)
        self.assertEqual(sec, 0)

        # Ping-pong switch to ota_1 in sector 1 (seq=2)
        ota1_data = flash_wg1200.build_otadata_binary(ota0_data, target_sec=1, next_seq=2)
        slot, seq, sec = flash_wg1200.get_boot_state(ota1_data)
        self.assertEqual(slot, "ota_1")
        self.assertEqual(seq, 2)
        self.assertEqual(sec, 1)
        self.assertEqual(flash_wg1200.get_previous_slot(ota1_data), "ota_0")

        # Ping-pong switch back to ota_0 in sector 0 (seq=3)
        ota0_v2_data = flash_wg1200.build_otadata_binary(ota1_data, target_sec=0, next_seq=3)
        slot, seq, sec = flash_wg1200.get_boot_state(ota0_v2_data)
        self.assertEqual(slot, "ota_0")
        self.assertEqual(seq, 3)
        self.assertEqual(sec, 0)
        self.assertEqual(flash_wg1200.get_previous_slot(ota0_v2_data), "ota_1")

    def test_flash_offsets_and_preservation_invariants(self):
        script_text = FLASH_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("OTA_0_OFFSET = 0x420000", script_text)
        self.assertIn("OTA_1_OFFSET = 0x820000", script_text)
        self.assertIn("OTADATA_OFFSET = 0x13000", script_text)
        self.assertIn("PARTITION_TABLE_OFFSET = 0xC000", script_text)

        self.assertIn("FACTORY_OFFSET = 0x20000", script_text)
        self.assertIn("[PROTECTED] factory firmware (0x20000): UNTOUCHED", script_text)
        self.assertIn("[PROTECTED] esp_secure_cert (0xD000): UNTOUCHED", script_text)

    def test_writes_are_limited_to_allow_listed_regions(self):
        check = flash_wg1200.check_write_allowed
        check("ota_0", 0x420000, 0x400000)
        check("ota_1", 0x820000, 0x1000)
        check("otadata", 0x13000, 0x2000)
        check("partition_table", 0xC000, 0xC00)
        for region, offset, size in [
            ("ota_0", 0x420000, 0x400001),  # spills into ota_1
            ("ota_0", 0x20000, 0x1000),  # factory
            ("otadata", 0x12000, 0x2000),  # nvs tail
            ("partition_table", 0xD000, 0x1000),  # esp_secure_cert
            ("bootloader", 0x0, 0x1000),
            ("nvs_key", 0xC20000, 0x1000),
        ]:
            with self.assertRaises(flash_wg1200.SafetyError, msg=(region, hex(offset))):
                check(region, offset, size)

    def test_describe_app_classifies_slots(self):
        self.assertEqual(flash_wg1200.describe_app(b"\xff" * 0x100).kind, "empty")
        self.assertEqual(flash_wg1200.describe_app(b"\x00" * 0x100).kind, "invalid")
        info = flash_wg1200.describe_app(deskos()[:0x100])
        self.assertTrue(info.is_deskos)
        self.assertEqual(info.version, "1.10.0")
        info = flash_wg1200.describe_app(weatherxm()[:0x100])
        self.assertTrue(info.is_weatherxm)
        self.assertEqual(info.project, "wg1010_port")

    def test_secure_boot_signature_must_bind_the_image(self):
        signed = deskos(signed=True)
        self.assertTrue(flash_wg1200.has_secure_boot_v2_signature(signed))
        self.assertFalse(flash_wg1200.has_secure_boot_v2_signature(deskos()))
        tampered = bytearray(signed)
        tampered[0x300] ^= 0xFF
        self.assertFalse(flash_wg1200.has_secure_boot_v2_signature(bytes(tampered)))

    def test_image_validation(self):
        validate = flash_wg1200.validate_deskos_image
        self.assertEqual(validate(deskos(signed=True), True), [])
        self.assertEqual(validate(deskos(), False), [])
        self.assertTrue(any("Secure Boot" in e for e in validate(deskos(), True)))
        self.assertTrue(any("Secure Boot" in e for e in validate(deskos(), None)))
        self.assertTrue(any("not a WG1200 build" in e for e in validate(deskos(wg1200=False), False)))
        self.assertTrue(any("not DeskOS" in e for e in validate(weatherxm(), False)))
        self.assertTrue(any("larger than" in e for e in validate(deskos(size=0x400100), False)))

    def test_install_slot_never_takes_running_or_weatherxm_slot(self):
        AppInfo = flash_wg1200.AppInfo
        wxm = AppInfo("app", "wg1010_port", "0.8.34")
        desk = AppInfo("app", flash_wg1200.DESKOS_PROJECT_NAME, "1.10.0")
        empty = AppInfo("empty")
        choose = flash_wg1200.choose_install_slot

        slots = {"factory": wxm, "ota_0": wxm, "ota_1": empty}
        self.assertEqual(choose("ota_0", slots, None, False), "ota_1")
        slots = {"factory": wxm, "ota_0": desk, "ota_1": wxm}
        with self.assertRaises(flash_wg1200.SafetyError):
            choose("ota_0", slots, None, False)  # second DeskOS install would hit WeatherXM
        self.assertEqual(choose("ota_0", slots, None, True), "ota_1")
        with self.assertRaises(flash_wg1200.SafetyError):
            choose("ota_1", {"factory": wxm, "ota_0": empty, "ota_1": desk}, 1, True)

    def test_rollback_prefers_the_weatherxm_slot_that_ran_before(self):
        AppInfo = flash_wg1200.AppInfo
        wxm = AppInfo("app", "wg1010_port", "0.8.34")
        desk = AppInfo("app", flash_wg1200.DESKOS_PROJECT_NAME, "1.10.0")
        choose = flash_wg1200.choose_weatherxm_slot
        empty = bytearray(b"\xff" * 0x2000)
        ran_ota1 = flash_wg1200.build_boot_selection(empty, "ota_1")
        then_ota0 = flash_wg1200.build_boot_selection(ran_ota1, "ota_0")

        self.assertEqual(choose(then_ota0, {"factory": wxm, "ota_0": desk, "ota_1": wxm}), "ota_1")
        self.assertEqual(choose(empty, {"factory": wxm, "ota_0": desk, "ota_1": wxm}), "ota_1")
        self.assertEqual(choose(empty, {"factory": wxm, "ota_0": wxm, "ota_1": wxm}), "factory")
        self.assertEqual(choose(then_ota0, {"factory": wxm, "ota_0": desk, "ota_1": desk}), "factory")

    def test_write_commands_require_the_expected_device(self):
        device = FakeDevice()
        image = self.write_bin(deskos(signed=True))
        self.assertEqual(self.run_main(device, "--ota", "--bin", str(image)), 2)
        self.assertEqual(self.run_main(device, "--expected-mac", "e8:06:90:00:00:01", "--ota", "--bin", str(image)), 2)
        self.assertEqual(self.run_main(FakeDevice(flash_mb=8), "--expected-mac", MAC, "--ota", "--bin", str(image)), 2)
        self.assertEqual(device.writes, [])
        self.assertFalse(self.backups.exists())

    def test_ota_installs_into_free_slot_with_backup(self):
        device = FakeDevice()
        device.put(flash_wg1200.FACTORY_OFFSET, weatherxm("0.1.9"))
        device.put(flash_wg1200.OTA_0_OFFSET, weatherxm("0.8.34"))
        device.put(flash_wg1200.OTADATA_OFFSET, flash_wg1200.build_boot_selection(bytearray(b"\xff" * 0x2000), "ota_0"))
        image = deskos(signed=True)

        rc = self.run_main(device, "--expected-mac", MAC.upper(), "--ota", "--bin", str(self.write_bin(image)))

        self.assertEqual(rc, 0)
        self.assertEqual(device.writes, [(flash_wg1200.OTA_1_OFFSET, len(image)), (flash_wg1200.OTADATA_OFFSET, 0x2000)])
        otadata = bytearray(device.flash[flash_wg1200.OTADATA_OFFSET : flash_wg1200.OTADATA_OFFSET + 0x2000])
        self.assertEqual(flash_wg1200.get_boot_state(otadata)[0], "ota_1")
        self.assertEqual(flash_wg1200.get_previous_slot(otadata), "ota_0")
        backups = list(self.backups.glob("*/flash_0x0_0x20000.bin"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes()[0xD000:0xD004], b"CERT")
        manifest = (backups[0].parent / "SHA256SUMS").read_text()
        self.assertIn(hashlib.sha256(backups[0].read_bytes()).hexdigest(), manifest)

        # Returning to WeatherXM boots the slot that ran before DeskOS, not factory.
        self.assertEqual(self.run_main(device, "--expected-mac", MAC, "--rollback"), 0)
        otadata = bytearray(device.flash[flash_wg1200.OTADATA_OFFSET : flash_wg1200.OTADATA_OFFSET + 0x2000])
        self.assertEqual(flash_wg1200.get_boot_state(otadata)[0], "ota_0")

    def test_ota_refuses_unsigned_image_on_secure_boot_device(self):
        device = FakeDevice(secure_boot=True)
        rc = self.run_main(device, "--expected-mac", MAC, "--ota", "--bin", str(self.write_bin(deskos())))
        self.assertEqual(rc, 2)
        self.assertEqual(device.writes, [])
        self.assertFalse(self.backups.exists())

    def test_ota_refuses_to_replace_weatherxm_slot(self):
        device = FakeDevice()
        device.put(flash_wg1200.FACTORY_OFFSET, weatherxm("0.1.9"))
        device.put(flash_wg1200.OTA_0_OFFSET, deskos())
        device.put(flash_wg1200.OTA_1_OFFSET, weatherxm("0.8.34"))
        device.put(flash_wg1200.OTADATA_OFFSET, flash_wg1200.build_boot_selection(bytearray(b"\xff" * 0x2000), "ota_0"))
        image = self.write_bin(deskos(signed=True))

        self.assertEqual(self.run_main(device, "--expected-mac", MAC, "--ota", "--bin", str(image)), 2)
        self.assertEqual(device.writes, [])
        self.assertEqual(self.run_main(device, "--expected-mac", MAC, "--ota", "--slot", "0", "--bin", str(image)), 2)
        self.assertEqual(device.writes, [])
        self.assertFalse(self.backups.exists())

    def test_switch_slot_refuses_empty_slot(self):
        device = FakeDevice()
        device.put(flash_wg1200.FACTORY_OFFSET, weatherxm("0.1.9"))
        self.assertEqual(self.run_main(device, "--expected-mac", MAC, "--switch-slot", "ota_1"), 2)
        self.assertEqual(device.writes, [])
        self.assertFalse(self.backups.exists())
        self.assertEqual(self.run_main(device, "--expected-mac", MAC, "--switch-slot", "factory"), 0)
        self.assertEqual(device.writes, [(flash_wg1200.OTADATA_OFFSET, 0x2000)])


if __name__ == "__main__":
    unittest.main()
