"""ゲーム内の窓 (キャラクター・インベントリ等) を見つけて閉じる。

どの窓も開閉はキーのトグルなので、画面で「開いている」と確認できたときだけキーを 1 回押す
(閉じているのに押すと開いてしまう)。押したあと閉じたか確かめ、閉じなければそれ以上押さない。

config.yaml:
  ui_windows:
    - name: character
      key: c
      template: templates/ui_character.png   # 窓の中で動かない部分 (タイトル等) の切り抜き
    - name: inventory
      key: v
      template: templates/ui_inventory.png
見本は tools/helper_cycle.py --snap で画面を保存し、--make-ui-template で切り抜く。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

DEFAULT_WINDOWS = [
    {"name": "character", "key": "c", "template": "templates/ui_character.png"},
    {"name": "inventory", "key": "v", "template": "templates/ui_inventory.png"},
]


@dataclass
class UiWindow:
    name: str
    key: str
    tpl: np.ndarray            # 縮小済みのグレー画像
    threshold: float


class WindowCloser:
    def __init__(self, base_dir: Path, entries: list[dict] | None = None, scale: float = 0.5,
                 log=print, sleep=time.sleep, settle_s: float = 0.4):
        self.scale = scale
        self.log = log
        self.sleep = sleep
        self.settle_s = settle_s
        self.windows: list[UiWindow] = []
        self.missing: list[str] = []
        for e in DEFAULT_WINDOWS if entries is None else entries:
            p = Path(e["template"])
            p = p if p.is_absolute() else Path(base_dir) / p
            img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
            if img is None:
                self.missing.append(f"{e['name']} ({p.name})")
                continue
            self.windows.append(UiWindow(e["name"], str(e["key"]), self._small(img), float(e.get("threshold", 0.8))))

    def _small(self, g: np.ndarray) -> np.ndarray:
        return g if self.scale == 1.0 else cv2.resize(g, None, fx=self.scale, fy=self.scale, interpolation=cv2.INTER_AREA)

    def open_windows(self, img: np.ndarray) -> list[tuple[UiWindow, float]]:
        """開いている窓と一致度。"""
        g = self._small(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img)
        out = []
        for w in self.windows:
            if w.tpl.shape[0] > g.shape[0] or w.tpl.shape[1] > g.shape[1]:
                continue
            s = float(cv2.minMaxLoc(cv2.matchTemplate(g, w.tpl, cv2.TM_CCOEFF_NORMED))[1])
            if s >= w.threshold:
                out.append((w, s))
        return out

    def close_all(self, grab, run) -> list[str]:
        """開いている窓をキーで閉じる。閉じた窓の名前を返す。"""
        closed = []
        img = grab()
        for w, s in self.open_windows(img):
            self.log(f"[ui] {w.name} 窓が開いています (一致度 {s:.2f})。{w.key} で閉じます")
            run(f"k:{w.key}")
            self.sleep(self.settle_s)
            img = grab()
            if any(o is w for o, _ in self.open_windows(img)):
                self.log(f"[ui] {w.name} 窓が閉じません (キーの割り当てを確認してください)。これ以上押しません")
            else:
                closed.append(w.name)
        return closed
