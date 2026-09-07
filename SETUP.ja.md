# セットアップ手順 — 空の SD カードから Sentinel を動かすまで

English: [SETUP.md](SETUP.md)

Sentinel は `git clone` してから `setup.sh` を実行して導入します。`setup.sh`
はスクリプトで判断できる部分は自動で進め、人の判断が要る 6 か所
(**H1**〜**H6**、下記) で止まって尋ねます。

DietPi 本体とこのプロジェクトのコンソール出力は、意図的にすべて英語です。
物理 HDMI コンソールや素のシリアル端末では日本語グリフが正しく描画されない
ためです。Web UI はブラウザで描画するため、そこだけは日本語のままです。

## 全体の流れ

| フェーズ | コマンド | 内容 |
|---|---|---|
| 0 | (お使いの PC 上で) | DietPi を書き込み、`dietpi.txt` を編集 |
| 1 | 初回起動 | DietPi 自体の初期設定 |
| 2 | `sudo ./setup.sh` | H1・H2 → `bootstrap.sh` → H3 (再起動) |
| 3 | `sudo ./setup.sh` | H4・H5 → `install.sh` → H6 |

`setup.sh` は進捗を `/var/lib/sentinel/setup-stage` に記録するため、H3 の
再起動後は同じコマンドを実行するだけで続きから再開します。いつ再実行しても
安全です。

## フェーズ 0 — 書き込みと事前設定

Raspberry Pi 用の公式 DietPi イメージ (ARMv7 または ARM64) を書き込み、初回
起動前に boot パーティション上の `dietpi.txt` を編集します。

```ini
AUTO_SETUP_LOCALE=en_US.UTF-8
AUTO_SETUP_KEYBOARD_LAYOUT=us
AUTO_SETUP_TIMEZONE=Asia/Tokyo

AUTO_SETUP_NET_ETHERNET_ENABLED=1
# 固定アドレスにします。Web UI のブックマークと、あとで配布する DNS の
# アドレスを固定するためです。192.168.0.x 以外のサブネットを使う場合は
# 下の 4 行をお使いの環境に合わせて変更してください。
AUTO_SETUP_NET_USESTATIC=1
AUTO_SETUP_NET_STATIC_IP=192.168.0.100
AUTO_SETUP_NET_STATIC_MASK=255.255.255.0
AUTO_SETUP_NET_STATIC_GATEWAY=192.168.0.1
AUTO_SETUP_NET_STATIC_DNS=192.168.0.1

AUTO_SETUP_SWAPFILE_SIZE=0

SURVEY_OPTED_IN=0
CONFIG_CPU_GOVERNOR=ondemand

# 任意: 既定の "dietpi" の代わりに使うパスワードをここで指定できます。
# root/dietpi のログインパスワードと AdGuard Home の管理者パスワードの
# 両方になり、初回起動時にこのファイルからは削除されます。
#AUTO_SETUP_GLOBAL_PASSWORD=dietpi
```

`AUTO_SETUP_NET_USESTATIC=1` は必須です。これを付けないと下の
IP・マスク・ゲートウェイ・DNS の項目はすべて無視され、DHCP が使われます。

これ以外に SD カードや外部ドライブへコピーしておくものはありません。
プロジェクト本体はフェーズ 2 で git から取得します。

**Windows をお使いの場合**、上の内容を手でコピー&ペーストする代わりに
`windows/Configure-DietPi.ps1` で自動化できます。

```powershell
# boot パーティションを挿した状態で、PowerShell から実行します
.\windows\Configure-DietPi.ps1
```

boot パーティションのドライブを自動検出し、上記と同じ既定値を適用します。
書き込み前にタイムスタンプ付きのバックアップを作成し、対応しているキー以外
は一切変更しません。パラメータを渡せば値を変更できます
(`-StaticIP`、`-Timezone`、H2 で何も聞かれなくなるよう WiFi ホットスポットを
先に決めておく `-HotspotSsid` / `-HotspotPassphrase`、実際には書き込まずに
確認だけする `-WhatIf` など)。詳細は
`Get-Help .\windows\Configure-DietPi.ps1 -Full` を参照してください。
なお、このスクリプト自身の出力も、このページの他の部分と同じ理由で英語です。

## フェーズ 1 — 初回起動

Ethernet を接続した状態で起動し、`root` / `dietpi` (または上で設定した
パスワード) で SSH ログインします。DietPi の初期設定が走るので、更新が
終わるのを待ち、ライセンスに同意し、聞かれたらパスワードを変更してください。
ソフトウェアの選択は空のままで構いません。必要なものは `bootstrap.sh` が
導入します。

## フェーズ 2 — clone して setup.sh を開始

まっさらな DietPi には git が入っていないので、まずそれを入れます。

```bash
apt-get update && apt-get install -y git     # または: dietpi-software install 17
git clone https://github.com/tamagoez/sentinel-pi.git ~/sentinel-pi
cd ~/sentinel-pi
sudo ./setup.sh
```

