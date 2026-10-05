/*
 * BadCodec エンコーダ (1 フレーム分) の C 移植。
 * 元: ghostinkoma/BadCodec tools/Codec.py v0.6.0 (Protocol 514 rev.19) の encode_frame_worker。
 * 候補の選び方・同点時の優先順位・FOR の最適化まで Codec.py と同じ結果 (同じバイト列) になるようにしている。
 *
 * BadCodec Non-Commercial License (Copyright (c) 2026 BadCodec Project / ghostinkoma)。
 *
 * 画素は 0/1 の uint8 配列 (w*h, 行優先)。w, h は 8 の倍数。
 *
 *   int bad_encode_frame(const uint8_t *curr, const uint8_t *prev, int w, int h,
 *                        uint8_t *out, int out_cap);
 *     戻り値: 書き込んだバイト数 (FRAME_DELIMITER は含まない)。容量不足なら -1。
 *
 * ビルド (pc/akm/bad16.py が読み込む):
 *   Windows (同梱の bad_encode.dll を作り直すとき):
 *     x86_64-w64-mingw32-gcc -O2 -shared -static-libgcc -o bad_encode.dll bad_encode.c
 *   Linux: 初回に bad16.py が gcc -O2 -shared -fPIC で libbad_encode.so を作る
 */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#define API __declspec(dllexport)
#else
#define API __attribute__((visibility("default")))
#endif

enum {
  OP_RLE_FRAME = 0x30, OP_SKIP_FRAME = 0x39, OP_FILL_BLACK = 0x3A, OP_FILL_WHITE = 0x3B,
  OP_BLOCK_STREAM = 0x3C, OP_DELTA_FRAME = 0x3D, OP_MASTER_FRAME = 0x3E, OP_INVERT_PREV = 0x3F,
  OP_MASTER_BLOCK = 0x3C, OP_XOR_BLOCK = 0x3F,
};

/* ---------------------------------------------------------------- 可変長バッファ */
typedef struct {
  uint8_t *p;
  int n, cap;
} buf_t;

static int bput(buf_t *b, uint8_t v) {
  if (b->n >= b->cap) {
    int nc = b->cap ? b->cap * 2 : 256;
    uint8_t *np = (uint8_t *)realloc(b->p, nc);
    if (!np) return 0;
    b->p = np;
    b->cap = nc;
  }
  b->p[b->n++] = v;
  return 1;
}

static void bputs(buf_t *b, const uint8_t *s, int n) {
  for (int i = 0; i < n; i++) bput(b, s[i]);
}

/* ---------------------------------------------------------------- 走査パス (SCAN_PATHS) */
static uint8_t SCAN_X[8][64], SCAN_Y[8][64];
static int g_scan_ready = 0;

static void build_scan_paths(void) {
  if (g_scan_ready) return;
  for (int p = 0; p < 8; p++) {
    int scan_dir = (p >> 2) & 1, sp = p & 3;
    int sx = (sp & 1) ? 7 : 0, sy = (sp & 2) ? 7 : 0;
    int dx = (sp & 1) ? -1 : 1, dy = (sp & 2) ? -1 : 1;
    int k = 0;
    if (scan_dir == 0) {
      for (int y = sy; y >= 0 && y < 8; y += dy)
        for (int x = sx; x >= 0 && x < 8; x += dx) SCAN_X[p][k] = (uint8_t)x, SCAN_Y[p][k++] = (uint8_t)y;
    } else {
      for (int x = sx; x >= 0 && x < 8; x += dx)
        for (int y = sy; y >= 0 && y < 8; y += dy) SCAN_X[p][k] = (uint8_t)x, SCAN_Y[p][k++] = (uint8_t)y;
    }
  }
  g_scan_ready = 1;
}

/* ---------------------------------------------------------------- RLE パック */
static void pack_rle(const int *r4, uint8_t *o) {
  int b0 = r4[0] & 63, b1 = r4[1] & 63, b2 = r4[2] & 63, b3 = r4[3] & 63;
  o[0] = (uint8_t)(b0 | ((b1 & 3) << 6));
  o[1] = (uint8_t)(((b1 >> 2) & 15) | ((b2 & 15) << 4));
  o[2] = (uint8_t)(((b2 >> 4) & 3) | (b3 << 2));
}

