"""動画を前もって変換して (.bad)、ESP32 の液晶へ元の fps で流す。

    # 変換 (YouTube の URL か手元の動画。yt-dlp と ffmpeg が要る)
    .\\.venv\\Scripts\\python.exe tools\\bad_video.py encode "https://www.youtube.com/watch?v=..." -o badapple.bad
    .\\.venv\\Scripts\\python.exe tools\\bad_video.py encode video.webm --format mono --kbps 380

    # 再生 (ESP32 の IP。仲介アプリのミラーは止めておく: 同じポートを使う)
    .\\.venv\\Scripts\\python.exe tools\\bad_video.py play badapple.bad --host 192.168.1.43
    .\\.venv\\Scripts\\python.exe tools\\bad_video.py play badapple.bad --loop

形式 (--format): index = インデックス 256 色 (既定) / 565 = BadCodec 16 ビット (可逆) / mono = 白黒 1 ビット (8x8 ディザ)
                 / jpeg = JPEG (上下 2 つ)
--kbps: 1 秒に送る量の上限 (KB)。超える 1 枚は出さず次にまとめる (Wi-Fi の実測 400〜500KB/s より少し下に)
"""
import argparse
import sys
from pathlib import Path

import _path  # noqa: F401

from akm import badvideo


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("encode", help="動画 / URL → .bad")
    e.add_argument("src", help="動画のファイルか URL")
    e.add_argument("-o", "--out", help="出力 (.bad)。省略すると元の名前 + .<形式>.bad")
    e.add_argument("--format", default="index", choices=list(badvideo.FORMATS))
    e.add_argument("--fps", type=float, default=0.0, help="省略すると元の動画の fps")
    e.add_argument("--kbps", type=float, default=380.0, help="1 秒に送る量の上限 (KB)。0 = 調整しない")
    e.add_argument("--start", type=float, default=0.0, help="始める位置 (秒)")
    e.add_argument("--duration", type=float, default=0.0, help="長さ (秒。0 = 最後まで)")
    e.add_argument("--key-every", type=float, default=10.0, help="画面全体を入れる間隔 (秒)")
    e.add_argument("--download-dir", default="data/videos", help="URL の動画を置く場所")
    p = sub.add_parser("play", help=".bad → ESP32")
    p.add_argument("file")
    p.add_argument("--host", default="", help="ESP32 の IP (省略すると config.yaml の device.host)")
    p.add_argument("--port", type=int, default=5005)
    p.add_argument("--loop", action="store_true", help="繰り返す")
    p.add_argument("--speed", type=float, default=1.0, help="再生の速さ (1 = 元の速さ)")
    a = ap.parse_args()

    if a.cmd == "encode":
        src = a.src
        if src.startswith(("http://", "https://")):
            path = badvideo.download(src, Path(a.download_dir))
            print(f"取得しました: {path}")
        else:
            path = Path(src)
            if not path.exists():
                raise SystemExit(f"見つかりません: {path}")
        out = Path(a.out) if a.out else path.with_suffix(f".{a.format}.bad")
        badvideo.encode_file(path, out, a.format, a.fps, a.kbps, a.start, a.duration, a.key_every)
        return 0
    host = a.host
    if not host:
        from akm.config import load_config

        host = str(((load_config("config.yaml") or {}).get("device") or {}).get("host") or "")
    if not host:
        raise SystemExit("--host で ESP32 の IP を指定してください")
    try:
        st = badvideo.play(Path(a.file), host, a.port, a.loop, a.speed)
    except KeyboardInterrupt:
        print("止めました")
        return 0
    print(f"終わりました: {st.sent} 枚、{st.bytes / 1024 / 1024:.1f}MB、一番の遅れ {st.late_ms:.0f}ms"
          + (f"、ESP32 が全体を求めた {st.resets} 回" if st.resets else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
