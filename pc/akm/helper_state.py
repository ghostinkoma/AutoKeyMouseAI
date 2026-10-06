"""MU Helper が動いているかを画面から読む (動作中 / 停止中 / 不明)。

  0. 学習済みのボタン判定 (templates/helper_button.npz があれば最優先):
     ■/▶ ボタンは画面の決まった位置にあるので、そこを切り出し、集めた見本の
     「動作中の平均画像」「停止中の平均画像」のどちらに近いかで決める (tools/helper_cycle.py --calibrate で学習)。
     どちらにも似ていない (何かが重なっている) ときは次の方法へ。
  1. 左上の MU Helper パネル: 動作中は右端に ■ (停止ボタン)、停止中は ▶ (開始ボタン)。
     インベントリやステータス窓を開いても残るので、これを一番に使う。パネルは画面全体から探す。
  2. パネルが見つからないとき: 「Instant Hunting Log」窓が見えれば動作中。
     見えないだけでは停止中とは言えない (インベントリ等を開くとログ窓は消える) ので「不明」。

テンプレート (クライアント 1680x1050 の等倍):
  templates/helper_panel.png  "MU Helper" の文字
  templates/helper_on.png     動作中の ■ ボタン
  templates/helper_off.png    停止中の ▶ ボタン
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .hunting_log import HuntingLogDetector

# パネル文字の左上から見たボタンの位置 (1680x1050 で実測)
BUTTON_OFFSET = (159, -2)
BUTTON_MARGIN = 12
# ■/▶ ボタンの決まった位置 (クライアント 1680x1050 での x, y, 幅, 高さ。他の大きさでは比率で合わせる)
BUTTON_ROI = (275, 21, 28, 26)
BUTTON_MODEL = "templates/helper_button.npz"
REF_SIZE = (1680, 1050)


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    a = a.astype(np.float32).ravel() - a.mean()
    b = b.astype(np.float32).ravel() - b.mean()
    d = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b / d) if d > 1e-6 else 0.0


class ButtonModel:
    """決まった位置のボタンを、動作中 / 停止中の見本の平均と比べる最小の学習器。
    ESP32 に載せる場合もこの 2 枚 (28x26 のグレー) と閾値だけで済む。"""

    def __init__(self, mean_on: np.ndarray, mean_off: np.ndarray, roi=BUTTON_ROI, threshold: float = 0.6,
                 shift: int = 3):
        self.mean_on = mean_on.astype(np.float32)
        self.mean_off = mean_off.astype(np.float32)
        self.roi = tuple(int(v) for v in roi)
        self.threshold = float(threshold)  # 近い方の一致度がこれ未満なら「分からない」
        self.shift = shift                 # 数ピクセルのずれは許す

    @staticmethod
    def patch(img: np.ndarray, roi=BUTTON_ROI, dx: int = 0, dy: int = 0) -> np.ndarray | None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        h, w = gray.shape[:2]
        sx, sy = w / REF_SIZE[0], h / REF_SIZE[1]
        x, y, bw, bh = roi
        x0, y0 = int(round((x + dx) * sx)), int(round((y + dy) * sy))
        x1, y1 = int(round((x + dx + bw) * sx)), int(round((y + dy + bh) * sy))
        if x0 < 0 or y0 < 0 or x1 > w or y1 > h:
            return None
        p = gray[y0:y1, x0:x1]
        return p if p.shape == (bh, bw) else cv2.resize(p, (bw, bh), interpolation=cv2.INTER_AREA)

    def scores(self, img: np.ndarray) -> tuple[float, float]:
        best_on = best_off = -1.0
        for dy in range(-self.shift, self.shift + 1):
            for dx in range(-self.shift, self.shift + 1):
                p = self.patch(img, self.roi, dx, dy)
                if p is None:
                    continue
                best_on = max(best_on, _ncc(p, self.mean_on))
                best_off = max(best_off, _ncc(p, self.mean_off))
        return best_on, best_off

    MARGIN = 0.05  # 動作中と停止中の一致度の差がこれ未満なら決めない

    def decide(self, on: float, off: float) -> bool | None:
        if max(on, off) < self.threshold or abs(on - off) < self.MARGIN:
            return None
        return on > off

    def classify(self, img: np.ndarray) -> bool | None:
        return self.decide(*self.scores(img))

    @classmethod
    def train(cls, on_imgs: list[np.ndarray], off_imgs: list[np.ndarray], roi=BUTTON_ROI) -> tuple["ButtonModel", dict]:
        pon = [p for p in (cls.patch(i, roi) for i in on_imgs) if p is not None]
        poff = [p for p in (cls.patch(i, roi) for i in off_imgs) if p is not None]
        if not pon or not poff:
            raise ValueError(f"見本が足りません (動作中 {len(pon)} / 停止中 {len(poff)})")
        m = cls(np.mean(pon, axis=0), np.mean(poff, axis=0), roi)
        # 見本を判定し直して、正解率と余裕 (近い方 - 遠い方) を出す
        right = wrong = unknown = 0
        margins = []
        for imgs, want in ((on_imgs, True), (off_imgs, False)):
            for i in imgs:
                on, off = m.scores(i)
                got = m.decide(on, off)
                right += got is want
                wrong += got is (not want)
                unknown += got is None
                margins.append((on - off) if want else (off - on))
        return m, {"on": len(pon), "off": len(poff), "right": right, "wrong": wrong, "unknown": unknown,
                   "min_margin": round(float(min(margins)), 3), "on_vs_off": round(_ncc(m.mean_on, m.mean_off), 3)}

    def save(self, path: Path) -> None:
        np.savez(path, mean_on=self.mean_on, mean_off=self.mean_off, roi=np.array(self.roi), threshold=self.threshold)

    @classmethod
    def load(cls, path: Path) -> "ButtonModel | None":
        if not Path(path).exists():
            return None
        d = np.load(path)
        return cls(d["mean_on"], d["mean_off"], tuple(d["roi"]), float(d["threshold"]))


@dataclass
class HelperReading:
    state: bool | None          # True = 動作中 / False = 停止中 / None = 分からない
    how: str                    # 判定の根拠 (表示用)
    panel: float = -1.0         # パネル文字の一致度
    on: float = -1.0            # ■ の一致度
    off: float = -1.0           # ▶ の一致度
    log: float = -1.0           # Hunting Log の一致度


class HelperStateReader:
    def __init__(self, base_dir: Path, panel_thr: float = 0.8, button_thr: float = 0.75,
                 log_detector: HuntingLogDetector | None = None):
        self.base = Path(base_dir)

        def load(name: str) -> np.ndarray | None:
            img = cv2.imread(str(self.base / "templates" / name), cv2.IMREAD_GRAYSCALE)
            return img

        self.tpl_panel = load("helper_panel.png")
        self.tpl_on = load("helper_on.png")
        self.tpl_off = load("helper_off.png")
        self.panel_thr = panel_thr
        self.button_thr = button_thr
        self.log = log_detector or HuntingLogDetector(self.base)
        self.model = ButtonModel.load(self.base / BUTTON_MODEL)

    @staticmethod
    def _best(area: np.ndarray, tpl: np.ndarray | None) -> tuple[float, tuple[int, int]]:
        if tpl is None or tpl.shape[0] > area.shape[0] or tpl.shape[1] > area.shape[1]:
            return -1.0, (0, 0)
        _, s, _, loc = cv2.minMaxLoc(cv2.matchTemplate(area, tpl, cv2.TM_CCOEFF_NORMED))
        return float(s), loc

    def read(self, img: np.ndarray) -> HelperReading:
        r = HelperReading(None, "")
        if self.model is not None:
            r.on, r.off = self.model.scores(img)
            st = self.model.decide(r.on, r.off)
            if st is not None:
                r.state = st
                r.how = "学習したボタン " + ("■" if r.state else "▶")
                return r
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        r.panel, (px, py) = self._best(gray, self.tpl_panel)
        if r.panel >= self.panel_thr:
            bx, by = px + BUTTON_OFFSET[0] - BUTTON_MARGIN, py + BUTTON_OFFSET[1] - BUTTON_MARGIN
            x0, y0 = max(0, bx), max(0, by)
            area = gray[y0:by + 26 + 2 * BUTTON_MARGIN, x0:bx + 28 + 2 * BUTTON_MARGIN]
            r.on, _ = self._best(area, self.tpl_on)
            r.off, _ = self._best(area, self.tpl_off)
            if max(r.on, r.off) >= self.button_thr:
                r.state = r.on > r.off
                r.how = "パネルの " + ("■" if r.state else "▶")
                return r
        m = self.log.score(img)
        r.log = m.score
        if m.visible:
            r.state, r.how = True, "Hunting Log"
        else:
            r.how = "パネルが見えず Hunting Log も無い (インベントリ等で隠れている?)"
        return r
