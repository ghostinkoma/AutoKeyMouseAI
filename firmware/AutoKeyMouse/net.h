#pragma once
// Wi-Fi と HTTP サーバ
//
// * SSID 設定済み : STA で接続 (DHCP)。つながったら SoftAP は止める
// * 未設定 / STA が 30 秒つながらない : SoftAP (192.168.4.1) を出して設定できるようにする
#include <Arduino.h>

namespace net {

void begin();
void loop();  // loop() から呼ぶ。ブロックしない

bool staConnected();
String staIp();
bool staConfigured();
String ssid();
bool apActive();
String apIp();

// 最後の切断理由 (0 = なし) とその説明
int lastDisconnectReason();
String lastDisconnectText();

// SSID / パスワードを NVS に保存して接続し直す (空の SSID で STA 無効 = AP のみ)
void setCredentials(const String& ssid, const String& pass);

}  // namespace net
