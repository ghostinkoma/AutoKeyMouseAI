"""ESP32 の液晶へテスト画像 (色の帯 + 動く四角 + 数字) を送り、届くか確かめる。ゲーム・OBS・仲介アプリは使わない。

    python tools\\esp_send_test.py                 # config.yaml の device.host へ 15 秒
    python tools\\esp_send_test.py 192.168.1.43 --seconds 30 --format jpeg

順に確かめて、どこで止まっているかを表示する:
  1. ESP32 の Web (http://<IP>/status) に返事があるか
  2. 画面の専用ポート (TCP 5005) につながるか
  3. 画像を送って、ESP32 から受け取りの返事が来るか (送れた枚数・fps)
"""
import argparse
import socket
import time

import _path  # noqa: F401  (akm を import できるようにする)

import cv2
import numpy as np


def test_image(i: int) -> np.ndarray:
    img = np.zeros((135, 240, 3), np.uint8)
    for k, c in enumerate([(0, 0, 255), (0, 255, 0), (255, 0, 0), (0, 255, 255), (255, 0, 255), (255, 255, 0)]):
        img[:, k * 40:(k + 1) * 40] = c
    x = (i * 6) % 200
    cv2.rectangle(img, (x, 45), (x + 40, 90), (255, 255, 255), -1)
    cv2.putText(img, f"AKM TEST {i}", (8, 125), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host", nargs="?")
    ap.add_argument("--port", type=int, default=5005)
    ap.add_argument("--seconds", type=float, default=15)
    ap.add_argument("--format", default="bad16", choices=["bad16", "jpeg"])
    ap.add_argument("--fps", type=float, default=30)
    ap.add_argument("--http-port", type=int, default=80)
    a = ap.parse_args()
    host = a.host
    if not host:
        from akm.config import load_config

        host = str((load_config("config.yaml").get("device") or {}).get("host") or "")
    if not host:
        raise SystemExit("ESP32 の IP を指定してください (例: python tools\\esp_send_test.py 192.168.1.43)")

    web = host if a.http_port == 80 else f"{host}:{a.http_port}"
    print(f"1) http://{web}/status ...", end=" ", flush=True)
    try:
        import requests

        st = requests.get(f"http://{web}/status", timeout=5).json()
        print(f"OK  firmware={st.get('fw')}  ble={st.get('ble')}  wifi={st.get('wifi')}")
    except Exception as e:
        print(f"返事なし ({e})\n   → PC と ESP32 が同じネットワークか、IP が合っているか確認してください")
        return

    print(f"2) TCP {host}:{a.port} ...", end=" ", flush=True)
    try:
        socket.create_connection((host, a.port), timeout=5).close()
        print("OK")
    except Exception as e:
        print(f"つながりません ({e})\n   → ESP32 のログに [STREAM] mirror stream on tcp port 5005 が出ているか確認してください")
        return
    time.sleep(0.5)  # 今の接続を ESP32 が閉じるのを待つ

    from akm.mirror import Mirror

    n = {"i": 0}

    def grab():
        n["i"] += 1
        return test_image(n["i"])

    print(f"3) {a.format} で {a.seconds:.0f} 秒送ります (液晶に色の帯と動く白い四角が出れば成功)")
    m = Mirror(host, 1.0 / a.fps, fmt=a.format, grab=grab, transport="tcp", port=a.port)
    m.set_lines([])
    t0 = time.monotonic()
    while time.monotonic() - t0 < a.seconds:
        time.sleep(1)
        el = time.monotonic() - t0
        print(f"   {el:4.0f}s  送れた {m.sent} 枚 ({m.sent / el:4.1f} fps)  1 枚 {m.last_bytes} バイト  失敗 {m.errors}")
    m.stop()
    print("結果:", "成功" if m.sent > 0 else "1 枚も送れませんでした (上の [mirror] の行を見てください)")


if __name__ == "__main__":
    main()
