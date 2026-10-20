#include "edge_win.h"

#include <Arduino.h>
#include <LittleFS.h>

#include "edge_win_core.h"
#include "edge_win_model.h"

namespace edge_win {
namespace {

constexpr uint32_t PERIOD_MS = 300;   // 判定の間隔
constexpr uint32_t STALE_MS = 3000;   // これより画面が来なければ「分からない」(状態画面などに戻った)
constexpr int CONFIRM = 2;            // 同じ答えが続いたら確定 (一瞬のちらつきで変わらないように)
constexpr const char* PATH = "/win.bin";

uint8_t* gBuf = nullptr;
volatile uint32_t gLastFeed = 0;
volatile bool gDirty = false;
uint32_t gLastRun = 0, gRunUs = 0;
bool gFs = false;

Model gModel;
uint8_t* gOwned = nullptr;  // Wi-Fi / フラッシュから読んだモデルのバイト列 (組み込みのときは nullptr)
const char* gSrc = "none";

struct Found {
  int8_t label = -1;  // 名前の番号 (-1 = 無い)
  float p = 0;
  int8_t pending = -1;
  uint8_t count = 0;
};
Found gSlots[MAX_SLOT];

void resetSlots() {
  for (auto& s : gSlots) s = Found();
}

bool ensureBuffer() {
  if (!gBuf) gBuf = (uint8_t*)calloc(BW * BH, 1);
  return gBuf != nullptr;
}

void announce() {
  Serial.printf("[WIN] model %08lx (%s): %d windows, %d slots, buffer %s\n", (unsigned long)gModel.id, gSrc,
                gModel.K - 1, gModel.nslot, gBuf ? "16KB" : "FAILED");
}

// フラッシュの /win.bin を読む (壊れていれば使わない)
bool loadFromFlash() {
  if (!gFs || !LittleFS.exists(PATH)) return false;
  File f = LittleFS.open(PATH, "r");
  if (!f) return false;
  size_t n = f.size();
  uint8_t* d = n && n <= MODEL_MAX ? (uint8_t*)malloc(n) : nullptr;
  bool ok = d && f.read(d, n) == n;
  f.close();
  Model m;
  if (!ok || !parseModel(d, n, m) || !m.ok()) {
    free(d);
    Serial.println("[WIN] /win.bin is broken, ignored");
    return false;
  }
  gModel = m;
  gOwned = d;
  gSrc = "flash";
  return true;
}

}  // namespace

void begin() {
  gFs = LittleFS.begin(true);  // 初回はフォーマット (16MB のパーティション表の spiffs 領域)
  if (!gFs) Serial.println("[WIN] LittleFS not available (Wi-Fi で届いたモデルは再起動で消えます)");
  if (!loadFromFlash()) {
    Model m;
    if (edge_win_builtin::SIZE && parseModel(edge_win_builtin::BLOB, edge_win_builtin::SIZE, m) && m.ok()) {
      gModel = m;
      gSrc = "built-in";
    }
  }
  if (!gModel.ok()) {
    Serial.println("[WIN] no window model yet (PC の window_train.py が学習すると Wi-Fi で届きます)");
    return;
  }
  ensureBuffer();  // 起動直後 (Wi-Fi・BLE より前) に取るとメモリが割れにくい
  announce();
}

String setModel(uint8_t* data, size_t len) {
  Model m;
  if (!data) return "no memory";
  if (!parseModel(data, len, m) || !m.ok()) {
    free(data);
    return "bad model (format / CRC)";
  }
  if (m.id == gModel.id && gModel.ok()) {
    free(data);
    return "";
  }
  if (!ensureBuffer()) {
    free(data);
    return "no memory for the 16KB buffer";
  }
  uint8_t* old = gOwned;
  gModel = m;  // loop() と同じタスク (Web サーバー) から呼ばれるので、判定の途中で変わることは無い
  gOwned = data;
  gSrc = "wifi";
  free(old);
  resetSlots();
  if (gFs) {
    File f = LittleFS.open(PATH, "w");
    bool saved = f && f.write(data, len) == len;
    if (f) f.close();
    if (!saved) Serial.println("[WIN] could not save /win.bin");
  }
  announce();
  return "";
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
  if (!gBuf || !gModel.ok()) return;
  uint32_t now = millis();
  if (now - gLastRun < PERIOD_MS) return;
  gLastRun = now;
  if (now - gLastFeed > STALE_MS) {
    resetSlots();
    return;
  }
  if (!gDirty) return;
  gDirty = false;
  uint32_t t0 = micros();
  // 描画中に読むことがあるが、半分古い画面でも次の判定で直るので鍵はかけない
  for (int i = 0; i < gModel.nslot; i++) {
    const SlotDef& sl = gModel.slots[i];
    uint8_t bits[32];
    float feats[3], conf = 0;
    slotFeatures(gBuf, sl.x, sl.y, sl.w, sl.h, bits, feats);
    int k = classify(gModel, bits, feats, &conf);
    // そのスロットに開かない窓の答え・自信の無い答えは「無い」(最後の番号が「無い」)
    int got = (k != gModel.K - 1 && (sl.mask >> k & 1) && conf >= gModel.thr) ? k : -1;
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
      if (got >= 0) Serial.printf("[WIN] open: %s (%.2f)\n", gModel.name(got), conf);
      else Serial.printf("[WIN] closed: slot %d\n", i);
    }
  }
  gRunUs = micros() - t0;
}

String json() {
  char id[12];
  snprintf(id, sizeof(id), "%08lx", (unsigned long)gModel.id);
  String j = "{\"on\":";
  j += (gBuf && gModel.ok()) ? "true" : "false";
  j += ",\"id\":\"" + String(gModel.ok() ? id : "") + "\",\"src\":\"" + String(gSrc) + "\",\"windows\":[";
  bool first = true;
  for (int i = 0; i < gModel.nslot; i++) {
    if (gSlots[i].label < 0) continue;
    if (!first) j += ",";
    first = false;
    j += "{\"name\":\"" + String(gModel.name(gSlots[i].label)) + "\",\"p\":" + String(gSlots[i].p, 2) + "}";
  }
  j += "],\"ms\":" + String(gRunUs / 1000.f, 1) + "}";
  return j;
}

String line() {
  if (!gBuf || !gModel.ok()) return "off";
  String s;
  for (int i = 0; i < gModel.nslot; i++) {
    if (gSlots[i].label < 0) continue;
    if (s.length()) s += ", ";
    s += String(gModel.name(gSlots[i].label)) + " " + String(gSlots[i].p, 2);
  }
  return s.length() ? s : String("none");
}

}  // namespace edge_win
