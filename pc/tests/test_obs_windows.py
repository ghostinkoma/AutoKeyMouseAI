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

    def get_input_settings(self, name):
        return NS(input_settings=dict(self.inputs[name][2]))

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


def test_not_ready_is_not_a_connection_error_and_settings_are_not_rewritten(monkeypatch):
    """OBS が 702 (まだ画像が無い) を返しても接続を張り直さず、張り直しても同じ設定は書き直さない (つかみ直しにならない)"""
    import pytest

    from akm.obs_windows import ObsNotReady

    fake = FakeObs()

    def shot(*a):
        raise RuntimeError("Request GetSourceScreenshot returned code 702. With message: Failed to render screenshot.")

    fake.get_source_screenshot = shot
    install(monkeypatch, fake)
    o = ObsWindows({}, log=lambda *a: None)
    src = o.ensure("Tiffany", "MURex Client || Tiffany || Lv: 400")
    with pytest.raises(ObsNotReady):
        o.grab(src)
    o.reset()  # 何かで張り直しても
    o.ensure("Tiffany", "MURex Client || Tiffany || Lv: 400")
    assert fake.calls == ["create"]  # 同じ設定は書き直さない


def test_source_per_client_follows_character_change(monkeypatch):
    """ゲーム内でキャラクターを変えても同じソース (プロセス ID ごと) を使い、撮影先のタイトルだけ書き直す"""
    fake = FakeObs()
    install(monkeypatch, fake)
    o = ObsWindows({}, log=lambda *a: None)
    a = o.ensure(30560, "MURex Client || Tiffany || Lv: 400")
    b = o.ensure(30560, "MURex Client || Thanatos || Lv: 400")
    assert a == b == "AKM 30560"
    assert fake.calls == ["create", "set"] and "Thanatos" in fake.inputs[a][2]["window"]


def test_other_window_uses_window_capture_and_follows_title(monkeypatch):
    """その他のウィンドウ (VLC・ブラウザ): ウィンドウキャプチャ (WGC)、同じ実行ファイルの中でタイトルが一番近い窓。
    動画を変えてタイトルが変わったら撮影先を書き直す。ゲームキャプチャを作っても、動いている窓のソースは消さない"""
    fake = FakeObs()
    install(monkeypatch, fake)
    monkeypatch.setattr("akm.obs_windows.process_alive", lambda pid: True)
    o = ObsWindows({}, log=lambda lv, m: None)
    src = o.ensure_window(4100, "a.mp4 - VLC media player", "Qt5QWindowIcon", "vlc.exe")
    scene, kind, st = fake.inputs[src]
    assert src == "AKM win 4100" and kind == "window_capture"
    assert (st["method"], st["priority"], st["cursor"], st["client_area"]) == (2, 2, False, True)
    assert st["window"] == "a.mp4 - VLC media player:Qt5QWindowIcon:vlc.exe"
    o.ensure_window(4100, "a.mp4 - VLC media player", "Qt5QWindowIcon", "vlc.exe")
    assert fake.calls == ["create"]
    o.ensure_window(4100, "b.mp4 - VLC media player", "Qt5QWindowIcon", "vlc.exe")
    assert fake.calls == ["create", "set"] and fake.inputs[src][2]["window"].startswith("b.mp4")
    o.ensure(30560, "MURex Client || Tiffany || Lv: 400")
    assert src in fake.inputs and "AKM 30560" in fake.inputs


def test_other_windows_are_listed_after_games():
    import gate_app

    games = gate_app.list_game_windows()
    both = gate_app.list_game_windows(include_other=True)
    assert all(w.is_game for w in games) and both[:len(games)] == games
    others = both[len(games):]
    assert others and all(not w.is_game for w in others)
    assert others[0].label(False).startswith("[他] ")
