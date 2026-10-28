#include "update/wg1200_boot.h"

#include "sdkconfig.h"

#if CONFIG_LCD_BOARD_SENSECAP_INDICATOR_WXM

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "comms/connectivity_manager.h"
#include "diagnostics/event_log.h"
#include "driver/gpio.h"
#include "esp_attr.h"
#include "esp_efuse.h"
#include "esp_image_format.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_rom_sys.h"
#include "esp_secure_boot.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "hal/indicator_board.h"
#include "hal/rp2040_bridge.h"
#include "mbedtls/sha256.h"
#include "mesh/route_store_worker.h"
#include "storage/storage_status.h"
#include "update/wg1200_boot_policy.h"
#include "update/wg1200_signature.h"

#define WG1200_USER_BUTTON_GPIO GPIO_NUM_38
#define WG1200_MENU_WINDOW_MS 3000U
#define WG1200_MENU_RELEASE_TIMEOUT_MS 10000U
#define WG1200_MENU_FAILURE_NOTICE_MS 2000U
#define WG1200_MENU_POLL_MS 20U
#define WG1200_MENU_REDRAW_MS 500U
#define WG1200_HEALTHY_UPTIME_US (60LL * 1000LL * 1000LL)
#define WG1200_QUIESCE_TIMEOUT_MS 5000U
#define WG1200_RESET_DRAIN_MS 300U
#define WG1200_FILE_TIMEOUT_MS 5000U
#define WG1200_TRUSTED_KEYS 3U

static const char *TAG = "wg1200_boot";

/* Survives panic and watchdog resets; the guard validates it before use. */
static RTC_NOINIT_ATTR d1l_wg1200_crash_guard_t s_crash_guard;
static esp_timer_handle_t s_healthy_timer;
static bool s_weatherxm_available;

static const esp_partition_t *slot_partition(d1l_wg1200_slot_t slot)
{
    static const esp_partition_subtype_t subtypes[D1L_WG1200_SLOT_COUNT] = {
        ESP_PARTITION_SUBTYPE_APP_FACTORY,
        ESP_PARTITION_SUBTYPE_APP_OTA_0,
        ESP_PARTITION_SUBTYPE_APP_OTA_1,
    };
    if (slot >= D1L_WG1200_SLOT_COUNT) {
        return NULL;
    }
    return esp_partition_find_first(ESP_PARTITION_TYPE_APP, subtypes[slot],
                                    NULL);
}

static d1l_wg1200_slot_t partition_slot(const esp_partition_t *partition)
{
    for (d1l_wg1200_slot_t slot = D1L_WG1200_SLOT_FACTORY;
         partition && slot < D1L_WG1200_SLOT_COUNT; ++slot) {
        const esp_partition_t *candidate = slot_partition(slot);
        if (candidate && candidate->address == partition->address) {
            return slot;
        }
    }
    return D1L_WG1200_SLOT_NONE;
}

static d1l_wg1200_slot_t running_slot(void)
{
    return partition_slot(esp_ota_get_running_partition());
}

static void read_slot_kinds(d1l_wg1200_slot_kind_t kinds[D1L_WG1200_SLOT_COUNT])
{
    uint8_t header[D1L_WG1200_APP_HEADER_BYTES];
    for (d1l_wg1200_slot_t slot = D1L_WG1200_SLOT_FACTORY;
         slot < D1L_WG1200_SLOT_COUNT; ++slot) {
        const esp_partition_t *partition = slot_partition(slot);
        kinds[slot] = partition &&
                esp_partition_read(partition, 0U, header, sizeof(header)) ==
                    ESP_OK ?
            d1l_wg1200_slot_kind(header, sizeof(header)) :
            D1L_WG1200_SLOT_KIND_INVALID;
    }
}

static const esp_partition_t *find_weatherxm_partition(void)
{
    d1l_wg1200_slot_kind_t kinds[D1L_WG1200_SLOT_COUNT];
    read_slot_kinds(kinds);
    return slot_partition(d1l_wg1200_weatherxm_slot(running_slot(), kinds));
}

static const esp_partition_t *find_rollback_partition(void)
{
    d1l_wg1200_slot_kind_t kinds[D1L_WG1200_SLOT_COUNT];
    read_slot_kinds(kinds);
    return slot_partition(d1l_wg1200_rollback_slot(running_slot(), kinds));
}

