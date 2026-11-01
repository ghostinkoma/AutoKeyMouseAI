"""ボタンの右に ▼ だけを付けた「分割ボタン」(tkinter)。

    [ 帰還: Lorencia ][▼]
    ┌──────────────────┐   ▼ を押すと、ボタンのすぐ下に選択肢の一覧を開く
    │ Lorencia         │   選ぶとボタンの表示がその名前に変わる (この時点では実行しない)
    │ Noria            │   本体のボタンを押すと、選んでいるもので on_click(値) を呼ぶ
    │ ...              │   Esc / 一覧の外をクリック / ▼ をもう一度押す で閉じる
    └──────────────────┘
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class SplitButton(ttk.Frame):
    def __init__(self, master, prefix: str, items: list[tuple[str, object]], on_click=None, on_select=None,
                 selected: int = 0, rows: int = 12, **kw):
        """items: [(表示名, 値), ...]。prefix はボタンの表示の頭 (例 "帰還: ")。"""
        super().__init__(master, **kw)
        self.prefix = prefix
        self.items = list(items)
        self.on_click = on_click
        self.on_select = on_select
        self.rows = rows
        self.index = max(0, min(selected, len(self.items) - 1))
        self.popup: tk.Toplevel | None = None
        self.listbox: tk.Listbox | None = None

        self.main = ttk.Button(self, command=self._click)
        self.main.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.arrow = ttk.Button(self, text="▼", width=2, command=self.toggle)
        self.arrow.pack(side=tk.LEFT, fill=tk.Y)
        self._caption()

    # ---- 値 -------------------------------------------------------------------
    @property
    def value(self):
        return self.items[self.index][1] if self.items else None

    @property
    def text(self) -> str:
        return self.items[self.index][0] if self.items else ""

    def select(self, index: int) -> None:
        if 0 <= index < len(self.items):
            self.index = index
            self._caption()
            if self.on_select:
                self.on_select(self.value)

    def _caption(self) -> None:
        self.main.configure(text=f"{self.prefix}{self.text}")

    def _click(self) -> None:
        self.close()
        if self.on_click and self.items:
            self.on_click(self.value)

    # ---- 一覧 (ボタンの下に開く) --------------------------------------------------
    def toggle(self) -> None:
        if self.popup is not None:
            self.close()
        else:
            self.open()

    def open(self) -> None:
        if self.popup is not None or not self.items:
            return
        top = self.popup = tk.Toplevel(self)
        top.overrideredirect(True)  # 枠もタイトルも無い、ボタンに付いた一覧にする
        top.transient(self.winfo_toplevel())
        self.update_idletasks()
        x, y = self.winfo_rootx(), self.winfo_rooty() + self.winfo_height()
        frame = ttk.Frame(top, borderwidth=1, relief=tk.SOLID)
        frame.pack(fill=tk.BOTH, expand=True)
        lb = self.listbox = tk.Listbox(frame, height=min(self.rows, len(self.items)), activestyle="dotbox",
                                       exportselection=False, borderwidth=0, highlightthickness=0,
                                       selectbackground="#2b7de9")
        for label, _ in self.items:
            lb.insert(tk.END, label)
        lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        if len(self.items) > self.rows:
            sb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=lb.yview)
            sb.pack(side=tk.RIGHT, fill=tk.Y)
            lb.configure(yscrollcommand=sb.set)
        lb.selection_set(self.index)
        lb.activate(self.index)
        lb.see(self.index)
        top.update_idletasks()
        top.geometry(f"{max(self.winfo_width(), top.winfo_reqwidth())}x{top.winfo_reqheight()}+{x}+{y}")
        lb.bind("<ButtonRelease-1>", self._pick)
        lb.bind("<Return>", self._pick)
        lb.bind("<Escape>", lambda e: self.close())
        lb.focus_set()
        # 一覧の外をクリックしたら閉じる (▼ 自身は toggle で閉じるので除く)
        self._outside = self.winfo_toplevel().bind("<Button-1>", self._click_outside, add="+")

    def _pick(self, _=None) -> None:
        sel = self.listbox.curselection()
        self.close()
        if sel:
            self.select(sel[0])

    def _click_outside(self, e) -> None:
        if self.popup is None or e.widget in (self.arrow, self.listbox):
            return
        self.close()

    def close(self) -> None:
        if self.popup is None:
            return
        try:
            self.winfo_toplevel().unbind("<Button-1>", self._outside)
        except tk.TclError:
            pass
        self.popup.destroy()
        self.popup = None
        self.listbox = None
