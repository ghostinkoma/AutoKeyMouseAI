"""akm/virtual_hid.py: ESP32 と同じ書式・同じ動き・遅れの模擬・ゲートで止まることを、記録する出口で確かめる。"""
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from akm.device import DeviceError, VirtualTransport
from akm.virtual_hid import (LatencyPipe, MacroError, RecordingBackend, VirtualHid, key_from_char, key_from_name,
                             parse, scan_code, split_script)


def keys(rec):
    return [(e[2], e[3]) for e in rec.events if e[1] == "key"]


def hid(**kw):
    rec = RecordingBackend()
    kw.setdefault("event_delay_ms", 0)
    return VirtualHid(rec, **kw), rec


def test_key_names():
    assert key_from_name("a") == 0x04
    assert key_from_name("Z") == 0x1D
    assert key_from_name("1") == 0x1E and key_from_name("0") == 0x27
    assert key_from_name("F1") == 0x3A and key_from_name("f12") == 0x45 and key_from_name("f13") == 0x68
    assert key_from_name("enter") == 0x28 and key_from_name("ctrl") == 0xE0
    assert key_from_name("0x3a") == 0x3A
    assert key_from_name("nope") == 0 and key_from_name("f25") == 0


def test_jp_layout_chars():
    assert key_from_char("@", "jp") == (0x2F, False)
    assert key_from_char(":", "jp") == (0x34, False)
    assert key_from_char("\\", "jp") == (0x87, False)
    assert key_from_char("_", "jp") == (0x87, True)
    assert key_from_char("|", "jp") == (0x89, True)
    assert key_from_char("@", "us") == (0x1F, True)
    assert key_from_char("A", "jp") == (0x04, True)
    assert key_from_char("あ") is None


def test_split_and_parse():
    assert split_script(r"t:a\;b;k:enter") == ["t:a;b", "k:enter"]
    ops = parse("k:ctrl+shift+esc; m:10,-5\na:0,32767;c:L,2;bd:R;bu:R;s:-3;w:100;d:20;ra;# comment;t: hi ")
    assert [o.kind for o in ops] == ["tap", "rel", "abs", "click", "bdown", "bup", "wheel", "wait", "delay",
                                     "release", "text"]
    assert ops[0].keys == (0xE0, 0xE1, 0x29)
    assert ops[-1].text == " hi "  # t: は前後の空白も文字


@pytest.mark.parametrize("bad", ["x:1", "k:nokey", "m:1", "a:-1,0", "a:0,40000", "c:X", "c:L,0", "d:3", "w:-1",
                                 "t:日本", "k:a+b+c+d+e"])
def test_parse_errors(bad):
    with pytest.raises(MacroError, match=r"line 2: invalid command"):
        parse("k:a;" + bad)


def test_tap_order_and_text_shift():
    v, rec = hid()
    assert v.run("k:ctrl+shift+esc;t:A_") == "ok"
    assert keys(rec) == [(0xE0, True), (0xE1, True), (0x29, True), (0x29, False), (0xE1, False), (0xE0, False),
                         (0xE1, True), (0x04, True), (0x04, False), (0xE1, False),
                         (0xE1, True), (0x87, True), (0x87, False), (0xE1, False)]


def test_rel_and_wheel_split():
    v, rec = hid()
    v.run("m:300,-10;s:-200;c:L,2")
    ev = [e[1:] for e in rec.events]
    assert ev[:3] == [("rel", 127, -10), ("rel", 127, 0), ("rel", 46, 0)]
    assert ev[3:5] == [("wheel", -127), ("wheel", -73)]
    assert ev[5:] == [("buttons", 1), ("buttons", 0), ("buttons", 1), ("buttons", 0)]


def test_event_delay_and_d():
    v, rec = hid(event_delay_ms=10)
    t0 = time.perf_counter()
    v.run("k:a;d:20;k:b")  # 10+10 + 20+20
    assert 0.055 <= time.perf_counter() - t0 < 0.2


def test_latency_pipe_quantizes_and_keeps_order():
    got = []
    p = LatencyPipe(latency_ms=5, jitter_ms=20, report_interval_ms=15, seed=1)
    t0 = p.t0
    for i in range(20):
        p.put(lambda i=i: got.append((i, time.perf_counter() - t0)))
    assert p.flush(2.0)
    assert [i for i, _ in got] == list(range(20))
    for _, t in got:
        k = t / 0.015
        assert t >= 0.005 and abs(k - round(k)) * 15 < 4  # 15ms の区切りで届く (Linux のずれを少し許す)


def test_passthrough_is_synchronous():
    p = LatencyPipe()
    got = []
    p.put(lambda: got.append(1))
    assert got == [1] and p.passthrough


def test_latency_delays_delivery():
    v, rec = hid(latency_ms=30)
    v.run("k:a")
    assert len(keys(rec)) == 2 and rec.events[0][0] >= 25


def test_gate_stops_and_releases():
    allowed = {"v": True}
    v, rec = hid(event_delay_ms=5, allow=lambda: allowed["v"])
    threading.Timer(0.05, lambda: allowed.update(v=False)).start()
    with pytest.raises(InterruptedError, match="gate"):
        v.run("kd:shift;bd:L;w:1000;k:a")
    assert rec.events[-1][1] == "release"
    assert ("key", 0x04, True) not in [e[1:] for e in rec.events]


def test_stop_interrupts_wait():
    v, rec = hid()
    threading.Timer(0.05, v.stop).start()
    t0 = time.perf_counter()
    with pytest.raises(InterruptedError, match="stopped"):
        v.run("w:5000")
    assert time.perf_counter() - t0 < 0.5
    assert rec.events[-1][1] == "release"


def test_scan_codes():
    assert scan_code(0x04) == 0x1E  # a
    assert scan_code(0x28) == 0x1C  # enter
    assert scan_code(0x52) == 0xE048  # up (拡張)
    assert scan_code(0x87) == 0x73  # ro (\ _)
    assert scan_code(0x48) is None  # pause は仮想キーで送る


def test_virtual_transport_errors():
    t = VirtualTransport({"event_delay_ms": 0}, backend=RecordingBackend())
    assert t.run("k:enter") == "ok"
    with pytest.raises(DeviceError, match="invalid command"):
        t.run("zz")
    t2 = VirtualTransport({"event_delay_ms": 0}, allow=lambda: False, backend=RecordingBackend())
    with pytest.raises(DeviceError, match="interrupted"):
        t2.run("k:a")
