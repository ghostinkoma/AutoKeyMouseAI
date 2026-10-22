"""ボットを動かさずに、ゲーム画面を ESP32 の液晶に映し続ける (表示の確認用)。

    python tools\\mirror_only.py

ウィンドウから直接撮る (PrintWindow) ので、仮想デスクトップを切り替えても映り続ける。
Ctrl+C で終了。
"""
import time

import _path  # noqa: F401

from akm.config import ask_obs_password, load_config
from akm.live_status import install_tee, status_rows, write_status
from akm.mirror import Mirror
from akm.screen import GameScreen


def start_home_return(cfg, screen, rec, dev, grab_full, input_lock, m, pc_dir):
    """MU Helper の帰還 (akm/home_return.py)。設定は地図ビューアで (models/home_return.properties)。"""
    from akm.config import abs_map_from
    from akm.device import open_device
    from akm.home_return import HomeReturn
    from akm.maploc import LocationReader, make_ocr
    from akm.screen import bring_to_front, foreground_window
    from akm.vision import bar_level

    if dev is None:
        dev = open_device(cfg, abs_map_from(cfg))
    lcfg = cfg.get("maplog") or {}
    try:
        teacher = make_ocr(lcfg.get("ocr", "auto"), lcfg.get("tesseract_cmd"))
    except Exception:
        teacher = None
    reader = LocationReader(lcfg, teacher) if teacher is not None else None
    hud = (cfg.get("hud") or {}).get("hp") or {}
    offset = (cfg.get("elf") or {}).get("character_offset", [0.0, -0.08])

    def safe_run(script):
        try:
            return dev.run(script, timeout=10)
        except Exception:
            try:
                dev.stop()
                dev.run("ra", timeout=3)
            except Exception:
                pass
            raise

    def read_loc(img):
        return reader.read_raw(img) if reader is not None else None

    def read_hp(img):
        if not hud.get("roi"):
            return None
        v = bar_level(img, hud["roi"], hud.get("color", "red"), hud.get("direction", "vertical"))
        return v if v > 0 else None  # 赤が全く見えない = 読めない (窓で隠れているなど)

    def click_at(x, y):
        return f"{dev.s_move(*screen.to_screen(x, y))};w:30;c:L"

    def char_pos(img):
        h, w = img.shape[:2]
        return w / 2 + offset[0] * h, h / 2 + offset[1] * h

    # MU Helper の ■ (動作中) / ▶ (停止中) を見る専用の認識 (認識のスレッドと取り合わないように別に持つ)。
    # AI = テンプレートの一致度 + 小さなネットの確率 (akm/edge/objects.py)。両方そろったものだけ「ある」
    from akm.edge.objects import ObjectDetector, load_objects
    from akm.edge.objects import MODEL as OBJ_MODEL

    helper_objs = [o for o in load_objects() if o.name in ("helper_running", "helper_stopped")]
    hdet_box = {"det": None, "mtime": None}

    def helper_det():
        mt = OBJ_MODEL.stat().st_mtime if OBJ_MODEL.exists() else 0
        if hdet_box["det"] is None or mt != hdet_box["mtime"]:  # 学習でモデルが新しくなったら読み直す
            d = ObjectDetector.load()
            d.objects = [o for o in d.objects if o.name in ("helper_running", "helper_stopped")] or helper_objs
            hdet_box["det"], hdet_box["mtime"] = d, mt
        return hdet_box["det"]

    def helper_probe():
        dets = {d.name: d for d in helper_det().detect(grab_full())}
        run, stop = dets.get("helper_running"), dets.get("helper_stopped")
        conf = lambda d: d.nn if d.nn >= 0 else d.ncc  # noqa: E731
        if run is not None and run.present and not (stop is not None and stop.present):
            return {"state": True, "p": conf(run), "click": None}
        if stop is not None and stop.present and not (run is not None and run.present):
            x, y, w, h = stop.box
            return {"state": False, "p": conf(stop), "click": (x + w / 2, y + h / 2)}
        return {"state": None, "p": 0.0, "click": None}

    hr = HomeReturn(safe_run, grab_full, read_loc, read_hp, lambda: rec.last.helper, click_at, char_pos,
                    helper_probe=helper_probe,
                    activate=getattr(screen, "activate", None), foreground=foreground_window, restore=bring_to_front,
                    lock=input_lock, settings_path=pc_dir / "models" / "home_return.properties",
                    state_path=pc_dir / "models" / "home_return_state.properties",
                    calib_path=pc_dir / "models" / "home_nav.json")
    orig_cycle = hr.cycle
    keys_fps = float((cfg.get("rare_windows") or {}).get("mirror_fps_during_keys", 2))

    def cycle_slow():
        # 歩いている間は液晶への画面送信を減らして、キー・マウス (Bluetooth) に電波を空ける (PT のお断りと同じ)
        if keys_fps > 0:
            m.slow_down(keys_fps)
        try:
            return orig_cycle()
        finally:
            m.slow_down(0)

    hr.cycle = cycle_slow
    hr.reload_settings()
    hr.start(lambda: rec.last.loc or rec.last.last_loc)
    if reader is None:
        print("[home] 現在地の OCR が使えないので、MU Helper の帰還はできません")
    print("[home] MU Helper の帰還: 地図ビューアの「MU Helper の帰還」で設定 (今は "
          + ("オン" if hr.settings.enabled else "オフ") + ")。MU Helper を手で開始した場所を帰る場所にします")
    return hr


