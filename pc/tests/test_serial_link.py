"""akm/serial_link.py: 偽の ESP32 (疑似端末) を相手に、切り替えの手順・返事の見分け・終わりの合図を確かめる。"""
import os
import struct
import sys
import threading

import pytest

pytestmark = pytest.mark.skipif(not hasattr(os, "openpty") or sys.platform == "win32", reason="疑似端末が要る")


def fake_esp(master, got):
    buf = b""

    def need(pred):
        nonlocal buf
        while not pred():
            buf += os.read(master, 65536)

    need(lambda: b"stream" in buf and b"\n" in buf.split(b"stream", 1)[1])
    got["cmd"] = buf.split(b"stream", 1)[1].split(b"\n", 1)[0].strip()
    buf = b""
    os.write(master, b"[BLE] log line\nOK STREAM " + got["cmd"] + b"\n")
    while True:
        need(lambda: b"AKMF" in buf and len(buf) >= buf.index(b"AKMF") + 8)
        i = buf.index(b"AKMF")
        n = struct.unpack("<I", buf[i + 4:i + 8])[0]
        buf = buf[i + 8:]
        if n == 0:
            got["end"] = True
            return
        need(lambda: len(buf) >= n)
        got.setdefault("frames", []).append(buf[:n])
        buf = buf[n:]
        os.write(master, b"[WIFI] noise \xa5 " + b"\xa5\x5a" + (b"R" if len(got["frames"]) == 2 else b"K"))


def test_handshake_frames_acks_and_close():
    from akm.serial_link import SerialFrameLink

    master, slave = os.openpty()
    got = {}
    t = threading.Thread(target=fake_esp, args=(master, got), daemon=True)
    t.start()
    link = SerialFrameLink(os.ttyname(slave), 2_000_000, log=lambda *a: None)
    assert got["cmd"] == b"2000000"
    for i, want in enumerate((b"K", b"R", b"K")):
        link.sendall(b"AKMF" + struct.pack("<I", 3) + bytes([i, i, i]))
        assert link.recv(1) == want  # ログの文字や 0xA5 だけのものは読み飛ばす
    link.close()
    t.join(2)
    assert got.get("end") and got["frames"] == [b"\x00\x00\x00", b"\x01\x01\x01", b"\x02\x02\x02"]
