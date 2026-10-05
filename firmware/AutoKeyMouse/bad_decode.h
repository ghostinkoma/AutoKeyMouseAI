/**
 * @file    bad_decode.h
 * @brief   BadCodec v0.5.1 - Decoder for 8/32-bit MCU
 * @version 0.5.1  (Protocol: 514)
 * @date    2026-03-15
 * @license Non-Commercial Use Only  ghostinkoma@gmail.com
 *
 * Supported platforms:
 *   ATmega328 / LGT8F328  Arduino (avr-gcc)
 *   ESP32-Cx              ESP-IDF (xtensa/riscv gcc)
 *   RP2040 / RP2350       PlatformIO (arm-none-eabi-gcc)
 *
 * Design policy:
 *   - NO malloc / free  (static allocation only)
 *   - NO floating point
 *   - NO division  (>>3 and &7 replace /8 and %8)
 *   - NO standard library except <stdint.h>
 *   - PROGMEM transparent via read callback
 *   - C99 compliant / C++11 compatible
 *
 * Protocol version history:
 *   510 : Fletcher-16 header checksum
 *   511 : Opcode remap  0x30=RLE_FRAME  0x3C=BLOCK_STREAM
 *   512 : RLE_FRAME x8 scan patterns (0x30-0x37)
 *   513 : RLE_BLOCK_8 (0x38-0x3B,0x3D-0x3E) + XOR_BLOCK (0x3F)
 *   514 : DELTA_FRAME (0x3D) frame-level XOR diff + FOR min repeat=4
 *   514r19: FOR min repeat changed 4->3 (0xC1 now valid encoder output)
 */

#ifndef BAD_DECODE_H
#define BAD_DECODE_H

/* ============================================================
 * Platform detection
 * ============================================================ */

#if defined(__AVR__)
  #include <stdint.h>
  #define BAD_PLATFORM_AVR
  #define BAD_PLATFORM_STR     "AVR"
  #define BAD_INLINE           inline
  #define BAD_STATIC_INLINE    static inline
  typedef uint16_t bad_addr_t;

#elif defined(ESP_PLATFORM) || defined(IDF_VER)
  #include <stdint.h>
  #define BAD_PLATFORM_ESP32
  #define BAD_PLATFORM_STR     "ESP32"
  #define BAD_INLINE           inline __attribute__((always_inline))
  #define BAD_STATIC_INLINE    static inline __attribute__((always_inline))
  typedef uint32_t bad_addr_t;

#elif defined(ARDUINO_ARCH_RP2040) || defined(PICO_BOARD) \
   || defined(TARGET_RP2040)       || defined(TARGET_RP2350)
  #include <stdint.h>
  #define BAD_PLATFORM_RP2
  #define BAD_PLATFORM_STR     "RP2"
  #define BAD_INLINE           inline __attribute__((always_inline))
  #define BAD_STATIC_INLINE    static inline __attribute__((always_inline))
  typedef uint32_t bad_addr_t;

#else
  #include <stdint.h>
  #define BAD_PLATFORM_UNKNOWN
  #define BAD_PLATFORM_STR     "UNKNOWN"
  #define BAD_INLINE           inline
  #define BAD_STATIC_INLINE    static inline
  typedef uint32_t bad_addr_t;
#endif

#ifndef NULL
  #define NULL ((void*)0)
#endif

/* ============================================================
 * Version / Protocol
 * ============================================================ */

#define BAD_VERSION_STR       "0.5.1"
#define BAD_PROTOCOL_VERSION  ((uint16_t)514)

/* ============================================================
 * File format constants
 * ============================================================ */

#define BAD_HEADER_SIZE       ((uint8_t)19)
#define BAD_HEADER_BODY_SIZE  ((uint8_t)15)
#define BAD_BLOCK_SIZE        ((uint8_t)8)
#define BAD_BLOCK_PIXELS      ((uint8_t)64)
#define BAD_BLOCK_BYTES       ((uint8_t)8)

/* ============================================================
 * Frame-level opcodes  (SPEC.md 4-2)
 *
 * 0x30-0x37 : RLE_FRAME x8 scan patterns
 *   bit2 scan_dir (0=H 1=V)  bit1 start_y  bit0 start_x
 * 0x38 : FRAME_DELIMITER
 * 0x39 : SKIP_FRAME
 * 0x3A : FRAME_FILL_BLACK
 * 0x3B : FRAME_FILL_WHITE
 * 0x3C : BLOCK_STREAM
 * 0x3D : DELTA_FRAME  (XOR diff + RLE, rev.16)
 * 0x3E : MASTER_FRAME
 * 0x3F : INVERT_PREV_FRAME
 * 0xFF : EXT_PREFIX
 * ============================================================ */

