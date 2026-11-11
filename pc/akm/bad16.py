"""BadCodec 16bit 版: RGB565 の 16 枚のビット面を、それぞれ BadCodec (ghostinkoma/BadCodec, Protocol 514)
の本来の命令で圧縮して送る。液晶ミラー (240x135, 下に 1 行足して 240x136) 用。

1 ビット面の圧縮は公式 tools/Codec.py の encode_frame_worker と同じ結果になる C 移植
(pc/native/bad_encode.c) を使う。ESP32 側は公式 bad_decode.cpp をそのまま 16 面分使って展開する。

コンテナ (1 フレーム)。既定は ver 3 (前フレーム不要。Encoder の説明と firmware/AutoKeyMouse/bad16.h 参照)。
ver 2 は
  'B' '6' ver(=2) flags(bit0: キーフレーム = 全面を 0 から) mask(u16 LE: 送る面。bit b = RGB565 の bit b)
  mask の立っている面の数だけ: 長さ (u16 LE) + そのビット面の BadCodec フレームデータ (FRAME_DELIMITER なし)
  面の順番は RGB565 の bit0 (青の最下位) → bit15 (赤の最上位)。送らない面は常に 0
  (ver 1 は mask 無しで 16 面すべて)
bits=(5, 6, 5) で可逆。各色の上位ビットだけ送ると面が減り、ESP32 のメモリ (面ごとに 4080B) も小さくなる。
PSRAM の無い無印 ESP32 は (3, 4, 2) = 9 面あたりまでしか入らない (LEVELS 参照)。
"""
from __future__ import annotations

import ctypes
import struct
import sys
from pathlib import Path

import numpy as np

W, H = 240, 136  # 135 行 + 1 (8 の倍数)
_LIB = None


