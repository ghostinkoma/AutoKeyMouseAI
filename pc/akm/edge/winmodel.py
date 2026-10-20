"""窓の種類の認識 (winfeat の特徴 + edge_nn) の見本づくり・判定・ESP32 への書き出し。

見本 (dataset/windows/labels.jsonl の 1 行 = 画面 1 枚):
    {"file": "...png", "w": 1366, "h": 768, "windows": [{"name": "inventory", "box": [x, y, w, h]}], "src": "tour"}
スロット (窓が開く場所) は窓ごとの枠から作る (手で撮った tour の枠を優先。det の枠は窓の一部のことがある)。
スロットごとに「そこに開く窓のどれか / 無い (none)」を 1 つのネットで当てる。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

import numpy as np

from .mlp import QModel
from .winfeat import NONE, Slot, box_to_slot, half_from_image, make_slots, slot_features

# 学習で画面を少し変える: 色の減らし方 (液晶 R3G4B3 / JPEG に近い 565)、明るさ、液晶の上の文字
VARIANTS = [
    {"bits": (3, 4, 3)},
    {"bits": None},
    {"bits": (3, 4, 3), "gain": 0.85},
    {"bits": (3, 4, 3), "gain": 1.15},
    {"bits": (3, 4, 3), "lines": True},
]
JITTER = [(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)]


def slots_from_labels(entries: Iterable[dict], min_count: int = 1) -> list[Slot]:
    tour: dict[str, list] = {}
    det: dict[str, list] = {}
    for e in entries:
        for w in e.get("windows", []):
            b = box_to_slot(w["box"], e["w"], e["h"])
            (tour if e.get("src") == "tour" else det).setdefault(w["name"], []).append(b)
    boxes = {n: bs for n, bs in det.items() if len(bs) >= min_count}
    boxes.update({n: bs for n, bs in tour.items() if len(bs) >= min_count})
    return make_slots(boxes)


def label_names(slots: list[Slot]) -> list[str]:
    return sorted({n for s in slots for n in s.names}) + [NONE]


def crop_label(slot: Slot, present: set[str]) -> str:
    for n in slot.names:
        if n in present:
            return n
    return NONE


def _variant(img: np.ndarray, v: dict, rng) -> np.ndarray:
    from ..mirror import compose

    if v.get("gain"):
        img = np.clip(img.astype(np.float32) * v["gain"], 0, 255).astype(np.uint8)
    if v.get("lines"):
        txt = ["RUN 12.3fps", "HP 87% MP 64%", "Icarus 50,72"]
        n = int(rng.integers(1, 4))
        img = compose(img, [(t, (255, 255, 255)) for t in txt[:n]])
    return half_from_image(img, v["bits"])


def build(entries: list[dict], slots: list[Slot], load: Callable[[dict], np.ndarray | None], augment: bool = True,
          seed: int = 0, log=print):
    """見本 → (bits, feats, y, frame 番号, slot 番号)。augment=False は液晶と同じ R3G4B3 だけ・ずらし無し。"""
    labels = label_names(slots)
    idx = {n: i for i, n in enumerate(labels)}
    rng = np.random.default_rng(seed)
    B, F, Y, FI, SI = [], [], [], [], []
    for fi, e in enumerate(entries):
        img = load(e)
        if img is None:
            continue
        present = {w["name"] for w in e.get("windows", [])}
        variants = VARIANTS if augment else VARIANTS[:1]
        for v in variants:
            half = _variant(img, v, rng)
            for si, s in enumerate(slots):
                y = idx[crop_label(s, present)]
                for dx, dy in (JITTER if augment else JITTER[:1]):
                    x0 = min(max(0, s.x + dx), half.shape[1] - s.w)
                    y0 = min(max(0, s.y + dy), half.shape[0] - s.h)
                    b, f = slot_features(half, (x0, y0, s.w, s.h))
                    B.append(b), F.append(f), Y.append(y), FI.append(fi), SI.append(si)
        if (fi + 1) % 200 == 0:
            log(f"[win] 特徴 {fi + 1}/{len(entries)}")
    if not B:
        z = np.zeros((0,), np.int64)
        return np.zeros((0, 256), np.uint8), np.zeros((0, 3), np.float32), z, z, z
    return (np.array(B, np.uint8), np.array(F, np.float32), np.array(Y, np.int64), np.array(FI, np.int64),
            np.array(SI, np.int64))


def detect(q: QModel, slots: list[Slot], half: np.ndarray, thr: float = 0.6) -> list[tuple[str, float, Slot]]:
    """画面 (120x68 の明るさ) の各スロットを判定し、見つかった窓 (名前, 確率, スロット)。
    そのスロットに開かない窓の答えは無視する (ESP32 と同じ)。"""
    if not slots:
        return []
    feats = [slot_features(half, s) for s in slots]
    k, p = q.predict(np.array([f[0] for f in feats]), np.array([f[1] for f in feats]))
    out = []
    for s, ki, pi in zip(slots, k, p):
        name = q.labels[int(ki)]
        if name != NONE and name in s.names and pi >= thr:
            out.append((name, float(pi), s))
    return out


def export_header(q: QModel, slots: list[Slot], path: Path, note: str = "", thr: float = 0.6) -> None:
    """ESP32 用: ネットの重み + スロットの表 + 窓の名前。"""
    labels = q.labels
    names = ",".join(f'"{n}"' for n in labels)
    rows = []
    for s in slots:
        mask = 0
        for n in s.names:
            mask |= 1 << labels.index(n)
        rows.append(f"  {{{s.x}, {s.y}, {s.w}, {s.h}, 0x{mask:08X}u}},  // {', '.join(s.names)}")
    extra = f"""// 窓の名前 (答えの番号順。最後は "{NONE}" = 窓は無い)
static const char* const NAMES[K] = {{{names}}};
constexpr int NONE = K - 1;
constexpr float THRESHOLD = {thr}f;
// 窓が開く場所 (120x68 の座標) と、そこに開く窓 (NAMES の番号のビット)
struct Slot {{
  uint8_t x, y, w, h;
  uint32_t mask;
}};
constexpr int NSLOT = {len(slots)};
static const Slot SLOTS[{max(1, len(slots))}] = {{
{chr(10).join(rows) if rows else '  {0, 0, 1, 1, 0u},'}
}};
"""
    q.export_c(path, name="edge_win", note=note, extra=extra)
