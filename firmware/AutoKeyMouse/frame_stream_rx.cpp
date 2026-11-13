// 液晶ミラーの受信: 受け取りと展開を別々のタスクで (試験用ビルド esp32_mirror_rx、-DAKM_RX_TASK=1)
//   受け取り (rxTask, コア 1・優先度 5): 公式 iperf と同じく lwIP のソケットで recv だけを回して、届いた分をすぐ溜め (StreamBuffer) に入れる。
//     返事 (K / R / E / T) は展開側から同じソケットへ直接送る (lwIP の全二重)
//   展開 (task, コア 1): 溜めから読んで展開・描画する。手元に 512 バイトの置き場を持ち、空になったときだけ
//     溜めからまとめて取り出す (前の試作 frame-stream-45 は 1〜9 バイトずつ数千回取り出し、そのたびに
//     もう片方のコアを起こしていて、ミラーが止まった)
// 状態 (受け取った量・溜まっている量・返事の数・切れた理由) は 5 秒ごとにシリアルへ、/status の rx にも出す
#include "frame_stream.h"

#include "config.h"

#if AKM_RX_TASK

#include <WiFi.h>
#include <freertos/queue.h>
#include <freertos/stream_buffer.h>
#include <lwip/sockets.h>

#include "display.h"
#include "entropy.h"

