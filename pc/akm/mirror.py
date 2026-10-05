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
                 fmt: str = "jpeg", quality: int = 70, grab=None, transport: str = "auto", port: int = 5005,
                 pipeline: int = 2, b16_prev_free: bool = True, b16_bits: tuple[int, int, int] = (5, 6, 5)):
        self.enabled = enabled and bool(host)
        self.url = None
        if self.enabled:
            base = host if host.startswith("http") else f"http://{host}"
            self.url = f"{base}/frame"
        self.interval = interval_s
        self.fmt = fmt
        self.quality = quality
        self.grab = grab
        # tcp : 専用ポートに接続しっぱなしで流す (速い。ファームウェア frame-stream-3 以降)
        # http: 1 枚ずつ POST /frame (古いファームウェア) / auto: tcp を試し、だめなら http
        self.transport = transport
        self.port = port
        self.hostname = ""
        if host:
            from urllib.parse import urlparse

            self.hostname = urlparse(host if host.startswith("http") else f"http://{host}").hostname or host
        self._sock = None
        self._tcp_fail = 0
        # fmt="bc": BlockDiff (変わった 8x8 ブロックだけ送る)。画面全体が変わって大きくなるフレームは JPEG で送る
        self._bc = None
        self.bc_tolerance = 16
        self.sent_kinds = {"bc": 0, "jpeg": 0}
        self.bc_max_ratio = 1.5  # BlockDiff が JPEG のこの倍数を超えたら JPEG で送る
        self.cand_bytes = {"bc": 0.0, "jpeg": 0.0}  # 直近の平均サイズ (比較用)
        # fmt="bad16": BadCodec 16bit 版 (RGB565 の 16 ビット面を公式 BadCodec の命令で送る。akm/bad16.py)
        # ESP32 側に (面の数 + 1) x 4KB 要る (16 面で約 69KB)。足りないと 'E' が返るので、
        # 色のビット数を 1 段ずつ下げて (面を減らして) 送り直し、最後は JPEG に戻す
        self._b16 = None
        self.b16_bits: tuple[int, int, int] = tuple(b16_bits)  # R, G, B のビット数 (5, 6, 5 = 可逆)
        # True: 前フレーム不要版 (ver 3)。ESP32 は変わったブロックを直接液晶へ描くのでメモリ不足にならない。
        # False: 面ごとのフレーム (ver 2)。少し小さいが ESP32 に (面の数 + 1) x 4KB 要る
        self.b16_prev_free = b16_prev_free
        # 返事を待たずに先に送ってよい枚数 (bc / bad16)。ESP32 が展開・描画している間に次のフレームを
        # 送っておけるので、送信と展開が重なって速くなる。1 = 1 枚ずつ返事を待つ
        self.pipeline = pipeline
        self._inflight: list[tuple] = []  # 返事待ちのフレーム (形式, bad16 の色, キーフレームの世代)
        self._key_gen = 0  # 送ったキーフレームの数
        self.last_bytes = 0
        self._lock = threading.Lock()
        self._pending: bytes | None = None
        self._lines: list[tuple[str, tuple[int, int, int]]] = [("STANDBY", (0, 200, 255))]
        self._last_put = 0.0
        self.sent = 0
        self.errors = 0
        self.jpeg_errors = 0
        # 計測 (直近の平均, ms)
        self.t_capture = 0.0
        self.t_encode = 0.0
        self.t_post = 0.0
        self._fps_t0 = time.monotonic()
        self._fps_n = 0
        self.fps = 0.0
        self._last_warn = 0.0
        self._ok_once = False
        self._last_img: np.ndarray | None = None
        self._stop = threading.Event()
        self._thread = None
        if self.enabled:
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """送信スレッドを止める。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

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

    def _encode_bc(self, frame: np.ndarray) -> bytes:
        """BlockDiff で圧縮。JPEG の 1.5 倍を超えるなら JPEG にして、エンコーダの基準絵も JPEG の絵にする。"""
        from .bcodec import Encoder, _pad, blocks, to565

        if self._bc is None:
            self._bc = Encoder(self.bc_tolerance)
        data = self._bc.encode(frame)
        jpg = encode(frame, "jpeg", self.quality)
        for k, v in (("bc", len(data)), ("jpeg", len(jpg))):
            self.cand_bytes[k] = v if not self.cand_bytes[k] else 0.8 * self.cand_bytes[k] + 0.2 * v
        if len(data) > len(jpg) * self.bc_max_ratio:
            dec = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            self._bc.ref = blocks(to565(_pad(dec)))  # ESP32 には JPEG の絵が出る
            self.sent_kinds["jpeg"] += 1
            return jpg
        self.sent_kinds["bc"] += 1
        return data

    def _ok(self, n: int) -> None:
        self.sent += 1
        self.errors = 0
        self._fps_n += 1
        dt = time.monotonic() - self._fps_t0
        if dt >= 5:
            self.fps = self._fps_n / dt
            self._fps_n = 0
            self._fps_t0 = time.monotonic()
        if not self._ok_once:
            self._ok_once = True
            print(f"[mirror] 液晶への表示に成功 ({self.fmt}, {n} バイト)")

    def _capture(self) -> bytes:
        with self._lock:
            lines = list(self._lines)
        t0 = time.perf_counter()
        try:
            img = self.grab()
            self._last_img = img
        except Exception as e:
            from .screen import CaptureHidden

            if isinstance(e, CaptureHidden) and self._last_img is not None:
                # 別の仮想デスクトップに切り替えた: 最後に撮れた画面を暗くして出し続ける
                img = (self._last_img * 0.5).astype(np.uint8)
                lines = lines + [("GAME ON OTHER DESKTOP", (0, 200, 255))]
            else:  # ウィンドウが無い等: 黒画面に理由を出す
                img = np.zeros((H, W, 3), np.uint8)
                label = "GAME ON OTHER DESKTOP" if isinstance(e, CaptureHidden) else "NO GAME WINDOW"
                lines = lines + [(label, (0, 0, 255)), (str(e)[:38].encode("ascii", "replace").decode(), (200, 200, 200))]
        t1 = time.perf_counter()
        frame = compose(img, lines)
        if self.fmt == "bc" and self.transport in ("tcp", "auto"):
            data = self._encode_bc(frame)
        elif self.fmt == "bad16" and self.transport in ("tcp", "auto"):
            if self._b16 is None:
                from .bad16 import Encoder

                self._b16 = Encoder(bits=self.b16_bits, prev_free=self.b16_prev_free)
            data = self._b16.encode(frame)
        else:
            data = encode(frame, "jpeg" if self.fmt in ("bc", "bad16") else self.fmt, self.quality)
        t2 = time.perf_counter()
        self.t_capture = 0.8 * self.t_capture + 0.2 * (t1 - t0) * 1000
        self.t_encode = 0.8 * self.t_encode + 0.2 * (t2 - t1) * 1000
        self.last_bytes = len(data)
        return data

    def _send_tcp(self, data: bytes) -> None:
        """専用ポートへ送り、受信完了の 1 バイト ('K') を待つ。失敗したら例外。"""
        import socket
        import struct

        if self._sock is None:
            self._sock = socket.create_connection((self.hostname, self.port), timeout=3)
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._sock.settimeout(3)
            print(f"[mirror] 専用ポート {self.hostname}:{self.port} に接続しました (TCP で連続送信)")
        try:
            self._sock.sendall(b"AKMF" + struct.pack("<I", len(data)) + data)
            kind = "bad16" if data[:2] == b"B6" else ("bc" if data[:2] == b"BC" else "jpeg")
            if kind == "jpeg" or data[3] & 1:  # JPEG も全体を描くのでキーフレームと同じ
                self._key_gen += 1
            self._inflight.append((kind, self.b16_bits, self._key_gen))
            depth = self.pipeline if kind != "jpeg" else 1
            while len(self._inflight) >= max(1, depth):
                ack = self._sock.recv(1)
                sent_kind, sent_bits, gen = self._inflight.pop(0)
                if ack == b"R":
                    # 液晶が上書きされた / 展開できなかった: 次は全体を送る。
                    # その後に送ったキーフレームが既に向かっているなら要らない
                    if gen == self._key_gen:
                        self._request_key()
                elif ack == b"E":
                    # 先に送っていた分の 'E' で 2 段下げないよう、今の色で送ったフレームのときだけ下げる
                    if sent_kind == "bad16" and sent_bits == self.b16_bits:
                        self._b16_shrink()
                elif ack != b"K":
                    raise ConnectionError("ESP32 が切断しました")
        except Exception:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self._inflight.clear()
                self._request_key()  # つなぎ直したら全体から
            raise

    def _b16_shrink(self) -> None:
        """ESP32 にメモリが足りないと言われた: 面を減らす。もう減らせなければ JPEG にする。"""
        from .bad16 import LEVELS, planes_needed

        n = planes_needed(self.b16_bits)
        smaller = [lv for lv in LEVELS if planes_needed(lv) < n]
        self._b16 = None
        if self.fmt != "bad16" or not smaller:
            print("[mirror] ESP32 のメモリが足りず BadCodec 16bit 版を展開できません。JPEG に切り替えます")
            self.fmt = "jpeg"
            return
        self.b16_bits = smaller[0]
        r, g, b = self.b16_bits
        print(f"[mirror] ESP32 のメモリが足りないので面を減らします: {n} 面 → {sum(self.b16_bits)} 面 "
              f"(色 R{r}G{g}B{b} ビット)")

    def _request_key(self) -> None:
        for enc in (self._bc, self._b16):
            if enc is not None:
                enc.request_key()

    def _warn(self, msg: str) -> None:
        if time.monotonic() - self._last_warn > 30:  # 失敗は 30 秒に 1 回だけ表示し、送り続ける
            self._last_warn = time.monotonic()
            print(f"[mirror] ESP32 への画面送信に失敗: {msg} (送り続けます)")

    def _worker(self) -> None:
        import requests

        s = requests.Session()
        try:  # ESP32 のファームウェアが画面の受け取りを直した版か確認する
            fw = s.get(self.url.replace("/frame", "/status"), timeout=5).json().get("fw")
            if fw:
                print(f"[mirror] ESP32 ファームウェア: {fw}")
                import re

                m = re.match(r"frame-stream-(\d+)", fw)
                need = 9 if self.b16_prev_free else 7
                if self.fmt == "bad16" and (not m or int(m.group(1)) < need):
                    print(f"[mirror] BadCodec 16bit 版{'(前フレーム不要版)' if self.b16_prev_free else ''}には "
                          f"frame-stream-{need} 以降が必要です。ファームウェアを書き込み直してください")
            else:
                print("[mirror] ESP32 のファームウェアが古いです。画面が映らない場合は書き込み直してください (README 参照)")
        except Exception as e:
            print(f"[mirror] ESP32 の状態を取得できません: {e}")
        while not self._stop.is_set():
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
            if self.transport in ("tcp", "auto") and self.fmt in ("jpeg", "bc", "bad16"):
                try:
                    tp = time.perf_counter()
                    self._send_tcp(data)
                    self.t_post = 0.8 * self.t_post + 0.2 * (time.perf_counter() - tp) * 1000
                    self._ok(len(data))
                    self._tcp_fail = 0
                    if self.grab is not None:
                        time.sleep(max(0.0, self.interval - (time.monotonic() - t0)))
                    continue
                except Exception as e:
                    self.errors += 1
                    self._tcp_fail += 1
                    if self.transport == "auto" and self._tcp_fail >= 3:
                        print(f"[mirror] 専用ポートに接続できないので HTTP で送ります ({e})。"
                              "ファームウェアを frame-stream-3 以降にすると速くなります")
                        self.transport = "http"
                        if self.fmt in ("bc", "bad16"):
                            self.fmt = "jpeg"  # ブロック形式は専用ポートでしか送れない
                    else:
                        self._warn(f"専用ポート: {e}")
                    time.sleep(0.5)
                    continue
            try:
                tp = time.perf_counter()
                r = s.post(self.url, data=data, headers={"Content-Type": "application/octet-stream"}, timeout=5)
                self.t_post = 0.8 * self.t_post + 0.2 * (time.perf_counter() - tp) * 1000
                if r.status_code == 200:
                    self.sent += 1
                    self.errors = 0
                    self._fps_n += 1
                    dt = time.monotonic() - self._fps_t0
                    if dt >= 5:
                        self.fps = self._fps_n / dt
                        self._fps_n = 0
                        self._fps_t0 = time.monotonic()
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
