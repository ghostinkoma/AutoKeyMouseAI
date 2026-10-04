# AutoKeyMouseAI

ESP32-S3 を **BLE キーボード + マウス** として PC につなぎ、Wi-Fi / USB シリアルから
送ったマクロを実行するデバイス。それに **画面認識 (テンプレートマッチング / YOLO)** を
組み合わせて、MU Online (Season 6 私鯖) のエルフを自動で狩らせる実験プロジェクト。

> 注意: ゲームの自動操作はほとんどのサーバの規約で禁止されています。アカウント停止に
> なる可能性を理解したうえで、自己責任で使ってください。

```
 [ PC (Windows) ] ──────────────────────────────────────────┐
   pc/run_bot.py                                            │
   画面キャプチャ → HP/MP・宝石/Zen ラベル検出 → 行動を決める  │
        │ USB シリアル (COM) または Wi-Fi HTTP                │ BLE HID
        ▼                                                    │ (キーボード+マウス)
 [ ESP32-S3 ] firmware/                                      │
   Web UI / API ─ マクロエンジン ─ BLE HID (NimBLE) ──────────┘
```

| ディレクトリ | 内容 |
|---|---|
| `firmware/` | ESP32-S3 のファームウェア (Arduino-ESP32 3.x / NimBLE-Arduino 2.x / PlatformIO) |
| `pc/` | Python のボット本体・認識・調整用ツール |

---

## 1. 準備 (Windows / PowerShell)

```powershell
# Git と Python が無ければ入れる (入れたあとは PowerShell を開き直す)
winget install --id Git.Git -e
winget install --id Python.Python.3.12 -e

# ソースを取得 (D:\hobby\AutoKeyMouseAI が無い、または空の場合)
cd D:\hobby
git clone https://github.com/ghostinkoma/AutoKeyMouseAI.git
cd AutoKeyMouseAI
```

ESP32 が何番の COM ポートか確認:

```powershell
Get-PnpDevice -Class Ports -PresentOnly | Select-Object FriendlyName, Status
```

## 2. ファームウェアを書き込む

```powershell
py -3.12 -m pip install -U platformio                 # PlatformIO は Python 3.10 以上が必要
cd D:\hobby\AutoKeyMouseAI\firmware
# チップと Flash 容量の確認 (モニタは閉じておく)
py -3.12 -m platformio pkg exec -p tool-esptoolpy -- esptool.py --port COM18 flash_id
# 書き込み (-e は基板に合わせる: esp32 / esp32_4mb / esp32s3)
py -3.12 -m platformio run -e esp32 -t upload --upload-port COM18
py -3.12 -m platformio device monitor -p COM18 -b 115200   # ログ確認 (Ctrl+C で終了)
```

| `-e` | 基板 |
|---|---|
| `esp32` (既定) | 無印 ESP32 / Flash 16MB (LilyGO T-Display 16MB など) |
| `esp32_4mb` | 無印 ESP32 / Flash 4MB |
| `esp32s3` | ESP32-S3 / Flash 16MB |

* 初回は ESP32 用のツールチェーンを自動ダウンロードするので数分かかる
* USB-C が 2 つある基板は **COM** 側を使う
* 書き込み中はシリアルモニタを閉じておく (COM ポートを奪い合って失敗する)
* 書き込みに失敗する場合は BOOT ボタンを押したまま RST を押してから再実行

### 書き込みが壊れる場合 (CH9102 搭載基板など)

`MD5 of file does not match` / `Failed to leave compressed flash mode` /
`Possible serial noise or corruption` が出る、または起動ログに `No bootable app partitions` が
繰り返し出る場合は、esptool の補助プログラム (stub) を使わず、ESP32 内蔵の書き込み機能で書く:

