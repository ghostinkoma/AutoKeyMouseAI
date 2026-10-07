"""マップガイド (https://wiki.rexmu.online/map-guide) のマップ / モンスター / レベルを DB に入れ、
ゲーム画面で認識したモンスター名をガイドと照らし合わせて「出現エリア」(10 マス一帯) を登録する。

表 (mu_map.db):
  guide_maps      マップ (ガイドに載っているもの)
  guide_monsters  マップごとのモンスターとレベル
  monster_areas   出現エリア: マップの 10 マス四方の区画 (ax, ay は 10 の倍数) ごとに、そこで見たモンスター

登録の条件: 文字認識の読み (辞書で直す前の生の読み) がガイドのモンスター名と同じか、似ている度合いが 85% 以上。
  同じマップに載っているモンスターを優先して照らし合わせる (載っていないマップで見たときは in_guide_map = 0)。
区画は「最初に見た場所」ではなく 10 マスの格子 (0-9, 10-19, …) なので、ビューアで四角く塗り分けられる。
"""
from __future__ import annotations

import difflib
import re
import sqlite3
import time
from dataclasses import dataclass

GUIDE_URL = "https://wiki.rexmu.online/map-guide"

SCHEMA = """
CREATE TABLE IF NOT EXISTS guide_maps (
    name TEXT PRIMARY KEY, source TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS guide_monsters (
    map TEXT NOT NULL, monster TEXT NOT NULL, level INTEGER, source TEXT, updated REAL,
    PRIMARY KEY (map, monster));
CREATE TABLE IF NOT EXISTS monster_areas (
    map TEXT NOT NULL, ax INTEGER NOT NULL, ay INTEGER NOT NULL, monster TEXT NOT NULL, level INTEGER,
    reads INTEGER NOT NULL DEFAULT 0, first_seen REAL, last_seen REAL, best_ratio REAL, in_guide_map INTEGER,
    PRIMARY KEY (map, ax, ay, monster));
"""


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def similarity(a: str, b: str) -> float:
    a, b = " ".join(a.split()).lower(), " ".join(b.split()).lower()
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


# ------------------------------------------------------------ ガイドの読み込み --
_LV_PATTERNS = [
    re.compile(r"^(?P<name>[A-Za-z][A-Za-z0-9 '\-.]*?)\s*[\(\[]\s*(?:lv\.?|lvl\.?|level)?\s*(?P<lv>\d{1,3})\s*[\)\]]$", re.I),
    re.compile(r"^(?P<name>[A-Za-z][A-Za-z0-9 '\-.]*?)\s*[-–:|,]?\s*(?:lv\.?|lvl\.?|level)\s*(?P<lv>\d{1,3})$", re.I),
    re.compile(r"^(?:lv\.?|lvl\.?|level)?\s*(?P<lv>\d{1,3})\s*[-–:|.,]?\s+(?P<name>[A-Za-z][A-Za-z0-9 '\-.]*)$", re.I),
    re.compile(r"^(?P<name>[A-Za-z][A-Za-z0-9 '\-.]*?)\s*[\t|,;]\s*(?P<lv>\d{1,3})\b.*$"),
]
_NAME_HDR = ("monster", "mob", "name")
_LV_HDR = ("lv", "level", "lvl")
_MAP_HDR = ("map", "location", "zone", "area")


def _clean_map(name: str) -> str:
    from .maploc import normalize_map

    name = re.sub(r"\s+", " ", re.sub(r"[#*:]+", " ", name)).strip(" -–")
    return normalize_map(name) if name else ""


def _looks_like_map(line: str, known: list[str]) -> str | None:
    """行がマップ名 (見出し) かどうか。既知のマップ名に 85% 以上近ければその名前。"""
    t = re.sub(r"^[#=*\s]+|[#=*:\s]+$", "", line).strip()
    t = re.sub(r"\s*\((?:lv\.?\s*)?\d+\s*[-~–]\s*\d+\)\s*$", "", t, flags=re.I)  # "Atlans (Lv 40-80)"
    if not t or len(t) > 40 or not t[0].isalpha():
        return None
    best = max(known, key=lambda k: similarity(t, k))
    return best if similarity(t, best) >= 0.85 else None


