/**
 * @file    bad_decode.cpp
 * @brief   BadCodec v0.5.1 - Decoder implementation
 * @version 0.5.1  (Protocol: 514)
 * @date    2026-03-15
 * @license Non-Commercial Use Only  ghostinkoma@gmail.com
 *
 * Design constraints:
 *   - NO malloc / free
 *   - NO floating point
 *   - NO division
 *   - Stack usage: max ~32 bytes per function (AVR safe)
 *   - <stdint.h> only
 *
 * Changelog:
 *   rev.16: DELTA_FRAME (0x3D) decode added
 *   rev.18: FOR min repeat=4 documented (decoder unchanged)
 *   rev.19: FOR min repeat changed 4->3 (0xC1 now valid); constants updated
 */

#include "bad_decode.h"
#include <string.h>

/* ============================================================
 * Internal: memset / memcpy  (no <string.h>)
 * ============================================================ */

static void bad_memset(uint8_t *dst, uint8_t val, uint16_t len)
{
    while (len--) { *dst++ = val; }
}

static void bad_memcpy(uint8_t *dst, const uint8_t *src, uint16_t len)
{
    memcpy(dst, src, len);  /* [AutoKeyMouse 高速化] 1 バイトずつのループを標準関数に */
}

/* ============================================================
 * Internal: Fletcher-16  (SPEC.md 3-3)
 * ============================================================ */

static uint16_t fletcher16(const uint8_t *data, uint8_t len)
{
    uint8_t s1 = 0U, s2 = 0U;
    while (len--) {
        s1 = (uint8_t)(s1 + *data++);
        s2 = (uint8_t)(s2 + s1);
    }
    return (uint16_t)(((uint16_t)s2 << 8U) | s1);
}

/* ============================================================
 * Internal: stream read helpers
 * ============================================================ */

static uint8_t bad_read1(bad_ctx_t *ctx)
{
    uint8_t b = 0xFFU;
    ctx->read(ctx->stream_offset, &b, 1U);
    ctx->stream_offset++;
    return b;
}

static uint16_t bad_readn(bad_ctx_t *ctx, uint8_t *buf, uint16_t len)
{
    uint16_t n = ctx->read(ctx->stream_offset, buf, len);
    ctx->stream_offset += n;
    return n;
}

/* ============================================================
 * Internal: gram bit helpers
 * bit_idx = y * width + x  (row-major, LE)
 * ============================================================ */

BAD_STATIC_INLINE uint8_t gp_get(const uint8_t *buf, uint16_t idx)
{
    return (buf[idx >> 3U] >> (idx & 7U)) & 1U;
}

BAD_STATIC_INLINE void gp_set(uint8_t *buf, uint16_t idx, uint8_t val)
{
    if (val) buf[idx>>3U] |=  (uint8_t)(1U<<(idx&7U));
    else     buf[idx>>3U] &= ~(uint8_t)(1U<<(idx&7U));
}

/* ============================================================
 * Internal: block coordinate helpers
 * ============================================================ */

BAD_STATIC_INLINE uint8_t  blk_nx(const bad_ctx_t *ctx)
{ return (uint8_t)(ctx->width  >> 3U); }

BAD_STATIC_INLINE uint8_t  blk_ny(const bad_ctx_t *ctx)
{ return (uint8_t)(ctx->height >> 3U); }

BAD_STATIC_INLINE uint16_t blk_bit(const bad_ctx_t *ctx,
                                    uint8_t bx, uint8_t by,
                                    uint8_t px, uint8_t py)
{
    return (uint16_t)(((uint16_t)(by<<3U)+py)*ctx->width
                    + ((uint16_t)(bx<<3U)+px));
}

/* [AutoKeyMouse 高速化] 幅は 8 の倍数・ブロックは 8 画素境界なので、ブロックの 1 行は gram の 1 バイト
 * (bit c = 列 c, LSB-first)。ブロック単位の命令はビットごとではなくバイトで読み書きする (結果は同じ)。 */
BAD_STATIC_INLINE uint16_t blk_row(const bad_ctx_t *ctx, uint8_t bx, uint8_t by, uint8_t py)
{
    return (uint16_t)((((uint16_t)(by<<3U)+py)*ctx->width >> 3U) + bx);
}

/* ============================================================
 * Internal: scan-path RLE apply
 * Places runs[] into gram block (bx,by) using scan pattern p_idx.
 * p_idx bits: bit2=scan_dir  bit1=start_y  bit0=start_x
 * ============================================================ */

