"""ESP32 で動かす文字認識 (エッジ AI) を学習する。

    python tools\\edge_train.py train            学習して models/edge_ocr.npz と
                                                firmware/AutoKeyMouse/edge_ocr_model.h (ESP32 用) を書き出す
    python tools\\edge_train.py eval             実画面の評価用の行 (labels.tsv の test) で正解率を出す
    python tools\\edge_train.py read 画像.png     画像 (1 行の切り抜き) を読む
    python tools\\edge_train.py label 画面.png    画面から文字の行を囲んで正解を入力し、実画面の見本を増やす

学習データ:
  1. 合成: 実際のゲーム画面を背景に、MU に近い字体で文字を描いたもの (大きさ・色・縁取りをいろいろに)
  2. 実画面: pc/akm/data/ocr_lines/ (同梱) と dataset/ocr_lines/ (label で追加) の行の切り抜きと正解。
     行を拡大縮小して切り出し、今のモデルで「どの切り出しがどの文字か」を対応づけて見本にする。
     実画面の見本を増やすほど MU の字体に合っていく。
"""
import random
import sys
import time
from pathlib import Path

import _path  # noqa: F401

import cv2
import numpy as np

from akm.config import PC_DIR
from akm.edge.align import align
from akm.edge.glyphs import line_glyphs
from akm.edge.mlp import train
from akm.edge.ocr import MODEL, OcrReader
from akm.edge.synth import CHARS, JUNK, font_files, make_samples

LINE_DIRS = [PC_DIR / "akm" / "data" / "ocr_lines", PC_DIR / "dataset" / "ocr_lines"]
HEADER = PC_DIR.parent / "firmware" / "AutoKeyMouse" / "edge_ocr_model.h"
LABELS = list(CHARS) + ["?"]


def real_lines(split: str | None = None) -> list[tuple[np.ndarray, str]]:
    out = []
    for d in LINE_DIRS:
        tsv = d / "labels.tsv"
        if not tsv.exists():
            continue
        for row in tsv.read_text(encoding="utf-8").splitlines():
            if not row.strip() or row.startswith("#"):
                continue
            name, sp, text = row.split("\t", 2)
            if split and sp != split:
                continue
            img = cv2.imread(str(d / name))
            if img is not None and all(c in CHARS for c in text.replace(" ", "")):
                out.append((img, text))
    return out


def backgrounds() -> list[np.ndarray]:
    """合成の背景: 集めたゲーム画面 (dataset/ 以下の png)。無ければ同梱の行の画像を並べて使う。"""
    files = list((PC_DIR / "dataset").rglob("*.png"))
    random.Random(0).shuffle(files)
    bgs = [b for b in (cv2.imread(str(f)) for f in files[:40]) if b is not None and b.shape[0] >= 200]
    if not bgs:
        tiles = [img for img, _ in real_lines()]
        bgs = [cv2.resize(t, (800, 400)) for t in tiles]
    return bgs


def edit_distance(a: str, b: str) -> int:
    d = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, d[0] = d[:], i
        for j, cb in enumerate(b, 1):
            d[j] = min(prev[j] + 1, d[j - 1] + 1, prev[j - 1] + (ca != cb))
    return d[-1]


def evaluate(reader: OcrReader, lines, quiet: bool = False) -> float:
    err = tot = 0
    for img, text in lines:
        got, _ = reader.read(img)
        want = text.replace(" ", "")
        err += edit_distance(got.replace(" ", ""), want)
        tot += len(want)
        if not quiet:
            print(f"  {text!r:40s} → {got!r}")
    return 1 - err / max(1, tot)


