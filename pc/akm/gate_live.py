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


# 送る形式 (仲介アプリのミラーのタブ)。BadCodec のカラーは R G B のビット数 (565 = RGB565 そのまま・可逆)
FORMATS = ("Index 256", "BadCodec 565", "JPEG", "BadCodec 1bit")
# 白黒 1 ビットのディザ。BadCodec の仕様 (8x8 のブロック) に合わせ、8x8 の組織的ディザ (Bayer) を
# ブロックの区切りにそろえて敷く: 同じ明るさの 8x8 は必ず同じ模様 (= 0〜64 個が白の 65 段階) になり、
# 模様は場所で決まるので動かない所は前のフレームと同じ (変わったブロックだけ送る差分が効く)。
# 誤差拡散 (Floyd-Steinberg) は 1 画素の変化で模様が広く変わってちらつき、差分が効かなくなるので使わない
DITHERS = ("なし", "8x8")
BLOCK = 8  # BadCodec のブロック


def _bayer(n: int) -> np.ndarray:
    m = np.array([[0, 2], [3, 1]])
    while m.shape[0] < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    return m


# 8x8 のしきい値 (0..256 に均等に 64 段)。画面の左上 (= BadCodec のブロックの区切り) から敷く
_BAYER8 = ((_bayer(BLOCK).astype(np.float32) + 0.5) * (256.0 / (BLOCK * BLOCK)))


