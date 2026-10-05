#include "display.h"

#include "config.h"

#if HAS_TFT

#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include <SPI.h>

#include "akm_tjpgd.h"
#include "bc_decode.h"
#include "bad16.h"
#include <esp_heap_caps.h>

#include "ble_hid.h"
#include "macro.h"
#include "net.h"

namespace display {
namespace {

Adafruit_ST7789 gTft(&SPI, TFT_PIN_CS, TFT_PIN_DC, TFT_PIN_RST);
String gShown;
uint32_t gLastCheck = 0;

// 受信 JPEG 用の共用バッファ (24KB)。無印 ESP32 は Wi-Fi + BLE でメモリに余裕が無いので 1 つだけ持つ
uint8_t gFrameBuf[FRAME_BUF_SIZE];
SemaphoreHandle_t gBufLock = nullptr;

// 液晶は loop() (状態画面) とミラー受信タスクの両方から描くので排他する
SemaphoreHandle_t gLock = nullptr;
struct TftLock {
  TftLock() { if (gLock) xSemaphoreTakeRecursive(gLock, portMAX_DELAY); }
  ~TftLock() { if (gLock) xSemaphoreGiveRecursive(gLock); }
};

// PC から送られてくる縮小画面 (ミラー)
constexpr uint32_t kFrameHoldMs = 5000;  // 最後のフレームからこの時間は状態画面に戻さない
uint32_t gLastFrame = 0;
bool gFrameShown = false;
uint32_t gFrames = 0;
volatile bool gNeedKey = true;  // 液晶が上書きされたので次は全体 (キーフレーム) が欲しい

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
  TftLock lock;
  gNeedKey = true;
  // 赤 → 緑 → 青 の順に全面を塗る (配線・初期化の確認用)
  const uint16_t colors[] = {ST77XX_RED, ST77XX_GREEN, ST77XX_BLUE};
  for (uint16_t c : colors) {
    gTft.fillScreen(c);
    delay(300);
  }
  gShown = "";  // 次の loop() で描き直す
  Serial.println("[TFT] test pattern drawn (red, green, blue)");
}

uint8_t* acquireFrameBuffer(uint32_t waitMs) {
  if (!gBufLock) return nullptr;
  return xSemaphoreTake(gBufLock, pdMS_TO_TICKS(waitMs)) == pdTRUE ? gFrameBuf : nullptr;
}

void releaseFrameBuffer() {
  if (gBufLock) xSemaphoreGive(gBufLock);
}

void begin() {
  if (!gLock) gLock = xSemaphoreCreateRecursiveMutex();
  if (!gBufLock) gBufLock = xSemaphoreCreateMutex();
  TftLock lock;
  Serial.printf("[TFT] ST7789 135x240  MOSI=%d SCLK=%d CS=%d DC=%d RST=%d BL=%d\n", TFT_PIN_MOSI,
                TFT_PIN_SCLK, TFT_PIN_CS, TFT_PIN_DC, TFT_PIN_RST, TFT_PIN_BL);
  pinMode(TFT_PIN_BL, OUTPUT);
  digitalWrite(TFT_PIN_BL, HIGH);
  SPI.begin(TFT_PIN_SCLK, -1, TFT_PIN_MOSI, TFT_PIN_CS);
  // ST7789 は MODE3 なら CS の有無に関係なく動く (T-Display 公式設定と同じ)
  gTft.init(135, 240, SPI_MODE3);
  gTft.setSPISpeed(40000000);  // ST7789 は 40MHz で書き込める (T-Display 公式と同じ)
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

size_t jpegIn(JDEC* jd, uint8_t* buf, size_t n) {
  JpegSrc* src = (JpegSrc*)jd->device;
  size_t left = src->len - src->pos;
  if (n > left) n = left;
  if (buf) memcpy(buf, src->data + src->pos, n);
  src->pos += n;
  return n;
}

// ---- 負荷計測 ----
struct Stats {
  uint32_t frames = 0;
  uint64_t recvUs = 0, decodeUs = 0, drawUs = 0, bytes = 0;
};
Stats gCur, gLast;
uint32_t gWinStart = 0, gLastWinMs = 0;
uint32_t gDrawUsFrame = 0;  // 今のフレームで液晶描画に使った時間

void rollStats() {
  uint32_t now = millis();
  if (gWinStart == 0) gWinStart = now;
  if (now - gWinStart >= 5000) {
    gLast = gCur;
    gLastWinMs = now - gWinStart;
    gCur = Stats();
    gWinStart = now;
  }
}

int jpegOut(JDEC* jd, void* bitmap, JRECT* r) {
  // RGB565 (ホストのバイト順) のブロックが来るのでそのまま液晶へ
  if (r->left >= FRAME_W || r->top >= FRAME_H) return 1;
  uint32_t t0 = micros();
  gTft.drawRGBBitmap(r->left, r->top, (uint16_t*)bitmap, r->right - r->left + 1, r->bottom - r->top + 1);
  gDrawUsFrame += micros() - t0;
  return 1;
}

bool drawJpeg(const uint8_t* data, size_t len) {
  static uint32_t work[TJPGD_WORKSPACE_SIZE / 4 + 1];  // 4 バイト境界に置く
  JDEC jd;
  JpegSrc src{data, len, 0};
  JRESULT rc = akm_jd_prepare(&jd, jpegIn, work, sizeof(work), &src);
  if (rc != JDR_OK) {
    Serial.printf("[TFT] jpeg prepare failed: %d\n", rc);
    return false;
  }
  if (jd.width > FRAME_W || jd.height > FRAME_H) {
    Serial.printf("[TFT] jpeg too large: %ux%u\n", jd.width, jd.height);
    return false;
  }
  jd.swap = 0;
  rc = akm_jd_decomp(&jd, jpegOut, 0);
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
  TftLock lock;
  bool ok;
  gDrawUsFrame = 0;
  uint32_t t0 = micros();
  bool jpeg = len > 2 && data[0] == 0xFF && data[1] == 0xD8;
  if (jpeg) ok = drawJpeg(data, len);  // JPEG
  else ok = drawRaw(data, len);        // RGB565 BE
  uint32_t total = micros() - t0;
  if (!jpeg) gDrawUsFrame = total;
  rollStats();
  gCur.drawUs += gDrawUsFrame;
  gCur.decodeUs += total > gDrawUsFrame ? total - gDrawUsFrame : 0;
  if (ok) {
    gLastFrame = millis();
    gFrameShown = true;
    ++gFrames;
    gNeedKey = false;  // JPEG は全面を描くのでキーフレームと同じ
  }
  return ok;
}

uint32_t framesShown() { return gFrames; }

bool needKeyframe() { return gNeedKey; }
void requestKeyframe() { gNeedKey = true; }

namespace {
struct TftSink {
  uint32_t drawUs = 0;
  void fill(int x, int y, int h, uint16_t c) {
    uint32_t d0 = micros();
    gTft.writeFillRect(x, y, 8, h, c);
    drawUs += micros() - d0;
  }
  void block(int x, int y, int h, const uint16_t* px) {
    uint32_t d0 = micros();
    gTft.setAddrWindow(x, y, 8, h);
    gTft.writePixels(const_cast<uint16_t*>(px), 8 * h);
    drawUs += micros() - d0;
  }
};
}  // namespace

int drawBadCodec(ReadFn read, void* ctx, uint32_t len, const uint32_t* recvUs) {
  TftLock lock;
  uint32_t t0 = micros();
  uint32_t recv0 = recvUs ? *recvUs : 0;
  TftSink sink;
  gTft.startWrite();
  int res = len >= 4 ? bc::decode(read, ctx, sink) : 0;
  bool ok = res != 0;
  bool key = res == 2;
  uint32_t drawUs = sink.drawUs;
  gTft.endWrite();
  uint32_t total = micros() - t0;
  uint32_t waited = recvUs ? *recvUs - recv0 : 0;
  rollStats();
  gCur.frames++;
  gCur.bytes += len;
  gCur.recvUs += waited;
  gCur.drawUs += drawUs;
  gCur.decodeUs += total > waited + drawUs ? total - waited - drawUs : 0;
  if (!ok) {
    gNeedKey = true;
    return 0;
  }
  if (key) gNeedKey = false;
  gLastFrame = millis();
  gFrameShown = true;
  ++gFrames;
  return key ? 2 : 1;
}

namespace {
// BadCodec 16bit 版の面の置き場所。
// 無印 ESP32 (PSRAM 無し) は JPEG 用の共用バッファ (24KB = 6 面) を借り、足りない分だけヒープから取る。
// ヒープは Wi-Fi / BLE 用に BAD16_HEAP_RESERVE は必ず残す。
bad16::State gB16;
bool gB16HasBuf = false;            // 共用バッファを借りている
uint8_t* gB16Heap[17] = {};         // ヒープから取った面 (解放用)
constexpr int kBufSlots = FRAME_BUF_SIZE / bad16::PLANE;  // 6

void b16Free() {
  for (auto& p : gB16Heap) {
    heap_caps_free(p);
    p = nullptr;
  }
  if (gB16HasBuf) {
    releaseFrameBuffer();
    gB16HasBuf = false;
  }
  gB16 = bad16::State();
}

uint8_t* b16Slot(int i) {
  // PSRAM があればすべて PSRAM へ (S3)
  if (void* p = heap_caps_malloc(bad16::PLANE, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)) return gB16Heap[i] = (uint8_t*)p;
  if (i < kBufSlots) {
    if (!gB16HasBuf) {
      if (!acquireFrameBuffer(1000)) return nullptr;
      gB16HasBuf = true;
    }
    return gFrameBuf + i * bad16::PLANE;
  }
  if (heap_caps_get_free_size(MALLOC_CAP_8BIT) < bad16::PLANE + BAD16_HEAP_RESERVE) return nullptr;
  return gB16Heap[i] = (uint8_t*)heap_caps_malloc(bad16::PLANE, MALLOC_CAP_8BIT);
}

// mask の面を置けるようにする。足りなければ全部返して false
bool b16Setup(uint16_t mask) {
  if (gB16.ready && gB16.mask == mask) return true;
  b16Free();
  if (bad16::setup(gB16, mask, b16Slot)) {
    Serial.printf("[TFT] bad16: %d planes ready (mask %04x, free heap %u)\n", bad16::planesIn(mask), mask,
                  (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT));
    return true;
  }
  b16Free();
  Serial.printf("[TFT] bad16: not enough memory for %d planes (mask %04x, needs %u bytes)\n", bad16::planesIn(mask),
                mask, (unsigned)((bad16::planesIn(mask) + 1) * bad16::PLANE));
  return false;
}
}  // namespace

void releaseBad16() {
  TftLock lock;
  if (gB16.ready || gB16HasBuf) {
    b16Free();
    Serial.println("[TFT] bad16: buffers released");
  }
}

int drawBad16(ReadFn read, void* ctx, uint32_t len, const uint32_t* recvUs) {
  TftLock lock;
  uint32_t t0 = micros();
  uint32_t recv0 = recvUs ? *recvUs : 0;
  bad16::Header h;
  if (len < 4 || !bad16::readHeader(read, ctx, h)) {
    gNeedKey = true;
    return 0;
  }
  if (h.abs) {
    // ver 3: 前フレーム不要。変わったブロックだけ液晶へ (面のメモリは使わないので返しておく)
    if (gB16.ready || gB16HasBuf) b16Free();
    uint32_t drawUs = 0;
    gTft.startWrite();
    int n = bad16::decodeAbs(h, read, ctx, [&](int bx, int by, int rows, uint16_t* px) {
      uint32_t d0 = micros();
      gTft.setAddrWindow(bx * 8, by * 8, 8, rows);
      gTft.writePixels(px, 8 * rows);
      drawUs += micros() - d0;
    });
    gTft.endWrite();
    uint32_t waited = recvUs ? *recvUs - recv0 : 0;
    uint32_t total = micros() - t0;
    rollStats();
    gCur.frames++;
    gCur.bytes += len;
    gCur.recvUs += waited;
    gCur.drawUs += drawUs;
    gCur.decodeUs += total > waited + drawUs ? total - waited - drawUs : 0;
    if (n < 0) {
      gNeedKey = true;
      return 0;
    }
    // 液晶が上書きされていたら、全体 (キーフレーム) が来るまで 'R' を返し続ける
    if (h.key) gNeedKey = false;
    gLastFrame = millis();
    gFrameShown = true;
    ++gFrames;
    return h.key ? 2 : (n ? 1 : 3);
  }
  if (!(gB16.ready && gB16.mask == h.mask)) {
    if (!h.key) {  // 面の組み合わせが変わった / まだ無い: キーフレームから
      gNeedKey = true;
      return 4;
    }
    if (!b16Setup(h.mask)) return -1;
  }
  int res = bad16::decodeBody(gB16, h, read, ctx);
  uint32_t waited = recvUs ? *recvUs - recv0 : 0;
  uint32_t drawUs = 0;
  // 差分フレームでも、液晶が状態画面で上書きされていたら全体を描き直す
  if (res == 1 || res == 2 || (res == 3 && gNeedKey)) {
    // 変わった行だけ描く (キーフレーム・液晶が上書きされていたときは全体)
    int y0 = gB16.dirtyTop, y1 = gB16.dirtyBottom;
    if (res != 1 || gNeedKey) {
      y0 = 0;
      y1 = FRAME_H - 1;
    }
    if (y0 <= y1) {
      uint32_t d0 = micros();
      gTft.startWrite();
      gTft.setAddrWindow(0, y0, FRAME_W, y1 - y0 + 1);
      bad16::compose(gB16, y0, y1, [](int, uint16_t* row) { gTft.writePixels(row, FRAME_W); });
      gTft.endWrite();
      drawUs = micros() - d0;
    }
  }
  uint32_t total = micros() - t0;
  rollStats();
  gCur.frames++;
  gCur.bytes += len;
  gCur.recvUs += waited;
  gCur.drawUs += drawUs;
  gCur.decodeUs += total > waited + drawUs ? total - waited - drawUs : 0;
  if (res == 0) {
    gNeedKey = true;  // 面がずれた可能性があるので、次はキーフレームから
    return 0;
  }
  // 全面を描き直したので液晶の上書きは解消 (面の内容はキーフレーム以外でも連続している)
  gNeedKey = false;
  gLastFrame = millis();
  gFrameShown = true;
  ++gFrames;
  return res;
}

void noteReceive(uint32_t us, size_t bytes) {
  rollStats();
  gCur.frames++;
  gCur.recvUs += us;
  gCur.bytes += bytes;
}

namespace {
void statsValues(float& fps, float& recv, float& dec, float& draw, float& cpu, float& kbps) {
  rollStats();
  const Stats& s = gLast;
  uint32_t win = gLastWinMs ? gLastWinMs : 1;
  uint32_t n = s.frames ? s.frames : 1;
  fps = s.frames * 1000.0f / win;
  recv = s.recvUs / 1000.0f / n;
  dec = s.decodeUs / 1000.0f / n;
  draw = s.drawUs / 1000.0f / n;
  cpu = (s.decodeUs + s.drawUs) / 10.0f / win;  // % (展開 + 描画が 1 コアの時間に占める割合)
  kbps = s.bytes * 8.0f / win;
}
}  // namespace

String frameStatsJson() {
  float fps, recv, dec, draw, cpu, kbps;
  statsValues(fps, recv, dec, draw, cpu, kbps);
  char b[200];
  snprintf(b, sizeof(b), "{\"fps\":%.1f,\"recv_ms\":%.1f,\"decode_ms\":%.1f,\"draw_ms\":%.1f,\"cpu_pct\":%.1f,\"kbps\":%.0f}",
           fps, recv, dec, draw, cpu, kbps);
  return String(b);
}

String frameStatsLine() {
  float fps, recv, dec, draw, cpu, kbps;
  statsValues(fps, recv, dec, draw, cpu, kbps);
  char b[160];
  snprintf(b, sizeof(b), "mirror %.1ffps recv=%.1fms decode=%.1fms draw=%.1fms cpu=%.0f%% %.0fkbps",
           fps, recv, dec, draw, cpu, kbps);
  return String(b);
}

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
  if (gLock) xSemaphoreTakeRecursive(gLock, portMAX_DELAY);  // rawEnd / rawAbort で返す
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
  if (gLock) xSemaphoreGiveRecursive(gLock);
  rollStats();  // raw は受信しながら描くので、描画時間は受信時間に含まれる
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
  if (gLock) xSemaphoreGiveRecursive(gLock);
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
  TftLock lock;
  if (gFrameShown) return;  // 待っている間にミラーが始まった
  gShown = state;
  gNeedKey = true;  // 状態画面で上書きするので、ミラーを再開するときは全体を描き直してもらう

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
void noteReceive(uint32_t, size_t) {}
int drawBadCodec(ReadFn, void*, uint32_t, const uint32_t*) { return 0; }
int drawBad16(ReadFn, void*, uint32_t, const uint32_t*) { return -1; }
void releaseBad16() {}
bool needKeyframe() { return false; }
void requestKeyframe() {}
uint8_t* acquireFrameBuffer(uint32_t) { return nullptr; }
void releaseFrameBuffer() {}
String frameStatsJson() { return "{}"; }
String frameStatsLine() { return "no display"; }
bool rawBegin() { return false; }
bool rawWrite(const uint8_t*, size_t) { return false; }
bool rawEnd() { return false; }
void rawAbort() {}
void test() { Serial.println("[TFT] this build has no TFT (HAS_TFT=0)"); }
}  // namespace display

#endif
