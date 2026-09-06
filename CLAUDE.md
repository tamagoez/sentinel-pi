# Sentinel — 開発時の指針

このファイルは Claude Code など AI に作業を任せるときに最初に読ませる想定です。
「なぜそうなっているか」を書いてあります。ここに反する変更は、
理由を確認せずに行わないでください。

## 対象ハードウェア

Raspberry Pi 3B+ / DietPi / USB カメラ 2 台 / 音声は AUX (3.5mm) 出力 / SWAP なし。

この機種特有の制約が設計の大半を決めています。

| 制約 | 影響 |
|---|---|
| RAM 1GB・SWAP なし | プロセス数と常駐メモリを厳しく抑える。`MemoryMax=700M` を設定済み |
| USB 2.0 ハブを Ethernet と共有 | カメラの FPS と解像度を上げすぎると帯域が枯渇する |
| WiFi と Bluetooth が同一チップ | ホットスポット運用中は A2DP に音飛びが出うる (許容済み) |
| 3.5mm は PWM 出力 | 音質の上限が低い。ゆえに mp3 の再エンコード劣化は問題にならない |
| ソフトリミット 60℃ | **60℃ 超は正常**。1.4GHz → 1.2GHz に落ちるだけ |

## 触ってはいけない前提

### 1. 60℃ を異常扱いしない

Pi 3B+ は既定で 60℃ に達するとクロックを 1.2GHz へ落とします。これは設計通りの
挙動で、`vcgencmd get_throttled` にビットも立ちません。危険域は 80℃ 以降です。
`temp_eco_c` の既定 72℃ はこの事実に基づいています。60℃ に下げないでください。

### 2. 音楽エンジンは mpg123 固定

`ffplay` では Pi のアナログ出力でアンダーラン (音の途切れ) が発生します。
mpg123 は ALSA へ直接書き込むため軽く、実機で安定が確認されています。
**この理由から、ライブラリの形式は mp3 に固定しています。** yt-dlp の出力形式を
m4a や opus に変えると mpg123 が再生できなくなります。変更しないでください。

### 3. 高頻度の書き込みは `/dev/shm` にだけ行う

カメラのフレームと状態は tmpfs (`config.RUNTIME`) 上に置いています。
SD カードの寿命を守るためです。ここを外部ストレージに変えないでください。
再生位置の永続化も、モード遷移時と 1 時間毎に限定しています
(`music.Player.persist`)。

### 4. root で動かさない

`sentinel` ユーザーで動作し、`video` `audio` `bluetooth` `plugdev` グループに
所属します。root 権限が必要な操作は再起動のみで、`/etc/sudoers.d/sentinel` で
それだけを許可しています。

Web 端末で root になる場合は、利用者が端末内で `su -` を実行します。
**アプリは root パスワードを受け取らず、保存も検証もしません。**
認証は OS の PAM がそのまま担当します。この設計を「便利だから」という理由で
アプリ側のパスワード検証に変えないでください。漏洩経路を作ることになります。

### 5. AdGuard Home の Web UI は localhost のみ

`http.address` を `127.0.0.1:8083` に固定し、iptables でも二重に塞いでいます。
管理画面には Sentinel の `/adguard/` プロキシ経由でのみ到達できます
(Sentinel の認証を通過した場合のみ)。

### 6. シェルスクリプトの出力は英語で統一する

`setup.sh` / `update.sh` / `bootstrap.sh` / `install.sh` / `scripts/*.sh` / 
`windows/Configure-DietPi.ps1` の echo・comment・systemd unit の
`Description=` は**すべて英語**です。物理コンソールや素のシリアル端末では
日本語グリフが描画できないため、これらのスクリプトに日本語を混ぜないで
ください。DietPi のロケール・タイムゾーン・キーボードなど、設定内容自体は
日本語環境向けであっても、この原則は変わりません。Python 側
(`sentinel/*.py`、Web UI) はブラウザ描画のため対象外で、従来どおり日本語の
ままで構いません。

`setup.sh` / `update.sh` / `install.sh` は起動時に `sentinel/main.py` の存在を確認する
堅牢なパス解決を行っています (`RAW_DIR` → `SRC` の判定ロジック)。clone が
不完全だった、または内側の `sentinel/` パッケージフォルダの中から実行した、
といった典型的なミスを検出し、`cp` の生の失敗ではなく分かりやすいエラー
メッセージで止めるためです。同じパターンを新しいスクリプトにも踏襲して
ください。

