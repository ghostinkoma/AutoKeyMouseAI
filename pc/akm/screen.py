"""ゲームウィンドウの検出と画面キャプチャ (Windows)。

座標系はすべて物理ピクセル。高 DPI 環境でもずれないよう、import 時にプロセスを
Per-Monitor DPI Aware にする。
"""
from __future__ import annotations

import ctypes
import sys
import threading
from dataclasses import dataclass

import numpy as np

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # Windows 8 以前
        user32.SetProcessDPIAware()


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return self.left + self.width // 2, self.top + self.height // 2


def primary_screen_size() -> tuple[int, int]:
    if not IS_WINDOWS:
        return 1920, 1080
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def virtual_screen_rect() -> Rect:
    """全モニタを合わせた仮想スクリーン (左や上のモニタは負の座標になる)"""
    if not IS_WINDOWS:
        return Rect(0, 0, 1920, 1080)
    return Rect(user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
                user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))


def cursor_pos() -> tuple[int, int]:
    if not IS_WINDOWS:
        return 0, 0
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def key_pressed(vk: int) -> bool:
    """PC 側キーボードの状態 (緊急停止キー用)。"""
    if not IS_WINDOWS:
        return False
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def window_pid(hwnd: int) -> int:
    if not IS_WINDOWS:
        return 0
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_name(hwnd: int) -> str:
    """ウィンドウを持つプロセスの実行ファイル名 (例: main.exe)。取れなければ空文字。"""
    if not IS_WINDOWS:
        return ""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    kernel32 = ctypes.windll.kernel32
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.replace("/", "\\").split("\\")[-1]
        return ""
    finally:
        kernel32.CloseHandle(h)


def list_windows() -> list[tuple[int, str, str, "Rect"]]:
    """可視でタイトルのあるウィンドウ一覧: (hwnd, タイトル, プロセス名, クライアント領域)"""
    if not IS_WINDOWS:
        return []
    out: list[tuple[int, str, str, Rect]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                out.append((hwnd, buf.value, process_name(hwnd), client_rect(hwnd)))
        return True

    user32.EnumWindows(cb, 0)
    return out


def find_window(title_contains: str, process: str | None = None) -> int | None:
    """ゲームウィンドウを探す。

    process (例: "main.exe") を指定するとそのプロセスのウィンドウだけを対象にする。
    ブラウザのタブ名などに同じ文字列が入っていても取り違えない。
    候補が複数ならクライアント領域が一番大きいものを選ぶ。
    """
    best, best_area = None, -1
    for hwnd, title, proc, rect in list_windows():
        if title_contains and title_contains.lower() not in title.lower():
            continue
        if process and proc.lower() != process.lower():
            continue
        area = rect.width * rect.height
        if area > best_area:
            best, best_area = hwnd, area
    return best


def client_rect(hwnd: int) -> Rect:
    """ウィンドウのクライアント領域 (枠・タイトルバーを除く) のスクリーン座標。"""
    rc = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return Rect(pt.x, pt.y, rc.right - rc.left, rc.bottom - rc.top)


_gdi_ready = False


def _setup_gdi() -> None:
    """64bit でハンドルが切り詰められないよう、使う API の型を宣言する。"""
    global _gdi_ready
    if _gdi_ready:
        return
    gdi32 = ctypes.windll.gdi32
    H = ctypes.c_void_p
    user32.GetDC.restype = H
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, H]
    user32.PrintWindow.argtypes = [wintypes.HWND, H, wintypes.UINT]
    gdi32.CreateCompatibleDC.restype = H
    gdi32.CreateCompatibleDC.argtypes = [H]
    gdi32.CreateCompatibleBitmap.restype = H
    gdi32.CreateCompatibleBitmap.argtypes = [H, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = H
    gdi32.SelectObject.argtypes = [H, H]
    gdi32.DeleteObject.argtypes = [H]
    gdi32.DeleteDC.argtypes = [H]
    gdi32.GetDIBits.argtypes = [H, H, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    _gdi_ready = True


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


def capture_window(hwnd: int) -> np.ndarray | None:
    """ウィンドウのクライアント領域をウィンドウ自身に描かせて取得する (PrintWindow)。

    画面に映っていなくても撮れるので、仮想デスクトップを切り替えたり他のウィンドウが
    上に重なったりしてもゲーム画面が取れる。最小化中や失敗時は None。
    """
    if not IS_WINDOWS:
        return None
    _setup_gdi()
    gdi32 = ctypes.windll.gdi32
    rc = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    w, h = rc.right - rc.left, rc.bottom - rc.top
    if w <= 0 or h <= 0:
        return None
    hdc = user32.GetDC(hwnd)
    if not hdc:
        return None
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        # 1 = PW_CLIENTONLY, 2 = PW_RENDERFULLCONTENT (DirectX のゲームも撮れる。Windows 8.1 以降)
        ok = user32.PrintWindow(hwnd, mem, 3)
        gdi32.SelectObject(mem, old)  # GetDIBits の前に DC から外す
        old = None
        if not ok:
            return None
        bmi = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0):
            return None
        return np.frombuffer(buf, np.uint8).reshape(h, w, 4)[:, :, :3].copy()
    finally:
        if old is not None:
            gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, hdc)


