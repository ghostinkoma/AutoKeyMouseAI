"""認識した「現在地」と「相手の名前」を、ビューア (Java) と同じ DB (mu_map.db) に記録する。

  現在地 (画面右下 "Devias (241, 85)") → maps / positions / cells / events (map_logger.py と同じ記録)
  相手の名前 (攻撃時に画面上部)          → sightings (目撃記録: いつ・どこで・何を・Lv・判定)
                                          正しいと判定したものは monsters にも足す (ビューアの一覧に出る)
どちらも登録の前に akm/record_filter.py の AI で「正しい読みか」を判定する:
  ok      → 登録する (辞書に無い新しいモンスター名は ok でも保留にして人が確かめる)
  pending → sightings に保留として残す (ビューアの「目撃一覧」で承認 / 却下。それが次の学習の正解になる)
  ng      → 登録しない (sightings には ng として残す。現在地は捨てる)
目撃は「モンスター × マップ × 範囲」で 1 件。最初に見た場所から 10 マス (area_cells) 未満なら同じ記録にまとめ
(reads が増え、最後に見た時刻と HP の最小値を更新)、10 マス以上離れたら出現モンスターが変わる別の範囲なので新しい記録にする。

知らないマップ (KNOWN_MAPS に無い名前) は位置を記録せず、map_names 表に「新しいマップの候補」(pending) として残す。
ビューアの「新しいマップ」タブで承認されたら (または手で登録されたら) 既知のマップとして記録を始める。
ビューアで承認・登録したモンスター名・マップ名は refresh_s 秒ごとに読み直して辞書に入れる (再起動は要らない)。
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import deque
from pathlib import Path

from .maploc import KNOWN_MAPS, Location, MapDB, parse_location, preprocess
from .record_filter import Candidate, RecordFilter, features
from .vision import roi_px

SIGHTINGS = """
CREATE TABLE IF NOT EXISTS sightings (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, last_ts REAL NOT NULL, monster TEXT NOT NULL, raw TEXT,
    level INTEGER, map TEXT, x INTEGER, y INTEGER, hp_min REAL, reads INTEGER NOT NULL DEFAULT 1,
    score REAL, status TEXT NOT NULL DEFAULT 'pending', reviewed INTEGER NOT NULL DEFAULT 0,
    source TEXT, features TEXT);
CREATE INDEX IF NOT EXISTS sightings_ts ON sightings(ts);
CREATE INDEX IF NOT EXISTS sightings_status ON sightings(status);
CREATE TABLE IF NOT EXISTS map_names (
    name TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending', reviewed INTEGER NOT NULL DEFAULT 0, raw TEXT,
    reads INTEGER NOT NULL DEFAULT 0, first_seen REAL, last_seen REAL, x INTEGER, y INTEGER);
