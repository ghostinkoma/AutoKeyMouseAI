"""Web ページのモンスター一覧を取り込み、マップビューアの狩場登録で選べるようにする。

    python tools\\import_monsters.py http://munou2014.web.fc2.com/mon_lv.html
    python tools\\import_monsters.py --file mon_lv.html          (保存した HTML から)
    python tools\\import_monsters.py URL --dump                  (表の中身を表示するだけ。取り込めないときの確認用)

ページの表から「モンスター名」「レベル」「マップ (出現場所)」の列を見出しの文字で自動判定する。
日本語のマップ名はゲーム内の英語名 (Atlans など) にも変換して保存する。
"""
import argparse
import sqlite3
from collections import Counter
from pathlib import Path

import _path  # noqa: F401

from akm.config import PC_DIR, load_config
from akm.monsters import extract_monsters, fetch_html, parse_tables, save_monsters


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?")
    ap.add_argument("--file", help="保存した HTML ファイル")
    ap.add_argument("--dump", action="store_true", help="表の見出しと最初の数行を表示する")
    args = ap.parse_args()
    if not args.url and not args.file:
        ap.error("URL か --file を指定してください")

    if args.file:
        raw = Path(args.file).read_bytes()
        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError:
            html = raw.decode("cp932", errors="replace")
    else:
        html = fetch_html(args.url)
    tables = parse_tables(html)
    print(f"[monsters] 表 {len(tables)} 個")

    if args.dump:
        for i, t in enumerate(tables):
            print(f"\n--- 表 {i}  見出し: {t['heading']!r}  {len(t['rows'])} 行")
            for r in t["rows"][:6]:
                print("   ", r)
        return

    mons = extract_monsters(tables)
    if not mons:
        raise SystemExit("[monsters] モンスターの表を見つけられませんでした。--dump の結果を送ってください")
    cfg = load_config("config.yaml")
    db_path = PC_DIR / (cfg.get("maplog") or {}).get("db", "data/mu_map.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(db_path))
    n = save_monsters(db, mons, args.url or args.file)
    db.close()
    by_map = Counter(m["map_en"] or m["map"] or "(マップ不明)" for m in mons)
    print(f"[monsters] {n} 件を {db_path} に保存しました")
    for mp, c in by_map.most_common():
        print(f"    {mp}: {c}")
    print("[monsters] 例:", ", ".join(f"{m['name']}(Lv{m['level']}, {m['map']})" for m in mons[:5]))


if __name__ == "__main__":
    main()
