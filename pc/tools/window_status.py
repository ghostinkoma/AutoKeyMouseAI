"""これまでに確認できたゲームの窓 (インベントリ・キャラクター…) が何種類あるかを表示する。

    python tools\\window_status.py           これまでの記録を表示 (すぐ終わる。学習中に実行してよい)
    python tools\\window_status.py --scan    先に新しい画面を調べてから表示 (discover_windows.py と同じ。少し時間がかかる)

読むもの:
  models/windows_seen.json   窓の発見 (discover_windows.py / 自動学習の毎回) で見つけた窓の記録
  akm/data/objects/objects.yaml  窓の認識に登録済みの窓 (ESP32 の TFT に WIN として出るもの)
タイトルが読めない窓は unknown_幅x高さ@位置 でまとめている (同じ窓が別の大きさで数えられることがある)。
何の窓か教えてもらえれば、タイトルの一覧 (akm/edge/windows.py の WINDOW_TITLES) に足します。
"""
import argparse
import subprocess
import sys

import _path  # noqa: F401

from akm.config import PC_DIR
from akm.edge.objects import load_objects
from akm.edge.windows import WINDOW_TITLES, load_history

HISTORY = PC_DIR / "models" / "windows_seen.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="先に新しい画面を調べる")
    ap.add_argument("--min-count", type=int, default=3, help="この枚数以上で見た窓を「確認できた」とする")
    args = ap.parse_args()
    if args.scan:
        subprocess.run([sys.executable, str(PC_DIR / "tools" / "discover_windows.py")], cwd=str(PC_DIR))
        print()
    hist = load_history(HISTORY)
    types = hist["types"]
    registered = {o.name for o in load_objects() if o.kind == "window"}
    if not types and not registered:
        print("[windows] まだ記録がありません。python tools\\window_status.py --scan で調べるか、"
              "learn_loop の学習 (毎回窓の発見をする) を待ってください")
        return
    named = {k: e for k, e in types.items() if e.get("name")}
    unknown = {k: e for k, e in types.items() if not e.get("name")}
    sure = [k for k, e in named.items() if e["count"] >= args.min_count]
    print(f"[windows] 調べた画面 {len(hist['frames'])} 枚")
    print(f"[windows] 確認できた窓: {len(sure)} 種類 (タイトルが一覧と一致し {args.min_count} 枚以上で見たもの)")
    print(f"           タイトル一致 {len(named)} 種類 / タイトル不明 {len(unknown)} 種類 / 一覧 {len(WINDOW_TITLES)} 種類"
          f" / 窓の認識に登録済み {len(registered)} 種類")

    def show(title, items):
        if not items:
            return
        print(f"\n  {title}")
        for k, e in sorted(items.items(), key=lambda kv: -kv[1]["count"]):
            size = e.get("size")
            sz = f"{size[0]}x{size[1]} @ ({size[2]},{size[3]})" if size else "?"
            mark = "登録済み" if k in registered else ("登録できる" if e.get("name") and e["count"] >= args.min_count else "")
            print(f"    {k:28s} {e['count']:5d} 枚  {sz:22s} 最後 {e['last']}  タイトル {e['titles'][:2]}  {mark}")

    show("タイトルが一致した窓", named)
    show("タイトルが読めない窓 (何の窓か教えてください。例の画像: dataset\\window_check\\)", unknown)
    extra = sorted(registered - set(types))
    if extra:
        print(f"\n  登録済みで、まだ発見の記録にない窓: {', '.join(extra)}")
    missing = [k for k in WINDOW_TITLES if k not in named and k not in registered]
    print(f"\n  一覧のうちまだ見ていない窓 ({len(missing)} 種類): {', '.join(missing)}")
    if any(e.get("name") and e["count"] >= args.min_count and k not in registered for k, e in named.items()):
        print("\n[windows] 「登録できる」窓は python tools\\discover_windows.py --register (自動学習中なら次の回) で登録します")


if __name__ == "__main__":
    sys.exit(main())
