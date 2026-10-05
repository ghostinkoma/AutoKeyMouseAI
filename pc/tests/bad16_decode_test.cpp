// ESP32 の BadCodec 16bit デコーダ (firmware/AutoKeyMouse/bad16.h + 公式 bad_decode.cpp) を PC で動かすテスト。
// 標準入力: [長さ u32 LE][コンテナ] の並び。フレームごとに 240x135 の RGB565 (LE) を標準出力へ。
#include <cstdio>
#include <cstdlib>
#include <vector>

#include "../../firmware/AutoKeyMouse/bad16.h"

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
  static bad16::State st;
  if (!bad16::init(st, malloc)) return 3;
  static uint16_t fb[bad16::ROWS][bad16::W];
  uint32_t len;
  while (fread(&len, 4, 1, stdin) == 1) {
    std::vector<uint8_t> buf(len);
    if (fread(buf.data(), 1, len, stdin) != len) return 2;
    Mem m{buf.data(), len};
    int r = bad16::decode(st, rd, &m);
    if (r == 0 || m.left) {
      fprintf(stderr, "decode failed r=%d left=%zu\n", r, m.left);
      return 1;
    }
    bad16::compose(st, bad16::ROWS, [&](int y, const uint16_t* row) { memcpy(fb[y], row, sizeof(fb[y])); });
    fwrite(fb, sizeof(fb), 1, stdout);
    fprintf(stderr, "r=%d\n", r);
  }
  return 0;
}
