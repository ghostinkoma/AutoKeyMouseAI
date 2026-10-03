#pragma once
#include <stddef.h>
#include <stdint.h>
// TFT への状態表示 (Wi-Fi の IP / BLE / マクロ)。HAS_TFT=0 なら何もしない。
namespace display {

void begin();
void loop();  // loop() から呼ぶ。内容が変わったときだけ描き直す
void test();  // 赤・緑・青で全面を塗る (表示確認用)

// PC から送られてくる縮小画面 (240x135, RGB565 ビッグエンディアン) を液晶へ流し込む。
// 受信しながらそのまま書き込むので大きなバッファは不要。
constexpr uint16_t FRAME_W = 240;
constexpr uint16_t FRAME_H = 135;
bool frameBegin(uint16_t w, uint16_t h);  // 液晶が無い / サイズ違いなら false
void frameData(const uint8_t* data, size_t len);
bool frameEnd();                          // 画素数ぴったり受け取れたら true
uint32_t framesShown();

}  // namespace display