static void apply_rle_runs(bad_ctx_t *ctx,
                            uint8_t bx, uint8_t by,
                            uint8_t p_idx, uint8_t start_color,
                            const uint8_t *runs, uint8_t n_runs)
{
    uint8_t scan_dir = (p_idx >> 2U) & 1U;
    uint8_t sx       = (p_idx & 1U) ? 7U : 0U;
    uint8_t sy       = (p_idx & 2U) ? 7U : 0U;
    int8_t  dx       = (p_idx & 1U) ? -1 : 1;
    int8_t  dy       = (p_idx & 2U) ? -1 : 1;

    uint8_t color   = start_color;
    uint8_t ri      = 0U;
    uint8_t remain  = (n_runs > 0U) ? runs[0] : 0U;

    /* [AutoKeyMouse 修正] 長さ 0 のランを飛ばす。RLE_BLOCK_8 は開始色が固定なので、
     * 先頭画素が開始色と違うと runs[0]=0 になる (Codec.py のエンコーダがそう出力する)。
     * 元の実装では 0 のランでも 1 画素描いてしまい、ブロックが 1 画素ずれていた。 */
    while (remain == 0U && ri + 1U < n_runs) {
        color ^= 1U;
        ri++;
        remain = runs[ri];
    }

    uint8_t rows[BAD_BLOCK_SIZE] = {0U,0U,0U,0U,0U,0U,0U,0U};
    uint8_t outer;
    for (outer = 0U; outer < BAD_BLOCK_SIZE; outer++) {
        uint8_t inner;
        for (inner = 0U; inner < BAD_BLOCK_SIZE; inner++) {
            uint8_t px, py;
            if (scan_dir == 0U) {
                py = (uint8_t)(sy + (int8_t)outer * dy);
                px = (uint8_t)(sx + (int8_t)inner * dx);
            } else {
                px = (uint8_t)(sx + (int8_t)outer * dx);
                py = (uint8_t)(sy + (int8_t)inner * dy);
            }
            if (color) rows[py] |= (uint8_t)(1U << px);

            if (remain > 0U) remain--;
            if (remain == 0U) {
                color ^= 1U;
                ri++;
                if (ri < n_runs) remain = runs[ri];
            }
        }
    }
    for (outer = 0U; outer < BAD_BLOCK_SIZE; outer++)
        ctx->gram[blk_row(ctx, bx, by, outer)] = rows[outer];
}

/* ============================================================
 * Internal: frame-level fill / copy / swap
 * ============================================================ */

static void fill_frame(bad_ctx_t *ctx, uint8_t color)
{
    bad_memset(ctx->gram, color ? 0xFFU : 0x00U,
               BAD_GRAM_SIZE(ctx->width, ctx->height));
}

static void swap_to_prev(bad_ctx_t *ctx)
{
    bad_memcpy(ctx->prev, ctx->gram,
               BAD_GRAM_SIZE(ctx->width, ctx->height));
}

/* ============================================================
 * Internal: MASTER_BLOCK decode  (SPEC.md 6-8)
 * ============================================================ */

static bad_result_t decode_master_block(bad_ctx_t *ctx,
                                         uint8_t bx, uint8_t by)
{
    uint8_t row, col, byte_buf;
    for (row = 0U; row < BAD_BLOCK_SIZE; row++) {
        if (ctx->read(ctx->stream_offset, &byte_buf, 1U) != 1U)
            return BAD_ERR_DATA;
        ctx->stream_offset++;
        (void)col;
        ctx->gram[blk_row(ctx, bx, by, row)] = byte_buf;
    }
    return BAD_OK;
}

/* ============================================================
 * Internal: RLE_BLOCK_4 decode  (SPEC.md 6-5)
 * ============================================================ */

static bad_result_t decode_rle4(bad_ctx_t *ctx,
                                  uint8_t bx, uint8_t by,
                                  uint8_t op)
{
    uint8_t d[3];
    if (bad_readn(ctx, d, 3U) != 3U) return BAD_ERR_DATA;

    uint8_t runs[4];
    runs[0] = (uint8_t)BAD_RLE4_R0(d[0]);
    runs[1] = (uint8_t)BAD_RLE4_R1(d[0],d[1]);
    runs[2] = (uint8_t)BAD_RLE4_R2(d[1],d[2]);
    runs[3] = (uint8_t)BAD_RLE4_R3(d[2]);

    apply_rle_runs(ctx, bx, by,
                   BAD_RLE4_PATTERN(op),
                   BAD_RLE4_START_COLOR(op),
                   runs, 4U);
    return BAD_OK;
}