### 7. 導入は git clone からの半自動 (setup.sh)

SD カードや外部ドライブからのコピーではなく、`git clone` した作業ツリーで
`setup.sh` を実行する形に統一しています。`setup.sh` 自体は判断を持たず、

1. 人の判断が要る 6 か所 (H1〜H6) で止まって尋ねる
2. その前後で `bootstrap.sh` と `install.sh` を呼ぶ
3. 進捗を `/var/lib/sentinel/setup-stage` に記録し、再起動を挟んでも
   同じコマンドで再開できるようにする

ことだけを行います。無人で完走させる仕組み (旧 `deploy.sh` の
systemd 再開ユニット) は廃止しました。ドライブのマウント先、ホットスポットの
パスフレーズ、AdGuard の初回ログインは、いずれも人が確認しないと誤りに
気付けないためです。導入手順そのものを変える場合は `SETUP.md` と
`SETUP.ja.md` の表、`setup.sh` のヘッダコメント (H1〜H6) を必ず同時に更新
してください。`SETUP.md` が正、`SETUP.ja.md` はその日本語訳という位置づけ
です。フェーズ 0 (`dietpi.txt` の事前編集) を自動化する
`windows/Configure-DietPi.ps1` も、対応するキーを変える場合は同時に更新して
ください。

**更新は `update.sh`** が担当します。かつて `setup.sh --update` は
`install.sh` しか呼んでいませんでした。しかし ffmpeg の drawtext 確認
(項目 10) のように `bootstrap.sh` 側にしか無い修正もあり、それらが更新時に
静かにスキップされる事例が実際に起きました。`update.sh` は
`git pull` → `bootstrap.sh` → `install.sh` の順で実行します
(`setup.sh --update` は `update.sh` への単なるエイリアスです)。
`bootstrap.sh`・`install.sh` の各ステップは冪等 (すでに満たされていれば
何もしない) なので、毎回フルで再実行しても安全です。**新しい自動修復を
追加するときは、`bootstrap.sh` と `install.sh` のどちらであっても
`update.sh` の再実行だけで確実に反映されることを忘れないでください** —
どちらか片方にしか置かないと、また同じ「更新しても直らない」に戻ります。

`bootstrap.sh` の末尾のメッセージ (「再起動が必要」) は `NEEDS_REBOOT`
フラグで実際に何か変わったときだけ表示します。`update.sh` は毎回
`bootstrap.sh` を呼ぶため、ロケール・Bluetooth・音声・SWAP がすでに
正しい状態なら「再起動不要」と正直に案内しなければならず、これを
やらないと `update.sh` を実行するたびに「再起動が必要」という嘘の案内が
出続けることになります。

### 8. 外部ストレージの所有権は chown だけに頼らない

