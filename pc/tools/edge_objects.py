"""窓・アイコンの認識 (ESP32 でも動く形) を登録・学習・確認する。

    python tools\\edge_objects.py list                       登録済みの窓・アイコン
    python tools\\edge_objects.py add inventory --kind window --key v 画面.png
                                                           画面から窓 (タイトルや枠など変わらない部分) を囲んで登録
    python tools\\edge_objects.py add-screen inventory --kind window --key v
                                                           今のゲーム画面を撮って、その場で囲んで登録
    python tools\\edge_objects.py import-ui                  helper_cycle の --make-ui-template で作った窓を取り込む
    python tools\\edge_objects.py train                      学習して models/edge_obj.npz と
                                                           firmware/AutoKeyMouse/edge_obj_model.h (ESP32 用) を書き出す
    python tools\\edge_objects.py find 画面.png               画面で探して結果を表示 (画面_found.png に枠を描く)

学習に使う画面: dataset/frames (mirror_only --learn で集まる)、dataset/helper_state、dataset/screens、dataset/ui の png。
登録した切り抜きを拡大縮小・明るさ・圧縮で変えた「ある」見本と、画面のいろいろな場所の「無い」見本で学ぶ。
"""
import random
import sys
from pathlib import Path

import _path  # noqa: F401

import cv2
import numpy as np

from akm.config import PC_DIR
from akm.edge.mlp import train
from akm.edge.objects import DATA, MODEL, ObjectDetector, load_objects, patch_features, save_object

HEADER = PC_DIR.parent / "firmware" / "AutoKeyMouse" / "edge_obj_model.h"
FRAME_DIRS = ["frames", "helper_state", "screens", "ui"]


def frames(limit: int = 300) -> list[np.ndarray]:
    files = []
    for d in FRAME_DIRS:
        files += list((PC_DIR / "dataset" / d).rglob("*.png"))
    random.Random(0).shuffle(files)
    out = []
    for f in files[:limit]:
        img = cv2.imread(str(f))
        if img is not None and img.shape[0] >= 300:
            out.append(img)
    return out


def augment(gray: np.ndarray, rng: random.Random) -> np.ndarray:
    h, w = gray.shape
    # 少しずれた切り出し + 画面の大きさ (縮小・拡大) + 明るさ + 圧縮
    dx, dy = int(w * rng.uniform(0, 0.08)), int(h * rng.uniform(0, 0.08))
    g = gray[dy:h - int(h * rng.uniform(0, 0.08)), dx:w - int(w * rng.uniform(0, 0.08))]
    s = rng.uniform(0.35, 1.6)
    g = cv2.resize(g, (max(4, int(g.shape[1] * s)), max(4, int(g.shape[0] * s))), interpolation=cv2.INTER_AREA)
    g = np.clip(g.astype(np.float32) * rng.uniform(0.75, 1.25) + rng.uniform(-20, 20), 0, 255).astype(np.uint8)
    if rng.random() < 0.5:
        _, enc = cv2.imencode(".jpg", g, [cv2.IMWRITE_JPEG_QUALITY, rng.randint(50, 95)])
        g = cv2.imdecode(enc, cv2.IMREAD_GRAYSCALE)
    return g


def cmd_train(args) -> None:
    objs = load_objects()
    if not objs:
        raise SystemExit("登録された窓・アイコンがありません (add で登録)")
    labels = [o.name for o in objs] + ["background"]
    bg = len(objs)
    rng = random.Random(0)
    X, F, Y = [], [], []

    def add(gray_patch, label):
        if gray_patch.size and min(gray_patch.shape) >= 3:
            b, f = patch_features(gray_patch)
            X.append(b)
            F.append(f)
            Y.append(label)

    for i, o in enumerate(objs):
        for _ in range(args.pos):
            add(augment(o.gray, rng), i)
    det = ObjectDetector(objs, None)
    shots = frames()
    print(f"[train] 登録 {len(objs)} 個、学習に使う画面 {len(shots)} 枚")
    found = {o.name: 0 for o in objs}
    for img in shots:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, None, fx=det.WORK, fy=det.WORK, interpolation=cv2.INTER_AREA)
        for i, o in enumerate(objs):
            ncc, (x, y, w, h) = det.candidate(o, gray, small)
            patch = gray[max(0, y):y + h, max(0, x):x + w]
            if ncc >= 0.9:          # 画面に本当にある: 実物の「ある」見本
                found[o.name] += 1
                for _ in range(4):
                    add(augment(patch, rng), i)
                for _ in range(6):  # 少しずれた場所は「無い」 (隣の別のアイコンを取り違えないように)
                    sx = int(rng.choice([-1, 1]) * rng.uniform(0.35, 1.0) * w)
                    sy = int(rng.choice([-1, 1]) * rng.uniform(0.35, 1.0) * h)
                    if 0 <= x + sx and x + sx + w <= gray.shape[1] and 0 <= y + sy and y + sy + h <= gray.shape[0]:
                        add(gray[y + sy:y + sy + h, x + sx:x + sx + w], bg)
            elif ncc < det.hi:      # 一番似ているが違う場所: 紛らわしい「無い」見本
                add(patch, bg)
            for _ in range(3):      # いろいろな場所の「無い」見本
                if w < gray.shape[1] and h < gray.shape[0]:
                    rx, ry = rng.randint(0, gray.shape[1] - w), rng.randint(0, gray.shape[0] - h)
                    if abs(rx - x) > w or abs(ry - y) > h:
                        add(gray[ry:ry + h, rx:rx + w], bg)
    X, F, Y = np.array(X, np.uint8), np.array(F, np.float32), np.array(Y, np.int64)
    print(f"[train] 見本 {len(Y)} 個 (無い {int((Y == bg).sum())})。画面の中に実物があった数: "
          + ", ".join(f"{k} {v}" for k, v in found.items()))
    model = train(X, F, Y, labels, hidden=args.hidden, epochs=args.epochs, log=(print if args.verbose else lambda s: None))
    q = model.quantize(X[:3000], F[:3000])
    acc = (q.predict(X, F)[0] == Y).mean()
    MODEL.parent.mkdir(parents=True, exist_ok=True)
    q.save(MODEL)
    q.export_c(HEADER, name="edge_obj", note=f"窓・アイコン {len(objs)} 種: {', '.join(labels[:-1])}")
    print(f"[train] 学習した見本での正解率 {acc:.1%}。保存: {MODEL} と {HEADER}")
    if shots:
        d = ObjectDetector.load()
        for img in shots[:5]:
            print("   ", ", ".join(f"{r.name}{'○' if r.present else '×'}" for r in d.detect(img)))


