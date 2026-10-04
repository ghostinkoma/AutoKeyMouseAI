"""ボットを動かさずに、ゲーム画面を ESP32 の液晶に映し続ける (表示の確認用)。

    python tools\\mirror_only.py

ウィンドウから直接撮る (PrintWindow) ので、仮想デスクトップを切り替えても映り続ける。
Ctrl+C で終了。
"""
import time

import _path  # noqa: F401

from akm.config import load_config
from akm.mirror import Mirror
from akm.screen import GameScreen


def main() -> None:
    cfg = load_config("config.yaml")
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
    m = Mirror(host, float(mcfg.get("interval_s", 1.0)), fmt=mcfg.get("format", "jpeg"),
               quality=int(mcfg.get("quality", 70)), grab=screen.grab)
    m.set_lines([("MIRROR ONLY", (0, 200, 255))])
    print(f"[mirror] {host} に送信中 ({m.fmt}) Ctrl+C で終了")
    try:
        while True:
            time.sleep(5)
            print(f"[mirror] 送信 {m.sent} 枚  失敗 {m.errors}  形式 {m.fmt}  {m.last_bytes} バイト/枚")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
