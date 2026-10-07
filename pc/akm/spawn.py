"""マップガイド (https://wiki.rexmu.online/map-guide) のマップ / モンスター / レベルを DB に入れ、
ゲーム画面で認識したモンスター名をガイドと照らし合わせて「出現エリア」(10 マス一帯) を登録する。

表 (mu_map.db):
  guide_maps      マップ (ガイドに載っているもの)
  guide_monsters  マップごとのモンスターとレベル
  monster_areas   出現エリア: マップの 10 マス四方の区画 (ax, ay は 10 の倍数) ごとに、そこで見たモンスター

登録の条件: 文字認識の読み (辞書で直す前の生の読み) を、まず今いるマップのモンスターだけと照らし合わせて 65% 以上
  (似た名前が複数ならレベルと 1 位・2 位の差で決める)、だめなら全マップから 85% 以上 (SpawnRegistry の説明を参照)。
  ガイドでは別のマップに載っているモンスターなら in_guide_map = 0。
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
    name TEXT PRIMARY KEY, source TEXT, updated REAL, min_level INTEGER);
CREATE TABLE IF NOT EXISTS guide_monsters (
    map TEXT NOT NULL, monster TEXT NOT NULL, level INTEGER, source TEXT, updated REAL, icon TEXT,
    PRIMARY KEY (map, monster));
CREATE TABLE IF NOT EXISTS monster_areas (
    map TEXT NOT NULL, ax INTEGER NOT NULL, ay INTEGER NOT NULL, monster TEXT NOT NULL, level INTEGER,
    reads INTEGER NOT NULL DEFAULT 0, first_seen REAL, last_seen REAL, best_ratio REAL, in_guide_map INTEGER,
    PRIMARY KEY (map, ax, ay, monster));
"""


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    if "icon" not in [r[1] for r in db.execute("PRAGMA table_info(guide_monsters)")]:
        db.execute("ALTER TABLE guide_monsters ADD COLUMN icon TEXT")
    cols = [r[1] for r in db.execute("PRAGMA table_info(guide_monsters)")]
    for col in ("hp", "dmg_min", "dmg_max", "def"):
        if col not in cols:
            db.execute(f"ALTER TABLE guide_monsters ADD COLUMN {col} INTEGER")
    gcols = [r[1] for r in db.execute("PRAGMA table_info(guide_maps)")]
    for col in ("min_level", "zen_cost", "resets"):  # 入場に必要なレベル・移動の Zen・推奨リセット回数
        if col not in gcols:
            db.execute(f"ALTER TABLE guide_maps ADD COLUMN {col} INTEGER")


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


def split_map_heading(text: str) -> tuple[str, int | None]:
    """見出し "Aida (230+)" → ("Aida", 230)。括弧の中は入場に必要なレベル。"""
    m = re.search(r"[\(\[]\s*(?:lv\.?\s*)?(\d{1,4})\s*\+?\s*[\)\]]", text, re.I)
    name = re.sub(r"\s*[\(\[].*?[\)\]]", "", text)
    return name, int(m.group(1)) if m else None


