"""実画面の行と正解の文字列から、文字ごとの見本を取り出す (強制アライメント)。

切り出しの数と正解の文字数が合わない (背景の点が混ざる・2 文字がくっつく・行の端が切れている) 行でも、
今のモデルの確率を使った動的計画法で「どの切り出しがどの文字か」を対応づけ、
確からしい対応だけを見本にする。実画面の字体で学習し直すため (tools/edge_train.py)。
"""
from __future__ import annotations

import numpy as np


def align(logp: np.ndarray, target: list[int], skip_glyph: float = 4.0, skip_char: float = 6.0) -> list[tuple[int, int]]:
    """logp: (切り出し数, K) の対数確率。target: 正解の文字番号の列 (スペースなし)。
    戻り値: 対応した (切り出し番号, 正解の位置) の列。"""
    n, m = len(logp), len(target)
    INF = 1e9
    D = np.full((n + 1, m + 1), INF)
    B = np.zeros((n + 1, m + 1), np.int8)
    D[0, 0] = 0
    for i in range(n + 1):
        for j in range(m + 1):
            if i and D[i - 1, j] + skip_glyph < D[i, j]:          # 切り出しを捨てる (背景の点など)
                D[i, j], B[i, j] = D[i - 1, j] + skip_glyph, 1
            if j and D[i, j - 1] + skip_char < D[i, j]:           # 正解の文字が切り出せていない
                D[i, j], B[i, j] = D[i, j - 1] + skip_char, 2
            if i and j:
                c = D[i - 1, j - 1] - logp[i - 1, target[j - 1]]
                if c < D[i, j]:
                    D[i, j], B[i, j] = c, 3
    out = []
    i, j = n, m
    while i or j:
        b = B[i, j]
        if b == 3:
            out.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif b == 1:
            i -= 1
        else:
            j -= 1
    return out[::-1]
