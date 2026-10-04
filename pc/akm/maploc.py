"""画面右下の「マップ名 (X, Y)」を読み取り、SQLite に記録する。

    例: "Atlans (32, 68)"  →  Location(map="Atlans", x=32, y=68)

読み取りは OCR で行う。
  * winrt     : Windows 標準の OCR (pip install winrt-Windows.Media.Ocr ほか。追加ソフト不要)
  * tesseract : Tesseract OCR (別途インストール + pip install pytesseract)
  * auto      : winrt → tesseract の順に使えるものを使う

OCR は時々読み違えるので、前回の位置から離れすぎた値は 2 回続けて同じ値が読めるまで採用しない
(テレポートやマップ移動はそれで追従できる)。

データベース (SQLite, 既定 data/mu_map.db):
  maps       マップごとの初めて/最後に見た時刻
  positions  位置の履歴 (変化したときと、一定間隔で記録)
  cells      実際に立ったマス = 歩けるマス (訪問回数つき)。移動の間のマスも補間して記録
  events     マップ移動・ワープなどの出来事
  spots      狩場などの登録地点 (tools/map_spot.py で登録)
ビューア (Java) は同じファイルを読みながら表示する (WAL モードなので記録中でも読める)。
"""
from __future__ import annotations

import difflib
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .vision import roi_px

# Season 6 までの主なマップ名 (OCR の読み違いを直すのに使う。ここに無い名前もそのまま記録する)
KNOWN_MAPS = [
    "Lorencia", "Dungeon", "Devias", "Noria", "Lost Tower", "Exile", "Arena", "Atlans", "Tarkan",
    "Devil Square", "Icarus", "Blood Castle", "Chaos Castle", "Kalima", "Land of Trials", "Aida",
    "Crywolf Fortress", "Kanturu", "Kanturu Relics", "Kanturu Refinery Tower", "Silent Map",
    "Barracks of Balgass", "Balgass Refuge", "Illusion Temple", "Elbeland", "Swamp of Calmness",
    "Raklion", "Raklion Boss", "Santa Village", "Vulcanus", "Duel Arena", "Doppelganger",
    "Imperial Guardian", "Loren Market", "Karutan", "Valley of Loren", "Lorencia Market",
]

_LOC_RE = re.compile(r"([A-Za-z][A-Za-z '\-]*?)\s*[\(\[\{]\s*([0-9OoIl|]{1,3})\s*[,.\s]\s*([0-9OoIl|]{1,3})\s*[\)\]\}]?")
_DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "|": "1"})


@dataclass(frozen=True)
class Location:
    map: str
    x: int
    y: int


def normalize_map(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip(" -'")
    m = difflib.get_close_matches(name.title(), KNOWN_MAPS, n=1, cutoff=0.75)
    return m[0] if m else name


def parse_location(text: str) -> Location | None:
    """OCR の文字列から位置を取り出す。読めなければ None。"""
    text = text.replace("\n", " ").replace("（", "(").replace("）", ")").replace("，", ",")
    m = _LOC_RE.search(text)
    if not m:
        return None
    name = normalize_map(m.group(1))
    try:
        x = int(m.group(2).translate(_DIGIT_FIX))
        y = int(m.group(3).translate(_DIGIT_FIX))
    except ValueError:
        return None
    if not name or not (0 <= x <= 255 and 0 <= y <= 255):
        return None
    return Location(name, x, y)


# ------------------------------------------------------------------- OCR --
def preprocess(crop: np.ndarray, scale: int = 3, method: str = "bright", threshold: int = 130) -> np.ndarray:
    """小さいゲーム文字を拡大し、白地に黒文字にする (OCR が読みやすい形)。

    bright: 明るく色の薄い画素 (白い文字) だけを残す / otsu: 大津の二値化 (背景が単色でない場合向け)
    """
    big = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    if method == "bright":
        hsv = cv2.cvtColor(big, cv2.COLOR_BGR2HSV)
        text = (hsv[:, :, 2] > threshold) & (hsv[:, :, 1] < 100)
    else:
        gray = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
        _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        text = bw > 0
        if text.mean() > 0.5:  # 背景の方が明るい場合は反転
            text = ~text
    out = np.where(text, 0, 255).astype(np.uint8)
    return cv2.copyMakeBorder(out, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)


class WinOcr:
    name = "winrt"

    def __init__(self, lang: str = "en-US"):
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine

        eng = None
        try:
            eng = OcrEngine.try_create_from_language(Language(lang))
        except Exception:
            eng = None
        if eng is None:
            eng = OcrEngine.try_create_from_user_profile_languages()
        if eng is None:
            raise RuntimeError("Windows OCR のエンジンを作れません")
        self.engine = eng

    def __call__(self, img: np.ndarray) -> str:
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.storage.streams import DataWriter

        bgra = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA if img.ndim == 2 else cv2.COLOR_BGR2BGRA)
        h, w = bgra.shape[:2]
        dw = DataWriter()
        dw.write_bytes(bgra.tobytes())
        bmp = SoftwareBitmap.create_copy_from_buffer(dw.detach_buffer(), BitmapPixelFormat.BGRA8, w, h)
        op = self.engine.recognize_async(bmp)
        try:
            res = op.get()  # 同期で待つ (winrt-Windows.Foundation)
        except AttributeError:
            import asyncio

            async def _wait():
                return await op

            res = asyncio.run(_wait())
        return res.text or ""


