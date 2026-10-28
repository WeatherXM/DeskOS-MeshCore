#include "update/wg1200_boot_policy.h"

#include <string.h>

#define ESP_IMAGE_MAGIC 0xE9U
#define APP_DESC_OFFSET 0x20U
#define APP_DESC_MAGIC 0xABCD5432U
#define APP_DESC_PROJECT_OFFSET (APP_DESC_OFFSET + 0x30U)
#define APP_DESC_PROJECT_BYTES 32U
#define SIGNATURE_MAGIC 0xE7U
#define SIGNATURE_VERSION_RSA 0x02U
#define SIGNATURE_CRC_BYTES 1196U
#define CRASH_GUARD_MAGIC 0x57584D42U /* "WXMB" */
#define CRASH_GUARD_MAX 255U

static uint32_t read_le32(const uint8_t *bytes)
{
    return (uint32_t)bytes[0] | ((uint32_t)bytes[1] << 8) |
           ((uint32_t)bytes[2] << 16) | ((uint32_t)bytes[3] << 24);
}

static bool slot_is_ota(d1l_wg1200_slot_t slot)
{
    return slot == D1L_WG1200_SLOT_OTA_0 || slot == D1L_WG1200_SLOT_OTA_1;
}

d1l_wg1200_slot_kind_t d1l_wg1200_slot_kind(const uint8_t *header,
                                            size_t length)
{
    if (!header || length < APP_DESC_PROJECT_OFFSET + APP_DESC_PROJECT_BYTES) {
        return D1L_WG1200_SLOT_KIND_INVALID;
    }
    bool erased = true;
    for (size_t i = 0U; erased && i < length; ++i) {
        erased = header[i] == 0xFFU;
    }
    if (erased) {
        return D1L_WG1200_SLOT_KIND_EMPTY;
    }
    if (header[0] != ESP_IMAGE_MAGIC ||
        read_le32(&header[APP_DESC_OFFSET]) != APP_DESC_MAGIC) {
        return D1L_WG1200_SLOT_KIND_INVALID;
    }
    char project[APP_DESC_PROJECT_BYTES + 1U] = {0};
    memcpy(project, &header[APP_DESC_PROJECT_OFFSET], APP_DESC_PROJECT_BYTES);
    return strcmp(project, D1L_WG1200_DESKOS_PROJECT) == 0 ?
        D1L_WG1200_SLOT_KIND_DESKOS : D1L_WG1200_SLOT_KIND_WEATHERXM;
}

d1l_wg1200_slot_t d1l_wg1200_update_slot(
    d1l_wg1200_slot_t running, d1l_wg1200_slot_t proposed,
    const d1l_wg1200_slot_kind_t kinds[D1L_WG1200_SLOT_COUNT])
{
    if (!kinds) {
        return D1L_WG1200_SLOT_NONE;
    }
    const d1l_wg1200_slot_t first =
        slot_is_ota(proposed) ? proposed : D1L_WG1200_SLOT_OTA_0;
    const d1l_wg1200_slot_t order[] = {
        first,
        first == D1L_WG1200_SLOT_OTA_0 ?
            D1L_WG1200_SLOT_OTA_1 : D1L_WG1200_SLOT_OTA_0,
    };
    for (size_t i = 0U; i < sizeof(order) / sizeof(order[0]); ++i) {
        if (order[i] != running &&
            kinds[order[i]] != D1L_WG1200_SLOT_KIND_WEATHERXM) {
            return order[i];
        }
    }
    return D1L_WG1200_SLOT_NONE;
}

d1l_wg1200_slot_t d1l_wg1200_weatherxm_slot(
    d1l_wg1200_slot_t running,
    const d1l_wg1200_slot_kind_t kinds[D1L_WG1200_SLOT_COUNT])
{
    if (!kinds) {
        return D1L_WG1200_SLOT_NONE;
    }
    d1l_wg1200_slot_t ota_slot = D1L_WG1200_SLOT_NONE;
    unsigned ota_count = 0U;
    for (d1l_wg1200_slot_t slot = D1L_WG1200_SLOT_OTA_0;
         slot <= D1L_WG1200_SLOT_OTA_1; ++slot) {
        if (slot != running &&
            kinds[slot] == D1L_WG1200_SLOT_KIND_WEATHERXM) {
            if (ota_slot == D1L_WG1200_SLOT_NONE) {
                ota_slot = slot;
            }
            ++ota_count;
        }
    }
    if (ota_count == 1U) {
        return ota_slot;
    }
    if (running != D1L_WG1200_SLOT_FACTORY &&
        kinds[D1L_WG1200_SLOT_FACTORY] == D1L_WG1200_SLOT_KIND_WEATHERXM) {
        return D1L_WG1200_SLOT_FACTORY;
    }
    return ota_slot;
}

