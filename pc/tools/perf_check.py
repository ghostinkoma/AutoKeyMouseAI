"""計算の分担を決めるために、どこに CPU を使っているかと、GPU (OpenCL) で速くなるかを測る。

    python tools\\perf_check.py            貯めた画面 (dataset/frames の新しい 20 枚) で測る
    python tools\\perf_check.py --n 50     枚数を変える

学習ループ (start_learn) を動かしたままでも測れる (そのぶん少し遅めに出る)。
測るもの:
  1. 1 回の認識 (1 秒ごと) の内訳: 窓・アイコンの認識 / 相手の名前 (自分の OCR) / 現在地 (Windows の OCR)
     / 液晶用の縮小と圧縮
  2. 窓・アイコンの候補探し (matchTemplate) を CPU と GPU (OpenCL) で比べる
  3. 学習 (numpy) の速さを、使うスレッドの数を変えて比べる
最後に、config.yaml の performance.opencl を true にした方が良いかを出す。
"""
import argparse
import os
import sys
import time

import _path  # noqa: F401

import cv2
import numpy as np

from akm.config import PC_DIR, load_config


def timeit(fn, n: int = 1) -> float:
    """1 回あたりのミリ秒 (n 回の中央値)。"""
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    return float(np.median(ts))


def frames(n: int) -> list[np.ndarray]:
    files = sorted((PC_DIR / "dataset" / "frames").glob("*.png"), key=lambda f: f.stat().st_mtime, reverse=True)
    out = []
    for f in files[:n]:
        img = cv2.imread(str(f))
        if img is not None and img.shape[0] >= 300:
            out.append(img)
    return out


def env_report() -> dict:
    info = {"cores": os.cpu_count() or 0, "cv": cv2.__version__, "cv_threads": cv2.getNumThreads(),
            "opencl": cv2.ocl.haveOpenCL(), "gpu": "", "cuda": 0}
    print(f"CPU: 論理コア {info['cores']}  OpenCV {info['cv']} (スレッド {info['cv_threads']})")
    if info["opencl"]:
        cv2.ocl.setUseOpenCL(True)
        d = cv2.ocl.Device.getDefault()
        info["gpu"] = f"{d.name()} ({d.vendorName()}, {d.version()}, 計算ユニット {d.maxComputeUnits()})"
        print(f"GPU (OpenCL): {info['gpu']}")
    else:
        print("GPU (OpenCL): 使えません (グラフィックドライバーの OpenCL が無い)")
    try:
        info["cuda"] = cv2.cuda.getCudaEnabledDeviceCount()
    except Exception:
        info["cuda"] = 0
    print("CUDA: " + ("使える" if info["cuda"] else
                      "使えない (pip の OpenCV は CUDA なし。GTX 660 は古く、今の PyTorch / CuPy も非対応)"))
    return info


def stage_report(imgs: list[np.ndarray]) -> dict:
    from akm.edge.objects import ObjectDetector

    res = {}
    det = ObjectDetector.load()
    img = imgs[0]
    det.detect(img)  # テンプレートの用意 (1 回目だけ遅い) を除く
    res["窓・アイコンの認識"] = np.median([timeit(lambda im=im: det.detect(im)) for im in imgs])
    try:
        from akm.edge.ocr import OcrReader
        from akm.edge.target import read_target

        ocr = OcrReader.load()
        res["相手の名前 (自分の OCR)"] = np.median([timeit(lambda im=im: read_target(im, ocr)) for im in imgs])
    except FileNotFoundError:
        pass
    try:
        from akm.maploc import LocationReader, make_ocr, preprocess

        cfg = load_config("config.yaml") if (PC_DIR / "config.yaml").exists() else {}
        lcfg = cfg.get("maplog") or {}
        teacher = make_ocr(lcfg.get("ocr", "auto"), lcfg.get("tesseract_cmd"))
        if teacher is not None:
            lr = LocationReader(lcfg, teacher)
            crops = [lr.crop(im) for im in imgs[:5]]
            res["現在地 (Windows の OCR, 1 回)"] = np.median(
                [timeit(lambda c=c: teacher(preprocess(c, lr.scale, "bright", lr.threshold))) for c in crops])
    except Exception as e:
        print(f"  (現在地の OCR は測れません: {e})")
    from akm.bad16 import Encoder
    from akm.mirror import compose

    enc = Encoder(bits=(3, 4, 3))
    res["液晶用の縮小と圧縮"] = np.median([timeit(lambda im=im: enc.encode(compose(im, []))) for im in imgs])
    total = sum(res.values())
    print("\n1 回の認識・送信の内訳 (1 枚あたり、中央値):")
    for k, v in sorted(res.items(), key=lambda kv: -kv[1]):
        print(f"  {k:28s} {v:7.1f} ms  {v / total:5.0%}  {'#' * int(30 * v / total)}")
    print(f"  {'合計':28s} {total:7.1f} ms")
    return res


