"""今いるマップと座標を読み取り続け、SQLite (data/mu_map.db) に記録する。

    python tools\\map_logger.py            記録する (Ctrl+C で終了)
    python tools\\map_logger.py --check    1 回だけ読んで、切り出し画像と OCR の結果を確認する
    python tools\\map_logger.py --show     読み取り位置の切り出しを窓に表示しながら記録する

画面右下 (ミニマップの下) の "Atlans (32, 68)" の表示を読む。位置がずれていたら
config.yaml の maplog.roi を調整する (--check で保存される maplog_crop.png を見る)。
ボットや mirror_only.py と同時に動かしてよい。
"""
import argparse
import time

import _path  # noqa: F401
import cv2

from akm.config import PC_DIR, ask_obs_password, load_config
from akm.maploc import LocationReader, MapDB, preprocess
from akm.screen import GameScreen


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--check", action="store_true", help="1 回だけ読んで結果を表示する")
    ap.add_argument("--show", action="store_true", help="切り出し画像を表示する")
    args = ap.parse_args()

    cfg = load_config(args.config)
    mcfg = cfg.get("maplog") or {}
    ask_obs_password(cfg, args.config)
    screen = GameScreen.from_config(cfg)
    screen.locate()
    reader = LocationReader(mcfg)
    print(f"[map] OCR: {reader.ocr.name}  読み取り範囲 roi={reader.roi}")

    if args.check:
        img = screen.grab()
        crop = reader.crop(img)
        cv2.imwrite(str(PC_DIR / "maplog_crop.png"), crop)
        cv2.imwrite(str(PC_DIR / "maplog_ocr.png"), preprocess(crop, reader.scale, "bright", reader.threshold))
        loc = reader.read_raw(img)
        print(f"[map] OCR の文字: {reader.last_text!r}")
        print(f"[map] 読み取り結果: {loc}")
        print(f"[map] 切り出し画像: {PC_DIR / 'maplog_crop.png'} / OCR に渡した画像: {PC_DIR / 'maplog_ocr.png'}")
        return

    db_path = mcfg.get("db", "data/mu_map.db")
    db = MapDB(PC_DIR / db_path, heartbeat_s=float(mcfg.get("heartbeat_s", 10)))
    print(f"[map] 記録先: {db.path}  (Ctrl+C で終了)")
    db.event("logger_start", None)
    interval = float(mcfg.get("interval_s", 0.5))
    last_print = 0.0
    last_fail_msg = 0.0
    shown = None
    try:
        while True:
            t0 = time.monotonic()
            try:
                img = screen.grab()
            except Exception as e:
                if time.monotonic() - last_fail_msg > 10:
                    last_fail_msg = time.monotonic()
                    print(f"[map] 画面を取得できません: {e}")
                time.sleep(1)
                continue
            if args.show:
                cv2.imshow("map roi", cv2.resize(reader.crop(img), None, fx=2, fy=2))
                cv2.waitKey(1)
            loc = reader.read(img)
            if loc is not None:
                kind = db.record(loc)
                if kind == "map_change" or shown is None:
                    print(f"[map] マップ: {loc.map} ({loc.x}, {loc.y})")
                elif kind == "warp":
                    print(f"[map] ワープ: {loc.map} ({loc.x}, {loc.y})")
                shown = loc
            elif reader.reads > 5 and reader.fails == reader.reads and time.monotonic() - last_fail_msg > 10:
                last_fail_msg = time.monotonic()
                print(f"[map] 位置を読めません (OCR: {reader.last_text!r})。--check で読み取り範囲を確認してください")
            if time.monotonic() - last_print > 10 and shown is not None:
                last_print = time.monotonic()
                n = db.db.execute("SELECT COUNT(*) FROM cells WHERE map=?", (shown.map,)).fetchone()[0]
                ok = 100 * (reader.reads - reader.fails) / max(1, reader.reads)
                print(f"[map] {shown.map} ({shown.x}, {shown.y})  記録したマス {n}  読み取り成功率 {ok:.0f}%")
            time.sleep(max(0.0, interval - (time.monotonic() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        db.event("logger_stop", db.last)
        db.close()


if __name__ == "__main__":
    main()