static esp_err_t quiesce_for_restart(void)
{
    esp_err_t ret =
        d1l_storage_manager_quiesce_begin(WG1200_QUIESCE_TIMEOUT_MS);
    if (ret != ESP_OK) {
        return ret;
    }
    ret = d1l_route_store_worker_quiesce_begin(WG1200_QUIESCE_TIMEOUT_MS);
    if (ret != ESP_OK) {
        d1l_storage_manager_quiesce_end();
        return ret;
    }
    ret = d1l_rp2040_bridge_quiesce_begin(WG1200_QUIESCE_TIMEOUT_MS);
    if (ret == ESP_OK) {
        ret = d1l_connectivity_prepare_reboot();
        if (ret == ESP_OK) {
            return ESP_OK;
        }
        d1l_rp2040_bridge_quiesce_end();
    }
    d1l_route_store_worker_quiesce_end();
    d1l_storage_manager_quiesce_end();
    return ret;
}

static esp_err_t restart_into(const esp_partition_t *target,
                              bool services_running, const char *reason)
{
    if (!target) {
        return ESP_ERR_NOT_FOUND;
    }
    /* esp_ota_set_boot_partition() repeats this check; doing it first keeps
     * a damaged target image from costing a quiesced, offline DeskOS. */
    const esp_partition_pos_t position = {
        .offset = target->address,
        .size = target->size,
    };
    esp_image_metadata_t metadata = {0};
    esp_err_t ret = esp_image_verify(ESP_IMAGE_VERIFY_SILENT, &position,
                                     &metadata);
    if (ret != ESP_OK) {
        return ret;
    }
    if (services_running) {
        ret = quiesce_for_restart();
        if (ret != ESP_OK) {
            return ret;
        }
    }
    ret = esp_ota_set_boot_partition(target);
    if (ret != ESP_OK) {
        if (services_running) {
            d1l_rp2040_bridge_quiesce_end();
            d1l_route_store_worker_quiesce_end();
            d1l_storage_manager_quiesce_end();
        }
        return ret;
    }
    d1l_wg1200_crash_guard_clear(&s_crash_guard);
    ESP_LOGW(TAG, "restarting into %s: %s", target->label,
             reason ? reason : "requested");
    if (services_running) {
        d1l_event_log_append(D1L_EVENT_LOG_LEVEL_INFO, "boot", target->label,
                             reason ? reason : "requested");
    }
    fflush(stdout);
    vTaskDelay(pdMS_TO_TICKS(WG1200_RESET_DRAIN_MS));
    /* A full system reset, like power-on, releases GPIO10 from the LCD, so
     * the stock bootloader never reads its factory-reset level. */
    esp_rom_software_reset_system();
    for (;;) {
    }
}

static bool reset_is_crash(esp_reset_reason_t reason)
{
    return reason == ESP_RST_PANIC || reason == ESP_RST_INT_WDT ||
           reason == ESP_RST_TASK_WDT || reason == ESP_RST_WDT ||
           reason == ESP_RST_BROWNOUT;
}

void d1l_wg1200_boot_guard_init(void)
{
    d1l_wg1200_slot_kind_t kinds[D1L_WG1200_SLOT_COUNT];
    read_slot_kinds(kinds);
    s_weatherxm_available =
        d1l_wg1200_weatherxm_slot(running_slot(), kinds) !=
        D1L_WG1200_SLOT_NONE;
    if (!d1l_wg1200_crash_guard_note_boot(
            &s_crash_guard, reset_is_crash(esp_reset_reason()))) {
        return;
    }
    ESP_LOGE(TAG, "%lu crash resets in a row",
             (unsigned long)s_crash_guard.crashes);
    if (!s_weatherxm_available) {
        ESP_LOGE(TAG, "no WeatherXM firmware to fall back to; starting DeskOS");
        return;
    }
    const esp_err_t ret =
        restart_into(find_weatherxm_partition(), false, "crash loop");
    ESP_LOGE(TAG, "WeatherXM fallback failed: %s", esp_err_to_name(ret));
}

static void healthy_timer_cb(void *argument)
{
    (void)argument;
    d1l_wg1200_crash_guard_clear(&s_crash_guard);
}

