# tools/ から akm パッケージを import できるようにする
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 自動学習から呼ばれたとき (akm/edge/cycle.py): OpenCV の使う CPU のスレッドも制限する (ゲーム・認識に残す)
import os  # noqa: E402

if os.environ.get("AKM_CV_THREADS"):
    try:
        import cv2

        cv2.setNumThreads(max(1, int(os.environ["AKM_CV_THREADS"])))
    except (ImportError, ValueError):
        pass
