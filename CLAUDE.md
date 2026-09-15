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

### 18. 「検知しました」と「検知が落ち着きました」は同じ秒数で開閉する

一時期、`notify.py` の `on_motion()` (即時の「検知しました」= 括弧の開始)
は専用の `motion_notify_reset_seconds` という秒数で、`summary_loop()`
(「検知が落ち着きました」= 括弧の終わり) は `notify_summary_after` という
**別の**秒数で、それぞれ独立に「無検知がどれだけ続いたら区切りとみなすか」
を判定していました。この 2 つの秒数がずれている限り (前者の既定値は 45〜90
秒、後者は 300 秒)、動体が両者の間の間隔で散発すると「終了」が 1 回も
届かないうちに「開始」だけ何度も届く、という報告どおりの不具合になります
— 開始のための静穏条件 (例: 45 秒) は動体が数分おきに散発するだけで
毎回満たされてしまうのに、終了のための静穏条件 (300 秒) はなかなか満たされ
ないためです。

修正は「開始と終了を同じ秒数 (`notify_summary_after`) で揃える」ことです。
`on_motion()` は `motion_notify_reset_seconds` を使わず `notify_summary_after`
をそのまま使うよう変更し、`motion_notify_reset_seconds` という設定項目
自体を削除しました (`core/config.py` の `DEFAULTS`/`_RANGES`、
`web/static/index.html` の `GROUPS`/`LABELS`/`MOTION_RECOMMENDED`、
`web/routes.py` の restart 対象除外リストから、すべて取り除いてあります)。
**開始・終了の判定に別々の秒数を持つ設計へ戻さないでください** — 秒数を
どちらか一方だけ変更できる設定が 2 つ存在する限り、運用中にずれて同じ
不具合に戻ります。「開始 1 回 → (その間の検知はまとめる) → 終了 1 回」
という対応関係を保証したいだけなら、境界の判定に使う秒数は 1 つで
足ります。

### 19. カメラの破損フレーム (単色ブロック/フレーム混在) は latest.jpg に書く前に捨てる

