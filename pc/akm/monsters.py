"""モンスター一覧 (マップごとの出現モンスター) の取り込みと保存。

Web ページの表 (HTML の <table>) から「モンスター名」「レベル」「マップ / 出現場所」の列を自動で見つけて
SQLite (mu_map.db) の monsters 表に入れる。狩場 (spots) に「狙うモンスター」を結び付けるのにも使う。
"""
from __future__ import annotations

import re
import sqlite3
import time
from html.parser import HTMLParser

# 日本語のマップ名 → ゲーム内 (英語) のマップ名。ゲーム画面の "Atlans (32, 68)" と突き合わせるのに使う
MAP_JA = {
    "ロレンシア": "Lorencia", "ダンジョン": "Dungeon", "デビアス": "Devias", "ノリア": "Noria",
    "ロストタワー": "Lost Tower", "エクサイル": "Exile", "アリーナ": "Arena", "アトランス": "Atlans",
    "タルカン": "Tarkan", "デビルスクエア": "Devil Square", "イカルス": "Icarus", "ブラッドキャッスル": "Blood Castle",
    "カオスキャッスル": "Chaos Castle", "カリマ": "Kalima", "試練の地": "Land of Trials", "アイーダ": "Aida",
    "クライウルフ": "Crywolf Fortress", "クライウルフ要塞": "Crywolf Fortress", "カントル": "Kanturu",
    "カンツル": "Kanturu", "カントル遺跡": "Kanturu Relics", "カンツル遺跡": "Kanturu Relics",
    "バルガス兵舎": "Barracks of Balgass", "バルガスの兵舎": "Barracks of Balgass", "避難所": "Balgass Refuge",
    "幻影寺院": "Illusion Temple", "エルベランド": "Elbeland", "平穏の沼": "Swamp of Calmness",
    "安息の沼": "Swamp of Calmness", "ラクリオン": "Raklion", "バルカヌス": "Vulcanus", "ヴァルカヌス": "Vulcanus",
    "サンタ村": "Santa Village", "ドッペルゲンガー": "Doppelganger", "帝国守護要塞": "Imperial Guardian",
    "ロレンマーケット": "Loren Market", "カルトゥラン": "Karutan", "カルタン": "Karutan",
}

NAME_KEYS = ("モンスター", "名前", "名称", "monster", "name")
LEVEL_KEYS = ("lv", "レベル", "level")
MAP_KEYS = ("マップ", "出現", "場所", "生息", "map", "location", "エリア")


def map_to_en(name: str) -> str:
    """日本語のマップ名を英語に。末尾の数字 (アトランス2 など) や記号は外して照合する。"""
    s = re.sub(r"[\s　]+", "", name)
    base = re.sub(r"[0-9０-９\-－~〜・()（）]+$", "", s)
    for ja, en in sorted(MAP_JA.items(), key=lambda kv: -len(kv[0])):
        if base.startswith(ja) or ja in base:
            return en
    return name.strip()


