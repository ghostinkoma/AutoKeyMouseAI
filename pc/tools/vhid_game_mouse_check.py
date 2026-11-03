"""ゲーム (MURex) が仮想 HID のマウスを受け付けるかを、カーソルの動かし方・クリックの長さを変えて調べる。

    .\\.venv\\Scripts\\python.exe tools\\vhid_game_mouse_check.py                # Thanatos の窓
    .\\.venv\\Scripts\\python.exe tools\\vhid_game_mouse_check.py --name Tiffany

キャラクター (画面の真ん中) の左右を交互にクリックし、画面右下の座標が変わったか (歩いたか) を見る:
  1. abs    速いクリック (前の版と同じ: 動かして 30ms 後に 10ms だけ押す)
  2. abs    ゆっくり (動かして 100ms 待ち、80ms 押したまま)
  3. absrel 絶対座標 + 1 ピクセルの相対移動 (動いた量だけを見るゲーム向け)
  4. rel    相対移動だけ (実際のカーソル位置を読みながら近づける)
歩いた方式を仲介アプリの「試験 (往復)」の「マウス」で選ぶ。

始めると、ゲームの窓を前面にするよう求める (タイトルバーをクリック)。前面でなくなると止まる。
開けた場所で試すこと (左右に数マス歩く)。
"""
import argparse
import sys
import time

import _path  # noqa: F401

from akm.config import load_config
from akm.device import AbsMap
from akm.screen import (client_rect, cursor_pos, foreground_window, is_foreground, list_windows, primary_screen_size,
                        window_pid)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="Thanatos", help="ゲームの窓のタイトルに含まれるキャラクター名")
    ap.add_argument("--dist", type=float, default=0.15, help="真ん中からクリックする位置までの距離 (画面の高さの割合)")
    ap.add_argument("--wait", type=float, default=1.5, help="クリックしてから座標を読むまでの秒数")
    a = ap.parse_args()
    if sys.platform != "win32":
        raise SystemExit("Windows で動かしてください")

    cfg = load_config("config.yaml") or {}
    hits = [(h, t) for h, t, p, _r in list_windows() if p.lower() == "main.exe" and a.name.lower() in t.lower()]
    if not hits:
        raise SystemExit(f"'{a.name}' のゲームの窓が見つかりません")
    hwnd, title = hits[0]
    print(f"1) 窓 {hwnd:#x} '{title}'")

    # 画面: 仲介アプリと同じく OBS のゲームキャプチャ (クライアントごとのソース)。だめならウィンドウから直接
    grab = None
    if cfg.get("obs"):
        try:
            from akm.obs_windows import ObsWindows

            obs = ObsWindows(cfg.get("obs"), log=lambda lv, m: print(f"   [{lv}] {m}"))
            src = obs.ensure(window_pid(hwnd), title)
            for _ in range(20):
                try:
                    obs.grab(src)
                    break
                except Exception:
                    time.sleep(0.5)
            grab = lambda: obs.grab(src)  # noqa: E731
            print(f"   画面は OBS ({src}) から撮ります")
        except Exception as e:  # noqa: BLE001
            print(f"   OBS から撮れません ({e})。ウィンドウから直接撮ります")
    if grab is None:
        from akm.screen import GameScreen

        gs = GameScreen(a.name, "main.exe", "auto", None)
        gs.locate()
        grab = gs.grab

    from akm.maploc import LocationReader, make_ocr

    lcfg = cfg.get("maplog") or {}
    reader = LocationReader(lcfg, make_ocr(lcfg.get("ocr", "auto"), lcfg.get("tesseract_cmd")))

    print("2) ゲームの窓のタイトルバーをクリックして前面にしてください (15 秒待ちます)")
    end = time.monotonic() + 15
    while time.monotonic() < end and not is_foreground(hwnd):
        time.sleep(0.1)
    if not is_foreground(hwnd):
        raise SystemExit("   前面になりませんでした")
    time.sleep(0.5)

    from akm.mouse_check import run_mouse_check
    from akm.virtual_hid import VirtualHid

    hid = VirtualHid(allow=lambda: is_foreground(hwnd))  # 前面でなくなったら止まる
    amap = AbsMap.for_screen(*primary_screen_size())

    def screen_to_hid(sx, sy):
        if not amap.reachable(sx, sy):
            raise RuntimeError("ゲームの窓がプライマリモニタの外にあります")
        return amap.to_hid(sx, sy)

    try:
        run_mouse_check(hid.run, grab, reader.read_raw, lambda: client_rect(hwnd), screen_to_hid, cursor_pos,
                        lambda lv, m: print(f"   {m}"), alive=lambda: is_foreground(hwnd),
                        dist=a.dist, wait_s=a.wait)
    except InterruptedError:
        fg = foreground_window()
        name = next((t for h, t, _p, _r in list_windows() if h == fg), "?")
        print(f"   ゲームが前面でなくなったので止めました (今の前面: '{name}')")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
