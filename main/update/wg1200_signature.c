#include "update/wg1200_signature.h"

#include <stdlib.h>
#include <string.h>

#include "mbedtls/rsa.h"
#include "mbedtls/sha256.h"
#include "update/wg1200_boot_policy.h"

#define SIGNATURE_BLOCKS 3U
#define RSA_BYTES 384U
#define KEY_EXPONENT_OFFSET RSA_BYTES
#define SIGNATURE_OFFSET \
    (D1L_WG1200_SIGNATURE_KEY_OFFSET + D1L_WG1200_SIGNATURE_KEY_BYTES)

static bool key_trusted(const uint8_t *block,
                        const uint8_t (*trusted_digests)[D1L_WG1200_SHA256_BYTES],
                        size_t trusted_count)
{
    uint8_t digest[D1L_WG1200_SHA256_BYTES] = {0};
    if (mbedtls_sha256(&block[D1L_WG1200_SIGNATURE_KEY_OFFSET],
                       D1L_WG1200_SIGNATURE_KEY_BYTES, digest, 0) != 0) {
        return false;
    }
    for (size_t i = 0U; i < trusted_count; ++i) {
        if (memcmp(trusted_digests[i], digest, sizeof(digest)) == 0) {
            return true;
        }
    }
    return false;
}

static bool rsa_pss_verifies(const uint8_t *block,
                             const uint8_t image_digest[D1L_WG1200_SHA256_BYTES])
{
    /* The block stores the modulus, exponent and signature little-endian;
     * mbedtls takes them big-endian. */
    const uint8_t *key = &block[D1L_WG1200_SIGNATURE_KEY_OFFSET];
    const uint8_t *signature = &block[SIGNATURE_OFFSET];
    uint8_t *modulus = malloc(RSA_BYTES);
    uint8_t *signature_be = malloc(RSA_BYTES);
    if (!modulus || !signature_be) {
        free(modulus);
        free(signature_be);
        return false;
    }
    for (size_t i = 0U; i < RSA_BYTES; ++i) {
        modulus[i] = key[RSA_BYTES - 1U - i];
        signature_be[i] = signature[RSA_BYTES - 1U - i];
    }
    const uint8_t exponent[4] = {
        key[KEY_EXPONENT_OFFSET + 3U], key[KEY_EXPONENT_OFFSET + 2U],
        key[KEY_EXPONENT_OFFSET + 1U], key[KEY_EXPONENT_OFFSET],
    };
    mbedtls_rsa_context rsa;
    mbedtls_rsa_init(&rsa);
    int ret = mbedtls_rsa_set_padding(&rsa, MBEDTLS_RSA_PKCS_V21,
                                      MBEDTLS_MD_SHA256);
    if (ret == 0) {
        ret = mbedtls_rsa_import_raw(&rsa, modulus, RSA_BYTES, NULL, 0U,
                                     NULL, 0U, NULL, 0U,
                                     exponent, sizeof(exponent));
    }
    if (ret == 0) {
        ret = mbedtls_rsa_complete(&rsa);
    }
    if (ret == 0) {
        ret = mbedtls_rsa_rsassa_pss_verify(&rsa, MBEDTLS_MD_SHA256,
                                            D1L_WG1200_SHA256_BYTES,
                                            image_digest, signature_be);
    }
    mbedtls_rsa_free(&rsa);
    free(modulus);
    free(signature_be);
    return ret == 0;
}

bool d1l_wg1200_signature_sector_trusted(
    const uint8_t *sector, size_t length,
    const uint8_t (*trusted_digests)[D1L_WG1200_SHA256_BYTES],
    size_t trusted_count)
{
    if (!sector || length < D1L_WG1200_SIGNATURE_SECTOR_BYTES ||
        !trusted_digests || trusted_count == 0U) {
        return false;
    }
    for (size_t i = 0U; i < SIGNATURE_BLOCKS; ++i) {
        const uint8_t *block = &sector[i * D1L_WG1200_SIGNATURE_BLOCK_BYTES];
        if (d1l_wg1200_signature_block_valid(
                block, D1L_WG1200_SIGNATURE_BLOCK_BYTES) &&
            key_trusted(block, trusted_digests, trusted_count)) {
            return true;
        }
    }
    return false;
}

bool d1l_wg1200_signature_sector_verifies(
    const uint8_t *sector, size_t length,
    const uint8_t image_digest[D1L_WG1200_SHA256_BYTES],
    const uint8_t (*trusted_digests)[D1L_WG1200_SHA256_BYTES],
    size_t trusted_count)
{
    if (!sector || length < D1L_WG1200_SIGNATURE_SECTOR_BYTES ||
        !image_digest || !trusted_digests || trusted_count == 0U) {
        return false;
    }
    for (size_t i = 0U; i < SIGNATURE_BLOCKS; ++i) {
        const uint8_t *block = &sector[i * D1L_WG1200_SIGNATURE_BLOCK_BYTES];
        if (d1l_wg1200_signature_block_valid(
                block, D1L_WG1200_SIGNATURE_BLOCK_BYTES) &&
            memcmp(&block[D1L_WG1200_SIGNATURE_DIGEST_OFFSET], image_digest,
                   D1L_WG1200_SHA256_BYTES) == 0 &&
            key_trusted(block, trusted_digests, trusted_count) &&
            rsa_pss_verifies(block, image_digest)) {
            return true;
        }
    }
    return false;
}
