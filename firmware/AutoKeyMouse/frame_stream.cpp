#include "frame_stream.h"

#include <WiFi.h>

#include "config.h"
#include "display.h"

namespace frame_stream {
namespace {

constexpr size_t kMax = display::FRAME_BUF_SIZE;  // JPEG 240x135 は 5〜10KB (液晶モジュールの共用バッファ)
volatile bool gClient = false;

bool readAll(WiFiClient& c, uint8_t* dst, size_t n, uint32_t timeoutMs) {
  size_t got = 0;
  uint32_t t0 = millis();
  while (got < n) {
    if (!c.connected() && !c.available()) return false;
    int a = c.available();
    if (a <= 0) {
      if (timeoutMs && millis() - t0 > timeoutMs) return false;  // 0 = 接続している限り待つ
      vTaskDelay(1);
      continue;
    }
    int r = c.read(dst + got, min((size_t)a, n - got));
    if (r > 0) {
      got += r;
      t0 = millis();
    }
  }
  return true;
}

// 受信しながら読む (BadCodec-C 用)。1 フレーム分 (len) を超えては読まない
struct Reader {
  WiFiClient* c;
  uint32_t left;      // このフレームの残りバイト
  uint32_t waitUs;    // データ待ちに使った時間
  uint8_t buf[512];
  size_t pos = 0, n = 0;
};

bool readFn(void* ctx, uint8_t* dst, size_t want) {
  Reader* r = (Reader*)ctx;
  while (want) {
    if (r->pos == r->n) {
      if (!r->left) return false;
      uint32_t w0 = micros(), t0 = millis();
      int a;
      while ((a = r->c->available()) <= 0) {
        if (!r->c->connected() || millis() - t0 > 3000) return false;
        vTaskDelay(1);
      }
      size_t get = min((size_t)a, min(sizeof(r->buf), (size_t)r->left));
      int got = r->c->read(r->buf, get);
      r->waitUs += micros() - w0;
      if (got <= 0) return false;
      r->pos = 0;
      r->n = got;
      r->left -= got;
    }
    size_t k = min(want, r->n - r->pos);
    memcpy(dst, r->buf + r->pos, k);
    r->pos += k;
    dst += k;
    want -= k;
  }
  return true;
}

void task(void*) {
  WiFiServer server(FRAME_STREAM_PORT);
  bool started = false;
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
      Serial.printf("[STREAM] mirror stream on tcp port %d\n", FRAME_STREAM_PORT);
    }
    if (!client || !client.connected()) {
      gClient = false;
      client = server.available();
      if (!client) {
        vTaskDelay(pdMS_TO_TICKS(50));
        continue;
      }
      client.setNoDelay(true);
      gClient = true;
      display::requestKeyframe();  // つなぎ直したら全体から
      Serial.printf("[STREAM] client %s connected\n", client.remoteIP().toString().c_str());
    }
    uint8_t hdr[8];
    if (!readAll(client, hdr, 8, 0)) {  // 次のフレームが来るまで待つ (切断されたら false)
      client.stop();
      continue;
    }
    if (memcmp(hdr, "AKMF", 4) != 0) {
      Serial.println("[STREAM] bad header, closing");
      client.stop();
      continue;
    }
    uint32_t len = hdr[4] | hdr[5] << 8 | hdr[6] << 16 | (uint32_t)hdr[7] << 24;
    // 先頭 2 バイトで形式を見分ける: FF D8 = JPEG / 'B' 'C' = BadCodec-C
    Reader rd;
    rd.c = &client;
    rd.left = len;
    rd.waitUs = 0;
    uint8_t head[2];
    if (len < 2 || !readFn(&rd, head, 2)) {
      client.stop();
      continue;
    }
    if (head[0] == 'B' && head[1] == 'C') {
      // 受信しながら描く (バッファ不要)。描き終わったら返事: 'K' / 'R' (次はキーフレームが欲しい)
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
      int res = display::drawBadCodec(readPre, &pre, len, &rd.waitUs);
      if (res == 0) {
        Serial.println("[STREAM] badcodec frame failed");
        client.stop();
        continue;
      }
      rd.pos = rd.n;  // 念のため、このフレームの残りを捨てる
      uint8_t junk[64];
      while (rd.left && readFn(&rd, junk, min((uint32_t)sizeof(junk), rd.left))) rd.pos = rd.n;
      client.write((uint8_t)(display::needKeyframe() ? 'R' : 'K'));
      continue;
    }
    if (len > kMax) {
      Serial.printf("[STREAM] jpeg too large: %lu\n", (unsigned long)len);
      client.stop();
      continue;
    }
    uint8_t* buf = display::acquireFrameBuffer(1000);
    if (!buf) {
      client.stop();
      continue;
    }
    uint32_t t0 = micros();
    buf[0] = head[0];
    buf[1] = head[1];
    if (!readFn(&rd, buf + 2, len - 2)) {
      display::releaseFrameBuffer();
      client.stop();
      continue;
    }
    display::noteReceive(micros() - t0, len);
    client.write((uint8_t)'K');  // 受信完了。PC はすぐ次を送ってよい (展開と次の受信が重なる)
    if (!display::showFrame(buf, len)) Serial.println("[STREAM] frame decode failed");
    display::releaseFrameBuffer();
  }
}

}  // namespace

void begin() {
#if HAS_TFT
  // Arduino の loop() と同じコア 1・同じ優先度。Wi-Fi / BLE の処理 (コア 0) は邪魔しない
  xTaskCreatePinnedToCore(task, "frame_stream", 4096, nullptr, 1, nullptr, 1);
#endif
}

bool clientConnected() { return gClient; }

}  // namespace frame_stream