`setup.sh` は続けて次を尋ねます。

- **H1 — ネットワークアドレスの確認。** 現在の IPv4 アドレスを表示し、
  DHCP 由来なら警告します。アドレスが変わるとブックマークと、ホットスポット
  が配る DNS の両方が壊れます。
- **H2 — WiFi ホットスポット。** 導入するかどうかと、その SSID・パスフレーズ
  (`/boot/dietpi.txt` の `SOFTWARE_WIFI_HOTSPOT_SSID` / `_KEY` に書き込まれ、
  DietPi のホットスポット導入処理がこれを読みます)。導入しない場合は完全に
  スキップされます。

その後 `bootstrap.sh` が無人で実行され、次の順序で導入します (順序に意味が
あります)。

| 手順 | 内容 | dietpi-software の ID |
|---|---|---|
| 1 | 英語 UTF-8 ロケールを強制 (コンソールの文字化けを防ぐ) | — |
| 2 | ALSA、FFmpeg、Git、Python 3、yt-dlp | 5, 7, 17, 130, 195 |
| 3 | AdGuard Home + Unbound (1 回の呼び出しで DietPi が両者を連携させ、Unbound はポート 5335 へ移動して AdGuard の上流になる) | 126, 182 |
| 4 | WiFi ホットスポット。AdGuard の後に入れるのは、その DHCP を後で AdGuard に向けられるようにするため | 60 |
| 5 | Bluetooth、および APT からの bluez / bluez-alsa-utils / bluez-tools / mpg123 / v4l-utils / python3-opencv | — |
| 6 | 音声を 3.5mm ジャックへ ( `dietpi-set_hardware soundcard rpi-bcm2835-3.5mm` ) | — |
| 7 | SWAP を無効化 | — |

- **H3 — 再起動。** Bluetooth・音声出力・SWAP の変更は再起動しないと反映
  されません。`setup.sh` はここで再起動するか尋ねます。まだ `/opt` には
  何も入っていないため、ここで止めても安全です。

## フェーズ 3 — setup.sh を再開

```bash
cd ~/sentinel-pi
sudo ./setup.sh
```

- **H4 — 外部ドライブ。** `/mnt/VIDEOSD` がマウントされていない場合、
  `setup.sh` が `dietpi-drive_manager` を開きます。そこでドライブをマウント
  し、マウント先を正確に `/mnt/VIDEOSD` に設定してください。ツールが
  `/etc/fstab` に登録するので、再起動後も自動でマウントされます。ここを
  断って SD カードにデータを置くこともできますが、カードの寿命を縮めます。
  一番簡単なのは ext4 ですが、exFAT や NTFS でも構いません。これらは
  Unix の所有権を持たないため `chown` だけでは直せませんが、`install.sh`
  と Guardian がマウントの `uid=`/`gid=` オプションを自動で修正するので、
  `sentinel` ユーザーが問題なく書き込めます。
- **H5 — AdGuard Home。** DietPi が設定済みの状態で導入するため、**初期設定
  ウィザードはありません**。導入した時点ですでに `0.0.0.0:8083` で待ち受け
  ており、ユーザーは `admin`、パスワードは DietPi のグローバルソフトウェア
  パスワード、クエリログも有効です。まだ直接到達できるこの段階で
  `http://<PiのIP>:8083` を開いてログインし、必要ならパスワードを変更して
  ください。この後はポート 8083 が LAN/ホットスポットから遮断されます。
  再度開きたいときは Pi 上で `sudo sentinel-adguard-8083 enable [分数]`
  を実行してください。

続けて `install.sh` が無人で実行されます。非 root の `sentinel` ユーザーを
作成し、`/opt/sentinel` へ配置し、venv を構築し、データ領域を用意し、旧
`camguard` / `music-player` サービスがあれば無効化し、Bluetooth 関連の
サービスを登録し、AdGuard を localhost 限定にし、設定全体を 2 分ごとに
再点検して直す **Guardian** を登録します。

- **H6 — アプリの設定。** `http://<PiのIP>:8080` を開き、設定タブで次を
  設定してください。
  1. **Web UI のパスワード** — 既定は未設定です。Web 端末はポート 8080 に
     到達できる誰にでもシェルを渡してしまうので、必ず設定してください
  2. **Discord Webhook URL**
  3. **AdGuard Home のパスワード** — H5 で使ったもの。クエリログを読むのに
     必要です

## 各フェーズを個別に実行する場合

`setup.sh` は下の 2 つを順につなぐだけなので、それぞれ単体でも動きます。

```bash
sudo ./bootstrap.sh      # 前提ソフト
reboot
dietpi-drive_manager     # /mnt/VIDEOSD をマウント
sudo ./install.sh        # アプリ本体
```

## 定着したことの確認

