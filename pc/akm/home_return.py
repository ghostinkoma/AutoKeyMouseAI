"""MU Helper の帰還: 狩場から流されないよう、MU Helper を手動で始めた場所へ定期的に歩いて戻る。

  記録: MU Helper が手で開始された (停止中 → 動作中。自分で押したときは除く) ときの現在地を「帰る場所」に
  繰り返し (interval_s ごと。MU Helper が動作中のときだけ):
    ① ゲームを手前に出す (別の仮想デスクトップにあっても。今手前にある窓は覚えておく)
    ② MU Helper を切る (Home)
    ③ off_wait_ms (100ms) 待つ
    ④ 帰る場所へ歩く (クリックした方向と現在地の変わり方から、画面の向きを自分で調べる)
       ②〜④ の間は HP を見て、potion_hp より少なければポーション (Q)
    ⑤ MU Helper を入れる (Home)。入ったか画面で確かめ、はっきり停止中なら押し直す
    ⑥ 元の窓を手前に戻す
設定は地図ビューアの「MU Helper の帰還」で変える (pc/models/home_return.properties)。
今の状態は pc/models/home_return_state.properties に書き、ビューアに出す。
"""
from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULTS = {
    "enabled": "false",        # 帰還する
    "interval_s": "120",       # 何秒ごとに帰還するか
    "min_distance": "0",       # 帰る場所からこのマス数以内なら、その回は何もしない (0 = 毎回)
    "off_wait_ms": "100",      # ③ MU Helper を切ってから歩き出すまで
    "arrive_tiles": "1",       # 帰る場所からこのマス数以内に着いたら終わり
    "max_walk_s": "20",        # 歩くのをあきらめる時間
    "potion_key": "q",         # ポーションのキー
    "potion_hp": "0.5",        # HP がこれより少なければポーション (0..1)
    "potion_every_ms": "400",  # ポーションを続けて押す間隔
    "helper_key": "home",      # MU Helper のオン/オフ
    "restore_focus": "true",   # ⑥ 元の窓を手前に戻す
    "home_override": "",       # ビューアで帰る場所を指定 "マップ,x,y" (空 = MU Helper を手で始めた場所)
    "override_seq": "0",       # ビューアが指定し直すたびに増やす
}


def load_props(path: Path) -> dict[str, str]:
    """key=value の設定ファイル (Java の Properties と同じ書き方の簡単なもの)。"""
    out = dict(DEFAULTS)
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line[0] in "#!" or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().replace("\\:", ":").replace("\\=", "=").replace("\\\\", "\\")
    except OSError:
        pass
    return out


def save_props(path: Path, d: dict, header: str = "") -> None:
    import os

    text = (f"# {header}\n" if header else "") + "".join(f"{k}={v}\n" for k, v in d.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # ビューアが読んでいる最中。次の回に書く


@dataclass
class Settings:
    enabled: bool = False
    interval_s: float = 120.0
    min_distance: float = 0.0
    off_wait_ms: int = 100
    arrive_tiles: float = 1.0
    max_walk_s: float = 20.0
    potion_key: str = "q"
    potion_hp: float = 0.5
    potion_every_ms: int = 400
    helper_key: str = "home"
    restore_focus: bool = True
    home_override: str = ""
    override_seq: int = 0

    @classmethod
    def from_props(cls, p: dict[str, str]) -> "Settings":
        def f(k, conv, default):
            try:
                return conv(p.get(k, ""))
            except (TypeError, ValueError):
                return default

        b = lambda v: str(v).strip().lower() in ("1", "true", "yes", "on")  # noqa: E731
        d = cls()
        return cls(enabled=b(p.get("enabled", "false")), interval_s=max(10.0, f("interval_s", float, d.interval_s)),
                   min_distance=f("min_distance", float, d.min_distance),
                   off_wait_ms=max(0, f("off_wait_ms", int, d.off_wait_ms)),
                   arrive_tiles=max(0.5, f("arrive_tiles", float, d.arrive_tiles)),
                   max_walk_s=max(2.0, f("max_walk_s", float, d.max_walk_s)),
                   potion_key=(p.get("potion_key") or d.potion_key).strip().lower(),
                   potion_hp=f("potion_hp", float, d.potion_hp),
                   potion_every_ms=max(50, f("potion_every_ms", int, d.potion_every_ms)),
                   helper_key=(p.get("helper_key") or d.helper_key).strip().lower(),
                   restore_focus=b(p.get("restore_focus", "true")),
                   home_override=(p.get("home_override") or "").strip(),
                   override_seq=f("override_seq", int, 0))


def parse_home(s: str):
    """'Icarus,51,70' → (マップ, x, y)。だめなら None。"""
    parts = [x.strip() for x in s.split(",")]
    if len(parts) != 3 or not parts[0]:
        return None
    try:
        return parts[0], int(parts[1]), int(parts[2])
    except ValueError:
        return None


@dataclass
class NavCalib:
    """画面のクリックの向き (ピクセル) → 現在地の変わり方 (マス)。A は 2x2 (マス / ピクセル)。
    最初は自分で調べる (右・下にクリックして現在地の変化を見る)。結果は保存して次から使う。"""
    A: list[list[float]] | None = None

    def ok(self) -> bool:
        if self.A is None:
            return False
        (a, b), (c, d) = self.A
        return abs(a * d - b * c) > 1e-6

    def offset_for(self, dx: float, dy: float) -> tuple[float, float]:
        """マスの差 (dx, dy) → クリックする画面の向き (ピクセル)。"""
        (a, b), (c, d) = self.A
        det = a * d - b * c
        return (d * dx - b * dy) / det, (-c * dx + a * dy) / det

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"A": self.A}), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "NavCalib":
        try:
            return cls(json.loads(Path(path).read_text(encoding="utf-8"))["A"])
        except (OSError, ValueError, KeyError, TypeError):
            return cls()


