#include "storage/storage_wxm_sd.h"

#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/unistd.h>
#include <dirent.h>

#include "esp_log.h"
#include "esp_vfs_fat.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "bsp_board.h"
#include "bsp_storage.h"
#include "tca9535.h"

static const char *TAG = "storage_wxm_sd";

#define WXM_SD_DET_PIN 12U
#define WXM_SD_MOUNT_POINT "/sdcard"

static bool s_mounted = false;
static bool s_tree_verified = false;
static SemaphoreHandle_t s_sd_file_mutex = NULL;

static void sd_file_lock(void)
{
    if (!s_sd_file_mutex) {
        s_sd_file_mutex = xSemaphoreCreateMutex();
    }
    if (s_sd_file_mutex) {
        xSemaphoreTake(s_sd_file_mutex, portMAX_DELAY);
    }
}

static void sd_file_unlock(void)
{
    if (s_sd_file_mutex) {
        xSemaphoreGive(s_sd_file_mutex);
    }
}

static bool resolve_sd_path(const char *rel_path, char *out_path, size_t out_size)
{
    if (!rel_path || rel_path[0] == '\0' || !out_path || out_size == 0) {
        return false;
    }
    int written = 0;
    if (strncmp(rel_path, WXM_SD_MOUNT_POINT, strlen(WXM_SD_MOUNT_POINT)) == 0) {
        written = snprintf(out_path, out_size, "%s", rel_path);
    } else if (strncmp(rel_path, "deskos/", 7) == 0) {
        written = snprintf(out_path, out_size, "%s/%s", WXM_SD_MOUNT_POINT, rel_path);
    } else if (strncmp(rel_path, "/deskos/", 8) == 0) {
        written = snprintf(out_path, out_size, "%s%s", WXM_SD_MOUNT_POINT, rel_path);
    } else if (rel_path[0] == '/') {
        written = snprintf(out_path, out_size, "%s/deskos%s", WXM_SD_MOUNT_POINT, rel_path);
    } else {
        written = snprintf(out_path, out_size, "%s/deskos/%s", WXM_SD_MOUNT_POINT, rel_path);
    }
    return (written > 0 && (size_t)written < out_size);
}

static void ensure_deskos_tree(void)
{
    const char *dirs[] = {
        WXM_SD_MOUNT_POINT "/deskos",
        WXM_SD_MOUNT_POINT "/deskos/stores",
        WXM_SD_MOUNT_POINT "/deskos/stores/messages",
        WXM_SD_MOUNT_POINT "/deskos/stores/messages/public",
        WXM_SD_MOUNT_POINT "/deskos/stores/messages/dm",
        WXM_SD_MOUNT_POINT "/deskos/stores/nodes",
        WXM_SD_MOUNT_POINT "/deskos/stores/contacts",
        WXM_SD_MOUNT_POINT "/deskos/stores/read_state",
        WXM_SD_MOUNT_POINT "/deskos/stores/routes",
        WXM_SD_MOUNT_POINT "/deskos/stores/packet_log",
        WXM_SD_MOUNT_POINT "/deskos/exports",
        WXM_SD_MOUNT_POINT "/deskos/exports/diagnostics",
        WXM_SD_MOUNT_POINT "/deskos/exports/data",
        WXM_SD_MOUNT_POINT "/deskos/canary",
        WXM_SD_MOUNT_POINT "/deskos/map",
        WXM_SD_MOUNT_POINT "/deskos/map/tiles",
    };
    for (size_t i = 0; i < sizeof(dirs) / sizeof(dirs[0]); i++) {
        struct stat st = {0};
        if (stat(dirs[i], &st) != 0) {
            mkdir(dirs[i], 0755);
        }
    }

    struct stat st = {0};
    if (stat(WXM_SD_MOUNT_POINT "/deskos/manifest.json", &st) != 0) {
        FILE *f = fopen(WXM_SD_MOUNT_POINT "/deskos/manifest.json", "w");
        if (f) {
            fputs("{\"name\":\"MeshCore DeskOS D1L SD\",\"schema\":1,\"created_by\":\"MeshCore DeskOS D1L\",\"device\":\"weatherxm-wg1200\",\"stores\":[\"messages\",\"dm\",\"nodes\",\"routes\",\"packets\",\"map_tiles\"]}\n", f);
            fclose(f);
        }
    }

    if (stat(WXM_SD_MOUNT_POINT "/deskos/map/manifest.json", &st) != 0) {
        FILE *f = fopen(WXM_SD_MOUNT_POINT "/deskos/map/manifest.json", "w");
        if (f) {
            fputs("{\"schema\":2,\"kind\":\"map_cache\",\"tile_template\":\"map/tiles/nrcan-cbmt/z{z}/x{x}/y{y}.png\",\"interactive_download_supported\":true,\"background_prefetch_supported\":true,\"source\":\"nrcan-cbmt\",\"provider_config\":\"map/offline-provider.json\"}\n", f);
            fclose(f);
        }
    }
}

