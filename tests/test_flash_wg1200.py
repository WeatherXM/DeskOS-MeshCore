import binascii
from pathlib import Path
import struct
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
FLASH_SCRIPT = ROOT / "scripts" / "flash_wg1200.py"

# Import helpers from flash_wg1200 directly
sys.path.insert(0, str(ROOT / "scripts"))
import flash_wg1200


class TestFlashWG1200(unittest.TestCase):
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

        # Ping-pong switch back to ota_0 in sector 0 (seq=3)
        ota0_v2_data = flash_wg1200.build_otadata_binary(ota1_data, target_sec=0, next_seq=3)
        slot, seq, sec = flash_wg1200.get_boot_state(ota0_v2_data)
        self.assertEqual(slot, "ota_0")
        self.assertEqual(seq, 3)
        self.assertEqual(sec, 0)

    def test_flash_offsets_and_preservation_invariants(self):
        script_text = FLASH_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("OTA_0_OFFSET = 0x420000", script_text)
        self.assertIn("OTA_1_OFFSET = 0x820000", script_text)
        self.assertIn("OTADATA_OFFSET = 0x13000", script_text)
        self.assertIn("PARTITION_TABLE_OFFSET = 0xC000", script_text)

        self.assertIn("FACTORY_OFFSET = 0x20000", script_text)
        self.assertIn("[PROTECTED] factory firmware (0x20000): UNTOUCHED", script_text)
        self.assertIn("[PROTECTED] esp_secure_cert (0xD000): UNTOUCHED", script_text)


if __name__ == "__main__":
    unittest.main()
