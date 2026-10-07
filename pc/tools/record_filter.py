"""DB に登録する前の「正しい読みか」を判定する AI (akm/record_filter.py) を PC で学習・確認する。

    python tools\\record_filter.py train           合成データ + ビューアで承認 / 却下した記録で学習し、
                                                   models/record_filter.npz に保存する
    python tools\\record_filter.py eval            いまのモデルを確かめる (合成の評価データ・人の判定との一致)
    python tools\\record_filter.py check "Budge Drag0n" --conf 0.6 --level 6
                                                   1 件の読みを判定してみる (--loc で現在地として)
    python tools\\record_filter.py pending         保留中の目撃を一覧する (承認 / 却下はビューアの「目撃一覧」で)

人の判定が増えるほど (ビューアで承認 / 却下するほど) 学習し直したときに賢くなる。
人の判定は合成データより重く (--human-weight 倍) 扱う。
"""
import sys

import _path  # noqa: F401

import numpy as np

from akm.config import PC_DIR, load_config
from akm.edge.harvest import lexicon_fix, load_lexicon
from akm.maploc import KNOWN_MAPS, parse_location
from akm.record_filter import (MODEL, Candidate, FilterModel, RecordFilter, reviewed_dataset, synth_dataset,
                               train_model)


def db_path():
    cfg = load_config("config.yaml") if (PC_DIR / "config.yaml").exists() else {}
    return PC_DIR / ((cfg.get("maplog") or {}).get("db", "data/mu_map.db"))


def report(name, model, X, y, ok=0.8, ng=0.3) -> float:
    if len(y) == 0:
        return float("nan")
    p = model.prob(X)
    acc = float(((p >= 0.5) == y).mean())
    auto_ok = p >= ok
    prec = float(y[auto_ok].mean()) if auto_ok.any() else float("nan")
    rej = p < ng
    print(f"  {name}: {len(y)} 件 正解率 {acc:.1%}  自動登録 {auto_ok.mean():.0%} (うち正しい {prec:.1%})  "
          f"除外 {rej.mean():.0%} (うち本当は正しい {float(y[rej].mean()) if rej.any() else 0:.1%})  "
          f"保留 {1 - auto_ok.mean() - rej.mean():.0%}")
    return acc


def cmd_train(args) -> None:
    words = load_lexicon()
    X, y = synth_dataset(args.n, words, seed=args.seed)
    Xt, yt = synth_dataset(args.n // 5, words, seed=args.seed + 1)
    Xh, yh = reviewed_dataset(db_path())
    w = np.ones(len(y), np.float32)
    if len(yh):
        X = np.concatenate([X, Xh])
        y = np.concatenate([y, yh])
        w = np.concatenate([w, np.full(len(yh), args.human_weight, np.float32)])
    print(f"[train] 合成 {args.n} 件 (正しい読み {y[:args.n].mean():.0%}) + 人の判定 {len(yh)} 件 で学習します")
    model = train_model(X, y, w, hidden=args.hidden, epochs=args.epochs)
    acc = report("合成の評価データ", model, Xt, yt)
    if len(yh):
        report("人の判定 (学習に使ったもの)", model, Xh, yh)
    model.save(MODEL)
    print(f"[train] 保存: {MODEL}")
    print(f"[result] record_filter {acc:.4f} {len(y)} {len(yh)}")


def cmd_eval(args) -> None:
    model = FilterModel.load(MODEL)
    words = load_lexicon()
    Xt, yt = synth_dataset(args.n, words, seed=12345)
    report("合成の評価データ", model, Xt, yt)
    Xh, yh = reviewed_dataset(db_path())
    report("人の判定", model, Xh, yh) if len(yh) else print("  人の判定: まだありません (ビューアの「目撃一覧」で承認 / 却下)")


def cmd_check(args) -> None:
    f = RecordFilter.load()
    if args.loc:
        loc = parse_location(args.text)
        text = f"{loc.map} ({loc.x}, {loc.y})" if loc else ""
        c = Candidate("location", args.text, text, conf=args.conf, in_lex=bool(loc and loc.map in KNOWN_MAPS),
                      repeat=args.repeat, map_known=bool(loc and loc.map in KNOWN_MAPS), coord_ok=loc is not None,
                      jump=args.jump, teacher=args.teacher)
    else:
        text, in_lex = lexicon_fix(args.text, load_lexicon())
        c = Candidate("monster", args.text, text, conf=args.conf, in_lex=in_lex, repeat=args.repeat,
                      level=args.level, hp_ok=True)
    status, s = f.judge(c)
    print(f"  読み {args.text!r} → {text!r}  確率 {s:.2f}  → {status}"
          + ("" if f.model else "  (モデルが無いので規則で判定。train で作ってください)"))


def cmd_pending(args) -> None:
    import sqlite3

    p = db_path()
    con = sqlite3.connect(str(p))
    try:
        rows = con.execute("SELECT id, monster, raw, level, map, x, y, reads, score FROM sightings "
                           "WHERE status='pending' AND reviewed=0 ORDER BY ts DESC LIMIT 50").fetchall()
    except sqlite3.OperationalError:
        rows = []
    for r in rows:
        print(f"  #{r[0]} {r[1]} (読み {r[2]!r}) Lv{r[3] or '?'} @ {r[4] or '?'} ({r[5]}, {r[6]})  {r[7]} 回  確率 {r[8]:.2f}")
    print(f"  保留 {len(rows)} 件 ({p})")


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train")
    t.add_argument("--n", type=int, default=30000, help="合成データの件数")
    t.add_argument("--hidden", type=int, default=24)
    t.add_argument("--epochs", type=int, default=400)
    t.add_argument("--human-weight", type=float, default=5.0, help="人の判定 1 件の重み")
    t.add_argument("--seed", type=int, default=0)
    e = sub.add_parser("eval")
    e.add_argument("--n", type=int, default=6000)
    c = sub.add_parser("check")
    c.add_argument("text")
    c.add_argument("--loc", action="store_true", help="現在地の読みとして判定")
    c.add_argument("--conf", type=float, default=0.8, help="文字認識の確信度")
    c.add_argument("--repeat", type=float, default=0.6, help="続けて同じに読めた割合")
    c.add_argument("--level", default="")
    c.add_argument("--jump", type=float, default=1.0)
    c.add_argument("--teacher", type=float, default=None, help="先生役の OCR と一致 1 / 不一致 0")
    sub.add_parser("pending")
    args = ap.parse_args()
    {"train": cmd_train, "eval": cmd_eval, "check": cmd_check, "pending": cmd_pending}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
