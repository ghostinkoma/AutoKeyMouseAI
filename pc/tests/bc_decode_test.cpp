// ESP32 の BadCodec-C デコーダ (firmware/AutoKeyMouse/bc_decode.h) を PC で動かすテスト用プログラム。
// 標準入力: [長さ u32 LE][フレーム] の並び。フレームごとに 240x135 の RGB565 (LE) を標準出力へ書く。
#include <cstdio>
#include <cstring>
#include <vector>

#include "../../firmware/AutoKeyMouse/bc_decode.h"

static uint16_t fb[bc::H][bc::W];

struct Sink {
  void fill(int x, int y, int h, uint16_t c) {
    for (int r = 0; r < h; ++r)
      for (int k = 0; k < 8; ++k) fb[y + r][x + k] = c;
  }
  void block(int x, int y, int h, const uint16_t* px) {
    for (int r = 0; r < h; ++r)
      for (int k = 0; k < 8; ++k) fb[y + r][x + k] = px[r * 8 + k];
  }
};

struct Mem {
  const uint8_t* p;
  size_t left;
};

static bool rd(void* ctx, uint8_t* dst, size_t n) {
  Mem* m = (Mem*)ctx;
  if (n > m->left) return false;
  memcpy(dst, m->p, n);
  m->p += n;
  m->left -= n;
  return true;
}

int main() {
  uint32_t len;
  while (fread(&len, 4, 1, stdin) == 1) {
    std::vector<uint8_t> buf(len);
    if (fread(buf.data(), 1, len, stdin) != len) return 2;
    Mem m{buf.data(), len};
    Sink s;
    int r = bc::decode(rd, &m, s);
    if (r == 0 || m.left != 0) {
      fprintf(stderr, "decode failed r=%d left=%zu\n", r, m.left);
      return 1;
    }
    fwrite(fb, sizeof(fb), 1, stdout);
  }
  return 0;
}
