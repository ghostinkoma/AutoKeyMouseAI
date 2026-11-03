"""仮想 HID: ESP32 の代わりに、この PC から Windows に直接キー・マウスを送る (開発・検証用)。

ESP32 (firmware/AutoKeyMouse/macro.cpp) と同じマクロ書式を同じ意味で実行するので、ここで作ったスクリプトは
そのまま実機の ESP32 でも動く。後で DLL (C/C++) に置き換えやすいよう、層を分けてある:

  run(script) ─ 解析 (parse) ─ 実行 (VirtualHid) ─ 遅れの模擬 (LatencyPipe) ─ 出口 (HidBackend)
                                                                             ├ SendInputBackend (Windows)
                                                                             └ RecordingBackend (テスト)

HidBackend は HID のレポートに近い単純な操作だけ (キーの押下/解放・ボタンの状態・相対/絶対移動・ホイール)。
DLL にするときはこの境目を C の関数にする (akm_hid_key(code, down) など)。

遅れの模擬 (config.yaml の virtual_hid。既定はすべて 0 = 遅れなし):
  event_delay_ms     : 1 つの操作ごとの間隔 (ESP32 の既定と同じ 10。ゲームが押下を取りこぼさないため)
  latency_ms         : 送ってから届くまでの固定の遅れ (BLE で 10〜30)
  jitter_ms          : 遅れのばらつき (0〜この値)
  report_interval_ms : この間隔の区切りでしか届かない (BLE の接続間隔の模擬。7.5〜50)

注意: Windows の SendInput で送った入力には「プログラムが作った入力」の印が付き、ゲーム側から見分けられる。
実機 (ESP32 の HID) とはそこが違う。開発・検証用の出口として使う。
"""
from __future__ import annotations

import heapq
import random
import sys
import threading
import time
from dataclasses import dataclass, field

IS_WINDOWS = sys.platform == "win32"

ABS_MAX = 32767
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x01, 0x02, 0x04
MIN_EVENT_DELAY_MS, MAX_EVENT_DELAY_MS = 5, 50  # d: の範囲 (ESP32 と同じ)


class MacroError(ValueError):
    pass


# ---------------------------------------------------------------- キー名 (keymap.cpp と同じ) --
_NAMED = {
    "enter": 0x28, "return": 0x28, "esc": 0x29, "escape": 0x29, "backspace": 0x2A, "bs": 0x2A, "tab": 0x2B,
    "space": 0x2C, "minus": 0x2D, "equal": 0x2E, "lbracket": 0x2F, "rbracket": 0x30, "backslash": 0x31,
    "semicolon": 0x33, "quote": 0x34, "grave": 0x35, "comma": 0x36, "dot": 0x37, "period": 0x37, "slash": 0x38,
    "capslock": 0x39, "printscreen": 0x46, "scrolllock": 0x47, "pause": 0x48, "insert": 0x49, "ins": 0x49,
    "home": 0x4A, "pageup": 0x4B, "pgup": 0x4B, "delete": 0x4C, "del": 0x4C, "end": 0x4D, "pagedown": 0x4E,
    "pgdn": 0x4E, "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52, "numlock": 0x53, "kp_div": 0x54,
    "kp_mul": 0x55, "kp_minus": 0x56, "kp_plus": 0x57, "kp_enter": 0x58, "kp1": 0x59, "kp2": 0x5A, "kp3": 0x5B,
    "kp4": 0x5C, "kp5": 0x5D, "kp6": 0x5E, "kp7": 0x5F, "kp8": 0x60, "kp9": 0x61, "kp0": 0x62, "kp_dot": 0x63,
    "menu": 0x65, "app": 0x65,
    "ro": 0x87, "kana": 0x88, "yen": 0x89, "henkan": 0x8A, "muhenkan": 0x8B, "zenkaku": 0x35, "hankaku": 0x35,
    "ctrl": 0xE0, "lctrl": 0xE0, "control": 0xE0, "shift": 0xE1, "lshift": 0xE1, "alt": 0xE2, "lalt": 0xE2,
    "win": 0xE3, "gui": 0xE3, "lwin": 0xE3, "cmd": 0xE3, "rctrl": 0xE4, "rshift": 0xE5, "ralt": 0xE6,
    "rwin": 0xE7, "rgui": 0xE7,
}

