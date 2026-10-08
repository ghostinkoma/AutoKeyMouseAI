"""モンスター名の読み取りを、貯めた等倍の画面 (dataset/frames) で調べる。どこで失敗しているかを数える。

    python tools\\check_names.py               新しい画面 300 枚で調べる
    python tools\\check_names.py --n 1000      枚数を変える
    python tools\\check_names.py 画面.png ...  指定した画面で調べる

1 枚ごとに:
  1. 画面上部の赤い HP バーが見つかるか          (見つからない = 攻撃していない画面か、バーの探し方が合っていない)
  2. 名前の枠を自分の文字認識で読んで、マップガイドと一致するか
  3. 一致しなければ、先生役の OCR (Windows) で読んで一致するか
現在地 (マップ) は画面右下を読む。読めなければ全マップから照らし合わせる。
名前の枠の切り抜きを dataset/name_check/ に保存する (ファイル名に読みと結果)。うまくいかない例を見せてもらえれば直します。
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import _path  # noqa: F401

import cv2

from akm.config import PC_DIR, load_config
from akm.edge.ocr import OcrReader
from akm.edge.target import find_bar, read_target
from akm.maploc import parse_location, preprocess
from akm.spawn import SpawnRegistry
from akm.vision import roi_px


def guide_registry(db_path: Path) -> SpawnRegistry:
    """マップガイドだけを読み込んだ照合器 (DB には書かない)。"""
    mem = sqlite3.connect(":memory:")
    reg = SpawnRegistry(mem)
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = src.execute("SELECT map, monster, level FROM guide_monsters").fetchall()
    except sqlite3.Error:
        rows = []
    src.close()
    mem.executemany("INSERT INTO guide_monsters(map, monster, level) VALUES (?,?,?)", rows)
    reg.reload()
    return reg


def safe(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s)[:30] or "empty"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="*")
    ap.add_argument("--n", type=int, default=300, help="調べる画面の枚数 (新しい順)")
    ap.add_argument("--no-teacher", action="store_true", help="先生役の OCR を使わない")
    args = ap.parse_args()
    cfg = load_config("config.yaml") if (PC_DIR / "config.yaml").exists() else {}
    lcfg = cfg.get("maplog") or {}
    reg = guide_registry(PC_DIR / lcfg.get("db", "data/mu_map.db"))
    if not reg.guide:
        raise SystemExit("マップガイドがありません (python tools\\import_map_guide.py --file guide.html)")
    ocr = OcrReader.load()
    teacher = None
    if not args.no_teacher:
        try:
            from akm.maploc import make_ocr

            teacher = make_ocr(lcfg.get("ocr", "auto"), lcfg.get("tesseract_cmd"))
        except Exception as e:
            print(f"[check] 先生役の OCR が使えません ({str(e).splitlines()[0]})")
    roi = lcfg.get("roi", [0.86, 0.955, 0.14, 0.045])
    if args.images:
        files = [Path(f) for f in args.images]
    else:
        files = sorted((PC_DIR / "dataset" / "frames").glob("*.png"), key=lambda f: f.stat().st_mtime, reverse=True)[:args.n]
    if not files:
        raise SystemExit("画面がありません (learn_loop / mirror_only --learn が dataset/frames に貯めます)")
    out = PC_DIR / "dataset" / "name_check"
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*.png"):
        f.unlink()
    n = {"frames": 0, "bar": 0, "own": 0, "teacher": 0, "none": 0, "loc": 0}
    n_strategy: dict[int, int] = {}
    for f in files:
        img = cv2.imread(str(f))
        if img is None:
            continue
        n["frames"] += 1
        x0, y0, x1, y1 = roi_px(img.shape, roi)
        from akm.maploc import read_location_own

        loc = read_location_own(ocr, img[y0:y1, x0:x1], [m for m, _, _ in reg.guide] or None)[0]
        if loc is not None:
            n["loc"] += 1
        if find_bar(img) is None:
            continue
        n["bar"] += 1
        t = read_target(img, ocr)
        lvl = int(t.level) if t.level.isdigit() else None
        mp = loc.map if loc else None
        best = None  # (Match, 読み, 切り出し方, 先生か)
        for raw, conf, box, k in t.alts:
            m = reg.match(raw, mp, lvl) if raw else None
            if m is not None and (best is None or m.ratio > best[0].ratio):
                best = (m, raw, k, False)
        tr = ""
        if best is None and teacher is not None:
            for _, _, box, k in t.alts:
                if k == 2:
                    continue
                x, y, w, h = box
                try:
                    tr = " ".join(teacher(preprocess(img[y:y + h, x:x + w], 3, "bright", 130)).split())
                except Exception:
                    tr = ""
                m = reg.match(tr, mp, lvl) if tr else None
                if m is not None:
                    best = (m, tr, k, True)
                    break
        how = None if best is None else "teacher" if best[3] else "own"
        n[how or "none"] += 1
        if best is not None:
            n_strategy[best[2]] = n_strategy.get(best[2], 0) + 1
        # 確認用の画像: 名前の行の周り。赤 = バー、黄 = 中央、色付きの枠 = 切り出し方 (0 緑 1 水色 2 白 3 灰)
        x, y, w, h = t.bar
        y0, y1 = max(0, y - 45), min(img.shape[0], y + h + 12)
        xs = [a[2][0] for a in t.alts] + [x]
        xe = [a[2][0] + a[2][2] for a in t.alts] + [x + w]
        x0, x1 = max(0, min(xs) - 20), min(img.shape[1], max(xe) + 20)
        dbg = img[y0:y1, x0:x1].copy()
        cv2.rectangle(dbg, (x - x0, y - y0), (x + w - x0, y + h - y0), (0, 0, 255), 1)
        colors = [(0, 255, 0), (255, 255, 0), (255, 255, 255), (128, 128, 128)]
        for _, _, (bx, by, bw, bh), k in t.alts:
            cv2.rectangle(dbg, (bx - x0, by - y0 + k), (bx + bw - x0, by + bh - y0 - k), colors[k % 4], 1)
        tag = f"{how or 'NG'}" + (f"_{safe(best[0].monster)}" if best else f"_{safe(t.name)}")
        cv2.imwrite(str(out / f"{f.stem}_{tag}.png"), cv2.resize(dbg, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST))
        reads = " | ".join(f"{a[3]}:{a[0]!r}" for a in t.alts)
        print(f"  {f.name}: {reads} (Lv {t.level or '?'})" + (f" 先生 {tr!r}" if tr else "") + f" @ {mp or '?'} → "
              + (f"{best[0].monster} ({'先生' if best[3] else '自分'} {best[0].ratio:.0%}, 切り出し {best[2]})" if best else "一致なし"))
    b = max(1, n["bar"])
    print(f"\n[check] 画面 {n['frames']} 枚 / 現在地が読めた {n['loc']} 枚 / HP バーが見つかった {n['bar']} 枚")
    print(f"[check] HP バーのあった画面のうち: 自分の文字認識でガイドと一致 {n['own']} ({n['own'] / b:.0%}) / "
          f"先生で一致 {n['teacher']} ({n['teacher'] / b:.0%}) / 一致なし {n['none']} ({n['none'] / b:.0%})")
    names = ["0 全体の中央", "1 赤の中央", "2 固定の枠", "3 左に広い枠"]
    print("[check] 一致した切り出し方: " + ", ".join(f"{names[k]} {v}" for k, v in sorted(n_strategy.items())))
    print(f"[check] 確認用の画像: {out} (赤 = バー、緑/水色/白/灰の枠 = 切り出し方 0/1/2/3)")
    if n["frames"] and n["bar"] < n["frames"] * 0.05:
        print("[check] HP バーがほとんど見つかっていません。攻撃中の画面が少ないか、バーの探し方が合っていません。"
              "攻撃中の画面 (上部に名前と赤いバー) を 1 枚 dataset/screens に入れて見せてください")


if __name__ == "__main__":
    sys.exit(main())
