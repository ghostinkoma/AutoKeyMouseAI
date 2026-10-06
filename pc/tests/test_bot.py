"""実機なしで動く範囲のテスト: python -m pytest tests"""
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest
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
    m = Mirror(f"127.0.0.1:{srv.server_port}", 0.05, fmt="jpeg", grab=lambda: frame, transport="http")
    m.set_lines([("RUN", (0, 255, 0))])
    t = time.monotonic()
    while len(got) < 2 and time.monotonic() - t < 5:
        time.sleep(0.05)
    m.stop()
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


def test_builtin_monsters_and_pasted_text():
    from akm.monsters import load_builtin, parse_pasted_text

    b = load_builtin()
    names = {m["name"] for m in b}
    assert len(names) > 320 and "スパイダー" in names and "バハムート" in names
    assert {"Lorencia", "Atlans", "Kalima"} <= {m["map_en"] for m in b}
    gp = [m for m in b if m["name"] == "ゴールドパージドラゴン"]
    assert {m["map_en"] for m in gp} == {"Lorencia", "Noria", "Devias"} and gp[0]["note"] == "レアキャラ"

    text = ("レベル\tモンスター名\t生命\t最小\n攻撃力\t最大\n攻撃力\t防御力\t防御\n成功率\t攻撃\n成功率\t属性\n"
            "最小攻撃力\t属性\n最大攻撃力\t属性\n防御力\t出現マップ\n"
            "2\t\tスパイダー\t40\t6\t8\t1\t1\t8\t-\t-\t-\tロレンシア\n"
            "14\t\tゴールドパージドラゴン\n※レアキャラ\t4400\t120\t125\t90\t30\t75\t-\t-\t-\tロレンシア\nノリア\nデビアス\n"
            "レベル\tモンスター名\t生命\t最小攻撃力\t最大攻撃力\t防御力\t防御成功率\t攻撃成功率\t属性\n最小攻撃力\t属性\n"
            "最大攻撃力\t属性\n防御力\t出現マップ\n"
            "30\t-\tデスキング\n※レアキャラ\t3600\t105\t110\t74\t37\t150\t-\t-\t-\tロレンシア\nノリア\nロストタワー\n"
            "?\t\t福袋\t?\t?\t?\t?\t?\t?\t?\t/\t/\tアイダ\nロストタワー\nEVENT\n")
    p = parse_pasted_text(text)
    assert ("スパイダー", 2, "Lorencia", 40) in [(m["name"], m["level"], m["map_en"], m["hp"]) for m in p]
    assert [m["map_en"] for m in p if m["name"] == "ゴールドパージドラゴン"] == ["Lorencia", "Noria", "Devias"]
    assert [m["map"] for m in p if m["name"] == "デスキング"] == ["ロレンシア", "ノリア", "ロストタワー"]
    assert [m["map"] for m in p if m["name"] == "福袋"] == ["アイダ", "ロストタワー"]
    assert all(m["note"] == "レアキャラ" for m in p if m["name"] == "デスキング")


def test_obs_preview_keeps_aspect_ratio(monkeypatch):
    from akm.screen import GameScreen, Rect

    gs = GameScreen("MU", obs={"enabled": True})
    calls = []
    full = np.zeros((1274, 1680, 3), np.uint8)
    full[:1050] = 100

    def fake_grab(width=None, height=None):
        calls.append((width, height))
        if width is None:
            return full
        import cv2
        return cv2.resize(full, (width, height), interpolation=cv2.INTER_AREA)  # OBS は指定の大きさに引き伸ばす

    gs._obs.grab = fake_grab
    gs._method = "obs"
    gs.hwnd = 1
    import akm.screen as scr
    monkeypatch.setattr(scr, "client_rect", lambda hwnd: Rect(0, 0, 1680, 1050))
    gs.rect = Rect(0, 0, 1680, 1050)
    img = gs.grab_preview(480)
    assert calls[-1] == (480, round(480 * 1274 / 1680))  # 縦横比どおりに頼む
    assert img.shape[:2] == (300, 480) and img.min() == 100  # 下の余白を落としてゲーム画面だけ


