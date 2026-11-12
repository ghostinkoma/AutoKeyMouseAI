#include "entropy.h"

#include <string.h>

#ifdef ESP_PLATFORM
#include <esp_attr.h>
#define ENT_IRAM IRAM_ATTR  // 展開の内側で 1 バイトごとに呼ばれるので IRAM に置く (フラッシュだと Wi-Fi と取り合う)
#else
#define ENT_IRAM
#endif

namespace ent {
namespace {

// tANS の表: 2^R 個 (最大 1024) の { 記号, nb, 次の状態の元 (u16) }。4KB
struct TEnt {
  uint8_t sym, nb;
  uint16_t base;
};
struct Table {
  TEnt tans[1024];
};
Table gTable;
Dec gDec;

// 受信から符号を 1 バイト (無くなったら 0 を足す)
ENT_IRAM inline bool nextByte(Dec& d, uint8_t& b) {
  if (d.inPos >= d.inN) {
    if (d.inLeft == 0) {
      b = 0;
      return true;
    }
    uint32_t k = d.inLeft < sizeof(d.in) ? d.inLeft : sizeof(d.in);
    if (!d.read(d.ctx, d.in, k)) return false;
    d.inLeft -= k;
    d.inPos = 0;
    d.inN = (int)k;
  }
  b = d.in[d.inPos++];
  return true;
}

ENT_IRAM inline bool fill(Dec& d, int need) {
  while (d.bits < need) {
    uint8_t b;
    if (!nextByte(d, b)) return false;
    d.acc = (d.acc << 8) | b;
    d.bits += 8;
  }
  return true;
}

ENT_IRAM inline bool getBits(Dec& d, int nb, uint32_t& v) {
  if (nb == 0) {
    v = 0;
    return true;
  }
  if (!fill(d, nb)) return false;
  v = (d.acc >> (d.bits - nb)) & ((1u << nb) - 1);
  d.bits -= nb;
  return true;
}

int highbit(uint32_t v) {
  int r = 0;
  while (v >>= 1) r++;
  return r;
}

}  // namespace

Dec& dec() { return gDec; }

bool begin(Dec& d, uint8_t kind, ReadFn rd, void* ctx, uint32_t len) {
  d = Dec();
  d.read = rd;
  d.ctx = ctx;
  d.kind = kind;
  uint8_t h[6];  // ver, maxLen / R, 元の長さ
  if (len < 8 || !rd(ctx, h, 6) || h[0] != 1) return false;
  uint32_t used = 8;
  d.bitsMax = h[1];
  d.outLeft = h[2] | h[3] << 8 | h[4] << 16 | (uint32_t)h[5] << 24;
  if (kind == 'A') {
    int R = d.bitsMax;
    if (R < 6 || R > 10) return false;
    int L = 1 << R;
    uint8_t ns;
    if (!rd(ctx, &ns, 1)) return false;
    used += 1;
    int nsym = ns + 1;
    uint16_t norm[256];
    memset(norm, 0, sizeof(norm));
    int sum = 0;
    for (int i = 0; i < nsym; i++) {
      uint8_t e[3];
      if (!rd(ctx, e, 3)) return false;
      norm[e[0]] = (uint16_t)(e[1] | e[2] << 8);
      sum += norm[e[0]];
    }
    used += 3 * nsym;
    if (sum != L) return false;
    // 記号を散らして並べる (エンコーダと同じ) → 状態ごとの { 記号, 読むビット数, 次の状態の元 }
    int pos = 0, step = (L >> 1) + (L >> 3) + 3;
    for (int s = 0; s < 256; s++)
      for (int i = 0; i < norm[s]; i++) {
        gTable.tans[pos].sym = (uint8_t)s;
        pos = (pos + step) & (L - 1);
      }
    uint16_t nxt[256];
    for (int s = 0; s < 256; s++) nxt[s] = norm[s];
    for (int x = 0; x < L; x++) {
      int s = gTable.tans[x].sym;
      uint32_t ns2 = nxt[s]++;
      int nb = R - highbit(ns2);
      gTable.tans[x].nb = (uint8_t)nb;
      gTable.tans[x].base = (uint16_t)((ns2 << nb) - L);
    }
  } else {
    return false;
  }
  if (len < used) return false;
  d.inLeft = len - used;
  if (kind == 'A') {
    uint32_t v;
    if (!getBits(d, d.bitsMax, v)) return false;
    d.state = v;
  }
  return true;
}

ENT_IRAM bool read(void* ctx, uint8_t* dst, size_t n) {
  Dec& d = *(Dec*)ctx;
  if (n > d.outLeft || d.err) return false;
  d.outLeft -= (uint32_t)n;
  {
    for (size_t i = 0; i < n; i++) {
      const TEnt& t = gTable.tans[d.state];
      dst[i] = t.sym;
      uint32_t v;
      if (!getBits(d, t.nb, v)) return d.err = true, false;
      d.state = t.base + v;
    }
  }
  return true;
}

}  // namespace ent
