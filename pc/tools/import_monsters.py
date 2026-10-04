"""モンスター一覧を mu_map.db に入れる (マップビューアの狩場登録のドロップダウンに出る)。

    python tools\\import_monsters.py                      同梱の一覧 (無[MU]脳2014, 約 380 種) を入れる
    python tools\\import_monsters.py --text paste.txt     ブラウザで表を選択してコピーしたテキストから
    python tools\\import_monsters.py URL                  Web ページから (アイコン画像も取得)
    python tools\\import_monsters.py URL --dump           ページの表の中身を表示するだけ (確認用)

何度実行してもよい (同じモンスターは上書き)。アイコンはページの画像を名前で突き合わせて付ける。
"""
import argparse
import sqlite3
from collections import Counter
from pathlib import Path

import _path  # noqa: F401

from akm.config import PC_DIR, load_config
from akm.monsters import (download_icons, extract_monsters, fetch_html, load_builtin, parse_pasted_text,
                          parse_tables, save_monsters)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?")
    ap.add_argument("--file", help="保存した HTML ファイル")
    ap.add_argument("--text", help="ブラウザからコピーした表のテキスト (UTF-8 か Shift_JIS)")
    ap.add_argument("--dump", action="store_true", help="表の見出しと最初の数行を表示する")
    args = ap.parse_args()

    source = "builtin:munou2014"
    if args.text:
        raw = Path(args.text).read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp932", errors="replace")
        mons = parse_pasted_text(text)
        source = args.text
    elif args.url or args.file:
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
            import re

            out = PC_DIR / "data" / "monster_page.html"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(html, encoding="utf-8")
            low = html.lower()
            print(f"[dump] HTML を保存しました: {out}  ({len(html)} 文字)")
            print(f"[dump] タグの数: table={low.count('<table')} tr={low.count('<tr')} td={low.count('<td')} "
                  f"img={low.count('<img')} frame={low.count('<frame') + low.count('<iframe')}")
            for i, t in enumerate(tables):
                print(f"\n--- 表 {i}  見出し: {t['heading']!r}  {len(t['rows'])} 行")
                for r, im in list(zip(t["rows"], t.get("imgs") or []))[:6]:
                    print("   ", r, im)
            return
        mons = extract_monsters(tables)
        source = args.url or args.file
    else:
        mons = load_builtin()

    if not mons:
        raise SystemExit("[monsters] モンスターを読み取れませんでした")
    cfg = load_config("config.yaml")
    db_path = PC_DIR / (cfg.get("maplog") or {}).get("db", "data/mu_map.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if any(m.get("icon") for m in mons):
        got = download_icons(mons, args.url, db_path.parent / "monster_icons")
        print(f"[monsters] アイコン画像 {got} 枚を取得 ({db_path.parent / 'monster_icons'})")
    db = sqlite3.connect(str(db_path))
    n = save_monsters(db, mons, source)
    total = db.execute("SELECT COUNT(DISTINCT name) FROM monsters").fetchone()[0]
    with_icon = db.execute("SELECT COUNT(DISTINCT name) FROM monsters WHERE icon IS NOT NULL AND icon != ''").fetchone()[0]
    db.close()
    names = {m["name"] for m in mons}
    print(f"[monsters] {len(names)} 種 ({n} 件: マップごと) を {db_path} に保存しました")
    print(f"[monsters] DB 全体: {total} 種 / アイコンあり {with_icon} 種")
    for mp, c in Counter(m["map_en"] or m["map"] or "(マップ不明)" for m in mons).most_common(12):
        print(f"    {mp}: {c}")
    if args.url and len(names) < 300:
        missing = sorted({m["name"] for m in load_builtin()} - names)
        if missing:
            print(f"[monsters] ページから取れなかったモンスター {len(missing)} 種 (同梱の一覧にはある): "
                  + ", ".join(missing[:15]) + (" …" if len(missing) > 15 else ""))


if __name__ == "__main__":
    main()