/* ---------------------------------------------------------------- ブロック */
typedef uint8_t blk_t[8][8];

static void apply_shift(const blk_t in, int sx, int sy, blk_t out) {
  blk_t a, t;
  memcpy(a, in, 64);
  for (int s = 0; s < (sx > 0 ? sx : -sx); s++) {
    for (int y = 0; y < 8; y++) {
      if (sx > 0) { /* 右へ: 左端を元の右端で埋める */
        t[y][0] = a[y][7];
        for (int x = 1; x < 8; x++) t[y][x] = a[y][x - 1];
      } else {
        for (int x = 0; x < 7; x++) t[y][x] = a[y][x + 1];
        t[y][7] = a[y][0];
      }
    }
    memcpy(a, t, 64);
  }
  for (int s = 0; s < (sy > 0 ? sy : -sy); s++) {
    if (sy > 0) {
      memcpy(t[0], a[7], 8);
      for (int y = 1; y < 8; y++) memcpy(t[y], a[y - 1], 8);
    } else {
      for (int y = 0; y < 7; y++) memcpy(t[y], a[y + 1], 8);
      memcpy(t[7], a[0], 8);
    }
    memcpy(a, t, 64);
  }
  memcpy(out, a, 64);
}

/* runs を数える。start_col から始め、ランが 63 超なら -1。最大 max_runs を超えたら -1 */
static int count_runs(const blk_t b, int p, int start_col, int *runs, int max_runs) {
  int n = 0, curr = start_col, count = 0;
  for (int k = 0; k < 64; k++) {
    int px = b[SCAN_Y[p][k]][SCAN_X[p][k]];
    if (px == curr) {
      count++;
    } else {
      if (count > 63) return -1;
      if (n >= max_runs) return -1;
      runs[n++] = count;
      curr = 1 - curr;
      count = 1;
    }
  }
  if (count > 63) return -1;
  if (n >= max_runs) return -1;
  runs[n++] = count;
  return n;
}

/* _try_rle: 最初に成立した走査パターン (start_color = 先頭画素) */
static int try_rle4(const blk_t b, uint8_t *out4) {
  for (int p = 0; p < 8; p++) {
    int start = b[SCAN_Y[p][0]][SCAN_X[p][0]];
    int runs[8] = {0};
    int n = count_runs(b, p, start, runs, 4);
    if (n < 0) continue;
    out4[0] = (uint8_t)(0x20 | (p << 1) | start);
    pack_rle(runs, out4 + 1);
    return 1;
  }
  return 0;
}

static const int RLE8_OP[6] = {0x38, 0x39, 0x3A, 0x3B, 0x3D, 0x3E};
static const int RLE8_P[6] = {0, 0, 1, 1, 4, 4};
static const int RLE8_C[6] = {0, 1, 0, 1, 0, 1};

static int try_rle8(const blk_t b, uint8_t *out7) {
  for (int t = 0; t < 6; t++) {
    int runs[8] = {0};
    int n = count_runs(b, RLE8_P[t], RLE8_C[t], runs, 8);
    if (n < 0) continue;
    out7[0] = (uint8_t)RLE8_OP[t];
    pack_rle(runs, out7 + 1);
    pack_rle(runs + 4, out7 + 4);
    return 1;
  }
  return 0;
}

static int xor_block_rle(const blk_t c, const blk_t p, uint8_t *out) {
  uint8_t x[64];
  for (int i = 0; i < 64; i++) x[i] = c[i >> 3][i & 7] ^ p[i >> 3][i & 7];
  int n = 0, i = 0;
  while (i < 64) {
    int color = x[i], count = 1;
    while (i + count < 64 && x[i + count] == color && count < 63) count++;
    out[n++] = (uint8_t)((color << 7) | count);
    i += count;
  }
  return n;
}