_SHIFT = 0x80
# US 配列の ASCII 0x20-0x7E → HID (0x80 = Shift)
_ASCII_US = [
    0x2C, 0x1E | _SHIFT, 0x34 | _SHIFT, 0x20 | _SHIFT, 0x21 | _SHIFT, 0x22 | _SHIFT, 0x24 | _SHIFT, 0x34,
    0x26 | _SHIFT, 0x27 | _SHIFT, 0x25 | _SHIFT, 0x2E | _SHIFT, 0x36, 0x2D, 0x37, 0x38,
    0x27, 0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23, 0x24,
    0x25, 0x26, 0x33 | _SHIFT, 0x33, 0x36 | _SHIFT, 0x2E, 0x37 | _SHIFT, 0x38 | _SHIFT,
    0x1F | _SHIFT,
    *[c | _SHIFT for c in range(0x04, 0x1E)],
    0x2F, 0x31, 0x30, 0x23 | _SHIFT, 0x2D | _SHIFT, 0x35,
    *range(0x04, 0x1E),
    0x2F | _SHIFT, 0x31 | _SHIFT, 0x30 | _SHIFT, 0x35 | _SHIFT,
]
assert len(_ASCII_US) == 95
# JIS 配列で US と違う記号
_JP = {'"': 0x1F | _SHIFT, "&": 0x23 | _SHIFT, "'": 0x24 | _SHIFT, "(": 0x25 | _SHIFT, ")": 0x26 | _SHIFT,
       "=": 0x2D | _SHIFT, "^": 0x2E, "~": 0x2E | _SHIFT, "@": 0x2F, "`": 0x2F | _SHIFT, "[": 0x30,
       "{": 0x30 | _SHIFT, "]": 0x32, "}": 0x32 | _SHIFT, ":": 0x34, "*": 0x34 | _SHIFT, "+": 0x33 | _SHIFT}


def key_from_name(name: str) -> int:
    """'a' / 'F1' / 'enter' / '0x3A' → HID Usage ID。不明なら 0。"""
    if not name:
        return 0
    n = name.lower()
    if len(n) == 1:
        if "a" <= n <= "z":
            return 0x04 + ord(n) - ord("a")
        if "1" <= n <= "9":
            return 0x1E + ord(n) - ord("1")
        if n == "0":
            return 0x27
    if n[0] == "f" and len(n) <= 3 and n[1:].isdigit():
        k = int(n[1:])
        if 1 <= k <= 12:
            return 0x3A + k - 1
        if 13 <= k <= 24:
            return 0x68 + k - 13
    if len(n) > 2 and n.startswith("0x"):
        try:
            return int(n[2:], 16) & 0xFF
        except ValueError:
            return 0
    return _NAMED.get(n, 0)


def key_from_char(c: str, layout: str = "jp") -> tuple[int, bool] | None:
    """1 文字 → (HID コード, Shift が要るか)。入力できない文字は None。"""
    if c == "\n":
        return 0x28, False
    if c == "\t":
        return 0x2B, False
    o = ord(c)
    if not 0x20 <= o <= 0x7E:
        return None
    if layout == "jp":
        if c in "\\_|":
            return (0x89 if c == "|" else 0x87), c != "\\"
        v = _JP.get(c, _ASCII_US[o - 0x20])
    else:
        v = _ASCII_US[o - 0x20]
    return v & 0x7F, bool(v & _SHIFT)


# ---------------------------------------------------------------- 解析 (macro.cpp と同じ文法) --
@dataclass
class Op:
    kind: str  # tap / down / up / text / rel / abs / click / bdown / bup / wheel / wait / delay / release
    a: int = 0
    b: int = 0
    keys: tuple[int, ...] = ()
    text: str = ""


