"""仮想 HID の試験: 別の仮想デスクトップにあるウィンドウ (開いておいたサクラエディタの文書) に入力できるか。

    python tools\\vhid_vdesktop_test.py                                   # KemMouseHIDtest.txt を開いている窓
    python tools\\vhid_vdesktop_test.py --title "KemMouseHIDtest.txt" --no-return

前提: HID (ESP32 も、この PC の SendInput も) の入力は、いま見えている仮想デスクトップの「前面のウィンドウ」に届く。
裏や別の仮想デスクトップの窓へ直接は届かない。それを次の順に確かめる:

  1. 窓を探し、どの仮想デスクトップにあるかを調べる (Windows の IVirtualDesktopManager)
  2. 前面でないまま送ろうとする → ゲートが止め、1 文字も送られないこと (いま前面の窓に誤って入らないこと)
  3. 窓を前面に出す → Windows が窓のある仮想デスクトップへ切り替えるか
  4. 文書の末尾に 1 行 (時刻入り) を打ち、Ctrl+A → Ctrl+C で読み戻して確かめる
  5. 元の窓 (この PowerShell) を前面に戻す → 元の仮想デスクトップへ戻るか

文書は書き換わる (保存はしない)。クリップボードは上書きされる。止めるときはほかのウィンドウをクリック。
"""
import argparse
import sys
import time
from datetime import datetime

import vhid_sakura_test as base  # 同じフォルダ (tools) の、窓の一覧・前面に出す・クリップボード・キーの数え方を使う

CLSID_VDM = "{aa509086-5ca9-4c25-8f95-589d3c07b48a}"
IID_VDM = "{a5cd92ff-29be-454c-8d04-d82879fb3f1b}"


