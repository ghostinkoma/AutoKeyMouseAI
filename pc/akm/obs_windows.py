"""OBS のゲームキャプチャで、MURex のウィンドウをキャラクターごとに撮る (obs-websocket 経由)。

OBS のゲームキャプチャは、OBS の DLL (graphics-hook) をゲームに入れて描画そのものを受け取る。
そのため別の仮想デスクトップにあっても・他のウィンドウが重なっていても撮れ、Windows 10 の黄色い枠も出ない。
DLL を自分で入れたり OBS 内部のやり取りを真似たりはせず、注入・受け取りは OBS 本体に任せ、
こちらは WebSocket でソースを作る・撮影先を合わせる・画像をもらうだけにする (OBS の版が変わっても壊れにくい)。

  * シーン "AKM Capture" を作り、キャラクターごとにゲームキャプチャ "AKM <名前>" を置く
    (ゲームキャプチャは OBS で「表示中」のときだけ撮るので、このシーンを OBS の番組 (プログラム) にする)
  * 撮影先はウィンドウのタイトルで決まる。MURex のタイトルはレベルなどで変わるので、
    変わったら OBS の設定を今のタイトルに書き換える (「タイトルが一致するウィンドウだけ」にして取り違えない)
  * 画像は GetSourceScreenshot で受け取る

準備: OBS 28 以降・ツール → WebSocket サーバー設定 で有効化 (1 回だけ。OBS が覚える)・pip install obsws-python。
Main.exe は管理者権限で動いているので、OBS も管理者として動かす (しないとゲームに入れない)。
OBS が動いていなければ自動で起動する (obs.autostart)。管理者で起動したこのアプリから起動すれば OBS も管理者になる。
"""
from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np

SCENE = "AKM Capture"
SOURCE_PREFIX = "AKM "
GAME_CLASS = "MURex Client"
PRIORITY_TITLE = 1  # OBS: ウィンドウのタイトルが一致するものだけ (0 = 同じクラス、2 = 同じ実行ファイルも可)


def obs_window_string(title: str, cls: str = GAME_CLASS, exe: str = "Main.exe") -> str:
    """OBS のウィンドウ指定 "タイトル:クラス:実行ファイル" (# と : は #22 / #3A に置き換える)。"""
    def enc(s: str) -> str:
        return s.replace("#", "#22").replace(":", "#3A")

    return f"{enc(title)}:{enc(cls)}:{enc(exe)}"


def connect_hint(e: Exception, host: str, port: int) -> str:
    """OBS につながらないときの、原因ごとの案内"""
    t = str(e)
    where = f"{host}:{port}"
    if "10061" in t or "refused" in t.lower() or "拒否" in t:
        return (f"OBS の WebSocket ({where}) が待ち受けていません。OBS (28 以降) を起動し、"
                f"ツール → WebSocket サーバー設定 で「WebSocket サーバーを有効にする」に印を付け、"
                f"サーバーポートを {port} にしてください (config.yaml の obs.port と合わせる)")
    if "auth" in t.lower() or "identify" in t.lower() or "4009" in t:
        return f"OBS の WebSocket ({where}) のパスワードが違います。config.yaml の obs.password を合わせてください"
    if "timed out" in t.lower() or "timeout" in t.lower():
        return f"OBS の WebSocket ({where}) から返事がありません。host / port を確認してください"
    return f"OBS ({where}) につながりません: {t}"


DEFAULT_OBS_EXE = r"C:\Program Files\obs-studio\bin\64bit\obs64.exe"


def obs_running() -> bool:
    """OBS (obs64.exe) が動いているか。Windows 以外では常に True (起動しない)"""
    if sys.platform != "win32":
        return True
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq obs64.exe", "/NH"], capture_output=True, text=True,
                             timeout=5, creationflags=0x08000000).stdout  # CREATE_NO_WINDOW
    except Exception:
        return True  # 分からないときは起動しない
    return "obs64.exe" in out.lower()


def obs_command(cfg: dict) -> tuple[list[str], Path]:
    """OBS を起動するコマンドと作業フォルダ (OBS は自分のフォルダで起動しないと言語ファイルが見つからない)。
    WebSocket のポート・パスワードは config.yaml の obs: のものを渡し、OBS 側の設定と食い違わないようにする。"""
    exe = Path(cfg.get("path") or DEFAULT_OBS_EXE)
    args = [str(exe), "--minimize-to-tray", "--disable-shutdown-check", f"--websocket_port={int(cfg.get('port', 4455))}"]
    if cfg.get("password"):
        args.append(f"--websocket_password={cfg['password']}")
    return args, exe.parent


def source_name(character: str) -> str:
    return SOURCE_PREFIX + character


