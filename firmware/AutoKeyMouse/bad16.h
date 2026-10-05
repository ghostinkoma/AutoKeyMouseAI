#pragma once
// BadCodec 16bit 版のデコード (pc/akm/bad16.py のコンテナ)。
// RGB565 の 16 ビット面を、公式 BadCodec デコーダ (bad_decode.cpp, Protocol 514) で 1 面ずつ展開する。
// 液晶にも PC のテストにも使えるよう、描画は呼び出し側に任せる (compose)。
//
// ver 3 (decodeAbs) は前フレームを持たず、変わったブロックをそのまま液晶へ描く (メモリ 128B)。
// 以下は ver 1/2 (面ごとのフレーム。前フレームが要る):
// メモリ: (送られてくる面の数 + 作業用 1) x 4080B。16 面 (可逆) で約 69KB (ESP32-S3 + PSRAM 向け)、
// 下位ビットを捨てると減る: drop 1 = 13 面 57KB / drop 2 = 10 面 45KB / drop 3 = 7 面 33KB。
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "bad_decode.h"

namespace bad16 {

constexpr int W = 240, H = 136, ROWS = 135;
constexpr uint16_t PLANE = W * H / 8;  // 4080

typedef bool (*ReadFn)(void* ctx, uint8_t* dst, size_t n);

struct State {
  uint8_t* planes[16] = {};  // 送られてこない面 (下位ビットを捨てた面) は nullptr = 常に 0
  uint8_t* scratch = nullptr;
  uint16_t mask = 0;
  bool ready = false;
  int dirtyTop = 0, dirtyBottom = -1;  // 直前の decodeBody で変わった行 (y の範囲。無ければ top > bottom)
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

// コンテナの先頭
//   ver 1: 'B' '6' 1 flags                … 16 面すべて、面ごとの BadCodec フレーム (前フレームが要る)
//   ver 2: 'B' '6' 2 flags mask(u16 LE)   … mask の面だけ、面ごとの BadCodec フレーム (前フレームが要る)
//   ver 3: 'B' '6' 3 flags mask(u16 LE)   … 前フレーム不要のブロックストリーム (decodeAbs)
struct Header {
  bool key = false;
  bool abs = false;        // ver 3
  uint16_t mask = 0xFFFF;  // 送られてくるビット面 (bit b = RGB565 の bit b)。無い面は常に 0
};

inline int planesIn(uint16_t mask) {
  int n = 0;
  for (; mask; mask &= mask - 1) n++;
  return n;
}

inline bool readHeader(ReadFn read, void* ctx, Header& h) {
  uint8_t b[4];
  if (!read(ctx, b, 4) || b[0] != 'B' || b[1] != '6') return false;
  h.key = b[3] & 1;
  if (b[2] == 1) {
    h.mask = 0xFFFF;
    return true;
  }
  if (b[2] != 2 && b[2] != 3) return false;
  h.abs = b[2] == 3;
  uint8_t m[2];
  if (!read(ctx, m, 2)) return false;
  h.mask = (uint16_t)(m[0] | m[1] << 8);
  return h.mask != 0;
}

// mask の面 + 作業用 1 面ぶんのメモリを割り当てる。slot(i) は i 番目 (0..面の数) の 4080 バイトを返す関数
// (最後の 1 つが作業用)。どれか nullptr なら false (呼び出し側で解放する)。
template <typename SlotFn>
inline bool setup(State& s, uint16_t mask, SlotFn slot) {
  s.ready = false;
  int i = 0;
  for (int b = 0; b < 16; b++) {
    s.planes[b] = nullptr;
    if (!(mask >> b & 1)) continue;
    s.planes[b] = slot(i++);
    if (!s.planes[b]) return false;
    memset(s.planes[b], 0, PLANE);
  }
  s.scratch = slot(i);
  if (!s.scratch) return false;
  s.mask = mask;
  detail::makeHeader();
  s.ready = true;
  return true;
}

// 16 面すべてを alloc で確保する簡易版 (PC のテスト用)
inline bool init(State& s, void* (*alloc)(size_t)) {
  return setup(s, 0xFFFF, [alloc](int) { return (uint8_t*)alloc(PLANE); });
}

// readHeader の後の本体を展開して面を更新する (s.mask == h.mask であること)。
// 戻り値: 0 = 失敗, 1 = 差分, 2 = キーフレーム, 3 = 全面 SKIP_FRAME (絵は変わらない)
inline int decodeBody(State& s, const Header& h, ReadFn read, void* ctx) {
  if (h.abs || !s.ready || s.mask != h.mask) return 0;
  bool all_skip = !h.key;
  int top = ROWS, bottom = -1;
  bad_ctx_t bc;
  memset(&bc, 0, sizeof(bc));
  bc.read = detail::readCb;
  bc.buf_size = PLANE;
  for (int b = 0; b < 16; b++) {
    if (!s.planes[b]) continue;
    uint8_t lb[2];
    if (!read(ctx, lb, 2)) return 0;
    uint32_t len = lb[0] | lb[1] << 8;
    if (h.key) memset(s.planes[b], 0, PLANE);  // キーフレームは 0 からの差分
    detail::Src src{read, ctx, len, false};
    detail::cur() = &src;
    // 公式デコーダは最初に gram → prev をコピーしてから prev を見て gram に描く。
    // gram = この面そのもの、prev = 作業用 にすれば書き戻しのコピーが要らない
    bc.gram = s.planes[b];
    bc.prev = s.scratch;
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
    // 変わった行の範囲 (scratch = 前フレームのこの面)
    if (h.key) {
      top = 0;
      bottom = ROWS - 1;
    } else {
      const uint8_t* a = s.scratch;
      const uint8_t* c = s.planes[b];
      constexpr int RB = W / 8;
      int y0 = 0, y1 = ROWS - 1;
      while (y0 < top && memcmp(a + y0 * RB, c + y0 * RB, RB) == 0) y0++;
      if (y0 < top) top = y0;
      if (y0 < ROWS) {
        while (y1 > bottom && memcmp(a + y1 * RB, c + y1 * RB, RB) == 0) y1--;
        if (y1 > bottom) bottom = y1;
      }
    }
  }
  s.dirtyTop = top;
  s.dirtyBottom = bottom;
  if (bottom >= 0) all_skip = false;
  return h.key ? 2 : (all_skip ? 3 : 1);
}

// ver 3 (前フレーム不要) の本体を読みながら、変わったブロックを 1 つずつ sink(bx, by, rows, px) に渡す。
// px は 8x8 の RGB565 (行優先)、rows はそのブロックの有効な行数 (最下段は 7)。メモリは 128 バイトだけ。
// 本体: SKIP (0x80-0xBF) = n+1 ブロック変化なし / それ以外 = 次のブロックの mask の面 (bit0 → bit15) の命令が 1 つずつ
// (FILL / RLE_BLOCK_4 / RLE_BLOCK_8 / MASTER_BLOCK)。戻り値: 描いたブロック数、失敗なら -1
template <typename Sink>
inline int decodeAbs(const Header& h, ReadFn read, void* ctx, Sink sink) {
  constexpr int NBX = W / 8, NBY = H / 8, TOTAL = NBX * NBY;
  uint16_t px[64];
  int ptr = 0, drawn = 0;
  while (ptr < TOTAL) {
    uint8_t op;
    if (!read(ctx, &op, 1)) return -1;
    if (BAD_IS_SKIP(op)) {
      ptr += BAD_SKIP_COUNT(op);
      continue;
    }
    memset(px, 0, sizeof(px));
    bool first = true;
    for (int b = 0; b < 16; b++) {
      if (!(h.mask >> b & 1)) continue;
      uint8_t d[9];
      if (first) {
        d[0] = op;
        first = false;
      } else if (!read(ctx, d, 1)) {
        return -1;
      }
      uint8_t n = bad_block_abs_len(d[0]);
      if (n == 0 || (n > 1 && !read(ctx, d + 1, n - 1))) return -1;
      uint8_t rows[8];
      if (bad_block_abs_rows(d, rows) != BAD_OK) return -1;
      uint16_t bit = (uint16_t)(1u << b);
      for (int y = 0; y < 8; y++) {
        uint8_t v = rows[y];
        if (!v) continue;
        for (int x = 0; x < 8; x++)
          if (v >> x & 1) px[y * 8 + x] |= bit;
      }
    }
    int bx = ptr % NBX, by = ptr / NBX;
    int rows = ROWS - by * 8;
    sink(bx, by, rows > 8 ? 8 : rows, px);
    ptr++;
    drawn++;
  }
  return ptr == TOTAL ? drawn : -1;
}

// 1 フレーム展開 (ヘッダーから)。面の組み合わせが変わったら 0
inline int decode(State& s, ReadFn read, void* ctx) {
  Header h;
  if (!readHeader(read, ctx, h)) return 0;
  return decodeBody(s, h, read, ctx);
}

namespace detail {
// 1 バイト (8 画素ぶんの 1 ビット面) → 8 バイト (各画素 0/1, LSB = 左)。2KB の定数表 (ESP32 ではフラッシュに置かれる)
struct Spread {
  uint64_t t[256];
  constexpr Spread() : t() {
    for (int v = 0; v < 256; v++) {
      uint64_t x = 0;
      for (int k = 0; k < 8; k++)
        if (v >> k & 1) x |= (uint64_t)1 << (8 * k);
      t[v] = x;
    }
  }
};
inline const uint64_t* spread() {
  static constexpr Spread s;
  return s.t;
}
}  // namespace detail

// 16 面から RGB565 の行を組み立てて sink(y, row) に渡す (y = y0..y1, row は 240 画素)
template <typename Sink>
inline void compose(const State& s, int y0, int y1, Sink sink) {
  static uint16_t row[W];
  const uint64_t* sp = detail::spread();
  for (int y = y0; y <= y1; y++) {
    int base = y * (W / 8);
    for (int xb = 0; xb < W / 8; xb++) {
      // 下位 8 面と上位 8 面を、それぞれ 8 画素ぶん (1 画素 1 バイト) まとめて組む
      uint64_t lo = 0, hi = 0;
      for (int b = 0; b < 8; b++)
        if (s.planes[b]) lo |= sp[s.planes[b][base + xb]] << b;
      for (int b = 8; b < 16; b++)
        if (s.planes[b]) hi |= sp[s.planes[b][base + xb]] << (b - 8);
      uint16_t* px = row + xb * 8;
      for (int k = 0; k < 8; k++) px[k] = (uint16_t)((lo >> (8 * k)) & 0xFF) | (uint16_t)(((hi >> (8 * k)) & 0xFF) << 8);
    }
    sink(y, row);
  }
}

template <typename Sink>
inline void compose(const State& s, int rows, Sink sink) {
  compose(s, 0, rows - 1, sink);
}

}  // namespace bad16