"""


def ensure_sightings(db: sqlite3.Connection) -> None:
    db.executescript(SIGHTINGS)


def add_monster_from_sighting(db: sqlite3.Connection, name: str, level: int | None, map_en: str | None) -> None:
    """目撃で確かめたモンスターを monsters に足す (手で登録したものは書き換えない)。"""
    from .monsters import ensure_schema

    ensure_schema(db)
    m = map_en or ""
    db.execute(
        "INSERT INTO monsters(name, level, map, map_en, source, updated) VALUES (?,?,?,?, 'sighting', ?) "
        "ON CONFLICT(name, map) DO UPDATE SET level=COALESCE(monsters.level, excluded.level), "
        "updated=excluded.updated", (name, level, m, m, time.time()))


class WorldLogger:
    def __init__(self, db_path: str | Path, filt: RecordFilter | None = None, teacher=None,
                 loc_roi=(0.86, 0.955, 0.14, 0.045), loc_every: float = 1.0, merge_s: float = 8.0,
                 here_s: float = 15.0, words: list[str] | None = None, log=print, clock=time.time,
                 refresh_s: float = 30.0, area_cells: int = 10):
        self.db_path = Path(db_path)
        self.filt = filt or RecordFilter.load()
        self.teacher = teacher          # (画像) → 文字列 (Windows の OCR 等)。無ければ None
        self.loc_roi = loc_roi
        self.loc_every = loc_every
        self.merge_s = merge_s          # 場所が分からないとき: この秒数以内に同じ相手を読んだら同じ目撃にまとめる
        self.area_cells = area_cells    # 同じ範囲とみなす広さ。最初に見た場所から area マス以上離れたら別の記録
        self.here_s = here_s            # 現在地がこの秒数以内に読めていれば目撃の場所にする
        self.log = log
        self.clock = clock
        self._fixed_words = words
        self._words: list[str] | None = None
        self._maps: list[str] = list(KNOWN_MAPS)
        self.refresh_s = refresh_s
        self._lists_ts = -1e9
        self.map: MapDB | None = None   # 認識のスレッドで開く (SQLite は開いたスレッドで使う)
        self.here: Location | None = None
        self.here_ts = 0.0
        self._last_loc_read = -1e9
        self._loc_hist: deque[str] = deque(maxlen=5)
        self._name_hist: deque[str] = deque(maxlen=6)
        self._cur: dict | None = None   # まとめ中の目撃
        self.stats = {"loc_ok": 0, "loc_ng": 0, "ok": 0, "pending": 0, "ng": 0}
        self.last_judge: tuple[str, float] | None = None

    # ------------------------------------------------------------ 準備
    def open(self) -> MapDB:
        if self.map is None:
            self.map = MapDB(self.db_path)
            ensure_sightings(self.map.db)
            self.map.db.commit()
            self.here = self.map.latest()
            self.here_ts = 0.0
        return self.map

    def _refresh_lists(self) -> None:
        """辞書と既知のマップを DB (ビューアで承認・登録したもの) から読み直す。"""
        now = self.clock()
        if self._words is not None and now - self._lists_ts < self.refresh_s:
            return
        self._lists_ts = now
        from .edge.harvest import load_lexicon

        db = self.open().db
        maps = set(KNOWN_MAPS)
        try:
            maps |= {r[0] for r in db.execute("SELECT name FROM map_names WHERE status='ok'")}
        except sqlite3.Error:
            pass
        self._maps = sorted(maps)
        if self._fixed_words is not None:
            self._words = sorted(set(self._fixed_words) | maps)
            return
        w = set(load_lexicon()) | maps
        for sql in ("SELECT DISTINCT monster FROM sightings WHERE status='ok' AND reviewed=1",
                    "SELECT DISTINCT name FROM monsters WHERE source IN ('manual', 'sighting')"):
            try:
                w |= {r[0] for r in db.execute(sql) if r[0] and r[0].isascii()}  # 日本語名は画面に出ないので除く
            except sqlite3.Error:
                pass
        self._words = sorted(w)

    @property
    def words(self) -> list[str]:
        """辞書: lexicon.txt + マップ名 + ビューアで承認・登録した名前。"""
        self._refresh_lists()
        return self._words

    @property
    def known_maps(self) -> list[str]:
        """KNOWN_MAPS + ビューアで承認・登録したマップ。"""
        self._refresh_lists()
        return self._maps

    def reload_lists(self) -> None:
        self._words = None

    def _new_map(self, loc: Location, raw: str) -> None:
        """知らないマップ名: 続けて同じに読めたら候補として残す (人がビューアで承認する)。"""
        name = loc.map
        if sum(t.startswith(name + " (") for t in self._loc_hist) < 3:
            return
        if len(name) < 3 or sum(ch.isalpha() for ch in name) < 0.8 * len(name.replace(" ", "")):
            return
        db = self.open().db
        now = self.clock()
        row = db.execute("SELECT status FROM map_names WHERE name=?", (name,)).fetchone()
        db.execute("INSERT INTO map_names(name, status, raw, reads, first_seen, last_seen, x, y) VALUES (?, 'pending', ?, 1, ?, ?, ?, ?) "
                   "ON CONFLICT(name) DO UPDATE SET reads=reads+1, last_seen=excluded.last_seen, x=excluded.x, y=excluded.y",
                   (name, raw, now, now, loc.x, loc.y))
        db.commit()
        if row is None:
            self.log(f"[world] 知らないマップ「{name}」を新しいマップの候補にしました (ビューアの「新しいマップ」で承認すると記録を始めます)")

    def current(self) -> Location | None:
        """最近読めた現在地 (古ければ None)。"""
        return self.here if self.here is not None and self.clock() - self.here_ts <= self.here_s else None

    # ---------------------------------------------------------- 現在地
    def location(self, img, ocr) -> Location | None:
        x0, y0, x1, y1 = roi_px(img.shape, self.loc_roi)
        crop = img[y0:y1, x0:x1]
        if crop.size == 0:
            return None
        raw, conf = ocr.read(crop) if ocr is not None else ("", 0.0)
        maps = self.known_maps
        loc = parse_location(raw, maps)
        teach = None
        if self.teacher is not None:
            try:
                t = self.teacher(preprocess(crop, 3, "bright", 130)).strip()
                tl = parse_location(t, maps)
                if loc is None and tl is not None:  # 自分では読めなかった: 先生の読みを候補にする
                    raw, loc, conf = t, tl, 0.5
                    teach = 1.0
                else:
                    teach = float(tl is not None and tl == loc)
            except Exception:
                teach = None
        if loc is None:
            return None
        text = f"{loc.map} ({loc.x}, {loc.y})"
        self._loc_hist.append(text)
        if self.here is None:
            jump = 99.0
        elif self.here.map != loc.map:
            jump = 99.0
        else:
            jump = float(max(abs(loc.x - self.here.x), abs(loc.y - self.here.y)))
        if loc.map not in maps:
            self._new_map(loc, raw)
            self.stats["loc_ng"] += 1
            return None
        c = Candidate("location", raw, text, conf=conf, in_lex=True,
                      repeat=self._loc_hist.count(text) / len(self._loc_hist), map_known=True,
                      coord_ok=True, jump=jump, teacher=teach)
        status, score = self.filt.judge(c)
        if status != "ok":
            self.stats["loc_ng"] += 1
            return None
        self.stats["loc_ok"] += 1
        kind = self.open().record(loc)
        if kind == "map_change":
            self.log(f"[world] マップ: {loc.map} ({loc.x}, {loc.y})")
        self.here, self.here_ts = loc, self.clock()
        return loc

    # ------------------------------------------------------------ 相手
    def sighting(self, target) -> tuple[str, float] | None:
        if target is None or not (target.raw_name or target.name):
            return None
        from .edge.harvest import lexicon_fix

        raw = target.raw_name or target.name
        text, in_lex = lexicon_fix(raw, self.words)
        self._name_hist.append(text)
        streak = 0  # 直前から続けて同じに読めた回数 (前の相手の読みで薄まらないように)
        for t in reversed(self._name_hist):
            if t != text:
                break
            streak += 1
        c = Candidate("monster", raw, text, conf=getattr(target, "conf", 0.0), in_lex=in_lex,
                      repeat=max(streak / 4, self._name_hist.count(text) / len(self._name_hist)), level=target.level or "",
                      hp_ok=target.hp_ratio is not None)
        status, score = self.filt.judge(c)
        if not in_lex and status == "ok":
            status = "pending"  # 辞書に無い (新しい) 名前は自動で登録せず、人に確かめてもらう
        now = self.clock()
        level = int(target.level) if target.level.isdigit() and 1 <= int(target.level) <= 400 else None
        here = self.current()
        db = self.open().db
        area = self.area_cells
        feat = json.dumps([round(float(v), 4) for v in features(c)])
        cur = self._cur
        recent = cur is not None and cur["monster"] == text and now - cur["last_ts"] <= self.merge_s
        row = None
        if here is not None:
            # 同じモンスターを、同じマップの area マス未満の範囲で見たことがあれば同じ記録 (範囲の中心は最初に見た場所)
            row = db.execute("SELECT id, status FROM sightings WHERE monster=? AND map=? AND x IS NOT NULL "
                             "AND ABS(x-?)<? AND ABS(y-?)<? ORDER BY ABS(x-?)+ABS(y-?) LIMIT 1",
                             (text, here.map, here.x, area, here.y, area, here.x, here.y)).fetchone()
            if row is None and recent:  # 場所が分からないうちに作った記録に、分かった場所を入れる
                r0 = db.execute("SELECT id, status FROM sightings WHERE id=? AND map IS NULL", (cur["id"],)).fetchone()
                if r0 is not None:
                    db.execute("UPDATE sightings SET map=?, x=?, y=? WHERE id=?", (here.map, here.x, here.y, r0[0]))
                    row = r0
        elif recent:  # 場所が分からないときは、続けて読めている間だけまとめる
            row = db.execute("SELECT id, status FROM sightings WHERE id=?", (cur["id"],)).fetchone()
        if row is not None:
            # 同じ記録にまとめる。判定は一番良い読みのものを残す (人が判定したものはそのまま)
            sid, before = row
            db.execute("UPDATE sightings SET last_ts=?, reads=reads+1, hp_min=MIN(COALESCE(hp_min, 1), ?), "
                       "level=COALESCE(level, ?), "
                       "status=CASE WHEN reviewed=1 OR ? <= score THEN status ELSE ? END, "
                       "features=CASE WHEN reviewed=0 AND ? > score THEN ? ELSE features END, "
                       "score=MAX(score, ?) WHERE id=?",
                       (now, target.hp_ratio if target.hp_ratio is not None else 1, level, score, status,
                        score, feat, score, sid))
        else:
            before = None
            r = db.execute("INSERT INTO sightings(ts, last_ts, monster, raw, level, map, x, y, hp_min, reads, score, status, "
                           "source, features) VALUES (?,?,?,?,?,?,?,?,?,1,?,?, 'live', ?)",
                           (now, now, text, raw, level, here.map if here else None, here.x if here else None,
                            here.y if here else None, target.hp_ratio, score, status, feat))
            sid = r.lastrowid
            self.stats[status] += 1
            mark = {"ok": "登録", "pending": "保留", "ng": "除外"}[status]
            self.log(f"[world] 目撃 {mark} ({score:.2f}): {text}" + (f" Lv{level}" if level else "")
                     + (f"  @ {here.map} ({here.x}, {here.y}) から {area} マスの範囲" if here else "")
                     + (f"  (読み {raw!r})" if raw != text else ""))
        self._cur = cur = {"id": sid, "monster": text, "last_ts": now}
        st, sc, mp = db.execute("SELECT status, score, map FROM sightings WHERE id=?", (sid,)).fetchone()
        if st == "ok" and before != "ok":
            add_monster_from_sighting(db, text, level, mp)
        db.commit()
        self.last_judge = (st, sc)
        return self.last_judge

    def feed(self, img, rec, ocr) -> None:
        """LiveRecognizer から 1 回の認識ごとに呼ばれる。"""
        now = self.clock()
        if now - self._last_loc_read >= self.loc_every:
            self._last_loc_read = now
            self.location(img, ocr)
        if getattr(rec, "target", None) is not None:
            self.sighting(rec.target)

    def close(self) -> None:
        if self.map is not None:
            self.map.close()
            self.map = None
