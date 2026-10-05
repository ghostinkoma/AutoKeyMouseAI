#include "ble_hid.h"

#include "config.h"

#include <Arduino.h>
#include <NimBLEDevice.h>
#include <NimBLEHIDDevice.h>

namespace hid {
namespace {

constexpr uint8_t RID_KEYBOARD = 1;
constexpr uint8_t RID_MOUSE    = 2;
constexpr uint8_t RID_ABSMOUSE = 3;

// clang-format off
const uint8_t kReportMap[] = {
  // ---- Keyboard (Report ID 1) ----
  0x05, 0x01,        // Usage Page (Generic Desktop)
  0x09, 0x06,        // Usage (Keyboard)
  0xA1, 0x01,        // Collection (Application)
  0x85, RID_KEYBOARD,
  0x05, 0x07,        //   Usage Page (Keyboard/Keypad)
  0x19, 0xE0, 0x29, 0xE7,  // Usage Min/Max (LeftCtrl..RightGUI)
  0x15, 0x00, 0x25, 0x01,
  0x75, 0x01, 0x95, 0x08,
  0x81, 0x02,        //   Input (Data,Var,Abs)  modifiers
  0x95, 0x01, 0x75, 0x08,
  0x81, 0x01,        //   Input (Const)         reserved
  0x95, 0x05, 0x75, 0x01,
  0x05, 0x08,        //   Usage Page (LEDs)
  0x19, 0x01, 0x29, 0x05,
  0x91, 0x02,        //   Output (Data,Var,Abs) LEDs
  0x95, 0x01, 0x75, 0x03,
  0x91, 0x01,        //   Output (Const)        padding
  0x95, 0x06, 0x75, 0x08,
  0x15, 0x00, 0x26, 0xFF, 0x00,
  0x05, 0x07,
  0x19, 0x00, 0x2A, 0xFF, 0x00,
  0x81, 0x00,        //   Input (Data,Array)    6 keys (JIS キー 0x87-0x8B も含む)
  0xC0,

  // ---- Relative mouse (Report ID 2) ----
  0x05, 0x01, 0x09, 0x02,
  0xA1, 0x01,
  0x85, RID_MOUSE,
  0x09, 0x01,
  0xA1, 0x00,        //   Collection (Physical)
  0x05, 0x09, 0x19, 0x01, 0x29, 0x05,
  0x15, 0x00, 0x25, 0x01,
  0x95, 0x05, 0x75, 0x01,
  0x81, 0x02,        //     buttons 1-5
  0x95, 0x01, 0x75, 0x03,
  0x81, 0x01,        //     padding
  0x05, 0x01,
  0x09, 0x30, 0x09, 0x31, 0x09, 0x38,  // X, Y, Wheel
  0x15, 0x81, 0x25, 0x7F,
  0x75, 0x08, 0x95, 0x03,
  0x81, 0x06,        //     Input (Data,Var,Rel)
  0xC0,
  0xC0,

  // ---- Absolute mouse (Report ID 3) ----
  0x05, 0x01, 0x09, 0x02,
  0xA1, 0x01,
  0x85, RID_ABSMOUSE,
  0x09, 0x01,
  0xA1, 0x00,
  0x05, 0x09, 0x19, 0x01, 0x29, 0x05,
  0x15, 0x00, 0x25, 0x01,
  0x95, 0x05, 0x75, 0x01,
  0x81, 0x02,
  0x95, 0x01, 0x75, 0x03,
  0x81, 0x01,
  0x05, 0x01,
  0x09, 0x30, 0x09, 0x31,
  0x16, 0x00, 0x00, 0x26, 0xFF, 0x7F,  // 0..32767
  0x75, 0x10, 0x95, 0x02,
  0x81, 0x02,        //     Input (Data,Var,Abs)
  0x09, 0x38,
  0x15, 0x81, 0x25, 0x7F,
  0x75, 0x08, 0x95, 0x01,
  0x81, 0x06,
  0xC0,
  0xC0,
};
// clang-format on

NimBLEHIDDevice*      gHid      = nullptr;
NimBLECharacteristic* gKbIn     = nullptr;
NimBLECharacteristic* gMouseIn  = nullptr;
NimBLECharacteristic* gAbsIn    = nullptr;

volatile uint32_t gPeers   = 0;
volatile bool     gSecured = false;

uint8_t gMods    = 0;
uint8_t gKeys[6] = {0};
uint8_t gButtons = 0;

class ServerCallbacks : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer* server, NimBLEConnInfo& info) override {
    gPeers = gPeers + 1;
    // 接続間隔を要求 (最終的な値はホストが決める)。短すぎると Wi-Fi の受信が遅くなる (config.h 参照)
    server->updateConnParams(info.getConnHandle(), BLE_CONN_INTERVAL_MIN, BLE_CONN_INTERVAL_MAX, 0, 400);
    Serial.printf("[BLE] connected: %s\n", info.getAddress().toString().c_str());
    // ホスト任せにせず、こちらから暗号化 (ペアリング / ボンド復元) を要求する
    NimBLEDevice::startSecurity(info.getConnHandle());
  }

