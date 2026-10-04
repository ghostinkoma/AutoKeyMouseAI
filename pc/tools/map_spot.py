"""狩場などの地点を登録する (map_logger.py が記録した最新の位置を使う)。

    python tools\\map_spot.py add "Atlans 狩場1" --radius 8      今いる位置を狩場として登録
    python tools\\map_spot.py add "Lorencia 薬屋" --kind shop --map Lorencia --x 128 --y 135
    python tools\\map_spot.py list
    python tools\\map_spot.py delete 3
"""
import argparse

import _path  # noqa: F401

from akm.config import PC_DIR, load_config
from akm.maploc import Location, MapDB


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("name")
    a.add_argument("--radius", type=int, default=5)
    a.add_argument("--kind", default="hunt", help="hunt (狩場) / shop / town / safe など")
    a.add_argument("--map")
    a.add_argument("--x", type=int)
    a.add_argument("--y", type=int)
    a.add_argument("--note", default="")
    sub.add_parser("list")
    d = sub.add_parser("delete")
    d.add_argument("id", type=int)
    args = ap.parse_args()

    cfg = load_config("config.yaml")
    db = MapDB(PC_DIR / (cfg.get("maplog") or {}).get("db", "data/mu_map.db"))
    if args.cmd == "add":
        if args.map and args.x is not None and args.y is not None:
            loc = Location(args.map, args.x, args.y)
        else:
            loc = db.latest()
            if loc is None:
                raise SystemExit("記録された位置がありません。先に map_logger.py を動かすか --map --x --y を指定してください")
        sid = db.add_spot(args.name, loc, args.radius, args.kind, args.note)
        print(f"登録しました #{sid}: {args.name} {loc.map} ({loc.x}, {loc.y}) 半径 {args.radius} [{args.kind}]")
    elif args.cmd == "list":
        for r in db.db.execute("SELECT id, name, map, x, y, radius, kind, note FROM spots ORDER BY id"):
            print(f"#{r[0]}  {r[1]}  {r[2]} ({r[3]}, {r[4]})  半径 {r[5]}  [{r[6]}]  {r[7] or ''}")
    elif args.cmd == "delete":
        db.db.execute("DELETE FROM spots WHERE id=?", (args.id,))
        db.db.commit()
        print(f"削除しました #{args.id}")
    db.close()


if __name__ == "__main__":
    main()