def is_foreground(hwnd: int) -> bool:
    if not IS_WINDOWS:
        return True
    return user32.GetForegroundWindow() == hwnd


class GameScreen:
    """ゲームのクライアント領域をキャプチャする。

    ウィンドウモードで起動すること (排他フルスクリーンだとキャプチャが黒くなることがある)。
    """

    def __init__(self, window_title: str, process: str | None = None, capture: str = "auto"):
        # capture: auto   = ウィンドウから直接 (PrintWindow)。真っ黒など撮れなければ画面から
        #          window = ウィンドウから直接だけ / screen = 画面に映っているものを撮る (mss)
        self.capture = capture
        self._local = threading.local()  # mss はスレッドごとに作る (液晶ミラーが別スレッドで撮る)
        self._warned_fallback = False
        self.window_title = window_title
        self.process = process
        self.hwnd: int | None = None
        self.pid = 0
        self.rect: Rect | None = None

    def locate(self) -> Rect:
        self.hwnd = find_window(self.window_title, self.process)
        if self.hwnd is None:
            raise RuntimeError(
                f"ウィンドウが見つかりません: title='{self.window_title}' process='{self.process}'"
                " (python tools\\list_windows.py で確認)"
            )
        self.rect = client_rect(self.hwnd)
        self.pid = window_pid(self.hwnd)
        print(f"[screen] ゲームウィンドウを固定: hwnd=0x{self.hwnd:X} pid={self.pid} "
              f"process={process_name(self.hwnd)} 画面 {self.rect.width}x{self.rect.height}")
        return self.rect

    def _grab_screen(self, r: Rect) -> np.ndarray:
        m = getattr(self._local, "mss", None)
        if m is None:
            import mss

            m = self._local.mss = mss.mss()
        shot = m.grab({"left": r.left, "top": r.top, "width": r.width, "height": r.height})
        return np.ascontiguousarray(np.asarray(shot)[:, :, :3])

    def grab(self) -> np.ndarray:
        """BGR 画像 (H, W, 3) を返す。起動時に固定したウィンドウを撮り続け、閉じられたら探し直す。"""
        if self.hwnd is None or (IS_WINDOWS and not user32.IsWindow(self.hwnd)):
            self.locate()
        hwnd = self.hwnd
        self.rect = client_rect(hwnd)
        if self.capture in ("auto", "window"):
            img = capture_window(hwnd)
            if img is not None and (self.capture == "window" or img.max() > 8):
                return img
            if self.capture == "window":
                raise RuntimeError("ゲーム画面を取得できません (最小化されていませんか)")
            if not self._warned_fallback:
                self._warned_fallback = True
                print("[screen] ウィンドウから直接撮れないので画面から撮ります (仮想デスクトップを切り替えると別の画面が映ります)")
        return self._grab_screen(self.rect)

    def to_screen(self, x: float, y: float) -> tuple[float, float]:
        """クライアント座標 → スクリーン座標"""
        assert self.rect is not None
        return self.rect.left + x, self.rect.top + y

    def is_active(self) -> bool:
        return self.hwnd is not None and is_foreground(self.hwnd)
