"""akm/map_explore.py: 偽のゲーム (クリックした方向へ歩く・x が 103 より先は壁) で、範囲を回って
歩けるマス・行けないマスを記録し、基準点へ戻って MU Helper を入れることを確かめる。"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from akm.map_explore import MapExplorer  # noqa: E402
from akm.pointer import Pointer  # noqa: E402
from test_shuttle_test import A_Y, Loc, use_clock  # noqa: E402


class Rec:
    def __init__(self):
        self.walked, self.blocked = set(), set()

    def walk(self, x, y):
        self.walked.add((x, y))


def test_explore_records_walkable_and_blocked(tmp_path):
    calib = tmp_path / "home_nav.json"
    calib.write_text(json.dumps({"A": A_Y}), encoding="utf-8")
    g = {"x": 100, "y": 100, "click": (0, 0), "helper": False, "homes": 0}

    def run(script):
        for cmd in script.split(";"):
            if cmd.startswith("a:"):
                g["click"] = tuple(int(v) for v in cmd[2:].split(","))
            elif cmd == "bu:L":
                ox, oy = g["click"][0] - 400, g["click"][1] - 300
                g["x"] = min(103, g["x"] + round(A_Y[0][0] * ox + A_Y[0][1] * oy))  # x は 103 より先へ行けない
                g["y"] += round(A_Y[1][0] * ox + A_Y[1][1] * oy)
                g["helper"] = False
            elif cmd == "ku:home":
                g["helper"] = not g["helper"]
                g["homes"] += 1
        return "ok"

    rec = Rec()
    recorder = type("R", (), {"walk": lambda self, x, y: rec.walk(x, y),
                              "blocked": lambda self, x, y: rec.blocked.add((x, y))})()
    box, logs = {}, []
    ptr = Pointer(lambda s: box["t"].run(s), lambda sx, sy: (int(round(sx)), int(round(sy))), settle_ms=0, hold_ms=0)
    t = MapExplorer(run, lambda: np.zeros((600, 800, 3), np.uint8), lambda img: Loc("m", g["x"], g["y"]),
                    lambda x, y: (x, y), ptr, log=lambda lv, m: logs.append(m), calib_path=calib, wait_s=0,
                    base_xy=(100, 100), recorder=recorder, radius=6, step=3, walk_s=3.0,
                    helper_probe=lambda: {"state": g["helper"], "p": 1.0, "click": None})
    box["t"] = t
    use_clock(t)
    t._main()
    assert any("探索を終えました" in m for m in logs), logs[-5:]
    assert len(t.targets) == 25
    # x = 106 の列 (壁の向こう) は行けない。それ以外は着く
    assert {p for p in t.unreachable} == {(106, y) for y in range(94, 107, 3)}
    assert rec.blocked == t.unreachable
    assert (100, 100) in rec.walked and all(x <= 103 for x, _ in rec.walked)
    assert abs(g["x"] - 100) <= 2 and abs(g["y"] - 100) <= 2  # 基準点へ戻った
    assert g["helper"] is True                                  # MU Helper を入れて終わる


def test_blocked_table_roundtrip(tmp_path):
    """行けないマスを DB に書き、仲介アプリの読み取り (MapSource) で出る。あとで歩けたマスは出ない"""
    from akm.gate_live import MapSource
    from akm.maploc import Location, MapDB

    db = MapDB(tmp_path / "m.db", interp_max=8)
    db.add_blocked("Noria", 10, 10)
    db.add_blocked("Noria", 11, 10)
    db.add_blocked("Noria", 11, 10)
    db.record(Location("Noria", 11, 10))
    src = MapSource(tmp_path / "m.db")
    assert src.blocked("Noria") == [(10, 10)]
    assert src.blocked("Lorencia") == []
