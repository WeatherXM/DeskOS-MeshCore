#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "esp_partition.h"

/* WeatherXM WG1200 dual boot: DeskOS shares the stock partition table and
 * bootloader with WeatherXM firmware. The stock bootloader has no app
 * rollback and, on production units, only runs apps signed for Secure Boot.
 * On other boards every call is a no-op that keeps the D1L behaviour. */

/* Call right after the GPIO10 restart guard. Counts consecutive crash resets
 * in RTC memory and restarts into WeatherXM firmware after
 * D1L_WG1200_CRASH_FALLBACK_THRESHOLD in a row. */
void d1l_wg1200_boot_guard_init(void);
/* Call when startup has finished; a minute later the crash count clears. */
void d1l_wg1200_boot_guard_mark_started(void);

bool d1l_wg1200_boot_weatherxm_available(void);
/* Splash prompt: a fresh USER button press in the first seconds restarts
 * into WeatherXM firmware. Returns when DeskOS should keep starting. */
void d1l_wg1200_boot_menu_run(void);
/* Settings: quiesce DeskOS, select the WeatherXM slot and restart. Returns
 * only on failure. */
esp_err_t d1l_wg1200_boot_switch_to_weatherxm(void);

/* True when the bootloader never moves a new image to PENDING_VERIFY. */
bool d1l_wg1200_boot_stock_bootloader(void);
/* Replaces *target with an OTA slot that is neither running nor holds
 * WeatherXM firmware, or refuses with ESP_ERR_NOT_ALLOWED. */
esp_err_t d1l_wg1200_boot_select_update_target(const esp_partition_t **target);
/* Before any flash write: on Secure Boot units the update file must end with
 * a signature sector made with a key this unit trusts. */
esp_err_t d1l_wg1200_boot_check_update_file(const char *path,
                                            uint32_t image_size);
/* After the write: verifies the installed image the way the bootloader will. */
esp_err_t d1l_wg1200_boot_verify_app_signature(const esp_partition_t *partition,
                                               uint32_t image_size);
/* For an update that fails its boot checks: the bootloader cannot roll back,
 * so select the DeskOS it replaced (else WeatherXM firmware) and restart.
 * Returns only on failure. */
esp_err_t d1l_wg1200_boot_roll_back(const char *reason);
