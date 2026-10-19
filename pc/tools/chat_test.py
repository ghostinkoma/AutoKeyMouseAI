"""チャットに 1 行送れるかだけを確かめる (勧誘を待たずに試せる)。

    python tools\\chat_test.py "test"              5 秒後に Enter → 文字 → Enter を送る (その間にゲームを手前に)
    python tools\\chat_test.py "test" --activate   ゲームを自動で手前に出してから送る (learn_loop と同じやり方)
    python tools\\chat_test.py "test" --open-wait 0.6 --enter-wait 1.0   待ち時間を変えて試す
    python tools\\chat_test.py "test" --hold 150                送信の Enter を長めに押す (ミリ秒。既定 80)
    python tools\\chat_test.py "test" --enters 2                送信の Enter を 2 回押す

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
    ap.add_argument("--hold", type=int, default=80, help="送信の Enter を押したままにするミリ秒")
    ap.add_argument("--enters", type=int, default=1, help="送信の Enter の回数")
    ap.add_argument("--check", action="store_true",
                    help="チャット欄の見本 (chat_input) で、Enter の前後に欄が開いているか判定して表示する")
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
    st = screen.input_state()
    focus = screen.is_active() and st.get("focus") not in (0, None)
    print(f"[chat] ゲームが手前: {screen.is_active()}  入力先: {st.get('focus_class') or 'なし'}  → フォーカス {'あり' if focus else 'なし'}")
    if not focus:
        raise SystemExit("[chat] ゲームにフォーカスが無いので送りません (本番もこの場合は送らずに止まります)")
    text = args.text.replace(";", "\\;")
    wait = args.enter_wait if args.enter_wait is not None else 0.3 + 0.03 * len(args.text)
    print("[chat] Enter (開く)")
    print("   →", dev.run("k:enter"))
    time.sleep(args.open_wait)
    if checker is not None:
        ncc, diff = checker._chat_score(screen.grab_desktop(), before)
        opened = ncc >= 0.75 or (diff >= 8 and ncc >= 0.45)
        print(f"[chat] Enter 後のチャット欄: 一致度 {ncc:.2f} / Enter 前との違い {diff:.1f} → "
              + ("開いた (入力できる)" if opened else "開いていない"))
        if not opened:
            dev.run("k:enter")  # 開いていない = 閉じてしまった可能性。もう一度押して元に戻す
            raise SystemExit("[chat] 開いたと判定できないので打ちません (Enter をもう一度押して元に戻しました)")
    print(f"[chat] 文字: {args.text}")
    print("   →", dev.run(f"t:{text}"))
    time.sleep(wait)
    for k in range(args.enters):
        if k:
            time.sleep(0.4)
        print(f"[chat] Enter (送信 {k + 1} 回目、{args.hold}ms 押す)")
        print("   →", dev.run(f"w:100;kd:enter;w:{args.hold};ku:enter"))
    if prev:
        time.sleep(0.5)
        bring_to_front(prev)
    print("[chat] 終わり。ゲームのチャットに出たか確かめてください")


if __name__ == "__main__":
    main()
