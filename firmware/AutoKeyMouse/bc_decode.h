#pragma once
// BlockDiff のデコード本体 (液晶にもテスト用の配列にも描けるよう、描画先をテンプレートにしている)。
// 形式は pc/akm/bcodec.py を参照。PC の単体テスト (pc/tests/bc_decode_test.cpp) でも同じものを使う。
#include <stddef.h>
#include <stdint.h>

namespace bc {

constexpr int W = 240, H = 135, BW = W / 8, BH = (H + 7) / 8, TOTAL = BW * BH;

typedef bool (*ReadFn)(void* ctx, uint8_t* dst, size_t n);

// Sink: void fill(int x, int y, int h, uint16_t c);  void block(int x, int y, int h, const uint16_t* px64);
// 戻り値: 0 = 失敗, 1 = 差分フレーム, 2 = キーフレーム
template <typename Sink>
int decode(ReadFn read, void* ctx, Sink& sink) {
  uint8_t hdr[4];
  if (!read(ctx, hdr, 4) || hdr[0] != 'B' || hdr[1] != 'C' || hdr[2] != 1) return 0;
  bool key = hdr[3] & 1;
  uint16_t px[64];
  uint8_t b[24];
  int bi = 0;
  auto pos = [](int i, int& x, int& y, int& h) {
    x = (i % BW) * 8;
    y = (i / BW) * 8;
    h = y + 8 > H ? H - y : 8;  // 最下段は 7 行
  };
  while (bi < TOTAL) {
    uint8_t op;
    if (!read(ctx, &op, 1)) return 0;
    int x, y, h;
    if (op < 0x40) {  // SKIP
      bi += op + 1;
    } else if (op < 0x80) {  // FILL n ブロック
      if (!read(ctx, b, 2)) return 0;
      uint16_t c = b[0] << 8 | b[1];
      for (int k = 0; k <= (op & 0x3F) && bi < TOTAL; ++k, ++bi) {
        pos(bi, x, y, h);
        sink.fill(x, y, h, c);
      }
    } else if (op == 0x80) {  // 2 色
      if (!read(ctx, b, 12)) return 0;
      uint16_t pal[2] = {(uint16_t)(b[0] << 8 | b[1]), (uint16_t)(b[2] << 8 | b[3])};
      for (int r = 0; r < 8; ++r)
        for (int c = 0; c < 8; ++c) px[r * 8 + c] = pal[(b[4 + r] >> (7 - c)) & 1];
      pos(bi++, x, y, h);
      sink.block(x, y, h, px);
    } else if (op == 0x81) {  // 4 色
      if (!read(ctx, b, 24)) return 0;
      uint16_t pal[4];
      for (int t = 0; t < 4; ++t) pal[t] = b[2 * t] << 8 | b[2 * t + 1];
      for (int r = 0; r < 8; ++r) {
        uint16_t w = b[8 + 2 * r] << 8 | b[9 + 2 * r];
        for (int c = 0; c < 8; ++c) px[r * 8 + c] = pal[(w >> (14 - 2 * c)) & 3];
      }
      pos(bi++, x, y, h);
      sink.block(x, y, h, px);
    } else if (op == 0x82) {  // RAW (ビッグエンディアン)
      uint8_t* raw = (uint8_t*)px;
      if (!read(ctx, raw, 128)) return 0;
      for (int k = 0; k < 64; ++k) px[k] = raw[2 * k] << 8 | raw[2 * k + 1];  // 前から上書きしても安全
      pos(bi++, x, y, h);
      sink.block(x, y, h, px);
    } else {
      return 0;
    }
  }
  return key ? 2 : 1;
}

}  // namespace bc