static void ensure_parent_dirs(const char *file_path)
{
    char temp[128];
    if (snprintf(temp, sizeof(temp), "%s", file_path) >= sizeof(temp)) {
        return;
    }
    char *last_slash = strrchr(temp, '/');
    if (!last_slash) {
        return;
    }
    *last_slash = '\0';

    char *p = temp;
    if (strncmp(p, WXM_SD_MOUNT_POINT, strlen(WXM_SD_MOUNT_POINT)) == 0) {
        p += strlen(WXM_SD_MOUNT_POINT);
    }
    if (*p == '/') {
        p++;
    }
    while (*p) {
        if (*p == '/') {
            *p = '\0';
            struct stat st;
            if (stat(temp, &st) != 0) {
                mkdir(temp, 0755);
            }
            *p = '/';
        }
        p++;
    }
    struct stat st;
    if (stat(temp, &st) != 0) {
        mkdir(temp, 0755);
    }
}

static uint32_t calc_crc32(const uint8_t *data, size_t len)
{
    uint32_t crc = UINT32_MAX;
    for (size_t i = 0; i < len; ++i) {
        crc ^= data[i];
        for (uint32_t bit = 0; bit < 8; ++bit) {
            const uint32_t mask = 0U - (crc & 1U);
            crc = (crc >> 1U) ^ (UINT32_C(0xedb88320) & mask);
        }
    }
    return ~crc;
}

static void init_file_result(d1l_rp2040_file_result_t *out, esp_err_t err)
{
    if (!out) return;
    memset(out, 0, sizeof(*out));
    out->bridge_ready = true;
    out->protocol_supported = true;
    out->last_error = err;
    out->ok = (err == ESP_OK);
}

static bool check_card_present(void)
{
    uint16_t pins = 0;
    if (tca9535_read_input_pins(&pins) != ESP_OK) {
        return false;
    }
    /* SD_DET is active LOW on PCA9535 pin 12 */
    return !(pins & (1U << WXM_SD_DET_PIN));
}

