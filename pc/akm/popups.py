"""ゲーム中に割り込んでくるダイアログ (パーティ申請など) を自動で断る。

config.yaml の popups に登録した画像が画面に出たら、
  * click 画像 (例: Cancel ボタン) があればそれをクリック
  * 無ければ key (例: esc) を押す
画像は tools/grab_templates.py でダイアログが出ているときに切り出す。
"""
from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np

from .device import Device
from .vision import roi_px


def _load(base: Path, f: str | None) -> np.ndarray | None:
    if not f:
        return None
    p = Path(f) if Path(f).is_absolute() else base / f
    img = cv2.imread(str(p), cv2.IMREAD_COLOR)
    if img is None:
        print(f"[popup] 画像が読めません: {p}")
    return img


def find(img: np.ndarray, tpl: np.ndarray, roi, thr: float) -> tuple[float, float] | None:
    """tpl が見つかれば中心 (クライアント座標)。"""
    x0, y0, x1, y1 = roi_px(img.shape, roi) if roi else (0, 0, img.shape[1], img.shape[0])
    area = img[y0:y1, x0:x1]
    th, tw = tpl.shape[:2]
    if th > area.shape[0] or tw > area.shape[1]:
        return None
    _, best, _, (bx, by) = cv2.minMaxLoc(cv2.matchTemplate(area, tpl, cv2.TM_CCOEFF_NORMED))
    if best < thr:
        return None
    return x0 + bx + tw / 2, y0 + by + th / 2


class PopupGuard:
    def __init__(self, entries: list[dict] | None, dev: Device, base_dir: Path):
        self.dev = dev
        self.to_screen = None  # ElfBot が設定する
        self.entries = []
        self.handled: dict[str, int] = {}
        for e in entries or []:
            detect = _load(base_dir, e.get("detect"))
            if detect is None:
                continue
            self.entries.append({
                "name": e.get("name", "popup"),
                "detect": detect,
                "click": _load(base_dir, e.get("click")),
                "key": e.get("key", "esc"),
                "roi": e.get("roi"),
                "thr": float(e.get("threshold", 0.8)),
                "wait_s": float(e.get("wait_ms", 300)) / 1000,
            })

    def check(self, img: np.ndarray | None) -> bool:
        """ダイアログを見つけて断ったら True (クリックした場合は MU Helper が止まっている可能性あり)。"""
        if img is None:
            return False
        for e in self.entries:
            if find(img, e["detect"], e["roi"], e["thr"]) is None:
                continue
            pos = find(img, e["click"], None, e["thr"]) if e["click"] is not None else None
            if pos is not None and self.to_screen is not None:
                sx, sy = self.to_screen(*pos)
                print(f"[popup] {e['name']} を断ります (クリック {sx:.0f},{sy:.0f})")
                self.dev.click(sx, sy)
            else:
                print(f"[popup] {e['name']} を断ります (キー {e['key']})")
                self.dev.key(e["key"])
            self.handled[e["name"]] = self.handled.get(e["name"], 0) + 1
            time.sleep(e["wait_s"])
            return True
        return False
