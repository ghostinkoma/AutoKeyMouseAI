// ESP32 の BadCodec 16bit デコーダ (firmware/AutoKeyMouse/bad16.h + 公式 bad_decode.cpp) を PC で動かすテスト。
// 標準入力: [長さ u32 LE][コンテナ] の並び。フレームごとに 240x135 の RGB565 (LE) を標準出力へ。
#include <cstdio>
#include <cstdlib>
#include <vector>

#include "../../firmware/AutoKeyMouse/bad16.h"
#include "../../firmware/AutoKeyMouse/entropy.h"

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
  // ESP32 と同じ流れ: キーフレームで面の組み合わせ (mask) が変わったら置き場所を用意し直す
  static bad16::State st;
  static uint8_t pool[17][bad16::PLANE];
  static uint16_t fb[bad16::ROWS][bad16::W];
  uint32_t len;
  while (fread(&len, 4, 1, stdin) == 1) {
    std::vector<uint8_t> buf(len);
    if (fread(buf.data(), 1, len, stdin) != len) return 2;
    Mem m{buf.data(), len};
    // 'B' 'A': tANS を戻しながら、中の BadCodec のフレームを読む (ESP32 と同じ)
    bad16::ReadFn rdf = rd;
    void* rctx = &m;
    if (len >= 2 && buf[0] == 'B' && buf[1] == 'A') {
      uint8_t h2[2];
      rd(&m, h2, 2);
      if (!ent::begin(ent::dec(), buf[1], rd, &m, len)) {
        fprintf(stderr, "entropy header failed\n");
        return 1;
      }
      rdf = ent::read;
      rctx = &ent::dec();
    }
    bad16::Header h;
    int r = 0;
    bool abs = false;
    if (bad16::readHeader(rdf, rctx, h)) {
      if (h.abs) {  // ver 3 / 7: 前フレーム無しで変わったブロックだけ fb (= 液晶) に描く
        abs = true;
        auto sink = [&](int bx, int by, int rows, const uint16_t* px) {
          for (int y = 0; y < rows; y++) memcpy(&fb[by * 8 + y][bx * 8], px + y * 8, 16);
        };
        r = bad16::decodeAbs(h, rdf, rctx, sink) >= 0 ? 1 : 0;
      } else {
        if (!(st.ready && st.mask == h.mask) && h.key) bad16::setup(st, h.mask, [](int i) { return pool[i]; });
        r = bad16::decodeBody(st, h, rdf, rctx);
      }
    }
    if (rctx != &m) {  // 符号の残り (詰め物) を捨てる
      if (ent::dec().outLeft) r = 0;
      m.left = 0;
    }
    if (r == 0 || m.left) {
      fprintf(stderr, "decode failed r=%d left=%zu\n", r, m.left);
      return 1;
    }
    if (!abs)
      bad16::compose(st, bad16::ROWS, [&](int y, const uint16_t* row) { memcpy(fb[y], row, sizeof(fb[y])); });
    fwrite(fb, sizeof(fb), 1, stdout);
    fprintf(stderr, "r=%d planes=%d\n", r, bad16::planesIn(st.mask));
  }
  return 0;
}
