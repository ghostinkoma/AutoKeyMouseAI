#pragma once
#include <stddef.h>
#include <stdint.h>
#include <WString.h>
// TFT への状態表示 (Wi-Fi の IP / BLE / マクロ)。HAS_TFT=0 なら何もしない。
namespace display {

void begin();
void loop();  // loop() から呼ぶ。内容が変わったときだけ描き直す
void test();  // 赤・緑・青で全面を塗る (表示確認用)

// PC から送られてくる縮小画面を液晶に表示する。形式は
//   * JPEG (240x135 以下)                         … 通常はこちら (数 KB で済む)
//   * RGB565 ビッグエンディアン 240x135 = 64800 バイト
constexpr uint16_t FRAME_W = 240;
constexpr uint16_t FRAME_H = 135;
bool showFrame(const uint8_t* data, size_t len);  // 液晶が無い / 形式不正なら false (別タスクから呼んでよい)
uint32_t framesShown();

// ミラー表示の負荷計測 (5 秒ごとに集計)。PC から 10fps などで送ったとき、
// JPEG 展開・液晶描画で CPU をどれだけ使っているかを見る (エッジ AI に残る余裕の確認用)
void noteReceive(uint32_t us, size_t bytes);  // 1 フレームの受信にかかった時間
String frameStatsJson();                      // {"fps":..,"recv_ms":..,"decode_ms":..,"draw_ms":..,"cpu_pct":..}
String frameStatsLine();                      // シリアル表示用

// BlockDiff (8x8 ブロック差分のカラー版, pc/akm/bcodec.py) を受信しながら描く。
// read(dst, n) は n バイト読めたら true。recvUs には受信待ちに使った時間を足していく。
// 戻り値: 0 = 失敗, 1 = 差分フレーム, 2 = キーフレーム
typedef bool (*ReadFn)(void* ctx, uint8_t* dst, size_t n);  // bc::ReadFn と同じ型
int drawBadCodec(ReadFn read, void* ctx, uint32_t len, const uint32_t* recvUs);
// BadCodec 16bit 版 (pc/akm/bad16.py, 公式 BadCodec Protocol 514 を RGB565 の 16 面に使う) を受信しながら展開して描く。
// 送られてくる面 + 作業用 1 面 (各 4080B) をキーフレームのときに用意する。PSRAM があれば PSRAM、
// 無ければ JPEG 用の共用バッファ (6 面ぶん) + ヒープ。使っている間は共用バッファを借りたままにする。
// 戻り値: -1 = メモリ不足 (面を減らせば入るかも), 0 = 失敗, 1 = 差分, 2 = キーフレーム, 3 = 変化なし,
//         4 = 展開できない差分 (面の用意が無い。読み捨ててキーフレームを頼む)
int drawBad16(ReadFn read, void* ctx, uint32_t len, const uint32_t* recvUs);
void releaseBad16();  // 面のメモリと共用バッファを返す (接続が切れた / JPEG に戻ったとき)
bool needKeyframe();    // 液晶が上書きされた (状態画面など) ので次は全体 (キーフレーム) が欲しい
void requestKeyframe();

// 受信した JPEG を置く共用バッファ (Web サーバーの /frame と専用 TCP の両方で使う。メモリ節約のため 1 つだけ)
// 取れなければ nullptr。使い終わったら releaseFrameBuffer()。
constexpr size_t FRAME_BUF_SIZE = 24 * 1024;
uint8_t* acquireFrameBuffer(uint32_t waitMs);
void releaseFrameBuffer();

// RGB565 BE を受信しながらそのまま液晶へ流す (64,800 バイトのバッファを確保しなくて済む)
bool rawBegin();
bool rawWrite(const uint8_t* data, size_t len);  // 画素数を超えたら false
bool rawEnd();                                   // ちょうど 240x135 画素なら true
void rawAbort();

}  // namespace display
