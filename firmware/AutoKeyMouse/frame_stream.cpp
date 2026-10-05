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
    if (len == 0 || len > kMax) {
      Serial.printf("[STREAM] frame too large: %lu\n", (unsigned long)len);
      client.stop();
      continue;
    }
    uint8_t* buf = display::acquireFrameBuffer(1000);
    if (!buf) {
      client.stop();
      continue;
    }
    uint32_t t0 = micros();
    if (!readAll(client, buf, len, 3000)) {
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
