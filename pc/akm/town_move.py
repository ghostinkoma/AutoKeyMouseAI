"""帰還 (街へ移動): チャット欄に /move <行き先> を打って送る。

流れ (ESP32 のマクロ書式):
    k:enter ; w:<開くのを待つ> ; t:/move <行き先> ; k:enter
送ったあとは現在地 (akm.maploc の OCR) のマップ名が行き先に変わったかで、着いたかを確かめる。

/move の行き先の名前はサーバーの MoveReq (クライアントとサーバーの両方にある移動先の表) で決まる。
Season 6 の一般的な名前を既定にしているが、MURex で確かめていないもの (verified=False) は
config.yaml の town_move.commands で上書きできる。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Town:
    key: str          # 設定で使う名前
    label: str        # ボタンに出す名前
    ja: str           # 日本語の呼び名
    command: str      # /move のあとに打つ名前
    maps: tuple[str, ...]  # 着いたときに現在地に出るマップ名 (どれか)
    verified: bool = False  # MURex で確かめたか


TOWNS: tuple[Town, ...] = (
    Town("lorencia", "Lorencia", "ロレンシア", "lorencia", ("Lorencia",), verified=True),
    Town("noria", "Noria", "ノリア", "noria", ("Noria",)),
    Town("devias", "Devias", "デビアス", "devias", ("Devias",)),
    Town("elbeland", "Elbeland", "エルベランド", "elbeland", ("Elbeland",)),
    Town("losttower", "Lost Tower", "ロストタワー", "losttower", ("Lost Tower",)),
    Town("tarkan", "Tarkan", "タルカン", "tarkan", ("Tarkan",)),
    Town("aida", "Aida", "アイダ", "aida", ("Aida",)),
    Town("kanturu_ruins", "Kanturu Ruins", "カントルランズ", "kanturu", ("Kanturu", "Kanturu Ruins")),
    Town("kanturu_relics", "Kanturu Relics", "カントル", "kanturu3", ("Kanturu Relics", "Kanturu")),
    Town("raklion", "Raklion", "ラキオン", "raklion", ("Raklion",)),
    Town("crywolf", "Crywolf", "クライウルフ", "crywolf", ("Crywolf Fortress", "Crywolf")),
)

DEFAULT_TOWN = "lorencia"


def load_towns(cfg: dict | None = None) -> list[Town]:
    """config.yaml の town_move.commands で /move の名前を上書きした一覧 (上書きしたものは確認済み扱い)。"""
    over = ((cfg or {}).get("town_move") or {}).get("commands") or {}
    out = []
    for t in TOWNS:
        cmd = over.get(t.key)
        if cmd:
            t = Town(t.key, t.label, t.ja, str(cmd).strip(), t.maps, verified=True)
        out.append(t)
    return out


def find_town(towns: list[Town], key_or_label: str) -> Town | None:
    k = key_or_label.strip().lower()
    return next((t for t in towns if k in (t.key, t.label.lower(), t.command.lower()) or k == t.ja), None)


def move_script(town: Town, open_wait_ms: int = 300, after_ms: int = 0) -> str:
    """チャット欄を開いて /move を送るマクロ。文字列中の ; は \\; にする (macro.h の書式)。"""
    text = f"/move {town.command}".replace(";", "\\;")
    s = f"k:enter;w:{int(open_wait_ms)};t:{text};k:enter"
    return s + (f";w:{int(after_ms)}" if after_ms > 0 else "")


def arrived(town: Town, map_name: str | None) -> bool:
    """現在地のマップ名が行き先のものか (大文字小文字・空白は無視。"Lost Tower 3" なども Lost Tower とみなす)。"""
    if not map_name:
        return False
    norm = "".join(map_name.lower().split())
    return any(norm.startswith("".join(m.lower().split())) for m in town.maps)