def opencl_report(imgs: list[np.ndarray], info: dict) -> tuple[float, float]:
    """縮小画面 (認識で使う大きさ) で、窓・アイコンの候補探しの matchTemplate を CPU と GPU で比べる。"""
    from akm.edge.objects import ObjectDetector

    if not info["opencl"]:
        return 0.0, 0.0
    det_cpu = ObjectDetector.load(opencl=False)
    det_gpu = ObjectDetector.load(opencl=True)
    for d in (det_cpu, det_gpu):
        for _ in range(2):  # GPU は最初の数回 (プログラムの用意) が遅いので除く
            d.detect(imgs[0])
    cpu = np.median([timeit(lambda im=im: det_cpu.detect(im)) for im in imgs])
    gpu = np.median([timeit(lambda im=im: det_gpu.detect(im)) for im in imgs])
    same = sum(
        [a.name for a in det_cpu.detect(im) if a.present] == [b.name for b in det_gpu.detect(im) if b.present]
        for im in imgs[:10])
    print(f"\n窓・アイコンの認識: CPU {cpu:.1f} ms / GPU (OpenCL) {gpu:.1f} ms  → {cpu / max(gpu, 1e-3):.2f} 倍"
          f"  (結果が同じ {same}/{min(10, len(imgs))} 枚)")
    return cpu, gpu


def train_report() -> None:
    """学習の行列の掛け算を、スレッドの数を変えて測る (自動学習は既定でコア数の半分)。"""
    try:
        from threadpoolctl import threadpool_limits
    except ImportError:
        threadpool_limits = None
    a = np.random.default_rng(0).standard_normal((512, 259)).astype(np.float32)
    w = np.random.default_rng(1).standard_normal((259, 96)).astype(np.float32)
    f = lambda: [a @ w for _ in range(200)]  # noqa: E731
    from akm.edge.cycle import train_threads

    print(f"\n学習 (numpy): 自動学習のスレッド {train_threads()} (コア {os.cpu_count()} の半分。AKM_TRAIN_THREADS で変更)")
    if threadpool_limits is None:
        print(f"  行列の掛け算 200 回: {timeit(f, 3):.0f} ms  (スレッド別の比較は pip install threadpoolctl で)")
        return
    for n in sorted({1, train_threads(), os.cpu_count() or 1}):
        with threadpool_limits(n):
            print(f"  スレッド {n:2d}: 行列の掛け算 200 回 {timeit(f, 3):6.0f} ms")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=20, help="使う画面の枚数")
    args = ap.parse_args()
    info = env_report()
    imgs = frames(args.n)
    if not imgs:
        raise SystemExit("画面がありません (学習ループが dataset/frames に貯めます)")
    print(f"画面 {len(imgs)} 枚 ({imgs[0].shape[1]}x{imgs[0].shape[0]}) で測ります")
    stage_report(imgs)
    cpu, gpu = opencl_report(imgs, info)
    train_report()
    print("\nおすすめ:")
    if info["opencl"] and gpu and cpu / gpu >= 1.3:
        print(f"  GPU の方が {cpu / gpu:.1f} 倍速いので、config.yaml に次を書いて学習ループを起動し直してください:\n"
              "    performance:\n      opencl: true")
    elif info["opencl"] and gpu:
        print("  GPU (OpenCL) は速くならないので、今のまま (CPU) が良いです (画面が小さく、GPU へ送る時間の方がかかる)")
    else:
        print("  GPU (OpenCL) が使えないので、今のまま (CPU) で")
    print("  自動学習は既定で CPU の半分のスレッド・低い優先度で動きます (ゲーム・認識・液晶への送信を先に)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
