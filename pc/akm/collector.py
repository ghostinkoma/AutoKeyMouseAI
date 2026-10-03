"""地面のアイテム名ラベルを自動で集める (テンプレート / 学習データ作り用)。

狩りの最中、まだテンプレートの無い金色 (などの) 文字ラベルを見つけたら切り出して保存する。
宝石が落ちた瞬間を狙ってスクリーンショットを撮らなくても、放置しておけば
dataset/labels/ に "Jewel of Bless" などのラベル画像が溜まっていく。

溜まった画像は:
  * ファイル名を bless.png などに変えて templates/ に移せばそのままテンプレートになる
  * 量が集まれば YOLO 等の学習データにも使える
"""
from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np

from .vision import Detection, color_mask, roi_px


class LabelCollector:
    def __init__(self, cfg: dict, base_dir: Path):
        self.enabled = bool(cfg.get("enabled", True))
        self.colors = cfg.get("colors", ["gold"])
        self.search_roi = cfg.get("search_roi", [0, 0.12, 1, 0.70])
        self.exclude_rois = cfg.get("exclude_rois", [])
        self.min_h, self.max_h = cfg.get("text_height", [7, 13])
        self.min_w, self.max_w = cfg.get("text_width", [30, 320])
        self.interval = float(cfg.get("interval_s", 1.0))
        self.out = base_dir / cfg.get("dir", "dataset/labels")
        self.out.mkdir(parents=True, exist_ok=True)
        self.seen: set[bytes] = set()
        for p in self.out.glob("*.png"):  # 再起動しても同じものを何度も保存しない
            img = cv2.imread(str(p))
            if img is not None:
                self.seen.add(self._signature(img))
        self.last = 0.0
        self.saved = 0

    def _signature(self, crop: np.ndarray) -> bytes:
        """文字の形だけで比べる (位置や背景が違っても同じラベルは同じ値)。"""
        m = np.zeros(crop.shape[:2], np.uint8)
        for c in self.colors:
            m |= color_mask(crop, c)
        ys, xs = np.nonzero(m)
        if len(xs) == 0:
            return b""
        m = m[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
        small = cv2.resize(m, (48, 8), interpolation=cv2.INTER_AREA) > 96
        return bytes([m.shape[1] // 4]) + np.packbits(small).tobytes()

    def find_lines(self, img: np.ndarray) -> list[tuple[int, int, int, int]]:
        """文字ラベルらしい横長の塊 (x, y, w, h) を返す。"""
        x0, y0, x1, y1 = roi_px(img.shape, self.search_roi)
        area = img[y0:y1, x0:x1]
        mask = np.zeros(area.shape[:2], np.uint8)
        for c in self.colors:
            mask |= color_mask(area, c)
        for ex in self.exclude_rois:
            ex0, ey0, ex1, ey1 = roi_px(img.shape, ex)
            mask[max(0, ey0 - y0) : max(0, ey1 - y0), max(0, ex0 - x0) : max(0, ex1 - x0)] = 0
        # 文字同士 (横方向) をつないで 1 行にする
        joined = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (11, 1)))
        n, _, stats, _ = cv2.connectedComponentsWithStats(joined)
        out = []
        for i in range(1, n):
            x, y, w, h, area_px = stats[i]
            if not (self.min_h <= h <= self.max_h and self.min_w <= w <= self.max_w):
                continue
            fill = mask[y : y + h, x : x + w].mean() / 255
            if not 0.12 <= fill <= 0.7:  # 文字らしい密度 (べた塗りや細線を除外)
                continue
            out.append((x + x0, y + y0, w, h))
        return out

    def collect(self, img: np.ndarray, known: list[Detection]) -> int:
        """新しいラベルを保存し、保存した数を返す。"""
        if not self.enabled or time.monotonic() - self.last < self.interval:
            return 0
        self.last = time.monotonic()
        saved = 0
        for x, y, w, h in self.find_lines(img):
            # 既に分かっているもの (Zen など) と重なる行は保存しない
            if any(d.x - 4 <= x + w // 2 <= d.x + max(d.w, w) + 4 and abs(d.y - y) <= h for d in known):
                continue
            pad = 3
            crop = img[max(0, y - pad) : y + h + pad, max(0, x - pad) : x + w + pad]
            sig = self._signature(crop)
            if not sig or sig in self.seen:
                continue
            self.seen.add(sig)
            name = time.strftime("%Y%m%d_%H%M%S") + f"_{self.saved:04d}_{w}x{h}.png"
            cv2.imwrite(str(self.out / name), crop)
            self.saved += 1
            saved += 1
        if saved:
            print(f"[collect] 新しいラベルを {saved} 件保存 ({self.out}) 合計 {self.saved}")
        return saved
