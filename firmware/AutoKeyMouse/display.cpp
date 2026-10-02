#include "display.h"

#include "config.h"

#if HAS_TFT

#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include <SPI.h>

#include "ble_hid.h"
#include "macro.h"
#include "net.h"

namespace display {
namespace {

Adafruit_ST7789 gTft(&SPI, TFT_PIN_CS, TFT_PIN_DC, TFT_PIN_RST);
String gShown;
uint32_t gLastCheck = 0;

// 液晶の内蔵フォントは ASCII のみなので、それ以外は '?' にする
String ascii(const String& s, size_t maxLen) {
  String o;
  for (size_t i = 0; i < s.length() && o.length() < maxLen; ++i) {
    char c = s[i];
    if ((uint8_t)c >= 0x80) {
      while (i + 1 < s.length() && ((uint8_t)s[i + 1] & 0xC0) == 0x80) ++i;  // UTF-8 の続きを飛ばす
      c = '?';
    }
    o += c;
  }
  return o;
}

void line(int y, uint8_t size, uint16_t color, const String& text) {
  gTft.setTextSize(size);
  gTft.setTextColor(color);
  gTft.setCursor(4, y);
  gTft.print(text);
}

}  // namespace

void begin() {
  pinMode(TFT_PIN_BL, OUTPUT);
  digitalWrite(TFT_PIN_BL, HIGH);
  SPI.begin(TFT_PIN_SCLK, -1, TFT_PIN_MOSI, TFT_PIN_CS);
  gTft.init(135, 240);
  gTft.setRotation(1);  // 横向き 240x135
  gTft.fillScreen(ST77XX_BLACK);
  line(4, 2, ST77XX_CYAN, "AutoKeyMouse");
  line(40, 2, ST77XX_WHITE, "booting...");
}

void loop() {
  if (millis() - gLastCheck < 300) return;
  gLastCheck = millis();

  bool sta = net::staConnected();
  bool ble = hid::connected();
  String ip = net::staIp();
  String ssid = ascii(net::ssid(), 19);
  String ap = net::apActive() ? net::apIp() : "";
  String cur = macro::busy() ? ascii(macro::currentLabel(), 30) : "";
  String state = String(sta) + ip + "|" + ssid + "|" + ap + "|" + ble + hid::connectedPeers() + "|" + cur;
  if (state == gShown) return;
  gShown = state;

  gTft.fillScreen(ST77XX_BLACK);
  line(4, 2, ST77XX_CYAN, "AutoKeyMouse");

  // Wi-Fi
  if (sta) {
    line(28, 1, ST77XX_GREEN, "WiFi: " + ssid);
    line(40, ip.length() <= 12 ? 3 : 2, ST77XX_WHITE, ip);  // 取得した IP を大きく
  } else if (net::staConfigured()) {
    line(28, 1, ST77XX_YELLOW, "WiFi: connecting " + ssid);
    line(40, 2, ST77XX_YELLOW, "no IP yet");
  } else {
    line(28, 1, ST77XX_YELLOW, "WiFi: not set (serial: wifi)");
  }
  if (!ap.isEmpty()) {
    line(70, 1, ST77XX_ORANGE, "AP " AP_SSID " / " AP_PASS);
    line(82, 2, ST77XX_ORANGE, ap);
  }

  // BLE / マクロ
  if (ble) line(104, 2, ST77XX_GREEN, "BLE connected");
  else if (hid::connectedPeers() > 0) line(104, 2, ST77XX_YELLOW, "BLE pairing...");
  else line(104, 2, ST77XX_RED, "BLE waiting");
  if (!cur.isEmpty()) line(124, 1, ST77XX_MAGENTA, "run: " + cur);
}

}  // namespace display

#else

namespace display {
void begin() {}
void loop() {}
}  // namespace display

#endif
