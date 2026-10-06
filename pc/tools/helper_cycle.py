"""MU Helper を 10 分おきに再起動し、止まっていたら再開する (Instant Hunting Log 窓の表示で判定)。

    python tools\\helper_cycle.py                 実行 (Ctrl+C で終了)
    python tools\\helper_cycle.py --dry-run       キーは送らず、判定と動作の予定だけ表示
    python tools\\helper_cycle.py --watch         判定 (一致度) を表示し続けるだけ
    python tools\\helper_cycle.py --once          今すぐ 1 回再起動して終わる (キー操作の確認用)

学習 (表示されているかの判定の閾値を決める):
    python tools\\helper_cycle.py --collect on    MU Helper 動作中の画面を集める (窓をあちこちに動かしながら)
    python tools\\helper_cycle.py --collect off   MU Helper 停止中の画面を集める
    python tools\\helper_cycle.py --calibrate     集めた画面から閾値を決めて templates/hunting_log.json に保存
    python tools\\helper_cycle.py --make-template dataset\\hunting_log\\on\\0001.png
                                                  画面からタイトルバー / 項目名の列を選んでテンプレートを作り直す

設定は config.yaml の helper_cycle: (無ければ既定値。config.example.yaml 参照)。
"""
import time
from pathlib import Path

import _path  # noqa: F401

from akm.config import PC_DIR, abs_map_from, ask_obs_password, load_config
from akm.device import open_device
from akm.helper_cycle import CycleConfig, HelperCycle
from akm.hunting_log import CALIB_FILE, DEFAULT_TEMPLATES, HuntingLogDetector, calibrate
from akm.screen import GameScreen

DATA = PC_DIR / "dataset" / "hunting_log"


def collect(screen: GameScreen, det: HuntingLogDetector, label: str, n: int, every: float) -> None:
    import cv2

    out = DATA / label
    out.mkdir(parents=True, exist_ok=True)
    start = len(list(out.glob("*.png")))
    print(f"[collect] {out} に {n} 枚集めます ({every:.1f} 秒ごと)。"
          + ("窓をあちこちに動かしてください" if label == "on" else "MU Helper を止めた状態で、いろいろな場面を映してください"))
    for i in range(n):
        img = screen.grab()
        p = out / f"{start + i + 1:04d}.png"
        cv2.imwrite(str(p), img)
        m = det.score(img)
        print(f"[collect] {p.name}  一致度 {m.score:.3f}  位置 {m.pos}")
        time.sleep(every)


def make_template(png: Path) -> None:
    import cv2

    img = cv2.imread(str(png))
    if img is None:
        raise SystemExit(f"画像が読めません: {png}")
    for name, what in zip(DEFAULT_TEMPLATES, ("赤いタイトルバーの「Instant Hunting Log」の文字",
                                               "左側の項目名の列 (Hunting Time 〜 Time to 400。数値は含めない)")):
        print(f"[template] {what} をマウスで囲んで Enter (やめるときは c)")
        x, y, w, h = cv2.selectROI(f"select: {what}", img, showCrosshair=False)
        cv2.destroyAllWindows()
        if w == 0 or h == 0:
            print("[template] 飛ばしました")
            continue
        cv2.imwrite(str(PC_DIR / name), img[y:y + h, x:x + w])
        print(f"[template] {name} を保存しました ({w}x{h})")


def main() -> None:
    import argparse
    import json

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="キーを送らない")
    ap.add_argument("--watch", action="store_true", help="判定だけ表示し続ける")
    ap.add_argument("--once", action="store_true", help="今すぐ 1 回再起動して終わる")
    ap.add_argument("--interval", type=float, help="再起動の間隔 (秒)。既定 600")
    ap.add_argument("--ignore-active", action="store_true", help="ゲームが前面でなくてもキーを送る")
    ap.add_argument("--collect", choices=["on", "off"], help="学習用の画面を集める")
    ap.add_argument("-n", type=int, default=30, help="--collect で集める枚数")
    ap.add_argument("--every", type=float, default=1.0, help="--collect の間隔 (秒)")
    ap.add_argument("--calibrate", action="store_true", help="集めた画面から閾値を決める")
    ap.add_argument("--make-template", type=Path, help="この画像からテンプレートを作り直す")
    args = ap.parse_args()

    if args.make_template:
        make_template(args.make_template)
        return
    if args.calibrate:
        det = HuntingLogDetector(PC_DIR, threshold=0.0)
        r = calibrate(det, DATA / "on", DATA / "off")
        print(f"[calibrate] 表示中 {r['on']} 枚 (一致度の最小 {r['on_min']}) / 非表示 {r['off']} 枚 (最大 {r['off_max']})")
        if not r["separated"]:
            print(f"[calibrate] 注意: 表示中と非表示が分けきれません (誤り {r['errors']} 枚)。"
                  "--make-template でテンプレートを作り直すか、画面を増やしてください")
        (PC_DIR / CALIB_FILE).write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[calibrate] 閾値 {r['threshold']} を {CALIB_FILE} に保存しました")
        return

    cfg = load_config("config.yaml")
    ask_obs_password(cfg, "config.yaml")
    screen = GameScreen.from_config(cfg)
    screen.locate()
    det = HuntingLogDetector(PC_DIR)
    print(f"[cycle] Hunting Log の判定閾値 {det.threshold:.3f}" + ("" if (PC_DIR / CALIB_FILE).exists() else
                                                               " (既定値。--collect と --calibrate で学習すると正確になります)"))
    if args.collect:
        collect(screen, det, args.collect, args.n, args.every)
        return
    if args.watch:
        while True:
            m = det.score(screen.grab())
            print(f"[watch] {'表示中' if m.visible else '----  '}  一致度 {m.score:.3f}  位置 {m.pos}")
            time.sleep(1.0)

    ccfg = CycleConfig.from_dict(cfg.get("helper_cycle"))
    if args.interval:
        ccfg.interval_s = args.interval
    dev = open_device(cfg, abs_map_from(cfg), dry_run=args.dry_run)
    cyc = HelperCycle(ccfg, run=lambda s: dev.run(s, timeout=10), grab=screen.grab, detector=det,
                      active=(lambda: True) if args.ignore_active else screen.is_active)
    if args.once:
        cyc.restart()
        return
    print(f"[cycle] {ccfg.interval_s / 60:.0f} 分ごとに MU Helper を再起動します。"
          f"Hunting Log が {ccfg.missing_s:.0f} 秒見えなければ再開します。Ctrl+C で終了")
    last = None
    try:
        while True:
            r = cyc.step()
            if r != last:
                left = ccfg.interval_s - (time.monotonic() - cyc.last_restart)
                msg = {"on": "MU Helper 動作中", "missing": "Hunting Log が見えません", "inactive": "ゲームが前面にありません (待機)",
                       "backoff": "見張りを休止中", "restart": "再起動しました", "recover": "再開を試みました"}[r]
                print(f"[cycle] {msg}  (次の再起動まで {max(0, left) / 60:.1f} 分, 再起動 {cyc.restarts} 回, 再開 {cyc.recoveries} 回)")
                last = r
            time.sleep(ccfg.check_s)
    except KeyboardInterrupt:
        print("[cycle] 終了します")


if __name__ == "__main__":
    main()
