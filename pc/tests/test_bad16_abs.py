"""BadCodec の前フレーム不要版 (ver 3 / 7) と tANS: C エンコーダ → ESP32 と同じ C++ デコーダ (firmware の bad16.h)
で、エンコーダが計算した液晶の絵 (last_display) とぴったり同じになる。"""
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

PC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PC))

from akm.bad16 import to565  # noqa: E402


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
    exe = tmp_path_factory.mktemp("bad16") / "bad16_decode_test"
    fw = PC.parent / "firmware" / "AutoKeyMouse"
    subprocess.run([gxx, "-std=c++17", "-O1", "-o", str(exe), str(PC / "tests" / "bad16_decode_test.cpp"),
                    str(fw / "bad_decode.cpp"), str(fw / "entropy.cpp")], check=True)
    return exe


def run_decoder(exe, frames):
    stdin = b"".join(struct.pack("<I", len(d)) + d for d in frames)
    out = subprocess.run([str(exe)], input=stdin, capture_output=True, check=True).stdout
    n = 240 * 135 * 2
    assert len(out) == n * len(frames)
    return [np.frombuffer(out[i * n:(i + 1) * n], "<u2").reshape(135, 240) for i in range(len(frames))]


def test_mono_single_plane_matches_esp32_decoder(decoder):
    """白黒 1 ビット: bit15 の 1 面だけ送り (mask 0x8000)、ESP32 が白 / 黒に広げる。ディザの模様もそのまま"""
    from akm.bad16 import Encoder
    from akm.gate_live import to_mono

    cv2 = pytest.importorskip("cv2")
    enc = Encoder(bits=(1, 1, 1))
    datas, expect = [], []
    for f in frames_of():
        bw = cv2.cvtColor(to_mono(f, 128, "8x8"), cv2.COLOR_GRAY2BGR)
        datas.append(enc.encode(bw))
        expect.append(enc.last_display[:135].copy())
        assert set(np.unique(expect[-1])) <= {0, 0xFFFF}
    assert datas[0][:3] == b"B6\x03" and datas[0][4:6] == b"\x00\x80"
    got = run_decoder(decoder, datas)
    for i, (g, e) in enumerate(zip(got, expect)):
        assert (g == e).all(), f"frame {i}"
    assert (got[0] == 0xFFFF).any() and (got[0] == 0).any()


def test_dither_8x8_follows_badcodec_blocks():
    """8x8 の組織的ディザ: 同じ明るさなら全部の 8x8 ブロックが同じ模様 (ブロックの区切りにそろう)、
    白の数 = 明るさ x 64 / 256 (65 段階)、同じ絵なら同じ模様 (差分が効く)"""
    from akm.gate_live import to_mono

    pytest.importorskip("cv2")
    for v in range(0, 256, 4):
        img = np.full((136, 240, 3), v, np.uint8)
        a = to_mono(img, 128, "8x8")
        assert (a == to_mono(img, 128, "8x8")).all()
        blocks = a.reshape(17, 8, 30, 8).transpose(0, 2, 1, 3).reshape(-1, 8, 8)
        assert (blocks == blocks[0]).all()
        assert abs(int((blocks[0] == 255).sum()) - v / 4) <= 1
    img = np.full((135, 240, 3), 100, np.uint8)
    assert (to_mono(img, 128, "なし") == 0).all() and (to_mono(img, 90, "なし") == 255).all()


@pytest.mark.parametrize("kind", ["tans"])
@pytest.mark.parametrize("make", ["rgb343", "rgb565", "index"])
def test_entropy_wrap_roundtrip(decoder, kind, make):
    """BadCodec のフレームを tANS で包んでも、ESP32 と同じコードで同じ絵に戻る (小さくもなる)"""
    from akm.bad16 import Encoder, IndexEncoder, entropy_wrap

    enc = {"rgb343": lambda: Encoder(bits=(3, 4, 3), gray=True), "rgb565": lambda: Encoder(bits=(5, 6, 5), gray=True),
           "index": lambda: IndexEncoder()}[make]()
    srcs = frames_of()
    raw, wrapped, expect = [], [], []
    for f in srcs:
        d = enc.encode(f)
        raw.append(d)
        wrapped.append(entropy_wrap(d, kind))
        expect.append(enc.last_display[:135].copy())
    assert wrapped[0][:2] == b"BA"
    assert len(wrapped[0]) < len(raw[0])
    got = run_decoder(decoder, wrapped)
    for i, (g, e) in enumerate(zip(got, expect)):
        assert (g == e).all(), f"frame {i}"


def test_entropy_random_bytes_roundtrip(tmp_path):
    """どんなバイト列でも元に戻る (C のエンコーダ → ESP32 と同じ C++ デコーダ)"""
    from akm.bad16 import entropy_wrap

    gxx = shutil.which("g++")
    if not gxx:
        pytest.skip("g++ が無い")
    src = tmp_path / "t.cpp"
    src.write_text("""
#include <cstdio>
#include <cstring>
#include <vector>
#include "%s"
struct M { const uint8_t* p; size_t left; };
static bool rd(void* c, uint8_t* d, size_t n) { M* m = (M*)c; if (n > m->left) return false; memcpy(d, m->p, n); m->p += n; m->left -= n; return true; }
int main() {
  std::vector<uint8_t> b; int ch; while ((ch = getchar()) != EOF) b.push_back((uint8_t)ch);
  M m{b.data() + 2, b.size() - 2};
  if (!ent::begin(ent::dec(), b[1], rd, &m, (uint32_t)b.size())) return 1;
  std::vector<uint8_t> out(ent::dec().outLeft);
  if (!ent::read(&ent::dec(), out.data(), out.size())) return 2;
  fwrite(out.data(), 1, out.size(), stdout);
}
""" % (PC.parent / "firmware" / "AutoKeyMouse" / "entropy.h").as_posix())
    exe = tmp_path / "t"
    subprocess.run([gxx, "-std=c++17", "-O1", "-o", str(exe), str(src),
                    str(PC.parent / "firmware" / "AutoKeyMouse" / "entropy.cpp")], check=True)
    rng = np.random.default_rng(1)
    for kind in ("tans",):
        for data in (bytes(rng.integers(0, 4, 5000, dtype=np.uint8)),          # 偏りが大きい
                     bytes(rng.geometric(0.05, 20000).clip(0, 255).astype(np.uint8)),  # 長い裾
                     bytes([7]) * 3000 + bytes([9]) * 5):                          # ほぼ 1 種類
            w = entropy_wrap(data, kind)
            assert w[:2] == b"BA" and len(w) < len(data)
            out = subprocess.run([str(exe)], input=w, capture_output=True, check=True).stdout
            assert out == data, kind


