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

### 5. AdGuard Home はユーザー名/パスワードで認証する

一時期、AdGuard Home 自身の `users:` を常に空にしてログインという概念ごと
無くす設計にしていましたが (実機の 401 対策として)、実機で別の 401 報告が
あり、パスワード認証そのものを求める運用へ戻しています。`netlog.py` の
`_auth_header()` が `adguard_user`/`adguard_password` (設定タブ) から
Basic 認証ヘッダを組み立て、`/control/querylog` へ送ります。AdGuard 側の
実際のパスワードは DietPi の初回セットアップ時のグローバルソフトウェア
パスワードです (ユーザー名は既定で `admin`)。Sentinel 側の設定タブに
**AdGuard 側と同じ値**を入力してください — 一致していなければ引き続き
401 になります。`sentinel-guardian.sh` はもう `users:` を空にしません
(`check_adguard_auth()` は削除済み)。**この方針をまた「ログイン不要」に
戻さないでください** — 少なくとも一度、それでは実機の 401 が解消しません
でした。

到達性は別の話です。`http.address` を `127.0.0.1:8083` に固定し、iptables
でも二重に塞いでいます。8080 経由の `/adguard/` プロキシは撤去したままです
(フィルタ設定などを直接いじりたいまれなケースには
`scripts/sentinel-adguard-8083.sh enable [MINUTES]` で :8083 を一時的に
(既定 15 分、Guardian の次の周期までに自動で再遮断) 開けます。`disable`
で即座に再遮断、`status` で現在の状態を確認できます)。

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

ext4 側の `chown -R` は **`$DATA` (`/mnt/VIDEOSD/sentinel`) だけを直しても
不十分**です。マウントの根 (`$MNT`、`/mnt/VIDEOSD` 自体) の実行ビットが
"other" に無ければ、`$DATA` がどれだけ正しく所有・権限設定されていても
`sentinel` はそこへ辿り着けません。他所で作成・フォーマットされたドライブを
再利用すると、ルートディレクトリの権限 (例: `root:root 0700`) がそのまま
残っていることがあり、これは実際に再現して踏んだ不具合です — 一度目の
`chown -R` 修正はこのケースを想定しておらず、書き込みテストが直らないまま
「still cannot write」を返し続けていました。`chmod o+x "$MNT"` を非再帰的に
1 回呼ぶだけで直ります (通過用の実行ビットを足すだけで、読み書き権限は
一切変えません)。**`$DATA` の chown だけに戻さないでください** — この
`$MNT` 自体のチェックを外すと同じ症状がまた起こります。

exFAT/NTFS 側で `/etc/fstab` を書き換えたあとは `systemctl daemon-reload`
も呼びます。systemd は `/etc/fstab` から自動生成した `.mount` ユニットを
起動時か `daemon-reload` のときにしか読み直さないため、これを呼ばずに
`umount`/`mount` で直接反映させただけだと、書き換え直後は直って見えても
systemd 側のユニットは古いオプションを覚えたままになります。あとで何かが
そのユニットを再度動かす (タイマー、`systemctl restart`、次の再起動) と、
古い壊れたオプションに巻き戻る可能性があります。

**「実機で直っていない」と報告された時の切り分け**: このスクリプトは
失敗時に `id`・`findmnt`・該当する `/etc/fstab` 行・`ls -ld` を
`dump_diagnostics()` で出力します。「fstab エントリが無い」「マウントが
そもそも認識されていない (`findmnt` が空)」「オプションは正しいのに書き
込めない」のどれなのかがこの出力だけで区別できるようにしてあります。
次に同じ報告が来たら、まずこの診断出力を見てから直してください — 過去
2 回、根拠のない推測で直したつもりのものが実機では直っていませんでした。

3 回目でようやくこの診断出力から実際の原因を特定できました:
`dietpi-drive_manager` が書く `noauto,x-systemd.automount` 付きの exFAT は
同じマウントポイントに **autofs (トリガー) と実体のファイルシステムが
二重に重なって** います。オプション無しの `findmnt -no FSTYPE "$MNT"` は
そのマウントポイントに載っている全部を 1 行ずつ (マウントした順、つまり
autofs が先) 出力するため、`fstype` 変数が `"autofs\nexfat"` という複数行
の文字列になり、`case` 文のどのパターンにも一致せず**黙って ext4 用の
`chown` 分岐に落ちていました** (exFAT では意味を持たない分岐です)。
`findmnt ... | tail -n1` で常に最後の行 (今まさに読み書きが通る、直近に
重なった実体側) だけを取るようにして直しています。**この `tail -n1` を
外して素の `findmnt` 出力に戻さないでください** — `noauto,
x-systemd.automount` を使わない単純なマウントでは無害な変更ですが、
DietPi の既定 (`noauto,x-systemd.automount` 付き) では今回と同じ「exFAT
の分岐に一生たどり着けない」不具合に戻ります。

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

