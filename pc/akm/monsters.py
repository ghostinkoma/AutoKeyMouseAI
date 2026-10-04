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
    "タルカン": "Tarkan", "デビルスクエア": "Devil Square", "DS": "Devil Square",
    "イカルス": "Icarus", "イカロス": "Icarus", "ブラッドキャッスル": "Blood Castle", "BC": "Blood Castle",
    "カオスキャッスル": "Chaos Castle", "CC": "Chaos Castle", "カリマ": "Kalima", "カルリマ": "Kalima",
    "試練の地": "Land of Trials", "アイダ": "Aida", "アイーダ": "Aida",
    "クライウルフ": "Crywolf Fortress", "クライウルフ要塞": "Crywolf Fortress", "ＣＷ攻防戦": "Crywolf Fortress",
    "カントル": "Kanturu", "カンツル": "Kanturu", "カントル遺跡": "Kanturu Relics", "カンツル遺跡": "Kanturu Relics",
    "バルガス兵舎": "Barracks of Balgass", "バルガス兵営": "Barracks of Balgass", "バルガスの兵舎": "Barracks of Balgass",
    "バルガス安息所": "Balgass Refuge", "避難所": "Balgass Refuge",
    "幻影寺院": "Illusion Temple", "IT": "Illusion Temple", "エルベランド": "Elbeland",
    "平穏の沼": "Swamp of Calmness", "平穏の沼地": "Swamp of Calmness", "安息の沼": "Swamp of Calmness",
    "ラクリオン": "Raklion", "バルカヌス": "Vulcanus", "ヴァルカヌス": "Vulcanus", "ヴォルカノス": "Vulcanus",
    "サンタ村": "Santa Village", "ドッペルゲンガー": "Doppelganger", "帝国守護要塞": "Imperial Guardian",
    "ロレンマーケット": "Loren Market", "カルタン": "Karutan", "カルトゥラン": "Karutan",
}

NAME_KEYS = ("モンスター", "名前", "名称", "monster", "name")
LEVEL_KEYS = ("lv", "レベル", "level")
MAP_KEYS = ("マップ", "出現", "場所", "生息", "map", "location", "エリア")


def map_to_en(name: str) -> str:
    """日本語のマップ名をゲーム内の英語名に。末尾の番号 (カルリマ1, DS3 など) は外して照合する。

    「奈落のアトランス」のような別マップを Atlans と取り違えないよう、完全一致か前方一致だけで判定する。
    分からない名前はそのまま返す。
    """
    s = re.sub(r"[\s　]+", "", name)
    base = re.sub(r"[0-9０-９\-－~〜・()（）]+$", "", s)
    if base in MAP_JA:
        return MAP_JA[base]
    for ja, en in sorted(MAP_JA.items(), key=lambda kv: -len(kv[0])):
        if len(ja) >= 3 and base.startswith(ja):
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
        self._row_imgs: list[str] = []
        self._heading: list[str] | None = None
        self.last_heading = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("h1", "h2", "h3", "h4", "caption"):
            self._heading = []
        elif tag == "table":
            self._stack.append({"rows": [], "imgs": [], "row": None, "spans": {}, "heading": self.last_heading})
        elif tag == "tr" and self._stack:
            self._stack[-1]["row"] = []
            self._row_imgs = []
        elif tag == "img" and self._cell is not None and a.get("src"):
            self._row_imgs.append(a["src"])
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
            t["imgs"].append(list(self._row_imgs))
            t["row"] = None
        elif tag == "table" and self._stack:
            t = self._stack.pop()
            self.tables.append({"heading": t["heading"], "rows": t["rows"], "imgs": t["imgs"]})

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
        imgs_all = t.get("imgs") or [[] for _ in t["rows"]]
        pairs = [(r, im) for r, im in zip(t["rows"], imgs_all) if any(c for c in r) or im]
        rows = [r for r, _ in pairs]
        imgs = [im for _, im in pairs]
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
        cl, cm = _find_col(header, LEVEL_KEYS), _find_col(header, MAP_KEYS)
        # 「モンスター名」が 2 列 (アイコン + 名前) にまたがることがある: 文字の入っている列を選ぶ
        name_cols = [i for i, h in enumerate(header) if any(k in h.lower() for k in NAME_KEYS)]
        sample = rows[hi + 1: hi + 30]
        cn = max(name_cols, key=lambda i: sum(1 for r in sample if i < len(r) and r[i].strip()))
        if cm == cn:
            cm = None
        for r, im in zip(rows[hi + 1:], imgs[hi + 1:]):
            if cn >= len(r) or not r[cn] or r == header:
                continue
            icon = im[0] if im else ""
            name = r[cn].strip()
            lv = _level(r[cl]) if cl is not None and cl < len(r) else None
            maps = r[cm] if cm is not None and cm < len(r) else t["heading"]
            # 「アトランス、カリマ」のように複数書かれていることがある
            for mp in [m.strip() for m in re.split(r"[、,/／・\s]+", maps or "") if m.strip()] or [""]:
                out.append({"name": name, "level": lv, "map": mp, "map_en": map_to_en(mp) if mp else "", "icon": icon})
    return out


