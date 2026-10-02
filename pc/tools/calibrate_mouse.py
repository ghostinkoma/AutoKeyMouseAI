"""ESP32 の絶対座標マウスがスクリーン上のどこに対応するかを実測する。

ESP32 で何点かにカーソルを動かし、Windows の実際のカーソル位置を読んで直線近似する。
出力された abs_map を config.yaml の mouse.abs_map に貼り付ける。

    python tools/calibrate_mouse.py
"""
import time

import _path  # noqa: F401
import numpy as np

from akm.config import abs_map_from, load_config
from akm.device import ABS_MAX, AbsMap, open_device
from akm.screen import IS_WINDOWS, cursor_pos, primary_screen_size


def main() -> None:
    if not IS_WINDOWS:
        raise SystemExit("Windows で実行してください")
    cfg = load_config("config.yaml")
    dev = open_device(cfg, abs_map_from(cfg))
    print("カーソルを動かします。マウスに触らないでください...")
    hid_pts, px_pts = [], []
    for fx in (0.1, 0.3, 0.5, 0.7, 0.9):
        for fy in (0.1, 0.5, 0.9):
            hx, hy = int(fx * ABS_MAX), int(fy * ABS_MAX)
            dev.run(f"a:{hx},{hy};w:150")
            time.sleep(0.1)
            hid_pts.append((hx, hy))
            px_pts.append(cursor_pos())
    hid = np.array(hid_pts, float)
    px = np.array(px_pts, float)
    # px = a + b * hid  →  hid = (px - a) / b
    bx, ax = np.polyfit(hid[:, 0], px[:, 0], 1)
    by, ay = np.polyfit(hid[:, 1], px[:, 1], 1)
    m = AbsMap(origin_x=ax, origin_y=ay, scale_x=1 / bx, scale_y=1 / by)

    err = []
    for (hx, hy), (x, y) in zip(hid_pts, px_pts):
        ex, ey = m.to_hid(x, y)
        err.append(max(abs(ex - hx) / m.scale_x, abs(ey - hy) / m.scale_y))
    w, h = primary_screen_size()
    print(f"\nプライマリモニタ: {w}x{h}")
    print(f"最大誤差: {max(err):.1f} px  (数 px 以内なら OK)")
    if max(err) > 5:
        print("誤差が大きい: マウスを触っていないか / ゲームがカーソルを固定していないか確認")
    print("\nconfig.yaml に貼り付け:\nmouse:\n  abs_map:")
    print(f"    origin_x: {m.origin_x:.2f}\n    origin_y: {m.origin_y:.2f}")
    print(f"    scale_x: {m.scale_x:.5f}\n    scale_y: {m.scale_y:.5f}")


if __name__ == "__main__":
    main()
