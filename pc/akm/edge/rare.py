"""めったに出ない窓 (パーティの勧誘など) を見張る。

  1. 見つけた瞬間の画面を dataset/screens/rare/<名前>/ に保存する (学習の見本が自然にたまる)。
     「ある」とまでは言えないが似ている画面 (一致度 maybe 以上) も dataset/screens/rare/_maybe/<名前>/ に残す
     → 見落としがないかを後で確かめられる。
  2. 断る設定 (decline) がある窓は、続けて confirm 回見えたら、キー (esc など) かクリックで断る。
     ゲームの窓が手前にあるときだけ押す (別のアプリを操作中に Esc などを送らないように)。

dataset/frames は 30 秒に 1 枚で古いものから消されるが、ここに保存した画面は消さない (1 種類 max_per_name 枚まで)。
"""
from __future__ import annotations

import time
from pathlib import Path

import cv2


class RareWatch:
    def __init__(self, names, out_dir: Path, decline: dict | None = None, press=None, click=None,
                 is_active=None, confirm: int = 2, cooldown_s: float = 4.0, save_every_s: float = 3.0,
                 maybe: float = 0.55, maybe_every_s: float = 60.0, max_per_name: int = 60, log=print,
                 clock=time.monotonic, responder=None, skip_wait_s: float = 30.0):
        self.names = set(names) | set(decline or {})
        self.out = Path(out_dir)
        self.decline = decline or {}   # 名前 → {"key": "esc"} / {"click": [横の割合, 縦の割合]} (窓の枠の中の位置)
                                       #        / 手順の list (akm/edge/respond.py)
        self.press = press             # (key) → None
        self.click = click             # (x, y) 画面のピクセル → None
        self.is_active = is_active or (lambda: True)
        self.confirm = confirm
        self.cooldown_s = cooldown_s
        self.save_every_s = save_every_s
        self.maybe = maybe
        self.maybe_every_s = maybe_every_s
        self.max_per_name = max_per_name
        self.log = log
        self.clock = clock
        self.skip_wait_s = skip_wait_s  # 条件に合わず断らなかった窓に、もう一度試すまでの秒数
        self.responder = responder     # akm.edge.respond.Responder (decline が手順の list のとき)
        self.hits: dict[str, int] = {}
        self.seen: dict[str, int] = {}       # 見つけた回数 (続けて見えている間は 1 回)
        self.declined: dict[str, int] = {}
        self.saved: dict[str, int] = {}
        self._last_save: dict[str, float] = {}
        self._last_act: dict[str, float] = {}

    def _save(self, img, sub: str, name: str, every: float) -> bool:
        now = self.clock()
        key = f"{sub}/{name}"
        if now - self._last_save.get(key, -1e9) < every:
            return False
        d = self.out / sub / name if sub else self.out / name
        d.mkdir(parents=True, exist_ok=True)
        files = sorted(d.glob("*.png"))
        if len(files) >= self.max_per_name:
            files[0].unlink()
        cv2.imwrite(str(d / time.strftime("%Y%m%d_%H%M%S.png")), img)
        self._last_save[key] = now
        if not sub:
            self.saved[name] = self.saved.get(name, 0) + 1
        return True

    def feed(self, img, dets) -> list[str]:
        """1 回の認識結果を受け取る。行ったこと ("save:名前" / "decline:名前") を返す。"""
        done = []
        for d in dets:
            if d.name not in self.names:
                continue
            if not d.present:
                self.hits[d.name] = 0
                if d.ncc >= self.maybe and self._save(img, "_maybe", d.name, self.maybe_every_s):
                    done.append(f"maybe:{d.name}")
                continue
            self.hits[d.name] = self.hits.get(d.name, 0) + 1
            if self.hits[d.name] == 1:
                self.seen[d.name] = self.seen.get(d.name, 0) + 1
                self.log(f"[rare] {d.name} が出ました (一致度 {d.ncc:.2f} ネット {d.nn:.2f}) 画面を保存します")
            if self._save(img, "", d.name, self.save_every_s):
                done.append(f"save:{d.name}")
            act = self.decline.get(d.name)
            if not act or self.hits[d.name] < self.confirm:
                continue
            now = self.clock()
            if now - self._last_act.get(d.name, -1e9) < self.cooldown_s:
                continue
            can_activate = isinstance(act, list) and getattr(self.responder, "activate", None) is not None
            if not self.is_active() and not can_activate:  # 手順 (responder) ならゲームを手前に出して行う
                if self.hits[d.name] == self.confirm:  # 見えている間に 1 回だけ知らせる
                    self.log(f"[rare] {d.name} を断りたいが、ゲームの窓が手前に無いので押しません")
                continue
            self._last_act[d.name] = now
            if isinstance(act, list):  # 手順 (Cancel をクリック → 窓を閉じる → チャット …)
                if self.responder is None:
                    continue
                res = self.responder.respond(d.name, act, d)
                if "click" not in res:
                    # 条件に合わず断らなかった: 同じ窓に何度も試さないよう、しばらく (skip_wait_s) 待つ
                    self._last_act[d.name] = now + self.skip_wait_s - self.cooldown_s
                    continue
                self.declined[d.name] = self.declined.get(d.name, 0) + 1
                done.append(f"decline:{d.name}")
                self.hits[d.name] = 0
                continue
            if act.get("click") and self.click is not None:
                fx, fy = act["click"]
                x, y, w, h = d.box
                self.click(x + w * float(fx), y + h * float(fy))
                how = f"クリック ({fx}, {fy})"
            elif self.press is not None:
                self.press(act.get("key", "esc"))
                how = f"キー {act.get('key', 'esc')}"
            else:
                continue
            self.declined[d.name] = self.declined.get(d.name, 0) + 1
            self.log(f"[rare] {d.name} を断りました ({how}、{self.declined[d.name]} 回目)")
            done.append(f"decline:{d.name}")
        return done