class TesseractOcr:
    name = "tesseract"

    def __init__(self, cmd: str | None = None):
        import pytesseract

        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
        else:
            default = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
            if default.exists():
                pytesseract.pytesseract.tesseract_cmd = str(default)
        pytesseract.get_tesseract_version()  # 無ければ例外
        self.pt = pytesseract

    def __call__(self, img: np.ndarray) -> str:
        # 許可する文字 (空白と ' は設定の書式を壊すので入れない。空白は区切りとして自然に出る)
        cfg = "--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789(),-"
        return self.pt.image_to_string(img, config=cfg)


def make_ocr(kind: str = "auto", tesseract_cmd: str | None = None):
    errors = []
    for k in (["winrt", "tesseract"] if kind == "auto" else [kind]):
        try:
            return WinOcr() if k == "winrt" else TesseractOcr(tesseract_cmd)
        except Exception as e:
            errors.append(f"{k}: {e}")
    raise RuntimeError(
        "OCR が使えません。次のどちらかを入れてください:\n"
        "  Windows 標準 OCR: pip install winrt-runtime winrt-Windows.Foundation winrt-Windows.Media.Ocr "
        "winrt-Windows.Graphics.Imaging winrt-Windows.Storage.Streams winrt-Windows.Globalization\n"
        "  Tesseract       : https://github.com/UB-Mannheim/tesseract/wiki から入れて pip install pytesseract\n"
        "  詳細: " + " / ".join(errors))


class LocationReader:
    """ゲーム画面から現在地を読む。読み違いを弾くため、位置の飛びを確認する。"""

    def __init__(self, cfg: dict, ocr=None):
        self.roi = cfg.get("roi", [0.86, 0.955, 0.14, 0.045])
        self.scale = int(cfg.get("scale", 3))
        self.threshold = int(cfg.get("threshold", 130))  # 文字とみなす明るさ (0-255)
        self.max_step = int(cfg.get("max_step", 12))  # 1 回の読み取りで動ける最大マス数 (これ以上は確認待ち)
        self.ocr = ocr if ocr is not None else make_ocr(cfg.get("ocr", "auto"), cfg.get("tesseract_cmd"))
        self.current: Location | None = None
        self._pending: Location | None = None
        self.last_text = ""
        self.reads = 0
        self.fails = 0

    def crop(self, img: np.ndarray) -> np.ndarray:
        x0, y0, x1, y1 = roi_px(img.shape, self.roi)
        return img[y0:y1, x0:x1]

    def read_raw(self, img: np.ndarray) -> Location | None:
        """2 通りの前処理で読み、読めた方を使う。"""
        crop = self.crop(img)
        texts = []
        for method in ("bright", "otsu"):
            t = self.ocr(preprocess(crop, self.scale, method, self.threshold)).strip()
            texts.append(t)
            loc = parse_location(t)
            if loc is not None:
                self.last_text = t
                return loc
        self.last_text = " | ".join(texts)
        return None

    def _close(self, a: Location, b: Location) -> bool:
        return a.map == b.map and abs(a.x - b.x) <= self.max_step and abs(a.y - b.y) <= self.max_step

    def accept(self, loc: Location | None) -> Location | None:
        """読み取り結果を採用するか決める。採用したら位置を返す。

        前回の位置の近くなら採用。大きく飛んだ (別マップ・ワープ・読み違い) ときは、
        次の読み取りがその近くなら採用する (読み違いは 2 回続けて同じにはまずならない)。
        """
        self.reads += 1
        if loc is None:
            self.fails += 1
            return None
        ok = (self.current is not None and self._close(loc, self.current)) or \
             (self._pending is not None and self._close(loc, self._pending))
        if ok:
            self.current = loc
            self._pending = None
            return loc
        self._pending = loc
        return None

    def read(self, img: np.ndarray) -> Location | None:
        return self.accept(self.read_raw(img))


