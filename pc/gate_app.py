r"""AutoKeyMouse Gate - Windows 側の仲介アプリ。

    cd D:\hobby\AutoKeyMouseAI\pc
    .\.venv\Scripts\python.exe gate_app.py

  * 上   : リボン (Excel のようなメニュータブ。タブごとにグループ分けしたボタン)
  * 中央 : 左 = 取得マップ / 右 = ESP32 へ送る画面 (240x135。BadCodec に渡す大きさ)
  * 下   : ステータス (送信先・ゲート・接続・現在地など) と HP / MP
  * 最下 : ログ

本物になっている部分 (Windows):
  * 送信先ウィンドウ: 開いている MURex のウィンドウ (クラス名 "MURex Client") を 1 秒ごとに一覧にする。
    タイトルからキャラクター名を取り出し、名前で送信先を覚える
  * 送信画面: 選んだウィンドウだけを撮り (他のウィンドウが重なっていても撮れる)、240x135 に縮めて
    送る形式 (BadCodec 1bit / 16bit / JPEG) の見た目で出す
  * 取得マップ: 選んだウィンドウの現在地を OCR で読み (config.yaml の maplog)、そのマップの
    記録 (data/mu_map.db。tools/map_logger.py が書く) の歩いたマス・登録地点と、このウィンドウの足跡を出す
  * 現在地・HP/MP: 撮った画面から読む (config.yaml の maplog / hud)
まだモックの部分: ゲート (ARM / DISARM)・ESP32 との接続・ボタンの多く。ESP32 には何も送らない。
Windows 以外では、ウィンドウ・画面・マップはダミーを出す。
"""
from __future__ import annotations

import base64
import math
import random
import re
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import scrolledtext, ttk

IS_WINDOWS = sys.platform == "win32"
APP_TITLE = "AutoKeyMouse Gate"
GAME_CLASS = "MURex Client"
SEND_W, SEND_H = 240, 135      # ESP32 へ送る画面 (BadCodec に渡す大きさ)
PREVIEW_ZOOM = 2               # 送信画面の表示倍率
MAP_SIZE = 256                 # MU のマップは 256x256 マス

# "MURex Client || Tiffany || Lv: 400 || Rs: 50 || RC: 71 || GC: 0 || AC: 2,235"
TITLE_RE = re.compile(
    r"\|\|\s*(?P<name>[^|]+?)\s*\|\|\s*Lv:\s*(?P<lv>\d+)\s*\|\|\s*Rs:\s*(?P<rs>\d+)\s*\|\|\s*RC:\s*(?P<rc>[\d,]+)")

# 画面の撮影とマップの記録 (numpy / OpenCV が要る。無ければダミーで動く)
try:
    import cv2

    from akm.gate_live import DITHERS, FORMATS, LiveFeed, MapSource, color_bits
    LIVE_ERROR = ""
except Exception as _e:  # noqa: BLE001
    cv2 = LiveFeed = MapSource = None
    LIVE_ERROR = f"{type(_e).__name__}: {_e}"
    FORMATS = ("Index 256", "BadCodec 565", "JPEG", "BadCodec 1bit")
    DITHERS = ("なし", "8x8")

    def color_bits(fmt):
        tail = fmt.split()[-1]
        return None if not fmt.startswith("BadCodec") else (1, 1, 1) if tail == "1bit" else \
            tuple(int(c) for c in tail) if tail.isdigit() and len(tail) == 3 else (5, 6, 5)


def load_cfg() -> dict:
    try:
        from akm.config import load_config

        return load_config("config.yaml") or {}
    except (Exception, SystemExit):
        return {}


# ---------------------------------------------------------------------------
# 送信先のウィンドウの一覧 (MURex + その他のウィンドウ)
# ---------------------------------------------------------------------------
@dataclass
class GameWindow:
    hwnd: int
    pid: int
    title: str
    name: str
    level: str = "?"
    resets: str = "?"
    rc: str = "?"
    kind: str = "game"     # "game" = MURex / "other" = その他のウィンドウ (VLC・ブラウザなど。映すだけ)
    exe: str = "Main.exe"  # 実行ファイル名 (OBS の撮影先に使う)
    cls: str = GAME_CLASS  # ウィンドウのクラス名 (同上)

    @classmethod
    def parse(cls, hwnd: int, pid: int, title: str) -> "GameWindow":
        m = TITLE_RE.search(title)
        if not m:  # ログイン前などで名前がまだ出ていない
            return cls(hwnd, pid, title, f"(名前なし) {hwnd:#x}")
        return cls(hwnd, pid, title, m["name"], m["lv"], m["rs"], m["rc"])

    @classmethod
    def other(cls, hwnd: int, pid: int, title: str, exe: str, wcls: str) -> "GameWindow":
        name = exe[:-4] if exe.lower().endswith(".exe") else (exe or "?")
        return cls(hwnd, pid, title, name, kind="other", exe=exe, cls=wcls)

    @property
    def is_game(self) -> bool:
        return self.kind == "game"

    def label(self, foreground: bool) -> str:
        fg = "  ← 前面" if foreground else ""
        if not self.is_game:
            t = self.title if len(self.title) <= 40 else self.title[:39] + "…"
            return f"[他] {self.name[:12]:<12} {t}{fg}"
        return f"{self.name:<12} Lv{self.level:>4}  Rs{self.resets:>3}  RC{self.rc:>4}   PID {self.pid}{fg}"


DUMMY_OTHER_WINDOWS = [
    GameWindow.other(0x00A1B2C4, 41000, "sample.mp4 - VLC media player", "vlc.exe", "Qt5QWindowIcon"),
    GameWindow.other(0x00A1B2D8, 41200, "YouTube - Google Chrome", "chrome.exe", "Chrome_WidgetWin_1"),
]
DUMMY_WINDOWS = [
    GameWindow.parse(0x007B0634, 30560, "MURex Client || Tiffany || Lv: 400 || Rs: 50 || RC: 71 || GC: 0 || AC: 2,235"),
    GameWindow.parse(0x0176045C, 24956, "MURex Client || Thanatos || Lv: 400 || Rs: 50 || RC: 18 || GC: 0 || AC: 470"),
    GameWindow.parse(0x003604F8, 31600, "MURex Client || Iovis || Lv: 351 || Rs: 50 || RC: 10 || GC: 0 || AC: 950"),
]

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32")
    _ENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_ENUMPROC, wintypes.LPARAM]
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    _user32.WindowFromPoint.argtypes = [wintypes.POINT]
    _user32.WindowFromPoint.restype = wintypes.HWND
    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetAncestor.restype = wintypes.HWND
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    _user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetWindow.restype = wintypes.HWND
    _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetWindowLongW.restype = ctypes.c_long
    _user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]


# その他のウィンドウから外すもの: このアプリ自身・デスクトップ・タスクバーなど映しても意味の無いもの
OTHER_SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Windows.UI.Core.CoreWindow",
                      "ApplicationFrameWindow_Hidden", "TkTopLevel", "Tk"}
OTHER_MIN_W, OTHER_MIN_H = 160, 90  # これより小さいウィンドウは出さない (ツールの小窓など)


def list_game_windows(include_other: bool = False) -> list[GameWindow]:
    """MURex のウィンドウ。include_other=True なら、その他の見えているウィンドウ (VLC・ブラウザなど) も後ろに付ける"""
    if not IS_WINDOWS:
        return list(DUMMY_WINDOWS) + (list(DUMMY_OTHER_WINDOWS) if include_other else [])
    import os

    from akm.screen import process_name

    games: list[GameWindow] = []
    others: list[GameWindow] = []
    me = os.getpid()

    @_ENUMPROC
    def cb(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, buf, 256)
        wcls = buf.value
        if wcls != GAME_CLASS and not include_other:
            return True
        title = ctypes.create_unicode_buffer(512)
        _user32.GetWindowTextW(hwnd, title, 512)
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if wcls == GAME_CLASS:
            games.append(GameWindow.parse(int(hwnd), int(pid.value), title.value))
            return True
        if (not title.value or wcls in OTHER_SKIP_CLASSES or int(pid.value) == me
                or _user32.GetWindow(hwnd, 4)  # GW_OWNER: ダイアログなど持ち主のある窓
                or _user32.GetWindowLongW(hwnd, -20) & 0x80  # GWL_EXSTYLE の WS_EX_TOOLWINDOW
                or _cloaked(hwnd)):  # 別の仮想デスクトップ以外で隠れている UWP の窓など
            return True
        r = wintypes.RECT()
        if not _user32.GetClientRect(hwnd, ctypes.byref(r)) or r.right < OTHER_MIN_W or r.bottom < OTHER_MIN_H:
            return True
        others.append(GameWindow.other(int(hwnd), int(pid.value), title.value, process_name(int(hwnd)), wcls))
        return True

    _user32.EnumWindows(cb, 0)
    others.sort(key=lambda w: (w.name.lower(), w.title.lower()))
    return games + others


def _cloaked(hwnd) -> bool:
    """DWM で隠されている (起動していない UWP アプリの窓など)。別の仮想デスクトップの窓は出す"""
    try:
        v = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 14, ctypes.byref(v), 4)  # DWMWA_CLOAKED
        return v.value not in (0, 2)  # 2 = DWM_CLOAKED_SHELL (別の仮想デスクトップ)
    except Exception:
        return False


def foreground_hwnd(windows: list[GameWindow]) -> int | None:
    if IS_WINDOWS:
        return int(_user32.GetForegroundWindow() or 0)
    return windows[0].hwnd if windows else None  # ダミー: 先頭が前面


# ---------------------------------------------------------------------------
# リボン (Excel のようなタブ + グループ)
# ---------------------------------------------------------------------------
# BadCodec のフレームをさらに包む符号 (表示 → akm.mirror の entropy)
ENTROPY_MODES = {"なし": None, "tANS": "tans"}  # BadCodec のフレームを tANS で包むか (JPEG は包まない)

# 往復テストの上下の決め方 (表示 → akm.shuttle_test の axis)
AXES = {"画面の上下": "screen", "Y (座標)": "y", "X (座標)": "x"}


def window_title(hwnd) -> str:
    if not IS_WINDOWS or not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def is_admin() -> bool:
    if not IS_WINDOWS:
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def process_elevated(pid: int) -> bool | None:
    """そのプロセスが管理者として動いているか。調べられなければ None
    (管理者でないこのアプリからは、管理者のプロセスを調べられないことが多い = たぶん管理者)"""
    if not IS_WINDOWS or not pid:
        return None
    k32, adv = ctypes.WinDLL("kernel32"), ctypes.WinDLL("advapi32")
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    tok = wintypes.HANDLE()
    try:
        if not adv.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):  # TOKEN_QUERY
            return None
        try:
            val, size = wintypes.DWORD(), wintypes.DWORD()
            if not adv.GetTokenInformation(tok, 20, ctypes.byref(val), 4, ctypes.byref(size)):  # TokenElevation
                return None
            return bool(val.value)
        finally:
            k32.CloseHandle(tok)
    finally:
        k32.CloseHandle(h)


def relaunch_as_admin() -> bool:
    """このアプリを管理者として起動し直す (UAC の確認が出る)。起動できたら True"""
    import os

    params = " ".join(f'"{a}"' for a in sys.argv)
    r = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, os.getcwd(), 1)
    return int(r) > 32


def title_bar_point(hwnd: int) -> tuple[int, int] | None:
    """ウィンドウのタイトルバーで、ほかの窓に隠れていない点 (画面の画素)。無ければ None
    (タイトルバーが無い窓・全部隠れている・別の仮想デスクトップにある)。ゲームの中 (クライアント) はクリックしない"""
    if not IS_WINDOWS:
        return None
    wr = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(wr))
    pt = wintypes.POINT(0, 0)
    _user32.ClientToScreen(hwnd, ctypes.byref(pt))
    cap_top, cap_bottom = wr.top + 4, pt.y - 4  # 外枠とクライアントの間 = タイトルバー
    if cap_bottom - cap_top < 8:
        return None
    y = (cap_top + cap_bottom) // 2
    left, right = wr.left + 40, wr.right - 160  # 左のアイコン (メニュー) と右のボタン (最小化・閉じる) を避ける
    for i in range(1, 12):
        x = left + (right - left) * i // 12
        p = wintypes.POINT(x, y)
        under = _user32.WindowFromPoint(p)
        if under and _user32.GetAncestor(under, 2) == hwnd:  # GA_ROOT
            return x, y
    return None


