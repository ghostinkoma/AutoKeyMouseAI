"""実機なしで動く範囲のテスト: python -m pytest tests"""
import sys
from pathlib import Path

import cv2
import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from akm.device import ABS_MAX, AbsMap, Device, Transport  # noqa: E402
from akm.elf_bot import ElfBot, choose_pickup  # noqa: E402
from akm.screen import Rect  # noqa: E402
from akm.vision import Detection, TemplateDetector, bar_level  # noqa: E402

PC = Path(__file__).resolve().parent.parent


def orb(level: float, size=100) -> np.ndarray:
    img = np.zeros((size, size, 3), np.uint8)
    cv2.circle(img, (size // 2, size // 2), size // 2 - 2, (40, 40, 40), -1)
    mask = np.zeros((size, size), np.uint8)
    cv2.circle(mask, (size // 2, size // 2), size // 2 - 2, 255, -1)
    top = int(size * (1 - level))
    mask[:top] = 0
    img[mask > 0] = (20, 20, 220)  # 赤 (BGR)
    return img


def test_bar_level_vertical():
    for lv in (0.0, 0.25, 0.5, 0.9):
        assert abs(bar_level(orb(lv), [0, 0, 1, 1], "red") - lv) < 0.08


def test_bar_level_horizontal():
    img = np.zeros((10, 200, 3), np.uint8)
    img[:, :70] = (220, 60, 20)  # 青
    assert abs(bar_level(img, [0, 0, 1, 1], "blue", "horizontal") - 0.35) < 0.02


def test_template_detector(tmp_path):
    rng = np.random.default_rng(0)
    scene = rng.integers(0, 60, (400, 600, 3), dtype=np.uint8)
    label = np.zeros((14, 90, 3), np.uint8)
    cv2.putText(label, "Jewel of Bless", (1, 11), cv2.FONT_HERSHEY_PLAIN, 0.8, (80, 200, 255), 1)
    cv2.imwrite(str(tmp_path / "bless.png"), label)
    scene[100:114, 300:390] = label
    scene[250:264, 50:140] = label
    det = TemplateDetector([{"name": "Jewel of Bless", "files": ["bless.png"]}], base_dir=tmp_path)
    found = sorted((d.x, d.y) for d in det.detect(scene))
    assert found == [(50, 250), (300, 100)]


def test_choose_pickup_prefers_nearest_target():
    dets = [
        Detection("Zen", "item", 500, 300, 30, 12, 0.9),
        Detection("Jewel of Bless", "item", 420, 330, 80, 12, 0.9),
        Detection("Apple", "item", 400, 300, 40, 12, 0.9),
        Detection("monster", "monster", 400, 300, 40, 40, 0.9),
    ]
    got = choose_pickup(dets, (400, 300), 600, ["Zen", "Jewel of Bless"], 0.35, {}, 0)
    assert got.label == "Jewel of Bless"
    got = choose_pickup(dets, (400, 300), 600, ["Zen", "Jewel of Bless"], 0.35, {"Jewel of Bless": 10}, 0)
    assert got.label == "Zen"
    assert choose_pickup(dets, (0, 0), 600, ["Zen"], 0.35, {}, 0) is None


def test_abs_map():
    m = AbsMap.for_screen(1920, 1080)
    assert m.to_hid(0, 0) == (0, 0)
    assert m.to_hid(1919, 1079) == (ABS_MAX, ABS_MAX)
    assert m.to_hid(5000, -10) == (ABS_MAX, 0)


class FakeScreen:
    def __init__(self, frames):
        self.frames = frames
        self.rect = Rect(100, 50, 800, 600)

    def grab(self):
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]

    def to_screen(self, x, y):
        return self.rect.left + x, self.rect.top + y


class Recorder(Transport):
    def __init__(self):
        self.sent = []

    def run(self, script, timeout=35.0):
        self.sent.append(script)
        return "ok"


class FakeDetector:
    def __init__(self, seq):
        self.seq = seq

    def detect(self, img):
        return self.seq.pop(0) if len(self.seq) > 1 else self.seq[0]


def make_bot(frames, det_seq, mode="manual", patrol=False):
    from akm.helper import HelperControl

    cfg = yaml.safe_load((PC / "config.example.yaml").read_text(encoding="utf-8"))
    cfg["elf"]["heal"] = None
    cfg["elf"]["mode"] = mode
    cfg["elf"]["patrol"]["enabled"] = patrol
    cfg["helper"]["settle_ms"] = 0
    cfg["helper"]["indicator"]["off_image"] = None  # テスト画像には MU Helper パネルが無い
    rec = Recorder()
    dev = Device(rec, AbsMap.for_screen(1920, 1080))
    helper = HelperControl(cfg["helper"], dev, PC)
    bot = ElfBot(cfg, FakeScreen(frames), dev, [FakeDetector(det_seq)], helper=helper)
    return bot, rec


def full_hud_frame():
    img = np.zeros((600, 800, 3), np.uint8)
    hud = yaml.safe_load((PC / "config.example.yaml").read_text(encoding="utf-8"))["hud"]
    from akm.vision import roi_px

    x0, y0, x1, y1 = roi_px(img.shape, hud["hp"]["roi"])
    img[y0:y1, x0:x1] = (20, 20, 220)
    x0, y0, x1, y1 = roi_px(img.shape, hud["mp"]["roi"])
    img[y0:y1, x0:x1] = (220, 60, 20)
    return img


def test_bot_buffs_then_attacks():
    bot, rec = make_bot([full_hud_frame()], [[]])
    bot.step()  # 起動直後はバフ
    assert "k:2" in rec.sent[-1] and "k:3" in rec.sent[-1] and rec.sent[-1].endswith("k:1")
    bot.step()  # 次は攻撃開始 (右ボタン押下)
    assert rec.sent[-1].startswith("k:1;a:") and rec.sent[-1].endswith("bd:R")
    assert bot.holding_attack
    bot.step()
    assert rec.sent[-1].startswith("a:")


def test_bot_picks_up_jewel_and_returns():
    jewel = Detection("Jewel of Bless", "item", 470, 330, 80, 12, 0.95)
    bot, rec = make_bot([full_hud_frame()], [[jewel], []])
    bot.last_buff = 1e18  # バフ済み扱い
    bot.holding_attack = True
    bot.step()
    assert rec.sent[0] == "bu:R"  # 攻撃を止めてから
    assert rec.sent[1].endswith("c:L")  # ラベルをクリック
    assert bot.picked["Jewel of Bless"] == 1
    assert "c:L" in rec.sent[2]  # 元の位置へ戻る


def test_bot_potion_when_hp_low():
    frame = full_hud_frame()
    hud = yaml.safe_load((PC / "config.example.yaml").read_text(encoding="utf-8"))["hud"]
    from akm.vision import roi_px

    x0, y0, x1, y1 = roi_px(frame.shape, hud["hp"]["roi"])
    frame[y0 : y0 + int((y1 - y0) * 0.75), x0:x1] = 0  # HP 25%
    bot, rec = make_bot([frame], [[]])
    bot.last_buff = 1e18
    bot.step()
    assert "k:q" in rec.sent


def test_relative_mouse_for_unreachable_position():
    pos = [0, 0]

    def cursor():
        return tuple(pos)

    class MoveRec(Recorder):
        def run(self, script, timeout=35.0):
            super().run(script, timeout)
            if script.startswith("m:"):
                dx, dy = map(int, script[2:].split(","))
                pos[0] += int(dx * 0.8)  # マウス加速などで少しずれる想定
                pos[1] += int(dy * 0.8)
            return "ok"

    rec = MoveRec()
    dev = Device(rec, AbsMap.for_screen(1920, 1080), mouse_mode="auto", cursor=cursor)
    assert dev.s_move(100, 100).startswith("a:")  # プライマリ内は絶対座標
    assert rec.sent == []
    assert dev.s_move(2500, 300) == ""  # 右のモニタ: 相対移動で合わせ込む
    assert abs(pos[0] - 2500) <= 2 and abs(pos[1] - 300) <= 2
    assert all(s.startswith("m:") for s in rec.sent)


def test_template_detector_gold_mask(tmp_path):
    # 背景色が違っても金色文字だけで照合できること
    gold = (40, 175, 205)  # BGR (H≈23 の金色)
    tpl = np.full((13, 27, 3), (40, 30, 20), np.uint8)
    cv2.putText(tpl, "Zen", (1, 10), cv2.FONT_HERSHEY_PLAIN, 0.8, gold, 1)
    cv2.imwrite(str(tmp_path / "zen.png"), tpl)
    scene = np.full((300, 400, 3), (160, 110, 60), np.uint8)  # 青っぽい雪原
    label = np.full((13, 27, 3), (90, 70, 50), np.uint8)       # 背景の濃さが違うラベル
    cv2.putText(label, "Zen", (1, 10), cv2.FONT_HERSHEY_PLAIN, 0.8, gold, 1)
    scene[200:213, 100:127] = label
    det = TemplateDetector([{"name": "Zen", "files": ["zen.png"], "threshold": 0.8, "color": "gold"}], base_dir=tmp_path)
    found = det.detect(scene)
    assert [(d.x, d.y) for d in found] == [(100, 200)]


def test_helper_mode_turns_helper_on_and_restarts_after_pickup():
    jewel = Detection("Jewel of Bless", "item", 470, 330, 80, 12, 0.95)
    bot, rec = make_bot([full_hud_frame()], [[], [jewel], []], mode="helper")
    bot.step()  # 何も落ちていない: MU Helper を入れるだけ
    assert rec.sent == ["k:home"]
    assert bot.helper.is_on()
    rec.sent.clear()
    bot.step()  # 宝石: クリックで拾う (MU Helper は止まる) → 戻る → Home で再開
    assert rec.sent[0].endswith("c:L")
    assert rec.sent[-1] == "k:home"
    assert not any(s.startswith("bd:R") or "bd:R" in s for s in rec.sent)  # 自分では攻撃しない
    assert bot.picked["Jewel of Bless"] == 1 and bot.helper.is_on()


def test_mirror_frame_format():
    from akm.mirror import compose, to_rgb565_be

    img = np.zeros((1050, 1680, 3), np.uint8)
    img[:, :, 2] = 255  # 赤一色
    out = compose(img, [("RUN", (0, 255, 0))])
    assert out.shape == (135, 240, 3)
    data = to_rgb565_be(out)
    assert len(data) == 240 * 135 * 2
    mid = (134 * 240 + 120) * 2  # 最下行の中央 (左右は縦横比を保つための黒帯)
    assert data[mid : mid + 2] == b"\xf8\x00"  # 赤 = RGB565 0xF800 (ビッグエンディアン)


def test_patrol_moves_then_volleys_8_directions_and_restarts_helper():
    bot, rec = make_bot([full_hud_frame()], [[]], mode="helper", patrol=True)
    bot.helper.believed_on = True
    bot.step()
    move, volley, toggle = rec.sent[0], rec.sent[1], rec.sent[2]
    assert move.endswith(move.split(";")[-1]) and ";c:L;" in move  # 上へ 5 マス移動
    assert volley.startswith("k:1") and volley.count("c:R,2") == 8  # 45 度ずつ 8 方向
    assert toggle == "k:home"  # クリックで止まった MU Helper を再開
    assert bot.nav_pos[1] < 0  # 上 (画面の上方向) に動いた
    assert bot.helper.is_on()


def test_helper_indicator_reads_play_button():
    import cv2
    from akm.helper import HelperControl

    cfg = yaml.safe_load((PC / "config.example.yaml").read_text(encoding="utf-8"))
    hc = HelperControl(cfg["helper"], Device(Recorder(), AbsMap.for_screen(1920, 1080)), PC)
    tpl = cv2.imread(str(PC / "templates/helper_off.png"))
    panel = cv2.imread(str(PC / "templates/helper_panel.png"))
    frame = np.zeros((1050, 1680, 3), np.uint8)
    assert hc.read_state(frame) is None  # パネルが無い = 判定しない (ボットの操作から推定)
    frame[8:25, 116:188] = panel
    assert hc.read_state(frame) is True  # パネルあり・▶ が見えない = 動作中
    frame[6:32, 276:304] = tpl
    assert hc.read_state(frame) is False  # ▶ が見える = 停止中


def test_helper_starts_by_clicking_play_button():
    import cv2
    from akm.helper import HelperControl

    cfg = yaml.safe_load((PC / "config.example.yaml").read_text(encoding="utf-8"))
    cfg["helper"]["settle_ms"] = 0
    cfg["helper"]["start_method"] = "click"
    rec = Recorder()
    hc = HelperControl(cfg["helper"], Device(rec, AbsMap.for_screen(1920, 1080)), PC)
    hc.to_screen = lambda x, y: (x + 100, y + 10)
    frame = np.zeros((1050, 1680, 3), np.uint8)
    frame[8:25, 116:188] = cv2.imread(str(PC / "templates/helper_panel.png"))
    tpl = cv2.imread(str(PC / "templates/helper_off.png"))
    frame[6:6 + tpl.shape[0], 276:276 + tpl.shape[1]] = tpl
    assert hc.find_start_button(frame) == (276 + tpl.shape[1] / 2, 6 + tpl.shape[0] / 2)
    hc.ensure_on(frame)
    assert rec.sent and "c:L" in rec.sent[-1] and "k:home" not in rec.sent[-1]
    assert hc.is_on()


def test_popup_guard_clicks_cancel():
    import cv2
    from akm.popups import PopupGuard

    rng = np.random.default_rng(1)
    detect = rng.integers(0, 255, (20, 60, 3), dtype=np.uint8)
    cancel = rng.integers(0, 255, (16, 40, 3), dtype=np.uint8)
    tmp = PC / "dataset" / "_test_popup"
    tmp.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(tmp / "d.png"), detect)
    cv2.imwrite(str(tmp / "c.png"), cancel)
    rec = Recorder()
    pg = PopupGuard([{"name": "party", "detect": str(tmp / "d.png"), "click": str(tmp / "c.png"), "wait_ms": 0}],
                    Device(rec, AbsMap.for_screen(1920, 1080)), PC)
    pg.to_screen = lambda x, y: (x, y)
    frame = np.zeros((600, 800, 3), np.uint8)
    assert not pg.check(frame)
    frame[100:120, 300:360] = detect
    frame[200:216, 320:360] = cancel
    assert pg.check(frame)
    assert "c:L" in rec.sent[-1] and pg.handled["party"] == 1
    import shutil
    shutil.rmtree(tmp)


def test_mirror_thread_captures_and_posts_by_itself():
    import http.server
    import threading
    import time

    from akm.mirror import Mirror

    got = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    frame = np.full((1050, 1680, 3), 80, np.uint8)
    m = Mirror(f"127.0.0.1:{srv.server_port}", 0.05, fmt="jpeg", grab=lambda: frame)
    m.set_lines([("RUN", (0, 255, 0))])
    t = time.monotonic()
    while len(got) < 2 and time.monotonic() - t < 5:
        time.sleep(0.05)
    srv.shutdown()
    assert len(got) >= 2 and got[0][:2] == b"\xff\xd8" and m.sent >= 2


def test_mirror_keeps_last_frame_when_game_on_other_desktop():
    from akm.mirror import Mirror
    from akm.screen import CaptureHidden

    frames = [np.full((1050, 1680, 3), 200, np.uint8)]

    def grab():
        if frames:
            return frames.pop()
        raise CaptureHidden("other desktop")

    m = Mirror(None, grab=grab)  # host 無し = 送信スレッドなし
    assert m._capture()[:2] == b"\xff\xd8"
    assert m._capture()[:2] == b"\xff\xd8" and m._last_img is not None


def test_obs_image_is_cropped_to_client_area():
    from akm.screen import GameScreen, Rect

    gs = GameScreen("MU", obs={"enabled": True})
    gs.rect = Rect(0, 0, 1680, 1050)
    img = np.zeros((1274, 1680, 3), np.uint8)
    img[:1050] = 100
    out = gs._fit_client(img)
    assert out.shape == (1050, 1680, 3) and out.min() == 100
    small = np.full((637, 840, 3), 50, np.uint8)  # 半分に縮小された画像 + 下に余白
    assert gs._fit_client(small).shape == (1050, 1680, 3)


def test_save_obs_password_rewrites_only_obs_block(tmp_path):
    import yaml as _y

    from akm.config import save_obs_password

    p = tmp_path / "c.yaml"
    p.write_text("device:\n  password: keep  # x\nobs:\n  enabled: true\n  password: \"old\"\n  source: MU\ngame:\n  a: 1\n",
                 encoding="utf-8")
    assert save_obs_password(p, 'n"ew#1')
    d = _y.safe_load(p.read_text(encoding="utf-8"))
    assert d["obs"]["password"] == 'n"ew#1' and d["device"]["password"] == "keep" and d["game"]["a"] == 1


def test_parse_location_handles_ocr_noise():
    from akm.maploc import Location, parse_location

    assert parse_location("Atlans (32, 68)") == Location("Atlans", 32, 68)
    assert parse_location("Atlans(32,68)") == Location("Atlans", 32, 68)
    assert parse_location("Atlns ( 3O, l7 )") == Location("Atlans", 30, 17)  # O→0, l→1, 綴り補正
    assert parse_location("Lost Tower (120, 200)") == Location("Lost Tower", 120, 200)
    assert parse_location("Custom Map (5, 6)") == Location("Custom Map", 5, 6)  # 未知のマップ名もそのまま
    assert parse_location("Welcome to Rex Project") is None
    assert parse_location("Atlans (300, 68)") is None  # 範囲外


def test_location_reader_rejects_single_jump():
    from akm.maploc import Location, LocationReader

    r = LocationReader({}, ocr=lambda img: "")
    A = Location("Atlans", 30, 60)
    assert r.accept(A) is None                       # 最初は確認待ち
    assert r.accept(Location("Atlans", 31, 60)) == Location("Atlans", 31, 60)
    assert r.accept(Location("Atlans", 200, 10)) is None   # 読み違いらしい飛び
    assert r.accept(Location("Atlans", 32, 61)) == Location("Atlans", 32, 61)
    assert r.accept(Location("Lorencia", 130, 130)) is None  # マップ移動: 1 回目は保留
    assert r.accept(Location("Lorencia", 131, 130)) == Location("Lorencia", 131, 130)


def test_mapdb_records_cells_and_events(tmp_path):
    from akm.maploc import Location, MapDB

    db = MapDB(tmp_path / "m.db")
    db.record(Location("Atlans", 10, 10), ts=1.0)
    db.record(Location("Atlans", 13, 10), ts=2.0)   # 途中の 11, 12 も歩けるマス
    assert db.record(Location("Lorencia", 130, 130), ts=3.0) == "map_change"
    cells = {(m, x, y) for m, x, y in db.db.execute("SELECT map, x, y FROM cells")}
    assert {("Atlans", x, 10) for x in range(10, 14)} <= cells and ("Lorencia", 130, 130) in cells
    assert db.db.execute("SELECT kind FROM events").fetchall() == [("map_change",)]
    db.add_spot("hunt1", db.latest(), 8)
    assert db.db.execute("SELECT name, map, radius FROM spots").fetchone() == ("hunt1", "Lorencia", 8)


def test_monster_table_parsing_with_rowspan():
    from akm.monsters import extract_monsters, parse_tables

    html = """<h2>一覧</h2><table>
      <tr><th>Lv</th><th>モンスター名</th><th>出現マップ</th></tr>
      <tr><td>2</td><td><img src="img/spider.gif">スパイダー</td><td rowspan="2">ロレンシア</td></tr>
      <tr><td>4</td><td>バッジドラゴン</td></tr>
      <tr><td>８６</td><td>バハムート</td><td>アトランス1、アトランス2</td></tr>
    </table>"""
    m = extract_monsters(parse_tables(html))
    assert {"name": "スパイダー", "level": 2, "map": "ロレンシア", "map_en": "Lorencia", "icon": "img/spider.gif"} in m
    assert {"name": "バッジドラゴン", "level": 4, "map": "ロレンシア", "map_en": "Lorencia", "icon": ""} in m
    assert [x["map_en"] for x in m if x["name"] == "バハムート"] == ["Atlans", "Atlans"]
    assert all(x["level"] == 86 for x in m if x["name"] == "バハムート")


def test_monster_table_with_icon_column_like_munou2014():
    from akm.monsters import extract_monsters, map_to_en, parse_tables

    html = """<table><tr><td>モンスター</td></tr></table>
    <table><caption>モンスター</caption>
      <tr><th>レベル</th><th colspan="2">モンスター名</th><th>生命</th><th>出現マップ</th></tr>
      <tr><td>2</td><td><img src="image/mon_s_lorncia1.jpg"></td><td>スパイダー</td><td>40</td><td>ロレンシア</td></tr>
      <tr><td>3</td><td><img src="image/mon_s_noria1.jpg"></td><td>パージゴブリン</td><td>60</td><td>ノリア</td></tr>
      <tr><td>80</td><td><img src="image/x.jpg"></td><td>バハムート</td><td>3000</td><td>アトランス</td></tr>
    </table>"""
    m = extract_monsters(parse_tables(html))
    assert [(x["name"], x["level"], x["map_en"], x["icon"]) for x in m] == [
        ("スパイダー", 2, "Lorencia", "image/mon_s_lorncia1.jpg"),
        ("パージゴブリン", 3, "Noria", "image/mon_s_noria1.jpg"),
        ("バハムート", 80, "Atlans", "image/x.jpg")]
    assert map_to_en("カルリマ3") == "Kalima" and map_to_en("DS2") == "Devil Square"
    assert map_to_en("奈落のアトランス") == "奈落のアトランス"  # Atlans と取り違えない
    assert map_to_en("バルガス兵営") == "Barracks of Balgass" and map_to_en("ヴォルカノス") == "Vulcanus"