/* encode_block: 戻り値 = 命令の長さ, type に S/X/F/I/R/D/M */
static int encode_block(const blk_t c, const blk_t p, uint8_t *out, char *type) {
  int same = memcmp(c, p, 64) == 0;
  int all0 = 1, all1 = 1, inv = 1;
  for (int y = 0; y < 8; y++)
    for (int x = 0; x < 8; x++) {
      if (c[y][x]) all0 = 0; else all1 = 0;
      if (c[y][x] != (1 - p[y][x])) inv = 0;
    }
  /* 1 バイト命令: 優先順 S > F > I > X */
  if (same) { out[0] = 0x80; *type = 'S'; return 1; }
  if (all0) { out[0] = 0x30; *type = 'F'; return 1; }
  if (all1) { out[0] = 0x34; *type = 'F'; return 1; }
  if (inv) { out[0] = 0x00; *type = 'I'; return 1; }
  for (int sx = -3; sx <= 3; sx++)
    for (int sy = -3; sy <= 3; sy++) {
      if (sx == 0 && sy == 0) continue;
      blk_t s;
      apply_shift(p, sx, sy, s);
      if (memcmp(s, c, 64) == 0) {
        int sign_x = sx < 0, mag_x = sx < 0 ? -sx : sx, sign_y = sy < 0, mag_y = sy < 0 ? -sy : sy;
        out[0] = (uint8_t)(0x40 | (sign_x << 5) | (mag_x << 3) | (sign_y << 2) | mag_y);
        *type = 'X';
        return 1;
      }
    }
  /* 多バイト: 候補の追加順 R4, D, R8, M で最小 (同サイズは先に追加した方) */
  uint8_t best[11];
  int best_n = 0;
  char best_t = 0;
  uint8_t r4[4];
  int has_r4 = try_rle4(c, r4);
  if (has_r4) { memcpy(best, r4, 4); best_n = 4; best_t = 'R'; }
  uint8_t xr[64];
  int xn = xor_block_rle(c, p, xr);
  if (2 + xn < 9 && (best_n == 0 || 2 + xn < best_n)) {
    best[0] = OP_XOR_BLOCK;
    best[1] = (uint8_t)xn;
    memcpy(best + 2, xr, xn);
    best_n = 2 + xn;
    best_t = 'D';
  }
  if (!has_r4) {
    uint8_t r8[7];
    if (try_rle8(c, r8) && (best_n == 0 || 7 < best_n)) {
      memcpy(best, r8, 7);
      best_n = 7;
      best_t = 'R';
    }
  }
  if (best_n == 0 || 9 < best_n) {
    best[0] = OP_MASTER_BLOCK;
    for (int y = 0; y < 8; y++) {
      uint8_t v = 0;
      for (int x = 0; x < 8; x++) v |= (uint8_t)(c[y][x] << x); /* bitorder='little' */
      best[1 + y] = v;
    }
    best_n = 9;
    best_t = 'M';
  }
  memcpy(out, best, best_n);
  *type = best_t;
  return best_n;
}

/* ---------------------------------------------------------------- FOR 最適化 (_best_for_skip) */
#define MAX_FOR 65
static int memo_inner = -1;
static int *memo = NULL;
static int memo_n = 0;

static int min_cost(int n, int max_inner) {
  if (n <= 0) return 0;
  if (memo_inner == max_inner && n < memo_n && memo[n] >= 0) return memo[n];
  int best = (n + max_inner - 1) / max_inner;
  for (int i = 1; i <= max_inner; i++) {
    int ks[2] = {n / i, n / i + 1};
    for (int t = 0; t < 2; t++) {
      int k = ks[t];
      if (k < 3 || k > MAX_FOR) continue;
      int product = k * i;
      if (product > n) continue;
      int cost = 2 + min_cost(n - product, max_inner);
      if (cost < best) best = cost;
    }
  }
  if (memo_inner == max_inner && n < memo_n) memo[n] = best;
  return best;
}

static void memo_reset(int max_inner, int n) {
  if (memo_n < n + 1) {
    free(memo);
    memo = (int *)malloc(sizeof(int) * (n + 1));
    memo_n = n + 1;
  }
  for (int i = 0; i < memo_n; i++) memo[i] = -1;
  memo_inner = max_inner;
}

