from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class TestWG1200PartitionTable(unittest.TestCase):
    def test_wg1200_partition_table_matches_stock_weatherxm(self):
        table = read("partitions_wg1200.csv")

        # Verify all stock WeatherXM partitions exist at exact stock offsets and sizes
        self.assertIn("esp_secure_cert,   0x3F, ,         0xd000,   0x2000,", table)
        self.assertIn("nvs,               data, nvs,      0xf000,   0x4000,", table)
        self.assertIn("otadata,           data, ota,      0x13000,  0x2000,", table)
        self.assertIn("phy_init,          data, phy,      0x15000,  0x1000,", table)
        self.assertIn("factory,           app,  factory,  0x20000,  4M,", table)
        self.assertIn("ota_0,             app,  ota_0,    0x420000, 4M,", table)
        self.assertIn("ota_1,             app,  ota_1,    0x820000, 4M,", table)
        self.assertIn("nvs_key,           data, nvs_keys, 0xc20000, 0x1000,", table)
        self.assertIn("spiffs,            data, spiffs,   0xc21000, 3M,", table)

        # Verify DeskOS does not inject any custom partitions into flash
        self.assertNotIn("d1l_ret_meta", table)
        self.assertNotIn("d1l_retained", table)

    def test_wg1200_partition_headroom_and_offsets(self):
        table = read("partitions_wg1200.csv")
        lines = [
            line.strip()
            for line in table.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        parsed = {}
        for line in lines:
            parts = [p.strip() for p in line.split(",")]
            name = parts[0]
            offset = int(parts[3], 16)
            size_str = parts[4]
            if size_str.endswith("M"):
                size = int(size_str[:-1]) * 1024 * 1024
            elif size_str.endswith("K"):
                size = int(size_str[:-1]) * 1024
            elif size_str.startswith("0x") or size_str.startswith("0X"):
                size = int(size_str, 16)
            else:
                size = int(size_str)
            parsed[name] = (offset, size)

        # Exactly 9 partitions matching stock WeatherXM 16MB layout
        self.assertEqual(len(parsed), 9)

        # Bootloader ends at 0xC000
        self.assertEqual(parsed["esp_secure_cert"], (0xD000, 0x2000))
        self.assertEqual(parsed["nvs"], (0xF000, 0x4000))
        self.assertEqual(parsed["otadata"], (0x13000, 0x2000))
        self.assertEqual(parsed["phy_init"], (0x15000, 0x1000))
        self.assertEqual(parsed["factory"], (0x20000, 4 * 1024 * 1024))
        self.assertEqual(parsed["ota_0"], (0x420000, 4 * 1024 * 1024))
        self.assertEqual(parsed["ota_1"], (0x820000, 4 * 1024 * 1024))
        self.assertEqual(parsed["nvs_key"], (0xC20000, 0x1000))
        self.assertEqual(parsed["spiffs"], (0xC21000, 3 * 1024 * 1024))

        spiffs_end = parsed["spiffs"][0] + parsed["spiffs"][1]
        self.assertEqual(spiffs_end, 0xF21000)
        self.assertLessEqual(spiffs_end, 0x1000000)


if __name__ == "__main__":
    unittest.main()
