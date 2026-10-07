"""実画面から「正解付きの文字の行」を自動で集める (文字認識の学習を回し続けるため)。

自分のモデルの読みをそのまま正解にすると、読み間違いを覚えてしまう。そこで次の確認を通ったものだけ使う:
  * 現在地の行 (画面右下 "Devias (241, 85)"): 書式 (マップ名 (x, y)) どおりで、マップ名が既知のもの。
    Windows の OCR (先生) があれば先生の読みで正解を決め、無ければ自分の読みが 3 回続けて同じときだけ
  * 相手の名前 (攻撃時に画面上部): 辞書 (akm/data/lexicon.txt) の語に十分近いもの。
    先生があれば、先生の読みも同じ語になるときだけ
集めた行は dataset/ocr_lines/ (labels.tsv) に保存し、tools/edge_train.py train が次の学習で使う。
同じ文字列は max_same 本まで。5 本に 1 本は評価用 (test) に取っておく。
"""
from __future__ import annotations

import difflib
import time
from pathlib import Path

import cv2
import numpy as np

from ..maploc import KNOWN_MAPS, parse_location, preprocess
from ..vision import roi_px
from .synth import CHARS

LEXICON = Path(__file__).resolve().parent.parent / "data" / "lexicon.txt"


def load_lexicon(path: Path = LEXICON) -> list[str]:
    words = list(KNOWN_MAPS)
    if Path(path).exists():
        words += [w.strip() for w in Path(path).read_text(encoding="utf-8").splitlines()
                  if w.strip() and not w.startswith("#")]
    return sorted(set(words))


def lexicon_fix(text: str, words: list[str], cutoff: float = 0.75) -> tuple[str, bool]:
    """辞書の語に十分近ければその語に直す。戻り値: (文字列, 辞書の語か)。"""
    t = " ".join(text.split())
    if not t:
        return t, False
    if t in words:
        return t, True
    lower = {w.lower(): w for w in words}
    m = difflib.get_close_matches(t.lower(), list(lower), n=1, cutoff=cutoff)
    return (lower[m[0]], True) if m else (t, False)


class OcrHarvester:
    def __init__(self, out_dir: Path, ocr, teacher=None, loc_roi=(0.86, 0.955, 0.14, 0.045),
                 every_s: float = 20.0, max_same: int = 15, log=print, clock=time.monotonic):
        self.out = Path(out_dir)
        self.ocr = ocr                # akm.edge.ocr.OcrReader
        self.teacher = teacher        # (画像) → 文字列 (Windows の OCR 等)。無ければ None
        self.loc_roi = loc_roi
        self.every_s = every_s
        self.max_same = max_same
        self.log = log
        self.clock = clock
        self.words = load_lexicon()
        self._last = {"loc": -1e9, "name": -1e9}
        self._loc_hist: list[str] = []
        self.saved = 0
        self._counts: dict[str, int] = {}
        self._load_counts()

    def _load_counts(self) -> None:
        tsv = self.out / "labels.tsv"
        if tsv.exists():
            for row in tsv.read_text(encoding="utf-8").splitlines():
                if row and not row.startswith("#"):
                    t = row.split("\t", 2)[-1]
                    self._counts[t] = self._counts.get(t, 0) + 1

    def _teach(self, crop: np.ndarray) -> str | None:
        if self.teacher is None:
            return None
        try:
            return self.teacher(preprocess(crop, 3, "bright", 130)).strip()
        except Exception:
            return None

    def _save(self, crop: np.ndarray, text: str, kind: str) -> bool:
        if not text or any(c not in CHARS for c in text.replace(" ", "")):
            return False
        if self._counts.get(text, 0) >= self.max_same:
            return False
        self.out.mkdir(parents=True, exist_ok=True)
        tsv = self.out / "labels.tsv"
        if not tsv.exists():
            tsv.write_text("# 画像\t用途\t正解の文字列\n", encoding="utf-8")
        n = len(list(self.out.glob("auto_*.png"))) + 1
        name = f"auto_{n:05d}.png"
        cv2.imwrite(str(self.out / name), crop)
        split = "test" if n % 5 == 0 else "train"
        with open(tsv, "a", encoding="utf-8") as f:
            f.write(f"{name}\t{split}\t{text}\n")
        self._counts[text] = self._counts.get(text, 0) + 1
        self.saved += 1
        self.log(f"[harvest] 見本を保存 ({kind}, {split}): {text}")
        return True

    def location(self, img: np.ndarray) -> str | None:
        x0, y0, x1, y1 = roi_px(img.shape, self.loc_roi)
        crop = img[y0:y1, x0:x1]
        if crop.size == 0:
            return None
        ours = parse_location(self.ocr.read(crop)[0])
        teach = self._teach(crop)
        loc = parse_location(teach) if teach is not None else None
        if teach is None:  # 先生がいない: 自分の読みが 3 回続けて同じときだけ
            key = f"{ours.map} ({ours.x}, {ours.y})" if ours else ""
            self._loc_hist = (self._loc_hist + [key])[-3:]
            loc = ours if key and self._loc_hist.count(key) == 3 else None
        if loc is None or loc.map not in KNOWN_MAPS:
            return None
        text = f"{loc.map} ({loc.x}, {loc.y})"
        return text if self._save(crop, text, "現在地") else None

    def target_name(self, img: np.ndarray, target) -> str | None:
        if target is None:
            return None
        x, y, w, h = target.name_box
        crop = img[y:y + h, x:x + w]
        if crop.size == 0:
            return None
        word, ok = lexicon_fix(target.name, self.words, cutoff=0.8)
        if not ok:
            return None
        teach = self._teach(crop)
        if teach is not None and lexicon_fix(teach, self.words, cutoff=0.8)[0] != word:
            return None
        return word if self._save(crop, word, "相手の名前") else None

    def feed(self, img: np.ndarray, target=None) -> None:
        now = self.clock()
        if now - self._last["loc"] >= self.every_s:
            self._last["loc"] = now
            self.location(img)
        if target is not None and now - self._last["name"] >= self.every_s:
            self._last["name"] = now
            self.target_name(img, target)
