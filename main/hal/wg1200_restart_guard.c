#include "hal/wg1200_restart_guard.h"

#include "sdkconfig.h"

#if CONFIG_LCD_BOARD_SENSECAP_INDICATOR_WXM

#include <stdbool.h>

#include "driver/gpio.h"
#include "esp_attr.h"
#include "esp_log.h"
#include "esp_rom_gpio.h"
#include "esp_system.h"
#include "hal/gpio_ll.h"
#include "soc/gpio_sig_map.h"
#include "soc/rtc_cntl_reg.h"
#include "soc/soc.h"

#define WG1200_FACTORY_RESET_GPIO GPIO_NUM_10

static const char *TAG = "wg1200_restart";

static void hold_factory_reset_pin_high(void)
{
    /* Detach G0 from the RGB peripheral, drive it high with input sampling
     * enabled, and hold the pad through the reset. The bootloader's own pad
     * setup is blocked by the hold, so it reads the held high level. */
    (void)gpio_reset_pin(WG1200_FACTORY_RESET_GPIO);
    (void)gpio_set_direction(WG1200_FACTORY_RESET_GPIO,
                             GPIO_MODE_INPUT_OUTPUT);
    (void)gpio_set_level(WG1200_FACTORY_RESET_GPIO, 1);
    (void)gpio_hold_en(WG1200_FACTORY_RESET_GPIO);
}

/* Linked with -Wl,--wrap=esp_panic_handler (main/CMakeLists.txt). Panics skip
 * shutdown handlers, so this runs in the panic context: ROM and register
 * access only. GPIO10 is an RTC pad on ESP32-S3, so its hold bit lives in
 * RTC_CNTL_PAD_HOLD_REG; GPIO_HOLD_MASK has no entry for it. */
void __real_esp_panic_handler(void *info);
void __wrap_esp_panic_handler(void *info);

void IRAM_ATTR __wrap_esp_panic_handler(void *info)
{
    esp_rom_gpio_connect_out_signal(WG1200_FACTORY_RESET_GPIO,
                                    SIG_GPIO_OUT_IDX, false, false);
    gpio_ll_input_enable(&GPIO, WG1200_FACTORY_RESET_GPIO);
    gpio_ll_output_enable(&GPIO, WG1200_FACTORY_RESET_GPIO);
    gpio_ll_set_level(&GPIO, WG1200_FACTORY_RESET_GPIO, 1);
    SET_PERI_REG_MASK(RTC_CNTL_PAD_HOLD_REG, RTC_CNTL_TOUCH_PAD10_HOLD_M);
    __real_esp_panic_handler(info);
}

esp_err_t d1l_wg1200_restart_guard_init(void)
{
    /* A guarded restart leaves the pad held high. Drive high before releasing
     * the hold so G0 cannot glitch low; LCD init takes the pin over later. */
    (void)gpio_set_direction(WG1200_FACTORY_RESET_GPIO,
                             GPIO_MODE_INPUT_OUTPUT);
    (void)gpio_set_level(WG1200_FACTORY_RESET_GPIO, 1);
    (void)gpio_hold_dis(WG1200_FACTORY_RESET_GPIO);

    const esp_err_t ret =
        esp_register_shutdown_handler(hold_factory_reset_pin_high);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "GPIO10 restart guard not registered: %s",
                 esp_err_to_name(ret));
    }
    return ret;
}

#else

esp_err_t d1l_wg1200_restart_guard_init(void)
{
    return ESP_OK;
}

#endif
