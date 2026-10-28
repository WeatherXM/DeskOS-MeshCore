#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "update/wg1200_boot_policy.h"
#include "update/wg1200_signature.h"

/* argv[1]: one espsecure signature block; argv[2]: the signed data's
 * SHA-256 followed by the SHA-256 of the signing key. */
static void read_file(const char *path, uint8_t *out, size_t length)
{
    FILE *file = fopen(path, "rb");
    assert(file);
    assert(fread(out, 1U, length, file) == length);
    fclose(file);
}

int main(int argc, char **argv)
{
    assert(argc == 3);
    static uint8_t sector[D1L_WG1200_SIGNATURE_SECTOR_BYTES];
    memset(sector, 0xFF, sizeof(sector));
    read_file(argv[1], sector, D1L_WG1200_SIGNATURE_BLOCK_BYTES);
    uint8_t digests[2][D1L_WG1200_SHA256_BYTES];
    read_file(argv[2], &digests[0][0], sizeof(digests));
    const uint8_t *image_digest = digests[0];
    const uint8_t (*trusted)[D1L_WG1200_SHA256_BYTES] = &digests[1];
    uint8_t other[2][D1L_WG1200_SHA256_BYTES];
    memset(other, 0x5A, sizeof(other));

    assert(d1l_wg1200_signature_sector_trusted(sector, sizeof(sector),
                                               trusted, 1U));
    assert(d1l_wg1200_signature_sector_verifies(sector, sizeof(sector),
                                                image_digest, trusted, 1U));
    /* Any one of the eFuse digests may match. */
    memcpy(other[1], trusted[0], D1L_WG1200_SHA256_BYTES);
    assert(d1l_wg1200_signature_sector_verifies(sector, sizeof(sector),
                                                image_digest, other, 2U));
    assert(!d1l_wg1200_signature_sector_verifies(sector, sizeof(sector),
                                                 image_digest, other, 1U));
    assert(!d1l_wg1200_signature_sector_trusted(sector, sizeof(sector),
                                                other, 1U));
    assert(!d1l_wg1200_signature_sector_verifies(sector, sizeof(sector),
                                                 image_digest, trusted, 0U));

    uint8_t changed[D1L_WG1200_SHA256_BYTES];
    memcpy(changed, image_digest, sizeof(changed));
    changed[31] ^= 1U;
    assert(!d1l_wg1200_signature_sector_verifies(sector, sizeof(sector),
                                                 changed, trusted, 1U));

    /* Re-binding the block to another image (with a fixed-up CRC) must still
     * fail the RSA check. */
    memcpy(&sector[D1L_WG1200_SIGNATURE_DIGEST_OFFSET], changed,
           sizeof(changed));
    const uint32_t crc = d1l_wg1200_crc32(0U, sector, 1196U);
    for (unsigned i = 0U; i < 4U; ++i) {
        sector[1196U + i] = (uint8_t)(crc >> (8U * i));
    }
    assert(d1l_wg1200_signature_block_valid(sector, sizeof(sector)));
    assert(d1l_wg1200_signature_sector_trusted(sector, sizeof(sector),
                                               trusted, 1U));
    assert(!d1l_wg1200_signature_sector_verifies(sector, sizeof(sector),
                                                 changed, trusted, 1U));

    memset(sector, 0xFF, sizeof(sector));
    assert(!d1l_wg1200_signature_sector_trusted(sector, sizeof(sector),
                                                trusted, 1U));
    puts("wg1200 signature ok");
    return 0;
}