esp_err_t d1l_storage_wxm_sd_probe(d1l_rp2040_sd_status_t *out_status)
{
    if (!out_status) {
        return ESP_ERR_INVALID_ARG;
    }
    memset(out_status, 0, sizeof(*out_status));
    out_status->bridge_ready = true;
    out_status->protocol_supported = true;
    out_status->file_ops_supported = true;
    out_status->atomic_rename_supported = true;
    out_status->file_chunk_max = 512;
    out_status->file_line_max = D1L_RP2040_FILE_LINE_MAX;
    out_status->path_max = 128;

    const bool present = check_card_present();
    out_status->card_present = present;

    if (!present) {
        s_mounted = false;
        out_status->filesystem_mounted = false;
        out_status->deskos_root_ready = false;
        out_status->data_ready = false;
        snprintf(out_status->state, sizeof(out_status->state), "no_card");
        snprintf(out_status->filesystem, sizeof(out_status->filesystem), "unknown");
        snprintf(out_status->note, sizeof(out_status->note), "MicroSD card not detected in slot");
        return ESP_OK;
    }

    if (!s_mounted) {
        out_status->filesystem_mounted = false;
        out_status->deskos_root_ready = false;
        out_status->data_ready = false;
        snprintf(out_status->state, sizeof(out_status->state), "mount_required");
        snprintf(out_status->filesystem, sizeof(out_status->filesystem), "fat32");
        snprintf(out_status->note, sizeof(out_status->note), "MicroSD inserted, mount required");
        return ESP_OK;
    }

    out_status->filesystem_mounted = true;
    snprintf(out_status->filesystem, sizeof(out_status->filesystem), "fat32");

    uint64_t total_bytes = 0;
    uint64_t free_bytes = 0;
    sd_file_lock();
    if (esp_vfs_fat_info(WXM_SD_MOUNT_POINT, &total_bytes, &free_bytes) == ESP_OK) {
        out_status->capacity_kb = (uint32_t)(total_bytes / 1024ULL);
        out_status->free_kb = (uint32_t)(free_bytes / 1024ULL);
    }

    if (!s_tree_verified) {
        ensure_deskos_tree();
        s_tree_verified = true;
    }

    struct stat st = {0};
    int stat_res = stat(WXM_SD_MOUNT_POINT "/deskos", &st);
    sd_file_unlock();

    if (stat_res == 0 && S_ISDIR(st.st_mode)) {
        out_status->deskos_root_ready = true;
        out_status->data_ready = true;
        snprintf(out_status->state, sizeof(out_status->state), "ready");
        snprintf(out_status->note, sizeof(out_status->note), "MicroSD ready with DeskOS filesystem");
    } else {
        out_status->deskos_root_ready = false;
        out_status->data_ready = false;
        snprintf(out_status->state, sizeof(out_status->state), "ready_no_root");
        snprintf(out_status->note, sizeof(out_status->note), "MicroSD mounted but deskos/ directory missing");
    }

    return ESP_OK;
}

esp_err_t d1l_storage_wxm_sd_mount(d1l_rp2040_sd_status_t *out_status)
{
    if (!out_status) {
        return ESP_ERR_INVALID_ARG;
    }

    if (!check_card_present()) {
        s_mounted = false;
        s_tree_verified = false;
        return d1l_storage_wxm_sd_probe(out_status);
    }

    if (!s_mounted) {
        ESP_LOGI(TAG, "Mounting SD card via SDSPI on SPI3_HOST...");
        sd_file_lock();
        esp_err_t ret = bsp_sdcard_init_default();
        if (ret == ESP_OK) {
            s_mounted = true;
            ensure_deskos_tree();
            s_tree_verified = true;
        }
        sd_file_unlock();
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "Failed to mount SD card: %s", esp_err_to_name(ret));
            d1l_storage_wxm_sd_probe(out_status);
            out_status->mount_error = (uint32_t)ret;
            snprintf(out_status->state, sizeof(out_status->state), "error");
            snprintf(out_status->note, sizeof(out_status->note), "SDSPI mount failed: %s", esp_err_to_name(ret));
            return ret;
        }
        ESP_LOGI(TAG, "SD card mounted successfully at %s", WXM_SD_MOUNT_POINT);
    }

    return d1l_storage_wxm_sd_probe(out_status);
}

esp_err_t d1l_storage_wxm_sd_diag(d1l_rp2040_sd_diag_t *out_diag)
{
    if (!out_diag) {
        return ESP_ERR_INVALID_ARG;
    }
    memset(out_diag, 0, sizeof(*out_diag));
    out_diag->bridge_ready = true;
    out_diag->protocol_supported = true;
    out_diag->mount_selected = s_mounted;
    out_diag->high_shared_present = check_card_present();
    out_diag->spi_hz = 20000000U;
    snprintf(out_diag->selected_power, sizeof(out_diag->selected_power), "on");
    snprintf(out_diag->selected_mode, sizeof(out_diag->selected_mode), "direct_spi3");
    snprintf(out_diag->pins, sizeof(out_diag->pins), "CS=11 DET=12");
    snprintf(out_diag->note, sizeof(out_diag->note), s_mounted ? "mounted_spi3" : "unmounted");
    return ESP_OK;
}

