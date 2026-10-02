#pragma once

// ---- デバイス名 -----------------------------------------------------------
#define DEVICE_NAME      "AutoKeyMouse"     // BLE 表示名 / mDNS 名 (autokeymouse.local)
#define BLE_MANUFACTURER "ghostinkoma"

// ---- Wi-Fi SoftAP ---------------------------------------------------------
#define AP_SSID "AutoKeyMouse"
#define AP_PASS "akm12345"                  // 8文字以上
// SoftAP の固定 IP は 192.168.4.1

// ---- ステータス LED (DevKitC-1 互換基板のオンボード WS2812) -----------------
// 基板によっては GPIO38 の場合がある。LED が光らなければ変更する。
#define STATUS_LED_PIN        48
#define STATUS_LED_BRIGHTNESS 16            // 0-255

// ---- マクロ実行 -----------------------------------------------------------
#define MACRO_SLOTS            10
#define DEFAULT_EVENT_DELAY_MS 10           // HID イベント間の待ち (取りこぼし防止)
#define MIN_EVENT_DELAY_MS     5
#define MAX_EVENT_DELAY_MS     50
#define SYNC_RUN_TIMEOUT_MS    30000        // /run の最大待ち時間

#define SERIAL_BAUD 115200
