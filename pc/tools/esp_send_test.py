"""ESP32 の液晶へテスト画像を送り、届くか・何 fps 出るか・どこに時間がかかっているかを測る。
ゲーム・OBS・仲介アプリは使わない (通信路と ESP32 だけを試す)。

    python tools\\esp_send_test.py 192.168.1.43                        # Wi-Fi (TCP 5005)
    python tools\\esp_send_test.py 192.168.1.43 --serial COM18          # 有線 2,000,000bps (frame-stream-15 以降)
    python tools\\esp_send_test.py 192.168.1.43 --serial COM18 --baud 921600
    python tools\\esp_send_test.py 192.168.1.43 --pattern game          # ゲームのように大きく変わる画像 (1 枚 15〜25KB)

  --pattern bars : 色の帯 + 動く白い四角 (変わるところが少ない。1 枚 1KB 前後)
  --pattern game : 画面全体が少しずつ動く模様 (ゲーム画面に近い大きさ)

IP を渡すと、最後に ESP32 自身が測った内訳 (受信待ち / 展開 / 描画) を Wi-Fi の /status から読んで出す。
有線で試すときは、シリアルモニタや仲介アプリを閉じて COM ポートを空けておく。
"""
import argparse
import socket
import time

import _path  # noqa: F401  (akm を import できるようにする)

import cv2
import numpy as np


def bars_image(i: int) -> np.ndarray:
    img = np.zeros((135, 240, 3), np.uint8)
    for k, c in enumerate([(0, 0, 255), (0, 255, 0), (255, 0, 0), (0, 255, 255), (255, 0, 255), (255, 255, 0)]):
        img[:, k * 40:(k + 1) * 40] = c
    x = (i * 6) % 200
    cv2.rectangle(img, (x, 45), (x + 40, 90), (255, 255, 255), -1)
    cv2.putText(img, f"AKM TEST {i}", (8, 125), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)
    return img


_TEX = None


