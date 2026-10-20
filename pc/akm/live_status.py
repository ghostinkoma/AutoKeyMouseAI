"""学習ループ (tools/mirror_only.py) の今の状況をファイルに書き、地図ビューアの「学習の状況」に出す。

models/live_status.txt (UTF-8):
    # 2026-10-21 00:10:05        ← 書いた時刻 (ビューアは古ければ「止まっている」と出す)
    現在地<TAB>Icarus 51,70     ← 項目と値 (順番どおりに表)
    ...
    #events
    00:10:01 [rare] party_invite …   ← 最近の出来事 (画面に出したログの行)
ビューアが読んでいる間でも書けるよう、別のファイルに書いてから置き換える (置き換えられなければ次の回に)。
"""
from __future__ import annotations

import collections
import os
import sys
import threading
import time
from pathlib import Path

# 出来事として残さない行 (5 秒ごとの送信の集計など、多すぎるもの)
SKIP_PREFIXES = ("[mirror] 送信 ", "         ESP32:")


class EventTee:
    """print の出力をそのまま画面に出しつつ、最近の行を取っておく。"""

    def __init__(self, out, keep: int = 200):
        self.out = out
        self.lines: collections.deque[str] = collections.deque(maxlen=keep)
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, s: str) -> int:
        n = self.out.write(s)
        with self._lock:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip() and not line.startswith(SKIP_PREFIXES):
                    self.lines.append(time.strftime("%H:%M:%S ") + line.rstrip())
        return n

    def flush(self) -> None:
        self.out.flush()

    def __getattr__(self, name):
        return getattr(self.out, name)

    def recent(self) -> list[str]:
        with self._lock:
            return list(self.lines)


def install_tee() -> EventTee:
    if isinstance(sys.stdout, EventTee):
        return sys.stdout
    tee = EventTee(sys.stdout)
    sys.stdout = tee
    return tee


def write_status(path: Path, rows: list[tuple[str, str]], events: list[str]) -> bool:
    text = "# " + time.strftime("%Y-%m-%d %H:%M:%S") + "\n"
    text += "".join(f"{k}\t{str(v).replace(chr(9), ' ').replace(chr(10), ' ')}\n" for k, v in rows)
    text += "#events\n" + "".join(e.replace("\t", " ") + "\n" for e in events)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False  # ビューアが読んでいる最中など。次の回に書く


def status_rows(m=None, rec=None, cycle=None, esp: dict | None = None, pc_dir: Path | None = None) -> list[tuple[str, str]]:
    """今の状況を (項目, 値) の並びに。分からないものは「?」。"""
    rows: list[tuple[str, str]] = []
    last = rec.last if rec is not None else None
    if last is not None:
        loc = last.loc or last.last_loc
        rows.append(("現在地", f"{loc.map} {loc.x},{loc.y}" + ("" if last.loc else " (前に読めた所)") if loc else "?"))
        rows.append(("MU Helper", {True: "ON (動作中)", False: "OFF (停止中)"}.get(last.helper, "?")))
        rows.append(("窓 (PC の認識)", ", ".join(last.windows) or "なし"))
        t = last.shown_target or last.target
        rows.append(("相手", f"{t.name} Lv{t.level or '?'}" if t is not None else "なし"))
        rows.append(("認識の時間", f"{last.ms:.0f} ms"))
    if m is not None:
        rows.append(("液晶への送信", f"{m.fps:.1f} fps  送信 {m.sent} 枚 / 失敗 {m.errors}  {m.last_bytes} バイト/枚 ({m.fmt})"))
    if esp:
        e = esp.get("mirror") or {}
        rows.append(("ESP32", f"{e.get('fps', 0)} fps  CPU {e.get('cpu_pct', 0)}%  空きメモリ {esp.get('heap', 0) // 1024}KB"
                              f" (最大連続 {esp.get('heapBlock', 0) // 1024}KB)  {esp.get('fw', '')}"))
        win = esp.get("win")
        if win is None:
            rows.append(("ESP32 の窓の認識", "ファームウェアに無い"))
        elif not win.get("on"):
            rows.append(("ESP32 の窓の認識", "モデル待ち (学習すると Wi-Fi で届きます)"))
        else:
            names = ", ".join(f"{w['name']} {w['p']:.2f}" for w in win.get("windows", [])) or "なし"
            rows.append(("ESP32 の窓の認識", f"{names}  (モデル {win.get('id', '')} / {win.get('src', '')},"
                                            f" {win.get('ms', 0)} ms)"))
    elif m is not None:
        rows.append(("ESP32", "状態を取得できません"))
    if rec is not None and rec.rare is not None:
        seen, dec = rec.rare.seen, rec.rare.declined
        rows.append(("PT 招待など", ", ".join(f"{n} {c} 回 (断った {dec.get(n, 0)})" for n, c in seen.items())
                     or "まだ来ていない"))
    if cycle is not None:
        rows.append(("自動学習", "学習中…" if cycle.busy else
                     f"次の学習まで {max(0, cycle.every_frames - cycle.new_frames())} 枚 (済 {cycle.rounds} 回)"))
        if cycle.history:
            rows.append(("前回の学習", cycle.history[-1]))
    if rec is not None and rec.world is not None:
        st = rec.world.stats
        rows.append(("DB 目撃", f"登録 {st['ok']} / 保留 {st['pending']} / 除外 {st['ng']}  位置 {st['loc_ok']}"))
    if pc_dir is not None:
        score = Path(pc_dir) / "models" / "edge_win_score.txt"
        table = Path(pc_dir) / "models" / "window_table.tsv"
        if table.exists():
            n = max(0, len(table.read_text(encoding="utf-8").strip().splitlines()) - 1)
            acc = score.read_text().split()[0] if score.exists() else ""
            rows.append(("窓の種類の学習", f"{n} 種" + (f"  正解率 {float(acc):.1%}" if acc else "")))
        else:
            rows.append(("窓の種類の学習", "まだ"))
    return rows
