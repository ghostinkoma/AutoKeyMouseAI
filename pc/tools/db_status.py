"""狩場 (出現エリア)・モンスター名の DB (mu_map.db) への登録の進み具合を表示する。

    python tools\\db_status.py                 マップごとの進み具合 (ガイドのモンスターのうち何体を見つけたか)
    python tools\\db_status.py Atlans          そのマップの詳細 (モンスターごとの出現エリア・まだ見ていないモンスター)
    python tools\\db_status.py --log 20        最近の登録ログ (ビューアの「登録ログ」タブと同じ)

mirror_only / learn_loop が動いている間に実行してよい (読むだけ)。
"""
import argparse
import sqlite3
import time
from pathlib import Path

import _path  # noqa: F401

from akm.config import PC_DIR, load_config


def db_path() -> Path:
    cfg = load_config("config.yaml") if (PC_DIR / "config.yaml").exists() else {}
    return PC_DIR / ((cfg.get("maplog") or {}).get("db", "data/mu_map.db"))


def has(con, table) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def bar(frac: float, width: int = 20) -> str:
    n = int(round(frac * width))
    return "#" * n + "." * (width - n)


def ago(ts) -> str:
    if not ts:
        return "-"
    s = time.time() - ts
    return f"{s / 60:.0f} 分前" if s < 3600 else f"{s / 3600:.1f} 時間前" if s < 86400 else f"{s / 86400:.0f} 日前"


def summary(con) -> None:
    def count(sql, *a):
        try:
            return con.execute(sql, a).fetchone()[0] or 0
        except sqlite3.Error:
            return 0

    print("== 全体 ==")
    print(f"  マップガイド      マップ {count('SELECT COUNT(*) FROM guide_maps')}、モンスター {count('SELECT COUNT(*) FROM guide_monsters')}")
    print(f"  出現エリア        {count('SELECT COUNT(*) FROM monster_areas')} 件 (10 マス区画 x モンスター)、"
          f"区画 {count('SELECT COUNT(*) FROM (SELECT DISTINCT map, ax, ay FROM monster_areas)')} 個")
    print(f"  目撃              登録 {count('SELECT COUNT(*) FROM sightings WHERE status=?', 'ok')} / "
          f"保留 {count('SELECT COUNT(*) FROM sightings WHERE status=? AND reviewed=0', 'pending')} / "
          f"除外 {count('SELECT COUNT(*) FROM sightings WHERE status=?', 'ng')}  "
          f"(人が判定 {count('SELECT COUNT(*) FROM sightings WHERE reviewed=1')})")
    print(f"  歩いたマス        {count('SELECT COUNT(*) FROM cells')} マス、位置の記録 {count('SELECT COUNT(*) FROM positions')} 件")
    print(f"  狩場 (登録地点)   {count('SELECT COUNT(*) FROM spots')} 件")
    print(f"  新しいマップ候補  {count('SELECT COUNT(*) FROM map_names WHERE reviewed=0')} 件 (ビューアの「新しいマップ」で承認)")
    last = count("SELECT MAX(ts) FROM spawn_log")
    print(f"  最後の自動登録    {ago(last)}")


