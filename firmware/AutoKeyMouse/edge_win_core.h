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

// ---- モデル (PC の pc/akm/edge/winmodel.py pack_model と同じ形式。little endian) ----
//   "AKW1" u8 版=1, u8 K, u16 H, u8 NSLOT, 3 バイト空き, f32 S1 SH S2 THR,
//   i8 W1Q[256*H], f32 W1F[3*H], f32 B1[H], i8 W2Q[H*K], f32 B2[K],
//   スロット NSLOT x {u8 x y w h, u32 mask}, 名前 K x 24 バイト, u32 CRC32 (それより前の全部)
constexpr int NAME_LEN = 24, MAX_H = 128, MAX_K = 32, MAX_SLOT = 32;

struct SlotDef {
  uint8_t x, y, w, h;
  uint32_t mask;  // そこに開く窓 (名前の番号のビット)
};

struct Model {
  const uint8_t* data = nullptr;  // 受け取ったバイト列 (持ち主は呼び出し側。モデルを使う間は消さない)
  int H = 0, K = 0, nslot = 0;
  float S1 = 0, SH = 0, S2 = 0, thr = 0.6f;
  const int8_t* W1Q = nullptr;
  const uint8_t *W1F = nullptr, *B1 = nullptr, *B2 = nullptr;  // float (境界がそろっていないので memcpy で読む)
  const int8_t* W2Q = nullptr;
  SlotDef slots[MAX_SLOT];
  const char* names = nullptr;  // K x NAME_LEN
  uint32_t id = 0;              // CRC32
  bool ok() const { return data && K > 0 && nslot > 0; }
  const char* name(int k) const { return names + k * NAME_LEN; }
};

inline uint32_t crc32(const uint8_t* p, size_t n) {
  uint32_t c = 0xFFFFFFFFu;
  for (size_t i = 0; i < n; i++) {
    c ^= p[i];
    for (int b = 0; b < 8; b++) c = (c >> 1) ^ (0xEDB88320u & (0u - (c & 1)));
  }
  return ~c;
}

inline float rdf(const uint8_t* p, int i) {
  float v;
  memcpy(&v, p + 4 * i, 4);
  return v;
}

// 壊れていたら false (m は変えない)
inline bool parseModel(const uint8_t* d, size_t len, Model& out) {
  if (len < 32 + 4 || memcmp(d, "AKW1", 4) != 0 || d[4] != 1) return false;
  uint32_t crc;
  memcpy(&crc, d + len - 4, 4);
  if (crc32(d, len - 4) != crc) return false;
  Model m;
  m.data = d;
  m.K = d[5];
  m.H = d[6] | d[7] << 8;
  m.nslot = d[8];
  if (m.K < 1 || m.K > MAX_K || m.H < 1 || m.H > MAX_H || m.nslot > MAX_SLOT) return false;
  m.S1 = rdf(d + 12, 0), m.SH = rdf(d + 12, 1), m.S2 = rdf(d + 12, 2), m.thr = rdf(d + 12, 3);
  size_t o = 28;
  size_t need = o + 256 * m.H + 4 * 3 * m.H + 4 * m.H + m.H * m.K + 4 * m.K + 8 * m.nslot + NAME_LEN * m.K + 4;
  if (need != len) return false;
  m.W1Q = (const int8_t*)(d + o), o += 256 * m.H;
  m.W1F = d + o, o += 4 * 3 * m.H;
  m.B1 = d + o, o += 4 * m.H;
  m.W2Q = (const int8_t*)(d + o), o += m.H * m.K;
  m.B2 = d + o, o += 4 * m.K;
  for (int i = 0; i < m.nslot; i++, o += 8) {
    SlotDef& s = m.slots[i];
    s.x = d[o], s.y = d[o + 1], s.w = d[o + 2], s.h = d[o + 3];
    memcpy(&s.mask, d + o + 4, 4);
    if (s.w == 0 || s.h == 0 || s.x + s.w > GW || s.y + s.h > GH) return false;
  }
  m.names = (const char*)(d + o);
  for (int k = 0; k < m.K; k++)
    if (m.names[k * NAME_LEN + NAME_LEN - 1] != 0) return false;
  m.id = crc;
  out = m;
  return true;
}

// edge_nn::classify と同じ計算 (モデルを後から差し替えられる版)。PC の QModel.logits と同じ答え
inline int classify(const Model& M, const uint8_t bits[32], const float feats[3], float* conf) {
  int32_t acc[MAX_H] = {0};
  for (int byte = 0; byte < 32; byte++) {
    uint8_t v = bits[byte];
    while (v) {
      int b = __builtin_ctz(v);
      v &= v - 1;
      const int8_t* w = M.W1Q + (byte * 8 + b) * M.H;
      for (int j = 0; j < M.H; j++) acc[j] += w[j];
    }
  }
  auto clip = [](float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); };
  float f[3] = {clip(feats[0], 0.f, 2.f) - 1.f, clip(feats[1], -1.f, 1.f), clip(feats[2], 0.f, 3.f) / 1.5f - 1.f};
  uint8_t hq[MAX_H];
  for (int j = 0; j < M.H; j++) {
    float h = (float)acc[j] * M.S1 + f[0] * rdf(M.W1F, j) + f[1] * rdf(M.W1F, M.H + j) +
              f[2] * rdf(M.W1F, 2 * M.H + j) + rdf(M.B1, j);
    float q = floorf((h > 0 ? h : 0) / M.SH + 0.5f);
    hq[j] = q > 255 ? 255 : (uint8_t)q;
  }
  int best = 0;
  float bestV = -1e30f, logits[MAX_K];
  for (int k = 0; k < M.K; k++) {
    int32_t a = 0;
    for (int j = 0; j < M.H; j++) a += (int32_t)hq[j] * M.W2Q[j * M.K + k];
    logits[k] = (float)a * (M.S2 * M.SH) + rdf(M.B2, k);
    if (logits[k] > bestV) bestV = logits[k], best = k;
  }
  if (conf) {
    float s = 0;
    for (int k = 0; k < M.K; k++) s += expf(logits[k] - bestV);
    *conf = 1.f / s;
  }
  return best;
}

}  // namespace edge_win
