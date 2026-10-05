"""ボットを動かさずに、ゲーム画面を ESP32 の液晶に映し続ける (表示の確認用)。

    python tools\\mirror_only.py

ウィンドウから直接撮る (PrintWindow) ので、仮想デスクトップを切り替えても映り続ける。
Ctrl+C で終了。
"""
import time

import _path  # noqa: F401

from akm.config import ask_obs_password, load_config
from akm.mirror import Mirror
from akm.screen import GameScreen


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--fps", type=float, default=10.0, help="送る枚数/秒 (既定 10。config の mirror.interval_s より優先)")
    ap.add_argument("--format", choices=["bc", "bad16", "jpeg", "raw"],
                    help="bc = ブロック差分 (変わったブロックだけ) / bad16 = BadCodec 16bit 版 (ESP32-S3 + PSRAM) / jpeg / raw")
    ap.add_argument("--drop-bits", type=int, default=0, choices=[0, 1, 2, 3],
                    help="bad16 で各色の下位ビットを捨てる数 (0 = 可逆)。ESP32 に入らなければ自動で減らす")
    ap.add_argument("--color", help="bad16 の色のビット数 R G B を 3 桁で (例 343 = 10 面, 342 = 9 面, 332 = 8 面)。"
                                    "--drop-bits より優先")
    ap.add_argument("--tolerance", type=int, default=16, help="ブロック差分方式の許容誤差 (大きいほど小さく粗い)")
    ap.add_argument("--max-ratio", type=float, default=1.5,
                    help="ブロック差分が JPEG の何倍までならそのまま送るか (大きくすると JPEG を使わない。99 で常にブロック差分)")
    args = ap.parse_args()
    cfg = load_config("config.yaml")
    ask_obs_password(cfg, "config.yaml")
    screen = GameScreen.from_config(cfg)
    obs = cfg.get("obs") or {}
    print(f"[mirror] 設定: game.capture={cfg['game'].get('capture', 'auto')}  "
          + (f"obs=有効 (source={obs.get('source')}, port={obs.get('port', 4455)})" if obs.get("enabled") else
             "obs=無効 (config.yaml に obs: が無いか enabled: false)"))
    screen.locate()
    mcfg = cfg.get("mirror") or {}
    host = (cfg.get("device") or {}).get("host")
    if not host:
        raise SystemExit("config.yaml の device.host に ESP32 の IP を書いてください")
    m = Mirror(host, 1.0 / max(0.5, args.fps), fmt=args.format or mcfg.get("format", "bc"),
               quality=int(mcfg.get("quality", 70)), grab=screen.grab_preview,
               transport=mcfg.get("transport", "auto"), port=int(mcfg.get("port", 5005)))
    m.bc_tolerance = args.tolerance
    m.bc_max_ratio = args.max_ratio
    from akm.bad16 import bits_of_drop

    if args.color:
        if len(args.color) != 3 or not args.color.isdigit():
            raise SystemExit("--color は 343 のように R G B のビット数を 3 桁で")
        r, g, b = (int(c) for c in args.color)
        if not (1 <= r <= 5 and 1 <= g <= 6 and 1 <= b <= 5):
            raise SystemExit("--color は R 1-5, G 1-6, B 1-5")
        m.b16_bits = (r, g, b)
    else:
        m.b16_bits = bits_of_drop(args.drop_bits)
    m.set_lines([("MIRROR ONLY", (0, 200, 255))])
    print(f"[mirror] {host} に毎秒 {1 / m.interval:.0f} 枚で送信中 ({m.fmt}) Ctrl+C で終了  (--fps で変更)")
    import requests

    status_url = m.url.replace("/frame", "/status")
    try:
        while True:
            time.sleep(5)
            kinds = (f" (bc {m.sent_kinds['bc']} / jpeg {m.sent_kinds['jpeg']}, 大きさ bc {m.cand_bytes['bc'] / 1024:.1f}KB"
                     f" : jpeg {m.cand_bytes['jpeg'] / 1024:.1f}KB)") if m.fmt == "bc" else ""
            if m.fmt == "bad16":
                kinds = " (色 R{}G{}B{} = {} 面)".format(*m.b16_bits, sum(m.b16_bits))
            print(f"[mirror] 送信 {m.sent} 枚  失敗 {m.errors}  {m.fps:.1f}fps  形式 {m.fmt}{kinds}  {m.last_bytes} バイト/枚  "
                  f"(PC: 撮影 {m.t_capture:.0f}ms 圧縮 {m.t_encode:.0f}ms 送信 {m.t_post:.0f}ms)")
            try:
                st = requests.get(status_url, timeout=2).json()
                if "mirror" not in st:
                    print(f"         ESP32: ファームウェアが古いので計測値がありません (fw={st.get('fw', 'なし')})。"
                          "frame-stream-3 を書き込んでください")
                    continue
                e = st.get("mirror") or {}
                print(f"         ESP32: {e.get('fps', 0)}fps 受信 {e.get('recv_ms', 0)}ms 展開 {e.get('decode_ms', 0)}ms "
                      f"描画 {e.get('draw_ms', 0)}ms → CPU {e.get('cpu_pct', 0)}% (1 コア, {st.get('cpuMHz', '?')}MHz)  "
                      f"空きメモリ {st.get('heap', 0) // 1024}KB (最大連続 {st.get('heapBlock', 0) // 1024}KB, "
                      f"最小 {st.get('heapMin', 0) // 1024}KB)")
            except Exception:
                pass
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