def _clean_map(name: str) -> str:
    from .maploc import normalize_map

    name = split_map_heading(name)[0]
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
    """表から {map, monster, level, icon, min_level}。列は見出しの文字で、マップは「マップ」列か表の直前の見出しから。
    アイコンは行の中の最初の画像。見出し "Aida (230+)" の括弧の数字は入場に必要なレベル (min_level)。"""
    from .monsters import parse_tables

    out = []
    for t in parse_tables(html):
        imgs_all = t.get("imgs") or [[] for _ in t["rows"]]
        pairs = [(r, im) for r, im in zip(t["rows"], imgs_all) if any(c.strip() for c in r)]
        rows = [r for r, _ in pairs]
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
        # ステータスの列 (あれば): HP / Damage "4-7" / Defense
        ch = next((i for i, c in enumerate(header) if c.strip() in ("hp", "life", "health")), None)
        cd = next((i for i, c in enumerate(header) if any(k in c for k in ("damage", "dmg", "attack", "atk"))), None)
        cf = next((i for i, c in enumerate(header) if c.strip(" .") in ("def", "defense", "defence")), None)

        def num(r, c):
            if c is None or c >= len(r):
                return None
            m2 = re.search(r"\d[\d,]*", r[c])
            return int(m2.group().replace(",", "")) if m2 else None

        for r, im in pairs[hi + 1:]:
            if cn >= len(r) or not r[cn].strip() or [c.lower() for c in r] == header:
                continue
            m = re.search(r"\d{1,3}", r[cl]) if cl < len(r) else None
            maps = r[cm] if cm is not None and cm < len(r) else t["heading"]
            dmg = re.findall(r"\d+", r[cd].replace(",", "")) if cd is not None and cd < len(r) else []
            stats = {"hp": num(r, ch), "dmg_min": int(dmg[0]) if dmg else None,
                     "dmg_max": int(dmg[-1]) if dmg else None, "def": num(r, cf)}
            for mp in [x for x in re.split(r"\s*[,/、]\s*", maps or "") if x.strip()] or [""]:
                out.append({"map": _clean_map(mp), "monster": r[cn].strip(), "level": int(m.group()) if m else None,
                            "icon": im[0] if im else None, "min_level": split_map_heading(mp)[1], **stats})
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


# <section id="map-guide-lorencia"> … </section> の形 (マップごとの区切り。中にモンスターのアイコン・名前・レベル)
_SECTION_ID = re.compile(r"^map-?guide-?(.+)$", re.I)
_LABELS = {"lv", "lvl", "level", "hp", "life", "exp", "monster", "monsters", "name", "icon", "map", "maps", "drop",
           "drops", "location", "spawn", "spawns", "boss", "bosses", "image", "type", "def", "defense", "attack",
           "dmg", "damage", "info", "guide", "zone", "area", "coords", "coordinates"}
_LV_TOKEN = re.compile(r"(?:lv|lvl|level)\s*[.:]?\s*(\d{1,3})", re.I)


class _SectionWalker:
    """HTML を読み、map-guide の section ごとに (見出し, [('img', src, alt) | ('text', 文字)]) を集める。"""

    def __init__(self):
        from html.parser import HTMLParser

        walker = self
        self.sections: list[dict] = []
        self._cur: dict | None = None
        self._depth = 0
        self._in_heading = False

        class P(HTMLParser):
            def handle_starttag(self, tag, attrs):
                walker.start(tag, dict(attrs))

            def handle_startendtag(self, tag, attrs):
                walker.start(tag, dict(attrs))
                if tag == "section":
                    walker.end(tag)

            def handle_endtag(self, tag):
                walker.end(tag)

            def handle_data(self, data):
                walker.data(data)

        self.parser = P(convert_charrefs=True)

    def start(self, tag, a):
        if self._cur is None:
            if tag == "section":
                m = _SECTION_ID.match(a.get("id") or "")
                if m:
                    self._cur = {"slug": m.group(1), "heading": "", "items": []}
                    self._depth = 1
            return
        if tag == "section":
            self._depth += 1
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6") and not self._cur["heading"]:
            self._in_heading = True
        elif tag in ("p", "div", "span", "h3", "h4") and a.get("title") and self._depth == 1:
            self._cur["items"].append(("name", a["title"].strip()))  # カードの名前 (<p title="Spider">)
        elif tag == "img" and self._depth == 1:
            src = a.get("src") or ""
            if not src or src.startswith("data:"):  # 遅れて読み込む画像は data-src に本物がある
                src = a.get("data-src") or a.get("data-original") or (a.get("srcset") or "").split(" ")[0] or src
            self._cur["items"].append(("img", src, (a.get("alt") or a.get("title") or "").strip()))

    def end(self, tag):
        if self._cur is None:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._in_heading = False
        elif tag == "section":
            self._depth -= 1
            if self._depth == 0:
                self.sections.append(self._cur)
                self._cur = None

    def data(self, d):
        if self._cur is None or self._depth > 1:  # 入れ子の section (説明など) の文字は使わない
            return
        t = re.sub(r"\s+", " ", d).strip()
        if not t:
            return
        if self._in_heading:
            self._cur["heading"] += (" " if self._cur["heading"] else "") + t
        else:
            self._cur["items"].append(("text", t))

    def feed(self, html: str) -> list[dict]:
        self.parser.feed(html)
        return self.sections


