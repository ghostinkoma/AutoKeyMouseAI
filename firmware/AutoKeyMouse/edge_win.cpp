#include "edge_win.h"

#include <Arduino.h>
#include <esp_partition.h>

#include <new>

#include "edge_win_core.h"
#include "edge_win_model.h"

namespace edge_win {
namespace {

constexpr uint32_t PERIOD_MS = 300;   // 判定の間隔
constexpr uint32_t STALE_MS = 3000;   // これより画面が来なければ「分からない」(状態画面などに戻った)
constexpr int CONFIRM = 2;            // 同じ答えが続いたら確定 (一瞬のちらつきで変わらないように)

Gray* gGray = nullptr;  // 8KB。モデルがあるときだけ
volatile uint32_t gLastFeed = 0;
volatile bool gDirty = false;
uint32_t gLastRun = 0, gRunUs = 0;

Model gModel;
const char* gSrc = "none";
const esp_partition_t* gPart = nullptr;  // モデルを置くフラッシュの領域 (パーティション表の spiffs)
esp_partition_mmap_handle_t gMap = 0;
bool gMapped = false;
size_t gUpLen = 0;
uint8_t* gRam = nullptr;  // spiffs 領域が無いときだけ、受け取ったモデルをメモリに置く

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

bool ensureGray() {
  if (!gGray) gGray = new (std::nothrow) Gray();
  return gGray != nullptr;
}

void announce() {
  Serial.printf("[WIN] model %08lx (%s): %d windows, %d slots, gray %s\n", (unsigned long)gModel.id, gSrc,
                gModel.K - 1, gModel.nslot, gGray ? "8KB" : "FAILED");
}

void unmap() {
  if (gMapped) esp_partition_munmap(gMap);
  gMapped = false;
}

// フラッシュの領域にあるモデルを (メモリに読まずに) 使う
bool loadFromFlash() {
  if (!gPart) return false;
  const void* p = nullptr;
  if (esp_partition_mmap(gPart, 0, MODEL_MAX, ESP_PARTITION_MMAP_DATA, &p, &gMap) != ESP_OK) return false;
  gMapped = true;
  const uint8_t* d = (const uint8_t*)p;
  size_t n = modelSize(d);
  Model m;
  if (n == 0 || n > MODEL_MAX || !parseModel(d, n, m) || !m.ok()) {
    unmap();
    return false;
  }
  gModel = m;
  gSrc = "flash";
  return true;
}

}  // namespace

void begin() {
  gPart = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_SPIFFS, nullptr);
  if (gPart && gPart->size < MODEL_MAX) gPart = nullptr;
  if (!gPart) Serial.println("[WIN] no spiffs partition: Wi-Fi で届いたモデルはメモリに置き、再起動で消えます");
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
  ensureGray();  // 起動直後 (Wi-Fi・BLE より前) に取るとメモリが割れにくい
  announce();
}

String uploadBegin() {
  // 受信の間は判定しない (Web サーバーと loop() は同じタスクなので、判定の途中で変わることは無い)
  gModel = Model();
  gSrc = "none";
  resetSlots();
  unmap();
  free(gRam);
  gRam = nullptr;
  gUpLen = 0;
  if (gPart) {
    if (esp_partition_erase_range(gPart, 0, MODEL_MAX) != ESP_OK) return "flash erase failed";
    return "";
  }
  gRam = (uint8_t*)malloc(MODEL_MAX);
  return gRam ? "" : "no memory";
}

String uploadWrite(const uint8_t* data, size_t len) {
  if (gUpLen + len > MODEL_MAX) return "model too large";
  if (gPart) {
    if (esp_partition_write(gPart, gUpLen, data, len) != ESP_OK) return "flash write failed";
  } else if (gRam) {
    memcpy(gRam + gUpLen, data, len);
  } else {
    return "no memory";
  }
  gUpLen += len;
  return "";
}

String uploadEnd() {
  if (gPart) {
    if (!loadFromFlash()) return "bad model (format / CRC)";
  } else {
    Model m;
    if (!gRam || !parseModel(gRam, gUpLen, m) || !m.ok()) return "bad model (format / CRC)";
    gModel = m;
    gSrc = "wifi (ram)";
  }
  if (!ensureGray()) {
    gModel = Model();
    return "no memory for the 8KB gray image";
  }
  resetSlots();
  announce();
  return "";
}

void feedBlock(int x, int y, int w, int h, const uint16_t* px, int stride) {
  if (!gGray) return;
  edge_win::feedBlock(gGray, x, y, w, h, px, stride);
  gDirty = true;
  gLastFeed = millis();
}

void feedFill(int x, int y, int w, int h, uint16_t color) {
  if (!gGray) return;
  edge_win::feedFill(gGray, x, y, w, h, color);
  gDirty = true;
  gLastFeed = millis();
}

void loop() {
  if (!gGray || !gModel.ok()) return;
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
    slotFeatures(*gGray, sl.x, sl.y, sl.w, sl.h, bits, feats);
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
  j += (gGray && gModel.ok()) ? "true" : "false";
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
  if (!gGray || !gModel.ok()) return "off";
  String s;
  for (int i = 0; i < gModel.nslot; i++) {
    if (gSlots[i].label < 0) continue;
    if (s.length()) s += ", ";
    s += String(gModel.name(gSlots[i].label)) + " " + String(gSlots[i].p, 2);
  }
  return s.length() ? s : String("none");
}

}  // namespace edge_win
