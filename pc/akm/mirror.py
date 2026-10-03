"""ゲーム画面を縮小して ESP32 の液晶 (240x135) に映す。

ボットの状態 (待機 / 動作中)・HP/MP・拾得数を上に重ねて描くので、
PageUp を押して動き出したかどうかが手元の液晶で分かる。

送信形式: POST http://<ESP32>/frame
  * jpeg (既定): 240x135 のベースライン JPEG。5〜10KB 程度で、ESP32 の ROM 内蔵 JPEG デコーダで表示する
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
    def __init__(self, host: str | None, interval_s: float = 1.0, enabled: bool = True,
                 fmt: str = "jpeg", quality: int = 70):
        self.enabled = enabled and bool(host)
        self.url = None
        if self.enabled:
            base = host if host.startswith("http") else f"http://{host}"
            self.url = f"{base}/frame"
        self.interval = interval_s
        self.fmt = fmt
        self.quality = quality
        self.last_bytes = 0
        self._lock = threading.Lock()
        self._pending: bytes | None = None
        self._last_put = 0.0
        self.sent = 0
        self.errors = 0
        self._warned = False
        if self.enabled:
            threading.Thread(target=self._worker, daemon=True).start()

    def due(self) -> bool:
        return self.enabled and time.monotonic() - self._last_put >= self.interval

    def update(self, img: np.ndarray, lines: list[tuple[str, tuple[int, int, int]]]) -> None:
        """最新の画面を渡す (間隔より早く呼ばれても間引く)。"""
        if not self.due():
            return
        self._last_put = time.monotonic()
        data = encode(compose(img, lines), self.fmt, self.quality)
        self.last_bytes = len(data)
        with self._lock:
            self._pending = data

    def _worker(self) -> None:
        import requests

        s = requests.Session()
        while True:
            with self._lock:
                data, self._pending = self._pending, None
            if data is None:
                time.sleep(0.05)
                continue
            try:
                r = s.post(self.url, data=data, headers={"Content-Type": "application/octet-stream"}, timeout=5)
                if r.status_code == 200:
                    self.sent += 1
                    continue
                msg = f"HTTP {r.status_code} {r.text.strip()}"
            except Exception as e:  # 通信エラーでボットは止めない
                msg = str(e)
            self.errors += 1
            if self.fmt == "jpeg" and self.errors >= 2:
                # JPEG で ESP32 が応答しなくなる (古いファームウェア等) 場合は非圧縮に切り替える
                print(f"[mirror] JPEG 送信に失敗が続いたので raw (非圧縮) に切り替えます: {msg}")
                self.fmt = "raw"
                self.errors = 0
                time.sleep(2)
                continue
            if not self._warned:
                print(f"[mirror] ESP32 への画面送信に失敗: {msg} (ファームウェアを更新してください)")
                self._warned = True
            time.sleep(2)
