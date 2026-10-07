"""DB に登録する前に、読み取った情報 (相手の名前・現在地) が正しそうかを判定する小さな AI (PC 側)。

文字認識はときどき読み違える。読み違いをそのまま DB (mu_map.db) に入れると、ビューアの
モンスター一覧や足跡が汚れる。そこで「この読みが正しい確率」を出すネットで振り分ける:
  score >= ok   → そのまま登録 (status 'ok')
  score <  ng   → 登録しない (status 'ng'。記録だけ残す)
  それ以外      → 保留 (status 'pending')。ビューアの「目撃一覧」で人が承認 / 却下する

入力 (特徴量) は文字の画像ではなく「読みの確からしさ」:
  文字認識の確信度、辞書の語との近さ、文字の並び (英字・数字・記号の割合、母音の割合、単語の頭が大文字か)、
  直前の数回で同じ読みが続いたか、レベル・HP バーが読めたか、
  現在地なら マップ名が既知か・座標が 0..255 か・前の位置からの飛び・先生役の OCR (Windows) との一致
学習は事前に PC で行う (tools/record_filter.py train)。最初は「読み違いの起こり方」をまねた合成データで学び、
ビューアで人が承認 / 却下した記録 (sightings 表) がたまったら、それも混ぜて学習し直す。
モデルは models/record_filter.npz (numpy だけで動く 2 層ネット)。
"""
from __future__ import annotations

import difflib
import json
import random
import string
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import PC_DIR

MODEL = PC_DIR / "models" / "record_filter.npz"
FEATURES = ["is_loc", "conf", "lex_ratio", "in_lex", "length", "alpha", "digit", "junk", "vowel", "caps",
            "repeat", "level_ok", "hp_ok", "map_known", "coord_ok", "jump", "teacher"]
NF = len(FEATURES)
VOWELS = set("aeiouyAEIOUY")


@dataclass
class Candidate:
    """判定する 1 件の読み。kind は 'monster' か 'location'。"""
    kind: str
    raw: str                    # 文字認識の読みそのまま
    text: str                   # 辞書で直した後 (登録する文字列)
    conf: float = 0.0           # 文字認識の確信度 (各文字の確率の最小値, 0..1)
    in_lex: bool = False        # 辞書の語か
    repeat: float = 0.0         # 直前の数回で同じ読みだった割合 (0..1)
    level: str = ""
    hp_ok: bool = False
    map_known: bool = False
    coord_ok: bool = False
    jump: float = 0.0           # 前に採用した位置からの飛び (マス)。別マップなら 99
    teacher: float | None = None  # 先生役の OCR と一致 1 / 不一致 0 / 先生なし None
    extra: dict = field(default_factory=dict)


def lex_ratio(raw: str, text: str) -> float:
    a, b = " ".join(raw.split()).lower(), text.lower()
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def features(c: Candidate) -> np.ndarray:
    s = c.raw.replace(" ", "")
    n = max(1, len(s))
    letters = [ch for ch in s if ch.isalpha()]
    words = c.raw.split()
    lv_ok = c.level.isdigit() and 1 <= int(c.level) <= 400
    return np.array([
        1.0 if c.kind == "location" else 0.0,
        float(np.clip(c.conf, 0, 1)),
        lex_ratio(c.raw, c.text),
        1.0 if c.in_lex else 0.0,
        min(len(s), 30) / 30,
        len(letters) / n,
        sum(ch.isdigit() for ch in s) / n,
        sum(not (ch.isalnum() or ch in "-'(),.") for ch in s) / n,
        sum(ch in VOWELS for ch in letters) / max(1, len(letters)),
        sum(w[:1].isupper() for w in words) / max(1, len(words)),
        float(np.clip(c.repeat, 0, 1)),
        1.0 if lv_ok else 0.0,
        1.0 if c.hp_ok else 0.0,
        1.0 if c.map_known else 0.0,
        1.0 if c.coord_ok else 0.0,
        min(c.jump, 50) / 50,
        0.5 if c.teacher is None else float(c.teacher),
    ], np.float32)


# ------------------------------------------------------------ モデル --
@dataclass
class FilterModel:
    W1: np.ndarray
    b1: np.ndarray
    W2: np.ndarray
    b2: float

    def prob(self, X: np.ndarray) -> np.ndarray:
        X = np.atleast_2d(X).astype(np.float32)
        h = np.maximum(0, X @ self.W1 + self.b1)
        return 1 / (1 + np.exp(-(h @ self.W2 + self.b2)))

    def score(self, c: Candidate) -> float:
        return float(self.prob(features(c))[0])

    def save(self, path: Path = MODEL) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        np.savez(path, W1=self.W1, b1=self.b1, W2=self.W2, b2=np.float32(self.b2),
                 features=np.array(FEATURES))

    @classmethod
    def load(cls, path: Path = MODEL) -> "FilterModel":
        z = np.load(path)
        if list(z["features"]) != FEATURES:
            raise ValueError("特徴量の並びが違うモデルです。tools/record_filter.py train で作り直してください")
        return cls(z["W1"], z["b1"], z["W2"], float(z["b2"]))


