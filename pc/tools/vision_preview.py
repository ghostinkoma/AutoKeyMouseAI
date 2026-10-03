"""認識結果をリアルタイム表示して ROI やしきい値を調整する (HID は送らない)。

    python tools/vision_preview.py                 # 表示のみ
    python tools/vision_preview.py --record 2.0    # 2 秒ごとに dataset/images に保存 (YOLO 学習用)

キー: s = 今の画面を保存 / q = 終了
"""
import argparse
import time

import _path  # noqa: F401
import cv2

from akm.config import PC_DIR, load_config
from akm.elf_bot import character_pos
from akm.screen import GameScreen
from akm.vision import bar_level, build_detectors, draw, roi_px


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--record", type=float, default=0.0, help="自動保存の間隔 (秒)")
    ap.add_argument("--scale", type=float, default=0.7)
    args = ap.parse_args()

    cfg = load_config(args.config)
    screen = GameScreen(cfg["game"]["window_title"], cfg["game"].get("process"))
    detectors = build_detectors(cfg.get("vision", {}), PC_DIR)
    out = PC_DIR / "dataset" / "images"
    out.mkdir(parents=True, exist_ok=True)
    last_rec = 0.0
    hud = cfg["hud"]
    while True:
        t0 = time.perf_counter()
        img = screen.grab()
        dets = [d for det in detectors for d in det.detect(img)]
        hp = bar_level(img, hud["hp"]["roi"], hud["hp"].get("color", "red"), hud["hp"].get("direction", "vertical"))
        mp = bar_level(img, hud["mp"]["roi"], hud["mp"].get("color", "blue"), hud["mp"].get("direction", "vertical"))
        ms = (time.perf_counter() - t0) * 1000
        vis = draw(img, dets, {"HP": f"{hp:.0%}", "MP": f"{mp:.0%}", "ms": f"{ms:.0f}"})
        for key, color in (("hp", (0, 0, 255)), ("mp", (255, 0, 0))):
            x0, y0, x1, y1 = roi_px(img.shape, hud[key]["roi"])
            cv2.rectangle(vis, (x0, y0), (x1, y1), color, 1)
        sr = cfg.get("vision", {}).get("templates", {}).get("search_roi")
        if sr:
            x0, y0, x1, y1 = roi_px(img.shape, sr)
            cv2.rectangle(vis, (x0, y0), (x1, y1), (128, 128, 128), 1)
        cx, cy = character_pos(img.shape, cfg["elf"].get("character_offset", [0, 0]))
        r = int(cfg["elf"]["pickup"].get("max_distance", 0.35) * img.shape[0])
        cv2.circle(vis, (int(cx), int(cy)), r, (0, 255, 0), 1)
        cv2.drawMarker(vis, (int(cx), int(cy)), (0, 255, 0), cv2.MARKER_CROSS, 20, 2)
        cv2.imshow("akm preview", cv2.resize(vis, None, fx=args.scale, fy=args.scale))

        now = time.time()
        k = cv2.waitKey(1) & 0xFF
        if k == ord("q"):
            break
        if k == ord("s") or (args.record and now - last_rec >= args.record):
            last_rec = now
            p = out / f"{time.strftime('%Y%m%d_%H%M%S')}_{int(now * 1000) % 1000:03d}.png"
            cv2.imwrite(str(p), img)
            print("saved", p.name)
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