def _name_like(t: str) -> bool:
    t = t.strip(" :-–")
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z '\-.]{2,40}", t)) and t.lower().strip(" .") not in _LABELS


def _section_map(sec: dict) -> str:
    from .maploc import KNOWN_MAPS, normalize_map

    slug = re.sub(r"[-_]+", " ", sec["slug"]).strip().title()
    head = re.sub(r"\s*[\(\[].*$", "", sec["heading"]).strip(" :-–")
    for cand in (slug, head):
        n = normalize_map(cand) if cand else ""
        if n in KNOWN_MAPS:
            return n
    return head if head and re.fullmatch(r"[A-Za-z][A-Za-z0-9 '\-]{1,40}", head) else slug


def parse_guide_sections(html: str) -> list[dict]:
    """<section id="map-guide-…"> ごとに、アイコン (img) と、その後に続く名前・レベルを組にする。

    img の alt が名前ならそれを、そうでなければアイコンの後から次のアイコンまでの文字のうち名前らしい最初のものを名前、
    "Lv 12" / "Level: 12" の数字 (無ければ 1〜400 の数字だけの文字) をレベルにする。アイコンの無い section は
    「名前 (Lv 12)」などの行の形で読む。
    """
    out = []
    for sec in _SectionWalker().feed(html):
        mp = _section_map(sec)
        items = sec["items"]
        if any(it[0] == "name" for it in items):
            out += _parse_cards(mp, items)
            continue
        imgs = [i for i, it in enumerate(items) if it[0] == "img"]
        if not imgs:
            lines = "\n".join(it[1] for it in items if it[0] == "text")
            out += [dict(r, map=mp) for r in parse_guide_text(mp + "\n" + lines, known=[mp])]
            continue
        for k, i in enumerate(imgs):
            texts = [it[1] for it in items[i + 1:(imgs[k + 1] if k + 1 < len(imgs) else len(items))] if it[0] == "text"]
            alt = items[i][2]
            # img の alt が名前そのもの ("Cursed Wizard") ならそれを使う。"spider icon" のような説明なら後ろの文字から
            if _name_like(alt) and not re.search(r"\b(icon|image|img|logo|picture|avatar)\b", alt, re.I):
                name = alt
            else:
                name = next((t.strip(" :-–") for t in texts if _name_like(t)), None)
            if name is None:
                continue
            lv = None
            joined = " ".join(texts)
            m = _LV_TOKEN.search(joined)
            if m:
                lv = int(m.group(1))
            else:
                nums = [int(t) for t in texts if re.fullmatch(r"\d{1,3}", t) and 1 <= int(t) <= 400]
                lv = nums[0] if nums else None
            out.append({"map": mp, "monster": name, "level": lv, "icon": items[i][1]})
    uniq = {}
    for r in out:
        uniq.setdefault((r["map"], r["monster"]), r)
    return list(uniq.values())


_NUM = re.compile(r"^\d[\d,]*$")


def _int(t: str) -> int | None:
    m = re.search(r"\d[\d,]*", t)
    return int(m.group().replace(",", "")) if m else None