def split_script(s: str) -> list[str]:
    out, cur, i = [], [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s) and s[i + 1] in ";\\":
            cur.append(s[i + 1])
            i += 2
            continue
        if c in ";\n\r":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def _int(s: str) -> int:
    s = s.strip()
    if not s or not (s.lstrip("+-").isdigit()):
        raise ValueError(s)
    return int(s)


def _pair(s: str) -> tuple[int, int]:
    if "," not in s:
        raise ValueError(s)
    a, b = s.split(",", 1)
    return _int(a), _int(b)


def _button(s: str) -> int:
    return {"L": BTN_LEFT, "R": BTN_RIGHT, "M": BTN_MIDDLE}[s.strip().upper()]


def _keys(s: str) -> tuple[int, ...]:
    rest, out = s.strip(), []
    while rest:
        plus = rest.find("+", 1)  # 先頭の "+" は名前の一部 (ESP32 と同じ)
        name, rest = (rest, "") if plus < 0 else (rest[:plus], rest[plus + 1:])
        code = key_from_name(name.strip())
        if not code or len(out) >= 4:
            raise ValueError(name)
        out.append(code)
    if not out:
        raise ValueError(s)
    return tuple(out)


def parse(script: str, layout: str = "jp") -> list[Op]:
    """スクリプト → 操作の並び。書式の誤りは MacroError ('line N: invalid command ...'。ESP32 と同じ文面)。"""
    ops: list[Op] = []
    for line_no, raw in enumerate(split_script(script), 1):
        cmd = raw.strip()
        if not cmd or cmd.startswith("#"):
            continue
        name, _, _ = cmd.partition(":")
        arg = raw[raw.find(":") + 1:] if ":" in raw else ""  # t: は前後の空白も文字列のうち
        name = name.strip().lower()
        if name != "t":
            arg = arg.strip()
        try:
            if name in ("k", "kd", "ku"):
                ops.append(Op({"k": "tap", "kd": "down", "ku": "up"}[name], keys=_keys(arg)))
            elif name == "t":
                for ch in arg:
                    if key_from_char(ch, layout) is None:
                        raise ValueError(ch)
                ops.append(Op("text", text=arg))
            elif name == "m":
                a, b = _pair(arg)
                ops.append(Op("rel", a, b))
            elif name == "a":
                a, b = _pair(arg)
                if not (0 <= a <= ABS_MAX and 0 <= b <= ABS_MAX):
                    raise ValueError(arg)
                ops.append(Op("abs", a, b))
            elif name == "c":
                btn, _, n = arg.partition(",")
                count = _int(n) if n else 1
                if not 0 < count <= 100:
                    raise ValueError(arg)
                ops.append(Op("click", _button(btn), count))
            elif name in ("bd", "bu"):
                ops.append(Op("bdown" if name == "bd" else "bup", _button(arg)))
            elif name == "s":
                ops.append(Op("wheel", _int(arg)))
            elif name == "w":
                ms = _int(arg)
                if not 0 <= ms <= 600000:
                    raise ValueError(arg)
                ops.append(Op("wait", ms))
            elif name == "d":
                ms = _int(arg)
                if not MIN_EVENT_DELAY_MS <= ms <= MAX_EVENT_DELAY_MS:
                    raise ValueError(arg)
                ops.append(Op("delay", ms))
            elif name == "ra":
                ops.append(Op("release"))
            else:
                raise ValueError(name)
        except (ValueError, KeyError):
            raise MacroError(f"line {line_no}: invalid command '{cmd}'") from None
    return ops


