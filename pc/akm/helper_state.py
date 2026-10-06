"""MU Helper が動いているかを画面から読む (動作中 / 停止中 / 不明)。

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

    @staticmethod
    def _best(area: np.ndarray, tpl: np.ndarray | None) -> tuple[float, tuple[int, int]]:
        if tpl is None or tpl.shape[0] > area.shape[0] or tpl.shape[1] > area.shape[1]:
            return -1.0, (0, 0)
        _, s, _, loc = cv2.minMaxLoc(cv2.matchTemplate(area, tpl, cv2.TM_CCOEFF_NORMED))
        return float(s), loc

    def read(self, img: np.ndarray) -> HelperReading:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        r = HelperReading(None, "")
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
