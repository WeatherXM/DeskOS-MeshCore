#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define D1L_WG1200_SHA256_BYTES 32U

/* True when a block of a Secure Boot V2 signature sector carries a key whose
 * SHA-256 is in trusted_digests (the eFuse key digests). */
bool d1l_wg1200_signature_sector_trusted(
    const uint8_t *sector, size_t length,
    const uint8_t (*trusted_digests)[D1L_WG1200_SHA256_BYTES],
    size_t trusted_count);

/* As above, and that block is bound to image_digest and carries a valid
 * RSA-PSS signature over it: what the bootloader checks before running an
 * app. image_digest is the SHA-256 of everything before the sector. */
bool d1l_wg1200_signature_sector_verifies(
    const uint8_t *sector, size_t length,
    const uint8_t image_digest[D1L_WG1200_SHA256_BYTES],
    const uint8_t (*trusted_digests)[D1L_WG1200_SHA256_BYTES],
    size_t trusted_count);
