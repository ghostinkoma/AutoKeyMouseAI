#pragma once

// CONFIG_IDF_TARGET_* (チップ種別) を判定に使うので、どのファイルから読まれても先に取り込む
#include <sdkconfig.h>

// ---- デバイス名 -----------------------------------------------------------
#define DEVICE_NAME      "AutoKeyMouse"     // BLE 表示名 / mDNS 名 (autokeymouse.local)
// ファームウェアの版 (/status と起動ログに出る。PC 側で書き込み済みか確認する)
// 画像転送 (液晶ミラー) だけのビルド: platformio.ini の -e esp32_mirror が -DAKM_MIRROR_ONLY=1 を付ける。
// BLE (キーボード・マウス) を組み込まないので、Wi-Fi が電波を独占でき (ミラーが約 3 倍速い)、メモリも空く
#ifndef AKM_MIRROR_ONLY
#define AKM_MIRROR_ONLY 0
#endif
#if AKM_MIRROR_ONLY
#define FW_VERSION "frame-stream-40-mirror"
#else
#define FW_VERSION "frame-stream-40"
#endif
#define BLE_MANUFACTURER "ghostinkoma"

// ---- Wi-Fi SoftAP ---------------------------------------------------------
#define AP_SSID "AutoKeyMouse"
#define AP_PASS "akm12345"                  // 8文字以上
// SoftAP の固定 IP は 192.168.4.1

// ---- Wi-Fi 送信出力 --------------------------------------------------------
// 既定は最大 (19.5dBm)。USB 給電だけで送信時にブラウンアウト (reset reason: BROWNOUT)
// する基板は、バッテリーを付けるか WIFI_POWER_11dBm などに下げる。
#define WIFI_TX_POWER WIFI_POWER_19_5dBm

// ---- ステータス LED (オンボード WS2812) ------------------------------------
// ESP32-S3 DevKitC-1 互換基板は GPIO48 (基板によっては 38)。
// 無印 ESP32 の基板 (LilyGO T-Display など) は RGB LED が無いので -1 (無効)。
#if defined(CONFIG_IDF_TARGET_ESP32S3)
#define STATUS_LED_PIN 48
#else
#define STATUS_LED_PIN -1
#endif
#define STATUS_LED_BRIGHTNESS 16            // 0-255

// ---- TFT (LilyGO T-Display: ST7789 135x240) ---------------------------------
// 無印 ESP32 は T-Display 前提で有効。液晶の無い基板では 0 にする。
#ifndef HAS_TFT
#if defined(CONFIG_IDF_TARGET_ESP32)
#define HAS_TFT 1
#else
#define HAS_TFT 0
#endif
#endif
#define TFT_PIN_MOSI 19
#define TFT_PIN_SCLK 18
#define TFT_PIN_CS   5
#define TFT_PIN_DC   16
#define TFT_PIN_RST  23
#define TFT_PIN_BL   4
// 液晶の SPI クロック (Hz)。ESP32 は 80MHz を割って作るので 80 / 40 / 26.7 / 20MHz ... になる。
// 80MHz は試験的: ST7789 の書き込みの上限 (約 62.5MHz) と、MOSI=GPIO19 (専用ピンでなく GPIO マトリクス経由)
// の上限 (40MHz) を超えるので、表示が乱れる・色化けすることがある。乱れたら 40000000 に戻す。
// 動かしたままシリアルの  tft spi 40  /  tft spi 80  でも切り替えられる (書き込み直し不要)。
#ifndef TFT_SPI_HZ
#define TFT_SPI_HZ 80000000
#endif
// BadCodec 16bit (ver 3) のミラーを DMA で描く (続いたブロックを帯にまとめ、送信中に次を組み立てる)。
// 表示がおかしいときは 0 にするか、シリアルで  tft dma off
#ifndef TFT_DMA
#define TFT_DMA 1
#endif

// ---- 液晶ミラーの専用 TCP 受信 (PC の mirror が接続しっぱなしで JPEG を流す) ----
#define FRAME_STREAM_PORT 5005
// BadCodec 16bit 版の面をヒープから取るとき、Wi-Fi / BLE 用に必ず残す空きメモリ (バイト)
#define BAD16_HEAP_RESERVE (16 * 1024)
// ---- BLE 接続間隔 (1.25ms 単位) -----------------------------------------------
// ESP32 は Wi-Fi と BLE が 1 つのアンテナを時分割で使う。間隔が短いほど BLE が電波を占有し、
// Wi-Fi (画面の受信) が遅くなる。24-40 = 30-50ms (キー・マウス操作には十分)
#define BLE_CONN_INTERVAL_MIN 24
#define BLE_CONN_INTERVAL_MAX 40
// スレーブレイテンシの既定 (0 = 毎回聞く)。試験は GET /radio?ble=30,30&lat=20
#define BLE_CONN_LATENCY 0

// ---- マクロ実行 -----------------------------------------------------------
#define MACRO_SLOTS            10
#define DEFAULT_EVENT_DELAY_MS 10           // HID イベント間の待ち (取りこぼし防止)
#define MIN_EVENT_DELAY_MS     5
#define MAX_EVENT_DELAY_MS     50
#define SYNC_RUN_TIMEOUT_MS    30000        // /run の最大待ち時間

#define SERIAL_BAUD 115200
// シリアルの受信バッファ。有線ミラー (stream 2000000) では 1ms に 200 バイト届くので、受け取っている間だけ広げる
// (常に広げると Wi-Fi の受信に使う空きメモリが減り、Wi-Fi のミラーが途中で詰まる)
#define SERIAL_RX_BUF 8192
#define SERIAL_RX_DEFAULT 256
