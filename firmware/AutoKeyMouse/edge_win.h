#pragma once
#include <stddef.h>
#include <stdint.h>
#include <WString.h>
// 窓の種類の認識 (エッジ AI)。液晶に描いた画面から、決まった場所 (スロット) に開いている窓を当てる。
// 学習は PC (pc/tools/window_train.py)。計算は edge_win_core.h。
// モデルは PC から Wi-Fi で届いたもの (POST /winmodel。フラッシュの spiffs 領域にそのまま書き、メモリに読まずに
// フラッシュから直接使う = ヒープを使わない) か、無ければ書き込み時のもの (edge_win_model.h)。
// 描画のたびに feed* で明るさの画面 (120x68 = 8KB) を更新し、loop() で 0.3 秒ごとに判定する。
namespace edge_win {

void begin();
void loop();
// 液晶に描いた画素 (表示と同じ RGB565)。描画の関数 (display.cpp) から呼ぶ
void feedBlock(int x, int y, int w, int h, const uint16_t* px, int stride);
void feedFill(int x, int y, int w, int h, uint16_t color);
// 新しいモデルの受信 (Web サーバーの本文を少しずつ)。end の戻り値は空 = 成功、それ以外はエラーの文
constexpr size_t MODEL_MAX = 32 * 1024;
String uploadBegin();
String uploadWrite(const uint8_t* data, size_t len);
String uploadEnd();
String json();  // {"on":true,"id":"1a2b3c4d","src":"flash","windows":[{"name":"inventory","p":0.93}],"ms":3}
String line();  // シリアル表示用 "inventory 0.93, ..." / "none"

}  // namespace edge_win