class Ribbon(ttk.Notebook):
    def __init__(self, master, on_action):
        super().__init__(master)
        self.on_action = on_action

    def tab(self, title: str) -> ttk.Frame:
        f = ttk.Frame(self, padding=(4, 4, 4, 0))
        self.add(f, text=f"  {title}  ")
        return f

    def group(self, tab: ttk.Frame, caption: str) -> ttk.Frame:
        outer = ttk.Frame(tab)
        outer.pack(side=tk.LEFT, fill=tk.Y)
        body = ttk.Frame(outer, padding=(6, 0))
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        ttk.Label(outer, text=caption, style="Caption.TLabel").pack(side=tk.BOTTOM, pady=(2, 0))
        ttk.Separator(tab, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=2)
        return body

    def button(self, parent, text: str, big: bool = False, command=None, **grid) -> ttk.Button:
        b = ttk.Button(parent, text=text, style="Big.TButton" if big else "TButton",
                       command=command or (lambda t=text: self.on_action(t)))
        if grid:
            b.grid(sticky="nsew", padx=1, pady=1, **grid)
        else:
            b.pack(side=tk.LEFT, fill=tk.Y, padx=1)
        return b


# ---------------------------------------------------------------------------
# アプリ本体
# ---------------------------------------------------------------------------
class GateApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("1180x860")
        root.minsize(960, 700)
        self._setup_style()

        self.windows: list[GameWindow] = []
        self.target_name: str | None = None
        self.target_hwnd: int | None = None  # 送信先のウィンドウ (キャラクターを変えても同じ)
        self.armed = tk.BooleanVar(value=False)
        self.bring_front = tk.BooleanVar(value=True)  # 送信先が前面でなければ、仲介アプリが前面に出す
        self.hid_out = tk.StringVar(value="virtual")   # キー・マウスの出口: virtual (この PC) / esp32
        self.shuttle_tiles = tk.IntVar(value=10)
        self.explore_radius = tk.IntVar(value=25)   # マップ探索: 基準点から ±何マス
        self.explore_step = tk.IntVar(value=5)      # 何マスおきに歩くか
        self.explore_minutes = tk.IntVar(value=30)  # 最大何分
        self.shuttle_axis = tk.StringVar(value="画面の上下")  # 上下の決め方
        self.shuttle_wait = tk.IntVar(value=5)
        self._armed = False        # 別のスレッド (往復テスト) から読む用 (tk の変数はスレッドをまたいで読まない)
        self._bring_front = True
        self.shuttle_map = tk.StringVar(value="Crywolf")   # 往復テストのマップ名 (OCR のマップ名は使わない)
        self.mouse_mode = tk.StringVar(value="abs")         # カーソルの動かし方 (akm/pointer.py)
        self.arm_runs = tk.StringVar(value="explore")       # ▶ 開始 (ARM) で続けて始めるもの: explore / shuttle / check / none
        self.bring_front.trace_add("write", lambda *_: setattr(self, "_bring_front", bool(self.bring_front.get())))
        self.vhid = None           # 仮想 HID (akm.virtual_hid.VirtualHid)。初めて使うときに作る
        self.shuttle = None        # 往復テスト (akm.shuttle_test.ShuttleTest)
        self.auto_refresh = tk.BooleanVar(value=True)
        self.show_other = tk.BooleanVar(value=False)  # 送信先の一覧に MURex 以外のウィンドウも出す
        self.mirror_fmt = tk.StringVar(value="Index 256")  # 既定はインデックス 256 色。config の mirror.color で変わり、選んだものは覚えておく
        self.mirror_ent = tk.StringVar(value="なし")  # BadCodec のフレームをさらに包む: なし / tANS
        self.threshold = tk.IntVar(value=100)
        self.dither = tk.StringVar(value="8x8")  # 白黒 1 ビットのディザ (BadCodec の 8x8 にそろえた組織的ディザ)
        self.fps = tk.IntVar(value=15)
        self.com = tk.StringVar(value="COM18")
        self.host = tk.StringVar(value="")
        self.auto_scroll = tk.BooleanVar(value=True)
        self.auto_send = tk.BooleanVar(value=True)  # 送信先を選んだら ESP32 へ送り始める
        self.route = tk.StringVar(value="wifi")      # ESP32 へ送る経路: wifi / serial (有線)
        self.serial_baud = tk.StringVar(value="2000000")
        self.show_trail = tk.BooleanVar(value=True)
        self.show_spots = tk.BooleanVar(value=True)
        self.map_view = 96         # マップに出す広さ (マス)。ホイールで変える
        self.cfg = load_cfg()
        self.capture_mode = tk.StringVar(value=self.cfg_capture_default())
        mc = str((self.cfg.get("mirror") or {}).get("color", "Index 256"))  # 仲介アプリで選んだもの (gate_ui.json) が優先
        if f"BadCodec {mc}" in FORMATS:
            self.mirror_fmt.set(f"BadCodec {mc}")
        elif mc in FORMATS:  # 例 "Index 256"
            self.mirror_fmt.set(mc)
        self.host.set(str((self.cfg.get("device") or {}).get("host") or ""))
        self.sender = None         # ESP32 への送信 (akm.mirror.Mirror)
        self.auto_send.set(bool((self.cfg.get("gate") or {}).get("auto_send", True)))
        gcfg = self.cfg.get("gate") or {}
        self.route.set(str(gcfg.get("route", "wifi")))
        self.serial_baud.set(str(gcfg.get("serial_baud", 2000000)))
        self.com.set(str((self.cfg.get("device") or {}).get("port") or "COM18"))
        self.live = None           # 本物の画面 (LiveFeed)
        self.maps = None           # マップの記録 (MapSource)
        self.snap = None           # 最後に受け取った画面 (Snapshot)
        self.preview_img = None
        self._map_drawn_at = 0.0

        # ダミーの状態
        self.hp, self.mp = 0.82, 0.64
        self.pos = [142, 118]
        self.trail: list[tuple[int, int]] = [tuple(self.pos)]
        self.walked: set[tuple[int, int]] = {tuple(self.pos)}
        self.spots = [("Atlans 狩場1", 150, 110, 8, "#ff9a3c"), ("薬屋", 120, 140, 4, "#4fc3f7"),
                      ("安全地帯", 128, 128, 6, "#9ccc65")]
        self.t0 = time.time()
        self.frames = 0

        self._load_ui_settings()
        self._build_ribbon()
        self._build_log()        # 下から順に詰める (ログ → ステータス → 中央)
        self._build_status()
        self._build_center()

        self.log("INFO", "起動しました。ゲートと ESP32 との接続はまだモックです (ESP32 には何も送りません)")
        self.log("INFO", "MURex のウィンドウを監視します (クラス名 '%s')" % GAME_CLASS
                 if IS_WINDOWS else "Windows 以外なのでダミーのウィンドウ・画面・マップを表示します")
        if IS_WINDOWS:
            self.log("INFO" if is_admin() else "WARN",
                     "管理者として動いています" if is_admin() else
                     "管理者として動いていません。ゲームは管理者で動くので、このままではキー・マウスが届きません"
                     " (「ファイル」タブの「管理者で起動し直す」)")
        root.report_callback_exception = self._tk_error  # ボタンなどの中のエラーもログに出す
        self._start_live()
        self.refresh_windows()
        self._tick_fast()
        self._tick_slow()
        self._tick_frame()

    # ---- 見た目 -------------------------------------------------------------
    def _setup_style(self):
        family = "Yu Gothic UI" if IS_WINDOWS else "Noto Sans CJK JP"
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(family=family, size=9)
        self.mono = tkfont.Font(family="Consolas" if IS_WINDOWS else "Noto Sans Mono CJK JP", size=9)
        s = ttk.Style()
        if "vista" in s.theme_names():
            s.theme_use("vista")
        elif "clam" in s.theme_names():
            s.theme_use("clam")
        s.configure("Caption.TLabel", foreground="#666666", font=(family, 8))
        s.configure("Big.TButton", padding=(10, 14))
        s.configure("Key.TLabel", foreground="#555555")
        s.configure("Val.TLabel", font=(family, 9, "bold"))

    # ---- リボン -------------------------------------------------------------
    def _build_ribbon(self):
        rb = self.ribbon = Ribbon(self.root, self.action)
        rb.pack(side=tk.TOP, fill=tk.X)

        # ファイル
        t = rb.tab("ファイル")
        g = rb.group(t, "設定")
        rb.button(g, "config.yaml\nを開く")
        rb.button(g, "設定を保存")
        rb.button(g, "再読み込み")
        g = rb.group(t, "アプリ")
        rb.button(g, "ログを保存")
        rb.button(g, "管理者で\n起動し直す", command=self.restart_as_admin)
        rb.button(g, "終了", command=self.close)

        # ホーム: 送信先の選択とゲート
        t = rb.tab("ホーム")
        g = rb.group(t, "送信先ウィンドウ")
        self.win_list = tk.Listbox(g, height=4, width=62, exportselection=False, font=self.mono,
                                   activestyle="none", selectbackground="#2b7de9")
        self.win_list.grid(row=0, column=0, rowspan=2, sticky="nsew")
        self.win_list.bind("<<ListboxSelect>>", self.on_select_window)
        side = ttk.Frame(g)
        side.grid(row=0, column=1, rowspan=2, sticky="ns", padx=(4, 0))
        ttk.Button(side, text="更新", command=self.refresh_windows).pack(fill=tk.X)
        ttk.Checkbutton(side, text="自動 (1秒)", variable=self.auto_refresh).pack(anchor="w", pady=(4, 0))
        # その他のウィンドウ (VLC・ブラウザなど): 液晶に映すだけ (ゲートや試験・マップ・HP は使わない)
        ttk.Checkbutton(side, text="その他の\nウィンドウも", variable=self.show_other,
                        command=self.refresh_windows).pack(anchor="w")
        ttk.Checkbutton(side, text="前面に出して\n実行する", variable=self.bring_front).pack(anchor="w")

        g = rb.group(t, "ゲート")
        rb.button(g, "▶ 開始\n(ARM)", big=True, command=self.arm)
        rb.button(g, "■ 停止\n(全キー解除)", big=True, command=self.disarm)

        g = rb.group(t, "動作")
        rb.button(g, "ボット開始", row=0, column=0)
        rb.button(g, "ボット停止", row=1, column=0)
        rb.button(g, "MU Helper\n切り替え", row=0, column=1, rowspan=2)
        # 帰還: ラベル + 街の名前のボタン + ▼ (akm/split_button.py)。押すと /move を送る (今はログに出すだけ)
        from akm.split_button import SplitButton
        from akm.town_move import DEFAULT_TOWN, find_town, load_towns, move_script

        towns = load_towns(self.cfg)
        first = find_town(towns, (self.cfg.get("town_move") or {}).get("default", DEFAULT_TOWN))
        self.town_button = SplitButton(
            g, "帰還", [(t.label, t) for t in towns], selected=towns.index(first) if first else 0,
            on_click=lambda t: self.log("INFO", f"(モック) 帰還 {t.label}: {move_script(t)}"))
        self.town_button.grid(row=0, column=2, rowspan=2, sticky="w", padx=(6, 0))

        # 試験: 往復テスト (上へ N マス → Home → 待つ → 元 → Home → 待つ → 下へ … の繰り返し)
        # (ホーム タブは横に収まらないので、別のタブにする)
        t = rb.tab("試験")
        g = rb.group(t, "試験 (往復)")
        rb.button(g, "往復テスト\n開始", row=0, column=0, rowspan=2, command=self.start_shuttle)
        rb.button(g, "停止", row=0, column=1, command=self.stop_shuttle)
        rb.button(g, "マウス診断", row=1, column=1, command=self.start_mouse_check)
        ttk.Label(g, text="マス").grid(row=0, column=2, sticky="e", padx=(6, 2))
        ttk.Spinbox(g, from_=1, to=20, textvariable=self.shuttle_tiles, width=4).grid(row=0, column=3)
        ttk.Label(g, text="待つ秒").grid(row=1, column=2, sticky="e", padx=(6, 2))
        ttk.Spinbox(g, from_=1, to=120, textvariable=self.shuttle_wait, width=4).grid(row=1, column=3)
        ttk.Label(g, text="マップ").grid(row=0, column=4, sticky="e", padx=(6, 2))
        ttk.Entry(g, textvariable=self.shuttle_map, width=8).grid(row=0, column=5)
        ttk.Label(g, text="マウス").grid(row=1, column=4, sticky="e", padx=(6, 2))
        ttk.Combobox(g, textvariable=self.mouse_mode, values=["abs", "absrel", "rel"], width=6,
                     state="readonly").grid(row=1, column=5)
        ttk.Label(g, text="方向").grid(row=0, column=6, sticky="e", padx=(6, 2))
        ttk.Combobox(g, textvariable=self.shuttle_axis, values=list(AXES), width=9,
                     state="readonly").grid(row=0, column=7)
        g = rb.group(t, "マップ探索")
        rb.button(g, "探索開始", row=0, column=0, rowspan=2, command=lambda: self.start_shuttle(kind="explore"))
        for i, (text, var, lo, hi) in enumerate((("範囲 ±", self.explore_radius, 5, 60),
                                                 ("間隔", self.explore_step, 1, 10),
                                                 ("最大 分", self.explore_minutes, 1, 240))):
            ttk.Label(g, text=text).grid(row=i % 2, column=1 + 2 * (i // 2), sticky="e", padx=(6, 2))
            ttk.Spinbox(g, from_=lo, to=hi, textvariable=var, width=4).grid(row=i % 2, column=2 + 2 * (i // 2))
        g = rb.group(t, "▶ 開始 (ARM) を押したら")
        for i, (v, text) in enumerate((("shuttle", "往復テストを始める"), ("explore", "マップ探索を始める"),
                                       ("check", "マウス診断をする"), ("none", "前面に出すだけ"))):
            ttk.Radiobutton(g, text=text, value=v, variable=self.arm_runs).grid(row=i % 2, column=i // 2, sticky="w")

        # ミラー: ESP32 へ送る画面
        t = rb.tab("ミラー")
        g = rb.group(t, "撮影方式")
        for i, (v, text) in enumerate((("obs", "OBS (ゲームキャプチャ)"), ("window", "ウィンドウから直接"))):
            ttk.Radiobutton(g, text=text, value=v, variable=self.capture_mode,
                            command=lambda t=text: self.log("INFO", f"撮影方式: {t}")).grid(row=i, column=0, sticky="w")
        g = rb.group(t, "形式")
        # BadCodec のカラーは R G B のビット数。565 = RGB565 そのまま (可逆・一番きれい・1 枚が一番大きい)
        labels = {"Index 256": "インデックス 8 ビット (256 色)", "BadCodec 565": "BadCodec 16 ビット (可逆)",
                  "JPEG": "JPEG", "BadCodec 1bit": "白黒 1 ビット"}
        names = [labels.get(f, f) for f in FORMATS]
        by_label = dict(zip(names, FORMATS))
        fmt_label = tk.StringVar(value=labels.get(self.mirror_fmt.get(), names[0]))

        def pick(_e=None):
            f = by_label[fmt_label.get()]
            self.mirror_fmt.set(f)  # 欄の有効・無効は _sync_fmt_widgets (mirror_fmt を見張っている)
            self.log("INFO", f"送信形式: {fmt_label.get()}")  # 送っている途中なら sender_status が送り直す

        cb = ttk.Combobox(g, textvariable=fmt_label, values=names, width=24, state="readonly")
        cb.grid(row=0, column=0, sticky="w")
        cb.bind("<<ComboboxSelected>>", pick)
        # tANS = BadCodec の 1 フレームをまるごと tANS で包む (frame-stream-35 以降)。インデックスで 2〜3 割小さくなる。
        # 白黒は 1 枚が小さく Wi-Fi に余裕があるので包まない (戻す手間だけ増える)。JPEG は圧縮済み
        self._ent_box = ttk.Checkbutton(g, text="tANS で圧縮", variable=self.mirror_ent, onvalue="tANS", offvalue="なし")
        self._ent_box.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self._ent_cap = ttk.Label(g, text="", style="Caption.TLabel")
        self._ent_cap.grid(row=2, column=0, sticky="w")
        g = rb.group(t, "白黒 1 ビット")
        self._mono_widgets = [ttk.Label(g, text="しきい値"),
                              ttk.Scale(g, from_=0, to=255, variable=self.threshold, length=140),
                              ttk.Label(g, textvariable=self.threshold)]
        self._mono_widgets[0].grid(row=0, column=0, sticky="w")
        self._mono_widgets[1].grid(row=1, column=0)
        self._mono_widgets[2].grid(row=1, column=1)
        df = ttk.Frame(g)
        df.grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self._mono_widgets.append(ttk.Label(df, text="ディザ"))
        self._mono_widgets[-1].pack(side=tk.LEFT)
        self._dither_box = ttk.Combobox(df, textvariable=self.dither, values=list(DITHERS), width=6, state="readonly")
        self._dither_box.pack(side=tk.LEFT)
        self._mono_cap = ttk.Label(g, text="", style="Caption.TLabel")
        self._mono_cap.grid(row=3, column=0, columnspan=2, sticky="w")
        self.mirror_fmt.trace_add("write", lambda *_: self._sync_fmt_widgets())
        self._sync_fmt_widgets()
        g = rb.group(t, "送信経路")
        for i, (v, text) in enumerate((("wifi", "Wi-Fi"), ("serial", "有線 (COM)"))):
            ttk.Radiobutton(g, text=text, value=v, variable=self.route,
                            command=self._route_changed).grid(row=i, column=0, sticky="w")
        g = rb.group(t, "送信")
        ttk.Label(g, text="fps").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(g, from_=1, to=60, textvariable=self.fps, width=5).grid(row=0, column=1)
        rb.button(g, "送信開始", row=1, column=0, command=self.start_sending)
        ttk.Checkbutton(g, text="選んだら自動で送信", variable=self.auto_send).grid(row=2, column=0, columnspan=2, sticky="w")
        rb.button(g, "送信停止", row=1, column=1, command=self.stop_sending)
        g = rb.group(t, "切り出し")
        rb.button(g, "画面全体")
        rb.button(g, "範囲を\n指定...")

        # デバイス: キー・マウスの出口 / ESP32
        t = rb.tab("デバイス")
        g = rb.group(t, "キー・マウスの出口")
        for i, (v, text) in enumerate((("virtual", "仮想 HID (この PC から送る)"), ("esp32", "ESP32 (BLE HID)"))):
            ttk.Radiobutton(g, text=text, value=v, variable=self.hid_out,
                            command=lambda t=text: self.log("INFO", f"キー・マウスの出口: {t}")).grid(
                row=i, column=0, sticky="w")
        g = rb.group(t, "シリアル")
        ttk.Label(g, text="ポート").grid(row=0, column=0, sticky="w")
        ttk.Combobox(g, textvariable=self.com, values=["COM18", "COM14"], width=8).grid(row=0, column=1)
        ttk.Label(g, text="有線の速さ").grid(row=1, column=0, sticky="w")
        ttk.Combobox(g, textvariable=self.serial_baud, values=["921600", "2000000", "3000000", "4000000"],
                     width=8).grid(row=1, column=1)
        g = rb.group(t, "Wi-Fi")
        ttk.Label(g, text="IP").grid(row=0, column=0, sticky="w")
        ttk.Entry(g, textvariable=self.host, width=14).grid(row=0, column=1)
        rb.button(g, "Web UI を開く", row=1, column=0, columnspan=2)
        g = rb.group(t, "BLE")
        rb.button(g, "状態を確認")
        rb.button(g, "ペアリング\n解除")
        g = rb.group(t, "ファームウェア")
        rb.button(g, "版を確認")
        rb.button(g, "書き込み...")

        # マップ
        t = rb.tab("マップ")
        g = rb.group(t, "記録")
        rb.button(g, "記録開始")
        rb.button(g, "記録停止")
        rb.button(g, "DB を開く...")
        g = rb.group(t, "地点")
        rb.button(g, "現在地を\n狩場に登録")
        rb.button(g, "地点の一覧")
        g = rb.group(t, "表示")
        ttk.Checkbutton(g, text="足跡", variable=self.show_trail, command=self.draw_map).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(g, text="登録地点", variable=self.show_spots, command=self.draw_map).grid(row=1, column=0, sticky="w")

        # AI・学習
        t = rb.tab("AI・学習")
        g = rb.group(t, "ESP32 のモデル")
        rb.button(g, "窓モデルを\n送る")
        rb.button(g, "モデルの\n情報")
        g = rb.group(t, "学習")
        rb.button(g, "素材を集める")
        rb.button(g, "学習開始")

        # 設定
        t = rb.tab("設定")
        g = rb.group(t, "ゲート")
        ttk.Label(g, text="心拍の間隔 100 ms\n途切れたら停止 300 ms").pack()
        g = rb.group(t, "ログ")
        rb.button(g, "保存先...")

        rb.select(1)  # ホームを開いておく

    # ---- 中央: 取得マップ / 送信画面 -----------------------------------------
    def _build_center(self):
        pw = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        pw.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=(6, 0))

        left = ttk.LabelFrame(pw, text=" 取得マップ ", padding=4)
        self.map_title = ttk.Label(left, text="Atlans", style="Val.TLabel")
        self.map_title.pack(anchor="w")
        self.map_canvas = tk.Canvas(left, bg="#101418", highlightthickness=0)
        self.map_canvas.pack(fill=tk.BOTH, expand=True)
        self.map_canvas.bind("<Configure>", lambda e: self.draw_map())
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.map_canvas.bind(ev, self.on_map_wheel)
        pw.add(left, weight=3)

        right = ttk.LabelFrame(pw, text=f" 送信画面 {SEND_W}×{SEND_H} (BadCodec へ) ", padding=4)
        self.preview = tk.Canvas(right, width=SEND_W * PREVIEW_ZOOM, height=SEND_H * PREVIEW_ZOOM,
                                 bg="black", highlightthickness=1, highlightbackground="#888")
        self.preview.pack(anchor="n")
        self.preview_info = ttk.Label(right, text="", font=self.mono)
        self.preview_info.pack(anchor="w", pady=(4, 0))
        self.preview_src = ttk.Label(right, text="", foreground="#555")
        self.preview_src.pack(anchor="w")
        # ESP32 が測った 1 枚あたりの時間 (/status の mirror)。受信待ち / 展開 / 液晶への描画 (SPI)
        self.esp_info = ttk.Label(right, text="", font=self.mono)
        self.esp_info.pack(anchor="w")
        self.esp_stats = None
        self.frame_img = tk.PhotoImage(width=SEND_W, height=SEND_H)
        self.preview_img = None
        pw.add(right, weight=0)

    # ---- ステータスと HP / MP ----------------------------------------------
    def _build_status(self):
        box = ttk.LabelFrame(self.root, text=" ステータス ", padding=(8, 4))
        box.pack(side=tk.BOTTOM, fill=tk.X, padx=6, pady=(6, 0))

        self.status_vals: dict[str, ttk.Label] = {}
        items = [("送信先", "target"), ("ゲート", "gate"), ("前面", "fg"), ("ESP32", "esp"),
                 ("BLE", "ble"), ("Wi-Fi", "wifi"), ("現在地", "loc"), ("MU Helper", "helper"),
                 ("対象", "mob"), ("送信", "send")]
        grid = ttk.Frame(box)
        grid.pack(side=tk.LEFT, fill=tk.X, expand=True)
        for i, (k, key) in enumerate(items):
            r, c = divmod(i, 5)
            ttk.Label(grid, text=k, style="Key.TLabel").grid(row=r, column=c * 2, sticky="e", padx=(8, 4), pady=1)
            v = ttk.Label(grid, text="-", style="Val.TLabel", width=18)
            v.grid(row=r, column=c * 2 + 1, sticky="w")
            self.status_vals[key] = v

        bars = ttk.Frame(box)
        bars.pack(side=tk.RIGHT, padx=(12, 0))
        self.hp_bar = self._bar(bars, 0)
        self.mp_bar = self._bar(bars, 1)

    def _bar(self, parent, row: int) -> tk.Canvas:
        c = tk.Canvas(parent, width=260, height=20, bg="#222", highlightthickness=1, highlightbackground="#666")
        c.grid(row=row, column=0, pady=2)
        return c

    def _draw_bar(self, c: tk.Canvas, label: str, ratio: float | None, color: str):
        c.delete("all")
        w, h = int(c["width"]), int(c["height"])
        if ratio is not None:
            c.create_rectangle(0, 0, int(w * ratio), h, fill=color, width=0)
        text = f"{label} {ratio * 100:3.0f}%" if ratio is not None else f"{label} ?"
        c.create_text(6, h // 2, text=text, anchor="w", fill="white",
                      font=(tkfont.nametofont("TkDefaultFont").actual("family"), 9, "bold"))

    # ---- ログ ---------------------------------------------------------------
    def _build_log(self):
        box = ttk.LabelFrame(self.root, text=" ログ ", padding=4)
        box.pack(side=tk.BOTTOM, fill=tk.X, padx=6, pady=6)
        bar = ttk.Frame(box)
        bar.pack(fill=tk.X)
        ttk.Button(bar, text="クリア", command=lambda: self.log_text.delete("1.0", tk.END)).pack(side=tk.LEFT)
        ttk.Checkbutton(bar, text="自動スクロール", variable=self.auto_scroll).pack(side=tk.LEFT, padx=8)
        self.log_text = scrolledtext.ScrolledText(box, height=6, font=self.mono, bg="#1e1e1e", fg="#d4d4d4",
                                                  insertbackground="white")
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        for tag, color in (("INFO", "#9cdcfe"), ("WARN", "#dcdcaa"), ("ERR", "#f48771"), ("GATE", "#b5cea8"), ("FPS", "#c586c0"),
                           ("TIME", "#808080")):
            self.log_text.tag_configure(tag, foreground=color)

    def log(self, level: str, msg: str):
        line = f"{time.strftime('%H:%M:%S')} [{level:<4}] {msg}"
        try:  # PowerShell の画面と logs/gate_日付.log にも出す (貼り付けて見せやすいように)
            print(line, flush=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            from pathlib import Path

            d = Path(__file__).resolve().parent / "logs"
            d.mkdir(exist_ok=True)
            with open(d / f"gate_{time.strftime('%Y%m%d')}.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        self.log_text.insert(tk.END, time.strftime("%H:%M:%S "), "TIME")
        self.log_text.insert(tk.END, f"[{level:<4}] ", level)
        self.log_text.insert(tk.END, msg + "\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > 2000:
            self.log_text.delete("1.0", "200.0")
        if self.auto_scroll.get():
            self.log_text.see(tk.END)

    def action(self, name: str):
        self.log("INFO", f"(モック) 「{name.replace(chr(10), '')}」が押されました")

    # ---- 本物の画面とマップ -------------------------------------------------------
    def _start_live(self):
        if not IS_WINDOWS:
            return
        if LiveFeed is None:
            self.log("WARN", f"画面の撮影を使えません ({LIVE_ERROR})。pc の .venv の python で起動してください")
            return
        try:
            self.live = LiveFeed(self.cfg, log=self.log_threadsafe)
        except Exception as e:  # noqa: BLE001
            self.log("ERR", f"画面の撮影を始められません: {e}")
            return
        from akm.config import config_path

        db = config_path((self.cfg.get("maplog") or {}).get("db", "data/mu_map.db"))
        self.maps = MapSource(db)
        self.log("INFO", f"マップの記録: {db}" + ("" if db.exists() else " (まだありません。tools\\map_logger.py で記録できます)"))

    def cfg_capture_default(self) -> str:
        """撮影方式の既定: config.yaml の gate.capture (obs / window)。無ければ OBS"""
        return str((self.cfg.get("gate") or {}).get("capture", "obs"))

    def _tk_error(self, exc, val, tb):
        import traceback

        where = traceback.extract_tb(tb)[-1] if tb else None
        self.log("ERR", f"{exc.__name__}: {val}" + (f" ({where.name} {where.lineno} 行目)" if where else ""))
        traceback.print_exception(exc, val, tb)

    def _mirror_print(self, *args, **kw):
        """akm/mirror.py の print をログ欄に出す (送信の様子・失敗の理由)"""
        msg = " ".join(str(a) for a in args)
        print(msg, **kw)
        self.log_threadsafe("ERR" if "失敗" in msg or "できません" in msg else "INFO", msg)

    # ---- ESP32 への送信 (液晶ミラー。akm/mirror.py の TCP 5005 + BadCodec 16bit をそのまま使う) ----------
    def start_sending(self):
        if self.live is None:
            self.log("WARN", "画面を撮れていないので送れません")
            return
        host = self.host.get().strip()
        wired = self.route.get() == "serial"
        if not host and not wired:
            self.log("WARN", "ESP32 の IP を「デバイス」タブの Wi-Fi に入れてください (シリアルの status / 液晶に出ます)")
            return
        self.stop_sending()
        import akm.mirror as mirror_mod
        from akm.mirror import Mirror

        mirror_mod.print = self._mirror_print  # 送信の様子をログ欄へ

        mcfg = self.cfg.get("mirror") or {}
        fmt = self.mirror_fmt.get()
        # BadCodec: 選んだ色のビット数で送る (1bit は送信画面と同じ白黒の絵を各色 1 ビット = 3 面で)
        bits = color_bits(fmt)
        if fmt == "Index 256":  # インデックスカラー (frame-stream-36 以降)
            kind, bits = "bad16", (5, 6, 5)
        else:
            kind, bits = ("jpeg", (3, 4, 3)) if bits is None else ("bad16", bits)
        link, where = None, host
        if wired:  # 有線: COM で送る (akm/serial_link.py。ファームウェア frame-stream-15 以降)
            from akm.serial_link import SerialFrameLink

            port, baud = self.com.get().strip(), int(self.serial_baud.get())

            def make_link():
                return SerialFrameLink(port, baud, log=self._mirror_print)

            link = make_link

            where = f"{port} {baud:,}bps"
        self.sender = Mirror(host or "serial", 1.0 / max(1, int(self.fps.get())), fmt=kind,
                             quality=int(mcfg.get("quality", 70)), grab=self._sender_grab,
                             # HTTP に逃げない (逃げると 1 枚ずつの POST が並行して届き、液晶が詰まる)
                             transport="tcp",
                             port=int(mcfg.get("port", 5005)), b16_bits=bits,
                             entropy=ENTROPY_MODES.get(self._ent_effective()) if kind == "bad16" else None,
                             palette=fmt == "Index 256",
                             # 有線は 1 枚ずつ返事を待つ (ESP32 の受信バッファ 8KB を溢れさせない)
                             pipeline=1 if wired else 2, link_factory=link,
                             b16_prev_free=not mcfg.get("with_prev", False), b16_gray=bool(mcfg.get("gray", False)))
        self.sender.set_lines([])  # 液晶にはゲームの画面だけを出す
        ent = self._ent_effective() if kind == "bad16" else "なし"
        self.sender_fmt = fmt
        self.sender_ent = self._ent_effective()
        self.sender_route = self.route.get()
        self._fps_begin(fmt + ("" if ent == "なし" else f" +{ent}"))
        if host:
            self._start_esp_poll(host)  # 有線のときも、内訳は Wi-Fi の /status から読む
        self.log("INFO", f"ESP32 ({where}) への送信を始めました: {fmt} → " +
                 ("インデックス 256 色" if fmt == "Index 256" else f"{kind} {''.join(map(str, bits))}"))

    def _route_changed(self):
        self.log("INFO", f"送信経路: {'有線 (' + self.com.get() + ')' if self.route.get() == 'serial' else 'Wi-Fi'}")
        if self.sender is not None:
            self.start_sending()  # 送っている途中なら経路を切り替えて送り直す

    def _start_esp_poll(self, host: str):
        """2 秒ごとに ESP32 の /status を読み、液晶側の内訳を esp_stats に置く (別スレッド)"""
        import threading

        def run(sender):
            import requests

            while self.sender is sender:
                try:
                    self.esp_stats = requests.get(f"http://{host}/status", timeout=2).json().get("mirror")
                except Exception:
                    self.esp_stats = None
                time.sleep(2)

        threading.Thread(target=run, args=(self.sender,), daemon=True).start()

    def show_esp_stats(self):
        st = self.esp_stats if self.sender is not None else None
        if not st:
            self.esp_info.configure(text="")
            return
        self.esp_info.configure(
            text=f"ESP32 {st.get('fps', 0):.1f}fps 受信待ち {st.get('recv_ms', 0):.0f}/展開 "
                 f"{st.get('decode_ms', 0):.0f}/描画(SPI) {st.get('draw_ms', 0):.0f}ms "
                 f"CPU{st.get('cpu_pct', 0):.0f}% {st.get('kbps', 0) / 8:.0f}KB/s")

    # ---- 平均 fps をログに出す (テキストで比べやすいように 1 行にまとめる) ------------------------------
    FPS_LOG_S = 10.0  # この間隔ごとに、その間の平均を 1 行出す

    def _fps_begin(self, fmt: str):
        m = self.sender
        now = time.monotonic()
        self._fps = {"fmt": fmt, "t0": now, "tw": now, "sent_w": 0, "bytes_w": 0, "fail_w": 0, "esp": [], "esp_w": []}
        if m is not None:
            self.root.after(int(self.FPS_LOG_S * 1000), lambda: self._fps_tick(m))

    @staticmethod
    def _esp_avg(samples):
        """ESP32 の /status の mirror (fps と内訳) の平均。無ければ None"""
        if not samples:
            return None
        keys = ("fps", "recv_ms", "decode_ms", "draw_ms", "cpu_pct", "kbps")
        return {k: sum(float(x.get(k, 0)) for x in samples) / len(samples) for k in keys}

    def _fps_line(self, label: str, fmt: str, dt: float, sent: int, nbytes: int, fails: int, esp) -> str:
        fps = sent / dt if dt > 0 else 0.0
        kb = nbytes / sent / 1024 if sent else 0.0
        line = (f"{label}{fmt.replace(' ', '')} {dt:.0f}s PC {fps:.1f}fps {kb:.1f}KB/枚 "
                f"{nbytes / dt / 1024 if dt > 0 else 0:.0f}KB/s 失敗{fails}")
        if esp:
            line += (f" | ESP {esp['fps']:.1f}fps 受信待ち{esp['recv_ms']:.0f} 展開{esp['decode_ms']:.0f} "
                     f"描画{esp['draw_ms']:.0f}ms CPU{esp['cpu_pct']:.0f}%")
        return line

    def _fps_tick(self, m):
        """FPS_LOG_S ごと: その間の平均を 1 行ログに出す"""
        f = getattr(self, "_fps", None)
        if m is not self.sender or f is None:
            return
        now = time.monotonic()
        st = self.esp_stats
        if st:
            f["esp_w"].append(st)
            f["esp"].append(st)
        sent, nbytes, fails = m.sent - f["sent_w"], m.bytes_sent - f["bytes_w"], m.fails - f["fail_w"]
        self.log("FPS", self._fps_line("", f["fmt"], now - f["tw"], sent, nbytes, fails, self._esp_avg(f["esp_w"])))
        f.update(tw=now, sent_w=m.sent, bytes_w=m.bytes_sent, fail_w=m.fails, esp_w=[])
        self.root.after(int(self.FPS_LOG_S * 1000), lambda: self._fps_tick(m))

    def close(self):
        """アプリを閉じる (× ボタン / 終了)。送っている途中なら、その形式の平均 fps をログに残してから"""
        try:
            if self.sender is not None:
                self._fps_summary(self.sender)
        except Exception:  # noqa: BLE001
            pass
        self.root.destroy()

    def _fps_summary(self, m):
        """送信をやめる / 形式を変えるとき: その形式で送っていた間の全体の平均を 1 行"""
        f = getattr(self, "_fps", None)
        if f is None or m is None:
            return
        dt = time.monotonic() - f["t0"]
        if dt >= 3 and m.sent:
            self.log("FPS", self._fps_line("平均 ", f["fmt"], dt, m.sent, m.bytes_sent, m.fails,
                                           self._esp_avg(f["esp"])))
        self._fps = None

    def stop_sending(self):
        if self.sender is not None:
            self._fps_summary(self.sender)
            # 前の送信が本当に終わるまで待つ (返事待ちは最大 3 秒)。残っていると Wi-Fi と有線の
            # 2 つが同時に送ることになり、ESP32 の液晶が両方を待って詰まる
            m, self.sender = self.sender, None
            m.stop(timeout=5)
            if m._thread is not None and m._thread.is_alive():
                self.log("WARN", "前の送信がまだ終わっていません (返事待ち)")
            self.log("INFO", "ESP32 への送信を止めました")

    def _sender_grab(self):
        """送信スレッドから呼ばれる: 送信画面と同じ 240x135 の絵 (形式に合わせて加工済み)"""
        snap = self.live.latest() if self.live is not None else None
        if snap is None or snap.preview is None:
            raise RuntimeError(snap.error if snap is not None and snap.error else "no frame")
        return snap.preview

    def sender_status(self):
        m = self.sender
        if m is None:
            return ("送信停止中", "#888888")
        if m.sent == 0:
            return ("接続中…" if m.errors == 0 else f"送れません ({m.errors})", "#e09000" if m.errors == 0 else "#d32f2f")
        if (getattr(self, "sender_fmt", "") != self.mirror_fmt.get()
                or getattr(self, "sender_ent", "なし") != self._ent_effective()):
            self.start_sending()  # 形式を変えたら送り直す
        return (f"{m.fps:4.1f} fps {m.last_bytes / 1024:.1f}KB", "#2e7d32" if m.errors == 0 else "#e09000")

    def log_threadsafe(self, level: str, msg: str):
        self.root.after(0, self.log, level, msg)

    # ---- 送信先ウィンドウ ---------------------------------------------------
    def refresh_windows(self):
        self.windows = list_game_windows(bool(self.show_other.get()))
        fg = foreground_hwnd(self.windows)
        self.win_list.delete(0, tk.END)
        for w in self.windows:
            self.win_list.insert(tk.END, w.label(w.hwnd == fg))
        t = self.target()
        if t is not None:
            if t.is_game and t.name != self.target_name:  # ゲーム内でキャラクターを変えた (同じウィンドウ) / クライアントを再起動した
                self.log("INFO", f"送信先のキャラクターが {self.target_name} → {t.name} に変わりました (同じウィンドウを撮り続けます)")
                self.target_name = t.name
            self.target_hwnd = t.hwnd
            self.win_list.selection_set(self.windows.index(t))
            if self.live is not None:  # タイトル (キャラクター名・レベル・動画の名前) の変化も OBS に伝える
                self._live_target(t)
        elif self.target_name and self.armed.get():
            self.log("WARN", f"送信先 {self.target_name} のウィンドウが見つかりません。停止します")
            self.disarm()

    def target(self) -> GameWindow | None:
        """送信先のウィンドウ。ウィンドウ (hwnd) で覚えているので、ゲーム内でキャラクターを変えても同じウィンドウ。
        クライアントを再起動して hwnd が変わったときだけ、キャラクター名で探し直す"""
        hwnd = getattr(self, "target_hwnd", None)
        t = next((w for w in self.windows if w.hwnd == hwnd), None) if hwnd else None
        if t is None and self.target_name:
            t = next((w for w in self.windows if w.is_game and w.name == self.target_name), None)
        return t

    def _live_target(self, w: GameWindow) -> None:
        self.live.set_target(w.hwnd, w.name, w.title, w.pid, kind=w.kind, exe=w.exe, cls=w.cls)

    def _target_is_game(self, what: str) -> bool:
        """ゲート・試験は MURex のウィンドウだけ。その他のウィンドウ (VLC など) は映すだけ"""
        t = self.target()
        if t is not None and not t.is_game:
            self.log_threadsafe("WARN", f"送信先がゲーム以外のウィンドウ ({t.name}) なので{what}は使えません (液晶に映すだけです)")
            return False
        return True

    def on_select_window(self, _):
        sel = self.win_list.curselection()
        if not sel:
            return
        w = self.windows[sel[0]]
        if w.hwnd != getattr(self, "target_hwnd", None):
            if self.armed.get():
                self.disarm()
            self.target_name = w.name
            self.target_hwnd = w.hwnd
            if w.is_game:
                self.log("INFO", f"送信先を {w.name} (PID {w.pid}, hwnd {w.hwnd:#010x}) にしました")
            else:
                self.log("INFO", f"送信先をゲーム以外のウィンドウ「{w.title}」({w.exe}, hwnd {w.hwnd:#010x}) にしました。"
                                 "液晶に映すだけで、ゲート・試験・マップ・HP は使いません")
            if self.auto_send.get() and self.sender is None and self.host.get().strip():
                self.root.after(500, self.start_sending)
            if self.live is not None:
                self._live_target(w)
                self.snap = None
                self.draw_map()

    def arm(self, activate: bool = True):
        if not self.target():
            self.log("WARN", "送信先のウィンドウを一覧から選んでください")
            return
        if not self._target_is_game("ゲート (ARM)"):
            return
        self.armed.set(True)
        self._armed = True
        if activate and IS_WINDOWS:  # 送信先の窓 (ミラーで映している窓) を前面にし、選んでおいた試験を始める
            then = self.arm_runs.get()
            self.log("INFO", "ARM のあと: " + {"explore": "マップ探索を始めます", "shuttle": "往復テストを始めます",
                                                "check": "マウス診断をします", "none": "前面に出すだけです"}.get(then, then) +
                     " (「試験」タブの「▶ 開始 (ARM) を押したら」で変えられます)")
            if then in ("shuttle", "explore") and self.shuttle is not None and self.shuttle.running:
                self.stop_shuttle()  # 押し直し: 前の往復テストを止め、新しい基準点でやり直す
            threading.Thread(target=self._activate_and_report, args=(then,), daemon=True).start()
        self.log("GATE", f"ARM: {self.target_name} が前面にあるときだけキー操作を通します")

    def disarm(self):
        was = self.armed.get()
        self.armed.set(False)
        self._armed = False
        if self.shuttle is not None and self.shuttle.running:
            self.shuttle.stop()
        if self.vhid is not None:
            self.vhid.stop()
            try:
                self.vhid.release_all()
            except Exception as e:  # noqa: BLE001
                self.log("ERR", f"キー・ボタンを離せません: {e}")
        if was:
            self.log("GATE", "DISARM: 全キー・ボタンを離して停止しました")

    def gate_state(self) -> tuple[str, str]:
        t = self.target()
        if not self.armed.get():
            return "停止", "#888888"
        if t is None:
            return "送信先なし", "#d32f2f"
        if foreground_hwnd(self.windows) == t.hwnd:
            return "● 許可中", "#2e7d32"
        return "待機 (前面が別)", "#e09000"

    # ---- キー・マウス (仮想 HID) と往復テスト -------------------------------------------
    def _hid_allowed(self) -> bool:
        """ゲート: ARM 中で、送信先のウィンドウが前面のときだけ送る (別のスレッドから呼ばれる)"""
        hwnd = self.target_hwnd
        if not (self._armed and hwnd):
            return False
        return not IS_WINDOWS or _user32.GetForegroundWindow() == hwnd

    def get_hid(self):
        if self.hid_out.get() != "virtual":
            raise RuntimeError("ESP32 の出口はまだつないでいません (デバイス タブで「仮想 HID」を選んでください)")
        if self.vhid is None:
            from akm.virtual_hid import VirtualHid

            self.vhid = VirtualHid.from_config(self.cfg.get("virtual_hid"), allow=self._hid_allowed)
            p = self.vhid.pipe
            self.log("INFO", f"仮想 HID を使います (間隔 {self.vhid.event_delay * 1000:g} ms / 遅れ {p.latency * 1000:g}"
                             f"±{p.jitter * 1000:g} ms / 区切り {p.interval * 1000:g} ms。config.yaml の virtual_hid)")
        return self.vhid

    def user_busy_ms(self) -> float:
        """人がこの PC を操作していれば、あと何 ms で「止まった」とみなすか (0 = 操作していない)"""
        return self.vhid.user.busy_left_ms() if self.vhid is not None else 0.0

    def _activate_and_report(self, then: str = "none"):
        base = None
        t = self.target()
        if then in ("shuttle", "explore") and t is not None:  # 押した瞬間の座標を、前面に出す前に読む (基準点)
            base = self._read_base(t.hwnd)
        if self._activate_once():
            if then in ("shuttle", "explore"):
                self.root.after(0, lambda: self.start_shuttle(base=base, have_base=True, kind=then))
            elif then == "check":
                self.root.after(0, self.start_mouse_check)

    def _activate_once(self) -> bool:
        t = self.target()
        if t is None:
            return False
        why = self.input_blocked_reason(t.pid)
        if why:
            self.log_threadsafe("ERR", why)
            return False
        self.get_hid()
        if self._hid_allowed():
            self.log_threadsafe("INFO", f"{t.name} の窓 (hwnd {t.hwnd:#010x}) はもう前面です")
            return True
        pt = title_bar_point(t.hwnd)
        self.log_threadsafe("INFO", f"{t.name} の窓 (hwnd {t.hwnd:#010x}) を前面にします: " +
                            (f"タイトルバー {pt} をクリック" if pt else "タイトルバーが見えないので Windows の機能で"))
        ok = self.activate_target()
        fg = _user32.GetForegroundWindow()
        self.log_threadsafe("INFO" if ok else "WARN",
                            f"{t.name} が前面になりました" if ok else
                            f"{t.name} を前面にできませんでした (今の前面 hwnd {fg or 0:#010x} '{window_title(fg)}')")
        return ok

    # ---- 送信形式に合わせた欄の有効・無効 ------------------------------------------------
    def _ent_effective(self) -> str:
        """実際に使う圧縮: 白黒・JPEG は tANS を使わない (チェックは他の形式のために覚えておく)"""
        return "なし" if self.mirror_fmt.get() in ("JPEG", "BadCodec 1bit") else self.mirror_ent.get()

    def _sync_fmt_widgets(self):
        if not hasattr(self, "_ent_box"):
            return
        f = self.mirror_fmt.get()
        ent_ok = f not in ("JPEG", "BadCodec 1bit")
        self._ent_box.state(["!disabled"] if ent_ok else ["disabled"])
        self._ent_cap.configure(text={"JPEG": "JPEG は圧縮済みなので使いません",
                                      "BadCodec 1bit": "白黒は小さいので使いません"}.get(f, "BadCodec の 1 枚を包む (fw35〜)"))
        self._ent_cap.state(["!disabled"] if ent_ok else ["disabled"])
        mono = f == "BadCodec 1bit"
        for w in self._mono_widgets:
            w.state(["!disabled"] if mono else ["disabled"])
        self._dither_box.configure(state="readonly" if mono else "disabled")
        self._mono_cap.configure(text="白黒のときだけ使います (fw37〜)" if not mono else "8x8 = BadCodec のブロックにそろえたディザ")
        self._mono_cap.state(["!disabled"] if mono else ["disabled"])

    # ---- 試験の設定を覚えておく (models/gate_ui.json。起動し直しても残す) -----------------------
    def _ui_vars(self) -> dict:
        return {"arm_runs": self.arm_runs, "mirror_fmt": self.mirror_fmt, "mirror_ent": self.mirror_ent,
                "threshold": self.threshold, "dither": self.dither, "show_other": self.show_other, "fps": self.fps, "shuttle_map": self.shuttle_map, "shuttle_tiles": self.shuttle_tiles,
                "shuttle_wait": self.shuttle_wait, "shuttle_axis": self.shuttle_axis, "mouse_mode": self.mouse_mode,
                "explore_radius": self.explore_radius, "explore_step": self.explore_step,
                "explore_minutes": self.explore_minutes}

    def _ui_settings_path(self):
        from pathlib import Path

        return Path(__file__).resolve().parent / "models" / "gate_ui.json"

    def _load_ui_settings(self):
        import json

        try:
            d = json.loads(self._ui_settings_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        if d.get("mirror_fmt") not in FORMATS:  # 古い・なくなった形式 (343 / YCoCg など) は覚えていないことにする
            d.pop("mirror_fmt", None)
        if d.get("mirror_ent") not in ENTROPY_MODES:  # ハフマン (外した) など
            d.pop("mirror_ent", None)
        if str(d.get("dither", "")).startswith("Bayer"):  # 前の版の Bayer 4x4 / 8x8 → 8x8
            d["dither"] = "8x8"
        if d.get("dither") not in DITHERS:
            d.pop("dither", None)
        for k, var in self._ui_vars().items():
            if k in d:
                try:
                    var.set(d[k])
                except (tk.TclError, ValueError):
                    pass
            var.trace_add("write", lambda *_: self._save_ui_settings())

    def _save_ui_settings(self):
        import json

        d = {}
        for k, var in self._ui_vars().items():
            try:
                d[k] = var.get()
            except (tk.TclError, ValueError):  # 入力の途中 (空欄など)
                return
        try:
            p = self._ui_settings_path()
            p.parent.mkdir(exist_ok=True)
            p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    def restart_as_admin(self):
        if not IS_WINDOWS:
            return
        if is_admin():
            self.log("INFO", "もう管理者として動いています")
            return
        if relaunch_as_admin():
            self.root.destroy()
        else:
            self.log("ERR", "管理者として起動し直せませんでした (UAC で「いいえ」を選んだ?)")

    def input_blocked_reason(self, pid: int) -> str:
        """キー・マウスがゲームに届かない理由があれば、その説明 (無ければ空)"""
        if not IS_WINDOWS or is_admin():
            return ""
        if process_elevated(pid) is False:
            return ""
        return ("この仲介アプリは管理者として動いていません。ゲーム (Main.exe) は管理者で動いているので、"
                "Windows がこのアプリからのキー・マウスを捨てます (カーソルも動きません)。"
                "「ファイル」タブの「管理者で起動し直す」を押すか、管理者の PowerShell から起動してください")

    def activate_target(self) -> bool:
        """送信先を前面に出す。まずキー・マウスの出口からタイトルバーをクリックし (人が操作するのと同じ)、
        だめなら Windows の機能で出す (別の仮想デスクトップにあるときはこちら。デスクトップが切り替わる)"""
        hwnd = self.target_hwnd
        if not hwnd or not IS_WINDOWS:
            return False
        from akm.screen import bring_to_front

        pt = title_bar_point(hwnd)
        if pt is not None and self.vhid is not None:
            try:
                b = self.vhid.backend
                hx, hy = self._screen_to_hid(*pt)
                b.move_abs(hx, hy)
                time.sleep(0.05)
                cx, cy = self._cursor()
                if max(abs(cx - pt[0]), abs(cy - pt[1])) > 3:
                    self.log_threadsafe("ERR", f"カーソルが動きません (狙い {pt} → 実際 ({cx}, {cy}))。"
                                               "Windows がこのアプリの入力を受け付けていません")
                    return bring_to_front(hwnd, 1.0)
                b.buttons(1)
                time.sleep(0.06)
                b.buttons(0)
                self.vhid.user.note_sent()
                for _ in range(10):
                    time.sleep(0.05)
                    if _user32.GetForegroundWindow() == hwnd:
                        return True
            except Exception as e:  # noqa: BLE001
                self.log_threadsafe("WARN", f"タイトルバーをクリックできません: {e}")
        return bring_to_front(hwnd, 1.0)

    def _screen_to_hid(self, sx: float, sy: float) -> tuple[int, int]:
        from akm.device import AbsMap
        from akm.screen import primary_screen_size

        amap = AbsMap.for_screen(*primary_screen_size())  # SendInput の絶対座標はプライマリモニタ全体
        if not amap.reachable(sx, sy):
            raise RuntimeError("ゲームの窓がプライマリモニタの外にあります (絶対座標のクリックが届きません)")
        return amap.to_hid(sx, sy)

    def _cursor(self) -> tuple[int, int]:
        pt = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def _hid_run(self, script: str, stopped) -> str:
        """送る。送信先の窓からフォーカスが外れたら止める (InterruptedError("focus lost"))。
        OS と取り合わないように、外れた窓を取り返しには行かない (人が別の窓を使い始めたら、そこで終わり)"""
        if stopped() or not self._armed:
            raise InterruptedError("stopped")
        if not self._hid_allowed():
            raise InterruptedError("focus lost")
        try:
            return self.get_hid().run(script)
        except InterruptedError as e:
            if stopped() or not self._armed:
                raise InterruptedError("stopped") from None
            raise InterruptedError("focus lost" if str(e).startswith("not allowed") else str(e)) from None

    def _game_parts(self, hwnd: int):
        """往復テスト・マウス診断で使う部品: 画面・座標の読み取り・表示範囲"""
        from akm.screen import client_rect

        reader = self._coord_reader()

        def grab():
            return self.live._grab(hwnd, full=True)[0]

        return grab, reader, (lambda: client_rect(hwnd))

    def _coord_reader(self):
        """座標を読む関数 (img) -> 候補の Location のリスト (空 = 読めない)。閉じ括弧まで見えているものだけ
        (マップ名が長いと表示が切れる)。2 通りの読みが違えば両方を候補にし、使う側が前の位置に近い方を選ぶ。
        読む範囲は maplog.coord_roi (無ければ maplog.roi を左へ広げたもの)。
        続けて読めないときは、読んだ範囲の画像を debug/coord_*.png に保存し、OCR の文字をログに出す"""
        from akm.config import PC_DIR
        from akm.maploc import Location, LocationReader, make_ocr, read_xy_candidates, widen_roi

        lcfg = self.cfg.get("maplog") or {}
        reader = LocationReader(lcfg, make_ocr(lcfg.get("ocr", "auto"), lcfg.get("tesseract_cmd")))
        state = {"fails": 0, "saved": 0.0, "roi": lcfg.get("coord_roi") or widen_roi(reader.roi),
                 "scan_at": 0.0, "found": None}

        def scan(img):
            """いつもの範囲で読めないとき: 画面の右下の広い範囲を帯に分けて座標 "(x, y)" を探す。
            2 回続けて同じ帯で見つかったら、そこを読む範囲にする (座標の表示位置が変わったとき)"""
            if time.monotonic() - state["scan_at"] < 3.0:
                return []
            state["scan_at"] = time.monotonic()
            y = 0.62
            while y <= 0.97:
                band = [0.50, y, 0.50, 0.06]
                got = read_xy_candidates(reader, img, band)
                if got:
                    if state["found"] == band:
                        state["roi"], state["found"] = band, None
                        self.log_threadsafe("WARN", f"座標の表示位置が変わったようなので、読む範囲を {band} に変えました "
                                                    "(いつも使うなら config.yaml の maplog.coord_roi に書いてください)")
                    else:
                        state["found"] = band
                    return got
                y += 0.03
            state["found"] = None
            return []

        def read(img):
            got = read_xy_candidates(reader, img, state["roi"])
            if not got and state["fails"] >= 3:
                got = scan(img)
            if got:
                state["fails"] = 0
                return [Location("", x, y) for x, y in got]
            state["fails"] += 1
            if state["fails"] >= 8 and time.monotonic() - state["saved"] > 30:
                state["saved"] = time.monotonic()
                path = PC_DIR / "debug" / f"coord_{time.strftime('%H%M%S')}.png"
                full = path.with_name(path.stem + "_full.jpg")
                try:
                    path.parent.mkdir(exist_ok=True)
                    cv2.imwrite(str(path), reader.last_crop)
                    cv2.imwrite(str(full), img, [cv2.IMWRITE_JPEG_QUALITY, 85])  # 撮った画面全体 (何が写っているか)
                    h, w = img.shape[:2]
                    where = (f"。読んだ範囲の画像: {path} / 画面全体 ({w}x{h}, 明るさ {float(img.mean()):.0f}): {full}")
                except Exception:  # noqa: BLE001
                    where = ""
                self.log_threadsafe("WARN", f"座標を {state['fails']} 回続けて読めません。OCR の文字: "
                                            f"'{reader.last_text}'{where}")
            return []

        read.reader = reader  # 読んだ文字 (reader.last_text) をマップ名にも使う
        return read

    def _begin_on_target(self, what: str):
        """開始ボタンの共通の手順: 送信先を確かめ、ARM し、送信先を前面に出す (別のスレッドで呼ぶ)。
        前面にできたら hwnd を返す。だめなら None"""
        t = self.target()
        if t is None:
            self.log_threadsafe("WARN", "送信先のウィンドウ (例 Thanatos) を一覧から選んでください")
            return None
        if not self._target_is_game(what):
            return None
        if self.live is None:
            self.log_threadsafe("ERR", "画面の撮影が動いていないので、現在地を読めません")
            return None
        why = self.input_blocked_reason(t.pid)
        if why:
            self.log_threadsafe("ERR", why)
            return None
        if not self._armed:
            self.root.after(0, lambda: self.arm(activate=False))
            for _ in range(20):
                if self._armed:
                    break
                time.sleep(0.05)
        self.get_hid()
        self.log_threadsafe("INFO", f"{what}: {t.name} の窓を前面にします")
        if not self._hid_allowed():
            pt = title_bar_point(t.hwnd)
            self.log_threadsafe("INFO", f"タイトルバーをクリックします {pt}" if pt else
                                "タイトルバーが見えないので、Windows の機能で前面に出します")
            if not self.activate_target():
                self.log_threadsafe("WARN", f"{t.name} の窓を前面にできません。止めます")
                return None
        self.log_threadsafe("INFO", f"{t.name} が前面になりました")
        time.sleep(0.3)
        # まずカーソルをゲーム画面の真ん中 (キャラクターの位置) へ動かし、届いているか確かめる (クリックはしない)
        from akm.screen import client_rect

        r = client_rect(t.hwnd)
        cx, cy = r.left + r.width // 2, r.top + r.height // 2
        try:
            hx, hy = self._screen_to_hid(cx, cy)
            self._hid_run(f"a:{hx},{hy};w:100", lambda: False)
        except InterruptedError:
            self.log_threadsafe("WARN", f"{t.name} からフォーカスが外れたので止めます")
            return None
        except Exception as e:  # noqa: BLE001
            self.log_threadsafe("ERR", f"カーソルを動かせません: {e}")
            return None
        ax, ay = self._cursor()
        if max(abs(ax - cx), abs(ay - cy)) > 3:
            self.log_threadsafe("ERR", f"カーソルがゲーム画面の真ん中 ({cx}, {cy}) に来ません (実際 ({ax}, {ay}))。"
                                       "Windows がこのアプリの入力を受け付けていません")
            return None
        self.log_threadsafe("INFO", f"カーソルをゲーム画面の真ん中 ({cx}, {cy}) へ動かしました "
                                    f"(表示範囲 {r.left},{r.top} {r.width}x{r.height})")
        return t.hwnd

    @staticmethod
    def _map_from_text(text: str) -> str | None:
        """OCR の文字 "Noria ( 55 , 137) | ..." からマップ名。知っているマップ名に合うときだけ (読み違えた名前は使わない)"""
        from akm.maploc import KNOWN_MAPS, normalize_map

        for part in (text or "").split("|"):
            head = part.split("(")[0].strip()
            if head:
                name = normalize_map(head)
                if name in KNOWN_MAPS:
                    return name
        return None

    def _read_base(self, hwnd: int):
        """基準点: 押した瞬間のゲーム画面から座標を読み直す (前に読んだ値・前回の基準点は使わない)。
        読み違いを避けるため、何回か読んで同じになった座標を使う。読めなければ None"""
        try:
            if getattr(self, "_base_reader", None) is None:
                self._base_reader = self._coord_reader()
            seen: dict[tuple[int, int], int] = {}
            for _ in range(8):
                for loc in self._base_reader(self.live._grab(hwnd, full=True)[0]):
                    xy = (int(loc.x), int(loc.y))
                    seen[xy] = seen.get(xy, 0) + 1
                    if seen[xy] >= 2:
                        self._base_map = self._map_from_text(self._base_reader.reader.last_text)
                        self.log_threadsafe("INFO", f"基準点 (押したときの座標): {xy}" +
                                            (f"  マップ {self._base_map}" if self._base_map else ""))
                        return xy
                time.sleep(0.1)
        except Exception as e:  # noqa: BLE001
            self.log_threadsafe("WARN", f"基準点の座標を読めません: {e}")
            return None
        self.log_threadsafe("WARN", "基準点の座標を読めませんでした (読みがそろわない)")
        return None

    def start_shuttle(self, base=None, have_base: bool = False, kind: str = "shuttle"):
        """往復テスト (kind="shuttle") / マップ探索 (kind="explore") を始める。押すたびに、その瞬間の座標を
        基準点にする (動いていれば止めてやり直す)"""
        what = "マップ探索" if kind == "explore" else "往復テスト"
        radius, step = int(self.explore_radius.get()), int(self.explore_step.get())
        minutes = float(self.explore_minutes.get())
        if getattr(self, "_busy", False):
            self.log("INFO", "往復テスト / マウス診断を準備しています")
            return
        old = self.shuttle if (self.shuttle is not None and self.shuttle.running) else None
        if old is not None:
            self.stop_shuttle()
        self.arm_runs.set(kind)  # 次に ▶ 開始 (ARM) を押したときも同じものを始める
        if not have_base:
            self._base_map = None
        tiles, wait = int(self.shuttle_tiles.get()), float(self.shuttle_wait.get())
        map_ui, mode = self.shuttle_map.get().strip() or "Crywolf", self.mouse_mode.get()
        axis = AXES.get(self.shuttle_axis.get(), "screen")
        pcfg = self.cfg.get("virtual_hid") or {}

        def go():
            self._busy = True
            try:
                base_xy = base
                if old is not None:  # 前の往復テストが止まるのを待つ
                    for _ in range(100):
                        if not old.running:
                            break
                        time.sleep(0.05)
                t0 = self.target()
                if not have_base and t0 is not None:
                    base_xy = self._read_base(t0.hwnd)
                if base_xy is None:
                    self.log_threadsafe("WARN", f"基準点を読めなかったので、{what}を始めるときの座標を使います")
                map_name = getattr(self, "_base_map", None) or map_ui  # 画面から読めたマップ名を優先
                if map_name != map_ui:
                    self.root.after(0, lambda: self.shuttle_map.set(map_name))
                hwnd = self._begin_on_target(what)
                if hwnd is None:
                    return
                from akm.config import PC_DIR
                from akm.pointer import Pointer
                from akm.shuttle_test import ShuttleTest

                grab, read_loc, _rect = self._game_parts(hwnd)
                try:
                    from akm.helper_probe import make_helper_probe

                    probe = make_helper_probe(grab)
                except Exception as e:  # noqa: BLE001
                    probe = None
                    self.log_threadsafe("WARN", f"MU Helper の判定 (学習済みの AI) を使えません: {e}。Home を押すだけにします")
                holder = {}

                def run(script):
                    return self._hid_run(script, lambda: holder["t"]._stop.is_set())

                def client_to_screen(x, y):
                    r = _rect()
                    return r.left + x, r.top + y

                ptr = Pointer(run, self._screen_to_hid, cursor=self._cursor if IS_WINDOWS else None, mode=mode,
                              settle_ms=int(pcfg.get("click_settle_ms", 100)),
                              hold_ms=int(pcfg.get("click_hold_ms", 80)), log=self.log_threadsafe)
                common = dict(log=self.log_threadsafe, calib_path=PC_DIR / "models" / "home_nav.json",
                              map_name=map_name, helper_probe=probe, alive=self._hid_allowed, base_xy=base_xy)
                if kind == "explore":
                    from akm.map_explore import MapExplorer

                    st = MapExplorer(run, grab, read_loc, client_to_screen, ptr, recorder=self._map_recorder(map_name),
                                     radius=radius, step=step, max_minutes=minutes, read_hp=self._hp_reader(),
                                     **common)
                    detail = f"±{radius} マス / {step} マスおき / 最大 {minutes:g} 分"
                else:
                    st = ShuttleTest(run, grab, read_loc, client_to_screen, ptr, tiles=tiles, wait_s=wait, axis=axis,
                                     **common)
                    detail = f"{tiles} マス / {wait:g} 秒"
                holder["t"] = st
                self.shuttle = st
                self.log_threadsafe("INFO", f"{what}を始めます: {detail} / マップ {map_name} / マウス {mode} "
                                            "(ゲームからフォーカスが外れたら止まります。Pause キーでも止まります)")
                st.start()
            except Exception as e:  # noqa: BLE001
                self.log_threadsafe("ERR", f"{what}を始められません: {e}")
            finally:
                self._busy = False

        threading.Thread(target=go, name="shuttle-start", daemon=True).start()

    def _map_recorder(self, map_name: str):
        """マップ探索の記録先 (data/mu_map.db)。DB は探索のスレッドで開く (sqlite はスレッドをまたげない)"""
        from akm.config import config_path
        from akm.maploc import Location, MapDB

        path = config_path((self.cfg.get("maplog") or {}).get("db", "data/mu_map.db"))
        box = {}

        def db():
            if "db" not in box:
                box["db"] = MapDB(path, interp_max=8)  # 読む間隔の間に歩いた分 (8 マスまで) も歩けるマスにする
            return box["db"]

        class Rec:
            def walk(self, x, y):
                db().record(Location(map_name, int(x), int(y)))

            def blocked(self, x, y):
                db().add_blocked(map_name, x, y)

        return Rec()

    def _hp_reader(self):
        """HP (0..1) を画面から読む関数。config.yaml の hud.hp が無ければ None"""
        h = (self.cfg.get("hud") or {}).get("hp") or {}
        if not h.get("roi"):
            return None
        from akm.vision import bar_level

        def read(img):
            v = bar_level(img, h["roi"], h.get("color", "red"), h.get("direction", "vertical"))
            return v if v > 0 else None  # 赤が全く見えない = 読めない (窓で隠れているなど)

        return read

    def start_mouse_check(self):
        if (self.shuttle is not None and self.shuttle.running) or getattr(self, "_busy", False):
            self.log("INFO", "往復テスト / マウス診断はもう動いています")
            return

        def go():
            self._busy = True
            try:
                hwnd = self._begin_on_target("マウス診断")
                if hwnd is None:
                    return
                from akm.mouse_check import run_mouse_check

                grab, read_loc, rect = self._game_parts(hwnd)
                res = run_mouse_check(lambda s: self._hid_run(s, lambda: False), grab, read_loc, rect,
                                      self._screen_to_hid, self._cursor, self.log_threadsafe, alive=self._hid_allowed)
                ok = [r for r in res if r[2]]
                if ok:
                    self.root.after(0, lambda: self.mouse_mode.set(ok[0][1]))
            except InterruptedError as e:
                self.log_threadsafe("INFO", "[マウス診断] " + ("ゲームからフォーカスが外れたので止めました"
                                                             if str(e) == "focus lost" else f"止めました ({e})"))
            except Exception as e:  # noqa: BLE001
                self.log_threadsafe("ERR", f"[マウス診断] 止まりました: {e}")
            finally:
                self._busy = False

        threading.Thread(target=go, name="mouse-check", daemon=True).start()

    def stop_shuttle(self):
        if self.shuttle is not None and self.shuttle.running:
            self.shuttle.stop()
            if self.vhid is not None:
                self.vhid.stop()
            self.log("INFO", "往復テストを止めます")

    # ---- 定期処理 -----------------------------------------------------------
    def _tick_fast(self):
        """0.2 秒ごと: ゲート・ステータス・HP/MP"""
        if IS_WINDOWS and self.armed.get() and _user32.GetAsyncKeyState(0x13) & 0x8000:  # Pause キー: 非常停止
            self.log("GATE", "Pause キーが押されました: 止めます")
            self.stop_shuttle()
            self.disarm()
        fg = foreground_hwnd(self.windows)
        fg_w = next((w for w in self.windows if w.hwnd == fg), None)
        gate, color = self.gate_state()
        snap = self.snap
        if self.live is not None:
            hp = snap.hp if snap else None
            mp = snap.mp if snap else None
            loc = self.current_loc()
            vals = {
                "loc": (f"{loc[0]} {loc[1]},{loc[2]}" if loc else "?", None),
                "helper": ((f"往復: {self.shuttle.status}", "#1565c0")
                           if self.shuttle is not None and self.shuttle.running else ("(未実装)", "#888888")),
                "mob": ("(未実装)", "#888888"),
                "send": (f"{snap.fps:4.1f} fps" if snap and not snap.error else "-", None),
            }
        else:
            self.hp = min(1.0, max(0.05, self.hp + random.uniform(-0.03, 0.025)))
            self.mp = min(1.0, max(0.05, self.mp + random.uniform(-0.02, 0.02)))
            hp, mp = self.hp, self.mp
            el = time.time() - self.t0
            vals = {
                "loc": (f"Atlans {self.pos[0]},{self.pos[1]}", None),
                "helper": ("ON", "#2e7d32"),
                "mob": ("Vepar Lv45", None),
                "send": (f"{self.frames / el if el > 0 else 0:4.1f} fps", None),
            }
        vals.update({
            "target": (self.target_name or "(未選択)", None),
            "gate": (gate, color),
            "fg": (fg_w.name if fg_w else "MURex 以外", None),
            "esp": self.sender_status(),
            "ble": ("(モック)", "#888888"),
            "wifi": (self.host.get() or "(未設定)", None),
        })
        for k, (text, c) in vals.items():
            self.status_vals[k].configure(text=text, foreground=c or "")
        self.show_esp_stats()
        self._draw_bar(self.hp_bar, "HP", hp, "#c62828")
        self._draw_bar(self.mp_bar, "MP", mp, "#1565c0")
        self.root.after(200, self._tick_fast)

    def _tick_slow(self):
        """1 秒ごと: ウィンドウ一覧・マップ (ダミーのときは位置とログも動かす)"""
        if self.auto_refresh.get():
            self.refresh_windows()
        if self.live is None:
            dx, dy = random.choice([(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (0, 0)])
            self.pos = [min(MAP_SIZE - 1, max(0, self.pos[0] + dx)), min(MAP_SIZE - 1, max(0, self.pos[1] + dy))]
            self.trail.append(tuple(self.pos))
            self.trail = self.trail[-300:]
            self.walked.add(tuple(self.pos))
            if random.random() < 0.15:
                self.log(random.choice(["INFO", "INFO", "WARN"]), random.choice([
                    "ESP32: 窓 'party_invite' を検出 → 断る手順を要求 (モック)",
                    "ESP32: 心拍 OK (rtt 4 ms)",
                    "拾得: Jewel of Bless",
                    "MU Helper: ON を確認",
                    "送信: キーフレーム要求 'R' を受信",
                ]))
        self.draw_map()
        self.root.after(1000, self._tick_slow)

    def _tick_frame(self):
        if self.live is not None:
            self.live.fmt = self.mirror_fmt.get()
            self.live.threshold = int(self.threshold.get())
            self.live.dither = self.dither.get()
            self.live.fps_target = float(self.fps.get())
            self.live.mode = self.capture_mode.get()
            if self.sender is not None:
                self.sender.interval = 1.0 / max(1, int(self.fps.get()))
            self.show_live()
            self.root.after(15, self._tick_frame)  # 60 fps まで表示できるように
            return
        self.render_frame()
        self.frames += 1
        self.root.after(max(30, 1000 // max(1, self.fps.get())), self._tick_frame)

    # ---- マップ ---------------------------------------------------------------
    SPOT_COLORS = {"hunt": "#ff9a3c", "shop": "#4fc3f7", "town": "#9ccc65", "safe": "#9ccc65"}

    def current_loc(self):
        """(マップ名, x, y)。OCR で読めた現在地、読めなければマップの記録の最後の位置"""
        if self.live is None:
            return ("Atlans", self.pos[0], self.pos[1])
        snap = self.snap
        if snap is not None and snap.loc is not None:
            return (snap.loc.map, snap.loc.x, snap.loc.y)
        if self.maps is not None and self.target() is not None:
            r = self.maps.latest()
            if r:
                return (r[0], r[1], r[2])
        return None

    def map_state(self):
        """(マップ名, 現在地 (x, y) か None, 歩いたマス [(x, y, 回数)], 登録地点, 足跡 [(x, y)], 注記)"""
        if self.live is None:
            spots = [(n, x, y, r, c) for n, x, y, r, c in self.spots]
            return ("Atlans", tuple(self.pos), [(x, y, 1) for x, y in self.walked], spots, self.trail, "※ダミー")
        loc = self.current_loc()
        if loc is None:
            note = "送信先を選んでください" if self.target() is None else "現在地を読めていません"
            return ("", None, [], [], [], note)
        name, x, y = loc
        cells, spots = self.maps.map_data(name) if self.maps is not None else ([], [])
        spots = [(n, sx, sy, r, self.SPOT_COLORS.get(k, "#bdbdbd")) for n, sx, sy, r, k in spots]
        trail = [(tx, ty) for m, tx, ty in (self.snap.trail if self.snap else []) if m == name]
        note = "" if (self.snap and self.snap.loc) else "(記録の最後の位置)"
        return (name, (x, y), cells, spots, trail, note)

    def on_map_wheel(self, e):
        up = getattr(e, "delta", 0) > 0 or getattr(e, "num", 0) == 4
        self.map_view = max(24, min(MAP_SIZE, int(self.map_view * (0.8 if up else 1.25))))
        self.draw_map()

    def draw_map(self):
        c = self.map_canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 10 or h < 10:
            return
        name, cur, cells, spots, trail, note = self.map_state()
        self.map_title.configure(text=name or "-")
        if cur is None:
            c.create_text(w / 2, h / 2, text=note, fill="#9e9e9e")
            return
        view = self.map_view  # 今いる場所を中心に view マス四方を表示
        s = min(w, h) / view
        cx, cy = cur
        ox, oy = w / 2 - (cx + 0.5) * s, h / 2 - (cy + 0.5) * s

        def px(x, y):
            return ox + x * s, oy + y * s

        x0, y0 = px(0, 0)
        x1, y1 = px(MAP_SIZE, MAP_SIZE)
        c.create_rectangle(x0, y0, x1, y1, outline="#37474f")  # マップの端
        for i in range(0, MAP_SIZE + 1, 16):  # 16 マスごとの格子
            c.create_line(*px(i, 0), *px(i, MAP_SIZE), fill="#1f2a33")
            c.create_line(*px(0, i), *px(MAP_SIZE, i), fill="#1f2a33")
        top = max((v for _, _, v in cells), default=1)
        for x, y, visits in cells:  # 歩いたマス (何度も通ったほど明るい)
            k = math.log1p(visits) / math.log1p(max(top, 2))
            g = int(90 + 140 * k)
            ax, ay = px(x, y)
            c.create_rectangle(ax, ay, ax + s, ay + s, fill=f"#1e{g:02x}2e", width=0)
        blocked = self.maps.blocked(name) if (self.live is not None and self.maps is not None) else []
        for x, y in blocked:  # マップ探索で行けなかったマス
            ax, ay = px(x, y)
            c.create_rectangle(ax, ay, ax + s, ay + s, fill="#8e2424", width=0)
        if self.show_spots.get():
            for sname, sx, sy, r, col in spots:
                ax, ay = px(sx - r, sy - r)
                bx, by = px(sx + r + 1, sy + r + 1)
                c.create_oval(ax, ay, bx, by, outline=col, width=2)
                c.create_text((ax + bx) / 2, ay - 8, text=sname, fill=col)
        if self.show_trail.get() and len(trail) > 1:
            pts = [v for p in trail for v in px(p[0] + 0.5, p[1] + 0.5)]
            c.create_line(*pts, fill="#fdd835", width=1)
        ax, ay = px(cx + 0.5, cy + 0.5)
        c.create_oval(ax - 5, ay - 5, ax + 5, ay + 5, fill="#e53935", outline="white")
        c.create_text(8, 8, anchor="nw", fill="#9e9e9e",
                      text=f"({cx}, {cy})  歩いたマス {len(cells)}" + (f"  行けない {len(blocked)}" if blocked else "") +
                      f"  表示 {view} マス (ホイールで拡大縮小)  {note}")

    # ---- 送信画面 -------------------------------------------------------------
    def show_live(self):
        """撮影スレッドの最新の画面を出す (新しいものが来たときだけ描き直す)"""
        snap = self.live.latest()
        t = self.target()
        if snap is self.snap:
            return
        old_loc = self.snap.loc if self.snap else None
        self.snap = snap
        c = self.preview
        c.delete("all")
        if t is None or snap.hwnd != t.hwnd:
            c.create_text(SEND_W, SEND_H, text="送信先を選んでください", fill="#9e9e9e", font=self.mono)
            self.preview_info.configure(text="")
            self.preview_src.configure(text="撮影元: -")
            return
        if snap.error or snap.preview is None:
            c.create_text(SEND_W, SEND_H, text=snap.error or "撮影を待っています…", fill="#ffcc00", font=self.mono,
                          width=SEND_W * PREVIEW_ZOOM - 20, justify=tk.CENTER)  # 長い文は折り返す
        else:
            ok, png = cv2.imencode(".png", snap.preview)
            if ok:
                img = tk.PhotoImage(data=base64.b64encode(png.tobytes()).decode("ascii"))
                self.preview_img = img.zoom(PREVIEW_ZOOM)
                c.create_image(0, 0, anchor="nw", image=self.preview_img)
            lines = []
            if snap.loc is not None:
                lines.append((f"MAP {snap.loc.map} {snap.loc.x},{snap.loc.y}", "#00ff00"))
            if snap.hp is not None:
                lines.append((f"HP {snap.hp * 100:.0f}%  MP {(snap.mp or 0) * 100:.0f}%", "#ff8080"))
            for i, (text, col) in enumerate(lines):
                c.create_rectangle(0, 4 + i * 18, 170, 20 + i * 18, fill="black", width=0)
                c.create_text(4, 12 + i * 18, anchor="w", text=text, fill=col, font=self.mono)
            if not self.armed.get():
                c.create_text(SEND_W, SEND_H * PREVIEW_ZOOM - 14, text="ゲート停止中", fill="#ffcc00", font=self.mono)
        fmt = self.mirror_fmt.get()
        cb = color_bits(fmt)
        raw = None if cb is None else (SEND_W * SEND_H // 8 if cb == (1, 1, 1) else SEND_W * SEND_H * sum(cb) // 8)
        size = f"圧縮前 {raw / 1024:.1f} KB" if raw else "JPEG"
        tm = snap.timing or {}
        ms = " / ".join(f"{label} {tm[k]:.0f}" for k, label in (("grab", "撮影"), ("make", "縮小"), ("analyze", "解析"))
                        if k in tm)
        self.preview_info.configure(text=f"{fmt} {size}  {snap.fps:4.1f} fps  ({ms} ms)")
        self.preview_src.configure(
            text=f"撮影元: {t.name} (hwnd {t.hwnd:#010x})  {snap.size[0]}x{snap.size[1]}  方式 {snap.method or '-'}")
        new_loc = snap.loc
        if (old_loc is None) != (new_loc is None) or (new_loc is not None and new_loc != old_loc):
            self.draw_map()

    def render_frame(self):
        """ダミーのゲーム画面 (240x135) を作る。1bit のときはしきい値で白黒にする"""
        t = time.time() - self.t0
        mono = self.mirror_fmt.get() == "BadCodec 1bit"
        thr = self.threshold.get()
        mx, my = 120 + 70 * math.sin(t * 0.7), 62 + 30 * math.cos(t * 0.9)
        hp_y = int(SEND_H - 26 * self.hp)
        rows = []
        for y in range(SEND_H):
            row = []
            for x in range(SEND_W):
                v = ((x * 7 + y * 13) ^ (x * y // 3)) & 47
                r, g, b = 30 + v, 52 + v // 2, 28 + v // 3              # 地面
                if (x - mx) ** 2 + (y - my) ** 2 < 90:                  # モンスター
                    r, g, b = 210, 70, 200
                elif (x - 120) ** 2 + (y - 68) ** 2 < 40:               # 自分
                    r, g, b = 240, 230, 160
                elif y >= SEND_H - 30 and (x - 22) ** 2 + (y - SEND_H + 13) ** 2 < 170:  # HP 球
                    r, g, b = (200, 30, 30) if y >= hp_y else (40, 10, 10)
                elif y >= SEND_H - 30 and (x - 218) ** 2 + (y - SEND_H + 13) ** 2 < 170:  # MP 球
                    r, g, b = 30, 60, 200
                elif y >= SEND_H - 8:                                   # 下のバー
                    r, g, b = 60, 50, 40
                if mono:
                    r = g = b = 255 if (r * 3 + g * 6 + b) // 10 > thr else 0
                row.append(f"#{r:02x}{g:02x}{b:02x}")
            rows.append("{" + " ".join(row) + "}")
        self.frame_img.put(" ".join(rows), to=(0, 0))
        self.preview_img = self.frame_img.zoom(PREVIEW_ZOOM)
        c = self.preview
        c.delete("all")
        c.create_image(0, 0, anchor="nw", image=self.preview_img)
        for i, (text, col) in enumerate((("MAP Atlans %d,%d" % tuple(self.pos), "#00ff00"),
                                         ("HELPER ON", "#00ff00"), ("MOB Vepar Lv45", "#ff78ff"))):
            c.create_rectangle(0, 4 + i * 18, 150, 20 + i * 18, fill="black", width=0)
            c.create_text(4, 12 + i * 18, anchor="w", text=text, fill=col, font=self.mono)
        if not self.armed.get():
            c.create_text(SEND_W, SEND_H * PREVIEW_ZOOM - 14, text="ゲート停止中", fill="#ffcc00", font=self.mono)
        size = SEND_W * SEND_H // 8 if mono else SEND_W * SEND_H * 2
        kb = size * random.uniform(0.12, 0.3) / 1024  # 差分で縮むふり
        self.preview_info.configure(text=f"{self.mirror_fmt.get():<15} 1 枚 約 {kb:4.1f} KB   しきい値 {thr}")
        tw = self.target()
        self.preview_src.configure(text=f"撮影元: {tw.name} (hwnd {tw.hwnd:#010x})" if tw else "撮影元: (送信先を選んでください)")


def main():
    if IS_WINDOWS:
        try:  # 高 DPI で文字がぼやけないように
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    app = GateApp(root)
    root.protocol("WM_DELETE_WINDOW", app.close)  # × で閉じたときも平均 fps をログに残す
    root.mainloop()
    if app.live is not None:
        app.live.stop()
    if app.sender is not None:
        app.sender.stop(timeout=2)


if __name__ == "__main__":
    main()
