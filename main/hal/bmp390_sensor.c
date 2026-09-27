#include "bmp390_sensor.h"

#include <string.h>
#include "esp_log.h"
#include "esp_timer.h"
#include "bmp3xx.h"

#define BMP390_I2C_ADDR 0x77

static const char *TAG = "d1l_bmp390";
static bool s_initialized = false;
static bool s_present = false;
static d1l_bmp390_reading_t s_latest = {0};

esp_err_t d1l_bmp390_init(void)
{
    float p = 0.0f, t = 0.0f;
    esp_err_t ret = bmp3xx_init(BMP390_I2C_ADDR);
    esp_err_t read_ret = bmp3xx_read_data(&p, &t);
    if (ret == ESP_OK || read_ret == ESP_OK) {
        s_present = true;
        s_initialized = true;
        s_latest.present = true;
        s_latest.pressure_hpa = p / 100.0f;
        s_latest.temperature_c = t;
        s_latest.last_read_time_us = esp_timer_get_time();
        ESP_LOGI(TAG, "BMP390 sensor ready at 0x%02x (P=%.2f hPa, T=%.2f C)",
                 BMP390_I2C_ADDR, s_latest.pressure_hpa, s_latest.temperature_c);
        return ESP_OK;
    }
    s_present = false;
    s_initialized = false;
    ESP_LOGW(TAG, "BMP390 sensor not detected at 0x%02x", BMP390_I2C_ADDR);
    return ESP_FAIL;
}

esp_err_t d1l_bmp390_read(float *out_pressure_hpa, float *out_temperature_c)
{
    if (!s_initialized || !s_present) {
        return ESP_ERR_INVALID_STATE;
    }

    float pressure_pa = 0.0f;
    float temp_c = 0.0f;
    esp_err_t ret = bmp3xx_read_data(&pressure_pa, &temp_c);
    if (ret == ESP_OK) {
        float hpa = pressure_pa / 100.0f;
        if (out_pressure_hpa) {
            *out_pressure_hpa = hpa;
        }
        if (out_temperature_c) {
            *out_temperature_c = temp_c;
        }
        s_latest.present = true;
        s_latest.pressure_hpa = hpa;
        s_latest.temperature_c = temp_c;
        s_latest.last_read_time_us = esp_timer_get_time();
    }
    return ret;
}

bool d1l_bmp390_get_latest(d1l_bmp390_reading_t *out_reading)
{
    if (!out_reading || !s_latest.present) {
        return false;
    }
    *out_reading = s_latest;
    return true;
}
