"""ESP32 向けの小さな 2 層ニューラルネット (学習は numpy だけ)。

入力: 16x16 の 1 ビット画像 (256) + 数値 3 つ (行の中での高さ比・上下位置・縦横比)
  1 層目 (隠れ H): 1 ビット入力なので「1 の画素の重みを足すだけ」(掛け算なし。BadCodec のビット面と相性が良い)
                   + 数値 3 つは float で掛ける → ReLU
  2 層目 (出力 K): int8 の重み × uint8 の隠れ層 → 一番大きいものが答え
重みは int8 に量子化して C ヘッダーに書き出す (export_c)。PC 側の int8 推論 (predict_q) と ESP32 の結果は一致する。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

NB = 256  # ビット入力の数


def feat_norm(f: np.ndarray) -> np.ndarray:
    """数値の特徴を -1..1 程度にそろえる (ESP32 側も同じ式)。"""
    f = np.asarray(f, np.float32)
    return np.stack([np.clip(f[..., 0], 0, 2) - 1.0,      # 高さ比 (行の高さ = 1)
                     np.clip(f[..., 1], -1, 1),           # 上下位置
                     np.clip(f[..., 2], 0, 3) / 1.5 - 1.0], -1)  # 縦横比


@dataclass
class Model:
    W1: np.ndarray   # (256 + 3, H)
    b1: np.ndarray   # (H,)
    W2: np.ndarray   # (H, K)
    b2: np.ndarray   # (K,)
    labels: list[str]

    # ------------------------------------------------------------ float
    def forward(self, bits: np.ndarray, feats: np.ndarray):
        x = np.concatenate([bits.astype(np.float32), feat_norm(feats)], 1)
        h = np.maximum(0, x @ self.W1 + self.b1)
        return x, h, h @ self.W2 + self.b2

    def predict(self, bits, feats):
        return self.forward(bits, feats)[2].argmax(1)

    # ------------------------------------------------------------ int8
    def quantize(self, bits_cal: np.ndarray, feats_cal: np.ndarray) -> "QModel":
        s1 = float(np.abs(self.W1[:NB]).max() / 127)
        w1q = np.clip(np.round(self.W1[:NB] / s1), -127, 127).astype(np.int8)
        _, h, _ = self.forward(bits_cal, feats_cal)
        sh = float(np.percentile(h, 99.9) / 255) or 1e-3
        s2 = float(np.abs(self.W2).max() / 127)
        w2q = np.clip(np.round(self.W2 / s2), -127, 127).astype(np.int8)
        return QModel(w1q, s1, self.W1[NB:].astype(np.float32), self.b1.astype(np.float32), sh, w2q, s2,
                      self.b2.astype(np.float32), self.labels)


@dataclass
class QModel:
    w1q: np.ndarray   # (256, H) int8
    s1: float
    w1f: np.ndarray   # (3, H) float
    b1: np.ndarray    # (H,)
    sh: float         # 隠れ層 → uint8 の刻み
    w2q: np.ndarray   # (H, K) int8
    s2: float
    b2: np.ndarray    # (K,)
    labels: list[str]

    def logits(self, bits: np.ndarray, feats: np.ndarray) -> np.ndarray:
        acc = bits.astype(np.int32) @ self.w1q.astype(np.int32)          # 1 の画素の重みを足すだけ
        h = acc.astype(np.float32) * np.float32(self.s1) + feat_norm(feats) @ self.w1f + self.b1
        hq = np.clip(np.floor(np.maximum(h, 0) / np.float32(self.sh) + np.float32(0.5)), 0, 255).astype(np.int32)
        acc2 = hq @ self.w2q.astype(np.int32)
        return acc2.astype(np.float32) * np.float32(self.s2 * self.sh) + self.b2

    def predict(self, bits, feats):
        lg = self.logits(bits, feats)
        e = np.exp(lg - lg.max(1, keepdims=True))
        p = e / e.sum(1, keepdims=True)
        return lg.argmax(1), p.max(1)

    def save(self, path) -> None:
        np.savez(path, w1q=self.w1q, s1=self.s1, w1f=self.w1f, b1=self.b1, sh=self.sh, w2q=self.w2q, s2=self.s2,
                 b2=self.b2, labels=np.array(self.labels))

    @classmethod
    def load(cls, path) -> "QModel":
        d = np.load(path)
        return cls(d["w1q"], float(d["s1"]), d["w1f"], d["b1"], float(d["sh"]), d["w2q"], float(d["s2"]), d["b2"],
                   [str(x) for x in d["labels"]])

    def export_c(self, path, name: str = "edge_ocr", note: str = "") -> None:
        """ESP32 用の C ヘッダー (重みは const なのでフラッシュに置かれる)。"""
        H, K = self.w2q.shape

        def arr(a, fmt):
            return ",".join(fmt % v for v in np.asarray(a).ravel())

        labels = "".join(l if len(l) == 1 else "?" for l in self.labels)
        esc = labels.replace("\\", "\\\\").replace('"', '\\"')
        txt = f"""// 自動生成: pc/tools/edge_train.py / edge_objects.py ({note})。手で編集しない
