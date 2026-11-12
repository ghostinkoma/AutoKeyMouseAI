"""akm/badvideo.py: FFmpeg で作った試験の動画 → .bad (量の調整あり) → ESP32 と同じデコーダで、
出した 1 枚 1 枚がその時刻の元の絵に近い (飛ばした分の差分も崩れない)。再生は偽の ESP32 に全部届く。"""
import shutil
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import pytest

PC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PC))

from akm import badvideo  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("g++"), reason="ffmpeg / g++ が無い")


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    p = tmp_path_factory.mktemp("v") / "test.avi"
    # 動く色の模様 (毎フレーム画面全体が変わる) + 4 秒、24fps
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=4",
                    "-c:v", "mpeg4", "-q:v", "3", str(p)], check=True)
    return p


@pytest.fixture(scope="module")
def decoder(tmp_path_factory):
    exe = tmp_path_factory.mktemp("dec") / "bad16_decode_test"
    fw = PC.parent / "firmware" / "AutoKeyMouse"
    subprocess.run(["g++", "-std=c++17", "-O1", "-o", str(exe), str(PC / "tests" / "bad16_decode_test.cpp"),
                    str(fw / "bad_decode.cpp"), str(fw / "entropy.cpp")], check=True)
    return exe


def decode_all(exe, datas):
    stdin = b"".join(struct.pack("<I", len(d)) + d for d in datas)
    out = subprocess.run([str(exe)], input=stdin, capture_output=True, check=True).stdout
    n = 240 * 135 * 2
    return [np.frombuffer(out[i * n:(i + 1) * n], "<u2").reshape(135, 240) for i in range(len(datas))]


def to_bgr(img565):
    from akm.gate_live import from565

    return from565(img565).astype(np.int32)


@pytest.mark.parametrize("fmt,kbps", [("index", 150.0), ("565", 150.0), ("mono", 15.0)])
def test_encode_with_rate_limit_decodes_to_source(tmp_path, video, decoder, fmt, kbps):
    out = tmp_path / f"v.{fmt}.bad"
    st = badvideo.encode_file(video, out, fmt, 24.0, kbps, log=lambda *a: None)
    h = badvideo.read_header(out)
    assert (h.fmt, h.frames, h.source_frames) == (fmt, st.written, st.source) and st.source == 96
    frames = list(badvideo.read_frames(out))
    assert len(frames) == st.written and st.skipped > 0  # 量が足りないので飛ばした
    ts = [t for t, _ in frames]
    assert ts == sorted(ts) and frames[0][1][3] & 1  # 時刻の順・最初は全体
    total = sum(len(d) for _t, d in frames)
    assert total / 4.0 <= kbps * 1024 * 1.6  # 1 秒あたりの量がおおむね上限内 (始めの貯めの分だけ超えてよい)
    src = list(badvideo.frames_of(video, 24.0))
    got = decode_all(decoder, [d for _t, d in frames])
    for (t, _d), g in zip(frames, got):
        s = src[round(t * 24 / 1000)]
        if fmt == "mono":  # 白黒: その時刻の絵を 8x8 ディザにしたものと、ぴったり同じ
            from akm.gate_live import to_mono

            assert ((g == 0xFFFF) == (to_mono(s, 128, "8x8") == 255)).mean() > 0.999, t
            continue
        err = np.abs(to_bgr(g) - s.astype(np.int32)).mean()
        assert err < 14, (fmt, t, err)  # 飛ばした後も、その時刻の絵になっている (差分が崩れていない)


def test_play_sends_all_frames_to_fake_esp32(tmp_path, video, monkeypatch):
    out = tmp_path / "v.index.bad"
    badvideo.encode_file(video, out, "index", 24.0, 0, duration=1.0, log=lambda *a: None)
    want = [d for _t, d in badvideo.read_frames(out)]
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    got = []

    def esp():
        c, _ = srv.accept()
        f = c.makefile("rb")
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            assert hdr[:4] == b"AKMF"
            got.append(f.read(struct.unpack("<I", hdr[4:])[0]))
            c.sendall(b"K")
        c.close()

    th = threading.Thread(target=esp, daemon=True)
    th.start()
    monkeypatch.setattr(badvideo, "check_firmware", lambda *a, **k: None)
    st = badvideo.play(out, "127.0.0.1", port, speed=4.0, log=lambda *a: None)
    th.join(5)
    assert st.sent == len(want) and got == want
