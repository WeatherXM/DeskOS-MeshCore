from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WXM_ROOT = Path("/Users/manos/Documents/mesh/WG1400/wg1200-firmware")


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_wg1200_partition_table_matches_stock_weatherxm():
    table = read("partitions_wg1200.csv")

    # Verify all stock WeatherXM partitions exist at exact stock offsets and sizes
    assert "esp_secure_cert,   0x3F, ,         0xd000,   0x2000," in table
    assert "nvs,               data, nvs,      0xf000,   0x4000," in table
    assert "otadata,           data, ota,      0x13000,  0x2000," in table
    assert "phy_init,          data, phy,      0x15000,  0x1000," in table
    assert "factory,           app,  factory,  0x20000,  4M," in table
    assert "ota_0,             app,  ota_0,    0x420000, 4M," in table
    assert "ota_1,             app,  ota_1,    0x820000, 4M," in table
    assert "nvs_key,           data, nvs_keys, 0xc20000, 0x1000," in table
    assert "spiffs,            data, spiffs,   0xc21000, 3M," in table

    # Verify DeskOS retained partitions are placed in unallocated space after SPIFFS (0xf21000)
    assert "d1l_ret_meta,      data, 0x40,     0xf21000, 0x1000," in table
    assert "d1l_retained,      data, nvs,      0xf22000, 0x1F000," in table


def test_wg1200_partition_headroom_and_offsets():
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

    # Bootloader ends at 0xC000
    assert parsed["esp_secure_cert"][0] == 0xD000
    assert parsed["esp_secure_cert"][1] == 0x2000

    assert parsed["nvs"][0] == 0xF000
    assert parsed["nvs"][1] == 0x4000

    assert parsed["otadata"][0] == 0x13000
    assert parsed["otadata"][1] == 0x2000

    assert parsed["phy_init"][0] == 0x15000
    assert parsed["phy_init"][1] == 0x1000

    assert parsed["factory"][0] == 0x20000
    assert parsed["factory"][1] == 4 * 1024 * 1024

    assert parsed["ota_0"][0] == 0x420000
    assert parsed["ota_0"][1] == 4 * 1024 * 1024

    assert parsed["ota_1"][0] == 0x820000
    assert parsed["ota_1"][1] == 4 * 1024 * 1024

    # nvs_key must not be overwritten (at 0xC20000, 4KB)
    assert parsed["nvs_key"][0] == 0xC20000
    assert parsed["nvs_key"][1] == 0x1000

    # spiffs must not be shifted (at 0xC21000, 3MB -> ends at 0xF21000)
    assert parsed["spiffs"][0] == 0xC21000
    assert parsed["spiffs"][1] == 3 * 1024 * 1024
    spiffs_end = parsed["spiffs"][0] + parsed["spiffs"][1]
    assert spiffs_end == 0xF21000

    # d1l_ret_meta starts at spiffs_end
    assert parsed["d1l_ret_meta"][0] == spiffs_end
    assert parsed["d1l_ret_meta"][1] == 0x1000

    # d1l_retained starts immediately after d1l_ret_meta
    retained_start = parsed["d1l_ret_meta"][0] + parsed["d1l_ret_meta"][1]
    assert parsed["d1l_retained"][0] == retained_start
    assert parsed["d1l_retained"][1] == 0x1F000

    # Total must fit well within 16MB flash (0x1000000)
    total_end = parsed["d1l_retained"][0] + parsed["d1l_retained"][1]
    assert total_end <= 0x1000000


if __name__ == "__main__":
    test_wg1200_partition_table_matches_stock_weatherxm()
    test_wg1200_partition_headroom_and_offsets()
    print("All WG1200 partition table tests passed!")
