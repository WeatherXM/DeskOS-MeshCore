#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "update/wg1200_boot_policy.h"

enum {
    F = D1L_WG1200_SLOT_FACTORY,
    O0 = D1L_WG1200_SLOT_OTA_0,
    O1 = D1L_WG1200_SLOT_OTA_1,
    NONE = D1L_WG1200_SLOT_NONE,
};

static const d1l_wg1200_slot_kind_t EMPTY = D1L_WG1200_SLOT_KIND_EMPTY;
static const d1l_wg1200_slot_kind_t BAD = D1L_WG1200_SLOT_KIND_INVALID;
static const d1l_wg1200_slot_kind_t DESK = D1L_WG1200_SLOT_KIND_DESKOS;
static const d1l_wg1200_slot_kind_t WXM = D1L_WG1200_SLOT_KIND_WEATHERXM;

static void make_header(uint8_t *header, const char *project)
{
    memset(header, 0, D1L_WG1200_APP_HEADER_BYTES);
    header[0] = 0xE9;
    header[12] = 9;
    const uint8_t magic[] = {0x32, 0x54, 0xCD, 0xAB};
    memcpy(&header[0x20], magic, sizeof(magic));
    memcpy(&header[0x30], "1.0", 3);
    memcpy(&header[0x50], project, strlen(project));
}

static void test_slot_kind(void)
{
    uint8_t header[D1L_WG1200_APP_HEADER_BYTES];
    memset(header, 0xFF, sizeof(header));
    assert(d1l_wg1200_slot_kind(header, sizeof(header)) == EMPTY);
    make_header(header, D1L_WG1200_DESKOS_PROJECT);
    assert(d1l_wg1200_slot_kind(header, sizeof(header)) == DESK);
    make_header(header, "wg1010_port");
    assert(d1l_wg1200_slot_kind(header, sizeof(header)) == WXM);
    /* A project name filling all 32 bytes has no terminator. */
    make_header(header, "meshcore_deskos_d1l_but_longer__");
    assert(d1l_wg1200_slot_kind(header, sizeof(header)) == WXM);
    header[0x20] = 0;
    assert(d1l_wg1200_slot_kind(header, sizeof(header)) == BAD);
    make_header(header, "wg1010_port");
    header[0] = 0;
    assert(d1l_wg1200_slot_kind(header, sizeof(header)) == BAD);
    assert(d1l_wg1200_slot_kind(header, 0x6F) == BAD);
    assert(d1l_wg1200_slot_kind(NULL, sizeof(header)) == BAD);
}

static void test_update_slot(void)
{
    /* Typical dual boot: WeatherXM in factory and ota_0, DeskOS in ota_1. */
    const d1l_wg1200_slot_kind_t dual[] = {WXM, WXM, DESK};
    assert(d1l_wg1200_update_slot(O1, O0, dual) == NONE);
    /* WeatherXM never updated over the air: ota_0 is free. */
    const d1l_wg1200_slot_kind_t fresh[] = {WXM, EMPTY, DESK};
    assert(d1l_wg1200_update_slot(O1, O0, fresh) == O0);
    const d1l_wg1200_slot_kind_t two_deskos[] = {WXM, DESK, DESK};
    assert(d1l_wg1200_update_slot(O1, O0, two_deskos) == O0);
    assert(d1l_wg1200_update_slot(O0, O1, two_deskos) == O1);
    /* The proposed slot is never trusted blindly. */
    assert(d1l_wg1200_update_slot(O1, O1, two_deskos) == O0);
    const d1l_wg1200_slot_kind_t broken[] = {WXM, BAD, DESK};
    assert(d1l_wg1200_update_slot(O1, O0, broken) == O0);
    const d1l_wg1200_slot_kind_t wxm_ota1[] = {WXM, DESK, WXM};
    assert(d1l_wg1200_update_slot(O0, O1, wxm_ota1) == NONE);
    assert(d1l_wg1200_update_slot(O0, NONE, fresh) == O1);
    assert(d1l_wg1200_update_slot(O1, O0, NULL) == NONE);
}

static void test_weatherxm_and_rollback_slots(void)
{
    const d1l_wg1200_slot_kind_t dual[] = {WXM, WXM, DESK};
    assert(d1l_wg1200_weatherxm_slot(O1, dual) == O0);
    const d1l_wg1200_slot_kind_t fresh[] = {WXM, EMPTY, DESK};
    assert(d1l_wg1200_weatherxm_slot(O1, fresh) == F);
    const d1l_wg1200_slot_kind_t no_wxm[] = {EMPTY, DESK, DESK};
    assert(d1l_wg1200_weatherxm_slot(O1, no_wxm) == NONE);
    const d1l_wg1200_slot_kind_t factory_deskos[] = {DESK, WXM, WXM};
    assert(d1l_wg1200_weatherxm_slot(F, factory_deskos) == O0);

    /* An update replaced DeskOS in ota_0 with a new DeskOS in ota_1. */
    const d1l_wg1200_slot_kind_t updated[] = {WXM, DESK, DESK};
    assert(d1l_wg1200_rollback_slot(O1, updated) == O0);
    /* Nothing to roll back to except WeatherXM. */
    assert(d1l_wg1200_rollback_slot(O1, dual) == O0);
    assert(d1l_wg1200_rollback_slot(O1, fresh) == F);
    assert(d1l_wg1200_rollback_slot(O1, NULL) == NONE);
}

