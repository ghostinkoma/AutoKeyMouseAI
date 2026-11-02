#include "frame_stream.h"

#include <WiFi.h>
#include <freertos/stream_buffer.h>

#include "config.h"
#include "display.h"

namespace frame_stream {
namespace {

volatile bool gClient = false;  // PC (ミラーの送り手) がつながっている

// ---- 受信の分担 -----------------------------------------------------------------------------
// 受信 (pump) は Wi-Fi と同じコア 0 で、TCP から読んで順番待ち (FreeRTOS の stream buffer) に入れ続ける。
// 展開・描画 (このファイルの task) はコア 1 で、順番待ちから読む。これで「次のフレームを受け取る」と
// 「今のフレームを展開・描画する」が同時に進む (これまでは展開・描画の間は受信が止まっていた)。
// 順番待ちの置き場所は液晶の共用バッファの後ろ側 (display::acquireRingArea) なので、ヒープは使わない。
constexpr size_t kChunk = 1436;  // pump が 1 回に TCP から読む量 (TCP の 1 パケット)
StreamBufferHandle_t gSb = nullptr;
StaticStreamBuffer_t gSbStatic;
uint8_t* gChunk = nullptr;
TaskHandle_t gPumpTask = nullptr;
WiFiClient* volatile gPumpClient = nullptr;
volatile bool gPumpStop = false;     // 展開側が「やめて」と言った
volatile bool gPumpRunning = false;  // pump が TCP を読んでいる
volatile bool gPumpEof = false;      // 切断された (順番待ちに残っている分は読める)
// 受信を分担するか (シリアル  mirror pipe on|off  / GET /mirror?pipe=0|1。次の接続から)
volatile bool gPipeOn = true;
// 受信タスクの計測 (5 秒ごとに集計): 読んだバイト数 / 順番待ちが満杯で待った時間
volatile uint32_t gPumpBytes = 0, gPumpFullUs = 0;
uint32_t gPumpWin = 0, gPumpLastBytes = 0, gPumpLastFullUs = 0, gPumpLastWinMs = 0;

void pump(void*) {
  for (;;) {
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);  // 接続ごとに起こされる
    WiFiClient* c = gPumpClient;
    while (!gPumpStop) {
      int a = c->available();
      if (a <= 0) {
        if (!c->connected()) break;
        vTaskDelay(1);
        continue;
      }
      size_t space = xStreamBufferSpacesAvailable(gSb);
      if (space == 0) {  // 展開が追いつかない: 待つ (その間 TCP の窓が詰まって PC が待つ)
        uint32_t w0 = micros();
        vTaskDelay(1);
        gPumpFullUs += micros() - w0;
        continue;
      }
      size_t get = min((size_t)a, min(space, kChunk));
      int got = c->read(gChunk, get);
      if (got <= 0) {
        if (!c->connected()) break;
        continue;
      }
      xStreamBufferSend(gSb, gChunk, (size_t)got, 0);  // 空きは確かめてあるので待たない
      gPumpBytes += got;
    }
    gPumpEof = true;
    gPumpRunning = false;
  }
}

// 1 フレーム分 (left) を超えずに読む。展開側は 1〜9 バイトずつ欲しがるので、取り出しは 512 バイトまとめて行い
// 手元から渡す (順番待ち・TCP から 1 回ずつ取り出すと、そのたびの手間で展開が数倍遅くなる)。
// pipe = 受信タスク経由 (順番待ちから) / 直接 TCP から (受信を分担しない。比べる用)
struct Reader {
  WiFiClient* c;
  bool pipe;
  uint32_t left;    // このフレームでまだ取り出していないバイト
  uint32_t waitUs;  // データ待ちに使った時間
  size_t pos, n;
  uint8_t buf[512];
};

// 順番待ちから want バイト (ちょうど)。待った時間を waitUs に。timeoutMs = 0 なら切断されるまで待つ
bool ringRecv(uint8_t* dst, size_t want, uint32_t* waitUs, uint32_t timeoutMs) {
  uint32_t t0 = millis();
  while (want) {
    bool empty = xStreamBufferIsEmpty(gSb) == pdTRUE;
    uint32_t w0 = micros();
    size_t n = xStreamBufferReceive(gSb, dst, want, pdMS_TO_TICKS(20));
    if (empty && waitUs) *waitUs += micros() - w0;
    if (n == 0) {
      if (gPumpEof && xStreamBufferIsEmpty(gSb) == pdTRUE) return false;  // 切断されて残りも無い
      if (timeoutMs && millis() - t0 > timeoutMs) return false;
      continue;
    }
    t0 = millis();
    dst += n;
    want -= n;
  }
  return true;
}

// 手元が空になったら、このフレームの残りから最大 512 バイトを取り出す
bool refill(Reader* r) {
  if (!r->left) return false;
  size_t cap = min(sizeof(r->buf), (size_t)r->left);
  size_t got = 0;
  if (r->pipe) {
    uint32_t t0 = millis();
    while (got == 0) {  // 来ている分だけ (最大 cap) をまとめて取る
      bool empty = xStreamBufferIsEmpty(gSb) == pdTRUE;
      uint32_t w0 = micros();
      got = xStreamBufferReceive(gSb, r->buf, cap, pdMS_TO_TICKS(20));
      if (empty) r->waitUs += micros() - w0;
      if (got == 0) {
        if (gPumpEof && xStreamBufferIsEmpty(gSb) == pdTRUE) return false;
        if (millis() - t0 > 3000) return false;
      }
    }
  } else {
    uint32_t w0 = micros(), t0 = millis();
    int a;
    while ((a = r->c->available()) <= 0) {
      if (!r->c->connected() || millis() - t0 > 3000) return false;
      vTaskDelay(1);
    }
    int g = r->c->read(r->buf, min((size_t)a, cap));
    r->waitUs += micros() - w0;
    if (g <= 0) return false;
    got = (size_t)g;
  }
  r->pos = 0;
  r->n = got;
  r->left -= got;
  return true;
}

bool readFn(void* ctx, uint8_t* dst, size_t want) {
  Reader* r = (Reader*)ctx;
  while (want) {
    if (r->pos == r->n && !refill(r)) return false;
    size_t k = min(want, r->n - r->pos);
    memcpy(dst, r->buf + r->pos, k);
    r->pos += k;
    dst += k;
    want -= k;
  }
  return true;
}

void discardRest(Reader& r) {
  uint8_t junk[64];
  while (r.pos < r.n || r.left) {
    size_t k = min((size_t)sizeof(junk), (size_t)(r.n - r.pos + r.left));
    if (!readFn(&r, junk, k)) break;
  }
}

// 次のフレームの頭 (8 バイト) を待つ。切断されたら false
bool readHeader(WiFiClient& c, bool pipe, uint8_t* hdr) {
  if (pipe) return ringRecv(hdr, 8, nullptr, 0);
  size_t got = 0;
  while (got < 8) {
    if (!c.connected() && !c.available()) return false;
    int a = c.available();
    if (a <= 0) {
      vTaskDelay(1);
      continue;
    }
    int r = c.read(hdr + got, min((size_t)a, 8 - got));
    if (r > 0) got += r;
  }
  return true;
}

// 受信の分担を始める (接続ごと)。順番待ちの置き場所が借りられなければ false
bool pumpStart(WiFiClient* c) {
  size_t area = 0;
  uint8_t* mem = display::acquireRingArea(&area, 1000);
  if (!mem) return false;
  gChunk = mem;
  size_t sbSize = area - kChunk - 1;  // stream buffer の置き場所は「大きさ + 1」バイト要る
  gSb = xStreamBufferCreateStatic(sbSize, 1, mem + kChunk, &gSbStatic);
  gPumpStop = false;
  gPumpEof = false;
  gPumpRunning = true;
  gPumpClient = c;
  xTaskNotifyGive(gPumpTask);
  return true;
}

// 受信の分担をやめる (pump が TCP を読み終えるのを待ってから順番待ちを返す)
void pumpStop() {
  if (!gSb) return;
  gPumpStop = true;
  uint32_t t0 = millis();
  while (gPumpRunning && millis() - t0 < 1000) vTaskDelay(1);
  vStreamBufferDelete(gSb);
  gSb = nullptr;
  display::releaseRingArea();
}

void task(void*) {
  WiFiServer server(FRAME_STREAM_PORT);
  bool started = false;
  bool pipe = false;  // この接続で受信を分担しているか
  WiFiClient client;
  for (;;) {
    if (!WiFi.isConnected()) {
      gClient = false;
      vTaskDelay(pdMS_TO_TICKS(500));
      continue;
    }
    if (!started) {
      server.begin();
      server.setNoDelay(true);
      started = true;
      Serial.printf("[STREAM] mirror stream on tcp port %d (receive on core 0, draw on core 1)\n", FRAME_STREAM_PORT);
    }
    if (!gClient) {
      client = server.available();
      if (!client) {
        vTaskDelay(pdMS_TO_TICKS(50));
        continue;
      }
      client.setNoDelay(true);
      pipe = gPipeOn;
      if (pipe && !pumpStart(&client)) {  // 共用バッファを JPEG の POST などが使っている
        Serial.println("[STREAM] frame buffer busy, closing");
        client.stop();
        continue;
      }
      gClient = true;
      display::requestKeyframe();  // つなぎ直したら全体から
      Serial.printf("[STREAM] client %s connected (%s)\n", client.remoteIP().toString().c_str(),
                    pipe ? "receive on core 0" : "receive in the draw task");
    }
    bool close = false;
    uint8_t hdr[8];
    if (!readHeader(client, pipe, hdr)) {  // 次のフレームが来るまで待つ (切断されたら false)
      close = true;
    } else if (memcmp(hdr, "AKMF", 4) != 0) {
      Serial.println("[STREAM] bad header, closing");
      close = true;
    } else {
      uint32_t len = hdr[4] | hdr[5] << 8 | hdr[6] << 16 | (uint32_t)hdr[7] << 24;
      static Reader rd;  // 512 バイトの手元を持つのでスタックに置かない
      rd.c = &client;
      rd.pipe = pipe;
      rd.left = len;
      rd.waitUs = 0;
      rd.pos = rd.n = 0;
      uint8_t head[2];
      if (len < 2 || !readFn(&rd, head, 2)) {
        close = true;
      } else {
        // 先頭 2 バイトは読んでしまったので、展開側には付け直して渡す
        struct Pre {
          Reader* r;
          uint8_t h[2];
          int used;
        } pre{&rd, {head[0], head[1]}, 0};
        auto readPre = [](void* ctx, uint8_t* dst, size_t n) -> bool {
          Pre* p = (Pre*)ctx;
          while (n && p->used < 2) {
            *dst++ = p->h[p->used++];
            --n;
          }
          return n == 0 || readFn(p->r, dst, n);
        };
        if (head[0] == 'B' && (head[1] == 'C' || head[1] == '6')) {
          // 'K' = OK / 'R' = 次はキーフレームが欲しい / 'E' = この形式は使えない (メモリ不足。PC は面を減らす)
          int res = head[1] == '6' ? display::drawBad16(readPre, &pre, len, &rd.waitUs)
                                   : display::drawBadCodec(readPre, &pre, len, &rd.waitUs);
          if (res == 0) {
            Serial.println("[STREAM] block frame failed");
            close = true;
          } else {
            discardRest(rd);  // このフレームの残り (メモリ不足で展開しなかったときは全部)
            if (res < 0) {
              display::releaseBad16();
              client.write((uint8_t)'E');
            } else {
              client.write((uint8_t)(display::needKeyframe() || res == 4 ? 'R' : 'K'));
            }
          }
        } else if (head[0] == 0xFF && head[1] == 0xD8) {
          display::releaseBad16();
          // JPEG も受信しながら展開する (共用バッファは順番待ちに貸しているので使わない)
          if (!display::showJpegStream(readPre, &pre, len)) Serial.println("[STREAM] frame decode failed");
          discardRest(rd);
          client.write((uint8_t)'K');
        } else {
          Serial.println("[STREAM] unknown frame, closing");
          close = true;
        }
      }
    }
    if (close || (pipe && gPumpEof && xStreamBufferIsEmpty(gSb) == pdTRUE)) {
      if (pipe) pumpStop();  // 先に pump を止めてから切る (pump がまだ TCP を読んでいることがある)
      client.stop();
      display::releaseBad16();  // 面のメモリと共用バッファを返す
      gClient = false;
    }
  }
}

}  // namespace

