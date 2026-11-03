"""MU Helper が動作中か停止中かを、学習済みの AI で画面から判定する (左上の ■ = 動作中 / ▶ = 停止中)。

AI = テンプレートの一致度 + 小さなネットの確率 (akm/edge/objects.py。学習結果は models/edge_obj.npz)。
両方そろったものだけ「ある」とみなす。学習でモデルが新しくなったら読み直す。
"""
from __future__ import annotations

NAMES = ("helper_running", "helper_stopped")


def make_helper_probe(grab):
    """grab(): ゲーム画面 (原寸)。戻り値の関数を呼ぶと
    {"state": True (動作中) / False (停止中) / None (分からない), "p": 確からしさ, "click": ▶ の座標 or None}"""
    from .edge.objects import MODEL, ObjectDetector, load_objects

    objs = [o for o in load_objects() if o.name in NAMES]
    box = {"det": None, "mtime": None}

    def detector():
        mt = MODEL.stat().st_mtime if MODEL.exists() else 0
        if box["det"] is None or mt != box["mtime"]:
            d = ObjectDetector.load()
            d.objects = [o for o in d.objects if o.name in NAMES] or objs
            box["det"], box["mtime"] = d, mt
        return box["det"]

    def probe():
        dets = {d.name: d for d in detector().detect(grab())}
        run, stop = dets.get("helper_running"), dets.get("helper_stopped")
        conf = lambda d: d.nn if d.nn >= 0 else d.ncc  # noqa: E731
        if run is not None and run.present and not (stop is not None and stop.present):
            return {"state": True, "p": conf(run), "click": None}
        if stop is not None and stop.present and not (run is not None and run.present):
            x, y, w, h = stop.box
            return {"state": False, "p": conf(stop), "click": (x + w / 2, y + h / 2)}
        return {"state": None, "p": 0.0, "click": None}

    return probe
