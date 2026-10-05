"""BlockDiff: 8x8 ブロック単位のカラー (RGB565) 差分形式 (独自形式。BadCodec ではない。BadCodec 本来の命令を使う版は bad16.py)。

液晶ミラー (240x135) 用。ESP32 は受信しながらブロックを液晶に描くだけで、JPEG の展開 (IDCT) が要らない。
前フレームと同じブロックは送らない (液晶に残っている絵をそのまま使う) ので、フレームバッファも要らない。

フレーム
  'B' 'C' ver(=1) flags(bit0: キーフレーム)  の後にブロック命令が続く。ブロックはラスタ順 (30x17 = 510 個)。
  最下段 (y=128) は 7 行だけ描く (データは 8 行分。最後の行は捨てる)。

ブロック命令 (1 バイト目)
  0x00-0x3F  SKIP    n+1 ブロック (1-64) 前フレームのまま
  0x40-0x7F  FILL    n+1 ブロック (1-64) を単色で塗る。続けて色 2B
  0x80       2COLOR  色 2 つ (4B) + 8x8 の 1bit マスク (8B, 1 行 1 バイト, 上位ビットが左)    = 13B
  0x81       4COLOR  色 4 つ (8B) + 8x8 の 2bit 番号 (16B, 1 行 2 バイト, 上位ビットが左)   = 25B
  0x82       RAW     64 画素 (128B)                                                         = 129B
色はすべて RGB565 ビッグエンディアン。

非可逆: tolerance (8bit 換算の 1 画素あたり最大誤差) 以内なら SKIP / FILL / 2COLOR / 4COLOR で済ませる。
エンコーダは ESP32 が表示しているはずの絵 (ref) を持ち、誤差が積み重ならないようにそれと比べる。
"""
from __future__ import annotations

import numpy as np

W, H = 240, 135
BS = 8
BW, BH = W // BS, (H + BS - 1) // BS  # 30 x 17
HP = BH * BS  # 136 (下を 1 行足す)

SKIP, FILL, C2, C4, RAW = 0x00, 0x40, 0x80, 0x81, 0x82


def to565(bgr: np.ndarray) -> np.ndarray:
    b = bgr[..., 0].astype(np.uint16)
    g = bgr[..., 1].astype(np.uint16)
    r = bgr[..., 2].astype(np.uint16)
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def rgb_of565(c: np.ndarray) -> np.ndarray:
    """RGB565 → 8bit 換算の (r, g, b) (誤差の計算用)。"""
    c = c.astype(np.int32)
    r = ((c >> 11) & 31) * 255 // 31
    g = ((c >> 5) & 63) * 255 // 63
    b = (c & 31) * 255 // 31
    return np.stack([r, g, b], axis=-1)


def blocks(img565: np.ndarray) -> np.ndarray:
    """(136, 240) → (510, 64) ブロック単位の並び (ラスタ順)。"""
    return img565.reshape(BH, BS, BW, BS).transpose(0, 2, 1, 3).reshape(BH * BW, BS * BS)


def unblocks(blk: np.ndarray) -> np.ndarray:
    return blk.reshape(BH, BW, BS, BS).transpose(0, 2, 1, 3).reshape(HP, W)


def _pad(bgr: np.ndarray) -> np.ndarray:
    assert bgr.shape[:2] == (H, W), bgr.shape
    return np.concatenate([bgr, bgr[-1:]], axis=0)  # 136 行に


