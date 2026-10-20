#pragma once
// 窓の種類の認識 (エッジ AI) の特徴づくり。Arduino に依存しない部分 (PC のテスト tests/edge_win_test.cpp でも使う)。
// PC 側の同じ計算: pc/akm/edge/winfeat.py
//
//   液晶に描いた RGB565 → 明るさ → 横 2 画素の平均 = 120x136 のバッファ (16KB。ESP32 が持つのはこれだけ)
//   スロット (窓が開く場所, 120x68 の座標) を 16x16 のマスに分けて平均 → 平均より明るいマスが 1 の 1 ビット画像
//   + 数値 3 つ (平均 / 128, ばらつき / 64 - 1, 横の明るさの変化 / 16)
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

namespace edge_win {

constexpr int FW = 240, FH = 135;  // 液晶に出る画面
constexpr int BW = 120, BH = 136;  // バッファ (行 135 は使わない)
constexpr int GW = 120, GH = 68;   // スロットの座標
constexpr int N = 16;

inline uint8_t gray565(uint16_t p) {
  uint32_t r = p >> 11 & 31, g = p >> 5 & 63, b = p & 31;
  uint32_t r8 = (r << 3) | (r >> 2), g8 = (g << 2) | (g >> 4), b8 = (b << 3) | (b >> 2);
  return (uint8_t)((77 * r8 + 150 * g8 + 29 * b8) >> 8);
}

// 液晶に描いた w x h の画素 (1 行 stride 画素) をバッファへ。x は偶数 (8x8 ブロック・JPEG の MCU・行全体)
inline void feedBlock(uint8_t* buf, int x, int y, int w, int h, const uint16_t* px, int stride) {
  if (!buf) return;
  int sx = 0;
  if (x & 1) sx = 1;  // 奇数から始まることは無いはずだが、来たら 1 画素捨てて偶数にそろえる
  for (int r = 0; r < h; r++) {
    int yy = y + r;
    if (yy < 0 || yy >= FH) continue;
    const uint16_t* p = px + r * stride;
    uint8_t* d = buf + yy * BW;
    for (int c = sx; c + 1 < w; c += 2) {
      int xx = x + c;
      if (xx < 0 || xx + 1 >= FW) continue;
      d[xx >> 1] = (uint8_t)((gray565(p[c]) + gray565(p[c + 1])) >> 1);
    }
  }
}

// 1 色で塗った 8 x h のブロック (BlockDiff)
inline void feedFill(uint8_t* buf, int x, int y, int w, int h, uint16_t color) {
  if (!buf) return;
  uint8_t g = gray565(color);
  for (int r = 0; r < h; r++) {
    int yy = y + r;
    if (yy < 0 || yy >= FH) continue;
    for (int c = (x & 1); c + 1 < w; c += 2) {
      int xx = x + c;
      if (xx >= 0 && xx + 1 < FW) buf[yy * BW + (xx >> 1)] = g;
    }
  }
}

// 120x68 の 1 画素 (縦 2 行の平均。最後の組は行 134 を 2 回)
inline int halfAt(const uint8_t* buf, int gx, int gy) {
  int y0 = gy * 2, y1 = gy * 2 + 1;
  if (y1 >= FH) y0 = y1 = FH - 1;
  return (buf[y0 * BW + gx] + buf[y1 * BW + gx]) >> 1;
}

// スロット (x, y, w, h は 120x68 の座標、画面の中) の特徴。bits は 32 バイト (1 行 2 バイト, LSB = 左)
inline void slotFeatures(const uint8_t* buf, int x, int y, int w, int h, uint8_t bits[32], float feats[3]) {
  int32_t cells[N][N];
  int64_t total = 0;
  for (int i = 0; i < N; i++) {
    int y0 = y + i * h / N, y1 = y + (i + 1) * h / N;
    if (y1 <= y0) y1 = y0 + 1;
    for (int j = 0; j < N; j++) {
      int x0 = x + j * w / N, x1 = x + (j + 1) * w / N;
      if (x1 <= x0) x1 = x0 + 1;
      int32_t s = 0;
      for (int yy = y0; yy < y1; yy++)
        for (int xx = x0; xx < x1; xx++) s += halfAt(buf, xx, yy);
      cells[i][j] = s / ((y1 - y0) * (x1 - x0));
      total += cells[i][j];
    }
  }
  float m = (float)total / (float)(N * N);
  float var = 0, tex = 0;
  memset(bits, 0, 32);
  for (int i = 0; i < N; i++)
    for (int j = 0; j < N; j++) {
      float d = (float)cells[i][j] - m;
      var += d * d;
      if ((float)cells[i][j] > m) bits[i * 2 + j / 8] |= (uint8_t)(1 << (j % 8));
      if (j + 1 < N) tex += (float)abs(cells[i][j + 1] - cells[i][j]);
    }
  float sd = sqrtf(var / (float)(N * N));
  feats[0] = m / 128.f;
  feats[1] = sd / 64.f - 1.f;
  feats[2] = tex / (float)(N * (N - 1)) / 16.f;
}

}  // namespace edge_win