```powershell
py -3.12 -m pip install -U esptool
cd D:\hobby\AutoKeyMouseAI\firmware
py -3.12 -m platformio run -e esp32
$b = ".pio\build\esp32"
$app0 = (Get-ChildItem "$env:USERPROFILE\.platformio\packages" -Recurse -Filter boot_app0.bin | Select-Object -First 1).FullName
py -3.12 -m esptool --chip esp32 --port COM18 --baud 115200 --no-stub erase-flash
py -3.12 -m esptool --chip esp32 --port COM18 --baud 115200 --no-stub write-flash 0x1000 $b\bootloader.bin 0x8000 $b\partitions.bin 0xe000 $app0 0x10000 $b\firmware.bin
py -3.12 -m esptool --chip esp32 --port COM18 --baud 115200 --no-stub verify-flash 0x10000 $b\firmware.bin
```

### ステータス LED (ESP32-S3 基板のオンボード RGB, GPIO48。無印 ESP32 では無効)

| 表示 | 状態 |
|---|---|
| 青の点滅 | BLE 未接続 (ペアリング待ち) |
| 緑の点灯 | BLE 接続済み |
| 黄 | マクロ実行中 |
| 赤みが混ざる (紫の点滅 / 黄緑) | Wi-Fi (STA) 未接続 (SoftAP は常に有効) |

光らない基板は `firmware/AutoKeyMouse/config.h` の `STATUS_LED_PIN` を 38 に変更。

## 3. BLE ペアリング

Windows の「設定 → Bluetooth とデバイス → デバイスの追加 → Bluetooth」で
**AutoKeyMouse** を選ぶ。キーボード+マウスとして認識される。
(PC に Bluetooth が無い場合は BLE 対応の USB ドングルが必要)

## 4. Wi-Fi 設定と Web UI / API

### 家の Wi-Fi (PC と同じネットワーク) につなぐ

シリアルモニタから対話形式で設定する:

```powershell
py -3.12 -m platformio device monitor -p COM18 -b 115200
```

```
wifi                       ← 入力して Enter
=== Wi-Fi setup ===
  1) MyHome-2G   -48 dBm  ch6  WPA2
  2) ...
SSID ? (number or name, empty = cancel): 1
Password ? (for "MyHome-2G", empty = open network): ********
[WIFI] got IP 192.168.1.23 ...
[WIFI] OK  IP = 192.168.1.23   Web UI: http://192.168.1.23/
```

* ESP32 は **2.4GHz のみ**。5GHz の SSID は選ばない
* 設定は NVS に保存され、次回から自動で接続する
* 取得した IP は T-Display の液晶にも表示される
* その他のシリアルコマンドは `help` で一覧表示

### SoftAP (設定用)

Wi-Fi 未設定のとき、または STA が 30 秒つながらないときだけ SoftAP `AutoKeyMouse`
(パスワード `akm12345`, `http://192.168.4.1/`) を出す。STA でつながると SoftAP は止まる。

### シリアルのデバッグ出力

`[WIFI]` 接続・切断理由・取得 IP / `[BLE]` 接続・ペアリング / `[HTTP]` リクエスト /
`[MACRO]` 実行結果 が出力される。

| エンドポイント | 説明 |
|---|---|
| `GET /` | Web UI (マクロボタン・編集・設定) |
| `GET /macro?id=N[&repeat=R]` | マクロ N (1-10) を開始。`repeat=0` で停止まで繰り返し。200 / 400 不正 / 409 実行中 / 503 BLE 未接続 |
| `GET/POST /run?s=<script>` | スクリプトを実行し、完了してから応答する (PC ボット用) |
| `GET /stop` | 実行中のマクロを中断 |
| `GET /status` | JSON (BLE・Wi-Fi・実行状態) |
| `GET /macros`, `POST /save` | マクロ一覧 / 保存 (NVS に保存されるので電源を切っても残る) |
| `GET /settings?delay=10&layout=jp` | HID イベント間隔 (5-50ms) / ターゲットのキー配列 |
| `POST /wifi` | `ssid`, `pass` を NVS に保存して接続 |

`/run` は完了まで応答しないため、その間は Web サーバが他の要求を受け付けない。
長いマクロは `/macro` (非同期) を使う。