#define BAD_OP_RLE_FRAME       ((uint8_t)0x30)  /* base; 0x30-0x37 valid */
#define BAD_OP_FRAME_DELIMITER ((uint8_t)0x38)
#define BAD_OP_SKIP_FRAME      ((uint8_t)0x39)
#define BAD_OP_FILL_BLACK      ((uint8_t)0x3A)
#define BAD_OP_FILL_WHITE      ((uint8_t)0x3B)
#define BAD_OP_BLOCK_STREAM    ((uint8_t)0x3C)
#define BAD_OP_DELTA_FRAME     ((uint8_t)0x3D)  /* rev.16 */
#define BAD_OP_MASTER_FRAME    ((uint8_t)0x3E)
#define BAD_OP_INVERT_PREV     ((uint8_t)0x3F)
#define BAD_OP_EXT_PREFIX      ((uint8_t)0xFF)

/** RLE_FRAME / DELTA_FRAME range tests */
#define BAD_IS_RLE_FRAME(op)   (((op) & 0xF8U) == 0x30U)

/** Scan parameter extraction (shared by RLE_FRAME and DELTA_FRAME) */
#define BAD_RLF_SCAN_DIR(op)   (((op) >> 2U) & 0x01U)
#define BAD_RLF_START_Y(op)    (((op) >> 1U) & 0x01U)
#define BAD_RLF_START_X(op)    ( (op)          & 0x01U)

/* ============================================================
 * Block-level opcode classifiers  (SPEC.md Ch.5)
 * All macros - zero call overhead, optimised for AVR branches.
 * ============================================================ */

/* SKIP_BLOCK  10nnnnnn  0x80-0xBF */
#define BAD_IS_SKIP(op)          (((op)&0xC0U)==0x80U)
#define BAD_SKIP_COUNT(op)       (((op)&0x3FU)+1U)

/* FOR  11nnnnnn  0xC0-0xFF  repeat=n+2
 * Encoder must NOT generate 0xC0 (repeat=2): FOR+CMD=2B = literal x2=2B, pointless.
 * Encoder minimum valid output: 0xC1 (repeat=3): FOR+CMD=2B < literal x3=3B.
 * Decoder handles all values including 0xC0 gracefully.  (SPEC.md rev.19) */
#define BAD_IS_FOR(op)           (((op)&0xC0U)==0xC0U)
#define BAD_FOR_COUNT(op)        (((op)&0x3FU)+2U)

/* SHIFT_BIT  01sxxyy  0x40-0x7F
 * Encoder must NOT generate SHIFT(0,0) - use SKIP_BLOCK instead. */
#define BAD_IS_SHIFT(op)         (((op)&0xC0U)==0x40U)
#define BAD_SHIFT_SIGN_X(op)     (((op)>>5U)&0x01U)
#define BAD_SHIFT_MAG_X(op)      (((op)>>3U)&0x03U)
#define BAD_SHIFT_SIGN_Y(op)     (((op)>>2U)&0x01U)
#define BAD_SHIFT_MAG_Y(op)      ( (op)      &0x03U)

/* BLOCK_INVERT  000nnnnn  0x00-0x1F */
#define BAD_IS_INVERT(op)        (((op)&0xE0U)==0x00U)
#define BAD_INVERT_COUNT(op)     (((op)&0x1FU)+1U)

/* RLE_BLOCK_4  0010pppc  0x20-0x2F  (3 data bytes) */
#define BAD_IS_RLE4(op)          (((op)&0xF0U)==0x20U)
#define BAD_RLE4_PATTERN(op)     (((op)>>1U)&0x07U)
#define BAD_RLE4_START_COLOR(op) ( (op)       &0x01U)

/* FILL_BLOCK  00110cnn  0x30-0x37 */
#define BAD_IS_FILL(op)          (((op)&0xF8U)==0x30U)
#define BAD_FILL_COLOR(op)       (((op)>>2U)&0x01U)
#define BAD_FILL_COUNT(op)       (((op)&0x03U)+1U)

