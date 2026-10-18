"""めったに出ない窓への「応じ方」を手順として実行する (判定 → 行動の最初の一歩)。

手順 (config.yaml の rare_windows.respond.<窓の名前> に書く。無ければ DEFAULT_RESPONSES):
  - click: party_invite_cancel   登録した物体 (Cancel ボタンなど) の真ん中をクリック
    click: [0.75, 0.9]           見つけた窓の枠の中の位置 (横, 縦 の割合) をクリック
                                 (ボタンが見つからないときは fallback_key を押す。既定 esc)
  - close_windows: esc           開いている窓の数だけ esc を押す (keep に書いた窓は数えない)
                                 窓が無いのに esc を押すとゲームのメニューが開くので、見えている数しか押さない
  - chat: "Sorry, I no need PT." Enter → 文字 → Enter でチャットに送る (ASCII だけ。chat_cooldown_s 秒に 1 回まで)

安全のため:
  * ゲームの窓が手前にあるときだけ動く
  * チャット欄の見本 (chat_input) を登録してあれば、Enter の後にチャット欄が開いたことを確かめてから打つ
    (開いていないのに文字を打つと、ゲームのショートカットキーとして効いてしまう)
  * チャットは chat_cooldown_s 秒 (既定 120 秒) に 1 回まで。何度も勧誘されても連投しない
"""
from __future__ import annotations

import time

DEFAULT_RESPONSES = {
    "party_invite": [
        {"click": "party_invite_cancel", "fallback_key": "esc"},
        {"close_windows": "esc"},
        {"chat": "Sorry, I no need PT."},
        {"chat": "Perdon, estoy cazando solo en este spot. No hago party, gracias!"},
    ],
}
KEEP_WINDOWS = ("hunting_log", "helper_panel")  # MU Helper の表示。閉じると Helper の判定ができなくなる
CHAT_MAX = 70


def chat_text_ok(text: str) -> str | None:
    """チャットに打てない文字があれば理由を返す。"""
    if not text:
        return "空"
    if any(ord(c) > 126 or (ord(c) < 32) for c in text):
        return "ASCII 以外の文字 (á ñ ¿ や日本語) は打てません"
    if len(text) > CHAT_MAX:
        return f"長すぎます ({len(text)} 文字。{CHAT_MAX} 文字まで)"
    return None


class Responder:
    def __init__(self, run, grab, detector, move=None, to_screen=None, is_active=None, keep=KEEP_WINDOWS,
                 chat_cooldown_s: float = 120.0, chat_check: str = "chat_input", max_esc: int = 5,
                 log=print, sleep=time.sleep, clock=time.monotonic):
        self.run = run                  # (script) → ESP32 の HID マクロを実行 (k:esc;w:100 …)
        self.grab = grab                # () → 今の等倍の画面
        self.detector = detector        # ObjectDetector (load し直されても使えるよう、関数でも可)
        self.move = move                # (スクリーン x, y) → "a:hx,hy" (Device.s_move)
        self.to_screen = to_screen or (lambda x, y: (x, y))
        self.is_active = is_active or (lambda: True)
        self.keep = set(keep)
        self.chat_cooldown_s = chat_cooldown_s
        self.chat_check = chat_check
        self.max_esc = max_esc
        self.log = log
        self.sleep = sleep
        self.clock = clock
        self._last_chat = -1e9
        self.history: list[str] = []

    def _det(self):
        return self.detector() if callable(self.detector) else self.detector

    def _detect(self):
        return self._det().detect(self.grab())

    def _has(self, name: str) -> bool:
        return any(o.name == name for o in self._det().objects)

    def _click(self, x: float, y: float) -> None:
        sx, sy = self.to_screen(x, y)  # 画面の中の位置 → スクリーン座標
        mv = self.move(sx, sy)
        self.run(f"{mv};w:40;c:L" if mv else "w:40;c:L")

    def _do(self, msg: str) -> None:
        self.history.append(msg)
        self.log(f"[respond] {msg}")

    def respond(self, name: str, steps: list[dict], det) -> list[str]:
        """見つけた窓 det (Detection) に対して手順を順に行う。行ったことの一覧を返す。"""
        done: list[str] = []
        if not self.is_active():
            self._do(f"{name}: ゲームの窓が手前に無いので何もしません")
            return done
        for step in steps:
            if "click" in step:
                target = step["click"]
                pos = None
                if isinstance(target, str):
                    if self._has(target):
                        for d in self._detect():
                            if d.name == target and d.present:
                                x, y, w, h = d.box
                                pos = (x + w / 2, y + h / 2)
                    else:
                        self._do(f"{target} が未登録なので {step.get('fallback_key', 'esc')} で閉じます"
                                 f" (python tools\\edge_objects.py add {target} --kind icon 画面.png で登録)")
                else:
                    fx, fy = target
                    x, y, w, h = det.box
                    pos = (x + w * float(fx), y + h * float(fy))
                if pos is not None and self.move is not None:
                    self._click(*pos)
                    self._do(f"{name}: {target} をクリック ({pos[0]:.0f}, {pos[1]:.0f})")
                    done.append("click")
                else:
                    key = step.get("fallback_key", "esc")
                    self.run(f"k:{key}")
                    self._do(f"{name}: {key} で閉じます")
                    done.append(f"key:{key}")
                self.sleep(0.4)
            elif "close_windows" in step:
                key = step["close_windows"] or "esc"
                pressed = 0
                for _ in range(2):  # 押した後にもう一度数え直す
                    opened = [d.name for d in self._detect() if d.present and d.kind == "window" and d.name not in self.keep]
                    n = min(len(opened), self.max_esc - pressed)
                    if n <= 0:
                        break
                    self.run(";".join([f"k:{key};w:150"] * n))
                    pressed += n
                    self._do(f"開いている窓 {', '.join(opened)} → {key} を {n} 回")
                    self.sleep(0.4)
                done.append(f"close:{pressed}")
            elif "chat" in step:
                text = str(step["chat"])
                bad = chat_text_ok(text)
                if bad:
                    self._do(f"チャット「{text}」を送りません: {bad}")
                    continue
                now = self.clock()
                if now - self._last_chat < self.chat_cooldown_s and "chat" not in " ".join(done):
                    self._do(f"チャットは {self.chat_cooldown_s:.0f} 秒に 1 回までなので送りません")
                    break  # 残りのチャットも送らない
                self.run("k:enter;w:300")
                if self._has(self.chat_check):
                    self.sleep(0.2)
                    if not any(d.name == self.chat_check and d.present for d in self._detect()):
                        self._do("Enter を押してもチャット欄が開かないので、文字は打ちません")
                        self.run("ra")
                        break
                self.run(f"t:{text.replace(';', chr(92) + ';')};w:150;k:enter;w:300")
                self._last_chat = now
                self._do(f"チャット: {text}")
                done.append("chat")
        self.run("ra")
        return done
