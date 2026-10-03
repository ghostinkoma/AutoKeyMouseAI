"""ゲームウィンドウの検出と画面キャプチャ (Windows)。

座標系はすべて物理ピクセル。高 DPI 環境でもずれないよう、import 時にプロセスを
Per-Monitor DPI Aware にする。
"""
from __future__ import annotations

import ctypes
import sys
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


def is_foreground(hwnd: int) -> bool:
    if not IS_WINDOWS:
        return True
    return user32.GetForegroundWindow() == hwnd


class GameScreen:
    """ゲームのクライアント領域をキャプチャする。

    ウィンドウモードで起動すること (排他フルスクリーンだとキャプチャが黒くなることがある)。
    """

    def __init__(self, window_title: str, process: str | None = None):
        import mss

        self._mss = mss.mss()
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

    def grab(self) -> np.ndarray:
        """BGR 画像 (H, W, 3) を返す。起動時に固定したウィンドウを撮り続け、閉じられたら探し直す。"""
        if self.hwnd is None or (IS_WINDOWS and not user32.IsWindow(self.hwnd)):
            self.locate()
        self.rect = client_rect(self.hwnd)
        r = self.rect
        shot = self._mss.grab({"left": r.left, "top": r.top, "width": r.width, "height": r.height})
        return np.ascontiguousarray(np.asarray(shot)[:, :, :3])

    def to_screen(self, x: float, y: float) -> tuple[float, float]:
        """クライアント座標 → スクリーン座標"""
        assert self.rect is not None
        return self.rect.left + x, self.rect.top + y

    def is_active(self) -> bool:
        return self.hwnd is not None and is_foreground(self.hwnd)
