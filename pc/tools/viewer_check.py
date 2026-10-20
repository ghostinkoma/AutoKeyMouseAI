"""Java のマップビューアが読む DB (mu_map.db) を、ビューアと同じ読み方で確かめる。
ビューアが動かない・更新されないときに、どこで止まっているかを調べる。

    python tools\\viewer_check.py
"""
import sqlite3
import time
from pathlib import Path

import _path  # noqa: F401

from akm.config import PC_DIR, load_config

QUERIES = {  # ビューア (MapDb.java) が 1 秒ごとに読むもの
    "マップの一覧": "SELECT name FROM maps ORDER BY last_seen DESC",
    "今いる場所": "SELECT map, x, y, ts FROM positions ORDER BY ts DESC LIMIT 1",
    "歩いたマス": "SELECT COUNT(*) FROM cells",
    "目撃": "SELECT COUNT(*), MAX(last_ts) FROM sightings",
    "出現エリア": "SELECT COUNT(*) FROM monster_areas",
    "登録ログ": "SELECT COUNT(*), MAX(ts) FROM spawn_log",
    "新しいマップの候補": "SELECT COUNT(*) FROM map_names WHERE reviewed=0",
    "スポット": "SELECT COUNT(*) FROM spots",
    "スポットのモンスター": "SELECT COUNT(*) FROM spot_monsters",
    "出来事": "SELECT ts, kind, map, x, y, detail FROM events ORDER BY ts DESC LIMIT 1",
}


def when(ts):
    if not ts:
        return "-"
    ago = time.time() - float(ts)
    return f"{time.strftime('%H:%M:%S', time.localtime(float(ts)))} ({ago:.0f} 秒前)"


def main() -> None:
    cfg = load_config("config.yaml") if (PC_DIR / "config.yaml").exists() else {}
    path = PC_DIR / ((cfg.get("maplog") or {}).get("db", "data/mu_map.db"))
    print(f"[check] DB: {path}")
    if not path.exists():
        raise SystemExit("[check] DB がありません")
    st = path.stat()
    print(f"[check] 最後に書き込まれた時刻: {when(st.st_mtime)}  大きさ {st.st_size // 1024} KB")
    wal = Path(str(path) + "-wal")
    if wal.exists():
        print(f"[check] 書き込み途中のファイル (-wal): {when(wal.stat().st_mtime)}  {wal.stat().st_size // 1024} KB")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
    print(f"[check] journal_mode: {con.execute('PRAGMA journal_mode').fetchone()[0]}")
    bad = 0
    for name, q in QUERIES.items():
        try:
            row = con.execute(q).fetchone()
            if name == "今いる場所" and row:
                print(f"  OK  {name}: {row[0]} ({row[1]}, {row[2]})  記録 {when(row[3])}")
            elif name in ("目撃", "登録ログ") and row:
                print(f"  OK  {name}: {row[0]} 件  最新 {when(row[1])}")
            elif name == "出来事" and row:
                print(f"  OK  {name}: {row[1]} {row[2]} ({row[3]}, {row[4]}) {when(row[0])}")
            elif name == "マップの一覧":
                rows = con.execute(q).fetchall()
                print(f"  OK  {name}: {', '.join(r[0] for r in rows[:8])}")
            else:
                print(f"  OK  {name}: {row[0] if row else '-'}")
        except sqlite3.Error as e:
            bad += 1
            print(f"  NG  {name}: {e}")
    print("[check] " + ("ビューアが読む表はすべて読めます" if not bad else f"{bad} 個の表が読めません (ビューアはここで止まります)"))
    print("[check] 「今いる場所」の記録が数秒前なら、Python 側は書けています。ビューアの「追従」にチェックがあるか、"
          "ビューアのタイトルの DB の場所が上と同じかを確かめてください")


if __name__ == "__main__":
    main()