static void test_crash_guard(void)
{
    d1l_wg1200_crash_guard_t guard;
    memset(&guard, 0xA5, sizeof(guard)); /* RTC memory after power-on */
    assert(!d1l_wg1200_crash_guard_note_boot(&guard, true));
    assert(guard.crashes == 1U);
    assert(!d1l_wg1200_crash_guard_note_boot(&guard, true));
    assert(d1l_wg1200_crash_guard_note_boot(&guard, true));
    assert(guard.crashes == D1L_WG1200_CRASH_FALLBACK_THRESHOLD);
    /* A normal reset starts over. */
    assert(!d1l_wg1200_crash_guard_note_boot(&guard, false));
    assert(guard.crashes == 0U);
    assert(!d1l_wg1200_crash_guard_note_boot(&guard, true));
    assert(!d1l_wg1200_crash_guard_note_boot(&guard, true));
    d1l_wg1200_crash_guard_clear(&guard);
    assert(!d1l_wg1200_crash_guard_note_boot(&guard, true));
    assert(guard.crashes == 1U);
    /* Another firmware wrote over the record. */
    guard.crashes = 7U;
    assert(!d1l_wg1200_crash_guard_note_boot(&guard, true));
    assert(guard.crashes == 1U);
    for (unsigned i = 0U; i < 300U; ++i) {
        (void)d1l_wg1200_crash_guard_note_boot(&guard, true);
    }
    assert(guard.crashes == 255U);
    assert(d1l_wg1200_crash_guard_note_boot(&guard, true));
}

static d1l_wg1200_button_phase_t feed(d1l_wg1200_button_t *button,
                                      bool pressed, unsigned samples)
{
    d1l_wg1200_button_phase_t phase = D1L_WG1200_BUTTON_WAIT_RELEASE;
    for (unsigned i = 0U; i < samples; ++i) {
        phase = d1l_wg1200_button_sample(button, pressed);
    }
    return phase;
}

static void test_button(void)
{
    /* Held through reset: ignored until released. */
    d1l_wg1200_button_t button = {0};
    assert(feed(&button, true, 100U) == D1L_WG1200_BUTTON_WAIT_RELEASE);
    assert(feed(&button, false, D1L_WG1200_BUTTON_PRESS_SAMPLES) ==
           D1L_WG1200_BUTTON_ARMED);
    /* Bounces shorter than the press time do not count. */
    assert(feed(&button, true, D1L_WG1200_BUTTON_PRESS_SAMPLES - 1U) ==
           D1L_WG1200_BUTTON_ARMED);
    assert(feed(&button, false, 1U) == D1L_WG1200_BUTTON_ARMED);
    assert(feed(&button, true, D1L_WG1200_BUTTON_PRESS_SAMPLES) ==
           D1L_WG1200_BUTTON_PRESSED);
    assert(feed(&button, false, D1L_WG1200_BUTTON_RELEASE_SAMPLES - 1U) ==
           D1L_WG1200_BUTTON_PRESSED);
    assert(feed(&button, true, 1U) == D1L_WG1200_BUTTON_PRESSED);
    assert(feed(&button, false, D1L_WG1200_BUTTON_RELEASE_SAMPLES) ==
           D1L_WG1200_BUTTON_RELEASED);
    assert(feed(&button, true, 5U) == D1L_WG1200_BUTTON_RELEASED);
    assert(d1l_wg1200_button_sample(NULL, true) ==
           D1L_WG1200_BUTTON_WAIT_RELEASE);
}

static void test_signature_block(const char *fixture_path)
{
    const uint8_t check[] = "123456789";
    assert(d1l_wg1200_crc32(0U, check, 9U) == 0xCBF43926U);
    const uint8_t seq[] = {1, 0, 0, 0};
    assert(d1l_wg1200_crc32(0xFFFFFFFFU, seq, sizeof(seq)) == 0x4743989AU);

    uint8_t block[D1L_WG1200_SIGNATURE_BLOCK_BYTES];
    FILE *file = fopen(fixture_path, "rb");
    assert(file);
    assert(fread(block, 1U, sizeof(block), file) == sizeof(block));
    fclose(file);
    assert(d1l_wg1200_signature_block_valid(block, sizeof(block)));
    assert(!d1l_wg1200_signature_block_valid(block, sizeof(block) - 1U));
    block[500] ^= 1U;
    assert(!d1l_wg1200_signature_block_valid(block, sizeof(block)));
    block[500] ^= 1U;
    block[1] = 0x03; /* ECDSA scheme */
    assert(!d1l_wg1200_signature_block_valid(block, sizeof(block)));
    memset(block, 0xFF, sizeof(block)); /* unsigned image tail */
    assert(!d1l_wg1200_signature_block_valid(block, sizeof(block)));
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    test_slot_kind();
    test_update_slot();
    test_weatherxm_and_rollback_slots();
    test_crash_guard();
    test_button();
    test_signature_block(argv[1]);
    puts("wg1200 boot policy ok");
    return 0;
}
