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


# 窓の枠の確かさ: 手で囲んだ (tour) > Windows の OCR でタイトルを読んだパネル全体 (ocr) > 登録した切り抜き (det。
# タイトルの帯だけのことがある)。窓ごとに一番確かな見本の枠からスロットを作る
SRC_RANK = {"tour": 0, "ocr": 1, "det": 2}


def slots_from_labels(entries: Iterable[dict], min_count: int = 1) -> list[Slot]:
    by_src: dict[int, dict[str, list]] = {}
    for e in entries:
        for w in e.get("windows", []):
            r = SRC_RANK.get(w.get("src", e.get("src")), 2)
            b = box_to_slot(w["box"], e["w"], e["h"])
            by_src.setdefault(r, {}).setdefault(w["name"], []).append(b)
    boxes: dict[str, list] = {}
    for r in sorted(by_src, reverse=True):  # 確かなもので上書き
        boxes.update({n: bs for n, bs in by_src[r].items() if len(bs) >= min_count})
    return make_slots(boxes)


def window_table(entries: Iterable[dict], slots: list[Slot]) -> list[dict]:
    """窓の一覧 (リテラルの表): 名前・タイトル (WINDOW_TITLES)・場所・見本の数。"""
    from collections import Counter

    from .windows import WINDOW_TITLES

    cnt, src = Counter(), {}
    for e in entries:
        for w in e.get("windows", []):
            cnt[w["name"]] += 1
            src.setdefault(w["name"], Counter())[w.get("src", e.get("src", "?"))] += 1
    out = []
    for s in slots:
        for n in s.names:
            out.append({"name": n, "titles": WINDOW_TITLES.get(n, ([], None))[0], "count": cnt[n],
                        "src": dict(src.get(n, {})), "slot": f"({s.x},{s.y}) {s.w}x{s.h}"})
    return out


def push_model(host: str, blob: bytes, timeout: float = 8.0, log=print) -> bool:
    """ESP32 にモデルを送る (同じモデルが入っていれば送らない)。古いファームウェア (/winmodel 無し) なら False。"""
    import requests

    base = host if host.startswith("http") else f"http://{host}"
    try:
        st = requests.get(f"{base}/status", timeout=timeout).json()
    except Exception as e:
        log(f"[win] ESP32 の状態を取得できません: {e}")
        return False
    win = st.get("win")
    if win is None or "id" not in win:
        log("[win] ESP32 のファームウェアが古く、窓の認識がありません (frame-stream-13 以降を書き込んでください)")
        return False
    if win.get("id") == model_id(blob):
        return True
    try:
        r = requests.post(f"{base}/winmodel", data=blob, timeout=timeout,
                          headers={"Content-Type": "application/octet-stream"})
    except Exception as e:
        log(f"[win] ESP32 にモデルを送れません: {e}")
        return False
    if r.status_code != 200:
        log(f"[win] ESP32 がモデルを受け取りません: {r.text.strip()}")
        return False
    log(f"[win] ESP32 に窓の認識のモデルを送りました ({len(blob)} バイト, {model_id(blob)})")
    return True


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


NAME_LEN = 24  # ESP32 に送る窓の名前の長さ (末尾の 0 を含む)
MAGIC = b"AKW1"


def pack_model(q: QModel, slots: list[Slot], thr: float = 0.6) -> bytes:
    """ESP32 に送るモデル (重み + スロットの表 + 窓の名前)。最後の 4 バイトは CRC32 (= モデルの番号)。
    形式は firmware/AutoKeyMouse/edge_win_core.h の parseModel と同じ。"""
    import struct
    import zlib

    H, K = q.w2q.shape
    if K > 32:
        raise ValueError(f"窓の種類が多すぎます ({K - 1} 種。31 種まで)")
    if H > 128:
        raise ValueError("隠れ層は 128 まで")
    out = bytearray(MAGIC + struct.pack("<BBHB3x", 1, K, H, len(slots)))
    out += struct.pack("<4f", q.s1, q.sh, q.s2, thr)
    out += np.asarray(q.w1q, np.int8).tobytes()
    out += np.asarray(q.w1f, "<f4").tobytes()
    out += np.asarray(q.b1, "<f4").tobytes()
    out += np.asarray(q.w2q, np.int8).tobytes()
    out += np.asarray(q.b2, "<f4").tobytes()
    for sl in slots:
        mask = 0
        for n in sl.names:
            mask |= 1 << q.labels.index(n)
        out += struct.pack("<4BI", sl.x, sl.y, sl.w, sl.h, mask)
    for n in q.labels:
        b = n.encode("ascii")[:NAME_LEN - 1]
        out += b + bytes(NAME_LEN - len(b))
    return bytes(out) + struct.pack("<I", zlib.crc32(bytes(out)))


def model_id(blob: bytes) -> str:
    return blob[-4:][::-1].hex() if blob else ""


def export_header(q: QModel | None, slots: list[Slot], path: Path, note: str = "", thr: float = 0.6,
                  table: list[dict] | None = None) -> None:
    """ESP32 に組み込むモデル (書き込み直したときの初期値)。中身は pack_model と同じバイト列。
    窓の一覧 (リテラルの表) をコメントに残す。q=None なら空 (モデル無し)。"""
    blob = pack_model(q, slots, thr) if q is not None else b""
    lines = []
    for t in table or []:
        lines.append(f"//   {t['name']:16s} {', '.join(t.get('titles', []))[:40]:40s} {t.get('slot', '')}")
    if q is not None and not lines:
        lines = [f"//   {', '.join(s.names)}  ({s.x},{s.y}) {s.w}x{s.h}" for s in slots]
    body = ",".join(str(b) for b in blob) or "0"
    txt = f"""// 自動生成: pc/tools/window_train.py ({note})。手で編集しない
// 窓の種類の認識のモデル (形式は edge_win_core.h の parseModel)。Wi-Fi で新しいモデルが届けばそちらを使う
#pragma once
#include <stdint.h>
// 窓 (名前 / タイトル / 120x68 の場所):
{chr(10).join(lines) if lines else '//   (まだ学習していない)'}
namespace edge_win_builtin {{
constexpr uint32_t SIZE = {len(blob)};
constexpr const char* ID = "{model_id(blob)}";
static const uint8_t BLOB[{max(1, len(blob))}] = {{{body}}};
}}  // namespace edge_win_builtin
"""
    Path(path).write_text(txt, encoding="utf-8")
