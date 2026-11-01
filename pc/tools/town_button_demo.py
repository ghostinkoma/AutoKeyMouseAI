"""帰還ボタン (分割ボタン) の見た目と動きの確認用。ESP32 には何も送らない。

    python tools\\town_button_demo.py

▼ で行き先を選ぶとボタンの表示が変わり、ボタンを押すと送る予定のマクロを表示する。
"""
import tkinter as tk
from tkinter import ttk

import _path  # noqa: F401

from akm.split_button import SplitButton
from akm.town_move import DEFAULT_TOWN, find_town, load_towns, move_script


def main():
    try:  # config.yaml が無くても既定の名前で動く
        from akm.config import load_config

        cfg = load_config("config.yaml") or {}
    except (Exception, SystemExit):
        cfg = {}
    tcfg = cfg.get("town_move") or {}
    towns = load_towns(cfg)
    first = find_town(towns, tcfg.get("default", DEFAULT_TOWN)) or towns[0]

    root = tk.Tk()
    root.title("帰還ボタンの確認")
    root.geometry("460x360")
    out = tk.StringVar(value="▼ で行き先を選び、ボタンを押してください")

    def run(town):
        mark = "" if town.verified else "  (MURex で未確認の名前)"
        out.set(f"{town.ja} へ帰還\n送るマクロ: {move_script(town, tcfg.get('open_chat_wait_ms', 300))}{mark}")

    items = [(t.label, t) for t in towns]
    sb = SplitButton(root, "帰還: ", items, on_click=run, selected=towns.index(first),
                     on_select=lambda t: out.set(f"行き先を {t.label} ({t.ja}) にしました"))
    sb.pack(anchor="nw", padx=12, pady=12)
    ttk.Label(root, textvariable=out, wraplength=420, justify=tk.LEFT).pack(anchor="nw", padx=12)
    root.mainloop()


if __name__ == "__main__":
    main()
