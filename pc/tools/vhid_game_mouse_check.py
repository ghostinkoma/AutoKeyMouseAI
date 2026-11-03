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
from akm.pointer import Pointer
from akm.screen import client_rect, cursor_pos, is_foreground, list_windows, primary_screen_size, window_pid

TRIALS = [
    ("abs 速いクリック (前の版)", "abs", 30, 10),
    ("abs ゆっくり", "abs", 100, 80),
    ("absrel 絶対 + 相対 1px", "absrel", 100, 80),
    ("rel 相対移動だけ", "rel", 100, 80),
]


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

    def read_xy():
        for _ in range(6):
            loc = reader.read_raw(grab())
            if loc is not None:
                return loc.x, loc.y
            time.sleep(0.3)
        return None

    print("2) ゲームの窓のタイトルバーをクリックして前面にしてください (15 秒待ちます)")
    end = time.monotonic() + 15
    while time.monotonic() < end and not is_foreground(hwnd):
        time.sleep(0.1)
    if not is_foreground(hwnd):
        raise SystemExit("   前面になりませんでした")
    time.sleep(0.5)

    from akm.virtual_hid import VirtualHid

    hid = VirtualHid(allow=lambda: is_foreground(hwnd))  # 人の操作の見張りはしない (この試験は人が見ている)
    amap = AbsMap.for_screen(*primary_screen_size())
    start = read_xy()
    print(f"3) 今の座標: {start if start else '読めません'}")
    if start is None:
        raise SystemExit("   画面右下の座標を読めません (config.yaml の maplog を確認してください)")

    results = []
    side = 1
    for label, mode, settle, hold in TRIALS:
        r = client_rect(hwnd)
        h = r.height
        tx, ty = r.left + r.width / 2 + side * a.dist * h, r.top + r.height / 2
        if not amap.reachable(tx, ty):
            print("   ゲームの窓がプライマリモニタの外にあります")
            return 1
        ptr = Pointer(hid.run, amap.to_hid, cursor=cursor_pos, mode=mode, settle_ms=settle, hold_ms=hold)
        before = read_xy()
        try:
            ptr.click(tx, ty)
        except InterruptedError:
            print("   ゲームが前面でなくなったので止めました")
            return 1
        time.sleep(a.wait)
        after = read_xy()
        moved = before is not None and after is not None and after != before
        results.append((label, mode, moved))
        print(f"   [{'歩いた' if moved else '動かない'}] {label}: カーソルのずれ {ptr.last_error}px  "
              f"座標 {before} → {after}")
        if moved:
            side = -side  # 次は反対側へ (元の辺りに戻る)
    ok = [r for r in results if r[2]]
    print("結果: " + (f"歩いた方式 = {', '.join(r[0] for r in ok)}。仲介アプリの「マウス」で "
                     f"{ok[0][1]} を選んでください" if ok else
                     "どの方式でも歩きませんでした (ゲームがプログラムの入力を受け付けていない可能性があります)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
