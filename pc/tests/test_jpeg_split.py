"""JPEG を上下 2 つに分けた 'B' 'J': ESP32 と同じ TJpgDec (firmware の akm_tjpgd.c) で、それぞれを決まった行に
展開すると元の絵 (JPEG の誤差の範囲) に戻る。下半分の高さ (71 行) は MCU (16 行) の倍数でなくてよい"""
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

PC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PC))

from akm.mirror import JPEG_SPLIT_Y, encode_jpeg_split  # noqa: E402

FW = PC.parent / "firmware" / "AutoKeyMouse"

HARNESS = r"""
#include <cstdio>
#include <cstring>
#include <vector>
extern "C" {
#include "akm_tjpgd.h"
}
static uint16_t fb[135][240];
struct Src { const uint8_t* p; size_t n, pos; int y0; };
static size_t in(JDEC* jd, uint8_t* b, size_t n) {
  Src* s = (Src*)jd->device; if (n > s->n - s->pos) n = s->n - s->pos;
  if (b) memcpy(b, s->p + s->pos, n); s->pos += n; return n; }
static int out(JDEC* jd, void* bm, JRECT* r) {
  Src* s = (Src*)jd->device; int bw = r->right - r->left + 1, bh = r->bottom - r->top + 1;
  for (int y = 0; y < bh; y++) { int yy = r->top + s->y0 + y; if (yy >= 135) break;
    memcpy(&fb[yy][r->left], (uint16_t*)bm + y * bw, bw * 2); }
  return 1; }
int main() {
  std::vector<uint8_t> d; int c; while ((c = getchar()) != EOF) d.push_back((uint8_t)c);
  if (d.size() < 3 || d[0] != 'B' || d[1] != 'J') return 1;
  int n = d[2]; size_t pos = 3 + 6 * n;
  static uint32_t work[TJPGD_WORKSPACE_SIZE / 4 + 1];
  for (int i = 0; i < n; i++) {
    const uint8_t* e = &d[3 + 6 * i];
    int y0 = e[0] | e[1] << 8; size_t len = e[2] | e[3] << 8 | e[4] << 16 | (uint32_t)e[5] << 24;
    Src s{&d[pos], len, 0, y0}; JDEC jd;
    if (akm_jd_prepare(&jd, in, work, sizeof(work), &s) != JDR_OK) return 2;
    jd.swap = 0;
    if (akm_jd_decomp(&jd, out, 0) != JDR_OK) return 3;
    pos += len;
  }
  fwrite(fb, sizeof(fb), 1, stdout);
}
"""


def test_split_jpeg_roundtrip_with_esp32_decoder(tmp_path):
    cv2 = pytest.importorskip("cv2")
    gxx, gcc = shutil.which("g++"), shutil.which("gcc")
    if not gxx or not gcc:
        pytest.skip("g++ / gcc が無い")
    (tmp_path / "h.cpp").write_text(HARNESS)
    obj = tmp_path / "tjpgd.o"
    subprocess.run([gcc, "-O1", "-c", "-I", str(FW), "-o", str(obj), str(FW / "akm_tjpgd.c")], check=True)
    exe = tmp_path / "h"
    subprocess.run([gxx, "-O1", "-I", str(FW), "-o", str(exe), str(tmp_path / "h.cpp"), str(obj)], check=True)

    yy, xx = np.mgrid[0:135, 0:240]
    img = np.stack([xx * 255 // 239, yy * 255 // 134, (xx + yy) * 255 // 373], -1).astype(np.uint8)
    img[40:90, 100:160] = (30, 200, 250)  # 分け目 (64 行) をまたぐ四角
    data = encode_jpeg_split(img, 90)
    assert data[:3] == b"BJ\x02"
    y0a, la = struct.unpack("<HI", data[3:9])
    y0b, lb = struct.unpack("<HI", data[9:15])
    assert (y0a, y0b) == (0, JPEG_SPLIT_Y) and len(data) == 15 + la + lb
    out = subprocess.run([str(exe)], input=data, capture_output=True, check=True).stdout
    fb = np.frombuffer(out, "<u2").reshape(135, 240).astype(np.int32)
    rgb = np.stack([(fb & 31) << 3, ((fb >> 5) & 63) << 2, (fb >> 11) << 3], -1)  # B G R
    err = np.abs(rgb - img.astype(np.int32)).mean(axis=(1, 2))
    assert err.max() < 8, err.max()  # 全部の行 (下端の 71 行目まで) が埋まり、元の絵に近い