SCHEMA = """
CREATE TABLE IF NOT EXISTS monsters (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, level INTEGER, map TEXT NOT NULL DEFAULT '',
    map_en TEXT NOT NULL DEFAULT '', source TEXT, updated REAL, icon TEXT, note TEXT, hp INTEGER, UNIQUE(name, map));
CREATE TABLE IF NOT EXISTS spot_monsters (
    spot_id INTEGER NOT NULL, monster TEXT NOT NULL, PRIMARY KEY (spot_id, monster));
"""


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    cols = [r[1] for r in db.execute("PRAGMA table_info(monsters)")]
    for col, typ in (("icon", "TEXT"), ("note", "TEXT"), ("hp", "INTEGER")):
        if col not in cols:  # 古い DB に列を足す
            db.execute(f"ALTER TABLE monsters ADD COLUMN {col} {typ}")


def download_icons(monsters: list[dict], base_url: str | None, out_dir) -> int:
    """アイコン画像 (ページの <img>) を out_dir に保存し、m["icon"] を保存先のファイル名にする。"""
    import hashlib
    from pathlib import Path
    from urllib.parse import urljoin

    import requests

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache: dict[str, str] = {}
    n = 0
    for m in monsters:
        src = m.get("icon") or ""
        if not src:
            continue
        url = urljoin(base_url, src) if base_url else src
        if url not in cache:
            ext = Path(url.split("?")[0]).suffix.lower() or ".png"
            if ext not in (".png", ".gif", ".jpg", ".jpeg", ".bmp"):
                ext = ".png"
            fname = hashlib.sha1(url.encode()).hexdigest()[:16] + ext
            path = out_dir / fname
            try:
                if not path.exists():
                    r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
                    r.raise_for_status()
                    path.write_bytes(r.content)
                    n += 1
                cache[url] = f"{out_dir.name}/{fname}"  # DB からの相対パス
            except Exception as e:
                print(f"[monsters] アイコンを取得できません: {url} ({e})")
                cache[url] = ""
        m["icon"] = cache[url]
    return n


def save_monsters(db: sqlite3.Connection, monsters: list[dict], source: str) -> int:
    ensure_schema(db)
    now = time.time()
    db.executemany(
        "INSERT INTO monsters(name, level, map, map_en, source, updated, icon, note, hp) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(name, map) DO UPDATE SET level=COALESCE(excluded.level, monsters.level), map_en=excluded.map_en, "
        "source=excluded.source, updated=excluded.updated, icon=COALESCE(NULLIF(excluded.icon, ''), monsters.icon), "
        "note=COALESCE(NULLIF(excluded.note, ''), monsters.note), hp=COALESCE(excluded.hp, monsters.hp)",
        [(m["name"], m["level"], m["map"], m["map_en"], source, now, m.get("icon") or "", m.get("note") or "",
          m.get("hp")) for m in monsters])
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


# ------------------------------------------------------------ 同梱データ --
BUILTIN = __import__("pathlib").Path(__file__).resolve().parent / "data" / "monsters_munou2014.tsv"


def load_builtin(path=BUILTIN) -> list[dict]:
    """同梱のモンスター一覧 (レベル|名前|備考|生命|マップ/マップ...)。"""
    out = []
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        if not line.strip() or line.startswith("#"):
            continue
        lv, name, note, hp, maps = (line.split("|") + [""] * 5)[:5]
        for mp in [m for m in maps.split("/") if m]:
            out.append({"name": name, "level": int(lv) if lv.isdigit() else None, "map": mp,
                        "map_en": map_to_en(mp), "icon": "", "note": note, "hp": int(hp) if hp.isdigit() else None})
    return out


def parse_pasted_text(text: str) -> list[dict]:
    """ブラウザで表を選択してコピーしたテキスト (タブ区切り) を読む。

    セルの中の改行 (「※レアキャラ」や複数の出現マップ) で 1 行が何行にも分かれるので、
    レベルの数字で始まる行から次のレベル行までを 1 件としてつなげる。見出し行 (レベル…出現マップ) は飛ばす。
    列: レベル, (アイコン), 名前, 生命, 最小攻撃力, 最大攻撃力, 防御力, 防御成功率, 攻撃成功率, 属性×3, 出現マップ…
    """
    records: list[list[str]] = []
    in_header = False
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        first = line.split("\t")[0].strip()
        if first == "レベル":
            in_header = "出現マップ" not in line
            continue
        if in_header:
            if "出現マップ" in line:
                in_header = False
            continue
        if re.fullmatch(r"[0-9０-９]+|[?？]", first) and "\t" in line:
            records.append(line.split("\t"))
        elif records:
            records[-1].extend(line.split("\t"))
    out = []
    for f in records:
        f = [c.strip() for c in f]
        notes = [c.lstrip("※") for c in f if c.startswith("※")]
        f = [c for c in f if not c.startswith("※")]
        if len(f) < 4:
            continue
        lv = _level(f[0]) if f[0] not in ("?", "？") else None
        name = f[2] if len(f) > 2 and f[2] else f[1]
        hp = _level(f[3]) if len(f) > 3 else None
        maps = [m for m in f[12:] if m] if len(f) > 12 else [f[-1]]
        tags = [m for m in maps if m.upper() == "EVENT"]
        maps = [m for m in maps if m.upper() != "EVENT"]
        note = "、".join(notes + (["イベント"] if tags and not notes else []))
        for mp in maps or [""]:
            out.append({"name": name, "level": lv, "map": mp, "map_en": map_to_en(mp) if mp else "",
                        "icon": "", "note": note, "hp": hp})
    return out


def ensure_builtin(db: sqlite3.Connection) -> int:
    """モンスター一覧が空なら同梱データを入れる (ビューアのドロップダウンに最初から出るように)。"""
    ensure_schema(db)
    if db.execute("SELECT COUNT(*) FROM monsters").fetchone()[0]:
        return 0
    return save_monsters(db, load_builtin(), "builtin:munou2014")
