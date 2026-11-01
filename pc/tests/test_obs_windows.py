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