@dataclass
class HomeReturn:
    """帰還の手順。画面・キー・窓の操作は呼び出し側から関数で渡す (テストでは偽物を渡す)。

    run(script)        : ESP32 のマクロを実行 (例 "k:home")
    grab()             : ゲーム画面 (BGR)
    read_loc(img)      : 現在地 (map, x, y を持つもの) か None
    read_hp(img)       : HP 0..1 か None (読めない)
    helper_state()     : MU Helper が動作中か (True / False / None = 分からない)
    click_at(x, y)     : ゲーム画面の座標を左クリックするマクロ (文字列)
    char_pos(img)      : キャラクターの画面上の位置 (x, y)
    activate()         : ゲームを手前に出す (True = 出せた)
    foreground()/restore(hwnd) : 今手前にある窓 / その窓を手前に戻す
    lock               : キー操作の順番待ち (PT 招待のお断りと同時に押さないように)
    helper_now()       : 今の画面を撮って MU Helper を確かめる (帰還の中で使う。無ければ helper_state)
    """
    run: callable
    grab: callable
    read_loc: callable
    read_hp: callable
    helper_state: callable
    click_at: callable
    char_pos: callable
    activate: callable = None
    foreground: callable = None
    restore: callable = None
    lock: object = None
    helper_now: callable = None
    settings_path: Path | None = None
    state_path: Path | None = None
    calib_path: Path | None = None
    log: callable = print
    clock: callable = time.monotonic
    sleep: callable = time.sleep
    settings: Settings = field(default_factory=Settings)
    home: tuple | None = None          # (マップ, x, y)
    home_src: str = ""                 # "helper" (手で開始した場所) / "viewer"
    status: str = "待機中"
    cycles: int = 0
    last_result: str = ""
    _next: float = 0.0
    _prev_helper: bool | None = None
    _own_until: float = 0.0            # 自分で Home を押した直後 (この間の「開始」は手動ではない)
    _seq: int = -1
    _settings_mtime: float = -1.0
    _last_potion: float = -1e9
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def __post_init__(self):
        self.calib = NavCalib.load(self.calib_path) if self.calib_path else NavCalib()
        if self.state_path is not None:  # 前回の帰る場所を引き継ぐ
            st = load_props(self.state_path)
            h = parse_home(st.get("home", ""))
            if h:
                self.home, self.home_src = h, st.get("home_src", "helper")

    # ------------------------------------------------------------------ 設定・状態
    def reload_settings(self) -> None:
        if self.settings_path is None:
            return
        try:
            mt = self.settings_path.stat().st_mtime
        except OSError:
            mt = 0.0
        if mt == self._settings_mtime:
            return
        self._settings_mtime = mt
        old = self.settings
        self.settings = Settings.from_props(load_props(self.settings_path))
        if self.settings.override_seq != self._seq:
            self._seq = self.settings.override_seq
            h = parse_home(self.settings.home_override)
            if h and self._seq > 0:
                self.home, self.home_src = h, "viewer"
                self.log(f"[home] 帰る場所をビューアの指定に: {h[0]} ({h[1]}, {h[2]})")
        if self.settings.enabled != old.enabled or self.settings.interval_s != old.interval_s:
            self._next = self.clock() + self.settings.interval_s
            self.log(f"[home] 帰還 {'オン' if self.settings.enabled else 'オフ'} ({self.settings.interval_s:.0f} 秒ごと)")
        self.write_state()

    def write_state(self, here=None) -> None:
        if self.state_path is None:
            return
        left = max(0, self._next - self.clock()) if self.settings.enabled else 0
        d = {
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": self.status,
            "home": f"{self.home[0]},{self.home[1]},{self.home[2]}" if self.home else "",
            "home_src": self.home_src,
            "here": f"{here.map},{here.x},{here.y}" if here is not None else "",
            "next_in_s": f"{left:.0f}",
            "cycles": str(self.cycles),
            "last_result": self.last_result,
            "calibrated": "yes" if self.calib.ok() else "no",
        }
        save_props(self.state_path, d, "MU Helper の帰還の状態 (学習ループが書く。地図ビューアが読む)")

    # ------------------------------------------------------------------ 見張り
    def watch(self, here=None) -> None:
        """MU Helper が手で開始されたら、そのときの現在地を帰る場所にする。"""
        now = self.clock()
        cur = self.helper_state()
        if cur is True and self._prev_helper is False and now >= self._own_until:
            if here is not None:
                self.home, self.home_src = (here.map, int(here.x), int(here.y)), "helper"
                self.log(f"[home] MU Helper が手で開始されました。帰る場所: {here.map} ({here.x}, {here.y})")
                self._next = now + self.settings.interval_s
            else:
                self.log("[home] MU Helper が手で開始されましたが、現在地が読めないので帰る場所は前のまま")
        if cur is not None:
            self._prev_helper = cur

    def due(self) -> bool:
        s = self.settings
        return s.enabled and self.home is not None and self.clock() >= self._next

    # ------------------------------------------------------------------ 帰還の 1 回
    def _potion(self, img) -> None:
        """HP を見て、少なければポーション (間隔を空けて)。HP が読めなければ押さない。"""
        hp = self.read_hp(img) if img is not None else None
        now = self.clock()
        if hp is not None and hp < self.settings.potion_hp and \
                now - self._last_potion >= self.settings.potion_every_ms / 1000:
            self.run(f"k:{self.settings.potion_key}")
            self._last_potion = now

    def _toggle_helper(self) -> None:
        self._own_until = self.clock() + 4.0
        self.run(f"k:{self.settings.helper_key}")

    def _wait(self, ms: int) -> None:
        """ms 待つ間も HP を見てポーション。"""
        end = self.clock() + ms / 1000
        while True:
            self._potion(self.grab())
            left = end - self.clock()
            if left <= 0:
                return
            self.sleep(min(0.1, left))

    def _loc(self):
        img = self.grab()
        self._potion(img)
        return img, self.read_loc(img)

    def calibrate(self, img) -> bool:
        """右と下にクリックして、現在地の変わり方から画面の向きを調べる。"""
        cx, cy = self.char_pos(img)
        h = img.shape[0]
        r = 0.18 * h
        cols = []
        for ox, oy in ((r, 0.0), (0.0, r), (-r, 0.0), (0.0, -r)):
            _, a = self._loc()
            if a is None:
                return False
            self.run(self.click_at(cx + ox, cy + oy))
            self._wait(1200)
            _, b = self._loc()
            if b is None or b.map != a.map:
                return False
            d = (b.x - a.x, b.y - a.y)
            if math.hypot(*d) >= 1:
                cols.append(((ox, oy), d))
            if len(cols) >= 2 and abs(cols[0][0][0] * cols[1][0][1] - cols[0][0][1] * cols[1][0][0]) > 1:
                break
        if len(cols) < 2:
            return False
        # [d1 d2] = A [o1 o2]  →  A = [d1 d2] [o1 o2]^-1
        (o1, d1), (o2, d2) = cols[0], cols[1]
        det = o1[0] * o2[1] - o1[1] * o2[0]
        if abs(det) < 1:
            return False
        inv = [[o2[1] / det, -o2[0] / det], [-o1[1] / det, o1[0] / det]]
        A = [[d1[0] * inv[0][0] + d2[0] * inv[1][0], d1[0] * inv[0][1] + d2[0] * inv[1][1]],
             [d1[1] * inv[0][0] + d2[1] * inv[1][0], d1[1] * inv[0][1] + d2[1] * inv[1][1]]]
        self.calib = NavCalib(A)
        if not self.calib.ok():
            return False
        if self.calib_path:
            self.calib.save(self.calib_path)
        self.log(f"[home] 画面の向きを調べました (1 マス ≒ {1 / max(1e-6, math.hypot(A[0][0], A[1][0])):.0f} px)")
        return True

    def walk_home(self) -> str:
        s = self.settings
        start = self.clock()
        img, here = self._loc()
        if here is None:
            return "現在地が読めない"
        if here.map != self.home[0]:
            return f"別のマップにいる ({here.map})"
        if not self.calib.ok() and not self.calibrate(img):
            return "画面の向きを調べられない (壁際? 少し開けた所で MU Helper を開始してください)"
        best, stuck, turn = 1e9, 0, 0.0
        while self.clock() - start < s.max_walk_s:
            img, here = self._loc()
            if here is None:
                self._wait(200)
                continue
            if here.map != self.home[0]:
                return f"別のマップにいる ({here.map})"
            dx, dy = self.home[1] - here.x, self.home[2] - here.y
            dist = math.hypot(dx, dy)
            self.write_state(here)
            if dist <= s.arrive_tiles:
                return f"着いた ({here.x}, {here.y})"
            if dist < best - 0.5:
                best, stuck = dist, 0
            else:
                stuck += 1
                if stuck >= 3:  # 進まない (障害物?): 少し横にずらしてみる
                    turn = (turn + 50) % 360 if turn < 300 else 0
                    stuck = 0
            ox, oy = self.calib.offset_for(dx, dy)
            if turn:
                t = math.radians(turn if turn <= 180 else turn - 360)
                ox, oy = ox * math.cos(t) - oy * math.sin(t), ox * math.sin(t) + oy * math.cos(t)
            h = img.shape[0]
            n = math.hypot(ox, oy)
            lim = 0.30 * h  # 画面の中に収まるように
            if n > lim:
                ox, oy = ox * lim / n, oy * lim / n
            elif n < 0.06 * h and n > 0:
                ox, oy = ox * 0.06 * h / n, oy * 0.06 * h / n
            cx, cy = self.char_pos(img)
            self.run(self.click_at(cx + ox, cy + oy))
            self._wait(int(300 + 1500 * min(1.0, math.hypot(ox, oy) / lim)))
        return "時間切れ (着かなかった)"

    def _helper(self):
        return (self.helper_now or self.helper_state)()

    def cycle(self) -> str:
        """①〜⑥ を 1 回。戻り値は結果の文。"""
        s = self.settings
        self._next = self.clock() + s.interval_s
        if self._helper() is not True:
            return "MU Helper が動作中でないので何もしない"
        img = self.grab()
        here = self.read_loc(img)
        if here is not None and here.map == self.home[0] and s.min_distance > 0 and \
                math.hypot(self.home[1] - here.x, self.home[2] - here.y) <= s.min_distance:
            return f"帰る場所の近く ({here.x}, {here.y}) なので何もしない"
        prev = self.foreground() if (self.foreground and s.restore_focus) else None
        if self.activate is not None and not self.activate():  # ①
            return "ゲームを手前に出せない"
        try:
            self.status = "帰還中"
            self.write_state(here)
            self._toggle_helper()                 # ② MU Helper を切る
            self._wait(s.off_wait_ms)             # ③ (ポーションを見ながら)
            result = self.walk_home()             # ④ (ポーションを見ながら)
            self._toggle_helper()                 # ⑤ MU Helper を入れる
            self._wait(1500)
            if self._helper() is False:          # はっきり停止中ならもう一度
                self._toggle_helper()
                self._wait(1500)
                result += " / MU Helper を押し直した"
        finally:
            self.status = "待機中"
            if prev and self.restore is not None:  # ⑥
                try:
                    self.restore(prev)
                except Exception:
                    pass
        return result

    def step(self, here=None) -> None:
        """見張りの 1 回 (学習ループから 0.5 秒ごとに)。"""
        self.reload_settings()
        self.watch(here)
        if self.due():
            lock = self.lock
            if lock is not None and not lock.acquire(timeout=5):
                return  # PT 招待のお断り中。次の回に
            try:
                self.log(f"[home] 帰還します: {self.home[0]} ({self.home[1]}, {self.home[2]}) へ")
                self.last_result = self.cycle()
                self.cycles += 1
                self.log(f"[home] 帰還 {self.cycles} 回目: {self.last_result}")
            except Exception as e:
                self.last_result = f"失敗: {e}"
                self.log(f"[home] 帰還に失敗: {e}")
            finally:
                if lock is not None:
                    lock.release()
        self.write_state(here)

    def start(self, here_fn=lambda: None, period_s: float = 0.5) -> None:
        def loop():
            while not self._stop.is_set():
                try:
                    self.step(here_fn())
                except Exception as e:
                    self.log(f"[home] 失敗: {e}")
                self._stop.wait(period_s)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
