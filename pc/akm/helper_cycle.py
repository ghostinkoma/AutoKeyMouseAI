"""MU Helper の定期再起動と見張り。

MU Helper は動作中「Instant Hunting Log」窓が出る。切替キー (Home) でオン/オフする。
  * 一定間隔 (既定 10 分) ごとに再起動する:
        W 連打 (500ms) → Home (停止) → W 連打 (200ms) → Home (再開) → W 連打 (500ms)
    W (ポーション) を 50ms ごとに押し続けるので、切り替えの間に襲われても死ににくい。
    キー操作は 1 本のスクリプトにまとめて ESP32 に送るので、間隔は ESP32 側で正確に守られる。
  * 再起動のあと、窓が出ているか画面で確かめる。出ていなければもう一度 Home。
  * 普段も見張り、窓が一定時間 (既定 6 秒) 見えなければ止まったとみなして Home で再開する。
    Home はトグルなので、押しても窓が出なければ (元から動いていて窓だけ隠れていた等) もう一度押して戻す。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


def potion_spam(ms: int, key: str = "w", period_ms: int = 50, press_ms: int = 5) -> str:
    """ms のあいだ period_ms ごとに key を押すスクリプト (ESP32 のマクロ)。"""
    n = max(1, round(ms / period_ms))
    wait = max(0, period_ms - press_ms)
    return ";".join(f"k:{key};w:{wait}" for _ in range(n))


@dataclass
class CycleConfig:
    interval_s: float = 600.0      # 再起動の間隔
    toggle_key: str = "home"
    potion_key: str = "w"
    period_ms: int = 50            # ポーション連打の間隔
    pre_ms: int = 500              # 切り替え前の連打
    gap_ms: int = 200              # 停止 → 再開の間 (この間も連打)
    post_ms: int = 500             # 再開後の連打
    check_s: float = 2.0           # 画面を見る間隔
    settle_s: float = 1.5          # Home のあと窓の表示が変わるまで待つ
    missing_s: float = 6.0         # 窓がこの時間見えなければ止まったとみなす
    backoff_s: float = 60.0        # 押しても直らないときに見張りを休む時間

    @classmethod
    def from_dict(cls, d: dict | None) -> "CycleConfig":
        d = d or {}
        return cls(**{k: type(getattr(cls, k))(v) for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class HelperCycle:
    cfg: CycleConfig
    run: callable                  # run(script) → ESP32 にスクリプトを送る (完了まで待つ)
    grab: callable                 # grab() → 画面 (BGR)
    detector: object               # .score(img) → Match
    active: callable = lambda: True  # ゲームが前面か (前面でないとキーが届かない)
    clock: callable = time.monotonic
    sleep: callable = time.sleep
    log: callable = print
    last_restart: float = field(default=None)
    missing_since: float | None = None
    backoff_until: float = 0.0
    restarts: int = 0
    recoveries: int = 0

    def __post_init__(self):
        if self.last_restart is None:
            self.last_restart = self.clock()

    # ---------------------------------------------------------------- scripts
    def _spam(self, ms: int) -> str:
        return potion_spam(ms, self.cfg.potion_key, self.cfg.period_ms)

    def restart_script(self) -> str:
        c = self.cfg
        k = f"k:{c.toggle_key}"
        return ";".join(["d:5", self._spam(c.pre_ms), k, self._spam(c.gap_ms), k, self._spam(c.post_ms)])

    def toggle_script(self) -> str:
        c = self.cfg
        return ";".join(["d:5", self._spam(c.pre_ms), f"k:{c.toggle_key}", self._spam(c.post_ms)])

    # ---------------------------------------------------------------- actions
    def _visible_after(self) -> bool:
        self.sleep(self.cfg.settle_s)
        m = self.detector.score(self.grab())
        return m.visible

    def restart(self) -> bool:
        """定期再起動。終わったら窓が出ているか確かめ、出ていなければ Home をもう一度。"""
        self.log("[cycle] MU Helper を再起動します (W 連打しながら Home → Home)")
        self.run(self.restart_script())
        self.restarts += 1
        self.last_restart = self.clock()
        if self._visible_after():
            self.log("[cycle] 再起動 OK (Hunting Log 表示中)")
            return True
        self.log("[cycle] 再起動後に Hunting Log が見えません。Home をもう一度押します")
        return self.recover(reason="再起動後")

    def recover(self, reason: str = "") -> bool:
        """止まった MU Helper を再開する。トグルなので、押して窓が出なければもう一度押して元に戻す。"""
        for attempt in (1, 2):
            self.log(f"[cycle] MU Helper を再開します{f' ({reason})' if reason else ''} (試行 {attempt})")
            self.run(self.toggle_script())
            if self._visible_after():
                self.recoveries += 1
                self.missing_since = None
                self.log("[cycle] 再開 OK (Hunting Log 表示中)")
                return True
        self.backoff_until = self.clock() + self.cfg.backoff_s
        self.log(f"[cycle] Hunting Log が見えないままです (窓を閉じた/隠れている?)。"
                 f"{self.cfg.backoff_s:.0f} 秒見張りを休みます")
        return False

    # ---------------------------------------------------------------- loop
    def step(self) -> str:
        """1 回分の見張り。何をしたかを返す (テスト用)。"""
        now = self.clock()
        if not self.active():
            return "inactive"
        img = self.grab()
        m = self.detector.score(img)
        if now - self.last_restart >= self.cfg.interval_s:
            self.restart()
            return "restart"
        if m.visible:
            self.missing_since = None
            return "on"
        if now < self.backoff_until:
            return "backoff"
        if self.missing_since is None:
            self.missing_since = now
            return "missing"
        if now - self.missing_since >= self.cfg.missing_s:
            self.missing_since = None
            self.recover(reason=f"{self.cfg.missing_s:.0f} 秒見えない")
            return "recover"
        return "missing"