/* ============================================================
 * FRAME_CONTROL space (0x38-0x3F) - block context assignments
 *
 * 0x38 : RLE_BLOCK_8  H/TL  black  (6 data bytes)
 * 0x39 : RLE_BLOCK_8  H/TL  white  (6 data bytes)
 * 0x3A : RLE_BLOCK_8  H/TR  black  (6 data bytes)
 * 0x3B : RLE_BLOCK_8  H/TR  white  (6 data bytes)
 * 0x3C : MASTER_BLOCK              (8 data bytes)
 * 0x3D : RLE_BLOCK_8  V/TL  black  (6 data bytes)
 * 0x3E : RLE_BLOCK_8  V/TL  white  (6 data bytes)
 * 0x3F : XOR_BLOCK             variable (1+N bytes)
 *
 * Note: 0x3C frame-context = BLOCK_STREAM  (no conflict)
 *       0x3D frame-context = DELTA_FRAME   (no conflict)
 *       0x3F frame-context = INVERT_PREV   (no conflict)
 * ============================================================ */

#define BAD_OP_MASTER_BLOCK  ((uint8_t)0x3C)
#define BAD_OP_XOR_BLOCK     ((uint8_t)0x3F)

/** RLE_BLOCK_8 range test */
#define BAD_IS_RLE8(op) \
    ((op)==0x38U||(op)==0x39U||(op)==0x3AU|| \
     (op)==0x3BU||(op)==0x3DU||(op)==0x3EU)

/** RLE_BLOCK_8: scan pattern index (0=H/TL  1=H/TR  4=V/TL) */
#define BAD_RLE8_PATTERN(op) \
    (((op)==0x38U||(op)==0x39U) ? 0U : \
     ((op)==0x3AU||(op)==0x3BU) ? 1U : 4U)

/** RLE_BLOCK_8: start color (0=black 1=white)
 *  [AutoKeyMouse 修正] 0x3D (V/TL 黒基準) / 0x3E (V/TL 白基準) は偶奇が逆なので op&1 では反転してしまう。
 *  SPEC.md 第5章・Codec.py の RLE8_TABLE に合わせる。 */
#define BAD_RLE8_START_COLOR(op)  (((op)==0x3EU) ? 1U : ((op)==0x3DU) ? 0U : ((op)&0x01U))

/* ============================================================
 * RLE_BLOCK_4 unpack macros  (SPEC.md 6-5)
 * 6bit x 4 runs -> 3 bytes LE
 * ============================================================ */

#define BAD_RLE4_R0(b0)      ((b0)&0x3FU)
#define BAD_RLE4_R1(b0,b1)   ((((b0)>>6U)&0x03U)|(((b1)&0x0FU)<<2U))
#define BAD_RLE4_R2(b1,b2)   ((((b1)>>4U)&0x0FU)|(((b2)&0x03U)<<4U))
#define BAD_RLE4_R3(b2)      (((b2)>>2U)&0x3FU)

/* ============================================================
 * RLE_BLOCK_8 unpack macros  (SPEC.md 6-6)
 * 6bit x 8 runs -> 6 bytes LE (two consecutive 3-byte packs)
 * ============================================================ */

#define BAD_RLE8_R0(b0)      BAD_RLE4_R0(b0)
#define BAD_RLE8_R1(b0,b1)   BAD_RLE4_R1(b0,b1)
#define BAD_RLE8_R2(b1,b2)   BAD_RLE4_R2(b1,b2)
#define BAD_RLE8_R3(b2)      BAD_RLE4_R3(b2)
#define BAD_RLE8_R4(b3)      BAD_RLE4_R0(b3)
#define BAD_RLE8_R5(b3,b4)   BAD_RLE4_R1(b3,b4)
#define BAD_RLE8_R6(b4,b5)   BAD_RLE4_R2(b4,b5)
#define BAD_RLE8_R7(b5)      BAD_RLE4_R3(b5)

/* ============================================================
 * RLE_FRAME / DELTA_FRAME byte layout  (SPEC.md 7-6, 7-8)
 * bit7=color  bit6-0=run_len(1-127)
 * ============================================================ */

#define BAD_RLEFRAME_COLOR(b)  (((b)>>7U)&0x01U)
#define BAD_RLEFRAME_LEN(b)    ( (b)      &0x7FU)

/* ============================================================
 * XOR_BLOCK data byte layout  (SPEC.md 6-9)
 * bit7=mask_value  bit6-0=run_len(1-63)
 * ============================================================ */

#define BAD_XOR_MASK_VAL(b)  (((b)>>7U)&0x01U)
#define BAD_XOR_RUN_LEN(b)   ( (b)      &0x7FU)

