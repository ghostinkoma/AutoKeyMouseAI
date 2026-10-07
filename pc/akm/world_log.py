"""認識した「現在地」と「相手の名前」を、ビューア (Java) と同じ DB (mu_map.db) に記録する。

  現在地 (画面右下 "Devias (241, 85)") → maps / positions / cells / events (map_logger.py と同じ記録)
  相手の名前 (攻撃時に画面上部)          → sightings (目撃記録: いつ・どこで・何を・Lv・判定)
                                          正しいと判定したものは monsters にも足す (ビューアの一覧に出る)
どちらも登録の前に akm/record_filter.py の AI で「正しい読みか」を判定する:
  ok      → 登録する
  pending → sightings に保留として残す (ビューアの「目撃一覧」で承認 / 却下。それが次の学習の正解になる)
  ng      → 登録しない (sightings には ng として残す。現在地は捨てる)
同じ相手を殴り続けている間は 1 件にまとめる (reads が増え、最後に見た時刻と HP の最小値を更新)。
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
                 here_s: float = 15.0, words: list[str] | None = None, log=print, clock=time.time):
        self.db_path = Path(db_path)
        self.filt = filt or RecordFilter.load()
        self.teacher = teacher          # (画像) → 文字列 (Windows の OCR 等)。無ければ None
        self.loc_roi = loc_roi
        self.loc_every = loc_every
        self.merge_s = merge_s          # この秒数以内に同じ相手を読んだら同じ目撃にまとめる
        self.here_s = here_s            # 現在地がこの秒数以内に読めていれば目撃の場所にする
        self.log = log
        self.clock = clock
        self._words = words
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

    @property
    def words(self) -> list[str]:
        """辞書: lexicon.txt + マップ名 + ビューアで承認した名前。"""
        if self._words is None:
            from .edge.harvest import load_lexicon

            w = set(load_lexicon())
            if self.map is not None:
                try:
                    w |= {r[0] for r in self.map.db.execute(
                        "SELECT DISTINCT monster FROM sightings WHERE status='ok' AND reviewed=1")}
                except sqlite3.Error:
                    pass
            self._words = sorted(w)
        return self._words

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
        loc = parse_location(raw)
        teach = None
        if self.teacher is not None:
            try:
                t = self.teacher(preprocess(crop, 3, "bright", 130)).strip()
                tl = parse_location(t)
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
        c = Candidate("location", raw, text, conf=conf, in_lex=loc.map in KNOWN_MAPS,
                      repeat=self._loc_hist.count(text) / len(self._loc_hist), map_known=loc.map in KNOWN_MAPS,
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
        c = Candidate("monster", raw, text, conf=getattr(target, "conf", 0.0), in_lex=in_lex,
                      repeat=self._name_hist.count(text) / len(self._name_hist), level=target.level or "",
                      hp_ok=target.hp_ratio is not None)
        status, score = self.filt.judge(c)
        now = self.clock()
        level = int(target.level) if target.level.isdigit() and 1 <= int(target.level) <= 400 else None
        here = self.current()
        db = self.open().db
        cur = self._cur
        if cur is not None and cur["monster"] == text and now - cur["last_ts"] <= self.merge_s:
            # 同じ相手: まとめる。判定は良い方を残す (続けて同じに読めるほど repeat が上がり確からしくなる)
            cur["last_ts"] = now
            better = score > cur["score"]
            if better:
                cur["score"], cur["status"] = score, status
            db.execute("UPDATE sightings SET last_ts=?, reads=reads+1, hp_min=MIN(COALESCE(hp_min, 1), ?), "
                       "level=COALESCE(level, ?), score=MAX(score, ?), status=CASE WHEN reviewed=1 THEN status ELSE ? END, "
                       "features=CASE WHEN ? THEN ? ELSE features END WHERE id=?",
                       (now, target.hp_ratio if target.hp_ratio is not None else 1, level, score, cur["status"],
                        better, json.dumps([round(float(v), 4) for v in features(c)]), cur["id"]))
            became_ok = better and status == "ok" and not cur["added"]
        else:
            r = db.execute("INSERT INTO sightings(ts, last_ts, monster, raw, level, map, x, y, hp_min, reads, score, status, "
                           "source, features) VALUES (?,?,?,?,?,?,?,?,?,1,?,?, 'live', ?)",
                           (now, now, text, raw, level, here.map if here else None, here.x if here else None,
                            here.y if here else None, target.hp_ratio, score, status,
                            json.dumps([round(float(v), 4) for v in features(c)])))
            self._cur = cur = {"id": r.lastrowid, "monster": text, "last_ts": now, "score": score, "status": status,
                               "added": False}
            self.stats[status] += 1
            became_ok = status == "ok"
            mark = {"ok": "登録", "pending": "保留", "ng": "除外"}[status]
            self.log(f"[world] 目撃 {mark} ({score:.2f}): {text}" + (f" Lv{level}" if level else "")
                     + (f"  @ {here.map} ({here.x}, {here.y})" if here else "") + (f"  (読み {raw!r})" if raw != text else ""))
        if became_ok:
            add_monster_from_sighting(db, text, level, here.map if here else None)
            cur["added"] = True
        db.commit()
        self.last_judge = (cur["status"], cur["score"])
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
