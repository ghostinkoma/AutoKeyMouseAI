#include "display.h"

#include "config.h"

#if HAS_TFT

#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include <SPI.h>

#if defined(CONFIG_IDF_TARGET_ESP32S3)
#include "esp32s3/rom/tjpgd.h"
#else
#include "esp32/rom/tjpgd.h"
#endif

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

struct JpegSrc {
  const uint8_t* data;
  size_t len;
  size_t pos;
};

UINT jpegIn(JDEC* jd, BYTE* buf, UINT n) {
  JpegSrc* src = (JpegSrc*)jd->device;
  size_t left = src->len - src->pos;
  if (n > left) n = left;
  if (buf) memcpy(buf, src->data + src->pos, n);
  src->pos += n;
  return n;
}

UINT jpegOut(JDEC* jd, void* bitmap, JRECT* r) {
  // tjpgd (ROM 版) は RGB888 で出力する。RGB565 に詰め直して液晶へ
  static uint16_t px[16 * 16];
  const uint8_t* rgb = (const uint8_t*)bitmap;
  int w = r->right - r->left + 1, h = r->bottom - r->top + 1;
  if (r->left >= FRAME_W || r->top >= FRAME_H) return 1;
  for (int i = 0; i < w * h; ++i, rgb += 3) {
    px[i] = (uint16_t)((rgb[0] & 0xF8) << 8 | (rgb[1] & 0xFC) << 3 | rgb[2] >> 3);
  }
  gTft.drawRGBBitmap(r->left, r->top, px, w, h);
  return 1;
}

bool drawJpeg(const uint8_t* data, size_t len) {
  static uint8_t work[3100];
  JDEC jd;
  JpegSrc src{data, len, 0};
  JRESULT rc = jd_prepare(&jd, jpegIn, work, sizeof(work), &src);
  if (rc != JDR_OK) {
    Serial.printf("[TFT] jpeg prepare failed: %d\n", rc);
    return false;
  }
  if (jd.width > FRAME_W || jd.height > FRAME_H) {
    Serial.printf("[TFT] jpeg too large: %ux%u\n", jd.width, jd.height);
    return false;
  }
  rc = jd_decomp(&jd, jpegOut, 0);  // drawRGBBitmap が SPI の開始/終了を行う
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
void test() { Serial.println("[TFT] this build has no TFT (HAS_TFT=0)"); }
}  // namespace display

#endif
