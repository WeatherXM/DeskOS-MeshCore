#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    bool present;
    float pressure_hpa;
    float temperature_c;
    int64_t last_read_time_us;
} d1l_bmp390_reading_t;

/**
 * @brief Initialize the BMP390 barometric pressure and temperature sensor.
 */
esp_err_t d1l_bmp390_init(void);

/**
 * @brief Read fresh pressure and temperature from the BMP390 sensor.
 * @param out_pressure_hpa Pressure in hPa
 * @param out_temperature_c Temperature in degrees Celsius
 */
esp_err_t d1l_bmp390_read(float *out_pressure_hpa, float *out_temperature_c);

/**
 * @brief Get the latest cached sensor reading.
 * @return true if valid reading is available, false otherwise.
 */
bool d1l_bmp390_get_latest(d1l_bmp390_reading_t *out_reading);

#ifdef __cplusplus
}
#endif