// 推論は edge_nn.h
#pragma once
#include <stdint.h>
namespace {name} {{
constexpr int NB = {NB}, NF = 3, H = {H}, K = {K};
constexpr float S1 = {self.s1!r}f, SH = {self.sh!r}f, S2 = {self.s2!r}f;
static const char LABELS[] = "{esc}";  // K 文字。最後の '?' は「文字ではない」
static const int8_t W1Q[NB * H] = {{{arr(self.w1q, '%d')}}};
static const float W1F[NF * H] = {{{arr(self.w1f, '%.7gf')}}};
static const float B1[H] = {{{arr(self.b1, '%.7gf')}}};
static const int8_t W2Q[H * K] = {{{arr(self.w2q, '%d')}}};
static const float B2[K] = {{{arr(self.b2, '%.7gf')}}};
// edge_nn::classify<{name}::Model>(bits, feats, &conf) で使う
struct Model {{
  static constexpr int H = {name}::H, K = {name}::K;
  static constexpr float S1 = {name}::S1, SH = {name}::SH, S2 = {name}::S2;
  static constexpr const int8_t* W1Q = {name}::W1Q;
  static constexpr const float* W1F = {name}::W1F;
  static constexpr const float* B1 = {name}::B1;
  static constexpr const int8_t* W2Q = {name}::W2Q;
  static constexpr const float* B2 = {name}::B2;
}};
}}  // namespace {name}
"""
        with open(path, "w", encoding="utf-8") as f:
            f.write(txt)


def train(bits: np.ndarray, feats: np.ndarray, y: np.ndarray, labels: list[str], hidden: int = 96, epochs: int = 20,
          lr: float = 2e-3, batch: int = 512, seed: int = 0, log=print) -> Model:
    rng = np.random.default_rng(seed)
    D, K = NB + 3, len(labels)
    W1 = (rng.standard_normal((D, hidden)) * np.sqrt(2 / D)).astype(np.float32)
    b1 = np.zeros(hidden, np.float32)
    W2 = (rng.standard_normal((hidden, K)) * np.sqrt(2 / hidden)).astype(np.float32)
    b2 = np.zeros(K, np.float32)
    params = [W1, b1, W2, b2]
    m = [np.zeros_like(p) for p in params]
    v = [np.zeros_like(p) for p in params]
    t = 0
    model = Model(W1, b1, W2, b2, labels)
    n = len(y)
    for ep in range(epochs):
        idx = rng.permutation(n)
        loss_sum = 0.0
        rate = lr * (0.5 * (1 + np.cos(np.pi * ep / epochs)))  # だんだん小さく
        for s in range(0, n, batch):
            j = idx[s:s + batch]
            x, h, lg = model.forward(bits[j], feats[j])
            lg = lg - lg.max(1, keepdims=True)
            p = np.exp(lg)
            p /= p.sum(1, keepdims=True)
            loss_sum += float(-np.log(p[np.arange(len(j)), y[j]] + 1e-9).sum())
            g = p
            g[np.arange(len(j)), y[j]] -= 1
            g /= len(j)
            gW2 = h.T @ g
            gb2 = g.sum(0)
            gh = (g @ W2.T) * (h > 0)
            gW1 = x.T @ gh + 1e-4 * W1
            gb1 = gh.sum(0)
            t += 1
            for p_, g_, m_, v_ in zip(params, (gW1, gb1, gW2, gb2), m, v):
                m_ *= 0.9
                m_ += 0.1 * g_
                v_ *= 0.999
                v_ += 0.001 * g_ * g_
                p_ -= rate * (m_ / (1 - 0.9 ** t)) / (np.sqrt(v_ / (1 - 0.999 ** t)) + 1e-8)
        log(f"[train] {ep + 1}/{epochs}  loss {loss_sum / n:.3f}")
    return model
