"""ゲームが仮想 HID のマウスを受け付けるかを、カーソルの動かし方・クリックの長さを変えて調べる。

キャラクター (画面の真ん中) の左右を交互にクリックし、画面右下の座標が変わったか (歩いたか) を見る。
仲介アプリの「マウス診断」と tools/vhid_game_mouse_check.py から使う。
"""
from __future__ import annotations

import time

from .pointer import Pointer

TRIALS = [
    ("abs 速いクリック (前の版)", "abs", 30, 10),
    ("abs ゆっくり", "abs", 100, 80),
    ("absrel 絶対 + 相対 1px", "absrel", 100, 80),
    ("rel 相対移動だけ", "rel", 100, 80),
]


def run_mouse_check(run, grab, read_loc, client_rect, screen_to_hid, cursor, log, alive=None,
                    dist: float = 0.15, wait_s: float = 1.5) -> list[tuple[str, str, bool]]:
    """run(script): マクロを送る / grab(): ゲーム画面 / read_loc(img): 座標 (x, y を持つもの) か None
    client_rect(): ゲームの表示範囲 (left, top, width, height を持つもの。画面の画素)
    screen_to_hid(sx, sy) / cursor(): Pointer と同じ。alive(): 続けてよいか (前面か)
    戻り値: [(名前, 方式, 歩いたか)]"""

    def check():
        if alive is not None and not alive():
            raise InterruptedError("focus lost")

    def read_xy():
        for _ in range(6):
            check()
            loc = read_loc(grab())
            if loc is not None:
                return int(loc.x), int(loc.y)
            time.sleep(0.3)
        return None

    start = read_xy()
    log("INFO", f"[マウス診断] 今の座標: {start if start else '読めません'}")
    if start is None:
        raise RuntimeError("画面右下の座標を読めません (config.yaml の maplog を確認してください)")
    results = []
    side = 1
    for label, mode, settle, hold in TRIALS:
        r = client_rect()
        tx, ty = r.left + r.width / 2 + side * dist * r.height, r.top + r.height / 2
        ptr = Pointer(run, screen_to_hid, cursor=cursor, mode=mode, settle_ms=settle, hold_ms=hold)
        before = read_xy()
        check()
        ptr.click(tx, ty)
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            check()
            time.sleep(0.05)
        after = read_xy()
        moved = before is not None and after is not None and after != before
        results.append((label, mode, moved))
        log("INFO" if moved else "WARN", f"[マウス診断] [{'歩いた' if moved else '動かない'}] {label}: "
                                         f"カーソルのずれ {ptr.last_error}px  座標 {before} → {after}")
        if moved:
            side = -side  # 次は反対側へ (元の辺りに戻る)
    ok = [r for r in results if r[2]]
    log("INFO", "[マウス診断] 結果: " + (f"歩いた方式 = {', '.join(r[0] for r in ok)}。「マウス」で {ok[0][1]} を選んでください"
                                       if ok else "どの方式でも歩きませんでした (ゲームがプログラムの入力を受け付けていない可能性)"))
    return results
