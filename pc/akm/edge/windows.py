"""ゲームの窓 (インベントリ・キャラクター・パーティ…) を、貯めた画面から自動で見つけて数える。

MU の窓は「暗い四角いパネル + 枠 + 上にタイトルの文字」という共通の見た目なので:
  1. 画面から四角いパネルを探す (はっきりした枠で囲まれ、中が暗い、ある程度大きい四角)
  2. パネルの上の帯 (タイトル) を文字認識 (自分 + 先生役の OCR) で読み、窓のタイトルの一覧と照らし合わせる
  3. 同じタイトルの窓をまとめ、何枚の画面で見たか数える (タイトルが読めない窓は大きさと位置でまとめる)
タイトルが一覧と一致し、何度も見た窓は、窓の認識 (akm/edge/objects.py) に自動で登録できる
(切り抜くのはタイトルの帯: 中身の品物や数値は変わるが、タイトルの帯は変わらないので)。
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field

import cv2
import numpy as np

# MU (Season 6 前後) の窓のタイトルと開閉キー。キーは確かめたもの (C / V) だけ入れる
# (間違ったキーだと窓を閉じるときに別の窓を開いてしまうので、分からないものは None)。名前は登録に使う英小文字
WINDOW_TITLES: dict[str, tuple[list[str], str | None]] = {
    "inventory": (["Inventory"], "v"),
    "character": (["Character", "Character Info", "Status"], "c"),
    "party": (["Party"], None),
    "guild": (["Guild"], None),
    "quest": (["Quest"], None),
    "friend": (["Friend", "Friends", "Messenger"], None),
    "skill_tree": (["Master Skill Tree", "Skill Tree", "Master Level"], None),
    "command": (["Command"], None),
    "option": (["Option", "Options", "Settings"], None),
    "pet": (["Pet", "Pet Info"], None),
    "trade": (["Trade"], None),
    "vault": (["Vault", "Warehouse", "Storage"], None),
    "personal_store": (["Personal Store", "Store", "Personal Shop"], None),
    "shop": (["Shop", "Merchant"], None),
    "chaos_machine": (["Chaos Machine", "Chaos Goblin", "Mix"], None),
    "gens": (["Gens", "Gens Info"], None),
    "map": (["Map", "World Map"], None),
    "cash_shop": (["Cash Shop", "In Game Shop", "X Shop"], None),
    "event_inventory": (["Event Inventory"], None),
    "mu_helper": (["MU Helper", "Helper Setup", "Helper"], None),
    "hunting_log": (["Instant Hunting Log", "Hunting Log"], None),
    "duel": (["Duel"], None),
    "lucky_coin": (["Lucky Coin", "Lucky Item"], None),
    "jewel_mix": (["Jewel Mix", "Jewel Combination"], None),
    "move": (["Move", "Teleport", "Warp"], None),
    "message": (["Message", "Mail", "Letter"], None),
}


def match_title(text: str, cutoff: float = 0.72) -> str | None:
    """読んだタイトルを窓の名前に。一覧のどれにも十分近くなければ None。"""
    t = " ".join(text.split()).lower()
    if len(t) < 3:
        return None
    best, score = None, 0.0
    for name, (titles, _) in WINDOW_TITLES.items():
        for title in titles:
            tl = title.lower()
            r = difflib.SequenceMatcher(None, t, tl).ratio()
            if tl in t and len(tl) >= 4:  # "Inventory (V)" のように余分な文字が付いていても
                r = max(r, 0.9)
            if r > score:
                best, score = name, r
    return best if score >= cutoff else None


@dataclass
class Panel:
    x: int
    y: int
    w: int
    h: int
    title_box: tuple[int, int, int, int]
    title: str = ""            # 読んだタイトル
    name: str | None = None    # 一覧と一致した窓の名前


def find_panels(img: np.ndarray, min_size: int = 110, max_frac: float = 0.95) -> list[Panel]:
    """四角い暗いパネル (窓の候補) を探す。大きさは画面の高さ 1050 を基準に min_size 以上。"""
    H, W = img.shape[:2]
    s = H / 1050
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 40, 120)
    edges = cv2.dilate(edges, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    out: list[Panel] = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w < min_size * s or h < min_size * s or w > max_frac * W or h > max_frac * H:
            continue
        approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
        if len(approx) < 4 or len(approx) > 8 or cv2.contourArea(c) < 0.8 * w * h:
            continue  # 四角くない
        inner = gray[y + h // 6:y + h - h // 6, x + w // 6:x + w - w // 6]
        if inner.size == 0 or inner.mean() > 110:
            continue  # 中が明るい (地面・空) はパネルではない
        th = max(int(26 * s), h // 12)
        out.append(Panel(x, y, w, h, (x, y, w, min(th, h))))
    # 入れ子・重なりは大きい方を残す
    out.sort(key=lambda p: -p.w * p.h)
    kept: list[Panel] = []
    for p in out:
        if all(not (p.x >= k.x - 4 and p.y >= k.y - 4 and p.x + p.w <= k.x + k.w + 4 and p.y + p.h <= k.y + k.h + 4)
               for k in kept):
            kept.append(p)
    return kept


def read_titles(img: np.ndarray, panels: list[Panel], ocr=None, teacher=None) -> list[Panel]:
    """パネルの上の帯を読んで、窓の名前を付ける。"""
    for p in panels:
        x, y, w, h = p.title_box
        band = img[y:y + h, x:x + w]
        texts = []
        if ocr is not None:
            texts.append(ocr.read(band)[0])
        if teacher is not None:
            try:
                from ..maploc import preprocess

                texts.append(teacher(preprocess(band, 2, "bright", 130)))
            except Exception:
                pass
        for t in texts:
            name = match_title(t)
            if name:
                p.title, p.name = " ".join(t.split()), name
                break
        else:
            p.title = " ".join((texts[0] if texts else "").split())
    return panels


@dataclass
class WindowType:
    key: str                     # 窓の名前 (一致しなかったら "unknown_幅x高さ@位置")
    name: str | None
    count: int = 0               # 見た画面の数
    titles: list[str] = field(default_factory=list)
    sample: tuple | None = None  # (画面, Panel) 1 つ


def discover(frames, ocr=None, teacher=None) -> list[WindowType]:
    """画面の列から窓の種類をまとめる。frames: [(名前, 画像), ...]"""
    types: dict[str, WindowType] = {}
    for fname, img in frames:
        H, W = img.shape[:2]
        seen = set()
        for p in read_titles(img, find_panels(img), ocr, teacher):
            if p.name:
                key = p.name
            else:  # 大きさ (画面の高さ比) と位置 (左 / 中 / 右) でまとめる
                pos = "left" if p.x + p.w / 2 < W / 3 else "right" if p.x + p.w / 2 > 2 * W / 3 else "center"
                key = f"unknown_{round(p.w / H * 20)}x{round(p.h / H * 20)}@{pos}"
            if key in seen:
                continue
            seen.add(key)
            t = types.setdefault(key, WindowType(key, p.name))
            t.count += 1
            if p.title and p.title not in t.titles and len(t.titles) < 5:
                t.titles.append(p.title)
            if t.sample is None:
                t.sample = (fname, img, p)
    return sorted(types.values(), key=lambda t: (t.name is None, -t.count))


def register(types: list[WindowType], min_count: int = 3, existing: set[str] | None = None, log=print,
             base=None) -> list[str]:
    """タイトルが一致し min_count 回以上見た窓を、窓の認識に登録する (タイトルの帯を切り抜いて)。"""
    from .objects import save_object

    done = []
    for t in types:
        if not t.name or t.count < min_count or (existing and t.name in existing):
            continue
        _, img, p = t.sample
        x, y, w, h = p.title_box
        kw = {"base": base} if base is not None else {}
        save_object(t.name, "window", img[y:y + h, x:x + w], img.shape[0], WINDOW_TITLES[t.name][1], **kw)
        done.append(t.name)
        log(f"[windows] 窓「{t.name}」を登録しました (見た画面 {t.count} 枚, タイトル {t.titles[:1]})")
    return done
