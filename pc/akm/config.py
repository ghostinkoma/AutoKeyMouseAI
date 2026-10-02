from __future__ import annotations

from pathlib import Path

import yaml

from .device import AbsMap
from .screen import primary_screen_size

PC_DIR = Path(__file__).resolve().parent.parent


def load_config(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = PC_DIR / p
    if not p.exists():
        raise SystemExit(f"設定ファイルがありません: {p}\n  config.example.yaml を config.yaml にコピーしてください")
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def abs_map_from(cfg: dict) -> AbsMap:
    m = (cfg.get("mouse") or {}).get("abs_map")
    if m:
        return AbsMap(**m)
    return AbsMap.for_screen(*primary_screen_size())
