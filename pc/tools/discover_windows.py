"""貯めた画面 (dataset/frames など) から、ゲームの窓が何種類あるかを自動で調べる。

    python tools\\discover_windows.py               窓の種類と、それぞれ何枚の画面で見たかを表示
    python tools\\discover_windows.py --register    タイトルが一覧と一致し 3 枚以上で見た窓を、窓の認識に登録
                                                   (次の python tools\\edge_objects.py train / 自動学習で覚える)

見つけた窓の例 (パネルとタイトルの帯に枠を描いたもの) を dataset/window_check/ に保存する。
タイトルが読めない窓は unknown_幅x高さ@位置 としてまとめて表示する
(何の窓か教えてもらえれば、タイトルの一覧 akm/edge/windows.py の WINDOW_TITLES に足します)。
"""
import argparse
import sys
from pathlib import Path

import _path  # noqa: F401

import cv2

from akm.config import PC_DIR, load_config
from akm.edge.objects import load_objects
from akm.edge.windows import WINDOW_TITLES, discover, register

FRAME_DIRS = ["frames", "screens", "helper_state", "ui"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=400, help="調べる画面の枚数 (新しい順)")
    ap.add_argument("--register", action="store_true", help="一致した窓を窓の認識に登録する")
    ap.add_argument("--min-count", type=int, default=3, help="登録に要る、見た画面の数")
    ap.add_argument("--no-teacher", action="store_true", help="先生役の OCR を使わない")
    args = ap.parse_args()
    files = []
    for d in FRAME_DIRS:
        files += list((PC_DIR / "dataset" / d).rglob("*.png"))
    files = sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)[:args.n]
    if not files:
        raise SystemExit("画面がありません (learn_loop が dataset/frames に貯めます)")
    ocr = teacher = None
    try:
        from akm.edge.ocr import OcrReader

        ocr = OcrReader.load()
    except FileNotFoundError:
        pass
    if not args.no_teacher:
        try:
            from akm.maploc import make_ocr

            cfg = load_config("config.yaml") if (PC_DIR / "config.yaml").exists() else {}
            lcfg = cfg.get("maplog") or {}
            teacher = make_ocr(lcfg.get("ocr", "auto"), lcfg.get("tesseract_cmd"))
        except Exception:
            teacher = None

    def frames():
        for f in files:
            img = cv2.imread(str(f))
            if img is not None and img.shape[0] >= 300:
                yield f.name, img

    types = discover(frames(), ocr, teacher)
    out = PC_DIR / "dataset" / "window_check"
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*.png"):
        f.unlink()
    have = {o.name for o in load_objects()}
    named = [t for t in types if t.name]
    print(f"[windows] 画面 {len(files)} 枚から、窓 {len(types)} 種類 (タイトルが読めたもの {len(named)} 種類 /"
          f" 一覧 {len(WINDOW_TITLES)} 種類)")
    for t in types:
        fname, img, p = t.sample
        mark = "登録済み" if t.name in have else ("登録できる" if t.name and t.count >= args.min_count else "")
        print(f"  {t.key:28s} {t.count:4d} 枚  大きさ {p.w}x{p.h} @ ({p.x},{p.y})  タイトル {t.titles[:2]}  {mark}")
        vis = img.copy()
        cv2.rectangle(vis, (p.x, p.y), (p.x + p.w, p.y + p.h), (0, 255, 0), 2)
        x, y, w, h = p.title_box
        cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 255, 255), 2)
        cv2.imwrite(str(out / f"{t.key}.png"), vis[max(0, p.y - 20):p.y + p.h + 20, max(0, p.x - 20):p.x + p.w + 20])
    print(f"[windows] 例の画像: {out} (緑 = 窓、黄 = タイトルの帯)")
    if args.register:
        done = register(types, args.min_count, have)
        print(f"[windows] {len(done)} 種類を登録しました。" + (" python tools\\edge_objects.py train で覚えます"
                                                         " (自動学習中なら次の回で)" if done else ""))
    elif any(t.name and t.count >= args.min_count and t.name not in have for t in types):
        print("[windows] --register を付けると、「登録できる」窓を窓の認識に登録します")


if __name__ == "__main__":
    sys.exit(main())