def test_mirror_tcp_stream_sends_framed_jpeg_and_waits_ack():
    import socket
    import struct
    import threading
    import time

    from akm.mirror import Mirror

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    got = []

    def esp():
        c, _ = srv.accept()
        f = c.makefile("rb")
        while len(got) < 3:
            hdr = f.read(8)
            assert hdr[:4] == b"AKMF"
            n = struct.unpack("<I", hdr[4:])[0]
            got.append(f.read(n))
            c.sendall(b"K")
        c.close()

    threading.Thread(target=esp, daemon=True).start()
    frame = np.full((1050, 1680, 3), 60, np.uint8)
    m = Mirror("127.0.0.1", 0.02, fmt="jpeg", grab=lambda: frame, transport="tcp", port=port)
    t = time.monotonic()
    while len(got) < 3 and time.monotonic() - t < 5:
        time.sleep(0.05)
    m.stop()
    assert len(got) == 3 and all(g[:2] == b"\xff\xd8" for g in got) and m.sent >= 3


def test_blockdiff_roundtrip_python_and_esp32_decoder():
    """PC のエンコーダ → Python の参照デコーダ / ESP32 と同じ C++ デコーダ で同じ絵になる。"""
    import shutil
    import struct
    import subprocess

    import cv2

    from akm.bcodec import Encoder, decode, rgb_of565, to565, unblocks

    rng = np.random.default_rng(3)
    base = rng.integers(0, 255, (135, 240, 3), dtype=np.uint8)
    base = cv2.GaussianBlur(base, (0, 0), 3)  # ゲーム画面らしい滑らかさ
    base[100:, :] = (30, 40, 50)  # 単色の帯 (FILL)
    enc = Encoder(16)
    frames, refs = [], []
    prev = None
    for f in range(4):
        img = base.copy()
        cv2.circle(img, (60 + 30 * f, 60), 20, (0, 0, 255), -1)  # 動く物
        if f == 2:
            img = cv2.add(img, 25)  # 全体が変わる
        data = enc.encode(img)
        dec = decode(data, prev)
        prev = dec
        assert (dec == unblocks(enc.ref)[:135]).all()
        assert np.abs(rgb_of565(dec) - rgb_of565(to565(img))).max() <= 16
        frames.append(data)
        refs.append(dec)
    assert frames[0][3] == 1 and frames[1][3] == 0  # 1 枚目はキーフレーム
    assert len(frames[1]) < len(frames[0]) // 5  # 変化が小さいフレームは小さい

    gxx = shutil.which("g++")
    if not gxx:
        return
    exe = PC / "tests" / "_bc_decode_test"
    subprocess.run([gxx, "-std=c++17", "-O1", "-o", str(exe), str(PC / "tests" / "bc_decode_test.cpp")], check=True)
    try:
        stdin = b"".join(struct.pack("<I", len(d)) + d for d in frames)
        out = subprocess.run([str(exe)], input=stdin, capture_output=True, check=True).stdout
        n = 240 * 135 * 2
        for i, ref in enumerate(refs):
            fb = np.frombuffer(out[i * n:(i + 1) * n], "<u2").reshape(135, 240)
            assert (fb == ref).all(), f"frame {i}"
    finally:
        exe.unlink(missing_ok=True)


def test_mirror_bc_over_tcp_falls_back_to_jpeg_for_big_changes_and_honors_keyframe_request():
    import socket
    import struct
    import threading
    import time

    from akm.mirror import Mirror

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    got = []

    def esp():
        c, _ = srv.accept()
        f = c.makefile("rb")
        while len(got) < 6:
            n = struct.unpack("<I", f.read(8)[4:])[0]
            got.append(f.read(n))
            c.sendall(b"R" if len(got) == 3 else b"K")  # 3 枚目のあと「全体をください」
        c.close()

    threading.Thread(target=esp, daemon=True).start()
    rng = np.random.default_rng(1)
    frames = [np.full((1050, 1680, 3), 70, np.uint8)]
    calls = {"n": 0}

    def grab():
        calls["n"] += 1
        img = frames[0].copy()
        if calls["n"] == 2:  # 2 枚目は全体がノイズで変わる → JPEG
            img = rng.integers(0, 255, img.shape, dtype=np.uint8)
        return img

    m = Mirror("127.0.0.1", 0.02, fmt="bc", grab=grab, transport="tcp", port=port,
               pipeline=1)  # 1 枚ずつ返事を待つ (R の次がキーフレームになる)
    t = time.monotonic()
    while len(got) < 6 and time.monotonic() - t < 5:
        time.sleep(0.05)
    assert got[0][:2] == b"BC" and got[0][3] == 1  # 最初はキーフレーム
    assert got[1][:2] == b"\xff\xd8"               # 大きな変化は JPEG
    assert got[2][:2] == b"BC"
    assert got[3][:2] == b"BC" and got[3][3] == 1  # R を受けたので次はキーフレーム
    assert m.sent_kinds["jpeg"] >= 1
    m.stop()


