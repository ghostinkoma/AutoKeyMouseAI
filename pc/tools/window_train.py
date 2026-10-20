"""窓の種類の認識 (ESP32 で動かすエッジ AI) を学習・評価する。文字は読まず、液晶と同じ 240x135 の絵の形だけで見分ける。

    python tools\\window_train.py train          学習 → 学習に使っていない画面で正解率 → models/edge_win.npz と
                                                firmware/AutoKeyMouse/edge_win_model.h (ESP32 用) を書き出す
    python tools\\window_train.py eval           保存したモデルで、全部の見本の正解率 (間違えた画面も表示)
    python tools\\window_train.py test 画面.png  1 枚の画面を判定 (液晶と同じ大きさにした絵も保存)
    python tools\\window_train.py test screen    今のゲーム画面を判定

見本は tools\\window_tour.py で集める (dataset/windows/。auto なら Windows の OCR でタイトルを読んで自動で)。
学習したモデルは models/edge_win.bin。学習ループ (mirror_only) が ESP32 に Wi-Fi で送るので、
ファームウェア (frame-stream-14 以降) を一度書き込めば、学習のたびに書き込み直す必要はない。
"""
import argparse
import json
import sys
import zlib
from collections import Counter
from pathlib import Path

import _path  # noqa: F401

import cv2
import numpy as np

from akm.config import PC_DIR
from akm.edge.mlp import QModel, train
from akm.edge.winfeat import NONE, half_from_image, load_slots, save_slots
from akm.edge.winmodel import (build, detect, export_header, label_names, model_id, pack_model, push_model,
                               save_table_tsv, slots_from_labels, win_log, window_table)
from window_tour import OUT, load_labels

MODEL = PC_DIR / "models" / "edge_win.npz"
SLOTS = PC_DIR / "models" / "edge_win_slots.json"
BLOB = PC_DIR / "models" / "edge_win.bin"           # ESP32 に Wi-Fi で送るモデル
TABLE = PC_DIR / "models" / "window_table.json"     # 窓の一覧 (名前・タイトル・場所・見本の数)
SCORE = PC_DIR / "models" / "edge_win_score.txt"  # 前回の評価用の画面での正解率 (--keep-better で使う)
HEADER = PC_DIR.parent / "firmware" / "AutoKeyMouse" / "edge_win_model.h"


def load_img(e: dict):
    p = OUT / e["file"]
    if not p.exists():
        return None
    return cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)


def is_test(e: dict, frac: float = 0.2) -> bool:
    """学習に使わない画面 (ファイル名で決まるので毎回同じ)。"""
    return (zlib.crc32(e["file"].encode()) % 1000) < frac * 1000


def report(q: QModel, slots, entries, thr: float, show_wrong: int = 10, per_window: dict | None = None) -> float:
    """画面ごとに、見えている窓を全部当て、余計な窓を言わなければ正解。per_window に {名前: (見つけた, 出た)}。"""
    ok = 0
    per = Counter()
    hit = Counter()
    fp = Counter()
    wrong = []
    for e in entries:
        img = load_img(e)
        if img is None:
            continue
        want = {w["name"] for w in e.get("windows", []) if any(w["name"] in s.names for s in slots)}
        got = {n for n, _, _ in detect(q, slots, half_from_image(img), thr)}
        for n in want:
            per[n] += 1
            hit[n] += n in got
        for n in got - want:
            fp[n] += 1
        if got == want:
            ok += 1
        else:
            wrong.append((e["file"], sorted(want), sorted(got)))
    n = sum(1 for e in entries if (OUT / e["file"]).exists())
    acc = ok / max(1, n)
    print(f"  画面ごとの正解率 {ok}/{n} = {acc:.1%}")
    for name in sorted(set(per) | set(fp)):
        print(f"    {name:16s} 見つけた {hit[name]:4d}/{per[name]:<4d}  余計に言った {fp[name]}")
    for f, w, g in wrong[:show_wrong]:
        print(f"    × {f}  正解 {w or ['なし']}  判定 {g or ['なし']}")
    if per_window is not None:
        per_window.update({k: (hit[k], per[k]) for k in per})
    return acc