void d1l_wg1200_boot_guard_mark_started(void)
{
    if (s_healthy_timer) {
        return;
    }
    const esp_timer_create_args_t args = {
        .callback = healthy_timer_cb,
        .name = "wg1200_healthy",
    };
    if (esp_timer_create(&args, &s_healthy_timer) != ESP_OK ||
        esp_timer_start_once(s_healthy_timer, WG1200_HEALTHY_UPTIME_US) !=
            ESP_OK) {
        ESP_LOGW(TAG, "crash count stays until the next normal restart");
    }
}

bool d1l_wg1200_boot_weatherxm_available(void)
{
    return s_weatherxm_available;
}

static bool user_button_pressed(void)
{
    /* Read the pad, not the button driver: its PRESS_DOWN event ends at a
     * long press while the button is still held. */
    return gpio_get_level(WG1200_USER_BUTTON_GPIO) == 0;
}

static uint32_t elapsed_ms(int64_t since_us)
{
    return (uint32_t)((esp_timer_get_time() - since_us) / 1000LL);
}

void d1l_wg1200_boot_menu_run(void)
{
    if (!s_weatherxm_available) {
        return;
    }
    d1l_wg1200_button_t button = {0};
    const int64_t opened_us = esp_timer_get_time();
    uint32_t next_redraw_ms = 0U;
    for (;;) {
        const uint32_t elapsed = elapsed_ms(opened_us);
        if (d1l_wg1200_button_sample(&button, user_button_pressed()) ==
            D1L_WG1200_BUTTON_PRESSED) {
            break;
        }
        if (elapsed >= WG1200_MENU_WINDOW_MS) {
            (void)d1l_board_display_boot_splash();
            return;
        }
        if (elapsed >= next_redraw_ms) {
            (void)d1l_board_display_boot_message(
                "PRESS USER BUTTON", "FOR WEATHERXM",
                (uint8_t)(100U - elapsed * 100U / WG1200_MENU_WINDOW_MS));
            next_redraw_ms = elapsed + WG1200_MENU_REDRAW_MS;
        }
        vTaskDelay(pdMS_TO_TICKS(WG1200_MENU_POLL_MS));
    }

    /* Restart only after release: a newer stock bootloader treats USER held
     * through reset as a factory-reset request. */
    (void)d1l_board_display_boot_message("RELEASE BUTTON",
                                         "TO START WEATHERXM", 100U);
    const int64_t pressed_us = esp_timer_get_time();
    while (d1l_wg1200_button_sample(&button, user_button_pressed()) !=
           D1L_WG1200_BUTTON_RELEASED) {
        if (elapsed_ms(pressed_us) >= WG1200_MENU_RELEASE_TIMEOUT_MS) {
            ESP_LOGW(TAG, "USER button held too long; starting DeskOS");
            (void)d1l_board_display_boot_splash();
            return;
        }
        vTaskDelay(pdMS_TO_TICKS(WG1200_MENU_POLL_MS));
    }
    (void)d1l_board_display_boot_message("STARTING", "WEATHERXM", 100U);
    const esp_err_t ret =
        restart_into(find_weatherxm_partition(), false, "boot menu");
    ESP_LOGE(TAG, "WeatherXM start failed: %s", esp_err_to_name(ret));
    (void)d1l_board_display_boot_message("SWITCH FAILED", "STARTING DESKOS",
                                         0U);
    vTaskDelay(pdMS_TO_TICKS(WG1200_MENU_FAILURE_NOTICE_MS));
    (void)d1l_board_display_boot_splash();
}

esp_err_t d1l_wg1200_boot_switch_to_weatherxm(void)
{
    return restart_into(find_weatherxm_partition(), true, "user request");
}

bool d1l_wg1200_boot_stock_bootloader(void)
{
    return true;
}

esp_err_t d1l_wg1200_boot_select_update_target(const esp_partition_t **target)
{
    if (!target) {
        return ESP_ERR_INVALID_ARG;
    }
    d1l_wg1200_slot_kind_t kinds[D1L_WG1200_SLOT_COUNT];
    read_slot_kinds(kinds);
    const esp_partition_t *partition = slot_partition(d1l_wg1200_update_slot(
        running_slot(), partition_slot(*target), kinds));
    if (!partition) {
        d1l_event_log_append(D1L_EVENT_LOG_LEVEL_ERROR, "update",
                             "slot_refused",
                             "no OTA slot free of WeatherXM firmware");
        return ESP_ERR_NOT_ALLOWED;
    }
    *target = partition;
    return ESP_OK;
}

