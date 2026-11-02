#include "serial_stream.h"

#include <Arduino.h>
#include <esp_heap_caps.h>

#include "config.h"
#include "display.h"

namespace serial_stream {
namespace {

constexpr uint8_t ACK0 = 0xA5, ACK1 = 0x5A;
constexpr uint32_t IDLE_MS = 3000;   // これだけ何も来なければ終わる (PC が止まった)
constexpr uint32_t READ_MS = 1000;   // フレームの途中で途切れたとみなす時間

// n バイト読む。待った時間を waitUs に足す
bool readExact(uint8_t* dst, size_t n, uint32_t* waitUs) {
  size_t got = 0;
  uint32_t t0 = millis();
  while (got < n) {
    int a = Serial.available();
    if (a <= 0) {
      if (millis() - t0 > READ_MS) return false;
      uint32_t w0 = micros();
      vTaskDelay(1);
      if (waitUs) *waitUs += micros() - w0;
      continue;
    }
    size_t k = Serial.read(dst + got, min((size_t)a, n - got));
    if (k > 0) {
      got += k;
      t0 = millis();
    }
  }
  return true;
}

// 受信しながら読む。展開側は 1〜2 バイトずつ欲しがるので、Serial からは最大 1KB ずつまとめて取り、
// ここから渡す (Serial.read を細かく呼ぶと 1 回ごとの手間で展開が数倍遅くなり、その間に受信バッファが溢れる)。
// 1 フレーム分 (left) を超えては取らない (次のフレームの頭を食べない)。
struct Reader {
  uint32_t left;    // このフレームでまだ Serial から取っていないバイト
  uint32_t waitUs;  // データ待ちに使った時間
  size_t pos, n;    // buf の中の読み位置 / 入っている量
  uint8_t buf[1024];
};
Reader* gRd = nullptr;  // 有線で受け取っている間だけ確保する (Wi-Fi のときはメモリを使わない)

bool readFn(void* ctx, uint8_t* dst, size_t want) {
  Reader* r = (Reader*)ctx;
  while (want) {
    if (r->pos == r->n) {
      if (!r->left) return false;
      uint32_t t0 = millis();
      int a;
      while ((a = Serial.available()) <= 0) {
        if (millis() - t0 > READ_MS) return false;
        uint32_t w0 = micros();
        vTaskDelay(1);
        r->waitUs += micros() - w0;
      }
      size_t get = min((size_t)a, min(sizeof(r->buf), (size_t)r->left));
      size_t got = Serial.read(r->buf, get);
      if (got == 0) continue;
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

void discardRest(Reader& r) {
  uint8_t junk[64];
  while (r.pos < r.n || r.left) {
    size_t k = min((size_t)sizeof(junk), (size_t)(r.n - r.pos + r.left));
    if (!readFn(&r, junk, k)) break;
  }
}

void ack(char c) {
  const uint8_t b[3] = {ACK0, ACK1, (uint8_t)c};
  Serial.write(b, 3);
}

// 1 枚分 (長さ len) を受け取って描き、返事をする
void handleFrame(uint32_t len) {
  Reader& rd = *gRd;
  rd.left = len;
  rd.waitUs = 0;
  rd.pos = rd.n = 0;
  uint8_t head[2];
  if (len < 2 || !readFn(&rd, head, 2)) {
    discardRest(rd);
    ack('R');
    return;
  }
  if (head[0] == 'B' && (head[1] == 'C' || head[1] == '6')) {
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
    int res = head[1] == '6' ? display::drawBad16(readPre, &pre, len, &rd.waitUs)
                             : display::drawBadCodec(readPre, &pre, len, &rd.waitUs);
    discardRest(rd);
    if (res < 0) {
      display::releaseBad16();
      ack('E');  // メモリ不足: PC は面を減らす
    } else {
      ack(res == 0 || res == 4 || display::needKeyframe() ? 'R' : 'K');
    }
    return;
  }
  // JPEG
  if (len > display::FRAME_BUF_SIZE) {
    discardRest(rd);
    ack('K');
    return;
  }
  display::releaseBad16();
  uint8_t* buf = display::acquireFrameBuffer(1000);
  if (!buf) {
    discardRest(rd);
    ack('K');
    return;
  }
  uint32_t t0 = micros();
  buf[0] = head[0];
  buf[1] = head[1];
  bool ok = readFn(&rd, buf + 2, len - 2);
  if (ok) display::noteReceive(micros() - t0, len);
  ack('K');  // 受け取った: PC は次を送ってよい (展開と次の受信が重なる)
  if (ok && !display::showFrame(buf, len)) Serial.println("[STREAM] frame decode failed");
  display::releaseFrameBuffer();
}

}  // namespace

// 受信バッファを広げて速さを変える / 元に戻す。広いバッファ (SERIAL_RX_BUF) は有線で受け取っている間だけ
// 確保する。常に確保すると、その分 Wi-Fi の受信に使える空きメモリが減り、Wi-Fi のミラーが途中で詰まる
void reopen(uint32_t baud, size_t rxBuf) {
  Serial.flush();
  Serial.end();
  Serial.setRxBufferSize(rxBuf);
  Serial.begin(baud);
}

void run(uint32_t baud) {
  gRd = (Reader*)malloc(sizeof(Reader));
  if (!gRd || heap_caps_get_largest_free_block(MALLOC_CAP_8BIT) < SERIAL_RX_BUF + 4096) {
    free(gRd);
    gRd = nullptr;
    Serial.println("ERR stream: not enough memory");
    return;
  }
  Serial.printf("OK STREAM %lu\n", (unsigned long)baud);
  Serial.flush();
  delay(30);  // PC が速さを切り替えるのを待つ
  reopen(baud, SERIAL_RX_BUF);
  display::requestKeyframe();
  uint32_t last = millis(), frames = 0;
  uint32_t sync = 0;  // 直近 4 バイト ("AKMF" を探す)
  for (;;) {
    if (Serial.available() <= 0) {
      if (millis() - last > IDLE_MS) break;
      vTaskDelay(1);
      continue;
    }
    sync = (sync << 8) | (uint8_t)Serial.read();
    if (sync != 0x414B4D46) continue;  // "AKMF"
    sync = 0;
    uint8_t lb[4];
    if (!readExact(lb, 4, nullptr)) continue;
    uint32_t len = lb[0] | lb[1] << 8 | lb[2] << 16 | (uint32_t)lb[3] << 24;
    last = millis();
    if (len == 0) break;  // PC からの終わりの合図
    if (len > 512 * 1024) continue;  // 長さが壊れている: 次の "AKMF" を探す
    handleFrame(len);
    ++frames;
    last = millis();
  }
  delay(20);
  reopen(SERIAL_BAUD, SERIAL_RX_DEFAULT);
  free(gRd);
  gRd = nullptr;
  delay(20);
  Serial.printf("\n[STREAM] serial stream end (%lu frames). back to %d bps\n", (unsigned long)frames, SERIAL_BAUD);
}

}  // namespace serial_stream