def train_model(X: np.ndarray, y: np.ndarray, w: np.ndarray | None = None, hidden: int = 24, epochs: int = 300,
                lr: float = 0.02, seed: int = 0) -> FilterModel:
    """2 層ネットを Adam で学習 (全件で 1 ステップ。データは数万件までなので十分速い)。"""
    rng = np.random.default_rng(seed)
    X = X.astype(np.float32)
    y = y.astype(np.float32)
    w = np.ones_like(y) if w is None else w.astype(np.float32)
    w = w / w.mean()
    p = {"W1": rng.normal(0, 0.5, (X.shape[1], hidden)).astype(np.float32), "b1": np.zeros(hidden, np.float32),
         "W2": rng.normal(0, 0.3, hidden).astype(np.float32), "b2": np.zeros(1, np.float32)}
    m = {k: np.zeros_like(v) for k, v in p.items()}
    v = {k: np.zeros_like(v) for k, v in p.items()}
    for t in range(1, epochs + 1):
        z1 = X @ p["W1"] + p["b1"]
        h = np.maximum(0, z1)
        out = 1 / (1 + np.exp(-(h @ p["W2"] + p["b2"])))
        d = (out - y) * w / len(y)
        dh = np.outer(d, p["W2"]) * (z1 > 0)
        g = {"W2": h.T @ d, "b2": np.array([d.sum()], np.float32), "W1": X.T @ dh, "b1": dh.sum(0)}
        g["W1"] += 1e-4 * p["W1"]
        for k in p:
            m[k] = 0.9 * m[k] + 0.1 * g[k]
            v[k] = 0.999 * v[k] + 0.001 * g[k] ** 2
            p[k] -= lr * (m[k] / (1 - 0.9 ** t)) / (np.sqrt(v[k] / (1 - 0.999 ** t)) + 1e-8)
    return FilterModel(p["W1"], p["b1"], p["W2"], float(p["b2"][0]))


# ------------------------------------------------- 合成データ (事前学習) --
CONFUSE = {"O": "0Q", "0": "OD", "I": "l1", "l": "I1", "1": "lI", "S": "5", "5": "S", "B": "8", "8": "B",
           "G": "6C", "e": "c", "c": "e", "n": "m", "m": "n", "rn": "m", "h": "b", "u": "v", "v": "u"}


def corrupt(s: str, k: int, rng: random.Random) -> str:
    """読み違いを k 回起こす (似た字への置き換え・抜け・余計な字・くっつき)。"""
    s = list(s)
    for _ in range(k):
        if not s:
            break
        i = rng.randrange(len(s))
        r = rng.random()
        if r < 0.45 and s[i] in CONFUSE:
            s[i] = rng.choice(CONFUSE[s[i]])
        elif r < 0.65:
            del s[i]
        elif r < 0.85:
            s.insert(i, rng.choice(string.ascii_letters + ".,'-:"))
        else:
            s[i] = rng.choice(string.ascii_letters)
    return "".join(s)


def _fix(text: str, words: list[str], cutoff: float = 0.75) -> tuple[str, bool]:
    from .edge.harvest import lexicon_fix

    return lexicon_fix(text, words, cutoff)


def synth_monster(rng: random.Random, words: list[str], monsters: list[str]) -> tuple[Candidate, int]:
    """相手の名前の読みを 1 件作る。戻り値の 2 番目が正解 (直した結果が本当の名前と同じなら 1)。"""
    kind = rng.random()
    if kind < 0.15:   # 名前ではないもの (赤い何かをバーと誤認・背景の模様)
        n = rng.randint(1, 14)
        raw = "".join(rng.choice(string.ascii_letters + string.digits + ".,:;'-|!_ ") for _ in range(n))
        truth = None
        conf = rng.uniform(0, 0.6)
    else:
        if rng.random() < 0.85:
            truth = rng.choice(monsters)
        else:  # 辞書に無い (サーバー独自の) 名前
            truth = " ".join(w.capitalize() for w in
                             ("".join(rng.choice("bcdfghklmnprstvz") + rng.choice("aeiou") for _ in range(rng.randint(2, 4)))
                              for _ in range(rng.randint(1, 2))))
        conf = rng.random() ** 0.6
        lam = 3.5 * (1 - conf) + 0.1   # 確信度が低いほど読み違いが多い
        k = int(np.random.default_rng(rng.randrange(1 << 30)).poisson(lam))
        raw = corrupt(truth, k, rng)
    text, in_lex = _fix(raw, words)
    ok = int(truth is not None and text == truth)
    # 正しい読みは続けて同じになりやすい。読み違いは毎回違うことが多い
    rep = rng.betavariate(5, 1.5) if ok else rng.betavariate(1.2, 4)
    lv = str(rng.randint(1, 150)) if rng.random() < (0.9 if truth else 0.3) else rng.choice(["", "7?", "1O"])
    c = Candidate("monster", raw, text, conf=conf, in_lex=in_lex, repeat=rep, level=lv,
                  hp_ok=rng.random() < (0.95 if truth else 0.5))
    return c, ok


