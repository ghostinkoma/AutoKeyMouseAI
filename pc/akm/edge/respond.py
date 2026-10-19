"""めったに出ない窓への「応じ方」を手順として実行する (判定 → 行動の最初の一歩)。

手順 (config.yaml の rare_windows.respond.<窓の名前> に書く。無ければ DEFAULT_RESPONSES):
  - click: "@cancel"             見つけた窓の中の「cancel」の場所をクリック
                                 (場所は python tools\\edge_objects.py point party_invite cancel で 1 回クリックして覚える)
    click: party_invite_cancel   登録した物体 (Cancel ボタンなど) の真ん中をクリック
    click: [0.75, 0.9]           見つけた窓の枠の中の位置 (横, 縦 の割合) をクリック
                                 (ボタンが見つからないときは fallback_key を押す。無ければ以降を行わない)
  - close_windows: esc           開いている窓の数だけ esc を押す (keep に書いた窓は数えない)
                                 窓が無いのに esc を押すとゲームのメニューが開くので、見えている数しか押さない
  - require_helper: on           MU Helper が動作中 (■) のときだけ行う。今 ON か、within_s 秒 (既定 120) 以内に
                                 ON だったなら OK (勧誘の窓が出ると Helper が止まることがあるので)。分からなければ行わない
  - require_text: [party, join]  窓の中の文字を読み、どれかの語が無ければ以降を行わない
                                 (タイトルが同じ別の窓を取り違えて、断ったりチャットしたりしないように)
  - chat: "I NO need PT" Enter → 文字 → Enter でチャットに送る (ASCII だけ。chat_cooldown_s 秒に 1 回まで)

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
        # MU Helper で狩りをしているとき (■) だけ断る。勧誘の窓が出ると Helper が止まることがあるので
        # 「今 ON」か「窓が出る前 within_s 秒以内に ON だった」なら行う
        {"require_helper": "on", "within_s": 120},
        # タイトルは "Information" で、ほかの知らせ (トレードの申し込みなど) と同じ。本文の文字で確かめる
        {"require_text": ["party", "join"]},  # "requests" はトレードの申し込みにもあるので使わない
        {"click": "@cancel"},  # 勧誘の窓は Esc では消えないので、押せなければ以降 (Esc・チャット) は行わない
        {"close_windows": "esc"},
        {"chat": "I NO need PT"},
        {"chat": "Perdon, cazo solo aqui. No hago party, gracias!"},  # 47 文字
    ],
}
KEEP_WINDOWS = ("hunting_log", "helper_panel")  # MU Helper の表示。閉じると Helper の判定ができなくなる
CHAT_MAX = 50  # MU のチャット欄はおよそ 51 文字で入らなくなる (実測)


def text_lines(crop, min_h: int = 6, max_h: int = 40):
    """窓の中を文字の行ごとに切り分ける (明るい文字の行)。[(y0, y1), ...]"""
    import cv2
    import numpy as np

    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = hsv[:, :, 2] > 130
    rows = mask.sum(axis=1) >= 2
    out, y = [], 0
    while y < len(rows):
        if rows[y]:
            y0 = y
            while y < len(rows) and rows[y]:
                y += 1
            if min_h <= y - y0 <= max_h:
                out.append((max(0, y0 - 2), min(len(rows), y + 2)))
        y += 1
    return out


def read_dialog(crop, own=None, teacher=None) -> str:
    """窓の中の文字を読む。行ごとに、自分の文字認識 (ゲームの字体で学習済み) と先生役の OCR (前処理 2 通り) で読む。"""
    from ..maploc import preprocess

    texts = []
    for y0, y1 in text_lines(crop) or [(0, crop.shape[0])]:
        line = crop[y0:y1]
        if own is not None:
            try:
                texts.append(own(line))
            except Exception:
                pass
        if teacher is not None:
            for method in ("bright", "value"):
                try:
                    texts.append(teacher(preprocess(line, 3, method, 130)))
                except Exception:
                    pass
    return " | ".join(" ".join(str(t).split()) for t in texts if t and str(t).strip())


def has_word(text: str, words, cutoff: float = 0.8) -> str | None:
    """text にどれかの語 (読み違いを少し許す) があれば、その語を返す。"""
    from ..spawn import similarity

    low = text.lower()
    toks = [t for t in "".join(c if c.isalnum() else " " for c in low).split() if len(t) >= 3]
    for w in words:
        w = w.lower()
        if w in low or any(similarity(t, w) >= cutoff for t in toks):
            return w
    return None


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
                 log=print, sleep=time.sleep, clock=time.monotonic, read_text=None, helper_state=None,
                 save_dir=None, cursor=None, activate=None, foreground=None, restore=None):
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
        self.read_text = read_text      # (画像) → 読んだ文字 (先生役の OCR)。require_text に使う
        self.cursor = cursor            # () → 実際のマウスの位置 (確認のログ用)
        self.activate = activate        # () → ゲームを手前に出す (裏で動いているときも断れるように)
        self.foreground = foreground    # () → 今手前にあるウィンドウ (終わったら戻す)
        self.restore = restore          # (ウィンドウ) → そのウィンドウを手前に戻す
        self.save_dir = save_dir        # 文字の確認で弾いた窓の切り抜きを残す (読み方を直す材料)
        self.helper_state = helper_state  # () → (今 ON か: True/False/None, 最後に ON を見てからの秒数)

    def _det(self):
        return self.detector() if callable(self.detector) else self.detector

    def _detect(self):
        return self._det().detect(self.grab())

    def _has(self, name: str) -> bool:
        return any(o.name == name for o in self._det().objects)

    def _click(self, x: float, y: float, button: str = "L") -> tuple[float, float]:
        sx, sy = self.to_screen(x, y)  # 画面の中の位置 → スクリーン座標
        mv = self.move(sx, sy)
        # ゲームはクリックが速すぎると取りこぼすので、動かしてから少し待ち、押す・離すを分けて送る
        self.run((f"{mv};" if mv else "") + f"w:120;bd:{button};w:90;bu:{button}")
        return sx, sy

    def _chat_open(self) -> bool:
        return any(d.name == self.chat_check and d.present for d in self._detect())

    def _still(self, name: str) -> bool:
        return any(d.name == name and d.present for d in self._detect())

    def _do(self, msg: str) -> None:
        self.history.append(msg)
        self.log(f"[respond] {msg}")

    def respond(self, name: str, steps: list[dict], det) -> list[str]:
        """見つけた窓 det (Detection) に対して手順を順に行う。行ったことの一覧を返す。
        ゲームが裏にあれば一時的に手前に出し (activate)、終わったら元のウィンドウに戻す。"""
        prev = None
        if not self.is_active():
            if self.activate is None:
                self._do(f"{name}: ゲームの窓が手前に無いので何もしません")
                return []
            prev = self.foreground() if self.foreground is not None else None
            if not self.activate() or not self.is_active():
                self._do(f"{name}: ゲームの窓を手前に出せないので何もしません")
                return []
            self._do(f"{name}: ゲームの窓を一時的に手前に出しました")
            self.sleep(0.3)
        try:
            return self._respond(name, steps, det)
        finally:
            if prev and self.restore is not None:
                self.sleep(0.2)
                self.restore(prev)
                self._do(f"{name}: 元のウィンドウに戻しました")

    def _respond(self, name: str, steps: list[dict], det) -> list[str]:
        done: list[str] = []
        for step in steps:
            if "require_helper" in step:
                want_on = str(step["require_helper"]).lower() in ("on", "true", "1")
                within = float(step.get("within_s", 120))
                st = self.helper_state() if self.helper_state is not None else None
                if st is None:
                    self._do(f"{name}: MU Helper の状態が分からないので何もしません")
                    self.run("ra")
                    return done
                now_on, since = st
                ok = (now_on is True or since <= within) if want_on else (now_on is False and since > within)
                if not ok:
                    state = "ON" if now_on else "OFF" if now_on is False else "?"
                    last = f"最後に ON だったのは {since:.0f} 秒前" if since < 1e8 else "ON を見ていない"
                    self._do(f"{name}: MU Helper が {state} ({last}) なので何もしません"
                             f" (Helper {'ON' if want_on else 'OFF'} のときだけ断る設定)")
                    self.run("ra")
                    return done
                done.append("helper")
            elif "require_text" in step:
                words = [w.lower() for w in step["require_text"]]
                text = ""
                if self.read_text is not None:
                    img = self.grab()
                    x, y, w, h = det.box
                    crop = img[max(0, y):y + h, max(0, x):x + w]
                    try:
                        text = " ".join(str(self.read_text(crop)).split())
                    except Exception as e:
                        text = ""
                        self._do(f"{name}: 文字が読めません ({e})")
                hit = has_word(text, words)
                if hit is None:
                    self._do(f"{name}: 窓の文字に {'/'.join(words)} が無いので何もしません (読み: {text[:120]!r})")
                    if self.read_text is not None and self.save_dir is not None:
                        try:
                            import cv2

                            self.save_dir.mkdir(parents=True, exist_ok=True)
                            cv2.imwrite(str(self.save_dir / time.strftime(f"{name}_%Y%m%d_%H%M%S.png")), crop)
                        except Exception:
                            pass
                    self.run("ra")
                    return done
                self._do(f"{name}: 窓の文字を確認 ({hit!r} … {text[:80]!r})")
                done.append("text")
            elif "click" in step:
                target = step["click"]
                pos = None
                if isinstance(target, str) and target.startswith("@"):
                    obj = next((o for o in self._det().objects if o.name == det.name), None)
                    pt = (getattr(obj, "points", None) or {}).get(target[1:]) if obj is not None else None
                    if pt:
                        x, y, w, h = det.box
                        pos = (x + w * float(pt[0]), y + h * float(pt[1]))
                    else:
                        self._do(f"{det.name} の {target[1:]} の場所が未登録です"
                                 f" (python tools\\edge_objects.py point {det.name} {target[1:]})")
                elif isinstance(target, str):
                    if self._has(target):
                        for d in self._detect():
                            if d.name == target and d.present:
                                x, y, w, h = d.box
                                pos = (x + w / 2, y + h / 2)
                    else:
                        self._do(f"{target} が未登録です"
                                 f" (python tools\\edge_objects.py add {target} --kind icon 画面.png で登録)")
                else:
                    fx, fy = target
                    x, y, w, h = det.box
                    pos = (x + w * float(fx), y + h * float(fy))
                if pos is not None and self.move is not None:
                    ok = False
                    buttons = [b.upper()[0] for b in step.get("buttons", ["L", "R"])]
                    tries = [(b, dx, dy) for b in buttons for dx, dy in [(0, 0), (2, 1)]]
                    for button, dx, dy in tries:  # 左で消えなければ少しずらして、それでも駄目なら右クリック
                        sx, sy = self._click(pos[0] + dx, pos[1] + dy, button)
                        cur = self.cursor() if self.cursor is not None else None
                        self._do(f"{name}: {target} を{'左' if button == 'L' else '右'}クリック (画面 {pos[0]:.0f},{pos[1]:.0f} → スクリーン {sx:.0f},{sy:.0f}"
                                 + (f"、マウスの位置 {cur[0]:.0f},{cur[1]:.0f}" if cur else "") + ")")
                        self.sleep(0.6)
                        if not self._still(det.name):
                            ok = True
                            self._do(f"{name}: 窓が消えました ({'左' if button == 'L' else '右'}クリック)")
                            break
                    if not ok:
                        self._do(f"{name}: クリックしても窓が消えません。以降 (Esc・チャット) は行いません"
                                 " (マウスの位置がずれていないか python tools\\edge_objects.py press で確かめてください)")
                        self.run("ra")
                        return done
                    done.append("click")
                elif step.get("fallback_key"):
                    key = step["fallback_key"]
                    self.run(f"k:{key}")
                    self._do(f"{name}: {key} で閉じます")
                    done.append(f"key:{key}")
                else:
                    self._do(f"{name}: 押す場所が分からないので、以降は行いません")
                    self.run("ra")
                    return done
                self.sleep(0.4)
            elif "close_windows" in step:
                key = step["close_windows"] or "esc"
                pressed = 0
                for _ in range(2):  # 押した後にもう一度数え直す
                    opened = [d.name for d in self._detect() if d.present and d.kind == "window"
                              and d.name not in self.keep and d.name != det.name]  # 断った窓は Esc で消えない
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
                sent_before = done.count("chat")
                if self._has(self.chat_check):
                    # チャット欄が開いているかを画面で確かめる。開いていれば Enter は押さない (押すと閉じてしまう)
                    if not self._chat_open():
                        self.run("k:enter;w:300")
                        self.sleep(0.3)
                        if not self._chat_open():
                            self._do("Enter を押してもチャット欄が開かないので、文字は打ちません")
                            self.run("k:esc;w:100" if sent_before else "ra")
                            break
                elif sent_before:
                    # 送った直後のチャット欄が開いたままか閉じたかは、見本 (chat_input) が無いと分からない。
                    # 分からないまま打つと文字がショートカットキー (A = スキルツリー など) になるので、2 つ目以降は送らない
                    self._do(f"チャット「{text}」は送りません (2 つ目以降は、チャット欄の見本 {self.chat_check} を登録すると送れます:"
                             f" python tools\\edge_objects.py add-screen {self.chat_check} --kind icon)")
                    break
                else:
                    self.run("k:enter;w:300")
                # 打ち終わるのを待ってから、Enter を別に送る (打っている途中の Enter は効かないことがある)
                self.run(f"t:{text.replace(';', chr(92) + ';')}")
                self.sleep(0.3 + 0.03 * len(text))
                self.run("w:100;k:enter;w:400")
                self.sleep(0.8)  # 送った後、チャット欄が落ち着くまで待つ
                self._last_chat = now
                self._do(f"チャット: {text}")
                done.append("chat")
        self.run("ra")
        return done
