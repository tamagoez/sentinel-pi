# Sentinel

Raspberry Pi 3B+ (DietPi) 向けの統合システムです。監視カメラ、常時 BGM 再生、
Bluetooth スピーカー、パフォーマンス監視、ネットワークのアクセス記録、
Web 端末を 1 つのサービスにまとめ、ポート 8080 で完結させます。

## 導入

git clone してから `setup.sh` を実行します。スクリプトが判断できる部分は自動で
進み、人の判断が要る 5 か所 (H1〜H5) で止まって尋ねる、半自動の導入です。
詳細は `SETUP.ja.md` (日本語) / `SETUP.md` (English) を参照してください。
Windows から SD カードを準備する場合、`windows/Configure-DietPi.ps1` で
`dietpi.txt` の事前編集を自動化できます。

```bash
# DietPi の初回設定が終わった直後の状態から
apt-get update && apt-get install -y git     # または dietpi-software install 17
git clone https://github.com/tamagoez/sentinel-pi.git ~/sentinel-pi
cd ~/sentinel-pi
sudo ./setup.sh          # H1・H2 → 前提ソフト導入 → H3 (再起動)

# 再起動後、同じコマンドで続きから再開します
sudo ./setup.sh          # H4 → 本体導入 → H5
```

| 印 | 人が介入する内容 |
|---|---|
| H1 | IP アドレスの確認 (固定アドレス推奨) |
| H2 | WiFi ホットスポットの SSID とパスフレーズ (導入しない選択も可) |
| H3 | 再起動 (Bluetooth・音声・SWAP の変更を反映するため) |
| H4 | `dietpi-drive_manager` で外部ストレージを `/mnt/VIDEOSD` にマウント |
| H5 | Web UI でパスワード・Discord Webhook を設定 |

進捗は `/var/lib/sentinel/setup-stage` に記録されるため、途中で中断しても
同じコマンドで再開できます。個別に動かしたい場合は
`sudo ./bootstrap.sh` → 再起動 → ドライブのマウント → `sudo ./install.sh`
の順で、`setup.sh` はこれらを繋いで人の確認を挟むだけのものです。

更新するときは `sudo ./update.sh` を実行します (`git pull` から
`bootstrap.sh`・`install.sh` の再実行まで一括で行います。設定とデータは
保持されます)。

なお、DietPi の初回起動前に `dietpi.txt` の言語設定を英語 (`en_US.UTF-8` /
`us`) にしておくことを強く推奨します。物理コンソールや素のシリアル端末では
日本語が正しく描画されないためで、スクリプトの出力もすべて英語で統一して
います (Web UI 自体はブラウザ描画のため日本語のままです)。

前提として、DietPi-Software から AdGuard Home・WiFi Hotspot・FFmpeg・yt-dlp が
導入されます (`bootstrap.sh` が ID 5, 7, 17, 60, 126, 130, 182, 195 を導入)。

AdGuard Home は DietPi が設定済みの状態で入るため、初期設定ウィザードはあり
ません。ログインという概念自体が不要で (Guardian が認証設定を常に空にします)、
外部からの直接アクセスも既定でブロックされています。フィルタ設定などを直接
編集したい場合だけ、Pi 上で `sudo sentinel-adguard-8083 enable [分数]` を実行
すると一時的に開放できます (既定 15 分で自動的に再ブロックされます)。

## 機能

### 動作モード

| モード | 条件 | 動作 |
|---|---|---|
| 通常 | 動体検知あり | 全カメラ稼働、BGM 再生、ガバナ `ondemand` |
| エコ | 全カメラで 10 分 無検知 (変更可) | BGM 完全停止、カメラ低 FPS・低解像度、ガバナ `conservative` |
| 緊急 | CPU 温度 80℃ 以上 | カメラ 1 台に縮退、Discord 通知 |

CPU 温度 72℃ 以上では、動体検知の有無に関わらず強制的にエコへ移ります。
なお **60℃ 超は Pi 3B+ の正常域**です (仕様上のソフトリミット)。

### 常時 BGM 再生

`<データ領域>/music/` の mp3 を再生します。エコモードでは再生位置を保存して
プロセスごと終了させ、ディスク I/O を完全に止めます。通常モードへの復帰時、
曲・再生位置・プレイリストの並び順まで元通りに戻ります。

シャッフルは乱数の種を固定しているため、並び順そのものが再現されます。

Web UI から URL を貼れば yt-dlp で追加できます (mp3 で保存)。
エコモード中と定時処理中はダウンロードが自動的に待機します。

### Bluetooth スピーカー

