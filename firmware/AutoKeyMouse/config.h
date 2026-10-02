#pragma once

// ---- デバイス名 -----------------------------------------------------------
#define DEVICE_NAME      "AutoKeyMouse"     // BLE 表示名 / mDNS 名 (autokeymouse.local)
#define BLE_MANUFACTURER "ghostinkoma"

// ---- Wi-Fi SoftAP ---------------------------------------------------------
#define AP_SSID "AutoKeyMouse"
#define AP_PASS "akm12345"                  // 8文字以上
// SoftAP の固定 IP は 192.168.4.1

// ---- ステータス LED (オンボード WS2812) ------------------------------------
// ESP32-S3 DevKitC-1 互換基板は GPIO48 (基板によっては 38)。
// 無印 ESP32 の基板 (LilyGO T-Display など) は RGB LED が無いので -1 (無効)。
#if defined(CONFIG_IDF_TARGET_ESP32S3)
#define STATUS_LED_PIN 48
#else
#define STATUS_LED_PIN -1
#endif
#define STATUS_LED_BRIGHTNESS 16            // 0-255

// ---- マクロ実行 -----------------------------------------------------------
#define MACRO_SLOTS            10
#define DEFAULT_EVENT_DELAY_MS 10           // HID イベント間の待ち (取りこぼし防止)
#define MIN_EVENT_DELAY_MS     5
#define MAX_EVENT_DELAY_MS     50
#define SYNC_RUN_TIMEOUT_MS    30000        // /run の最大待ち時間

#define SERIAL_BAUD 115200
