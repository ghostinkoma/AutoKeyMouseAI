#pragma once
// BadCodec 16bit 版のデコード (pc/akm/bad16.py のコンテナ)。
// RGB565 の 16 ビット面を、公式 BadCodec デコーダ (bad_decode.cpp, Protocol 514) で 1 面ずつ展開する。
// 液晶にも PC のテストにも使えるよう、描画は呼び出し側に任せる (compose)。
//
// メモリ: 16 面 x 4080B (前フレーム) + 作業用 4080B = 約 69KB。ESP32-S3 (PSRAM) 向け。
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "bad_decode.h"

namespace bad16 {

constexpr int W = 240, H = 136, ROWS = 135;
constexpr uint16_t PLANE = W * H / 8;  // 4080

typedef bool (*ReadFn)(void* ctx, uint8_t* dst, size_t n);

struct State {
  uint8_t* planes[16] = {};
  uint8_t* scratch = nullptr;
  bool ready = false;
};

namespace detail {
// 公式デコーダの read コールバックは offset 指定なので、ヘッダーは作って返し、本体は受信から順に流す
inline uint8_t* hdr() {
  static uint8_t h[BAD_HEADER_SIZE];
  return h;
}
struct Src {
  ReadFn read;
  void* ctx;
  uint32_t left;  // この面の残りバイト
  bool err;
};
inline Src*& cur() {
  static Src* s = nullptr;
  return s;
}
inline uint16_t readCb(bad_addr_t off, uint8_t* buf, uint16_t len) {
  if (off < BAD_HEADER_SIZE) {
    uint16_t n = (uint16_t)((off + len <= BAD_HEADER_SIZE) ? len : BAD_HEADER_SIZE - off);
    memcpy(buf, hdr() + off, n);
    return n;
  }
  Src* s = cur();
  if (!s || len > s->left || !s->read(s->ctx, buf, len)) {
    if (s) s->err = true;
    return 0;
  }
  s->left -= len;
  return len;
}
inline void makeHeader() {
  uint8_t* h = hdr();
  const uint8_t body[15] = {'B', 'a', 'd', 514 & 0xFF, 514 >> 8, 2, 0, W & 0xFF, W >> 8, H & 0xFF, H >> 8, 8, 0, 0xFF, 0xFF};
  uint8_t s1 = 0, s2 = 0;
  for (int i = 0; i < 15; i++) {
    s1 += body[i];
    s2 += s1;
  }
  h[0] = BAD_HEADER_SIZE;
  h[1] = 0;
  h[2] = s1;
  h[3] = s2;
  memcpy(h + 4, body, 15);
}
}  // namespace detail

// alloc: 17 x 4080 バイト確保する関数 (PSRAM 優先など)。失敗したら false
inline bool init(State& s, void* (*alloc)(size_t)) {
  if (s.ready) return true;
  for (int b = 0; b < 16; b++) {
    s.planes[b] = (uint8_t*)alloc(PLANE);
    if (!s.planes[b]) return false;
    memset(s.planes[b], 0, PLANE);
  }
  s.scratch = (uint8_t*)alloc(PLANE);
  if (!s.scratch) return false;
  detail::makeHeader();
  s.ready = true;
  return true;
}

// 1 フレーム (コンテナ) を展開して 16 面を更新する。
// 戻り値: 0 = 失敗, 1 = 差分, 2 = キーフレーム, 3 = 全面 SKIP_FRAME (絵は変わらない)
inline int decode(State& s, ReadFn read, void* ctx) {
  if (!s.ready) return 0;
  uint8_t h[4];
  if (!read(ctx, h, 4) || h[0] != 'B' || h[1] != '6' || h[2] != 1) return 0;
  bool key = h[3] & 1;
  bool all_skip = !key;
  bad_ctx_t bc;
  memset(&bc, 0, sizeof(bc));
  bc.read = detail::readCb;
  bc.buf_size = PLANE;
  for (int b = 0; b < 16; b++) {
    uint8_t lb[2];
    if (!read(ctx, lb, 2)) return 0;
    uint32_t len = lb[0] | lb[1] << 8;
    if (key) memset(s.planes[b], 0, PLANE);  // キーフレームは 0 からの差分
    detail::Src src{read, ctx, len, false};
    detail::cur() = &src;
    // 作業用 (gram) に前フレームを入れておく: 公式デコーダは最初に gram → prev をコピーするため
    memcpy(s.scratch, s.planes[b], PLANE);
    bc.gram = s.scratch;
    bc.prev = s.planes[b];
    bc.width = W;
    bc.height = H;
    bc.total_frames = 0xFFFF;
    bc.current_frame = 0;
    bc.stream_offset = BAD_HEADER_SIZE;
    bc.initialized = 1;
    bad_result_t r = bad_next_frame(&bc);
    detail::cur() = nullptr;
    if ((r != BAD_OK && r != BAD_EOF) || src.err) return 0;
    while (src.left) {  // 念のため残りを読み捨てる
      uint8_t junk[32];
      uint32_t k = src.left < sizeof(junk) ? src.left : sizeof(junk);
      if (!read(ctx, junk, k)) return 0;
      src.left -= k;
    }
    if (memcmp(s.scratch, s.planes[b], PLANE) != 0) all_skip = false;
    memcpy(s.planes[b], s.scratch, PLANE);
  }
  return key ? 2 : (all_skip ? 3 : 1);
}

// 16 面から RGB565 の行を組み立てて sink(y, row) に渡す (y = 0..rows-1, row は 240 画素)
template <typename Sink>
inline void compose(const State& s, int rows, Sink sink) {
  static uint16_t row[W];
  for (int y = 0; y < rows; y++) {
    int base = y * (W / 8);
    for (int xb = 0; xb < W / 8; xb++) {
      uint16_t px[8] = {0, 0, 0, 0, 0, 0, 0, 0};
      for (int b = 0; b < 16; b++) {
        uint8_t v = s.planes[b][base + xb];  // LSB = 左 (公式デコーダの並び)
        if (!v) continue;
        uint16_t bit = (uint16_t)(1u << b);
        for (int k = 0; k < 8; k++)
          if (v >> k & 1) px[k] |= bit;
      }
      memcpy(row + xb * 8, px, sizeof(px));
    }
    sink(y, row);
  }
}

}  // namespace bad16
