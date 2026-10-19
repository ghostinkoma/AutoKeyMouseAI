"""ゲームを手前に出さずに (フォーカスが無くても) 操作できるか試す。Windows のメッセージを直接送る。

    python tools\\bg_test.py info                 ゲームの中の部品 (チャット欄の Edit など) を表示
    python tools\\bg_test.py chat "test bg"       チャットに送る: 入力欄 (Edit) に文字を入れて Enter
    python tools\\bg_test.py enter                ゲームの窓に Enter だけ送る (チャット欄が開くか見る)
    python tools\\bg_test.py cancel               勧誘の窓の Cancel をクリックしたことにする (勧誘が出ている間に)

どれも 3 秒後に送ります。その間に別のウィンドウ (PowerShell など) を手前にして、ゲームは裏にしておいてください。
結果 (ゲームのチャットに出たか、勧誘の窓が消えたか) を教えてください。効いたものを本番で使います。
"""
import argparse
import time

import _path  # noqa: F401

from akm import bginput as bg
from akm.config import load_config
from akm.screen import GameScreen, foreground_window


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["info", "chat", "enter", "cancel"])
    ap.add_argument("text", nargs="?", default="test bg")
    args = ap.parse_args()
    cfg = load_config("config.yaml")
    screen = GameScreen.from_config(cfg)
    screen.locate()
    hwnd = screen.hwnd
    kids = bg.child_windows(hwnd)
    print(f"[bg] ゲームの部品: " + (", ".join(f"{c}{'' if v else '(隠れ)'}" for _, c, v in kids) or "なし"))
    if args.what == "info":
        return
    print("[bg] 3 秒後に送ります。別のウィンドウを手前にしておいてください")
    time.sleep(3)
    print(f"[bg] 手前のウィンドウがゲームか: {foreground_window() == hwnd} (False なら本当の裏での操作)")
    if args.what == "chat":
        edit = bg.find_edit(hwnd)
        if edit is None:
            print("[bg] チャットの入力欄 (Edit) が見つからないので、ゲームの窓に Enter → 文字 → Enter を送ります")
            bg.post_key(hwnd, bg.VK_RETURN)
            time.sleep(0.4)
            bg.type_text(hwnd, args.text)
            time.sleep(0.2)
            bg.post_key(hwnd, bg.VK_RETURN)
        else:
            print(f"[bg] 入力欄 (Edit) に文字を入れます: {args.text}")
            bg.type_text(edit, args.text)
            time.sleep(0.2)
            print(f"[bg] 入力欄の中身: {bg.get_text(edit)!r}")
            if bg.get_text(edit) != args.text:
                bg.set_text(edit, args.text)
                print(f"[bg] 直接書き込み後の中身: {bg.get_text(edit)!r}")
            bg.post_key(edit, bg.VK_RETURN)
            time.sleep(0.3)
            print(f"[bg] Enter 後の中身: {bg.get_text(edit)!r} (空になっていれば送信された可能性が高い)")
    elif args.what == "enter":
        bg.post_key(hwnd, bg.VK_RETURN)
    else:
        from akm.edge.objects import ObjectDetector

        det = ObjectDetector.load()
        img = screen.grab()
        d = next((d for d in det.detect(img) if d.name == "party_invite"), None)
        obj = next((o for o in det.objects if o.name == "party_invite"), None)
        if d is None or not d.present or obj is None or "cancel" not in obj.points:
            raise SystemExit("[bg] 勧誘の窓が見えていないか、Cancel の場所が未登録です")
        x, y, w, h = d.box
        fx, fy = obj.points["cancel"]
        cx, cy = x + w * fx, y + h * fy
        print(f"[bg] Cancel ({cx:.0f}, {cy:.0f}) をクリックしたことにします")
        bg.post_click(hwnd, cx, cy)
        time.sleep(0.8)
        still = any(e.name == "party_invite" and e.present for e in det.detect(screen.grab()))
        print("[bg] 勧誘の窓は " + ("まだ見えます (効かない)" if still else "消えました (裏でも効く)"))
    print("[bg] 終わり")


if __name__ == "__main__":
    main()
