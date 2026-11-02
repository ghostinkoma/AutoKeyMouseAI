"""仮想 HID (akm/virtual_hid.py) のキーボード + マウスの試験: サクラエディタで CSV の 3 列目だけを矩形選択してコピーし、末尾に貼る。

    python tools\\vhid_sakura_mouse_test.py
    python tools\\vhid_sakura_mouse_test.py --latency 20 --jitter 10 --interval 15

手順:
  1. サクラエディタを新しい (無題の) ウィンドウで起動し、プライマリモニタの左上に置く
     (HID の絶対座標はプライマリモニタだけに届くため)
  2. 固定長 5 列 × 5 行の CSV を打つ
  3. キーボードで 3 列目の左上・右下へカーソル (キャレット) を動かし、その画面上の位置を Windows から読む
     (文字の位置をここで測るので、フォントの大きさ・行番号の幅・画面の拡大率に左右されない)
  4. マウスを左上へ動かし、実際のマウスの位置と比べる (位置取りの確認)
  5. Alt を押したまま左上から右下へドラッグ (サクラエディタの矩形選択) → Ctrl+C
     → クリップボードが 3 列目だけになっているか
  6. 末尾へ移って Ctrl+V → 全体を読み戻し、CSV の下に 3 列目が並んでいるか

止めるときはほかのウィンドウをクリック。クリップボードは上書きされる。終わったら「保存しない」で閉じてよい。
"""
import argparse
import sys
import time

import vhid_sakura_test as base  # 同じフォルダ (tools) の、起動・クリップボード・ダイアログの確認を使う

ROWS = [
    ("0001", "APPLE", 120, 12, "JP01"),
    ("0002", "BANANA", 98, 340, "PH02"),
    ("0003", "CHERRY", 2480, 5, "US03"),
    ("0004", "DATE", 760, 48, "IR04"),
    ("0005", "ELDER", 31500, 7, "DE05"),
]
COL3_START = 4 + 1 + 6 + 1  # 3 列目の始まり (0 から数えた文字の位置)
COL3_WIDTH = 5


def csv_lines() -> list[str]:
    return [f"{i},{name:<6},{price:05d},{qty:03d},{code}" for i, name, price, qty, code in ROWS]


def col3_lines() -> list[str]:
    return [line[COL3_START:COL3_START + COL3_WIDTH] for line in csv_lines()]


def set_dpi_aware() -> None:
    """座標を実際の画素で扱う (画面の拡大率が 100% でなくても、読んだ位置と送る位置が一致するように)"""
    ct = base.ctypes
    try:
        ct.windll.user32.SetProcessDpiAwarenessContext(ct.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        return
    except Exception:
        pass
    try:
        ct.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


class GUITHREADINFO(base.ctypes.Structure if base.IS_WINDOWS else object):
    if base.IS_WINDOWS:
        w = base.wintypes
        _fields_ = [("cbSize", w.DWORD), ("flags", w.DWORD), ("hwndActive", w.HWND), ("hwndFocus", w.HWND),
                    ("hwndCapture", w.HWND), ("hwndMenuOwner", w.HWND), ("hwndMoveSize", w.HWND),
                    ("hwndCaret", w.HWND), ("rcCaret", w.RECT)]


def gui_info(hwnd) -> "GUITHREADINFO | None":
    u = base.user32
    tid = u.GetWindowThreadProcessId(hwnd, None)
    gi = GUITHREADINFO()
    gi.cbSize = base.ctypes.sizeof(GUITHREADINFO)
    return gi if u.GetGUIThreadInfo(tid, base.ctypes.byref(gi)) else None


def caret(hwnd) -> tuple[int, int, int] | None:
    """キャレット (文字入力のカーソル) の画面上の位置: (左端 x, 上端 y, 高さ)。無ければ None"""
    time.sleep(0.15)  # エディタが動かし終えるのを待つ
    gi = gui_info(hwnd)
    if not gi or not gi.hwndCaret:
        return None
    pt = base.wintypes.POINT(gi.rcCaret.left, gi.rcCaret.top)
    base.user32.ClientToScreen(gi.hwndCaret, base.ctypes.byref(pt))
    return pt.x, pt.y, max(1, gi.rcCaret.bottom - gi.rcCaret.top)


def in_menu(hwnd) -> bool:
    gi = gui_info(hwnd)
    return bool(gi and gi.flags & 0x4)  # GUI_INMENUMODE


def cursor() -> tuple[int, int]:
    pt = base.wintypes.POINT()
    base.user32.GetCursorPos(base.ctypes.byref(pt))
    return pt.x, pt.y


def screen_size() -> tuple[int, int]:
    return base.user32.GetSystemMetrics(0), base.user32.GetSystemMetrics(1)


def to_hid(x: int, y: int) -> tuple[int, int]:
    """プライマリモニタの画素 → HID の絶対座標 (0..32767)"""
    w, h = screen_size()
    return (max(0, min(32767, round(x * 32767 / max(1, w - 1)))),
            max(0, min(32767, round(y * 32767 / max(1, h - 1)))))


def place_on_primary(hwnd) -> None:
    u = base.user32
    if u.IsZoomed(hwnd) or u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)  # SW_RESTORE
    w, h = screen_size()
    u.SetWindowPos(hwnd, None, 40, 40, min(1100, w - 80), min(700, h - 80), 0x0004)  # SWP_NOZORDER
    time.sleep(0.3)