class VirtualDesktops:
    """Windows の IVirtualDesktopManager (公開されている COM)。窓がいまの仮想デスクトップにあるか・どのデスクトップか"""

    def __init__(self):
        ct, w = base.ctypes, base.wintypes

        class GUID(ct.Structure):
            _fields_ = [("d1", w.DWORD), ("d2", w.WORD), ("d3", w.WORD), ("d4", ct.c_ubyte * 8)]

        self.GUID = GUID
        ole32 = ct.WinDLL("ole32")
        ole32.CoInitialize(None)
        clsid, iid = GUID(), GUID()
        ole32.CLSIDFromString(ct.c_wchar_p(CLSID_VDM), ct.byref(clsid))
        ole32.CLSIDFromString(ct.c_wchar_p(IID_VDM), ct.byref(iid))
        self.p = ct.c_void_p()
        ole32.CoCreateInstance.restype = ct.c_long
        hr = ole32.CoCreateInstance(ct.byref(clsid), None, 0x17, ct.byref(iid), ct.byref(self.p))  # CLSCTX_ALL
        if hr != 0 or not self.p:
            raise OSError(f"IVirtualDesktopManager を使えません (0x{hr & 0xFFFFFFFF:08X})")
        vtbl = ct.cast(self.p, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
        # IUnknown の 3 つの後: IsWindowOnCurrentVirtualDesktop, GetWindowDesktopId
        self._on = ct.WINFUNCTYPE(ct.c_long, ct.c_void_p, w.HWND, ct.POINTER(w.BOOL))(vtbl[3])
        self._id = ct.WINFUNCTYPE(ct.c_long, ct.c_void_p, w.HWND, ct.POINTER(GUID))(vtbl[4])

    def on_current(self, hwnd) -> bool | None:
        b = base.wintypes.BOOL()
        return bool(b.value) if self._on(self.p, hwnd, base.ctypes.byref(b)) == 0 else None

    def desktop_id(self, hwnd) -> str | None:
        g = self.GUID()
        if self._id(self.p, hwnd, base.ctypes.byref(g)) != 0:
            return None
        return f"{g.d1:08X}-{g.d2:04X}-{g.d3:04X}-{bytes(g.d4[:2]).hex().upper()}-{bytes(g.d4[2:]).hex().upper()}"


def cloaked(hwnd) -> int:
    """DWM が隠しているか (別の仮想デスクトップの窓は 2 = シェルが隠している)"""
    ct, w = base.ctypes, base.wintypes
    v = w.DWORD()
    try:
        ct.WinDLL("dwmapi").DwmGetWindowAttribute(w.HWND(hwnd), 14, ct.byref(v), 4)  # DWMWA_CLOAKED
    except Exception:
        return -1
    return v.value


def title_of(hwnd) -> str:
    return base._text(hwnd).strip()


def describe(vd, hwnd, name: str) -> None:
    on = vd.on_current(hwnd) if vd else None
    where = {True: "いまの仮想デスクトップ", False: "別の仮想デスクトップ", None: "不明"}[on]
    print(f"   {name}: {where}  (デスクトップ {vd.desktop_id(hwnd) if vd else '?'} / 隠し {cloaked(hwnd)} / "
          f"前面 {'はい' if base.is_foreground(hwnd) else 'いいえ'})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="KemMouseHIDtest.txt", help="入力先の窓のタイトルに含まれる文字")
    ap.add_argument("--process", default="sakura.exe", help="入力先の窓のプロセス名")
    ap.add_argument("--delay", type=float, default=10, help="1 つの操作ごとの間隔 ms")
    ap.add_argument("--layout", choices=["jp", "us"], help="キー配列 (省略 = この PC のキーボードから判断)")
    ap.add_argument("--no-return", action="store_true", help="終わったあと元の窓 (元の仮想デスクトップ) に戻らない")
    a = ap.parse_args()
    if not base.IS_WINDOWS:
        raise SystemExit("Windows で動かしてください")

    from akm.virtual_hid import VirtualHid

    print(f"0) この試験は{'管理者として' if base.is_admin() else '管理者でなく'}動いています")
    home = base.user32.GetForegroundWindow()
    print(f"   いま前面の窓: '{title_of(home)}' (終わったらここへ戻します)")
    try:
        vd = VirtualDesktops()
    except OSError as e:
        print(f"   {e} (仮想デスクトップの判定は省きます)")
        vd = None

    print(f"1) '{a.title}' を開いている {a.process} の窓を探します")
    hits = [(h, t) for h, t, p in base.list_windows()
            if p.lower() == a.process.lower() and a.title.lower() in t.lower()]
    if not hits:
        print("   見つかりません (サクラエディタで開いているか、--title を確認してください)")
        return 1
    hwnd = hits[0][0]
    print(f"   窓 {hwnd:#x} '{hits[0][1].strip()}'" + (f" (ほかに {len(hits) - 1} 個)" if len(hits) > 1 else ""))
    describe(vd, hwnd, "入力先")
    describe(vd, home, "この窓  ")
    other_desktop = vd is not None and vd.on_current(hwnd) is False
    if not other_desktop:
        print("   ※ 入力先がいまの仮想デスクトップにあります。別の仮想デスクトップの試験にするには、"
              "サクラエディタの窓を別のデスクトップへ移してからもう一度実行してください (このまま続けます)")

    layout = a.layout or base.keyboard_layout()
    hid = VirtualHid(event_delay_ms=a.delay, layout=layout, allow=lambda: base.is_foreground(hwnd))
    results: dict[str, bool] = {}

    print("2) 前面に出さずに送ってみます (ゲートが止めて、どの窓にも 1 文字も入らないはず)")
    with base.KeyWatch() as kw:
        try:
            hid.run("t:SHOULD NOT BE TYPED")
            blocked = False
        except InterruptedError as e:
            blocked = True
            print(f"   止まりました ({e})")
        time.sleep(0.2)
    ok = blocked and kw.down == 0
    results["前面でない窓には送らない"] = ok
    print(f"   [{'OK' if ok else 'NG'}] 送られたキー {kw.down} 個")

    print("3) 入力先を前面に出します")
    t0 = time.perf_counter()
    front = base.bring_to_front(hwnd, 2.0)
    print(f"   前面に{'なりました' if front else 'なりません'} ({time.perf_counter() - t0:.2f} 秒)")
    if vd:
        describe(vd, hwnd, "入力先")
        if other_desktop:
            switched = vd.on_current(hwnd) is True
            print(f"   仮想デスクトップは{'入力先のデスクトップへ切り替わりました' if switched else '切り替わっていません'}")
    results["前面に出せる"] = front
    if not front:
        print("   Windows が前面に出すのを許しませんでした。入力先へは送れません")
        return finish(results, home, a.no_return, vd)
    if base.ime_open(hwnd):
        base.set_ime(hwnd, False)

    marker = f"AKM virtual HID {datetime.now():%Y-%m-%d %H:%M:%S}"
    print(f"4) 文書の末尾に 1 行打ちます: {marker}")
    title0 = title_of(hwnd)
    try:
        with base.KeyWatch() as kw:
            hid.run(f"k:ctrl+end;k:enter;t:{base.escape(marker)}")
            time.sleep(0.2)
        print(f"   送ったキー: 押す {kw.down} / 離す {kw.up}")
        print(f"   タイトル: '{title0}' → '{title_of(hwnd)}'")
        base.clear_clipboard()
        hid.run("k:ctrl+a;w:200;k:ctrl+c;w:300;k:ctrl+end")
    except InterruptedError as e:
        print(f"   止まりました ({e})")
        results["末尾に打てた"] = False
        return finish(results, home, a.no_return, vd)
    clip, _ = base.read_clipboard()
    last = (clip or "").replace("\r\n", "\n").rstrip("\n").split("\n")[-1]
    ok = last == marker
    results["末尾に打てた"] = ok
    print(f"   [{'OK' if ok else 'NG'}] 読み戻した最後の行: {last!r}")
    return finish(results, home, a.no_return, vd)


def finish(results, home, no_return, vd) -> int:
    if not no_return and home:
        print("5) 元の窓を前面に戻します")
        back = base.bring_to_front(home, 2.0)
        print(f"   {'戻りました' if back else '戻せません (手で戻してください)'}")
        if vd:
            describe(vd, home, "この窓  ")
    print("結果:")
    for k, v in results.items():
        print(f"   {'OK' if v else 'NG'}  {k}")
    ok = all(results.values())
    print("   " + ("成功" if ok else "期待と違うところがあります") + "  (文書は保存していません。不要なら最後の行を消すか、保存せずに閉じてください)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