/* ============================================================
 * FOR encoder constraint  (SPEC.md 9-2, rev.19)
 *
 * Encoder minimum repeat count = 3 (opcode 0xC1).
 *   repeat=2 (0xC0): FOR+CMD=2B == literal x2=2B  -> FORBIDDEN (pointless)
 *   repeat=3 (0xC1): FOR+CMD=2B  < literal x3=3B  -> VALID (saves 1B)
 *   repeat=4 (0xC2): FOR+CMD=2B  < literal x4=4B  -> VALID (saves 2B)
 *   ...
 * Maximum repeat = 65 (0xFF): physical limit of 6-bit field (63+2=65).
 * repeat=66 would require bit value 64 = 0x40 (7 bits) -- impossible.
 * Decoder handles 0xC0 (repeat=2) gracefully.
 * ============================================================ */

#define BAD_FOR_MIN_REPEAT   ((uint8_t)3U)
#define BAD_FOR_MIN_OPCODE   ((uint8_t)0xC1U)
#define BAD_FOR_MAX_REPEAT   ((uint8_t)65U)
#define BAD_FOR_MAX_OPCODE   ((uint8_t)0xFFU)

/* ============================================================
 * Result codes
 * ============================================================ */

typedef enum {
    BAD_OK        = 0,
    BAD_BUSY      = 1,
    BAD_EOF       = 2,
    BAD_ERR_HDR   = 3,
    BAD_ERR_MAGIC = 4,
    BAD_ERR_DATA  = 5,
    BAD_ERR_MEM   = 6,
    BAD_ERR_ARG   = 7,
} bad_result_t;

/* ============================================================
 * Data source callback
 * ============================================================ */

typedef uint16_t (*bad_read_fn)(bad_addr_t offset,
                                uint8_t   *buf,
                                uint16_t   len);

/* ============================================================
 * Decoder context
 * ============================================================ */

typedef struct {
    bad_read_fn  read;
    uint8_t     *gram;
    uint8_t     *prev;
    uint16_t     buf_size;
    uint16_t     width;
    uint16_t     height;
    uint16_t     total_frames;
    uint16_t     current_frame;
    bad_addr_t   stream_offset;
    uint8_t      initialized;
} bad_ctx_t;

/* ============================================================
 * Public API
 * ============================================================ */

#ifdef __cplusplus
extern "C" {
#endif

bad_result_t bad_init       (bad_ctx_t *ctx);
bad_result_t bad_next_frame (bad_ctx_t *ctx);
bad_result_t bad_rewind     (bad_ctx_t *ctx);
bad_result_t bad_seek       (bad_ctx_t *ctx, uint16_t frame_no);
const char  *bad_result_str (bad_result_t result);

BAD_STATIC_INLINE uint16_t bad_current_frame(const bad_ctx_t *ctx)
{ return (ctx != NULL) ? ctx->current_frame : 0U; }

BAD_STATIC_INLINE uint16_t bad_total_frames(const bad_ctx_t *ctx)
{ return (ctx != NULL) ? ctx->total_frames : 0U; }

#ifdef __cplusplus
}
#endif

/* ============================================================
 * Utility macros
 * ============================================================ */

#define BAD_GRAM_SIZE(w,h) \
    ((uint16_t)(((uint16_t)(w)*(uint16_t)(h))>>3U))

#define BAD_GET_PIXEL(gram,x,y,w) \
    (((gram)[((uint16_t)(y)*(uint16_t)(w)+(uint16_t)(x))>>3U] \
      >>(((uint16_t)(y)*(uint16_t)(w)+(uint16_t)(x))&0x07U))&0x01U)

/* ============================================================
 * Usage example (Arduino / AVR)
 *
 *   #include "bad_decode.h"
 *
 *   static uint8_t   gram[BAD_GRAM_SIZE(128,64)];
 *   static uint8_t   prev[BAD_GRAM_SIZE(128,64)];
 *   static bad_ctx_t ctx;
 *
 *   uint16_t my_read(bad_addr_t off, uint8_t *buf, uint16_t len) {
 *       for (uint16_t i=0; i<len; i++)
 *           buf[i] = pgm_read_byte(VIDEO_DATA + off + i);
 *       return len;
 *   }
 *
 *   void setup() {
 *       ctx.read     = my_read;
 *       ctx.gram     = gram;
 *       ctx.prev     = prev;
 *       ctx.buf_size = sizeof(gram);
 *       bad_init(&ctx);
 *   }
 *
 *   void loop() {
 *       bad_result_t r = bad_next_frame(&ctx);
 *       if (r == BAD_OK)  display_write(ctx.gram);
 *       if (r == BAD_EOF) bad_rewind(&ctx);
 *   }
 * ============================================================ */

#endif /* BAD_DECODE_H */
