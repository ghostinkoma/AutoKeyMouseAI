"""マウスのカーソルを画面の位置へ動かしてクリックする (マクロ書式で送る。仮想 HID でも ESP32 でもよい)。

ゲームによって、カーソルの動きの受け取り方が違う:
  abs    : 絶対座標で一度に動かす (普通のアプリはこれで動く)
  absrel : 絶対座標で動かしたあと、1 ピクセル行って戻る相対移動を足す
           (DirectInput などで「動いた量」だけを見るゲームに、動いたことを伝える)
  rel    : 相対移動だけで、実際のカーソル位置を読みながら近づける (どのモニタでも動く。少し遅い)
クリックは「動かす → settle_ms 待つ → 押す → hold_ms 押したまま → 離す」。押して離すのが速すぎると、
1 コマに 1 回しか入力を見ないゲームは取りこぼす。
"""
from __future__ import annotations

import time

MODES = ("abs", "absrel", "rel")


class Pointer:
    def __init__(self, run, screen_to_hid, cursor=None, mode: str = "abs", settle_ms: int = 100,
                 hold_ms: int = 80, log=None):
        """run(script): マクロを送る / screen_to_hid(sx, sy): 画面の画素 → HID の絶対座標 (0..32767)
        cursor(): 実際のカーソル位置 (画面の画素)。rel に要る。あればずれの確認にも使う"""
        if mode not in MODES:
            raise ValueError(f"mouse mode: {mode} ({' / '.join(MODES)})")
        if mode == "rel" and cursor is None:
            raise ValueError("rel needs cursor()")
        self.run, self.screen_to_hid, self.cursor = run, screen_to_hid, cursor
        self.mode, self.settle_ms, self.hold_ms = mode, int(settle_ms), int(hold_ms)
        self.log = log
        self.moves = 0
        self.misses = 0      # 狙いから 3 ピクセルより離れた回数
        self.last_error = 0  # 最後に動かしたときのずれ (画素)

    def move(self, sx: float, sy: float) -> None:
        sx, sy = int(round(sx)), int(round(sy))
        if self.mode == "rel":
            for _ in range(10):
                cx, cy = self.cursor()
                dx, dy = sx - cx, sy - cy
                if abs(dx) <= 1 and abs(dy) <= 1:
                    break
                self.run(f"m:{dx},{dy}")
                time.sleep(0.02)  # Windows がカーソルを動かし終えるのを待つ
        else:
            hx, hy = self.screen_to_hid(sx, sy)
            self.run(f"a:{hx},{hy}" + (";w:20;m:1,0;w:20;m:-1,0" if self.mode == "absrel" else ""))
        self.moves += 1
        if self.cursor is not None:
            time.sleep(0.01)
            cx, cy = self.cursor()
            self.last_error = max(abs(cx - sx), abs(cy - sy))
            if self.last_error > 3:
                self.misses += 1
                if self.log and (self.misses <= 3 or self.misses % 20 == 0):
                    self.log("WARN", f"カーソルが狙いの位置に来ていません: 狙い ({sx}, {sy}) → 実際 ({cx}, {cy}) "
                                     f"[{self.mode}] ({self.misses}/{self.moves} 回)")

    def click(self, sx: float, sy: float, button: str = "L") -> None:
        self.move(sx, sy)
        self.run(f"w:{self.settle_ms};bd:{button};w:{self.hold_ms};bu:{button}")
