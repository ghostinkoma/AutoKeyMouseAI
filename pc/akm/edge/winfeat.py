"""窓の種類の認識 (エッジ AI) の特徴。ESP32 の液晶に出ている画面と同じものから、同じ計算で作る。

OCR は 240x135 に縮めると文字が潰れて効かないので、窓の「見た目の形」で見分ける:
  1. ゲーム画面を液晶と同じ 240x135 に縮め、色を R3G4B3 (bad16) に減らして液晶と同じ RGB565 にする
  2. 明るさ (gray) にして横 2 画素、縦 2 行を平均 → 120x68 (ESP32 はこれだけ持つ: 8KB)
  3. 窓ごとの決まった場所 (スロット。MU の窓はいつも同じ場所に開く) を、120x68 の
     座標で 16x16 のマスに分けて平均 → 平均より明るいマスを 1 にした 16x16 の 1 ビット画像
     + 数値 3 つ (平均の明るさ / ばらつき / 横の明るさの変化 = 文字や枠の多さ)
  4. edge_nn (akm/edge/mlp.py) の小さなネットで「そのスロットに何の窓があるか (無いか)」を当てる
ESP32 (firmware/AutoKeyMouse/edge_win.cpp) は整数で同じ計算をするので、PC と同じ答えになる。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from ..bad16 import replicate565
from ..mirror import compose

FW, FH = 240, 135     # 液晶に出る画面
BW, BH = 120, 136     # 横 2 画素の平均 (途中の計算。行 135 は使わない)
GW, GH = 120, 68      # スロットの座標 (縦横 2 画素の平均)
N = 16                # 16x16 のマス
NONE = "none"         # 「窓は無い」のラベル


@dataclass
class Slot:
    """窓がいつも開く場所 (120x68 の座標)。names はそこに開く窓 (ほぼ同じ場所の窓はまとめる)。"""
    x: int
    y: int
    w: int
    h: int
    names: list[str]

    def as_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h, "names": self.names}


def display565(img: np.ndarray, bits: tuple[int, int, int] | None = (3, 4, 3)) -> np.ndarray:
    """ゲーム画面 (BGR, 何解像度でも) → 液晶に出る RGB565 (135x240)。bits=None なら色を減らさない (JPEG に近い)。"""
    small = img if img.shape[:2] == (FH, FW) else compose(img, [])
    if bits is None:
        b = small[..., 0].astype(np.uint16) >> 3
        g = small[..., 1].astype(np.uint16) >> 2
        r = small[..., 2].astype(np.uint16) >> 3
        return (r << 11) | (g << 5) | b
    qs = []
    for ch, n in ((2, bits[0]), (1, bits[1]), (0, bits[2])):
        levels = (1 << n) - 1
        qs.append(np.rint(small[..., ch].astype(np.float32) * levels / 255.0).astype(np.uint16))
    r, g, b = qs
    q = ((r << (5 - bits[0])) << 11) | ((g << (6 - bits[1])) << 5) | (b << (5 - bits[2]))
    return replicate565(q, bits)


def gray_of(p565: np.ndarray) -> np.ndarray:
    """RGB565 → 明るさ 0..255 (ESP32 と同じ整数の式)。"""
    p = p565.astype(np.int32)
    r, g, b = p >> 11 & 31, p >> 5 & 63, p & 31
    r8, g8, b8 = (r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)
    return ((77 * r8 + 150 * g8 + 29 * b8) >> 8).astype(np.uint8)


def buffer_of(p565: np.ndarray) -> np.ndarray:
    """120x136 の明るさ (横 2 画素の平均)。行 135 は液晶に来ないので 0。"""
    g = gray_of(p565).astype(np.int32)
    out = np.zeros((BH, BW), np.uint8)
    out[:FH] = (g[:, 0::2] + g[:, 1::2]) >> 1
    return out


def half_of(buf: np.ndarray) -> np.ndarray:
    """120x136 → 120x68 (縦 2 行の平均。最後の組は行 134 を 2 回使う)。"""
    b = buf.astype(np.int32)
    lo = b[0::2][:GH]
    hi = b[1::2][:GH].copy()
    hi[GH - 1] = b[FH - 1]
    return ((lo + hi) >> 1).astype(np.uint8)


def cell_edges(start: int, size: int) -> list[tuple[int, int]]:
    out = []
    for j in range(N):
        a = start + j * size // N
        b = start + (j + 1) * size // N
        out.append((a, max(b, a + 1)))
    return out


def slot_features(half: np.ndarray, slot) -> tuple[np.ndarray, np.ndarray]:
    """スロットの 16x16 の 1 ビット (256) と数値 3 つ。slot は Slot か (x, y, w, h)。"""
    x, y, w, h = (slot.x, slot.y, slot.w, slot.h) if isinstance(slot, Slot) else slot
    ys, xs = np.array(cell_edges(y, h)), np.array(cell_edges(x, w))
    # スロットは画面の中 (box_to_slot) なので、はみ出さない。マスの合計は積分画像で
    ii = np.zeros((half.shape[0] + 1, half.shape[1] + 1), np.int64)
    ii[1:, 1:] = half.astype(np.int64).cumsum(0).cumsum(1)
    y0, y1 = ys[:, 0:1], ys[:, 1:2]
    x0, x1 = xs[None, :, 0], xs[None, :, 1]
    sums = ii[y1, x1] - ii[y0, x1] - ii[y1, x0] + ii[y0, x0]
    cells = (sums // ((y1 - y0) * (x1 - x0))).astype(np.int32)
    c = cells.astype(np.float32)
    m = np.float32(cells.sum()) / np.float32(N * N)
    sd = np.float32(np.sqrt(np.float32(((c - m) ** 2).sum()) / np.float32(N * N)))
    tex = np.float32(np.abs(np.diff(cells, axis=1)).sum()) / np.float32(N * (N - 1))
    bits = (c > m).astype(np.uint8).ravel()
    return bits, np.array([m / 128.0, sd / 64.0 - 1.0, tex / 16.0], np.float32)


def half_from_image(img: np.ndarray, bits: tuple[int, int, int] | None = (3, 4, 3)) -> np.ndarray:
    return half_of(buffer_of(display565(img, bits)))


def box_to_slot(box, screen_w: int, screen_h: int) -> tuple[int, int, int, int]:
    """ゲーム画面の枠 (x, y, w, h) → 120x68 の座標 (compose と同じ縮め方)。"""
    s = min(FW / screen_w, FH / screen_h)
    ox = (FW - int(screen_w * s)) // 2
    oy = (FH - int(screen_h * s)) // 2
    x, y, w, h = box
    x0, y0 = (x * s + ox) / 2, (y * s + oy) / 2
    x1, y1 = ((x + w) * s + ox) / 2, ((y + h) * s + oy) / 2
    xi, yi = int(round(x0)), int(round(y0))
    xi, yi = max(0, min(GW - 1, xi)), max(0, min(GH - 1, yi))
    return xi, yi, max(1, min(GW - xi, int(round(x1)) - xi)), max(1, min(GH - yi, int(round(y1)) - yi))


def iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter / float(aw * ah + bw * bh - inter or 1)


def make_slots(boxes: dict[str, list[tuple[int, int, int, int]]], merge_iou: float = 0.8) -> list[Slot]:
    """窓ごとの枠 (120x68) の中央値をその窓のスロットにし、ほぼ同じ場所の窓は 1 つのスロットにまとめる。"""
    per = {}
    for name, bs in boxes.items():
        if name == NONE or not bs:
            continue
        a = np.array(bs, np.float32)
        per[name] = tuple(int(round(v)) for v in np.median(a, 0))
    slots: list[Slot] = []
    for name, b in sorted(per.items(), key=lambda kv: -kv[1][2] * kv[1][3]):
        for s in slots:
            if iou((s.x, s.y, s.w, s.h), b) >= merge_iou:
                s.names.append(name)
                break
        else:
            slots.append(Slot(*b, [name]))
    return slots


def save_slots(path: Path, slots: list[Slot]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([s.as_dict() for s in slots], ensure_ascii=False, indent=1), encoding="utf-8")


def load_slots(path: Path) -> list[Slot]:
    if not path.exists():
        return []
    return [Slot(d["x"], d["y"], d["w"], d["h"], list(d["names"])) for d in json.loads(path.read_text("utf-8"))]