def compare(title: str, got_text: str | None, want: list[str]) -> bool:
    if got_text is None:
        print(f"   [NG] {title}: クリップボードに文字がありません")
        return False
    got = got_text.replace("\r\n", "\n").rstrip("\n").split("\n")
    ok = got == want
    print(f"   [{'OK' if ok else 'NG'}] {title}")
    if not ok:
        for i in range(max(len(got), len(want))):
            w = want[i] if i < len(want) else "(無い)"
            g = got[i] if i < len(got) else "(無い)"
            print(f"        {'  ' if w == g else '!!'} 期待 {w!r:<30} 実際 {g!r}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", help="sakura.exe の場所")
    ap.add_argument("--delay", type=float, default=10, help="1 つの操作ごとの間隔 ms (ESP32 の既定 10)")
    ap.add_argument("--latency", type=float, default=0, help="届くまでの遅れ ms")
    ap.add_argument("--jitter", type=float, default=0, help="遅れのばらつき ms")
    ap.add_argument("--interval", type=float, default=0, help="送信間隔の区切り ms (BLE の接続間隔の模擬)")
    ap.add_argument("--layout", choices=["jp", "us"], help="キー配列 (省略 = この PC のキーボードから判断)")
    a = ap.parse_args()
    if not base.IS_WINDOWS:
        raise SystemExit("Windows で動かしてください")
    set_dpi_aware()
    base.user32.GetGUIThreadInfo.argtypes = [base.wintypes.DWORD, base.ctypes.POINTER(GUITHREADINFO)]
    base.user32.ClientToScreen.argtypes = [base.wintypes.HWND, base.ctypes.POINTER(base.wintypes.POINT)]
    base.user32.SetWindowPos.argtypes = [base.wintypes.HWND, base.wintypes.HWND] + [base.ctypes.c_int] * 4 + [
        base.wintypes.UINT]
    base.user32.IsZoomed.argtypes = [base.wintypes.HWND]
    print(f"0) この試験は{'管理者として' if base.is_admin() else '管理者でなく'}動いています  "
          f"プライマリモニタ {screen_size()[0]}x{screen_size()[1]}")

    from akm.virtual_hid import VirtualHid

    hwnd = base.open_sakura(a.exe)
    if not hwnd:
        return 1
    place_on_primary(hwnd)
    base.bring_to_front(hwnd, 1.0)
    hid = VirtualHid(event_delay_ms=a.delay, latency_ms=a.latency, jitter_ms=a.jitter,
                     report_interval_ms=a.interval, layout=a.layout or base.keyboard_layout(),
                     allow=lambda: base.is_foreground(hwnd))
    results: dict[str, bool] = {}
    try:
        print(f"3) 固定長の CSV ({len(ROWS)} 行 × 5 列) を打ちます")
        lines = csv_lines()
        for line in lines:
            print(f"      {line}")
        hid.run(";k:enter;".join(f"t:{base.escape(s)}" for s in lines) + ";k:enter")
        if base.check_dialogs("打ち終わったとき"):
            return 1

        print(f"4) 3 列目 ({COL3_START + 1}〜{COL3_START + COL3_WIDTH} 文字目) の左上と右下の画面上の位置を測ります")
        hid.run("k:ctrl+home;" + "k:right;" * COL3_START)
        pa = caret(hwnd)
        hid.run("k:down;" * (len(ROWS) - 1) + "k:right;" * COL3_WIDTH)
        pb = caret(hwnd)
        if not pa or not pb:
            print("   キャレットの位置を読めません (サクラエディタがキャレットを出していない)")
            return 1
        cw = (pb[0] - pa[0]) / COL3_WIDTH
        lh = (pb[1] - pa[1]) / (len(ROWS) - 1)
        print(f"   左上 ({pa[0]}, {pa[1]})  右下 ({pb[0]}, {pb[1]})  1 文字の幅 {cw:.1f}px / 1 行の高さ {lh:.1f}px")
        hid.run("k:ctrl+end")  # キャレットを離しておく (マウスで選んだ結果と取り違えない)
        # 文字の境目 (キャレットの左端) の、行の高さの真ん中を狙う
        ax, ay = pa[0], pa[1] + pa[2] // 2
        bx, by = pb[0], pb[1] + pb[2] // 2
        sw, sh = screen_size()
        if not (0 <= min(ax, bx) and max(ax, bx) < sw and 0 <= min(ay, by) and max(ay, by) < sh):
            print("   3 列目がプライマリモニタの外にあります (絶対座標が届かない)")
            return 1

        print("5) マウスを左上へ動かして、実際の位置と比べます")
        hx, hy = to_hid(ax, ay)
        hid.run(f"a:{hx},{hy};w:80")
        cx, cy = cursor()
        err = max(abs(cx - ax), abs(cy - ay))
        results["マウスの位置取り"] = err <= 1
        print(f"   [{'OK' if err <= 1 else 'NG'}] 狙い ({ax}, {ay}) → 実際 ({cx}, {cy})  ずれ {err}px (HID {hx},{hy})")

        print("6) Alt を押したまま左上から右下へドラッグ (矩形選択) → Ctrl+C")
        mx, my = to_hid((ax + bx) // 2, (ay + by) // 2)
        ex, ey = to_hid(bx, by)
        hid.run(f"kd:alt;w:30;bd:L;w:60;a:{mx},{my};w:40;a:{ex},{ey};w:80;bu:L;w:30;ku:alt;w:150")
        if in_menu(hwnd):  # Alt を離したときにメニューバーに入った
            print("   Alt を離したときメニューに入ったので Esc で戻します")
            hid.run("k:esc;w:100")
        pc = caret(hwnd)
        if pc:
            print(f"   ドラッグ後のキャレット ({pc[0]}, {pc[1]})  (右下 ({pb[0]}, {pb[1]}) と同じなら選択の終わりが合っている)")
        if base.check_dialogs("ドラッグしたとき"):
            return 1
        base.clear_clipboard()
        hid.run("k:ctrl+c;w:300")
        clip, _fmts = base.read_clipboard()
        results["矩形選択のコピー"] = compare("クリップボード = 3 列目だけ", clip, col3_lines())

        print("7) 末尾へ移って Ctrl+V → 全体を読み戻して比べます")
        hid.run("k:ctrl+end;w:50;k:ctrl+v;w:300")
        if base.check_dialogs("貼り付けたとき"):
            return 1
        base.clear_clipboard()
        hid.run("k:ctrl+a;w:200;k:ctrl+c;w:300")
        clip, _fmts = base.read_clipboard()
        results["末尾への貼り付け"] = compare("全体 = CSV + 3 列目", clip, lines + col3_lines())
        hid.run("k:ctrl+end")  # 選択を外す
    except InterruptedError as e:
        print(f"   止まりました ({e})。押していたキー・ボタンは離しました")
        return 1
    except OSError as e:
        print(f"   送れません: {e}")
        return 1

    print("結果:")
    for k, v in results.items():
        print(f"   {'OK' if v else 'NG'}  {k}")
    ok = all(results.values()) and len(results) == 3
    print("   " + ("成功 (キーボード・マウスとも期待どおり)" if ok else "期待と違うところがあります"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