def synth_location(rng: random.Random, maps: list[str]) -> tuple[Candidate, int]:
    from .maploc import KNOWN_MAPS, parse_location

    if rng.random() < 0.1:
        raw = "".join(rng.choice(string.ascii_letters + string.digits + " (),.") for _ in range(rng.randint(3, 18)))
        truth = None
        conf = rng.uniform(0, 0.5)
    else:
        truth = f"{rng.choice(maps)} ({rng.randint(0, 255)}, {rng.randint(0, 255)})"
        conf = rng.random() ** 0.5
        k = int(np.random.default_rng(rng.randrange(1 << 30)).poisson(2.5 * (1 - conf) + 0.05))
        raw = corrupt(truth, k, rng)
    loc = parse_location(raw)
    text = f"{loc.map} ({loc.x}, {loc.y})" if loc else ""
    ok = int(truth is not None and text == truth)
    if ok:
        jump = 0 if rng.random() < 0.5 else (rng.uniform(0, 4) if rng.random() < 0.9 else 99)  # たまにワープ
    else:
        jump = rng.choice([rng.uniform(0, 6), rng.uniform(10, 120), 99])
    teacher = None if rng.random() < 0.4 else float(rng.random() < (0.95 if ok else 0.08))
    c = Candidate("location", raw, text, conf=conf, in_lex=bool(loc and loc.map in KNOWN_MAPS),
                  repeat=rng.betavariate(4, 1.5) if ok else rng.betavariate(1.2, 3),
                  map_known=bool(loc and loc.map in KNOWN_MAPS), coord_ok=loc is not None, jump=jump,
                  teacher=teacher)
    return c, ok


def synth_dataset(n: int, words: list[str], seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    from .maploc import KNOWN_MAPS

    rng = random.Random(seed)
    monsters = [w for w in words if w not in KNOWN_MAPS] or ["Spider"]
    X, y = [], []
    for i in range(n):
        c, ok = synth_location(rng, KNOWN_MAPS) if i % 3 == 0 else synth_monster(rng, words, monsters)
        X.append(features(c))
        y.append(ok)
    return np.array(X, np.float32), np.array(y, np.int64)


def reviewed_dataset(db_path: Path) -> tuple[np.ndarray, np.ndarray]:
    """ビューアで人が承認 (approved) / 却下 (rejected) した目撃記録。特徴量は記録時に features 列に保存してある。"""
    import sqlite3

    X, y = [], []
    if not Path(db_path).exists():
        return np.zeros((0, NF), np.float32), np.zeros(0, np.int64)
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute("SELECT features, status FROM sightings WHERE reviewed=1 AND features IS NOT NULL").fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    for f, st in rows:
        try:
            v = json.loads(f)
        except ValueError:
            continue
        if len(v) == NF:
            X.append(v)
            y.append(1 if st == "ok" else 0)
    return np.array(X, np.float32).reshape(-1, NF), np.array(y, np.int64)


class RecordFilter:
    """読み込んだモデルで振り分ける。モデルが無ければ単純な規則で代わりに判定する。"""

    def __init__(self, model: FilterModel | None = None, ok: float = 0.8, ng: float = 0.3):
        self.model = model
        self.ok = ok
        self.ng = ng

    @classmethod
    def load(cls, path: Path = MODEL, **kw) -> "RecordFilter":
        try:
            return cls(FilterModel.load(path), **kw)
        except (FileNotFoundError, OSError, ValueError, KeyError):
            return cls(None, **kw)

    def score(self, c: Candidate) -> float:
        if self.model is not None:
            return self.model.score(c)
        f = features(c)  # モデルが無いとき: 辞書の語で、確信度があり、続けて同じなら正しいとみなす
        return float(np.clip(0.4 * f[3] + 0.3 * f[1] + 0.3 * f[10], 0, 1))

    def judge(self, c: Candidate) -> tuple[str, float]:
        s = self.score(c)
        return ("ok" if s >= self.ok else "ng" if s < self.ng else "pending"), s