def real_samples(model, lines, rounds: int = 60, seed: int = 0):
    rng = random.Random(seed)
    X, F, Y = [], [], []
    for line, text in lines:
        target = [CHARS.index(c) for c in text.replace(" ", "")]
        for r in range(rounds):
            s = 1.0 if r == 0 else rng.uniform(0.6, 2.6)  # いろいろな画面の大きさ
            im = cv2.resize(line, None, fx=s, fy=s, interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
            if rng.random() < 0.5:
                _, enc = cv2.imencode(".jpg", im, [cv2.IMWRITE_JPEG_QUALITY, rng.randint(55, 95)])
                im = cv2.imdecode(enc, cv2.IMREAD_COLOR)
            gl, _ = line_glyphs(im)
            if not gl:
                continue
            bits = np.array([g.bits.ravel() for g in gl])
            ft = np.array([g.feats for g in gl])
            lg = model.forward(bits, ft)[2]
            lg = lg - lg.max(1, keepdims=True)
            logp = lg - np.log(np.exp(lg).sum(1, keepdims=True))
            for gi, tj in align(logp, target):
                X.append(bits[gi])
                F.append(ft[gi])
                Y.append(target[tj])
    return np.array(X, np.uint8), np.array(F, np.float32), np.array(Y, np.int64)


def cmd_train(args) -> None:
    from concurrent.futures import ProcessPoolExecutor

    t0 = time.time()
    fonts = font_files(PC_DIR / "dataset" / "fonts")
    bgs = backgrounds()
    n = args.lines // 2500 + 1
    print(f"[train] 合成データを作っています ({n * 2500} 行, 字体 {len(fonts)} 種, 背景 {len(bgs)} 枚)...")
    with ProcessPoolExecutor() as ex:
        parts = list(ex.map(make_samples, [2500] * n, [bgs] * n, [fonts] * n, range(n)))
    X = np.concatenate([p[0] for p in parts])
    F = np.concatenate([p[1] for p in parts])
    Y = np.concatenate([p[2] for p in parts])
    print(f"[train] 合成の見本 {len(Y)} 文字 ({time.time() - t0:.0f} 秒)")
    log = (lambda s: None) if not args.verbose else print
    model = train(X, F, Y, LABELS, hidden=args.hidden, epochs=args.epochs, log=log)
    tr = real_lines("train")
    if tr:
        RX, RF, RY = real_samples(model, tr)
        rep = max(1, len(Y) // (4 * max(1, len(RY))))
        print(f"[train] 実画面の見本 {len(RY)} 文字 (行 {len(tr)}) を加えて学習し直します")
        model = train(np.concatenate([X] + [RX] * rep), np.concatenate([F] + [RF] * rep),
                      np.concatenate([Y] + [RY] * rep), LABELS, hidden=args.hidden, epochs=args.epochs, log=log)
    q = model.quantize(X[:5000], F[:5000])
    te = real_lines("test")
    if args.keep_better and te and MODEL.exists():
        # 評価用の実画面で前のモデルより悪くなったら採用しない (学習を繰り返しても悪くならないように)
        old = evaluate(OcrReader.load(), te, quiet=True)
        new = evaluate(OcrReader(q), te, quiet=True)
        if new < old - 0.005:
            print(f"[train] 新しいモデル {new:.1%} が前 {old:.1%} より悪いので採用しません")
            print(f"[result] ocr kept {old:.4f} {new:.4f}")
            return
        print(f"[train] 採用: 実画面の評価 {old:.1%} → {new:.1%}")
        print(f"[result] ocr adopted {old:.4f} {new:.4f}")
    MODEL.parent.mkdir(parents=True, exist_ok=True)
    q.save(MODEL)
    q.export_c(HEADER, note=f"合成 {len(Y)} 文字 + 実画面 {len(tr)} 行, 隠れ層 {args.hidden}")
    kb = (q.w1q.size + q.w2q.size + 4 * (q.w1f.size + q.b1.size + q.b2.size)) / 1024
    print(f"[train] 保存: {MODEL} と {HEADER} (ESP32 のフラッシュに約 {kb:.0f}KB)  {time.time() - t0:.0f} 秒")
    te = real_lines("test")
    if te:
        print(f"[eval] 実画面の評価用の行 (学習に使っていない) の文字正解率 {evaluate(OcrReader(q), te):.1%}")


def cmd_eval(args) -> None:
    """文字認識の正解率を、正解付きの実画面の行 (学習に使っていない test、--all で全部) で調べる。"""
    from akm.edge.harvest import lexicon_fix, load_lexicon
    from akm.maploc import parse_location

    reader = OcrReader.load()
    lines = real_lines(None if args.all else "test")
    if not lines:
        raise SystemExit("正解付きの行がありません (dataset/ocr_lines は learn_loop で自動で貯まります)")
    words = load_lexicon()
    try:  # マップガイドのモンスター名も辞書に (記録のときと同じ)
        import sqlite3

        from akm.config import load_config

        cfg = load_config("config.yaml") if (PC_DIR / "config.yaml").exists() else {}
        db = PC_DIR / (cfg.get("maplog") or {}).get("db", "data/mu_map.db")
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        words = sorted(set(words) | {r[0] for r in con.execute("SELECT monster FROM guide_monsters")})
        con.close()
    except Exception:
        pass
    stats = {}
    bad = []
    for img, text in lines:
        got, conf = reader.read(img)
        kind = "現在地" if parse_location(text) else "名前など"
        want = text.replace(" ", "")
        e = edit_distance(got.replace(" ", ""), want)
        loc = parse_location(got)
        fixed = (f"{loc.map} ({loc.x}, {loc.y})" if loc else got) if kind == "現在地" else lexicon_fix(got, words)[0]
        for k in (kind, "全体"):
            st = stats.setdefault(k, [0, 0, 0, 0, 0])  # 行数, 文字の誤り, 文字数, 行がそのまま正解, 辞書で直して正解
            st[0] += 1
            st[1] += e
            st[2] += len(want)
            st[3] += got.replace(" ", "") == want
            st[4] += " ".join(fixed.split()) == " ".join(text.split())
        if e:
            bad.append((e / max(1, len(want)), text, got))
    print(f"[eval] 調べた行: {len(lines)} 行 ({'全部' if args.all else '学習に使っていない test の行'})")
    print(f"  {'':8s} {'行数':>5s}  {'文字の正解率':>10s}  {'行がそのまま正解':>12s}  {'辞書で直して正解':>12s}")
    for k in ("全体", "現在地", "名前など"):
        if k in stats:
            n, err, tot, exact, fixed = stats[k]
            print(f"  {k:8s} {n:5d}  {1 - err / max(1, tot):10.1%}  {exact / n:14.1%}  {fixed / n:14.1%}")
    if bad:
        print("  読み違いの多い行:")
        for r, text, got in sorted(bad, reverse=True)[:args.show]:
            print(f"    {text!r:32s} → {got!r}  (文字の誤り {r:.0%})")
    log = PC_DIR / "models" / "learn_log.txt"
    if log.exists():
        hist = [l for l in log.read_text(encoding="utf-8").splitlines() if "文字認識" in l][-5:]
        if hist:
            print("  最近の文字認識の学習 (models/learn_log.txt):")
            for l in hist:
                print("    " + l)


def cmd_read(args) -> None:
    img = cv2.imread(args.image)
    if img is None:
        raise SystemExit(f"画像が読めません: {args.image}")
    text, conf = OcrReader.load().read(img)
    print(f"{text}  (一番自信のない文字 {conf:.2f})")


def cmd_label(args) -> None:
    img = cv2.imread(args.image)
    if img is None:
        raise SystemExit(f"画像が読めません: {args.image}")
    out = PC_DIR / "dataset" / "ocr_lines"
    out.mkdir(parents=True, exist_ok=True)
    tsv = out / "labels.tsv"
    if not tsv.exists():
        tsv.write_text("# 画像\t用途\t正解の文字列\n", encoding="utf-8")
    reader = None
    try:
        reader = OcrReader.load()
    except FileNotFoundError:
        pass
    print("[label] 文字の行を 1 行ずつマウスで囲んで Enter。終わるときは何も囲まずに Enter (または c)")
    n = len(list(out.glob("*.png")))
    while True:
        x, y, w, h = cv2.selectROI("label: 1 行を囲む", img, showCrosshair=False)
        cv2.destroyAllWindows()
        if w == 0 or h == 0:
            break
        crop = img[y:y + h, x:x + w]
        guess = reader.read(crop)[0] if reader else ""
        text = input(f"[label] 正解を入力 (Enter で '{guess}' のまま、- で捨てる): ").strip() or guess
        if text == "-" or not text:
            continue
        bad = [c for c in text.replace(" ", "") if c not in CHARS]
        if bad:
            print(f"[label] 使えない文字があります: {''.join(bad)} (今は {CHARS} のみ)")
            continue
        n += 1
        name = f"line_{n:04d}.png"
        cv2.imwrite(str(out / name), crop)
        split = "test" if n % 5 == 0 else "train"  # 5 行に 1 行は評価用に取っておく
        with open(tsv, "a", encoding="utf-8") as f:
            f.write(f"{name}\t{split}\t{text}\n")
        print(f"[label] 保存 {name} ({split}): {text}")


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train")
    t.add_argument("--lines", type=int, default=40000, help="合成する行の数")
    t.add_argument("--hidden", type=int, default=160)
    t.add_argument("--epochs", type=int, default=30)
    t.add_argument("-v", "--verbose", action="store_true")
    t.add_argument("--keep-better", action="store_true", help="評価用の実画面で前のモデルより悪ければ保存しない")
    ev = sub.add_parser("eval")
    ev.add_argument("--all", action="store_true", help="学習に使った行も含めて全部で調べる")
    ev.add_argument("--show", type=int, default=10, help="読み違いの多い行を何行表示するか")
    r = sub.add_parser("read")
    r.add_argument("image")
    lb = sub.add_parser("label")
    lb.add_argument("image")
    args = ap.parse_args()
    {"train": cmd_train, "eval": cmd_eval, "read": cmd_read, "label": cmd_label}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