def _parse_cards(mp: str, items: list) -> list[dict]:
    """wiki.rexmu.online のカード形式:
         見出し: "Minimum level: 10" / "Zen cost: 1,000" / "Recommended resets: 0 RR"
         カード: <img alt="Spider" src=…> (無ければ骸骨の印) → <p title="Spider"> → "4~7" (攻撃力)
                → "Level" "2" / "HP" "30" / "Defense" "1"
    """
    out: list[dict] = []
    head = {"min_level": None, "zen_cost": None, "resets": None}
    card = None
    pending_img = None
    label = None
    for it in items:
        if it[0] == "img":
            pending_img = it[1]
            continue
        if it[0] == "name":
            card = {"map": mp, "monster": it[1], "level": None, "icon": pending_img, "hp": None,
                    "dmg_min": None, "dmg_max": None, "def": None}
            pending_img = None
            out.append(card)
            label = None
            continue
        t = it[1]
        low = t.lower().rstrip(": ")
        if card is None:  # 見出しの情報
            if low.startswith("minimum level"):
                label = "min_level"
            elif low.startswith("zen cost"):
                label = "zen_cost"
            elif low.startswith("recommended reset"):
                label = "resets"
            elif label and _int(t) is not None:
                head[label] = _int(t)
                label = None
            continue
        if t == card["monster"]:
            continue
        m = re.fullmatch(r"(\d[\d,]*)\s*[~\-–]\s*(\d[\d,]*)", t)
        if m and card["dmg_min"] is None:
            card["dmg_min"], card["dmg_max"] = _int(m.group(1)), _int(m.group(2))
        elif low in ("level", "lv", "lvl"):
            label = "level"
        elif low in ("hp", "life"):
            label = "hp"
        elif low in ("defense", "def", "defence"):
            label = "def"
        elif label and _NUM.match(t):
            card[label] = _int(t)
            label = None
    for c in out:
        c.update(head)
    return out


def section_html(html: str, key: str) -> str | None:
    """id に key を含む map-guide の section の HTML (確認用)。"""
    m = re.search(r'<section[^>]*id=["\']map-?guide-?[^"\']*' + re.escape(key) + r'[^"\']*["\'][^>]*>', html, re.I)
    if not m:
        return None
    end = html.find("</section>", m.end())
    return html[m.start(): end + 10 if end >= 0 else m.start() + 6000]


