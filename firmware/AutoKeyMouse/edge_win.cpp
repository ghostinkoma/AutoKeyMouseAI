#include "edge_win.h"

#include <Arduino.h>

#include "edge_nn.h"
#include "edge_win_core.h"
#include "edge_win_model.h"

namespace edge_win {
namespace {

constexpr uint32_t PERIOD_MS = 300;   // 判定の間隔
constexpr uint32_t STALE_MS = 3000;   // これより画面が来なければ「分からない」(状態画面などに戻った)
constexpr int CONFIRM = 2;            // 同じ答えが続いたら確定 (一瞬のちらつきで変わらないように)

uint8_t* gBuf = nullptr;
volatile uint32_t gLastFeed = 0;
volatile bool gDirty = false;
uint32_t gLastRun = 0, gRunUs = 0;

struct Found {
  int8_t label = -1;  // edge_win::NAMES の番号 (-1 = 無い)
  float p = 0;
  int8_t pending = -1;
  uint8_t count = 0;
};
Found gSlots[edge_win::NSLOT > 0 ? edge_win::NSLOT : 1];

}  // namespace

void begin() {
  if (edge_win::NSLOT == 0) {
    Serial.println("[WIN] no window model (pc/tools/window_train.py train で作る)");
    return;
  }
  gBuf = (uint8_t*)calloc(BW * BH, 1);
  Serial.printf("[WIN] window recognition: %d slots, %d labels, buffer %s\n", edge_win::NSLOT, edge_win::K,
                gBuf ? "16KB" : "FAILED");
}

void feedBlock(int x, int y, int w, int h, const uint16_t* px, int stride) {
  if (!gBuf) return;
  edge_win::feedBlock(gBuf, x, y, w, h, px, stride);
  gDirty = true;
  gLastFeed = millis();
}

void feedFill(int x, int y, int w, int h, uint16_t color) {
  if (!gBuf) return;
  edge_win::feedFill(gBuf, x, y, w, h, color);
  gDirty = true;
  gLastFeed = millis();
}

void loop() {
  if (!gBuf) return;
  uint32_t now = millis();
  if (now - gLastRun < PERIOD_MS) return;
  gLastRun = now;
  if (now - gLastFeed > STALE_MS) {
    for (auto& s : gSlots) s = Found();
    return;
  }
  if (!gDirty) return;
  gDirty = false;
  uint32_t t0 = micros();
  // 描画中に読むことがあるが、半分古い画面でも次の判定で直るので鍵はかけない
  for (int i = 0; i < edge_win::NSLOT; i++) {
    const auto& sl = edge_win::SLOTS[i];
    uint8_t bits[32];
    float feats[3], conf = 0;
    slotFeatures(gBuf, sl.x, sl.y, sl.w, sl.h, bits, feats);
    int k = edge_nn::classify<edge_win::Model>(bits, feats, &conf);
    // そのスロットに開かない窓の答え・自信の無い答えは「無い」
    int got = (k != edge_win::NONE && (sl.mask >> k & 1) && conf >= edge_win::THRESHOLD) ? k : -1;
    Found& f = gSlots[i];
    if (got == f.label) {
      f.pending = -1;
      f.count = 0;
      if (got >= 0) f.p = conf;
      continue;
    }
    if (got != f.pending) {
      f.pending = got;
      f.count = 0;
    }
    if (++f.count >= CONFIRM) {
      f.label = got;
      f.p = got >= 0 ? conf : 0;
      f.pending = -1;
      f.count = 0;
      if (got >= 0) Serial.printf("[WIN] open: %s (%.2f)\n", edge_win::NAMES[got], conf);
      else Serial.printf("[WIN] closed: slot %d\n", i);
    }
  }
  gRunUs = micros() - t0;
}

String json() {
  String j = "{\"on\":";
  j += gBuf ? "true" : "false";
  j += ",\"windows\":[";
  bool first = true;
  for (int i = 0; i < edge_win::NSLOT; i++) {
    if (gSlots[i].label < 0) continue;
    if (!first) j += ",";
    first = false;
    j += "{\"name\":\"" + String(edge_win::NAMES[gSlots[i].label]) + "\",\"p\":" + String(gSlots[i].p, 2) + "}";
  }
  j += "],\"ms\":" + String(gRunUs / 1000.f, 1) + "}";
  return j;
}

String line() {
  if (!gBuf) return "off";
  String s;
  for (int i = 0; i < edge_win::NSLOT; i++) {
    if (gSlots[i].label < 0) continue;
    if (s.length()) s += ", ";
    s += String(edge_win::NAMES[gSlots[i].label]) + " " + String(gSlots[i].p, 2);
  }
  return s.length() ? s : String("none");
}

}  // namespace edge_win