def parse_guide_tables(html: str) -> list[dict]:
    """表から {map, monster, level}。列は見出しの文字で、マップは「マップ」列か表の直前の見出しから。"""
    from .monsters import parse_tables

    out = []
    for t in parse_tables(html):
        rows = [r for r in t["rows"] if any(c.strip() for c in r)]
        hi = None
        for i, r in enumerate(rows[:5]):
            low = [c.lower() for c in r]
            if any(any(k in c for k in _NAME_HDR) for c in low) and any(any(k == c.strip(" .") or c.startswith(k) for k in _LV_HDR) for c in low):
                hi = i
                break
        if hi is None:
            continue
        header = [c.lower() for c in rows[hi]]
        cn = next(i for i, c in enumerate(header) if any(k in c for k in _NAME_HDR) and not any(k in c for k in _MAP_HDR))
        cl = next(i for i, c in enumerate(header) if any(k == c.strip(" .") or c.startswith(k) for k in _LV_HDR))
        cm = next((i for i, c in enumerate(header) if any(k in c for k in _MAP_HDR) and i != cn), None)
        for r in rows[hi + 1:]:
            if cn >= len(r) or not r[cn].strip() or [c.lower() for c in r] == header:
                continue
            m = re.search(r"\d{1,3}", r[cl]) if cl < len(r) else None
            maps = r[cm] if cm is not None and cm < len(r) else t["heading"]
            for mp in [x for x in re.split(r"\s*[,/、]\s*", maps or "") if x.strip()] or [""]:
                out.append({"map": _clean_map(mp), "monster": r[cn].strip(), "level": int(m.group()) if m else None})
    return [e for e in out if e["map"] and e["monster"]]


def parse_guide_text(text: str, known: list[str] | None = None) -> list[dict]:
    """ページの文字 (ブラウザで全選択してコピーしたもの) から。マップ名の行の後に「名前 (Lv 12)」などが続く形。"""
    from .maploc import KNOWN_MAPS

    known = list(known or KNOWN_MAPS)
    out, cur = [], None
    for line in text.splitlines():
        line = line.strip().strip("•·-*").strip()
        if not line:
            continue
        mp = _looks_like_map(line, known)
        if mp:
            cur = mp
            continue
        m = re.match(r"^(?:map|location)\s*[:：]\s*(.+)$", line, re.I)
        if m:
            cur = _clean_map(m.group(1))
            continue
        if cur is None:
            continue
        for part in re.split(r"\s*[;]\s*|\s{3,}", line):
            for pat in _LV_PATTERNS:
                g = pat.match(part.strip())
                if g and len(g.group("name").strip()) >= 3:
                    out.append({"map": cur, "monster": g.group("name").strip(" -.:"), "level": int(g.group("lv"))})
                    break
    return out