def main() -> None:
    from akm.config import PC_DIR

    live_status = PC_DIR / "models" / "live_status.txt"  # 地図ビューアの「学習の状況」
    tee = install_tee()  # 画面に出した行を、最近の出来事として取っておく
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--fps", type=float, default=10.0, help="送る枚数/秒 (既定 10。config の mirror.interval_s より優先)")
    ap.add_argument("--format", choices=["bc", "bad16", "jpeg", "raw"],
                    help="bad16 = BadCodec 16bit 版 (既定) / bc = ブロック差分 / jpeg / raw (省略時は config の mirror.format)")
    ap.add_argument("--drop-bits", type=int, default=0, choices=[0, 1, 2, 3],
                    help="bad16 で各色の下位ビットを捨てる数 (0 = 可逆)。ESP32 に入らなければ自動で減らす")
    ap.add_argument("--with-prev", action="store_true",
                    help="bad16 を面ごとのフレーム (ver 2) で送る。少し小さいが ESP32 に面の数 x 4KB 要る "
                         "(既定は前フレーム不要版: メモリ不足にならない)")
    ap.add_argument("--gray", action="store_true",
                    help="bad16 で各色を Gray 符号にしてから送る (可逆のまま 1 割強小さい。firmware frame-stream-10 以降)")
    ap.add_argument("--color", help="bad16 の色のビット数 R G B を 3 桁で (例 343 = 10 面, 342 = 9 面, 332 = 8 面)。"
                                    "--drop-bits より優先")
    ap.add_argument("--recognize", action="store_true",
                    help="縮小前の画面で窓・アイコン・相手の名前を認識し、液晶に重ねて表示する")
    ap.add_argument("--learn", action="store_true",
                    help="等倍の画面をときどき dataset/frames に保存する (窓・アイコン・文字の学習の素材)")
    ap.add_argument("--recognize-every", type=float, default=1.0, help="認識の間隔 (秒)")
    ap.add_argument("--auto-train", action="store_true",
                    help="蓄積 → 学習 → 出力 → 反映 を自動で繰り返す (--recognize --learn を含む)")
    ap.add_argument("--learn-every", type=float, default=30.0, help="画面を保存する間隔 (秒)")
    ap.add_argument("--train-every", type=int, default=60, help="新しい画面が何枚たまったら学習するか")
    ap.add_argument("--ocr-every", type=int, default=3, help="文字認識は何回に 1 回学習するか (0 でしない)")
    ap.add_argument("--no-ask", action="store_true",
                    help="保存済みの OBS のパスワードでつながれば聞かない (learn_loop.ps1 の自動再起動用)")
    ap.add_argument("--no-db", action="store_true",
                    help="--recognize で読んだ現在地・相手の名前を DB (ビューアと共通の mu_map.db) に記録しない")
    ap.add_argument("--rare", default=None,
                    help="めったに出ない窓の名前 (カンマ区切り)。見えた瞬間の画面を dataset/screens/rare に残す"
                         " (省略時は config の rare_windows.watch。無ければ party_invite,trade)")
    ap.add_argument("--no-home", action="store_true", help="MU Helper の帰還 (地図ビューアで設定) を使わない")
    ap.add_argument("--decline", default=None,
                    help="見えたら断る窓の名前 (カンマ区切り。例 party_invite)。ゲームの窓が手前のときだけキーを送る"
                         " (押すキー・クリック位置は config の rare_windows.decline)")
    ap.add_argument("--tolerance", type=int, default=16, help="ブロック差分方式の許容誤差 (大きいほど小さく粗い)")
    ap.add_argument("--max-ratio", type=float, default=1.5,
                    help="ブロック差分が JPEG の何倍までならそのまま送るか (大きくすると JPEG を使わない。99 で常にブロック差分)")
    args = ap.parse_args()
    if args.auto_train:
        args.recognize = args.learn = True
    cfg = load_config("config.yaml")
    ask_obs_password(cfg, "config.yaml", ask=not args.no_ask)
    screen = GameScreen.from_config(cfg)
    obs = cfg.get("obs") or {}
    print(f"[mirror] 設定: game.capture={cfg['game'].get('capture', 'auto')}  "
          + (f"obs=有効 (source={obs.get('source')}, port={obs.get('port', 4455)})" if obs.get("enabled") else
             "obs=無効 (config.yaml に obs: が無いか enabled: false)"))
    screen.locate()
    mcfg = cfg.get("mirror") or {}
    host = (cfg.get("device") or {}).get("host")
    if not host:
        raise SystemExit("config.yaml の device.host に ESP32 の IP を書いてください")
    from akm.bad16 import bits_of_drop

    from akm.bad16 import parse_color

    try:
        if args.color:
            bits = parse_color(args.color)
        elif args.drop_bits:
            bits = bits_of_drop(args.drop_bits)
        else:
            bits = parse_color(mcfg.get("color", "343"))  # 既定: R3 G4 B3 (10 面)
    except ValueError as e:
        raise SystemExit(str(e))
    import threading

    shot_lock = threading.Lock()  # OBS への問い合わせはミラーと認識で同時にしない
    input_lock = threading.Lock()  # キー・マウスの操作 (PT 招待のお断り / MU Helper の帰還) は 1 つずつ
    dev = None

    def grab_preview():
        with shot_lock:
            return screen.grab_preview()

    def grab_full():
        with shot_lock:
            return screen.grab()

    m = Mirror(host, 1.0 / max(0.5, args.fps), fmt=args.format or mcfg.get("format", "bad16"),
               quality=int(mcfg.get("quality", 70)), grab=grab_preview,
               transport=mcfg.get("transport", "auto"), port=int(mcfg.get("port", 5005)),
               b16_bits=bits, b16_prev_free=not (args.with_prev or mcfg.get("with_prev", False)),
               b16_gray=args.gray or bool(mcfg.get("gray", False)))
    m.bc_tolerance = args.tolerance
    m.bc_max_ratio = args.max_ratio
    base_lines = [("MIRROR ONLY", (0, 200, 255))]
    m.set_lines(base_lines)
    # 計算の分担 (python tools\perf_check.py で測ってから): GPU (OpenCL) で窓・アイコンの候補探し
    opencl = bool((cfg.get("performance") or {}).get("opencl", False))
    # 窓・アイコンの画面全体を探すのは何回に 1 回か (それ以外は前に見つけた場所・登録した場所の周りだけ)
    full_every = int((cfg.get("performance") or {}).get("full_search_every", 3))
    rec = None
    if args.recognize or args.learn:
        from akm.edge.live import LiveRecognizer, overlay_lines
        from akm.edge.objects import ObjectDetector

        det = ObjectDetector.load(opencl=opencl)
        det.full_every = full_every
        if opencl:
            import cv2

            print("[perf] 窓・アイコンの候補探しを GPU (OpenCL) で: "
                  + (cv2.ocl.Device.getDefault().name() if det.opencl else "使えないので CPU のまま"))
        ocr = None
        try:
            from akm.edge.ocr import OcrReader

            ocr = OcrReader.load()
        except FileNotFoundError as e:
            print(f"[recognize] {e}")
        print(f"[recognize] 窓・アイコン {len(det.objects)} 種 ({', '.join(o.name for o in det.objects)})"
              f"{'' if det.model else ' (ネット未学習: 一致度だけで判定)'} を {args.recognize_every:.1f} 秒ごとに等倍の画面で認識します"
              + (" / 等倍の画面を dataset/frames に保存します" if args.learn else ""))
        harvester = None
        if args.auto_train and ocr is not None:
            # 正解付きの文字の行を自動で集める。Windows の OCR があれば「先生」として読みを照らし合わせる
            from akm.edge.harvest import OcrHarvester
            from akm.maploc import make_ocr

            lcfg = cfg.get("maplog") or {}
            teacher = None
            try:
                teacher = make_ocr(lcfg.get("ocr", "auto"), lcfg.get("tesseract_cmd"))
            except Exception as e:
                print(f"[harvest] 先生役の OCR が使えないので、自分の読みが続けて同じときだけ見本にします ({str(e).splitlines()[0]})")
            harvester = OcrHarvester(PC_DIR / "dataset" / "ocr_lines", ocr, teacher,
                                     loc_roi=lcfg.get("roi", [0.86, 0.955, 0.14, 0.045]))
        world = None
        if args.recognize and ocr is not None and not args.no_db:
            # 現在地と相手の名前を、正しい読みか AI (models/record_filter.npz) で判定してから DB に記録する
            from akm.maploc import make_ocr
            from akm.record_filter import MODEL as FILTER_MODEL, RecordFilter
            from akm.world_log import WorldLogger

            lcfg = cfg.get("maplog") or {}
            wcfg = cfg.get("world_log") or {}
            wteacher = None
            try:
                wteacher = make_ocr(lcfg.get("ocr", "auto"), lcfg.get("tesseract_cmd"))
            except Exception:
                pass
            filt = RecordFilter.load(ok=float(wcfg.get("ok", 0.8)), ng=float(wcfg.get("ng", 0.3)))
            world = WorldLogger(PC_DIR / lcfg.get("db", "data/mu_map.db"), filt, teacher=wteacher,
                                loc_roi=lcfg.get("roi", [0.86, 0.955, 0.14, 0.045]),
                                area_cells=int(wcfg.get("area_cells", 10)),
                                spawn_threshold=float(wcfg.get("spawn_match", 0.85)),
                                spawn_map_threshold=float(wcfg.get("spawn_match_map", 0.65)),
                                spawn_margin=float(wcfg.get("spawn_margin", 0.10)))
            print(f"[world] 現在地と相手の名前を {world.db_path} に記録します (判定 AI: "
                  f"{'あり' if filt.model else 'なし → python tools/record_filter.py train で作成'}、"
                  f"先生役の OCR: {'あり' if wteacher else 'なし'})。ビューア: java -jar viewer\\mapviewer.jar")
            if not FILTER_MODEL.exists():
                print("[world] 判定 AI のモデルがありません。規則で代わりに判定します")
        rare = None
        if args.recognize:
            from akm.edge.rare import RareWatch

            rcfg = cfg.get("rare_windows") or {}
            watch = [n.strip() for n in (args.rare or ",".join(rcfg.get("watch") or ["party_invite", "trade"])).split(",")
                     if n.strip()]
            dcfg = rcfg.get("decline") or {}
            names = [n.strip() for n in (args.decline or "").split(",") if n.strip()]
            from akm.edge.respond import DEFAULT_RESPONSES

            steps_cfg = {**DEFAULT_RESPONSES, **(rcfg.get("respond") or {})}  # 応じ方の手順 (config で上書き)
            decline = {n: (dcfg.get(n) or steps_cfg.get(n) or {"key": "esc"}) for n in names}
            press = click = responder = None
            if decline:
                from akm.config import abs_map_from
                from akm.device import open_device
                from akm.edge.respond import Responder

                dev = open_device(cfg, abs_map_from(cfg))
                press = dev.key
                click = lambda x, y: dev.click(*screen.to_screen(x, y))  # noqa: E731
                from akm.edge.respond import read_dialog
                from akm.screen import bring_to_front, foreground_window
                from akm.maploc import make_ocr

                t_ocr = None
                try:
                    lcfg0 = cfg.get("maplog") or {}
                    t_ocr = make_ocr(lcfg0.get("ocr", "auto"), lcfg0.get("tesseract_cmd"))
                except Exception as e:
                    print(f"[rare] 先生役の OCR が使えません ({str(e).splitlines()[0]})")

                def own_read(line):
                    return rec.ocr.read(line)[0] if rec.ocr is not None else ""

                # 窓の文字を行ごとに、自分の文字認識 (ゲームの字体) と先生役の OCR で読む
                read_text = lambda crop: read_dialog(crop, own_read, t_ocr)  # noqa: E731
                def safe_run(script):
                    # ESP32 が固まったときに 35 秒待たない。失敗したらキー・ボタンを全部離す
                    try:
                        return dev.run(script, timeout=8)
                    except Exception:
                        try:
                            dev.stop()
                            dev.run("ra", timeout=3)
                        except Exception:
                            pass
                        raise

                responder = Responder(safe_run, grab_full, lambda: rec.detector, move=dev.s_move, read_text=read_text,
                                      helper_state=lambda: rec.helper_state(),
                                      save_dir=PC_DIR / "dataset" / "screens" / "rare" / "_text",
                                      cursor=dev.cursor,
                                      chat_open_wait=float(rcfg.get("chat_open_wait_s", 0.3)),
                                      chat_verify=bool(rcfg.get("chat_verify", False)),
                                      enter_hold_ms=int(rcfg.get("chat_enter_hold_ms", 80)),
                                      send_enters=int(rcfg.get("chat_send_enters", 1)),
                                      check_caret=bool(rcfg.get("chat_check_caret", True)),
                                      input_state=getattr(screen, "input_state", None),
                                      ime_off=(getattr(screen, "ime_off", None) if rcfg.get("ime_off", True) else None),
                                      grab_chat=(getattr(screen, "grab_desktop", None)
                                                 if rcfg.get("chat_capture", "desktop") == "desktop" else None),
                                      blind_first=bool(rcfg.get("chat_blind_first", True)),
                                      activate=(getattr(screen, "activate", None)
                                                if rcfg.get("background", False) else None),
                                      foreground=foreground_window, restore=bring_to_front,
                                      to_screen=screen.to_screen, is_active=getattr(screen, "is_active", None),
                                      keep=rcfg.get("keep", ["hunting_log", "helper_panel"]),
                                      chat_cooldown_s=float(rcfg.get("chat_cooldown_s", 10)))
            known = {o.name for o in det.objects}
            missing = [n for n in set(watch) | set(decline) if n not in known]
            if missing:
                print(f"[rare] 未登録の窓: {', '.join(missing)} (python tools\\edge_objects.py add-screen 名前 で登録)")
            if responder is not None:
                # 断っている間は液晶への画面送信を止める (ESP32 がキー入力と画面の受信で詰まらないように)
                orig_respond = responder.respond

                def respond_paused(*a, **k):
                    # ESP32 は Wi-Fi (画面) と Bluetooth (キー・マウス) で同じ電波を使う。断っている間は
                    # 液晶への画面送信を減らして (既定 毎秒 2 枚) 電波を空ける。止めはしないので液晶の更新は続く
                    # (keys_fps: 0 にすると完全に止める = キーは一番確実に届くが、その間液晶は止まる)
                    keys_fps = float(rcfg.get("mirror_fps_during_keys", 2))
                    if keys_fps > 0:
                        m.slow_down(keys_fps)
                    else:
                        m.paused = True
                    with input_lock:  # MU Helper の帰還で歩いている間は、終わるのを待ってから断る
                        time.sleep(float(rcfg.get("pause_before_keys_s", 0.6)))
                        try:
                            return orig_respond(*a, **k)
                        finally:
                            m.slow_down(0)
                            m.paused = False

                responder.respond = respond_paused
            rare = RareWatch(watch, PC_DIR / "dataset" / "screens" / "rare", decline, press=press, click=click,
                             is_active=getattr(screen, "is_active", None), confirm=int(rcfg.get("confirm", 2)),
                             responder=responder)
            def how(a):
                if isinstance(a, list):
                    return " → ".join(str(next(iter(st.values()))) if "chat" not in st else f"チャット「{st['chat']}」"
                                      for st in a)
                return "クリック" if a.get("click") else "キー " + a.get("key", "esc")

            print(f"[rare] 見張る窓: {', '.join(sorted(rare.names))} (見えたら dataset/screens/rare に画面を保存)")
            for n, a in decline.items():
                print(f"[rare] {n} が出たら: {how(a)}")
            if decline:
                print("[rare] " + ("ゲームが裏にあっても、一時的に手前に出して断ります (background: true)"
                                   if rcfg.get("background", False) else
                                   "ゲームが手前にあるときに断ります (裏のときは、手前に戻ったときに断ります)"))
        rec = LiveRecognizer(grab_full, det, ocr if args.recognize else None, period=args.recognize_every,
                             on_result=(lambda r: m.set_lines(overlay_lines(r))) if args.recognize else None,
                             learn_dir=(PC_DIR / "dataset" / "frames") if args.learn else None,
                             learn_every=args.learn_every, harvester=harvester, world=world, rare=rare)
        rec.start()
    home = None
    if rec is not None and args.recognize and not args.no_home:
        home = start_home_return(cfg, screen, rec, dev, grab_full, input_lock, m, PC_DIR)
    cycle = None
    if args.auto_train:
        from akm.edge.cycle import LearnCycle

        def reload_models():
            rec.detector = ObjectDetector.load(opencl=opencl)  # 新しく登録した窓・アイコンも読み直す
            rec.detector.full_every = full_every
            try:
                rec.ocr = OcrReader.load()
                if rec.harvester is not None:
                    rec.harvester.ocr = rec.ocr
            except FileNotFoundError:
                pass
            if rec.world is not None:
                from akm.record_filter import RecordFilter

                old = rec.world.filt
                rec.world.filt = RecordFilter.load(ok=old.ok, ng=old.ng)
                rec.world.reload_lists()  # 承認された名前・マップを辞書に入れ直す

        def push_win_model():
            # 窓の種類の認識 (ESP32): 学習したモデルを Wi-Fi で送る (同じものが入っていれば送らない)
            from akm.edge.winmodel import push_model

            blob_path = PC_DIR / "models" / "edge_win.bin"
            if blob_path.exists():
                push_model(host, blob_path.read_bytes())

        def reload_and_push():
            reload_models()
            push_win_model()

        threading.Thread(target=push_win_model, daemon=True).start()
        cycle = LearnCycle(PC_DIR, PC_DIR / "dataset" / "frames", every_frames=args.train_every,
                           ocr_every=args.ocr_every, reload=reload_and_push)
        cycle.start()
        print(f"[learn] 自動学習: 画面を {args.learn_every:.0f} 秒ごとに保存し、新しい画面が {args.train_every} 枚"
              f" (約 {args.learn_every * args.train_every / 60:.0f} 分) たまるたびに 学習 → 出力 → 反映 します"
              f" (文字認識は {args.ocr_every} 回に 1 回)。記録: models/learn_log.txt")
    print(f"[mirror] {host} に毎秒 {1 / m.interval:.0f} 枚で送信中 ({m.fmt}) Ctrl+C で終了  (--fps で変更)")
    import requests

    status_url = m.url.replace("/frame", "/status")
    try:
        while True:
            time.sleep(5)
            m.watchdog()  # 液晶への送信が止まっていたら、つなぎ直す
            kinds = (f" (bc {m.sent_kinds['bc']} / jpeg {m.sent_kinds['jpeg']}, 大きさ bc {m.cand_bytes['bc'] / 1024:.1f}KB"
                     f" : jpeg {m.cand_bytes['jpeg'] / 1024:.1f}KB)") if m.fmt == "bc" else ""
            if m.fmt == "bad16":
                enc = m._b16
                kinds = " (色 R{}G{}B{} = {} 面, {})".format(
                    *m.b16_bits, sum(m.b16_bits),
                    f"前フレーム不要・変化 {enc.changed_blocks}/510 ブロック" if m.b16_prev_free and enc else "面ごと")
            if rec is not None and rec.last.ms:
                kinds += f"  認識 {rec.last.ms:.0f}ms"
            if rec is not None and rec.harvester is not None:
                kinds += f"  文字の見本 +{rec.harvester.saved}"
            if rec is not None and rec.world is not None:
                st = rec.world.stats
                kinds += (f"  DB 目撃 登録{st['ok']}/保留{st['pending']}/除外{st['ng']} 位置{st['loc_ok']}"
                          f"  名前 {st['names']} 回 (ガイド一致: 自分 {st['names_own']} / 先生 {st['names_teacher']},"
                          f" 不一致 {st['names_nomatch']}" + (f" 最後 {rec.world.last_unmatched!r}" if rec.world.last_unmatched else "") + ")")
            if rec is not None and rec.rare is not None and (rec.rare.seen or rec.rare.declined):
                kinds += "  珍しい窓 " + ", ".join(f"{n} {c} 回" + (f" (断った {rec.rare.declined[n]})" if n in rec.rare.declined
                                                                   else "") for n, c in rec.rare.seen.items())
            if cycle is not None:
                kinds += ("  学習中…" if cycle.busy else
                          f"  次の学習まで {max(0, cycle.every_frames - cycle.new_frames())} 枚 (済 {cycle.rounds} 回)")
            print(f"[mirror] 送信 {m.sent} 枚  失敗 {m.errors}  {m.fps:.1f}fps  形式 {m.fmt}{kinds}  {m.last_bytes} バイト/枚  "
                  f"(PC: 撮影 {m.t_capture:.0f}ms 圧縮 {m.t_encode:.0f}ms 送信 {m.t_post:.0f}ms)")
            st = None
            try:
                st = requests.get(status_url, timeout=2).json()
                if "mirror" not in st:
                    print(f"         ESP32: ファームウェアが古いので計測値がありません (fw={st.get('fw', 'なし')})。"
                          "frame-stream-3 を書き込んでください")
                e = st.get("mirror") or {}
                if e:
                    print(f"         ESP32: {e.get('fps', 0)}fps 受信 {e.get('recv_ms', 0)}ms 展開 {e.get('decode_ms', 0)}ms "
                          f"描画 {e.get('draw_ms', 0)}ms → CPU {e.get('cpu_pct', 0)}% (1 コア, {st.get('cpuMHz', '?')}MHz)  "
                          f"空きメモリ {st.get('heap', 0) // 1024}KB (最大連続 {st.get('heapBlock', 0) // 1024}KB, "
                          f"最小 {st.get('heapMin', 0) // 1024}KB)")
            except Exception:
                pass
            try:  # 地図ビューアの「学習の状況」へ (失敗しても送信・PT のお断りは続ける)
                write_status(live_status, status_rows(m, rec, cycle, st, PC_DIR), tee.recent())
            except Exception as e:
                print(f"[status] 状況を書けません: {e}")
    except KeyboardInterrupt:
        if home is not None:
            home.stop()
        if rec is not None:
            rec.stop()
        if cycle is not None:
            cycle.stop()


if __name__ == "__main__":
    main()
