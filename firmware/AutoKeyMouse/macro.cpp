#include "macro.h"

#include <Preferences.h>

#include <atomic>
#include <vector>

#include "ble_hid.h"
#include "config.h"

namespace macro {
namespace {

enum class OpType : uint8_t {
  KeyTap, KeyDown, KeyUp, Text, MoveRel, MoveAbs, Click, BtnDown, BtnUp, Wheel, Wait, Delay, ReleaseAll
};

struct Op {
  OpType type;
  int32_t a = 0;
  int32_t b = 0;
  uint8_t keys[4] = {0};  // KeyTap/Down/Up: 押す順のキーコード (修飾含む)
  uint8_t nkeys = 0;
  String text;
};

struct Job {
  std::vector<Op> ops;
  uint32_t repeat = 1;  // 0 = 無限
  String label;
  SemaphoreHandle_t done = nullptr;  // 同期実行時のみ
  Result result = Result::Ok;
  // 同期実行の所有権: WAITING -> FINISHED (待ち側が解放) / WAITING -> DETACHED (ワーカーが解放)
  std::atomic<int> state{0};
};
constexpr int WAITING = 0, FINISHED = 1, DETACHED = 2;

QueueHandle_t gQueue;
portMUX_TYPE gMux = portMUX_INITIALIZER_UNLOCKED;
volatile bool gAbort = false;
volatile bool gBusy = false;
SemaphoreHandle_t gStrLock;  // gCurrent / gLast 保護 (Web とワーカーが別コア)
String gCurrent;
String gLast = "-";

void setStatus(const String& current, const String* last) {
  xSemaphoreTake(gStrLock, portMAX_DELAY);
  gCurrent = current;
  if (last) gLast = *last;
  xSemaphoreGive(gStrLock);
}
uint8_t gDelay = DEFAULT_EVENT_DELAY_MS;
keymap::Layout gLayout = keymap::Layout::JP;
Preferences gPrefs;

// ---------------------------------------------------------------- parser --

std::vector<String> splitScript(const String& s) {
  std::vector<String> out;
  String cur;
  for (size_t i = 0; i < s.length(); ++i) {
    char c = s[i];
    if (c == '\\' && i + 1 < s.length() && (s[i + 1] == ';' || s[i + 1] == '\\')) {
      cur += s[++i];
    } else if (c == ';' || c == '\n' || c == '\r') {
      out.push_back(cur);
      cur = "";
    } else {
      cur += c;
    }
  }
  out.push_back(cur);
  return out;
}

bool parseInt(const String& s, int32_t* v) {
  String t = s;
  t.trim();
  if (t.isEmpty()) return false;
  char* end = nullptr;
  long r = strtol(t.c_str(), &end, 10);
  if (*end != '\0') return false;
  *v = r;
  return true;
}

bool parsePair(const String& s, int32_t* a, int32_t* b) {
  int comma = s.indexOf(',');
  if (comma < 0) return false;
  return parseInt(s.substring(0, comma), a) && parseInt(s.substring(comma + 1), b);
}

bool parseButton(const String& s, int32_t* mask) {
  String t = s;
  t.trim();
  t.toUpperCase();
  if (t == "L") *mask = hid::BTN_LEFT;
  else if (t == "R") *mask = hid::BTN_RIGHT;
  else if (t == "M") *mask = hid::BTN_MIDDLE;
  else return false;
  return true;
}

bool parseKeys(const String& s, Op* op) {
  String rest = s;
  rest.trim();
  op->nkeys = 0;
  while (rest.length()) {
    int plus = rest.indexOf('+', 1);  // "+" 単体は先頭なら名前扱いしない
    String name = plus < 0 ? rest : rest.substring(0, plus);
    rest = plus < 0 ? "" : rest.substring(plus + 1);
    name.trim();
    uint8_t code = keymap::keyFromName(name.c_str());
    if (!code || op->nkeys >= sizeof(op->keys)) return false;
    op->keys[op->nkeys++] = code;
  }
  return op->nkeys > 0;
}

bool parse(const String& script, std::vector<Op>* ops, String* err) {
  int lineNo = 0;
  for (String raw : splitScript(script)) {
    ++lineNo;
    String cmd = raw;
    cmd.trim();
    if (cmd.isEmpty() || cmd[0] == '#') continue;

    int colon = cmd.indexOf(':');
    String name = colon < 0 ? cmd : cmd.substring(0, colon);
    // t: は前後の空白も文字列の一部として扱う
    String arg = colon < 0 ? "" : raw.substring(raw.indexOf(':') + 1);
    name.trim();
    name.toLowerCase();
    if (name != "t") arg.trim();

    Op op;
    bool ok = true;
    if (name == "k" || name == "kd" || name == "ku") {
      op.type = name == "k" ? OpType::KeyTap : name == "kd" ? OpType::KeyDown : OpType::KeyUp;
      ok = parseKeys(arg, &op);
    } else if (name == "t") {
      op.type = OpType::Text;
      op.text = arg;
      for (size_t i = 0; i < arg.length() && ok; ++i) {
        uint8_t c;
        bool sh;
        ok = keymap::keyFromChar(arg[i], gLayout, &c, &sh);
      }
    } else if (name == "m") {
      op.type = OpType::MoveRel;
      ok = parsePair(arg, &op.a, &op.b);
    } else if (name == "a") {
      op.type = OpType::MoveAbs;
      ok = parsePair(arg, &op.a, &op.b) && op.a >= 0 && op.b >= 0 &&
           op.a <= hid::ABS_MAX && op.b <= hid::ABS_MAX;
    } else if (name == "c") {
      op.type = OpType::Click;
      int comma = arg.indexOf(',');
      op.b = 1;
      ok = parseButton(comma < 0 ? arg : arg.substring(0, comma), &op.a) &&
           (comma < 0 || (parseInt(arg.substring(comma + 1), &op.b) && op.b > 0 && op.b <= 100));
    } else if (name == "bd" || name == "bu") {
      op.type = name == "bd" ? OpType::BtnDown : OpType::BtnUp;
      ok = parseButton(arg, &op.a);
    } else if (name == "s") {
      op.type = OpType::Wheel;
      ok = parseInt(arg, &op.a);
    } else if (name == "w") {
      op.type = OpType::Wait;
      ok = parseInt(arg, &op.a) && op.a >= 0 && op.a <= 600000;
    } else if (name == "d") {
      op.type = OpType::Delay;
      ok = parseInt(arg, &op.a) && op.a >= MIN_EVENT_DELAY_MS && op.a <= MAX_EVENT_DELAY_MS;
    } else if (name == "ra") {
      op.type = OpType::ReleaseAll;
    } else {
      ok = false;
    }
    if (!ok) {
      if (err) *err = "line " + String(lineNo) + ": invalid command '" + cmd + "'";
      return false;
    }
    ops->push_back(op);
  }
  return true;
}

// -------------------------------------------------------------- executor --

// 中断要求を見ながら待つ。中断されたら false。
bool sleepMs(uint32_t ms) {
  uint32_t start = millis();
  while (millis() - start < ms) {
    if (gAbort || !hid::connected()) return false;
    uint32_t left = ms - (millis() - start);
    vTaskDelay(pdMS_TO_TICKS(left < 5 ? left : 5) + 1);
  }
  return !gAbort && hid::connected();
}

bool execOp(const Op& op, uint8_t* delayMs) {
  auto ev = [&]() { return sleepMs(*delayMs); };
  switch (op.type) {
    case OpType::KeyTap:
      for (uint8_t i = 0; i < op.nkeys; ++i) {
        hid::keyDown(op.keys[i]);
        if (!ev()) return false;
      }
      for (int i = op.nkeys - 1; i >= 0; --i) {
        hid::keyUp(op.keys[i]);
        if (!ev()) return false;
      }
      return true;
    case OpType::KeyDown:
      for (uint8_t i = 0; i < op.nkeys; ++i) {
        hid::keyDown(op.keys[i]);
        if (!ev()) return false;
      }
      return true;
    case OpType::KeyUp:
      for (uint8_t i = 0; i < op.nkeys; ++i) {
        hid::keyUp(op.keys[i]);
        if (!ev()) return false;
      }
      return true;
    case OpType::Text:
      for (size_t i = 0; i < op.text.length(); ++i) {
        uint8_t code;
        bool shift;
        keymap::keyFromChar(op.text[i], gLayout, &code, &shift);
        if (shift) hid::keyDown(0xE1);
        hid::keyDown(code);
        if (!ev()) return false;
        hid::keyUp(code);
        if (shift) hid::keyUp(0xE1);
        if (!ev()) return false;
      }
      return true;
    case OpType::MoveRel: {
      int32_t x = op.a, y = op.b;
      while (x || y) {
        int8_t sx = constrain(x, -127, 127);
        int8_t sy = constrain(y, -127, 127);
        hid::moveRelStep(sx, sy);
        x -= sx;
        y -= sy;
        if (!ev()) return false;
      }
      return true;
    }
    case OpType::MoveAbs:
      hid::moveAbs(op.a, op.b);
      return ev();
    case OpType::Click:
      for (int32_t i = 0; i < op.b; ++i) {
        hid::buttonsDown(op.a);
        if (!ev()) return false;
        hid::buttonsUp(op.a);
        if (!ev()) return false;
      }
      return true;
    case OpType::BtnDown:
      hid::buttonsDown(op.a);
      return ev();
    case OpType::BtnUp:
      hid::buttonsUp(op.a);
      return ev();
    case OpType::Wheel: {
      int32_t w = op.a;
      while (w) {
        int8_t s = constrain(w, -127, 127);
        hid::wheelStep(s);
        w -= s;
        if (!ev()) return false;
      }
      return true;
    }
    case OpType::Wait:
      return sleepMs(op.a);
    case OpType::Delay:
      *delayMs = op.a;
      return true;
    case OpType::ReleaseAll:
      hid::releaseAll();
      return ev();
  }
  return true;
}

Result execJob(Job* job) {
  for (uint32_t n = 0; job->repeat == 0 || n < job->repeat; ++n) {
    uint8_t delayMs = gDelay;
    for (const Op& op : job->ops) {
      if (!hid::connected()) return Result::NotConnected;
      if (!execOp(op, &delayMs)) return gAbort ? Result::Aborted : Result::NotConnected;
    }
    if (job->ops.empty()) break;
    if (job->repeat == 0 && !sleepMs(1)) return gAbort ? Result::Aborted : Result::NotConnected;
  }
  return Result::Ok;
}

void workerTask(void*) {
  for (;;) {
    Job* job = nullptr;
    if (xQueueReceive(gQueue, &job, portMAX_DELAY) != pdTRUE) continue;

    gAbort = false;
    setStatus(job->label, nullptr);
    Result r = execJob(job);
    hid::releaseAll();  // 押しっぱなしを残さない
    String last = job->label + ": " + resultText(r);
    setStatus("", &last);
    gBusy = false;
    Serial.printf("[MACRO] %s\n", last.c_str());

    if (job->done) {
      job->result = r;
      int expected = WAITING;
      if (job->state.compare_exchange_strong(expected, FINISHED)) {
        xSemaphoreGive(job->done);  // 待ち側が解放する
        continue;
      }
      vSemaphoreDelete(job->done);  // DETACHED
    }
    delete job;
  }
}

// 受付 (busy フラグを立てる)。既に実行中なら false。
bool claim() {
  bool ok = false;
  portENTER_CRITICAL(&gMux);
  if (!gBusy) {
    gBusy = true;
    ok = true;
  }
  portEXIT_CRITICAL(&gMux);
  return ok;
}

// clang-format off
const char* const kDefaultSlots[MACRO_SLOTS][2] = {
  {"動作テスト: 文字入力",  "t:Hello from AutoKeyMouse\\; ok;k:enter"},
  {"MUヘルパー ON/OFF",     "k:home"},
  {"HPポーション (Q)",      "k:q"},
  {"MPポーション (W)",      "k:w"},
  {"画面中央へ",            "a:16384,16384"},
  {"右クリック",            "c:R"},
  {"エルフ自己バフ",        "# 2=Greater Defense / 3=Greater Damage / 1=攻撃スキル に割り当てておく\n"
                            "a:16384,15500;k:2;w:150;c:R;w:700;k:3;w:150;c:R;w:700;k:1"},
  {"街から狩場へ /move",    "k:enter;w:200;t:/move lorencia;k:enter"},
  {"全キー解放",            "ra"},
  {"(空き)",                ""},
};
// clang-format on

}  // namespace

void begin() {
  gPrefs.begin("akm", false);
  gDelay = constrain(gPrefs.getUChar("delay", DEFAULT_EVENT_DELAY_MS), MIN_EVENT_DELAY_MS,
                     MAX_EVENT_DELAY_MS);
  gLayout = (keymap::Layout)gPrefs.getUChar("layout", (uint8_t)keymap::Layout::JP);
  gStrLock = xSemaphoreCreateMutex();
  gQueue = xQueueCreate(1, sizeof(Job*));
  // BLE ホストタスクと同じコアで動かす (loop() / Web とは別コア)
  xTaskCreatePinnedToCore(workerTask, "macro", 8192, nullptr, 3, nullptr, 0);
}

bool validate(const String& script, String* err) {
  std::vector<Op> ops;
  return parse(script, &ops, err);
}

Result start(const String& script, uint32_t repeat, const String& label, String* err) {
  Job* job = new Job();
  if (!parse(script, &job->ops, err)) {
    delete job;
    return Result::ParseError;
  }
  if (!hid::connected()) {
    delete job;
    return Result::NotConnected;
  }
  if (!claim()) {
    delete job;
    return Result::Busy;
  }
  job->repeat = repeat;
  job->label = label;
  xQueueSend(gQueue, &job, portMAX_DELAY);
  return Result::Ok;
}

Result runSync(const String& script, uint32_t timeoutMs, String* err) {
  Job* job = new Job();
  if (!parse(script, &job->ops, err)) {
    delete job;
    return Result::ParseError;
  }
  if (!hid::connected()) {
    delete job;
    return Result::NotConnected;
  }
  if (!claim()) {
    delete job;
    return Result::Busy;
  }
  job->label = "run";
  job->done = xSemaphoreCreateBinary();
  xQueueSend(gQueue, &job, portMAX_DELAY);

  if (xSemaphoreTake(job->done, pdMS_TO_TICKS(timeoutMs)) != pdTRUE) {
    int expected = WAITING;
    if (job->state.compare_exchange_strong(expected, DETACHED)) {
      return Result::Timeout;  // 実行は継続。後始末はワーカーが行う
    }
    // 直前に完了していた: Give は確定しているので受け取る
    xSemaphoreTake(job->done, portMAX_DELAY);
  }
  Result r = job->result;
  vSemaphoreDelete(job->done);
  delete job;
  return r;
}

void stop() { gAbort = true; }
bool busy() { return gBusy; }
String currentLabel() {
  xSemaphoreTake(gStrLock, portMAX_DELAY);
  String s = gCurrent;
  xSemaphoreGive(gStrLock);
  return s;
}
String lastResult() {
  xSemaphoreTake(gStrLock, portMAX_DELAY);
  String s = gLast;
  xSemaphoreGive(gStrLock);
  return s;
}

uint8_t eventDelayMs() { return gDelay; }
void setEventDelayMs(uint8_t ms) {
  gDelay = constrain(ms, MIN_EVENT_DELAY_MS, MAX_EVENT_DELAY_MS);
  gPrefs.putUChar("delay", gDelay);
}

keymap::Layout layout() { return gLayout; }
void setLayout(keymap::Layout l) {
  gLayout = l;
  gPrefs.putUChar("layout", (uint8_t)l);
}

Slot slot(uint8_t id) {
  Slot s;
  if (id < 1 || id > MACRO_SLOTS) return s;
  String n = "n" + String(id), b = "s" + String(id);
  s.name = gPrefs.isKey(n.c_str()) ? gPrefs.getString(n.c_str()) : String(kDefaultSlots[id - 1][0]);
  s.script = gPrefs.isKey(b.c_str()) ? gPrefs.getString(b.c_str()) : String(kDefaultSlots[id - 1][1]);
  return s;
}

void saveSlot(uint8_t id, const String& name, const String& script) {
  if (id < 1 || id > MACRO_SLOTS) return;
  gPrefs.putString(("n" + String(id)).c_str(), name);
  gPrefs.putString(("s" + String(id)).c_str(), script);
}

const char* resultText(Result r) {
  switch (r) {
    case Result::Ok: return "ok";
    case Result::ParseError: return "parse error";
    case Result::NotConnected: return "BLE not connected";
    case Result::Busy: return "busy";
    case Result::Aborted: return "aborted";
    case Result::Timeout: return "timeout (still running)";
  }
  return "?";
}

}  // namespace macro
