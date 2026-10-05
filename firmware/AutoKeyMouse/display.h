#pragma once
#include <stddef.h>
#include <stdint.h>
#include <WString.h>
// TFT への状態表示 (Wi-Fi の IP / BLE / マクロ)。HAS_TFT=0 なら何もしない。
namespace display {

void begin();
void loop();  // loop() から呼ぶ。内容が変わったときだけ描き直す
void test();  // 赤・緑・青で全面を塗る (表示確認用)

// PC から送られてくる縮小画面を液晶に表示する。形式は
//   * JPEG (240x135 以下)                         … 通常はこちら (数 KB で済む)
//   * RGB565 ビッグエンディアン 240x135 = 64800 バイト
constexpr uint16_t FRAME_W = 240;
constexpr uint16_t FRAME_H = 135;
bool showFrame(const uint8_t* data, size_t len);  // 液晶が無い / 形式不正なら false
uint32_t framesShown();

// ミラー表示の負荷計測 (5 秒ごとに集計)。PC から 10fps などで送ったとき、
// JPEG 展開・液晶描画で CPU をどれだけ使っているかを見る (エッジ AI に残る余裕の確認用)
void noteReceive(uint32_t us, size_t bytes);  // 1 フレームの受信にかかった時間
String frameStatsJson();                      // {"fps":..,"recv_ms":..,"decode_ms":..,"draw_ms":..,"cpu_pct":..}
String frameStatsLine();                      // シリアル表示用

// RGB565 BE を受信しながらそのまま液晶へ流す (64,800 バイトのバッファを確保しなくて済む)
bool rawBegin();
bool rawWrite(const uint8_t* data, size_t len);  // 画素数を超えたら false
bool rawEnd();                                   // ちょうど 240x135 画素なら true
void rawAbort();

}  // namespace display