def _bad16_frames(bits, prev_free=False, gray=False):
    """ゲーム画面らしい絵 → 小さな変化 / 全体ノイズ / 変化なし / 左右反転 / ずらし / 反転。
    6 枚目から色のビット数を変える (ESP32 で面が足りずに減らしたときと同じ)。"""
    import cv2

    from akm.bad16 import Encoder, to565

    rng = np.random.default_rng(5)
    base = cv2.GaussianBlur(rng.integers(0, 255, (135, 240, 3), dtype=np.uint8), (0, 0), 3)
    base[100:, :] = (30, 40, 50)
    imgs, prev = [], None
    for f in range(9):
        img = base.copy()
        cv2.circle(img, (60 + 10 * f, 60), 20, (0, 0, 255), -1)
        if f == 3:
            img = cv2.add(img, rng.integers(0, 8, img.shape, dtype=np.uint8))
        if f == 4:
            img = prev
        if f == 5:
            img = cv2.flip(prev, 1)
        if f == 6:
            img = np.roll(prev, 2, axis=1)
        if f == 7:
            img = 255 - prev
        prev = img
        imgs.append(img)
    enc = Encoder(bits=bits, prev_free=prev_free, gray=gray)
    frames, expect = [], []
    for f, img in enumerate(imgs):
        if f == 6:  # 途中で面の組み合わせを変える (次はキーフレーム)
            enc = Encoder(bits=(2, 3, 2), prev_free=prev_free, gray=gray)
        frames.append(enc.encode(img))
        expect.append(enc.last_display[:135])  # 液晶に出る絵 (色を減らすときは四捨五入 + 下位ビットの複製)
    return frames, expect