def save_guide(db: sqlite3.Connection, rows: list[dict], source: str = GUIDE_URL) -> tuple[int, int]:
    """ガイドの内容を保存する。モンスター一覧 (monsters) とマップ名の辞書 (map_names) にも入れる。"""
    from .monsters import ensure_schema as ensure_monsters

    ensure_schema(db)
    ensure_monsters(db)
    now = time.time()
    maps = sorted({r["map"] for r in rows})
    def per_map(key):
        return {r["map"]: r[key] for r in rows if r.get(key) is not None}

    min_lv, zen, resets = per_map("min_level"), per_map("zen_cost"), per_map("resets")
    # 前回の取り込みで入った、今回のガイドに無いもの (読み方を直す前の名前など) は消す
    q = ",".join("?" * len(maps))
    db.execute(f"DELETE FROM guide_monsters WHERE map NOT IN ({q})", maps)
    db.execute(f"DELETE FROM guide_maps WHERE name NOT IN ({q})", maps)
    db.execute(f"DELETE FROM monsters WHERE source='guide:rexmu' AND map NOT IN ({q})", maps)
    db.executemany("INSERT INTO guide_maps(name, source, updated, min_level, zen_cost, resets) VALUES (?,?,?,?,?,?) "
                   "ON CONFLICT(name) DO UPDATE SET source=excluded.source, updated=excluded.updated, "
                   "min_level=COALESCE(excluded.min_level, guide_maps.min_level), "
                   "zen_cost=COALESCE(excluded.zen_cost, guide_maps.zen_cost), "
                   "resets=COALESCE(excluded.resets, guide_maps.resets)",
                   [(m, source, now, min_lv.get(m), zen.get(m), resets.get(m)) for m in maps])
    db.executemany("INSERT INTO guide_monsters(map, monster, level, source, updated, icon, hp, dmg_min, dmg_max, def) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?) "
                   "ON CONFLICT(map, monster) DO UPDATE SET level=COALESCE(excluded.level, guide_monsters.level), "
                   "source=excluded.source, updated=excluded.updated, icon=COALESCE(excluded.icon, guide_monsters.icon), "
                   "hp=COALESCE(excluded.hp, guide_monsters.hp), dmg_min=COALESCE(excluded.dmg_min, guide_monsters.dmg_min), "
                   "dmg_max=COALESCE(excluded.dmg_max, guide_monsters.dmg_max), def=COALESCE(excluded.def, guide_monsters.def)",
                   [(r["map"], r["monster"], r["level"], source, now, r.get("icon") or None, r.get("hp"), r.get("dmg_min"),
                     r.get("dmg_max"), r.get("def")) for r in rows])
    # ビューアのモンスター一覧にも (生命・攻撃力・防御力も。手で登録したものは書き換えない)
    db.executemany("INSERT INTO monsters(name, level, map, map_en, source, updated, icon, hp, atk_min, atk_max, def) "
                   "VALUES (?,?,?,?, 'guide:rexmu', ?,?,?,?,?,?) "
                   "ON CONFLICT(name, map) DO UPDATE SET level=COALESCE(monsters.level, excluded.level), "
                   "updated=excluded.updated, icon=COALESCE(NULLIF(excluded.icon, ''), monsters.icon), "
                   "hp=COALESCE(monsters.hp, excluded.hp), atk_min=COALESCE(monsters.atk_min, excluded.atk_min), "
                   "atk_max=COALESCE(monsters.atk_max, excluded.atk_max), def=COALESCE(monsters.def, excluded.def)",
                   [(r["monster"], r["level"], r["map"], r["map"], now, r.get("icon") or "", r.get("hp"), r.get("dmg_min"),
                     r.get("dmg_max"), r.get("def")) for r in rows])
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
    """ガイドとの照合と、出現エリアの登録。db は記録用の接続 (WorldLogger が開いたもの)。

    照合は 2 段 (マスター 14 マップ・101 体での試算: 読み違い 2 文字で正しく登録 98% / 間違い 0%、
    3 文字で 90% / 0%。全体から 85% 以上で探す方式は 2 文字 65%、3 文字 31%):
      1. 今いるマップのモンスター (5〜11 体) だけを候補にして、似ている度合い map_threshold (65%) 以上。
         2 体以上が近いとき (Bahamut / Great Bahamut など) は、読めたレベルが合う方 (±level_tol)、
         それでも決まらなければ 1 位と 2 位の差が margin 以上のときだけ採用 (決まらなければ登録しない)
      2. 1 で決まらなければ全マップから threshold (85%) 以上 (ガイドで別マップのモンスターなら in_map = False)
    """

    def __init__(self, db: sqlite3.Connection, threshold: float = 0.85, area: int = 10, map_threshold: float = 0.65,
                 margin: float = 0.10, level_tol: int = 3):
        self.db = db
        self.threshold = threshold
        self.map_threshold = map_threshold
        self.margin = margin
        self.level_tol = level_tol
        self.area = area
        ensure_schema(db)
        self.reload()

    def reload(self) -> None:
        self.guide = self.db.execute("SELECT map, monster, level FROM guide_monsters").fetchall()

    @property
    def names(self) -> list[str]:
        return sorted({g[1] for g in self.guide})

    def match_in_map(self, raw: str, map_name: str, level: int | None = None) -> Match | None:
        """今いるマップのモンスターだけで照合する (1 段目)。"""
        cands = sorted(((similarity(raw, name), name, lv, mp) for mp, name, lv in self.guide
                        if mp.lower() == map_name.lower()), reverse=True)
        cands = [c for c in cands if c[0] >= self.map_threshold]
        if not cands:
            return None
        if level is not None:  # レベルが読めていれば、レベルの合うものに絞る
            near = [c for c in cands if c[2] is not None and abs(c[2] - level) <= self.level_tol]
            if len(near) == 1:
                c = near[0]
                return Match(c[1], c[2], c[3], c[0], True)
            if near:
                cands = near
        if len(cands) > 1 and cands[0][0] - cands[1][0] < self.margin:
            return None  # 似た名前が 2 体以上で決めきれない: 登録しない (次の読みで決まるのを待つ)
        c = cands[0]
        return Match(c[1], c[2], c[3], c[0], True)

    def match(self, raw: str, map_name: str | None = None, level: int | None = None) -> Match | None:
        if map_name is not None:
            m = self.match_in_map(raw, map_name, level)
            if m is not None:
                return m
        best = None
        for mp, name, lv in self.guide:  # 2 段目: 全マップから 85% 以上
            r = similarity(raw, name)
            if r >= self.threshold and (best is None or r > best.ratio):
                best = Match(name, lv, mp, r, map_name is not None and mp.lower() == map_name.lower())
        if best is not None and map_name is not None and not best.in_map:  # 同じ名前が今いるマップにも載っていないか
            best.in_map = any(mp.lower() == map_name.lower() and name == best.monster for mp, name, _ in self.guide)
        return best

    def register(self, raw: str, level: int | None, loc, ts: float | None = None) -> Match | None:
        """読みがガイドの名前と一致したら (上の 2 段の照合)、その場所の 10 マス一帯を出現エリアとして登録する。"""
        if loc is None or not self.guide:
            return None
        m = self.match(raw, loc.map, level)
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


