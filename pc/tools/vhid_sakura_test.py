"""仮想 HID (akm/virtual_hid.py) の初歩の試験: サクラエディタに文字を打ち込み、打てた文字が正しいか確かめる。

    python tools\\vhid_sakura_test.py
    python tools\\vhid_sakura_test.py --latency 20 --jitter 10 --interval 15   # BLE に近い遅れを付けて
    python tools\\vhid_sakura_test.py --exe "D:\\tools\\sakura\\sakura.exe"

手順:
  1. サクラエディタを新しい (無題の) ウィンドウで起動する (開いている文書には打たない)
  2. そのウィンドウを手前に出し、IME (日本語入力) を OFF にする
  3. 英字・数字・記号・改行を仮想 HID で打つ
  4. Ctrl+A → Ctrl+C で打てた文字をクリップボードに写し、打とうとした文字と比べる

打っている間にほかのウィンドウをクリックすると、その場で止まる (送信先が前面のときだけ送るゲートの確認)。
クリップボードの中身は上書きされる。終わったらサクラエディタは「保存しない」で閉じてよい。
"""
import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

import _path  # noqa: F401  (akm を import できるようにする)

# 標準ライブラリだけで動かす (akm.screen は numpy / OpenCV が要るので使わない。virtual_hid も標準ライブラリだけ)
IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    imm32 = ctypes.WinDLL("imm32")
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    imm32.ImmGetDefaultIMEWnd.argtypes = [wintypes.HWND]
    imm32.ImmGetDefaultIMEWnd.restype = wintypes.HWND
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [_ENUM, wintypes.LPARAM]
    user32.EnumChildWindows.argtypes = [wintypes.HWND, _ENUM, wintypes.LPARAM]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
    user32.EnumClipboardFormats.restype = wintypes.UINT
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE


def _proc_name(hwnd) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return buf.value.replace("/", "\\").split("\\")[-1] if ok else ""
    finally:
        kernel32.CloseHandle(h)


def list_windows() -> list[tuple[int, str, str]]:
    """可視でタイトルのあるウィンドウ: (hwnd, タイトル, プロセス名)"""
    out = []

    @_ENUM
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                out.append((hwnd, buf.value, _proc_name(hwnd)))
        return True

    user32.EnumWindows(cb, 0)
    return out


def _text(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(hwnd, buf, 128)
    return buf.value


def sakura_dialogs() -> list[str]:
    """サクラエディタが出しているメッセージボックスの文面 (無ければ空)"""
    out = []
    for h, title, proc in list_windows():
        if proc.lower() != "sakura.exe" or _class(h) != "#32770":
            continue
        parts = []

        @_ENUM
        def cb(child, _):
            if _class(child) == "Static" and _text(child).strip():
                parts.append(_text(child).strip())
            return True

        user32.EnumChildWindows(h, cb, 0)
        out.append(f"[{title}] " + " ".join(parts))
    return out


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def check_dialogs(when: str) -> bool:
    """サクラエディタがエラーを出していたら案内して True"""
    ds = sakura_dialogs()
    if not ds:
        return False
    print(f"   !! {when}、サクラエディタがメッセージを出しています:")
    for d in ds:
        print(f"      {d}")
    if any("権限" in d for d in ds):
        me = "管理者" if is_admin() else "管理者でない"
        print(f"   → この試験は {me} として動いていますが、すでに別の権限で動いているサクラエディタ (タスクトレイの常駐を含む) "
              "があり、話ができません。次のどちらかで試してください:\n"
              "      a) サクラエディタをすべて閉じる (タスクトレイのサクラエディタも右クリック → 終了) → もう一度実行\n"
              "      b) サクラエディタと同じ権限の PowerShell から実行する (常駐が管理者でないなら、管理者でない PowerShell)")
    print("   メッセージボックスは OK で閉じてください")
    return True


def is_foreground(hwnd) -> bool:
    return user32.GetForegroundWindow() == hwnd


def bring_to_front(hwnd, wait_s: float = 1.0) -> bool:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.keybd_event(0x87, 0, 0, 0)  # F24 を一瞬押すと、手前に出すのを Windows が許す
    user32.keybd_event(0x87, 0, 2, 0)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        if is_foreground(hwnd):
            return True
        time.sleep(0.05)
    return is_foreground(hwnd)


def ime_open(hwnd) -> bool | None:
    ime = imm32.ImmGetDefaultIMEWnd(hwnd)
    if not ime:
        return None
    return bool(user32.SendMessageW(ime, 0x0283, 0x0005, 0))  # WM_IME_CONTROL, IMC_GETOPENSTATUS


def set_ime(hwnd, on: bool) -> None:
    ime = imm32.ImmGetDefaultIMEWnd(hwnd)
    if ime:
        user32.SendMessageW(ime, 0x0283, 0x0006, 1 if on else 0)  # IMC_SETOPENSTATUS

CANDIDATES = [r"C:\Program Files (x86)\sakura\sakura.exe", r"C:\Program Files\sakura\sakura.exe"]

# 1 行ずつ打つ文字 (JIS 配列で位置の違う記号・Shift が要る記号を一通り)
LINES = [
    "Hello AKM virtual HID 0123456789",
    "abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "!\"#$%&'()=~|-^\\@`[{]};+:*,<.>/?_",
    "/move lorencia",
]


def escape(text: str) -> str:
    """スクリプトの t: に入れる形 (\\ と ; は前に \\ を付ける)"""
    return text.replace("\\", "\\\\").replace(";", "\\;")


def sakura_windows() -> set[int]:
    return {h for h, _t, proc in list_windows() if proc.lower() == "sakura.exe" and _class(h) != "#32770"}


def find_exe(given: str | None) -> str:
    for p in ([given] if given else []) + CANDIDATES:
        if p and Path(p).exists():
            return p
    raise SystemExit("サクラエディタ (sakura.exe) が見つかりません。--exe で場所を指定してください")


def keyboard_layout() -> str:
    return "jp" if ctypes.windll.user32.GetKeyboardType(0) == 7 else "us"  # 7 = 日本語キーボード


def _clip_open() -> bool:
    for _ in range(40):  # ほかのアプリが開いていることがあるので少し待つ
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.025)
    return False


