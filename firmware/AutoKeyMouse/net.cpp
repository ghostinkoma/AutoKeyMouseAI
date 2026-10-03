#include "net.h"

#include <ESPmDNS.h>
#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>

#include "ble_hid.h"
#include "config.h"
#include "display.h"
#include "macro.h"
#include "web_ui.h"

namespace net {
namespace {

WebServer gServer(80);
Preferences gPrefs;
String gSsid, gPass;
uint32_t gLastAttempt = 0;     // 最後に WiFi.begin した時刻
uint32_t gLastUp = 0;          // 最後に STA が接続していた時刻
uint32_t gUpSince = 0;         // 今回の STA 接続が始まった時刻 (0 = 未接続)
bool gApOn = false;
volatile bool gGotIp = false;
volatile int gLastReason = 0;      // 最後の切断理由 (0 = なし)
int gLoggedReason = -1;
uint32_t gLoggedAt = 0;

// 他タスク (シリアルの対話設定) からの変更要求。loop() 側で適用する
SemaphoreHandle_t gReqLock;
bool gReqPending = false;
String gReqSsid, gReqPass;

constexpr uint32_t kApFallbackMs = 30000;  // STA がこの時間つながらなければ AP を出す
constexpr uint32_t kRetryMs = 60000;       // 自動再接続が止まっていたときの保険
constexpr uint32_t kApOffDelayMs = 10000;  // STA 接続後、AP を止めるまでの猶予

void startAp() {
  if (gApOn) return;
  WiFi.mode(WIFI_AP_STA);
  WiFi.softAPConfig(IPAddress(192, 168, 4, 1), IPAddress(192, 168, 4, 1),
                    IPAddress(255, 255, 255, 0));
  WiFi.softAP(AP_SSID, AP_PASS);
  WiFi.setTxPower(WIFI_TX_POWER);
  gApOn = true;
  Serial.printf("[WIFI] SoftAP ON  ssid=%s pass=%s  http://%s/\n", AP_SSID, AP_PASS,
                WiFi.softAPIP().toString().c_str());
}

void stopAp() {
  if (!gApOn) return;
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_STA);
  gApOn = false;
  Serial.println("[WIFI] SoftAP OFF (STA connected)");
}

void connectSta() {
  if (gSsid.isEmpty()) return;
  gLastAttempt = millis();
  gLastReason = 0;
  gLoggedReason = -1;
  Serial.printf("[WIFI] connecting to \"%s\" ...\n", gSsid.c_str());
  // 接続処理中に begin すると "sta is connecting, cannot set config" になるので一度止める
  WiFi.disconnect();
  delay(100);
  WiFi.begin(gSsid.c_str(), gPass.c_str());
  WiFi.setTxPower(WIFI_TX_POWER);
}

const char* reasonHint(int r) {
  switch (r) {
    case WIFI_REASON_NO_AP_FOUND:
      return "SSID not found: check spelling/case/spaces, and that it is a 2.4GHz network";
    case WIFI_REASON_AUTH_FAIL:
    case WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT:
    case WIFI_REASON_HANDSHAKE_TIMEOUT:
    case WIFI_REASON_MIC_FAILURE:
      return "wrong password?";
    case WIFI_REASON_AUTH_EXPIRE:
    case WIFI_REASON_ASSOC_FAIL:
    case WIFI_REASON_CONNECTION_FAIL:
      return "the router refused the connection (MAC filter / WPA3-only / too far?)";
    default:
      return "";
  }
}

void onWifiEvent(arduino_event_id_t event, arduino_event_info_t info) {
  switch (event) {
    case ARDUINO_EVENT_WIFI_STA_CONNECTED:
      Serial.printf("[WIFI] associated (ch %d), waiting for DHCP...\n",
                    info.wifi_sta_connected.channel);
      break;
    case ARDUINO_EVENT_WIFI_STA_GOT_IP:
      gGotIp = true;
      Serial.printf("[WIFI] got IP %s  gw %s  rssi %d dBm\n", WiFi.localIP().toString().c_str(),
                    WiFi.gatewayIP().toString().c_str(), WiFi.RSSI());
      Serial.printf("[WIFI] Web UI: http://%s/  (http://autokeymouse.local/)\n",
                    WiFi.localIP().toString().c_str());
      break;
    case ARDUINO_EVENT_WIFI_STA_DISCONNECTED: {
      wifi_err_reason_t r = (wifi_err_reason_t)info.wifi_sta_disconnected.reason;
      // 8 / 36 (LEAVING) はこちらから切っただけなので無視する
      bool self = r == WIFI_REASON_ASSOC_LEAVE || r == WIFI_REASON_STA_LEAVING;
      if (!self) gLastReason = r;
      // 同じ理由の連続は 30 秒に 1 回だけ出す
      if (!self && (r != gLoggedReason || millis() - gLoggedAt > 30000)) {
        gLoggedReason = r;
        gLoggedAt = millis();
        const char* hint = reasonHint(r);
        Serial.printf("[WIFI] disconnected: reason %d (%s)%s%s\n", r, WiFi.disconnectReasonName(r),
                      *hint ? " -> " : "", hint);
      }
      gGotIp = false;
      break;
    }
    case ARDUINO_EVENT_WIFI_AP_STACONNECTED:
      Serial.println("[WIFI] a client joined the SoftAP");
      break;
    default:
      break;
  }
}

String jsonEscape(const String& s) {
  String o;
  o.reserve(s.length() + 8);
  for (size_t i = 0; i < s.length(); ++i) {
    char c = s[i];
    switch (c) {
      case '"': o += "\\\""; break;
      case '\\': o += "\\\\"; break;
      case '\n': o += "\\n"; break;
      case '\r': o += "\\r"; break;
      case '\t': o += "\\t"; break;
      default:
        if ((uint8_t)c < 0x20) {
          char buf[8];
          snprintf(buf, sizeof(buf), "\\u%04x", c);
          o += buf;
        } else {
          o += c;
        }
    }
  }
  return o;
}

void logRequest(int code) {
  if (gServer.uri() == "/status") return;  // Web UI が毎秒呼ぶので出さない
  Serial.printf("[HTTP] %s %s -> %d\n", gServer.method() == HTTP_GET ? "GET" : "POST",
                gServer.uri().c_str(), code);
}

void sendText(int code, const String& msg) {
  logRequest(code);
  gServer.sendHeader("Access-Control-Allow-Origin", "*");
  gServer.send(code, "text/plain; charset=utf-8", msg + "\n");
}

void sendJson(const String& json) {
  logRequest(200);
  gServer.sendHeader("Access-Control-Allow-Origin", "*");
  gServer.sendHeader("Cache-Control", "no-store");
  gServer.send(200, "application/json", json);
}

int httpCode(macro::Result r) {
  switch (r) {
    case macro::Result::Ok: return 200;
    case macro::Result::ParseError: return 400;
    case macro::Result::NotConnected: return 503;
    case macro::Result::Busy: return 409;
    case macro::Result::Aborted: return 409;
    case macro::Result::Timeout: return 504;
  }
  return 500;
}

// ---------------------------------------------------------------- handlers --

void handleRoot() {
  logRequest(200);
  gServer.send_P(200, "text/html; charset=utf-8", kIndexHtml);
}

// GET /macro?id=N[&repeat=R]  R=0 で停止まで繰り返し
void handleMacro() {
  long id = gServer.arg("id").toInt();
  if (!gServer.hasArg("id") || id < 1 || id > MACRO_SLOTS) {
    sendText(400, "bad id (1-" + String(MACRO_SLOTS) + ")");
    return;
  }
  long repeat = gServer.hasArg("repeat") ? gServer.arg("repeat").toInt() : 1;
  if (repeat < 0) {
    sendText(400, "bad repeat");
    return;
  }
  macro::Slot s = macro::slot(id);
  String err;
  macro::Result r = macro::start(s.script, repeat, "#" + String(id) + " " + s.name, &err);
  sendText(httpCode(r), r == macro::Result::Ok ? "started #" + String(id)
                        : r == macro::Result::ParseError ? err
                                                          : String(macro::resultText(r)));
}

// GET|POST /run?s=<script>  完了まで待って結果を返す (PC 側ボット用)
void handleRun() {
  if (!gServer.hasArg("s")) {
    sendText(400, "missing s");
    return;
  }
  String err;
  macro::Result r = macro::runSync(gServer.arg("s"), SYNC_RUN_TIMEOUT_MS, &err);
  sendText(httpCode(r), r == macro::Result::ParseError ? err : String(macro::resultText(r)));
}

void handleStop() {
  macro::stop();
  sendText(200, "stopping");
}

void handleStatus() {
  String j = "{";
  j += "\"ble\":" + String(hid::connected() ? "true" : "false");
  j += ",\"blePeers\":" + String(hid::connectedPeers());
  j += ",\"wifi\":" + String(staConnected() ? "true" : "false");
  j += ",\"ssid\":\"" + jsonEscape(gSsid) + "\"";
  j += ",\"ip\":\"" + staIp() + "\"";
  j += ",\"apIp\":\"" + (gApOn ? WiFi.softAPIP().toString() : String("")) + "\"";
  j += ",\"busy\":" + String(macro::busy() ? "true" : "false");
  j += ",\"current\":\"" + jsonEscape(macro::currentLabel()) + "\"";
  j += ",\"last\":\"" + jsonEscape(macro::lastResult()) + "\"";
  j += ",\"delay\":" + String(macro::eventDelayMs());
  j += ",\"layout\":\"" + String(macro::layout() == keymap::Layout::JP ? "jp" : "us") + "\"";
  j += ",\"uptime\":" + String(millis() / 1000);
  j += "}";
  sendJson(j);
}

void handleMacros() {
  String j = "[";
  for (uint8_t i = 1; i <= MACRO_SLOTS; ++i) {
    macro::Slot s = macro::slot(i);
    if (i > 1) j += ",";
    j += "{\"id\":" + String(i) + ",\"name\":\"" + jsonEscape(s.name) + "\",\"script\":\"" +
         jsonEscape(s.script) + "\"}";
  }
  j += "]";
  sendJson(j);
}

// POST /save  id, name, s
void handleSave() {
  long id = gServer.arg("id").toInt();
  if (id < 1 || id > MACRO_SLOTS) {
    sendText(400, "bad id");
    return;
  }
  String err;
  if (!macro::validate(gServer.arg("s"), &err)) {
    sendText(400, err);
    return;
  }
  macro::saveSlot(id, gServer.arg("name"), gServer.arg("s"));
  sendText(200, "saved #" + String(id));
}

// GET /settings?delay=10&layout=jp
void handleSettings() {
  if (gServer.hasArg("delay")) {
    long d = gServer.arg("delay").toInt();
    if (d < MIN_EVENT_DELAY_MS || d > MAX_EVENT_DELAY_MS) {
      sendText(400, "delay must be " + String(MIN_EVENT_DELAY_MS) + "-" + String(MAX_EVENT_DELAY_MS));
      return;
    }
    macro::setEventDelayMs(d);
  }
  if (gServer.hasArg("layout")) {
    String l = gServer.arg("layout");
    if (l == "jp") macro::setLayout(keymap::Layout::JP);
    else if (l == "us") macro::setLayout(keymap::Layout::US);
    else {
      sendText(400, "layout must be jp or us");
      return;
    }
  }
  sendText(200, "ok");
}

// POST /wifi  ssid, pass  → NVS に保存して接続し直す
void handleWifi() {
  String ssid = gServer.arg("ssid");
  ssid.trim();
  sendText(200, ssid.isEmpty() ? "STA disabled" : "connecting to " + ssid);
  delay(100);
  setCredentials(ssid, gServer.arg("pass"));
}

// POST /frame  本文 = 240x135 の RGB565 (ビッグエンディアン) 64800 バイト
// PC 側ボットが縮小したゲーム画面を送ってくる。受信しながら液晶に流し込む
bool gFrameOk = false;

void handleFrameBody() {
  HTTPRaw& raw = gServer.raw();
  if (raw.status == RAW_START) {
    gFrameOk = display::frameBegin(display::FRAME_W, display::FRAME_H);
  } else if (raw.status == RAW_WRITE) {
    if (gFrameOk) display::frameData(raw.buf, raw.currentSize);
  } else if (raw.status == RAW_END || raw.status == RAW_ABORTED) {
    if (gFrameOk) gFrameOk = display::frameEnd() && raw.status == RAW_END;
  }
}

void handleFrameDone() {
  gServer.sendHeader("Access-Control-Allow-Origin", "*");
  if (gFrameOk) gServer.send(200, "text/plain", "ok\n");
  else gServer.send(HAS_TFT ? 400 : 501, "text/plain", HAS_TFT ? "bad frame (240x135 RGB565 = 64800 bytes)\n" : "no display\n");
}

void handleNotFound() { sendText(404, "not found"); }

}  // namespace

