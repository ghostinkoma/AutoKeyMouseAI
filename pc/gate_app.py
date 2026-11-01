"""AutoKeyMouse Gate - Windows 側の仲介アプリ (GUI モック)。

    python gate_app.py

画面の配置を確かめるためのモック。ESP32 には何も送らない (ボタンは押すとログに出るだけ)。
  * 上   : リボン (Excel のようなメニュータブ。タブごとにグループ分けしたボタン)
  * 中央 : 左 = 取得マップ / 右 = ESP32 へ送る画面 (240x135。BadCodec に渡す大きさ)
  * 下   : ステータス (送信先・ゲート・接続・現在地など) と HP / MP
  * 最下 : ログ

Windows では、開いている MURex のウィンドウ (クラス名 "MURex Client") を実際に一覧にする
(1 秒ごとに更新。タイトルからキャラクター名を取り出し、名前で送信先を覚える)。
それ以外の OS ではダミーの 3 窓を出す。マップ・送信画面・HP/MP・ログはダミー。
標準ライブラリ (tkinter) だけで動く。
"""
from __future__ import annotations

import math
import random
import re
import sys
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import scrolledtext, ttk

IS_WINDOWS = sys.platform == "win32"
APP_TITLE = "AutoKeyMouse Gate (モック)"
GAME_CLASS = "MURex Client"
SEND_W, SEND_H = 240, 135      # ESP32 へ送る画面 (BadCodec に渡す大きさ)
PREVIEW_ZOOM = 2               # 送信画面の表示倍率
MAP_SIZE = 256                 # MU のマップは 256x256 マス

# "MURex Client || Tiffany || Lv: 400 || Rs: 50 || RC: 71 || GC: 0 || AC: 2,235"
TITLE_RE = re.compile(
    r"\|\|\s*(?P<name>[^|]+?)\s*\|\|\s*Lv:\s*(?P<lv>\d+)\s*\|\|\s*Rs:\s*(?P<rs>\d+)\s*\|\|\s*RC:\s*(?P<rc>[\d,]+)")


# ---------------------------------------------------------------------------
# MURex のウィンドウの一覧
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

    @classmethod
    def parse(cls, hwnd: int, pid: int, title: str) -> "GameWindow":
        m = TITLE_RE.search(title)
        if not m:  # ログイン前などで名前がまだ出ていない
            return cls(hwnd, pid, title, f"(名前なし) {hwnd:#x}")
        return cls(hwnd, pid, title, m["name"], m["lv"], m["rs"], m["rc"])

    def label(self, foreground: bool) -> str:
        fg = "  ← 前面" if foreground else ""
        return f"{self.name:<12} Lv{self.level:>4}  Rs{self.resets:>3}  RC{self.rc:>4}   PID {self.pid}{fg}"


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


def list_game_windows() -> list[GameWindow]:
    if not IS_WINDOWS:
        return list(DUMMY_WINDOWS)
    found: list[GameWindow] = []

    @_ENUMPROC
    def cb(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, buf, 256)
        if buf.value != GAME_CLASS:
            return True
        title = ctypes.create_unicode_buffer(512)
        _user32.GetWindowTextW(hwnd, title, 512)
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append(GameWindow.parse(int(hwnd), int(pid.value), title.value))
        return True

    _user32.EnumWindows(cb, 0)
    return found


def foreground_hwnd(windows: list[GameWindow]) -> int | None:
    if IS_WINDOWS:
        return int(_user32.GetForegroundWindow() or 0)
    return windows[0].hwnd if windows else None  # ダミー: 先頭が前面


