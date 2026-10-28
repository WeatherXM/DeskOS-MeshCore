"""WG1200 dual-boot policy, Secure Boot signature check and their wiring."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]

# One Secure Boot V2 RSA-3072 signature block made by
# `espsecure sign-data --version 2` with a throwaway key (deleted after use)
# over 8192 bytes of (i * 7 + 3) & 0xFF. Not the WeatherXM key.
SIGNATURE_BLOCK_HEX = (
    "e702000079a68194a5a1dc354264d70a556ff0a6acf1478d589a98cbb22bbb81fe55b5e50fb57d3c44d335c34ade7b54"
    "ccf0d03700fbe5217ab11658aa651c40db5005392f7cac5d355ac420da0cad5734e799b4cc9eb721acd36c5e2221f094"
    "47ded5f4be1ceed163b19278c37381b4021f99b5ca2b550cd7dd8ffd74ef15909674e4a30f08f94cf4aec4509c4a746b"
    "e3a6429cd4c66eedf189f50c32b2334cb3ff32b8627cd0e0672e501060d48da3891b12a966700332b47d22f0fe7605d6"
    "479f9119bee8a2198ea927a805e9600d1bf82ef238c4af21bbd6767fcb9cfd9baf999783103a5b38b0dba8205020847f"
    "d4f313a813e642ca286dd3719031e12b9dd296b87a98ccaef28bae038684ef632193a6338e04c5fa505a35debed182db"
    "ee92d5dda2561b425cbf9de540af4d026de380ac001359d3b593f55ef1cf85762d1d347a694ae09bd80198e6585df9da"
    "aebbf490b8295af9f5c84477879ec25da37983701f88615a42b20f2508c1cbe68b53304f52db2a304ad4fabca97c42df"
    "5e36e7b6f8f2baab1f80fe2b96c28c98b2b39ba575243e0e0e7527d3b2eafcb16f971da101000100d9f21f36b9ecccee"
    "f5f2f461134bf043bd73a06adcdca6f516243ae14bd611d97ac1a61013381d4f99f6ba9824e2d78860f248b445fbd81a"
    "88472550bb05166765f9d227a219cd0ecccd256faeb3e9303df14e9f396e612d8ef17e991681fcacc0163527bd3fd353"
    "3e82368e418e080e82851d252ecc4547ff311020e33c3d198e902942a4efbd8a1d0e5ff2adeab8e630ae65cf286e8411"
    "21a89d97ee189bffe029bfd0f79c78518a4d25269d6d967f41c92ee2cf4eb369963609465cbec03cd1edc372bc846bb4"
    "0775458987ccfedaaa82e3a5127110b48fa1a5b9888d614f0c97f5a3fafb849953a3976d262f79abc572c95732d41037"
    "d8a1c6d3d300406dd0325b3ea204d07b76336daa63bbe986e2106b6e71a0f9ea2721bb88d735c10e111e55efa2b66c2d"
    "b9bdf2dda439bdc43855e06dde70314b180e672171481e59768051c92381487b883f6608cffbd814aa2b5c85b3450ac1"
    "522f56f66cc9b4741b2f9ed796eaf80ae93e4f837678e6d2943021c675a3ea3557b4691a2178482c11664d903e3b9782"
    "a5bd5c9106857a50988a727b65041d6bfcd868c7f6c56a6df83afce7ca75c29959b97f032a45192eb36f17a6d8f9fb8a"
    "b6cbff4bd096ea24d2e06c57d93bb287a1febdbd9f55754a1edb6c2d6fcdface69ca9d5fb766291dd6a9227bf926fdb3"
    "1db4fa5befa8c599bbe20c0b6c8b1140b7af2ecb5d86d8ae0170c59407839ccecfcb4991cb9b9e2076a500adcd5605f6"
    "1bbb22eedb3eab7ce70574b409d89cf310e996942ac55fc2582812ef1c6563460a09714f74b2ed20ee949fc625acde36"
    "380413fc32e3b5ee98940b193024f6f2716d06c1321f9ffa53ddb2e335af60be328daa2588c0cad8f0894c166ee5de71"
    "681ac91933146e25970d62c75c4bdf4967a5d1b4d3dc8e466c64d5349081c8596aeea5cfd7b964755786441b53df44e7"
    "2306cd205237ad798965bd76eb93c0ce1537cfc61d44236fab365640d2246eb8312ef33302d5fd9a2148df9061ae93db"
    "eae8c0aba9ff1cb6563c40ec09e8f2845375100a4145668b2fe4cb4a88beba52e7071d2e8c70f16e933b1d2e142ce816"
    "00000000000000000000000000000000"
)
IMAGE_DIGEST_HEX = "79a68194a5a1dc354264d70a556ff0a6acf1478d589a98cbb22bbb81fe55b5e5"
KEY_DIGEST_HEX = "37ad4606e9d0a52960aa4f4e3ec12c704b8131199160736dc57f56053c610af0"

MBEDTLS_MODULES = (
    "rsa", "rsa_alt_helpers", "bignum", "bignum_core", "constant_time", "md",
    "sha256", "platform_util", "oid", "asn1parse", "asn1write",
)


def read(path):
    return (ROOT / path).read_text()


def compiler():
    found = shutil.which("gcc") or shutil.which("clang")
    assert found, "A C compiler is required for WG1200 boot tests"
    return found


def write_fixture(tmp_path):
    block = tmp_path / "signature_block.bin"
    block.write_bytes(bytes.fromhex(SIGNATURE_BLOCK_HEX))
    digests = tmp_path / "digests.bin"
    digests.write_bytes(bytes.fromhex(IMAGE_DIGEST_HEX + KEY_DIGEST_HEX))
    return block, digests


def test_wg1200_boot_policy_native(tmp_path):
    block, _ = write_fixture(tmp_path)
    binary = tmp_path / ("policy.exe" if os.name == "nt" else "policy")
    subprocess.run([compiler(), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-I", str(ROOT / "main"),
                    str(ROOT / "main/update/wg1200_boot_policy.c"),
                    str(ROOT / "tests/native/wg1200_boot_policy_test.c"),
                    "-o", str(binary)], check=True)
    subprocess.run([str(binary), str(block)], check=True)


def find_mbedtls():
    candidates = []
    if os.environ.get("IDF_PATH"):
        candidates.append(Path(os.environ["IDF_PATH"]))
    candidates.append(Path.home() / ".platformio/packages/framework-espidf")
    for idf in candidates:
        library = idf / "components/mbedtls/mbedtls"
        if (library / "library/rsa.c").is_file():
            return library
    return None


def test_wg1200_signature_check_accepts_only_bound_trusted_rsa_pss(tmp_path):
    mbedtls = find_mbedtls()
    if mbedtls is None:
        pytest.skip("ESP-IDF mbedtls sources not installed")
    config = tmp_path / "mbedtls_test_config.h"
    config.write_text("".join(f"#define {name}\n" for name in (
        "MBEDTLS_BIGNUM_C", "MBEDTLS_RSA_C", "MBEDTLS_PKCS1_V21",
        "MBEDTLS_MD_C", "MBEDTLS_SHA256_C", "MBEDTLS_OID_C",
        "MBEDTLS_ASN1_PARSE_C", "MBEDTLS_ASN1_WRITE_C")))
    block, digests = write_fixture(tmp_path)
    binary = tmp_path / ("signature.exe" if os.name == "nt" else "signature")
    sources = [str(mbedtls / "library" / f"{name}.c") for name in MBEDTLS_MODULES]
    subprocess.run([compiler(), "-std=c11", "-O1", "-w",
                    "-I", str(tmp_path), "-I", str(mbedtls / "include"),
                    "-I", str(mbedtls / "library"), "-I", str(ROOT / "main"),
                    '-DMBEDTLS_CONFIG_FILE="mbedtls_test_config.h"',
                    str(ROOT / "main/update/wg1200_boot_policy.c"),
                    str(ROOT / "main/update/wg1200_signature.c"),
                    str(ROOT / "tests/native/wg1200_signature_test.c"),
                    *sources, "-o", str(binary)], check=True)
    subprocess.run([str(binary), str(block), str(digests)], check=True)


def test_updater_checks_slot_and_signature_before_trusting_the_image():
    install = read("main/update/update_manager.c").split(
        "static esp_err_t run_install(void)", 1)[1].split(
        "static void update_task", 1)[0]
    order = [
        "esp_ota_get_next_update_partition(NULL)",
        "d1l_wg1200_boot_select_update_target(&target)",
        "d1l_wg1200_boot_check_update_file(",
        "ret = write_image(",
        "ret = hash_written_image(",
        "d1l_wg1200_boot_verify_app_signature(target, manifest.image_size)",
        "ret = save_pending(&manifest);",
        "esp_ota_set_boot_partition(target)",
    ]
    positions = [install.index(step) for step in order]
    assert positions == sorted(positions)


def test_wg1200_boot_hooks_run_at_safe_points():
    startup = read("main/app_main.c")
    assert (startup.index("d1l_secure_random_init()")
            < startup.index("d1l_wg1200_restart_guard_init()")
            < startup.index("d1l_wg1200_boot_guard_init()")
            < startup.index("d1l_usb_console_init()"))
    assert (startup.index("d1l_board_display_boot_splash()")
            < startup.index("d1l_wg1200_boot_menu_run()")
            < startup.index("d1l_storage_status_init()")
            < startup.index("d1l_meshcore_service_start_rx_async()"))
    assert (startup.index("d1l_update_boot_confirm(update_boot_health)")
            < startup.index("d1l_wg1200_boot_guard_mark_started()")
            < startup.index("d1l_usb_console_run()"))
    boot = read("main/update/wg1200_boot.c")
    # Never restart while the USER button is held (factory reset on newer
    # stock bootloaders), and leave GPIO10 released by a full system reset.
    menu = boot.split("void d1l_wg1200_boot_menu_run(void)", 1)[1]
    assert menu.index("D1L_WG1200_BUTTON_RELEASED") < menu.index("restart_into(")
    assert "esp_rom_software_reset_system();" in boot
    assert "esp_restart()" not in boot
    settings = read("main/ui/ui_phase1.c").split(
        "case D1L_UI_SETTINGS_ACTION_WEATHERXM:", 1)[1].split("break;\n    case", 1)[0]
    assert "confirmation_active(" in settings
    assert settings.index("confirmation_active(") < settings.index(
        "d1l_wg1200_boot_switch_to_weatherxm()")