def cmd_find(args) -> None:
    img = cv2.imread(args.image)
    if img is None:
        raise SystemExit(f"画像が読めません: {args.image}")
    d = ObjectDetector.load()
    out = img.copy()
    for r in d.detect(img):
        print(f"  {'○ ある' if r.present else '× 無い'}  {r.name:16s} 一致度 {r.ncc:.2f}  ネット {r.nn:.2f}  {r.box}")
        if r.present:
            x, y, w, h = r.box
            cv2.rectangle(out, (x, y), (x + w, y + h), (0, 255, 0), 2)
            cv2.putText(out, r.name, (x, max(12, y - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
    p = Path(args.image)
    cv2.imwrite(str(p.with_name(p.stem + "_found.png")), out)


def select_and_save(img: np.ndarray, args) -> None:
    print(f"[add] {args.name} の変わらない部分 (タイトル・枠・アイコン。中身の数値や品物は含めない) を囲んで Enter")
    x, y, w, h = cv2.selectROI(f"add: {args.name}", img, showCrosshair=False)
    cv2.destroyAllWindows()
    if w == 0 or h == 0:
        raise SystemExit("やめました")
    save_object(args.name, args.kind, img[y:y + h, x:x + w], img.shape[0], args.key, group=args.group)
    print(f"[add] {args.name} を登録しました ({w}x{h}, 画面の高さ {img.shape[0]})。train で学習し直してください")


def cmd_add(args) -> None:
    img = cv2.imread(args.image)
    if img is None:
        raise SystemExit(f"画像が読めません: {args.image}")
    select_and_save(img, args)


def cmd_add_screen(args) -> None:
    from akm.config import ask_obs_password, load_config
    from akm.screen import GameScreen

    cfg = load_config("config.yaml")
    ask_obs_password(cfg, "config.yaml")
    screen = GameScreen.from_config(cfg)
    screen.locate()
    img = screen.grab()
    shot = PC_DIR / "dataset" / "screens" / f"add_{args.name}.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(shot), img)
    select_and_save(img, args)


def cmd_import_ui(args) -> None:
    from akm.ui_windows import DEFAULT_WINDOWS

    n = 0
    for e in DEFAULT_WINDOWS:
        p = PC_DIR / e["template"]
        img = cv2.imread(str(p))
        if img is None:
            continue
        save_object(e["name"], "window", img, args.screen_h, e["key"])
        print(f"[import] {e['name']} ({p.name}, 開閉キー {e['key']})")
        n += 1
    print(f"[import] {n} 個取り込みました" + ("" if n else " (templates/ui_*.png がまだありません)"))


def cmd_list(args) -> None:
    for o in load_objects():
        print(f"  {o.name:16s} {o.kind:6s} {o.image.shape[1]}x{o.image.shape[0]} (画面の高さ {o.ref_h})"
              + (f"  開閉キー {o.key}" if o.key else ""))
    print(f"  学習済みモデル: {'あり' if MODEL.exists() else 'なし (train で作る)'}")


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("add", "add-screen"):
        a = sub.add_parser(name)
        a.add_argument("name")
        a.add_argument("--kind", choices=["window", "icon"], default="window")
        a.add_argument("--key", help="窓を開閉するキー (例 v)")
        a.add_argument("--group", help="同時には出ないもの同士の名前 (例 helper_button)。一番確からしい 1 つだけ残す")
        if name == "add":
            a.add_argument("image")
    im = sub.add_parser("import-ui")
    im.add_argument("--screen-h", type=int, default=1050, help="切り抜いた画面の高さ")
    t = sub.add_parser("train")
    t.add_argument("--pos", type=int, default=400, help="1 つあたりの「ある」見本の数 (登録画像を変形して作る)")
    t.add_argument("--hidden", type=int, default=64)
    t.add_argument("--epochs", type=int, default=40)
    t.add_argument("-v", "--verbose", action="store_true")
    f = sub.add_parser("find")
    f.add_argument("image")
    args = ap.parse_args()
    {"list": cmd_list, "add": cmd_add, "add-screen": cmd_add_screen, "import-ui": cmd_import_ui,
     "train": cmd_train, "find": cmd_find}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
