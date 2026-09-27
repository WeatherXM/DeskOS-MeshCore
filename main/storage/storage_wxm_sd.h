#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "hal/rp2040_bridge.h"

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t d1l_storage_wxm_sd_probe(d1l_rp2040_sd_status_t *out_status);
esp_err_t d1l_storage_wxm_sd_mount(d1l_rp2040_sd_status_t *out_status);
esp_err_t d1l_storage_wxm_sd_diag(d1l_rp2040_sd_diag_t *out_diag);

esp_err_t d1l_storage_wxm_sd_file_stat(
    const char *path,
    d1l_rp2040_file_result_t *out_result);

esp_err_t d1l_storage_wxm_sd_file_read(
    const char *path,
    uint32_t offset,
    uint8_t *out_data,
    size_t max_len,
    d1l_rp2040_file_result_t *out_result);

esp_err_t d1l_storage_wxm_sd_file_write(
    const char *path,
    uint32_t offset,
    const uint8_t *data,
    size_t len,
    bool truncate,
    d1l_rp2040_file_result_t *out_result);

esp_err_t d1l_storage_wxm_sd_file_write_verified(
    const char *path,
    const uint8_t *data,
    size_t len,
    uint32_t expected_crc32,
    d1l_rp2040_file_continue_cb_t should_continue,
    void *continue_context,
    d1l_rp2040_file_result_t *out_result);

esp_err_t d1l_storage_wxm_sd_file_append(
    const char *path,
    const uint8_t *data,
    size_t len,
    d1l_rp2040_file_result_t *out_result);

esp_err_t d1l_storage_wxm_sd_file_delete(
    const char *path,
    d1l_rp2040_file_result_t *out_result);

esp_err_t d1l_storage_wxm_sd_file_rename(
    const char *from_path,
    const char *to_path,
    bool replace,
    d1l_rp2040_file_result_t *out_result);

esp_err_t d1l_storage_wxm_sd_ping(d1l_rp2040_ping_t *out_ping);

#ifdef __cplusplus
}
#endif