esp_err_t d1l_storage_wxm_sd_file_stat(
    const char *path,
    d1l_rp2040_file_result_t *out_result)
{
    if (!path || !out_result) {
        return ESP_ERR_INVALID_ARG;
    }
    char full_path[128];
    if (!resolve_sd_path(path, full_path, sizeof(full_path))) {
        init_file_result(out_result, ESP_ERR_INVALID_ARG);
        return ESP_ERR_INVALID_ARG;
    }

    init_file_result(out_result, ESP_OK);
    snprintf(out_result->op, sizeof(out_result->op), "stat");

    struct stat st;
    sd_file_lock();
    int res = stat(full_path, &st);
    sd_file_unlock();

    if (res == 0) {
        out_result->exists = true;
        out_result->is_directory = S_ISDIR(st.st_mode);
        out_result->size = (uint32_t)st.st_size;
    } else {
        out_result->exists = false;
    }
    return ESP_OK;
}

esp_err_t d1l_storage_wxm_sd_file_read(
    const char *path,
    uint32_t offset,
    uint8_t *out_data,
    size_t max_len,
    d1l_rp2040_file_result_t *out_result)
{
    if (!path || !out_data || !out_result) {
        return ESP_ERR_INVALID_ARG;
    }
    char full_path[128];
    if (!resolve_sd_path(path, full_path, sizeof(full_path))) {
        init_file_result(out_result, ESP_ERR_INVALID_ARG);
        return ESP_ERR_INVALID_ARG;
    }

    init_file_result(out_result, ESP_OK);
    snprintf(out_result->op, sizeof(out_result->op), "get");
    out_result->offset = offset;

    sd_file_lock();
    FILE *f = fopen(full_path, "rb");
    if (!f) {
        sd_file_unlock();
        out_result->last_error = ESP_ERR_NOT_FOUND;
        out_result->ok = false;
        snprintf(out_result->err, sizeof(out_result->err), "not_found");
        return ESP_ERR_NOT_FOUND;
    }

    fseek(f, 0, SEEK_END);
    long file_size = ftell(f);
    if (fseek(f, offset, SEEK_SET) != 0) {
        fclose(f);
        sd_file_unlock();
        out_result->last_error = ESP_ERR_INVALID_SIZE;
        out_result->ok = false;
        snprintf(out_result->err, sizeof(out_result->err), "seek_failed");
        return ESP_ERR_INVALID_SIZE;
    }

    size_t bytes_read = fread(out_data, 1, max_len, f);
    bool eof = feof(f) || (bytes_read < max_len) || (file_size >= 0 && (offset + bytes_read) >= (size_t)file_size);
    fclose(f);
    sd_file_unlock();

    out_result->size = (uint32_t)(file_size > 0 ? file_size : 0);
    out_result->length = bytes_read;
    out_result->next_offset = offset + bytes_read;
    out_result->eof = eof;
    out_result->crc32 = calc_crc32(out_data, bytes_read);
    return ESP_OK;
}

