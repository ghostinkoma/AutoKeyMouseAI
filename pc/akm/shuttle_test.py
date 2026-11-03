"""往復の試験: 開始ボタンを押したときの座標を基準点に、画面の上 (北) へ N マス → Home → 待つ → 基準点 → Home →
待つ → 画面の下 (南) へ N マス → Home → 待つ → 基準点 → Home → 待つ、を止めるまで繰り返す。
(MU Helper はアイテムを拾うたびに動くので、行き先はいつも基準点から決める。着けないときは ±2 マスまで許す)

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

    def _loc(self):
        """現在地を読む。読めない・読み違いとして捨てたときは、少し待って読み直す (最大 2 秒ほど)"""
        img = None
        for _ in range(10):
            img = self.grab()
            loc = self.read_loc(img)
            if loc is not None:
                return img, loc
            self.sleep(0.2)
        return img, None


class ShuttleTest:
    def __init__(self, run, grab, read_loc, client_to_screen, pointer, log=print, calib_path: Path | None = None,
                 tiles: int = 5, wait_s: float = 5.0, helper_key: str = "home", max_walk_s: float = 30.0,
                 arrive_tiles: float = 0.5, map_name: str = "Crywolf", max_jump: int = 15, far_tiles: int = 40,
                 helper_probe=None, alive=None, max_speed: float = 8.0, base_tiles: int = 3, jump_reads: int = 5,
                 jump_s: float = 2.0, clock=time.monotonic, base_xy: tuple[int, int] | None = None,
                 exact_s: float = 8.0, tolerance: int = 2, axis: str = "y", helper_wait_s: float = 3.0):
        """run(script): マクロを送る (前面でなければ待つ・止められたら InterruptedError)
        grab(): ゲーム画面 (原寸)。read_loc(img): 現在地 (x, y を持つもの。マップ名は使わない) か None
        client_to_screen(x, y): ゲーム画面の座標 → 画面の画素
        pointer: カーソルを動かしてクリックするもの (akm.pointer.Pointer。run は同じものを使う)
        helper_probe(): MU Helper の状態を学習済みの AI で見る (akm.helper_probe)。あれば、着いたら
                        「停止中なら Home → 動作中になったか確かめる」。無ければ Home を押すだけ
        alive(): 続けてよいか (送信先が前面か)。False になったらその場で止める (InterruptedError)"""
        self.alive = alive
        self.calib_path = calib_path
        self.helper_wait_s = float(helper_wait_s)  # Home を押してから動作中になるのを待つ秒数 (過ぎたら押し直す)
        self._center = (0.0, 0.0)
        self._click = None     # 最後のクリック (その時の座標, キャラからの向き px, 時刻)
        self._samples = []     # 歩いた結果 [(クリックの向き px, 変わった座標)]
        self.clicks = 0
        # 上下の決め方: "y" = 座標の Y (上 = Y が減る。画面では斜めに歩く: 座標は画面から 45 度傾いている)
        #               "x" = 座標の X / "screen" = 画面の上下
        if axis not in ("y", "x", "screen"):
            raise ValueError(f"axis: {axis}")
        self.axis = axis
        self.base_xy = base_xy        # 開始ボタンを押したときの座標 (基準点)。None なら始めに読む
        self.exact_s = float(exact_s)  # まずこの秒数はちょうどのマスを狙う
        self.tolerance = int(tolerance)  # 着けないときは上下左右にこのマス数までずれてよい
        self.max_walk_s = float(max_walk_s)
        self.stop_reason = ""
        self._run, self.grab, self._read_loc, self.log = run, grab, read_loc, log
        self.client_to_screen, self.pointer = client_to_screen, pointer
        self.map_name, self.max_jump, self.far_tiles = map_name, int(max_jump), int(far_tiles)
        self._last = None      # 最後に採用した座標
        self._last_t = 0.0
        self._odd = []         # 離れすぎて捨てた読み (同じ所が続けば本当に飛んだ)
        self.rejected = 0
        self.max_speed, self.base_tiles = float(max_speed), int(base_tiles)
        self.jump_reads, self.jump_s, self.clock = int(jump_reads), float(jump_s), clock
        self.tiles, self.wait_s, self.helper_key = int(tiles), float(wait_s), helper_key
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.origin = None
        self.status = "停止中"
        self.laps = 0
        self.walker = _Walker(self.run, grab, self.read_loc, lambda img: None, lambda: None, self._click_at,
                              self.char_pos, calib_path=calib_path, log=self._log_walker, sleep=self._sleep)
        self.walker.settings = Settings(arrive_tiles=arrive_tiles, max_walk_s=max_walk_s, helper_key=helper_key)
        self.walker.helper_probe = helper_probe

    # ---- 部品 ---------------------------------------------------------------
    def _check(self) -> None:
        if self._stop.is_set():
            raise InterruptedError("stopped")
        if self.alive is not None and not self.alive():
            raise InterruptedError("focus lost")

    def run(self, script: str) -> str:
        self._check()
        if not script:
            return "ok"
        return self._run(script)

    def char_pos(self, img):
        """キャラクターは常にゲーム画面の真ん中"""
        h, w = img.shape[:2]
        self._center = (w / 2, h / 2)
        return self._center

    def read_loc(self, img):
        """座標だけを読む (マップ名は map_name に置き換える)。

        OCR は同じ読み違いを続けることがある (立ち止まっている間は同じ画像なので、18 を 1 や 191 と何度も読む)。
        そこで歩ける速さで判定する: 前に採用した座標から「base_tiles + 速さ (8 マス/秒) × 経った秒数」より離れた読みは捨てる。
        離れた読みが jump_reads 回以上・jump_s 秒以上続けて同じ所なら、本当に飛んだ (死んだ・ワープ) とみなす"""
        raw = self._read_loc(img)
        if raw is None:
            return None
        now = self.clock()
        loc = Location(self.map_name, int(raw.x), int(raw.y))

        def near(a, b, d):
            return abs(a.x - b.x) <= d and abs(a.y - b.y) <= d

        if self._last is None:
            ok = True
        else:
            ok = near(loc, self._last, self.base_tiles + self.max_speed * max(0.0, now - self._last_t))
        if ok:
            self._last, self._last_t, self._odd = loc, now, []
            self._learn(loc, now)
        else:
            self._odd = [(lo, t) for lo, t in self._odd if near(lo, loc, 2)] + [(loc, now)]
            if len(self._odd) >= self.jump_reads and now - self._odd[0][1] >= self.jump_s:
                self.log("WARN", f"[往復] 座標が大きく変わりました ({self._last.x}, {self._last.y}) → ({loc.x}, {loc.y})"
                                 f" ({len(self._odd)} 回続けて同じ読み)")
                self._last, self._last_t, self._odd = loc, now, []
            else:
                self.rejected += 1
                if self.rejected <= 3 or self.rejected % 20 == 0:
                    self.log("INFO", f"[往復] 座標の読み違いとして捨てました: ({loc.x}, {loc.y}) "
                                     f"(前 ({self._last.x}, {self._last.y}))")
                return None
        if self.origin is not None and not near(loc, Location(self.map_name, self.origin[1], self.origin[2]),
                                                self.far_tiles):
            raise RuntimeError(f"基準点 ({self.origin[1]}, {self.origin[2]}) から {self.far_tiles} マスより離れました "
                               f"(今 ({loc.x}, {loc.y}))。死んで飛ばされた・ワープしたなど。往復テストを止めます")
        return loc

    def _sleep(self, s: float) -> None:
        end = time.monotonic() + s
        while True:
            self._check()
            left = end - time.monotonic()
            if left <= 0:
                return
            self._stop.wait(min(0.1, left))

    def _click_at(self, x: float, y: float) -> str:
        """ゲーム画面の (x, y) をクリックする (ここで送り終える。歩く側には空のマクロを返す)。
        クリックした向きと、そのあと座標がどう変わったかを覚えて、画面の向きの記録を直すのに使う"""
        cx, cy = self._center
        ox, oy = x - cx, y - cy
        here, goal = self._last, self.walker.home
        self.clicks += 1
        self.log("INFO", f"[往復] 歩く {self.clicks}: 今 " + (f"({here.x}, {here.y})" if here else "(?)") +
                 (f" → 目標 ({goal[1]}, {goal[2]})" if goal else "") + f"  キャラから ({ox:+.0f}, {oy:+.0f})px をクリック")
        self._click = (here, (ox, oy), self.clock())
        self.pointer.click(*self.client_to_screen(x, y))
        return ""

    def _learn(self, loc, now) -> None:
        """前のクリックのあと座標がどう変わったか (歩いた結果) を覚え、何回か分たまったら画面の向きの記録を直す"""
        c = self._click
        if c is None or c[0] is None or now - c[2] < 0.25:
            return
        before, (ox, oy), _t = c
        self._click = None
        dx, dy = loc.x - before.x, loc.y - before.y
        if abs(dx) + abs(dy) < 1 or math.hypot(ox, oy) < 1:
            return
        self._samples = (self._samples + [((ox, oy), (dx, dy))])[-12:]
        if len(self._samples) >= 3:
            self._refit()

    def _refit(self) -> None:
        import numpy as np

        o = np.array([s[0] for s in self._samples], float)
        d = np.array([s[1] for s in self._samples], float)
        if abs(np.linalg.det(o.T @ o)) < 1e-6 * max(1.0, float((o ** 2).sum())) ** 2:
            return  # 同じ向きばかり: 2 次元の向きが決まらない
        at, *_ = np.linalg.lstsq(o, d, rcond=None)  # d = o @ A^T
        A = at.T.tolist()
        (a, b), (c, e) = A
        if abs(a * e - b * c) < 1e-9:
            return
        old = self.walker.calib.A
        self.walker.calib.A = A
        if old is None or max(abs(A[i][j] - old[i][j]) for i in range(2) for j in range(2)) > 0.004:
            self.log("INFO", f"[往復] 歩いた結果から画面の向きを直しました: 右へ 100px で X {a * 100:+.1f} / "
                             f"Y {c * 100:+.1f}、下へ 100px で X {b * 100:+.1f} / Y {e * 100:+.1f} マス "
                             f"({len(self._samples)} 回分)")
            if self.calib_path:
                try:
                    self.walker.calib.save(self.calib_path)
                except OSError:
                    pass

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
        """上 (direction=-1) / 下 (+1) へ tiles 進んだマス。axis が y なら Y だけ、x なら X だけ変える"""
        m, x, y = self.origin
        if self.axis == "y":
            return m, x, y + direction * self.tiles
        if self.axis == "x":
            return m, x + direction * self.tiles, y
        (a, b), (c, d) = self.walker.calib.A
        vx, vy = b * direction, d * direction  # 画面の縦 1 ピクセルで変わるマス (x, y)
        n = max(abs(vx), abs(vy))
        if n < 1e-9:
            raise RuntimeError("画面の向きの記録がおかしい (models/home_nav.json を消して調べ直してください)")
        return m, int(round(x + vx / n * self.tiles)), int(round(y + vy / n * self.tiles))

    def _within(self, here, target, d: int) -> bool:
        return here is not None and abs(here.x - target[1]) <= d and abs(here.y - target[2]) <= d

    def walk_to(self, target, label: str) -> str:
        """target へ歩く。まず exact_s 秒はちょうどのマスを狙い、着けなければ上下左右 tolerance マスまでで良しとする"""
        self.status = f"{label} ({target[1]}, {target[2]}) へ"
        self.walker.home = target
        st = self.walker.settings
        st.arrive_tiles, st.max_walk_s = 0.5, self.exact_s
        res = self.walker.walk_home()
        _, here = self.walker._loc()
        ok = res.startswith("着いた")
        if not ok and not self._within(here, target, self.tolerance):
            # ちょうどには着けない (障害物・アイテム・ほかのキャラクターなど): 近くまででよい
            st.arrive_tiles = self.tolerance * 1.42  # 斜めに tolerance マス離れていても着いたことにする
            st.max_walk_s = max(2.0, self.max_walk_s - self.exact_s)
            res = self.walker.walk_home()
            _, here = self.walker._loc()
        if not ok:
            ok = self._within(here, target, self.tolerance)
            if ok:
                res = f"近くに着いた (許容 ±{self.tolerance})"
        st.arrive_tiles, st.max_walk_s = 0.5, self.max_walk_s
        where = f"({here.x}, {here.y})" if here is not None else "(現在地不明)"
        self.log("INFO" if ok else "WARN", f"[往復] {label} ({target[1]}, {target[2]}): {res}  今 {where}")
        return res

    def press_helper(self, moved: bool = True) -> None:
        """MU Helper を必ず入れる。Home は入・切の切り替えなので、入ったのを確かめてから次へ進む:
          * 歩いたあと (moved) は切れているので、見ずにすぐ Home (キーは 80ms 押したまま。短いと取りこぼす)
          * 歩いていないときは、先に学習済みの AI で見て、動作中なら押さない
          * 押したら 0.15 秒ごとに AI で見て、2 回続けて動作中なら終わり。helper_wait_s 秒たっても入らなければもう一度
        AI が無ければ Home を押すだけ"""
        key = self.helper_key
        probe = self.walker.helper_probe
        if probe is None:
            self.run(f"kd:{key};w:80;ku:{key}")
            self.log("INFO", f"[往復] {key} を押しました (MU Helper。状態の確認はしていません)")
            return
        self.status = "MU Helper を入れています"
        if not moved:
            st, _p, _ = self.walker.confirm(2, 150)
            if st is True:
                self.log("INFO", "[往復] MU Helper はもう動作中です")
                return
        t_all = time.monotonic()
        for attempt in range(1, 4):
            self.run(f"kd:{key};w:80;ku:{key}")
            t0 = time.monotonic()
            on = 0
            while time.monotonic() - t0 < self.helper_wait_s:
                self._sleep(0.15)
                r = probe()
                on = on + 1 if r.get("state") is True else 0
                if on >= 2:
                    self.walker.helper_seen = f"動作中 ({float(r.get('p', 0)):.2f})"
                    self.log("INFO", f"[往復] MU Helper 動作中を確認 ({key} {attempt} 回 / "
                                     f"{time.monotonic() - t_all:.1f} 秒)")
                    return
            self.log("WARN", f"[往復] {key} を押して {self.helper_wait_s:g} 秒たっても MU Helper が動作中になりません。"
                             "もう一度押します" if attempt < 3 else
                             f"[往復] MU Helper を動作中にできませんでした ({key} 3 回)")

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
        except InterruptedError as e:
            self.stop_reason = str(e)
            why = {"stopped": "止めました", "focus lost": "送信先の窓からフォーカスが外れたので止めました"}.get(
                str(e), f"止めました ({e})")
            self.log("INFO", f"[往復] {why}")
        except Exception as e:  # noqa: BLE001
            self.log("ERR", f"[往復] 止まりました: {type(e).__name__}: {e}")
            try:  # 止まるときも MU Helper は入れておく (ゲームが前面のままなら)
                self.press_helper(moved=False)
            except Exception as e2:  # noqa: BLE001
                self.log("WARN", f"[往復] 止まるときに MU Helper を確かめられませんでした: {e2}")
        finally:
            self.status = "停止中"

    def _loop(self) -> None:
        if self.base_xy is not None:  # 開始ボタンを押したときの座標を基準点にする
            bx, by = self.base_xy
            here = Location(self.map_name, int(bx), int(by))
            self._last, self._last_t = here, self.clock()  # 読み違いの判定もここから
            src = "開始ボタンを押したときの座標"
        else:
            self.status = "現在地を読んでいます"
            here = self.read_here()
            if here is None:
                raise RuntimeError("現在地が読めません (画面右下の座標。OCR が使えるか確認してください)")
            src = "今の座標"
        self.origin = (here.map, int(here.x), int(here.y))
        if self.walker.helper_probe is not None:
            st, p, _ = self.walker.confirm(2)
            self.log("INFO", f"[往復] MU Helper の今の状態 (学習済みの AI): {self.walker.helper_seen}")
        self.log("INFO", f"[往復] 基準点 ({src}): {here.map} ({here.x}, {here.y})  {self.tiles} マス / "
                         f"{self.wait_s:g} 秒 / 着けないときは ±{self.tolerance} マスまで")
        need = not self.walker.calib.ok()
        if not need:
            # 座標の軸は画面から見て 45 度傾いている (画面の真下へ歩くと X も Y も同じくらい変わる)。
            # 記録がそうなっていなければ (古い・読み違いで測った) 調べ直す
            (a, b), (c, d) = self.walker.calib.A

            def diag(p, q):
                return min(abs(p), abs(q)) > 0 and 0.5 <= abs(p) / abs(q) <= 2.0

            if not (diag(a, c) and diag(b, d)):
                need = True
                self.log("WARN", f"[往復] 画面の向きの記録 (models/home_nav.json) が「座標は画面から 45 度傾いている」と"
                                 f"合いません (画面の右へ 100px で X {a * 100:+.1f} / Y {c * 100:+.1f}、下へ 100px で "
                                 f"X {b * 100:+.1f} / Y {d * 100:+.1f} マス)。調べ直します")
        if need:
            self.status = "画面の向きを調べています"
            self.log("INFO", "[往復] 画面の向き (何ピクセルで何マス) を調べます。右・下などへ少し歩きます")
            if not self.walker.calibrate(self.grab()):
                raise RuntimeError("画面の向きを調べられません (壁際? 少し開けた所で始めてください)")
            (a, b), (c, d) = self.walker.calib.A
            self.log("INFO", f"[往復] 測った向き: 画面の右へ 100px で X {a * 100:+.1f} / Y {c * 100:+.1f} マス、"
                             f"下へ 100px で X {b * 100:+.1f} / Y {d * 100:+.1f} マス")
            self.walk_to(self.origin, "基準点")
        (a, b), (c, d) = self.walker.calib.A
        px = 1 / max(1e-6, math.hypot(b, d))
        up, down = self.offset_target(-1), self.offset_target(+1)
        self.log("INFO", f"[往復] 上 = ({up[1]}, {up[2]})  下 = ({down[1]}, {down[2]})  "
                         f"[{ {'y': '座標の Y', 'x': '座標の X', 'screen': '画面の上下'}[self.axis]} ±{self.tiles}]  "
                         f"画面の縦 1 マス ≒ {px:.0f} px")
        while True:
            for label, target in (("上", up), ("下", down)):
                n = self.clicks
                self.walk_to(target, label)
                self.press_helper(moved=self.clicks > n)
                self.status = f"{label} で待機 {self.wait_s:g} 秒"
                self._sleep(self.wait_s)
                n = self.clicks
                self.walk_to(self.origin, "基準点")
                self.press_helper(moved=self.clicks > n)
                self.status = f"基準点で待機 {self.wait_s:g} 秒"
                self._sleep(self.wait_s)
            self.laps += 1
            self.log("INFO", f"[往復] {self.laps} 周しました")