USB シリアル (115200bps) からも `run <script>` / `macro <id> [repeat]` / `stop` / `status` / `wifi`
で同じ操作ができる (ボットの既定はこちら。Wi-Fi より遅延が小さく、`stop` は実行中でも割り込める)。

### マクロ書式

`;` または改行区切り。`#` で始まる行はコメント。

| コマンド | 意味 |
|---|---|
| `k:a` / `k:ctrl+shift+esc` | キーを押して離す (同時押しは `+`) |
| `kd:shift` / `ku:shift` | 押したまま / 離す |
| `t:/move lorencia` | 文字列入力 (文字列中の `;` は `\;`) |
| `m:100,-50` | マウス相対移動 |
| `a:16384,16384` | マウス絶対移動 (0-32767 で画面全体) |
| `c:L` / `c:R,2` | クリック (回数指定可) |
| `bd:R` / `bu:R` | ボタンを押したまま / 離す |
| `s:-3` | ホイール |
| `w:500` | 待機 (ms) |
| `d:20` | このスクリプト内のイベント間隔 (ms) |
| `ra` | すべてのキー/ボタンを離す |

キー名: `a`-`z`, `0`-`9`, `f1`-`f24`, `enter`, `esc`, `tab`, `space`, `backspace`,
`home`, `end`, `pageup`, `pagedown`, `insert`, `delete`, `up`/`down`/`left`/`right`,
`ctrl`, `shift`, `alt`, `win`, JIS 用 `zenkaku`, `henkan`, `muhenkan`, `kana`, `yen`, `ro`,
または `0x3A` のように HID コードを直接指定。

---

## 5. PC ボット (MU Online S6 / エルフ)

```powershell
cd D:\hobby\AutoKeyMouseAI\pc
py -m venv .venv
.\.venv\Scripts\Activate.ps1      # エラーなら: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
notepad config.yaml               # device.port を COM 番号に、game.window_title を MU のタイトルに
```

MU は **ウィンドウモード** で起動する (排他フルスクリーンだと画面を取れないことがある)。

### 調整の順番

1. **HID の確認**: `python tools\hid_test.py` (メモ帳に文字が入り、カーソルが円を描けば OK)
2. **マウス座標の校正**: `python tools\calibrate_mouse.py` → 出力を `config.yaml` の `mouse.abs_map` に貼る
3. **HP/MP 枠の調整**: `python tools\vision_preview.py` を見ながら `hud.hp.roi` / `hud.mp.roi` を合わせる
   (赤枠/青枠が HP 球・MP 球に重なり、表示の % が実際と合えば OK)
4. **宝石・Zen のテンプレート作成**: 地面に宝石/Zen が落ちている状態で
   `python tools\grab_templates.py` → ラベル文字を囲んで `bless`, `soul`, `zen` などの名前で保存。
   `vision_preview.py` で黄色い枠が付くことを確認 (付かなければ `threshold` を 0.8 程度に下げる)
5. **スキル配置** (ゲーム内): 1 = 攻撃スキル (Triple Shot 等), 2 = Greater Defense,
   3 = Greater Damage, 4 = Heal, Q = HP ポーション, W = MP ポーション
6. **試運転**: `python run_bot.py --dry-run --show` (送る予定のコマンドを表示するだけ)
7. **本番**: `python run_bot.py --show`。起動すると待機状態になり、**PageUp で開始 / PageDown で停止** (`game.start_key_vk` / `stop_key_vk`)。Ctrl+C で終了。MU が前面にないときは何もしない

### ボットの動き

1. HP が 0 のまま 3 秒続いたら死亡と判定 → `elf.death.script` (街から `/move` など) を実行
2. HP が減ったら Heal (自分に右クリック)、さらに減ったら Q / MP が減ったら W
3. 宝石・Zen のラベルがキャラの近くにあれば攻撃をやめてクリック → 拾えたら元の位置へ戻る
4. 60 秒ごとに Greater Defense / Greater Damage を自分に掛け直す
5. 右ボタンを押したまま、モンスターを検出していればその方向、していなければ周囲 8 方向を順に撃つ

