#include "display.h"

#include "config.h"

#if HAS_TFT

#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include <SPI.h>

#include <TJpg_Decoder.h>

#include "ble_hid.h"
#include "macro.h"
#include "net.h"

namespace display {
namespace {

Adafruit_ST7789 gTft(&SPI, TFT_PIN_CS, TFT_PIN_DC, TFT_PIN_RST);
String gShown;
uint32_t gLastCheck = 0;

// PC から送られてくる縮小画面 (ミラー)
constexpr uint32_t kFrameHoldMs = 5000;  // 最後のフレームからこの時間は状態画面に戻さない
uint32_t gLastFrame = 0;
bool gFrameShown = false;
uint32_t gFrames = 0;

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

void test() {
  // 赤 → 緑 → 青 の順に全面を塗る (配線・初期化の確認用)
  const uint16_t colors[] = {ST77XX_RED, ST77XX_GREEN, ST77XX_BLUE};
  for (uint16_t c : colors) {
    gTft.fillScreen(c);
    delay(300);
  }
  gShown = "";  // 次の loop() で描き直す
  Serial.println("[TFT] test pattern drawn (red, green, blue)");
}

void begin() {
  Serial.printf("[TFT] ST7789 135x240  MOSI=%d SCLK=%d CS=%d DC=%d RST=%d BL=%d\n", TFT_PIN_MOSI,
                TFT_PIN_SCLK, TFT_PIN_CS, TFT_PIN_DC, TFT_PIN_RST, TFT_PIN_BL);
  pinMode(TFT_PIN_BL, OUTPUT);
  digitalWrite(TFT_PIN_BL, HIGH);
  SPI.begin(TFT_PIN_SCLK, -1, TFT_PIN_MOSI, TFT_PIN_CS);
  // ST7789 は MODE3 なら CS の有無に関係なく動く (T-Display 公式設定と同じ)
  gTft.init(135, 240, SPI_MODE3);
  gTft.setSPISpeed(27000000);
  gTft.setRotation(1);  // 横向き 240x135
  test();
  gTft.fillScreen(ST77XX_BLACK);
  line(4, 2, ST77XX_CYAN, "AutoKeyMouse");
  line(40, 2, ST77XX_WHITE, "booting...");
}

namespace {

bool jpegOut(int16_t x, int16_t y, uint16_t w, uint16_t h, uint16_t* bitmap) {
  // TJpg_Decoder は RGB565 (16x16 などのブロック単位) で渡してくる。そのまま液晶へ
  if (x >= FRAME_W || y >= FRAME_H) return false;  // 画面外は打ち切り
  gTft.drawRGBBitmap(x, y, bitmap, w, h);
  return true;
}

bool drawJpeg(const uint8_t* data, size_t len) {
  // ESP32 の ROM 内蔵 tjpgd は版が古く OpenCV の JPEG を展開できないことがあるため、
  // ソフトウェア版 (Bodmer の TJpg_Decoder) で展開する
  static bool init = false;
  if (!init) {
    TJpgDec.setJpgScale(1);
    TJpgDec.setSwapBytes(false);  // drawRGBBitmap はホストのバイト順 (リトルエンディアン) を受け取る
    TJpgDec.setCallback(jpegOut);
    init = true;
  }
  uint16_t w = 0, h = 0;
  if (TJpgDec.getJpgSize(&w, &h, data, len) != JDR_OK) {
    Serial.println("[TFT] jpeg header error");
    return false;
  }
  if (w > FRAME_W || h > FRAME_H) {
    Serial.printf("[TFT] jpeg too large: %ux%u\n", w, h);
    return false;
  }
  JRESULT rc = TJpgDec.drawJpg(0, 0, data, len);
  if (rc != JDR_OK) Serial.printf("[TFT] jpeg decode failed: %d\n", rc);
  return rc == JDR_OK;
}

bool drawRaw(const uint8_t* data, size_t len) {
  if (len != (size_t)FRAME_W * FRAME_H * 2) return false;
  static uint16_t row[FRAME_W];
  gTft.startWrite();
  gTft.setAddrWindow(0, 0, FRAME_W, FRAME_H);
  for (int y = 0; y < FRAME_H; ++y) {
    const uint8_t* p = data + (size_t)y * FRAME_W * 2;
    for (int x = 0; x < FRAME_W; ++x) row[x] = (uint16_t)(p[2 * x] << 8 | p[2 * x + 1]);
    gTft.writePixels(row, FRAME_W);
  }
  gTft.endWrite();
  return true;
}

}  // namespace

bool showFrame(const uint8_t* data, size_t len) {
  bool ok;
  if (len > 2 && data[0] == 0xFF && data[1] == 0xD8) ok = drawJpeg(data, len);  // JPEG
  else ok = drawRaw(data, len);                                                 // RGB565 BE
  if (ok) {
    gLastFrame = millis();
    gFrameShown = true;
    ++gFrames;
  }
  return ok;
}

uint32_t framesShown() { return gFrames; }

namespace {
bool gRawActive = false;
uint32_t gRawPx = 0;
int gRawCarry = -1;  // チャンクの境目で 2 バイトが分かれたときの上位バイト
uint16_t gRawBuf[64];
uint8_t gRawN = 0;

void rawFlush() {
  if (gRawN) gTft.writePixels(gRawBuf, gRawN);
  gRawN = 0;
}
}  // namespace

bool rawBegin() {
  gTft.startWrite();
  gTft.setAddrWindow(0, 0, FRAME_W, FRAME_H);
  gRawActive = true;
  gRawPx = 0;
  gRawCarry = -1;
  gRawN = 0;
  return true;
}

bool rawWrite(const uint8_t* data, size_t len) {
  if (!gRawActive) return false;
  const uint32_t total = (uint32_t)FRAME_W * FRAME_H;
  for (size_t i = 0; i < len; ++i) {
    if (gRawCarry < 0) {
      gRawCarry = data[i];
      continue;
    }
    if (gRawPx >= total) return false;
    gRawBuf[gRawN++] = (uint16_t)(gRawCarry << 8 | data[i]);
    gRawCarry = -1;
    ++gRawPx;
    if (gRawN == 64) rawFlush();
  }
  return true;
}

bool rawEnd() {
  if (!gRawActive) return false;
  rawFlush();
  gTft.endWrite();
  gRawActive = false;
  bool ok = gRawPx == (uint32_t)FRAME_W * FRAME_H && gRawCarry < 0;
  if (ok) {
    gLastFrame = millis();
    gFrameShown = true;
    ++gFrames;
  } else {
    Serial.printf("[TFT] raw frame size mismatch: %lu px\n", (unsigned long)gRawPx);
  }
  return ok;
}

void rawAbort() {
  if (!gRawActive) return;
  rawFlush();
  gTft.endWrite();
  gRawActive = false;
}

void loop() {
  if (millis() - gLastCheck < 300) return;
  gLastCheck = millis();

  // ミラー表示中は状態画面を描かない。途切れたら状態画面に戻す
  if (gFrameShown) {
    if (millis() - gLastFrame < kFrameHoldMs) return;
    gFrameShown = false;
    gShown = "";
    Serial.println("[TFT] mirror stopped, back to status screen");
  }

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

#include <Arduino.h>

namespace display {
void begin() {}
void loop() {}
bool showFrame(const uint8_t*, size_t) { return false; }
uint32_t framesShown() { return 0; }
bool rawBegin() { return false; }
bool rawWrite(const uint8_t*, size_t) { return false; }
bool rawEnd() { return false; }
void rawAbort() {}
void test() { Serial.println("[TFT] this build has no TFT (HAS_TFT=0)"); }
}  // namespace display

#endif
