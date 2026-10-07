"""マップガイド (https://wiki.rexmu.online/map-guide) のマップ / モンスター / レベルを mu_map.db に入れる。

    python tools\\import_map_guide.py                    ページを取得して取り込む
    python tools\\import_map_guide.py --dump             取り込まずに、読み取れた内容を表示するだけ (確認用)
    python tools\\import_map_guide.py --file page.html   ブラウザで「名前を付けて保存」した HTML から
    python tools\\import_map_guide.py --text page.txt    ブラウザで全選択 (Ctrl+A) → コピーして貼った文字から
    python tools\\import_map_guide.py --section lorencia  <section id="map-guide-…lorencia…"> の HTML をそのまま表示
                                                       (読み取りがおかしいとき、これを見せてもらえれば読み方を合わせます)
    python tools\\import_map_guide.py --no-icons         アイコン画像を取得しない

読み取りの順番: <section id="map-guide-マップ名"> (アイコン・名前・レベル) → 表 (<table>) → 文字の並び。
アイコンは DB と同じフォルダの monster_icons\\ に保存し、ビューアのモンスター一覧に付く。

取り込むもの:
  guide_maps / guide_monsters   ガイドのマップとモンスター・レベル (認識した名前との照合に使う)
  monsters                      ビューアのモンスター一覧 (source = guide:rexmu)
  map_names                     画面右下の現在地の読みで既知のマップとして扱う名前
何度実行してもよい (同じものは上書き)。
ページの書き方が想定と違って読み取れないときは --dump の出力を見せてください (読み方を合わせます)。
"""
import argparse
import sqlite3
from collections import Counter
from pathlib import Path

import _path  # noqa: F401

from akm.config import PC_DIR, load_config
from akm.spawn import GUIDE_URL, parse_guide_sections, parse_guide_tables, parse_guide_text, save_guide, section_html


def html_to_text(html: str) -> str:
    """表が無いページ用: タグを外して行ごとの文字にする。"""
    import re
    from html import unescape

    html = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|td|th)>", "\n", html)
    html = re.sub(r"(?i)</t[dh]>", "\t", html)
    return unescape(re.sub(r"<[^>]+>", " ", html))