def per_map(con) -> None:
    if not has(con, "guide_monsters"):
        print("\nマップガイドが取り込まれていません (python tools\\import_map_guide.py --file guide.html)")
        return
    areas = has(con, "monster_areas")
    print("\n== マップごと: ガイドのモンスターのうち見つけた数 ==")
    print(f"  {'マップ':<18} {'見つけた':>8}  {'':20}  {'区画':>4}  {'歩いたマス':>8}  最後に見た")
    maps = con.execute("SELECT name, min_level FROM guide_maps ORDER BY COALESCE(min_level, 0), name").fetchall()
    for mp, lv in maps:
        total = con.execute("SELECT COUNT(*) FROM guide_monsters WHERE map=?", (mp,)).fetchone()[0]
        found = con.execute("SELECT COUNT(DISTINCT monster) FROM monster_areas WHERE map=? AND monster IN "
                            "(SELECT monster FROM guide_monsters WHERE map=?)", (mp, mp)).fetchone()[0] if areas else 0
        cells = con.execute("SELECT COUNT(*) FROM monster_areas WHERE map=?", (mp,)).fetchone()[0] if areas else 0
        blocks = con.execute("SELECT COUNT(*) FROM (SELECT DISTINCT ax, ay FROM monster_areas WHERE map=?)",
                             (mp,)).fetchone()[0] if areas else 0
        walked = con.execute("SELECT COUNT(*) FROM cells WHERE map=?", (mp,)).fetchone()[0] if has(con, "cells") else 0
        last = con.execute("SELECT MAX(last_seen) FROM monster_areas WHERE map=?", (mp,)).fetchone()[0] if areas else None
        name = mp + (f" ({lv}+)" if lv else "")
        print(f"  {name:<18} {found:>3}/{total:<4}  {bar(found / total if total else 0)}  {blocks:>4}  {walked:>8}  {ago(last)}"
              + ("" if cells or not walked else "  ← 歩いたが未登録"))


def detail(con, mp: str) -> None:
    row = con.execute("SELECT name FROM guide_maps WHERE lower(name)=lower(?)", (mp,)).fetchone()
    mp = row[0] if row else mp
    print(f"\n== {mp} ==")
    guide = con.execute("SELECT monster, level FROM guide_monsters WHERE map=? ORDER BY level", (mp,)).fetchall()
    for name, lv in guide:
        areas = con.execute("SELECT ax, ay, reads, last_seen FROM monster_areas WHERE map=? AND monster=? "
                            "ORDER BY reads DESC", (mp, name)).fetchall() if has(con, "monster_areas") else []
        if areas:
            where = ", ".join(f"({ax}-{ax + 9},{ay}-{ay + 9}) {n}回" for ax, ay, n, _ in areas[:6])
            print(f"  ○ {name:<22} Lv{lv:<4} 区画 {len(areas)}: {where}" + (" …" if len(areas) > 6 else ""))
        else:
            print(f"  × {name:<22} Lv{lv:<4} まだ見ていない")
    extra = con.execute("SELECT DISTINCT monster FROM monster_areas WHERE map=? AND monster NOT IN "
                        "(SELECT monster FROM guide_monsters WHERE map=?)", (mp, mp)).fetchall() if has(con, "monster_areas") else []
    if extra:
        print("  ガイドでは別マップのモンスター: " + ", ".join(e[0] for e in extra))


def log(con, n: int) -> None:
    if not has(con, "spawn_log"):
        print("登録ログはまだありません")
        return
    print(f"\n== 最近の自動登録 {n} 件 ==")
    for ts, kind, mp, ax, ay, mon, lv, ratio, raw in con.execute(
            "SELECT ts, kind, map, ax, ay, monster, level, ratio, raw FROM spawn_log ORDER BY id DESC LIMIT ?", (n,)):
        k = "出現エリア" if kind == "area" else "確定" if kind == "confirm" else kind
        print(f"  {time.strftime('%m/%d %H:%M:%S', time.localtime(ts))} [{k}] {mon} Lv{lv or '?'} @ {mp} "
              f"({ax}-{ax + 9}, {ay}-{ay + 9}) 一致 {ratio:.0%}" + (f" (読み {raw})" if raw and raw != mon else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("map", nargs="?", help="このマップの詳細を表示")
    ap.add_argument("--log", type=int, default=0, help="最近の登録ログを何件表示するか")
    args = ap.parse_args()
    p = db_path()
    if not p.exists():
        raise SystemExit(f"DB がありません: {p}")
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)  # 読むだけ (記録中でもよい)
    try:
        print(f"DB: {p}")
        if args.map:
            detail(con, args.map)
        else:
            summary(con)
            per_map(con)
        if args.log:
            log(con, args.log)
    finally:
        con.close()


if __name__ == "__main__":
    main()
