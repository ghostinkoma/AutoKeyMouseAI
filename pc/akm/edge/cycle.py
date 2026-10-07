"""学習 → 蓄積 → 出力 → 学習 … を自動で繰り返す。

  蓄積: ミラーを続けながら等倍の画面を dataset/frames に貯める (akm/edge/live.py の learn_dir)
  学習: 新しい画面が every_frames 枚たまったら、別のプロセスで学習する (優先度を下げてゲームの邪魔をしない)
          窓・アイコン: 毎回 (tools/edge_objects.py train)
          文字認識:     ocr_every 回に 1 回 (時間がかかるので)。評価用の実画面で前より悪ければ採用しない
  出力: models/*.npz と ESP32 用の firmware/AutoKeyMouse/edge_*_model.h を書き出す
  反映: 動いている認識 (LiveRecognizer) に新しいモデルを差し替える → また蓄積へ
記録は models/learn_log.txt に 1 行ずつ残す。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path


def run_low_priority(cmd: list[str], cwd: Path) -> tuple[int, str]:
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.BELOW_NORMAL_PRIORITY_CLASS
    else:
        kw["preexec_fn"] = lambda: os.nice(10)
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


class LearnCycle:
    def __init__(self, pc_dir: Path, frames_dir: Path, every_frames: int = 60, ocr_every: int = 3,
                 reload=None, runner=run_low_priority, check_s: float = 30.0, log=print):
        self.pc = Path(pc_dir)
        self.frames_dir = Path(frames_dir)
        self.every_frames = every_frames  # 何枚たまったら学習するか (30 秒に 1 枚なら 60 枚 ≒ 30 分)
        self.ocr_every = ocr_every
        self.reload = reload              # () → None: 新しいモデルを認識に差し替える
        self.runner = runner
        self.check_s = check_s
        self.log = log
        self.rounds = 0
        self.busy = False
        self._last_names = self._names()  # この時点までの画面は「学習済み」とみなす
        self._stop = threading.Event()
        self._thread = None
        self.history: list[str] = []

    def _count(self) -> int:
        return len(list(self.frames_dir.glob("*.png"))) if self.frames_dir.exists() else 0

    def _names(self) -> set[str]:
        return {f.name for f in self.frames_dir.glob("*.png")} if self.frames_dir.exists() else set()

    def new_frames(self) -> int:
        return len(self._names() - self._last_names)

    def _record(self, line: str) -> None:
        self.history.append(line)
        self.log(f"[learn] {line}")
        try:
            log_file = self.pc / "models" / "learn_log.txt"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
        except OSError:
            pass

    def round(self) -> None:
        """1 周: 学習 → 出力 → 反映。"""
        self.busy = True
        self.rounds += 1
        n = self.rounds
        try:
            t0 = time.time()
            self.log(f"[learn] 第 {n} 回の学習を始めます (新しい画面 {self.new_frames()} 枚、合計 {self._count()} 枚)")
            self._last_names = self._names()
            py = sys.executable
            code, out = self.runner([py, "tools/edge_objects.py", "train"], self.pc)
            res = [l for l in out.splitlines() if l.startswith("[result]")]
            if code == 0:
                self._record(f"第 {n} 回 窓・アイコン: 出力しました {res[-1][9:] if res else ''}")
            else:
                self._record(f"第 {n} 回 窓・アイコン: 失敗 ({out.strip().splitlines()[-1] if out.strip() else code})")
            if self.ocr_every and n % self.ocr_every == 0:
                code2, out2 = self.runner([py, "tools/edge_train.py", "train", "--lines", "20000", "--keep-better"],
                                          self.pc)
                res2 = [l for l in out2.splitlines() if l.startswith("[result]")]
                if code2 == 0:
                    self._record(f"第 {n} 回 文字認識: {res2[-1][9:] if res2 else '出力しました'}")
                else:
                    self._record(f"第 {n} 回 文字認識: 失敗 ({out2.strip().splitlines()[-1] if out2.strip() else code2})")
            if self.reload:
                self.reload()
            self.log(f"[learn] 第 {n} 回を反映しました ({time.time() - t0:.0f} 秒)。また画面を貯めます")
        finally:
            self.busy = False

    def due(self) -> bool:
        return not self.busy and self.new_frames() >= self.every_frames

    def start(self) -> None:
        def loop():
            while not self._stop.is_set():
                if self.due():
                    try:
                        self.round()
                    except Exception as e:
                        self._record(f"学習で失敗: {e}")
                self._stop.wait(self.check_s)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
