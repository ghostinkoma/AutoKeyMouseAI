"""動画を前もって BadCodec などに変換してファイル (.bad) にし、元の fps の時刻に合わせて ESP32 へ送る。

実時間で撮って変換すると、PC の処理の揺れでコマが落ちる。前もって変換しておけば、再生は「決まった時刻に
決まったデータを送るだけ」になる。変換の時点で、Wi-Fi で送れる量 (kbps) に収まるよう 1 枚ずつ調整する:
  * 量の残り (1 秒に kbps ずつ貯まる。上限は数枚分) より大きい 1 枚は出さず、次の 1 枚にまとめる
    (差分は「液晶に出ている絵」との差で作り直すので崩れない)。続けて max_skip 枚飛ばしたら、大きくても出す
  * key_every 秒ごとに画面全体 (キーフレーム) を入れる (遅れたとき・通信が途切れたときにそこから続ける)

流れ: (URL なら yt-dlp で取得) → FFmpeg で 240x135 (縦横比を保って黒帯)・BGR で受け取る → 符号化 → .bad

ファイル (.bad): 先頭 32 バイト
  'AKMV' ver(u8=1) 形式(u8) 幅(u16) 高さ(u16) fps(f32) 枚数(u32) 元の枚数(u32) 予約(10)
  続けて 1 枚ずつ: 時刻 ms (u32) 長さ (u32) 中身 (ESP32 にそのまま送るフレーム: 'B' '6' ... / 'B' 'J' ...)
  形式: 1 = インデックス 256 色 / 2 = BadCodec 16 ビット (565) / 3 = 白黒 1 ビット (8x8 ディザ) / 4 = JPEG (上下 2 つ)
"""
from __future__ import annotations

import copy
import shutil
import socket
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

W, H = 240, 135
MAGIC = b"AKMV"
HEAD = struct.Struct("<4sBBHHfII10x")
FRAME = struct.Struct("<II")
FORMATS = {"index": 1, "565": 2, "mono": 3, "jpeg": 4}
FORMAT_NAMES = {v: k for k, v in FORMATS.items()}
NEED_FW = {"index": 36, "565": 9, "mono": 37, "jpeg": 39}  # ESP32 のファームウェア frame-stream-N 以降


@dataclass
class Header:
    fmt: str
    fps: float
    frames: int = 0
    source_frames: int = 0
    width: int = W
    height: int = H


# ---------------------------------------------------------------- ファイル -----
class Writer:
    def __init__(self, path: Path, fmt: str, fps: float):
        self.path = Path(path)
        self.head = Header(fmt, float(fps))
        self.f = open(self.path, "wb")
        self.f.write(b"\0" * HEAD.size)  # 枚数は最後に書く

    def add(self, t_ms: int, data: bytes) -> None:
        self.f.write(FRAME.pack(int(t_ms), len(data)) + data)
        self.head.frames += 1

    def close(self, source_frames: int) -> None:
        self.head.source_frames = source_frames
        self.f.seek(0)
        h = self.head
        self.f.write(HEAD.pack(MAGIC, 1, FORMATS[h.fmt], h.width, h.height, h.fps, h.frames, h.source_frames))
        self.f.close()


def read_header(path: Path) -> Header:
    with open(path, "rb") as f:
        b = f.read(HEAD.size)
    if len(b) < HEAD.size:
        raise ValueError("ファイルが短すぎます")
    magic, ver, fmt, w, h, fps, n, src = HEAD.unpack(b)
    if magic != MAGIC or ver != 1 or fmt not in FORMAT_NAMES:
        raise ValueError(f"{path} は .bad (AKMV) のファイルではありません")
    return Header(FORMAT_NAMES[fmt], fps, n, src, w, h)


def read_frames(path: Path):
    """(時刻 ms, 中身) を順に返す"""
    with open(path, "rb") as f:
        f.seek(HEAD.size)
        while True:
            b = f.read(FRAME.size)
            if len(b) < FRAME.size:
                return
            t, n = FRAME.unpack(b)
            data = f.read(n)
            if len(data) < n:
                return
            yield t, data