def save_guide(db: sqlite3.Connection, rows: list[dict], source: str = GUIDE_URL) -> tuple[int, int]:
    """ガイドの内容を保存する。モンスター一覧 (monsters) とマップ名の辞書 (map_names) にも入れる。"""
    from .monsters import ensure_schema as ensure_monsters

    ensure_schema(db)
    ensure_monsters(db)
    now = time.time()
    maps = sorted({r["map"] for r in rows})
    db.executemany("INSERT INTO guide_maps(name, source, updated) VALUES (?,?,?) "
                   "ON CONFLICT(name) DO UPDATE SET source=excluded.source, updated=excluded.updated",
                   [(m, source, now) for m in maps])
    db.executemany("INSERT INTO guide_monsters(map, monster, level, source, updated) VALUES (?,?,?,?,?) "
                   "ON CONFLICT(map, monster) DO UPDATE SET level=COALESCE(excluded.level, guide_monsters.level), "
                   "source=excluded.source, updated=excluded.updated",
                   [(r["map"], r["monster"], r["level"], source, now) for r in rows])
    db.executemany("INSERT INTO monsters(name, level, map, map_en, source, updated) VALUES (?,?,?,?, 'guide:rexmu', ?) "
                   "ON CONFLICT(name, map) DO UPDATE SET level=COALESCE(monsters.level, excluded.level), updated=excluded.updated",
                   [(r["monster"], r["level"], r["map"], r["map"], now) for r in rows])
    # 英字だけのマップ名は、画面右下の現在地の読みでも既知のマップとして扱う
    db.execute("CREATE TABLE IF NOT EXISTS map_names (name TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending', "
               "reviewed INTEGER NOT NULL DEFAULT 0, raw TEXT, reads INTEGER NOT NULL DEFAULT 0, first_seen REAL, "
               "last_seen REAL, x INTEGER, y INTEGER)")
    db.executemany("INSERT INTO map_names(name, status, reviewed, raw, first_seen, last_seen) VALUES (?, 'ok', 1, 'guide', ?, ?) "
                   "ON CONFLICT(name) DO UPDATE SET status='ok', reviewed=1",
                   [(m, now, now) for m in maps if re.fullmatch(r"[A-Za-z][A-Za-z '\-]+", m)])
    db.commit()
    return len(maps), len(rows)


# ------------------------------------------------------------ 照合と登録 --
@dataclass
class Match:
    monster: str          # ガイドの名前
    level: int | None     # ガイドのレベル
    map: str              # ガイドで載っているマップ
    ratio: float          # 読みとの似ている度合い (0..1)
    in_map: bool          # 今いるマップのモンスターとして載っているか


class SpawnRegistry:
    """ガイドとの照合と、出現エリアの登録。db は記録用の接続 (WorldLogger が開いたもの)。"""

    def __init__(self, db: sqlite3.Connection, threshold: float = 0.85, area: int = 10):
        self.db = db
        self.threshold = threshold
        self.area = area
        ensure_schema(db)
        self.reload()

    def reload(self) -> None:
        self.guide = self.db.execute("SELECT map, monster, level FROM guide_monsters").fetchall()

    @property
    def names(self) -> list[str]:
        return sorted({g[1] for g in self.guide})

    def match(self, raw: str, map_name: str | None = None) -> Match | None:
        best = None
        for mp, name, lv in self.guide:
            r = similarity(raw, name)
            same = map_name is not None and mp.lower() == map_name.lower()
            key = (r >= self.threshold and same, r)  # 85% 以上なら同じマップのものを優先
            if best is None or key > best[0]:
                best = (key, Match(name, lv, mp, r, same))
        if best is None or best[1].ratio < self.threshold:
            return None
        m = best[1]
        if map_name is not None and not m.in_map:  # 同じ名前が今いるマップにも載っていないか
            m.in_map = any(mp.lower() == map_name.lower() and name == m.monster for mp, name, _ in self.guide)
        return m

    def register(self, raw: str, level: int | None, loc, ts: float | None = None) -> Match | None:
        """読みがガイドの名前と 85% 以上一致したら、その場所の 10 マス一帯を出現エリアとして登録する。"""
        if loc is None or not self.guide:
            return None
        m = self.match(raw, loc.map)
        if m is None:
            return None
        ts = ts or time.time()
        ax, ay = loc.x // self.area * self.area, loc.y // self.area * self.area
        self.db.execute(
            "INSERT INTO monster_areas(map, ax, ay, monster, level, reads, first_seen, last_seen, best_ratio, in_guide_map) "
            "VALUES (?,?,?,?,?,1,?,?,?,?) ON CONFLICT(map, ax, ay, monster) DO UPDATE SET reads=reads+1, "
            "last_seen=excluded.last_seen, best_ratio=MAX(best_ratio, excluded.best_ratio), "
            "level=COALESCE(monster_areas.level, excluded.level)",
            (loc.map, ax, ay, m.monster, level if level is not None else m.level, ts, ts, m.ratio, int(m.in_map)))
        return m