d1l_wg1200_slot_t d1l_wg1200_rollback_slot(
    d1l_wg1200_slot_t running,
    const d1l_wg1200_slot_kind_t kinds[D1L_WG1200_SLOT_COUNT])
{
    if (!kinds) {
        return D1L_WG1200_SLOT_NONE;
    }
    if (slot_is_ota(running)) {
        const d1l_wg1200_slot_t previous = running == D1L_WG1200_SLOT_OTA_0 ?
            D1L_WG1200_SLOT_OTA_1 : D1L_WG1200_SLOT_OTA_0;
        if (kinds[previous] == D1L_WG1200_SLOT_KIND_DESKOS) {
            return previous;
        }
    }
    return d1l_wg1200_weatherxm_slot(running, kinds);
}

static void crash_guard_seal(d1l_wg1200_crash_guard_t *guard)
{
    guard->magic = CRASH_GUARD_MAGIC;
    guard->check = ~(guard->magic ^ guard->crashes);
}

bool d1l_wg1200_crash_guard_note_boot(d1l_wg1200_crash_guard_t *guard,
                                      bool crash_reset)
{
    if (!guard) {
        return false;
    }
    const bool valid = guard->magic == CRASH_GUARD_MAGIC &&
        guard->crashes <= CRASH_GUARD_MAX &&
        guard->check == ~(guard->magic ^ guard->crashes);
    uint32_t crashes = valid ? guard->crashes : 0U;
    if (!crash_reset) {
        crashes = 0U;
    } else if (crashes < CRASH_GUARD_MAX) {
        ++crashes;
    }
    guard->crashes = crashes;
    crash_guard_seal(guard);
    return crashes >= D1L_WG1200_CRASH_FALLBACK_THRESHOLD;
}

void d1l_wg1200_crash_guard_clear(d1l_wg1200_crash_guard_t *guard)
{
    if (!guard) {
        return;
    }
    guard->crashes = 0U;
    crash_guard_seal(guard);
}

d1l_wg1200_button_phase_t d1l_wg1200_button_sample(
    d1l_wg1200_button_t *button, bool pressed)
{
    if (!button) {
        return D1L_WG1200_BUTTON_WAIT_RELEASE;
    }
    bool wanted = false;
    uint8_t needed = D1L_WG1200_BUTTON_PRESS_SAMPLES;
    d1l_wg1200_button_phase_t next = button->phase;
    switch (button->phase) {
    case D1L_WG1200_BUTTON_WAIT_RELEASE:
        wanted = !pressed;
        next = D1L_WG1200_BUTTON_ARMED;
        break;
    case D1L_WG1200_BUTTON_ARMED:
        wanted = pressed;
        next = D1L_WG1200_BUTTON_PRESSED;
        break;
    case D1L_WG1200_BUTTON_PRESSED:
        wanted = !pressed;
        needed = D1L_WG1200_BUTTON_RELEASE_SAMPLES;
        next = D1L_WG1200_BUTTON_RELEASED;
        break;
    case D1L_WG1200_BUTTON_RELEASED:
    default:
        return D1L_WG1200_BUTTON_RELEASED;
    }
    if (!wanted) {
        button->stable_samples = 0U;
        return button->phase;
    }
    if (++button->stable_samples >= needed) {
        button->phase = next;
        button->stable_samples = 0U;
    }
    return button->phase;
}

uint32_t d1l_wg1200_crc32(uint32_t crc, const uint8_t *data, size_t length)
{
    crc = ~crc;
    for (size_t i = 0U; data && i < length; ++i) {
        crc ^= data[i];
        for (unsigned bit = 0U; bit < 8U; ++bit) {
            crc = (crc >> 1) ^ (0xEDB88320U & (0U - (crc & 1U)));
        }
    }
    return ~crc;
}

bool d1l_wg1200_signature_block_valid(const uint8_t *block, size_t length)
{
    return block && length >= D1L_WG1200_SIGNATURE_BLOCK_BYTES &&
        block[0] == SIGNATURE_MAGIC && block[1] == SIGNATURE_VERSION_RSA &&
        d1l_wg1200_crc32(0U, block, SIGNATURE_CRC_BYTES) ==
            read_le32(&block[SIGNATURE_CRC_BYTES]);
}