# ---------------------------------------------------------------- 出口 (DLL にする境目) --
class HidBackend:
    """HID のレポートに近い操作。DLL にするときはこの 6 つを C の関数にする。"""

    def key(self, code: int, down: bool) -> None:  # HID Usage ID (0x04 = a, 0xE0-0xE7 = 修飾)
        raise NotImplementedError

    def buttons(self, mask: int) -> None:  # 今押しているボタン (BTN_LEFT | ...)。前と違うものだけ押す/離す
        raise NotImplementedError

    def move_rel(self, dx: int, dy: int) -> None:  # -127..127
        raise NotImplementedError

    def move_abs(self, x: int, y: int) -> None:  # 0..ABS_MAX (プライマリモニタ全体)
        raise NotImplementedError

    def wheel(self, n: int) -> None:  # -127..127 (+ = 上)
        raise NotImplementedError

    def release_all(self) -> None:
        raise NotImplementedError


class RecordingBackend(HidBackend):
    """送った操作を記録するだけ (テスト・Windows 以外での確認用)。"""

    def __init__(self):
        self.events: list[tuple] = []
        self.t0 = time.perf_counter()

    def _add(self, *e):
        self.events.append((round((time.perf_counter() - self.t0) * 1000, 1),) + e)

    def key(self, code, down):
        self._add("key", code, down)

    def buttons(self, mask):
        self._add("buttons", mask)

    def move_rel(self, dx, dy):
        self._add("rel", dx, dy)

    def move_abs(self, x, y):
        self._add("abs", x, y)

    def wheel(self, n):
        self._add("wheel", n)

    def release_all(self):
        self._add("release")


# HID Usage ID → PC のスキャンコード (set 1)。0xE000 が付いたものは拡張キー (E0 プレフィックス)
_E0 = 0xE000
_SCAN = {
    **dict(zip(range(0x04, 0x1E), [0x1E, 0x30, 0x2E, 0x20, 0x12, 0x21, 0x22, 0x23, 0x17, 0x24, 0x25, 0x26, 0x32,
                                    0x31, 0x18, 0x19, 0x10, 0x13, 0x1F, 0x14, 0x16, 0x2F, 0x11, 0x2D, 0x15, 0x2C])),
    **dict(zip(range(0x1E, 0x28), [0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B])),
    0x28: 0x1C, 0x29: 0x01, 0x2A: 0x0E, 0x2B: 0x0F, 0x2C: 0x39, 0x2D: 0x0C, 0x2E: 0x0D, 0x2F: 0x1A, 0x30: 0x1B,
    0x31: 0x2B, 0x32: 0x2B, 0x33: 0x27, 0x34: 0x28, 0x35: 0x29, 0x36: 0x33, 0x37: 0x34, 0x38: 0x35, 0x39: 0x3A,
    **dict(zip(range(0x3A, 0x44), range(0x3B, 0x45))), 0x44: 0x57, 0x45: 0x58,
    0x46: _E0 | 0x37, 0x47: 0x46, 0x49: _E0 | 0x52, 0x4A: _E0 | 0x47, 0x4B: _E0 | 0x49, 0x4C: _E0 | 0x53,
    0x4D: _E0 | 0x4F, 0x4E: _E0 | 0x51, 0x4F: _E0 | 0x4D, 0x50: _E0 | 0x4B, 0x51: _E0 | 0x50, 0x52: _E0 | 0x48,
    0x53: 0x45, 0x54: _E0 | 0x35, 0x55: 0x37, 0x56: 0x4A, 0x57: 0x4E, 0x58: _E0 | 0x1C,
    0x59: 0x4F, 0x5A: 0x50, 0x5B: 0x51, 0x5C: 0x4B, 0x5D: 0x4C, 0x5E: 0x4D, 0x5F: 0x47, 0x60: 0x48, 0x61: 0x49,
    0x62: 0x52, 0x63: 0x53, 0x65: _E0 | 0x5D,
    **dict(zip(range(0x68, 0x74), [0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x6B, 0x6C, 0x6D, 0x6E, 0x76])),
    0x87: 0x73, 0x88: 0x70, 0x89: 0x7D, 0x8A: 0x79, 0x8B: 0x7B,
    0xE0: 0x1D, 0xE1: 0x2A, 0xE2: 0x38, 0xE3: _E0 | 0x5B, 0xE4: _E0 | 0x1D, 0xE5: 0x36, 0xE6: _E0 | 0x38,
    0xE7: _E0 | 0x5C,
}


