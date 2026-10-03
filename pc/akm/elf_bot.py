"""MU Online (Season 6) エルフ用ボット。

1 ループの流れ (優先度の高い順):
  1. 緊急停止キー / ゲームが前面にない → 攻撃を止めて待機
  2. 死亡判定 (HP 0 が続く) → 復帰スクリプト
  3. HP/MP 回復 (エルフ Heal / ポーション)
  4. 宝石・Zen の拾得 (画面認識で見つけたラベルをクリック → 拾ったら元の位置へ戻る)
  5. 自己バフ (Greater Defense / Greater Damage) の掛け直し
  6. 攻撃: 右ボタンを押したまま、モンスター検出があればそちら、なければ周囲を順に向く

座標はゲーム画面 (クライアント領域) のピクセル。送信時にスクリーン座標へ変換する。
"""
from __future__ import annotations

import math
import time
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from .device import Device, DeviceError
from .screen import GameScreen
from .vision import Detection, bar_level


@dataclass
class Status:
    hp: float = 1.0
    mp: float = 1.0
    detections: list[Detection] = field(default_factory=list)


def character_pos(shape: tuple[int, ...], offset: list[float]) -> tuple[float, float]:
    """キャラクターの画面上の位置 (カメラ追従なので常にほぼ中央)。"""
    h, w = shape[:2]
    return w / 2 + offset[0] * h, h / 2 + offset[1] * h


def choose_pickup(
    dets: list[Detection],
    char: tuple[float, float],
    screen_h: int,
    targets: list[str],
    max_distance: float,
    ignored: dict[str, float],
    now: float,
) -> Detection | None:
    """拾う対象のうちキャラに一番近いものを返す。"""
    best, best_d = None, float("inf")
    for d in dets:
        if d.kind != "item":
            continue
        if targets and d.label not in targets:
            continue
        if ignored.get(d.label, 0) > now:
            continue
        cx, cy = d.center
        dist = math.hypot(cx - char[0], cy - char[1]) / screen_h
        if dist <= max_distance and dist < best_d:
            best, best_d = d, dist
    return best


def nearest(dets: list[Detection], kind: str, pos: tuple[float, float]) -> Detection | None:
    cands = [d for d in dets if d.kind == kind]
    if not cands:
        return None
    return min(cands, key=lambda d: math.hypot(d.center[0] - pos[0], d.center[1] - pos[1]))