### 10. テロップは ffmpeg の drawtext を使わない (Pillow + overlay/drawbox)

ffmpeg 6.1 以降、`drawtext` フィルタには `libfreetype` だけでなく
`libharfbuzz` も有効化してビルドされている必要があります。Debian/Raspberry
Pi OS のパッケージは一時期これを有効にせずビルドしており
([Debian #1056597](https://bugs.debian.org/1056597))、しかも安定版
(bookworm など) は一度リリースされたバージョンを機能追加のために更新しない
ため、`apt-get install --only-upgrade ffmpeg` を何度実行しても直る保証が
ありません — リポジトリに直ったビルドがそもそも存在しないことがあります。
実機でこれを踏み、`--only-upgrade` では解消しないことを確認しました。

そのため、テロップ (NODATA ラベル・カメラごとのキャプション・アクセスログ
のティッカー) は `drawtext` に一切依存しない方式に作り替えています
(`sentinel/modules/maintenance.py` の `_render_text_png()` /
`_can_render_text()`)。文字は Pillow (`python3-pil`) で PNG に描画し、
ffmpeg 側は `overlay` と `drawbox` という、ビルドオプションに関わらず常に
存在するコアフィルタだけで重ねます。Pillow は ffmpeg を一切リンクしない
ので、この種のビルドフラグ問題そのものが起こり得ません。**drawtext を
使う実装に戻さないでください** — 同じ「ディストリのパッケージ次第で機能
が消える」問題に戻ります。フォントが見つからない、または Pillow が無い
場合は `_can_render_text()` が false を返し、テロップ/キャプションを
省略するだけで処理は止まりません (「意図的にしていないこと」参照)。

### 11. CPU ガバナの切り替えは sudo 経由で行う (直接書き込みでは効かない)

`/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor` は root しか書き込め
ません。`sentinel` は非 root ユーザーで動作するため (#4)、
`core/state.py` の `_apply_governor()` が以前これへ直接書き込んでいたコード
は `PermissionError` を握り潰すだけで、実機では **eco モードに入っても
CPU ガバナが一度も切り替わっていませんでした**。「eco モードでも負荷が
ほぼ変わらない」という報告の直接の原因です。エラーも一切表面化しなかった
ため (例外を投げずに黙って `return` していたため) 気付けませんでした。

`scripts/sentinel-set-governor.sh <governor>` を新設し、`/etc/sudoers.d/
sentinel` にこれ 1 本だけを (reboot 系コマンドと並んで) 個別に許可しました。
sudoers は引数の *値* までは制限できないため、渡された governor 名が
カーネルの実際の候補 (`performance`/`powersave`/`userspace`/`ondemand`/
`conservative`/`schedutil`) のいずれかであることをスクリプト側で検証して
から書き込みます。ここが実質的な権限の境界です。**このスクリプトを経由
せず直接 sysfs に書き込むコードへ戻さないでください** — 同じ「実機では
静かに何も起きない」不具合に戻ります。

eco の既定ガバナは `conservative` から `powersave` に変更しました。
`conservative` は結局のところ負荷が続けば周波数を最大まで上げてしまう
可変ガバナで、eco の「確実に下げる」という目的に合っていなかったため
です (`powersave` は常に最低クロックに固定されます)。

また、`core/state.py` の `_apply_governor()` は `ModeManager.evaluate()` が
モード「遷移」を検知したときにしか呼ばれません。起動直後は `_mode` の
初期値が既に `normal` で最初の判定も大抵 `normal` のままのため遷移が
起きず、起動時点でガバナが一切確認・強制されない隙間があります。
`state.sync_governor()` を `main.py` の起動処理から一度だけ呼んで
埋めています。

### 12. Bluetooth/Hotspot の起動直後の競合は、Guardian の周期実行で拾う (再起動では直らない)

RPi 3B+ の Bluetooth チップは UART 接続で、起動時に `hciuart.service`
(raspberrypi-sys-mods 提供) がアタッチを担います。この UART アタッチが
`bluetooth.service` の起動と競合し、`bluetoothd` がコントローラを一つも
掴めないまま起動し切ってしまうことがあります
([DietPi #2390](https://github.com/MichaIng/DietPi/issues/2390) など、
RPi 3/3B+ で広く報告されている既知の競合)。WiFi Hotspot (`hostapd`) も
同様に、無線インタフェースの準備が起動直後にまだ整っていないまま起動を
試みて失敗することがあります。**これは一度きりの故障ではなく毎回一定確率で
起こりうる競合なので、「もう一度再起動する」ことは修正になりません** —
運が良ければ直るだけで、次の再起動でまた起こり得ます。

`sentinel-guardian.sh` の `check_bluetooth()` は `bluetoothctl show` が
コントローラなし ("No default controller available" または空) を返した
場合に `hciuart.service` → `bluetooth.service` の順に再起動します。
`check_services()` も `hciuart.service`/`hostapd.service` を監視対象に
加え、起動直後に failed のまま止まっていれば拾って再起動します。Guardian
は起動 45 秒後に初回実行、以降 2 分ごとなので、これらの競合は再起動を
待たずに次の周期までに自己修復されます。**このチェックを外して「再起動を
促すだけ」の実装に戻さないでください** — 競合の性質上、再現性のある
修正になりません。

もう一つ、これとは別に見つかった不具合として、`bluealsad`/
`bluealsa-aplay` は `bluetoothd` への D-Bus 接続を張ったまま動き続けます。
`bluetooth.service` が (上記の自己修復や `apt` の自動アップグレード、
OOM Kill など) 何らかの理由で再起動すると、この D-Bus 接続は静かに
無効になりますが、`bluealsad` プロセス自体は生きたままなので
`systemctl is-active` は "active" を返し続け、`check_services()` は
異常に気付けません。結果として **Bluetooth 再生が理由不明のまま止まる**
という症状になります。`check_bluealsa_freshness()` は
`bluetooth.service` と各 BlueALSA 系ユニットの `ActiveEnterTimestamp`
を比較し、`bluetoothd` の方が後から起動していれば (= 接続が古い) その
ユニットを再起動します。単純な `is-active` チェックでは生きた接続と
死んだ接続を区別できないため、**この比較をやめて `is-active` だけに
戻さないでください** — 同じ「サービスは動いているのに音が出ない」不具合
に戻ります。

### 13. Discord Webhook には User-Agent ヘッダが必須

Discord は Cloudflare の裏にあり、`urllib.request` の既定 User-Agent
(`Python-urllib/3.x`) を付けたまま送ると、Discord 側に届く前に Cloudflare
が 403 (エラーコード 1010) で弾きます。Webhook URL 自体が正しくても、
AdGuard Home のブロックとも無関係に、このヘッダが無いというだけで
毎回 403 になります。`notify.py` の `_post()` は必ず `User-Agent` ヘッダを
付けて送ってください。**このヘッダを外さないでください** — 同じ 403 に
戻ります。

とはいえ実機の 403 が本当に AdGuard 側のブロックというケースもあり得る
ため、`netlog.check_host_blocked()` (AdGuard の `/control/filtering/
check_host` を呼ぶ) で Webhook 宛先ドメインの実際の判定を確認できるように
しています。ブロックされていれば `netlog.allow_host()` が、そのドメイン
だけを対象にした例外ルール (`@@||host^`) をユーザールールに追記し、他の
ブロックには一切触れずに 403 の原因だけを取り除きます (Web UI 設定タブの
「AdGuard のブロックを解除」ボタン)。

### 14. 日次動画のテロップは「流す」のをやめ、その瞬間の1件だけを表示する

アクセスログの件数が多い日は、右から左へ流れるテロップだと1件あたりの
表示時間が短すぎて文字が重なり、そもそも読める表示幅がありませんでした。
`maintenance.py` の `_build_info_band()` はスクロールをやめ、代わりに
「時計 (左) + その瞬間にアクセスしていたドメイン (右)」を1本の帯映像として
作り、カメラ映像と全く同じ実時刻対応 (`_video_t()`) で下部に静止表示します
(`_render_band_png()` / `_overlay_info_band()`)。内容が変わる瞬間だけ新しい
PNG を描画し、変わらない区間は同じ PNG を繰り返し指定するので (カメラ映像の
concat と同じ手法)、実際に描く PNG の枚数は「時計の分が変わる回数」+
「アクセス先が変わる回数」程度に収まります。

これに合わせて `_make_camera_clip()` も変えています。以前は「最初の
フレームを動画の先頭まで、最後のフレームを動画の末尾まで引き伸ばす」ことで
埋めていました (concat デマルチプレクサが負の時刻を表現できないための
回避策) が、これは「まだ撮っていない/もう撮らなくなった」時間帯を撮影が
続いているかのように見せてしまいます。テキスト描画が使える環境では、
最初の撮影より前・最後の撮影より後を実際に NODATA 表示するようにしました
(フォント/Pillow が無い環境では、処理を止めないという既定方針のとおり
従来の引き伸ばしにフォールバックします)。

**この NODATA フレームは `.jpg` で保存してください (`.png` に戻さないで
ください)。** 実際に撮影されたフレームは全て `.jpg` で、同じ concat
リストの中に `.png` を混在させると、ffmpeg の concat デマルチプレクサが
リスト先頭のファイルで検出したコーデックをストリーム全体に固定してしまい、
途中から別形式が混じった時点で "Invalid PNG signature" のようなデコード
エラーが大量に出てフレームを落とし、動画が `target_seconds` よりずっと
短くなる (他のカメラ・帯映像と尺が揃わなくなる) という不具合を実際に
踏みました。`_render_text_png()` は保存先の拡張子が `.jpg`/`.jpeg` なら
自動で RGBA を RGB に変換してから保存します (JPEG は透過を持てないため)。

### 15. Bluetooth 端末のエイリアス変更は D-Bus 直叩き、音量は numid=1

`bluetoothctl` の対話コマンドに、ペアリング済み**端末側**の表示名
(`org.bluez.Device1.Alias`) を変更する手段はありません。`bluetoothctl
alias <name>` はこの Pi 自身 (ローカルアダプタ) の名前を変えるだけで、
別物です。`bluetooth.set_alias()` は `dbus-send` で `Device1.Alias`
プロパティを直接書き換えます。`dbus-send` は BlueZ 自体が D-Bus 無しには
一切動作しない以上、`bluez` と一緒に必ず入っている前提が使え、
`dbus-python` のような新規依存にはなりません (CLAUDE.md「依存を増やさない」
に沿っています)。

Bluetooth 経由で流れてくる音声は `bluealsa-aplay` が mpg123 を介さず直接
ALSA へ書き込むため、`music_volume` (mpg123 側のソフトウェアゲイン) では
音量を変えられません。`bluetooth._apply_volume()` は `amixer -c <card>
cset numid=1 <%>` で bcm2835 サウンドカードの "PCM Playback Volume" を
直接操作します。**numid=3 (`sentinel-guardian.sh` の `check_audio()` が
使っている出力ルート選択) とは別のコントロールです** — 混同して同じ
numid を使わないでください。端末ごとの音量は `bt_device_volumes`
(MAC アドレス -> %) に保存し、その端末が接続するたびに自動で再適用します。

`core/config.py` にはこの `bt_device_volumes` のために、dict 型の既定値を
持つキーだけ型強制をバイパスする `_DICT_KEYS` を追加しています。他の
設定項目のように文字列化・数値化されると壊れるためです。**このキーを
一般の設定 UI (GROUPS/LABELS) には追加しないでください** — dict を
テキスト入力欄で編集させる作りにはなっていません。

### 16. ペアリングエージェントは bluez-tools の bt-agent を使わない (bluetoothctl 内蔵のものを使う)

`bt-agent --capability=NoInputNoOutput` (bluez-tools) は、Raspberry Pi OS
Bullseye 以降の bluez (5.55+) で「NoInputNoOutput を指定しても着信ペアリング
要求を自動承認しなくなる」既知のリグレッションを抱えています
([RPi-Distro/repo#291](https://github.com/RPi-Distro/repo/issues/291))。
bt-agent は確認応答を返さないまま待ち続け、iPhone 側はこの確認応答の
タイムアウトを **"Pairing Unsuccessful"** としてそのまま表示します —
PIN/パスキーの入力画面にすらならず、原因がエージェント側にあることが
UI からは分かりません。SSP を要求する iOS は PIN 認証 (sspmode=0) へは
フォールバックしないため、「PIN 認証に倒す」回避策も iPhone には使えません。

`bluetoothctl` を対話セッションで使い `agent NoInputNoOutput` /
`default-agent` を自分で打った場合はこのリグレッションの影響を受けません。
そのため `scripts/sentinel-bt-agent.sh` (`sentinel-bt-agent.service` から
起動) は bt-agent を使わず、`bluetoothctl` の標準入力へ同じコマンド列を
流し込んで対話セッションを模倣します。**この一連のパターンを bt-agent や
sspmode=0 の PIN 認証方式に戻さないでください** — 同じ "Pairing
Unsuccessful" に戻ります (bluez-tools パッケージ自体も `bootstrap.sh` から
外しています)。

これだけでは不十分でした。実機では「ペアリングはできる (iPhone の設定画面に
一瞬 Connected と出る) が、直後に接続が切れる」という別の症状が残りました。
原因は Authorize service (プロファイル接続のたびに毎回聞かれる、ペアリング
確認とは別の許可要求) です。エージェントの capability (NoInputNoOutput) は
**ペアリング確認にしか効かず**、Authorize service には別途エージェントの
応答が要ります。`bluetoothctl` 内蔵のエージェントはこれを標準出力への
プロンプト + 標準入力からの読み取りで処理しますが、`sleep infinity` で
入力パイプを埋めているだけの旧版ではこの応答が一切返らず、A2DP などの
プロファイル接続要求がタイムアウトしてすぐ切断されていました。BlueZ は
**Trusted な端末にはこの Authorize service 自体を聞きません**。そのため
`scripts/sentinel-bt-agent.sh` は `bluetoothctl` を bash の `coproc` で
起動し (書き込み用の標準入力と読み取り用の標準出力を同じプロセスから
両方掴むため)、標準出力に流れてくるイベント行 (`Device <MAC> ...
Connected: yes` など) を監視して、端末が現れた瞬間に `trust <MAC>` を
打ち返します。起動時には既存のペアリング済み端末も一括で trust し直し、
この修正より前にペアリングして未信頼のまま固まっていた端末も次回起動で
救済します。**この coproc + 自動 trust の仕組みを外して `sleep infinity`
だけのパイプに戻さないでください** — 同じ「一瞬繋がってすぐ切れる」に
戻ります。

### 17. eco モードでのカメラ開き直しは `cam_opened_at` を巻き戻さない

eco/critical で誰も見ておらず、かつフレーム間隔が長いとき、`camera.py` の
`_worker()` は USB 帯域を空けるため毎サイクル `cap.release()` してから
次サイクルで開き直します (CLAUDE.md「USB 2.0 ハブを Ethernet と共有」)。
`motion_warmup_seconds` (#8 dietpi-setup PR で追加、オートフォーカス対策)
はカメラを開いた時刻 `cam_opened_at` からの経過秒数で判定しますが、この
意図した毎サイクルの開き直しのたびに `cam_opened_at` を現在時刻へ更新して
しまうと、eco モードに入った瞬間から二度と `warm` が false に戻らず、
**eco モードの動体検知が事実上永久に無効化**されます。診断ログ
(`motion_debug_log`) で実際に踏んだ不具合で、normal モードでは数秒で
`warm: false` になるのに、eco に切り替わった直後から `warm: true` が
何分経っても続いていました。

修正は、この「同じ解像度への意図した開き直し」だけ `cam_opened_at` の
更新をスキップする `skip_warmup_reset` フラグです。本当にカメラが壊れて
再接続した場合や、モード遷移で解像度が実際に変わった場合は今までどおり
`cam_opened_at` を更新し、正しくウォームアップが働きます。**この
`skip_warmup_reset` の判定を外して、毎回無条件に `cam_opened_at` を
更新するコードに戻さないでください** — 同じ「eco で動体検知が働かない」
不具合に戻ります。診断ログには `since_open` (直近オープンからの経過秒数)
と `viewers`/`reconnects` を追加してあります。次に同じ報告が来たら、まず
これで「毎サイクル 0 付近に戻り続けていないか」を確認してください。

これでもまだ不十分でした。`warm` は正しく false になるのに `ratio` が
**ずっと `null` のまま**で、結局 eco では一切動体を検知できていませんでした
(これも診断ログで発見)。原因は 2 つ、どちらも「eco は毎サイクル
`open_cam()` を呼び直す」ことと衝突していました。

1. `open_cam()` は `prev = None` (比較用の基準フレームを作り直す) も
   していました。`cam_opened_at` と全く同じ理由で、eco の毎サイクルの
   意図した開き直しのたびにこれが起きると、`prev` が一生 `None` のまま
   になり (`if not warm and prev is not None:` が常に false)、warm が
   解消したあとも `ratio` を一度も計算できません。`skip_warmup_reset` が
   立っているとき (=意図した同一解像度の開き直し) は `prev` もリセット
   しないよう、`cam_opened_at` と同じタイミングで更新するように直しました。
2. `open_cam()` はカメラが本当に読めるか確認するために内部で 1 フレーム
   `c.read()` していますが、これは検証用に**使い捨てて**いました。eco では
   毎サイクル `open_cam()` が呼ばれるため、この使い捨てが毎回起きると、
   ただでさえ疎な eco のフレームのうち実際に動体判定へ回るのは半分だけに
   なります (実機で確認: `open_cam()` 内の読み取りとループ本体の
   `cap.read()` で、同じサイクル中に 2 回読んでいた)。`open_cam()` が
   読んだこのフレームを `pending_frame` として持ち回り、直後のループ本体
   でそのまま動体判定に使う (捨てて読み直さない) ように直しました。

**この 2 つを合わせないと直りません** — `cam_opened_at` だけ直しても
`prev` が空のままなら `ratio` は一生計算されず、`prev` だけ直しても
半分のフレームが握りつぶされたままです。**`open_cam()` に `prev = None`
を書き戻したり、`pending_frame` を使わず毎回 `cap.read()` をやり直す
実装に戻したりしないでください** — 同じ「eco で `warm` は直るが
`ratio` が null のまま/検知が半分しか起きない」不具合に戻ります。
`sentinel/modules/camera.py` にスタブ cv2 を差し込んで `_worker()` を
直接スレッドで走らせるテストで、この一連の不具合と修正の両方を
実際に再現・確認済みです。

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
scripts/sentinel-adguard-8083.sh
                    AdGuard Home の :8083 への直接アクセスを一時的に
                    有効化 / 恒久的に無効化する (人が手動で実行する)
scripts/sentinel-set-governor.sh
                    CPU ガバナを切り替える。root しか書き込めないため
                    sudoers で個別に許可し、core/state.py が sudo 経由で
                    呼ぶ (直接書き込みでは権限エラーで無視される)
scripts/sentinel-set-hotspot-ssid.sh
                    WiFi ホットスポットの SSID を実行中に変更する。
                    /etc/hostapd/hostapd.conf は root しか書き込めないため
                    sudoers で個別に許可し、modules/hotspot.py が sudo 経由
                    で呼ぶ (sentinel-set-governor.sh と同じパターン)
scripts/sentinel-bt-agent.sh
                    sentinel-bt-agent.service から起動される、永続的な
                    ペアリングエージェント。bt-agent (bluez-tools) の
                    NoInputNoOutput リグレッションを避けるため bluetoothctl
                    を直接駆動する (CLAUDE.md #16)

core/config.py      設定の唯一の保管場所。型と範囲を強制する
core/state.py       モード状態機械。「今どのモードか」の唯一の決定者
core/supervisor.py  タスク監督。例外で落ちても指数バックオフで再起動する

modules/camera.py       カメラ (別プロセス)。動体検知 -> MODE.report_motion()
                         個別カメラの上書き設定は config の camera_overrides
                         (カメラID -> {設定キー: 値}) で持つ。camera.py の
                         effective_settings()/set_overrides() が唯一の窓口。
                         オートフォーカスの再合焦を動体と誤検知する機種向けに
                         cam_autofocus (無効化)・motion_area_max_ratio (画面
                         全体が一度に変化するケースを上限で除外)・
                         motion_warmup_seconds (開いた直後は判定を休止) を持つ
modules/music.py        mpg123 制御、位置復帰、yt-dlp キュー
modules/thermal.py      温度と CPU -> MODE.report_temperature()
modules/bluetooth.py    A2DP 接続検知 -> 音楽の退避と復帰。この Pi 自身の
                         表示名 (set_local_name、bluetoothctl system-alias)
                         と相手端末のエイリアス (set_alias、D-Bus 直叩き) は
                         別物なので混同しないこと
modules/hotspot.py       WiFi ホットスポット SSID の表示・変更
                         (sentinel-set-hotspot-ssid.sh を sudo 経由で呼ぶ)
modules/terminal.py     pty over WebSocket
modules/netlog.py       AdGuard querylog -> サービス名変換
modules/notify.py       Discord (レート制限対応キュー)。notify_motion_grouped
                         で「カメラごとに即時送信」と「複数カメラの検知を
                         1通にまとめる」を切り替えられる。動体が途切れず
                         続く間は再通知しない (motion_notify_reset_seconds
                         秒の無検知で「止まった」とみなし、次の検知だけが
                         新しいイベントとして通知される) — 以前は
                         notify_min_interval の周期だけで間引いていたため、
                         動体が続く限りその間隔で延々と通知が飛び続けていた
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