  void onDisconnect(NimBLEServer*, NimBLEConnInfo& info, int reason) override {
    if (gPeers > 0) gPeers = gPeers - 1;
    if (gPeers == 0) gSecured = false;
    Serial.printf("[BLE] disconnected (reason=%d), advertising again\n", reason);
    NimBLEDevice::startAdvertising();
  }

  void onAuthenticationComplete(NimBLEConnInfo& info) override {
    gSecured = info.isEncrypted();
    Serial.printf("[BLE] auth complete: encrypted=%d bonded=%d\n",
                  info.isEncrypted(), info.isBonded());
    if (!info.isEncrypted()) {
      Serial.println("[BLE] encryption failed. Remove AutoKeyMouse in Windows Bluetooth settings and pair again.");
    }
  }
};

uint32_t gRetries = 0;
uint32_t gDropped = 0;

// 送信バッファが一杯だと notify が失敗してキーを取りこぼすので、空くまで待って再送する
void send(NimBLECharacteristic* chr, const uint8_t* data, size_t len) {
  if (!connected()) return;
  for (int i = 0; i < 40; ++i) {  // 最大 約 200ms
    if (chr->notify(data, len)) return;
    ++gRetries;
    vTaskDelay(pdMS_TO_TICKS(5));
    if (!connected()) return;
  }
  ++gDropped;
  Serial.printf("[BLE] report dropped (total %u)\n", (unsigned)gDropped);
}

void sendKeyboard() {
  uint8_t r[8] = {gMods, 0, gKeys[0], gKeys[1], gKeys[2], gKeys[3], gKeys[4], gKeys[5]};
  send(gKbIn, r, sizeof(r));
}

void sendMouse(int8_t dx, int8_t dy, int8_t wheel) {
  uint8_t r[4] = {gButtons, (uint8_t)dx, (uint8_t)dy, (uint8_t)wheel};
  send(gMouseIn, r, sizeof(r));
}

}  // namespace

void begin(const char* deviceName, const char* manufacturer) {
  NimBLEDevice::init(deviceName);
  // ボンディングあり / MITM なし / Secure Connections。表示・入力手段がないので Just Works。
  NimBLEDevice::setSecurityAuth(true, false, true);
  NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);

  NimBLEServer* server = NimBLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());

  gHid     = new NimBLEHIDDevice(server);
  gKbIn    = gHid->getInputReport(RID_KEYBOARD);
  gMouseIn = gHid->getInputReport(RID_MOUSE);
  gAbsIn   = gHid->getInputReport(RID_ABSMOUSE);
  gHid->getOutputReport(RID_KEYBOARD);  // LED 出力レポート (内容は使わない)

  gHid->setManufacturer(manufacturer);
  gHid->setPnp(0x02, 0xE502, 0xA111, 0x0210);
  gHid->setHidInfo(0x00, 0x01);
  gHid->setReportMap((uint8_t*)kReportMap, sizeof(kReportMap));
  gHid->setBatteryLevel(100);
  server->start();

  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  // Windows 11 の「基本」検出でも一覧に出るよう、外観はキーボードにする
  adv->setAppearance(0x03C1);  // HID Keyboard
  adv->addServiceUUID(gHid->getHidService()->getUUID());
  adv->setName(deviceName);
  adv->enableScanResponse(true);
  adv->start();
  Serial.printf("[BLE] advertising as \"%s\"  address %s  bonds %d\n", deviceName,
                NimBLEDevice::getAddress().toString().c_str(), NimBLEDevice::getNumBonds());
}