void applyCredentials(const String& ssid, const String& pass);

void begin() {
  gReqLock = xSemaphoreCreateMutex();
  gPrefs.begin("wifi", false);
  gSsid = gPrefs.getString("ssid", "");
  gPass = gPrefs.getString("pass", "");

  WiFi.persistent(false);
  WiFi.onEvent(onWifiEvent);
  // 注意: BLE と併用するため WiFi.setSleep(false) にはしないこと。
  // Wi-Fi/BT コイグジスタンスはモデムスリープ有効が前提 (無効にすると起動時に abort する)。
  WiFi.setAutoReconnect(true);
  if (gSsid.isEmpty()) {
    Serial.println("[WIFI] no Wi-Fi configured. Type 'wifi' + Enter in the serial monitor to set it up.");
    startAp();
  } else {
    WiFi.mode(WIFI_STA);
    connectSta();
  }

  if (MDNS.begin("autokeymouse")) MDNS.addService("http", "tcp", 80);

  gServer.on("/", HTTP_GET, handleRoot);
  gServer.on("/macro", HTTP_GET, handleMacro);
  gServer.on("/run", handleRun);
  gServer.on("/stop", handleStop);
  gServer.on("/status", HTTP_GET, handleStatus);
  gServer.on("/macros", HTTP_GET, handleMacros);
  gServer.on("/save", HTTP_POST, handleSave);
  gServer.on("/settings", handleSettings);
  gServer.on("/wifi", HTTP_POST, handleWifi);
  gServer.on("/frame", HTTP_POST, handleFrameDone, handleFrameBody);
  gServer.onNotFound(handleNotFound);
  gServer.begin();
}

