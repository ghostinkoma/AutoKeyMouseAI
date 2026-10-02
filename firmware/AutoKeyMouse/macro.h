#pragma once
// マクロ実行エンジン
//
// スクリプトは ';' または改行で区切ったコマンド列。文字列中の ';' は "\;" と書く。
//
//   k:<key>        キーを押して離す。修飾は "+" でつなぐ (例: k:ctrl+shift+esc)
//   kd:<key>       キーを押したままにする
//   ku:<key>       キーを離す
//   t:<text>       文字列を入力 (ASCII。ホスト配列は設定 layout=us/jp に合わせる)
//   m:<dx>,<dy>    マウス相対移動 (大きな値は分割送信)
//   a:<x>,<y>      マウス絶対移動 (0..32767 で画面全体を正規化)
//   c:<L|R|M>[,n]  クリック (n 回)
//   bd:<L|R|M>     ボタンを押したままにする
//   bu:<L|R|M>     ボタンを離す
//   s:<n>          ホイール (+ で上, - で下)
//   w:<ms>         待機
//   d:<ms>         このスクリプト内の HID イベント間ディレイを変更 (5..50)
//   ra             すべてのキー/ボタンを離す
//
// 例: "k:enter;w:200;t:/move lorencia;k:enter"
#include <Arduino.h>

#include "keymap.h"

namespace macro {

enum class Result : uint8_t { Ok, ParseError, NotConnected, Busy, Aborted, Timeout };

struct Slot {
  String name;
  String script;
};

void begin();

// スクリプトの文法チェックのみ。エラー時は err にメッセージ。
bool validate(const String& script, String* err);

// 非同期実行。repeat=0 で stop() まで無限ループ。
Result start(const String& script, uint32_t repeat, const String& label, String* err);

// 同期実行 (完了まで待つ)。タイムアウトしても実行は継続される。
Result runSync(const String& script, uint32_t timeoutMs, String* err);

void stop();
bool busy();
String currentLabel();
String lastResult();

// 設定
uint8_t eventDelayMs();
void setEventDelayMs(uint8_t ms);
keymap::Layout layout();
void setLayout(keymap::Layout l);

// マクロスロット (1..MACRO_SLOTS)。NVS に保存される。
Slot slot(uint8_t id);
void saveSlot(uint8_t id, const String& name, const String& script);

const char* resultText(Result r);

}  // namespace macro
