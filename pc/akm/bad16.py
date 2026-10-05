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

    def __init__(self, drop_bits: int = 0, bits: tuple[int, int, int] | None = None, prev_free: bool = True):
        self.bits = tuple(bits) if bits else bits_of_drop(drop_bits)
        self.prev_free = prev_free
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
        img = to565(np.concatenate([bgr, bgr[-1:]], axis=0)) & mask
        if self.prev_free:
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
        return b"B6\x03" + bytes([1 if key else 0]) + struct.pack("<H", mask) + out[:n].tobytes()


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
