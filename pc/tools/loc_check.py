"""画面右下の現在地 ("Devias (34, 23)") を、今どこを読んで何と読んでいるか確かめる。

    python tools\\loc_check.py                       今のゲーム画面で読む
    python tools\\loc_check.py --roi 0.84 0.95 0.16 0.05   読む場所を変えて試す (横, 縦, 幅, 高さ の割合)

dataset/screens/loc_check.png に、右下の周り (赤い枠 = 今読んでいる場所) と、読んでいる所の拡大を保存する。
赤い枠が現在地の文字からずれていたら、--roi で合う値を探して config.yaml の maplog.roi に書く。
"""
import argparse

import _path  # noqa: F401

import cv2
import numpy as np

from akm.config import PC_DIR, ask_obs_password, load_config
from akm.maploc import parse_location, preprocess, read_location_own
from akm.screen import GameScreen
from akm.vision import roi_px


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roi", type=float, nargs=4, help="読む場所 (横, 縦, 幅, 高さ の割合)")
    ap.add_argument("image", nargs="?", help="画面の画像 (省略時は今のゲーム画面を撮る)")
    args = ap.parse_args()
    cfg = load_config("config.yaml")
    lcfg = cfg.get("maplog") or {}
    roi = args.roi or lcfg.get("roi", [0.86, 0.955, 0.14, 0.045])
    if args.image:
        img = cv2.imread(args.image)
    else:
        ask_obs_password(cfg, "config.yaml")
        screen = GameScreen.from_config(cfg)
        screen.locate()
        img = screen.grab()
    H, W = img.shape[:2]
    x0, y0, x1, y1 = roi_px(img.shape, roi)
    crop = img[y0:y1, x0:x1]
    print(f"[loc] 画面 {W}x{H}  読む場所 roi={roi} → ピクセル ({x0},{y0})-({x1},{y1})")
    from akm.edge.ocr import OcrReader

    import sqlite3

    db = PC_DIR / lcfg.get("db", "data/mu_map.db")
    maps = []
    if db.exists():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            maps = [r[0] for r in con.execute("SELECT name FROM guide_maps")] + \
                   [r[0] for r in con.execute("SELECT name FROM maps")]
        except sqlite3.Error:
            pass
    loc, raw, conf = read_location_own(OcrReader.load(), crop, maps or None, None)
    print(f"[loc] 自分の文字認識: 読み {raw!r} → {loc} (確信度 {conf:.2f})")
    try:
        from akm.maploc import make_ocr

        t = make_ocr(lcfg.get("ocr", "auto"), lcfg.get("tesseract_cmd"))(preprocess(crop, 3, "bright", 130)).strip()
        print(f"[loc] 先生役の OCR: 読み {t!r} → {parse_location(t, maps or None)}")
    except Exception as e:
        print(f"[loc] 先生役の OCR が使えません ({str(e).splitlines()[0]})")
    # 確認用の画像: 右下の周り + 赤い枠、その下に読んでいる所の拡大
    ax0, ay0 = max(0, x0 - 250), max(0, y0 - 220)
    around = img[ay0:H, ax0:W].copy()
    cv2.rectangle(around, (x0 - ax0, y0 - ay0), (x1 - ax0, y1 - ay0), (0, 0, 255), 2)
    big = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
    canvas = np.zeros((around.shape[0] + big.shape[0] + 10, max(around.shape[1], big.shape[1]), 3), np.uint8)
    canvas[:around.shape[0], :around.shape[1]] = around
    canvas[around.shape[0] + 10:, :big.shape[1]] = big
    out = PC_DIR / "dataset" / "screens" / "loc_check.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), canvas)
    print(f"[loc] 確認用の画像: {out} (上 = 右下の周りと赤い枠、下 = 読んでいる所の拡大)")


if __name__ == "__main__":
    main()