# ---------------------------------------------------------------------------
# リボン (Excel のようなタブ + グループ)
# ---------------------------------------------------------------------------
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
        self.armed = tk.BooleanVar(value=False)
        self.bring_front = tk.BooleanVar(value=False)
        self.auto_refresh = tk.BooleanVar(value=True)
        self.mirror_fmt = tk.StringVar(value="BadCodec 1bit")
        self.threshold = tk.IntVar(value=60)
        self.fps = tk.IntVar(value=5)
        self.com = tk.StringVar(value="COM18")
        self.host = tk.StringVar(value="192.168.1.43")
        self.auto_scroll = tk.BooleanVar(value=True)
        self.show_trail = tk.BooleanVar(value=True)
        self.show_spots = tk.BooleanVar(value=True)

        # ダミーの状態
        self.hp, self.mp = 0.82, 0.64
        self.pos = [142, 118]
        self.trail: list[tuple[int, int]] = [tuple(self.pos)]
        self.walked: set[tuple[int, int]] = {tuple(self.pos)}
        self.spots = [("Atlans 狩場1", 150, 110, 8, "#ff9a3c"), ("薬屋", 120, 140, 4, "#4fc3f7"),
                      ("安全地帯", 128, 128, 6, "#9ccc65")]
        self.t0 = time.time()
        self.frames = 0

        self._build_ribbon()
        self._build_log()        # 下から順に詰める (ログ → ステータス → 中央)
        self._build_status()
        self._build_center()

        self.log("INFO", "モックを起動しました。ESP32 には何も送りません")
        self.log("INFO", "MURex のウィンドウを監視します (クラス名 '%s')" % GAME_CLASS
                 if IS_WINDOWS else "Windows 以外なのでダミーのウィンドウを表示します")
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
        rb.button(g, "終了", command=self.root.destroy)

        # ホーム: 送信先の選択とゲート
        t = rb.tab("ホーム")
        g = rb.group(t, "送信先ウィンドウ (MURex)")
        self.win_list = tk.Listbox(g, height=4, width=62, exportselection=False, font=self.mono,
                                   activestyle="none", selectbackground="#2b7de9")
        self.win_list.grid(row=0, column=0, rowspan=2, sticky="nsew")
        self.win_list.bind("<<ListboxSelect>>", self.on_select_window)
        side = ttk.Frame(g)
        side.grid(row=0, column=1, rowspan=2, sticky="ns", padx=(4, 0))
        ttk.Button(side, text="更新", command=self.refresh_windows).pack(fill=tk.X)
        ttk.Checkbutton(side, text="自動 (1秒)", variable=self.auto_refresh).pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(side, text="前面に出して\n実行する", variable=self.bring_front).pack(anchor="w")

        g = rb.group(t, "ゲート")
        rb.button(g, "▶ 開始\n(ARM)", big=True, command=self.arm)
        rb.button(g, "■ 停止\n(全キー解除)", big=True, command=self.disarm)

        g = rb.group(t, "動作")
        rb.button(g, "ボット開始", row=0, column=0)
        rb.button(g, "ボット停止", row=1, column=0)
        rb.button(g, "MU Helper\n切り替え", row=0, column=1, rowspan=2)
        rb.button(g, "帰還", row=0, column=2, rowspan=2)

        # ミラー: ESP32 へ送る画面
        t = rb.tab("ミラー")
        g = rb.group(t, "形式")
        for i, f in enumerate(("BadCodec 1bit", "BadCodec 16bit", "JPEG")):
            ttk.Radiobutton(g, text=f, value=f, variable=self.mirror_fmt,
                            command=lambda f=f: self.log("INFO", f"送信形式: {f}")).grid(row=i, column=0, sticky="w")
        g = rb.group(t, "2値化")
        ttk.Label(g, text="しきい値").grid(row=0, column=0, sticky="w")
        ttk.Scale(g, from_=0, to=255, variable=self.threshold, length=140).grid(row=1, column=0)
        ttk.Label(g, textvariable=self.threshold).grid(row=1, column=1)
        g = rb.group(t, "送信")
        ttk.Label(g, text="fps").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(g, from_=1, to=30, textvariable=self.fps, width=5).grid(row=0, column=1)
        rb.button(g, "送信開始", row=1, column=0)
        rb.button(g, "送信停止", row=1, column=1)
        g = rb.group(t, "切り出し")
        rb.button(g, "画面全体")
        rb.button(g, "範囲を\n指定...")

        # デバイス: ESP32
        t = rb.tab("デバイス")
        g = rb.group(t, "シリアル")
        ttk.Label(g, text="ポート").grid(row=0, column=0, sticky="w")
        ttk.Combobox(g, textvariable=self.com, values=["COM18", "COM14"], width=8).grid(row=0, column=1)
        rb.button(g, "接続", row=1, column=0)
        rb.button(g, "切断", row=1, column=1)
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
        pw.add(left, weight=3)

        right = ttk.LabelFrame(pw, text=f" 送信画面 {SEND_W}×{SEND_H} (BadCodec へ) ", padding=4)
        self.preview = tk.Canvas(right, width=SEND_W * PREVIEW_ZOOM, height=SEND_H * PREVIEW_ZOOM,
                                 bg="black", highlightthickness=1, highlightbackground="#888")
        self.preview.pack(anchor="n")
        self.preview_info = ttk.Label(right, text="", font=self.mono)
        self.preview_info.pack(anchor="w", pady=(4, 0))
        self.preview_src = ttk.Label(right, text="", foreground="#555")
        self.preview_src.pack(anchor="w")
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

    def _draw_bar(self, c: tk.Canvas, label: str, ratio: float, color: str):
        c.delete("all")
        w, h = int(c["width"]), int(c["height"])
        c.create_rectangle(0, 0, int(w * ratio), h, fill=color, width=0)
        c.create_text(6, h // 2, text=f"{label} {ratio * 100:3.0f}%", anchor="w", fill="white",
                      font=(tkfont.nametofont("TkDefaultFont").actual("family"), 9, "bold"))

    # ---- ログ ---------------------------------------------------------------
    def _build_log(self):
        box = ttk.LabelFrame(self.root, text=" ログ ", padding=4)
        box.pack(side=tk.BOTTOM, fill=tk.X, padx=6, pady=6)
        bar = ttk.Frame(box)
        bar.pack(fill=tk.X)
        ttk.Button(bar, text="クリア", command=lambda: self.log_text.delete("1.0", tk.END)).pack(side=tk.LEFT)
        ttk.Checkbutton(bar, text="自動スクロール", variable=self.auto_scroll).pack(side=tk.LEFT, padx=8)
        self.log_text = scrolledtext.ScrolledText(box, height=9, font=self.mono, bg="#1e1e1e", fg="#d4d4d4",
                                                  insertbackground="white")
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        for tag, color in (("INFO", "#9cdcfe"), ("WARN", "#dcdcaa"), ("ERR", "#f48771"), ("GATE", "#b5cea8"),
                           ("TIME", "#808080")):
            self.log_text.tag_configure(tag, foreground=color)

    def log(self, level: str, msg: str):
        self.log_text.insert(tk.END, time.strftime("%H:%M:%S "), "TIME")
        self.log_text.insert(tk.END, f"[{level:<4}] ", level)
        self.log_text.insert(tk.END, msg + "\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > 2000:
            self.log_text.delete("1.0", "200.0")
        if self.auto_scroll.get():
            self.log_text.see(tk.END)

    def action(self, name: str):
        self.log("INFO", f"(モック) 「{name.replace(chr(10), '')}」が押されました")

    # ---- 送信先ウィンドウ ---------------------------------------------------
    def refresh_windows(self):
        self.windows = list_game_windows()
        fg = foreground_hwnd(self.windows)
        self.win_list.delete(0, tk.END)
        sel = None
        for i, w in enumerate(self.windows):
            self.win_list.insert(tk.END, w.label(w.hwnd == fg))
            if w.name == self.target_name:
                sel = i
        if sel is not None:
            self.win_list.selection_set(sel)
        elif self.target_name and self.armed.get():
            self.log("WARN", f"送信先 {self.target_name} のウィンドウが見つかりません。停止します")
            self.disarm()

    def target(self) -> GameWindow | None:
        return next((w for w in self.windows if w.name == self.target_name), None)

    def on_select_window(self, _):
        sel = self.win_list.curselection()
        if not sel:
            return
        w = self.windows[sel[0]]
        if w.name != self.target_name:
            if self.armed.get():
                self.disarm()
            self.target_name = w.name
            self.log("INFO", f"送信先を {w.name} (PID {w.pid}, hwnd {w.hwnd:#010x}) にしました")

    def arm(self):
        if not self.target():
            self.log("WARN", "送信先のウィンドウを一覧から選んでください")
            return
        self.armed.set(True)
        self.log("GATE", f"ARM: {self.target_name} が前面にあるときだけキー操作を通します")

    def disarm(self):
        was = self.armed.get()
        self.armed.set(False)
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

    # ---- 定期処理 -----------------------------------------------------------
    def _tick_fast(self):
        """0.2 秒ごと: ゲート・ステータス・HP/MP"""
        t = self.target()
        fg = foreground_hwnd(self.windows)
        fg_w = next((w for w in self.windows if w.hwnd == fg), None)
        gate, color = self.gate_state()
        self.hp = min(1.0, max(0.05, self.hp + random.uniform(-0.03, 0.025)))
        self.mp = min(1.0, max(0.05, self.mp + random.uniform(-0.02, 0.02)))
        el = time.time() - self.t0
        fps = self.frames / el if el > 0 else 0
        vals = {
            "target": (self.target_name or "(未選択)", None),
            "gate": (gate, color),
            "fg": (fg_w.name if fg_w else "MURex 以外", None),
            "esp": (f"{self.com.get()} (モック)", None),
            "ble": ("接続中", "#2e7d32"),
            "wifi": (self.host.get(), None),
            "loc": (f"Atlans {self.pos[0]},{self.pos[1]}", None),
            "helper": ("ON", "#2e7d32"),
            "mob": ("Vepar Lv45", None),
            "send": (f"{fps:4.1f} fps", None),
        }
        for k, (text, c) in vals.items():
            self.status_vals[k].configure(text=text, foreground=c or "")
        self._draw_bar(self.hp_bar, "HP", self.hp, "#c62828")
        self._draw_bar(self.mp_bar, "MP", self.mp, "#1565c0")
        if self.armed.get() and t is not None and self.hp < 0.3 and random.random() < 0.05:
            self.log("WARN", f"HP {self.hp * 100:.0f}% → ポーション (Q) を要求 (モック)")
        self.root.after(200, self._tick_fast)

    def _tick_slow(self):
        """1 秒ごと: ウィンドウ一覧・マップ・ダミーのログ"""
        if self.auto_refresh.get():
            self.refresh_windows()
        dx, dy = random.choice([(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (0, 0)])
        self.pos = [min(MAP_SIZE - 1, max(0, self.pos[0] + dx)), min(MAP_SIZE - 1, max(0, self.pos[1] + dy))]
        self.trail.append(tuple(self.pos))
        self.trail = self.trail[-300:]
        self.walked.add(tuple(self.pos))
        self.draw_map()
        if random.random() < 0.15:
            self.log(random.choice(["INFO", "INFO", "WARN"]), random.choice([
                "ESP32: 窓 'party_invite' を検出 → 断る手順を要求 (モック)",
                "ESP32: 心拍 OK (rtt 4 ms)",
                "拾得: Jewel of Bless",
                "MU Helper: ON を確認",
                "送信: キーフレーム要求 'R' を受信",
            ]))
        self.root.after(1000, self._tick_slow)

    def _tick_frame(self):
        self.render_frame()
        self.frames += 1
        self.root.after(max(30, 1000 // max(1, self.fps.get())), self._tick_frame)

    # ---- 描画 ---------------------------------------------------------------
    def draw_map(self):
        c = self.map_canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 10 or h < 10:
            return
        # 今いる場所を中心に 96 マス四方を表示
        view = 96
        s = min(w, h) / view
        cx, cy = self.pos
        ox, oy = w / 2 - cx * s, h / 2 - cy * s

        def px(x, y):
            return ox + x * s, oy + y * s

        for i in range(0, MAP_SIZE + 1, 16):  # 16 マスごとの格子
            x0, y0 = px(i, 0)
            x1, y1 = px(i, MAP_SIZE)
            c.create_line(x0, y0, x1, y1, fill="#1f2a33")
            x0, y0 = px(0, i)
            x1, y1 = px(MAP_SIZE, i)
            c.create_line(x0, y0, x1, y1, fill="#1f2a33")
        for (x, y) in self.walked:  # 歩いたマス
            x0, y0 = px(x, y)
            c.create_rectangle(x0, y0, x0 + s, y0 + s, fill="#2e7d32", width=0)
        if self.show_spots.get():
            for name, x, y, r, col in self.spots:
                x0, y0 = px(x - r, y - r)
                x1, y1 = px(x + r, y + r)
                c.create_oval(x0, y0, x1, y1, outline=col, width=2)
                c.create_text((x0 + x1) / 2, y0 - 8, text=name, fill=col)
        if self.show_trail.get() and len(self.trail) > 1:
            pts = [v for p in self.trail for v in px(p[0] + 0.5, p[1] + 0.5)]
            c.create_line(*pts, fill="#fdd835", width=1)
        x0, y0 = px(cx + 0.5, cy + 0.5)
        c.create_oval(x0 - 5, y0 - 5, x0 + 5, y0 + 5, fill="#e53935", outline="white")
        c.create_text(8, 8, anchor="nw", fill="#9e9e9e",
                      text=f"({cx}, {cy})  歩いたマス {len(self.walked)}  ※ダミー")

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
    GateApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
