"""ESP32 との通信と BLE HID の動作確認。

メモ帳を前面に出してから実行すると、文字入力とマウス移動を確認できる。

    python tools/hid_test.py
"""
import math
import time

import _path  # noqa: F401

from akm.config import abs_map_from, load_config
from akm.device import open_device
from akm.screen import cursor_pos, primary_screen_size


def main() -> None:
    cfg = load_config("config.yaml")
    dev = open_device(cfg, abs_map_from(cfg))
    print("status:", dev.t.status())
    print("5 秒後に入力します。メモ帳などを前面にしてください")
    time.sleep(5)
    expected = "AutoKeyMouse test 123 @[]:;"
    dev.run("t:AutoKeyMouse test 123 @[]:\\;;k:enter")  # 文字列中の ; は \; と書く
    print(f"メモ帳に次の 1 行が入っていれば OK: {expected}")
    w, h = primary_screen_size()
    cx, cy, r = w / 2, h / 2, h / 4
    t0 = time.monotonic()
    for i in range(37):
        a = 2 * math.pi * i / 36
        dev.move(cx + r * math.cos(a), cy + r * math.sin(a))
    dt = (time.monotonic() - t0) / 37
    print(f"絶対移動 1 回あたり {dt * 1000:.0f} ms / 現在のカーソル {cursor_pos()}")


if __name__ == "__main__":
    main()