class _TableParser(HTMLParser):
    """全ての <table> を「行 = セル文字列のリスト」にする。rowspan / colspan も展開する。直前の見出しも覚える。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[dict] = []
        self._stack: list[dict] = []
        self._cell: list[str] | None = None
        self._cell_span = (1, 1)
        self._heading: list[str] | None = None
        self.last_heading = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("h1", "h2", "h3", "h4", "caption"):
            self._heading = []
        elif tag == "table":
            self._stack.append({"rows": [], "row": None, "spans": {}, "heading": self.last_heading})
        elif tag == "tr" and self._stack:
            self._stack[-1]["row"] = []
        elif tag in ("td", "th") and self._stack:
            self._cell = []
            try:
                self._cell_span = (int(a.get("rowspan") or 1), int(a.get("colspan") or 1))
            except ValueError:
                self._cell_span = (1, 1)
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3", "h4", "caption") and self._heading is not None:
            self.last_heading = "".join(self._heading).strip()
            if self._stack and tag == "caption":
                self._stack[-1]["heading"] = self.last_heading
            self._heading = None
        elif tag in ("td", "th") and self._stack and self._cell is not None:
            t = self._stack[-1]
            if t["row"] is None:
                t["row"] = []
            text = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            rs, cs = self._cell_span
            col = self._next_col(t)
            for c in range(cs):
                t["row"].append(None)  # 位置だけ確保 (下で埋める)
                self._place(t, col + c, text)
                if rs > 1:
                    t["spans"][col + c] = [rs - 1, text]
            self._cell = None
        elif tag == "tr" and self._stack and self._stack[-1]["row"] is not None:
            t = self._stack[-1]
            self._fill_spans(t, upto=None)
            t["rows"].append([c if c is not None else "" for c in t["row"]])
            t["row"] = None
        elif tag == "table" and self._stack:
            t = self._stack.pop()
            self.tables.append({"heading": t["heading"], "rows": t["rows"]})

    def _next_col(self, t):
        self._fill_spans(t, upto=len(t["row"]))
        return len([c for c in t["row"] if c is not None])

    def _place(self, t, col, text):
        row = t["row"]
        while len(row) <= col:
            row.append(None)
        row[col] = text
        # 余分に確保した None を詰める
        while row and row[-1] is None and len(row) > col + 1:
            row.pop()

    def _fill_spans(self, t, upto):
        """上の行から縦に続くセル (rowspan) を今の行に入れる。"""
        row = t["row"]
        filled = True
        while filled:
            filled = False
            col = len([c for c in row if c is not None])
            if col in t["spans"]:
                left, text = t["spans"][col]
                self._place(t, col, text)
                if left <= 1:
                    del t["spans"][col]
                else:
                    t["spans"][col][0] = left - 1
                filled = True

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        if self._heading is not None:
            self._heading.append(data)


def parse_tables(html: str) -> list[dict]:
    p = _TableParser()
    p.feed(html)
    return p.tables


def _find_col(header: list[str], keys) -> int | None:
    for i, h in enumerate(header):
        hl = h.lower()
        if any(k in hl for k in keys):
            return i
    return None


def _level(text: str) -> int | None:
    m = re.search(r"\d+", text.translate(str.maketrans("０１２３４５６７８９", "0123456789")))
    return int(m.group()) if m else None


def extract_monsters(tables: list[dict]) -> list[dict]:
    """表から {name, level, map, map_en} の一覧を作る。列は見出しの文字で判定する。"""
    out = []
    for t in tables:
        rows = [r for r in t["rows"] if any(c for c in r)]
        if len(rows) < 2:
            continue
        # 見出し行: 名前とレベルの列が見つかる最初の行
        hi = None
        for i, r in enumerate(rows[:5]):
            if _find_col(r, NAME_KEYS) is not None and _find_col(r, LEVEL_KEYS) is not None:
                hi = i
                break
        if hi is None:
            continue
        header = rows[hi]
        cn, cl, cm = _find_col(header, NAME_KEYS), _find_col(header, LEVEL_KEYS), _find_col(header, MAP_KEYS)
        if cm == cn:
            cm = None
        for r in rows[hi + 1:]:
            if cn >= len(r) or not r[cn] or r == header:
                continue
            name = r[cn].strip()
            lv = _level(r[cl]) if cl is not None and cl < len(r) else None
            maps = r[cm] if cm is not None and cm < len(r) else t["heading"]
            # 「アトランス、カリマ」のように複数書かれていることがある
            for mp in [m.strip() for m in re.split(r"[、,/／・\s]+", maps or "") if m.strip()] or [""]:
                out.append({"name": name, "level": lv, "map": mp, "map_en": map_to_en(mp) if mp else ""})
    return out


SCHEMA = """
CREATE TABLE IF NOT EXISTS monsters (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, level INTEGER, map TEXT NOT NULL DEFAULT '',
    map_en TEXT NOT NULL DEFAULT '', source TEXT, updated REAL, UNIQUE(name, map));
CREATE TABLE IF NOT EXISTS spot_monsters (
    spot_id INTEGER NOT NULL, monster TEXT NOT NULL, PRIMARY KEY (spot_id, monster));
"""


def save_monsters(db: sqlite3.Connection, monsters: list[dict], source: str) -> int:
    db.executescript(SCHEMA)
    now = time.time()
    db.executemany(
        "INSERT INTO monsters(name, level, map, map_en, source, updated) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(name, map) DO UPDATE SET level=excluded.level, map_en=excluded.map_en, "
        "source=excluded.source, updated=excluded.updated",
        [(m["name"], m["level"], m["map"], m["map_en"], source, now) for m in monsters])
    db.commit()
    return len(monsters)


def fetch_html(url: str) -> str:
    import requests

    r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (AutoKeyMouse monster import)"})
    r.raise_for_status()
    # 古いサイトは Shift_JIS が多い。<meta charset> を見て、無ければ推定
    m = re.search(rb'charset=["\']?([A-Za-z0-9_\-]+)', r.content[:3000])
    enc = m.group(1).decode() if m else (r.apparent_encoding or "utf-8")
    if enc.lower() in ("shift_jis", "sjis", "x-sjis", "shift-jis"):
        enc = "cp932"
    return r.content.decode(enc, errors="replace")