void begin() {
#if HAS_TFT
  // 展開・描画: Arduino の loop() と同じコア 1・同じ優先度。
  // 受信 (pump): Wi-Fi と同じコア 0。Wi-Fi / BLE の処理 (優先度 18 以上) より低い優先度で、TCP から読むだけ
  xTaskCreatePinnedToCore(pump, "frame_pump", 2560, nullptr, 2, &gPumpTask, 0);
  xTaskCreatePinnedToCore(task, "frame_stream", 4096, nullptr, 1, nullptr, 1);
#endif
}

bool clientConnected() { return gClient; }

void setPipe(bool on) {
  gPipeOn = on;
  Serial.printf("[STREAM] receive on core 0: %s (from the next connection)\n", on ? "on" : "off");
}

bool pipeOn() { return gPipeOn; }

String pipeJson() {
  uint32_t now = millis();
  if (gPumpWin == 0) gPumpWin = now;
  if (now - gPumpWin >= 5000) {  // 5 秒ごとに集計し直す
    gPumpLastBytes = gPumpBytes;
    gPumpLastFullUs = gPumpFullUs;
    gPumpLastWinMs = now - gPumpWin;
    gPumpBytes = gPumpFullUs = 0;
    gPumpWin = now;
  }
  uint32_t win = gPumpLastWinMs ? gPumpLastWinMs : 1;
  char b[160];
  snprintf(b, sizeof(b), "{\"on\":%s,\"active\":%s,\"kbps\":%.0f,\"full_pct\":%.1f,\"stack_free\":%u}",
           gPipeOn ? "true" : "false", gSb ? "true" : "false", gPumpLastBytes * 8.0f / win,
           gPumpLastFullUs / 10.0f / win,
           gPumpTask ? (unsigned)uxTaskGetStackHighWaterMark(gPumpTask) : 0U);
  return String(b);
}

}  // namespace frame_stream
