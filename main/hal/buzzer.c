#include "buzzer.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "tca9535.h"

static const char *TAG = "d1l_buzzer";

/* Note: On WeatherXM WG1200, the buzzer circuit (B1 / Q13) is DNP (Do Not Populate)
 * on IO expander pin 13 (IO1_5). Expander pin 9 is ECC_PWR_SW, not buzzer.
 * To protect hardware lines, buzzer operations safely no-op when not populated. */

esp_err_t d1l_buzzer_init(void)
{
#if defined(CONFIG_LCD_BOARD_SENSECAP_INDICATOR_WXM) || !defined(EXPANDER_IO_BUZZER)
    ESP_LOGI(TAG, "Buzzer not populated on this hardware revision");
    return ESP_ERR_NOT_SUPPORTED;
#else
    esp_err_t ret = tca9535_set_direction(EXPANDER_IO_BUZZER, true); /* output */
    if (ret == ESP_OK) {
        ret = tca9535_set_level(EXPANDER_IO_BUZZER, false); /* silent */
    }
    ESP_LOGI(TAG, "Buzzer initialized on IO expander pin %d (ret: %s)",
             EXPANDER_IO_BUZZER, esp_err_to_name(ret));
    return ret;
#endif
}

#if !defined(CONFIG_LCD_BOARD_SENSECAP_INDICATOR_WXM) && defined(EXPANDER_IO_BUZZER)
static void buzzer_beep_task(void *pvParameters)
{
    uint32_t ms = (uint32_t)(uintptr_t)pvParameters;
    tca9535_set_level(EXPANDER_IO_BUZZER, true);
    vTaskDelay(pdMS_TO_TICKS(ms > 0 ? ms : 50));
    tca9535_set_level(EXPANDER_IO_BUZZER, false);
    vTaskDelete(NULL);
}
#endif

void d1l_buzzer_beep(uint32_t duration_ms)
{
#if defined(CONFIG_LCD_BOARD_SENSECAP_INDICATOR_WXM) || !defined(EXPANDER_IO_BUZZER)
    (void)duration_ms;
    return;
#else
    if (duration_ms == 0) duration_ms = 50;
    if (duration_ms > 2000) duration_ms = 2000;
    xTaskCreate(buzzer_beep_task, "buzzer_beep", 2048, (void *)(uintptr_t)duration_ms, 5, NULL);
#endif
}
