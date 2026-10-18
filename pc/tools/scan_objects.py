"""貯めた画面 (dataset/frames など) を全部、窓・アイコンの認識にかけて「どの画面で何が見えたか」を数える。
リアルタイムで出てこない窓 (パーティの勧誘など) を、これまでの画面で確かめるためのもの。

    python tools\\scan_objects.py                        全部の画面で、登録した窓・アイコンを数える
    python tools\\scan_objects.py party_invite           その窓について、似ている順の上位も表示して切り抜きを保存
    python tools\\scan_objects.py party_invite --n 500   新しい画面 500 枚だけ

似ている順の上位を dataset/object_scan/<名前>/ に保存する (ファイル名に 一致度・ネット・○×)。
○ = 認識が「ある」と判定、× = 「無い」。上位に本物が × で混じっていれば見落とし、偽物が ○ なら誤検出。
"""
import argparse
import heapq
import sys
import time
from collections import Counter
from pathlib import Path

import _path  # noqa: F401

import cv2

from akm.config import PC_DIR
from akm.edge.objects import ObjectDetector

FRAME_DIRS = ["frames", "helper_state", "screens", "ui"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("name", nargs="?", help="詳しく調べる窓・アイコンの名前 (例 party_invite)")
    ap.add_argument("--n", type=int, default=0, help="新しい画面から何枚調べるか (0 = 全部)")
    ap.add_argument("--top", type=int, default=15, help="似ている順に何枚表示・保存するか")
    args = ap.parse_args()
    det = ObjectDetector.load()
    names = [o.name for o in det.objects]
    if not names:
        raise SystemExit("登録された窓・アイコンがありません (python tools\\edge_objects.py add ...)")
    if args.name and args.name not in names:
        raise SystemExit(f"{args.name} は登録されていません。登録済み: {', '.join(names)}")
    if det.model is None:
        print("[scan] 学習済みモデルがありません (一致度だけで判定します。python tools\\edge_objects.py train)")
    files = []
    for d in FRAME_DIRS:
        files += list((PC_DIR / "dataset" / d).rglob("*.png"))
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    if args.n:
        files = files[:args.n]
    if not files:
        raise SystemExit("画面がありません (learn_loop が dataset/frames に貯めます)")

    present = Counter()
    sizes = Counter()
    first_last: dict[str, list] = {}
    ranked = []  # 似ている上位だけ (点数, 通し番号, 一致度, ネット, ある, ファイル, box, 切り抜き)
    # 切り抜きは見つけたときに取っておく (学習中は古い画面が消されるので、後で読み直すと無いことがある)
    t0 = time.time()
    for i, f in enumerate(files, 1):
        img = cv2.imread(str(f)) if f.exists() else None
        if img is None or img.shape[0] < 300:
            continue
        sizes[f"{img.shape[1]}x{img.shape[0]}"] += 1
        for r in det.detect(img):
            if r.present:
                present[r.name] += 1
                fl = first_last.setdefault(r.name, [f, f])
                fl[1] = f  # files は新しい順なので、最後に来たものが一番古い
            if r.name == args.name:
                x, y, w, h = r.box
                m = 30
                crop = img[max(0, y - m):y + h + m, max(0, x - m):x + w + m].copy()
                cv2.rectangle(crop, (min(m, x), min(m, y)), (min(m, x) + w, min(m, y) + h),
                              (0, 255, 0) if r.present else (0, 0, 255), 2)
                item = (r.ncc + max(0.0, r.nn), i, r.ncc, r.nn, r.present, f, r.box, crop)
                if len(ranked) < args.top:
                    heapq.heappush(ranked, item)
                else:
                    heapq.heappushpop(ranked, item)
        if i % 100 == 0:
            print(f"[scan] {i}/{len(files)} 枚 ({time.time() - t0:.0f} 秒)", flush=True)

    n = sum(sizes.values())
    print(f"\n[scan] 調べた画面 {n} 枚 ({time.time() - t0:.0f} 秒)。画面の大きさ: "
          + ", ".join(f"{k} {v} 枚" for k, v in sizes.most_common()))
    print("[scan] 「ある」と判定した画面の数:")
    for nm in names:
        fl = first_last.get(nm)
        when = (f"  (新しい {fl[0].name} … 古い {fl[1].name})" if fl else "")
        print(f"    {nm:16s} {present[nm]:5d} 枚{when}")
    if not args.name:
        print("[scan] 名前を付けると、その窓の似ている順の上位を表示して切り抜きを保存します"
              " (例 python tools\\scan_objects.py party_invite)")
        return
    out = PC_DIR / "dataset" / "object_scan" / args.name
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.png"):
        old.unlink()
    ranked.sort(key=lambda r: -r[0])
    print(f"\n[scan] {args.name}: 似ている順の上位 {len(ranked)} 枚 (○ = ある、× = 無い)")
    for k, (_, _, ncc, nn, ok, f, (x, y, w, h), crop) in enumerate(ranked, 1):
        print(f"    {k:2d}. {'○' if ok else '×'} 一致度 {ncc:.2f}  ネット {nn:.2f}  {f.parent.name}/{f.name}  ({x},{y},{w},{h})")
        cv2.imwrite(str(out / f"{k:02d}_{'o' if ok else 'x'}_{ncc:.2f}_{max(0.0, nn):.2f}_{f.stem}.png"), crop)
    hits = present[args.name]
    print(f"\n[scan] {args.name} を「ある」と判定した画面: {hits} 枚 / {n} 枚")
    best = max((r[2] for r in ranked), default=0.0)
    if hits == 0:
        print(f"[scan] 一番似ていた画面でも一致度 {best:.2f}。"
              + ("貯めた画面には写っていないと思われます (誤検出なし)。" if best < 0.6 else
                 "上位の切り抜きに本物が写っていないか見てください (写っていれば見落とし)。"))
    print(f"[scan] 切り抜き: {out}  (緑の枠 = ある、赤の枠 = 無い)")


if __name__ == "__main__":
    sys.exit(main())
