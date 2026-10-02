"""ゲーム画面からアイテム名ラベルを切り出してテンプレート画像にする。

1. ゲームで宝石や Zen を地面に落とした (落ちている) 状態にする
2. python tools/grab_templates.py
3. 表示された画面でラベル文字の部分をドラッグして Enter (やり直しは c)
4. 保存名 (例: bless) を入力 → templates/bless.png に保存
   空 Enter で終了 / r で画面を撮り直す

ラベルは文字部分だけをきっちり囲むと誤検出が減る。
"""
from pathlib import Path

import _path  # noqa: F401
import cv2

from akm.config import PC_DIR, load_config
from akm.screen import GameScreen


def main() -> None:
    cfg = load_config("config.yaml")
    screen = GameScreen(cfg["game"]["window_title"])
    out_dir = PC_DIR / "templates"
    out_dir.mkdir(exist_ok=True)
    img = screen.grab()
    while True:
        roi = cv2.selectROI("select label (Enter=OK, c=cancel)", img, showCrosshair=False)
        cv2.destroyAllWindows()
        if roi[2] == 0 or roi[3] == 0:
            name = input("選択なし。r=撮り直し / Enter=終了: ").strip()
            if name == "r":
                img = screen.grab()
                continue
            break
        x, y, w, h = roi
        name = input("保存名 (例: bless, zen) / 空で破棄: ").strip()
        if not name:
            continue
        path = out_dir / f"{name}.png"
        if path.exists():
            i = 2
            while (out_dir / f"{name}_{i}.png").exists():
                i += 1
            path = out_dir / f"{name}_{i}.png"
        cv2.imwrite(str(path), img[y : y + h, x : x + w])
        print(f"保存: {path.relative_to(PC_DIR)}  (config.yaml の vision.templates.targets に登録)")


if __name__ == "__main__":
    main()
