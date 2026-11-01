#pragma once
#include <stdint.h>
// 液晶ミラーを USB シリアル (有線) で受け取る (Wi-Fi を通さない。遅さの原因の切り分け用)。
//
// シリアルで "stream <baud>" を受けたら "OK STREAM <baud>" を返して速さを切り替え、
// Wi-Fi の専用ポート (frame_stream) と同じ  "AKMF" + 長さ(4 バイト LE) + 本文  を受け取って液晶に描く。
// 1 枚ごとの返事は 0xA5 0x5A + 'K' / 'R' / 'E' の 3 バイト (同じ線に流れるログの文字と見分けるため)。
// 長さ 0 のフレームか、3 秒何も来なければ元の速さ (SERIAL_BAUD) に戻ってコマンド受付に戻る。
// 展開・描画は Wi-Fi のときと同じ関数 (display::drawBad16 など) を使うので、違いは通信路だけになる。
namespace serial_stream {
void run(uint32_t baud);  // シリアル受信タスクから呼ぶ (終わるまで戻らない)
}  // namespace serial_stream