def scan_code(hid_code: int) -> int | None:
    """HID Usage ID → スキャンコード (0xE0xx = 拡張キー)。対応表に無ければ None"""
    return _SCAN.get(hid_code)


class SendInputBackend(HidBackend):
    """Windows の SendInput で送る。キーはスキャンコード (キーボードの物理位置) で送るので、
    DirectInput でキーを読むゲームにも届く (仮想キーコードだと届かないことがある)。"""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        ULONG_PTR = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

        class _U(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        self._INPUT, self._MI, self._KI = INPUT, MOUSEINPUT, KEYBDINPUT
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        self._user32.SendInput.restype = wintypes.UINT
        self._down: set[int] = set()
        self._buttons = 0

    def _send(self, inp) -> None:
        if self._user32.SendInput(1, self._ct.byref(inp), self._ct.sizeof(inp)) != 1:
            raise OSError(f"SendInput failed ({self._ct.get_last_error()}). ゲームが管理者で動いているなら、"
                          "このアプリも管理者で起動してください")

    def key(self, code, down):
        sc = scan_code(code)
        inp = self._INPUT(type=1)  # INPUT_KEYBOARD
        if sc is None:
            if code == 0x48:  # Pause は特別なスキャンコード列なので仮想キーで送る
                inp.u.ki = self._KI(wVk=0x13, wScan=0, dwFlags=0 if down else 0x0002, time=0, dwExtraInfo=0)
                self._send(inp)
            return
        flags = 0x0008  # KEYEVENTF_SCANCODE
        if sc & _E0:
            flags |= 0x0001  # KEYEVENTF_EXTENDEDKEY
        if not down:
            flags |= 0x0002  # KEYEVENTF_KEYUP
        inp.u.ki = self._KI(wVk=0, wScan=sc & 0xFF, dwFlags=flags, time=0, dwExtraInfo=0)
        self._send(inp)
        (self._down.add if down else self._down.discard)(code)

    def _mouse(self, dx=0, dy=0, data=0, flags=0):
        inp = self._INPUT(type=0)  # INPUT_MOUSE
        inp.u.mi = self._MI(dx=dx, dy=dy, mouseData=data & 0xFFFFFFFF, dwFlags=flags, time=0, dwExtraInfo=0)
        self._send(inp)

    def buttons(self, mask):
        changed = mask ^ self._buttons
        for bit, dn, up in ((BTN_LEFT, 0x0002, 0x0004), (BTN_RIGHT, 0x0008, 0x0010), (BTN_MIDDLE, 0x0020, 0x0040)):
            if changed & bit:
                self._mouse(flags=dn if mask & bit else up)
        self._buttons = mask

    def move_rel(self, dx, dy):
        self._mouse(dx, dy, flags=0x0001)  # MOUSEEVENTF_MOVE (Windows の「ポインターの精度」の影響を受ける)

    def move_abs(self, x, y):
        # HID の 0..32767 → SendInput の 0..65535 (どちらもプライマリモニタ全体)
        self._mouse(round(x * 65535 / ABS_MAX), round(y * 65535 / ABS_MAX), flags=0x0001 | 0x8000)

    def wheel(self, n):
        self._mouse(data=n * 120, flags=0x0800)  # MOUSEEVENTF_WHEEL (1 段 = 120)

    def release_all(self):
        for code in list(self._down):
            self.key(code, False)
        self.buttons(0)


# ---------------------------------------------------------------- 待ち・遅れの模擬 --
def precise_sleep(sec: float, stop: threading.Event | None = None) -> bool:
    """待つ (Windows の time.sleep は 1〜15ms ずれるので、最後の 2ms は回して待つ)。stop が立ったら False"""
    end = time.perf_counter() + sec
    while True:
        left = end - time.perf_counter()
        if stop is not None and stop.is_set():
            return False
        if left <= 0:
            return True
        if left > 0.003:
            time.sleep(min(left - 0.002, 0.02))
        else:
            pass  # 残り 3ms 未満は回して待つ


@dataclass(order=True)
class _Due:
    t: float
    seq: int
    fn: object = field(compare=False)


class LatencyPipe:
    """操作を「遅れ + ばらつき、送信間隔の区切り」の後に出口へ届ける。順番は入れ替えない。
    遅れの設定がすべて 0 なら、その場で届ける (スレッドを使わない)。"""

    def __init__(self, latency_ms=0.0, jitter_ms=0.0, report_interval_ms=0.0, seed: int | None = None):
        self.latency = latency_ms / 1000
        self.jitter = jitter_ms / 1000
        self.interval = report_interval_ms / 1000
        self._rng = random.Random(seed)
        self._q: list[_Due] = []
        self._cv = threading.Condition()
        self._seq = 0
        self._last = 0.0
        self._pending = 0                 # まだ届けていない数 (届けている途中も含む)
        self._deliver = threading.Lock()  # 届ける操作と release_all を重ねない
        self._thread = None
        self.t0 = time.perf_counter()

    @property
    def passthrough(self) -> bool:
        return not (self.latency or self.jitter or self.interval)

    def put(self, fn) -> None:
        if self.passthrough:
            fn()
            return
        now = time.perf_counter()
        due = now + self.latency + (self._rng.uniform(0, self.jitter) if self.jitter else 0)
        if self.interval:  # 送信間隔の区切り (BLE の接続イベント) まで待つ
            k = -(-(due - self.t0) // self.interval)
            due = self.t0 + k * self.interval
        due = max(due, self._last)  # 順番は入れ替えない
        self._last = due
        with self._cv:
            self._seq += 1
            self._pending += 1
            heapq.heappush(self._q, _Due(due, self._seq, fn))
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="virtual-hid", daemon=True)
                self._thread.start()
            self._cv.notify()

    def _run(self):
        while True:
            with self._cv:
                while not self._q:
                    self._cv.wait()
                d = self._q[0]
                wait = d.t - time.perf_counter()
                if wait > 0.003:
                    self._cv.wait(min(wait - 0.002, 0.02))
                    continue
                if wait > 0:
                    continue  # 残りわずか: 回して待つ
                heapq.heappop(self._q)
                self._deliver.acquire()
            try:
                d.fn()
            except Exception as e:  # 出口の失敗で届けるスレッドを止めない
                print(f"[virtual_hid] {e}", file=sys.stderr)
            finally:
                self._deliver.release()
                with self._cv:
                    self._pending -= 1
                    self._cv.notify_all()

    def flush(self, timeout: float = 2.0) -> bool:
        """溜まっている操作がすべて届くまで待つ。届いたら True"""
        end = time.perf_counter() + timeout
        with self._cv:
            while self._pending:
                left = end - time.perf_counter()
                if left <= 0:
                    return False
                self._cv.wait(min(left, 0.01))
        return True

    def clear(self, then=None) -> None:
        """まだ届けていない操作を捨てる。then があれば、届けている途中のものが終わってから呼ぶ"""
        with self._cv:
            self._pending -= len(self._q)
            self._q.clear()
            self._last = 0.0
            self._cv.notify_all()
        if then is not None:
            with self._deliver:
                then()


# ---------------------------------------------------------------- 人の操作の見張り --
class UserActivity:
    """人 (この PC のキーボード・マウス) が操作したかを見る。人が操作している間は送らないため
    (送り続けると OS の操作ができなくなる)。

    Windows の「最後に入力があった時刻」(GetLastInputInfo) は、こちらが送った入力でも更新される。
    そこで、こちらが最後に送った時刻より margin_ms 以上あとに入力があれば、人の操作とみなす。
    注意: ESP32 (本物の HID) から送る入力も「人の入力」と区別できないので、ESP32 のときは別の見分け方が要る。"""

    def __init__(self, idle_ms: float = 3000, margin_ms: float = 60):
        self.idle_ms = float(idle_ms)
        self.margin_ms = float(margin_ms)
        self._sent = 0  # こちらが最後に送った時刻 (GetTickCount)
        self._k32 = self._u32 = None
        if IS_WINDOWS and self.idle_ms > 0:
            import ctypes
            from ctypes import wintypes

            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

            self._ct, self._LII = ctypes, LASTINPUTINFO
            self._u32 = ctypes.WinDLL("user32")
            self._k32 = ctypes.WinDLL("kernel32")
            self._k32.GetTickCount.restype = wintypes.DWORD

    def _now(self) -> int:
        return int(self._k32.GetTickCount())

    def note_sent(self) -> None:
        if self._k32 is not None:
            self._sent = self._now()

    def last_user_ms_ago(self) -> float | None:
        """人が最後に操作してから何 ms か。こちらが送ったあと操作が無ければ None"""
        if self._k32 is None:
            return None
        li = self._LII()
        li.cbSize = self._ct.sizeof(li)
        if not self._u32.GetLastInputInfo(self._ct.byref(li)):
            return None
        last = int(li.dwTime)
        if ((last - self._sent) & 0xFFFFFFFF) > 0x7FFFFFFF:  # 送った時刻より前 (32 ビットの一周も考える)
            return None
        if ((last - self._sent) & 0xFFFFFFFF) <= self.margin_ms:
            return None  # こちらが送った入力
        return float((self._now() - last) & 0xFFFFFFFF)

    def busy_left_ms(self) -> float:
        """人が操作中なら、あと何 ms 待てば「操作が止まった」とみなすか (0 = 操作していない)"""
        ago = self.last_user_ms_ago()
        return 0.0 if ago is None or ago >= self.idle_ms else self.idle_ms - ago

    def active(self) -> bool:
        return self.busy_left_ms() > 0


# ---------------------------------------------------------------- 実行 (macro.cpp と同じ動き) --
class VirtualHid:
    """スクリプトを ESP32 と同じ手順で実行する。allow() が False になったら (送信先が前面でなくなった等)
    その場で止めて、押しているキー・ボタンをすべて離す (ESP32 のゲートと同じ考え方)。"""

    def __init__(self, backend: HidBackend | None = None, event_delay_ms: float = 10, latency_ms: float = 0,
                 jitter_ms: float = 0, report_interval_ms: float = 0, layout: str = "jp", allow=None, seed=None,
                 user_idle_ms: float = 0):
        if backend is None:
            backend = SendInputBackend() if IS_WINDOWS else RecordingBackend()
        self.backend = backend
        self.event_delay = event_delay_ms / 1000
        self.layout = layout
        self.allow = allow  # () -> bool。None なら常に送る
        self.pipe = LatencyPipe(latency_ms, jitter_ms, report_interval_ms, seed)
        # 人が操作したら止める (user_idle_ms の間、操作が無くなるまで送らない。0 = 見ない)
        self.user = UserActivity(user_idle_ms)
        self._stop = threading.Event()
        self._lock = threading.Lock()  # 1 度に 1 つのスクリプト
        self._down: list[int] = []
        self._buttons = 0
        if IS_WINDOWS:
            try:  # Windows のタイマーの細かさを 1ms に (time.sleep のずれを小さく)
                import ctypes

                ctypes.WinDLL("winmm").timeBeginPeriod(1)
            except Exception:
                pass

    @classmethod
    def from_config(cls, cfg: dict | None, allow=None, backend=None) -> "VirtualHid":
        c = cfg or {}
        return cls(backend, float(c.get("event_delay_ms", 10)), float(c.get("latency_ms", 0)),
                   float(c.get("jitter_ms", 0)), float(c.get("report_interval_ms", 0)), str(c.get("layout", "jp")),
                   allow, user_idle_ms=float(c.get("user_idle_ms", 3000)))

    # -- 操作の送り出し (遅れの模擬を通す)
    def _emit(self, fn, *args) -> None:
        def send():
            fn(*args)
            self.user.note_sent()

        self.pipe.put(send)

    def _ok(self) -> bool:
        return not self._stop.is_set() and not self.user.active() and (self.allow is None or self.allow())

    def _why(self) -> str:
        if self._stop.is_set():
            return "stopped"
        if self.user.active():
            return "user input"
        return "not allowed (gate)"

    def _ev(self, delay: float) -> bool:
        """1 つの操作の後の間隔。止められたら False"""
        if not self._ok():
            return False
        return precise_sleep(delay, self._stop) and self._ok() if delay > 0 else True

    def _key(self, code: int, down: bool) -> None:
        if down and code not in self._down:
            self._down.append(code)
        elif not down and code in self._down:
            self._down.remove(code)
        self._emit(self.backend.key, code, down)

    def _btn(self, mask: int) -> None:
        self._buttons = mask
        self._emit(self.backend.buttons, mask)

    def release_all(self) -> None:
        self._down.clear()
        self._buttons = 0
        self.pipe.clear(then=self.backend.release_all)

    def stop(self) -> None:
        """実行中のスクリプトを止める (別のスレッドから呼んでよい)"""
        self._stop.set()

    def run(self, script: str) -> str:
        """スクリプトを最後まで実行する。返り値は ESP32 と同じ 'ok' / 例外 (書式の誤り・中断)"""
        ops = parse(script, self.layout)
        with self._lock:
            self._stop.clear()
            delay = self.event_delay
            try:
                for op in ops:
                    if not self._ok():
                        raise InterruptedError(self._why())
                    if op.kind == "delay":
                        delay = op.a / 1000
                        continue
                    if not self._exec(op, delay):
                        raise InterruptedError(self._why())
                self.pipe.flush()
                return "ok"
            finally:
                if not self._ok():
                    self.release_all()  # 止められたら押しっぱなしを残さない (ESP32 と同じ)

    def _exec(self, op: Op, delay: float) -> bool:
        k = op.kind
        if k == "tap":
            for c in op.keys:
                self._key(c, True)
                if not self._ev(delay):
                    return False
            for c in reversed(op.keys):
                self._key(c, False)
                if not self._ev(delay):
                    return False
            return True
        if k in ("down", "up"):
            for c in op.keys:
                self._key(c, k == "down")
                if not self._ev(delay):
                    return False
            return True
        if k == "text":
            for ch in op.text:
                code, shift = key_from_char(ch, self.layout)
                if shift:
                    self._key(0xE1, True)
                self._key(code, True)
                if not self._ev(delay):
                    return False
                self._key(code, False)
                if shift:
                    self._key(0xE1, False)
                if not self._ev(delay):
                    return False
            return True
        if k == "rel":
            x, y = op.a, op.b
            while x or y:
                sx, sy = max(-127, min(127, x)), max(-127, min(127, y))
                self._emit(self.backend.move_rel, sx, sy)
                x, y = x - sx, y - sy
                if not self._ev(delay):
                    return False
            return True
        if k == "abs":
            self._emit(self.backend.move_abs, op.a, op.b)
            return self._ev(delay)
        if k == "click":
            for _ in range(op.b):
                self._btn(self._buttons | op.a)
                if not self._ev(delay):
                    return False
                self._btn(self._buttons & ~op.a)
                if not self._ev(delay):
                    return False
            return True
        if k in ("bdown", "bup"):
            self._btn(self._buttons | op.a if k == "bdown" else self._buttons & ~op.a)
            return self._ev(delay)
        if k == "wheel":
            w = op.a
            while w:
                s = max(-127, min(127, w))
                self._emit(self.backend.wheel, s)
                w -= s
                if not self._ev(delay):
                    return False
            return True
        if k == "wait":
            return precise_sleep(op.a / 1000, self._stop) and self._ok()
        if k == "release":
            self.release_all()
            return self._ev(delay)
        return True
