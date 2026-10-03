"""ESP32 (AutoKeyMouse firmware) との通信。

経路は 2 つ:
  * serial: USB の COM ポート。低遅延。``run <script>`` を送って ``OK`` / ``ERR`` を待つ
  * http  : Wi-Fi。``GET /run?s=<script>``

スクリプト書式は firmware/AutoKeyMouse/macro.h を参照。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

ABS_MAX = 32767


class DeviceError(RuntimeError):
    pass


@dataclass
class AbsMap:
    """スクリーン座標 (px) → HID 絶対座標 (0..32767) の線形変換。

    hid = (px - origin) * scale。tools/calibrate_mouse.py で実測して求める。
    初期値はプライマリモニタ全体に正規化される前提。
    """

    origin_x: float = 0.0
    origin_y: float = 0.0
    scale_x: float = 1.0
    scale_y: float = 1.0

    def reachable(self, x: float, y: float) -> bool:
        """絶対座標マウスでその位置まで動かせるか (例: プライマリモニタだけに対応している場合の別モニタは不可)"""
        hx = (x - self.origin_x) * self.scale_x
        hy = (y - self.origin_y) * self.scale_y
        return -0.5 <= hx <= ABS_MAX + 0.5 and -0.5 <= hy <= ABS_MAX + 0.5

    def to_hid(self, x: float, y: float) -> tuple[int, int]:
        hx = round((x - self.origin_x) * self.scale_x)
        hy = round((y - self.origin_y) * self.scale_y)
        return max(0, min(ABS_MAX, hx)), max(0, min(ABS_MAX, hy))

    @classmethod
    def for_screen(cls, width: int, height: int) -> "AbsMap":
        return cls(0.0, 0.0, ABS_MAX / max(1, width - 1), ABS_MAX / max(1, height - 1))


class Transport:
    def run(self, script: str, timeout: float = 35.0) -> str:
        raise NotImplementedError

    def close(self) -> None:
        pass


class SerialTransport(Transport):
    def __init__(self, port: str, baud: int = 115200):
        import serial  # pyserial

        self._ser = serial.Serial()
        self._ser.port = port
        self._ser.baudrate = baud
        self._ser.timeout = 0.1
        # 開いた瞬間に DTR/RTS で ESP32 がリセットされるのを避ける
        self._ser.dtr = False
        self._ser.rts = False
        self._ser.open()
        self._lock = threading.Lock()
        time.sleep(0.2)
        self._ser.reset_input_buffer()

    def _command(self, line: str, timeout: float) -> str:
        with self._lock:
            self._ser.reset_input_buffer()
            self._ser.write((line + "\n").encode("utf-8"))
            deadline = time.monotonic() + timeout
            buf = b""
            while time.monotonic() < deadline:
                buf += self._ser.read(256)
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    text = raw.decode("utf-8", "replace").strip()
                    if text.startswith("OK"):
                        return text[2:].strip()
                    if text.startswith("ERR"):
                        raise DeviceError(text[3:].strip())
                    # それ以外はファームウェアのログ行なので読み捨てる
            raise DeviceError(f"serial timeout: {line[:40]}")

    def run(self, script: str, timeout: float = 35.0) -> str:
        if "\n" in script:
            script = script.replace("\n", ";")
        return self._command("run " + script, timeout)

    def status(self) -> str:
        return self._command("status", 3.0)

    def stop(self) -> None:
        # stop は同期実行中でも割り込めるようロックを取らずに送る
        self._ser.write(b"stop\n")

    def close(self) -> None:
        self._ser.close()


class HttpTransport(Transport):
    def __init__(self, host: str):
        import requests

        self._base = host if host.startswith("http") else f"http://{host}"
        self._s = requests.Session()

    def run(self, script: str, timeout: float = 35.0) -> str:
        r = self._s.post(f"{self._base}/run", data={"s": script}, timeout=timeout)
        if r.status_code != 200:
            raise DeviceError(f"HTTP {r.status_code}: {r.text.strip()}")
        return r.text.strip()

    def status(self) -> dict:
        return self._s.get(f"{self._base}/status", timeout=3).json()

    def stop(self) -> None:
        self._s.get(f"{self._base}/stop", timeout=3)


class Device:
    """HID 操作の高水準 API。座標はすべてスクリーン座標 (物理ピクセル)。

    mouse_mode:
      absolute : 絶対座標マウスで一発移動 (Windows はプライマリモニタにだけ対応させることが多い)
      relative : 相対移動 + 実際のカーソル位置を読んで合わせ込む (どのモニタでも動くが遅い)
      auto     : 絶対座標で届く範囲は absolute、届かない位置 (別モニタ等) は relative
    """

    def __init__(
        self,
        transport: Transport,
        abs_map: AbsMap,
        dry_run: bool = False,
        mouse_mode: str = "auto",
        cursor=None,
    ):
        self.t = transport
        self.abs_map = abs_map
        self.dry_run = dry_run
        self.mouse_mode = mouse_mode
        self.cursor = cursor  # () -> (x, y) 実際のカーソル位置。relative に必要

    # -- 低水準
    def run(self, script: str, timeout: float = 35.0) -> str:
        if self.dry_run:
            print(f"[dry-run] {script}")
            return "ok"
        return self.t.run(script, timeout)

    def stop(self) -> None:
        if not self.dry_run:
            self.t.stop()

    def move_relative(self, x: float, y: float, tolerance: int = 2, max_steps: int = 8) -> bool:
        """相対移動を繰り返して (x, y) に合わせる。マウス加速があっても数回で収束する。"""
        if self.cursor is None:
            raise DeviceError("relative mouse mode needs a cursor position reader")
        for _ in range(max_steps):
            cx, cy = self.cursor()
            dx, dy = round(x - cx), round(y - cy)
            if abs(dx) <= tolerance and abs(dy) <= tolerance:
                return True
            if self.dry_run:
                print(f"[dry-run] m:{dx},{dy}")
                return True
            self.run(f"m:{dx},{dy}")
        return False

    def _use_relative(self, x: float, y: float) -> bool:
        if self.mouse_mode == "relative":
            return True
        if self.mouse_mode == "auto" and self.cursor is not None:
            return not self.abs_map.reachable(x, y)
        return False

    # -- スクリプト断片 (組み合わせて 1 回で送ると速い)
    def s_move(self, x: float, y: float) -> str:
        """移動コマンドを返す。相対移動が必要な位置ならここで移動を済ませて空文字を返す。"""
        if self._use_relative(x, y):
            self.move_relative(x, y)
            return ""
        hx, hy = self.abs_map.to_hid(x, y)
        return f"a:{hx},{hy}"

    @staticmethod
    def s_key(key: str) -> str:
        return f"k:{key}"

    # -- よく使う操作
    def move(self, x: float, y: float) -> None:
        cmd = self.s_move(x, y)
        if cmd:
            self.run(cmd)

    def click(self, x: float, y: float, button: str = "L", settle_ms: int = 30) -> None:
        self.run(f"{self.s_move(x, y)};w:{settle_ms};c:{button}")

    def key(self, key: str) -> None:
        self.run(self.s_key(key))

    def release_all(self) -> None:
        self.run("ra")


def open_device(cfg: dict, abs_map: AbsMap, dry_run: bool = False) -> Device:
    from .screen import IS_WINDOWS, cursor_pos

    dev_cfg = cfg["device"]
    mode = (cfg.get("mouse") or {}).get("mode", "auto")
    cursor = cursor_pos if IS_WINDOWS else None
    if dry_run:
        return Device(Transport(), abs_map, dry_run=True, mouse_mode=mode, cursor=cursor)
    if dev_cfg.get("transport", "serial") == "serial":
        t: Transport = SerialTransport(dev_cfg["port"], int(dev_cfg.get("baud", 115200)))
    else:
        t = HttpTransport(dev_cfg["host"])
    return Device(t, abs_map, mouse_mode=mode, cursor=cursor)
