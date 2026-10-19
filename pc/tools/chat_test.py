"""チャットに 1 行送れるかだけを確かめる (勧誘を待たずに試せる)。

    python tools\\chat_test.py "test"              5 秒後に Enter → 文字 → Enter を送る (その間にゲームを手前に)
    python tools\\chat_test.py "test" --activate   ゲームを自動で手前に出してから送る (learn_loop と同じやり方)
    python tools\\chat_test.py "test" --open-wait 0.6                Enter で開いてから確かめるまでの秒数を変える
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
    ap.add_argument("--hold", type=int, default=80, help="送信の Enter を押したままにするミリ秒")
    ap.add_argument("--enters", type=int, default=1, help="送信の Enter の回数")
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
        ok = False
        for k in range(3):  # Windows がすぐ戻してしまうことがあるので数回試す
            screen.activate()
            time.sleep(0.3)
            if screen.is_active() and screen.input_state().get("focus"):
                ok = True
                break
        print(f"[chat] 手前に出す: {'できた' if ok else 'できなかった'} ({k + 1} 回目)")
        time.sleep(0.6)  # 手前に出た直後の Enter はゲームが取りこぼすことがある
    else:
        print("[chat] 5 秒後に送ります。今のうちにゲームをクリックして手前にしてください")
        time.sleep(5)
    st = screen.input_state()
    focus = screen.is_active() and st.get("focus") not in (0, None)
    print(f"[chat] ゲームが手前: {screen.is_active()}  入力先: {st.get('focus_class') or 'なし'}  → フォーカス {'あり' if focus else 'なし'}")
    if not focus:
        raise SystemExit("[chat] ゲームにフォーカスが無いので送りません (本番もこの場合は送らずに止まります)")
    # 本番 (learn_loop) と同じ手順で送る:
    #   入力欄にフォーカスがある (入力先 Edit + カーソル | が点滅) → そのまま打つ
    #   欄は出ているがフォーカスが無い / 欄が閉じている           → Enter を押してから打つ
    from akm.edge.objects import ObjectDetector
    from akm.edge.respond import Responder

    r = Responder(lambda sc: print(f"   ESP32 ← {sc}  → {dev.run(sc)}"), screen.grab, ObjectDetector.load(),
                  grab_chat=screen.grab_desktop, input_state=screen.input_state, is_active=screen.is_active,
                  log=print, sleep=time.sleep, chat_open_wait=args.open_wait,
                  enter_hold_ms=args.hold, send_enters=args.enters)
    print(f"[chat] 今のチャット欄: {r.chat_state() or '分からない (chat_input を --desktop で登録すると分かります)'}"
          "  (focused = 入力欄にフォーカス / open = 出ているがフォーカスなし / closed = 閉じている)")
    done = r._respond("chat_test", [{"chat": args.text}], None)
    print("[chat] " + ("送りました" if done == ["chat"] else "送りませんでした (上の理由を見てください)"))
    if prev:
        time.sleep(0.5)
        bring_to_front(prev)
    print("[chat] 終わり。ゲームのチャットに出たか確かめてください")


if __name__ == "__main__":
    main()
