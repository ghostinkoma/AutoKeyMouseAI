"""ミラーを続けながら、縮小する前の画面で窓・アイコン・相手の名前を認識し、液晶に重ねて表示する。

ミラー (OBS → 縮小 → BadCodec → ESP32) とは別のスレッドで、一定間隔で等倍の画面を撮って認識する。
OBS への問い合わせは同時にできないので、撮影はロックで順番にする。
learn_dir を渡すと、撮った等倍の画面をときどき保存する (窓・アイコンや文字の学習の素材)。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2

GREEN, RED, YELLOW, MAGENTA, GRAY = (0, 255, 0), (60, 60, 255), (0, 220, 255), (255, 120, 255), (200, 200, 200)


@dataclass
class Recognition:
    helper: bool | None = None                  # MU Helper (■ = 動作中 / ▶ = 停止中)
    windows: list[str] = field(default_factory=list)
    icons: list[str] = field(default_factory=list)
    target: object = None                       # akm.edge.target.Target
    loc: object = None                          # 現在地 (akm.maploc.Location。world を渡したとき)
    judge: tuple | None = None                  # 相手の名前の判定 ('ok' / 'pending' / 'ng', 確率)
    ms: float = 0.0
    last_loc: object = None                     # 現在地が読めないときに出す、最後に分かった現在地
    shown_target: object = None                 # 液晶に出す相手 (見えなくなっても少しの間は残す)
    target_age: float = 0.0                     # shown_target を最後に見てからの秒数


def overlay_lines(r: Recognition) -> list[tuple[str, tuple[int, int, int]]]:
    """液晶に重ねる認識結果。マップ・MU Helper・モンスターの 3 行は、分からないときも「?」で常に出す。"""
    if r.loc is not None:
        mp = (f"MAP {r.loc.map} {r.loc.x},{r.loc.y}", GREEN)
    elif r.last_loc is not None:
        mp = (f"MAP {r.last_loc.map} (old)", GRAY)
    else:
        mp = ("MAP ?", GRAY)
    helper = {True: ("HELPER ON", GREEN), False: ("HELPER OFF", RED)}.get(r.helper, ("HELPER ?", GRAY))
    t = r.shown_target if r.shown_target is not None else r.target
    if t is not None:
        hp = f" {t.hp_ratio * 100:.0f}%" if t.hp_ratio is not None and r.target is not None else ""
        mark = {"ok": "", "pending": " ?", "ng": " x"}.get(r.judge[0], "") if r.judge else ""
        mob = (f"MOB {t.name or '?'}" + (f" Lv{t.level}" if t.level else "") + hp + mark,
               MAGENTA if r.target is not None else GRAY)
    else:
        mob = ("MOB -", GRAY)
    lines = [mp, helper, mob]
    if r.windows:
        lines.append(("WIN " + " ".join(r.windows), YELLOW))
    return lines


class LiveRecognizer:
    def __init__(self, grab_full, detector, ocr=None, period: float = 1.0, on_result=None,
                 learn_dir: Path | None = None, learn_every: float = 30.0, learn_max: int = 500, log=print,
                 harvester=None, world=None):
        self.grab_full = grab_full      # () → 等倍の画面 (BGR)
        self.detector = detector        # akm.edge.objects.ObjectDetector
        self.ocr = ocr                  # akm.edge.ocr.OcrReader (無ければ名前は読まない)
        self.period = period
        self.on_result = on_result      # (Recognition) → None
        self.learn_dir = learn_dir
        self.learn_every = learn_every
        self.learn_max = learn_max
        self.log = log
        self.harvester = harvester      # akm.edge.harvest.OcrHarvester (正解付きの文字の行を自動で集める)
        self.world = world              # akm.world_log.WorldLogger (現在地と相手を DB に記録する)
        self.last = Recognition()
        self.keep_target_s = 8.0        # 相手が見えなくなっても液晶に残す秒数
        self._shown = None
        self._shown_ts = -1e9
        self._last_loc = None
        self.words = None
        self._last_save = 0.0
        self._stop = threading.Event()
        self._thread = None

    def recognize(self, img) -> Recognition:
        t0 = time.perf_counter()
        r = Recognition()
        for d in self.detector.detect(img):
            if not d.present:
                continue
            if d.name == "helper_running":
                r.helper = True
            elif d.name == "helper_stopped":
                r.helper = False
            elif d.kind == "window":
                r.windows.append(d.name)
            else:
                r.icons.append(d.name)
        if self.ocr is not None:
            from .target import read_target

            r.target = read_target(img, self.ocr)
            if r.target is not None and r.target.name:
                from .harvest import lexicon_fix, load_lexicon

                if self.words is None:
                    self.words = load_lexicon()
                fixed, _ = lexicon_fix(r.target.name, self.words)
                r.target.raw_name = r.target.name
                r.target.name = fixed  # 辞書の名前に十分近ければ直す
        r.ms = (time.perf_counter() - t0) * 1000
        return r

    def _save(self, img) -> None:
        now = time.monotonic()
        if self.learn_dir is None or now - self._last_save < self.learn_every:
            return
        self._last_save = now
        self.learn_dir.mkdir(parents=True, exist_ok=True)
        files = sorted(self.learn_dir.glob("*.png"))
        if len(files) >= self.learn_max:
            files[0].unlink()  # 古いものから消す
        cv2.imwrite(str(self.learn_dir / time.strftime("%Y%m%d_%H%M%S.png")), img)

    def step(self) -> Recognition:
        img = self.grab_full()
        r = self.recognize(img)
        self._save(img)
        if self.world is not None:
            try:
                self.world.feed(img, r, self.ocr)
                r.loc = self.world.current()
                if r.target is not None:
                    r.judge = self.world.last_judge
                    cur = getattr(self.world, "_cur", None)
                    if cur and cur.get("monster"):
                        r.target.name = cur["monster"]  # マップガイドで直した名前を出す
            except Exception as e:
                self.log(f"[world] 記録に失敗: {e}")
        if self.harvester is not None:  # 記録の後: マップガイドで直した名前を正解の見本として集められる
            try:
                if self.world is not None:
                    self.harvester.words = self.world.words
                self.harvester.feed(img, r.target)
            except Exception as e:
                self.log(f"[harvest] 失敗: {e}")
        now = time.monotonic()
        if r.loc is not None:
            self._last_loc = r.loc
        r.last_loc = self._last_loc
        if r.target is not None and r.target.name:
            self._shown, self._shown_ts = r.target, now
        if now - self._shown_ts <= self.keep_target_s:
            r.shown_target, r.target_age = self._shown, now - self._shown_ts
            if r.target is None and self.last.judge is not None:
                r.judge = self.last.judge
        if (r.helper, r.windows, getattr(r.target, "name", None)) != \
                (self.last.helper, self.last.windows, getattr(self.last.target, "name", None)):
            parts = [f"Helper {'ON' if r.helper else 'OFF' if r.helper is False else '?'}",
                     f"窓 {', '.join(r.windows) or 'なし'}"]
            if r.target is not None:
                parts.append(f"相手 {r.target.name or '?'} Lv{r.target.level or '?'}")
            self.log(f"[recognize] {'  '.join(parts)}  ({r.ms:.0f}ms)")
        self.last = r
        if self.on_result:
            self.on_result(r)
        return r

    def start(self) -> None:
        def loop():
            while not self._stop.is_set():
                t0 = time.monotonic()
                try:
                    self.step()
                except Exception as e:  # 認識で失敗してもミラーは止めない
                    self.log(f"[recognize] 失敗: {e}")
                self._stop.wait(max(0.05, self.period - (time.monotonic() - t0)))

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(5)