スマートフォンなどから接続すると BGM を退避し、切断すると元の位置から
再開します。接続の検知は 3 秒間隔です。

### 監視カメラ

USB カメラを自動検出します (`/dev/v4l/by-id` を使うため、再接続しても
ID が変わりません)。動体を検知したら即座に Discord へ通知し、
検知が数分間 途絶えたらその期間の統計をまとめて送ります。

### ネットワークのアクセス記録

AdGuard Home のクエリログを取得し、ドメインをサービス名 (YouTube、X、LINE など)
に変換して日次で記録します。HTTPS の中身は見えないため、記録できるのは
DNS の問い合わせ先です。

AdGuard Home の管理画面は localhost 限定・ログイン不要の設計で、日常的に
開く想定がありません。直接開きたいときだけ `sudo sentinel-adguard-8083
enable [分数]` で一時的に外部へ開放できます (「導入」の節を参照)。

### 定時処理 (毎日 4 時)

1. カメラと音楽を停止
2. 動体検知時の静止画からカメラごとのタイムラプスを生成
3. 全カメラをタイル状に統合 (映像がないカメラは NODATA 表示)
4. アクセスログをテロップとして下部に重畳
5. `<データ領域>/archive/` に保存して再起動

映像が 1 枚もない日でも、NODATA 画面 + テロップで動画を生成します。

### エラー収集と診断

全モジュールの警告・エラーを自動で集約します。Web UI の「エラー」タブで
発生元別の件数と直近の一覧を確認でき、詳細 (トレースバック) はクリックで
展開できます。

原因調査が必要なときは、次のいずれかで診断バンドルを取得できます。

- **Web UI** — 「エラー」タブの「診断バンドルをダウンロード」
  (アプリ内の状態のみ: 設定・エラー履歴・各モジュールの状態)
- **CLI** — `sentinel-diagnose` (システム全体を含む: journalctl・iptables・
  Bluetooth の実機状態・カメラ検出状況などを 1 つの tar.gz にまとめます)

```bash
sentinel-diagnose            # /tmp に作成
sentinel-diagnose ~/report   # 出力先を指定
```

ストレージ使用率が 92% を超えた場合、Guardian が自動で 1 回だけ
診断バンドルをデータ領域に残します。

### Web 端末

対話型のシェルをブラウザから使えます。root 権限が必要な場合は端末内で
`su -` を実行してください。**パスワードは端末の中だけで扱われ、
Sentinel は受け取りません。** 認証は OS の PAM が担当します。

セッションの開始と終了は Discord へ通知されます。

## キーボード操作

Web UI では数字キーでページを切り替えられます。

`1` ダッシュボード / `2` カメラ / `3` イベント / `4` 音楽 /
`5` ネットワーク / `6` アーカイブ / `7` 端末 / `8` 設定 / `9` ログ /
`Esc` オーバーレイを閉じる

## ファイル配置

```
~/sentinel-pi/                  git clone した作業ツリー (setup.sh はここから)
/opt/sentinel/                  アプリ本体
/opt/sentinel/.venv/            Python 環境
/mnt/VIDEOSD/sentinel/          データ (設定・音楽・録画・記録)
  ├ config.json                 全設定
  ├ state.json                  再生位置などの復帰情報
  ├ music/                      楽曲
  ├ captures/<カメラ>/<日付>/    動体検知の静止画
  ├ archive/<日付>/              日次の統合動画
  ├ netlog/<日付>.jsonl          アクセス記録
  └ sentinel.log                ログ (4MB × 4 世代でローテーション)
/dev/shm/sentinel-runtime/      高頻度の書き込み (RAM 上)
```

## 運用

```bash
systemctl status sentinel          # 状態
journalctl -u sentinel -f          # ログを追う
systemctl restart sentinel         # 再起動
sentinel-diagnose                  # システム+アプリの統合診断バンドルを作成
sudo ./update.sh                   # 更新 (何度実行しても安全です)
```

サービスは `Restart=always` で、どんな理由で落ちても 5 秒後に復帰します。
アプリ内部のタスクも監督され、例外で停止しても指数バックオフで再起動します。

## 既知の制約

- 内蔵 WiFi と Bluetooth はチップを共有するため、ホットスポット運用中は
  Bluetooth 再生に音飛びが出ることがあります (許容前提の構成です)
- テロップは Pillow で描画するため、`python3-pil` またはフォントが無い環境
  では省略されます (ffmpeg の `drawtext` 対応可否には左右されません)
- `h264_v4l2m2m` が使えない場合、`libx264 -preset ultrafast` に自動で落ちます