esp_err_t d1l_storage_wxm_sd_file_write(
    const char *path,
    uint32_t offset,
    const uint8_t *data,
    size_t len,
    bool truncate,
    d1l_rp2040_file_result_t *out_result)
{
    if (!path || !out_result || (len > 0 && !data)) {
        return ESP_ERR_INVALID_ARG;
    }
    char full_path[128];
    if (!resolve_sd_path(path, full_path, sizeof(full_path))) {
        init_file_result(out_result, ESP_ERR_INVALID_ARG);
        return ESP_ERR_INVALID_ARG;
    }

    init_file_result(out_result, ESP_OK);
    snprintf(out_result->op, sizeof(out_result->op), "put");
    out_result->offset = offset;

    sd_file_lock();
    ensure_parent_dirs(full_path);
    FILE *f = NULL;
    if (truncate || offset == 0) {
        f = fopen(full_path, "wb");
    } else {
        f = fopen(full_path, "r+b");
        if (!f) {
            f = fopen(full_path, "wb");
        }
    }

    if (!f) {
        sd_file_unlock();
        out_result->last_error = ESP_FAIL;
        out_result->ok = false;
        snprintf(out_result->err, sizeof(out_result->err), "open_failed");
        return ESP_FAIL;
    }

    if (offset > 0) {
        fseek(f, offset, SEEK_SET);
    }

    size_t written = 0;
    if (len > 0) {
        written = fwrite(data, 1, len, f);
    }
    fflush(f);
    fclose(f);
    sd_file_unlock();

    out_result->length = written;
    out_result->size = offset + written;
    out_result->next_offset = offset + written;
    out_result->ok = (written == len);
    if (!out_result->ok) {
        out_result->last_error = ESP_FAIL;
        snprintf(out_result->err, sizeof(out_result->err), "short_write");
        return ESP_FAIL;
    }
    return ESP_OK;
}

esp_err_t d1l_storage_wxm_sd_file_write_verified(
    const char *path,
    const uint8_t *data,
    size_t len,
    uint32_t expected_crc32,
    d1l_rp2040_file_continue_cb_t should_continue,
    void *continue_context,
    d1l_rp2040_file_result_t *out_result)
{
    if (!path || !out_result || (len > 0 && !data)) {
        return ESP_ERR_INVALID_ARG;
    }

    const uint32_t actual_crc = calc_crc32(data, len);
    if (actual_crc != expected_crc32) {
        init_file_result(out_result, ESP_ERR_INVALID_CRC);
        snprintf(out_result->err, sizeof(out_result->err), "crc_mismatch");
        return ESP_ERR_INVALID_CRC;
    }

    if (should_continue && !should_continue(continue_context)) {
        init_file_result(out_result, ESP_ERR_INVALID_STATE);
        out_result->cancelled = true;
        return ESP_ERR_INVALID_STATE;
    }

    char tmp_path[136];
    char full_path[128];
    if (!resolve_sd_path(path, full_path, sizeof(full_path))) {
        init_file_result(out_result, ESP_ERR_INVALID_ARG);
        return ESP_ERR_INVALID_ARG;
    }
    snprintf(tmp_path, sizeof(tmp_path), "%s.tmp", full_path);

    sd_file_lock();
    ensure_parent_dirs(full_path);
    FILE *f = fopen(tmp_path, "wb");
    if (!f) {
        sd_file_unlock();
        init_file_result(out_result, ESP_FAIL);
        snprintf(out_result->err, sizeof(out_result->err), "open_failed");
        return ESP_FAIL;
    }

    size_t written = 0;
    if (len > 0) {
        written = fwrite(data, 1, len, f);
    }
    fflush(f);
    fclose(f);

    if (written != len) {
        unlink(tmp_path);
        sd_file_unlock();
        init_file_result(out_result, ESP_FAIL);
        snprintf(out_result->err, sizeof(out_result->err), "short_write");
        return ESP_FAIL;
    }

    /* Atomic commit */
    unlink(full_path);
    if (rename(tmp_path, full_path) != 0) {
        unlink(tmp_path);
        sd_file_unlock();
        init_file_result(out_result, ESP_FAIL);
        snprintf(out_result->err, sizeof(out_result->err), "commit_failed");
        return ESP_FAIL;
    }
    sd_file_unlock();

    init_file_result(out_result, ESP_OK);
    out_result->length = written;
    out_result->size = written;
    out_result->crc32 = actual_crc;
    snprintf(out_result->op, sizeof(out_result->op), "put_commit");
    return ESP_OK;
}

