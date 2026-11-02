// BadCodec の RLE_BLOCK_4 / RLE_BLOCK_8 の高速版 (firmware/AutoKeyMouse/bad_decode.cpp の apply_rle_runs) が、
// 元の 1 画素ずつ歩く処理と同じ 8x8 を出すことを確かめる。RLE_BLOCK_4 は全命令 x 3 バイトの全組み合わせ (2^24 x 16)、
// RLE_BLOCK_8 は全命令 x 乱数 400 万通り。違いがあれば最初の 1 つを出して終了コード 1。
#include <cstdio>
#include <cstdint>
#include <cstring>

#include "../../firmware/AutoKeyMouse/bad_decode.h"

// 元の処理 (AutoKeyMouse の長さ 0 の先頭ランの修正を含む)。rows[py] の bit px
static void ref_apply(uint8_t p_idx, uint8_t start_color, const uint8_t* runs, uint8_t n_runs, uint8_t rows[8]) {
  uint8_t scan_dir = (p_idx >> 2) & 1;
  uint8_t sx = (p_idx & 1) ? 7 : 0, sy = (p_idx & 2) ? 7 : 0;
  int8_t dx = (p_idx & 1) ? -1 : 1, dy = (p_idx & 2) ? -1 : 1;
  uint8_t color = start_color, ri = 0, remain = n_runs ? runs[0] : 0;
  while (remain == 0 && ri + 1 < n_runs) { color ^= 1; ri++; remain = runs[ri]; }
  memset(rows, 0, 8);
  for (uint8_t outer = 0; outer < 8; outer++)
    for (uint8_t inner = 0; inner < 8; inner++) {
      uint8_t px, py;
      if (scan_dir == 0) { py = sy + (int8_t)outer * dy; px = sx + (int8_t)inner * dx; }
      else { px = sx + (int8_t)outer * dx; py = sy + (int8_t)inner * dy; }
      if (color) rows[py] |= (uint8_t)(1u << px);
      if (remain > 0) remain--;
      if (remain == 0) { color ^= 1; ri++; if (ri < n_runs) remain = runs[ri]; }
    }
}

static int check(const uint8_t* data) {
  uint8_t op = data[0], got[8], want[8], runs[8];
  if (bad_block_abs_rows(data, got) != BAD_OK) { printf("decode failed op=%02x\n", op); return 1; }
  if (BAD_IS_RLE4(op)) {
    runs[0] = BAD_RLE4_R0(data[1]); runs[1] = BAD_RLE4_R1(data[1], data[2]);
    runs[2] = BAD_RLE4_R2(data[2], data[3]); runs[3] = BAD_RLE4_R3(data[3]);
    ref_apply(BAD_RLE4_PATTERN(op), BAD_RLE4_START_COLOR(op), runs, 4, want);
  } else {
    const uint8_t* d = data + 1;
    runs[0] = BAD_RLE8_R0(d[0]); runs[1] = BAD_RLE8_R1(d[0], d[1]); runs[2] = BAD_RLE8_R2(d[1], d[2]);
    runs[3] = BAD_RLE8_R3(d[2]); runs[4] = BAD_RLE8_R4(d[3]); runs[5] = BAD_RLE8_R5(d[3], d[4]);
    runs[6] = BAD_RLE8_R6(d[4], d[5]); runs[7] = BAD_RLE8_R7(d[5]);
    ref_apply(BAD_RLE8_PATTERN(op), BAD_RLE8_START_COLOR(op), runs, 8, want);
  }
  if (memcmp(got, want, 8) != 0) {
    printf("mismatch op=%02x data=%02x %02x %02x %02x %02x %02x\n", op, data[1], data[2], data[3], data[4], data[5],
           data[6]);
    return 1;
  }
  return 0;
}

int main(int argc, char** argv) {
  // quick: pytest から (RLE_BLOCK_4 は 1/97 に間引き、RLE_BLOCK_8 は 1/20)。引数なしは全部 (1 分ほど)
  bool quick = argc > 1 && strcmp(argv[1], "quick") == 0;
  uint32_t step = quick ? 97 : 1;
  int n8 = quick ? 200000 : 4000000;
  uint8_t d[9] = {};
  for (int op = 0x20; op <= 0x2F; op++)  // RLE_BLOCK_4
    for (uint32_t v = 0; v < (1u << 24); v += step) {
      d[0] = (uint8_t)op; d[1] = (uint8_t)v; d[2] = (uint8_t)(v >> 8); d[3] = (uint8_t)(v >> 16);
      if (check(d)) return 1;
    }
  const uint8_t rle8[] = {0x38, 0x39, 0x3A, 0x3B, 0x3D, 0x3E};
  uint32_t x = 12345;
  for (uint8_t op : rle8)
    for (int i = 0; i < n8; i++) {
      d[0] = op;
      for (int k = 1; k <= 6; k++) { x ^= x << 13; x ^= x >> 17; x ^= x << 5; d[k] = (uint8_t)x; }
      if (check(d)) return 1;
    }
  puts("ok");
  return 0;
}
