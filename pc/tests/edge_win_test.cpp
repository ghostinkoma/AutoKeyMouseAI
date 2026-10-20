// ESP32 の窓の認識 (firmware/AutoKeyMouse/edge_win_core.h + edge_nn.h + edge_win_model.h) を PC で動かすテスト。
// モデルは -I で渡したフォルダの edge_win_model.h (テストが作る)。
// 標準入力: 液晶の RGB565 (135x240, little endian) → 液晶と同じく 8x8 ブロックで流し込み、
// スロットごとに 1 行「答えの番号 確率 ビット(16 進 64 文字) 数値 3 つ」
#include <cstdio>

#include "edge_nn.h"
#include "edge_win_core.h"
#include "edge_win_model.h"

int main() {
  static uint16_t frame[edge_win::FH][edge_win::FW];
  if (fread(frame, 2, edge_win::FH * edge_win::FW, stdin) != (size_t)(edge_win::FH * edge_win::FW)) return 1;
  static uint8_t buf[edge_win::BW * edge_win::BH];
  for (int by = 0; by * 8 < edge_win::FH; by++)
    for (int bx = 0; bx < edge_win::FW / 8; bx++) {
      uint16_t px[64];
      int rows = edge_win::FH - by * 8 > 8 ? 8 : edge_win::FH - by * 8;  // 最後は 7 行 (bad16 と同じ)
      for (int y = 0; y < rows; y++)
        for (int x = 0; x < 8; x++) px[y * 8 + x] = frame[by * 8 + y][bx * 8 + x];
      edge_win::feedBlock(buf, bx * 8, by * 8, 8, rows, px, 8);
    }
  for (int i = 0; i < edge_win::NSLOT; i++) {
    const auto& s = edge_win::SLOTS[i];
    uint8_t bits[32];
    float feats[3], conf;
    edge_win::slotFeatures(buf, s.x, s.y, s.w, s.h, bits, feats);
    int k = edge_nn::classify<edge_win::Model>(bits, feats, &conf);
    printf("%d %.4f ", k, conf);
    for (int b = 0; b < 32; b++) printf("%02x", bits[b]);
    printf(" %.6f %.6f %.6f\n", feats[0], feats[1], feats[2]);
  }
  return 0;
}