### AI 検出 (YOLO) に進む場合

テンプレートマッチングは学習不要ですぐ動くが、モンスターの位置は分からない。
モンスターも狙いたい場合や、ラベルの取りこぼしが多い場合は YOLO を学習させる。

```powershell
python tools\vision_preview.py --record 2.0       # 狩り中の画面を dataset\images に 2 秒ごとに保存
pip install ultralytics label-studio              # ラベル付けツールは好みで (CVAT, Roboflow 等でも可)
# monster / jewel_bless / zen ... のクラスで枠を付け、YOLO 形式で書き出す
yolo detect train data=dataset\data.yaml model=yolo11n.pt imgsz=960 epochs=100
Copy-Item runs\detect\train\weights\best.pt models\mu_s6.pt
```

`config.yaml` の `vision.yolo.enabled: true` にすると、`monster` クラスを狙って撃ち、
それ以外のクラスは拾得対象になる (テンプレートと併用可)。

### テスト (実機不要)

```powershell
pip install pytest
python -m pytest tests
```

---

## 6. マップの記録とビューア

### 現在地の記録 (Python)
画面右下 (ミニマップの下) の `Atlans (32, 68)` を OCR で読み、SQLite (`pc/data/mu_map.db`) に記録する。
実際に立ったマス (= 歩けるマス)、位置の履歴、マップ移動・ワープ、狩場などの登録地点が溜まっていく。

```powershell
cd D:\hobby\AutoKeyMouseAI\pc
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt          # Windows 標準 OCR (winrt) が入る
python tools\map_logger.py --check       # 1 回だけ読む。maplog_crop.png で読み取り位置を確認
python tools\map_logger.py               # 記録を続ける (Ctrl+C で終了)
python tools\map_spot.py add "Atlans 狩場1" --radius 8   # 今いる場所を狩場として登録
python tools\map_spot.py list
```
読み取り位置は `config.yaml` の `maplog.roi`、文字の明るさは `maplog.threshold` で調整する。
Windows 標準 OCR が使えない場合は Tesseract (https://github.com/UB-Mannheim/tesseract/wiki) を入れて
`pip install pytesseract` (`maplog.ocr: tesseract`)。

### ビューア (Java)
`viewer/mapviewer.jar` (Java 17 以上)。記録中でも 1 秒ごとに読み直して表示する。
```powershell
java -jar D:\hobby\AutoKeyMouseAI\viewer\mapviewer.jar D:\hobby\AutoKeyMouseAI\pc\data\mu_map.db
```
* 緑 = 歩いたマス (明るいほど何度も通った) / 黄線 = 足跡 / 赤丸 = 現在地 / 円 = 登録地点 (橙 = 狩場)
* ホイールで拡大縮小、ドラッグで移動
* **狩場の登録**: 上の「現在地を狩場に登録…」(map_logger.py が読んだ今いる場所) か、地図を右クリック →「ここを狩場などに登録…」。
  名前・種類 (狩場 / 薬屋 / 街 / 安全地帯 / その他)・マップ・座標・半径・メモを入力して「登録」
* 右側の一覧で 追加 / 編集 / 削除 / 地図で見る。地図の右クリックで地点の中心を移すこともできる
* **狙うモンスター**: 登録画面の「モンスター」のドロップダウンに、そのマップに出るモンスター (レベル付き) が出る。
  選んで「追加」(一覧に無い名前は手入力でも可)。モンスター一覧は Web ページから取り込む:
  ```powershell
  python tools\import_monsters.py http://munou2014.web.fc2.com/mon_lv.html
  python tools\import_monsters.py URL --dump     # 取り込めないとき: 表の中身を表示
  ```
  日本語のマップ名 (アトランス1 など) はゲーム内の英語名 (Atlans) に変換して突き合わせる
* ビルドし直す場合: `cd viewer` → `mvn package` → `target\mapviewer.jar`
