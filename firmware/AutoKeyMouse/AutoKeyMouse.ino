// AutoKeyMouse - ESP32-S3 Wi-Fi → BLE HID (キーボード + マウス) マクロデバイス
//
// 指示の入口は 2 つ:
//   * HTTP (Web UI / API)  ... net.cpp
//   * USB シリアル (COM)   ... 1 行 1 コマンド。PC 側ボットの低遅延経路
//       run <script>   同期実行して "OK <結果>" / "ERR <理由>" を返す
//       macro <id> [repeat]
//       stop           実行中のマクロを中断 ("STOPPED" を返す)
//       status
//       wifi           対話形式で Wi-Fi (SSID / パスワード) を設定して接続
//       wifi clear     Wi-Fi 設定を消す (SoftAP のみになる)
//       ble clear      BLE のペアリング情報を消す
//       help
#include <WiFi.h>

#include "ble_hid.h"
#include "config.h"
#include "display.h"
#include "macro.h"
#include "net.h"

namespace {

QueueHandle_t gLines;  // シリアルから受けた 1 行 (String*)

// 対話入力中のエコーバック (モニタは打った文字を表示しないため)
enum class Echo : uint8_t { Off, Plain, Masked };
volatile Echo gEcho = Echo::Off;

// 対話入力: 次の 1 行を待つ。タイムアウトなら false
bool readLine(String* out, Echo echo, uint32_t timeoutMs) {
  gEcho = echo;
  String* item = nullptr;
  bool ok = xQueueReceive(gLines, &item, pdMS_TO_TICKS(timeoutMs)) == pdTRUE;
  gEcho = Echo::Off;
  if (!ok) {
    Serial.println("\n[WIFI] timeout, cancelled");
    return false;
  }
  *out = *item;
  delete item;
  return true;
}

const char* authName(wifi_auth_mode_t m) {
  switch (m) {
    case WIFI_AUTH_OPEN: return "open";
    case WIFI_AUTH_WEP: return "WEP";
    case WIFI_AUTH_WPA_PSK: return "WPA";
    case WIFI_AUTH_WPA2_PSK: return "WPA2";
    case WIFI_AUTH_WPA_WPA2_PSK: return "WPA/WPA2";
    case WIFI_AUTH_WPA3_PSK: return "WPA3";
    case WIFI_AUTH_WPA2_WPA3_PSK: return "WPA2/WPA3";
    default: return "?";
  }
}

// 1 回分の入力と接続。つながったら true
// *again: 失敗したのでもう一度聞くべきなら true (キャンセル / タイムアウトなら false)
bool wifiWizardOnce(bool* again) {
  *again = false;
  Serial.println("\n=== Wi-Fi setup ===");
  Serial.println("[WIFI] scanning...");
  if (!net::staConnected()) {
    WiFi.disconnect();  // 再接続を繰り返している最中だとスキャンが失敗するので止める
    delay(200);
  }
  int n = WiFi.scanNetworks();
  if (n <= 0) Serial.println("[WIFI] no networks found (you can still type the SSID)");
  for (int i = 0; i < n && i < 20; ++i) {
    Serial.printf("  %2d) %-32s %4d dBm  ch%-2d %s\n", i + 1, WiFi.SSID(i).c_str(), (int)WiFi.RSSI(i),
                  (int)WiFi.channel(i), authName(WiFi.encryptionType(i)));
  }
  Serial.println("  * ESP32 is 2.4GHz only. A 5GHz-only network will not appear in this list.");
  Serial.println("  * Typing the number is safer than typing the name (case and spaces must match).");

  Serial.print("SSID ? (number or name, empty = cancel): ");
  String ssid;
  if (!readLine(&ssid, Echo::Plain, 120000)) return false;
  ssid.trim();
  if (ssid.isEmpty()) {
    Serial.println("[WIFI] cancelled");
    WiFi.scanDelete();
    return false;
  }
  long num = ssid.toInt();
  if (num >= 1 && num <= n && String(num) == ssid) {
    ssid = WiFi.SSID(num - 1);
  } else {
    bool found = false;
    for (int i = 0; i < n; ++i) found |= WiFi.SSID(i) == ssid;
    if (!found) {
      Serial.printf("[WIFI] warning: \"%s\" is not in the scan list (typo? 5GHz? out of range?)\n",
                    ssid.c_str());
    }
  }
  WiFi.scanDelete();

  Serial.printf("Password ? (for \"%s\", empty = open network): ", ssid.c_str());
  String pass;
  if (!readLine(&pass, Echo::Masked, 120000)) return false;

  net::setCredentials(ssid, pass);
  Serial.printf("[WIFI] connecting to \"%s\" (up to 20 s)", ssid.c_str());
  uint32_t start = millis();
  delay(1000);
  while (!net::staConnected() && millis() - start < 20000) {
    delay(500);
    Serial.print('.');
  }
  Serial.println();
  if (net::staConnected()) {
    Serial.printf("[WIFI] OK  IP = %s   Web UI: http://%s/\n", net::staIp().c_str(),
                  net::staIp().c_str());
    return true;
  }
  String why = net::lastDisconnectText();
  Serial.printf("[WIFI] FAILED: %s\n", why.isEmpty() ? "timeout (no answer from the router)" : why.c_str());
  Serial.println("[WIFI] let's try again (empty SSID = stop and keep retrying in the background)");
  *again = true;
  return false;
}

void wifiWizard() {
  bool again = true;
  while (again) {
    if (wifiWizardOnce(&again)) return;
  }
}

// 起動後、Wi-Fi が未設定 / つながらないときは 1 回だけ自動で対話設定を始める
void maybeAutoWizard() {
  static bool done = false;
  if (done) return;
  uint32_t t = millis();
  if (net::staConnected()) {
    done = true;
    return;
  }
  bool unset = !net::staConfigured() && t > 3000;
  bool failing = net::staConfigured() && t > 25000;
  if (!unset && !failing) return;
  done = true;
  if (failing) {
    String why = net::lastDisconnectText();
    Serial.printf("\n[WIFI] could not connect to \"%s\": %s\n", net::ssid().c_str(),
                  why.isEmpty() ? "timeout" : why.c_str());
  }
  wifiWizard();
}

const char* resetReasonName(esp_reset_reason_t r) {
  switch (r) {
    case ESP_RST_POWERON: return "power on";
    case ESP_RST_SW: return "software restart";
    case ESP_RST_PANIC: return "CRASH (panic)";
    case ESP_RST_INT_WDT:
    case ESP_RST_TASK_WDT:
    case ESP_RST_WDT: return "WATCHDOG";
    case ESP_RST_BROWNOUT: return "BROWNOUT (supply voltage dropped)";
    case ESP_RST_EXT: return "reset button / external";
    default: return "other";
  }
}

void printHelp() {
  Serial.println("commands:");
  Serial.println("  wifi              set up Wi-Fi interactively (SSID / password)");
  Serial.println("  wifi clear        forget Wi-Fi settings (SoftAP only)");
  Serial.println("  status            show BLE / Wi-Fi / macro state");
  Serial.println("  run <script>      run a macro script (e.g. run t:hello;k:enter)");
  Serial.println("  macro <id> [rep]  run macro slot 1-10 (rep 0 = loop until stop)");
  Serial.println("  stop              stop the running macro");
  Serial.println("  tft test          fill the display red/green/blue");
  Serial.println("  ble clear         forget BLE pairings (then remove the device in Windows and pair again)");
}

void led(uint8_t r, uint8_t g, uint8_t b) {
  if (STATUS_LED_PIN < 0) return;
  const uint16_t k = STATUS_LED_BRIGHTNESS;
  rgbLedWrite(STATUS_LED_PIN, r * k / 255, g * k / 255, b * k / 255);
}

// 青点滅: BLE 未接続 / 緑: BLE 接続 / 黄: マクロ実行中 / 紫が混ざる: Wi-Fi STA 未接続
void updateLed() {
  static uint32_t last = 0;
  static bool phase = false;
  if (millis() - last < 250) return;
  last = millis();
  phase = !phase;
  bool wifi = net::staConnected();
  if (macro::busy()) led(255, 160, 0);
  else if (hid::connected()) led(wifi ? 0 : 120, 255, 0);
  else if (phase) led(wifi ? 0 : 160, 0, 255);
  else led(0, 0, 0);
}

void reply(macro::Result r, const String& err) {
  if (r == macro::Result::Ok) Serial.println("OK");
  else Serial.println("ERR " + (r == macro::Result::ParseError ? err : String(macro::resultText(r))));
}

void handleLine(String line) {
  line.trim();
  if (line.isEmpty()) return;
  String err;
  if (line.startsWith("run ")) {
    reply(macro::runSync(line.substring(4), SYNC_RUN_TIMEOUT_MS, &err), err);
  } else if (line.startsWith("macro ")) {
    String rest = line.substring(6);
    int sp = rest.indexOf(' ');
    long id = rest.toInt();
    long repeat = sp < 0 ? 1 : rest.substring(sp + 1).toInt();
    if (id < 1 || id > MACRO_SLOTS || repeat < 0) {
      Serial.println("ERR bad args");
      return;
    }
    macro::Slot s = macro::slot(id);
    reply(macro::start(s.script, repeat, "#" + String(id) + " " + s.name, &err), err);
  } else if (line == "status") {
    Serial.printf("OK uptime=%lus heap=%u ble=%d adv=%d peers=%u busy=%d wifi=%d ssid=%s ip=%s ap=%s last=%s\n",
                  (unsigned long)(millis() / 1000), (unsigned)ESP.getFreeHeap(), hid::connected(), hid::advertising(),
                  (unsigned)hid::connectedPeers(), macro::busy(), net::staConnected(),
                  net::ssid().c_str(), net::staIp().c_str(), net::apIp().c_str(),
                  macro::lastResult().c_str());
  } else if (line == "wifi") {
    wifiWizard();
  } else if (line == "wifi clear") {
    net::setCredentials("", "");
    Serial.println("OK Wi-Fi settings cleared");
  } else if (line == "tft test") {
    display::test();
  } else if (line == "ble clear") {
    hid::clearBonds();
    Serial.println("OK");
  } else if (line == "help" || line == "?") {
    printHelp();
  } else {
    Serial.println("ERR unknown command");
  }
}

// 受信タスク: stop は実行中のマクロにすぐ割り込めるようここで処理し、
// それ以外のコマンドは実行タスクへ渡す
void serialRxTask(void*) {
  String line;
  for (;;) {
    while (Serial.available()) {
      char c = Serial.read();
      Echo echo = gEcho;
      if (c == '\n' || (c == '\r' && echo != Echo::Off)) {
        if (echo != Echo::Off) Serial.println();
        if (c == '\r') {  // CR LF の LF を読み捨てる
          delay(2);
          if (Serial.peek() == '\n') Serial.read();
        }
        String trimmed = line;
        trimmed.trim();
        if (echo == Echo::Off && trimmed == "stop") {
          macro::stop();
          // OK/ERR は実行中の run の応答と区別できないので別の語で返す
          Serial.println("STOPPED");
        } else if (echo != Echo::Off || !trimmed.isEmpty()) {
          // 対話入力中は空行 (= キャンセル / パスワード無し) も渡す。パスワードは trim しない
          String* item = new String(echo == Echo::Masked ? line : trimmed);
          if (xQueueSend(gLines, &item, 0) != pdTRUE) {
            delete item;
            Serial.println("ERR busy");
          }
        }
        line = "";
      } else if (c == '\b' || c == 0x7F) {
        if (line.length()) {
          line.remove(line.length() - 1);
          if (echo != Echo::Off) Serial.print("\b \b");
        }
      } else if (c != '\r' && line.length() < 2048) {
        line += c;
        if (echo == Echo::Plain) Serial.print(c);
        else if (echo == Echo::Masked) Serial.print('*');
      }
    }
    vTaskDelay(pdMS_TO_TICKS(2));
  }
}

// 実行タスク: run は完了まで待つので loop() (Web サーバ) とは別タスクで処理する
void serialCmdTask(void*) {
  for (;;) {
    String* item = nullptr;
    if (xQueueReceive(gLines, &item, pdMS_TO_TICKS(1000)) != pdTRUE) {
      maybeAutoWizard();
      continue;
    }
    handleLine(*item);
    delete item;
  }
}

}  // namespace

void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(200);
  Serial.println("\n[AKM] AutoKeyMouse boot");
  esp_reset_reason_t rr = esp_reset_reason();
  Serial.printf("[AKM] reset reason: %d (%s)\n", (int)rr, resetReasonName(rr));
  led(0, 0, 255);

  display::begin();
  macro::begin();
  hid::begin(DEVICE_NAME, BLE_MANUFACTURER);
  net::begin();
  Serial.println("[AKM] type 'help' + Enter for serial commands");

  gLines = xQueueCreate(4, sizeof(String*));
  xTaskCreatePinnedToCore(serialRxTask, "serRx", 4096, nullptr, 2, nullptr, 1);
  xTaskCreatePinnedToCore(serialCmdTask, "serCmd", 6144, nullptr, 2, nullptr, 1);
}

void loop() {
  net::loop();
  hid::loop();
  display::loop();
  updateLed();
  delay(1);
}
