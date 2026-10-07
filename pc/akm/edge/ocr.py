"""文字を読む (PC 側)。ESP32 と同じ int8 のモデル (models/edge_ocr.npz) を使う。

    from akm.edge.ocr import OcrReader
    text, conf = OcrReader.load().read(bgr_roi)

画面の大きさに依らない: 1 文字ずつ切り出して 16x16 にそろえてから判定するため。
くっついた 2 文字 (太字の縁取り) は「1 文字として読む」「真ん中で切って 2 文字として読む」の
確からしい方を選ぶ (ESP32 でも同じ考え方で実装できる)。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .glyphs import Glyph, line_glyphs, normalize, text_mask
from .mlp import QModel

MODEL = Path(__file__).resolve().parent.parent.parent / "models" / "edge_ocr.npz"


class OcrReader:
    def __init__(self, model: QModel):
        self.m = model
        self.junk = len(model.labels) - 1

    @classmethod
    def load(cls, path: Path = MODEL) -> "OcrReader":
        if not Path(path).exists():
            raise FileNotFoundError(f"文字認識のモデルがありません: {path}  (python tools\\edge_train.py train で作る)")
        return cls(QModel.load(path))

    def _probs(self, glyphs: list[Glyph]) -> np.ndarray:
        bits = np.array([g.bits.ravel() for g in glyphs])
        ft = np.array([g.feats for g in glyphs])
        lg = self.m.logits(bits, ft)
        e = np.exp(lg - lg.max(1, keepdims=True))
        return e / e.sum(1, keepdims=True)

    def read(self, bgr: np.ndarray, junk_penalty: float = 0.5) -> tuple[str, float]:
        """1 行を読む。戻り値: (文字列, 一番自信のない文字の確率)。"""
        glyphs, line_h = line_glyphs(bgr)
        if not glyphs:
            return "", 0.0
        glyphs = self._split_touching(bgr, glyphs, line_h)
        p = self._probs(glyphs)
        p[:, self.junk] *= junk_penalty  # 行の中の切り出しはたいてい文字
        idx = p.argmax(1)
        keep = [i for i, k in enumerate(idx) if k != self.junk]
        if not keep:
            return "", 0.0
        labels = [self.m.labels[idx[i]] for i in keep]
        from .glyphs import with_spaces

        text = with_spaces([glyphs[i] for i in keep], line_h, labels)
        return text, float(min(p[i, idx[i]] for i in keep))

    def _split_touching(self, bgr: np.ndarray, glyphs: list[Glyph], line_h: float) -> list[Glyph]:
        """幅の広い切り出しは、2 つに切った方が確からしければ切る。"""
        wide = [i for i, g in enumerate(glyphs) if g.x1 - g.x0 > 1.05 * line_h]
        if not wide:
            return glyphs
        mask = text_mask(bgr)
        out = []
        p_one = self._probs(glyphs)
        for i, g in enumerate(glyphs):
            if i not in wide:
                out.append(g)
                continue
            w = g.x1 - g.x0
            proj = mask[g.y0:g.y1, g.x0:g.x1].sum(axis=0)
            lo, hi = int(w * 0.3), max(int(w * 0.3) + 1, int(w * 0.7))
            cut = g.x0 + lo + int(np.argmin(proj[lo:hi]))
            parts = []
            for x0, x1 in ((g.x0, cut), (cut, g.x1)):
                ys = np.where(mask[g.y0:g.y1, x0:x1].any(axis=1))[0]
                if len(ys) == 0:
                    break
                parts.append(normalize(mask, [x0, g.y0 + ys[0], x1, g.y0 + ys[-1] + 1], line_h,
                                       (g.y0 + g.y1) / 2 - g.feats[1] * line_h))
            if len(parts) == 2:
                pp = self._probs(parts)
                one = p_one[i, :self.junk].max()
                two = min(pp[0, :self.junk].max(), pp[1, :self.junk].max())
                if two > one + 0.15:
                    out.extend(parts)
                    continue
            out.append(g)
        return out