def cmd_train(args) -> None:
    entries = load_labels()
    if not entries:
        raise SystemExit("見本がありません (python tools\\window_tour.py で集める)")
    slots = slots_from_labels(entries, args.min_count)
    if not slots:
        raise SystemExit("窓の見本がありません (window_tour.py で窓を開いて撮る)")
    labels = label_names(slots)
    if len(labels) > 32:
        raise SystemExit(f"窓の種類が多すぎます ({len(labels) - 1} 種、ESP32 の表は 31 種まで)")
    print(f"[win] スロット {len(slots)} 個 (120x68 の座標):")
    for s in slots:
        print(f"    ({s.x},{s.y}) {s.w}x{s.h}  {', '.join(s.names)}")
    tr = [e for e in entries if not is_test(e)]
    te = [e for e in entries if is_test(e)]
    if not te or args.all:
        print("[win] 評価用の画面を分けずに学習します" + ("" if args.all else " (見本が少ない)"))
        tr = entries
    X, F, Y, _, _ = build(tr, slots, load_img, augment=True, seed=args.seed)
    if not len(Y):
        raise SystemExit("画面が読めません (dataset/windows/ を確かめてください)")
    cnt = Counter(labels[i] for i in Y)
    print(f"[win] 学習の見本 {len(Y)} 個: " + ", ".join(f"{k} {v}" for k, v in cnt.most_common()))
    # 「無い」が多すぎると窓を見逃しやすいので、窓の見本を増やして釣り合わせる
    rng = np.random.default_rng(args.seed)
    none_i = labels.index(NONE)
    pos = np.where(Y != none_i)[0]
    neg = np.where(Y == none_i)[0]
    if len(pos) and len(neg) > 3 * len(pos):
        pos = rng.choice(pos, size=len(neg) // 3, replace=True)
    sel = np.concatenate([pos, neg])
    model = train(X[sel], F[sel], Y[sel], labels, hidden=args.hidden, epochs=args.epochs, seed=args.seed,
                  log=lambda s: None)
    q = model.quantize(X[:3000], F[:3000])
    k, _ = q.predict(X, F)
    print(f"[win] 学習した見本での正解率 {(k == Y).mean():.1%}")
    acc = None
    scores: dict = {}
    if te and not args.all:
        print(f"[win] 学習に使っていない画面 {len(te)} 枚で評価:")
        acc = report(q, slots, te, args.thr, per_window=scores)
        prev = float(SCORE.read_text().split()[0]) if SCORE.exists() else None
        # 窓の種類が増えたときは比べられないので採用する
        same = SLOTS.exists() and [s.names for s in load_slots(SLOTS)] == [s.names for s in slots]
        if args.keep_better and prev is not None and same and acc < prev - 0.02:
            print(f"[result] 窓の種類: 前の方が良いので採用しません (今回 {acc:.1%} < 前回 {prev:.1%})")
            win_log(f"学習: 前の方が良いので採用しません (今回 {acc:.1%} < 前回 {prev:.1%})")
            return
        if acc < 0.9:
            print("[win] 正解率が 90% 未満です。window_tour.py で見本を増やしてから学習し直すのがおすすめ")
        # 評価が済んだら全部で学習し直して書き出す
        X, F, Y, _, _ = build(entries, slots, load_img, augment=True, seed=args.seed)
        pos = np.where(Y != none_i)[0]
        neg = np.where(Y == none_i)[0]
        if len(pos) and len(neg) > 3 * len(pos):
            pos = rng.choice(pos, size=len(neg) // 3, replace=True)
        sel = np.concatenate([pos, neg])
        model = train(X[sel], F[sel], Y[sel], labels, hidden=args.hidden, epochs=args.epochs, seed=args.seed,
                      log=lambda s: None)
        q = model.quantize(X[:3000], F[:3000])
    MODEL.parent.mkdir(parents=True, exist_ok=True)
    q.save(MODEL)
    save_slots(SLOTS, slots)
    table = window_table(entries, slots)
    TABLE.write_text(json.dumps(table, ensure_ascii=False, indent=1), encoding="utf-8")
    save_table_tsv(TABLE.with_suffix(".tsv"), table, scores)
    win_log(f"学習: 窓 {len(labels) - 1} 種 / 見本の画面 {len(entries)} 枚"
            + (f" / 正解率 {acc:.1%} ({len(te)} 枚)" if acc is not None else "")
            + " / " + ", ".join(f"{t['name']} {scores[t['name']][0]}/{scores[t['name']][1]}" if t["name"] in scores
                                else f"{t['name']} (評価なし)" for t in table))
    blob = pack_model(q, slots, args.thr)
    BLOB.write_bytes(blob)
    if args.header:  # 書き込み時の初期モデル (普段は Wi-Fi で送るので書き換えない。git pull がぶつからないように)
        export_header(q, slots, HEADER, thr=args.thr, table=table,
                      note=f"窓 {len(labels) - 1} 種 / スロット {len(slots)} / 見本の画面 {len(entries)} 枚")
        print(f"[win] ファームウェアに組み込むモデルも書き換えました: {HEADER}")
    print("[win] 窓の一覧:")
    for t in table:
        print(f"    {t['name']:16s} {', '.join(t['titles'])[:30]:30s} {t['slot']:16s} 見本 {t['count']}  {t['src']}")
    if args.push:
        push_model(args.push, blob)
    if acc is not None:
        SCORE.write_text(f"{acc:.4f} {len(te)}\n")
    print(f"[win] 保存: {MODEL.name}, {BLOB.name} ({len(blob)} バイト, {model_id(blob)}), {TABLE.name}")
    print(f"[result] 窓の種類 {len(labels) - 1} 種 / 画面 {len(entries)} 枚"
          + (f" / 正解率 {acc:.1%} ({len(te)} 枚)" if acc is not None else ""))
    print("[win] ESP32 へは学習ループ (mirror_only) が Wi-Fi で送ります (--push IP でも送れる)")


def load_model():
    if not MODEL.exists():
        raise SystemExit("モデルがありません (python tools\\window_train.py train)")
    return QModel.load(MODEL), load_slots(SLOTS)


def cmd_eval(args) -> None:
    q, slots = load_model()
    entries = load_labels()
    te = [e for e in entries if is_test(e)]
    print(f"[win] 全部の見本 {len(entries)} 枚 (最後の学習では全部使っているので甘めに出ます):")
    report(q, slots, entries, args.thr, show_wrong=20)
    if te:
        print(f"[win] うち評価用に分けている {len(te)} 枚:")
        report(q, slots, te, args.thr)


def cmd_test(args) -> None:
    q, slots = load_model()
    if args.image == "screen":
        from window_tour import open_screen

        img = open_screen().grab()
    else:
        img = cv2.imdecode(np.fromfile(args.image, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise SystemExit(f"画像が読めません: {args.image}")
    half = half_from_image(img)
    found = detect(q, slots, half, 0.0)
    for n, p, s in sorted(found, key=lambda t: -t[1]):
        print(f"  {'○' if p >= args.thr else '△'} {n:16s} {p:.2f}  スロット ({s.x},{s.y}) {s.w}x{s.h}")
    if not [f for f in found if f[1] >= args.thr]:
        print("  窓は無い")
    big = cv2.resize(cv2.cvtColor(half, cv2.COLOR_GRAY2BGR), None, fx=6, fy=6, interpolation=cv2.INTER_NEAREST)
    for n, p, s in found:
        c = (0, 255, 0) if p >= args.thr else (0, 160, 255)
        cv2.rectangle(big, (s.x * 6, s.y * 6), ((s.x + s.w) * 6, (s.y + s.h) * 6), c, 2)
        cv2.putText(big, f"{n} {p:.2f}", (s.x * 6 + 3, s.y * 6 + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, c, 1)
    out = PC_DIR / "dataset" / "screens" / "window_test.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imencode(".png", big)[1].tofile(str(out))
    print(f"[win] ESP32 が見ている絵 (120x68 を 6 倍) を {out} に保存")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["train", "eval", "test"])
    ap.add_argument("image", nargs="?", default="screen")
    ap.add_argument("--thr", type=float, default=0.6, help="窓があると言う確率")
    ap.add_argument("--hidden", type=int, default=48)
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--min-count", type=int, default=1, help="スロットを作るのに要る窓の見本の数")
    ap.add_argument("--all", action="store_true", help="評価用に分けず全部で学習")
    ap.add_argument("--header", action="store_true",
                    help="firmware/AutoKeyMouse/edge_win_model.h (書き込み時の初期モデル) も書き換える")
    ap.add_argument("--push", default="", help="学習したモデルを ESP32 (IP) に送る")
    ap.add_argument("--keep-better", action="store_true", help="前回より正解率が下がったら保存しない")
    args = ap.parse_args()
    {"train": cmd_train, "eval": cmd_eval, "test": cmd_test}[args.cmd](args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