class ObsWindows:
    def __init__(self, cfg: dict | None, log=print):
        cfg = cfg or {}
        self.cfg = cfg
        self.log = log
        self.exe = str(cfg.get("exe", "Main.exe"))
        self.switch_scene = bool(cfg.get("switch_scene", True))
        self.autostart = bool(cfg.get("autostart", True))
        self._launched_at = 0.0
        self._lock = threading.RLock()  # 撮影と解析の 2 つのスレッドから使う (WebSocket は 1 本なので順番に)
        self._client = None
        self._known: dict[str, str] = {}   # ソース名 → 設定済みのウィンドウ指定
        self._scene_ready = False

    # ---- 接続 -----------------------------------------------------------------
    def client(self):
        if self._client is None:
            try:
                import obsws_python as obs
            except ImportError as e:
                raise RuntimeError("obsws-python が入っていません (pip install obsws-python)") from e
            logging.getLogger("obsws_python").setLevel(logging.CRITICAL)
            host, port = self.cfg.get("host", "localhost"), int(self.cfg.get("port", 4455))
            self._maybe_launch(host)
            try:
                self._client = obs.ReqClient(host=host, port=port, password=self.cfg.get("password") or "", timeout=3)
            except Exception as e:
                if time.monotonic() - self._launched_at < 60:  # 起動したばかり: 立ち上がるのを待っている
                    raise RuntimeError("OBS を起動しています… (立ち上がると自動でつながります)") from e
                raise RuntimeError(connect_hint(e, host, port)) from e
            self._known.clear()
            self._scene_ready = False
        return self._client

    def _maybe_launch(self, host: str) -> None:
        """OBS が動いていなければ起動する (このパソコンの OBS のときだけ。1 分に 1 回まで)"""
        if not self.autostart or host not in ("localhost", "127.0.0.1", "::1"):
            return
        if time.monotonic() - self._launched_at < 60 or obs_running():
            return
        args, cwd = obs_command(self.cfg)
        if not Path(args[0]).exists():
            self.log("WARN", f"OBS が見つかりません: {args[0]} (config.yaml の obs.path に obs64.exe の場所を書いてください)")
            self._launched_at = time.monotonic()
            return
        try:
            flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(args, cwd=str(cwd), creationflags=flags, close_fds=True)
            self.log("INFO", "OBS を起動しました (トレイに最小化)。立ち上がると自動でつながります")
        except Exception as e:
            self.log("ERR", f"OBS を起動できません: {e}")
        self._launched_at = time.monotonic()

    def reset(self) -> None:
        """接続が切れたとき: 次の呼び出しでつなぎ直す"""
        with self._lock:
            self._client = None

    # ---- ソースの用意 -------------------------------------------------------------
    def _ensure_scene(self, c) -> None:
        if self._scene_ready:
            return
        scenes = [s.get("sceneName") for s in c.get_scene_list().scenes]
        if SCENE not in scenes:
            c.create_scene(SCENE)
            self.log("INFO", f"OBS にシーン '{SCENE}' を作りました")
        if self.switch_scene:
            cur = c.get_current_program_scene()
            name = getattr(cur, "current_program_scene_name", None) or getattr(cur, "scene_name", None)
            if name != SCENE:
                c.set_current_program_scene(SCENE)
                self.log("INFO", f"OBS の番組を '{SCENE}' に切り替えました (ゲームキャプチャは表示中のときだけ撮るため)")
        self._scene_ready = True

    def ensure(self, character: str, title: str) -> str:
        """このキャラクターのゲームキャプチャを用意し、撮影先を今のタイトルに合わせる。ソース名を返す。"""
        with self._lock:
            return self._ensure(character, title)

    def _ensure(self, character: str, title: str) -> str:
        src = source_name(character)
        want = obs_window_string(title, GAME_CLASS, self.exe)
        if self._known.get(src) == want:
            return src
        c = self.client()
        self._ensure_scene(c)
        settings = {"capture_mode": "window", "window": want, "priority": PRIORITY_TITLE, "capture_cursor": False}
        names = {i.get("inputName") for i in c.get_input_list(None).inputs}
        if src in names:
            c.set_input_settings(src, settings, True)
            if src in self._known:
                self.log("INFO", f"OBS '{src}' の撮影先を今のタイトルに合わせました")
        else:
            c.create_input(SCENE, src, "game_capture", settings, True)
            self.log("INFO", f"OBS にゲームキャプチャ '{src}' を作りました")
        self._known[src] = want
        return src

    # ---- 画像 -------------------------------------------------------------------
    def grab(self, src: str, width: int | None = None, height: int | None = None, quality: int = 90) -> np.ndarray:
        with self._lock:
            r = self.client().get_source_screenshot(src, "jpg", width, height, quality)
        data = r.image_data
        data = data.split(",", 1)[1] if data.startswith("data:") else data
        img = cv2.imdecode(np.frombuffer(base64.b64decode(data), np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("OBS の画像を展開できません")
        return img
