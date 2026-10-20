#pragma once
#include <stdint.h>
#include <WString.h>
// 窓の種類の認識 (エッジ AI)。液晶に描いた画面から、決まった場所 (スロット) に開いている窓を当てる。
// 学習は PC (pc/tools/window_train.py)。モデルは edge_win_model.h (自動生成)。計算は edge_win_core.h。
// 描画のたびに feed* で明るさのバッファ (16KB) を更新し、loop() で 0.3 秒ごとに判定する。
namespace edge_win {

void begin();
void loop();
// 液晶に描いた画素 (表示と同じ RGB565)。描画の関数 (display.cpp) から呼ぶ
void feedBlock(int x, int y, int w, int h, const uint16_t* px, int stride);
void feedFill(int x, int y, int w, int h, uint16_t color);
String json();  // {"on":true,"windows":[{"name":"inventory","p":0.93}],"ms":3}
String line();  // シリアル表示用 "inventory 0.93, ..." / "none"

}  // namespace edge_win
