"""画像認識。

* bar_level      : HP/MP オーブ・ゲージの残量 (色マスク)
* TemplateDetector: 地面に落ちたアイテム名ラベル ("Jewel of Bless", "Zen" など) を
                   テンプレートマッチングで検出。学習不要で最初に動かす用
* YoloDetector   : 自前で学習した YOLO モデルで アイテム/モンスター を検出 (任意)

ROI はすべてゲーム画面サイズに対する比率 (x, y, w, h) で指定する。
解像度を変えても設定を流用できるようにするため。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

# HSV (OpenCV: H 0-179) のプリセット。複数レンジは OR
COLOR_PRESETS: dict[str, list[tuple[tuple[int, int, int], tuple[int, int, int]]]] = {
    "red": [((0, 90, 60), (10, 255, 255)), ((170, 90, 60), (179, 255, 255))],
    "blue": [((95, 90, 60), (130, 255, 255))],
    "yellow": [((18, 90, 90), (35, 255, 255))],
    "green": [((40, 90, 60), (85, 255, 255))],
}


@dataclass
class Detection:
    label: str
    kind: str  # "item" | "monster"
    x: int
    y: int
    w: int
    h: int
    score: float

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


def roi_px(shape: tuple[int, ...], roi: list[float] | tuple[float, ...]) -> tuple[int, int, int, int]:
    h, w = shape[:2]
    x0 = int(round(roi[0] * w))
    y0 = int(round(roi[1] * h))
    x1 = int(round((roi[0] + roi[2]) * w))
    y1 = int(round((roi[1] + roi[3]) * h))
    x0, x1 = max(0, x0), min(w, x1)
    y0, y1 = max(0, y0), min(h, y1)
    return x0, y0, x1, y1


def color_mask(bgr: np.ndarray, color: str | list) -> np.ndarray:
    ranges = COLOR_PRESETS[color] if isinstance(color, str) else color
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = np.zeros(bgr.shape[:2], np.uint8)
    for lo, hi in ranges:
        mask |= cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
    return mask


def bar_level(
    img: np.ndarray,
    roi: list[float],
    color: str | list = "red",
    direction: str = "vertical",
    fill_ratio: float = 0.25,
) -> float:
    """ゲージ残量 0.0-1.0。

    vertical  : 下から溜まるオーブ (MU の HP/MP 球)。行ごとに色画素の割合を見る
    horizontal: 左から伸びるバー
    """
    x0, y0, x1, y1 = roi_px(img.shape, roi)
    crop = img[y0:y1, x0:x1]
    if crop.size == 0:
        return 0.0
    mask = color_mask(crop, color) > 0
    if direction == "vertical":
        filled = mask.mean(axis=1) >= fill_ratio  # 行ごと (上→下)
        n = len(filled)
        # 一番上の「埋まっている行」から下を残量とみなす (球の縁のノイズに強い)
        idx = np.flatnonzero(filled)
        return 0.0 if idx.size == 0 else float(n - idx[0]) / n
    filled = mask.mean(axis=0) >= fill_ratio  # 列ごと (左→右)
    idx = np.flatnonzero(filled)
    return 0.0 if idx.size == 0 else float(idx[-1] + 1) / len(filled)


def nms(dets: list[Detection], iou_thr: float = 0.3) -> list[Detection]:
    dets = sorted(dets, key=lambda d: d.score, reverse=True)
    keep: list[Detection] = []
    for d in dets:
        ok = True
        for k in keep:
            ix = max(0, min(d.x + d.w, k.x + k.w) - max(d.x, k.x))
            iy = max(0, min(d.y + d.h, k.y + k.h) - max(d.y, k.y))
            inter = ix * iy
            union = d.w * d.h + k.w * k.h - inter
            if union and inter / union > iou_thr:
                ok = False
                break
        if ok:
            keep.append(d)
    return keep


class TemplateDetector:
    """テンプレート画像 (tools/grab_templates.py で切り出す) によるラベル検出。

    targets: [{"name": "Jewel of Bless", "files": ["templates/bless.png"],
               "threshold": 0.85, "kind": "item"}, ...]
    """

    def __init__(
        self,
        targets: list[dict],
        search_roi: list[float] | None = None,
        exclude_rois: list[list[float]] | None = None,
        base_dir: Path | None = None,
    ):
        self.search_roi = search_roi or [0, 0, 1, 1]
        self.exclude_rois = exclude_rois or []
        self.templates: list[tuple[str, str, float, np.ndarray]] = []
        base = base_dir or Path.cwd()
        for t in targets:
            for f in t["files"]:
                path = Path(f) if Path(f).is_absolute() else base / f
                tpl = cv2.imread(str(path), cv2.IMREAD_COLOR)
                if tpl is None:
                    print(f"[vision] テンプレートが読めません (スキップ): {path}")
                    continue
                self.templates.append((t["name"], t.get("kind", "item"), float(t.get("threshold", 0.85)), tpl))

    def detect(self, img: np.ndarray) -> list[Detection]:
        x0, y0, x1, y1 = roi_px(img.shape, self.search_roi)
        area = img[y0:y1, x0:x1].copy()
        for ex in self.exclude_rois:  # UI 部分を塗りつぶして誤検出を防ぐ
            ex0, ey0, ex1, ey1 = roi_px(img.shape, ex)
            area[max(0, ey0 - y0) : max(0, ey1 - y0), max(0, ex0 - x0) : max(0, ex1 - x0)] = 0

        out: list[Detection] = []
        for name, kind, thr, tpl in self.templates:
            th, tw = tpl.shape[:2]
            if th > area.shape[0] or tw > area.shape[1]:
                continue
            res = cv2.matchTemplate(area, tpl, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(res >= thr)
            dets = [Detection(name, kind, int(x + x0), int(y + y0), tw, th, float(res[y, x])) for y, x in zip(ys, xs)]
            out.extend(nms(dets))
        return nms(out)


class YoloDetector:
    """ultralytics YOLO モデルによる検出 (pip install ultralytics が必要)。

    class_kinds: {"jewel_bless": "item", "zen": "item", "monster": "monster", ...}
    """

    def __init__(self, model_path: str, conf: float = 0.5, class_kinds: dict[str, str] | None = None):
        from ultralytics import YOLO

        self.model = YOLO(model_path)
        self.conf = conf
        self.class_kinds = class_kinds or {}

    def detect(self, img: np.ndarray) -> list[Detection]:
        res = self.model.predict(img, conf=self.conf, verbose=False)[0]
        out = []
        for box, cls, score in zip(res.boxes.xyxy.tolist(), res.boxes.cls.tolist(), res.boxes.conf.tolist()):
            name = res.names[int(cls)]
            x0, y0, x1, y1 = (int(v) for v in box)
            out.append(Detection(name, self.class_kinds.get(name, "item"), x0, y0, x1 - x0, y1 - y0, float(score)))
        return out


def build_detectors(vcfg: dict, base_dir: Path) -> list:
    dets: list = []
    tcfg = vcfg.get("templates")
    if tcfg and tcfg.get("enabled", True):
        dets.append(
            TemplateDetector(tcfg.get("targets", []), tcfg.get("search_roi"), tcfg.get("exclude_rois"), base_dir)
        )
    ycfg = vcfg.get("yolo")
    if ycfg and ycfg.get("enabled", False):
        dets.append(YoloDetector(str(base_dir / ycfg["model"]), ycfg.get("conf", 0.5), ycfg.get("class_kinds")))
    return dets


def draw(img: np.ndarray, dets: list[Detection], extra: dict[str, str] | None = None) -> np.ndarray:
    """デバッグ表示用に検出結果を描画する。"""
    vis = img.copy()
    for d in dets:
        color = (0, 255, 255) if d.kind == "item" else (0, 0, 255)
        cv2.rectangle(vis, (d.x, d.y), (d.x + d.w, d.y + d.h), color, 2)
        cv2.putText(vis, f"{d.label} {d.score:.2f}", (d.x, max(12, d.y - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
    y = 20
    for k, v in (extra or {}).items():
        cv2.putText(vis, f"{k}: {v}", (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)
        y += 22
    return vis
