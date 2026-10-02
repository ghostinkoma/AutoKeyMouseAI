#include "ble_hid.h"

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
    // 7.5ms - 15ms 間隔を要求 (最終的な値はホストが決める)
    server->updateConnParams(info.getConnHandle(), 6, 12, 0, 200);
    Serial.printf("[BLE] connected: %s\n", info.getAddress().toString().c_str());
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
  }
};

void sendKeyboard() {
  if (!connected()) return;
  uint8_t r[8] = {gMods, 0, gKeys[0], gKeys[1], gKeys[2], gKeys[3], gKeys[4], gKeys[5]};
  gKbIn->setValue(r, sizeof(r));
  gKbIn->notify();
}

void sendMouse(int8_t dx, int8_t dy, int8_t wheel) {
  if (!connected()) return;
  uint8_t r[4] = {gButtons, (uint8_t)dx, (uint8_t)dy, (uint8_t)wheel};
  gMouseIn->setValue(r, sizeof(r));
  gMouseIn->notify();
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
  adv->setAppearance(0x03C0);  // HID Generic
  adv->addServiceUUID(gHid->getHidService()->getUUID());
  adv->setName(deviceName);
  adv->enableScanResponse(true);
  adv->start();
  Serial.println("[BLE] advertising");
}

bool connected() { return gPeers > 0 && gSecured; }
uint32_t connectedPeers() { return gPeers; }

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
  gAbsIn->setValue(r, sizeof(r));
  gAbsIn->notify();
}

void releaseAll() {
  gMods = 0;
  memset(gKeys, 0, sizeof(gKeys));
  gButtons = 0;
  sendKeyboard();
  sendMouse(0, 0, 0);
}

}  // namespace hid
