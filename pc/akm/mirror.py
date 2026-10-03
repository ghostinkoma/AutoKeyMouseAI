"""ゲーム画面を縮小して ESP32 の液晶 (240x135) に映す。

ボットの状態 (待機 / 動作中)・HP/MP・拾得数を上に重ねて描くので、
PageUp を押して動き出したかどうかが手元の液晶で分かる。

送信形式: POST http://<ESP32>/frame
  * jpeg (既定): 240x135 のベースライン JPEG。5〜10KB 程度で、ESP32 側は TJpg_Decoder で展開して表示する
  * raw        : RGB565 (ビッグエンディアン) 64,800 バイト
JPEG にすると送るデータが 1/8 程度になり、Wi-Fi が空くので HID 操作の遅れも減る。
240x135 の 1 枚を圧縮するのは CPU で 1ms 未満なので、GPU (NVENC) や FFmpeg は使わない
(GTX 660 の NVENC は H.264 しか出せず、ESP32 では H.264 を展開できない)。
送信は別スレッドで行い、ボットの判断ループを待たせない。
"""
from __future__ import annotations

import threading
import time

import cv2
import numpy as np

W, H = 240, 135


def to_rgb565_be(bgr: np.ndarray) -> bytes:
    b = bgr[:, :, 0].astype(np.uint16)
    g = bgr[:, :, 1].astype(np.uint16)
    r = bgr[:, :, 2].astype(np.uint16)
    v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
    return v.astype(">u2").tobytes()


def encode(bgr: np.ndarray, fmt: str = "jpeg", quality: int = 70) -> bytes:
    if fmt == "raw":
        return to_rgb565_be(bgr)
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise RuntimeError("jpeg encode failed")
    return buf.tobytes()


def compose(img: np.ndarray, lines: list[tuple[str, tuple[int, int, int]]]) -> np.ndarray:
    """ゲーム画面を 240x135 に収め (縦横比を保って余白は黒)、上に文字を重ねる。"""
    h, w = img.shape[:2]
    s = min(W / w, H / h)
    small = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
    out = np.zeros((H, W, 3), np.uint8)
    y0 = (H - small.shape[0]) // 2
    x0 = (W - small.shape[1]) // 2
    out[y0 : y0 + small.shape[0], x0 : x0 + small.shape[1]] = small
    y = 11
    for text, color in lines:
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.38, 1)
        cv2.rectangle(out, (0, y - th - 2), (tw + 4, y + 3), (0, 0, 0), -1)
        cv2.putText(out, text, (2, y), cv2.FONT_HERSHEY_SIMPLEX, 0.38, color, 1, cv2.LINE_AA)
        y += 13
    return out


class Mirror:
    """液晶へ画面を送る。

    grab (画面を撮る関数) を渡すと専用スレッドが自分で撮って送り続ける。
    ボットが待機中・停止中・ゲームが前面にないとき・仮想デスクトップを切り替えたときも途切れない。
    ボットは set_lines() で重ねる文字 (状態・HP など) を渡すだけ。
    """

    def __init__(self, host: str | None, interval_s: float = 1.0, enabled: bool = True,
                 fmt: str = "jpeg", quality: int = 70, grab=None):
        self.enabled = enabled and bool(host)
        self.url = None
        if self.enabled:
            base = host if host.startswith("http") else f"http://{host}"
            self.url = f"{base}/frame"
        self.interval = interval_s
        self.fmt = fmt
        self.quality = quality
        self.grab = grab
        self.last_bytes = 0
        self._lock = threading.Lock()
        self._pending: bytes | None = None
        self._lines: list[tuple[str, tuple[int, int, int]]] = [("STANDBY", (0, 200, 255))]
        self._last_put = 0.0
        self.sent = 0
        self.errors = 0
        self.jpeg_errors = 0
        self._last_warn = 0.0
        self._ok_once = False
        if self.enabled:
            threading.Thread(target=self._worker, daemon=True).start()

    def due(self) -> bool:
        return self.enabled and time.monotonic() - self._last_put >= self.interval

    def set_lines(self, lines: list[tuple[str, tuple[int, int, int]]]) -> None:
        """液晶に重ねる文字を更新する (撮影・送信はスレッドが行う)。"""
        with self._lock:
            self._lines = list(lines)

    def update(self, img: np.ndarray, lines: list[tuple[str, tuple[int, int, int]]]) -> None:
        """画面を渡して送る (grab を渡していない場合)。grab ありなら文字だけ更新する。"""
        if self.grab is not None:
            self.set_lines(lines)
            return
        if not self.due():
            return
        self._last_put = time.monotonic()
        data = encode(compose(img, lines), self.fmt, self.quality)
        self.last_bytes = len(data)
        with self._lock:
            self._pending = data

    def _capture(self) -> bytes:
        with self._lock:
            lines = list(self._lines)
        try:
            img = self.grab()
        except Exception as e:  # ウィンドウが無い等: 黒画面に理由を出す
            img = np.zeros((H, W, 3), np.uint8)
            lines = lines + [("NO GAME WINDOW", (0, 0, 255)), (str(e)[:38], (200, 200, 200))]
        data = encode(compose(img, lines), self.fmt, self.quality)
        self.last_bytes = len(data)
        return data

    def _warn(self, msg: str) -> None:
        if time.monotonic() - self._last_warn > 30:  # 失敗は 30 秒に 1 回だけ表示し、送り続ける
            self._last_warn = time.monotonic()
            print(f"[mirror] ESP32 への画面送信に失敗: {msg} (送り続けます)")

    def _worker(self) -> None:
        import requests

        s = requests.Session()
        while True:
            t0 = time.monotonic()
            if self.grab is not None:
                try:
                    data = self._capture()
                except Exception as e:
                    self._warn(f"画面の取得: {e}")
                    time.sleep(self.interval)
                    continue
            else:
                with self._lock:
                    data, self._pending = self._pending, None
                if data is None:
                    time.sleep(0.05)
                    continue
            try:
                r = s.post(self.url, data=data, headers={"Content-Type": "application/octet-stream"}, timeout=5)
                if r.status_code == 200:
                    self.sent += 1
                    self.errors = 0
                    if not self._ok_once:
                        self._ok_once = True
                        print(f"[mirror] 液晶への表示に成功 ({self.fmt}, {len(data)} バイト)")
                else:
                    self.errors += 1
                    msg = f"HTTP {r.status_code} {r.text.strip()}"
                    if self.fmt == "jpeg" and r.status_code == 400:
                        self.jpeg_errors += 1
                        if self.jpeg_errors >= 2:
                            # 古いファームウェア等で JPEG を展開できない: 非圧縮に切り替える
                            print(f"[mirror] ESP32 が JPEG を表示できないので raw (非圧縮) に切り替えます: {msg}")
                            self.fmt = "raw"
                    else:
                        self._warn(msg)
            except Exception as e:  # 通信エラーでボットは止めない
                self.errors += 1
                self._warn(str(e))
                s = requests.Session()  # 接続を張り直す
            if self.grab is not None:
                time.sleep(max(0.0, self.interval - (time.monotonic() - t0)))
