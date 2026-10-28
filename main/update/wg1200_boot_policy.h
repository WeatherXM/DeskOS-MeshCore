#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* WeatherXM WG1200 dual-boot rules shared by the in-app updater, the
 * crash-loop fallback and the "Switch to WeatherXM" controls. Pure logic with
 * no ESP-IDF calls, so it runs in the native tests; scripts/flash_wg1200.py
 * applies the same slot rules from the host. */

#define D1L_WG1200_APP_HEADER_BYTES 0x100U
#define D1L_WG1200_DESKOS_PROJECT "meshcore_deskos_d1l"

/* Secure Boot V2 (RSA) signature sector appended by espsecure sign-data. */
#define D1L_WG1200_SIGNATURE_SECTOR_BYTES 4096U
#define D1L_WG1200_SIGNATURE_BLOCK_BYTES 1216U
#define D1L_WG1200_SIGNATURE_DIGEST_OFFSET 4U
#define D1L_WG1200_SIGNATURE_KEY_OFFSET 36U
#define D1L_WG1200_SIGNATURE_KEY_BYTES 776U

#define D1L_WG1200_CRASH_FALLBACK_THRESHOLD 3U
#define D1L_WG1200_BUTTON_PRESS_SAMPLES 3U
#define D1L_WG1200_BUTTON_RELEASE_SAMPLES 10U

typedef enum {
    D1L_WG1200_SLOT_FACTORY = 0,
    D1L_WG1200_SLOT_OTA_0,
    D1L_WG1200_SLOT_OTA_1,
    D1L_WG1200_SLOT_COUNT,
    D1L_WG1200_SLOT_NONE = D1L_WG1200_SLOT_COUNT,
} d1l_wg1200_slot_t;

typedef enum {
    D1L_WG1200_SLOT_KIND_EMPTY = 0,
    D1L_WG1200_SLOT_KIND_INVALID,
    D1L_WG1200_SLOT_KIND_DESKOS,
    /* Any other application: WeatherXM firmware, or unknown and kept. */
    D1L_WG1200_SLOT_KIND_WEATHERXM,
} d1l_wg1200_slot_kind_t;

typedef struct {
    uint32_t magic;
    uint32_t crashes;
    uint32_t check;
} d1l_wg1200_crash_guard_t;

typedef enum {
    /* Held since before the prompt appeared; ignored until released. */
    D1L_WG1200_BUTTON_WAIT_RELEASE = 0,
    D1L_WG1200_BUTTON_ARMED,
    D1L_WG1200_BUTTON_PRESSED,
    D1L_WG1200_BUTTON_RELEASED,
} d1l_wg1200_button_phase_t;

typedef struct {
    d1l_wg1200_button_phase_t phase;
    uint8_t stable_samples;
} d1l_wg1200_button_t;

/* Classifies the first D1L_WG1200_APP_HEADER_BYTES of an app slot by its ESP
 * image header and app descriptor, like describe_app() in flash_wg1200.py. */
d1l_wg1200_slot_kind_t d1l_wg1200_slot_kind(const uint8_t *header,
                                            size_t length);

/* The OTA slot an in-app DeskOS update may write: never the running slot and
 * never one holding WeatherXM firmware. proposed is tried first. */
d1l_wg1200_slot_t d1l_wg1200_update_slot(
    d1l_wg1200_slot_t running, d1l_wg1200_slot_t proposed,
    const d1l_wg1200_slot_kind_t kinds[D1L_WG1200_SLOT_COUNT]);

/* The slot that returns the device to WeatherXM firmware: the WeatherXM OTA
 * slot (newer cloud releases land there), else factory. */
d1l_wg1200_slot_t d1l_wg1200_weatherxm_slot(
    d1l_wg1200_slot_t running,
    const d1l_wg1200_slot_kind_t kinds[D1L_WG1200_SLOT_COUNT]);

/* Where a DeskOS update that fails its boot checks returns to: the DeskOS it
 * replaced in the other OTA slot, else WeatherXM firmware. */
d1l_wg1200_slot_t d1l_wg1200_rollback_slot(
    d1l_wg1200_slot_t running,
    const d1l_wg1200_slot_kind_t kinds[D1L_WG1200_SLOT_COUNT]);

/* Counts consecutive crash resets; true once the fallback threshold is hit.
 * Any other reset, or state that fails its check, starts again from zero. */
bool d1l_wg1200_crash_guard_note_boot(d1l_wg1200_crash_guard_t *guard,
                                      bool crash_reset);
void d1l_wg1200_crash_guard_clear(d1l_wg1200_crash_guard_t *guard);

/* Debounces the USER button: a press counts only after a release was seen,
 * and RELEASED follows a press that has been let go. */
d1l_wg1200_button_phase_t d1l_wg1200_button_sample(
    d1l_wg1200_button_t *button, bool pressed);

/* Standard CRC-32 (zlib), the same as esp_rom_crc32_le(). */
uint32_t d1l_wg1200_crc32(uint32_t crc, const uint8_t *data, size_t length);

/* True when block is a well-formed Secure Boot V2 RSA signature block. It
 * does not check the RSA signature or the key; the ROM verifier does that. */
bool d1l_wg1200_signature_block_valid(const uint8_t *block, size_t length);
