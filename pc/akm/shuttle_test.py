"""往復の試験: 今いる場所を基準に、画面の上 (北) へ N マス → Home → 待つ → 元の座標 → Home → 待つ →
画面の下 (南) へ N マス → Home → 待つ → 元の座標 → Home → 待つ、を止めるまで繰り返す。

  * Home で MU Helper が入る。自分で歩くと MU Helper は切れるので、着くたびに Home で入れ直す
  * 歩くのは MU Helper の帰還 (akm/home_return.py) と同じ方法: 行き先の方向をクリックし、現在地 (OCR) を
    読んで着くまで繰り返す。画面の向き (何ピクセルで何マス) は models/home_nav.json を使い、無ければ
    最初に右・下へ少し歩いて調べる
  * 「上へ N マス」は、画面の真上の方向に N 歩 (斜めの 1 歩も 1 マス) 進んだマス
  * キー・マウスは run(script) で送る (ESP32 と同じマクロ書式。仮想 HID でも ESP32 でもよい)
  * キャラクターは常にゲーム画面の真ん中。歩くときはそこから離れた所 (画面の高さの 6% 以上) をクリックする
  * 現在地は座標だけを使う (マップ名の OCR は読み違えて、無いマップ名になることがあるため。マップ名は
    map_name で決める)。座標の読み違い (1 回だけ大きく飛ぶ) は捨て、2 回続けて同じ所なら本当に動いたとみなす。
    元の座標から far_tiles より離れたら (死んで飛ばされたなど) 止める

画面・入力・前面の扱いは呼び出し側から関数で渡す (テストでは偽物を渡す)。
"""
from __future__ import annotations

import math
import threading
import time
from pathlib import Path

from .home_return import HomeReturn, Settings
from .maploc import Location


class _Walker(HomeReturn):
    """HomeReturn の歩き方だけを使う (待つ間に画面を撮ってポーションを見る処理は省く)"""

    def _wait(self, ms: int) -> None:
        self.sleep(ms / 1000)


