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


class Clock:
    """偽の時計: 待つと進む (歩く側の待ち時間で、ゲームの時間も進んだことにする)"""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def use_clock(t):
    c = Clock()
    t.clock = t.walker.clock = c
    t.walker.sleep = c.sleep
    t.max_speed = 100.0  # 偽のゲームは一瞬で歩くので、速さの判定はゆるめる
    return c


def near_all(log, want, d=1):
    """押した場所 (home の記録) が、それぞれ want の d マス以内か"""
    return len(log) == len(want) and all(abs(h[1] - x) <= d and abs(h[2] - y) <= d for h, (x, y) in zip(log, want))


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
            elif cmd in ("k:home", "ku:home"):
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
                    wait_s=0, map_name="Crywolf", axis="screen")
    box["t"] = t
    use_clock(t)
    g.test = t
    t._main()
    assert t.origin == ("Crywolf", 100, 100)
    assert near_all(g.log, [(105, 105), (100, 100), (95, 95), (100, 100)])
    assert any("止めました" in m for _, m in logs)


def test_offset_is_five_steps(tmp_path):
    calib = tmp_path / "home_nav.json"
    calib.write_text(json.dumps({"A": A}), encoding="utf-8")
    t = ShuttleTest(lambda s: "ok", lambda: None, lambda img: None, lambda x, y: (0, 0), None,
                    calib_path=calib, tiles=5, axis="screen")
    t.origin = ("Atlans", 50, 60)
    assert t.offset_target(-1) == ("Atlans", 55, 65)
    assert t.offset_target(+1) == ("Atlans", 45, 55)


def test_read_loc_filters_misread_and_far_jump(tmp_path):
    """立ち止まっている間に同じ読み違い (18 → 1 / 191) が続いても捨てる。本当に飛んだときは何回も・何秒も続く"""
    reads = iter([(225, 18), (222, 1), (222, 1), (224, 18), (222, 191), (222, 191),
                  (150, 150), (150, 150), (151, 150), (150, 150), (150, 151)])
    now = {"t": 0.0}
    logs = []
    t = ShuttleTest(lambda s: "ok", lambda: None, lambda img: Loc("x", *next(reads)), lambda x, y: (x, y), None,
                    log=lambda lv, m: logs.append(m), calib_path=tmp_path / "none.json", far_tiles=40,
                    clock=lambda: now["t"])
    xy = lambda r: None if r is None else (r.map, r.x, r.y)  # noqa: E731

    def step(dt=0.5):
        now["t"] += dt
        return xy(t.read_loc(None))

    assert step() == ("Crywolf", 225, 18)
    t.origin = ("Crywolf", 225, 18)
    assert step() is None and step() is None      # (222, 1) が 2 回続いても捨てる
    assert step() == ("Crywolf", 224, 18)
    assert step() is None and step() is None      # (222, 191) も
    import pytest

    for _ in range(4):
        assert step() is None                      # (150, 150) 付近が続く: まだ様子を見る
    with pytest.raises(RuntimeError, match="離れました"):
        step()                                     # 5 回・2 秒以上続いた = 本当に飛んだ。元から遠いので止める


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


def test_helper_is_ensured_on_after_each_move(tmp_path):
    """着いたら AI で MU Helper を見て、停止中のときだけ Home を押す (歩くと切れる)。動作中なら押さない"""
    calib = tmp_path / "home_nav.json"
    calib.write_text(json.dumps({"A": A}), encoding="utf-8")
    g = FakeGame()
    g.helper = True  # 始めは動作中 (最初の移動で切れる)
    orig_run = g.run

    def run(script):
        if "bu:L" in script:
            g.helper = False  # 自分で歩くと MU Helper は切れる
        if "ku:home" in script or script == "k:home":
            g.helper = not g.helper
        return orig_run(script)

    box = {}
    ptr = Pointer(lambda s: box["t"].run(s), lambda sx, sy: (int(round(sx)), int(round(sy))), settle_ms=0, hold_ms=0)
    t = ShuttleTest(run, lambda: np.zeros((600, 800, 3), np.uint8), lambda img: Loc("m", g.x, g.y),
                    lambda x, y: (x, y), ptr, log=lambda lv, m: None, calib_path=calib, wait_s=0,
                    helper_probe=lambda: {"state": g.helper, "p": 0.9, "click": None}, axis="screen")
    box["t"] = t
    use_clock(t)
    g.test = t
    t._main()
    assert near_all(g.log, [(105, 105), (100, 100), (95, 95), (100, 100)])
    assert g.helper is True


