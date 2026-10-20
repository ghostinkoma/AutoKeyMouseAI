// ESP32 の窓の認識 (firmware/AutoKeyMouse/edge_win_core.h) を PC で動かすテスト。
// 引数: モデルのファイル (pc/akm/edge/winmodel.py pack_model)。壊れていれば "bad model" で終了 2。
// 標準入力: 液晶の RGB565 (135x240, little endian) → 液晶と同じく 8x8 ブロックで流し込み、
// スロットごとに 1 行「答えの番号 確率 ビット(16 進 64 文字) 数値 3 つ」
#include <cstdio>
#include <vector>

#include "edge_win_core.h"

int main(int argc, char** argv) {
  if (argc < 2) return 1;
  FILE* f = fopen(argv[1], "rb");
  if (!f) return 1;
  std::vector<uint8_t> blob;
  int c;
  while ((c = fgetc(f)) != EOF) blob.push_back((uint8_t)c);
  fclose(f);
  edge_win::Model m;
  if (!edge_win::parseModel(blob.data(), blob.size(), m) || !m.ok()) {
    printf("bad model\n");
    return 2;
  }
  printf("id %08lx K %d H %d slots %d name0 %s\n", (unsigned long)m.id, m.K, m.H, m.nslot, m.name(0));
  static uint16_t frame[edge_win::FH][edge_win::FW];
  if (fread(frame, 2, edge_win::FH * edge_win::FW, stdin) != (size_t)(edge_win::FH * edge_win::FW)) return 1;
  static edge_win::Gray buf, rows;
  for (int by = 0; by * 8 < edge_win::FH; by++)
    for (int bx = 0; bx < edge_win::FW / 8; bx++) {
      uint16_t px[64];
      int n = edge_win::FH - by * 8 > 8 ? 8 : edge_win::FH - by * 8;  // 最後は 7 行 (bad16 と同じ)
      for (int y = 0; y < n; y++)
        for (int x = 0; x < 8; x++) px[y * 8 + x] = frame[by * 8 + y][bx * 8 + x];
      edge_win::feedBlock(&buf, bx * 8, by * 8, 8, n, px, 8);
    }
  // 1 行ずつ来ても (bad16 の面の版・RGB565) 同じ明るさの画面になる
  for (int y = 0; y < edge_win::FH; y++) edge_win::feedBlock(&rows, 0, y, edge_win::FW, 1, frame[y], edge_win::FW);
  if (memcmp(buf.half, rows.half, sizeof(buf.half)) != 0) {
    printf("rows differ\n");
    return 3;
  }
  // 端が画面からはみ出したブロック (JPEG の MCU) でも壊れない
  edge_win::feedBlock(&rows, 232, 128, 16, 16, frame[0], edge_win::FW);
  for (int i = 0; i < m.nslot; i++) {
    const auto& s = m.slots[i];
    uint8_t bits[32];
    float feats[3], conf;
    edge_win::slotFeatures(buf, s.x, s.y, s.w, s.h, bits, feats);
    int k = edge_win::classify(m, bits, feats, &conf);
    printf("%d %.4f ", k, conf);
    for (int b = 0; b < 32; b++) printf("%02x", bits[b]);
    printf(" %.6f %.6f %.6f\n", feats[0], feats[1], feats[2]);
  }
  return 0;
}