exFAT・NTFS・vfat は Unix の所有権を持たず、カーネル/FUSE ドライバがマウント
オプションの uid=/gid=/umask= を全プロセスに一律で返すだけです。そのため
`chown` は root からでも失敗する (`Operation not permitted`) か、エラーなく
成功した「ふり」をして実際には何も変わらないかのどちらかになります。
`dietpi-drive_manager` はこれらの形式をマウントするとき uid=/gid= を
`/etc/fstab` に付けないため ([DietPi #4680](https://github.com/MichaIng/DietPi/issues/4680))、
`sentinel` ユーザーが `/mnt/VIDEOSD/sentinel/config.json.tmp` に書けず
`PermissionError` でクラッシュループする、という事例が実際に起きています。

`scripts/sentinel-fix-storage-owner.sh` がこれを解決します。まず
`sentinel` ユーザーが書き込めるか実際に試し (安価なテストなので毎回実行して
問題ありません)、書けなければファイルシステム種別を見て

- exFAT/NTFS/vfat (`vfat`/`exfat`/`ntfs`/`ntfs3`/`fuseblk`) → `/etc/fstab`
  の当該行に `uid=/gid=/umask=` を追記し、umount → mount で反映
- それ以外 (ext4 など通常の Unix ファイルシステム) → 従来どおり `chown -R`

のどちらかで直します。`install.sh` の STEP 4 で 1 回、`sentinel-guardian.sh`
で 2 分ごとに呼ばれるため、`dietpi-drive_manager` を後から再実行してマウント
設定が消えても自動で直ります。**このファイルを削除して単純な `chown -R` に
戻さないでください**。ext4 の外部ドライブしか想定していなければ動きますが、
exFAT/NTFS のドライブでは所有権が直らず今回と同じ症状に戻ります。

修正後の確認は `mount`/`findmnt` の出力ではなく実際の書き込みテスト
(`can_write()`) だけで判断してください。ntfs-3g・exfat-fuse など FUSE 系
ドライバは、カーネルに見える `user_id=`/`group_id=` (FUSE をマウントした
呼び出し元、常に root) と、ファイルの所有者として実際に返す `uid=`/`gid=`
オプションが別物です。`findmnt -no OPTIONS` で `uid=` を grep して検証しよう
とすると、修正が効いているのに「効いていない」という誤検知になります
(実際に踏んだ失敗です)。

umount は 1 回失敗しただけで諦めないでください。`sentinel` を止めた直後でも
ファイルディスクリプタの解放に一呼吸かかることがあるため、数回リトライし、
最後は `umount -l` (lazy) にも倒しています。

### 9. systemd の `StartLimitIntervalSec` は `[Unit]` に書く

`[Service]` に書いても構文エラーにはならず黙って無視されます
(`systemd-analyze verify` で `Unknown key name 'StartLimitIntervalSec' in
section 'Service', ignoring.` と出ます)。`Restart=always` な常駐サービス
(`sentinel.service` や Bluetooth 系ユニット) は既定の「10 秒に 5 回まで」を
超えると `failed (start-limit-hit)` に固定され、`Restart=always` があっても
二度と自動復帰しません。`sentinel-bluealsa*.service` が「初回は起動したのに
`sudo ./install.sh` を再起動なしで再実行したら failed になった」という事例は
これが原因でした (`install.sh` の STEP 6 が `bluetooth.service` を毎回無条件
に再起動し、STEP 8 が `sentinel-bluealsa(-aplay).service` を連続して再起動
することで、短時間に規定回数を超えていました)。新しい常駐サービスを追加する
ときは `[Unit]` セクションに `StartLimitIntervalSec=0` を必ず書いてください。
既に `failed (start-limit-hit)` になっているユニットは `systemctl start` を
呼んでも `start request repeated too quickly` で無視されるため、
`systemctl reset-failed <unit>` を先に呼ぶ必要があります
(`install.sh` の STEP 8 と `sentinel-guardian.sh` の `check_services` は
どちらもこれを行っています)。

### 10. ffmpeg の drawtext フィルタが無いのは大抵 Debian 側のビルド問題

ffmpeg 6.1 以降、`drawtext` フィルタには `libfreetype` だけでなく
`libharfbuzz` も有効化してビルドされている必要があります。Debian の
ffmpeg パッケージは一時期 (trixie/sid の 7:6.1-4) harfbuzz を有効にせず
ビルドしていたため drawtext が丸ごと欠けていました
([Debian #1056597](https://bugs.debian.org/1056597)、7:6.1-5 で修正済み)。
`bootstrap.sh` は drawtext が無いことを検知すると `apt-get update` の後に
`apt-get install --only-upgrade ffmpeg` を試みます。ベースイメージの apt
キャッシュが古いまま (`apt-get update` が一度も走っていない) だとこの
壊れたビルドを掴んだままになるため、これで直ることが多いです。それでも
直らない場合はテロップ (drawtext) を省略するだけで処理は止めません
(「意図的にしていないこと」参照)。

## モジュール構成

各モジュールは疎結合で、`core/state.py` の `MODE` を購読するだけです。

```
setup.sh            初回導入の司会。人の判断が要る箇所で止まり、下の 2 つを呼ぶ
update.sh           更新の司会。git pull → bootstrap.sh → install.sh
bootstrap.sh        前提ソフト (DietPi-Software / APT / 音声 / Bluetooth)。冪等
install.sh          アプリ本体の配置と systemd 登録。冪等、更新時もこれを実行
scripts/sentinel-fix-storage-owner.sh
                    外部ストレージへの書き込み権限を確認し、必要なら
                    fstab のマウントオプションか chown で直す (install.sh
                    と Guardian の両方から呼ばれる)

core/config.py      設定の唯一の保管場所。型と範囲を強制する
core/state.py       モード状態機械。「今どのモードか」の唯一の決定者
core/supervisor.py  タスク監督。例外で落ちても指数バックオフで再起動する

modules/camera.py       カメラ (別プロセス)。動体検知 -> MODE.report_motion()
modules/music.py        mpg123 制御、位置復帰、yt-dlp キュー
modules/thermal.py      温度と CPU -> MODE.report_temperature()
modules/bluetooth.py    A2DP 接続検知 -> 音楽の退避と復帰
modules/terminal.py     pty over WebSocket
modules/netlog.py       AdGuard querylog -> サービス名変換
modules/notify.py       Discord (レート制限対応キュー)
modules/maintenance.py  4 時の定時処理と再起動

web/routes.py           全 HTTP / WebSocket エンドポイント
web/static/index.html   単一ファイル SPA
```

### モジュールを追加するとき

1. `modules/` に置き、`async def loop()` を用意する
2. `main.py` の lifespan で `SUPERVISOR.spawn("名前", モジュール.loop)` を呼ぶ
3. モードに応じた挙動が要るなら `on_mode_change(new, old)` を書いて
   `MODE.subscribe()` に登録する
4. 状態を UI に出すなら `routes._overview()` に追加する

### 設定項目を追加するとき

1. `core/config.py` の `DEFAULTS` に既定値を追加 (型が既定値から推論される)
2. 範囲制限が要るなら `_RANGES` に追加
3. `web/static/index.html` の `GROUPS` と `LABELS` に追加 (日本語ラベル必須)

## モード遷移の優先順位

上から順に評価し、最初に当たった条件を採用します。

1. `温度 ≥ temp_critical_c` → `critical` (カメラ 1 台に縮退、音楽停止)
2. ヒステリシス中 (`温度 > temp_recover_c` かつ直前が温度強制) → `eco` を維持
3. `温度 ≥ temp_eco_c` → `eco`
4. `mode_override` が `auto` 以外 → その値
5. 無検知が `eco_idle_minutes` 以上 → `eco`
6. それ以外 → `normal`

ヒステリシスは温度が閾値付近で振動したときにモードが往復するのを防ぎます。
これを外すとカメラと音楽が数秒おきに止まったり動いたりします。

## 意図的にしていないこと

- **データベースを使わない**。設定は JSON、記録は JSONL。SD カード上で
  破損しても被害が 1 行で済むことを優先しました。
- **HTTPS を張らない**。LAN 内運用の前提です。外部公開する場合は
  リバースプロキシ (Caddy など) を前に置いてください。
- **依存を増やさない**。psutil も dbus-python も使わず、`/proc` と
  `bluetoothctl` の出力で済ませています。Pi でのビルド時間を避けるためです。
- **タイムラプスの映像がなくても処理を止めない**。NODATA 画面を生成して
  テロップだけの動画を作ります。

## エラー収集の仕組み

`core/errors.py` の `LogCaptureHandler` をルートロガーに 1 つ付けているだけです。
各モジュールは通常どおり `log.warning(...)` / `log.exception(...)` を呼べば、
自動的に `GET /api/errors` と診断バンドルに載ります。
**モジュール側で `errors.record()` を個別に呼ぶ必要はありません。** 新しい
モジュールを追加するときも、この点は意識しなくて構いません。

診断バンドル (`modules/diagnostics.py`) は稼働中プロセスのメモリ上の状態
(エラー履歴、各モジュールの `status()`) を集めます。プロセスを跨いだ情報
(journalctl、iptables、Bluetooth の実機状態) は `scripts/sentinel-diagnose.sh`
が別途 CLI から集め、両者を 1 つの tar.gz にまとめます。Web UI の
「診断バンドルをダウンロード」はアプリ内の情報のみです。

## 動作確認の手順

```bash
# 構文チェック
python3 -m py_compile sentinel/core/*.py sentinel/modules/*.py sentinel/web/*.py sentinel/main.py

# 実機での確認
systemctl status sentinel
journalctl -u sentinel -f
curl -s localhost:8080/api/overview | python3 -m json.tool | head -40

# 音の確認 (mpg123 が素で鳴るか)
sudo -u sentinel mpg123 /mnt/VIDEOSD/sentinel/music/任意.mp3

# カメラの確認
v4l2-ctl --list-devices
ls -l /dev/v4l/by-id/

# 温度とスロットリングの確認
vcgencmd measure_temp; vcgencmd get_throttled
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor

# 統合診断 (システム + アプリ)
sentinel-diagnose
```