static size_t read_trusted_digests(
    uint8_t digests[WG1200_TRUSTED_KEYS][D1L_WG1200_SHA256_BYTES])
{
    static const esp_efuse_purpose_t purposes[WG1200_TRUSTED_KEYS] = {
        ESP_EFUSE_KEY_PURPOSE_SECURE_BOOT_DIGEST0,
        ESP_EFUSE_KEY_PURPOSE_SECURE_BOOT_DIGEST1,
        ESP_EFUSE_KEY_PURPOSE_SECURE_BOOT_DIGEST2,
    };
    size_t count = 0U;
    for (unsigned i = 0U; i < WG1200_TRUSTED_KEYS; ++i) {
        esp_efuse_block_t block = EFUSE_BLK_MAX;
        if (esp_efuse_get_digest_revoke(i) ||
            !esp_efuse_find_purpose(purposes[i], &block) ||
            esp_efuse_read_block(block, digests[count], 0U,
                                 D1L_WG1200_SHA256_BYTES * 8U) != ESP_OK) {
            continue;
        }
        ++count;
    }
    return count;
}

static esp_err_t read_file_range(const char *path, uint32_t offset,
                                 uint8_t *out, size_t length)
{
    size_t done = 0U;
    while (done < length) {
        const size_t requested = length - done < D1L_RP2040_FILE_CHUNK_MAX ?
            length - done : D1L_RP2040_FILE_CHUNK_MAX;
        d1l_rp2040_file_result_t result = {0};
        const esp_err_t ret = d1l_rp2040_bridge_file_read(
            path, offset + (uint32_t)done, &out[done], requested, &result,
            WG1200_FILE_TIMEOUT_MS);
        if (ret != ESP_OK) {
            return ret;
        }
        if (result.offset != offset + done || result.length == 0U ||
            result.length > requested) {
            return ESP_ERR_INVALID_RESPONSE;
        }
        done += result.length;
    }
    return ESP_OK;
}

static esp_err_t refuse_unsigned(const char *detail)
{
    d1l_event_log_append(D1L_EVENT_LOG_LEVEL_ERROR, "update", "unsigned",
                         detail);
    return ESP_ERR_INVALID_CRC;
}

esp_err_t d1l_wg1200_boot_check_update_file(const char *path,
                                            uint32_t image_size)
{
    if (!esp_secure_boot_enabled()) {
        return ESP_OK;
    }
    if (!path || image_size < 2U * D1L_WG1200_SIGNATURE_SECTOR_BYTES ||
        image_size % D1L_WG1200_SIGNATURE_SECTOR_BYTES != 0U) {
        return refuse_unsigned("image has no Secure Boot signature sector");
    }
    uint8_t trusted[WG1200_TRUSTED_KEYS][D1L_WG1200_SHA256_BYTES] = {{0}};
    const size_t trusted_count = read_trusted_digests(trusted);
    uint8_t *sector = malloc(D1L_WG1200_SIGNATURE_SECTOR_BYTES);
    if (!sector) {
        return ESP_ERR_NO_MEM;
    }
    esp_err_t ret = read_file_range(
        path, image_size - D1L_WG1200_SIGNATURE_SECTOR_BYTES, sector,
        D1L_WG1200_SIGNATURE_SECTOR_BYTES);
    if (ret == ESP_OK &&
        !d1l_wg1200_signature_sector_trusted(
            sector, D1L_WG1200_SIGNATURE_SECTOR_BYTES, trusted,
            trusted_count)) {
        ret = refuse_unsigned("image is not signed with a key this unit trusts");
    }
    free(sector);
    return ret;
}