static void best_for_skip(buf_t *out, int N, int base_op, int max_inner) {
  if (N <= 0) return;
  memo_reset(max_inner, N);
  int best_cost = (N + max_inner - 1) / max_inner;
  int bk = 0, bi = 0, brem = 0, found = 0;
  for (int i = 1; i <= max_inner; i++) {
    int k_max = N / i < MAX_FOR ? N / i : MAX_FOR;
    for (int k = 3; k <= k_max; k++) {
      int product = k * i;
      if (product > N) continue;
      int rem = N - product;
      int cost = 2 + min_cost(rem, max_inner);
      if (cost < best_cost) {
        best_cost = cost;
        bk = k, bi = i, brem = rem, found = 1;
      }
    }
  }
  if (!found) {
    int rem = N;
    while (rem > 0) {
      int take = rem < max_inner ? rem : max_inner;
      bput(out, (uint8_t)(base_op | (take - 1)));
      rem -= take;
    }
  } else {
    bput(out, (uint8_t)(0xC0 | (bk - 2)));
    bput(out, (uint8_t)(base_op | (bi - 1)));
    best_for_skip(out, brem, base_op, max_inner);
  }
}

/* ---------------------------------------------------------------- BLOCK_STREAM */
typedef struct {
  uint8_t b[11];
  int n;
  char t;
} cmd_t;

static void encode_block_stream(const uint8_t *curr, const uint8_t *prev, int w, int h, buf_t *out) {
  int bx = w / 8, n_blk = bx * (h / 8);
  cmd_t *raw = (cmd_t *)malloc(sizeof(cmd_t) * n_blk);
  for (int b = 0; b < n_blk; b++) {
    int y0 = (b / bx) * 8, x0 = (b % bx) * 8;
    blk_t c, p;
    for (int y = 0; y < 8; y++)
      for (int x = 0; x < 8; x++) {
        c[y][x] = curr[(y0 + y) * w + x0 + x] ? 1 : 0;
        p[y][x] = prev[(y0 + y) * w + x0 + x] ? 1 : 0;
      }
    raw[b].n = encode_block(c, p, raw[b].b, &raw[b].t);
  }
  /* _merge_multiblock: S/I/F のランをまとめる → トークン列 (バイト列, type) */
  buf_t *tok = (buf_t *)calloc(n_blk, sizeof(buf_t));
  int nt = 0, i = 0;
  while (i < n_blk) {
    cmd_t *c = &raw[i];
    buf_t *t = &tok[nt];
    if (c->t == 'S' && c->n == 1 && c->b[0] == 0x80) {
      int j = i;
      while (j < n_blk && raw[j].t == 'S' && raw[j].n == 1 && raw[j].b[0] == 0x80) j++;
      best_for_skip(t, j - i, 0x80, 64);
      i = j;
    } else if (c->t == 'I' && c->n == 1 && c->b[0] == 0x00) {
      int j = i;
      while (j < n_blk && raw[j].t == 'I' && raw[j].n == 1 && raw[j].b[0] == 0x00) j++;
      best_for_skip(t, j - i, 0x00, 32);
      i = j;
    } else if (c->t == 'F' && c->n == 1) {
      int base = c->b[0] & 0xFC, j = i;
      while (j < n_blk && raw[j].t == 'F' && raw[j].n == 1 && (raw[j].b[0] & 0xFC) == base) j++;
      best_for_skip(t, j - i, base, 4);
      i = j;
    } else {
      bputs(t, c->b, c->n);
      i++;
    }
    nt++;
  }
  /* optimize_for: 同じ 1 バイト命令の連続を FOR でまとめる */
  bput(out, OP_BLOCK_STREAM);
  i = 0;
  while (i < nt) {
    buf_t *t = &tok[i];
    if (t->n != 1) {
      bputs(out, t->p, t->n);
      i++;
      continue;
    }
    int j = i + 1;
    while (j < nt && tok[j].n == 1 && tok[j].p[0] == t->p[0]) j++;
    best_for_skip(out, j - i, t->p[0], 1);
    i = j;
  }
  for (int k = 0; k < nt; k++) free(tok[k].p);
  free(tok);
  free(raw);
}

