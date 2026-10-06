"""MU Helper を 10 分おきに再起動し、止まっていたら再開する。
状態は左上の MU Helper パネルの ■ (動作中) / ▶ (停止中) で判定し、パネルが見えないときは
Instant Hunting Log 窓で補う。どちらも見えない (インベントリ等で隠れている) ときは何もしない。

    python tools\\helper_cycle.py                 実行 (Ctrl+C で終了)
    python tools\\helper_cycle.py --dry-run       キーは送らず、判定と動作の予定だけ表示
    python tools\\helper_cycle.py --watch         判定 (一致度) を表示し続けるだけ
    python tools\\helper_cycle.py --once          今すぐ 1 回再起動して終わる (キー操作の確認用)

学習 (表示されているかの判定の閾値を決める):
    python tools\\helper_cycle.py --collect on    MU Helper 動作中の画面を集める (窓をあちこちに動かしながら)
    python tools\\helper_cycle.py --collect off   MU Helper 停止中の画面を集める
    python tools\\helper_cycle.py --calibrate     集めた画面から閾値を決めて templates/hunting_log.json に保存
    python tools\\helper_cycle.py --snap character   キャラクター窓を開いた画面を保存
    python tools\\helper_cycle.py --make-ui-template character dataset\\ui\\character.png
                                                  保存した画面から窓のタイトル等を囲んで見本を作る
                                                  (inventory も同じ。閉じるキーは config の ui_windows:)
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
from akm.helper_state import HelperStateReader
from akm.ui_windows import DEFAULT_WINDOWS, WindowCloser
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
        r = HelperStateReader(PC_DIR, log_detector=det).read(img)
        print(f"[collect] {p.name}  判定 {fmt_state(r)}")
        time.sleep(every)


def fmt_state(r) -> str:
    s = {True: "動作中", False: "停止中", None: "不明  "}[r.state]
    return (f"{s} ({r.how})  パネル {r.panel:.2f} ■ {r.on:.2f} ▶ {r.off:.2f}"
            + (f" ログ {r.log:.2f}" if r.log >= 0 else ""))


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


def make_ui_template(name: str, png: Path, entries: list[dict]) -> None:
    import cv2

    img = cv2.imread(str(png))
    if img is None:
        raise SystemExit(f"画像が読めません: {png}")
    e = next((e for e in entries if e["name"] == name), None)
    if e is None:
        raise SystemExit(f"config の ui_windows: に {name} がありません ({', '.join(x['name'] for x in entries)})")
    print(f"[template] {name} 窓の中で動かない部分 (タイトル・枠の飾りなど。中身のアイテムや数値は含めない) を"
          "マウスで囲んで Enter")
    x, y, w, h = cv2.selectROI(f"select: {name}", img, showCrosshair=False)
    cv2.destroyAllWindows()
    if w == 0 or h == 0:
        raise SystemExit("やめました")
    out = PC_DIR / e["template"]
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), img[y:y + h, x:x + w])
    print(f"[template] {out} を保存しました ({w}x{h})。--watch で開閉が見分けられるか確認してください")


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
    ap.add_argument("--snap", metavar="NAME", help="今の画面を dataset/ui/NAME.png に保存する (窓の見本づくり用)")
    ap.add_argument("--make-ui-template", nargs=2, metavar=("NAME", "PNG"), help="画面から窓の見本を切り抜く")
    args = ap.parse_args()
    import yaml

    try:
        with open(PC_DIR / "config.yaml", encoding="utf-8") as f:
            ui_entries = (yaml.safe_load(f) or {}).get("ui_windows") or DEFAULT_WINDOWS
    except FileNotFoundError:
        ui_entries = DEFAULT_WINDOWS
    if args.make_ui_template:
        make_ui_template(args.make_ui_template[0], Path(args.make_ui_template[1]), ui_entries)
        return

    if args.make_template:
        make_template(args.make_template)
        return
    if args.calibrate:
        import cv2

        reader = HelperStateReader(PC_DIR)
        for label, want in (("on", True), ("off", False)):
            files = sorted((DATA / label).glob("*.png"))
            res = [reader.read(cv2.imread(str(f))).state for f in files]
            ok = sum(r == want for r in res)
            wrong = sum(r == (not want) for r in res)
            print(f"[calibrate] {'動作中' if want else '停止中'}の画面 {len(files)} 枚: 正しい {ok} / 不明 {res.count(None)} / "
                  f"逆に判定 {wrong}" + ("  ← 危険 (誤って Home を押す)" if wrong else ""))
        det = HuntingLogDetector(PC_DIR, threshold=0.0)
        r = calibrate(det, DATA / "on", DATA / "off")
        print(f"[calibrate] Hunting Log 窓: 動作中の画面で一致度の最小 {r['on_min']} / 停止中の画面で最大 {r['off_max']}"
              " (インベントリ等で窓が隠れた画面があると最小は低くなります。パネルで判定できていれば問題ありません)")
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
    if args.snap:
        import cv2

        out = PC_DIR / "dataset" / "ui" / f"{args.snap}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out), screen.grab())
        print(f"[snap] {out} に保存しました。次は --make-ui-template {args.snap} {out}")
        return
    reader = HelperStateReader(PC_DIR, log_detector=det)
    closer = WindowCloser(PC_DIR, ui_entries)
    if closer.missing:
        print(f"[ui] 窓の見本がまだありません: {', '.join(closer.missing)}。"
              "--snap と --make-ui-template で作ると、開いていれば閉じてから判定します")
    if args.watch:
        while True:
            img = screen.grab()
            opened = ", ".join(f"{w.name} {s:.2f}" for w, s in closer.open_windows(img)) or "なし"
            print(f"[watch] {fmt_state(reader.read(img))}  開いている窓: {opened}")
            time.sleep(1.0)

    ccfg = CycleConfig.from_dict(cfg.get("helper_cycle"))
    if args.interval:
        ccfg.interval_s = args.interval
    dev = open_device(cfg, abs_map_from(cfg), dry_run=args.dry_run)
    cyc = HelperCycle(ccfg, run=lambda s: dev.run(s, timeout=10), grab=screen.grab, reader=reader,
                      active=(lambda: True) if args.ignore_active else screen.is_active, closer=closer)
    if args.once:
        cyc.restart()
        return
    print(f"[cycle] {ccfg.interval_s / 60:.0f} 分ごとに MU Helper を再起動します。"
          f"停止中が {ccfg.missing_s:.0f} 秒続けば再開します (状態が分からないときは何もしません)。Ctrl+C で終了")
    last = None
    try:
        while True:
            r = cyc.step()
            if r != last:
                left = ccfg.interval_s - (time.monotonic() - cyc.last_restart)
                msg = {"on": "MU Helper 動作中", "missing": "MU Helper 停止中", "unknown": "状態が分かりません (待機)",
                       "inactive": "ゲームが前面にありません (待機)",
                       "backoff": "見張りを休止中", "restart": "再起動しました", "recover": "再開を試みました"}[r]
                print(f"[cycle] {msg}  (次の再起動まで {max(0, left) / 60:.1f} 分, 再起動 {cyc.restarts} 回, 再開 {cyc.recoveries} 回)")
                last = r
            time.sleep(ccfg.check_s)
    except KeyboardInterrupt:
        print("[cycle] 終了します")


if __name__ == "__main__":
    main()
