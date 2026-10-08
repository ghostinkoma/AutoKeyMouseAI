"""攻撃が当たると画面上部に出る、相手 (モンスター) の名前・レベル・HP を読む。

画面上部の中央に「名前」、その下に「レベルの箱」と「赤い HP バー」が並ぶ。
  1. 赤い HP バー: 画面上部の中央で、横に細長い赤い塊を探す (色と形で見つかる。文字認識は要らない)
  2. 名前: バーの上の行を文字認識 (akm/edge/ocr.py) で読む
  3. レベル: バーの左の箱の数字を読む
  4. HP の割合: 赤い部分の長さ ÷ バー全体の長さ (バー全体はバーの行で暗い部分が続く範囲)。おおよそ
位置や大きさは画面の高さに対する比で決めるので、どの画面の大きさでも同じ手順で動く
(基準は高さ 1050 の画面。s = 画面の高さ / 1050)。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np


@dataclass
class Target:
    name: str
    level: str
    hp_ratio: float | None            # 0..1 (おおよそ)
    bar: tuple[int, int, int, int]    # 赤い部分 x, y, w, h
    name_box: tuple[int, int, int, int]
    raw_name: str = ""                # 辞書で直す前の読み
    conf: float = 0.0                 # 名前の文字認識の確信度 (各文字の確率の最小値)
    alts: list = field(default_factory=list)  # 切り出し方を変えた読み [(読み, 確信度, (x, y, w, h), 切り出し方), ...]


def find_bar(img: np.ndarray) -> tuple[int, int, int, int] | None:
    H, W = img.shape[:2]
    y1, x0, x1 = int(H * 0.12), int(W * 0.2), int(W * 0.8)
    top = img[:y1, x0:x1].astype(np.int16)
    b, g, r = top[..., 0], top[..., 1], top[..., 2]
    red = ((r > 110) & (r - g > 60) & (r - b > 60)).astype(np.uint8)
    s = H / 1050
    # バーの上の HP の数字 (白い文字) で赤が途切れるので、横につなぐ
    k = max(3, int(round(10 * s)))
    red = cv2.morphologyEx(red, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, 1)))
    n, _, st, _ = cv2.connectedComponentsWithStats(red, connectivity=8)
    best = None
    for i in range(1, n):
        x, y, w, h, a = st[i]
        # 横に細長い (HP バー)。高さは基準で 2〜18 ピクセル (HP の数字で途切れて短く見えることもある)
        if w >= 3 * h and w >= 15 * s and 2 * s <= h <= 18 * s and a >= 0.5 * w * h:
            if best is None or w > best[2]:
                best = (x + x0, y, w, h)
    return best


def _bar_extent(img: np.ndarray, bar) -> tuple[int, int]:
    """バー全体の左右: 赤い部分の右から、暗い (まだ削れていない HP の背景) 画素が続く範囲。"""
    x, y, w, h = bar
    row = img[y + h // 2].astype(np.int16)
    v = row.max(axis=1)
    right = x + w
    W = img.shape[1]
    s = img.shape[0] / 1050
    limit = min(W - 1, x + int(260 * s))  # バーの幅は基準で 260 ほどまで (暗い背景で画面の端まで行かないように)
    gap = 0
    while right < limit and gap < max(2, h):
        right += 1
        gap = gap + 1 if v[right] > 110 else 0
    return x, right - gap


def name_span(img: np.ndarray, y0: int, y1: int, cx: int, s: float) -> tuple[int, int]:
    """名前の行 (y0..y1) で、cx を含む文字のまとまりの左右。

    明るい画素 (文字) のある列を、隙間が gap 以下なら同じまとまりとしてつなぐ (単語の間の空白はつなぐ)。
    cx を含むまとまりが無ければ cx に一番近いもの。何も無ければ中央の固定幅。
    """
    W = img.shape[1]
    half = int(260 * s)
    bx0, bx1 = max(0, cx - half), min(W, cx + half)
    band = img[y0:y1, bx0:bx1]
    if band.size == 0:
        return max(0, cx - int(90 * s)), min(W, cx + int(90 * s))
    v = band.max(axis=2).astype(np.int16)
    bright = v > max(150, int(np.percentile(v, 85)))
    cols = bright.sum(axis=0) >= max(1, int(round(1 * s)))
    gap = max(4, int(round(9 * s)))
    runs = []
    i = 0
    n = len(cols)
    while i < n:
        if cols[i]:
            j = i
            while j < n and cols[j]:
                j += 1
            if runs and i - runs[-1][1] <= gap:
                runs[-1][1] = j
            else:
                runs.append([i, j])
            i = j
        else:
            i += 1
    c = cx - bx0
    if not runs:
        return max(0, cx - int(90 * s)), min(W, cx + int(90 * s))
    best = min(runs, key=lambda r: 0 if r[0] <= c < r[1] else min(abs(r[0] - c), abs(r[1] - c)))
    pad = max(2, int(round(4 * s)))
    return max(0, bx0 + best[0] - pad), min(W, bx0 + best[1] + pad)


def read_target(img: np.ndarray, ocr) -> Target | None:
    bar = find_bar(img)
    if bar is None:
        return None
    H = img.shape[0]
    s = H / 1050
    x, y, w, h = bar
    left, right = _bar_extent(img, bar)
    full = max(w, right - left)
    hp = min(1.0, w / full) if full > 0 else None
    # 名前: バーの上。切り出し方を何通りか試し、それぞれ読む (呼ぶ側がマップガイドと一致するものを選ぶ)
    #   span_full: バー全体の中央を含む文字のまとまり (長い名前の頭が切れない・右の別の表示を含めない)
    #   span_red : 赤い部分の中央を含む文字のまとまり (バー全体の幅を見誤ったとき用)
    #   box      : バーの左から右へ広めの固定の枠 (前からのやり方)
    #   box_wide : box を左に広げたもの
    ny0, ny1 = max(0, int(y - 32 * s)), max(1, int(y - 3 * s))
    W = img.shape[1]
    boxes = [name_span(img, ny0, ny1, left + full // 2, s), name_span(img, ny0, ny1, x + w // 2, s),
             (max(0, int(x - 60 * s)), min(W, int(x + max(full, 120 * s) + 60 * s))),
             (max(0, int(x - 170 * s)), min(W, int(x + max(full, 120 * s) + 60 * s)))]
    alts, seen = [], set()
    for k, (bx0, bx1) in enumerate(boxes):
        if (bx0, bx1) in seen or bx1 - bx0 < 4 or ny1 - ny0 < 4:
            continue
        seen.add((bx0, bx1))
        t, c = ocr.read(img[ny0:ny1, bx0:bx1])
        alts.append((t.strip(), c, (bx0, ny0, bx1 - bx0, ny1 - ny0), k))  # k: 切り出し方の番号
    if alts:
        name, conf, nbox, _ = max(alts, key=lambda a: a[1])
    else:
        name, conf, nbox = "", 0.0, (0, ny0, 1, max(1, ny1 - ny0))
    # レベル: バーの左の箱
    lx0, lx1 = max(0, int(x - 48 * s)), max(1, int(x - 2 * s))
    ly0, ly1 = max(0, int(y - 8 * s)), int(y + h + 8 * s)
    level, _ = ocr.read(img[ly0:ly1, lx0:lx1]) if lx1 - lx0 >= 4 else ("", 0.0)
    level = "".join(c for c in level if c.isdigit())
    if not (level.isdigit() and 1 <= int(level) <= 400):  # HP の数字などを読んだもの
        level = ""
    return Target(name, level, hp, bar, nbox, conf=conf, alts=alts)