# ---------------------------------------------------------------- 取得・展開 -----
def download(url: str, out_dir: Path, log=print) -> Path:
    """yt-dlp で動画 (映像だけ、480p 以下) を取得してファイルの場所を返す"""
    out_dir.mkdir(parents=True, exist_ok=True)
    tmpl = str(out_dir / "%(title).80s [%(id)s].%(ext)s")
    fmt = "bv*[height<=480]/b[height<=480]/bv*/b"
    try:
        import yt_dlp

        opts = {"format": fmt, "outtmpl": tmpl, "quiet": True, "noprogress": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return Path(ydl.prepare_filename(info))
    except ImportError:
        exe = shutil.which("yt-dlp")
        if not exe:
            raise RuntimeError("yt-dlp がありません (pip install yt-dlp)")
        log(f"yt-dlp で取得しています: {url}")
        r = subprocess.run([exe, "-f", fmt, "-o", tmpl, "--print", "after_move:filepath", url],
                           capture_output=True, text=True, check=True)
        return Path(r.stdout.strip().splitlines()[-1])


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg が見つかりません (ffmpeg.exe を PATH の通った場所に置いてください)")
    return exe


def probe_fps(path: Path) -> float:
    """動画の fps (取れなければ 30)"""
    exe = shutil.which("ffprobe")
    if not exe:
        return 30.0
    r = subprocess.run([exe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate,r_frame_rate",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    for tok in r.stdout.replace("\n", ",").split(","):
        if "/" in tok:
            a, b = tok.split("/")
            if float(b or 0) > 0 and float(a) > 0:
                return float(a) / float(b)
    return 30.0


def frames_of(path: Path, fps: float, start: float = 0.0, duration: float = 0.0):
    """FFmpeg で 240x135 の BGR の絵を 1 枚ずつ返す (縦横比を保って黒帯)"""
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=area,"
          f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps={fps:g}")
    cmd = [ffmpeg_exe(), "-v", "error"]
    if start > 0:
        cmd += ["-ss", f"{start:g}"]
    cmd += ["-i", str(path)]
    if duration > 0:
        cmd += ["-t", f"{duration:g}"]
    cmd += ["-an", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    n = W * H * 3
    try:
        while True:
            b = p.stdout.read(n)
            if len(b) < n:
                break
            yield np.frombuffer(b, np.uint8).reshape(H, W, 3)
    finally:
        p.stdout.close()
        p.wait()


# ---------------------------------------------------------------- 変換 -----
def make_encoder(fmt: str):
    """1 枚 (240x135 BGR) → ESP32 に送るフレーム、の関数と、状態を持つ符号化器 (無ければ None)"""
    from .bad16 import Encoder, IndexEncoder

    if fmt == "index":
        enc = IndexEncoder()
        return enc, enc.encode
    if fmt == "565":
        enc = Encoder(bits=(5, 6, 5))
        return enc, enc.encode
    if fmt == "mono":
        import cv2

        from .gate_live import to_mono

        enc = Encoder(bits=(1, 1, 1))
        return enc, lambda f: enc.encode(cv2.cvtColor(to_mono(f, 128, "8x8"), cv2.COLOR_GRAY2BGR))
    if fmt == "jpeg":
        from .mirror import encode_jpeg_split

        return None, lambda f: encode_jpeg_split(f, 70)
    raise ValueError(f"形式: {fmt} (index / 565 / mono / jpeg)")


@dataclass
class EncodeStats:
    source: int = 0
    written: int = 0
    skipped: int = 0
    keys: int = 0
    bytes: int = 0
    biggest: int = 0


def encode_frames(frames, fmt: str, fps: float, kbps: float, writer: Writer, key_every: float = 3.0,
                  max_skip: int = 6, burst: float = 3.0, log=None) -> EncodeStats:
    """1 枚ずつ符号化して writer に足す。1 秒に kbps KB ずつ貯まる量 (上限は burst 枚分) を超える 1 枚は出さない
    (状態を戻して次の 1 枚にまとめる)。kbps <= 0 なら調整しない"""
    enc, fn = make_encoder(fmt)
    budget = kbps * 1024 / fps if kbps > 0 else float("inf")
    cap = budget * burst
    credit = cap
    st = EncodeStats()
    run = 0  # 続けて飛ばした枚数
    next_key = 0.0
    for i, f in enumerate(frames):
        st.source += 1
        t = i / fps
        credit = min(cap, credit + budget)
        if enc is not None and t >= next_key:
            enc.request_key()  # 一定間隔で画面全体 (途中から再生するとき用)
            next_key = t + key_every
        saved = copy.deepcopy(enc) if enc is not None and budget != float("inf") else None
        data = fn(f)
        if len(data) > credit and run < max_skip:
            st.skipped += 1
            run += 1
            if saved is not None:  # 出さない: 状態を戻す (次の 1 枚は、液晶に出ている絵との差になる)
                enc.__dict__.update(saved.__dict__)
            continue
        run = 0
        credit -= len(data)
        writer.add(round(t * 1000), data)
        st.written += 1
        st.bytes += len(data)
        st.biggest = max(st.biggest, len(data))
        if data[:2] == b"B6" and data[3] & 1:
            st.keys += 1
        if log and st.source % 240 == 0:
            log(f"  {st.source} 枚 ({t:.0f} 秒): 書いた {st.written} 飛ばした {st.skipped} "
                f"平均 {st.bytes / max(1, st.written) / 1024:.1f}KB")
    return st


def encode_file(src: Path, out: Path, fmt: str = "index", fps: float = 0.0, kbps: float = 340.0,
                start: float = 0.0, duration: float = 0.0, key_every: float = 3.0, log=print) -> EncodeStats:
    fps = fps or probe_fps(src)
    log(f"変換: {src.name} → {out.name} ({fmt}, {fps:.3f}fps, 上限 {kbps:g}KB/s)")
    w = Writer(out, fmt, fps)
    t0 = time.monotonic()
    st = EncodeStats()
    try:
        st = encode_frames(frames_of(src, fps, start, duration), fmt, fps, kbps, w, key_every, log=log)
    finally:
        w.close(st.source)
    dt = time.monotonic() - t0
    sec = st.source / fps
    log(f"できました: {st.source} 枚 ({sec:.0f} 秒) のうち {st.written} 枚 (飛ばした {st.skipped})、"
        f"{st.bytes / 1024 / 1024:.1f}MB、平均 {st.bytes / max(1, sec) / 1024:.0f}KB/s・最大 1 枚 {st.biggest / 1024:.1f}KB、"
        f"変換 {dt:.0f} 秒")
    return st


# ---------------------------------------------------------------- 再生 -----
@dataclass
class PlayStats:
    sent: int = 0
    bytes: int = 0
    late_ms: float = 0.0    # 予定の時刻より遅れて送った分 (最大)
    resets: int = 0         # ESP32 が「全体が欲しい」と言った回数
    skipped_to_key: int = 0  # 遅れすぎて、次の画面全体まで飛ばした回数
    reconnects: int = 0     # 通信が途切れて、つなぎ直した回数


def check_firmware(host: str, fmt: str, log=print) -> None:
    import json
    import re
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}/status", timeout=3) as r:
            fw = json.loads(r.read().decode("utf-8", "replace")).get("fw", "")
    except Exception as e:  # noqa: BLE001
        log(f"ESP32 の版を確かめられません ({e})。そのまま送ります")
        return
    m = re.match(r"frame-stream-(\d+)", fw or "")
    need = NEED_FW[fmt]
    log(f"ESP32 ファームウェア: {fw}")
    if not m or int(m.group(1)) < need:
        raise RuntimeError(f"この形式 ({fmt}) には frame-stream-{need} 以降が必要です (今: {fw or '不明'})")


def index_frames(path: Path) -> list[tuple[int, int, int, bool]]:
    """(時刻 ms, ファイルの位置, 長さ, 画面全体か) の一覧 (中身は読まない。途中から送り直すとき用)"""
    out = []
    with open(path, "rb") as f:
        pos = HEAD.size
        while True:
            f.seek(pos)
            b = f.read(FRAME.size + 4)
            if len(b) < FRAME.size:
                break
            t, n = FRAME.unpack(b[:FRAME.size])
            d = b[FRAME.size:]
            key = d[:2] != b"B6" or (len(d) >= 4 and bool(d[3] & 1))  # JPEG ('B' 'J' / FF D8) は毎回全体
            out.append((t, pos + FRAME.size, n, key))
            pos += FRAME.size + n
    return out


def play(path: Path, host: str, port: int = 5005, loop: bool = False, speed: float = 1.0, pipeline: int = 2,
         catch_up_ms: float = 500.0, log=print, stop=None) -> PlayStats:
    """.bad を元の時刻に合わせて ESP32 へ送る (今のミラーと同じ TCP の送り方: 'AKMF' 長さ 中身 → 返事 1 バイト)。
    差分のフレームは途中を飛ばせないので、遅れが catch_up_ms を超えたら、今の時刻より後の画面全体の 1 枚まで
    飛ばして追いつく。返事が途切れた・切れたら、つなぎ直して次の画面全体の 1 枚から続ける"""
    h = read_header(path)
    check_firmware(host, h.fmt, log)
    frames = index_frames(path)
    if not frames:
        raise RuntimeError("フレームがありません")
    keys = [i for i, fr in enumerate(frames) if fr[3]]
    st = PlayStats()
    log(f"再生: {path.name} ({h.fmt}, {h.fps:.3f}fps, {h.frames} 枚 / 元 {h.source_frames} 枚、"
        f"画面全体 {len(keys)} 枚) → {host}:{port}")
    sock = None
    inflight = 0

    def connect():
        nonlocal sock, inflight
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        sock = socket.create_connection((host, port), timeout=5)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        inflight = 0

    def ack():
        nonlocal inflight
        b = sock.recv(1)
        inflight -= 1
        if b == b"R":
            st.resets += 1
        elif b == b"E":
            raise RuntimeError("ESP32 がこの形式を受け付けません (メモリ不足)")
        elif not b:
            raise ConnectionError("ESP32 が接続を切りました")

    def next_key(i: int, t_ms: float) -> int | None:
        """i 以降で、時刻 t_ms 以後の最初の画面全体の 1 枚"""
        for k in keys:
            if k >= i and frames[k][0] >= t_ms:
                return k
        return None

    connect()
    f = open(path, "rb")
    try:
        while True:
            t0 = time.monotonic()
            win_t, win_n, win_b = t0, 0, 0
            i = 0
            while i < len(frames):
                if stop is not None and stop():
                    return st
                t_ms, pos, n, _key = frames[i]
                due = t0 + t_ms / 1000.0 / speed
                wait = due - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                elif -wait * 1000 > catch_up_ms:  # 遅れすぎ: 今の時刻より後の画面全体まで飛ばす
                    now_ms = (time.monotonic() - t0) * 1000 * speed
                    k = next_key(i + 1, now_ms)
                    if k is not None:
                        st.skipped_to_key += 1
                        i = k
                        continue
                    st.late_ms = max(st.late_ms, -wait * 1000)
                else:
                    st.late_ms = max(st.late_ms, -wait * 1000)
                f.seek(pos)
                data = f.read(n)
                try:
                    sock.sendall(b"AKMF" + struct.pack("<I", len(data)) + data)
                    inflight += 1
                    while inflight >= max(1, pipeline):
                        ack()
                except (OSError, ConnectionError) as e:  # 途切れた: つなぎ直して、次の画面全体から
                    st.reconnects += 1
                    log(f"  ESP32 との通信が途切れました ({e})。つなぎ直します")
                    for n_try in range(10):  # すぐ 1 回、だめなら 1 秒おきに
                        try:
                            time.sleep(0.2 if n_try == 0 else 1.0)
                            connect()
                            break
                        except OSError:
                            continue
                    else:
                        raise
                    now_ms = (time.monotonic() - t0) * 1000 * speed
                    k = next_key(i + 1, now_ms)
                    if k is None:
                        break
                    i = k
                    continue
                st.sent += 1
                st.bytes += len(data)
                win_n += 1
                win_b += len(data)
                now = time.monotonic()
                if now - win_t >= 10:
                    lag = max(0.0, now - due) * 1000
                    log(f"  {t_ms / 1000:6.1f} 秒: {win_n / (now - win_t):5.1f}fps {win_b / (now - win_t) / 1024:5.0f}KB/s "
                        f"遅れ {lag:4.0f}ms" + (f" 追いついた {st.skipped_to_key} 回" if st.skipped_to_key else ""))
                    win_t, win_n, win_b = now, 0, 0
                i += 1
            if not loop:
                break
        while inflight:
            ack()
    finally:
        f.close()
        if sock is not None:
            sock.close()
    return st
