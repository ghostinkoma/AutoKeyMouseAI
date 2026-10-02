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


def find_window(title_contains: str) -> int | None:
    """タイトルに文字列を含む最初の可視ウィンドウのハンドル。"""
    if not IS_WINDOWS:
        return None
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if title_contains.lower() in buf.value.lower():
                    found.append(hwnd)
                    return False
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


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

    def __init__(self, window_title: str):
        import mss

        self._mss = mss.mss()
        self.window_title = window_title
        self.hwnd: int | None = None
        self.rect: Rect | None = None

    def locate(self) -> Rect:
        self.hwnd = find_window(self.window_title)
        if self.hwnd is None:
            raise RuntimeError(f"ウィンドウが見つかりません: '{self.window_title}'")
        self.rect = client_rect(self.hwnd)
        return self.rect

    def grab(self) -> np.ndarray:
        """BGR 画像 (H, W, 3) を返す。"""
        if self.hwnd is None:
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