def _to565_rgb(m: np.ndarray) -> np.ndarray:
    """8bit 換算の (..., 3) → RGB565 (四捨五入)。"""
    m = np.rint(m).astype(np.int32)
    return (((m[..., 0] * 31 + 127) // 255) << 11 | ((m[..., 1] * 63 + 127) // 255) << 5
            | ((m[..., 2] * 31 + 127) // 255)).astype(np.uint16)


def _palette(p: np.ndarray, lab: np.ndarray, groups: int) -> np.ndarray:
    """グループごとの平均色 (RGB565)。p: (k, 64, 3), lab: (k, 64) → (k, groups)"""
    oh = (lab[..., None] == np.arange(groups)).astype(np.float32)  # (k, 64, g)
    cnt = oh.sum(axis=1)  # (k, g)
    sums = np.einsum("kpg,kpc->kgc", oh, p.astype(np.float32))
    mean = sums / np.maximum(cnt, 1)[..., None]
    return _to565_rgb(mean)


class Encoder:
    def __init__(self, tolerance: int = 16):
        self.tol = int(tolerance)
        self.ref: np.ndarray | None = None  # (510, 64) ESP32 に出ているはずの絵 (RGB565)
        self.force_key = True

    def request_key(self) -> None:
        self.force_key = True

    def encode(self, bgr: np.ndarray) -> bytes:
        cur = blocks(to565(_pad(bgr)))  # (510, 64) uint16
        key = self.force_key or self.ref is None
        self.force_key = False
        rgb = rgb_of565(cur)  # (510, 64, 3)
        n = cur.shape[0]
        if key:
            changed = np.ones(n, bool)
        else:
            changed = np.abs(rgb - rgb_of565(self.ref)).max(axis=(1, 2)) > self.tol
        new_ref = cur.copy() if self.ref is None else self.ref.copy()
        kinds = np.full(n, SKIP, np.int32)
        pal_of: dict[int, np.ndarray] = {}
        lab_of: dict[int, np.ndarray] = {}

        idx = np.nonzero(changed)[0]
        if len(idx):
            p = rgb[idx]  # (k, 64, 3)
            tol = self.tol
            # 単色
            span = (p.max(axis=1) - p.min(axis=1)).max(axis=1)
            fill = span <= tol
            c1 = _palette(p, np.zeros(p.shape[:2], np.int32), 1)[:, 0]
            # 2 色 (輝度の中央値で分ける)
            lum = p[..., 0] * 3 + p[..., 1] * 6 + p[..., 2]
            lab2 = (lum > np.median(lum, axis=1, keepdims=True)).astype(np.int32)
            pal2 = _palette(p, lab2, 2)
            err2 = np.abs(rgb_of565(np.take_along_axis(pal2, lab2, 1)) - p).max(axis=(1, 2))
            # 4 色 (輝度の四分位で分ける)
            q = np.percentile(lum, [25, 50, 75], axis=1).T  # (k, 3)
            lab4 = ((lum > q[:, :1]).astype(np.int32) + (lum > q[:, 1:2]) + (lum > q[:, 2:3]))
            pal4 = _palette(p, lab4, 4)
            err4 = np.abs(rgb_of565(np.take_along_axis(pal4, lab4, 1)) - p).max(axis=(1, 2))
            for j, bi in enumerate(idx):
                if fill[j]:
                    kinds[bi] = FILL
                    pal_of[bi] = c1[j:j + 1]
                    new_ref[bi] = c1[j]
                elif err2[j] <= tol:
                    kinds[bi] = C2
                    pal_of[bi], lab_of[bi] = pal2[j], lab2[j]
                    new_ref[bi] = pal2[j][lab2[j]]
                elif err4[j] <= tol:
                    kinds[bi] = C4
                    pal_of[bi], lab_of[bi] = pal4[j], lab4[j]
                    new_ref[bi] = pal4[j][lab4[j]]
                else:
                    kinds[bi] = RAW
                    new_ref[bi] = cur[bi]
        self.ref = new_ref
        return _serialize(kinds, pal_of, lab_of, cur, key)


def _serialize(kinds: np.ndarray, pal_of: dict, lab_of: dict, cur: np.ndarray, key: bool) -> bytes:
    out = bytearray(b"BC\x01" + bytes([1 if key else 0]))
    n = len(kinds)
    i = 0
    while i < n:
        k = kinds[i]
        if k == SKIP or k == FILL:
            j = i + 1
            while j < n and j - i < 64 and kinds[j] == k and (k == SKIP or pal_of[j][0] == pal_of[i][0]):
                j += 1
            out.append((SKIP if k == SKIP else FILL) | (j - i - 1))
            if k == FILL:
                out += int(pal_of[i][0]).to_bytes(2, "big")
            i = j
            continue
        out.append(int(k))
        if k == RAW:
            out += cur[i].astype(">u2").tobytes()
        else:
            out += pal_of[i].astype(">u2").tobytes()
            lab = lab_of[i].reshape(8, 8)
            if k == C2:
                out += np.packbits(lab.astype(np.uint8), axis=1).tobytes()
            else:
                rows = (lab.astype(np.uint16) << (14 - 2 * np.arange(8, dtype=np.uint16))).sum(axis=1).astype(">u2")
                out += rows.tobytes()
        i += 1
    return bytes(out)


def decode(data: bytes, prev565: np.ndarray | None = None) -> np.ndarray:
    """参照用のデコーダ (ESP32 の実装と同じ動作)。(135, 240) RGB565 を返す。"""
    assert data[:2] == b"BC" and data[2] == 1
    blk = blocks(np.concatenate([prev565, prev565[-1:]], 0)) if prev565 is not None else np.zeros((BH * BW, 64), np.uint16)
    blk = blk.copy()
    pos, bi = 4, 0
    while bi < BH * BW:
        op = data[pos]
        pos += 1
        if op < 0x40:
            bi += op + 1
        elif op < 0x80:
            c = int.from_bytes(data[pos:pos + 2], "big")
            pos += 2
            for _ in range((op & 0x3F) + 1):
                blk[bi] = c
                bi += 1
        elif op == C2:
            pal = [int.from_bytes(data[pos + 2 * t:pos + 2 * t + 2], "big") for t in range(2)]
            pos += 4
            px = []
            for r in range(8):
                byte = data[pos + r]
                px += [pal[(byte >> (7 - x)) & 1] for x in range(8)]
            pos += 8
            blk[bi] = px
            bi += 1
        elif op == C4:
            pal = [int.from_bytes(data[pos + 2 * t:pos + 2 * t + 2], "big") for t in range(4)]
            pos += 8
            px = []
            for r in range(8):
                w = int.from_bytes(data[pos + 2 * r:pos + 2 * r + 2], "big")
                px += [pal[(w >> (14 - 2 * x)) & 3] for x in range(8)]
            pos += 16
            blk[bi] = px
            bi += 1
        elif op == RAW:
            blk[bi] = np.frombuffer(data[pos:pos + 128], ">u2")
            pos += 128
            bi += 1
        else:
            raise ValueError(f"bad op {op:#x} at {pos - 1}")
    assert pos == len(data), (pos, len(data))
    return unblocks(blk)[:H]