def read_text_file(path: str) -> str:
    raw = Path(path).read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp932", errors="replace")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", default=GUIDE_URL)
    ap.add_argument("--file", help="保存した HTML ファイル")
    ap.add_argument("--text", help="ページの文字をコピーして保存したテキスト")
    ap.add_argument("--dump", action="store_true", help="取り込まずに内容を表示する")
    ap.add_argument("--section", help="id にこの文字を含む map-guide の section の HTML を表示する (確認用)")
    ap.add_argument("--no-icons", action="store_true", help="アイコン画像を取得しない")
    args = ap.parse_args()

    if args.text:
        rows = parse_guide_text(read_text_file(args.text))
        src = f"text:{Path(args.text).name}"
        html = None
    else:
        if args.file:
            if not Path(args.file).exists():
                raise SystemExit(f"[guide] {args.file} がありません。ブラウザで表示した HTML を保存してから指定してください"
                                 " (F12 → Elements の <html> を右クリック → Copy → Copy outerHTML → メモ帳に貼って UTF-8 で保存)")
            html = read_text_file(args.file)
            src = f"file:{Path(args.file).name}"
        else:
            import requests

            r = requests.get(args.url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (AutoKeyMouse map guide import)"})
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            html = r.text
            src = args.url
        if args.section:
            sec = section_html(html, args.section)
            if sec is None:
                import re

                ids = re.findall(r'<section[^>]*id=["\']([^"\']+)', html)
                print(f"[guide] id に {args.section!r} を含む map-guide の section がありません。section の id: {ids[:40]}")
                # 代わりに、その名前の後にある最初の表の HTML を出す (アイコンの書き方を確かめるため)
                i = html.lower().find(args.section.lower())
                j = html.lower().find("<table", i) if i >= 0 else -1
                if j >= 0:
                    k = html.lower().find("</table>", j)
                    print(f"[guide] 代わりに「{args.section}」の後の表:")
                    print(html[j:(k + 8 if k >= 0 else j + 6000)][:6000])
            else:
                print(sec[:8000] + ("\n… (以下略)" if len(sec) > 8000 else ""))
            return
        rows, how = parse_guide_sections(html), "section"
        if not rows:
            rows, how = parse_guide_tables(html), "表"
        if not rows:  # 表になっていないページ: 文字から読む
            rows, how = parse_guide_text(html_to_text(html)), "文字"
        print(f"[guide] 読み取り方: {how}")
    # 同じマップ・同じモンスターは 1 つに
    uniq = {}
    for r in rows:
        uniq.setdefault((r["map"], r["monster"]), r)
    rows = list(uniq.values())

    per_map = Counter(r["map"] for r in rows)
    print(f"[guide] {src}: マップ {len(per_map)} 個、モンスター {len(rows)} 件 (アイコンあり {sum(1 for r in rows if r.get('icon'))}。"
          "一覧の * がアイコンあり)")
    for mp, n in sorted(per_map.items()):
        mons = [r for r in rows if r["map"] == mp]
        lv = next((r.get("min_level") for r in mons if r.get("min_level")), None)
        zen = next((r.get("zen_cost") for r in mons if r.get("zen_cost")), None)
        rr = next((r.get("resets") for r in mons if r.get("resets") is not None), None)
        extra = (f" 入場 Lv{lv}+" if lv else "") + (f" Zen {zen:,}" if zen else "") + (f" 推奨 {rr}RR" if rr is not None else "")
        print(f"  {mp}{f' ({extra.strip()})' if extra else ''} ({n}): " + ", ".join(
            f"{r['monster']} Lv{r['level'] or '?'}" + (f" HP{r['hp']}" if r.get("hp") else "")
            + (f" 攻{r['dmg_min']}-{r['dmg_max']}" if r.get("dmg_min") is not None else "")
            + (f" 防{r['def']}" if r.get("def") is not None else "") + ("*" if r.get("icon") else "") for r in mons[:6])
              + (" …" if n > 6 else ""))
    if not rows:
        print("[guide] マップとモンスターを読み取れませんでした。")
        if html is not None:
            from akm.monsters import parse_tables

            tables = parse_tables(html)
            print(f"  ページの大きさ {len(html)} 文字、表 {len(tables)} 個")
            for t in tables[:5]:
                print(f"  表「{t['heading']}」: " + " / ".join(" | ".join(r) for r in t["rows"][:3]))
            text = [l.strip() for l in html_to_text(html).splitlines() if l.strip()]
            print("  文字の先頭 40 行:")
            for l in text[:40]:
                print("    " + l[:100])
            if len(text) < 20:
                print("  文字がほとんどありません。ページが JavaScript で作られている可能性があります。ブラウザで開いて"
                      " Ctrl+A → Ctrl+C し、メモ帳に貼って保存してから --text で取り込んでください")
        raise SystemExit(1)
    if args.dump:
        return
    cfg = load_config("config.yaml") if (PC_DIR / "config.yaml").exists() else {}
    db_path = PC_DIR / ((cfg.get("maplog") or {}).get("db", "data/mu_map.db"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not args.no_icons and any(r.get("icon") for r in rows):
        from akm.monsters import download_icons

        base = args.url if not args.file else GUIDE_URL
        n = download_icons(rows, base, db_path.parent / "monster_icons")
        print(f"[guide] アイコンを {n} 枚取得しました ({db_path.parent / 'monster_icons'})")
    else:
        for r in rows:
            r["icon"] = ""
    con = sqlite3.connect(str(db_path))
    try:
        n_maps, n_mons = save_guide(con, rows, src)
    finally:
        con.close()
    print(f"[guide] {db_path} に保存しました (マップ {n_maps}、モンスター {n_mons})。"
          "mirror_only / learn_loop は 30 秒以内に読み直します")


if __name__ == "__main__":
    main()
