/* Shared-library wrapper around fphelper's LZMA decoder (lzmadec.h).
 * sprd = 1 selects the Spreadtrum variant, which decodes every literal
 * as a plain literal (no matched-literal mode). Returns 0 on success;
 * src_size and dst_size are updated to the bytes consumed and produced.
 *
 * build: make (lzmadec.h is vendored in third_party/)
 */
#include <stddef.h>
#include <stdint.h>

static int lzma_sprd;
#define LZMA_SPRD_HACK lzma_sprd
#include "lzmadec.h"

uint32_t lzma_decode(const uint8_t *src, size_t *src_size,
		uint8_t *dst, size_t *dst_size, int sprd) {
	lzma_sprd = sprd;
	return decode_lzma(src, src_size, dst, dst_size);
}
