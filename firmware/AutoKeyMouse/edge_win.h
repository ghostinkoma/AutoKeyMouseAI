#pragma once
#include <stddef.h>
#include <stdint.h>
#include <WString.h>
// 窓の種類の認識 (エッジ AI)。液晶に描いた画面から、決まった場所 (スロット) に開いている窓を当てる。
// 学習は PC (pc/tools/window_train.py)。計算は edge_win_core.h。
// モデルは PC から Wi-Fi で届いたもの (POST /winmodel, フラッシュの /win.bin に保存) を使い、
// 無ければ書き込み時のもの (edge_win_model.h)。描画のたびに feed* で明るさのバッファ (16KB) を更新し、
// loop() で 0.3 秒ごとに判定する。
namespace edge_win {

void begin();
void loop();
// 液晶に描いた画素 (表示と同じ RGB565)。描画の関数 (display.cpp) から呼ぶ
void feedBlock(int x, int y, int w, int h, const uint16_t* px, int stride);
void feedFill(int x, int y, int w, int h, uint16_t color);
// 新しいモデル (malloc したバッファ。受け取ったら持ち主はこちら。失敗しても free する)。
// 正しければ使い始めてフラッシュに保存する。戻り値は空 = 成功、それ以外はエラーの文
String setModel(uint8_t* data, size_t len);
constexpr size_t MODEL_MAX = 24 * 1024;  // 隠れ層 48 で約 16KB
String json();  // {"on":true,"id":"1a2b3c4d","src":"wifi","windows":[{"name":"inventory","p":0.93}],"ms":3}
String line();  // シリアル表示用 "inventory 0.93, ..." / "none"

}  // namespace edge_win