class ElfBot:
    def __init__(
        self,
        cfg: dict,
        screen: GameScreen,
        device: Device,
        detectors: list,
        show: bool = False,
        helper=None,
        collector=None,
    ):
        self.cfg = cfg
        self.ecfg = cfg["elf"]
        # helper: 攻撃は MU Helper に任せ、ボットは拾得・回復・死亡復帰だけ行う
        # manual: 攻撃・バフもボットが行う
        self.mode = self.ecfg.get("mode", "helper")
        self.helper = helper if self.mode == "helper" else None
        self.collector = collector
        self.screen = screen
        self.dev = device
        self.detectors = detectors
        self.show = show

        self.holding_attack = False
        self.dir_index = 0
        self.last_dir_change = 0.0
        self.last_buff = 0.0  # 起動直後にバフを掛ける
        self.last_potion = 0.0
        self.last_heal = 0.0
        self.hp_zero_since: float | None = None
        self.ignored: dict[str, float] = {}
        self.picked: Counter[str] = Counter()
        self.last_report = time.monotonic()
        self.img: np.ndarray | None = None

    # ------------------------------------------------------------ helpers --
    def _pt(self, x: float, y: float) -> str:
        """クライアント座標 → 絶対移動コマンド"""
        sx, sy = self.screen.to_screen(x, y)
        return self.dev.s_move(sx, sy)

    def _char(self) -> tuple[float, float]:
        assert self.img is not None
        return character_pos(self.img.shape, self.ecfg.get("character_offset", [0, 0]))

    def _clamp(self, x: float, y: float) -> tuple[float, float]:
        assert self.img is not None
        h, w = self.img.shape[:2]
        m = 0.03 * h
        return min(max(x, m), w - m), min(max(y, m), h - m)

    def observe(self) -> Status:
        self.img = self.screen.grab()
        hud = self.cfg["hud"]
        st = Status()
        st.hp = bar_level(self.img, hud["hp"]["roi"], hud["hp"].get("color", "red"), hud["hp"].get("direction", "vertical"))
        st.mp = bar_level(self.img, hud["mp"]["roi"], hud["mp"].get("color", "blue"), hud["mp"].get("direction", "vertical"))
        for det in self.detectors:
            st.detections.extend(det.detect(self.img))
        return st

    def release_attack(self) -> None:
        if self.holding_attack:
            self.dev.run("bu:R")
            self.holding_attack = False

    # ------------------------------------------------------------ actions --
    def handle_death(self, st: Status, now: float) -> bool:
        dcfg = self.ecfg.get("death") or {}
        if st.hp > 0.02:
            self.hp_zero_since = None
            return False
        if self.hp_zero_since is None:
            self.hp_zero_since = now
            return False
        if now - self.hp_zero_since < float(dcfg.get("hp_zero_s", 3.0)):
            return False
        print("[bot] 死亡と判定。復帰スクリプトを実行します")
        self.release_attack()
        script = dcfg.get("script")
        if script:
            self.dev.run(script, timeout=120)
        if self.helper:
            self.helper.believed_on = False  # 死亡で MU Helper は止まる
            self.helper.ensure_on(self.screen.grab())
        self.hp_zero_since = None
        self.last_buff = 0.0  # 復帰後にバフを掛け直す
        return True

    def handle_recovery(self, st: Status, now: float) -> None:
        heal = self.ecfg.get("heal")
        pots = self.ecfg.get("potions") or {}
        cooldown = float(pots.get("cooldown_s", 1.0))

        if heal and self.mode == "manual" and st.hp < float(heal["hp_below"]) and st.mp > 0.1 and now - self.last_heal > float(heal.get("cooldown_s", 1.5)):
            # Heal は自分にカーソルを合わせて右クリック
            self.release_attack()
            cx, cy = self._char()
            atk = self.ecfg["attack"]["skill_key"]
            self.dev.run(f"k:{heal['key']};w:80;{self._pt(cx, cy)};w:30;c:R;w:{int(heal.get('cast_ms', 600))};k:{atk}")
            self.last_heal = now
            return

        if now - self.last_potion < cooldown:
            return
        if pots.get("hp_key") and st.hp < float(pots.get("hp_below", 0.4)):
            self.dev.key(pots["hp_key"])
            self.last_potion = now
        elif pots.get("mp_key") and st.mp < float(pots.get("mp_below", 0.2)):
            self.dev.key(pots["mp_key"])
            self.last_potion = now

    def handle_pickup(self, st: Status, now: float) -> bool:
        pcfg = self.ecfg["pickup"]
        assert self.img is not None
        h = self.img.shape[0]
        char = self._char()
        target = choose_pickup(
            st.detections, char, h, pcfg.get("targets", []), float(pcfg.get("max_distance", 0.35)), self.ignored, now
        )
        if target is None:
            return False

        self.release_attack()
        off = pcfg.get("click_offset", [0, 0.6])
        tx = target.center[0] + off[0] * target.h
        ty = target.center[1] + off[1] * target.h
        tx, ty = self._clamp(tx, ty)
        vec = (tx - char[0], ty - char[1])

        picked = False
        for attempt in range(int(pcfg.get("attempts", 2))):
            self.dev.run(f"{self._pt(tx, ty)};w:30;c:L")
            if self.helper:
                self.helper.note_mouse_used()  # クリックで MU Helper は止まる
            deadline = time.monotonic() + float(pcfg.get("walk_timeout_s", 3.0))
            while time.monotonic() < deadline:
                time.sleep(0.15)
                st2 = self.observe()
                left = [d for d in st2.detections if d.label == target.label]
                near = nearest(left, "item", self._char())
                # キャラの近くに同じラベルが残っていなければ拾えたとみなす
                if near is None or math.hypot(near.center[0] - char[0], near.center[1] - char[1]) / h > 0.25:
                    picked = True
                    break
                # まだ残っている: 位置が変わっていれば (歩いた) 狙い直す
                tx, ty = self._clamp(near.center[0] + off[0] * near.h, near.center[1] + off[1] * near.h)
            if picked:
                break

        if picked:
            self.picked[target.label] += 1
            print(f"[bot] 拾得: {target.label}  (累計 {dict(self.picked)})")
            if pcfg.get("return_to_anchor", True):
                bx, by = self._clamp(char[0] - vec[0], char[1] - vec[1])
                self.dev.run(f"{self._pt(bx, by)};w:30;c:L;w:{int(pcfg.get('return_wait_ms', 800))}")
        else:
            # インベントリ満杯などで拾えない: しばらく無視する
            print(f"[bot] 拾えませんでした: {target.label} (しばらく無視)")
            self.ignored[target.label] = now + float(pcfg.get("ignore_s", 15))
        return True

    def handle_buffs(self, now: float) -> bool:
        bcfg = self.ecfg.get("buffs") or {}
        keys = bcfg.get("keys") or []
        if not keys or now - self.last_buff < float(bcfg.get("interval_s", 60)):
            return False
        self.release_attack()
        cx, cy = self._char()
        cast = int(bcfg.get("cast_ms", 700))
        parts = [f"{self._pt(cx, cy)};w:30"]
        for k in keys:
            parts.append(f"k:{k};w:120;c:R;w:{cast}")
        parts.append(f"k:{self.ecfg['attack']['skill_key']}")
        self.dev.run(";".join(parts), timeout=60)
        self.last_buff = now
        return True

    def handle_attack(self, st: Status, now: float) -> None:
        acfg = self.ecfg["attack"]
        assert self.img is not None
        h = self.img.shape[0]
        cx, cy = self._char()

        monster = nearest(st.detections, "monster", (cx, cy))
        if monster is not None:
            ax, ay = monster.center
        else:
            n = int(acfg.get("directions", 8))
            dwell = float(acfg.get("dwell_ms", 600)) / 1000
            if now - self.last_dir_change >= dwell:
                self.dir_index = (self.dir_index + 1) % n
                self.last_dir_change = now
            ang = 2 * math.pi * self.dir_index / n
            r = float(acfg.get("radius", 0.18)) * h
            ax, ay = cx + r * math.cos(ang), cy + r * math.sin(ang) * 0.6  # 斜め見下ろしなので縦を潰す
        ax, ay = self._clamp(ax, ay)

        if not self.holding_attack:
            self.dev.run(f"k:{acfg['skill_key']};{self._pt(ax, ay)};w:20;bd:R")
            self.holding_attack = True
        else:
            self.dev.run(self._pt(ax, ay))

    # --------------------------------------------------------------- main --
    def report(self, st: Status, now: float) -> None:
        if now - self.last_report < 10:
            return
        self.last_report = now
        hs = ""
        if self.helper is not None:
            hs = f"  MU Helper {'ON' if self.helper.is_on() else 'OFF'} (切替 {self.helper.toggles} 回)"
        print(f"[bot] HP {st.hp:.0%}  MP {st.mp:.0%}  検出 {len(st.detections)}  拾得 {dict(self.picked)}{hs}")

    def step(self) -> None:
        now = time.monotonic()
        st = self.observe()
        if self.show:
            self._show(st)
        if self.collector is not None:
            self.collector.collect(self.img, st.detections)
        if self.handle_death(st, now):
            return
        self.handle_recovery(st, now)
        if self.ecfg["pickup"].get("enabled", True) and self.handle_pickup(st, now):
            if self.helper is not None:
                self.helper.ensure_on(self.img)  # 拾い終わったらすぐ再開
            return
        if self.mode == "helper":
            if self.helper is not None:
                self.helper.ensure_on(self.img)  # 拾得のクリック等で止まっていたら再開
            self.report(st, now)
            return
        if self.handle_buffs(now):
            return
        self.handle_attack(st, now)
        self.report(st, now)

    def _show(self, st: Status) -> None:
        import cv2

        from .vision import draw

        vis = draw(self.img, st.detections, {"HP": f"{st.hp:.0%}", "MP": f"{st.mp:.0%}"})
        cv2.imshow("akm", cv2.resize(vis, None, fx=0.6, fy=0.6))
        cv2.waitKey(1)

    def run(self) -> None:
        """待機状態で起動し、開始キーで動作、停止キーで待機に戻る。Ctrl+C で終了。"""
        from .screen import key_pressed

        g = self.cfg["game"]
        start_vk = int(g.get("start_key_vk", 0x21))  # PageUp
        stop_vk = int(g.get("stop_key_vk", 0x22))    # PageDown
        interval = float(self.cfg.get("loop_interval_ms", 50)) / 1000
        self.screen.locate()
        print(f"[bot] ゲーム画面 {self.screen.rect}")
        print("[bot] 待機中: PageUp で開始 / PageDown で停止 / Ctrl+C で終了")

        running = False
        try:
            while True:
                if not running:
                    if key_pressed(start_vk):
                        running = True
                        self.last_buff = 0.0
                        print(f"[bot] 開始 ({self.mode} モード / PageDown で停止)")
                        if self.helper is not None and self.screen.is_active():
                            self.helper.ensure_on(self.screen.grab())
                    time.sleep(0.05)
                    continue
                if key_pressed(stop_vk):
                    running = False
                    self.pause()
                    print(f"[bot] 停止。拾得数: {dict(self.picked)}  (PageUp で再開)")
                    continue
                if g.get("require_foreground", True) and not self.screen.is_active():
                    self.release_attack()
                    time.sleep(0.5)
                    continue
                try:
                    self.step()
                except DeviceError as e:
                    print(f"[bot] デバイスエラー: {e}  (1秒後に再試行)")
                    self.holding_attack = False
                    time.sleep(1.0)
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
        finally:
            self.pause()
            print(f"[bot] 終了。拾得数: {dict(self.picked)}")

    def pause(self) -> None:
        """押しっぱなしのキー・ボタンを全部離す。MU Helper も止める (helper.stop_on_pause)。"""
        self.holding_attack = False
        try:
            self.dev.stop()
            self.dev.release_all()
            if self.helper is not None and self.helper.cfg.get("stop_on_pause", True):
                self.helper.ensure_off()
        except Exception:
            pass
