"""MU Helper 動作中に出る「Instant Hunting Log」窓を画面から見つける。

窓はマウスで動かせるので位置は決め打ちせず、画面全体からテンプレート (赤いタイトルバーの文字と、
左側の項目名の列) を探す。どちらかが閾値以上で見つかれば「表示中 = MU Helper 動作中」。

学習 (閾値の決め方):
  tools/helper_cycle.py --collect on / --collect off で、表示中・非表示の画面を dataset/hunting_log/ に集め、
  --calibrate で両者を一番よく分ける閾値を求めて templates/hunting_log.json に保存する。
  窓の見た目が違う (解像度・UI 倍率) ときは --make-template で集めた画面からテンプレートを作り直す。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

DEFAULT_TEMPLATES = ("templates/hunting_log_title.png", "templates/hunting_log_labels.png")
CALIB_FILE = "templates/hunting_log.json"


@dataclass
class Match:
    visible: bool
    score: float               # 一番よく合ったテンプレートの一致度 (0..1)
    pos: tuple[int, int] | None  # その左上 (画面のピクセル)


class HuntingLogDetector:
    def __init__(self, base_dir: Path, templates=DEFAULT_TEMPLATES, threshold: float | None = None,
                 scale: float = 0.5):
        self.base = Path(base_dir)
        self.scale = scale  # 速さのため縮小して探す (0.5 = 1680x1050 → 840x525)
        self.templates: list[np.ndarray] = []
        for t in templates:
            p = self.base / t
            img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
            if img is None:
                raise FileNotFoundError(f"テンプレートが読めません: {p}")
            self.templates.append(self._small(img))
        calib = self.base / CALIB_FILE
        saved = None
        if calib.exists():
            try:
                saved = float(json.loads(calib.read_text(encoding="utf-8"))["threshold"])
            except Exception:
                saved = None
        self.threshold = float(threshold if threshold is not None else (saved if saved is not None else 0.7))

    def _small(self, gray: np.ndarray) -> np.ndarray:
        if self.scale == 1.0:
            return gray
        return cv2.resize(gray, None, fx=self.scale, fy=self.scale, interpolation=cv2.INTER_AREA)

    def score(self, img: np.ndarray) -> Match:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        g = self._small(gray)
        best, pos = -1.0, None
        for t in self.templates:
            if t.shape[0] > g.shape[0] or t.shape[1] > g.shape[1]:
                continue
            res = cv2.matchTemplate(g, t, cv2.TM_CCOEFF_NORMED)
            _, s, _, loc = cv2.minMaxLoc(res)
            if s > best:
                best, pos = float(s), (int(loc[0] / self.scale), int(loc[1] / self.scale))
        return Match(best >= self.threshold, best, pos)

    def visible(self, img: np.ndarray) -> bool:
        return self.score(img).visible


def calibrate(det: HuntingLogDetector, on_dir: Path, off_dir: Path) -> dict:
    """集めた画面 (表示中 / 非表示) の一致度から閾値を決める。"""
    def scores(d: Path) -> list[float]:
        out = []
        for p in sorted(d.glob("*.png")):
            img = cv2.imread(str(p))
            if img is not None:
                out.append(det.score(img).score)
        return out

    on, off = scores(on_dir), scores(off_dir)
    if not on or not off:
        raise SystemExit(f"画面が足りません (表示中 {len(on)} 枚 / 非表示 {len(off)} 枚)。--collect on / off で集めてください")
    lo, hi = max(off), min(on)
    thr = (lo + hi) / 2 if hi > lo else hi - 0.01
    errors = sum(s < thr for s in on) + sum(s >= thr for s in off)
    return {"threshold": round(thr, 3), "on_min": round(hi, 3), "off_max": round(lo, 3),
            "on": len(on), "off": len(off), "errors": errors, "separated": hi > lo}
