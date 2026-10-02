#pragma once
#include <stdint.h>

namespace keymap {

enum class Layout : uint8_t { US = 0, JP = 1 };

// "a", "F1", "enter", "0x3A" などのキー名を HID Usage ID に変換する。
// 修飾キー名 (ctrl, shift, alt, win, rctrl ...) は 0xE0-0xE7 を返す。
// 不明なら 0。
uint8_t keyFromName(const char* name);

// ASCII 1文字をキーコード + Shift 要否に変換 (ホストのキーボード配列に合わせる)。
// 入力できない文字なら false。
bool keyFromChar(char c, Layout layout, uint8_t* code, bool* shift);

}  // namespace keymap
