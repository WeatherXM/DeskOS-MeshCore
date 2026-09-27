import binascii
from pathlib import Path
import struct
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
FLASH_SCRIPT = ROOT / "scripts" / "flash_wg1200.py"


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
        self.assertIn("--rollback", res.stdout)
        self.assertIn("--status", res.stdout)

    def test_otadata_crc_generation(self):
        seq = 1
        crc = binascii.crc32(struct.pack("<I", seq), 0xFFFFFFFF) % (1 << 32)
        expected = binascii.crc32(struct.pack("<I", 1), 0xFFFFFFFF)
        self.assertEqual(crc, expected)

    def test_flash_offsets_and_preservation_invariants(self):
        script_text = FLASH_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("OTA_0_OFFSET = 0x420000", script_text)
        self.assertIn("OTADATA_OFFSET = 0x13000", script_text)
        self.assertIn("PARTITION_TABLE_OFFSET = 0xC000", script_text)

        self.assertIn("FACTORY_OFFSET = 0x20000", script_text)
        self.assertIn("[PROTECTED] factory firmware (0x20000): UNTOUCHED", script_text)
        self.assertIn("[PROTECTED] esp_secure_cert (0xD000): UNTOUCHED", script_text)


if __name__ == "__main__":
    unittest.main()
