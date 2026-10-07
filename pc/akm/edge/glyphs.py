"""文字の切り出しと正規化 (ESP32 でも同じ手順で動かせるよう、単純な処理だけで組む)。

1 行の文字の領域 (ROI) → 1 文字ずつの 16x16 の 1 ビット画像 + 3 つの数値:
  1. 明るさ v = max(R, G, B)。MU の文字は明るい塗り + 暗い縁取りなので、行の中で大津の二値化をすると
     塗りの部分だけが 1 になり、縁取りで隣の文字と離れる。
     (BadCodec のビット面で言えば、各色の上位ビットの OR に近い)
  2. 8 近傍でつながった塊を 1 文字とする。上下に並ぶ塊 (i の点、: など) は 1 つにまとめる。
  3. 各文字を外接矩形で切り出し、縦横比を保って 16x16 に収める (大きさに依らない)。
     大文字/小文字 (o/O) や , と ' の区別のため、行の中での高さ・上下位置・縦横比を数値で添える。
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

N = 16  # 1 文字の格子


@dataclass
class Glyph:
    x0: int
    y0: int
    x1: int
    y1: int
    bits: np.ndarray          # (16, 16) uint8 0/1
    feats: np.ndarray         # (3,) float: 高さ比, 上下位置, 縦横比


def text_mask(bgr: np.ndarray) -> np.ndarray:
    v = bgr.max(axis=2) if bgr.ndim == 3 else bgr
    _, m = cv2.threshold(v, 0, 1, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return m.astype(np.uint8)


def _score(boxes: list[list[int]]) -> float:
    """切り出しのもっともらしさ: 文字らしい (高さがそろった) 塊が多いほど高い。"""
    if not boxes:
        return 0.0
    hs = np.array([b[3] - b[1] for b in boxes], np.float32)
    med = np.median(hs)
    return float(np.sum((hs > 0.5 * med) & (hs < 1.6 * med)))


def components(mask: np.ndarray, min_area: int = 1) -> list[list[int]]:
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    H, W = mask.shape
    boxes = []
    for i in range(1, n):
        x, y, w, h, a = stats[i]
        if a < min_area:
            continue
        # 背景 (明るい空など): 文字は暗い縁取りに囲まれているので、領域の端に触れる大きな塊は背景
        touches = x == 0 or y == 0 or x + w == W or y + h == H
        if touches and (w > 0.5 * W or h >= H or a > 0.5 * w * h and w > 2 * h):
            continue
        boxes.append([x, y, x + w, y + h])
    boxes.sort(key=lambda b: b[0])
    # 上下に積み重なった塊 (横に重なり、縦には重ならない) は 1 文字にまとめる (i, j, :, ;, ! など)
    merged: list[list[int]] = []
    for b in boxes:
        hit = None
        for k in range(len(merged) - 1, max(-1, len(merged) - 3), -1):
            m = merged[k]
            ov = min(m[2], b[2]) - max(m[0], b[0])
            vov = min(m[3], b[3]) - max(m[1], b[1])
            if ov > 0.6 * min(m[2] - m[0], b[2] - b[0]) and vov <= 0:
                hit = k
                break
        if hit is not None:
            m = merged[hit]
            merged[hit] = [min(m[0], b[0]), min(m[1], b[1]), max(m[2], b[2]), max(m[3], b[3])]
        else:
            merged.append(b)
    merged.sort(key=lambda b: b[0])
    return merged


def split_wide(mask: np.ndarray, boxes: list[list[int]], line_h: float) -> list[list[int]]:
    """太字の縁取りでくっついた 2 文字を、縦の投影が一番細いところで切る。"""
    out = []
    for b in boxes:
        x0, y0, x1, y1 = b
        w = x1 - x0
        if w > 1.3 * line_h:
            proj = mask[y0:y1, x0:x1].sum(axis=0).astype(np.float32)
            lo, hi = int(w * 0.25), int(w * 0.75)
            cut = x0 + lo + int(np.argmin(proj[lo:hi]))
            for c in ([x0, y0, cut, y1], [cut, y0, x1, y1]):
                sub = mask[c[1]:c[3], c[0]:c[2]]
                ys = np.where(sub.any(axis=1))[0]
                if len(ys):
                    out.extend(split_wide(mask, [[c[0], c[1] + ys[0], c[2], c[1] + ys[-1] + 1]], line_h))
        else:
            out.append(b)
    return out


def normalize(mask: np.ndarray, box, line_h: float, line_mid: float) -> Glyph:
    x0, y0, x1, y1 = box
    crop = mask[y0:y1, x0:x1]
    h, w = crop.shape
    s = (N - 2) / max(h, w)
    nh, nw = max(1, round(h * s)), max(1, round(w * s))
    small = cv2.resize(crop * 255, (nw, nh), interpolation=cv2.INTER_AREA)
    g = np.zeros((N, N), np.uint8)
    oy, ox = (N - nh) // 2, (N - nw) // 2
    g[oy:oy + nh, ox:ox + nw] = small > 96
    feats = np.array([h / line_h, ((y0 + y1) / 2 - line_mid) / line_h, w / h], np.float32)
    return Glyph(x0, y0, x1, y1, g, feats)


def line_glyphs(bgr: np.ndarray) -> tuple[list[Glyph], float]:
    """1 行の画像から文字を切り出す。戻り値: (文字の列, 行の高さ)。"""
    # 明るい塗り (普通) と、暗い文字 (まれ) の両方を試し、文字らしく切れた方を使う
    m = text_mask(bgr)
    boxes = components(m)
    m2 = 1 - m
    b2 = components(m2)
    if _score(b2) > _score(boxes) * 1.5:
        m, boxes = m2, b2
    if not boxes:
        return [], 0.0
    hs = np.array([b[3] - b[1] for b in boxes], np.float32)
    tall = [b for b, h in zip(boxes, hs) if h >= 0.6 * hs.max()]
    line_h = float(np.median([b[3] - b[1] for b in tall]))
    line_mid = float(np.median([(b[1] + b[3]) / 2 for b in tall]))
    # 行の帯から外れた塊 (上下の背景の模様) は捨てる
    boxes = [b for b in boxes if abs((b[1] + b[3]) / 2 - line_mid) <= 0.8 * line_h and b[3] - b[1] <= 1.6 * line_h]
    if not boxes:
        return [], line_h
    return [normalize(m, b, line_h, line_mid) for b in boxes], line_h


def with_spaces(glyphs: list[Glyph], line_h: float, labels: list[str]) -> str:
    """認識結果の文字列に、隙間の広さからスペースを入れる。"""
    out = []
    for i, (g, c) in enumerate(zip(glyphs, labels)):
        if i and g.x0 - glyphs[i - 1].x1 > 0.35 * line_h:
            out.append(" ")
        out.append(c)
    return "".join(out)
