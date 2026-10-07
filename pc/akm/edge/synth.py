"""学習用の文字画像を作る (実際のゲーム画面を背景に、MU に近い字体で描く)。

MU の文字は太めのゴシック (Montserrat に近い) に暗い縁取り。色は白・青・金・赤・緑など。
大きさをいろいろに変えて描き、glyphs.line_glyphs と同じ手順で切り出すので、
本番 (ESP32 / PC) と同じ見え方の見本になり、画面の大きさに依らない学習ができる。
"""
from __future__ import annotations

import random
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .glyphs import line_glyphs

CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz()/:,.-+%[]!?'"
JUNK = len(CHARS)  # 文字ではない (背景の模様など)

FONT_URLS = {
    "Montserrat-Bold.ttf": "https://raw.githubusercontent.com/JulietaUla/Montserrat/master/fonts/ttf/Montserrat-Bold.ttf",
    "Montserrat-SemiBold.ttf": "https://raw.githubusercontent.com/JulietaUla/Montserrat/master/fonts/ttf/Montserrat-SemiBold.ttf",
    "Montserrat-ExtraBold.ttf": "https://raw.githubusercontent.com/JulietaUla/Montserrat/master/fonts/ttf/Montserrat-ExtraBold.ttf",
}
SYSTEM_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/tahomabd.ttf",
]
FILLS = [(255, 255, 255), (235, 235, 235), (120, 170, 255), (90, 140, 255), (255, 210, 80), (255, 190, 40),
         (255, 90, 90), (120, 230, 120), (200, 120, 255), (255, 150, 60), (180, 220, 255)]
WORDS = ("Lorencia Devias Noria Dungeon Atlans Lost Tower Tarkan Icarus Kanturu Aida Elbeland Raklion Karutan "
         "Vulcanus Arena Crywolf Barracks Refuge Swamp Calmness Acheron Debenter Nixies Lake Uruk Mountain "
         "Hunting Time Normal Exc Crit Total DMG Kills EXP Life Mana Zen Obtained Killed Events Helper MU "
         "Instant Log Inventory Character Strength Agility Vitality Energy Command Level Reset Master "
         "Jewel of Bless Soul Chaos Life Creation Guardian Silver Medal Gold Potion Healing Mana Large "
         "Spider Budge Dragon Bull Fighter Hound Elite Yeti Goblin Ice Monster Worm Skeleton Lich Giant").split()


def font_files(cache: Path) -> list[str]:
    """使える字体。Montserrat は無ければ取得を試みる (取れなければ手元の字体だけで学習)。"""
    cache.mkdir(parents=True, exist_ok=True)
    out = []
    for name, url in FONT_URLS.items():
        p = cache / name
        if not p.exists():
            try:
                import urllib.request

                urllib.request.urlretrieve(url, p)
            except Exception:
                continue
        out.append(str(p))
    out += [f for f in SYSTEM_FONTS if Path(f).exists()]
    if not out:
        raise RuntimeError("字体が見つかりません")
    return out


def random_text(rng: random.Random) -> str:
    k = rng.random()
    if k < 0.35:
        return " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 3)))
    if k < 0.55:  # 座標・数値の表示
        return rng.choice([f"{rng.choice(WORDS)} ({rng.randint(0, 255)}, {rng.randint(0, 255)})",
                           f"{rng.randint(0, 99):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}",
                           f"Life: {rng.randint(1, 9999)} / {rng.randint(1, 9999)}",
                           f"{rng.randint(0, 999999):,}", f"+{rng.randint(1, 99)}%", f"[{rng.choice(WORDS)}]"])
    return "".join(rng.choice(CHARS) for _ in range(rng.randint(3, 12)))


def render_line(text: str, font_path: str, size: int, fill, stroke: int, rng: random.Random,
                bg: np.ndarray) -> tuple[np.ndarray, list[tuple[str, int, int]]]:
    """1 行を描く。戻り値: (BGR 画像, [(文字, x0, x1), ...])"""
    font = ImageFont.truetype(font_path, size)
    pad = size // 2 + stroke + 2
    w = int(font.getlength(text)) + 2 * pad
    h = size + 2 * pad
    y_off = int(rng.uniform(0, max(1, bg.shape[0] - h)))
    x_off = int(rng.uniform(0, max(1, bg.shape[1] - w)))
    tile = bg[y_off:y_off + h, x_off:x_off + w]
    if tile.shape[0] < h or tile.shape[1] < w:
        tile = cv2.resize(bg, (w, h))
    img = Image.fromarray(cv2.cvtColor(tile, cv2.COLOR_BGR2RGB))
    d = ImageDraw.Draw(img)
    dark = tuple(int(c * rng.uniform(0, 0.25)) for c in fill)
    boxes = []
    x = pad
    for ch in text:
        if ch != " ":
            d.text((x, pad), ch, font=font, fill=fill, stroke_width=stroke, stroke_fill=dark)
            bx0, _, bx1, _ = d.textbbox((x, pad), ch, font=font)
            boxes.append((ch, bx0, bx1))
        x += font.getlength(ch)
    out = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    if rng.random() < 0.5:
        out = cv2.GaussianBlur(out, (0, 0), rng.uniform(0.3, 0.7))
    if rng.random() < 0.6:  # 圧縮のにじみ (OBS / 縮小表示)
        ok, enc = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, rng.randint(55, 95)])
        out = cv2.imdecode(enc, cv2.IMREAD_COLOR)
    return out, boxes


def make_samples(n_lines: int, backgrounds: list[np.ndarray], fonts: list[str], seed: int = 0):
    """切り出した文字の見本 (bits, feats, 文字) を作る。切り出しが文字数と合わない行は捨てる。"""
    rng = random.Random(seed)
    X, F, Y = [], [], []
    kept = 0
    for _ in range(n_lines):
        text = random_text(rng)
        size = rng.randint(9, 30)
        stroke = 0 if rng.random() < 0.15 else (1 if size < 20 else rng.choice([1, 2]))
        img, boxes = render_line(text, rng.choice(fonts), size, rng.choice(FILLS), stroke, rng,
                                 rng.choice(backgrounds))
        glyphs, _ = line_glyphs(img)
        if not glyphs:
            continue
        # 文字の描いた範囲に中心が入る切り出しを対応させる。1 文字に 1 つだけ対応したものを見本にし、
        # どの文字にも対応しない切り出しは「文字ではない (JUNK)」の見本にする (背景の模様を見分けるため)
        hits = [[] for _ in boxes]
        owner = [-1] * len(glyphs)
        for gi, g in enumerate(glyphs):
            cx = (g.x0 + g.x1) / 2
            for bi, (_, x0, x1) in enumerate(boxes):
                if x0 - 1 <= cx <= x1 + 1:
                    hits[bi].append(gi)
                    owner[gi] = bi
                    break
        kept += 1
        for bi, (ch, x0, x1) in enumerate(boxes):
            if len(hits[bi]) == 1:
                g = glyphs[hits[bi][0]]
                if abs((g.x1 - g.x0) - (x1 - x0)) <= max(2, 0.35 * (x1 - x0)):  # 隣とくっついた切り出しは使わない
                    X.append(g.bits.ravel())
                    F.append(g.feats)
                    Y.append(CHARS.index(ch))
        for gi, g in enumerate(glyphs):
            if owner[gi] < 0 and rng.random() < 0.15:
                X.append(g.bits.ravel())
                F.append(g.feats)
                Y.append(JUNK)
    return np.array(X, np.uint8), np.array(F, np.float32), np.array(Y, np.int64), kept
