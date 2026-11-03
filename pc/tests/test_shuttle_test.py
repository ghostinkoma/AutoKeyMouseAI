"""akm/shuttle_test.py: 偽のゲーム (クリックした方向へ歩く) で、上 → Home → 元 → Home → 下 → Home → 元 → Home の順を確かめる。"""
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
            elif cmd == "c:L":
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
    t = ShuttleTest(g.run, lambda: np.zeros((600, 800, 3), np.uint8), lambda img: Loc("Atlans", g.x, g.y),
                    lambda x, y: (int(round(x)), int(round(y))), lambda img: (400, 300),
                    log=lambda lv, m: logs.append((lv, m)), calib_path=calib, tiles=5, wait_s=0)
    t.walker.sleep = lambda s: None
    g.test = t
    t._main()
    assert t.origin == ("Atlans", 100, 100)
    assert g.log == [("home", 105, 105), ("home", 100, 100), ("home", 95, 95), ("home", 100, 100)]
    assert any("止めました" in m for _, m in logs)


def test_offset_is_five_steps(tmp_path):
    calib = tmp_path / "home_nav.json"
    calib.write_text(json.dumps({"A": A}), encoding="utf-8")
    t = ShuttleTest(lambda s: "ok", lambda: None, lambda img: None, lambda x, y: (0, 0), lambda img: (0, 0),
                    calib_path=calib, tiles=5)
    t.origin = ("Atlans", 50, 60)
    assert t.offset_target(-1) == ("Atlans", 55, 65)
    assert t.offset_target(+1) == ("Atlans", 45, 55)
