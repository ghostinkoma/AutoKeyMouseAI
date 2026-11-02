#pragma once
#include <WString.h>
// 液晶ミラー用の専用 TCP 受信 (FRAME_STREAM_PORT)。
// PC が接続しっぱなしで  "AKMF" + 長さ(4 バイト LE) + JPEG  を送り続ける。
// 受け取ったらすぐ 1 バイト 'K' を返し (PC は次のフレームの準備に入れる)、そのあと展開・描画する。
// Web サーバー (loop) とは別タスクなので、受信中もキー・マウスの命令は待たされない。
namespace frame_stream {
void begin();
bool clientConnected();
// 受信をコア 0 の別タスクに分担するか (既定 on)。次の接続から効く。比べる用に off にできる
void setPipe(bool on);
bool pipeOn();
String pipeJson();  // {"on":true,"active":true,"kbps":980,"full_pct":12.0,"stack_free":800} (受信タスクの計測)
}  // namespace frame_stream