class ShuttleTest:
    def __init__(self, run, grab, read_loc, client_to_screen, pointer, log=print, calib_path: Path | None = None,
                 tiles: int = 5, wait_s: float = 5.0, helper_key: str = "home", max_walk_s: float = 30.0,
                 arrive_tiles: float = 0.5, map_name: str = "Crywolf", max_jump: int = 15, far_tiles: int = 40):
        """run(script): マクロを送る (前面でなければ待つ・止められたら InterruptedError)
        grab(): ゲーム画面 (原寸)。read_loc(img): 現在地 (x, y を持つもの。マップ名は使わない) か None
        client_to_screen(x, y): ゲーム画面の座標 → 画面の画素
        pointer: カーソルを動かしてクリックするもの (akm.pointer.Pointer。run は同じものを使う)"""
        self._run, self.grab, self._read_loc, self.log = run, grab, read_loc, log
        self.client_to_screen, self.pointer = client_to_screen, pointer
        self.map_name, self.max_jump, self.far_tiles = map_name, int(max_jump), int(far_tiles)
        self._last = None      # 最後に採用した座標
        self._pending = None   # 大きく飛んだ読み (もう 1 回同じ所なら採用)
        self.tiles, self.wait_s, self.helper_key = int(tiles), float(wait_s), helper_key
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.origin = None
        self.status = "停止中"
        self.laps = 0
        self.walker = _Walker(self.run, grab, self.read_loc, lambda img: None, lambda: None, self._click_at,
                              self.char_pos, calib_path=calib_path, log=self._log_walker, sleep=self._sleep)
        self.walker.settings = Settings(arrive_tiles=arrive_tiles, max_walk_s=max_walk_s)

    # ---- 部品 ---------------------------------------------------------------
    def run(self, script: str) -> str:
        if self._stop.is_set():
            raise InterruptedError("stopped")
        if not script:
            return "ok"
        return self._run(script)

    @staticmethod
    def char_pos(img):
        """キャラクターは常にゲーム画面の真ん中"""
        h, w = img.shape[:2]
        return w / 2, h / 2

    def read_loc(self, img):
        """座標だけを読む (マップ名は map_name に置き換える)。1 回だけ大きく飛んだ読みは捨てる"""
        raw = self._read_loc(img)
        if raw is None:
            return None
        loc = Location(self.map_name, int(raw.x), int(raw.y))

        def near(a, b, d):
            return abs(a.x - b.x) <= d and abs(a.y - b.y) <= d

        if self._last is None or near(loc, self._last, self.max_jump):
            self._last, self._pending = loc, None
        elif self._pending is not None and near(loc, self._pending, 2):
            self.log("WARN", f"[往復] 座標が大きく変わりました ({self._last.x}, {self._last.y}) → ({loc.x}, {loc.y})")
            self._last, self._pending = loc, None
        else:
            self._pending = loc  # 読み違いかもしれない: 次も同じ所なら採用
            return None
        if self.origin is not None and not near(loc, Location(self.map_name, self.origin[1], self.origin[2]),
                                                self.far_tiles):
            raise RuntimeError(f"元の座標 ({self.origin[1]}, {self.origin[2]}) から {self.far_tiles} マスより離れました "
                               f"(今 ({loc.x}, {loc.y}))。死んで飛ばされた・ワープしたなど。往復テストを止めます")
        return loc

    def _sleep(self, s: float) -> None:
        if self._stop.wait(s):
            raise InterruptedError("stopped")

    def _click_at(self, x: float, y: float) -> str:
        """ゲーム画面の (x, y) をクリックする (ここで送り終える。歩く側には空のマクロを返す)"""
        self.pointer.click(*self.client_to_screen(x, y))
        return ""

    def _log_walker(self, msg: str) -> None:
        self.log("INFO", msg.replace("[home] ", "[往復] "))

    def read_here(self, timeout: float = 5.0):
        end = time.monotonic() + timeout
        while True:
            loc = self.read_loc(self.grab())
            if loc is not None or time.monotonic() > end:
                return loc
            self._sleep(0.3)

    def offset_target(self, direction: int):
        """画面の上 (direction=-1) / 下 (+1) へ tiles 歩いたマス"""
        (a, b), (c, d) = self.walker.calib.A
        vx, vy = b * direction, d * direction  # 画面の縦 1 ピクセルで変わるマス (x, y)
        n = max(abs(vx), abs(vy))
        if n < 1e-9:
            raise RuntimeError("画面の向きの記録がおかしい (models/home_nav.json を消して調べ直してください)")
        m, x, y = self.origin
        return m, int(round(x + vx / n * self.tiles)), int(round(y + vy / n * self.tiles))

    def walk_to(self, target, label: str) -> str:
        self.status = f"{label} ({target[1]}, {target[2]}) へ"
        self.walker.home = target
        res = self.walker.walk_home()
        here = self.read_loc(self.grab())
        where = f"({here.x}, {here.y})" if here is not None else "(現在地不明)"
        ok = res.startswith("着いた")
        self.log("INFO" if ok else "WARN", f"[往復] {label} ({target[1]}, {target[2]}): {res}  今 {where}")
        return res

    def press_helper(self) -> None:
        self.run(f"k:{self.helper_key}")
        self.log("INFO", f"[往復] {self.helper_key} を押しました (MU Helper)")

    # ---- 実行 ---------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._main, name="shuttle-test", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _main(self) -> None:
        try:
            self._loop()
        except InterruptedError:
            self.log("INFO", "[往復] 止めました")
        except Exception as e:  # noqa: BLE001
            self.log("ERR", f"[往復] 止まりました: {type(e).__name__}: {e}")
        finally:
            self.status = "停止中"

    def _loop(self) -> None:
        self.status = "現在地を読んでいます"
        here = self.read_here()
        if here is None:
            raise RuntimeError("現在地が読めません (画面右下の座標。OCR が使えるか確認してください)")
        self.origin = (here.map, int(here.x), int(here.y))
        self.log("INFO", f"[往復] 元の座標: {here.map} ({here.x}, {here.y})  {self.tiles} マス / {self.wait_s:g} 秒")
        if not self.walker.calib.ok():
            self.status = "画面の向きを調べています"
            self.log("INFO", "[往復] 画面の向き (何ピクセルで何マス) を調べます。右・下へ少し歩きます")
            if not self.walker.calibrate(self.grab()):
                raise RuntimeError("画面の向きを調べられません (壁際? 少し開けた所で始めてください)")
            self.walk_to(self.origin, "元の座標")
        (a, b), (c, d) = self.walker.calib.A
        px = 1 / max(1e-6, math.hypot(b, d))
        up, down = self.offset_target(-1), self.offset_target(+1)
        self.log("INFO", f"[往復] 上 (北) = ({up[1]}, {up[2]})  下 (南) = ({down[1]}, {down[2]})  "
                         f"画面の縦 1 マス ≒ {px:.0f} px")
        while True:
            for label, target in (("上 (北)", up), ("下 (南)", down)):
                self.walk_to(target, label)
                self.press_helper()
                self.status = f"{label} で待機 {self.wait_s:g} 秒"
                self._sleep(self.wait_s)
                self.walk_to(self.origin, "元の座標")
                self.press_helper()
                self.status = f"元の座標で待機 {self.wait_s:g} 秒"
                self._sleep(self.wait_s)
            self.laps += 1
            self.log("INFO", f"[往復] {self.laps} 周しました")
