#pragma once
// 1 フレームまるごとのエントロピー符号 ('B' 'H' = ハフマン / 'B' 'A' = tANS) を、受け取りながら戻す。
// 戻したバイト列は普通の BadCodec のフレーム ('B' '6' ...) なので、read(ctx, dst, n) の形でそのまま
// display::drawBad16 に渡す (BadCodec の展開は変えない)。形式は pc/native/bad_encode.c の bad_entropy_* 参照。
// メモリ: 表 4KB (ハフマンと tANS で共用) + 受信の小さなバッファ。1 つのタスクだけで使う。
#include <stddef.h>
#include <stdint.h>

namespace ent {

typedef bool (*ReadFn)(void* ctx, uint8_t* dst, size_t n);

struct Dec {
  ReadFn read = nullptr;  // 元 (受信) の読み出し
  void* ctx = nullptr;
  uint32_t outLeft = 0;  // まだ戻していないバイト数
  uint32_t inLeft = 0;   // まだ受け取っていない符号のバイト数
  uint8_t kind = 0;      // 'H' / 'A'
  uint8_t bitsMax = 0;   // ハフマン: 符号長の最大 / tANS: R
  uint32_t acc = 0;      // ビットの溜め (上位から使う)
  int bits = 0;
  uint32_t state = 0;  // tANS の状態
  uint8_t in[64];
  int inPos = 0, inN = 0;
  bool err = false;
};

// 'B' と 'H' / 'A' を読んだあとに呼ぶ: 残りのヘッダーを読み、表を作る。len = フレーム全体の長さ
bool begin(Dec& d, uint8_t kind, ReadFn read, void* ctx, uint32_t len);
// 戻したバイトを読む (ReadFn の形。drawBad16 に渡す)
bool read(void* ctx, uint8_t* dst, size_t n);
// 1 つだけの作業領域 (表 4KB を含む)
Dec& dec();

}  // namespace ent
