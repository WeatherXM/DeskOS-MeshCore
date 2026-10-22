#pragma once

#include "esp_err.h"

/* WG1200 field units run a stock bootloader that treats GPIO10 held low for
 * 5 s as a factory-reset request (erases nvs and otadata). GPIO10 is also RGB
 * LCD data G0, which can sit low across a reset. Keep it high through
 * restarts and panics. No-op on other boards. Call before the LCD starts. */
esp_err_t d1l_wg1200_restart_guard_init(void);