# ---------------------------------------------------------------- SQLite --
SCHEMA = """
CREATE TABLE IF NOT EXISTS maps (
    name TEXT PRIMARY KEY, first_seen REAL, last_seen REAL);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, map TEXT NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS positions_ts ON positions(ts);
CREATE TABLE IF NOT EXISTS cells (
    map TEXT NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL,
    visits INTEGER NOT NULL DEFAULT 0, first_seen REAL, last_seen REAL,
    PRIMARY KEY (map, x, y));
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, map TEXT, x INTEGER, y INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS spots (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, map TEXT NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL,
    radius INTEGER NOT NULL DEFAULT 5, kind TEXT NOT NULL DEFAULT 'hunt', note TEXT, created REAL);
"""


def line_cells(x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
    """2 点の間のマス (両端を含む)。歩いて通ったとみなす。"""
    n = max(abs(x1 - x0), abs(y1 - y0))
    if n == 0:
        return [(x0, y0)]
    return [(round(x0 + (x1 - x0) * i / n), round(y0 + (y1 - y0) * i / n)) for i in range(n + 1)]


class MapDB:
    def __init__(self, path: str | Path, heartbeat_s: float = 10.0, interp_max: int = 4):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.execute("PRAGMA journal_mode=WAL")  # 記録中でもビューアが読めるように
        self.db.executescript(SCHEMA)
        from .monsters import SCHEMA as MON_SCHEMA

        self.db.executescript(MON_SCHEMA)  # モンスター一覧と狩場の狙うモンスター
        self.heartbeat_s = heartbeat_s
        self.interp_max = interp_max
        self.last: Location | None = None
        self.last_ts = 0.0

    def event(self, kind: str, loc: Location | None, detail: str = "", ts: float | None = None) -> None:
        ts = ts or time.time()
        self.db.execute("INSERT INTO events(ts, kind, map, x, y, detail) VALUES (?,?,?,?,?,?)",
                        (ts, kind, loc.map if loc else None, loc.x if loc else None, loc.y if loc else None, detail))
        self.db.commit()

    def record(self, loc: Location, ts: float | None = None) -> str | None:
        """位置を記録する。マップ移動やワープがあればその種類を返す。"""
        ts = ts or time.time()
        db = self.db
        kind = None
        prev = self.last
        db.execute("INSERT INTO maps(name, first_seen, last_seen) VALUES (?,?,?) "
                   "ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen", (loc.map, ts, ts))
        if prev is None or loc != prev or ts - self.last_ts >= self.heartbeat_s:
            db.execute("INSERT INTO positions(ts, map, x, y) VALUES (?,?,?,?)", (ts, loc.map, loc.x, loc.y))
            self.last_ts = ts
        if prev is not None and prev.map == loc.map and \
                max(abs(loc.x - prev.x), abs(loc.y - prev.y)) <= self.interp_max:
            cells = line_cells(prev.x, prev.y, loc.x, loc.y)[1:]  # 移動の途中のマスも歩けるマス
        else:
            cells = [(loc.x, loc.y)]
            if prev is not None:
                kind = "map_change" if prev.map != loc.map else "warp"
                db.execute("INSERT INTO events(ts, kind, map, x, y, detail) VALUES (?,?,?,?,?,?)",
                           (ts, kind, loc.map, loc.x, loc.y, f"from {prev.map} ({prev.x},{prev.y})"))
        if prev is None or loc != prev:
            db.executemany(
                "INSERT INTO cells(map, x, y, visits, first_seen, last_seen) VALUES (?,?,?,1,?,?) "
                "ON CONFLICT(map, x, y) DO UPDATE SET visits=visits+1, last_seen=excluded.last_seen",
                [(loc.map, cx, cy, ts, ts) for cx, cy in cells])
        db.commit()
        self.last = loc
        return kind

    def add_spot(self, name: str, loc: Location, radius: int = 5, kind: str = "hunt", note: str = "") -> int:
        cur = self.db.execute("INSERT INTO spots(name, map, x, y, radius, kind, note, created) VALUES (?,?,?,?,?,?,?,?)",
                              (name, loc.map, loc.x, loc.y, radius, kind, note, time.time()))
        self.db.commit()
        return int(cur.lastrowid)

    def latest(self) -> Location | None:
        r = self.db.execute("SELECT map, x, y FROM positions ORDER BY ts DESC LIMIT 1").fetchone()
        return Location(*r) if r else None

    def close(self) -> None:
        self.db.close()
