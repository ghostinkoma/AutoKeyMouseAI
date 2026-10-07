#pragma once
// ESP32 で動かす小さなニューラルネット (エッジ AI) の推論。学習は PC (pc/tools/edge_train.py)。
//
// 入力: 16x16 の 1 ビット画像 (32 バイト、行ごとに 2 バイト、LSB = 左) + 数値 3 つ
//   1 層目: 1 の画素の重み (int8) を足すだけ。掛け算なし (BadCodec のビット面をそのまま入れられる)
//   2 層目: int8 の重み × uint8 の隠れ層 → 一番大きいものが答え
// モデルは namespace (例: edge_ocr_model.h の edge_ocr) を テンプレート引数ではなく
// マクロで渡す代わりに、同じ名前の定数を持つ構造体として扱う:
//     int k = edge_nn::classify<edge_ocr::Model>(bits, feats, &conf);
#include <math.h>
#include <stdint.h>

namespace edge_nn {

// PC (akm/edge/mlp.py feat_norm) と同じ式
inline void featNorm(const float in[3], float out[3]) {
  auto clip = [](float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); };
  out[0] = clip(in[0], 0.f, 2.f) - 1.f;           // 行の中での高さ比
  out[1] = clip(in[1], -1.f, 1.f);                // 上下位置
  out[2] = clip(in[2], 0.f, 3.f) / 1.5f - 1.f;    // 縦横比
}

// 戻り値: 答えの番号 (M::LABELS[k])。conf があれば確率 (0..1) を入れる
template <typename M>
int classify(const uint8_t bits[32], const float feats[3], float* conf = nullptr) {
  int32_t acc[M::H] = {0};
  for (int byte = 0; byte < 32; byte++) {
    uint8_t v = bits[byte];
    while (v) {
      int b = __builtin_ctz(v);
      v &= v - 1;
      const int8_t* w = M::W1Q + (byte * 8 + b) * M::H;
      for (int j = 0; j < M::H; j++) acc[j] += w[j];
    }
  }
  float f[3];
  featNorm(feats, f);
  uint8_t hq[M::H];
  for (int j = 0; j < M::H; j++) {
    float h = (float)acc[j] * M::S1 + f[0] * M::W1F[j] + f[1] * M::W1F[M::H + j] + f[2] * M::W1F[2 * M::H + j] +
              M::B1[j];
    float q = floorf((h > 0 ? h : 0) / M::SH + 0.5f);
    hq[j] = q > 255 ? 255 : (uint8_t)q;
  }
  int best = 0;
  float bestV = -1e30f, logits[M::K];
  for (int k = 0; k < M::K; k++) {
    int32_t a = 0;
    for (int j = 0; j < M::H; j++) a += (int32_t)hq[j] * M::W2Q[j * M::K + k];
    logits[k] = (float)a * (M::S2 * M::SH) + M::B2[k];
    if (logits[k] > bestV) bestV = logits[k], best = k;
  }
  if (conf) {
    float s = 0;
    for (int k = 0; k < M::K; k++) s += expf(logits[k] - bestV);
    *conf = 1.f / s;
  }
  return best;
}

}  // namespace edge_nn
