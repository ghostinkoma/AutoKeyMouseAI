// ESP32 の推論 (firmware/AutoKeyMouse/edge_nn.h + edge_ocr_model.h) を PC で動かすテスト。
// 標準入力: [32 バイトのビット][float 3 つ] の並び → 1 行に「答えの番号 確率」
#include <cstdio>

#include "../../firmware/AutoKeyMouse/edge_nn.h"
#include "../../firmware/AutoKeyMouse/edge_ocr_model.h"

int main() {
  uint8_t bits[32];
  float feats[3];
  while (fread(bits, 1, 32, stdin) == 32 && fread(feats, sizeof(float), 3, stdin) == 3) {
    float conf;
    int k = edge_nn::classify<edge_ocr::Model>(bits, feats, &conf);
    printf("%d %.4f\n", k, conf);
  }
  return 0;
}
