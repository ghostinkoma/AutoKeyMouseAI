"""ゲームにフォーカスが無くても (裏にあっても) 操作する: Windows のメッセージを窓に直接送る (Windows のみ)。

ESP32 の HID (本物のキーボード・マウス) は、Windows が手前の窓にしか届けない。
こちらはキー・クリックを「メッセージ」として対象の窓に直接置くので、手前に出さなくてよい。
ただしゲームによっては受け付けない (DirectInput などで本物の入力だけを読むゲーム)。
MU のチャット欄は Windows の Edit 部品なので、文字の書き込みと Enter はこちらでも効く見込みがある。
クリック (Cancel) が効くかは試すまで分からない → tools/bg_test.py で確かめる。
"""
from __future__ import annotations

import ctypes
import sys
import time

IS_WINDOWS = sys.platform == "win32"
WM_SETTEXT, WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x000C, 0x0100, 0x0101, 0x0102
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0200, 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP, WM_ACTIVATE, WM_SETFOCUS = 0x0204, 0x0205, 0x0006, 0x0007
VK_RETURN, VK_ESCAPE = 0x0D, 0x1B
_SCAN = {VK_RETURN: 0x1C, VK_ESCAPE: 0x01}

if IS_WINDOWS:
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]


def child_windows(hwnd: int) -> list[tuple[int, str, bool]]:
    """子ウィンドウの一覧 [(hwnd, クラス名, 見えているか)]。"""
    if not IS_WINDOWS or not hwnd:
        return []
    out = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(h, buf, 64)
        out.append((h, buf.value, bool(user32.IsWindowVisible(h))))
        return True

    user32.EnumChildWindows(hwnd, proc(cb), 0)
    return out


def find_edit(hwnd: int) -> int | None:
    """ゲームの中のチャット入力欄 (Edit)。見えているものを優先。"""
    edits = [(h, vis) for h, cls, vis in child_windows(hwnd) if cls.lower() == "edit"]
    if not edits:
        return None
    edits.sort(key=lambda e: not e[1])
    return edits[0][0]


def _key_lparam(vk: int, up: bool) -> int:
    lp = 1 | (_SCAN.get(vk, 0) << 16)
    if up:
        lp |= (1 << 30) | (1 << 31)
    return lp


def post_key(hwnd: int, vk: int, hold_s: float = 0.05) -> None:
    user32.PostMessageW(hwnd, WM_KEYDOWN, vk, _key_lparam(vk, False))
    if vk == VK_RETURN:
        user32.PostMessageW(hwnd, WM_CHAR, 0x0D, _key_lparam(vk, False))
    time.sleep(hold_s)
    user32.PostMessageW(hwnd, WM_KEYUP, vk, _key_lparam(vk, True))


def type_text(hwnd: int, text: str, per_char_s: float = 0.01) -> None:
    """1 文字ずつ WM_CHAR で送る (Edit なら打ったのと同じように入る)。"""
    for ch in text:
        user32.PostMessageW(hwnd, WM_CHAR, ord(ch), 1)
        time.sleep(per_char_s)


def set_text(edit: int, text: str) -> None:
    user32.SendMessageW(edit, WM_SETTEXT, 0, ctypes.cast(ctypes.c_wchar_p(text), ctypes.c_void_p).value)


def get_text(edit: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.SendMessageW(edit, 0x000D, 256, ctypes.cast(buf, ctypes.c_void_p).value)  # WM_GETTEXT
    return buf.value


def post_click(hwnd: int, x: int, y: int, button: str = "L", hold_s: float = 0.08) -> None:
    """窓の中の (x, y) (クライアント座標) をクリックしたことにする。"""
    lp = (int(y) << 16) | (int(x) & 0xFFFF)
    down, up, mk = (WM_LBUTTONDOWN, WM_LBUTTONUP, 0x0001) if button == "L" else (WM_RBUTTONDOWN, WM_RBUTTONUP, 0x0002)
    user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp)
    time.sleep(0.03)
    user32.PostMessageW(hwnd, down, mk, lp)
    time.sleep(hold_s)
    user32.PostMessageW(hwnd, up, 0, lp)
