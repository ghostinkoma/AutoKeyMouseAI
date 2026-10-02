// AutoKeyMouse - ESP32-S3 Wi-Fi → BLE HID (キーボード + マウス) マクロデバイス
//
// 指示の入口は 2 つ:
//   * HTTP (Web UI / API)  ... net.cpp
//   * USB シリアル (COM)   ... 1 行 1 コマンド。PC 側ボットの低遅延経路
//       run <script>   同期実行して "OK <結果>" / "ERR <理由>" を返す
//       macro <id> [repeat]
//       stop           実行中のマクロを中断 ("STOPPED" を返す)
//       status
#include "ble_hid.h"
#include "config.h"
#include "macro.h"
#include "net.h"

namespace {

QueueHandle_t gLines;  // シリアルから受けた 1 行 (String*)

void led(uint8_t r, uint8_t g, uint8_t b) {
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
    Serial.printf("OK ble=%d busy=%d wifi=%d ip=%s last=%s\n", hid::connected(), macro::busy(),
                  net::staConnected(), net::staIp().c_str(), macro::lastResult().c_str());
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
      if (c == '\n') {
        line.trim();
        if (line == "stop") {
          macro::stop();
          // OK/ERR は実行中の run の応答と区別できないので別の語で返す
          Serial.println("STOPPED");
        } else if (!line.isEmpty()) {
          String* item = new String(line);
          if (xQueueSend(gLines, &item, 0) != pdTRUE) {
            delete item;
            Serial.println("ERR busy");
          }
        }
        line = "";
      } else if (c != '\r' && line.length() < 2048) {
        line += c;
      }
    }
    vTaskDelay(pdMS_TO_TICKS(2));
  }
}

// 実行タスク: run は完了まで待つので loop() (Web サーバ) とは別タスクで処理する
void serialCmdTask(void*) {
  for (;;) {
    String* item = nullptr;
    if (xQueueReceive(gLines, &item, portMAX_DELAY) != pdTRUE) continue;
    handleLine(*item);
    delete item;
  }
}

}  // namespace

void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(200);
  Serial.println("\n[AKM] AutoKeyMouse boot");
  led(0, 0, 255);

  macro::begin();
  hid::begin(DEVICE_NAME, BLE_MANUFACTURER);
  net::begin();

  gLines = xQueueCreate(4, sizeof(String*));
  xTaskCreatePinnedToCore(serialRxTask, "serRx", 4096, nullptr, 2, nullptr, 1);
  xTaskCreatePinnedToCore(serialCmdTask, "serCmd", 6144, nullptr, 2, nullptr, 1);
}

void loop() {
  net::loop();
  updateLed();
  delay(1);
}
