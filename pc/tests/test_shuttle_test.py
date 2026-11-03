"""akm/shuttle_test.py: 偽のゲーム (クリックした方向へ歩く) で、上 → Home → 元 → Home → 下 → Home → 元 → Home の順を確かめる。"""
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from akm.pointer import Pointer  # noqa: E402
from akm.shuttle_test import ShuttleTest  # noqa: E402

# 画面の向き: ピクセル → マス。画面の上 (0, -1) は (+1, +1) 方向 (見下ろしの斜め)
A = [[0.0177, -0.025], [-0.0177, -0.025]]


@dataclass
class Loc:
    map: str
    x: int
    y: int


class FakeGame:
    def __init__(self):
        self.x, self.y = 100, 100
        self.log = []
        self.homes = 0
        self.test = None

    def run(self, script):
        for cmd in script.split(";"):
            if cmd.startswith("a:"):
                px, py = (int(v) for v in cmd[2:].split(","))
                self.click = (px, py)
            elif cmd == "bu:L":
                ox, oy = self.click[0] - 400, self.click[1] - 300
                self.x += round(A[0][0] * ox + A[0][1] * oy)
                self.y += round(A[1][0] * ox + A[1][1] * oy)
            elif cmd == "k:home":
                self.homes += 1
                self.log.append(("home", self.x, self.y))
                if self.homes >= 4:
                    self.test.stop()
        return "ok"


def test_shuttle_order(tmp_path):
    calib = tmp_path / "home_nav.json"
    calib.write_text(json.dumps({"A": A}), encoding="utf-8")
    g = FakeGame()
    logs = []
    box = {}
    ptr = Pointer(lambda s: box["t"].run(s), lambda sx, sy: (int(round(sx)), int(round(sy))), settle_ms=0, hold_ms=0)
    t = ShuttleTest(g.run, lambda: np.zeros((600, 800, 3), np.uint8), lambda img: Loc("N0isyMap", g.x, g.y),
                    lambda x, y: (x, y), ptr, log=lambda lv, m: logs.append((lv, m)), calib_path=calib, tiles=5,
                    wait_s=0, map_name="Crywolf")
    box["t"] = t
    t.walker.sleep = lambda s: None
    g.test = t
    t._main()
    assert t.origin == ("Crywolf", 100, 100)
    assert g.log == [("home", 105, 105), ("home", 100, 100), ("home", 95, 95), ("home", 100, 100)]
    assert any("止めました" in m for _, m in logs)


def test_offset_is_five_steps(tmp_path):
    calib = tmp_path / "home_nav.json"
    calib.write_text(json.dumps({"A": A}), encoding="utf-8")
    t = ShuttleTest(lambda s: "ok", lambda: None, lambda img: None, lambda x, y: (0, 0), None,
                    calib_path=calib, tiles=5)
    t.origin = ("Atlans", 50, 60)
    assert t.offset_target(-1) == ("Atlans", 55, 65)
    assert t.offset_target(+1) == ("Atlans", 45, 55)


def test_read_loc_filters_misread_and_far_jump(tmp_path):
    reads = iter([(100, 100), (180, 100), (101, 100), (150, 150), (151, 150)])
    logs = []
    t = ShuttleTest(lambda s: "ok", lambda: None, lambda img: Loc("x", *next(reads)), lambda x, y: (x, y), None,
                    log=lambda lv, m: logs.append(m), calib_path=tmp_path / "none.json", far_tiles=40)
    assert (lambda r: (r.map, r.x, r.y))(t.read_loc(None)) == ("Crywolf", 100, 100)
    t.origin = ("Crywolf", 100, 100)
    assert t.read_loc(None) is None            # 1 回だけ大きく飛んだ: 読み違いとして捨てる
    assert (lambda r: (r.map, r.x, r.y))(t.read_loc(None)) == ("Crywolf", 101, 100)
    assert t.read_loc(None) is None            # 大きく飛んだ (1 回目)
    import pytest

    with pytest.raises(RuntimeError, match="離れました"):
        t.read_loc(None)                       # 2 回続いた = 本当に飛んだ。元から遠いので止める


def test_pointer_modes():
    sent = []
    p = Pointer(sent.append, lambda sx, sy: (sx * 10, sy * 10), mode="absrel", settle_ms=100, hold_ms=80)
    p.click(12, 34)
    assert sent == ["a:120,340;w:20;m:1,0;w:20;m:-1,0", "w:100;bd:L;w:80;bu:L"]
    pos = [0, 0]

    def run(s):
        dx, dy = (int(v) for v in s[2:].split(","))
        pos[0] += dx // 2 + (1 if dx > 0 else -1 if dx < 0 else 0)  # 加速が効いて半分しか動かない
        pos[1] += dy // 2 + (1 if dy > 0 else -1 if dy < 0 else 0)

    r = Pointer(run, None, cursor=lambda: tuple(pos), mode="rel")
    r.move(100, -50)
    assert abs(pos[0] - 100) <= 1 and abs(pos[1] + 50) <= 1
