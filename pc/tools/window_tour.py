"""窓の種類の認識 (ESP32 で動かす) の見本を集める。

    python tools\\window_tour.py              窓を 1 つずつ開いて見本を撮る (下の手順)
    python tools\\window_tour.py auto         貯めた画面 (dataset/frames など) から、登録済みの窓の認識
                                             (edge_objects.py) で見つけた窓と「窓の無い画面」を見本に加える
    python tools\\window_tour.py list         集めた見本の数

手順 (tour):
  1. 窓を全部閉じて Enter → 窓の無い画面を撮る (比べる元)
  2. ゲームで窓を 1 つ開いて Enter → 元の画面と違う四角い所を窓として自動で囲む
  3. 囲みが合っていれば Enter (違えば r で囲み直す)、窓の名前を番号か英字で入力
  4. 同じ窓のまま歩いたり中身を変えて Enter でもう 1 枚 (3〜5 枚あると良い)。n で次の窓、q で終わり
見本は dataset/windows/ に画面 (png) と labels.jsonl (どの窓がどこにあるか) で保存する。
"""
import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

import _path  # noqa: F401

import cv2
import numpy as np

from akm.config import PC_DIR
from akm.edge.windows import WINDOW_TITLES

OUT = PC_DIR / "dataset" / "windows"
LABELS = OUT / "labels.jsonl"
FRAME_DIRS = ["frames", "helper_state", "screens", "ui"]