esp_err_t d1l_wg1200_boot_verify_app_signature(const esp_partition_t *partition,
                                               uint32_t image_size)
{
    if (!esp_secure_boot_enabled()) {
        return ESP_OK;
    }
    if (!partition || image_size == 0U || image_size > partition->size) {
        return ESP_ERR_INVALID_ARG;
    }
    /* The bootloader hashes the image padded to a sector and reads the
     * signature sector right after it. */
    const esp_partition_pos_t position = {
        .offset = partition->address,
        .size = partition->size,
    };
    esp_image_metadata_t metadata = {0};
    esp_err_t ret = esp_image_get_metadata(&position, &metadata);
    if (ret != ESP_OK) {
        return ret;
    }
    const uint32_t signed_bytes =
        (metadata.image_len + D1L_WG1200_SIGNATURE_SECTOR_BYTES - 1U) &
        ~(D1L_WG1200_SIGNATURE_SECTOR_BYTES - 1U);
    if (signed_bytes == 0U || signed_bytes > image_size ||
        image_size - signed_bytes < D1L_WG1200_SIGNATURE_SECTOR_BYTES) {
        return refuse_unsigned("installed image has no signature sector");
    }
    uint8_t *buffer = malloc(D1L_WG1200_SIGNATURE_SECTOR_BYTES);
    if (!buffer) {
        return ESP_ERR_NO_MEM;
    }
    uint8_t digest[D1L_WG1200_SHA256_BYTES] = {0};
    mbedtls_sha256_context context;
    mbedtls_sha256_init(&context);
    ret = mbedtls_sha256_starts(&context, 0) == 0 ? ESP_OK : ESP_FAIL;
    for (uint32_t offset = 0U; ret == ESP_OK && offset < signed_bytes;) {
        const size_t length =
            signed_bytes - offset < D1L_WG1200_SIGNATURE_SECTOR_BYTES ?
                signed_bytes - offset : D1L_WG1200_SIGNATURE_SECTOR_BYTES;
        ret = esp_partition_read(partition, offset, buffer, length);
        if (ret == ESP_OK &&
            mbedtls_sha256_update(&context, buffer, length) != 0) {
            ret = ESP_FAIL;
        }
        offset += length;
        taskYIELD();
    }
    if (ret == ESP_OK && mbedtls_sha256_finish(&context, digest) != 0) {
        ret = ESP_FAIL;
    }
    mbedtls_sha256_free(&context);
    if (ret == ESP_OK) {
        ret = esp_partition_read(partition, signed_bytes, buffer,
                                 D1L_WG1200_SIGNATURE_SECTOR_BYTES);
    }
    if (ret == ESP_OK) {
        uint8_t trusted[WG1200_TRUSTED_KEYS][D1L_WG1200_SHA256_BYTES] = {{0}};
        const size_t trusted_count = read_trusted_digests(trusted);
        if (!d1l_wg1200_signature_sector_verifies(
                buffer, D1L_WG1200_SIGNATURE_SECTOR_BYTES, digest, trusted,
                trusted_count)) {
            ret = refuse_unsigned("installed image fails Secure Boot verification");
        }
    }
    free(buffer);
    return ret;
}

esp_err_t d1l_wg1200_boot_roll_back(const char *reason)
{
    return restart_into(find_rollback_partition(), true, reason);
}

#else

void d1l_wg1200_boot_guard_init(void)
{
}

void d1l_wg1200_boot_guard_mark_started(void)
{
}

bool d1l_wg1200_boot_weatherxm_available(void)
{
    return false;
}

void d1l_wg1200_boot_menu_run(void)
{
}

esp_err_t d1l_wg1200_boot_switch_to_weatherxm(void)
{
    return ESP_ERR_NOT_SUPPORTED;
}

bool d1l_wg1200_boot_stock_bootloader(void)
{
    return false;
}

esp_err_t d1l_wg1200_boot_select_update_target(const esp_partition_t **target)
{
    return target ? ESP_OK : ESP_ERR_INVALID_ARG;
}

esp_err_t d1l_wg1200_boot_check_update_file(const char *path,
                                            uint32_t image_size)
{
    (void)path;
    (void)image_size;
    return ESP_OK;
}

esp_err_t d1l_wg1200_boot_verify_app_signature(const esp_partition_t *partition,
                                               uint32_t image_size)
{
    (void)partition;
    (void)image_size;
    return ESP_OK;
}

esp_err_t d1l_wg1200_boot_roll_back(const char *reason)
{
    (void)reason;
    return ESP_ERR_NOT_SUPPORTED;
}

#endif