/* ============================================================
 * Internal: RLE_BLOCK_8 decode  (SPEC.md 6-6)
 * ============================================================ */

static bad_result_t decode_rle8(bad_ctx_t *ctx,
                                  uint8_t bx, uint8_t by,
                                  uint8_t op)
{
    uint8_t d[6];
    if (bad_readn(ctx, d, 6U) != 6U) return BAD_ERR_DATA;

    uint8_t runs[8];
    runs[0] = (uint8_t)BAD_RLE8_R0(d[0]);
    runs[1] = (uint8_t)BAD_RLE8_R1(d[0],d[1]);
    runs[2] = (uint8_t)BAD_RLE8_R2(d[1],d[2]);
    runs[3] = (uint8_t)BAD_RLE8_R3(d[2]);
    runs[4] = (uint8_t)BAD_RLE8_R4(d[3]);
    runs[5] = (uint8_t)BAD_RLE8_R5(d[3],d[4]);
    runs[6] = (uint8_t)BAD_RLE8_R6(d[4],d[5]);
    runs[7] = (uint8_t)BAD_RLE8_R7(d[5]);

    apply_rle_runs(ctx, bx, by,
                   BAD_RLE8_PATTERN(op),
                   BAD_RLE8_START_COLOR(op),
                   runs, 8U);
    return BAD_OK;
}

/* ============================================================
 * Internal: XOR_BLOCK decode  (SPEC.md 6-9)
 * curr = prev XOR mask  (row-major order)
 * ============================================================ */

static bad_result_t decode_xor_block(bad_ctx_t *ctx,
                                      uint8_t bx, uint8_t by)
{
    uint8_t xor_len = bad_read1(ctx);
    uint8_t bit_idx = 0U;
    uint8_t i;
    uint8_t mask[BAD_BLOCK_SIZE] = {0U,0U,0U,0U,0U,0U,0U,0U};

    for (i = 0U; i < xor_len && bit_idx < BAD_BLOCK_PIXELS; i++) {
        uint8_t b       = bad_read1(ctx);
        uint8_t mask_v  = BAD_XOR_MASK_VAL(b);
        uint8_t run_len = BAD_XOR_RUN_LEN(b);
        uint8_t k;
        for (k = 0U; k < run_len && bit_idx < BAD_BLOCK_PIXELS; k++, bit_idx++) {
            uint8_t py = bit_idx >> 3U;
            if (mask_v) mask[py] |= (uint8_t)(1U << (bit_idx & 7U));
        }
    }
    /* 元の実装はマスクが届いた画素だけ書くが、gram は bad_next_frame の最初に prev と同じにしてあるので
     * 残りの画素を prev ^ 0 で書いても結果は同じ */
    for (i = 0U; i < BAD_BLOCK_SIZE; i++) {
        uint16_t r = blk_row(ctx, bx, by, i);
        ctx->gram[r] = (uint8_t)(ctx->prev[r] ^ mask[i]);
    }
    return BAD_OK;
}

/* ============================================================
 * Internal: SHIFT_BIT decode  (SPEC.md 6-3)
 * ============================================================ */

