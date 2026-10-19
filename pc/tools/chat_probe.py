"""チャット欄が開いたかを、何で見分けられるか調べる (Windows)。

    python tools\\chat_probe.py            20 秒間、ゲームの入力の状態を表示する。その間にゲームで Enter を押して
                                         チャット欄を開いたり閉じたりしてください (文字も少し打ってみる)
    python tools\\chat_probe.py --send     ESP32 から Enter を送って、開く前と後を比べる (5 秒後。ゲームを手前に)

表示するもの:
  caret  文字入力の点滅カーソルがあるか (チャット欄が Windows の部品なら、開くと True になる)
  focus  入力先のウィンドウとその種類 (開くと変わることがある)
  ime    日本語入力 (IME) が ON か。ON だと英字が変換待ちになり、Enter 1 回目は確定だけで送信されない
  画面   画面に映っているとおりに撮ったときの、下の方 (チャット欄のあたり) の明るさ
"""
import argparse
import time

import _path  # noqa: F401

from akm.config import load_config
from akm.screen import GameScreen, foreground_window, input_state


def snapshot(screen):
    st = input_state(screen.hwnd)
    try:
        img = screen.grab_desktop()
        h = img.shape[0]
        bright = float(img[int(h * 0.80):int(h * 0.97), : img.shape[1] // 2].mean())
    except Exception as e:
        bright = f"撮れない ({e})"
    st["前面"] = foreground_window() == screen.hwnd
    st["画面"] = bright if isinstance(bright, str) else round(bright, 1)
    return st


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="ESP32 から Enter を送って比べる")
    ap.add_argument("--seconds", type=float, default=20)
    args = ap.parse_args()
    cfg = load_config("config.yaml")
    screen = GameScreen.from_config(cfg)
    screen.locate()
    if args.send:
        from akm.config import abs_map_from
        from akm.device import open_device

        dev = open_device(cfg, abs_map_from(cfg))
        print("[probe] 5 秒後に Enter を送ります。ゲームを手前にして、チャット欄は閉じておいてください")
        time.sleep(5)
        a = snapshot(screen)
        print(f"  Enter 前: {a}")
        dev.run("k:enter")
        for k in range(5):
            time.sleep(0.2)
            print(f"  Enter 後 {0.2 * (k + 1):.1f} 秒: {snapshot(screen)}")
        dev.run("k:enter")
        print("  (もう一度 Enter を送って閉じました。開いたままなら手で閉じてください)")
        return
    print(f"[probe] {args.seconds:.0f} 秒間表示します。ゲームを手前にして Enter でチャット欄を開閉してください")
    last = None
    end = time.monotonic() + args.seconds
    while time.monotonic() < end:
        st = snapshot(screen)
        key = (st["caret"], st["focus"], st["ime"], st["前面"])
        if key != last:
            print(f"  {time.strftime('%H:%M:%S')}  {st}")
            last = key
        time.sleep(0.2)
    print("[probe] チャット欄を開いたときと閉じたときで変わった項目を教えてください")


if __name__ == "__main__":
    main()
