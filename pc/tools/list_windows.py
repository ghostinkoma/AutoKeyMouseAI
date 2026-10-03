"""開いているウィンドウのタイトル・プロセス名・大きさを一覧表示する。

config.yaml の game.window_title / game.process を決めるのに使う。

    python tools/list_windows.py
"""
import _path  # noqa: F401

from akm.screen import IS_WINDOWS, list_windows


def main() -> None:
    if not IS_WINDOWS:
        raise SystemExit("Windows で実行してください")
    rows = sorted(list_windows(), key=lambda r: -(r[3].width * r[3].height))
    print(f"{'process':<24} {'size':>11}  {'position':>12}  title")
    for _, title, proc, r in rows:
        if r.width < 100 or r.height < 100:
            continue
        print(f"{proc:<24} {r.width:>5}x{r.height:<5}  {r.left:>5},{r.top:<6}  {title}")
    print("\nMU の行の process を config.yaml の game.process に書くと、同じ文字を含む別のウィンドウと取り違えない")


if __name__ == "__main__":
    main()