def test_focus_lost_stops(tmp_path):
    calib = tmp_path / "home_nav.json"
    calib.write_text(json.dumps({"A": A}), encoding="utf-8")
    g = FakeGame()
    box = {}
    ptr = Pointer(lambda s: box["t"].run(s), lambda sx, sy: (int(round(sx)), int(round(sy))), settle_ms=0, hold_ms=0)
    alive = {"v": True}
    logs = []

    def run(script):
        if "bu:L" in script:
            alive["v"] = False  # 1 歩目のあと、人が別の窓をクリックした
        return g.run(script)

    t = ShuttleTest(run, lambda: np.zeros((600, 800, 3), np.uint8), lambda img: Loc("m", g.x, g.y),
                    lambda x, y: (x, y), ptr, log=lambda lv, m: logs.append(m), calib_path=calib, wait_s=0,
                    alive=lambda: alive["v"], axis="screen")
    box["t"] = t
    use_clock(t)
    t._main()
    assert t.stop_reason == "focus lost"
    assert any("フォーカスが外れた" in m for m in logs)
    assert g.homes == 0


def test_base_point_and_tolerance(tmp_path):
    """開始ボタンを押したときの座標が基準点。壁で目的のマスに着けないときは ±2 マスまでで良しとする"""
    calib = tmp_path / "home_nav.json"
    calib.write_text(json.dumps({"A": A}), encoding="utf-8")
    g = FakeGame()
    g.x, g.y = 101, 100  # MU Helper がアイテムを拾って、押したときから 1 マス動いた

    def run(script):
        r = g.run(script)
        g.x = min(g.x, 103)  # 壁: x は 103 より先へ行けない
        return r

    box = {}
    logs = []
    ptr = Pointer(lambda s: box["t"].run(s), lambda sx, sy: (int(round(sx)), int(round(sy))), settle_ms=0, hold_ms=0)
    t = ShuttleTest(run, lambda: np.zeros((600, 800, 3), np.uint8), lambda img: Loc("m", g.x, g.y),
                    lambda x, y: (x, y), ptr, log=lambda lv, m: logs.append(m), calib_path=calib, wait_s=0,
                    base_xy=(100, 100), exact_s=0.2, max_walk_s=0.6, axis="screen")
    box["t"] = t
    use_clock(t)
    g.test = t
    t._main()
    assert t.origin[1:] == (100, 100)
    first = g.log[0]
    assert abs(first[1] - 105) <= 2 and abs(first[2] - 105) <= 2  # 上 (105, 105) の近く
    assert any("近くに着いた" in m for m in logs)
    assert near_all(g.log[1:2], [(100, 100)])


# 本物の MU: 座標は画面から見て 45 度傾いている (画面の右 = X が増え Y が減る / 画面の下 = X も Y も増える)
A_Y = [[0.025, 0.025], [-0.025, 0.025]]


def test_y_axis_round_trip_recalibrates_wrong_record(tmp_path):
    """上下は座標の Y ±10 (画面では斜め)。向きの記録が 45 度の傾きと合わなければ、歩いて調べ直してから往復する"""
    calib = tmp_path / "home_nav.json"
    # 古い (合わない) 記録: 画面の上へ歩くと主に X が変わる (前に読み違いで測ったもの)
    calib.write_text(json.dumps({"A": [[0.035, 0.037], [0.0, -0.007]]}), encoding="utf-8")
    g = FakeGame()

    def run(script):
        for cmd in script.split(";"):
            if cmd.startswith("a:"):
                g.click = tuple(int(v) for v in cmd[2:].split(","))
            elif cmd == "bu:L":
                ox, oy = g.click[0] - 400, g.click[1] - 300
                g.x += round(A_Y[0][0] * ox + A_Y[0][1] * oy)
                g.y += round(A_Y[1][0] * ox + A_Y[1][1] * oy)
            elif cmd in ("k:home", "ku:home"):
                g.homes += 1
                g.log.append(("home", g.x, g.y))
                if g.homes >= 4:
                    t.stop()
        return "ok"

    box, logs = {}, []
    ptr = Pointer(lambda s: box["t"].run(s), lambda sx, sy: (int(round(sx)), int(round(sy))), settle_ms=0, hold_ms=0)
    t = ShuttleTest(run, lambda: np.zeros((600, 800, 3), np.uint8), lambda img: Loc("m", g.x, g.y),
                    lambda x, y: (x, y), ptr, log=lambda lv, m: logs.append(m), calib_path=calib, wait_s=0,
                    tiles=10, base_xy=(100, 100))
    box["t"] = t
    use_clock(t)
    t._main()
    assert any("調べ直します" in m for m in logs)
    assert near_all(g.log, [(100, 90), (100, 100), (100, 110), (100, 100)])
    (a, b), (c, d) = json.loads(calib.read_text(encoding="utf-8"))["A"]
    assert 0.5 <= abs(b) / abs(d) <= 2 and 0.5 <= abs(a) / abs(c) <= 2  # 測り直した 45 度の記録が保存されている


