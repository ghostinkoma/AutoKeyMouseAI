"""MU Helper (ゲーム内の自動狩り) との協調。

ボットは MU Helper に狩りを任せ、必要なときだけ止めて (マウス操作で止まる) 作業し、
終わったら切替キーで再開させる。

状態の判定方法:
  1. helper.indicator を設定した場合: 画面の表示 (左上の MU Helper パネル等) をテンプレートで見て判定
  2. 未設定の場合: ボット自身の操作から推定
     - 切替キーを押した → 反転
     - マウスでクリックした → 停止したとみなす (helper.mouse_stops_helper)
"""
from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np

from .device import Device
from .vision import roi_px


class HelperControl:
    def __init__(self, cfg: dict, dev: Device, base_dir: Path):
        self.cfg = cfg or {}
        self.dev = dev
        self.key = str(self.cfg.get("toggle_key", "f9"))
        # click: パネルの ▶ ボタンをクリックして開始 / key: 切替キー / auto: ▶ が見えればクリック、無ければキー
        self.start_method = str(self.cfg.get("start_method", "auto"))
        self.to_screen = None  # クライアント座標 → スクリーン座標 (ElfBot が設定する)
        self.mouse_stops = bool(self.cfg.get("mouse_stops_helper", True))
        self.settle_s = float(self.cfg.get("settle_ms", 800)) / 1000
        self.retry_s = float(self.cfg.get("retry_s", 5))
        self.believed_on = bool(self.cfg.get("initially_on", False))
        self.last_toggle = 0.0
        self.toggles = 0

        ind = self.cfg.get("indicator") or {}
        self.ind_roi = ind.get("roi")
        self.ind_thr = float(ind.get("threshold", 0.85))
        self.tpl_on = self._load(base_dir, ind.get("on_image"))  # YAML では on/off が真偽値になるので別名
        self.tpl_off = self._load(base_dir, ind.get("off_image"))
        # パネル自体 ("MU Helper" の文字) が見えているか。見えなければ判定不能として推定値を使う
        self.tpl_panel = self._load(base_dir, ind.get("panel_image"))

    @staticmethod
    def _load(base: Path, f: str | None) -> np.ndarray | None:
        if not f:
            return None
        p = Path(f) if Path(f).is_absolute() else base / f
        img = cv2.imread(str(p), cv2.IMREAD_COLOR)
        if img is None:
            print(f"[helper] インジケータ画像が読めません: {p}")
        return img

    # ------------------------------------------------------------------ state
    def read_state(self, img: np.ndarray | None) -> bool | None:
        """画面から MU Helper の状態を読む。判定できなければ None。"""
        if img is None or self.ind_roi is None or (self.tpl_on is None and self.tpl_off is None):
            return None
        x0, y0, x1, y1 = roi_px(img.shape, self.ind_roi)
        area = img[y0:y1, x0:x1]

        def score(tpl: np.ndarray | None) -> float:
            if tpl is None or tpl.shape[0] > area.shape[0] or tpl.shape[1] > area.shape[1]:
                return -1.0
            return float(cv2.matchTemplate(area, tpl, cv2.TM_CCOEFF_NORMED).max())

        if self.tpl_panel is not None and score(self.tpl_panel) < self.ind_thr:
            return None  # パネルが見えない (隠れている / 位置が違う) ので判定しない
        s_on, s_off = score(self.tpl_on), score(self.tpl_off)
        if self.tpl_on is None:
            # 停止中の表示 (▶ ボタン) だけ分かっている場合: 見えていれば停止中、見えなければ動作中
            return s_off < self.ind_thr
        if max(s_on, s_off) < self.ind_thr:
            return None
        return s_on > s_off

    def find_start_button(self, img: np.ndarray | None) -> tuple[float, float] | None:
        """停止中の ▶ ボタンの中心 (クライアント座標)。見えなければ None。"""
        if img is None or self.ind_roi is None or self.tpl_off is None:
            return None
        x0, y0, x1, y1 = roi_px(img.shape, self.ind_roi)
        area = img[y0:y1, x0:x1]
        th, tw = self.tpl_off.shape[:2]
        if th > area.shape[0] or tw > area.shape[1]:
            return None
        res = cv2.matchTemplate(area, self.tpl_off, cv2.TM_CCOEFF_NORMED)
        _, best, _, (bx, by) = cv2.minMaxLoc(res)
        if best < self.ind_thr:
            return None
        return x0 + bx + tw / 2, y0 + by + th / 2

    def is_on(self, img: np.ndarray | None = None) -> bool:
        s = self.read_state(img)
        if s is not None:
            self.believed_on = s
        return self.believed_on

    # ---------------------------------------------------------------- actions
    def note_mouse_used(self) -> None:
        """ボットがクリックした。MU Helper は止まっているはず。"""
        if self.mouse_stops:
            self.believed_on = False

    def start(self, img: np.ndarray | None) -> None:
        """MU Helper を開始する。▶ ボタンが見えればクリック、見えなければ切替キー。"""
        pos = None
        if self.start_method in ("click", "auto") and self.to_screen is not None:
            pos = self.find_start_button(img)
        if pos is not None:
            sx, sy = self.to_screen(*pos)
            print(f"[helper] ▶ ボタンをクリック ({sx:.0f},{sy:.0f})")
            self.dev.click(sx, sy)
            self.believed_on = True
            self.last_toggle = time.monotonic()
            self.toggles += 1
        else:
            self.toggle()

    def toggle(self) -> None:
        self.dev.run(f"k:{self.key}")
        self.believed_on = not self.believed_on
        self.last_toggle = time.monotonic()
        self.toggles += 1

    def ensure_on(self, img: np.ndarray | None = None, grab=None) -> bool:
        """止まっていれば切替キーで再開する。押したら True。

        grab (画面を撮る関数) を渡すと、押したあと画面で再開を確認し、だめなら押し直す。
        """
        if self.is_on(img):
            return False
        if time.monotonic() - self.last_toggle < self.retry_s and self.read_state(img) is not None:
            return False  # 画面で確認できる場合は、押した直後の連打を避ける
        for attempt in range(int(self.cfg.get("verify_attempts", 3))):
            print("[helper] MU Helper を開始します" + (f" (再試行 {attempt})" if attempt else ""))
            self.start(img)
            time.sleep(self.settle_s)
            if grab is None:
                return True
            img = grab()
            state = self.read_state(img)
            if state is None or state:
                if state:
                    self.believed_on = True
                return True
            self.believed_on = False  # 画面上はまだ停止中: もう一度押す
        print("[helper] MU Helper の開始を確認できませんでした")
        return True

    def ensure_off(self, img: np.ndarray | None = None) -> bool:
        if not self.is_on(img):
            return False
        print("[helper] MU Helper を停止します")
        self.toggle()
        time.sleep(self.settle_s)
        return True