```bash
systemctl status sentinel
journalctl -t sentinel-guardian --since "10 min ago"   # 何も出ないのが正常
ss -ltn 'sport = :8083'                                # 127.0.0.1:8083 のみ
ls -l /dev/v4l/by-id/                                  # カメラが見えているか
sudo -u sentinel mpg123 /mnt/VIDEOSD/sentinel/music/<file>.mp3   # 途切れないか
vcgencmd measure_temp && vcgencmd get_throttled
```

別の端末から、`http://<IP>:8083` が LAN からもホットスポットからも
**開けない**ことを確認してください。

## 再起動をまたいでも設定が保たれる仕組み

`iptables` のルールはカーネルメモリ上にしかなく、再起動すると消えます。
WiFi ホットスポットも起動後にこれを触ることがあります。設定は 1 回きり
適用するのではなく、**Guardian** (`sentinel-guardian.timer`) が 2 分ごとに
全体を点検して直します。

| 項目 | 何が壊すか | Guardian の対処 |
|---|---|---|
| AdGuard の bind アドレス | AdGuard の自動更新 | `AdGuardHome.yaml` を書き直して再起動 |
| ポート 8083 が実際に遮断されているか | 再起動、hostapd | iptables ルールを再投入 |
| 音声出力 (AUX) | カーネル更新 | ALSA `numid=3` をリセット |
| Bluetooth の discoverable/pairable | bluetoothd の再起動 | `bluetoothctl` で再度有効化 |
| サービスの稼働 | 何らかのクラッシュ | 再度 enable して起動 |
| ストレージの所有権 | ドライブの再マウント、dietpi-drive_manager による exFAT/NTFS の設定リセット | `/etc/fstab` の `uid=`/`gid=` を直す (ext4 なら `chown`)、再マウント |
| ホットスポットの DNS 転送先 | ホットスポットの再設定 | AdGuard へ向け直す |
| ディスク使用量 | 蓄積 | 92% で警告し、診断バンドルを 1 つ保持 |
| yt-dlp のバージョン | サイト側の変更 | 週 1 回の更新を試行 |

## 更新するとき

```bash
cd ~/sentinel-pi
sudo ./update.sh
```

`update.sh` は `git pull` 自体を行い、その後 `bootstrap.sh` と `install.sh`
を再実行します。どちらも冪等 (いつ再実行しても安全) なため、これは単なる
再配置ではなく「最新化して、判明している修正をすべて適用する」ための
1 コマンドです。`sudo ./setup.sh --update` は同じ処理へのエイリアスとして
残しています (以前の `setup.sh --update` は `install.sh` しか実行しておらず、
`bootstrap.sh` 側にしかない修正 — 例えば ffmpeg の drawtext 確認 — が更新時に
静かにスキップされていました。今はそうなりません)。

設定とデータは保持されます。最初からガイド付きで通しでやり直したい場合は
`sudo ./setup.sh --reset` で進捗を忘れさせられます。

## トラブルシューティング

| 症状 | 確認方法 |
|---|---|
| Web UI が開かない | `journalctl -u sentinel -n 60 --no-pager` |
| `PermissionError: ... config.json.tmp` で再起動を繰り返す | `sudo /opt/sentinel/scripts/sentinel-fix-storage-owner.sh /mnt/VIDEOSD/sentinel /mnt/VIDEOSD sentinel` (Guardian も 2 分ごとに自動で再試行します。下の「再起動をまたいでも設定が保たれる仕組み」を参照) |
| カメラが見えない | `ls /dev/v4l/by-id/`、`dmesg \| tail -30` |
| 音が途切れる | 素の再生を試す: `mpg123 <file>`。設定の mpg123 バッファを上げる。PulseAudio が動いていれば止める |
| 音が全く出ない | `aplay -l`。`/boot/dietpi/func/dietpi-set_hardware soundcard rpi-bcm2835-3.5mm` を再実行して再起動 |
| Bluetooth がペアリングできない | `systemctl status sentinel-bt-agent`。`bluetoothctl show` で `Discoverable: yes` になっているか |
| `sentinel-bluealsa*` が `failed (start-limit-hit)` になっている | `sudo systemctl reset-failed sentinel-bluealsa.service sentinel-bluealsa-aplay.service && sudo systemctl restart sentinel-bluealsa.service sentinel-bluealsa-aplay.service` (Guardian も 2 分以内に自動で同じことをします) |
| 8083 がまだ外から開ける | `systemctl start sentinel-guardian`。`journalctl -t sentinel-guardian -n 20` |
| ネットワークログが空 | 設定タブの AdGuard パスワードが違う、または AdGuard 側でクエリログが無効 |
| `setup.sh` が想定と違うフェーズから始まる | `sudo ./setup.sh --reset` |
| それ以外 | `sentinel-diagnose` — システムとアプリの状態を 1 つのアーカイブにまとめます |
