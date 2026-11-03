"""akm/maploc.py の座標だけの読み取り: 表示が切れている (閉じ括弧が無い) ものは使わない。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from akm.maploc import parse_xy_strict, widen_roi  # noqa: E402


def test_parse_xy_strict():
    assert parse_xy_strict("Crywolf (225, 18)") == (225, 18)
    assert parse_xy_strict("Crywolf Fortress (222, 1") is None   # マップ名が長くて表示が切れた
    assert parse_xy_strict("Lost Tower (12 , 7)") == (12, 7)
    assert parse_xy_strict("x (1O, l8)") == (10, 18)              # O → 0, l → 1
    assert parse_xy_strict("(300, 5)") is None                    # 0〜255 の外
    # 英語の OCR が無い PC (日本語で読む): "7)" → "刀"、全角
    assert parse_xy_strict("Noria ( 55 , 13 刀") == (55, 137)
    assert parse_xy_strict("Noria （５５，１３７）") == (55, 137)


def test_widen_roi():
    x, y, w, h = widen_roi([0.86, 0.955, 0.14, 0.045])
    assert abs(x - 0.80) < 1e-9 and abs(x + w - 1.0) < 1e-9 and y == 0.955 and h == 0.045
