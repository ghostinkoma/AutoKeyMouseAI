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
        self.fps_target = 5.0
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
        self._setup_ocr()
        self._thread = threading.Thread(target=self._run, name="gate-live", daemon=True)
        self._thread.start()

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
                if self._reader is not None:
                    self._reader.current = None  # 別のキャラクターの現在地を引き継がない
                    self._reader._pending = None
                self._snap = Snapshot(hwnd=hwnd)

    def latest(self) -> Snapshot:
        with self._lock:
            return self._snap

    def stop(self) -> None:
        self._stop.set()

    # ---- 撮影スレッド -------------------------------------------------------------
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
                if IS_WINDOWS and not user32.IsWindow(hwnd):
                    raise RuntimeError("ウィンドウが閉じられました")
                if IS_WINDOWS and user32.IsIconic(hwnd):
                    raise RuntimeError("ウィンドウが最小化されています")
                img, snap.method = self._grab(hwnd)
                snap.size = (img.shape[1], img.shape[0])
                if self._reader is not None and t0 - self._loc_at >= self._loc_every:
                    self._loc_at = t0
                    loc = self._reader.read(img)
                    if loc is not None and (not self._trail or self._trail[-1] != (loc.map, loc.x, loc.y)):
                        self._trail.append((loc.map, loc.x, loc.y))
                        self._trail = self._trail[-500:]
                    with self._lock:
                        prev = self._snap.loc
                    snap.loc = loc or prev
                else:
                    with self._lock:
                        snap.loc = self._snap.loc
                for key in ("hp", "mp"):
                    h = self.hud.get(key)
                    if h and h.get("roi"):
                        from .vision import bar_level

                        setattr(snap, key, bar_level(img, h["roi"], h.get("color", "red" if key == "hp" else "blue"),
                                                     h.get("direction", "vertical")))
                small = compose(img, [])
                snap.preview, _ = to_send_format(small, self.fmt, self.threshold)
                now = time.monotonic()
                fps = 0.8 * fps + 0.2 * (1.0 / max(1e-3, now - last))
                last = now
                snap.fps = fps
            except Exception as e:
                snap.error = str(e)
            snap.trail = list(self._trail)
            with self._lock:
                if hwnd == self.hwnd:
                    self._snap = snap
            wait = 1.0 / max(0.5, self.fps_target) - (time.monotonic() - t0)
            if wait > 0:
                self._stop.wait(wait)


    def _grab(self, hwnd: int) -> tuple[np.ndarray, str]:
        if self.mode == "obs":
            from .obs_windows import ObsWindows

            if self._obs is None:
                self._obs = ObsWindows(self.cfg.get("obs"), log=self.log)
            try:
                src = self._obs.ensure(self.name, self.title)
                img = self._obs.grab(src)
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
        self.screen.hwnd = hwnd  # 選んだウィンドウだけを撮る (タイトルで探し直さない)
        img = self.screen.grab()
        return img, self.screen._method


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
