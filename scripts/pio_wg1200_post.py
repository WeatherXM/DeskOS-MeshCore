"""PlatformIO post script for the WG1200 environment.

The espressif32 platform always merges the bootloader, partition table and app
into firmware.factory.bin for flashing at offset 0x0. On a WG1200 that image
would fill esp_secure_cert (0xD000) with 0xFF, erase nvs and the factory
WeatherXM app, and install an unsigned bootloader that Secure Boot rejects.
Remove it after every build so it cannot be flashed by mistake; install with
scripts/flash_wg1200.py instead.
"""

import os

Import("env")  # noqa: F821 - provided by PlatformIO


def remove_factory_image(source, target, env):
    path = env.subst("$BUILD_DIR/${PROGNAME}.factory.bin")
    if os.path.isfile(path):
        os.remove(path)
        print("Removed %s: a 0x0 image would erase WG1200 credentials" % os.path.basename(path))


env.AddPostAction("$BUILD_DIR/${PROGNAME}.bin", remove_factory_image)  # noqa: F821
