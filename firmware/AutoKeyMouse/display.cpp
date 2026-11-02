#include "display.h"

#include "config.h"

#if HAS_TFT

#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include <SPI.h>
#include <driver/gpio.h>
#include <driver/spi_master.h>
#include <esp_rom_gpio.h>
#include <soc/spi_periph.h>

#include "akm_tjpgd.h"
#include "bc_decode.h"
#include "bad16.h"
#include "edge_win.h"
#include <esp_heap_caps.h>

#include "ble_hid.h"
#include "macro.h"
#include "net.h"

namespace display {
namespace {

// Adafruit の液晶の座標のずれ (_xstart / _ystart) を DMA の描画でも使うため、読めるようにしておく
class Tft : public Adafruit_ST7789 {
 public:
  using Adafruit_ST7789::Adafruit_ST7789;
  int16_t xStart() const { return _xstart; }
  int16_t yStart() const { return _ystart; }
};
Tft gTft(&SPI, TFT_PIN_CS, TFT_PIN_DC, TFT_PIN_RST);
String gShown;
uint32_t gLastCheck = 0;

// 受信 JPEG 用の共用バッファ (24KB)。無印 ESP32 は Wi-Fi + BLE でメモリに余裕が無いので 1 つだけ持つ。
// BadCodec 16bit (ver 3) を DMA で描くときは、ここを 2 本の帯 (240x8 画素 x 2) として使う
alignas(4) uint8_t gFrameBuf[FRAME_BUF_SIZE];
SemaphoreHandle_t gBufLock = nullptr;

// 液晶は loop() (状態画面) とミラー受信タスクの両方から描くので排他する
SemaphoreHandle_t gLock = nullptr;
void dmaEnd();  // DMA の描画をやめて、Adafruit (Arduino の SPI) に液晶を戻す (下で定義)
// keepDma = false (既定): Adafruit で描く前に DMA をやめる。DMA で描く場所だけ true
struct TftLock {
  explicit TftLock(bool keepDma = false) {
    if (gLock) xSemaphoreTakeRecursive(gLock, portMAX_DELAY);
    if (!keepDma) dmaEnd();
  }
  ~TftLock() { if (gLock) xSemaphoreGiveRecursive(gLock); }
};

// PC から送られてくる縮小画面 (ミラー)
constexpr uint32_t kFrameHoldMs = 5000;  // 最後のフレームからこの時間は状態画面に戻さない
uint32_t gLastFrame = 0;
bool gFrameShown = false;
uint32_t gFrames = 0;
volatile bool gNeedKey = true;  // 液晶が上書きされたので次は全体 (キーフレーム) が欲しい
uint32_t gSpiHz = 40000000;  // 今の液晶の SPI クロック (setSpiHz)

// ---- DMA で描く (BadCodec 16bit ver 3 のブロック) -------------------------------------------
// Adafruit は Arduino の SPI (VSPI) で 1 画素ずつ CPU が送り終わるまで待つ。DMA で描くときは ESP-IDF の
// spi_master を別の SPI (HSPI) で使い、同じピン (SCLK / MOSI) をそちらへつなぎ替える。CS / DC は今まで通り GPIO。
//   * 同じ行の続いたブロックを 1 本の帯にまとめて 1 回で送る (範囲指定の命令の回数を減らす)
//   * 帯を 2 本交互に使い、1 本を DMA で送っている間に CPU は次の帯を組み立てる (展開と描画を重ねる)
// 状態画面・JPEG などを Adafruit で描くときは dmaEnd() で VSPI に戻す (TftLock が自動で行う)。
bool gDmaOn = TFT_DMA;          // 使うか (シリアルの tft dma on / off)
bool gDmaActive = false;        // 今 HSPI につなぎ替えてある
// HSPI のドライバー (DMA の記述子など) は最初に 1 回だけ用意して、使い終わっても返さない。
// 毎回用意・解放するとヒープが細切れになり、Wi-Fi の受信に使える大きな塊が無くなる
bool gDmaReady = false;
spi_device_handle_t gDmaDev = nullptr;
uint32_t gDmaHz = 0;
bool gDmaPending = false;       // 送信中の DMA がある
spi_transaction_t gDmaTrans[2];
int gDmaCur = 0;                // 今組み立てている帯 (0 / 1)
int gRunX = -1, gRunY = 0, gRunW = 0, gRunRows = 0;  // 組み立て中の帯 (x < 0 = 無し)
constexpr int kStrip = 240 * 8;  // 帯 1 本の画素数 (gFrameBuf の前半 / 後半)

uint16_t* dmaBuf(int i) { return (uint16_t*)gFrameBuf + i * kStrip; }

void dmaWait() {
  if (!gDmaPending) return;
  spi_transaction_t* r = nullptr;
  spi_device_get_trans_result(gDmaDev, &r, portMAX_DELAY);
  gDmaPending = false;
}

void dmaSend(const uint8_t* d, int n) {  // 短い送信 (命令・範囲)。DMA の送信が終わってから呼ぶ
  spi_transaction_t t = {};
  t.length = 8 * n;
  if (n <= 4) {
    t.flags = SPI_TRANS_USE_TXDATA;
    memcpy(t.tx_data, d, n);
  } else {
    t.tx_buffer = d;
  }
  spi_device_polling_transmit(gDmaDev, &t);
}

void dmaCmd(uint8_t c, uint16_t a, uint16_t b) {
  digitalWrite(TFT_PIN_DC, LOW);
  dmaSend(&c, 1);
  digitalWrite(TFT_PIN_DC, HIGH);
  const uint8_t d[4] = {(uint8_t)(a >> 8), (uint8_t)a, (uint8_t)(b >> 8), (uint8_t)b};
  dmaSend(d, 4);
}

// HSPI の液晶用の設定 (速さ) を作る。80MHz を受け付けなければ 40MHz で
esp_err_t dmaAddDevice() {
  spi_device_interface_config_t dc = {};
  dc.mode = 3;
  dc.spics_io_num = -1;  // CS は GPIO で持つ
  dc.queue_size = 2;
  dc.flags = SPI_DEVICE_HALFDUPLEX | SPI_DEVICE_NO_DUMMY;
  dc.clock_speed_hz = (int)gSpiHz;
  esp_err_t e = spi_bus_add_device(SPI2_HOST, &dc, &gDmaDev);
  if (e != ESP_OK && gSpiHz > 40000000) {
    dc.clock_speed_hz = 40000000;
    e = spi_bus_add_device(SPI2_HOST, &dc, &gDmaDev);
  }
  gDmaHz = dc.clock_speed_hz;
  return e;
}

// SCLK / MOSI を HSPI の信号につなぐ (2 回目からはドライバーを作り直さず、ピンのつなぎ先だけ変える)
void dmaRoutePins() {
  const int pins[2] = {TFT_PIN_SCLK, TFT_PIN_MOSI};
  const uint32_t sigs[2] = {spi_periph_signal[SPI2_HOST].spiclk_out, spi_periph_signal[SPI2_HOST].spid_out};
  for (int i = 0; i < 2; i++) {
    esp_rom_gpio_pad_select_gpio(pins[i]);
    gpio_set_direction((gpio_num_t)pins[i], GPIO_MODE_OUTPUT);
    esp_rom_gpio_connect_out_signal(pins[i], sigs[i], false, false);
  }
}

bool dmaBegin() {
  if (gDmaActive) return true;
  SPI.end();  // Arduino の SPI からピンを外す
  if (gDmaReady) {
    dmaRoutePins();
    digitalWrite(TFT_PIN_CS, LOW);
    gDmaActive = true;
    gRunX = -1;
    return true;
  }
  spi_bus_config_t bc = {};
  bc.mosi_io_num = TFT_PIN_MOSI;
  bc.miso_io_num = -1;
  bc.sclk_io_num = TFT_PIN_SCLK;
  bc.quadwp_io_num = -1;
  bc.quadhd_io_num = -1;
  bc.max_transfer_sz = kStrip * 2;
  esp_err_t e = spi_bus_initialize(SPI2_HOST, &bc, SPI_DMA_CH_AUTO);
  if (e == ESP_OK) {
    e = dmaAddDevice();
    if (e != ESP_OK) spi_bus_free(SPI2_HOST);
  }
  if (e != ESP_OK) {
    Serial.printf("[TFT] DMA start failed (%d). back to normal SPI\n", (int)e);
    gDmaOn = false;
    SPI.begin(TFT_PIN_SCLK, -1, TFT_PIN_MOSI, TFT_PIN_CS);
    return false;
  }
  gDmaReady = true;
  Serial.printf("[TFT] DMA drawing on (HSPI %lu Hz, free heap %u)\n", (unsigned long)gDmaHz,
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT));
  digitalWrite(TFT_PIN_CS, LOW);
  gDmaActive = true;
  gRunX = -1;
  return true;
}

// 組み立てた帯を送る (前の帯の DMA が終わるのを待ってから)
void dmaFlush() {
  if (gRunX < 0) return;
  uint16_t* buf = dmaBuf(gDmaCur);
  if (gRunW < 240) {  // 1 行 240 画素の間隔で置いたので、帯の幅に詰める
    for (int r = 1; r < gRunRows; r++) memmove(buf + r * gRunW, buf + r * 240, gRunW * 2);
  }
  dmaWait();
  int x = gRunX + gTft.xStart(), y = gRunY + gTft.yStart();
  dmaCmd(ST77XX_CASET, x, x + gRunW - 1);
  dmaCmd(ST77XX_RASET, y, y + gRunRows - 1);
  digitalWrite(TFT_PIN_DC, LOW);
  const uint8_t ramwr = ST77XX_RAMWR;
  dmaSend(&ramwr, 1);
  digitalWrite(TFT_PIN_DC, HIGH);
  spi_transaction_t& t = gDmaTrans[gDmaCur];
  t = {};
  t.length = 16 * gRunW * gRunRows;
  t.tx_buffer = buf;
  spi_device_queue_trans(gDmaDev, &t, portMAX_DELAY);
  gDmaPending = true;
  gDmaCur ^= 1;
  gRunX = -1;
}

// 8 x rows のブロックを帯に足す (画素はホストのバイト順。液晶は上位バイトが先なので入れ替えて置く)
IRAM_ATTR void dmaBlock(int x, int y, int rows, const uint16_t* px);

// BadCodec 16bit (ver 3) の 1 フレームの展開を、ブロックごとの描画まで含めて IRAM で回す。
// フラッシュから実行すると、展開の命令を読むたびに Wi-Fi の処理とキャッシュを取り合って遅くなる
// (1 ブロックにかかる時間が命令の数から計算した値の数倍あった)。decodeAbs は always_inline でここに展開される
// noinline: 呼び出し元 (drawBad16。フラッシュ) に埋め込まれると IRAM に置かれないため
__attribute__((noinline)) IRAM_ATTR int decodeAbsDma(const bad16::Header& h, ReadFn read, void* ctx, uint32_t* drawUs,
                                                    uint32_t* edgeUs) {
  return bad16::decodeAbs(h, read, ctx, [&](int bx, int by, int rows, uint16_t* px) __attribute__((always_inline)) {
    uint32_t d0 = micros();
    edge_win::feedBlock(bx * 8, by * 8, 8, rows, px, 8);
    uint32_t d1 = micros();
    dmaBlock(bx * 8, by * 8, rows, px);  // 帯がつながらなくなったら前の帯を DMA で送る
    *edgeUs += d1 - d0;
    *drawUs += micros() - d1;
  });
}

IRAM_ATTR void dmaBlock(int x, int y, int rows, const uint16_t* px) {
  if (gRunX >= 0 && (y != gRunY || x != gRunX + gRunW || rows != gRunRows)) dmaFlush();
  if (gRunX < 0) {
    gRunX = x;
    gRunY = y;
    gRunW = 0;
    gRunRows = rows;
  }
  uint16_t* dst = dmaBuf(gDmaCur) + gRunW;
  for (int r = 0; r < rows; r++)
    for (int k = 0; k < 8; k++) dst[r * 240 + k] = __builtin_bswap16(px[r * 8 + k]);
  gRunW += 8;
}

void dmaEnd() {
  if (!gDmaActive) return;
  dmaFlush();
  dmaWait();
  digitalWrite(TFT_PIN_CS, HIGH);
  gDmaActive = false;
  // HSPI のドライバーはそのまま (次に使うときはピンをつなぎ替えるだけ)。ピンは Arduino の SPI に戻す
  SPI.begin(TFT_PIN_SCLK, -1, TFT_PIN_MOSI, TFT_PIN_CS);
}

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

void setDma(bool on) {
  TftLock lock;  // DMA をやめる (次の ver 3 のフレームから on なら使う)
  gDmaOn = on;
  gNeedKey = true;
  Serial.printf("[TFT] DMA drawing %s\n", on ? "on" : "off");
}

bool dmaOn() { return gDmaOn; }

void setSpiHz(uint32_t hz) {
  TftLock lock;  // DMA をやめてから
  gTft.setSPISpeed(hz);
  gSpiHz = hz;
  if (gDmaReady) {  // HSPI の速さも変える (液晶の設定だけ作り直す。滅多に無いので細切れは気にしない)
    spi_bus_remove_device(gDmaDev);
    if (dmaAddDevice() != ESP_OK) {
      spi_bus_free(SPI2_HOST);
      gDmaReady = false;
    }
  }
  gNeedKey = true;  // 全体を描き直してもらう (乱れていてもすぐ分かるように)
  Serial.printf("[TFT] SPI clock %lu Hz\n", (unsigned long)hz);
}

uint32_t spiHz() { return gSpiHz; }

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
  gTft.setSPISpeed(TFT_SPI_HZ);  // 既定 config.h の TFT_SPI_HZ (T-Display 公式は 40MHz)
  gSpiHz = TFT_SPI_HZ;
  Serial.printf("[TFT] SPI clock %lu Hz\n", (unsigned long)gSpiHz);
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
  // 展開の内訳 (BadCodec 16bit ver 3)。命令の読み取り / 画素の組み立て / 窓認識への受け渡し
  uint64_t parseUs = 0, asmUs = 0, edgeUs = 0;
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
  int bw = r->right - r->left + 1, bh = r->bottom - r->top + 1;
  edge_win::feedBlock(r->left, r->top, bw, bh, (const uint16_t*)bitmap, bw);  // 窓の認識にも同じ画素を
  gTft.drawRGBBitmap(r->left, r->top, (uint16_t*)bitmap, bw, bh);
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
    edge_win::feedBlock(0, y, FRAME_W, 1, row, FRAME_W);
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
    edge_win::feedFill(x, y, 8, h, c);
    gTft.writeFillRect(x, y, 8, h, c);
    drawUs += micros() - d0;
  }
  void block(int x, int y, int h, const uint16_t* px) {
    uint32_t d0 = micros();
    edge_win::feedBlock(x, y, 8, h, px, 8);
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
  TftLock lock(true);  // ver 3 は DMA で描く。それ以外は下で dmaEnd()
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
    uint32_t drawUs = 0, edgeUs = 0;
    bad16::Prof p0 = bad16::prof();
    int n;
    // DMA: 共用バッファを帯に使うので借りる (JPEG の受信中などで借りられなければ今まで通り)
    bool dma = gDmaOn && acquireFrameBuffer(0) != nullptr;
    if (dma && !dmaBegin()) {
      releaseFrameBuffer();
      dma = false;
    }
    if (dma) {
      n = decodeAbsDma(h, read, ctx, &drawUs, &edgeUs);
      uint32_t d0 = micros();
      dmaFlush();
      dmaWait();  // 帯は共用バッファなので、返す前に送り終える
      drawUs += micros() - d0;
      releaseFrameBuffer();
    } else {
      dmaEnd();
      gTft.startWrite();
      n = bad16::decodeAbs(h, read, ctx, [&](int bx, int by, int rows, uint16_t* px) {
        uint32_t d0 = micros();
        edge_win::feedBlock(bx * 8, by * 8, 8, rows, px, 8);  // 液晶に送る前に (送ると並びが変わることがある)
        gTft.setAddrWindow(bx * 8, by * 8, 8, rows);
        gTft.writePixels(px, 8 * rows);
        drawUs += micros() - d0;
      });
      gTft.endWrite();
    }
    uint32_t waited = recvUs ? *recvUs - recv0 : 0;
    uint32_t total = micros() - t0;
    rollStats();
    const uint32_t mhz = getCpuFrequencyMhz();
    gCur.frames++;
    gCur.bytes += len;
    gCur.recvUs += waited;
    gCur.drawUs += drawUs;
    gCur.decodeUs += total > waited + drawUs ? total - waited - drawUs : 0;
    // 命令の読み取りの中には受信待ち (データがまだ来ていない) も入っているので、その分を引く
    uint32_t parseUs = (bad16::prof().parse - p0.parse) / mhz;
    gCur.parseUs += parseUs > waited ? parseUs - waited : 0;
    gCur.asmUs += (bad16::prof().assemble - p0.assemble) / mhz;
    gCur.edgeUs += edgeUs;
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
  dmaEnd();  // ver 1/2 は Adafruit で描く
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
      bad16::compose(gB16, y0, y1, [](int y, uint16_t* row) {
        edge_win::feedBlock(0, y, FRAME_W, 1, row, FRAME_W);
        gTft.writePixels(row, FRAME_W);
      });
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
void statsValues(float& fps, float& recv, float& dec, float& draw, float& cpu, float& kbps, float* parse = nullptr,
                 float* assemble = nullptr, float* edge = nullptr) {
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
  if (parse) *parse = s.parseUs / 1000.0f / n;
  if (assemble) *assemble = s.asmUs / 1000.0f / n;
  if (edge) *edge = s.edgeUs / 1000.0f / n;
}
}  // namespace

String frameStatsJson() {
  float fps, recv, dec, draw, cpu, kbps, parse, assemble, edge;
  statsValues(fps, recv, dec, draw, cpu, kbps, &parse, &assemble, &edge);
  char b[300];
  snprintf(b, sizeof(b),
           "{\"fps\":%.1f,\"recv_ms\":%.1f,\"decode_ms\":%.1f,\"draw_ms\":%.1f,\"cpu_pct\":%.1f,\"kbps\":%.0f,"
           "\"spi_mhz\":%.1f,\"dma\":%s,\"parse_ms\":%.1f,\"asm_ms\":%.1f,\"edge_ms\":%.1f}",
           fps, recv, dec, draw, cpu, kbps, gSpiHz / 1e6, gDmaOn ? "true" : "false", parse, assemble, edge);
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
  dmaEnd();
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
void setSpiHz(uint32_t) {}
void setDma(bool) {}
bool dmaOn() { return false; }
uint32_t spiHz() { return 0; }
}  // namespace display

#endif
