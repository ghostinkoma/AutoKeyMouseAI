// 自動生成: pc/tools/edge_train.py / edge_objects.py / window_train.py (まだ学習していない空のモデル。window_train.py train で置き換わる)。手で編集しない
// 推論は edge_nn.h
#pragma once
#include <stdint.h>
namespace edge_win {
constexpr int NB = 256, NF = 3, H = 1, K = 1;
constexpr float S1 = 1.0f, SH = 1.0f, S2 = 1.0f;
static const char LABELS[] = "?";  // K 文字。最後の '?' は「文字ではない」
static const int8_t W1Q[NB * H] = {0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0};
static const float W1F[NF * H] = {0.0f,0.0f,0.0f};
static const float B1[H] = {0.0f};
static const int8_t W2Q[H * K] = {0};
static const float B2[K] = {0.0f};
// edge_nn::classify<edge_win::Model>(bits, feats, &conf) で使う
struct Model {
  static constexpr int H = edge_win::H, K = edge_win::K;
  static constexpr float S1 = edge_win::S1, SH = edge_win::SH, S2 = edge_win::S2;
  static constexpr const int8_t* W1Q = edge_win::W1Q;
  static constexpr const float* W1F = edge_win::W1F;
  static constexpr const float* B1 = edge_win::B1;
  static constexpr const int8_t* W2Q = edge_win::W2Q;
  static constexpr const float* B2 = edge_win::B2;
};
// 窓の名前 (答えの番号順。最後は "none" = 窓は無い)
static const char* const NAMES[K] = {"none"};
constexpr int NONE = K - 1;
constexpr float THRESHOLD = 0.6f;
// 窓が開く場所 (120x68 の座標) と、そこに開く窓 (NAMES の番号のビット)
struct Slot {
  uint8_t x, y, w, h;
  uint32_t mask;
};
constexpr int NSLOT = 0;
static const Slot SLOTS[1] = {
  {0, 0, 1, 1, 0u},
};
}  // namespace edge_win