static void decode_shift_bit(bad_ctx_t *ctx,
                               uint8_t bx, uint8_t by,
                               uint8_t op)
{
    uint8_t rows[BAD_BLOCK_SIZE];
    uint8_t r, c;

    for (r = 0U; r < BAD_BLOCK_SIZE; r++)
        rows[r] = ctx->prev[blk_row(ctx, bx, by, r)];
    (void)c;

    /* [AutoKeyMouse 修正] Codec.py apply_shift は 1px ずつ「反対側の端」を
     * 取り込んで進めるため、結果は端値パディングではなく巡回シフト(回転)になる。
     * リファレンス(Codec.py)とビット一致させるため回転で実装する。
     * +X: 列 c <- 列 c-1 (bit c = 列 c なので rotl)、-X: rotr
     * +Y: 行 r <- 行 r-1 (下方向回転)、-Y: 上方向回転 */
    uint8_t mag_x = BAD_SHIFT_MAG_X(op);
    if (mag_x > 0U) {
        uint8_t sh = (uint8_t)(mag_x & 7U);
        if (sh) {
            for (r = 0U; r < BAD_BLOCK_SIZE; r++) {
                if (BAD_SHIFT_SIGN_X(op) == 0U)
                    rows[r] = (uint8_t)((rows[r] << sh) | (rows[r] >> (8U - sh)));
                else
                    rows[r] = (uint8_t)((rows[r] >> sh) | (rows[r] << (8U - sh)));
            }
        }
    }

    uint8_t mag_y = BAD_SHIFT_MAG_Y(op);
    if (mag_y > 0U) {
        uint8_t tmp[BAD_BLOCK_SIZE];
        uint8_t i;
        for (i = 0U; i < BAD_BLOCK_SIZE; i++) tmp[i] = rows[i];
        for (i = 0U; i < BAD_BLOCK_SIZE; i++) {
            if (BAD_SHIFT_SIGN_Y(op) == 0U)
                rows[(uint8_t)((i + mag_y) & 7U)] = tmp[i];
            else
                rows[i] = tmp[(uint8_t)((i + mag_y) & 7U)];
        }
    }

    for (r = 0U; r < BAD_BLOCK_SIZE; r++)
        ctx->gram[blk_row(ctx, bx, by, r)] = rows[r];
}

/* ============================================================
 * Internal: block-level fill / copy / invert
 * ============================================================ */

static void fill_block(bad_ctx_t *ctx, uint8_t bx, uint8_t by, uint8_t color)
{
    uint8_t py, v = color ? 0xFFU : 0x00U;
    for (py=0U; py<BAD_BLOCK_SIZE; py++)
        ctx->gram[blk_row(ctx,bx,by,py)] = v;
}

static void copy_block(bad_ctx_t *ctx, uint8_t bx, uint8_t by)
{
    uint8_t py;
    for (py=0U; py<BAD_BLOCK_SIZE; py++) {
        uint16_t r = blk_row(ctx,bx,by,py);
        ctx->gram[r] = ctx->prev[r];
    }
}

static void invert_block(bad_ctx_t *ctx, uint8_t bx, uint8_t by)
{
    uint8_t py;
    for (py=0U; py<BAD_BLOCK_SIZE; py++) {
        uint16_t r = blk_row(ctx,bx,by,py);
        ctx->gram[r] = (uint8_t)~ctx->prev[r];
    }
}

/* ============================================================
 * Internal: BLOCK_STREAM decode  (SPEC.md 7-7)
 * FOR nest forbidden.
 * ============================================================ */