def load_labels(path: Path = LABELS) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def append_label(entry: dict, path: Path = LABELS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def save_frame(img: np.ndarray, tag: str, sub: str = "") -> str:
    d = OUT / sub if sub else OUT
    d.mkdir(parents=True, exist_ok=True)
    name = f"{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}_{tag}.png"
    cv2.imencode(".png", img)[1].tofile(str(d / name))
    return str((d / name).relative_to(OUT)).replace("\\", "/")


def diff_box(base: np.ndarray, img: np.ndarray, min_frac: float = 0.004) -> tuple[int, int, int, int] | None:
    """元の画面と違う所のうち、一番大きくて四角く詰まった所の枠。窓は不透明な四角なので、
    動くモンスターや光 (小さい・まばら) より大きく、中までびっしり違う。"""
    if base.shape != img.shape:
        return None
    a = cv2.cvtColor(base, cv2.COLOR_BGR2GRAY)
    b = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    d = (cv2.absdiff(a, b) > 22).astype(np.uint8)
    k = max(3, img.shape[0] // 150)
    d = cv2.morphologyEx(d, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k * 3, k * 3)))
    d = cv2.morphologyEx(d, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    n, _, stats, _ = cv2.connectedComponentsWithStats(d)
    H, W = d.shape
    best, score = None, 0.0
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < min_frac * W * H or w < 20 or h < 20:
            continue
        fill = area / float(w * h)
        s = area * fill  # 大きくて詰まっているほど窓らしい
        if fill > 0.45 and s > score:
            best, score = (int(x), int(y), int(w), int(h)), s
    return best


def show_box(img: np.ndarray, box, title: str) -> int:
    out = img.copy()
    if box:
        x, y, w, h = box
        cv2.rectangle(out, (x, y), (x + w, y + h), (0, 255, 0), 3)
    s = min(1.0, 1200 / img.shape[1], 650 / img.shape[0])
    cv2.imshow(title, cv2.resize(out, None, fx=s, fy=s, interpolation=cv2.INTER_AREA))
    k = cv2.waitKey(0) & 0xFF
    cv2.destroyAllWindows()
    return k


def select_box(img: np.ndarray, title: str):
    s = min(1.0, 1200 / img.shape[1], 650 / img.shape[0])
    shown = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    x, y, w, h = cv2.selectROI(title, shown, showCrosshair=False)
    cv2.destroyAllWindows()
    if w == 0 or h == 0:
        return None
    return int(x / s), int(y / s), int(round(w / s)), int(round(h / s))


def ask_name(known: list[str]) -> str | None:
    names = list(WINDOW_TITLES) + [n for n in known if n not in WINDOW_TITLES and n != "none"]
    for i, n in enumerate(names, 1):
        titles = WINDOW_TITLES.get(n, ([""], None))[0]
        print(f"  {i:2d}. {n:16s} {titles[0]}")
    while True:
        s = input("窓の名前 (番号か英小文字。新しい名前も可、空 Enter でやめる): ").strip().lower()
        if not s:
            return None
        if s.isdigit() and 1 <= int(s) <= len(names):
            return names[int(s) - 1]
        if s.replace("_", "").isalnum() and s.isascii():
            return s
        print("  英小文字・数字・_ だけにしてください")


def open_screen():
    from akm.config import ask_obs_password, load_config
    from akm.screen import GameScreen

    cfg = load_config("config.yaml")
    ask_obs_password(cfg, "config.yaml")
    screen = GameScreen.from_config(cfg)
    screen.locate()
    return screen


def cmd_tour(args) -> None:
    screen = open_screen()
    input("[tour] ゲームの窓を全部閉じて Enter (窓の無い画面を撮ります)")
    base = screen.grab()
    H, W = base.shape[:2]
    print(f"[tour] 画面 {W}x{H}")
    if (W, H) != (1366, 768):
        print("[tour] 注意: いつもの 1366x768 と違います (OBS のゲームキャプチャか確かめてください)")
    append_label({"file": save_frame(base, "none"), "w": W, "h": H, "windows": [], "src": "tour"})
    known = sorted({w["name"] for e in load_labels() for w in e.get("windows", [])})
    while True:
        s = input("[tour] ゲームで窓を 1 つ開いて Enter (q で終わり / b で窓の無い画面を撮り直し): ").strip().lower()
        if s == "q":
            break
        if s == "b":
            base = screen.grab()
            append_label({"file": save_frame(base, "none"), "w": W, "h": H, "windows": [], "src": "tour"})
            print("[tour] 撮り直しました")
            continue
        img = screen.grab()
        box = diff_box(base, img)
        if box is None:
            print("[tour] 違う所が見つかりません。自分で囲んでください")
        k = show_box(img, box, "window: Enter = OK / r = redraw / Esc = skip")
        if k == 27:
            continue
        if k in (ord("r"), ord("R")) or box is None:
            box = select_box(img, "draw the window frame, then Enter")
            if box is None:
                continue
        name = ask_name(known)
        if not name:
            continue
        if name not in known:
            known.append(name)
        n = 0
        while True:
            append_label({"file": save_frame(img, name), "w": W, "h": H, "src": "tour",
                          "windows": [{"name": name, "box": list(box)}]})
            n += 1
            print(f"[tour] {name} {n} 枚目を保存 (枠 {box})")
            s = input("  同じ窓のまま歩く・中身を変えるなどして Enter でもう 1 枚 / n で次の窓: ").strip().lower()
            if s:
                break
            img = screen.grab()
            b2 = diff_box(base, img)
            if b2 and _iou(b2, box) < 0.7:
                print(f"  枠が動きました {box} → {b2} (窓を動かした? このまま使います)")
                box = b2
    cmd_list(args)


def _iou(a, b) -> float:
    from akm.edge.winfeat import iou

    return iou(a, b)


def make_readers(use_teacher: bool = True):
    """窓のタイトルを読む OCR: 自分の OCR と、先生役 (Windows の OCR)。無ければ None。"""
    from akm.config import load_config

    ocr = teacher = None
    try:
        from akm.edge.ocr import OcrReader

        ocr = OcrReader.load()
    except FileNotFoundError:
        pass
    if use_teacher:
        try:
            from akm.maploc import make_ocr

            cfg = load_config("config.yaml") if (PC_DIR / "config.yaml").exists() else {}
            lcfg = cfg.get("maplog") or {}
            teacher = make_ocr(lcfg.get("ocr", "auto"), lcfg.get("tesseract_cmd"))
        except Exception:
            teacher = None
    return ocr, teacher


def label_frame(img, det, wins: set, ocr, teacher) -> tuple[list[dict], bool]:
    """1 枚の画面の窓。(窓の一覧, 何か窓らしいものがあるか)
    1. 窓のパネルを探し、タイトルの帯を OCR (Windows) で読んで WINDOW_TITLES と照らす → 名前とパネル全体の枠 (ocr)
    2. 読めなかった窓は、登録した窓の認識で見つかれば足す (det)"""
    from akm.edge.windows import find_panels, read_titles

    panels = read_titles(img, find_panels(img), ocr, teacher) if (ocr or teacher) else []
    windows = [{"name": p.name, "box": [p.x, p.y, p.w, p.h], "src": "ocr", "title": p.title}
               for p in panels if p.name]
    have = {w["name"] for w in windows}
    found = [r for r in det.detect(img) if r.present] if det else []
    windows += [{"name": r.name, "box": [int(v) for v in r.box], "src": "det"}
                for r in found if r.name in wins and r.name not in have]
    others = {r.name for r in found} - {"helper_running", "helper_stopped", "helper_panel"}
    return windows, bool(panels or others)


def cmd_auto(args) -> None:
    """貯めた画面に窓のラベルを付ける。Windows の OCR で窓のタイトルを読む (src=ocr) のが基本で、
    読めなかった窓は登録済みの窓の認識 (src=det) で補う。窓らしいものが何も無い画面は
    「窓の無い画面」として全部で max_none 枚まで。"""
    from akm.edge.objects import ObjectDetector

    det = ObjectDetector.load()
    wins = {o.name for o in det.objects if o.kind == "window"}
    ocr, teacher = make_readers(not args.no_teacher)
    if teacher is None:
        print("[auto] Windows の OCR が使えません (自分の OCR と登録した窓の認識だけで付けます)")
    entries = load_labels()
    scanned_file = OUT / "scanned.txt"
    done = {e.get("orig") for e in entries}
    if scanned_file.exists():
        done |= set(scanned_file.read_text(encoding="utf-8").splitlines())
    files = []
    for d in FRAME_DIRS:
        files += list((PC_DIR / "dataset" / d).rglob("*.png"))
    files = [f for f in files if str(f) not in done]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    if args.n:
        files = files[:args.n]
    cnt = Counter()
    rng = np.random.default_rng(0)
    # 窓の無い画面は全部で max_none 枚まで (学習のたびに増え続けないように)
    none_left = args.max_none - sum(1 for e in entries if e.get("src") != "tour" and not e.get("windows"))
    scanned = []
    for i, f in enumerate(files, 1):
        scanned.append(str(f))
        img = cv2.imread(str(f)) if f.exists() else None
        if img is None or img.shape[0] < 300:
            continue
        windows, something = label_frame(img, det, wins, ocr, teacher)
        if not windows:
            # 窓らしいものが何も無い画面だけ「窓の無い画面」にする (読めない窓が混ざらないように)
            if something or none_left <= 0 or rng.random() > 0.3:
                continue
            none_left -= 1
        H, W = img.shape[:2]
        tag = windows[0]["name"] if windows else "none"
        src = "ocr" if any(w["src"] == "ocr" for w in windows) else "det"
        append_label({"file": save_frame(img, tag, "auto"), "w": W, "h": H, "windows": windows, "src": src,
                      "orig": str(f)})
        for w in windows or [{"name": "none", "src": src}]:
            cnt[f"{w['name']}({w['src']})"] += 1
        if i % 100 == 0:
            print(f"[auto] {i}/{len(files)}  {dict(cnt)}")
    if scanned:
        OUT.mkdir(parents=True, exist_ok=True)
        with open(scanned_file, "a", encoding="utf-8") as fh:
            fh.write("\n".join(scanned) + "\n")
    print(f"[auto] 追加: {dict(cnt) or 'なし'}")
    cmd_list(args)


def cmd_list(args) -> None:
    cnt, src = Counter(), Counter()
    for e in load_labels():
        ws = e.get("windows", [])
        for w in ws or [{"name": "none"}]:
            cnt[w["name"]] += 1
            src[(w["name"], w.get("src", e.get("src", "?")))] += 1
    if not cnt:
        print("見本がありません (python tools\\window_tour.py で集める)")
        return
    print("窓の見本の数 (ocr = Windows の OCR でタイトルを読んだ / tour = 手で撮った / det = 登録した窓の認識):")
    for n, c in cnt.most_common():
        print(f"  {n:16s} {c:5d}  (ocr {src[(n, 'ocr')]}, tour {src[(n, 'tour')]}, det {src[(n, 'det')]})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="tour", choices=["tour", "auto", "list"])
    ap.add_argument("--n", type=int, default=0, help="auto: 新しい画面から何枚調べるか (0 = 全部)")
    ap.add_argument("--no-teacher", action="store_true", help="auto: Windows の OCR を使わない")
    ap.add_argument("--max-none", type=int, default=400, help="auto: 窓の無い画面を何枚まで加えるか")
    args = ap.parse_args()
    {"tour": cmd_tour, "auto": cmd_auto, "list": cmd_list}[args.cmd](args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