void loop() {
  gServer.handleClient();

  if (gReqPending) {
    xSemaphoreTake(gReqLock, portMAX_DELAY);
    String ssid = gReqSsid, pass = gReqPass;
    gReqPending = false;
    xSemaphoreGive(gReqLock);
    applyCredentials(ssid, pass);
  }

  uint32_t now = millis();
  bool up = staConnected();
  if (up) {
    gLastUp = now;
    if (!gUpSince) gUpSince = now;
    // STA でつながったら AP は不要 (同じ無線で AP を出し続けるとチャネルが固定されて不安定になる)
    if (gApOn && now - gUpSince > kApOffDelayMs) stopAp();
    return;
  }
  gUpSince = 0;
  if (gSsid.isEmpty()) {
    startAp();
    return;
  }
  // STA がしばらくつながらなければ AP を出して設定できるようにする
  if (!gApOn && now - gLastUp > kApFallbackMs && now - gLastAttempt > kApFallbackMs / 2) {
    Serial.println("[WIFI] STA not connected, enabling SoftAP as fallback");
    startAp();
  }
  // オートリコネクトの保険
  if (now - gLastAttempt > kRetryMs) connectSta();
}

void applyCredentials(const String& ssid, const String& pass) {
  gSsid = ssid;
  gPass = pass;
  gPrefs.putString("ssid", gSsid);
  gPrefs.putString("pass", gPass);
  Serial.printf("[WIFI] saved ssid=\"%s\" (pass %u chars)\n", gSsid.c_str(), (unsigned)gPass.length());
  gLastUp = millis();  // ここから AP フォールバックの時間を数える
  if (gSsid.isEmpty()) {
    WiFi.disconnect();
    startAp();
  } else {
    if (!gApOn) WiFi.mode(WIFI_STA);
    connectSta();
  }
}

int lastDisconnectReason() { return gLastReason; }
String lastDisconnectText() {
  int r = gLastReason;
  if (!r) return "";
  String t = String(r) + " (" + WiFi.disconnectReasonName((wifi_err_reason_t)r) + ")";
  const char* hint = reasonHint(r);
  if (*hint) t += " -> " + String(hint);
  return t;
}

void setCredentials(const String& ssid, const String& pass) {
  xSemaphoreTake(gReqLock, portMAX_DELAY);
  gReqSsid = ssid;
  gReqPass = pass;
  gReqPending = true;
  xSemaphoreGive(gReqLock);
}

String ssid() { return gSsid; }
bool apActive() { return gApOn; }
String apIp() { return gApOn ? WiFi.softAPIP().toString() : String(""); }
bool staConfigured() { return !gSsid.isEmpty(); }

bool staConnected() { return WiFi.status() == WL_CONNECTED; }
String staIp() { return staConnected() ? WiFi.localIP().toString() : String(""); }

}  // namespace net
