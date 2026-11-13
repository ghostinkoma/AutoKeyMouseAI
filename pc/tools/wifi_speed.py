"""ESP32 までの Wi-Fi (TCP) の本当の速さを測る (液晶ミラーの展開・描画を挟まない。frame-stream-44 以降)。

    .\\.venv\\Scripts\\python.exe tools\\wifi_speed.py --host 192.168.1.43

仲介アプリのミラー・動画の再生は止めておく (同じポート 5005 を使う)。
ESP32 は受け取って捨てるだけなので、ミラーの「受信」の速さの上限が分かる:
  poll = (frame-stream-44) 今のミラーと同じ受け取り方 (届いていなければ 1ms 寝て見直す)
  recv = (frame-stream-44) 待ち受け (届いたらすぐ起きる)
  frame-stream-45 以降は、どちらもミラーと同じ「受け取り専用タスク → 溜め」の流れで測る (2 回測るのと同じ)
"""
import argparse
import os
import socket
import struct
import sys
import time

import _path  # noqa: F401


def one(sock, mode: bytes, n: int) -> tuple[float, float]:
    """n バイト送って、(PC で測った KB/s, ESP32 で測った KB/s)"""
    payload = os.urandom(min(n, 1 << 20))
    t0 = time.perf_counter()
    sock.sendall(mode + struct.pack("<I", n))
    left = n
    while left:
        k = min(left, len(payload))
        sock.sendall(payload[:k])
        left -= k
    ack = b""
    while len(ack) < 5:
        b = sock.recv(5 - len(ack))
        if not b:
            raise ConnectionError("ESP32 が接続を切りました (frame-stream-44 以降ですか)")
        ack += b
    dt = time.perf_counter() - t0
    if ack[:1] != b"T":
        raise RuntimeError(f"返事が違います: {ack!r} (frame-stream-44 以降が必要です)")
    us = struct.unpack("<I", ack[1:5])[0]
    return n / 1024 / dt, n / 1024 / (us / 1e6 if us else 1e-9)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="")
    ap.add_argument("--port", type=int, default=5005)
    ap.add_argument("--mb", type=float, default=2.0, help="1 回に送る量 (MB)")
    ap.add_argument("--rounds", type=int, default=3)
    a = ap.parse_args()
    host = a.host
    if not host:
        from akm.config import load_config

        host = str(((load_config("config.yaml") or {}).get("device") or {}).get("host") or "")
    if not host:
        raise SystemExit("--host で ESP32 の IP を指定してください")
    sock = socket.create_connection((host, a.port), timeout=10)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    n = int(a.mb * 1024 * 1024)
    print(f"{host}:{a.port} に {a.mb:g}MB を {a.rounds} 回ずつ送ります")
    for mode, name in ((b"AKMT", "poll (45〜: 受け取りタスク)"), (b"AKMU", "recv (45〜: 受け取りタスク)")):
        res = [one(sock, mode, n) for _ in range(a.rounds)]
        pc = [r[0] for r in res]
        esp = [r[1] for r in res]
        print(f"  {name:28} PC {sum(pc) / len(pc):6.0f}KB/s (最大 {max(pc):5.0f})  "
              f"ESP32 {sum(esp) / len(esp):6.0f}KB/s")
    sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