def test_learns_direction_while_walking(tmp_path):
    """向きの記録が 45 度だが左右が逆 (確認は通ってしまう) でも、歩いた結果から直して着く"""
    calib = tmp_path / "home_nav.json"
    calib.write_text(json.dumps({"A": [[0.025, -0.025], [0.025, 0.025]]}), encoding="utf-8")
    g = FakeGame()

    def run(script):
        for cmd in script.split(";"):
            if cmd.startswith("a:"):
                g.click = tuple(int(v) for v in cmd[2:].split(","))
            elif cmd == "bu:L":
                ox, oy = g.click[0] - 400, g.click[1] - 300
                g.x += round(A_Y[0][0] * ox + A_Y[0][1] * oy)
                g.y += round(A_Y[1][0] * ox + A_Y[1][1] * oy)
            elif cmd in ("k:home", "ku:home"):
                g.homes += 1
                g.log.append(("home", g.x, g.y))
                if g.homes >= 2:
                    t.stop()
        return "ok"

    box, logs = {}, []
    ptr = Pointer(lambda s: box["t"].run(s), lambda sx, sy: (int(round(sx)), int(round(sy))), settle_ms=0, hold_ms=0)
    t = ShuttleTest(run, lambda: np.zeros((600, 800, 3), np.uint8), lambda img: Loc("m", g.x, g.y),
                    lambda x, y: (x, y), ptr, log=lambda lv, m: logs.append(m), calib_path=calib, wait_s=0,
                    tiles=10, base_xy=(100, 100))
    box["t"] = t
    use_clock(t)
    t._main()
    assert any("歩いた結果から画面の向きを直しました" in m for m in logs)
    assert abs(g.log[0][1] - 100) <= 2 and abs(g.log[0][2] - 90) <= 2   # 上 (100, 90) の近く
    assert abs(g.log[1][1] - 100) <= 2 and abs(g.log[1][2] - 100) <= 2  # 基準点の近く


def test_helper_press_waits_for_lagging_screen(tmp_path):
    """画面 (AI の判定) が少し遅れて変わっても、Home を 2 回押して切ってしまわない"""
    calib = tmp_path / "home_nav.json"
    calib.write_text(json.dumps({"A": A}), encoding="utf-8")
    state = {"on": False, "presses": 0, "probes_after": 0}

    def run(script):
        if "ku:home" in script:
            state["on"] = not state["on"]
            state["presses"] += 1
            state["probes_after"] = 0
        return "ok"

    def probe():
        state["probes_after"] += 1
        shown = state["on"] if state["probes_after"] > 4 else (not state["on"])  # 4 回 (約 0.6 秒) は前の画面
        return {"state": shown, "p": 1.0, "click": None}

    t = ShuttleTest(run, lambda: None, lambda img: None, lambda x, y: (x, y), None, calib_path=calib,
                    helper_probe=probe)
    t.press_helper(moved=True)
    assert state["presses"] == 1 and state["on"] is True


def test_read_loc_picks_candidate_near_last(tmp_path):
    """2 通りの読みが違うとき (候補が 2 つ)、前の座標に近い方を使う"""
    reads = iter([[Loc("", 152, 160)], [Loc("", 155, 15), Loc("", 155, 157)], []])
    t = ShuttleTest(lambda s: "ok", lambda: None, lambda img: next(reads), lambda x, y: (x, y), None,
                    calib_path=tmp_path / "none.json", clock=lambda: 0.0)
    a = t.read_loc(None)
    b = t.read_loc(None)
    assert (a.x, a.y) == (152, 160) and (b.x, b.y) == (155, 157)
    assert t.read_loc(None) is None  # 読めない