def _lib():
    global _LIB
    if _LIB is None:
        d = Path(__file__).resolve().parent.parent / "native"
        name = "bad_encode.dll" if sys.platform == "win32" else "libbad_encode.so"
        path = d / name
        if not path.exists() and sys.platform != "win32":
            import subprocess

            subprocess.run(["gcc", "-O2", "-shared", "-fPIC", "-o", str(path), str(d / "bad_encode.c")], check=True)
        lib = ctypes.CDLL(str(path))
        lib.bad_encode_frame.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                         ctypes.c_void_p, ctypes.c_int]
        lib.bad_encode_frame.restype = ctypes.c_int
        lib.bad16_encode_abs.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint16,
                                         ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.bad16_encode_abs.restype = ctypes.c_int
        for fn in ("bad_entropy_huff", "bad_entropy_tans"):
            if hasattr(lib, fn):
                getattr(lib, fn).argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
                getattr(lib, fn).restype = ctypes.c_int
        if hasattr(lib, "bad16_encode_ycc"):
            lib.bad16_encode_ycc.argtypes = [ctypes.c_void_p] * 6 + [ctypes.c_int] * 5 + [
                ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
            lib.bad16_encode_ycc.restype = ctypes.c_int
        _LIB = lib
    return _LIB


def encode_plane(curr: np.ndarray, prev: np.ndarray) -> bytes:
    """1 ビット面 (0/1, 136x240) を BadCodec の 1 フレームにする (公式 Codec.py と同じバイト列)。"""
    c = np.ascontiguousarray(curr, np.uint8)
    p = np.ascontiguousarray(prev, np.uint8)
    out = np.empty(c.size // 8 + 64, np.uint8)
    n = _lib().bad_encode_frame(c.ctypes.data, p.ctypes.data, c.shape[1], c.shape[0], out.ctypes.data, out.size)
    if n < 0:
        raise RuntimeError("bad_encode_frame failed")
    return out[:n].tobytes()


def to565(bgr: np.ndarray) -> np.ndarray:
    b = bgr[..., 0].astype(np.uint16)
    g = bgr[..., 1].astype(np.uint16)
    r = bgr[..., 2].astype(np.uint16)
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def planes_of(img565: np.ndarray) -> list[np.ndarray]:
    return [((img565 >> b) & 1).astype(np.uint8) for b in range(16)]


# 色の細かさ (R, G, B のビット数) の段階。ESP32 にメモリが足りないと言われたら次へ下げる
LEVELS = [(5, 6, 5), (4, 5, 4), (3, 4, 3), (3, 4, 2), (3, 3, 2), (2, 3, 2)]


def bits_of_drop(drop_bits: int) -> tuple[int, int, int]:
    d = max(0, int(drop_bits))
    return (max(1, 5 - d), max(1, 6 - d), max(1, 5 - d))


def gray565(img: np.ndarray, bits: tuple[int, int, int]) -> np.ndarray:
    """各色の送る上位ビットを Gray 符号にする (下位の捨てたビットは 0 のまま)。
    隣り合う明るさで変わるビットが 1 つになり、下位のビット面のノイズが減る (可逆)。"""
    img = img.astype(np.uint16)
    out = np.zeros_like(img)
    for shift, width, n in ((11, 5, bits[0]), (5, 6, bits[1]), (0, 5, bits[2])):
        v = (img >> shift) & ((1 << width) - 1)
        v >>= width - n
        v ^= v >> 1
        out |= (v << (width - n)) << shift
    return out


def replicate565(img: np.ndarray, bits: tuple[int, int, int]) -> np.ndarray:
    """送った上位ビットを下位に繰り返して埋めた RGB565 (ESP32 の表示と同じ。例 R3 → r2 r1 r0 r2 r1)。"""
    img = img.astype(np.uint16)
    out = np.zeros_like(img)
    for shift, width, n in ((11, 5, bits[0]), (5, 6, bits[1]), (0, 5, bits[2])):
        q = ((img >> shift) & ((1 << width) - 1)) >> (width - n)
        v = np.zeros_like(img)
        pos = width
        while pos > 0:
            pos -= n
            v |= (q << pos) if pos >= 0 else (q >> -pos)
        out |= (v & ((1 << width) - 1)) << shift
    return out


def parse_color(s) -> tuple[int, int, int]:
    """'343' → (3, 4, 3)。R 1-5, G 1-6, B 1-5。"""
    s = str(s).strip()
    if len(s) != 3 or not s.isdigit():
        raise ValueError(f"色のビット数は 343 のように R G B を 3 桁で: {s!r}")
    r, g, b = (int(c) for c in s)
    if not (1 <= r <= 5 and 1 <= g <= 6 and 1 <= b <= 5):
        raise ValueError(f"色のビット数は R 1-5, G 1-6, B 1-5: {s!r}")
    return (r, g, b)


def mask_of(bits: tuple[int, int, int]) -> int:
    """RGB565 のうち送るビット (各色の上位 r, g, b ビット)。"""
    r, g, b = bits
    return (((0x1F << (5 - r)) & 0x1F) << 11) | (((0x3F << (6 - g)) & 0x3F) << 5) | ((0x1F << (5 - b)) & 0x1F)


def planes_needed(bits: tuple[int, int, int]) -> int:
    return sum(bits)


class Encoder:
    """prev_free=True (既定): ver 3。受信側は前フレームを持たず、変わったブロックだけ液晶に直接描く
    (ESP32 のメモリはほぼ不要。無印 ESP32 でも 16 面 = 可逆で使える)。
    prev_free=False: ver 2。面ごとに BadCodec のフレームを送る (前フレームを使う命令も使えて小さいが、
    受信側に (面の数 + 1) x 4KB 要る)。"""

    def __init__(self, drop_bits: int = 0, bits: tuple[int, int, int] | None = None, prev_free: bool = True,
                 gray: bool = False, hold: float = 0.6):
        self.bits = tuple(bits) if bits else bits_of_drop(drop_bits)
        self.prev_free = prev_free
        self.gray = gray and prev_free  # Gray 符号は ver 3 だけ (frame-stream-10 以降)
        # 色を減らすとき (ver 3): 四捨五入で段階を選び、ESP32 は送らない下位ビットを上位ビットの複製で埋める
        # (flags bit2。白が白になり暗部がつぶれない)。hold: 前の段階から hold 段以内の変化なら前の段階のまま
        # (段階の境目で色が行き来するちらつきを防ぐ。変化ブロックも減る)
        self.smooth = prev_free and self.bits != (5, 6, 5)
        self.hold = hold
        self._q_prev: list[np.ndarray] | None = None
        self.last_display: np.ndarray | None = None  # 液晶に出る RGB565 (136x240, テスト用)
        self.prev: list[np.ndarray] | None = None
        self.prev565: np.ndarray | None = None
        self.force_key = True
        self.changed_blocks = 0

    def request_key(self) -> None:
        self.force_key = True

    def _mask(self) -> int:
        return mask_of(self.bits)

    def encode(self, bgr: np.ndarray) -> bytes:
        assert bgr.shape[:2] == (135, W), bgr.shape
        mask = self._mask()
        src = np.concatenate([bgr, bgr[-1:]], axis=0)
        if self.smooth:
            img = self._quantize(src)
            self.last_display = replicate565(img, self.bits)
        else:
            img = to565(src) & mask
            self.last_display = img
        if self.prev_free:
            if self.gray:
                img = gray565(img, self.bits)
            return self._encode_abs(np.ascontiguousarray(img, np.uint16), mask)
        planes = planes_of(img)
        key = self.force_key or self.prev is None
        self.force_key = False
        prev = [np.zeros((H, W), np.uint8)] * 16 if key else self.prev
        out = bytearray(b"B6\x02" + bytes([1 if key else 0]) + struct.pack("<H", mask))
        for b in range(16):
            if mask >> b & 1:
                data = encode_plane(planes[b], prev[b])
                out += struct.pack("<H", len(data)) + data
        self.prev = planes
        return bytes(out)

    def _quantize(self, src: np.ndarray) -> np.ndarray:
        """各色を n ビットに四捨五入 (+ ヒステリシス) して RGB565 の上位ビットに置く。"""
        qs = []
        for ch, n in ((2, self.bits[0]), (1, self.bits[1]), (0, self.bits[2])):
            v = src[..., ch].astype(np.float32)
            levels = (1 << n) - 1
            q = np.rint(v * levels / 255.0)
            if self._q_prev is not None and self.hold > 0:
                p = self._q_prev[len(qs)]
                keep = np.abs(v - p * 255.0 / levels) <= self.hold * 255.0 / levels
                q = np.where(keep, p, q)
            qs.append(q.astype(np.uint16))
        self._q_prev = qs
        r, g, b = qs
        return ((r << (5 - self.bits[0])) << 11) | ((g << (6 - self.bits[1])) << 5) | (b << (5 - self.bits[2]))

    def _encode_abs(self, img: np.ndarray, mask: int) -> bytes:
        key = self.force_key or self.prev565 is None
        self.force_key = False
        prev = None if key else self.prev565
        out = np.empty(W * H * 2 + 1024, np.uint8)
        ch = ctypes.c_int()
        n = _lib().bad16_encode_abs(img.ctypes.data, None if prev is None else prev.ctypes.data, W, H, mask,
                                    out.ctypes.data, out.size, ctypes.byref(ch))
        if n < 0:
            raise RuntimeError("bad16_encode_abs failed")
        self.changed_blocks = ch.value
        self.prev565 = img
        flags = (1 if key else 0) | (2 if self.gray else 0) | (4 if self.smooth else 0)
        return b"B6\x03" + bytes([flags]) + struct.pack("<H", mask) + out[:n].tobytes()


# ------------------------------------------------------------------ YCoCg 版 (ver 4) --
# 明るさ (Y) と色差 (Co, Cg) に分けてからビット面にする。RGB の 3 色は同じ輪郭を持つので、RGB のまま面にすると
# 輪郭が 3 回送られる。YCoCg なら輪郭は Y に集まり、色差の面はほとんど平ら (小さく圧縮できる)。
# 色差は 4:2:0 (縦横半分) にもできる。戻すのは足し算だけ (t = Y - Cg, G = Y + Cg, B = t - Co, R = t + Co)。
#
# コンテナ: 'B' '6' 5 flags mode ny nc + 本体 (pc/native/bad_encode.c の bad16_encode_ycc)
#   16x16 単位で、変わったものは先頭 1 バイトに「どの 8x8 が変わったか」。変わった 8x8 だけ送り・描く
#   flags bit0 = キーフレーム, bit1 = 各値を Gray 符号にしてある
#   mode 0 = 4:4:4 / 1 = 4:2:0 (どちらも非可逆: Y・色差を 8 ビットにしてから上位 ny / nc ビット)
#        2 = 可逆 (RGB565 そのものを YCoCg-R に: Y 6 ビット、Co / Cg 7 ビット (+64)。4:4:4)
#   画面は 240 x 144 (135 行 + 9 行の余白。16 の倍数)
YCC_H = 144
YCC_MODES = {"444": 0, "420": 1, "lossless": 2}


def rep_bits(q, n: int):
    """n ビットの値を 8 ビットに広げる (上位ビットを下位に繰り返す。ESP32 も同じ計算)"""
    v = np.asarray(q, np.int32) << (8 - n)
    s = n
    while s < 8:
        v = v | (v >> s)
        s *= 2
    return v & 0xFF


def _nearest_lut(n: int) -> np.ndarray:
    """0..255 → rep_bits で一番近くに戻る n ビットの値"""
    reps = rep_bits(np.arange(1 << n), n)
    return np.abs(np.arange(256)[:, None] - reps[None, :]).argmin(axis=1).astype(np.uint8)


def gray8(v):
    return v ^ (v >> 1)


class YccEncoder:
    """BadCodec YCoCg 版。mode: "444" / "420" / "lossless"。ny / nc: Y / 色差のビット数 (lossless では無視)"""

    def __init__(self, mode: str = "420", ny: int = 6, nc: int = 5, gray: bool = True):
        if mode not in YCC_MODES:
            raise ValueError(f"mode: {mode}")
        self.mode = mode
        self.ny, self.nc = (6, 7) if mode == "lossless" else (int(ny), int(nc))
        if not (1 <= self.ny <= 8 and 1 <= self.nc <= 8):
            raise ValueError("ny / nc は 1〜8")
        self.gray = gray
        self.prev = None
        self.force_key = True
        self.changed_blocks = 0
        self.last_display: np.ndarray | None = None  # 液晶に出る RGB565 (135x240, テスト用)
        if mode != "lossless":
            self._qy, self._qc = _nearest_lut(self.ny), _nearest_lut(self.nc)

    def request_key(self) -> None:
        self.force_key = True

    def planes(self) -> int:
        return self.ny + 2 * self.nc

    def label(self) -> str:
        return "YCoCg 可逆" if self.mode == "lossless" else f"YCoCg {self.mode} Y{self.ny}C{self.nc}"

    def _channels(self, bgr: np.ndarray):
        """BGR (144x240) → 量子化した Y, Co, Cg (uint8) と、液晶に出る RGB565"""
        if self.mode == "lossless":
            r = (bgr[..., 2] >> 3).astype(np.int32)
            g = (bgr[..., 1] >> 2).astype(np.int32)
            b = (bgr[..., 0] >> 3).astype(np.int32)
            co = r - b
            t = b + (co >> 1)
            cg = g - t
            y = t + (cg >> 1)
            disp = ((r << 11) | (g << 5) | b).astype(np.uint16)
            return y.astype(np.uint8), (co + 64).astype(np.uint8), (cg + 64).astype(np.uint8), disp
        f = bgr.astype(np.int32)
        b, g, r = f[..., 0], f[..., 1], f[..., 2]
        y8 = (r + 2 * g + b + 2) >> 2
        co8 = np.clip((r - b + 256) >> 1, 0, 255)
        cg8 = np.clip((-r + 2 * g - b + 512 + 2) >> 2, 0, 255)
        if self.mode == "420":  # 2x2 の平均
            h2, w2 = co8.shape[0] // 2, co8.shape[1] // 2
            co8 = (co8.reshape(h2, 2, w2, 2).sum(axis=(1, 3)) + 2) >> 2
            cg8 = (cg8.reshape(h2, 2, w2, 2).sum(axis=(1, 3)) + 2) >> 2
        qy, qco, qcg = self._qy[y8], self._qc[co8], self._qc[cg8]
        return qy, qco, qcg, self.display(qy, qco, qcg)

    def display(self, qy, qco, qcg) -> np.ndarray:
        """ESP32 と同じ計算で、量子化した値から液晶の RGB565 を作る"""
        y = rep_bits(qy, self.ny)
        co = rep_bits(qco, self.nc) - 128
        cg = rep_bits(qcg, self.nc) - 128
        if self.mode == "420":
            co = np.repeat(np.repeat(co, 2, axis=0), 2, axis=1)
            cg = np.repeat(np.repeat(cg, 2, axis=0), 2, axis=1)
        t = y - cg
        g = np.clip(y + cg, 0, 255)
        b = np.clip(t - co, 0, 255)
        r = np.clip(t + co, 0, 255)
        return (((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)).astype(np.uint16)

    def encode(self, bgr: np.ndarray) -> bytes:
        assert bgr.shape[:2] == (135, W), bgr.shape
        src = np.concatenate([bgr, np.repeat(bgr[-1:], YCC_H - 135, axis=0)], axis=0)
        y, co, cg, disp = self._channels(src)
        self.last_display = disp[:135]
        if self.gray:
            y, co, cg = gray8(y), gray8(co), gray8(cg)
        cur = tuple(np.ascontiguousarray(a, np.uint8) for a in (y, co, cg))
        key = self.force_key or self.prev is None
        self.force_key = False
        prev = (None, None, None) if key else self.prev
        out = np.empty(W * YCC_H * 4 + 1024, np.uint8)
        ch = ctypes.c_int()
        lib = _lib()
        if not hasattr(lib, "bad16_encode_ycc"):
            raise RuntimeError("bad_encode が古い (YCoCg 版が無い)。pc/native を作り直してください")
        n = lib.bad16_encode_ycc(*(a.ctypes.data for a in cur),
                                 *(None if a is None else a.ctypes.data for a in prev),
                                 W, YCC_H, 1 if self.mode == "420" else 0, self.ny, self.nc,
                                 out.ctypes.data, out.size, ctypes.byref(ch))
        if n < 0:
            raise RuntimeError("bad16_encode_ycc failed")
        self.changed_blocks = ch.value
        self.prev = cur
        flags = (1 if key else 0) | (2 if self.gray else 0)
        return b"B6\x05" + bytes([flags, YCC_MODES[self.mode], self.ny, self.nc]) + out[:n].tobytes()


# ------------------------------------------------------- 1 フレームまるごとのエントロピー符号 --
# BadCodec の出力 ('B' '6' ...) を、さらにハフマン ('B' 'H') か tANS ('B' 'A') で小さくする。
# ESP32 は受け取りながら戻して、そのまま BadCodec の展開に渡す (firmware frame-stream-35 以降)。
ENTROPY = {"huff": 11, "tans": 10}  # ハフマンの符号長の上限 / tANS の表の大きさ (2^10)


def entropy_wrap(data: bytes, kind: str) -> bytes:
    """kind: "huff" / "tans"。小さくならなかったら元のまま返す"""
    lib = _lib()
    fn = {"huff": "bad_entropy_huff", "tans": "bad_entropy_tans"}[kind]
    if not hasattr(lib, fn):
        raise RuntimeError("bad_encode が古い (エントロピー符号が無い)。pc/native を作り直してください")
    src = np.frombuffer(data, np.uint8)
    out = np.empty(len(data) + 1024, np.uint8)
    n = getattr(lib, fn)(src.ctypes.data, len(data), out.ctypes.data, out.size, ENTROPY[kind])
    if n < 0 or n >= len(data):
        return data
    return out[:n].tobytes()


def parse_ycc(name: str):
    """'YCoCg 420 Y6C5' / 'YCoCg 可逆' → (mode, ny, nc)。YCoCg でなければ None"""
    import re

    if not name.startswith("YCoCg"):
        return None
    if "可逆" in name or "lossless" in name:
        return ("lossless", 6, 7)
    m = re.search(r"(444|420)\s*Y(\d)\s*C(\d)", name)
    if not m:
        raise ValueError(f"YCoCg の形式: {name!r} (例 'YCoCg 420 Y6C5')")
    return (m.group(1), int(m.group(2)), int(m.group(3)))


# ------------------------------------------------------------- 参照デコーダ (テスト用) --
def official_tools() -> Path | None:
    """公式 BadCodec の tools/ (Codec.py)。環境変数 BADCODEC_TOOLS か、このリポジトリと同じ階層の clone。"""
    import os

    cands = [os.environ.get("BADCODEC_TOOLS")]
    root = Path(__file__).resolve().parents[3]
    cands += [root / "ghostinkoma" / "badcodec" / "tools", root / "badcodec" / "tools", root / "BadCodec" / "tools"]
    for c in cands:
        if c and (Path(c) / "Codec.py").exists():
            return Path(c)
    return None


def decode(data: bytes, prev_planes: list[np.ndarray] | None) -> tuple[np.ndarray, list[np.ndarray]]:
    """公式 Codec.py の decode_frame で 16 面を展開して RGB565 (135x240) を返す。"""
    tools = official_tools()
    if tools is None:
        raise RuntimeError("公式 BadCodec の tools/Codec.py が見つかりません (BADCODEC_TOOLS で指定)")
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    import Codec  # 公式

    assert data[:2] == b"B6" and data[2] in (1, 2), "ver 3 は tests/bad16_decode_test.cpp で確認する"
    key = data[3] & 1
    mask, pos = (0xFFFF, 4) if data[2] == 1 else (struct.unpack_from("<H", data, 4)[0], 6)
    planes = []
    for b in range(16):
        if not mask >> b & 1:
            planes.append(np.zeros((H, W), np.uint8))
            continue
        n = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        prev = np.zeros((H, W), np.uint8) if key or prev_planes is None else prev_planes[b]
        planes.append(np.asarray(Codec.decode_frame(data[pos:pos + n], prev, W, H), np.uint8))
        pos += n
    img = np.zeros((H, W), np.uint16)
    for b in range(16):
        img |= planes[b].astype(np.uint16) << b
    return img[:135], planes
