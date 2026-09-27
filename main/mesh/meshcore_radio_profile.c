#include "meshcore_radio_profile.h"

#include <string.h>

#include "d1l_config.h"

static const d1l_radio_profile_t s_eu = {
    .profile_id = D1L_RADIO_EU_PROFILE_ID,
    .region_label = D1L_RADIO_EU_REGION_LABEL,
    .frequency_hz = D1L_RADIO_EU_FREQ_HZ,
    .bandwidth_khz = D1L_RADIO_EU_BW_KHZ,
    .spreading_factor = D1L_RADIO_EU_SF,
    .coding_rate = D1L_RADIO_EU_CR,
    .tx_power_dbm = D1L_RADIO_EU_TX_POWER_DBM,
    .tcxo = D1L_RADIO_TCXO,
    .rx_boost = true,
};

static const d1l_radio_profile_t s_uscan = {
    .profile_id = D1L_RADIO_USCAN_PROFILE_ID,
    .region_label = D1L_RADIO_USCAN_REGION_LABEL,
    .frequency_hz = D1L_RADIO_USCAN_FREQ_HZ,
    .bandwidth_khz = D1L_RADIO_USCAN_BW_KHZ,
    .spreading_factor = D1L_RADIO_USCAN_SF,
    .coding_rate = D1L_RADIO_USCAN_CR,
    .tx_power_dbm = D1L_RADIO_USCAN_TX_POWER_DBM,
    .tcxo = D1L_RADIO_TCXO,
    .rx_boost = true,
};

const d1l_radio_profile_t *d1l_radio_profile_default(void)
{
    return &s_eu;
}

const d1l_radio_profile_t *d1l_radio_profile_eu_default(void)
{
    return &s_eu;
}

const d1l_radio_profile_t *d1l_radio_profile_uscan_default(void)
{
    return &s_uscan;
}

bool d1l_radio_profile_is_safe_eu(const d1l_radio_profile_t *profile)
{
    if (!profile) {
        return false;
    }
    return profile->frequency_hz >= 863000000UL &&
           profile->frequency_hz <= 870000000UL &&
           profile->spreading_factor >= 5 &&
           profile->spreading_factor <= 12 &&
           profile->coding_rate >= 5 &&
           profile->coding_rate <= 8 &&
           profile->tx_power_dbm <= D1L_RADIO_EU_TX_POWER_DBM &&
           strcmp(profile->tcxo, "NONE") == 0;
}

bool d1l_radio_profile_is_safe_uscan(const d1l_radio_profile_t *profile)
{
    if (!profile) {
        return false;
    }
    return profile->frequency_hz >= 902000000UL &&
           profile->frequency_hz <= 928000000UL &&
           profile->spreading_factor >= 5 &&
           profile->spreading_factor <= 12 &&
           profile->coding_rate >= 5 &&
           profile->coding_rate <= 8 &&
           profile->tx_power_dbm <= D1L_RADIO_USCAN_TX_POWER_DBM &&
           strcmp(profile->tcxo, "NONE") == 0;
}

bool d1l_radio_profile_is_safe(const d1l_radio_profile_t *profile)
{
    return d1l_radio_profile_is_safe_eu(profile) || d1l_radio_profile_is_safe_uscan(profile);
}
