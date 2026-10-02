#include "net.h"

#include <ESPmDNS.h>
#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>

#include "ble_hid.h"
#include "config.h"
#include "macro.h"
#include "web_ui.h"

namespace net {
namespace {

WebServer gServer(80);
Preferences gPrefs;
String gSsid, gPass;
uint32_t gLastReconnect = 0;
bool gWasConnected = false;

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

void sendText(int code, const String& msg) {
  gServer.sendHeader("Access-Control-Allow-Origin", "*");
  gServer.send(code, "text/plain; charset=utf-8", msg + "\n");
}

void sendJson(const String& json) {
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

void handleRoot() { gServer.send_P(200, "text/html; charset=utf-8", kIndexHtml); }

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
  j += ",\"apIp\":\"" + WiFi.softAPIP().toString() + "\"";
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
  gSsid = gServer.arg("ssid");
  gPass = gServer.arg("pass");
  gPrefs.putString("ssid", gSsid);
  gPrefs.putString("pass", gPass);
  sendText(200, gSsid.isEmpty() ? "STA disabled" : "connecting to " + gSsid);
  delay(100);
  WiFi.disconnect();
  if (!gSsid.isEmpty()) WiFi.begin(gSsid.c_str(), gPass.c_str());
  gLastReconnect = millis();
}

void handleNotFound() { sendText(404, "not found"); }

}  // namespace

void begin() {
  gPrefs.begin("wifi", false);
  gSsid = gPrefs.getString("ssid", "");
  gPass = gPrefs.getString("pass", "");

  WiFi.persistent(false);
  WiFi.mode(WIFI_AP_STA);
  // 注意: BLE と併用するため WiFi.setSleep(false) にはしないこと。
  // Wi-Fi/BT コイグジスタンスはモデムスリープ有効が前提 (無効にすると起動時に abort する)。
  WiFi.softAPConfig(IPAddress(192, 168, 4, 1), IPAddress(192, 168, 4, 1),
                    IPAddress(255, 255, 255, 0));
  WiFi.softAP(AP_SSID, AP_PASS);
  WiFi.setAutoReconnect(true);
  if (!gSsid.isEmpty()) WiFi.begin(gSsid.c_str(), gPass.c_str());
  Serial.printf("[NET] SoftAP %s @ %s\n", AP_SSID, WiFi.softAPIP().toString().c_str());

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
  gServer.onNotFound(handleNotFound);
  gServer.begin();
}

void loop() {
  gServer.handleClient();

  bool up = staConnected();
  if (up != gWasConnected) {
    gWasConnected = up;
    if (up) Serial.printf("[NET] STA connected: %s  http://%s/\n", gSsid.c_str(), staIp().c_str());
    else Serial.println("[NET] STA disconnected");
  }
  // オートリコネクト (setAutoReconnect が効かなかった場合の保険)
  if (!up && !gSsid.isEmpty() && millis() - gLastReconnect > 15000) {
    gLastReconnect = millis();
    WiFi.disconnect();
    WiFi.begin(gSsid.c_str(), gPass.c_str());
  }
}

bool staConnected() { return WiFi.status() == WL_CONNECTED; }
String staIp() { return staConnected() ? WiFi.localIP().toString() : String(""); }

}  // namespace net
