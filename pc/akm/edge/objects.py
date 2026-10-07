"""窓・アイコンの認識 (どの画面の大きさでも)。

登録: 実画面から窓やアイコンを 1 回切り抜くだけ (pc/akm/data/objects/。tools/edge_objects.py add)。
  切り抜いたときの画面の高さを覚えておき、別の大きさの画面では比率で大きさを合わせて探す
  (UI の倍率が違ってもよいように、前後の倍率も試す)。

認識は 2 段:
  1. 候補探し: 縮小した画面でテンプレートの一致度 (NCC) が一番高い場所 (PC では OpenCV で速い)
  2. 確かめ: その場所を 16x16 の 1 ビット画像 + 3 つの数値にして、小さなネットで「どの窓か / 背景か」を判定。
     文字認識と同じ形 (edge_nn.h) なので ESP32 でも動く。学習は tools/edge_objects.py train。
  一致度がとても高ければそれだけで「ある」、中くらいならネットが認めたときだけ「ある」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import yaml

from .mlp import QModel

DATA = Path(__file__).resolve().parent.parent / "data" / "objects"
MODEL = Path(__file__).resolve().parent.parent.parent / "models" / "edge_obj.npz"
N = 16


@dataclass
class ObjectDef:
    name: str
    kind: str                 # window / icon
    image: np.ndarray         # 切り抜き (BGR)
    ref_h: int                # 切り抜いたときの画面の高さ
    key: str | None = None    # 窓を開閉するキー (あれば)
    group: str | None = None  # 同時には出ないもの同士 (動作中の ■ と停止中の ▶ など)。一番確からしい 1 つだけ残す
    gray: np.ndarray = field(init=False, repr=False)

    def __post_init__(self):
        self.gray = cv2.cvtColor(self.image, cv2.COLOR_BGR2GRAY)


@dataclass
class Detection:
    name: str
    kind: str
    present: bool
    ncc: float
    nn: float                 # ネットの確率 (この物体である)
    box: tuple[int, int, int, int]  # x, y, w, h (画面のピクセル)


def patch_features(gray_patch: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """パッチ → 16x16 の 1 ビット画像 (大津) + 数値 3 つ (明るさ・コントラスト・縦横比)。"""
    h, w = gray_patch.shape[:2]
    small = cv2.resize(gray_patch, (N, N), interpolation=cv2.INTER_AREA)
    _, b = cv2.threshold(small, 0, 1, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    feats = np.array([small.mean() / 128.0, small.std() / 64.0 - 1.0, w / max(1, h)], np.float32)
    return b.astype(np.uint8).ravel(), feats


def load_objects(base: Path = DATA) -> list[ObjectDef]:
    f = base / "objects.yaml"
    if not f.exists():
        return []
    out = []
    for e in yaml.safe_load(f.read_text(encoding="utf-8")) or []:
        img = cv2.imread(str(base / e["image"]))
        if img is not None:
            out.append(ObjectDef(e["name"], e.get("kind", "window"), img, int(e.get("ref_h", 1050)), e.get("key"),
                                 e.get("group")))
    return out


def save_object(name: str, kind: str, crop: np.ndarray, screen_h: int, key: str | None = None,
                base: Path = DATA, group: str | None = None) -> None:
    base.mkdir(parents=True, exist_ok=True)
    f = base / "objects.yaml"
    items = (yaml.safe_load(f.read_text(encoding="utf-8")) or []) if f.exists() else []
    items = [e for e in items if e["name"] != name]
    img_name = f"{name}.png"
    cv2.imwrite(str(base / img_name), crop)
    e = {"name": name, "kind": kind, "image": img_name, "ref_h": int(screen_h)}
    if key:
        e["key"] = key
    if group:
        e["group"] = group
    items.append(e)
    f.write_text(yaml.safe_dump(items, allow_unicode=True, sort_keys=False), encoding="utf-8")


class ObjectDetector:
    SCALES = (0.85, 1.0, 1.15)   # 予想した大きさの前後も試す
    WORK = 0.5                   # 候補探しは半分に縮小した画面で

    def __init__(self, objects: list[ObjectDef], model: QModel | None = None, hi: float = 0.8, lo: float = 0.55,
                 nn_min: float = 0.6):
        self.objects = objects
        self.model = model
        self.hi, self.lo, self.nn_min = hi, lo, nn_min
        self._cache: dict = {}

    @classmethod
    def load(cls, base: Path = DATA, model_path: Path = MODEL) -> "ObjectDetector":
        model = QModel.load(model_path) if Path(model_path).exists() else None
        return cls(load_objects(base), model)

    def _templates(self, o: ObjectDef, screen_h: int):
        """倍率ごとに (倍率, 縮小画面用のテンプレート or None, 等倍のテンプレート)。"""
        key = (o.name, screen_h)
        if key not in self._cache:
            base = screen_h / o.ref_h
            out = []
            for s in self.SCALES:
                f = base * s
                w, h = int(round(o.gray.shape[1] * f)), int(round(o.gray.shape[0] * f))
                if min(w, h) < 4:
                    continue
                full = cv2.resize(o.gray, (w, h), interpolation=cv2.INTER_AREA)
                ws, hs = int(round(w * self.WORK)), int(round(h * self.WORK))
                # 小さいアイコンは縮小すると見分けられないので、等倍の画面で直接探す
                small = cv2.resize(o.gray, (ws, hs), interpolation=cv2.INTER_AREA) if min(ws, hs) >= 10 else None
                out.append((f, small, full))
            self._cache[key] = out
        return self._cache[key]

    def candidate(self, o: ObjectDef, gray: np.ndarray, small: np.ndarray) -> tuple[float, tuple[int, int, int, int]]:
        best, box = -1.0, (0, 0, 0, 0)
        H, W = gray.shape[:2]
        for scale, ts, tf in self._templates(o, H):
            th, tw = tf.shape
            if th > H or tw > W:
                continue
            if ts is None:
                _, s, _, (x, y) = cv2.minMaxLoc(cv2.matchTemplate(gray, tf, cv2.TM_CCOEFF_NORMED))
            else:
                if ts.shape[0] > small.shape[0] or ts.shape[1] > small.shape[1]:
                    continue
                _, _, _, (sx, sy) = cv2.minMaxLoc(cv2.matchTemplate(small, ts, cv2.TM_CCOEFF_NORMED))
                # 縮小画面で見つけた場所の周りを等倍で探し直す (一致度を正確に)
                m = int(2 / self.WORK) + 2
                x0, y0 = max(0, int(sx / self.WORK) - m), max(0, int(sy / self.WORK) - m)
                area = gray[y0:min(H, y0 + th + 2 * m), x0:min(W, x0 + tw + 2 * m)]
                if area.shape[0] < th or area.shape[1] < tw:
                    continue
                _, s, _, (x, y) = cv2.minMaxLoc(cv2.matchTemplate(area, tf, cv2.TM_CCOEFF_NORMED))
                x, y = x + x0, y + y0
            if s > best:
                best, box = float(s), (int(x), int(y), tw, th)
        return best, box

    def nn_prob(self, gray: np.ndarray, box, name: str) -> float:
        if self.model is None or name not in self.model.labels:
            return -1.0
        index = self.model.labels.index(name)
        x, y, w, h = box
        patch = gray[max(0, y):y + h, max(0, x):x + w]
        if patch.size == 0:
            return 0.0
        bits, feats = patch_features(patch)
        lg = self.model.logits(bits[None], feats[None])[0]
        e = np.exp(lg - lg.max())
        return float(e[index] / e.sum())

    def detect(self, img: np.ndarray) -> list[Detection]:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        small = cv2.resize(gray, None, fx=self.WORK, fy=self.WORK, interpolation=cv2.INTER_AREA)
        out = []
        for o in self.objects:
            ncc, box = self.candidate(o, gray, small)
            nn = self.nn_prob(gray, box, o.name) if ncc >= self.lo else 0.0
            if self.model is None:
                present = ncc >= self.hi
            else:  # ネットがあれば、一致度がとても高くてもネットが強く否定したら「無い」
                present = (ncc >= self.hi and nn >= 0.2) or (ncc >= self.lo and nn >= self.nn_min)
            out.append(Detection(o.name, o.kind, present, ncc, nn, box))
        # 同じ場所に重なった結果や、同じグループ (同時には出ないもの) は、確からしい方だけ残す
        groups = {o.name: o.group for o in self.objects}
        for a in out:
            for b in out:
                same_group = groups[a.name] is not None and groups[a.name] == groups[b.name]
                if a is not b and a.present and b.present and (same_group or _iou(a.box, b.box) > 0.2):
                    weak = a if a.ncc + a.nn < b.ncc + b.nn else b
                    weak.present = False
        return out


def _iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter / max(1, aw * ah + bw * bh - inter)