static bad_result_t decode_block_stream(bad_ctx_t *ctx)
{
    uint8_t  nbx   = blk_nx(ctx);
    uint16_t total = (uint16_t)nbx * (uint16_t)blk_ny(ctx);
    uint16_t ptr   = 0U;
    bad_result_t ret;

    while (ptr < total) {
        uint8_t op = bad_read1(ctx);

        /* FOR: decoder handles all repeat values including 0xC0 (repeat=2).
         * Encoder min is 0xC1 (repeat=3) since rev.19; 0xC0 is forbidden.
         * Decoder is tolerant and processes any value 0xC0-0xFF correctly. */
        if (BAD_IS_FOR(op)) {
            uint8_t repeat = (uint8_t)BAD_FOR_COUNT(op);
            uint8_t iop    = bad_read1(ctx);
            uint8_t fi;
            for (fi = 0U; fi < repeat && ptr < total; fi++) {
                uint8_t bx = (uint8_t)(ptr % nbx);
                uint8_t by = (uint8_t)(ptr / nbx);
                if (BAD_IS_SKIP(iop)) {
                    uint8_t cnt = (uint8_t)BAD_SKIP_COUNT(iop);
                    while (cnt-- && ptr < total) {
                        copy_block(ctx,(uint8_t)(ptr%nbx),(uint8_t)(ptr/nbx));
                        ptr++;
                    }
                } else if (BAD_IS_INVERT(iop)) {
                    uint8_t cnt = (uint8_t)BAD_INVERT_COUNT(iop);
                    while (cnt-- && ptr < total) {
                        invert_block(ctx,(uint8_t)(ptr%nbx),(uint8_t)(ptr/nbx));
                        ptr++;
                    }
                } else if (BAD_IS_FILL(iop)) {
                    uint8_t col = BAD_FILL_COLOR(iop);
                    uint8_t cnt = (uint8_t)BAD_FILL_COUNT(iop);
                    while (cnt-- && ptr < total) {
                        fill_block(ctx,(uint8_t)(ptr%nbx),(uint8_t)(ptr/nbx),col);
                        ptr++;
                    }
                } else if (BAD_IS_SHIFT(iop)) {
                    decode_shift_bit(ctx, bx, by, iop);
                    ptr++;
                } else {
                    return BAD_ERR_DATA;
                }
            }
            continue;
        }

        uint8_t bx = (uint8_t)(ptr % nbx);
        uint8_t by = (uint8_t)(ptr / nbx);

        if (BAD_IS_SKIP(op)) {
            uint8_t cnt = (uint8_t)BAD_SKIP_COUNT(op);
            while (cnt-- && ptr < total) {
                copy_block(ctx,(uint8_t)(ptr%nbx),(uint8_t)(ptr/nbx));
                ptr++;
            }
        } else if (BAD_IS_INVERT(op)) {
            uint8_t cnt = (uint8_t)BAD_INVERT_COUNT(op);
            while (cnt-- && ptr < total) {
                invert_block(ctx,(uint8_t)(ptr%nbx),(uint8_t)(ptr/nbx));
                ptr++;
            }
        } else if (BAD_IS_FILL(op)) {
            uint8_t col = BAD_FILL_COLOR(op);
            uint8_t cnt = (uint8_t)BAD_FILL_COUNT(op);
            while (cnt-- && ptr < total) {
                fill_block(ctx,(uint8_t)(ptr%nbx),(uint8_t)(ptr/nbx),col);
                ptr++;
            }
        } else if (BAD_IS_SHIFT(op)) {
            decode_shift_bit(ctx, bx, by, op);
            ptr++;
        } else if (BAD_IS_RLE4(op)) {
            ret = decode_rle4(ctx, bx, by, op);
            if (ret != BAD_OK) return ret;
            ptr++;
        } else if (BAD_IS_RLE8(op)) {
            ret = decode_rle8(ctx, bx, by, op);
            if (ret != BAD_OK) return ret;
            ptr++;
        } else if (op == BAD_OP_MASTER_BLOCK) {
            ret = decode_master_block(ctx, bx, by);
            if (ret != BAD_OK) return ret;
            ptr++;
        } else if (op == BAD_OP_XOR_BLOCK) {
            ret = decode_xor_block(ctx, bx, by);
            if (ret != BAD_OK) return ret;
            ptr++;
        } else {
            return BAD_ERR_DATA;
        }
    }
    return BAD_OK;
}

/* ============================================================
 * Internal: RLE_FRAME body decode  (SPEC.md 7-6)
 * Shared by RLE_FRAME and DELTA_FRAME.
 * Writes decoded pixels directly to dst_buf (no intermediate buffer).
 * ============================================================ */

static bad_result_t decode_rle_body(bad_ctx_t *ctx,
                                     uint8_t   *dst_buf,
                                     uint8_t    op)
{
    uint8_t scan_dir = BAD_RLF_SCAN_DIR(op);
    uint8_t start_y  = BAD_RLF_START_Y(op);
    uint8_t start_x  = BAD_RLF_START_X(op);
    uint16_t w = ctx->width;
    uint16_t h = ctx->height;
    int16_t  sx = start_x ? (int16_t)(w-1U) : 0;
    int16_t  sy = start_y ? (int16_t)(h-1U) : 0;
    int8_t   dx = start_x ? -1 : 1;
    int8_t   dy = start_y ? -1 : 1;

    uint8_t run_color  = 0U;
    uint8_t run_remain = 0U;

#define NEXT_PX(oc) do {                                    \
    if (run_remain == 0U) {                                 \
        uint8_t _b = bad_read1(ctx);                        \
        if (_b == 0x00U) return BAD_ERR_DATA;               \
        run_color  = BAD_RLEFRAME_COLOR(_b);                \
        run_remain = (uint8_t)BAD_RLEFRAME_LEN(_b);        \
        if (run_remain == 0U) return BAD_ERR_DATA;          \
    }                                                       \
    (oc) = run_color; run_remain--;                         \
} while(0)

    if (scan_dir == 0U) {
        int16_t y;
        for (y=sy; dy>0?(y<(int16_t)h):(y>=0); y+=dy) {
            int16_t x;
            for (x=sx; dx>0?(x<(int16_t)w):(x>=0); x+=dx) {
                uint8_t c; NEXT_PX(c);
                gp_set(dst_buf, (uint16_t)y*w+(uint16_t)x, c);
            }
        }
    } else {
        int16_t x;
        for (x=sx; dx>0?(x<(int16_t)w):(x>=0); x+=dx) {
            int16_t y;
            for (y=sy; dy>0?(y<(int16_t)h):(y>=0); y+=dy) {
                uint8_t c; NEXT_PX(c);
                gp_set(dst_buf, (uint16_t)y*w+(uint16_t)x, c);
            }
        }
    }
