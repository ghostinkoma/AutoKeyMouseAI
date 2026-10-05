"""ゲームウィンドウの検出と画面キャプチャ (Windows)。

座標系はすべて物理ピクセル。高 DPI 環境でもずれないよう、import 時にプロセスを
Per-Monitor DPI Aware にする。
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from dataclasses import dataclass

import numpy as np

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # Windows 8 以前
        user32.SetProcessDPIAware()


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return self.left + self.width // 2, self.top + self.height // 2


def primary_screen_size() -> tuple[int, int]:
    if not IS_WINDOWS:
        return 1920, 1080
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def virtual_screen_rect() -> Rect:
    """全モニタを合わせた仮想スクリーン (左や上のモニタは負の座標になる)"""
    if not IS_WINDOWS:
        return Rect(0, 0, 1920, 1080)
    return Rect(user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
                user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))


def cursor_pos() -> tuple[int, int]:
    if not IS_WINDOWS:
        return 0, 0
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def key_pressed(vk: int) -> bool:
    """PC 側キーボードの状態 (緊急停止キー用)。"""
    if not IS_WINDOWS:
        return False
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def window_pid(hwnd: int) -> int:
    if not IS_WINDOWS:
        return 0
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_name(hwnd: int) -> str:
    """ウィンドウを持つプロセスの実行ファイル名 (例: main.exe)。取れなければ空文字。"""
    if not IS_WINDOWS:
        return ""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    kernel32 = ctypes.windll.kernel32
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.replace("/", "\\").split("\\")[-1]
        return ""
    finally:
        kernel32.CloseHandle(h)


def list_windows() -> list[tuple[int, str, str, "Rect"]]:
    """可視でタイトルのあるウィンドウ一覧: (hwnd, タイトル, プロセス名, クライアント領域)"""
    if not IS_WINDOWS:
        return []
    out: list[tuple[int, str, str, Rect]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                out.append((hwnd, buf.value, process_name(hwnd), client_rect(hwnd)))
        return True

    user32.EnumWindows(cb, 0)
    return out


def find_window(title_contains: str, process: str | None = None) -> int | None:
    """ゲームウィンドウを探す。

    process (例: "main.exe") を指定するとそのプロセスのウィンドウだけを対象にする。
    ブラウザのタブ名などに同じ文字列が入っていても取り違えない。
    候補が複数ならクライアント領域が一番大きいものを選ぶ。
    """
    best, best_area = None, -1
    for hwnd, title, proc, rect in list_windows():
        if title_contains and title_contains.lower() not in title.lower():
            continue
        if process and proc.lower() != process.lower():
            continue
        area = rect.width * rect.height
        if area > best_area:
            best, best_area = hwnd, area
    return best


def client_rect(hwnd: int) -> Rect:
    """ウィンドウのクライアント領域 (枠・タイトルバーを除く) のスクリーン座標。"""
    rc = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return Rect(pt.x, pt.y, rc.right - rc.left, rc.bottom - rc.top)


_gdi_ready = False


def _setup_gdi() -> None:
    """64bit でハンドルが切り詰められないよう、使う API の型を宣言する。"""
    global _gdi_ready
    if _gdi_ready:
        return
    gdi32 = ctypes.windll.gdi32
    H = ctypes.c_void_p
    user32.GetDC.restype = H
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, H]
    user32.PrintWindow.argtypes = [wintypes.HWND, H, wintypes.UINT]
    gdi32.CreateCompatibleDC.restype = H
    gdi32.CreateCompatibleDC.argtypes = [H]
    gdi32.CreateCompatibleBitmap.restype = H
    gdi32.CreateCompatibleBitmap.argtypes = [H, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = H
    gdi32.SelectObject.argtypes = [H, H]
    gdi32.DeleteObject.argtypes = [H]
    gdi32.DeleteDC.argtypes = [H]
    gdi32.GetDIBits.argtypes = [H, H, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    _gdi_ready = True


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


def capture_window(hwnd: int) -> np.ndarray | None:
    """ウィンドウのクライアント領域をウィンドウ自身に描かせて取得する (PrintWindow)。

    画面に映っていなくても撮れるので、仮想デスクトップを切り替えたり他のウィンドウが
    上に重なったりしてもゲーム画面が取れる。最小化中や失敗時は None。
    """
    if not IS_WINDOWS:
        return None
    _setup_gdi()
    gdi32 = ctypes.windll.gdi32
    rc = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    w, h = rc.right - rc.left, rc.bottom - rc.top
    if w <= 0 or h <= 0:
        return None
    hdc = user32.GetDC(hwnd)
    if not hdc:
        return None
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        # 1 = PW_CLIENTONLY, 2 = PW_RENDERFULLCONTENT (DirectX のゲームも撮れる。Windows 8.1 以降)
        ok = user32.PrintWindow(hwnd, mem, 3)
        gdi32.SelectObject(mem, old)  # GetDIBits の前に DC から外す
        old = None
        if not ok:
            return None
        bmi = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0):
            return None
        return np.frombuffer(buf, np.uint8).reshape(h, w, 4)[:, :, :3].copy()
    finally:
        if old is not None:
            gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, hdc)


def client_offset_in_window(hwnd: int) -> tuple[int, int, int, int]:
    """Windows Graphics Capture が返すウィンドウ画像の中で、クライアント領域がどこにあるか (x, y, w, h)。"""
    rc = wintypes.RECT()
    # DWMWA_EXTENDED_FRAME_BOUNDS = 9: 影を除いた見た目の枠 (WGC の画像はこの範囲)
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 9, ctypes.byref(rc), ctypes.sizeof(rc)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(rc))
    c = client_rect(hwnd)
    return c.left - rc.left, c.top - rc.top, c.width, c.height


class WgcCapture:
    """Windows Graphics Capture (windows-capture パッケージ) でウィンドウを撮り続ける。

    OpenGL / DirectX のゲームでも撮れて、他のウィンドウが重なったり仮想デスクトップを
    切り替えたりしても撮れる。最新の 1 枚を保持し、latest() で取り出す。
    """

    def __init__(self, hwnd: int):
        from windows_capture import WindowsCapture  # 無ければ ImportError

        self.hwnd = hwnd
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._t = 0.0
        self.closed = False
        kw = dict(cursor_capture=False, window_hwnd=hwnd, minimum_update_interval=50)
        try:
            self.control = self._start(WindowsCapture(draw_border=False, **kw))
        except Exception as e:
            # 枠の非表示は Windows 11 のみ対応 (Windows 10 では開始時に例外になる): 枠ありでやり直す
            if "border" not in str(e).lower():
                raise
            self.control = self._start(WindowsCapture(**kw))

    def _start(self, cap):
        @cap.event
        def on_frame_arrived(frame, control):
            if self.closed:
                control.stop()
                return
            img = np.ascontiguousarray(frame.frame_buffer[:, :, :3])
            with self._lock:
                self._frame = img
                self._t = time.monotonic()

        @cap.event
        def on_closed():
            self.closed = True

        return cap.start_free_threaded()

    def latest(self, max_age_s: float = 3.0) -> np.ndarray | None:
        """クライアント領域だけを切り出した最新画像。古い / 無ければ None。"""
        with self._lock:
            img, t = self._frame, self._t
        if img is None or time.monotonic() - t > max_age_s:
            return None
        x, y, w, h = client_offset_in_window(self.hwnd)
        if w <= 0 or h <= 0:
            return None
        x, y = max(0, x), max(0, y)
        out = img[y : y + h, x : x + w]
        return out if out.shape[0] >= h // 2 and out.shape[1] >= w // 2 else img

    def stop(self) -> None:
        self.closed = True
        try:
            self.control.stop()
        except Exception:
            pass


def is_cloaked(hwnd: int) -> bool:
    """ウィンドウが別の仮想デスクトップにある (Windows が描画を止めている) か。"""
    if not IS_WINDOWS:
        return False
    v = ctypes.c_int(0)
    # DWMWA_CLOAKED = 14
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 14, ctypes.byref(v), ctypes.sizeof(v)) != 0:
        return False
    return v.value != 0


class CaptureHidden(RuntimeError):
    """ゲームが別の仮想デスクトップにあり、画面からは撮れない。"""


class ObsCapture:
    """OBS Studio の「ゲームキャプチャ」ソースから画像をもらう (obs-websocket 経由)。

    ゲームキャプチャはゲームの描画そのものを取り出すので、ゲームを別の仮想デスクトップに
    移しても撮り続けられる。OBS 側で WebSocket サーバーを有効にし、ゲームキャプチャのソースを作っておく。
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.source = str(cfg.get("source", "MU"))
        self._local = threading.local()  # 接続はスレッドごと

    def _client(self):
        c = getattr(self._local, "client", None)
        if c is None:
            import logging

            import obsws_python as obs  # pip install obsws-python

            logging.getLogger("obsws_python").setLevel(logging.CRITICAL)  # 失敗時のトレースバックを出さない
            c = self._local.client = obs.ReqClient(
                host=self.cfg.get("host", "localhost"), port=int(self.cfg.get("port", 4455)),
                password=self.cfg.get("password") or "", timeout=3)
        return c

    def _find_game_capture(self) -> bool:
        """source の名前が見つからないとき、OBS の「ゲームキャプチャ」ソースを探して使う。"""
        try:
            inputs = self._client().get_input_list(None).inputs
        except Exception:
            return False
        names = [i.get("inputName") for i in inputs]
        games = [i.get("inputName") for i in inputs if i.get("inputKind") == "game_capture"]
        if games and games[0] != self.source:
            print(f"[screen] OBS にソース '{self.source}' が無いので、ゲームキャプチャ '{games[0]}' を使います"
                  f" (config.yaml の obs.source を '{games[0]}' にすると消えます)")
            self.source = games[0]
            return True
        print(f"[screen] OBS にソース '{self.source}' がありません。OBS のソース: {names}")
        return False

    def grab(self, width: int | None = None, height: int | None = None) -> np.ndarray:
        import base64

        import cv2

        try:
            r = self._client().get_source_screenshot(self.source, "jpg", width, height, 90)
        except Exception as e:
            if "No source was found" in str(e) and self._find_game_capture():
                return self.grab(width, height)
            self._local.client = None  # 次回つなぎ直す
            raise
        data = r.image_data
        data = data.split(",", 1)[1] if data.startswith("data:") else data
        img = cv2.imdecode(np.frombuffer(base64.b64decode(data), np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("OBS の画像を展開できません")
        return img


def is_foreground(hwnd: int) -> bool:
    if not IS_WINDOWS:
        return True
    return user32.GetForegroundWindow() == hwnd


class GameScreen:
    """ゲームのクライアント領域をキャプチャする。

    ウィンドウモードで起動すること (排他フルスクリーンだとキャプチャが黒くなることがある)。
    """

    def __init__(self, window_title: str, process: str | None = None, capture: str = "auto",
                 obs: dict | None = None):
        # capture: auto   = ウィンドウから直接 (Windows Graphics Capture → PrintWindow)。撮れなければ画面から
        #          wgc / printwindow = その方式だけ / screen = 画面に映っているものを撮る (mss)
        self.capture = "printwindow" if capture == "window" else capture
        self._wgc: WgcCapture | None = None
        self._wgc_failed = False
        self._wgc_started = 0.0
        self._pw_black = 0
        self._method = ""
        self._wgc_lock = threading.Lock()
        self._obs = ObsCapture(obs) if obs and obs.get("enabled", True) else None
        self._obs_err = 0.0
        self._obs_size_logged = False
        self._obs_full_w = 0  # OBS が返す全体画像の横幅 (縮小画像の切り出しに使う)
        self._local = threading.local()  # mss はスレッドごとに作る (液晶ミラーが別スレッドで撮る)
        self.window_title = window_title
        self.process = process
        self.hwnd: int | None = None
        self.pid = 0
        self.rect: Rect | None = None

    @classmethod
    def from_config(cls, cfg: dict) -> "GameScreen":
        g = cfg["game"]
        return cls(g["window_title"], g.get("process"), g.get("capture", "auto"), cfg.get("obs"))

    def locate(self) -> Rect:
        self.hwnd = find_window(self.window_title, self.process)
        if self.hwnd is None:
            raise RuntimeError(
                f"ウィンドウが見つかりません: title='{self.window_title}' process='{self.process}'"
                " (python tools\\list_windows.py で確認)"
            )
        self.rect = client_rect(self.hwnd)
        self.pid = window_pid(self.hwnd)
        print(f"[screen] ゲームウィンドウを固定: hwnd=0x{self.hwnd:X} pid={self.pid} "
              f"process={process_name(self.hwnd)} 画面 {self.rect.width}x{self.rect.height}")
        return self.rect

    def _grab_screen(self, r: Rect) -> np.ndarray:
        m = getattr(self._local, "mss", None)
        if m is None:
            import mss

            m = self._local.mss = mss.mss()
        shot = m.grab({"left": r.left, "top": r.top, "width": r.width, "height": r.height})
        return np.ascontiguousarray(np.asarray(shot)[:, :, :3])

    def _grab_wgc(self, hwnd: int) -> np.ndarray | None:
        with self._wgc_lock:
            if self._wgc_failed:
                return None
            if self._wgc is not None and (self._wgc.hwnd != hwnd or self._wgc.closed):
                self._wgc.stop()
                self._wgc = None
            if self._wgc is None:
                try:
                    self._wgc = WgcCapture(hwnd)
                    self._wgc_started = time.monotonic()
                except ImportError:
                    self._wgc_failed = True
                    print("[screen] windows-capture が無いので Windows Graphics Capture は使いません"
                          " (pip install windows-capture で入れると仮想デスクトップを切り替えても撮れます)")
                    return None
                except Exception as e:
                    self._wgc_failed = True
                    print(f"[screen] Windows Graphics Capture を開始できません: {e}")
                    return None
            wgc, started = self._wgc, self._wgc_started
        for _ in range(40):  # 開始直後は最初の 1 枚を待つ (最大 2 秒)
            img = wgc.latest()
            if img is not None or time.monotonic() - started > 2.0:
                return img
            time.sleep(0.05)
        return None

    def _fit_client(self, img: np.ndarray) -> np.ndarray:
        """OBS の画像をゲームのクライアント領域の大きさに合わせる。

        ゲームキャプチャはゲームの描画バッファの大きさで返すので、ウィンドウより大きく
        余白 (黒) が付くことがある。左上を基準に切り取り、大きさが違えば縮小して合わせる。
        """
        r = self.rect
        crop = (self._obs.cfg.get("crop") if self._obs else None)  # [x, y, w, h] (ピクセル) で手動指定も可
        h, w = img.shape[:2]
        self._obs_full_w = w
        if not self._obs_size_logged:
            self._obs_size_logged = True
            print(f"[screen] OBS の画像 {w}x{h} / ゲーム画面 {r.width}x{r.height}" if r else f"[screen] OBS の画像 {w}x{h}")
            try:  # 確認用に 1 枚保存する
                import cv2
                from pathlib import Path

                out = Path(__file__).resolve().parent.parent / "obs_debug.png"
                cv2.imwrite(str(out), img)
                print(f"[screen] OBS から受け取った画像を保存しました: {out}")
            except Exception:
                pass
            if float(img.mean()) < 3:
                print("[screen] OBS の画像が真っ黒です。OBS でソース名が合っているか、ソースが今のシーンで"
                      "表示 (目のアイコン) になっているか確認してください。obs.source にシーン名を書いても撮れます")
        if crop:
            x, y, cw, ch = (int(v) for v in crop)
            img = img[y : y + ch, x : x + cw]
        elif r is not None and r.width > 0 and r.height > 0 and (w, h) != (r.width, r.height):
            if w >= r.width and h >= r.height:
                img = img[: r.height, : r.width]  # 右・下の余白を落とす
            else:
                # 縮小されている: 横幅を基準に、ゲーム画面の縦横比になるよう下を落とす
                eh = min(h, round(w * r.height / r.width))
                img = img[:eh, :]
        if r is not None and r.width > 0 and img.shape[:2] != (r.height, r.width):
            import cv2

            img = cv2.resize(img, (r.width, r.height), interpolation=cv2.INTER_AREA)
        return np.ascontiguousarray(img)

    def _use(self, method: str) -> None:
        if method != self._method:
            self._method = method
            names = {"obs": "OBS ゲームキャプチャ (仮想デスクトップを切り替えても撮れる)",
                     "wgc": "Windows Graphics Capture (ウィンドウから直接)",
                     "printwindow": "PrintWindow (ウィンドウから直接)",
                     "screen": "画面から (仮想デスクトップを切り替えると別の画面が映ります)"}
            print(f"[screen] 撮影方式: {names[method]}")

    def grab(self) -> np.ndarray:
        """BGR 画像 (H, W, 3) を返す。起動時に固定したウィンドウを撮り続け、閉じられたら探し直す。"""
        if self.hwnd is None or (IS_WINDOWS and not user32.IsWindow(self.hwnd)):
            self.locate()
        hwnd = self.hwnd
        self.rect = client_rect(hwnd)
        if self._obs is not None and self.capture in ("auto", "obs"):
            try:
                img = self._fit_client(self._obs.grab())
                self._use("obs")
                return img
            except Exception as e:
                if self.capture == "obs":
                    raise RuntimeError(f"OBS から画像を取得できません: {e}") from e
                if time.monotonic() - self._obs_err > 30:
                    self._obs_err = time.monotonic()
                    hint = ""
                    if "identify" in str(e):
                        hint = ("\n         → OBS のパスワードが違います。OBS の WebSocket サーバー設定の「接続情報を表示」で"
                                "確認するか、「認証を有効にする」を外して config.yaml の obs.password を \"\" にしてください")
                    elif "refused" in str(e).lower() or "10061" in str(e):
                        hint = "\n         → OBS が起動していないか、WebSocket サーバーが有効になっていません"
                    print(f"[screen] OBS から画像を取得できません (他の方式で撮ります): {e}{hint}")
        if IS_WINDOWS and self.capture in ("auto", "wgc"):
            img = self._grab_wgc(hwnd)
            if img is not None:
                self._use("wgc")
                return img
            if self.capture == "wgc":
                raise RuntimeError("Windows Graphics Capture でゲーム画面を取得できません (最小化されていませんか)")
        if self.capture in ("auto", "printwindow") and self._pw_black < 3:
            img = capture_window(hwnd)
            if img is not None and (self.capture == "printwindow" or img.max() > 8):
                self._pw_black = 0
                self._use("printwindow")
                return img
            if self.capture == "printwindow":
                raise RuntimeError("PrintWindow でゲーム画面を取得できません (最小化されていませんか)")
            self._pw_black += 1  # このゲームでは真っ黒になる: 3 回続いたら以後は使わない
        if is_cloaked(hwnd):
            # 別の仮想デスクトップにある: 画面から撮ると関係ない画面が映るので撮らない
            raise CaptureHidden("ゲームが別の仮想デスクトップにあります")
        self._use("screen")
        return self._grab_screen(self.rect)

    def grab_preview(self, width: int = 480) -> np.ndarray:
        """液晶ミラー用の小さい画像。OBS を使っているときは OBS に縮小して返してもらう (全画面の転送を省いて速くする)。"""
        if self._obs is not None and self._method == "obs":
            if self.hwnd is None or (IS_WINDOWS and not user32.IsWindow(self.hwnd)):
                self.locate()
            r = client_rect(self.hwnd)
            try:
                img = self._obs.grab(width, 4096)  # 横幅を合わせて縮小 (縦横比は保たれる)
            except Exception:
                return self.grab()
            if r.width > 0 and r.height > 0:
                crop = self._obs.cfg.get("crop")
                h, w = img.shape[:2]
                if crop and self._obs_full_w:  # 全画面での切り出し指定を縮小後の座標に直す
                    sc = w / self._obs_full_w
                    x, y, cw, ch = (int(round(float(v) * sc)) for v in crop)
                    img = img[y : y + ch, x : x + cw]
                else:  # 下に余白が付くことがあるので、ゲーム画面の縦横比で上から切る
                    eh = min(h, round(w * r.height / r.width))
                    img = img[:eh]
            return np.ascontiguousarray(img)
        return self.grab()

    def to_screen(self, x: float, y: float) -> tuple[float, float]:
        """クライアント座標 → スクリーン座標"""
        assert self.rect is not None
        return self.rect.left + x, self.rect.top + y

    def is_active(self) -> bool:
        return self.hwnd is not None and is_foreground(self.hwnd)