bool connected() { return gPeers > 0 && gSecured; }

void clearBonds() {
  NimBLEDevice::deleteAllBonds();
  Serial.println("[BLE] all bonds deleted. Remove AutoKeyMouse in Windows too, then pair again.");
}
uint32_t connectedPeers() { return gPeers; }
uint32_t sendRetries() { return gRetries; }
uint32_t droppedReports() { return gDropped; }

bool advertising() { return NimBLEDevice::getAdvertising()->isAdvertising(); }

void loop() {
  static uint32_t last = 0;
  if (millis() - last < 30000) return;
  last = millis();
  if (gPeers == 0 && !advertising()) {
    Serial.println("[BLE] advertising had stopped, restarting");
    NimBLEDevice::startAdvertising();
  }
  if (!connected()) {
    Serial.printf("[BLE] waiting for host: advertising=%d peers=%u bonds=%d (pair \"%s\" from Windows)\n",
                  advertising(), (unsigned)gPeers, NimBLEDevice::getNumBonds(), DEVICE_NAME);
  }
}

void keyDown(uint8_t code) {
  if (code >= 0xE0 && code <= 0xE7) {
    gMods |= (1 << (code - 0xE0));
    sendKeyboard();
    return;
  }
  for (uint8_t k : gKeys)
    if (k == code) return;
  for (uint8_t& k : gKeys) {
    if (k == 0) {
      k = code;
      break;
    }
  }
  sendKeyboard();
}

void keyUp(uint8_t code) {
  if (code >= 0xE0 && code <= 0xE7) {
    gMods &= ~(1 << (code - 0xE0));
  } else {
    for (uint8_t& k : gKeys)
      if (k == code) k = 0;
  }
  sendKeyboard();
}

void modifiersDown(uint8_t mods) {
  gMods |= mods;
  sendKeyboard();
}

void modifiersUp(uint8_t mods) {
  gMods &= ~mods;
  sendKeyboard();
}

void releaseKeys() {
  gMods = 0;
  memset(gKeys, 0, sizeof(gKeys));
  sendKeyboard();
}

void buttonsDown(uint8_t mask) {
  gButtons |= mask;
  sendMouse(0, 0, 0);
}

void buttonsUp(uint8_t mask) {
  gButtons &= ~mask;
  sendMouse(0, 0, 0);
}

void moveRelStep(int8_t dx, int8_t dy) { sendMouse(dx, dy, 0); }

void wheelStep(int8_t w) { sendMouse(0, 0, w); }

void moveAbs(uint16_t x, uint16_t y) {
  if (!connected()) return;
  if (x > ABS_MAX) x = ABS_MAX;
  if (y > ABS_MAX) y = ABS_MAX;
  uint8_t r[6] = {0, (uint8_t)(x & 0xFF), (uint8_t)(x >> 8),
                  (uint8_t)(y & 0xFF), (uint8_t)(y >> 8), 0};
  send(gAbsIn, r, sizeof(r));
}

void releaseAll() {
  gMods = 0;
  memset(gKeys, 0, sizeof(gKeys));
  gButtons = 0;
  sendKeyboard();
  sendMouse(0, 0, 0);
}

}  // namespace hid
