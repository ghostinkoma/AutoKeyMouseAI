"""BadCodec YCoCg 版 (ver 5): C エンコーダ → ESP32 と同じ C++ デコーダ (firmware の bad16.h) で、
エンコーダが計算した液晶の絵 (last_display) とぴったり同じになる。可逆は RGB565 そのものに戻る。"""
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

PC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PC))

from akm.bad16 import YccEncoder, parse_ycc, to565  # noqa: E402


def frames_of(seed=0, n=6):
    """ゲームっぽい絵: なめらかな色の背景 + 動く四角 + 細い線 (前フレームとの差分も試す)"""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:135, 0:240]
    base = np.stack([(xx * 255 // 239), (yy * 255 // 134), ((xx + yy) * 255 // 373)], -1).astype(np.uint8)
    noise = rng.integers(0, 40, (135, 240, 3), dtype=np.uint8)
    out = []
    for i in range(n):
        f = base.copy()
        if i % 3 == 0:
            f = np.clip(f.astype(int) + noise, 0, 255).astype(np.uint8)
        x = 20 + 13 * i
        f[40:70, x:x + 30] = (30, 200, 250)
        f[100, :] = (255, 255, 255)
        out.append(f)
    out.append(out[-1].copy())  # 変化なし
    return out


@pytest.fixture(scope="module")
def decoder(tmp_path_factory):
    gxx = shutil.which("g++")
    if not gxx or (sys.platform != "win32" and not shutil.which("gcc")):
        pytest.skip("g++ / gcc が無い")
    exe = tmp_path_factory.mktemp("ycc") / "bad16_decode_test"
    fw = PC.parent / "firmware" / "AutoKeyMouse"
    subprocess.run([gxx, "-std=c++17", "-O1", "-o", str(exe), str(PC / "tests" / "bad16_decode_test.cpp"),
                    str(fw / "bad_decode.cpp")], check=True)
    return exe


def run_decoder(exe, frames):
    stdin = b"".join(struct.pack("<I", len(d)) + d for d in frames)
    out = subprocess.run([str(exe)], input=stdin, capture_output=True, check=True).stdout
    n = 240 * 135 * 2
    assert len(out) == n * len(frames)
    return [np.frombuffer(out[i * n:(i + 1) * n], "<u2").reshape(135, 240) for i in range(len(frames))]


@pytest.mark.parametrize("mode,ny,nc,gray", [("420", 6, 5, True), ("420", 4, 4, True), ("444", 6, 5, True),
                                             ("444", 5, 4, False), ("420", 5, 3, False), ("lossless", 6, 7, True)])
def test_ycc_roundtrip_matches_esp32_decoder(decoder, mode, ny, nc, gray):
    enc = YccEncoder(mode, ny, nc, gray)
    srcs = frames_of()
    datas, expect = [], []
    for f in srcs:
        datas.append(enc.encode(f))
        expect.append(enc.last_display.copy())
    assert datas[0][:3] == b"B6\x05" and datas[0][3] & 1 and not datas[1][3] & 1
    assert len(datas[-1]) < 20  # 変化なし: SKIP だけ
    got = run_decoder(decoder, datas)
    for i, (g, e) in enumerate(zip(got, expect)):
        assert (g == e).all(), f"frame {i}: {int((g != e).sum())} 画素違う"
    if mode == "lossless":
        for g, f in zip(got, srcs):
            assert (g == to565(f)).all()  # 可逆: RGB565 そのもの


def test_ycc_is_close_to_source():
    """非可逆でも元の色に近い (4:2:0 Y6C5 の平均誤差は 8 ビット換算で数段階)"""
    enc = YccEncoder("420", 6, 5)
    f = frames_of(n=1)[0]
    enc.encode(f)
    d = enc.last_display.astype(np.int32)
    r, g, b = (d >> 11) << 3, ((d >> 5) & 63) << 2, (d & 31) << 3
    err = np.abs(np.stack([b, g, r], -1) - f.astype(np.int32)).mean()
    assert err < 8, err


def test_parse_ycc():
    assert parse_ycc("YCoCg 420 Y6C5") == ("420", 6, 5)
    assert parse_ycc("YCoCg 444 Y6 C5") == ("444", 6, 5)
    assert parse_ycc("YCoCg 可逆") == ("lossless", 6, 7)
    assert parse_ycc("BadCodec 343") is None


def test_ycc_sends_only_changed_8x8(decoder):
    """1 画素だけ変わったら、16x16 のうち変わった 8x8 だけを送る (前は 16x16 を丸ごと)"""
    enc = YccEncoder("420", 6, 5)
    a = frames_of(n=1)[0]
    b = a.copy()
    b[5, 5] = (255, 0, 255)  # 左上のマクロブロックの左上の 8x8 だけ
    d0, e0 = enc.encode(a), enc.last_display.copy()
    d1, e1 = enc.encode(b), enc.last_display.copy()
    assert d1[7] == 0x01  # 最初のマクロブロック: 左上の 8x8 だけ
    got = run_decoder(decoder, [d0, d1])
    assert (got[0] == e0).all() and (got[1] == e1).all()
    assert len(d1) < 7 + 1 + 6 * 9 + 2 * 5 * 9 + 8  # mask + Y 6 面 + 色差 10 面 (1 ブロックずつ) + SKIP
