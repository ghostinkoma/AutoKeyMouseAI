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


def make_bot(frames, det_seq, mode="manual"):
    from akm.helper import HelperControl

    cfg = yaml.safe_load((PC / "config.example.yaml").read_text(encoding="utf-8"))
    cfg["elf"]["heal"] = None
    cfg["elf"]["mode"] = mode
    cfg["helper"]["settle_ms"] = 0
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
    assert rec.sent == ["k:f9"]
    assert bot.helper.is_on()
    rec.sent.clear()
    bot.step()  # 宝石: クリックで拾う (MU Helper は止まる) → 戻る → F9 で再開
    assert rec.sent[0].endswith("c:L")
    assert rec.sent[-1] == "k:f9"
    assert not any(s.startswith("bd:R") or "bd:R" in s for s in rec.sent)  # 自分では攻撃しない
    assert bot.picked["Jewel of Bless"] == 1 and bot.helper.is_on()
