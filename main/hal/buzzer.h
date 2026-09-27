#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* On WG1200 schematic, net BUZZER is PCA9535 pin 18 (IO1_5 = expander pin 13), marked DNP.
 * Expander pin 9 is ECC_PWR_SW. */
#define EXPANDER_IO_BUZZER 13

esp_err_t d1l_buzzer_init(void);
void d1l_buzzer_beep(uint32_t duration_ms);

#ifdef __cplusplus
}
#endif