実機で「画面の一部が単色 (#009900 など) で埋まる」「1 枚の中に同じカメラが
複数合成されたように見える」という報告があり、しかも再起動しても直らない
という特徴がありました。原因は #1 の表にある「USB 2.0 ハブを Ethernet と
共有」という制約です — 帯域が逼迫すると UVC カメラの MJPEG 転送が完了
しないまま OpenCV に渡り、`cv2.VideoCapture.read()` は `ok=True` の
「正常な」フレームとしてこれを返してきます (デコードしきれなかった行を
単色や直前のフレームバッファの内容で埋めるだけで、エラーにはならない)。
`ok`/`frame is None` のチェックだけでは検出できず、このため
`latest.jpg`・動体判定・キャプチャ保存のすべてに破損フレームがそのまま
流れていました。再起動で直らないのは、原因が USB 帯域という物理的な
制約であり、プロセスを再起動しても帯域そのものは増えないためです。

`camera.py` の `_frame_corruption_ratio()` が、フレームを粗い横バンドに
分けて標準偏差を見ることでこれを検出します。一部のバンドだけが不自然に
平坦 (デコードされず単色/直前データで埋まった) で、かつ他のバンドは通常
どおり分散があるフレームだけを「破損」と判定し、そのフレームの公開
(`latest.jpg` への書き込み・動体判定・保存) を丸ごとスキップします。
画面全体が均一に暗い/白飛びしているだけの正常なシーン (全バンドが揃って
平坦) は、非平坦バンドとの分散比 (`_CORRUPT_CONTRAST_MULT`) を要求する
ことで誤検知しないようにしています。**この「一部だけ平坦」という条件を
外して「平坦なバンドがあれば即破損」に単純化しないでください** — 暗い
部屋や壁を映しているだけの正常なカメラまで毎フレーム破損扱いになり、
`latest.jpg` が一切更新されなくなります。

短時間に破損が連発する場合 (直近 20 フレーム中 10 フレーム以上が対象、
`_CORRUPT_RATE_THRESHOLD`)、UVC セッションが詰まっている可能性を考えて
一度だけ強制的に再接続します (`_CORRUPT_RECONNECT_COOLDOWN` 秒のクール
ダウン付き)。ただし帯域不足そのものが原因の場合はこれでも直らないため、
`corrupt_frames`/`last_corrupt` を `status.json` 経由で公開し、
どのカメラで慢性的に発生しているかを Web UI (カメラカードの「破損 N」
バッジ、直近 5 分以内なら赤色) から特定できるようにしています。
**この破損検出・カウンタ公開を削除して単純な `ok`/`frame is None` の
チェックだけに戻さないでください** — 同じ「見た目は直っていないのに
原因も分からない」不具合に戻ります。

これでもまだ不十分でした。実機から「再接続しても破損が直らず、破損
カウントが際限なく増え続ける」という報告がありました。真の USB 帯域
不足による破損は、転送が途切れる瞬間が USB バス上の他のトラフィック
(Ethernet など) とのタイミング次第で毎回変わるはずなので、位置・範囲が
フレームごとにばらつくのが自然です。それが「際限なく」「再接続しても」
起き続けるということは、実際には破損ではなく、**そのカメラが毎回まったく
同じ位置に出している絵** (レターボックス/ビネット (周辺減光)・オンスクリーン
表示の黒帯など) を誤検知していた可能性が高いと判断しました。

`_frame_corruption_ratio()` の戻り値に「どのバンドが平坦だったか」の
パターン (バンドごとの真偽値のタプル) を追加し、`_worker()` 側で追跡
するようにしました。同じパターンが `_CORRUPT_LEARN_STREAK` (既定 8) 回
連続したら、それは破損ではなくカメラ本来の絵だと学習し (`known_ok_patterns`
に追加)、以後そのパターンは二度と破損として扱いません。64x48 に正規化
してから判定しているため、解像度が変わってもバンド位置 (画面の上から
何割目か) の意味は変わらず、学習した内容はそのまま使えます。学習済みの
フレームは `corrupt_tolerated` としてカウントし (`corrupt_frames` とは
別)、「本当にまだ問題として扱っている件数」と「カメラの個性として許容
した件数」を区別できるようにしています。**この学習の仕組みを外して
「一致するパターンは無条件で何度でも破損扱いし続ける」実装に戻さないで
ください** — 同じ「際限なく増え続ける」不具合に戻ります。

あわせて、直っていない破損に対して 20 秒おきに無条件で再接続を繰り返す
のも見直しました。効果がないまま再接続を連発するのは USB をさらに
揺らすだけで無意味なので、再接続しても直らないたびに次のクールダウンを
倍々に伸ばし (`_CORRUPT_RECONNECT_MAX_BACKOFF` を上限に)、正常なフレーム
が 1 枚でも来た時点でクールダウンをリセットするようにしています。
**この指数バックオフを外して固定 20 秒間隔の再接続に戻さないでください**
— 直らない原因に対して延々と再接続を繰り返すだけの状態に戻ります。

### 20. 動体検知の「滞在時間」は分未満を秒で表示し、集計ウィンドウはカメラ単位で持つ

実機で「短期間に何度も 0 分の滞在として認識される」という報告があり、
2 つの独立した原因がありました。

1. `notify.py` の「検知期間」「静穏時間」は `f"{seconds/60:.0f} 分"` で
   常に分単位に丸めていたため、60 秒未満の検知はすべて「0 分」という
   同じ表示に潰れていました。数秒〜数十秒の本物の検知が、区別のつかない
   「0 分」として何度も届いていただけで、検知そのものが誤りだったとは
   限りません。`_fmt_minsec()` を追加し、60 秒未満は秒 (`"12秒"`)、それ
   以上は分 (+端数の秒) で表示するようにしました。**この関数を経由せず
   `{seconds/60:.0f} 分` に戻さないでください** — 同じ「0 分」ばかりの
   表示に戻ります。
2. より根本的な問題として、「検知期間」の集計ウィンドウ
   (`_window_open`/`_window_start`/`_last_motion_any`/`_window_counts`)
   が、カメラを問わずグローバルな 1 本しかありませんでした。対象
   ハードウェアの標準構成である USB カメラ **2 台**構成で
   `notify_motion_grouped=False` (カメラごとに即時通知) のとき、
   「検知しました」はカメラ単位で独立に届くのに、「検知が落ち着き
   ました」は 2 台分の検知回数・時間帯を合算した 1 通しか届かない、
   という開始・終了の単位の不一致がありました。この状態では、あるカメラ
   の一瞬の検知が別カメラの検知でウィンドウを延命させられて実際より長い
   「検知期間」になったり、逆に一方のカメラだけがずっと静かなのに
   もう一方の散発検知のたびにグローバルウィンドウの `_window_start` が
   固定されたままの状態で終了間際になり、結果として意味の薄い集計に
   なったりしていました。

   `on_motion()`/`summary_loop()` の双方を、`episode_key` (grouped=False
   ならカメラID、grouped=True なら `"__all__"`、開始判定の `_last_motion_seen`
   と同じキー) ごとに独立したウィンドウとして扱うよう書き換えました。
   これにより「検知しました」1 回 → (そのカメラ/まとめ単位の検知はまとめる)
   → 「検知が落ち着きました」1 回、という対応がカメラ単位でも保証されます。
   **`_window_*` をグローバルなスカラ変数に戻さないでください** — 2 台
   カメラ構成で開始・終了の単位が再びずれ、同じ「意味の薄い 0 分」報告に
   戻ります。

### 21. 動体判定はヒステリシスで「開始」「終了」を確定する (1 回の判定だけで即断しない)

#20 は通知側の表示・集計の不具合でしたが、実機からさらに「動体が無いのに
0 分の検知が大量に通知される (=誤検知)」「継続的に動体がいるのに検知が
ブツブツ途切れる」という報告があり、これは通知側ではなく検知そのものの
不具合でした。

`camera.py` の動体判定は、1 サイクル分のフレーム差分の面積比
(`ratio`) が `motion_area_ratio`〜`motion_area_max_ratio` の範囲に
入っているかどうかを見ているだけで、この 1 回の判定結果をそのまま
`motion_flag` の書き込み (= notify.py への通知トリガー) に使っていました。
1 回の判定は照明のちらつき・虫・圧縮ノイズなど一瞬の偶然でも簡単に閾値を
跨ぐため、これをそのまま公開すると「動体が無いのに何度も検知される」に
なります。逆に本物の動体が続いている最中でも、対象がわずかに静止した・
背景と同化したなどで 1 サイクルだけ ratio が閾値を割ることがあり、これを
そのまま公開すると「継続的に動体がいるのに検知が途切れて見える」になり
ます。どちらも「1 回の判定を、そのまま公開状態として扱っている」ことが
共通の原因です。

`core/state.py` の `ModeManager` が温度のヒステリシス (`temp_eco_c`/
`temp_recover_c`) でモードのバタつきを防いでいるのと同じ考え方を、動体
判定にも適用しました。`_worker()` は 1 回ごとの判定を `raw_hit` として
別に保持し、連続 `motion_confirm_checks` 回 `raw_hit` が続いて初めて
「動体開始」、連続 `motion_release_checks` 回 `raw_miss` (= 非 raw_hit)
が続いて初めて「動体終了」と確定します (`motion_confirmed` 変数)。
`motion_flag` に書く・notify.py に流れる「公開用の motion」は、常に
この確定後の状態であり、`raw_hit` そのものではありません。**この
ヒステリシスを外して `raw_hit` をそのまま公開状態として使う実装に
戻さないでください** — 同じ「誤検知の乱発」「検知のブツブツ途切れ」に
戻ります。

既定値は `motion_confirm_checks=2`・`motion_release_checks=3` です。
開始より終了を少し長めにして「粘る」方向に倒しているのは、動体が完全に
消えたと確信できるまで通知を送らない方が、途切れて何度も「開始」が届く
より実用上ましだと判断したためです。値はカメラごとに上書きできます
(`CAMERA_OVERRIDE_KEYS`) — 動きの速い被写体を扱うカメラでは
`motion_confirm_checks` を小さく、逆に誤検知が多いカメラでは大きく、
といった個別調整を想定しています。「推奨設定を適用」ボタン
(`MOTION_RECOMMENDED`) にもこの 2 つを含めています。

診断ログ (`motion_debug_log`) には `raw_hit`/`hit_streak`/`miss_streak`/
`confirm_n`/`release_n` を追加しました。「`motion` が動かないのは
`raw_hit` 自体が閾値に届いていないからか、それとも streak が
confirm_n/release_n に届く前に途切れているからか」をログだけで切り分け
られるようにするためです。次に「誤検知が多い」「検知が途切れる」と
報告されたら、まずこのログで `raw_hit` の実際のパターンを見てから
`motion_confirm_checks`/`motion_release_checks`/`motion_threshold` の
どれを調整すべきか判断してください。

### 22. カメラの破損が再接続でも解消しないときは、原因情報を残してから Pi ごと再起動する

#19 の破損フレーム対策は「再接続しても直らなければ、これ以上ソフト側で
できることはない」という前提で止めていました。しかしユーザーからは
「再接続を試みても対処できない場合は Pi を reboot してほしい、かつ原因を
解明できるようにしてほしい」という要望があり、実際 USB コントローラ自体が
詰まっている・ケーブルやハブの物理的な問題など、プロセスの再接続では
届かない原因はあり得ます。

`camera.py` の `_worker()` に `corrupt_unresolved_reconnects` というカウンタ
を追加しました。強制再接続 (#19) のたびに +1 し、直近 `_CORRUPT_HIST_LEN`
枚が丸ごと正常だった (=本当に解消した) ときだけ 0 に戻します。1 枚良い
フレームが来ただけでリセットする `corrupt_reconnect_backoff` より基準を
厳しくしているのは、破損と正常が入り混じるカメラで「解消した」と誤判定
してエスカレーションが一生起こらなくなるのを避けるためです。このカウンタ
が `_CORRUPT_REBOOT_THRESHOLD` (既定 4 回) に達すると、`corrupt_reboot_request`
ファイルに **device・corrupt_frames・corrupt_tolerated・reconnects・
unresolved_reconnects** を添えて書き出し、`log.error()` も出します
(自動的に `/api/errors` と診断バンドルに載る、CLAUDE.md「エラー収集の
仕組み」参照)。**この情報を削らないでください** — 「原因を解明できる
ように」という要望の核心で、機体が再起動されたあとでもこれが唯一の
手がかりになります。

実際に Pi を再起動する処理は `camera.py` からは行いません。カメラの
ワーカーはあくまで「別プロセス」であり、複数カメラが同時に閾値へ達した
ときに二重に `sudo reboot` を呼んでしまう競合を避けたいため、
`camera.py` は `ON_CORRUPT_REBOOT` フック (`ON_MOTION` と同じパターン) 経由で
要求を通知するだけに留め、`main.py` の `camera.loop()` 呼び出し元が
`maintenance.emergency_reboot()` へ委譲します。`emergency_reboot()` は
日次の `run_now()` と同じ「音楽・カメラを止めてから `_reboot()`」という
手順・sudo フォールバックをそのまま再利用します (タイムラプス生成だけは
省略 — 異常系なので原因究明を優先し、時間のかかる処理を挟まない)。
**この委譲をやめて `camera.py` (=ワーカーが直接触れる側) から直接
`sudo reboot` を呼ぶ実装や、`_reboot()` の sudo フォールバックを重複して
書く実装に戻さないでください** — 複数カメラの二重再起動や、既存の
再起動手順とのズレに戻ります。

`camera.py` の `loop()` 側にも `last_reboot_attempt` による 10 分の
クールダウンを設けています。複数カメラがほぼ同時に要求しても 1 回しか
再起動しないための重複排除であると同時に、万一 `sudo reboot` が実際には
実行されなかった場合 (sudoers の設定漏れなど) に**永久に諦めたままには
ならない**ようにするためのものでもあります — クールダウンが明けたあとに
まだ破損が続いていれば、`corrupt_unresolved_reconnects` は伸び続けている
ので自然に再度要求されます。**このクールダウンを外して「一度要求したら
二度と要求しない」実装にしないでください** — 最初の再起動要求が何らかの
理由で失敗した場合に復旧手段が無くなります。

### 23. `FileResponse` を、他プロセスが継続的に上書きするファイルに使わない

実機のログに `ERROR uvicorn.error Exception in ASGI application` →
`RuntimeError: Response content shorter than Content-Length` /
`RuntimeError: Response content longer than Content-Length` が、1 時間で
数十回という頻度で記録されていました。再起動しても直らず、カメラの
ライブ映像を開いている間に集中して発生していました。

原因は `GET /api/camera/{cid}/snapshot` (`web/routes.py`) が
`FileResponse(camera.rt(cid) / "latest.jpg", ...)` を返していたことです。
Starlette の `FileResponse.__call__()` は (1) `os.stat(path)` でファイル
サイズを取得して `Content-Length` ヘッダを確定させ、(2) そのあと**同じ
パス名を改めて `open()` し直して**本文を送信する、という 2 段階の処理に
なっています。`latest.jpg` はカメラワーカーが毎フレーム
`tmp.write_bytes(...)` → `os.replace(tmp, "latest.jpg")` で原子的に
差し替え続けているファイルなので (CLAUDE.md「高頻度の書き込みは
`/dev/shm` にだけ行う」)、(1) の `stat` と (2) の `open` の間にこの
差し替えが割り込むと、`Content-Length` に書いた古い版のサイズと、実際に
`open` し直して送る新しい版のサイズがずれます。uvicorn は ASGI アプリが
宣言した `Content-Length` と実際に送られたバイト数を突き合わせて検証して
おり、ここが一致しないと丸ごと例外で落ちます。ライブ映像を見ている間は
このスナップショット取得が頻繁に呼ばれるため、発生頻度が上がっていたのも
筋が通ります。

同じファイルを MJPEG ストリーム (`_mjpeg()`、`/api/camera/{cid}/stream`)
としても配信していますが、こちらは `p.read_bytes()` で 1 回だけ丸ごと
読み、その戻り値の `len()` から `Content-Length` を計算しているため、
`os.replace()` の原子性 (読み取り側は古い版・新しい版のどちらかを必ず
完全な形で読める) の恩恵をそのまま受けられておりレースが起きません。

`snapshot()` もこれに合わせ、`FileResponse` (stat とオープンが別ステップ)
をやめて `p.read_bytes()` で 1 回読んだ内容を `Response(content=data,
media_type="image/jpeg", ...)` で返すよう変更しました。`Response` の
`Content-Length` は渡した `content` の実際のバイト長から計算されるため、
読んだ内容と送る内容が常に一致します。**この `snapshot()` を
`FileResponse` に戻さないでください** — 同じ `Content-Length` 不一致の
例外に戻ります。他のエンドポイントの `FileResponse` (`/api/capture/...`
の保存済みキャプチャ、タイムラプス動画、`index.html` など) は他プロセス
から継続的に上書きされるファイルではないため対象外です — 今後
`FileResponse` を新しいエンドポイントに使うときは、配信対象のファイルが
別プロセス/別スレッドから頻繁に置き換えられるものでないか、都度確認して
ください。継続的に上書きされるファイルには `read_bytes()` 一発読みの
`Response` パターンを使ってください。

### 24. `sentinel-adguard-8083.sh enable` はファイアウォールだけでなく AdGuard 自身の bind アドレスも変える

「`enable` を実行してもアクセスできない」という報告がありました。原因は
#5 で書いた二重の到達性対策のうち、片方 (iptables) しか `enable` が
動かしていなかったことです。`sentinel-guardian.sh` の
`check_adguard_bind()` は `AdGuardHome.yaml` の `http.address` を常に
`127.0.0.1:8083` に固定し、ズレていれば 2 分ごとに戻して `adguardhome`
を再起動します。`sentinel-adguard-8083.sh enable` は iptables の DROP
ルールを外すだけで、この bind アドレスには一切触れていませんでした。
つまり `enable` を実行しても AdGuard Home 自身は相変わらずループバック
以外のどのアドレスでも一切 listen していないため、ファイアウォールを
開けても「そもそも誰も listen していないポートに繋ごうとする」状態で、
接続を拒否される (または到達すらしない) だけでした。

修正は 2 箇所です。

1. `sentinel-guardian.sh` に `agh_unblock_active()` を追加し、
   `check_adguard_bind()`/`check_adguard_listen()`/`check_firewall()`
   の 3 つが同じ `$UNBLOCK_FILE` (`sentinel-adguard-8083.sh` が書く
   Unix タイムスタンプ) を見るようにしました。unblock 有効時は
   `check_adguard_bind()` が `127.0.0.1` ではなく `0.0.0.0:8083` を
   強制し、`check_adguard_listen()` は「ループバック以外で listen して
   いる」という本来の異常検知を一時的にスキップします (unblock 中は
   それが意図した状態のため)。**この `agh_unblock_active()` の参照を
   外し、`check_adguard_bind()` が unblock 中かどうかに関係なく常に
   `127.0.0.1` を強制する実装に戻さないでください** — Guardian の
   2 分ごとの周期がすぐにまた bind を締め直し、同じ「ファイアウォールは
   開いているのに繋がらない」に戻ります。
2. `sentinel-adguard-8083.sh` 自身にも `check_adguard_bind()` と同じ
   awk ロジックを持つ `rebind_agh()` を追加し、`enable` は
   `0.0.0.0:8083`、`disable` は `127.0.0.1:8083` への rebind を
   `adguardhome` の再起動込みで即座に行うようにしました。「次の
   Guardian 周期 (最大 2 分) まで待たずにこのスクリプト自身がすぐ直す」
   という既存のコメント (`enable`/`disable` 両方) は元々 iptables に
   ついてのものでしたが、bind アドレスについても同じことが当てはまる
   ため揃えています。**この `rebind_agh()` 呼び出しを外して iptables
   だけを操作する実装に戻さないでください** — Guardian の次の周期を
   待つだけならまだいいですが、`AGH_YAML` の検出に失敗した場合など
   Guardian 側が直せないケースでは無期限に到達不能なままになります。

`status` にも `ss` で実際に listen しているアドレスを表示するように
しました。「iptables は正しいのに繋がらない」のか「そもそも AdGuard が
listen していない」のかを、設定ファイルの中身を見なくてもこのコマンド
だけで切り分けられるようにするためです。

### 25. `vcgencmd get_throttled` の常時監視は削除した (エラーにも診断にも出さない)

`thermal.py` はかつて 30 秒ごとに `vcgencmd get_throttled` を呼び、
ビットの変化をログしていました (低電圧のみ `log.warning()`、それ以外は
`log.info()`)。実機では USB 2.0 ハブと Ethernet を共有する構成上
(CLAUDE.md 冒頭の制約表)、"ARM周波数を制限中" 系のビットがほぼ常時
立ちっぱなしになり、状態変化のたびにログが出るだけで実用上の意味を
持たなくなっていました。無駄な `subprocess.check_output()` 呼び出しと
`asyncio.to_thread()` 経由のスレッド消費も、RAM 1GB・SWAP なしの機体では
チリも積もれば無視できません。

`read_throttled()`・`_THROTTLE_BITS`・`loop()` 内の 30 秒ごとの
スロットリング監視、および `snapshot()` の `throttled` フィールド、
Web UI の「スロットリング」カードをすべて削除しました。低電圧検出も
含めてまるごと削除です — 低電圧は本来なら電源側の実害を示す重要な
シグナルですが、実機のログで常時ノイズと化している以上、どのビットだけ
残すかを選別しても同じ「意味を成さない監視」に戻るだけと判断しました。
**この監視を `snapshot()`/`loop()`/UI に復活させないでください** —
同じ「常に何かが立っていて役に立たない」状態に戻ります。電源トラブルを
実際に切り分けたいときは `vcgencmd get_throttled` を手動で叩けば従来
どおり確認できます (「動作確認の手順」参照)。

### 26. 更新の自動適用は `git fetch` による定期チェック + `update.sh` 呼び出しで行う (常時ポーリングのデーモンにしない)

以前は `main` へのマージを実機へ反映するのに、誰かが手動で SSH して
`sudo ./update.sh` を実行するしかありませんでした。これを
`scripts/sentinel-autoupdate.sh` + `sentinel-autoupdate.timer`
(30 分ごと) で自動化しています。

設計上の要点:

- `update.sh` 自体が `git pull` を行う都合上、実際の git clone の場所を
  知る必要があります。しかし `install.sh` がアプリ本体をコピーする先
  (`$APP_DIR` = `/opt/sentinel`、`sentinel-autoupdate.sh` 自身もここに
  デプロイされる) は git clone そのものではありません。そこで
  `install.sh`/`update.sh` の両方が、パス解決した直後の `$SRC` を
  `/var/lib/sentinel/repo-path` に書き出すようにしました。
  `sentinel-autoupdate.sh` はこのファイルを読むだけで、自分では一切
  パスを推測しません。**このファイルへの書き出しを省略しないでください**
  — `sentinel-autoupdate.sh` が「どこを更新すればいいか分からず何も
  しない」状態に戻ります。
- 常時起動のデーモンではなく、Guardian と同じ「systemd タイマーで定期的に
  1 回だけ実行し、何もなければ即終了する」パターンにしています。
  `git fetch` はネットワーク越しの軽い操作なので 30 分間隔でも負荷は
  ほぼ無視できますが、これを prunning のない `while true` ループの常駐
  プロセスにする必要はありません (CLAUDE.md「依存を増やさない」/RAM 1GB
  制約と同じ考え方)。
- ローカルに未コミットの変更がある場合は何もせず終了します。
  `update.sh` 自身の `git pull --ff-only` は untracked/変更のある
  ワークツリーに対して失敗するだけなので、そのまま自動実行すると毎周期
  同じ失敗ログが出続けるだけになります。人が `git status`/`git stash`
  で解決するまで静かに待ちます (`update.sh` 自身の案内文言と同じ方針)。
- 実際に新しいコミットがあるとき以外は `git fetch` して比較するだけで
  終わり、`update.sh` (= `bootstrap.sh` + `install.sh` のフルサイクル、
  `apt-get update` を含む) は呼びません。**この「fetch して比較 ->
  差分がある時だけ `update.sh` を呼ぶ」の順序を外して毎周期無条件に
  `update.sh` を呼ぶ実装にしないでください** — 30 分ごとに無駄な
  `apt-get update` が走り続けるだけで、RAM 1GB・SD カードという制約に
  逆行します。
- `git config --global --add safe.directory` を明示的に呼んでいます。
  このスクリプトは root で無人実行されるため、git clone の所有者が
  別ユーザー (通常 `setup.sh` を実行した対話ユーザー) だと git の
  「dubious ownership」ガードに引っかかり、エラーも出さないまま永久に
  何もしなくなる可能性があります。**この行を外さないでください** —
  信頼するパスは `/var/lib/sentinel/repo-path` 由来 (自分自身が書いた
  ものだけ) に限定しているため、`safe.directory` を無条件に許可しても
  外部から任意のパスを注入される経路にはなりません。

### 27. 音声アナウンス (`modules/voice.py`) は mpg123 と別経路、ALSA numid=1 で音量を持つ

時報・エラー通知・カメラ再起動通知・その他システムイベントを喋る機能を
追加しました。設計上の判断は #2/#15 で確立済みのパターンをそのまま踏襲
しています。

- **TTS エンジンは espeak-ng。** Open JTalk のような高品質な日本語 TTS は
  辞書だけで数十 MB あり、RAM 1GB・SD カードという制約 (「依存を増やさ
  ない」) に見合いません。espeak-ng は数 MB で完結する代わりに発音は
  機械的で、辞書を持たないため漢字を読み違えることがあります。**この
  トレードオフを承知の上での選択です** — 発音品質を優先して Open JTalk
  などへ差し替える場合は、SD カード容量とインストール時間への影響を
  必ず確認してください。
- **音楽ライブラリ (mpg123 固定、CLAUDE.md #2) とは完全に別経路です。**
  espeak-ng は ALSA へ直接書き込むだけで、mpg123 のキュー・ライブラリ
  管理には一切触れません。#2 の「mpg123 固定」はあくまで音楽ライブラリの
  再生エンジンについての制約であり、この単発の短い音声アナウンスとは
  別物です。
- **鳴らす前に必ず曲を一時停止します。** bcm2835 の ALSA 出力は dmix
  なしでは同時に 1 ストリームしか受け付けないため、`music.py` に
  `bluetooth.py` の `suspend_for_bluetooth()`/`resume_from_bluetooth()`
  と全く同じパターンで `duck_for_voice()`/`resume_from_voice()` を追加し、
  `reason="voice"` という別タグで使っています。**この `reason` タグを
  `"bluetooth"` と共有させないでください** — Bluetooth 接続中の一時停止
  ("bluetooth") を voice 側が誤って再開してしまう (またはその逆) と、
  どちらの機能が音楽を止めているのか分からなくなります。
- **Bluetooth 接続中はアナウンス自体をスキップします。** `bluealsa-aplay`
  が同じ ALSA デバイスを排他的に使っているため、割り込むと双方の音声が
  壊れるだけです。ここは「ducking して待つ」のではなく完全にスキップする
  という判断です — 音楽と違って音声アナウンスは待たせても情報が古く
  なるだけで実害が小さいため、複雑な調停を作るより単純に諦める方を選んで
  います。
- **音量は ALSA numid=1 を直接操作します。** mpg123 はソフトウェアゲイン
  (`music_volume`) を持つため ALSA ミキサーを一切操作しませんが、
  espeak-ng はそれをバイパスするので、Bluetooth 再生 (#15) と全く同じ
  理由でハードウェア側の音量調整が必要です。**numid=3 (`sentinel-
  guardian.sh` の `check_audio()` が使う出力ルート選択) とは別の
  コントロールです** — #15 で bluetooth.py がすでに踏んだのと同じ
  混同をしないでください。
- **カテゴリごとに独立したオン/オフを持ちます** (`voice_time_enabled`/
  `voice_error_enabled`/`voice_camera_reboot_enabled`/`voice_other_enabled`
  + 総元栓 `voice_enabled`)。時報だけ聞きたい、エラーだけ知りたい、と
  いった個別の使い方を想定しているため、#18/#20 の「開始・終了は同じ
  秒数で揃える」とは逆に、ここはあえて対応関係を持たない独立スイッチに
  しています。
- **時報は壁時計の分境界に揃えて判定します** (`(hour*60+minute) //
  interval` が変化した瞬間だけ喋る)。単純に「前回から N 分経過したか」
  というタイマーにすると、サービス再起動のたびに基準時刻がずれ、
  「n 分ごと」のはずが実際の時計の分とは無関係な半端な時刻に鳴り続ける
  ことになります。壁時計に揃えることで、サービスが何度再起動されても
  常に同じ (例: 毎時 0 分・30 分) タイミングで鳴るようにしています。

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
scripts/sentinel-autoupdate.sh
                    sentinel-autoupdate.timer (30 分ごと) から起動される。
                    git clone の場所を install.sh/update.sh が書き出す
                    /var/lib/sentinel/repo-path から読み、git fetch して
                    リモートに新しいコミットがあれば update.sh を自動で
                    実行する (CLAUDE.md #26)

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
                         motion_warmup_seconds (開いた直後は判定を休止) を持つ。
                         USB 帯域不足による破損フレーム (単色ブロック化/フレーム
                         混在) は _frame_corruption_ratio() で検出し、latest.jpg
                         への公開・動体判定・保存の前に捨てる (CLAUDE.md #19)。
                         動体判定自体もヒステリシスを持つ — motion_confirm_checks
                         回連続で閾値超えが続いて初めて「開始」、
                         motion_release_checks 回連続で閾値割れが続いて初めて
                         「終了」とする (1 回の判定をそのまま公開しない、
                         CLAUDE.md #21)。破損が強制再接続 (#19) を
                         _CORRUPT_REBOOT_THRESHOLD 回繰り返しても解消しない
                         場合は corrupt_reboot_request を書き、
                         ON_CORRUPT_REBOOT フック経由で Pi 再起動を要求する
                         (実際の再起動は maintenance.emergency_reboot() に
                         委譲、CLAUDE.md #22)
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
                         1通にまとめる」を切り替えられる。「検知しました」
                         (開始) と「検知が落ち着きました」(終了、
                         summary_loop() が送る) は notify_summary_after
                         という同じ秒数で開閉する 1 組の括弧 — 開始・終了を
                         別の秒数で判定しないこと (CLAUDE.md #18)。この括弧
                         と集計ウィンドウ (_window_*) は episode_key
                         (grouped=False ならカメラID、grouped=True なら
                         "__all__") ごとに独立している — グローバルな
                         1 本に戻さないこと (CLAUDE.md #20)。検知期間/
                         静穏時間は _fmt_minsec() で 60 秒未満は秒表示
                         (CLAUDE.md #20)
modules/maintenance.py  4 時の定時処理と再起動。emergency_reboot() は
                         camera.py の破損検知エスカレーション専用の緊急
                         再起動 (タイムラプス生成は省略、CLAUDE.md #22)
modules/voice.py        espeak-ng 経由の音声アナウンス (時報・エラー・
                         カメラ再起動・その他システムイベント)。mpg123 の
                         音楽ライブラリとは別経路、鳴らす前に music.py の
                         duck_for_voice()/resume_from_voice() で曲を一時
                         停止し、numid=1 で音量を持つ (CLAUDE.md #27)

web/routes.py           全 HTTP / WebSocket エンドポイント。latest.jpg の
                         ように他プロセスが継続的に上書きするファイルは
                         FileResponse (stat とオープンが別ステップ) では
                         配信せず、read_bytes() で 1 回読んで Response に
                         渡す (CLAUDE.md #23)
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
