"""マップ探索: 開始ボタンを押した座標 (基準点) のまわりを自動で歩き回り、歩けるマス・行けないマスを記録する。

  * 基準点から ±radius マスの範囲に、step マスおきの目標を並べる
  * 今いる所から一番近い、まだ行っていない目標へ歩く (歩き方は往復テストと同じ: クリックして座標を読み、
    着くまで繰り返す。画面の向きは歩いた結果から直し続ける)
  * 歩いている間に読めた座標は、すべて「歩けるマス」として記録する (data/mu_map.db の cells。
    仲介アプリの取得マップに出る)
  * 時間内に近くまで行けなかった目標は「行けないマス」として記録する (blocked。取得マップに赤で出る)
  * 全部の目標を回ったら (または max_minutes を過ぎたら) 基準点へ戻り、MU Helper を入れて終わる
  * 探索中に MU Helper は入れない (クリックで歩くと切れる)。HP が potion_hp より減ったら potion_key を押す
  * 基準点から大きく離れたら (死んで町などへ飛ばされた) 止める
"""
from __future__ import annotations

import math
import time

from .shuttle_test import ShuttleTest


class MapExplorer(ShuttleTest):
    def __init__(self, *args, recorder=None, radius: int = 25, step: int = 3, max_minutes: float = 30.0,
                 read_hp=None, potion_key: str = "q", potion_hp: float = 0.5, walk_s: float = 12.0, **kw):
        """recorder: walk(x, y) / blocked(x, y) を持つもの (記録先)。read_hp(img): HP 0..1 か None"""
        kw.setdefault("far_tiles", int(radius) + 20)
        kw.setdefault("max_walk_s", walk_s)
        kw.setdefault("exact_s", 2.0)
        super().__init__(*args, **kw)
        self.tag = "[探索]"
        self.arrive_first = self.tolerance * 1.42  # 目標は通り道: 近くまで来たらすぐ次へ (立ち止まらない)
        self.unread = 0  # 続けて座標が読めなかった回数
        self.recorder = recorder
        self.radius, self.step = int(radius), max(1, int(step))
        self.max_minutes = float(max_minutes)
        self.read_hp, self.potion_key, self.potion_hp = read_hp, potion_key, float(potion_hp)
        self._last_potion = 0.0
        self.seen: set[tuple[int, int]] = set()    # 読めた (歩いた) マス
        self.done: set[tuple[int, int]] = set()    # 着いた目標
        self.unreachable: set[tuple[int, int]] = set()
        self.targets: list[tuple[int, int]] = []

    # ---- 座標を読むたびに: 記録・ポーション ---------------------------------------------
    def read_loc(self, img):
        loc = super().read_loc(img)
        if loc is not None:
            xy = (int(loc.x), int(loc.y))
            if xy not in self.seen:
                self.seen.add(xy)
            if self.recorder is not None:
                try:
                    self.recorder.walk(*xy)
                except Exception as e:  # noqa: BLE001
                    self.log("WARN", f"{self.tag} 記録できません: {e}")
            for t in self.targets:  # 通りがかった目標も「行った」ことにする
                if t not in self.done and max(abs(t[0] - xy[0]), abs(t[1] - xy[1])) <= 1:
                    self.done.add(t)
        self._potion(img)
        return loc

    def _potion(self, img) -> None:
        if self.read_hp is None or img is None:
            return
        try:
            hp = self.read_hp(img)
        except Exception:  # noqa: BLE001
            return
        now = time.monotonic()
        if hp is not None and hp < self.potion_hp and now - self._last_potion > 0.5:
            self._last_potion = now
            self.log("INFO", f"{self.tag} HP {hp:.0%}: {self.potion_key} (ポーション)")
            self.run(f"kd:{self.potion_key};w:60;ku:{self.potion_key}")

    # ---- 目標 ---------------------------------------------------------------
    def make_targets(self) -> list[tuple[int, int]]:
        _m, bx, by = self.origin
        r, s = self.radius, self.step
        pts = []
        for dy in range(-r, r + 1, s):
            for dx in range(-r, r + 1, s):
                x, y = bx + dx, by + dy
                if 0 <= x <= 255 and 0 <= y <= 255:
                    pts.append((x, y))
        return pts

    def next_target(self):
        here = self._last
        left = [t for t in self.targets if t not in self.done and t not in self.unreachable]
        if not left:
            return None
        if here is None:
            return left[0]
        # 近いもの (同じくらいなら基準点に近いもの: 外へ外へと広げる)
        _m, bx, by = self.origin
        return min(left, key=lambda t: (math.hypot(t[0] - here.x, t[1] - here.y),
                                        math.hypot(t[0] - bx, t[1] - by)))

    # ---- 実行 ---------------------------------------------------------------
    def _loop(self) -> None:
        self._setup(f"±{self.radius} マスを {self.step} マスおきに探索 (最大 {self.max_minutes:g} 分)")
        self.targets = self.make_targets()
        self.log("INFO", f"{self.tag} 目標 {len(self.targets)} か所")
        t_end = time.monotonic() + self.max_minutes * 60
        n = 0
        while time.monotonic() < t_end:
            t = self.next_target()
            if t is None:
                break
            n += 1
            m = self.origin[0]
            res = self.walk_to((m, t[0], t[1]), f"探索 {len(self.done) + len(self.unreachable) + 1}/{len(self.targets)}")
            here = self._here()
            if here is None:  # 座標が読めない (窓で隠れている・表示が消えている): この目標は後回し
                self.unread += 1
                if self.unread >= 5:
                    raise RuntimeError("座標が読めない状態が続くので止めます (画面右下の座標が見えているか確認してください)")
                self.log("WARN", f"{self.tag} 座標が読めないので ({t[0]}, {t[1]}) は後回しにします")
                self._sleep(1.0)
                continue
            self.unread = 0
            if self._within(here, (m, t[0], t[1]), self.tolerance):
                self.done.add(t)
            else:
                self.unreachable.add(t)
                if self.recorder is not None:
                    try:
                        self.recorder.blocked(*t)
                    except Exception as e:  # noqa: BLE001
                        self.log("WARN", f"{self.tag} 記録できません: {e}")
                self.log("INFO", f"{self.tag} ({t[0]}, {t[1]}) へは行けませんでした ({res})")
            self.status = (f"探索 {len(self.done)}/{len(self.targets)} 行けない {len(self.unreachable)} "
                           f"歩いたマス {len(self.seen)}")
            if n % 5 == 0:
                self.log("INFO", f"{self.tag} {self.status}")
        why = "全部の目標を回りました" if self.next_target() is None else f"{self.max_minutes:g} 分たちました"
        self.log("INFO", f"{self.tag} {why}: 着いた {len(self.done)} / 行けない {len(self.unreachable)} / "
                         f"歩いたマス {len(self.seen)}。基準点へ戻ります")
        n0 = self.clicks
        self.walk_to(self.origin, "基準点")
        self.press_helper(moved=self.clicks > n0)
        self.log("INFO", f"{self.tag} 探索を終えました")
