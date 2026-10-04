"""同梱のモンスター一覧をマップビューア (Java) 用に書き出す (英語のマップ名を付ける)。

    python tools\\export_viewer_monsters.py   → viewer/src/main/resources/akm/viewer/monsters.tsv
その後 viewer で mvn package すると jar に入る。
"""
import _path  # noqa: F401

from akm.config import PC_DIR
from akm.monsters import load_builtin

out = PC_DIR.parent / "viewer" / "src" / "main" / "resources" / "akm" / "viewer" / "monsters.tsv"
rows = load_builtin()
with open(out, "w", encoding="utf-8") as f:
    f.write("# pc/akm/data/monsters_munou2014.tsv から生成 (python pc/tools/export_viewer_monsters.py)\n"
            "# レベル|名前|備考|生命|マップ|マップ(英語)\n")
    for m in rows:
        f.write("|".join(["" if m["level"] is None else str(m["level"]), m["name"], m["note"] or "",
                          "" if m["hp"] is None else str(m["hp"]), m["map"], m["map_en"]]) + "\n")
print(f"{len(rows)} 件 → {out}")
