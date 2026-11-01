"""仲介アプリ (gate_app.py) の「本物」の部分: 選んだウィンドウの画面と、マップの記録。

LiveFeed  : 別スレッドで、選んだ MURex のウィンドウを撮り続ける。撮り方は 2 つ:
              obs    = OBS のゲームキャプチャ (akm/obs_windows.py。仮想デスクトップ・重なりに強く、枠も出ない)
              window = ウィンドウから直接 (Windows Graphics Capture → PrintWindow。OBS が要らない)
            ESP32 に送る大きさ (240x135) の画像・現在地 (OCR)・HP/MP を作って、最新の 1 つだけを持つ。
MapSource : マップの記録 (SQLite, 既定 data/mu_map.db。tools/map_logger.py が書く) を読むだけで開く。

どちらも ESP32 には何も送らない。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .mirror import H as SEND_H
from .mirror import W as SEND_W
from .mirror import compose
from .screen import IS_WINDOWS, GameScreen

if IS_WINDOWS:
    from .screen import user32


@dataclass
class Snapshot:
    preview: np.ndarray | None = None   # 送る画面 (SEND_H, SEND_W, 3) BGR。形式に合わせて加工済み
    size: tuple[int, int] = (0, 0)      # 撮った画面の大きさ (幅, 高さ)
    loc: object = None                  # akm.maploc.Location (読めなければ None)
    hp: float | None = None
    mp: float | None = None
    method: str = ""                    # 撮影方式
    hwnd: int = 0
    ts: float = 0.0
    fps: float = 0.0
    error: str = ""
    trail: list = field(default_factory=list)  # このウィンドウで読めた現在地の履歴 [(map, x, y)]
    timing: dict = field(default_factory=dict)  # 1 枚にかかった時間 (ms): grab 撮影 / make 縮小・形式 / analyze 解析


def to_send_format(small: np.ndarray, fmt: str, threshold: int) -> tuple[np.ndarray, int]:
    """送る形式に合わせた見た目と、おおよその 1 枚の大きさ (バイト。差分で縮む前)。"""
    if fmt.startswith("BadCodec 1bit"):
        g = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        bw = np.where(g > threshold, 255, 0).astype(np.uint8)
        return cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR), SEND_W * SEND_H // 8
    if fmt.startswith("BadCodec 16bit"):
        q = small.copy()  # RGB565 にしたときの色
        q[:, :, 0] &= 0xF8
        q[:, :, 1] &= 0xFC
        q[:, :, 2] &= 0xF8
        return q, SEND_W * SEND_H * 2
    ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return (cv2.imdecode(buf, cv2.IMREAD_COLOR) if ok else small), (len(buf) if ok else 0)


PREVIEW_GRAB_W = 480  # OBS から毎回もらう大きさ (横)。送る 240x135 の 2 倍あれば縮小で十分きれい


class LiveFeed:
    def __init__(self, cfg: dict, log=print):
        self.cfg = cfg or {}
        self.log = log
        g = self.cfg.get("game") or {}
        cap = g.get("capture", "auto")
        # OBS は 1 つのゲームしか撮れない (どのウィンドウか選べない) ので、ここではウィンドウから直接撮る
        self.screen = GameScreen("MURex Client", "main.exe", "auto" if cap == "obs" else cap, None)
        self.hud = self.cfg.get("hud") or {}
        self.fmt = "BadCodec 1bit"
        self.threshold = 60
        self.fps_target = 15.0
        self.mode = "obs"          # "obs" / "window"
        self.hwnd = 0
        self.name = ""             # キャラクター名 (OBS のソース名に使う)
        self.title = ""            # 今のウィンドウのタイトル (OBS の撮影先に使う)
        self._obs = None
        self._obs_err = ""
        self._snap = Snapshot()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._reader = None
        self._loc_every = float((self.cfg.get("maplog") or {}).get("interval_s", 0.5))
        self._loc_at = 0.0
        self._trail: list[tuple[str, int, int]] = []
        self._loc = None                 # 解析スレッドの結果
        self._hp = self._mp = None
        self._analyze_ms = 0.0
        self._last_full = None           # ウィンドウから直接撮ったときの原寸 (解析に使う)
        self._last_full_ts = 0.0
        self._full_size = (0, 0)         # OBS の原寸 (縮めて頼むときの縦横比)
        self._setup_ocr()
        self._thread = threading.Thread(target=self._run, name="gate-live", daemon=True)
        self._thread.start()
        self._analyzer = threading.Thread(target=self._analyze_run, name="gate-analyze", daemon=True)
        self._analyzer.start()

    def _setup_ocr(self):
        try:
            from .maploc import LocationReader, make_ocr

            lcfg = self.cfg.get("maplog") or {}
            self._reader = LocationReader(lcfg, make_ocr(lcfg.get("ocr", "auto"), lcfg.get("tesseract_cmd")))
        except Exception as e:  # OCR が無くても画面は映す
            self._reader = None
            self.log("WARN", f"現在地の読み取り (OCR) は使えません: {e}")

    # ---- 操作 (UI のスレッドから) -------------------------------------------------
    def set_target(self, hwnd: int, name: str = "", title: str = "") -> None:
        with self._lock:
            self.title = title or self.title
            if hwnd != self.hwnd or (name and name != self.name):
                self.hwnd = hwnd
                self.name = name
                self._trail = []
                self._loc = None
                self._hp = self._mp = None
                self._last_full = None
                self._full_size = (0, 0)
                if self._reader is not None:
                    self._reader.current = None  # 別のキャラクターの現在地を引き継がない
                    self._reader._pending = None
                self._snap = Snapshot(hwnd=hwnd)

    def latest(self) -> Snapshot:
        with self._lock:
            return self._snap

    def stop(self) -> None:
        self._stop.set()

    # ---- 撮影スレッド (速さ優先: 撮る → 240x135 に縮める → 送る形式にする だけ) ----------------
    def _run(self):
        last = time.monotonic()
        fps = 0.0
        while not self._stop.is_set():
            t0 = time.monotonic()
            hwnd = self.hwnd
            if not hwnd:
                time.sleep(0.2)
                continue
            snap = Snapshot(hwnd=hwnd, ts=time.time())
            try:
                self._check_window(hwnd)
                img, snap.method = self._grab(hwnd, full=False)
                t1 = time.monotonic()
                small = compose(img, [])
                snap.preview, _ = to_send_format(small, self.fmt, self.threshold)
                t2 = time.monotonic()
                snap.size = self._full_size if self.mode == "obs" and self._full_size[0] else (img.shape[1], img.shape[0])
                snap.timing = {"grab": (t1 - t0) * 1000, "make": (t2 - t1) * 1000}
                now = time.monotonic()
                fps = 0.8 * fps + 0.2 * (1.0 / max(1e-3, now - last))
                last = now
                snap.fps = fps
            except Exception as e:
                snap.error = str(e)
            with self._lock:
                snap.loc, snap.hp, snap.mp = self._loc, self._hp, self._mp
                snap.trail = list(self._trail)
                snap.timing["analyze"] = self._analyze_ms
                if hwnd == self.hwnd:
                    self._snap = snap
            wait = 1.0 / max(0.5, self.fps_target) - (time.monotonic() - t0)
            if wait > 0:
                self._stop.wait(wait)

    # ---- 解析スレッド (遅くてよい: 原寸で現在地の OCR と HP/MP。撮影を待たせない) ---------------
    def _analyze_run(self):
        done_ts = 0.0
        while not self._stop.is_set():
            self._stop.wait(self._loc_every)
            hwnd = self.hwnd
            if not hwnd:
                continue
            try:
                t0 = time.monotonic()
                if self.mode == "obs":  # OBS からは小さく受け取っているので、原寸をもう 1 枚もらう
                    self._check_window(hwnd)
                    img, _ = self._grab(hwnd, full=True)
                else:  # ウィンドウから直接: 撮影スレッドが撮った原寸をそのまま使う
                    with self._lock:
                        img, ts = self._last_full, self._last_full_ts
                    if img is None or ts == done_ts:
                        continue
                    done_ts = ts
                loc = self._reader.read(img) if self._reader is not None else None
                hp = mp = None
                from .vision import bar_level

                for key in ("hp", "mp"):
                    h = self.hud.get(key)
                    if h and h.get("roi"):
                        v = bar_level(img, h["roi"], h.get("color", "red" if key == "hp" else "blue"),
                                      h.get("direction", "vertical"))
                        hp, mp = (v, mp) if key == "hp" else (hp, v)
                with self._lock:
                    if hwnd != self.hwnd:
                        continue  # 解析中に送信先が変わった
                    if loc is not None:
                        self._loc = loc
                        if not self._trail or self._trail[-1] != (loc.map, loc.x, loc.y):
                            self._trail.append((loc.map, loc.x, loc.y))
                            self._trail = self._trail[-500:]
                    self._hp, self._mp = hp, mp
                    self._analyze_ms = (time.monotonic() - t0) * 1000
            except Exception:
                pass  # 撮れないときの理由は撮影スレッドが画面に出す

    def _check_window(self, hwnd: int) -> None:
        if IS_WINDOWS and not user32.IsWindow(hwnd):
            raise RuntimeError("ウィンドウが閉じられました")
        if IS_WINDOWS and user32.IsIconic(hwnd):
            raise RuntimeError("ウィンドウが最小化されています")

    def _grab(self, hwnd: int, full: bool) -> tuple[np.ndarray, str]:
        """full=False なら小さくてよい (OBS には縮めて送ってもらう。転送と JPEG の展開が 1/8 になる)"""
        if self.mode == "obs":
            from .obs_windows import ObsWindows

            if self._obs is None:
                self._obs = ObsWindows(self.cfg.get("obs"), log=self.log)
            try:
                src = self._obs.ensure(self.name, self.title)
                fw, fh = self._full_size
                if full or not fw:  # 原寸 (初回は縦横比を知るためにも)
                    img = self._obs.grab(src)
                    self._full_size = (img.shape[1], img.shape[0])
                else:
                    img = self._obs.grab(src, PREVIEW_GRAB_W, max(8, round(PREVIEW_GRAB_W * fh / fw)), 85)
            except Exception as e:
                self._obs.reset()
                msg = str(e)
                if msg != self._obs_err:  # 同じ失敗を何度もログに出さない
                    self._obs_err = msg
                    self.log("ERR", f"OBS から撮れません: {msg}")
                raise RuntimeError(f"OBS: {msg}") from e
            self._obs_err = ""
            if float(img.max()) < 8:
                raise RuntimeError("OBS の画像が真っ黒です (ゲームに入れていない。OBS を管理者として実行していますか)")
            return img, f"obs ({src})"
        # 選んだウィンドウだけを撮る (タイトルで探し直さない)。画面から撮る方式には戻さない:
        # ほかのウィンドウ (このアプリなど) が重なっていると、それが写ってしまうため
        from .screen import capture_window, client_rect

        self.screen.hwnd = hwnd
        self.screen.rect = client_rect(hwnd)
        img = self.screen._grab_wgc(hwnd) if IS_WINDOWS else None
        method = "wgc"
        if img is None:
            img = capture_window(hwnd)
            method = "printwindow"
            if img is None or float(img.max()) <= 8:
                raise RuntimeError("ウィンドウから撮れません (真っ黒)。Main.exe は管理者で動いているので、"
                                   "このアプリも管理者の PowerShell から起動するか、撮影方式を OBS にしてください")
        with self._lock:  # 解析スレッドが原寸を使う
            self._last_full, self._last_full_ts = img, time.monotonic()
        return img, method

class MapSource:
    """マップの記録を読むだけ (書くのは tools/map_logger.py)。ファイルが無ければ何も出さない。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._db: sqlite3.Connection | None = None
        self._cache: dict[str, tuple[float, list, list]] = {}

    def _conn(self) -> sqlite3.Connection | None:
        if self._db is None and self.path.exists():
            self._db = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
        return self._db

    def map_data(self, name: str, max_age_s: float = 5.0) -> tuple[list, list]:
        """(歩いたマス [(x, y, visits)], 登録地点 [(name, x, y, radius, kind)])。5 秒ごとに読み直す。"""
        hit = self._cache.get(name)
        if hit and time.monotonic() - hit[0] < max_age_s:
            return hit[1], hit[2]
        cells, spots = [], []
        try:
            db = self._conn()
            if db is not None:
                cells = db.execute("SELECT x, y, visits FROM cells WHERE map = ?", (name,)).fetchall()
                spots = db.execute("SELECT name, x, y, radius, kind FROM spots WHERE map = ?", (name,)).fetchall()
        except sqlite3.Error:
            self._db = None  # 開き直す
        self._cache[name] = (time.monotonic(), cells, spots)
        return cells, spots

    def latest(self):
        """記録の最後の現在地 (map, x, y)。OCR が使えないときの代わり。"""
        try:
            db = self._conn()
            return db.execute("SELECT map, x, y FROM positions ORDER BY ts DESC LIMIT 1").fetchone() if db else None
        except sqlite3.Error:
            self._db = None
            return None
