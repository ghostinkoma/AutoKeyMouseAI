"""エルフボットを起動する。

    python run_bot.py                   # config.yaml を使って実行
    python run_bot.py --show            # 認識結果をウィンドウ表示しながら実行
    python run_bot.py --dry-run --show  # ESP32 に送らず、送る予定のコマンドを表示するだけ
"""
from __future__ import annotations

import argparse

from akm.config import PC_DIR, abs_map_from, ask_obs_password, load_config
from akm.device import open_device
from akm.collector import LabelCollector
from akm.elf_bot import ElfBot
from akm.helper import HelperControl
from akm.mirror import Mirror
from akm.popups import PopupGuard
from akm.screen import GameScreen
from akm.vision import build_detectors


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--show", action="store_true", help="認識結果を表示する")
    ap.add_argument("--dry-run", action="store_true", help="HID を送らない")
    args = ap.parse_args()

    cfg = load_config(args.config)
    ask_obs_password(cfg, args.config)
    screen = GameScreen.from_config(cfg)
    device = open_device(cfg, abs_map_from(cfg), dry_run=args.dry_run)
    detectors = build_detectors(cfg.get("vision", {}), PC_DIR)
    helper = HelperControl(cfg.get("helper", {}), device, PC_DIR)
    ccfg = (cfg.get("vision") or {}).get("collect") or {}
    tcfg = (cfg.get("vision") or {}).get("templates") or {}
    ccfg.setdefault("search_roi", tcfg.get("search_roi"))
    ccfg.setdefault("exclude_rois", tcfg.get("exclude_rois", []))
    collector = LabelCollector(ccfg, PC_DIR) if ccfg.get("enabled", True) else None
    mcfg = cfg.get("mirror") or {}
    host = (cfg.get("device") or {}).get("host")
    mirror = None
    if mcfg.get("enabled", True) and not args.dry_run:
        from akm.bad16 import parse_color

        mirror = Mirror(host, float(mcfg.get("interval_s", 0.1)), fmt=mcfg.get("format", "bad16"),
                        quality=int(mcfg.get("quality", 70)), grab=screen.grab_preview,
                        transport=mcfg.get("transport", "auto"), port=int(mcfg.get("port", 5005)),
                        b16_bits=parse_color(mcfg.get("color", "343")),
                        b16_prev_free=not mcfg.get("with_prev", False),
                        b16_gray=bool(mcfg.get("gray", False)))
        if mirror.enabled:
            print(f"[mirror] ESP32 ({host}) の液晶にゲーム画面を {mirror.interval:.1f} 秒ごとに送ります ({mirror.fmt})")
    popups = PopupGuard(cfg.get("popups"), device, PC_DIR)
    if popups.entries:
        print(f"[popup] 自動で断るダイアログ: {', '.join(e['name'] for e in popups.entries)}")
    ElfBot(cfg, screen, device, detectors, show=args.show, helper=helper, collector=collector, mirror=mirror,
           popups=popups).run()


if __name__ == "__main__":
    main()
