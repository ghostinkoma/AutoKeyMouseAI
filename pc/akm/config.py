from __future__ import annotations

from pathlib import Path

import yaml

from .device import AbsMap
from .screen import primary_screen_size

PC_DIR = Path(__file__).resolve().parent.parent


def load_config(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = PC_DIR / p
    if not p.exists():
        raise SystemExit(f"設定ファイルがありません: {p}\n  config.example.yaml を config.yaml にコピーしてください")
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def abs_map_from(cfg: dict) -> AbsMap:
    m = (cfg.get("mouse") or {}).get("abs_map")
    if m:
        return AbsMap(**m)
    return AbsMap.for_screen(*primary_screen_size())


def config_path(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else PC_DIR / p


def save_obs_password(path: str | Path, password: str) -> bool:
    """config.yaml の obs: の password 行だけを書き換える (コメントや並びはそのまま)。"""
    import json
    import re

    p = config_path(path)
    lines = p.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    start = max((i for i, l in enumerate(lines) if re.match(r"^obs:\s*(#.*)?$", l)), default=None)
    if start is None:
        return False
    for i in range(start + 1, len(lines)):
        l = lines[i]
        if l.strip() and not l[0].isspace():  # 次のトップレベルの項目: obs: の範囲外
            break
        m = re.match(r"^(\s+password:\s*)", l)
        if m:
            nl = "\r\n" if l.endswith("\r\n") else "\n"
            lines[i] = m.group(1) + json.dumps(password) + nl  # JSON 文字列は YAML としても正しい
            p.write_text("".join(lines), encoding="utf-8")
            return True
    return False


def check_obs(obs: dict) -> str | None:
    """OBS の WebSocket に接続できるか。できなければ理由を返す。"""
    import logging

    try:
        import obsws_python as obsws
    except ImportError:
        return "obsws-python が入っていません (pip install obsws-python)"
    logging.getLogger("obsws_python").setLevel(logging.CRITICAL)
    try:
        c = obsws.ReqClient(host=obs.get("host", "localhost"), port=int(obs.get("port", 4455)),
                            password=obs.get("password") or "", timeout=3)
        try:
            c.get_version()
        finally:
            try:
                c.disconnect()
            except Exception:
                pass
        return None
    except Exception as e:
        msg = str(e)
        if "identify" in msg:
            return "パスワードが違います"
        if "refused" in msg.lower() or "10061" in msg:
            return "OBS が起動していないか、WebSocket サーバーが有効になっていません"
        return msg


def ask_obs_password(cfg: dict, path: str | Path = "config.yaml", ask: bool = True) -> None:
    """起動時に OBS のパスワードを聞く (Enter で変更しない)。つながるまで聞き直し、新しいパスワードは保存する。

    ask=False なら、保存済みのパスワードでつながるときは聞かない (つながらなければ聞く)。
    """
    obs = cfg.get("obs") or {}
    if not obs.get("enabled"):
        return
    if not ask and check_obs(obs) is None:
        print("[obs] OBS に接続できました (保存済みのパスワード)")
        return
    print("[obs] OBS の「ツール」→「WebSocket サーバー設定」→「接続情報を表示」のパスワードを貼り付けてください")
    while True:
        try:
            pw = input("OBS Password ? (Enter で変更しない): ").strip()
        except EOFError:
            pw = ""
        if pw:
            obs["password"] = pw
        err = check_obs(obs)
        if err is None:
            print("[obs] OBS に接続できました")
            if pw:
                if save_obs_password(path, pw):
                    print("[obs] パスワードを config.yaml に保存しました")
                else:
                    print("[obs] config.yaml の obs: に password: の行が無いので保存しませんでした")
            return
        print(f"[obs] OBS に接続できません: {err}")
        if not pw:
            ans = input("もう一度入力しますか? (Enter = OBS を使わずに続ける / y = 入力し直す): ").strip().lower()
            if ans != "y":
                obs["enabled"] = False
                print("[obs] OBS を使わずに続けます (ウィンドウや画面から撮ります)")
                return
