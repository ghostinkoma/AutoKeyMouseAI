"""チャットに 1 行送れるかだけを確かめる (勧誘を待たずに試せる)。

    python tools\\chat_test.py "test"              5 秒後に Enter → 文字 → Enter を送る (その間にゲームを手前に)
    python tools\\chat_test.py "test" --activate   ゲームを自動で手前に出してから送る (learn_loop と同じやり方)
    python tools\\chat_test.py "test" --open-wait 0.6 --enter-wait 1.0   待ち時間を変えて試す

自分で手前にしたときは送れて --activate では送れないなら、手前に出し方が原因。
どちらでも送れなければ、待ち時間かキーの送り方が原因。
"""
import argparse
import time

import _path  # noqa: F401

from akm.config import abs_map_from, load_config
from akm.device import open_device
from akm.screen import GameScreen, bring_to_front, foreground_window


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--activate", action="store_true", help="ゲームを自動で手前に出す")
    ap.add_argument("--open-wait", type=float, default=0.3, help="Enter で開いてから打つまでの秒数")
    ap.add_argument("--enter-wait", type=float, default=None, help="打ち終わってから送信の Enter までの秒数")
    args = ap.parse_args()
    cfg = load_config("config.yaml")
    screen = GameScreen.from_config(cfg)
    screen.locate()
    dev = open_device(cfg, abs_map_from(cfg))
    prev = None
    if args.activate:
        print("[chat] 3 秒後にゲームを手前に出して送ります (ほかのウィンドウを手前にしておいてください)")
        time.sleep(3)
        prev = foreground_window()
        print(f"[chat] 手前に出す: {'できた' if screen.activate() else 'できなかった'}")
        time.sleep(0.3)
    else:
        print("[chat] 5 秒後に送ります。今のうちにゲームをクリックして手前にしてください")
        time.sleep(5)
    print(f"[chat] ゲームが手前: {screen.is_active()}  入力の状態: {screen.input_state()}")
    text = args.text.replace(";", "\;")
    wait = args.enter_wait if args.enter_wait is not None else 0.3 + 0.03 * len(args.text)
    print("[chat] Enter (開く)")
    print("   →", dev.run("k:enter"))
    time.sleep(args.open_wait)
    print(f"[chat] 文字: {args.text}")
    print("   →", dev.run(f"t:{text}"))
    time.sleep(wait)
    print("[chat] Enter (送信)")
    print("   →", dev.run("w:100;k:enter"))
    if prev:
        time.sleep(0.5)
        bring_to_front(prev)
    print("[chat] 終わり。ゲームのチャットに出たか確かめてください")


if __name__ == "__main__":
    main()
