#pragma once
// Wi-Fi (STA + SoftAP 併用) と HTTP サーバ
#include <Arduino.h>

namespace net {

void begin();
void loop();  // loop() から呼ぶ。ブロックしない

bool staConnected();
String staIp();

}  // namespace net