/* ---------------------------------------------------------------- RLE_FRAME (8 走査パターン) */
static void rle_frame_best(const uint8_t *f, int w, int h, buf_t *out) {
  buf_t best = {0};
  int best_op = 0x30, have = 0;
  int n = w * h;
  uint8_t *pix = (uint8_t *)malloc(n);
  for (int scan_dir = 0; scan_dir < 2; scan_dir++)
    for (int sy = 0; sy < 2; sy++)
      for (int sx = 0; sx < 2; sx++) {
        int op = 0x30 | (scan_dir << 2) | (sy << 1) | sx;
        /* f[:, ::-1] (sx) → f[::-1, :] (sy) → 転置 (scan_dir) → flatten */
        int k = 0;
        if (!scan_dir) {
          for (int r = 0; r < h; r++)
            for (int c = 0; c < w; c++) {
              int y = sy ? h - 1 - r : r, x = sx ? w - 1 - c : c;
              pix[k++] = f[y * w + x];
            }
        } else {
          for (int c = 0; c < w; c++)
            for (int r = 0; r < h; r++) {
              int y = sy ? h - 1 - r : r, x = sx ? w - 1 - c : c;
              pix[k++] = f[y * w + x];
            }
        }
        buf_t rle = {0};
        int i = 0;
        while (i < n) {
          int color = pix[i] ? 1 : 0, count = 1;
          while (i + count < n && (pix[i + count] ? 1 : 0) == color && count < 127) count++;
          bput(&rle, (uint8_t)((color << 7) | count));
          i += count;
        }
        if (!have || rle.n < best.n) {
          free(best.p);
          best = rle;
          best_op = op;
          have = 1;
        } else {
          free(rle.p);
        }
      }
  bput(out, (uint8_t)best_op);
  bputs(out, best.p, best.n);
  free(best.p);
  free(pix);
}

/* ---------------------------------------------------------------- フレーム */
API int bad_encode_frame(const uint8_t *curr, const uint8_t *prev, int w, int h, uint8_t *out, int out_cap) {
  build_scan_paths();
  int n = w * h;
  int same = 1, all0 = 1, all1 = 1, inv = 1;
  for (int i = 0; i < n; i++) {
    int c = curr[i] ? 1 : 0, p = prev[i] ? 1 : 0;
    if (c != p) same = 0;
    if (c) all0 = 0; else all1 = 0;
    if (c != 1 - p) inv = 0;
  }
  uint8_t one = 0;
  if (same) one = OP_SKIP_FRAME;
  else if (all0) one = OP_FILL_BLACK;
  else if (all1) one = OP_FILL_WHITE;
  else if (inv) one = OP_INVERT_PREV;
  if (one) {
    if (out_cap < 1) return -1;
    out[0] = one;
    return 1;
  }
  /* 候補: C (MASTER), B (RLE_FRAME), E (DELTA), A (BLOCK_STREAM) の順。同サイズは先の方 */
  buf_t cand[4] = {{0}};
  bput(&cand[0], OP_MASTER_FRAME);
  for (int i = 0; i < n; i += 8) {
    uint8_t v = 0;
    for (int k = 0; k < 8 && i + k < n; k++) v |= (uint8_t)((curr[i + k] ? 1 : 0) << k);
    bput(&cand[0], v);
  }
  rle_frame_best(curr, w, h, &cand[1]);
  {
    uint8_t *d = (uint8_t *)malloc(n);
    for (int i = 0; i < n; i++) d[i] = (uint8_t)((curr[i] ? 1 : 0) ^ (prev[i] ? 1 : 0));
    buf_t tmp = {0};
    rle_frame_best(d, w, h, &tmp);
    bput(&cand[2], OP_DELTA_FRAME);
    bputs(&cand[2], tmp.p, tmp.n);  /* tmp[0] (0x30-0x37) をパターンバイトとして使う */
    free(tmp.p);
    free(d);
  }
  encode_block_stream(curr, prev, w, h, &cand[3]);
  int bi = 0;
  for (int k = 1; k < 4; k++)
    if (cand[k].n < cand[bi].n) bi = k;
  int len = cand[bi].n;
  if (len > out_cap) len = -1;
  else memcpy(out, cand[bi].p, len);
  for (int k = 0; k < 4; k++) free(cand[k].p);
  return len;
}

