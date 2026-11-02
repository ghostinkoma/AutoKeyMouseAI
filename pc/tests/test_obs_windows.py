"""akm/obs_windows.py: OBS (obs-websocket) を偽物に差し替えて、ソースの用意と撮影先の追従を確かめる。"""
import base64
import sys
import types
from types import SimpleNamespace as NS

import cv2
import numpy as np

from akm.obs_windows import SCENE, ObsWindows, obs_window_string


class FakeObs:
    def __init__(self, **kw):
        self.scenes, self.program = ["Scene"], "Scene"
        self.inputs = {}  # name -> (scene, kind, settings)
        self.calls = []

    def get_scene_list(self):
        return NS(scenes=[{"sceneName": s} for s in self.scenes])

    def create_scene(self, name):
        self.scenes.append(name)

    def get_current_program_scene(self):
        return NS(current_program_scene_name=self.program)

    def set_current_program_scene(self, name):
        self.program = name

    def get_input_list(self, kind):
        return NS(inputs=[{"inputName": n, "inputKind": v[1]} for n, v in self.inputs.items()])

    def create_input(self, scene, name, kind, settings, enabled):
        self.calls.append("create")
        self.inputs[name] = (scene, kind, dict(settings))

    def set_input_settings(self, name, settings, overlay):
        self.calls.append("set")
        self.inputs[name][2].update(settings)

    def get_source_screenshot(self, name, fmt, w, h, q):
        img = np.full((768, 1366, 3), 90, np.uint8)
        ok, buf = cv2.imencode(".jpg", img)
        return NS(image_data="data:image/jpg;base64," + base64.b64encode(buf.tobytes()).decode())


def install(monkeypatch, fake):
    monkeypatch.setitem(sys.modules, "obsws_python", types.SimpleNamespace(ReqClient=lambda **kw: fake))


def test_window_string_escapes_colons():
    t = "MURex Client || Tiffany || Lv: 400 || Rs: 50"
    assert obs_window_string(t) == "MURex Client || Tiffany || Lv#3A 400 || Rs#3A 50:MURex Client:Main.exe"
    assert obs_window_string("a#b") == "a#22b:MURex Client:Main.exe"


def test_ensure_creates_scene_and_source_then_follows_title(monkeypatch):
    fake = FakeObs()
    install(monkeypatch, fake)
    logs = []
    o = ObsWindows({}, log=lambda lv, m: logs.append(m))
    src = o.ensure("Tiffany", "MURex Client || Tiffany || Lv: 400")
    assert src == "AKM Tiffany" and SCENE in fake.scenes and fake.program == SCENE
    scene, kind, st = fake.inputs[src]
    assert (scene, kind, st["priority"], st["capture_mode"]) == (SCENE, "game_capture", 1, "window")
    o.ensure("Tiffany", "MURex Client || Tiffany || Lv: 400")      # 同じタイトル: 何もしない
    assert fake.calls == ["create"]
    o.ensure("Tiffany", "MURex Client || Tiffany || Lv: 401")      # レベルが上がった: 撮影先を書き換える
    assert fake.calls == ["create", "set"] and "Lv#3A 401" in fake.inputs[src][2]["window"]
    img = o.grab(src)
    assert img.shape == (768, 1366, 3)


def test_no_scene_switch_when_disabled(monkeypatch):
    fake = FakeObs()
    install(monkeypatch, fake)
    ObsWindows({"switch_scene": False}, log=lambda *a: None).ensure("Iovis", "t")
    assert fake.program == "Scene"


def test_connect_hint_for_refused():
    from akm.obs_windows import connect_hint

    m = connect_hint(ConnectionRefusedError("[WinError 10061] 対象のコンピューターによって接続を拒否されました。"),
                     "localhost", 4455)
    assert "WebSocket サーバーを有効にする" in m and "localhost:4455" in m


def test_obs_command_passes_port_and_password_and_runs_in_obs_folder():
    from pathlib import Path

    from akm.obs_windows import obs_command

    args, cwd = obs_command({"port": 4455, "password": "pw", "path": r"C:\\OBS\\bin\\64bit\\obs64.exe"})
    assert "--websocket_port=4455" in args and "--websocket_password=pw" in args and "--minimize-to-tray" in args
    assert cwd == Path(args[0]).parent
    args, _ = obs_command({})
    assert not any(a.startswith("--websocket_password") for a in args)


def test_color_formats_preview_matches_bits():
    import numpy as np

    from akm.bad16 import replicate565, to565
    from akm.gate_live import color_bits, from565, to_send_format

    assert color_bits("BadCodec 565") == (5, 6, 5) and color_bits("BadCodec 343") == (3, 4, 3)
    assert color_bits("BadCodec 1bit") == (1, 1, 1) and color_bits("BadCodec 16bit") == (5, 6, 5)
    assert color_bits("JPEG") is None
    rng = np.random.default_rng(0)
    img = rng.integers(0, 256, (135, 240, 3), dtype=np.uint8)
    full, n565 = to_send_format(img, "BadCodec 565", 100)
    assert n565 == 240 * 135 * 2 and (from565(to565(full)) == full).all()  # 565 は 565 で表せる色だけ
    q, n343 = to_send_format(img, "BadCodec 343", 100)
    assert n343 == 240 * 135 * 10 // 8
    assert (to565(q) == replicate565(to565(img), (3, 4, 3))).all()  # ESP32 の表示 (下位ビットは繰り返し) と同じ