namespace frame_stream {
namespace {

constexpr size_t kMax = display::FRAME_BUF_SIZE;  // JPEG の 1 枚の上限
constexpr size_t kRing = 24 * 1024;               // 溜め (受け取り → 展開)
constexpr size_t kChunk = 512;                    // 展開側が 1 回に取り出す量

StreamBufferHandle_t gRing = nullptr;

volatile bool gClient = false;
volatile uint32_t gSession = 0;  // 接続の番号 (受け取り側がつないだら増やす)
volatile bool gEof = true;       // 今の接続は切れた (溜めに残っている分は読める)
volatile bool gKill = false;     // 展開側が「この接続を切って」と言った
volatile bool gIdle = true;      // 展開側が今の接続を読み終えた (次の接続を受け付けてよい)

// 状態 (シリアルと /status)
volatile uint32_t gRxBytes = 0, gRxCalls = 0, gAcksSent = 0, gRingHigh = 0, gFrames = 0, gSessions = 0;
volatile uint32_t gDecWaitUs = 0;  // 展開側がデータを待った時間 (合計)
const char* volatile gLastEnd = "-";  // 最後に接続が終わった理由

volatile int gFd = -1;  // 今の接続のソケット (受け取り側がつないだら決める)

// 返事は展開側から直接送る (ESP-IDF の lwIP は、1 つのソケットを「片方のタスクが受け取り、もう片方が送る」
// 使い方に対応している: LWIP_NETCONN_FULLDUPLEX)。受け取り側の recv の待ち (最大 20ms) で返事が遅れない
void ack(const uint8_t* b, uint8_t n) {
  int fd = gFd;
  if (fd < 0) return;
  if (send(fd, b, n, 0) == n) gAcksSent = gAcksSent + 1;
  else gLastEnd = "ack send failed";
}
void ack(uint8_t c) { ack(&c, 1); }

// 溜めから最大 n バイト (少なくとも 1 バイト) 取り出す。届くまで待つ (timeoutMs = 0: 接続している限り)。
// 切れて残りも無い・時間切れなら 0
size_t pull(uint8_t* dst, size_t n, uint32_t timeoutMs, uint32_t* waitUs) {
  size_t got = xStreamBufferReceive(gRing, dst, n, 0);
  if (got) return got;
  uint32_t t0 = millis(), w0 = micros();
  for (;;) {
    if (gEof && xStreamBufferIsEmpty(gRing)) break;
    if (timeoutMs && millis() - t0 > timeoutMs) break;
    got = xStreamBufferReceive(gRing, dst, n, pdMS_TO_TICKS(20));  // 届いたらすぐ起きる
    if (got) break;
  }
  uint32_t w = micros() - w0;
  if (waitUs) *waitUs += w;
  gDecWaitUs = gDecWaitUs + w;
  return got;
}

// ちょうど n バイト (ヘッダーなど)
bool pullAll(uint8_t* dst, size_t n, uint32_t timeoutMs) {
  while (n) {
    size_t got = pull(dst, n, timeoutMs, nullptr);
    if (!got) return false;
    dst += got;
    n -= got;
  }
  return true;
}

// 1 フレーム分 (left) を超えては取り出さない。手元の置き場 (512B) から渡す
struct Reader {
  uint32_t left;    // このフレームでまだ溜めから取り出していないバイト
  uint32_t waitUs;  // データ待ちに使った時間
  uint8_t buf[kChunk];
  size_t pos, n;
};

IRAM_ATTR bool readFn(void* ctx, uint8_t* dst, size_t want) {
  Reader* r = (Reader*)ctx;
  while (want) {
    if (r->pos == r->n) {
      if (!r->left) return false;
      size_t got = pull(r->buf, min((size_t)kChunk, (size_t)r->left), 3000, &r->waitUs);
      if (!got) return false;
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

// 先頭 2 バイト (形式の判定に読んだ分) を付け直して展開側に渡す
struct Pre {
  Reader* r;
  uint8_t h[2];
  int used;
};

IRAM_ATTR bool readPre(void* ctx, uint8_t* dst, size_t n) {
  Pre* p = (Pre*)ctx;
  while (n && p->used < 2) {
    *dst++ = p->h[p->used++];
    --n;
  }
  return n == 0 || readFn(p->r, dst, n);
}

void discard(Reader& rd) {
  uint8_t junk[64];
  while (rd.pos < rd.n || rd.left) {
    size_t k = min((size_t)sizeof(junk), (size_t)(rd.n - rd.pos + rd.left));
    if (!readFn(&rd, junk, k)) break;
  }
}

// Wi-Fi の速さの試験 (tools/wifi_speed.py): len バイトを受け取って捨てるだけ (受け取りタスク → 溜め の流れで測る)
bool speedTest(uint32_t len) {
  static uint8_t buf[2048];
  uint32_t t0 = micros(), left = len;
  while (left) {
    size_t got = pull(buf, min((uint32_t)sizeof(buf), left), 3000, nullptr);
    if (!got) return false;
    left -= got;
  }
  uint32_t us = micros() - t0;
  uint8_t a[5] = {'T', (uint8_t)us, (uint8_t)(us >> 8), (uint8_t)(us >> 16), (uint8_t)(us >> 24)};
  ack(a, 5);
  Serial.printf("[RX] speed test: %lu bytes in %lu us = %lu KB/s\n", (unsigned long)len, (unsigned long)us,
                (unsigned long)((uint64_t)len * 1000000 / 1024 / (us ? us : 1)));
  return true;
}

// ---- 受け取り -----------------------------------------------------------------------

void report() {
  static uint32_t last = 0;
  if (millis() - last < 5000) return;
  last = millis();
  if (!gClient && gEof) return;
  size_t used = xStreamBufferBytesAvailable(gRing);
  Serial.printf("[RX] recv %lu KB (%lu calls) ring %u/%u (max %lu) acks %lu frames %lu wait %lu ms last end: %s\n",
                (unsigned long)(gRxBytes / 1024), (unsigned long)gRxCalls, (unsigned)used, (unsigned)kRing,
                (unsigned long)gRingHigh, (unsigned long)gAcksSent, (unsigned long)gFrames,
                (unsigned long)(gDecWaitUs / 1000), (const char*)gLastEnd);
}

// 公式の examples/wifi/iperf (ESP-IDF) と同じ形: Arduino の WiFiServer / WiFiClient を使わず、lwIP の
// socket → bind → listen → accept を直接使い、recv だけを回す (届くまで待つ。1 秒で時間切れ)。
// 試作 47〜49 は 1 回ごとに client.connected() (中身は 1 バイトの覗き見 recv(MSG_PEEK)) と WiFi.isConnected() を
// 呼び、20ms などで時間切れにしていて、受け取りが 1 秒に 5〜50KB しか出なかった
void rxTask(void*) {
  static uint8_t buf[4096];
  int ls = -1;
  for (;;) {
    report();
    if (ls < 0) {
      if (!WiFi.isConnected()) {
        vTaskDelay(pdMS_TO_TICKS(500));
        continue;
      }
      ls = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
      int one = 1;
      setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
      sockaddr_in a = {};
      a.sin_family = AF_INET;
      a.sin_port = htons(FRAME_STREAM_PORT);
      a.sin_addr.s_addr = htonl(INADDR_ANY);
      if (ls < 0 || bind(ls, (sockaddr*)&a, sizeof(a)) < 0 || listen(ls, 1) < 0) {
        Serial.printf("[RX] listen failed (errno %d)\n", errno);
        if (ls >= 0) close(ls);
        ls = -1;
        vTaskDelay(pdMS_TO_TICKS(1000));
        continue;
      }
      Serial.printf("[RX] mirror stream on tcp port %d (lwIP socket, rx task core 1 prio 5, ring %u bytes)\n",
                    FRAME_STREAM_PORT, (unsigned)kRing);
    }
    while (!gIdle) vTaskDelay(pdMS_TO_TICKS(5));  // 展開側が前の接続を読み終えるまで待つ
    sockaddr_in peer = {};
    socklen_t plen = sizeof(peer);
    int fd = accept(ls, (sockaddr*)&peer, &plen);  // つながるまで待つ
    if (fd < 0) {
      vTaskDelay(pdMS_TO_TICKS(100));
      continue;
    }
    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    struct timeval rt = {1, 0};  // 1 秒で時間切れ (展開側が「切って」と言ったかを見るため。普段は届くまで待つ)
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &rt, sizeof(rt));
    xStreamBufferReset(gRing);
    gKill = false;
    gIdle = false;
    gEof = false;
    gFd = fd;
    gSessions = gSessions + 1;
    gSession = gSession + 1;
    gClient = true;
    Serial.printf("[RX] client %s connected (session %lu)\n", inet_ntoa(peer.sin_addr), (unsigned long)gSession);
    for (;;) {
      int r = recv(fd, buf, sizeof(buf), 0);
      if (r > 0) {
        gRxBytes = gRxBytes + r;
        gRxCalls = gRxCalls + 1;
        size_t off = 0;
        while (off < (size_t)r && !gKill)  // 溜めがいっぱいなら空くまで待つ (TCP の窓で PC も止まる)
          off += xStreamBufferSend(gRing, buf + off, r - off, pdMS_TO_TICKS(100));
        size_t used = xStreamBufferBytesAvailable(gRing);
        if (used > gRingHigh) gRingHigh = used;
        if (gKill) break;
        report();  // 5 秒ごと (時刻を見るだけ)
        continue;
      }
      if (r < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) && !gKill) {  // 1 秒何も来ない
        report();
        continue;
      }
      if (!gKill) gLastEnd = r == 0 ? "PC closed" : "recv error";
      break;
    }
    if (gKill && strcmp((const char*)gLastEnd, "-") == 0) gLastEnd = "decoder stopped";
    gFd = -1;
    gEof = true;
    close(fd);
  }
}

// ---- 展開 (コア 1) ------------------------------------------------------------------
void task(void*) {
  uint32_t mine = 0;
  static Reader rd;  // 512B の置き場を持つのでスタックに置かない
  for (;;) {
    if (gSession == mine) {  // 次の接続を待つ
      gClient = false;
      vTaskDelay(pdMS_TO_TICKS(20));
      continue;
    }
    mine = gSession;
    gClient = true;
    display::requestKeyframe();  // つなぎ直したら全体から
    for (;;) {  // この接続のフレームを順に
      uint8_t hdr[8];
      if (!pullAll(hdr, 8, 0)) break;  // 次のフレームが来るまで待つ (切断されたら false)
      uint32_t len = hdr[4] | hdr[5] << 8 | hdr[6] << 16 | (uint32_t)hdr[7] << 24;
      if (memcmp(hdr, "AKMT", 4) == 0 || memcmp(hdr, "AKMU", 4) == 0) {  // Wi-Fi の速さの試験
        if (!speedTest(len)) {
          gLastEnd = "speed test cut";
          break;
        }
        continue;
      }
      if (memcmp(hdr, "AKMF", 4) != 0) {
        Serial.println("[RX] bad header, closing");
        gLastEnd = "bad header";
        break;
      }
      // 先頭 2 バイトで形式を見分ける: FF D8 = JPEG / 'B' 'C' = ブロック差分 / 'B' '6' = BadCodec / 'B' 'A' = tANS
      rd.left = len;
      rd.waitUs = 0;
      rd.pos = rd.n = 0;
      uint8_t head[2];
      if (len < 2 || !readFn(&rd, head, 2)) {
        gLastEnd = "frame cut";
        break;
      }
      if (head[0] == 'B' && (head[1] == 'C' || head[1] == '6' || head[1] == 'A')) {
        // 受信しながら描く。描き終わったら返事: 'K' = OK / 'R' = 次は全体が欲しい / 'E' = この形式は使えない
        Pre pre{&rd, {head[0], head[1]}, 0};
        int res;
        if (head[1] == 'A') {
          ent::Dec& d = ent::dec();
          res = ent::begin(d, head[1], readFn, &rd, len) ? display::drawBad16(ent::read, &d, d.outLeft, &rd.waitUs) : 0;
        } else {
          res = head[1] == '6' ? display::drawBad16(readPre, &pre, len, &rd.waitUs)
                               : display::drawBadCodec(readPre, &pre, len, &rd.waitUs);
        }
        if (res == 0) {
          Serial.println("[RX] block frame failed");
          gLastEnd = "block frame failed";
          break;
        }
        discard(rd);
        gFrames = gFrames + 1;
        if (res < 0) {
          display::releaseBad16();
          ack('E');
        } else {
          ack(display::needKeyframe() || res == 4 ? 'R' : 'K');
        }
        continue;
      }
      if (len > kMax) {
        Serial.printf("[RX] jpeg too large: %lu\n", (unsigned long)len);
        gLastEnd = "jpeg too large";
        break;
      }
      display::releaseBad16();  // JPEG に戻った: 共用バッファを返してもらう
      uint8_t* buf = display::acquireFrameBuffer(1000);
      if (!buf) {
        gLastEnd = "no frame buffer";
        break;
      }
      uint32_t t0 = micros();
      buf[0] = head[0];
      buf[1] = head[1];
      if (!readFn(&rd, buf + 2, len - 2)) {
        display::releaseFrameBuffer();
        gLastEnd = "jpeg cut";
        break;
      }
      display::noteReceive(micros() - t0, len);
      ack('K');  // 受信完了。PC はすぐ次を送ってよい
      if (!display::showFrame(buf, len)) Serial.println("[RX] frame decode failed");
      display::releaseFrameBuffer();
      gFrames = gFrames + 1;
    }
    // 接続を終える: 受け取り側に切ってもらい (recv の待ちを shutdown で起こす)、溜めの残りを捨てる
    gKill = true;
    if (gFd >= 0) shutdown(gFd, SHUT_RDWR);
    static uint8_t junk[512];
    while (pull(junk, sizeof(junk), 300, nullptr)) {
    }
    display::releaseBad16();  // 面のメモリと共用バッファを返す
    gClient = false;
    gIdle = true;
  }
}

}  // namespace

void begin() {
#if HAS_TFT
  gRing = xStreamBufferCreate(kRing, 1);
  if (!gRing) {
    Serial.println("[RX] no memory for the receive ring");
    return;
  }
  // 受け取り: コア 1・優先度 5 (展開・loop より上)。届いたら展開を一瞬止めて溜めに入れ、待っている間は展開が進む
  xTaskCreatePinnedToCore(rxTask, "frame_rx", 6144, nullptr, 5, nullptr, 1);
  // 展開: Arduino の loop() と同じコア 1・同じ優先度
  xTaskCreatePinnedToCore(task, "frame_stream", 4096, nullptr, 1, nullptr, 1);
#endif
}

bool clientConnected() { return gClient; }

String rxJson() {
  size_t used = gRing ? xStreamBufferBytesAvailable(gRing) : 0;
  return "{\"on\":true,\"recv_kb\":" + String(gRxBytes / 1024) + ",\"recv_calls\":" + String(gRxCalls) +
         ",\"ring\":" + String(used) + ",\"ring_size\":" + String(kRing) + ",\"ring_max\":" + String(gRingHigh) +
         ",\"acks\":" + String(gAcksSent) + ",\"frames\":" + String(gFrames) + ",\"sessions\":" + String(gSessions) +
         ",\"wait_ms\":" + String(gDecWaitUs / 1000) + ",\"last_end\":\"" + String((const char*)gLastEnd) + "\"}";
}

}  // namespace frame_stream

#endif  // AKM_RX_TASK