/* ==================================================================== 前フレーム不要版 (bad16 ver 3)
 * 受信側が前フレームを持たない (液晶にそのまま描く) ための 16 面ブロックストリーム。
 * 使う命令は BadCodec の BLOCK_STREAM のうち前フレームを参照しないものだけ:
 *   SKIP (0x80-0xBF, n+1 ブロック) … 全部の面が変わらないブロックを飛ばす (液晶に描かない)
 *   FILL (0x30 / 0x34), RLE_BLOCK_4 (0x20-0x2F), RLE_BLOCK_8 (0x38-0x3B, 0x3D, 0x3E), MASTER_BLOCK (0x3C)
 * 変わったブロックは mask の面ごとに (bit0 → bit15 の順に) 1 命令ずつ並べる。
 */
static int encode_block_abs(const blk_t c, uint8_t *out) {
  int all0 = 1, all1 = 1;
  for (int y = 0; y < 8; y++)
    for (int x = 0; x < 8; x++) {
      if (c[y][x]) all0 = 0; else all1 = 0;
    }
  if (all0) { out[0] = 0x30; return 1; }
  if (all1) { out[0] = 0x34; return 1; }
  if (try_rle4(c, out)) return 4;
  if (try_rle8(c, out)) return 7;
  out[0] = OP_MASTER_BLOCK;
  for (int y = 0; y < 8; y++) {
    uint8_t v = 0;
    for (int x = 0; x < 8; x++) v |= (uint8_t)(c[y][x] << x);
    out[1 + y] = v;
  }
  return 9;
}

/* curr / prev: RGB565 (w*h, 行優先, h は 8 の倍数)。prev が NULL なら全ブロックを送る。
 * 戻り値: 書いたバイト数 (容量不足なら -1)。changed_blocks に変わったブロック数 */
API int bad16_encode_abs(const uint16_t *curr, const uint16_t *prev, int w, int h, uint16_t mask,
                         uint8_t *out, int out_cap, int *changed_blocks) {
  if (!g_scan_ready) build_scan_paths();
  int nbx = w / 8, nby = h / 8, n = 0, skip = 0, changed = 0;
#define PUT(v) do { if (n >= out_cap) return -1; out[n++] = (uint8_t)(v); } while (0)
#define FLUSH_SKIP() do { while (skip > 0) { int k = skip > 64 ? 64 : skip; PUT(0x80 | (k - 1)); skip -= k; } } while (0)
  for (int by = 0; by < nby; by++)
    for (int bx = 0; bx < nbx; bx++) {
      int diff = prev == NULL;
      for (int y = 0; y < 8 && !diff; y++) {
        const uint16_t *a = curr + (by * 8 + y) * w + bx * 8, *b = prev + (by * 8 + y) * w + bx * 8;
        for (int x = 0; x < 8; x++)
          if ((a[x] ^ b[x]) & mask) { diff = 1; break; }
      }
      if (!diff) { skip++; continue; }
      FLUSH_SKIP();
      changed++;
      for (int bit = 0; bit < 16; bit++) {
        if (!(mask >> bit & 1)) continue;
        blk_t c;
        for (int y = 0; y < 8; y++)
          for (int x = 0; x < 8; x++) c[y][x] = (uint8_t)(curr[(by * 8 + y) * w + bx * 8 + x] >> bit & 1);
        uint8_t op[9];
        int k = encode_block_abs(c, op);
        for (int i = 0; i < k; i++) PUT(op[i]);
      }
    }
  FLUSH_SKIP();
#undef PUT
#undef FLUSH_SKIP
  if (changed_blocks) *changed_blocks = changed;
  return n;
}