# ------------------------------------------------------------ アイコン --
def fetch_icons(rows: list[dict], base_url: str, out_dir, local_dirs=(), log=print) -> dict:
    """ガイドのアイコンを out_dir に PNG で保存し、r["icon"] を DB からの相対パス (monster_icons/xxx.png) にする。

    webp (ビューアの Java では表示できない) は PNG に変換する。local_dirs にブラウザで「Web ページ、完全」で保存した
    フォルダを渡すと、同じファイル名の画像をそこから使う (サイトから取れないとき用)。
    戻り値: {"new": 新しく保存, "have": 保存済み, "failed": 取れなかった, "reason": 最初の失敗の理由}
    """
    import hashlib
    from pathlib import Path
    from urllib.parse import urljoin

    import cv2
    import numpy as np

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    local = {}
    for d in local_dirs:
        for f in Path(d).rglob("*"):
            if f.suffix.lower() in (".webp", ".png", ".jpg", ".jpeg", ".gif", ".bmp"):
                local.setdefault(f.name.lower(), f)
    stats = {"new": 0, "have": 0, "failed": 0, "reason": ""}
    done: dict[str, str] = {}
    session = None
    for r in rows:
        src = r.get("icon") or ""
        if not src:
            r["icon"] = ""
            continue
        url = urljoin(base_url, src)
        if url in done:
            r["icon"] = done[url]
            continue
        fname = hashlib.sha1(url.encode()).hexdigest()[:16] + ".png"
        path = out_dir / fname
        rel = f"{out_dir.name}/{fname}"
        if path.exists() and path.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":  # 前の版が webp のまま保存したもの
            old = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_UNCHANGED)
            if old is not None:
                cv2.imwrite(str(path), old)
            else:
                path.unlink()
        if path.exists():
            stats["have"] += 1
            done[url] = r["icon"] = rel
            continue
        data, why = None, ""
        name = Path(url.split("?")[0]).name.lower()
        if name in local:
            data = local[name].read_bytes()
        else:
            try:
                import requests

                session = session or requests.Session()
                resp = session.get(url, timeout=15, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/126.0 Safari/537.36",
                    "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8", "Referer": base_url})
                resp.raise_for_status()
                data = resp.content
            except Exception as e:
                why = f"{url}: {e}"
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED) if data else None
        if img is None:
            stats["failed"] += 1
            if not stats["reason"]:
                stats["reason"] = why or f"{url}: 画像として読めません (先頭 {data[:40]!r})"
            done[url] = r["icon"] = ""
            continue
        cv2.imwrite(str(path), img)  # PNG で保存 (webp も変換)
        stats["new"] += 1
        done[url] = r["icon"] = rel
    return stats