def to_mono(small: np.ndarray, threshold: int, dither: str = "なし") -> np.ndarray:
    """白黒 (0 / 255 の 1 チャンネル)。threshold: しきい値 (ディザでは明るさの中心。128 で元のまま)"""
    g = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    if dither != "8x8":
        return np.where(g > threshold, 255, 0).astype(np.uint8)
    h, w = g.shape
    t = np.tile(_BAYER8, ((h + BLOCK - 1) // BLOCK, (w + BLOCK - 1) // BLOCK))[:h, :w]
    v = g.astype(np.float32) + (128 - int(threshold))
    return np.where(v > t, 255, 0).astype(np.uint8)


_PREVIEW: dict = {}


def color_bits(fmt: str) -> tuple[int, int, int] | None:
    """'BadCodec 343' → (3, 4, 3)。1bit は (1, 1, 1)。BadCodec 以外は None。古い名前 'BadCodec 16bit' は 565"""
    if not fmt.startswith("BadCodec"):
        return None
    tail = fmt.split()[-1]
    if tail == "1bit":
        return (1, 1, 1)
    if tail == "16bit":
        return (5, 6, 5)
    from .bad16 import parse_color

    return parse_color(tail)


def from565(img565: np.ndarray) -> np.ndarray:
    """RGB565 → BGR888 (液晶と同じく上位ビットを下位に繰り返して 8 ビットに広げる)"""
    r = (img565 >> 11) & 31
    g = (img565 >> 5) & 63
    b = img565 & 31
    out = np.empty(img565.shape + (3,), np.uint8)
    out[..., 0] = ((b << 3) | (b >> 2)).astype(np.uint8)
    out[..., 1] = ((g << 2) | (g >> 4)).astype(np.uint8)
    out[..., 2] = ((r << 3) | (r >> 2)).astype(np.uint8)
    return out


def to_send_format(small: np.ndarray, fmt: str, threshold: int, dither: str = "なし") -> tuple[np.ndarray, int]:
    """送る形式に合わせた見た目 (ESP32 の液晶に出るのと同じ色) と、おおよその 1 枚の大きさ (バイト。差分で縮む前)。"""
    if fmt.startswith("BadCodec 1bit"):
        bw = to_mono(small, threshold, dither)
        return cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR), SEND_W * SEND_H // 8
    if fmt == "Index 256":  # インデックスカラー: パレットの色 (使い回し・作り直しも送るときと同じ)
        from .bad16 import IndexEncoder

        enc = _PREVIEW.get("index")
        if enc is None:
            enc = _PREVIEW["index"] = IndexEncoder()
        return from565(enc.display(small)), SEND_W * SEND_H
    bits = color_bits(fmt)
    if bits is not None:
        from .bad16 import replicate565, to565

        img = to565(small)
        if bits != (5, 6, 5):  # 送らない下位ビットは上位ビットの繰り返しで埋まる (ESP32 の表示と同じ)
            img = replicate565(img, bits)
        return from565(img), SEND_W * SEND_H * sum(bits) // 8
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
        self.dither = "なし"  # 白黒のディザ (DITHERS)
        self.fps_target = 15.0
        self.mode = "obs"          # "obs" / "window"
        self.hwnd = 0
        self.name = ""             # キャラクター名 (表示用)
        self.pid = 0               # ゲームのクライアントのプロセス ID (OBS のソース名に使う)
        self.title = ""            # 今のウィンドウのタイトル (OBS の撮影先に使う)
        self._obs = None
        self._obs_err = ""
        self._obs_wait_since = 0.0  # OBS がゲームをつかむのを待ち始めた時刻
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
    def set_target(self, hwnd: int, name: str = "", title: str = "", pid: int = 0) -> None:
        """送信先のウィンドウ。同じウィンドウでキャラクター名・タイトルが変わったときは、撮影先 (OBS) だけ合わせる"""
        with self._lock:
            self.title = title or self.title
            self.name = name or self.name
            self.pid = pid or self.pid
            if hwnd != self.hwnd:
                self.hwnd = hwnd
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
                snap.preview, _ = to_send_format(small, self.fmt, self.threshold, self.dither)
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
                src = self._obs.ensure(self.pid or self.name, self.title)
                fw, fh = self._full_size
                if full or not fw:  # 原寸 (初回は縦横比を知るためにも)
                    img = self._obs.grab(src)
                    self._full_size = (img.shape[1], img.shape[0])
                else:
                    img = self._obs.grab(src, PREVIEW_GRAB_W, max(8, round(PREVIEW_GRAB_W * fh / fw)), 85)
            except Exception as e:
                from .obs_windows import ObsNotReady

                if isinstance(e, ObsNotReady):
                    # 接続は正常で、OBS がまだゲームをつかんでいない。張り直すとつかみ直しになるので待つだけ
                    now = time.monotonic()
                    if not self._obs_wait_since:
                        self._obs_wait_since = now
                    waited = now - self._obs_wait_since
                    msg = "OBS がゲームの画面をつかむのを待っています"
                    if waited > 10:
                        msg += (f" ({waited:.0f} 秒)。OBS を管理者として実行しているか・ゲームを最小化していないか・"
                                "OBS のソース 'AKM ...' のプロパティでウィンドウが選ばれているか確認してください")
                    if waited > 10 and self._obs_err != "wait":
                        self._obs_err = "wait"
                        self.log("WARN", msg)
                    raise RuntimeError(msg) from e
                self._obs.reset()
                msg = str(e)
                if msg != self._obs_err:  # 同じ失敗を何度もログに出さない
                    self._obs_err = msg
                    self.log("ERR", f"OBS から撮れません: {msg}")
                raise RuntimeError(f"OBS: {msg}") from e
            self._obs_err = ""
            self._obs_wait_since = 0.0
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

    def blocked(self, name: str) -> list:
        """歩いて行けなかったマス [(x, y)] (マップ探索が書く。あとで歩けたマスは除く)"""
        try:
            db = self._conn()
            if db is None:
                return []
            return db.execute("SELECT b.x, b.y FROM blocked b LEFT JOIN cells c ON c.map = b.map AND c.x = b.x "
                              "AND c.y = b.y WHERE b.map = ? AND c.x IS NULL", (name,)).fetchall()
        except sqlite3.Error:  # 前の版の DB (blocked が無い) など
            return []

    def latest(self):
        """記録の最後の現在地 (map, x, y)。OCR が使えないときの代わり。"""
        try:
            db = self._conn()
            return db.execute("SELECT map, x, y FROM positions ORDER BY ts DESC LIMIT 1").fetchone() if db else None
        except sqlite3.Error:
            self._db = None
            return None