def game_image(i: int) -> np.ndarray:
    """模様が毎回 3 画素ずつ流れる (ゲームで画面が動いているときのように、ほぼ全部のブロックが変わる)"""
    global _TEX
    if _TEX is None:
        rng = np.random.default_rng(1)
        t = (rng.random((270, 480, 3)) * 255).astype(np.uint8)
        _TEX = cv2.GaussianBlur(t, (0, 0), 4)
        _TEX = cv2.normalize(_TEX, None, 0, 255, cv2.NORM_MINMAX)
    s = (i * 3) % 240
    img = _TEX[s // 2:s // 2 + 135, s:s + 240].copy()
    cv2.putText(img, f"GAME {i}", (8, 125), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    return img


def esp_stats(host: str, http_port: int):
    try:
        import requests

        web = host if http_port == 80 else f"{host}:{http_port}"
        return requests.get(f"http://{web}/status", timeout=3).json()
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host", nargs="?", help="ESP32 の IP (Wi-Fi で送るとき・内訳を読むとき)")
    ap.add_argument("--serial", help="有線で送る COM ポート (例 COM18)")
    ap.add_argument("--baud", type=int, default=2_000_000)
    ap.add_argument("--port", type=int, default=5005)
    ap.add_argument("--seconds", type=float, default=15)
    ap.add_argument("--format", default="bad16", choices=["bad16", "jpeg"])
    ap.add_argument("--color", default="343", help="bad16 の色のビット数 (343 / 332 / 111 など)")
    ap.add_argument("--pattern", default="bars", choices=["bars", "game"])
    ap.add_argument("--fps", type=float, default=60)
    ap.add_argument("--http-port", type=int, default=80)
    a = ap.parse_args()
    host = a.host
    if not host and not a.serial:
        from akm.config import load_config

        host = str((load_config("config.yaml").get("device") or {}).get("host") or "")
    if not host and not a.serial:
        raise SystemExit("ESP32 の IP か --serial COMx を指定してください")

    up0 = None
    if host:
        st = esp_stats(host, a.http_port)
        print(f"1) http://{host}/status ... " + (f"OK  firmware={st.get('fw')}  起動から {st.get('uptime')} 秒  "
                                                f"空きメモリ {st.get('heap')} (最小 {st.get('heapMin')})" if st else "返事なし"))
        up0 = (st or {}).get("uptime")
        if not st:
            print("   → Web の返事がありません (IP・同じネットワークか確認)。送信は続けて試します")
    link = None
    if a.serial:
        from akm.serial_link import SerialFrameLink

        print(f"2) 有線 {a.serial} {a.baud:,}bps ...", end=" ", flush=True)
        try:
            SerialFrameLink(a.serial, a.baud, log=lambda *x: None).close()
            print("OK")
        except Exception as e:
            print(f"だめ ({e})")
            return
        time.sleep(0.3)

        def make_link():
            return SerialFrameLink(a.serial, a.baud)

        link = make_link
    else:
        print(f"2) TCP {host}:{a.port} ...", end=" ", flush=True)
        try:
            socket.create_connection((host, a.port), timeout=5).close()
            print("OK")
        except Exception as e:
            print(f"つながりません ({e})")
            return
        time.sleep(0.5)

    from akm.bad16 import parse_color
    from akm.mirror import Mirror

    make = game_image if a.pattern == "game" else bars_image
    n = {"i": 0}

    def grab():
        n["i"] += 1
        return make(n["i"])

    route = f"有線 {a.serial} {a.baud:,}bps" if a.serial else "Wi-Fi"
    print(f"3) {route} / {a.format} {a.color} / 模様 {a.pattern} で {a.seconds:.0f} 秒送ります")
    m = Mirror(host or "serial", 1.0 / a.fps, fmt=a.format, grab=grab, transport="tcp", port=a.port,
               pipeline=1 if a.serial else 2, link_factory=link, b16_bits=parse_color(a.color))
    m.set_lines([])
    t0 = time.monotonic()
    total = 0
    last_sent = 0
    try:
        while time.monotonic() - t0 < a.seconds:
            time.sleep(1)
            el = time.monotonic() - t0
            total += m.last_bytes * (m.sent - last_sent)
            last_sent = m.sent
            print(f"   {el:4.0f}s  送れた {m.sent} 枚 ({m.sent / el:4.1f} fps)  1 枚 {m.last_bytes} バイト  "
                  f"約 {total / el / 1024:5.1f} KB/s  失敗 {m.errors}")
    except KeyboardInterrupt:
        pass
    m.stop()
    el = time.monotonic() - t0
    print(f"結果: {route}  {m.sent / el:4.1f} fps  " + ("成功" if m.sent > 0 else "1 枚も送れませんでした"))
    if host:
        st = None
        for _ in range(5):  # 再起動中・忙しいときは少し待って聞き直す
            st = esp_stats(host, a.http_port)
            if st:
                break
            time.sleep(1)
        if not st:
            print("ESP32 の /status に返事がありません (固まっている / 再起動中かもしれません)")
        elif up0 is not None and st.get("uptime", 0) < up0:
            print(f"!! ESP32 が試験の途中で再起動しました (起動から {st.get('uptime')} 秒)。"
                  "シリアルモニタに出た reset reason / Backtrace の行を見てください")
        else:
            print(f"ESP32 は再起動していません  空きメモリ {st.get('heap')} (最小 {st.get('heapMin')}  "
                  f"最大の塊 {st.get('heapBlock')})")
        mm = (st or {}).get("mirror")
        if mm:
            print(f"ESP32 の内訳 (直近 5 秒): {mm.get('fps', 0):.1f} fps  受信待ち {mm.get('recv_ms', 0):.0f} ms / "
                  f"展開 {mm.get('decode_ms', 0):.0f} ms / 描画(SPI) {mm.get('draw_ms', 0):.0f} ms  "
                  f"CPU {mm.get('cpu_pct', 0):.0f}%" + (f"  SPI {mm['spi_mhz']:.0f}MHz" if "spi_mhz" in mm else ""))
            if "parse_ms" in mm:
                print(f"   展開の内訳: 命令の読み取り {mm['parse_ms']:.1f} ms / 画素の組み立て {mm['asm_ms']:.1f} ms / "
                      f"窓認識への受け渡し {mm['edge_ms']:.1f} ms" + (f"  DMA {'on' if mm.get('dma') else 'off'}"))


if __name__ == "__main__":
    main()
