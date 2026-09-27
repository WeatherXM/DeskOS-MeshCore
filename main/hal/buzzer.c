#include "buzzer.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "tca9535.h"

static const char *TAG = "d1l_buzzer";

esp_err_t d1l_buzzer_init(void)
{
    esp_err_t ret = tca9535_set_direction(EXPANDER_IO_BUZZER, true); /* output */
    if (ret == ESP_OK) {
        ret = tca9535_set_level(EXPANDER_IO_BUZZER, false); /* silent */
    }
    ESP_LOGI(TAG, "Buzzer initialized on IO expander pin %d (ret: %s)",
             EXPANDER_IO_BUZZER, esp_err_to_name(ret));
    return ret;
}

static void buzzer_beep_task(void *pvParameters)
{
    uint32_t ms = (uint32_t)(uintptr_t)pvParameters;
    tca9535_set_level(EXPANDER_IO_BUZZER, true);
    vTaskDelay(pdMS_TO_TICKS(ms > 0 ? ms : 50));
    tca9535_set_level(EXPANDER_IO_BUZZER, false);
    vTaskDelete(NULL);
}

void d1l_buzzer_beep(uint32_t duration_ms)
{
    if (duration_ms == 0) duration_ms = 50;
    if (duration_ms > 2000) duration_ms = 2000;
    xTaskCreate(buzzer_beep_task, "buzzer_beep", 2048, (void *)(uintptr_t)duration_ms, 5, NULL);
}
