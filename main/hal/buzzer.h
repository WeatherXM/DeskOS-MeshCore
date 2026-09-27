#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define EXPANDER_IO_BUZZER 9

esp_err_t d1l_buzzer_init(void);
void d1l_buzzer_beep(uint32_t duration_ms);

#ifdef __cplusplus
}
#endif