#undef NEXT_PX
    return BAD_OK;
}

/* ============================================================
 * Internal: RLE_FRAME decode  (SPEC.md 7-6)
 * Decodes directly into gram.
 * ============================================================ */

static bad_result_t decode_rle_frame(bad_ctx_t *ctx, uint8_t op)
{
    return decode_rle_body(ctx, ctx->gram, op);
}

/* ============================================================
 * Internal: DELTA_FRAME decode  (SPEC.md 7-8)
 * Format: [0x3D][pattern_byte][RLE_data]
 * curr = prev XOR diff
 * ============================================================ */

static bad_result_t decode_delta_frame(bad_ctx_t *ctx)
{
    uint8_t      pat_byte = bad_read1(ctx);
    uint16_t     sz       = BAD_GRAM_SIZE(ctx->width, ctx->height);
    bad_result_t ret;
    uint16_t     i;

    /* Decode diff into gram temporarily, then XOR with prev */
    bad_memset(ctx->gram, 0x00U, sz);
    ret = decode_rle_body(ctx, ctx->gram, pat_byte);
    if (ret != BAD_OK) return ret;

    /* curr = prev XOR diff  (byte-level XOR) */
    for (i = 0U; i < sz; i++)
        ctx->gram[i] ^= ctx->prev[i];

    return BAD_OK;
}

/* ============================================================
 * Internal: MASTER_FRAME decode  (SPEC.md 7-5)
 * ============================================================ */

static bad_result_t decode_master_frame(bad_ctx_t *ctx)
{
    uint16_t sz = BAD_GRAM_SIZE(ctx->width, ctx->height);
    if (bad_readn(ctx, ctx->gram, sz) != sz) return BAD_ERR_DATA;
    return BAD_OK;
}

/* Skip FRAME_DELIMITER bytes and return the next opcode */
static uint8_t read_frame_op(bad_ctx_t *ctx)
{
    uint8_t op;
    do { op = bad_read1(ctx); } while (op == BAD_OP_FRAME_DELIMITER);
    return op;
}

/* ============================================================
 * Public API: bad_init
 * ============================================================ */

bad_result_t bad_init(bad_ctx_t *ctx)
{
    if (!ctx || !ctx->read || !ctx->gram || !ctx->prev || !ctx->buf_size)
        return BAD_ERR_ARG;

    uint8_t  hdr[BAD_HEADER_SIZE];
    uint16_t n = ctx->read(0U, hdr, BAD_HEADER_SIZE);
    if (n < BAD_HEADER_SIZE) return BAD_ERR_HDR;

    uint16_t hdr_size = (uint16_t)(((uint16_t)hdr[1]<<8U)|hdr[0]);
    if (hdr_size != BAD_HEADER_SIZE) return BAD_ERR_HDR;

    uint16_t stored_chk = (uint16_t)(((uint16_t)hdr[3]<<8U)|hdr[2]);
    if (stored_chk != fletcher16(&hdr[4], BAD_HEADER_BODY_SIZE))
        return BAD_ERR_HDR;

    if (hdr[4]!=(uint8_t)'B'||hdr[5]!=(uint8_t)'a'||hdr[6]!=(uint8_t)'d')
        return BAD_ERR_MAGIC;

    ctx->width        = (uint16_t)(((uint16_t)hdr[12]<<8U)|hdr[11]);
    ctx->height       = (uint16_t)(((uint16_t)hdr[14]<<8U)|hdr[13]);
    ctx->total_frames = (uint16_t)(((uint16_t)hdr[18]<<8U)|hdr[17]);

    if (ctx->buf_size < BAD_GRAM_SIZE(ctx->width, ctx->height))
        return BAD_ERR_MEM;

    ctx->stream_offset = BAD_HEADER_SIZE;
    ctx->current_frame = 0U;
    ctx->initialized   = 1U;
    bad_memset(ctx->prev, 0x00U, BAD_GRAM_SIZE(ctx->width, ctx->height));
    return BAD_OK;
}