@pytest.mark.parametrize("prev_free,gray", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("bits", [(5, 6, 5), (4, 5, 4), (3, 4, 2), (2, 3, 2)])
def test_bad16_esp32_decoder_matches_encoder(bits, prev_free, gray):
    """BadCodec 16bit 版: C エンコーダ → ESP32 と同じ C++ デコーダ (公式 bad_decode.cpp + bad16.h) で元の絵に戻る。"""
    import shutil
    import struct
    import subprocess

    from akm.bad16 import mask_of

    gxx = shutil.which("g++")
    if not gxx or (sys.platform != "win32" and not shutil.which("gcc")):
        pytest.skip("g++ / gcc が無い")
    frames, expect = _bad16_frames(bits, prev_free, gray)
    ver = 3 if prev_free else 2
    g = 2 if gray else 0
    assert frames[0][:3] == b"B6" + bytes([ver]) and frames[0][3] & 3 == 1 | g and frames[1][3] & 3 == g  # 1 枚目はキー
    if prev_free:  # 色を減らすときは下位ビットを複製して表示 (flags bit2)
        assert bool(frames[0][3] & 4) == (bits != (5, 6, 5))
    assert struct.unpack_from("<H", frames[0], 4)[0] == mask_of(bits)
    assert bin(mask_of(bits)).count("1") == sum(bits)
    assert frames[6][3] & 1                         # 面を変えたらキーフレーム
    assert len(frames[1]) < len(frames[0]) // 10  # 変化が小さいフレームは小さい
    assert len(frames[4]) < 100                    # 変化なしはほぼ 0
    exe = PC / "tests" / ("_bad16_decode_test_" + "".join(map(str, bits)) + str(ver) + str(g))
    fw = PC.parent / "firmware" / "AutoKeyMouse"
    subprocess.run([gxx, "-std=c++17", "-O1", "-o", str(exe), str(PC / "tests" / "bad16_decode_test.cpp"),
                    str(fw / "bad_decode.cpp")], check=True)
    try:
        stdin = b"".join(struct.pack("<I", len(d)) + d for d in frames)
        out = subprocess.run([str(exe)], input=stdin, capture_output=True, check=True).stdout
        n = 240 * 135 * 2
        assert len(out) == n * len(frames)
        for i, e in enumerate(expect):
            fb = np.frombuffer(out[i * n:(i + 1) * n], "<u2").reshape(135, 240)
            assert (fb == e).all(), f"frame {i}"
    finally:
        exe.unlink(missing_ok=True)


def test_bad16_matches_official_python_decoder():
    """公式 tools/Codec.py の decode_frame でも同じ絵に戻る (BADCODEC_TOOLS か隣の clone がある場合だけ)。"""
    from akm.bad16 import decode, official_tools

    if official_tools() is None:
        pytest.skip("公式 BadCodec (tools/Codec.py) が無い")
    frames, expect = _bad16_frames((3, 4, 2))
    planes = None
    for i, (d, e) in enumerate(zip(frames[:3], expect[:3])):
        img, planes = decode(d, planes)
        assert (img == e).all(), f"frame {i}"


def test_mirror_bad16_over_tcp_shrinks_planes_on_E_then_jpeg():
    """ESP32 がメモリ不足 ('E') を返したら面を減らして送り直し、もう減らせなければ JPEG。'R' でキーフレーム。"""
    import shutil
    import socket
    import struct
    import threading
    import time

    from akm.mirror import Mirror

    if sys.platform != "win32" and not shutil.which("gcc") and not (PC / "native" / "libbad_encode.so").exists():
        pytest.skip("gcc が無い")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    got = []
    limit = {"planes": 9}  # この ESP32 には 9 面までしか入らない

    def planes(d):
        return bin(struct.unpack_from("<H", d, 4)[0]).count("1") if d[:3] == b"B6\x02" else 0

    def esp():
        c, _ = srv.accept()
        f = c.makefile("rb")
        while len(got) < 12:
            n = struct.unpack("<I", f.read(8)[4:])[0]
            d = f.read(n)
            got.append(d)
            if len(got) == 9:
                limit["planes"] = 0  # 途中でメモリがさらに減った → JPEG へ
            if d[:2] == b"B6" and planes(d) > limit["planes"]:
                c.sendall(b"E")
            else:
                c.sendall(b"R" if len(got) == 7 else b"K")
        c.close()

    threading.Thread(target=esp, daemon=True).start()
    # 1 枚ずつ返事を待つ (送る順番を決まったものにする)。メモリ不足 ('E') があるのは面ごとのフレーム (ver 2) だけ
    m = Mirror("127.0.0.1", 0.02, fmt="bad16", grab=lambda: np.full((1050, 1680, 3), 70, np.uint8),
               transport="tcp", port=port, pipeline=1, b16_prev_free=False)
    t = time.monotonic()
    while len(got) < 12 and time.monotonic() - t < 15:
        time.sleep(0.05)
    m.stop()
    seq = [planes(d) for d in got[:6]]
    assert seq == [16, 13, 10, 9, 9, 9], seq       # 16 → 13 → 10 → 9 面で入った
    assert got[3][3] == 1 and got[4][3] == 0         # 入ったキーフレームの次は差分
    assert got[7][:3] == b"B6\x02" and got[7][3] == 1  # R のあとはキーフレーム
    assert any(d[:2] == b"\xff\xd8" for d in got[9:])  # もう入らない → JPEG
    assert m.fmt == "jpeg"


def test_mirror_bad16_pipeline_sends_ahead_and_shrinks_one_level_at_a_time():
    """返事を待たずに 1 枚先に送る。先に送った分の 'E' で段階を飛ばさない。"""
    import shutil
    import socket
    import struct
    import threading
    import time

    from akm.mirror import Mirror

    if sys.platform != "win32" and not shutil.which("gcc") and not (PC / "native" / "libbad_encode.so").exists():
        pytest.skip("gcc が無い")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    got = []
    ahead = []  # 返事を出す前に次のフレームが届いていたか

    def planes(d):
        return bin(struct.unpack_from("<H", d, 4)[0]).count("1")

    def esp():
        c, _ = srv.accept()
        f = c.makefile("rb")
        state = None  # 用意できている面の数
        while len(got) < 14:
            n = struct.unpack("<I", f.read(8)[4:])[0]
            d = f.read(n)
            got.append(d)
            time.sleep(0.05)  # 展開・描画している間
            c.setblocking(False)
            try:
                ahead.append(len(c.recv(1, socket.MSG_PEEK)) == 1)
            except BlockingIOError:
                ahead.append(False)
            c.setblocking(True)
            p, key = planes(d), d[3] & 1
            if key:
                state = p if p <= 9 else None
                c.sendall(b"K" if state else b"E")
            else:
                c.sendall(b"K" if state == p else b"R")
        c.close()

    threading.Thread(target=esp, daemon=True).start()
    m = Mirror("127.0.0.1", 0.01, fmt="bad16", grab=lambda: np.full((1050, 1680, 3), 70, np.uint8),
               transport="tcp", port=port, b16_prev_free=False)
    t = time.monotonic()
    while len(got) < 14 and time.monotonic() - t < 15:
        time.sleep(0.05)
    m.stop()
    keys = [planes(d) for d in got if d[3] & 1]
    assert keys[:4] == [16, 13, 10, 9], keys  # 段階を飛ばさない
    assert m.b16_bits == (3, 4, 2)
    assert any(ahead[4:]), ahead               # 返事の前に次のフレームが届いている (重なっている)


def test_mirror_bad16_prev_free_default_sends_ver3_and_only_changed_blocks():
    """既定 (前フレーム不要版): ver 3 で送り、変わらないフレームは SKIP だけになる。'R' で全体を送り直す。"""
    import shutil
    import socket
    import struct
    import threading
    import time

    from akm.mirror import Mirror

    if sys.platform != "win32" and not shutil.which("gcc") and not (PC / "native" / "libbad_encode.so").exists():
        pytest.skip("gcc が無い")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    got = []

    def esp():
        c, _ = srv.accept()
        f = c.makefile("rb")
        while len(got) < 6:
            n = struct.unpack("<I", f.read(8)[4:])[0]
            got.append(f.read(n))
            c.sendall(b"R" if len(got) == 3 else b"K")
        c.close()

    threading.Thread(target=esp, daemon=True).start()
    m = Mirror("127.0.0.1", 0.02, fmt="bad16", grab=lambda: np.full((1050, 1680, 3), 70, np.uint8),
               transport="tcp", port=port, pipeline=1, b16_bits=(3, 3, 2))
    t = time.monotonic()
    while len(got) < 6 and time.monotonic() - t < 10:
        time.sleep(0.05)
    m.stop()
    assert got[0][:3] == b"B6\x03" and got[0][3] & 1
    assert struct.unpack_from("<H", got[0], 4)[0] == 0b1110011100011000  # R3 G3 B2
    assert got[1][:3] == b"B6\x03" and not got[1][3] & 1 and len(got[1]) <= 6 + 8  # 変化なし = SKIP 命令だけ
    assert got[3][3] & 1                                         # R のあとは全体


# ------------------------------------------------------------------ MU Helper 定期再起動 --
def test_helper_cycle_restart_script_spams_potion_around_toggles():
    from akm.helper_cycle import CycleConfig, HelperCycle

    c = HelperCycle(CycleConfig(), run=None, grab=None, reader=None)
    cmds = c.restart_script().split(";")
    homes = [i for i, x in enumerate(cmds) if x == "k:home"]
    assert len(homes) == 2
    w_before = cmds[:homes[0]].count("k:w")
    w_gap = cmds[homes[0]:homes[1]].count("k:w")
    w_after = cmds[homes[1]:].count("k:w")
    assert (w_before, w_gap, w_after) == (10, 4, 10)  # 500ms / 200ms / 500ms を 50ms 間隔
    waits = [int(x[2:]) for x in cmds if x.startswith("w:")]
    assert set(waits) == {45}  # 押す時間 (約 5ms, d:5) + 待ち 45ms = 50ms 間隔
    assert cmds[0] == "d:5"


class _FakeReader:
    """state の並び (True = 動作中 / False = 停止中 / None = 不明) を順に返す。最後の値は繰り返す。"""

    def __init__(self, seq):
        self.seq = list(seq)

    def read(self, img):
        from akm.helper_state import HelperReading

        v = self.seq.pop(0) if len(self.seq) > 1 else self.seq[0]
        return HelperReading(v, "test")


def _cycle(seq, **kw):
    from akm.helper_cycle import CycleConfig, HelperCycle

    t = {"now": 0.0}
    sent = []
    c = HelperCycle(CycleConfig(**kw), run=sent.append, grab=lambda: None, reader=_FakeReader(seq),
                    clock=lambda: t["now"], sleep=lambda s: t.__setitem__("now", t["now"] + s), log=lambda *a: None)
    return c, t, sent


def test_helper_cycle_restarts_every_interval_and_verifies():
    c, t, sent = _cycle([True], interval_s=600)
    t["now"] = 599
    assert c.step() == "on" and not sent
    t["now"] = 600
    assert c.step() == "restart"
    assert len(sent) == 1 and sent[0].count("k:home") == 2 and c.restarts == 1
    t["now"] += 10
    assert c.step() == "on"  # 次の再起動は 600 秒後


def test_helper_cycle_recovers_when_stopped_and_retries_once():
    # 停止中が 6 秒続いたら Home。押しても停止中 → もう一度 Home。それでも停止中なら休む
    c, t, sent = _cycle([False], missing_s=6, backoff_s=60)
    assert c.step() == "missing"
    t["now"] += 7
    assert c.step() == "recover"
    assert len(sent) == 2 and all(s.count("k:home") == 1 for s in sent)
    t["now"] += 1
    assert c.step() == "backoff"
    # 押したら見えるようになる場合は 1 回で済む
    c, t, sent = _cycle([False, False, True], missing_s=6)
    c.step()
    t["now"] += 7
    assert c.step() == "recover" and len(sent) == 1 and c.recoveries == 1


def test_helper_cycle_never_presses_when_state_unknown():
    # インベントリ等でパネルもログ窓も見えない = 不明。動いているかもしれないので Home は押さない
    c, t, sent = _cycle([None], missing_s=1)
    for _ in range(10):
        t["now"] += 5
        assert c.step() == "unknown"
    assert not sent


def test_helper_state_reads_panel_button_then_log_window():
    from akm.helper_state import BUTTON_OFFSET, HelperStateReader

    rng = np.random.default_rng(1)
    bg = cv2.GaussianBlur(rng.integers(0, 255, (1050, 1680, 3), dtype=np.uint8), (0, 0), 6)
    tp = PC / "templates"
    panel, on, off = (cv2.imread(str(tp / f)) for f in ("helper_panel.png", "helper_on.png", "helper_off.png"))
    log = cv2.imread(str(tp / "hunting_log_title.png"))
    reader = HelperStateReader(PC)

    def put(img, tpl, x, y):
        img[y:y + tpl.shape[0], x:x + tpl.shape[1]] = tpl

    for btn, want in ((on, True), (off, False)):
        img = bg.copy()
        put(img, panel, 116, 23)
        put(img, btn, 116 + BUTTON_OFFSET[0], 23 + BUTTON_OFFSET[1])
        r = reader.read(img)
        assert r.state is want, r
    img = bg.copy()  # パネルが無く、ログ窓だけ見える → 動作中
    put(img, log, 900, 600)
    assert reader.read(img).state is True
    assert reader.read(bg).state is None  # どちらも無い → 不明 (押さない)


def test_helper_cycle_does_nothing_when_game_not_active():
    c, t, sent = _cycle([False], missing_s=0)
    c.active = lambda: False
    t["now"] = 10_000
    assert c.step() == "inactive" and not sent


def test_hunting_log_detector_finds_window_anywhere():
    from akm.hunting_log import DEFAULT_TEMPLATES, HuntingLogDetector

    rng = np.random.default_rng(0)
    bg = cv2.GaussianBlur(rng.integers(0, 255, (1050, 1680, 3), dtype=np.uint8), (0, 0), 6)
    det = HuntingLogDetector(PC, threshold=0.7)
    assert not det.score(bg).visible
    tpl = cv2.imread(str(PC / DEFAULT_TEMPLATES[0]))
    for x, y in ((100, 80), (1300, 700)):
        img = bg.copy()
        img[y:y + tpl.shape[0], x:x + tpl.shape[1]] = tpl
        m = det.score(img)
        assert m.visible and abs(m.pos[0] - x) <= 4 and abs(m.pos[1] - y) <= 4
