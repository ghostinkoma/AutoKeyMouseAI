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
    m = Mirror(host, 1.0 / max(0.5, args.fps), fmt=mcfg.get("format", "jpeg"),
               quality=int(mcfg.get("quality", 70)), grab=screen.grab_preview,
               transport=mcfg.get("transport", "auto"), port=int(mcfg.get("port", 5005)))
    m.set_lines([("MIRROR ONLY", (0, 200, 255))])
    print(f"[mirror] {host} に毎秒 {1 / m.interval:.0f} 枚で送信中 ({m.fmt}) Ctrl+C で終了  (--fps で変更)")
    import requests

    status_url = m.url.replace("/frame", "/status")
    try:
        while True:
            time.sleep(5)
            print(f"[mirror] 送信 {m.sent} 枚  失敗 {m.errors}  {m.fps:.1f}fps  形式 {m.fmt}  {m.last_bytes} バイト/枚  "
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