esp_err_t d1l_storage_wxm_sd_file_append(
    const char *path,
    const uint8_t *data,
    size_t len,
    d1l_rp2040_file_result_t *out_result)
{
    if (!path || !out_result || (len > 0 && !data)) {
        return ESP_ERR_INVALID_ARG;
    }
    char full_path[128];
    if (!resolve_sd_path(path, full_path, sizeof(full_path))) {
        init_file_result(out_result, ESP_ERR_INVALID_ARG);
        return ESP_ERR_INVALID_ARG;
    }

    init_file_result(out_result, ESP_OK);
    snprintf(out_result->op, sizeof(out_result->op), "append");

    sd_file_lock();
    ensure_parent_dirs(full_path);
    FILE *f = fopen(full_path, "ab");
    if (!f) {
        sd_file_unlock();
        out_result->last_error = ESP_FAIL;
        out_result->ok = false;
        snprintf(out_result->err, sizeof(out_result->err), "open_failed");
        return ESP_FAIL;
    }

    size_t written = 0;
    if (len > 0) {
        written = fwrite(data, 1, len, f);
    }
    fflush(f);
    long fsz = ftell(f);
    fclose(f);
    sd_file_unlock();

    out_result->length = written;
    out_result->size = (uint32_t)(fsz > 0 ? fsz : 0);
    out_result->next_offset = out_result->size;
    out_result->ok = (written == len);
    if (!out_result->ok) {
        out_result->last_error = ESP_FAIL;
        snprintf(out_result->err, sizeof(out_result->err), "short_write");
        return ESP_FAIL;
    }
    return ESP_OK;
}

esp_err_t d1l_storage_wxm_sd_file_delete(
    const char *path,
    d1l_rp2040_file_result_t *out_result)
{
    if (!path || !out_result) {
        return ESP_ERR_INVALID_ARG;
    }
    char full_path[128];
    if (!resolve_sd_path(path, full_path, sizeof(full_path))) {
        init_file_result(out_result, ESP_ERR_INVALID_ARG);
        return ESP_ERR_INVALID_ARG;
    }

    init_file_result(out_result, ESP_OK);
    snprintf(out_result->op, sizeof(out_result->op), "del");

    sd_file_lock();
    int res = unlink(full_path);
    sd_file_unlock();

    out_result->removed = (res == 0);
    out_result->removed_known = true;
    return ESP_OK;
}

esp_err_t d1l_storage_wxm_sd_file_rename(
    const char *from_path,
    const char *to_path,
    bool replace,
    d1l_rp2040_file_result_t *out_result)
{
    if (!from_path || !to_path || !out_result) {
        return ESP_ERR_INVALID_ARG;
    }
    char full_from[128];
    char full_to[128];
    if (!resolve_sd_path(from_path, full_from, sizeof(full_from)) ||
        !resolve_sd_path(to_path, full_to, sizeof(full_to))) {
        init_file_result(out_result, ESP_ERR_INVALID_ARG);
        return ESP_ERR_INVALID_ARG;
    }

    init_file_result(out_result, ESP_OK);
    snprintf(out_result->op, sizeof(out_result->op), "rename");

    sd_file_lock();
    ensure_parent_dirs(full_to);
    if (replace) {
        unlink(full_to);
    }
    int res = rename(full_from, full_to);
    sd_file_unlock();

    out_result->ok = (res == 0);
    if (res != 0) {
        out_result->last_error = ESP_FAIL;
        snprintf(out_result->err, sizeof(out_result->err), "rename_failed");
        return ESP_FAIL;
    }
    return ESP_OK;
}

esp_err_t d1l_storage_wxm_sd_ping(d1l_rp2040_ping_t *out_ping)
{
    if (!out_ping) {
        return ESP_ERR_INVALID_ARG;
    }
    memset(out_ping, 0, sizeof(*out_ping));
    out_ping->bridge_ready = true;
    out_ping->protocol_supported = true;
    out_ping->protocol_version = 1;
    out_ping->atomic_rename_supported = true;
    out_ping->stream_write_supported = true;
    out_ping->file_line_max = D1L_RP2040_FILE_LINE_MAX;
    out_ping->file_chunk_max = 512;
    out_ping->path_max = 128;
    return ESP_OK;
}
