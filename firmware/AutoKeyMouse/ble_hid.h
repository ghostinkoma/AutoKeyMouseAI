#pragma once
// BLE HID (HOGP) キーボード + マウス複合デバイス
//
// レポート構成 (1つのレポート記述子に統合):
//   ID 1 : キーボード (修飾 1byte + 予約 1byte + キー 6byte)  / LED 出力
//   ID 2 : 相対マウス (ボタン 5bit, X/Y/ホイール int8)
//   ID 3 : 絶対座標マウス (X/Y 0..32767, ホイール int8)
//
// マウスボタンは常に ID 2 (相対マウス) 側で送る。
// Windows はコレクションごとにボタン状態を持つため、押下と解放を別コレクションで
// 送るとボタンが押しっぱなしになる。それを避けるため ID 3 のボタンは常に 0。
#include <stdint.h>

namespace hid {

constexpr uint8_t BTN_LEFT   = 0x01;
constexpr uint8_t BTN_RIGHT  = 0x02;
constexpr uint8_t BTN_MIDDLE = 0x04;

constexpr uint16_t ABS_MAX = 32767;

void begin(const char* deviceName, const char* manufacturer);

// ホストと接続済みかつ暗号化 (ペアリング) 済みなら true
bool connected();
uint32_t connectedPeers();

// キーボード (code は HID Usage ID, modifier は 0xE0-0xE7 もコードとして受け付ける)
void keyDown(uint8_t code);
void keyUp(uint8_t code);
void modifiersDown(uint8_t mods);
void modifiersUp(uint8_t mods);
void releaseKeys();

// マウス
void buttonsDown(uint8_t mask);
void buttonsUp(uint8_t mask);
void moveRelStep(int8_t dx, int8_t dy);   // 1レポート分 (-127..127)
void wheelStep(int8_t w);
void moveAbs(uint16_t x, uint16_t y);     // 0..ABS_MAX
void releaseAll();

}  // namespace hid
