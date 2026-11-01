"""液晶ミラーを USB シリアル (有線) で ESP32 に送る (firmware の serial_stream。frame-stream-15 以降)。

Wi-Fi の代わりに使って、液晶が遅い原因が 通信路 (Wi-Fi) / ESP32 の展開 / 液晶の SPI のどれかを切り分ける。
akm/mirror.py の Mirror は TCP のソケットに sendall / recv(1) / close を使うので、同じ形にしてある
(Mirror(link_factory=...) で渡すと、展開・描画の命令はそのままで通信路だけが変わる)。

手順:
  1. 115200bps で開き、"stream <baud>" を送る → ESP32 は "OK STREAM <baud>" を返して速さを切り替える
  2. こちらも <baud> に切り替え、"AKMF" + 長さ + 本文 を送る
  3. 返事は 0xA5 0x5A + 'K' / 'R' / 'E' (同じ線にログの文字も流れてくるので、印で見分ける)
  4. 閉じるときは長さ 0 のフレームを送る (ESP32 は 115200bps に戻る。送らなくても 3 秒で戻る)

開くときは DTR/RTS を立てない (立てると ESP32 がリセットされる基板がある)。
"""
from __future__ import annotations

import struct
import time

ACK = b"\xa5\x5a"
DEFAULT_BAUD = 2_000_000


class SerialFrameLink:
    def __init__(self, port: str, baud: int = DEFAULT_BAUD, log=print, base_baud: int = 115200):
        import serial  # pyserial

        self.port, self.baud, self.log = port, int(baud), log
        ser = serial.Serial()
        ser.port, ser.baudrate, ser.timeout, ser.write_timeout = port, base_baud, 0.05, 5
        ser.dtr = False
        ser.rts = False
        ser.open()
        self.ser = ser
        self._buf = b""
        try:
            self._handshake()
        except Exception:
            ser.close()
            raise

    def _handshake(self) -> None:
        ser = self.ser
        time.sleep(0.1)
        ser.reset_input_buffer()
        for attempt in range(2):
            ser.write(f"\nstream {self.baud}\n".encode())
            deadline = time.monotonic() + 2.0
            got = b""
            while time.monotonic() < deadline:
                got += ser.read(256)
                if f"OK STREAM {self.baud}".encode() in got:
                    time.sleep(0.01)  # "OK" の残りを送り終えるのを待つ
                    ser.baudrate = self.baud
                    time.sleep(0.06)  # ESP32 が切り替えるのを待つ
                    ser.reset_input_buffer()
                    self.log(f"[serial] {self.port} を {self.baud:,} bps に切り替えました (有線で送ります)")
                    return
            if attempt == 0:
                # 前の接続の高速のままかもしれない: 3 秒待てば ESP32 は 115200bps に戻る
                time.sleep(3.2)
                ser.reset_input_buffer()
        text = got.decode("utf-8", "replace").strip()[-120:]
        raise ConnectionError(f"ESP32 が stream に応じません (ファームウェアが frame-stream-15 より古い?) 返事: {text!r}")

    # ---- Mirror から使う (socket と同じ形) -------------------------------------------
    def sendall(self, data: bytes) -> None:
        self.ser.write(data)

    def recv(self, n: int = 1) -> bytes:
        """返事 1 つ ('K' / 'R' / 'E') を待つ。3 秒来なければ b"" (Mirror は切断として扱う)"""
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            i = self._buf.find(ACK)
            if i >= 0 and len(self._buf) >= i + 3:
                code = self._buf[i + 2:i + 3]
                self._buf = self._buf[i + 3:]
                return code
            chunk = self.ser.read(max(1, self.ser.in_waiting))
            if chunk:
                self._buf = (self._buf + chunk)[-4096:]  # ログの文字が溜まりすぎないように
        return b""

    def setsockopt(self, *a) -> None:  # socket と同じ呼び方をされても困らないように
        pass

    def settimeout(self, *a) -> None:
        pass

    def close(self) -> None:
        try:
            self.ser.write(b"AKMF" + struct.pack("<I", 0))  # 終わりの合図 (ESP32 は 115200bps に戻る)
            self.ser.flush()
            time.sleep(0.05)
        except Exception:
            pass
        try:
            self.ser.close()
        except Exception:
            pass