def clear_clipboard() -> None:
    if _clip_open():
        user32.EmptyClipboard()
        user32.CloseClipboard()


def read_clipboard() -> tuple[str | None, list[int]]:
    """クリップボードの文字 (無ければ None) と、入っている形式の番号"""
    if not _clip_open():
        return None, []
    try:
        fmts, f = [], 0
        while True:
            f = user32.EnumClipboardFormats(f)
            if not f:
                break
            fmts.append(f)
        h = user32.GetClipboardData(13)  # CF_UNICODETEXT
        if not h:
            return None, fmts
        ptr = kernel32.GlobalLock(h)
        try:
            return (ctypes.wstring_at(ptr) if ptr else None), fmts
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


class KeyWatch:
    """Windows に届いたキー入力を数える (低水準キーボードフック。数えるだけで入力は止めない)。
    SendInput で送った入力が Windows まで届いたか、ほかのアプリ (サクラエディタ) の問題かを切り分ける"""

    def __init__(self):
        self.down = self.up = self.injected = 0
        self.first: list[tuple[int, int, int]] = []  # (仮想キー, スキャンコード, 印)
        self._tid = 0
        self._ready = threading.Event()
        self._th = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._th.start()
        self._ready.wait(2)
        return self

    def __exit__(self, *e):
        if self._tid:
            user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)  # WM_QUIT
        self._th.join(2)

    def _run(self):
        class KBD(ctypes.Structure):
            _fields_ = [("vk", wintypes.DWORD), ("scan", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]

        PROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, PROC, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HANDLE

        @PROC
        def proc(code, wp, lp):
            if code == 0:
                k = ctypes.cast(lp, ctypes.POINTER(KBD)).contents
                if wp in (0x100, 0x104):  # WM_KEYDOWN / WM_SYSKEYDOWN
                    self.down += 1
                    if len(self.first) < 6:
                        self.first.append((k.vk, k.scan, k.flags))
                else:
                    self.up += 1
                if k.flags & 0x10:  # LLKHF_INJECTED (プログラムが送った入力)
                    self.injected += 1
            return user32.CallNextHookEx(None, code, wp, lp)

        self._proc = proc
        self._tid = kernel32.GetCurrentThreadId()
        hook = user32.SetWindowsHookExW(13, proc, kernel32.GetModuleHandleW(None), 0)  # WH_KEYBOARD_LL
        self._ready.set()
        if not hook:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        user32.UnhookWindowsHookEx(hook)


def open_sakura(exe_arg: str | None):
    """サクラエディタを新しい (無題の) ウィンドウで起動し、手前に出して IME を OFF にする。ウィンドウを返す (だめなら None)"""
    exe = find_exe(exe_arg)
    before = sakura_windows()
    print(f"1) サクラエディタを起動します: {exe}")
    subprocess.Popen([exe])
    hwnd = None
    end = time.monotonic() + 10
    while time.monotonic() < end and not hwnd:
        new = sakura_windows() - before
        hwnd = next(iter(new), None)
        time.sleep(0.1)
    if not hwnd:
        raise SystemExit("   新しいウィンドウが見つかりません (10 秒待ちました)")
    title = next((t for h, t, _p in list_windows() if h == hwnd), "")
    print(f"   ウィンドウ {hwnd:#x} '{title}'")
    time.sleep(1.0)  # 立ち上がりきるのを待つ (エラーのメッセージボックスが出るならこの間に出る)
    if check_dialogs("起動したとき"):
        return None

    print("2) 手前に出して IME を OFF にします")
    if not bring_to_front(hwnd, 1.0):
        print("   手前に出せません。サクラエディタのウィンドウを 1 回クリックしてください (10 秒待ちます)")
        end = time.monotonic() + 10
        while time.monotonic() < end and not is_foreground(hwnd):
            time.sleep(0.1)
        if not is_foreground(hwnd):
            raise SystemExit("   前面になりませんでした")
    if ime_open(hwnd):
        set_ime(hwnd, False)
    print(f"   IME: {'ON のまま (英字がかなになるかもしれません)' if ime_open(hwnd) else 'OFF'}")

    return hwnd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", help="sakura.exe の場所")
    ap.add_argument("--delay", type=float, default=10, help="1 つの操作ごとの間隔 ms (ESP32 の既定 10)")
    ap.add_argument("--latency", type=float, default=0, help="届くまでの遅れ ms")
    ap.add_argument("--jitter", type=float, default=0, help="遅れのばらつき ms")
    ap.add_argument("--interval", type=float, default=0, help="送信間隔の区切り ms (BLE の接続間隔の模擬)")
    ap.add_argument("--layout", choices=["jp", "us"], help="キー配列 (省略 = この PC のキーボードから判断)")
    a = ap.parse_args()
    if not IS_WINDOWS:
        raise SystemExit("Windows で動かしてください")
    print(f"0) この試験は{'管理者として' if is_admin() else '管理者でなく'}動いています")

    from akm.virtual_hid import VirtualHid

    hwnd = open_sakura(a.exe)
    if not hwnd:
        return 1

    layout = a.layout or keyboard_layout()
    hid = VirtualHid(event_delay_ms=a.delay, latency_ms=a.latency, jitter_ms=a.jitter,
                     report_interval_ms=a.interval, layout=layout, allow=lambda: is_foreground(hwnd))
    script = ";k:enter;".join(f"t:{escape(s)}" for s in LINES)
    n_chars = sum(len(s) for s in LINES)
    print(f"3) {n_chars} 文字を打ちます (配列 {layout} / 間隔 {a.delay:g}ms / 遅れ {a.latency:g}±{a.jitter:g}ms / "
          f"区切り {a.interval:g}ms)。止めるときはほかのウィンドウをクリック")
    title0 = _text(hwnd)
    t0 = time.perf_counter()
    with KeyWatch() as kw:
        try:
            hid.run(script)
        except InterruptedError as e:
            print(f"   止まりました ({e})。押していたキーは離しました")
            return 1
        except OSError as e:
            print(f"   送れません: {e}")
            return 1
        el = time.perf_counter() - t0
        time.sleep(0.2)
    print(f"   {el:.2f} 秒 ({n_chars / el:.0f} 文字/秒)")
    print(f"   Windows に届いたキー: 押す {kw.down} / 離す {kw.up} (うちプログラムの入力 {kw.injected})"
          + ("" if kw.down else "  ← 0 なら SendInput が Windows に受け付けられていない"))
    if kw.first:
        print("   最初のキー (仮想キー, スキャンコード, 印): " + " ".join(f"({v:#x},{c:#x},{f:#x})" for v, c, f in kw.first))
    title1 = _text(hwnd)
    print(f"   タイトル: '{title0.strip()}' → '{title1.strip()}'" + ("  (変わった = 文書が書き換わった)" if title0 != title1 else ""))

    print("4) Ctrl+A → Ctrl+C で読み戻して比べます")
    if check_dialogs("打ち終わったとき"):
        return 1
    clear_clipboard()  # 前の中身を読み違えないように
    try:
        hid.run("k:ctrl+a;w:300")
        if check_dialogs("すべて選択したとき"):
            return 1
        hid.run("k:ctrl+c;w:300")
    except InterruptedError as e:
        print(f"   読み戻しの途中でサクラエディタが前面でなくなりました ({e})")
        check_dialogs("読み戻しのとき")
        return 1
    if check_dialogs("コピーしたとき"):
        return 1
    clip, fmts = read_clipboard()
    if clip is None:
        print(f"   クリップボードに文字がありません (入っている形式 {fmts or 'なし'})。"
              "Ctrl+A / Ctrl+C がサクラエディタに効いていません。サクラエディタの画面に文字が打ててるか見てください")
        return 1
    got = clip.replace("\r\n", "\n").rstrip("\n").split("\n")
    ok = got == LINES
    for i, want in enumerate(LINES):
        have = got[i] if i < len(got) else "(無い)"
        mark = "OK" if have == want else "NG"
        print(f"   [{mark}] {want}")
        if have != want:
            print(f"        実際: {have}")
            diff = [f"{w!r}→{h!r}" for w, h in zip(want, have) if w != h][:8]
            if diff:
                print(f"        違う文字: {' '.join(diff)}")
    for extra in got[len(LINES):]:
        print(f"   [NG] 余分な行: {extra}")
    print("結果: " + ("成功 (すべて一致)" if ok else "一致しない文字があります"))
    hid.run("k:ctrl+end")  # 選択を外す (誤って上書きしないように)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