/* ============================================================
 * Public API: bad_next_frame
 * ============================================================ */

bad_result_t bad_next_frame(bad_ctx_t *ctx)
{
    if (!ctx || !ctx->initialized) return BAD_ERR_ARG;
    if (ctx->current_frame >= ctx->total_frames) return BAD_EOF;

    swap_to_prev(ctx);
    uint8_t op = read_frame_op(ctx);
    bad_result_t ret = BAD_OK;

    switch (op) {

    case BAD_OP_SKIP_FRAME:
        bad_memcpy(ctx->gram, ctx->prev,
                   BAD_GRAM_SIZE(ctx->width, ctx->height));
        break;

    case BAD_OP_FILL_BLACK:
        fill_frame(ctx, 0U);
        break;

    case BAD_OP_FILL_WHITE:
        fill_frame(ctx, 1U);
        break;

    case BAD_OP_INVERT_PREV: {
        uint16_t sz = BAD_GRAM_SIZE(ctx->width, ctx->height);
        uint16_t i;
        for (i=0U; i<sz; i++) ctx->gram[i]=(uint8_t)~ctx->prev[i];
        break;
    }

    case BAD_OP_MASTER_FRAME:
        ret = decode_master_frame(ctx);
        break;

    /* RLE_FRAME: 0x30-0x37 (8 scan patterns) */
    case 0x30U: case 0x31U: case 0x32U: case 0x33U:
    case 0x34U: case 0x35U: case 0x36U: case 0x37U:
        ret = decode_rle_frame(ctx, op);
        break;

    case BAD_OP_BLOCK_STREAM:
        ret = decode_block_stream(ctx);
        break;

    /* DELTA_FRAME: 0x3D  XOR diff + RLE (rev.16) */
    case BAD_OP_DELTA_FRAME:
        ret = decode_delta_frame(ctx);
        break;

    case BAD_OP_EXT_PREFIX:
        (void)bad_read1(ctx);   /* consume sub-command; skip unknown */
        bad_memcpy(ctx->gram, ctx->prev,
                   BAD_GRAM_SIZE(ctx->width, ctx->height));
        break;

    default:
        ret = BAD_ERR_DATA;
        break;
    }

    if (ret == BAD_OK) {
        ctx->current_frame++;
        if (ctx->current_frame >= ctx->total_frames) ret = BAD_EOF;
    }
    return ret;
}

/* ============================================================
 * Public API: bad_rewind
 * ============================================================ */

bad_result_t bad_rewind(bad_ctx_t *ctx)
{
    if (!ctx || !ctx->initialized) return BAD_ERR_ARG;
    ctx->stream_offset = BAD_HEADER_SIZE;
    ctx->current_frame = 0U;
    bad_memset(ctx->prev, 0x00U,
               BAD_GRAM_SIZE(ctx->width, ctx->height));
    return BAD_OK;
}

/* ============================================================
 * Public API: bad_seek
 * ============================================================ */

bad_result_t bad_seek(bad_ctx_t *ctx, uint16_t frame_no)
{
    if (!ctx || !ctx->initialized) return BAD_ERR_ARG;
    if (frame_no >= ctx->total_frames) return BAD_EOF;

    bad_result_t ret = bad_rewind(ctx);
    if (ret != BAD_OK) return ret;

    while (ctx->current_frame < frame_no) {
        ret = bad_next_frame(ctx);
        if (ret != BAD_OK && ret != BAD_EOF) return ret;
    }
    return BAD_OK;
}

/* ============================================================
 * Public API: bad_result_str
 * ============================================================ */

const char *bad_result_str(bad_result_t result)
{
    switch (result) {
    case BAD_OK:        return "BAD_OK";
    case BAD_BUSY:      return "BAD_BUSY";
    case BAD_EOF:       return "BAD_EOF";
    case BAD_ERR_HDR:   return "BAD_ERR_HDR";
    case BAD_ERR_MAGIC: return "BAD_ERR_MAGIC";
    case BAD_ERR_DATA:  return "BAD_ERR_DATA";
    case BAD_ERR_MEM:   return "BAD_ERR_MEM";
    case BAD_ERR_ARG:   return "BAD_ERR_ARG";
    default:            return "BAD_ERR_UNKNOWN";
    }
}