def test_index_color_roundtrip(decoder):
    """インデックスカラー (ver 7): ESP32 と同じコードで、PC が計算した液晶の絵とぴったり同じ。
    パレットは使い回し (差分フレームは小さい)、場面が変わったら作り直してキーフレームで送る"""
    from akm.bad16 import IndexEncoder, entropy_wrap

    enc = IndexEncoder(partial=False)  # 作り直しの動き (部分更新は test_index_partial_palette_update)
    srcs = frames_of()
    other = np.ascontiguousarray(255 - srcs[0][:, ::-1])  # 色ががらっと変わる場面
    srcs = srcs[:4] + [other, other.copy()]
    datas, expect = [], []
    for f in srcs:
        datas.append(enc.encode(f))
        expect.append(enc.last_display.copy())
    assert datas[0][:3] == b"B6\x07" and datas[0][3] & 1
    # 2 枚目 → 3 枚目は四角が動くだけ: パレットは送らず、変わった所だけ
    assert not datas[1][3] & 1 and not datas[2][3] & 1 and len(datas[2]) < len(datas[0]) // 3
    assert datas[4][3] & 1 and enc.rebuilds == 2  # 場面が変わった: 作り直してキーフレーム
    assert len(datas[5]) < 60  # 変化なし
    got = run_decoder(decoder, datas)
    for i, (g, e) in enumerate(zip(got, expect)):
        assert (g == e).all(), f"frame {i}: {int((g != e).sum())} 画素違う"
    got = run_decoder(decoder, [entropy_wrap(d, "tans") for d in datas])  # tANS で包んでも同じ
    assert all((g == e).all() for g, e in zip(got, expect))


def test_rgb565_worst_case_noise_fits(decoder):
    """画面全体が細かいノイズ (16 面すべて MASTER_BLOCK) でも符号化に失敗せず、ESP32 と同じ絵に戻る
    (前は 66KB の置き場があふれて bad16_encode_abs failed になった)"""
    from akm.bad16 import Encoder

    rng = np.random.default_rng(3)
    enc = Encoder(bits=(5, 6, 5))
    frames = [rng.integers(0, 256, (135, 240, 3), dtype=np.uint8) for _ in range(2)]
    datas, expect = [], []
    for f in frames:
        datas.append(enc.encode(f))
        expect.append(enc.last_display[:135].copy())
    assert len(datas[0]) > 240 * 136 * 2  # RGB565 そのものより大きい
    got = run_decoder(decoder, datas)
    for g, e in zip(got, expect):
        assert (g == e).all()


def test_index_partial_palette_update(decoder):
    """新しい色が出てきても、パレットを作り直さず使われていない番号だけを入れ替える (キーフレームにしない)。
    入れ替えた番号を前に描いていたブロックは送り直すので、ESP32 と同じコードでぴったり同じ絵になる"""
    from akm.bad16 import IndexEncoder

    yy, xx = np.mgrid[0:135, 0:240]
    base = np.stack([(xx * 255 // 239), (yy * 255 // 134), ((xx + yy) * 255 // 373)], -1).astype(np.uint8)
    frames = [base.copy() for _ in range(5)]
    rng = np.random.default_rng(7)
    # 2 枚目から: 左上に、パレットに無い鮮やかな色の四角 (動く)。4 枚目でまた別の色
    for i in (1, 2, 3, 4):
        x = 10 + 12 * i
        col = (255, 0, 255) if i < 3 else (0, 255, 64)
        frames[i][20:80, x:x + 60] = col
        frames[i][20:80, x:x + 60] = np.clip(frames[i][20:80, x:x + 60].astype(int)
                                            + rng.integers(-30, 30, (60, 60, 3)), 0, 255)
    enc = IndexEncoder()
    datas, expect = [], []
    for f in frames:
        datas.append(enc.encode(f))
        expect.append(enc.last_display.copy())
    assert datas[0][3] & 1  # 最初だけキーフレーム
    assert enc.rebuilds == 1 and enc.swaps >= 1  # 作り直さず、入れ替えで済んだ
    assert all(not d[3] & 1 for d in datas[1:])
    swapped = [d for d in datas[1:] if d[6:38] != bytes(32)]
    assert swapped and all(len(d) < len(datas[0]) for d in swapped)  # 全体を送るより小さい
    got = run_decoder(decoder, datas)
    for i, (g, e) in enumerate(zip(got, expect)):
        assert (g == e).all(), f"frame {i}: {int((g != e).sum())} 画素違う"
    # 部分更新なし (前と同じ) だと作り直してキーフレームになる
    old = IndexEncoder(partial=False)
    for f in frames:
        old.encode(f)
    assert old.rebuilds > 1
