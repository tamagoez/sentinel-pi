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

### 27. 音声アナウンス (`modules/voice.py`) は mpg123 と別経路で鳴らす (音量制御は #31 で見直し)

時報・エラー通知・カメラ再起動通知・その他システムイベントを喋る機能を
追加しました。設計上の判断は #2/#15 で確立済みのパターンをそのまま踏襲
しています。

- **TTS エンジンは Open JTalk を優先し、espeak-ng へ自動フォールバックする。**
  当初は espeak-ng だけでしたが、実機で「発音が機械的すぎて聞き取れない」
  という報告があり切り替えました (#28 参照)。espeak-ng は形態素解析辞書を
  持たず漢字を読み違えることが多かったのに対し、Open JTalk は MeCab 由来
  の辞書 (naist-jdic) で形態素解析してから音声合成するため、同じ文でも
  読み違えが大幅に減ります。辞書 + 音声モデルで数十 MB 追加が必要になり
  ますが、VOICEVOX のような数百 MB 級のエンジンに比べれば RAM 1GB・SD
  カードという制約 (「依存を増やさない」) の範囲内という判断です。
  **Open JTalk のパッケージ (`open-jtalk`/`open-jtalk-mecab-naist-jdic`/
  `hts-voice-nitech-jp-atr503-m001`) が見つからない環境では espeak-ng へ
  自動フォールバックし、無音にはしません** — `voice.py` の
  `_has_open_jtalk()` が辞書・音声モデルの実在を都度確認し、無ければ
  `_speak_espeak()` にそのまま落とします。**このフォールバックを外して
  Open JTalk 固定にしないでください** — bootstrap.sh の再実行を忘れて
  いるだけの環境でアナウンス機能ごと沈黙してしまいます。
- **音楽ライブラリ (mpg123 固定、CLAUDE.md #2) とは完全に別経路です。**
  Open JTalk/espeak-ng はどちらも ALSA へ直接書き込むだけで、mpg123 の
  キュー・ライブラリ管理には一切触れません。#2 の「mpg123 固定」は
  あくまで音楽ライブラリの再生エンジンについての制約であり、この単発の
  短い音声アナウンスとは別物です。
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
  Open JTalk/espeak-ng はどちらもそれをバイパスするので、Bluetooth 再生
  (#15) と全く同じ理由でハードウェア側の音量調整が必要です。**numid=3
  (`sentinel-guardian.sh` の `check_audio()` が使う出力ルート選択) とは
  別のコントロールです** — #15 で bluetooth.py がすでに踏んだのと同じ
  混同をしないでください。
- **カテゴリごとに独立したオン/オフ + 読み上げ文のテンプレートを持ちます**
  (`voice_time_enabled`/`voice_error_enabled`/`voice_camera_reboot_enabled`/
  `voice_other_enabled` + 総元栓 `voice_enabled`、対になる
  `voice_time_text`/`voice_error_text`/`voice_camera_reboot_text`/
  `voice_other_text`)。時報だけ聞きたい、エラーだけ知りたい、といった
  使い方を分けられるようにするため、#18/#20 の「開始・終了は同じ秒数で
  揃える」とは逆に、ここはあえて対応関係を持たない独立スイッチにして
  います。テンプレートは notify.py の `notify_motion_title` などと同じ
  `_fmt()` 方式 (知らないプレースホルダや壊れた書式は既定文へ静かに
  戻す) — time カテゴリは `{hour}`/`{minute}`、それ以外は呼び出し元が
  渡す元のメッセージ文字列がそのまま入る `{message}` を使えます。
- **時報は壁時計の分境界に揃えて判定します** (`(hour*60+minute) //
  interval` が変化した瞬間だけ喋る)。単純に「前回から N 分経過したか」
  というタイマーにすると、サービス再起動のたびに基準時刻がずれ、
  「n 分ごと」のはずが実際の時計の分とは無関係な半端な時刻に鳴り続ける
  ことになります。壁時計に揃えることで、サービスが何度再起動されても
  常に同じ (例: 毎時 0 分・30 分) タイミングで鳴るようにしています。

### 28. カメラ破損検知は横バンドではなく 2 次元グリッドで判定する (向きによる検出漏れ対策)

#19 の破損フレーム検出は画面を横バンド (行) に分け、「連続する行がまとまって
平坦かどうか」だけを見ていました。実機から「約 70% の面積が単色 (#009900)
で埋まっているのに、破損検知がたまにすり抜ける」という報告があり、調べると
これは検出アルゴリズムの盲点でした。横バンドだけで判定していると、破損が
画面の左右どちらかに偏る場合 (縦方向の帯として出る場合)、各行の中に破損
部分 (平坦) と正常部分 (分散あり) が両方含まれることになり、行全体の標準
偏差は正常部分に引きずられて「平坦」と判定されません — つまり破損の向きに
よって検出できたりできなかったりしていました。

`camera.py` の `_frame_corruption_ratio()` を、横バンドではなく 8x6 の
2 次元グリッドで判定するよう書き換えました。セルごとに独立して標準偏差を
見て、平坦なセルの面積比をそのまま「破損の疑いがあるか」の指標にすることで、
破損が上下・左右・中央のどこに出ても面積ベースで直接検出できます。
標準偏差の閾値・非平坦セルとのコントラスト比 (誤検知防止) は #19 から
そのまま引き継いでおり、判定の厳しさ自体は変えていません。純粋なロジック
部分 (numpy 非依存) を抜き出し、縦方向の帯状破損を人工的に作ったテストで
「旧アルゴリズムは検出漏れ・新アルゴリズムは検出できる」ことを確認済みです。
**この 2 次元グリッド判定をやめて横バンドだけの判定に戻さないでください**
— 同じ「破損の向きによっては検知をすり抜ける」不具合に戻ります。

あわせて、判定の閾値 (`corrupt_min_area_ratio`、既定 0.12) と緊急再起動
までの再接続回数 (`corrupt_reboot_threshold`、既定 4、#22) を、他のカメラ
設定と同じく `CAMERA_OVERRIDE_KEYS` 経由でカメラごとに上書き可能な設定
にしました。カメラによって USB 帯域の逼迫具合や許容できる誤検知率が違う
ため、以前はコード内の固定定数だった感度をユーザー側で調整できるように
しています。

### 29. `asyncio.create_task()` の戻り値は必ず変数へ保持する (イベントループは弱参照しか持たない)

「カメラの破損検知による緊急再起動 (#22) が動いていない」という報告が
ありました。原因は `main.py` の `_on_corrupt_reboot()` が
`asyncio.create_task(maintenance.emergency_reboot(cid, reason))` の戻り値
(`Task` オブジェクト) をどこにも保持せずに捨てていたことです。Python の
公式ドキュメントが明記している既知の落とし穴で、イベントループはスケジュール
した `Task` を**弱参照**でしか保持しないため、どこにも強参照が残っていない
と、ガベージコレクタがタスクを実行途中で回収してしまうことがあります。
`emergency_reboot()` は「通知が飛ぶのを待つ」ための `await asyncio.sleep(4)`
など複数の待機を挟むため、GC に回収される猶予が十分にあり、実機では
「破損検知のログは出るのに Pi が実際には再起動されない」という、ちょうど
報告どおりの不具合になっていました。ログにエラーは出ないため、コード
レビューだけでは気付きにくいタイプの不具合です。

`main.py` に module レベルの `_background_tasks: set[asyncio.Task] = set()`
を追加し、`create_task()` の戻り値をこの集合に保持した上で
`task.add_done_callback(_background_tasks.discard)` で完了時に自動的に
取り除くようにしました (Python 公式ドキュメントが推奨する定石のパターン)。
**この参照保持を外して `create_task()` の戻り値を再び捨てる実装に戻さない
でください** — 同じ「要求だけ出て実際には再起動されない」不具合に戻ります。
今後 `camera.loop()`/`main.py` のような「同期フックから `create_task()` で
バックグラウンドタスクを起動する」パターンを新しく書くときは、必ずこの
`_background_tasks` と同じ「集合に足して done コールバックで外す」形にして
ください。

### 30. 設定項目の追加を忘れると「機能はあるが誰も操作できない」状態になる

音声アナウンス・自動更新のように、実装当初は動作確認や既定値だけで済ませて
Web UI 設定タブへの反映を後回しにした機能がありました。ユーザーから
「これまで頼んできた内容の設定が全て実装されているか確認してほしい」という
指摘があり、実際に `core/config.py` の `DEFAULTS` と `web/static/index.html`
の `GROUPS`/`CAM_SETTING_LABELS` を突き合わせたところ、以下の抜けが
見つかりました。

- **自動更新 (#26) を無効化する設定が存在しなかった。** `sentinel-
  autoupdate.timer` は常に有効という前提で作っており、ユーザーが Web UI
  から止める手段が一切ありませんでした。`system_autoupdate_enabled`
  (既定 True) を追加し、`scripts/sentinel-autoupdate.sh` が `config.json`
  を直接 (python3 経由で) 読んで確認するようにしました。この bash
  スクリプトは Python 側の `core/config.py` の path 解決ロジックに
  アクセスできないため、`sentinel-guardian.service` と同じ
  `Environment=SENTINEL_DATA=` の慣習を `sentinel-autoupdate.service` にも
  追加し (`install.sh` が実際のストレージパスへ sed で書き換える)、
  そこから `config.json` の場所を特定しています。
- **カメラ破損検知のしきい値 (#19/#22) が設定不可能な固定定数だった。**
  #28/この節の直前で `corrupt_min_area_ratio`/`corrupt_reboot_threshold`
  として設定化しました。
- **音声アナウンスの読み上げ文がカスタマイズできなかった。** #27 で
  `voice_*_text` テンプレートとして追加しました。

**新しい機能を追加するときは、`core/config.py` の `DEFAULTS` に既定値を
足すだけで終わらせず、必ず `web/static/index.html` の `GROUPS`/`LABELS`
(カメラ単位で上書きできる設定なら `CAM_SETTING_LABELS` も) まで一括りで
終わらせてください** — 「動く設定はあるが、UI から見えない/変えられない」
状態は、機能が存在しないのとユーザーから見て区別がつきません。この監査は
`DEFAULTS` のキー集合と `GROUPS`/`CAM_SETTING_LABELS` のキー集合を突き
合わせるだけの単純なスクリプトで機械的にできるので、次に似た指摘が来たら
まずこれを流してから個別の機能を疑ってください。

### 31. 音楽と音声アナウンスは ALSA の dmix で重ねて鳴らし、音量は完全に別系統にする

#27 で音声アナウンス機能を追加した当初は、bcm2835 の出力が dmix なしでは
同時に 1 ストリームしか受け付けないという制約に対し、「曲を完全に止めて
から喋る」(`music.duck_for_voice()`/`resume_from_voice()`) という単純な
排他制御で対応していました。実機からさらに 2 つの報告があり、この設計を
見直しました。

1. 「音声アナウンスと音楽の音量がたまに独立せず、音楽の音量が大きく
   なってしまったりする」。原因は `voice.py` の `_apply_volume()` が
   `amixer -c <card> cset numid=1 <%>` でハードウェアのアンプそのもの
   (bcm2835 の "PCM Playback Volume") を直接書き換えていたことです。
   numid=1 は Bluetooth 再生 (#15) とこの音声アナウンス機能の両方が
   共有する**たった 1 つの**ハードウェアレジスタなので、たとえ ducking
   で曲を一時停止していても、声を鳴らすたびにこの共有レジスタが書き
   換わり、次に曲を再開したときの実際の聞こえ方 (ハードウェア段の
   増幅率) が声の音量設定に引きずられてズレていました。
2. 「同時に重ねられるようにしてほしい」。ducking である以上、原理的に
   同時再生はできません。

`scripts/sentinel-setup-audio-mixing.sh` (新設、root 権限が要るため
sudoers 経由、sentinel-set-governor.sh などと同じ「引数はスクリプト
自身が検証する」パターン) が `/etc/asound.conf` に以下を書きます。

```
pcm.sentinel_dmix   dmix (実体のハードウェアミキシング段)
pcm.sentinel_music  音楽 (mpg123 -a sentinel_music) の入口。素通し、または
                    (イコライザー有効時) LADSPA 段を挟んでから dmix へ
pcm.sentinel_voice  音声 (aplay -D sentinel_voice) の入口。専用の ALSA
                    softvol コントロール "SentinelVoice" を持ち、dmix へ
```

`voice.py` の音量操作は numid=1 をやめ、`sentinel_voice` PCM 自身の
softvol コントロール ("SentinelVoice") だけを操作するようにしました。
これはストリームごとに独立したソフトウェアゲインなので、声の音量を
いくら変えても numid=1 (ハードウェア段、音楽・Bluetooth と共有) は
一切変わりません。**この "SentinelVoice" softvol を経由せず、再び
numid=1 を直接操作する実装に戻さないでください** — 同じ「音声を鳴らす
たびに音楽の音量が変わって聞こえる」不具合に戻ります。mpg123 側の
音量 (`music_volume`) はこれまでどおり mpg123 自身のソフトウェアゲイン
(V コマンド) のままで、ALSA ミキサーには一切触れません — 変える理由が
ないので変えていません。

`music.py`/`voice.py` はどちらも、この named PCM が実際に用意できて
いるか (`aplay -L` に `sentinel_music`/`sentinel_voice` があるか) を
都度確認し、無ければ (LADSPA プラグイン欠如以外の理由でセットアップ
スクリプトが失敗していた場合など) 従来の ducking 経路へ自動で
フォールバックします。**この段階的劣化を外して「named PCM が無ければ
何もしない (無音)」にしないでください** — dmix セットアップが何らかの
理由で失敗した機体で、音楽・音声アナウンスの両方が一切鳴らなくなります。

`sentinel-setup-audio-mixing.sh` 自身も、書き込み前の `/etc/asound.conf`
をバックアップしておき、新しい設定が実際の再生テスト (`aplay -D
sentinel_music`/`sentinel_voice` を実際に鳴らしてみる、
`sentinel-fix-storage-owner.sh` の `can_write()` と同じ「実際に試す」
哲学、CLAUDE.md #8) に失敗したら元の内容へ戻します。**このバックアップ・
復元と実際の再生テストを外さないでください** — カード番号を誤って
渡した、LADSPA プラグインが実は入っていなかった、といったケースで
「前は (dmix なしで) 鳴っていた音が、この機能のせいで一切鳴らなくなる」
という今回より悪い regression になります。

### 32. イコライザーは ALSA の LADSPA プラグイン (mbeq) を使い、設定変更は曲の切れ目でだけ反映する

「重くなりすぎない程度にイコライザー機能を、全体設定と曲ごとの設定の
両方に対応させ、オフにもできるように」という要望を受けて追加しました。

- **エンジンは swh-plugins の mbeq (15 バンドのグラフィック EQ、LADSPA
  プラグイン)。** alsaequal のような専用パッケージ (Debian の公式
  リポジトリでの提供が不安定) には頼らず、alsa-lib 標準の `type ladspa`
  PCM プラグインだけで完結させています。**この理由から、LADSPA プラグイン
  の実ファイルは `find` で探しています** (`open-jtalk`/`hts-voice` の
  辞書探索、CLAUDE.md #27 と同じ「決め打ちパスにしない」考え方) —
  distro/アーキテクチャでインストール先が微妙に違うためです。見つから
  なければ (パッケージ未インストールなど) EQ は自動的にオフへフォール
  バックし、音楽自体は鳴り続けます。
- **オフの間は LADSPA 段そのものを asound.conf から外します。** 「ゲイン
  0dB のフィルタを挟んだまま素通しする」のではなく、`pcm.sentinel_music`
  の定義そのものを EQ 無しの素通し (`type plug`) に書き換えます。これに
  より、オフのときは本当に CPU コストがゼロになります — 「オフにして
  負荷を抑えられるようにしてほしい」という要望の核心です。
- **設定変更 (オン/オフの切り替え、バンドの変更、曲の切り替わりによる
  実効バンドの変化) は、そのつど asound.conf を書き換えて mpg123 を
  再起動することで反映します。** alsa-lib 標準の LADSPA PCM プラグインは
  (alsaequal が独自に用意する専用の ctl プラグインと違って) バンドの値を
  ライブでは調整できません — 変えるには PCM を開き直す必要があります。
  そのため `Player._sync_eq()` は「直前に実際に適用した (enabled, bands)
  の組」を憶えておき、**次に鳴らす曲の実効設定 (全体設定 + その曲の
  上書き) が前回と完全に同じなら何もしません** — 曲を跨ぐたびに毎回
  無条件で再構成すると、EQ 設定が変わっていない曲同士の間にも再生の
  ギャップができてしまいます。**この「変化がなければ何もしない」判定を
  外して毎曲ごとに無条件で再構成する実装に戻さないでください** — 同じ
  曲間ギャップの不具合に戻ります。設定が実際に変わったとき (曲の切れ目、
  または設定タブでの変更直後の `music.refresh_eq()` 呼び出し) だけ、
  短い再生の途切れを許容しています。
- **全体設定 (`music_eq_bands`) と曲ごとの上書き (`music_eq_track_overrides`)
  は、`camera_overrides`/`bt_device_volumes` と同じ「疎な dict、無指定の
  バンドは 0dB (フラット) 扱い」パターンです。** 一般の設定 UI
  (GROUPS/LABELS) には出さず (CLAUDE.md #15 と同じ理由 — dict をテキスト
  入力欄で編集させる作りにはなっていない)、音楽タブに専用の 15 本
  スライダー UI と `全体設定`/`この曲だけ` の切り替えを設けています。

### 33. yt-dlp の `--restrict-filenames` は日本語タイトルを消していた

「取得した曲の日本語名が消されてしまう」という報告がありました。原因は
`music.py` の `_run_ytdlp()` が渡していた yt-dlp の `--restrict-filenames`
オプションです。このオプションは名前のとおり「制限された」ファイル名
(`[A-Za-z0-9_.-]` のみ) を強制するため、曲名が日本語であれば実質的に
ほぼ全文字が失われていました — 危険な文字だけを避けるのではなく、
非 ASCII 文字を丸ごと弾く強い制限です。yt-dlp は元々このオプション無しでも
ファイルシステムに使えない文字 (`/` や制御文字など) は既定で適切に
サニタイズするため、`--restrict-filenames` を外すだけで日本語タイトルを
保ったままファイルシステム安全性も保たれます。**この
`--restrict-filenames` を「Windows のファイル名制限を気にして」等の理由で
復活させないでください** — 同じ「日本語タイトルが消える」不具合に戻ります。

### 34. アクセス記録は AdGuard の「新しい順」を、保存前にひっくり返す

「ネットワークのアクセス記録の時刻順がおかしくなる」という報告がありました。
原因は `netlog.py` の `loop()` が、AdGuard Home の `/control/querylog` が
**新しい順 (降順)** で返してくることを踏まえずに書かれていたことです。

- 日次 JSONL ファイル (`_log_path()`/`read_day()`) は **古い順 (昇順)** を
  前提に読まれています — `web/routes.py` の `netlog_view()` は
  `read_day(day)[-limit:]` で「ファイルの末尾 = 直近」を取ってから
  `reverse()` していますし、`maintenance.py` の `ticker_entries()` も
  日次動画のテロップをタイムラプスの実時刻と対応させるため昇順を前提に
  読んでいます。ところが `loop()` は AdGuard から返ってきた (新しい順の)
  `fresh` をそのままファイルへ `_append()` していたため、ポーリング 1 回分
  の中では新しい→古いの順で書かれ、かつポーリングのたびに (より新しい)
  次のブロックがファイル末尾へ追記される、という「ブロック内は降順、
  ブロック間は昇順」のノコギリ状の並びになっていました。
- `RECENT` (直近イベント一覧) は逆に **新しい順** を前提にしています —
  `netlog_view()` は `day` 未指定のとき `RECENT[:limit]` をそのまま返し、
  `reverse()` していません。ところが `loop()` は `RECENT[:0] =
  reversed(fresh)` としており、新しい順の `fresh` をわざわざ古い順に
  ひっくり返してから先頭に挿し込んでいたため、こちらも向きが逆でした。

修正は `loop()` 内の 2 箇所だけです。日次ファイルへは `list(reversed(fresh))`
(古い順) で書き、`RECENT` へは `fresh` をそのまま (新しい順) 挿入します。
**この 2 か所の向きを揃えて元の実装 (ファイルへ `fresh` をそのまま、
`RECENT` へ `reversed(fresh)`) に戻さないでください** — AdGuard の
querylog が新しい順である以上、同じ「時刻順がおかしい」不具合に戻ります。

### 35. mpg123 の自己修復が、失敗し続けるイコライザー同期に道連れにされていた

「mpg123 が停止して、復帰を試みても復帰できない」という報告がありました。
原因は #32 で追加した `music.py` の `Player._sync_eq()` です。イコライザー
設定 (enabled/bands) が前回適用時から変わっていれば `_apply_audio_mixing()`
(sudo 経由で `sentinel-setup-audio-mixing.sh` を呼ぶ、最大 30 秒かかりうる)
を実行しますが、**これが失敗した場合に `_last_applied_eq` を一切更新して
いませんでした**。`play()` は `self._lock` を握ったまま `_sync_eq()` を
呼ぶため、`_apply_audio_mixing()` が (sudoers 未設定・`_card_index()` が
起動直後の ALSA 初期化と競合して失敗・スクリプト自身の実再生テストが
デバイスビジーで失敗、など) 一度でも失敗すると、`_last_applied_eq` が
「未適用」のまま固定され、**以後のすべての `play()` 呼び出しで同じ 30 秒
ブロックしうる sudo 呼び出しを再試行し続ける**ことになります。`loop()`
の自己修復ループは 5 秒おきに `PLAYER.play()` を呼んで mpg123 の復帰を
試みますが、その 1 回 1 回がこの重い再試行を踏むため、`self._lock` を
取り合う `status()` など他の Player 操作まで巻き添えで固まり、復帰が
実質的に機能しなくなっていました (mpg123 自体の再起動 `_spawn()` は
`_sync_eq()` の成否に関わらず続行されるため、深刻な環境では「復帰にとても
時間がかかる」、失敗が恒久的なら「復帰試行のたびに固まったように見える」
という症状になります)。

修正は `_eq_sync_failed_at`/`_EQ_RETRY_COOLDOWN_SEC` (既定 60 秒) を追加し、
直近で同じ適用に失敗していれば `_apply_audio_mixing()` 自体を呼ばずに即座に
諦める (mpg123 自体の復帰は妨げない) ことです。**この失敗記憶・クール
ダウンを外して、失敗するたびに毎回無条件で `_apply_audio_mixing()` を
呼ぶ実装に戻さないでください** — 同じ「mpg123 が停止しても復帰が固まって
機能しなくなる」不具合に戻ります。

### 36. イベントのタイムラインは既定表示にし、URL は「点」ではなく「期間」で描く

タイムライン表示 (#10/#18 で追加) について 3 つの要望がありました:
「URL アクセスが 1 列だと一部しか把握できないので複数行にして期間も
表示してほしい」「タイムラインを既定表示にしてほしい」「マウスホイールで
拡大、Shift+ホイールで移動、ドラッグで範囲選択し、その範囲を横に分割して
カメラ/URL データを順に並べてほしい」。

- **既定表示**: `#ev-view` の `<option>` を並べ替え、`timeline` に
  `selected` を付けました。これに伴い、以前は「タイムライン表示のときだけ」
  行っていた URL アクセス記録の取得 (`/api/netlog`) を `renderEvents()`
  側 (`EV_NET_DAY !== EV_DAY` のときだけ) に移し、タイムラインが既定に
  なっても "本体の R/W・通信は増やしたくない" という元の節約方針
  (グリッド/カメラ別表示では取得しない) を維持しています。
- **URL は期間で描く**: `buildNetSpans()` が、同じサービスへのアクセスを
  `TL_NET_SPAN_MIN` 分 (既定 5 分) 以内の間隔でまとめて「期間」(span:
  開始・終了時刻を持つ) にします。単純に「直前の記録と同じサービスか」
  だけを見ると、間に別サービスの記録が挟まった瞬間に誤って期間が分断
  されるため、サービスごとに独立した「今開いている期間」を追いかける
  (`open` map) 実装にしています。
- **複数行**: `packNetRows()` が、期間を開始時刻順に見て「既存の行のうち
  直前の終了時刻がこの開始時刻以前のものがあればそこへ、無ければ新しい
  行」という単純な区間スケジューリングで行に詰めます。同時期に複数の
  サービスへアクセスしていた場合だけ自然に複数行へ分かれ、URL トラックの
  高さ (`heightPx`) はこの行数から動的に決まります (画像トラックは
  従来どおり固定 1 行)。**この行パッキングを外して固定 1 行に戻さないで
  ください** — 同じ「一部しか把握できない」不具合に戻ります。
- **ホイール操作**: `tlHandleWheel()` を `#tl-scroll` の `wheel` イベントへ
  束縛しています。Shift 押下時は `scrollLeft` を直接動かして横移動、
  それ以外はカーソル位置の時刻を保ったままズームします (地図アプリの
  ズームと同じ「カーソル下の地点が画面上で動かない」挙動 — 画面中央基準
  ではなくカーソル基準にしているのは、ホイール操作は見ている場所を
  そのまま拡大/縮小したいことが大半なため)。
- **ドラッグ範囲選択 → その範囲だけ拡大表示** (#38 で見た目を修正済み):
  `#tl-scroll` に `mousedown`/`mousemove`/`mouseup` を束縛し
  (`tlMouseDown`/`tlMouseMove`/`tlMouseUp`)、実際に一定距離動いた場合
  だけドラッグとみなします (動きがほぼ無ければ従来どおり
  `tlScrollClick` のクリック=カーソル設置として扱う — `TL_SUPPRESS_CLICK`
  フラグで、ドラッグ確定直後に発火する click イベントだけを 1 回だけ
  握りつぶしています)。`tlZoomToRange()` が選択範囲を元に `TL_PXMIN`
  (ホイールズームと同じ変数) を引き上げて `renderEventsRecall()` を
  呼び直し、選択開始時刻が画面左端に来るようスクロールします — 本体の
  タイムラインと全く同じ見た目・同じトラック構成のまま、その範囲だけ
  拡大された状態になります。
- 1 分未満の期間が常に「1分」に丸められて区別できなくなる問題も
  `fmtDur()` に持ち込んでいました。`core/notify.py` の `_fmt_minsec()`
  (CLAUDE.md #20) と同じ考え方で、60 秒未満は秒表示にしています。

### 37. ALSA の出力カードは「最初に見つかったカード」を無条件で使わない

実機のエラーログで `sentinel-setup-audio-mixing.sh` (#31) が dmix の
スレーブを開けず失敗していました:
`ALSA lib pcm_dmix.c:1057:(snd_pcm_dmix_open) unable to open slave` /
`aplay: main:850: audio open error: Invalid argument`。スクリプト自身の
実再生テスト (#31 が導入した「実際に鳴らしてみる」検証) がこれを検知し、
`/etc/asound.conf` を安全に元へ戻していたため機能自体は壊れませんでした
が、ミキシング (音楽と音声アナウンスの同時再生・イコライザー) は一切
有効にならないままでした。

原因は `music.py` の `_card_index()` (`bluetooth.py` の `_card()`、
`voice.py` の `_sound_card()`、`sentinel-guardian.sh` の
`check_audio()` も同じ実装) が、`aplay -l` に列挙された**最初の**
`card N:` 行を無条件で使っていたことです。近年の Raspberry Pi カーネル/
DietPi では HDMI 出力ごとに別カード (`vc4hdmi0` など) が先に並び、
CLAUDE.md 冒頭の想定どおりの 3.5mm アナログ出力 (bcm2835 の
"Headphones") はそのあとの番号になることがあります。この機体では実際に
card 0 が HDMI で、`dmix` が `hw:0,0` を固定フォーマット (S16_LE/44100/
2ch) で開こうとして失敗していました。amixer 系の音量操作 (numid=1/
numid=3/SentinelVoice) は間違ったカードへ静かに書き込むだけで気付き
にくいのに対し、dmix はフォーマットを literal に要求して開こうとする
ため、ここで初めて表面化した形です。

`core/audio.py` に `find_output_card()` を新設し、"Headphones" (現行の
命名) を優先、無ければ "bcm2835" (旧来の単一カード構成)、それも無ければ
最初のカードにフォールバックする優先順位に直しました。`music.py`/
`bluetooth.py`/`voice.py` はこの共通関数を使うよう変更し、Python を
呼べない `sentinel-guardian.sh` の `check_audio()` には同じ優先順位の
`find_output_card()` を bash で複製してあります。**この優先順位を外して
「最初に見つかったカード」に戻さないでください** — 同じ「dmix がアナログ
出力ではなく HDMI を掴んで開けない」不具合に戻ります。**4 か所のうち
どれか 1 つだけ直すのもやめてください** — 例えば `music.py` だけ直すと、
音楽の dmix ミキシングは直っても Bluetooth 音量や音声アナウンスの音量は
違うカードを操作し続けることになります (`voice.py` の `SentinelVoice`
softvol コントロールは `sentinel-setup-audio-mixing.sh` が正しいカードに
対して作った設定なので、`_sound_card()` が別のカードを見ていると
そもそもそのコントロールが見つからず失敗します)。

### 38. タイムラインのドラッグ範囲選択は「区画への分割」ではなく「その場拡大」にする

#36 で追加したドラッグ範囲選択は、選択した範囲を横に並んだ区画へ分割し、
各区画にカメラ/URL データを時系列リストとして表示する専用レイアウトでした。
しかし本体のタイムライン (カメラ/URL のトラックを実時間軸に重ねる表示)
と見た目が別物になってしまい、「元の縮小表示と同じように、部分的に拡大
したように見せてほしい」という指摘がありました。

`renderTlDrilldown()`/`clearTlDrilldown()`/`TL_RANGE_SEL`/
`TL_DRILL_SEGMENTS`・関連 CSS (`.tl-drill*`) をすべて削除し、
`tlMouseUp()` はドラッグ確定時に `tlZoomToRange(t0, t1)` を呼ぶだけに
しました。`tlZoomToRange()` は選択範囲の長さから `TL_PXMIN` (ホイール
ズーム `tlHandleWheel()` と全く同じ変数) を計算し、
`renderEventsRecall()` で通常のタイムラインをそのまま再構築してから、
選択開始時刻が画面左端に来るようスクロールするだけです。**専用の区画
分割 UI を復活させないでください** — 本体のタイムラインと見た目が分裂
する、同じ指摘に戻ります。ドラッグ範囲選択は「ホイールズームのショート
カット」以上のものではない、というのがこの設計の要点です。

### 39. 外部からの安全なアクセスは Tailscale で行う (`--accept-dns=false` を必ず付ける)

「外部から安全にアクセスしたい」という要望で Tailscale を導入しました。
「意図的にしていないこと」に書いたとおり、この機体は元々 HTTPS を自前で
張らない LAN 内運用の前提で作っています。ポート開放やリバースプロキシを
新たに持ち込む代わりに、WireGuard ベースの暗号化された私設オーバーレイ
ネットワークを提供する Tailscale を使うことで、その前提を崩さずに外部
アクセスを実現しています — Sentinel の Web UI (8080) 自体には何も変更が
要りません。到達経路が LAN からオーバーレイネットワークへ広がるだけです。

**`tailscale up` には必ず `--accept-dns=false` を付けてください。**
既定では Tailscale が (MagicDNS や管理コンソールで設定したネームサーバー
経由で) この Pi の DNS 解決先を自分自身へ切り替えます。CLAUDE.md #5 の
とおり `netlog.py` は AdGuard Home の実際のクエリログをそのまま読んで
いるだけなので、DNS が AdGuard 経由でなくなると、この Pi 自身が行う
名前解決についてはクエリログに何も残らなくなり、ネットワークログが
「AdGuard の設定は合っているのに、なぜか一部だけ何も記録されない」形で
静かに壊れます (他の LAN 端末の記録は、その端末自身が引き続き AdGuard を
DNS に使っていれば影響を受けません — 壊れるのはこの Pi 自身の名前解決
だけです)。**この `--accept-dns=false` を外さないでください** — 同じ
「ネットワークログの一部が静かに欠落する」不具合に戻ります。

さらに、`tailscale up` の各フラグは **実行のたびに指定し直す必要があり、
前回の指定を記憶しません**。このため:

- 導入直後の接続は `setup.sh` の H7 で行います。`bootstrap.sh` は
  Tailscale の**パッケージだけ**をインストールします (STEP 8/8, 公式の
  インストールスクリプト経由 — dietpi-software に該当 ID が無いため)。
  `tailscale up` は初回認証にブラウザで開く URL が必要な、人手を要する
  操作 (`setup.sh` の他の H1〜H6 と同じ性質) なので、無人実行の
  `bootstrap.sh` 側では絶対に呼びません。
- 今後このリポジトリのどこか (診断スクリプト、Guardian の自己修復など)
  で `tailscale up` を再度呼ぶコードを書くことがあれば、そこでも必ず
  `--accept-dns=false` を明示してください。付け忘れても構文エラーには
  ならず、次に Tailscale との接続がリセットされるまで気付けません。

現時点では Guardian は Tailscale の状態を監視・修復しません — `tailscale
up` 実行後の DNS 設定は tailscaled 側の永続状態であり、勝手に巻き戻ることは
無いため、CLAUDE.md #12 のような周期的な自己修復は今のところ不要と判断
しています。

### 40. Obsidian の同期は CouchDB/LiveSync ではなく Syncthing で行う。ホームディレクトリは外部ストレージへバインドマウントする

「PC・iPad・スマホの Obsidian を自動同期し、外出先では Tailscale 経由で
安全に同期したい」という要望がありました。Obsidian コミュニティでは
Self-hosted LiveSync + CouchDB が第一候補として知られていますが、この
機体には採用しませんでした。

- **CouchDB には 32bit ARM (armhf) 向けの公式パッケージが存在しません**
  ([apache/couchdb#885](https://github.com/apache/couchdb/issues/885))。
  DietPi を 32bit で導入している場合、Docker を使わない前提では CouchDB
  はそもそも導入不可能です。
- **64bit (arm64) で導入できたとしても、CouchDB (Erlang/OTP VM) は
  この機体には重すぎます。** 公式ドキュメント系の導入ガイドは
  4GB RAM を前提にしており ([Micro Focus/OpenText の CouchDB 前提要件](https://www.microfocus.com/documentation/file-dynamics/6.6/guides/content/install/couchdb/installing_couchdb.htm))、
  CLAUDE.md 冒頭の制約表のとおりこの Pi は RAM 1GB・SWAP なしで、しかも
  `sentinel.service` だけで `MemoryMax=700M` を予約済みです。新しい常駐
  デーモンにそれだけの余地はありません。

代わりに **Syncthing** (`dietpi-software install 50`) を使っています。
Go 製の単一バイナリで armhf/arm64 どちらもビルドがあり、DietPi に
組み込みの導入 ID が存在するため、この項目の bootstrap.sh 導入は他の
dietpi-software 項目と全く同じパターンで済みます。**この選択を
CouchDB/LiveSync に戻さないでください** — 32bit 機では文字通り導入
できず、64bit 機でも RAM 予算を脅かします。

Syncthing はファイル単位でしか同期しないため (LiveSync のような
Obsidian 内でのリアルタイム差分マージはしない)、同じノートを 2 台から
ほぼ同時に編集すると `.sync-conflict-*` ファイルが作られるだけで自動
マージはされません。通常は 1 人が 1 台ずつ順番に編集する使い方が
大半なので実害は小さいと判断していますが、この制約は Syncthing を選んだ
以上ついて回ります。

- **DietPi の Syncthing はホーム/設定/インデックス DB を既定で
  `/mnt/dietpi_userdata/syncthing` (通常 SD カード上) に置きます。**
  同期される Obsidian Vault はこのプロジェクトが触るどの設定よりも
  高頻度に書き込まれます (全端末・全編集のたびに) — CLAUDE.md #3 の
  「高頻度の書き込みは `/dev/shm` にだけ行う」と同じ理由で、SD カードの
  摩耗を避けるため `install.sh` がこのディレクトリを `$STORAGE/syncthing`
  (外部ストレージ) へ**バインドマウント**しています。実際に同期される
  Vault のフォルダ自体も `$STORAGE/obsidian` に置くよう `setup.sh` の
  H8 で案内しています。
- **シンボリックリンクではなくバインドマウントを使っている理由**:
  DietPi が生成する `syncthing.service` の `ExecStart` (実際の `-home`
  フラグの綴り) は DietPi 側の実装詳細であり、このプロジェクトが把握・
  固定できるものではありません。そこを直接書き換えるコードは、DietPi が
  Syncthing を再導入するたびに (`update.sh` が `bootstrap.sh` を毎回
  再実行するため、通常のアップデートで容易に起こり得ます) 上書きされて
  静かに壊れます。バインドマウントはファイルシステム層で完結するため
  この問題を受けず、また DietPi のユニットに systemd のパスサンドボックス
  (`ReadWritePaths=` など) が掛かっていた場合でも、シンボリックリンクと
  違って `/mnt/dietpi_userdata/syncthing` という**その場所自体**が
  読み書き対象になるため影響を受けません。**この判断を、ExecStart や
  `-home` フラグを直接書き換える実装、あるいはシンボリックリンクに
  戻さないでください** — どちらも同じ「DietPi の再導入で静かに壊れる」
  不具合に戻ります。

  この bind マウントには、もう 1 つ別のレースが実機で見つかっています。
  DietPi 自身のドライブ自動検出が、起動直後の競合 (CLAUDE.md #12 の
  Bluetooth/Hotspot と同じ種類のレース) でこの場所へパーティションを
  **バインドではなく直接** 先にマウントしてしまうことがあります。
  `mount`/`findmnt` の SOURCE が `$ST_HOME[/...]` ではなく生のデバイス
  (`/dev/sda1` など) のままなら、それはこの直接マウントです。これを
  放置して `mount --bind` を重ねると、**同じ exFAT/NTFS パーティションが
  2 つの独立したマウントとして同時に生き続け**、双方に書き込みが起きると
  データ破損の恐れがあります。`install.sh`/`check_syncthing_storage()` は
  どちらも、bind する前に `$ST_DEFAULT` の現在のマウント元を確認し、自分の
  bind 由来でなければ umount (リトライ + `umount -l`) してから bind し
  直します。**このチェックを外して無条件に `mount --bind` を重ねる実装に
  戻さないでください** — 同じ二重マウントに戻ります。

  さらに、これらの障害が実機で実際に重なって起きた際、利用者が手作業で
  切り分けるのは難しいと判断し、`scripts/sentinel-fix-syncthing-mount.sh`
  を新設しました。`install.sh` の STEP 4 冒頭 (`sentinel-fix-storage-owner.sh`
  より前) で毎回呼ばれ、(1) `$STORAGE` 自体が古いマウントオプションの
  まま (`mount -a` は既にマウント済みのファイルシステムを直さないため、
  fstab を直しただけでは効きません)、(2) 同じデバイスがどこか別の場所にも
  二重マウントされている、(3) `$ST_DEFAULT` が bind ではなく生のデバイスに
  直接奪われている、の 3 つを条件分岐で自動修復します。自動で直せなかった
  項目は無言で諦めず、その場でコピペ実行できる手動コマンドとしてまとめて
  表示します。冪等 (問題が無ければ何もせず即終了) なので、`update.sh` の
  再実行だけで毎回このチェックが走ります。**この自動修復スクリプトの呼び
  出しを install.sh から外さないでください** — 外すと、次にこの種の障害が
  起きたとき利用者が再び手作業での切り分けを強いられます。
- **`install.sh`/Guardian の両方が、このバインドマウントを device+inode
  比較で検証しています** (`stat -c '%d:%i'` が `$STORAGE/syncthing` と
  `/mnt/dietpi_userdata/syncthing` とで一致するかどうか)。`findmnt` の
  オプション文字列を見るより頑丈な判定方法です — CLAUDE.md #8 で
  `findmnt` がマウントの重なり (autofs + 実体) を 1 つの呼び出しに複数行
  まとめて返し、意図しない分岐に落ちた実例があるのと同じ理由で避けて
  います。一致していなければ (DietPi の再導入・起動直後のレース等で
  ズレていれば) Guardian の `check_syncthing_storage()` が 2 分ごとに
  再マウントします。**この device+inode 比較をやめて `findmnt`/`mount`
  出力の文字列一致に戻さないでください** — 同じ「一見動いているのに
  実は判定が外れている」不具合を作り込むリスクに戻ります。
- **所有権は `sentinel-fix-storage-owner.sh` を `dietpi` ユーザー向けに
  再度呼び出すのではなく、`dietpi` を `sentinel` のグループへ追加する
  ことで共有しています。** 当初は「`<data-dir> <mountpoint> <user>` と
  いう既存の汎用シグネチャのまま、ユーザーだけ `sentinel` から `dietpi`
  に変えて呼ぶ」実装でしたが、これは実機で**実際に全サービス停止を
  引き起こす障害**になりました。原因は CLAUDE.md #8 そのものです —
  exFAT/NTFS では `uid=`/`gid=` は**マウント全体**に効く `/etc/fstab`
  オプションであり、ディレクトリ単位のものではありません。`install.sh`
  の STEP 4 が `$STORAGE` を `sentinel` 向けに直した直後、この節の当初の
  実装が同じ `$STORAGE` を今度は `dietpi` 向けに直そうとして
  `fix_fat_mount()` を再度走らせ、`sentinel` のために設定した
  `uid=`/`gid=` を上書きしていました。さらに Guardian の
  `check_storage_owner()` (sentinel 向け) と `check_syncthing_storage()`
  (dietpi 向け、当時) がどちらも 2 分ごとに独立して所有権を再確認して
  いたため、2 つのチェックが同じマウントの `uid=`/`gid=` を取り合い、
  周期のたびに `fix_fat_mount()` の umount → mount (最後は `umount -l`
  にも倒す) が発火し続け、他のサービス (`sentinel.service`・カメラ・
  `syncthing.service`) がまだファイルを開いたままのマウントを何度も
  付け外しすることになりました。結果、全サービスが停止し、物理ドライブ
  (`sda1`) が `$STORAGE` (`/mnt/VIDEOSD`) ではなく
  `/mnt/dietpi_userdata/syncthing` 側にマウントされたまま固定される、
  という報告どおりの障害が実際に発生しました。

  修正は `usermod -aG "$SVC_USER" dietpi` です。`fix_fat_mount()` は
  `sentinel` のためにマウントを直すとき既に `umask=002` (グループ書き込み
  可) を設定しているため、`dietpi` をこのグループへ加えるだけで
  `$STORAGE` 配下すべてへの書き込み権限を、fstab にも再マウントにも
  一切触れずに共有できます。exFAT/NTFS ではグループ書き込みがマウント
  オプション由来なのでこれで足り、ext4 のような通常の Unix ファイル
  システムに備えて `chgrp -R`/`chmod -R g+rwX`/`chmod g+s` もディレクトリ
  単位で (安全に、マウント自体には触れずに) 掛けています。新規に加わった
  補助グループは**既に起動中のプロセスには効かない**ため (`dietpi` の
  `id -nG` に `$SVC_USER` がまだ無いときだけ新規追加とみなし)、その場合は
  `syncthing.service` を再起動して反映させています。**この
  `sentinel-fix-storage-owner.sh` への `dietpi` 向け呼び出しを復活させ
  ないでください** — マウント全体の `uid=`/`gid=` を 2 人のユーザーが
  奪い合う限り、同じ「実機で全サービス停止」障害に戻ります。グループ
  共有ならこの奪い合いが原理的に起こりません。
- **Syncthing 自身の公開ディスカバリ/リレーサーバーへの依存は、GUI から
  オフにするよう案内しています** (`setup.sh` H8)。Tailscale (#39) が
  既に「外出先からの安全な到達性」を提供しているため、Syncthing 側でも
  同じ役割を持つ公開インフラに頼る必要がなく、オフにすることでこの
  Pi の Syncthing がどんな公開ディスカバリ網にも一切載らないようにできます
  — このプロジェクト全体の「WAN に何も晒さない」という設計 (#39 の
  HTTPS を張らない方針と同じ思想) に揃えるためです。自動化はしておらず
  (GUI のトグルのため)、案内のみです。
- **Guardian は `syncthing.service` を `check_services()` の監視対象にも
  追加しています** (存在すれば)。GUI 認証が未設定かどうかまでは検証して
  いません — `sentinel` 自身の Web UI パスワードにも同様の自動検証が
  無いのと同じ基準で、今回もスコープ外としています。
- **Syncthing の GUI (`:8384`) は、素の DietPi パッケージのままだと
  ループバックにしか listen しません** — `config.xml` の
  `<address>127.0.0.1:8384</address>` (`<gui>` の子要素、属性ではない
  — [公式ドキュメント](https://docs.syncthing.net/users/config.html)、
  [MichaIng/DietPi#3329](https://github.com/MichaIng/DietPi/issues/3329)
  で同じ症状が報告されています) が Syncthing 自体の上流既定値で、
  DietPi 側もこれを変えていません。**最初の修正はこれを `address="127.
  0.0.1:8384"` という属性だと誤認し**、`sed` パターンが一致せず何も
  変更されないまま `[OK]` のような見た目で終わっていました (実機で
  `ERR_CONNECTION_REFUSED` として発覚)。**このパターンを属性形式
  (`address="..."`) に戻さないでください** — 同じ「変更したはずなのに
  何も変わらない」不具合に戻ります。ところが `setup.sh` H8・`SETUP.md`/
  `SETUP.ja.md`・`install.sh` 末尾の案内はどれも `http://<Pi のIP>:8384`
  へ LAN からブラウザでアクセスして GUI パスワードを設定する前提で
  書かれており、ループバックのままではそもそも到達できず、実機で
  「開けない」という報告になりました。AdGuard の `:8083` (CLAUDE.md #5)
  は過去の実機トラブル固有の理由でループバック固定にしていますが、
  Syncthing の GUI にはその理由がなく、Sentinel 自身の Web UI (`:8080`)
  と同じ「到達可能・パスワードで保護」という設計のつもりで案内文を
  書いていたので、`install.sh` の Syncthing ブロック末尾
  (`systemctl enable --now syncthing` の直後) で `config.xml` の
  address を `0.0.0.0:8384` に書き換えて Syncthing を再起動します。
  `config.xml` は Syncthing が一度も起動していないと存在しないため、
  導入したその回の `install.sh` では間に合わないことがあります —
  `sentinel-guardian.sh` の `check_syncthing_gui()` が同じ書き換えを
  2 分ごとにも確認するので、次の周期までには追いつきます。**この
  0.0.0.0 への書き換えを外して素のループバック待ち受けに戻さないで
  ください** — 同じ「案内どおりに開いても繋がらない」不具合に戻ります。

### 41. 「動いているように見えるのに何も起きない」系は、設定を書いた後に実物を検証する

Syncthing の GUI (`:8384`) と音楽の再生が、どちらも**エラーを一切出さない
まま機能していない**という報告が実機から続きました。2 つは別の機能ですが、
原因の形は同じです — 設定を書いた側が「書けた = 効いた」と見なしていて、
実際にそうなったかを確かめていませんでした。

**Syncthing GUI (`:8384` が ERR_CONNECTION_REFUSED)**

`config.xml` の `<address>` をループバックから `0.0.0.0` へ書き換える修正を
2 度入れ、2 度とも実機では何も変わりませんでした。原因は 2 つあります。

1. 1 度目は `address="..."` という**属性**だと誤認していました。実際には
   `<address>127.0.0.1:8384</address>` という `<gui>` の子要素です
   ([公式ドキュメント](https://docs.syncthing.net/users/config.html))。
   `sed` は一致せず、何も変更しないまま成功したように見えていました。
2. 2 度目は形式を直しましたが、**Syncthing が動いたまま `config.xml` を
   編集していました**。Syncthing は設定をメモリ上に保持し、**終了時に
   config.xml へ書き戻します**。つまり「編集 → `systemctl restart`」は、
   restart の停止フェーズでこちらの編集が消される順序です。Syncthing 自身の
   案内どおり、**停止 → 編集 → 起動**の順でなければなりません。

`scripts/sentinel-fix-syncthing-gui.sh` がこの順序を守り、さらに 2 つの
前提を置きません。**config.xml の場所を決め打ちしません** (`syncthing
--paths` が正。Syncthing 1.27 以降は Unix での既定が
`$XDG_STATE_HOME/syncthing` へ移っており、この機体の bind マウント先が
実際に読まれているファイルとは限りません)。そして**最後に `ss` で
ループバック以外が本当に listen しているかを確認**し、駄目ならコピペ用の
診断コマンドを出します。**「動作中に sed して restart」という実装に
戻さないでください** — 同じ「直したはずなのに繋がらない」に戻ります。

**音楽が「再生中」と表示されるのに無音**

mpg123 はどのケースでもエラーを出しません。デバイスは開けて、サンプルも
受け取られているためです — スピーカーまで届いていないだけです。原因に
なりうる箇所が 3 つあり、どれも誰も検証していませんでした。

1. **共有ハードウェア音量 (numid=1) を元に戻すコードが、どこにも存在
   しませんでした。** voice.py は #31 で専用の softvol へ移って numid=1 を
   触らなくなり、bluetooth.py は端末が接続したときだけ書き込み (#15)、
   Guardian は numid=3 (出力ルート) しか見ていませんでした。つまり
   何かが一度 numid=1 を最小値にしたら、**永久に無音のまま誰も気付き
   ません**。`sentinel-fix-audio-output.sh` は、値が可変域の最下端
   (= 消音) のときだけ 80% へ戻します — 「小さいが聞こえる」設定
   (Bluetooth 端末ごとの音量など) を勝手に上書きしないための条件です。
2. **dmix が用意できていないとき、mpg123 が ALSA の既定デバイスへ
   流れていました。** `_spawn()` は `sentinel_music` が無ければ `-a` を
   付けずに起動していましたが、カードが複数ある Pi では既定が HDMI
   (card 0) になることが多く、正常に開けてしまいます。`music.py` は
   CLAUDE.md #37 と同じ優先順位で見つけたアナログ出力へ
   `plughw:<card>,0` として明示的に流すようにしました。**`-a` 無しの
   既定デバイス任せに戻さないでください。**
3. **`/etc/asound.conf` の `hw:N,0` が実際のカードとズレても再生成
   されませんでした。** `Player._sync_eq()` の「変わっていなければ
   何もしない」判定 (#32) の key が EQ 設定だけだったためです。カード
   番号と、asound.conf に実際に焼き込まれている値の両方を key に含める
   よう直しました。**この 2 つを key から外さないでください** — 一度
   間違ったカードで書かれた asound.conf から二度と復帰できなくなります。

どちらのスクリプトも `install.sh` から呼ばれ (= `update.sh` の再実行だけで
反映される)、Guardian も 2 分ごとに同じスクリプトへ委譲します。
`check_audio()` が持っていた `find_output_card()` の bash 複製は
`sentinel-fix-audio-output.sh` 側へ 1 本化しました (CLAUDE.md #37 の
「4 か所のうち 1 つだけ直すな」を、そもそも複製を減らして守るため)。

**実機のログで判明した続き (2 点)**

`:8384` がまだ開けない原因は、GUI の bind アドレス以前に **Syncthing が
そもそも起動できていない**ことでした。ログはこうです:

```
WRN Failed to correct directory permissions
    (error="chmod /mnt/dietpi_userdata/syncthing: operation not permitted")
ERR Failed to acquire lock
    (error="open /mnt/dietpi_userdata/syncthing/syncthing.lock: permission denied")
syncthing.service: Start request repeated too quickly.
```

1. **`$ST_DEFAULT` (`/mnt/dietpi_userdata/syncthing`) が root 所有のまま
   でした。** `install.sh` の `mkdir -p "$ST_DEFAULT"` は root で走るので
   `root:root 0755` になります。その上に bind マウントが載っている間は
   exFAT 側の `uid=`/`gid=` が効くので問題になりませんが、**bind が外れて
   いる瞬間 (マウント修復スクリプトが一度 umount した直後や、bind に失敗
   した場合) は素の root 所有ディレクトリが露出**し、`dietpi` で動く
   Syncthing は lock ファイルすら作れません。`mkdir` の直後に
   `chown dietpi:$SVC_USER` + `chmod 0775` を掛け、Guardian も
   「マウントポイントでないとき」だけ同じ修正を毎周期行います。
   **この chown を外さないでください** — bind が外れた瞬間に Syncthing が
   起動不能になる状態に戻ります。
2. **起動失敗の連発で `failed (start-limit-hit)` に固定されていました**
   (CLAUDE.md #9 と全く同じ罠)。権限エラーで即死するため systemd の既定
   「10 秒に 5 回」をすぐ超え、以後の `systemctl start` は
   `Start request repeated too quickly` で**無視**されます。つまり権限を
   直しても自動では起き上がりません。`install.sh`・
   `sentinel-fix-syncthing-gui.sh`・Guardian の
   `check_syncthing_storage()` の**すべての** `systemctl start syncthing`
   の前に `systemctl reset-failed syncthing` を入れました。**この
   reset-failed を外さないでください。**

あわせて、`install.sh` の dietpi 書き込みテストの対象を `$ST_HOME`
(`$STORAGE/syncthing`) から **`$ST_DEFAULT`** へ変え、bind マウント確定後に
実行するようにしました。**Syncthing が実際に開くのは `$ST_DEFAULT` 側**
であり、bind が効いていない場合この 2 つは別のディレクトリです — 今回は
まさにその状況で、テストは `$ST_HOME` を見て「書ける」と報告していました。
失敗時は `ls -ld` と `id -nG dietpi` も出すので、次は journalctl を見に
行かなくても切り分けられます。

**mpg123 の stderr を捨てていました**

音楽側は「無音」ではなく、`mpg123 が停止していたため復帰させます` が
繰り返し記録される = **mpg123 が即死し続けている**状態でした。ところが
`_spawn()` は `stderr=subprocess.DEVNULL` で起動していたため、ALSA の
「デバイスを開けない/使用中」といった死因がすべて捨てられていました。
`stderr=subprocess.PIPE` にし、専用スレッドで読み続けて (読まないと
mpg123 側のパイプが詰まります) 直近 5 行を保持し、復帰ループが死亡を
検知した時点で `last_error` とログに載せるようにしました。**この
stderr を再び DEVNULL に戻さないでください** — 同じ「即死し続けるのに
理由が分からない」状態に戻ります。

### 42. 実機の状況報告は `sentinel-logs` で取る (生のジャーナルを貼らせない)

実機の不具合を切り分けるとき、これまでは利用者に `journalctl` の出力を
そのまま貼ってもらっていました。これには 2 つの実害がありました。

1. **同じ行が何十回も並ぶ。** 権限エラーで毎秒再起動するサービスは、
   同じ文を延々と出します。実際に届いた報告では、10 行のうち意味のある
   情報は 4 種類だけで、残りは同一メッセージの繰り返しでした。長いので
   途中で切られ、**肝心の 1 行 (`Start request repeated too quickly`)
   が埋もれる**ことが起きました。
2. **状態が分からない。** ログだけでは「今どのサービスが落ちているか」
   「どのカードが音声出力か」「誰がどこに書き込めるか」が読み取れず、
   毎回追加で質問する往復が発生していました。

`scripts/sentinel-logs.sh` はこの 2 点だけを解決します。ジャーナルの
接頭辞 (タイムスタンプ・ホスト・`unit[pid]`) と、デーモンが本文に自分で
書くタイムスタンプを取り除いてから同一メッセージを数え、`x14` のように
回数を付けて 1 行にまとめます — PID が毎回変わる再起動ループでも正しく
畳めます。先頭にはサービスの active/failed (`start-limit-hit` は
「`reset-failed` が要る」と明示)、`$STORAGE` のマウントと書き込み可否、
`$ST_DEFAULT` の所有者と dietpi の書き込み可否、音声カードと numid=1/3、
待ち受けポートを置きます。

**`sentinel-diagnose` を置き換えるものではありません。** あちらは
tar.gz を作る深掘り用で、チャットに貼るには向きません。こちらは標準
出力へのプレーンテキスト 1 画面分で、全選択して貼るだけで済むことを
目的にしています。**「とりあえず journalctl を全部貼ってください」に
戻さないでください** — 上の 2 つの実害にそのまま戻ります。

ジャーナルの読み取りには root (または `systemd-journal` グループ) が
要ります。権限が無い場合でも状態ブロックは出し、「読めなかった」と
明示してから `sudo sentinel-logs` を案内します — 黙って空を返すと
「何も問題が無い」と誤読されるためです。

### 43. 集計ツール自身が実機で嘘をついた 2 点 (#42 の続き)

`sentinel-logs` を実機で初めて使ったところ、**ツール側の欠陥で肝心の情報が
落ちていました**。どちらも「まとめる」道具が必ず踏む種類の失敗です。

1. **Python の logging はミリ秒を `,926` とカンマ区切りで付ける。** 本文
   先頭のタイムスタンプを取り除く正規表現が秒までしか見ていなかったため、
   `2026-09-16 02:01:14,926 WARNING ... mpg123 が停止していたため復帰
   させます` は 1 行ごとに別メッセージ扱いになり、**180 行がまったく
   畳まれませんでした**。秒の後ろの `[.,][0-9]+` も食わせて解決。
2. **上限に達したとき、古い方から 25 件を表示していました。** これは
   逆です。修正を当てた直後に読みたいのは**新しい行**で、実機では
   「あとの 167 件」の中に今まさに必要な情報が入っていたのに、2 時間前の
   繰り返しで埋まっていました。`sort` してから `tail` で**直近**を残す
   ように変更。**この 2 つを元に戻さないでください** — 畳めないか、
   新しい情報が押し出されるかのどちらかに戻ります。

あわせて 2 つ追加しました。

- **failed / activating のユニットは、フィルタを通さず生ログ 12 行を出す。**
  実機では `sentinel-bluealsa.service` が 312 回失敗していたのに、
  ログに出ていたのは systemd 自身の `Failed with result 'exit-code'`
  だけで、**なぜ落ちたかを述べた行が 1 つも残っていません**でした
  (デーモンの出力がフィルタ語に一致しなかったため)。今壊れているものに
  ついてはフィルタを信用しません。
- **そのユニットの `User=` とサンドボックス設定も出す。** systemd は
  サンドボックス系ディレクティブが 1 つでもあると専用のマウント名前空間を
  作るため、**あとから host 側で張った bind マウントはサービスからは
  見えません**。この状態では「シェルからの書き込みテストは通るのに、
  サービスだけ EACCES」という、出力だけ見ても矛盾にしか見えない現象が
  起きます (実機の Syncthing がまさにこの疑い)。`PrivateMounts`/
  `ProtectSystem`/`ReadWritePaths` を並べておけば一目で切り分けられます。

### 44. numid は名前を確認してから書く

実機の `sentinel-logs` が `numid=3 (route) 230` を報告しました。bcm2835 の
出力ルートは 0/1/2 の列挙なので、230 という値はそもそもこの numid が
ルート制御ではないことを意味します。にもかかわらず Guardian は
`FIXED: output routing (numid=3) was 230 - reset to AUX` を周期ごとに
繰り返しており、**無関係なコントロールへ 1 を書き続けていました**
(書いても意味が無いので毎回「直っていない」と判定され、永久に繰り返す)。

numid は単なる索引で、カードが違えば別のコントロールを指します。
`sentinel-fix-audio-output.sh` は `amixer cget` の `name='...'` を読み、
numid=3 の名前に `Route`、numid=1 の名前に `Volume` が含まれるときだけ
書き込むようにしました。含まれない場合は書かずに実際の名前を報告します。
**numid を名前の確認なしに書く実装へ戻さないでください** — CLAUDE.md #15
が numid=1 と numid=3 の取り違えを警告しているのと同じ罠で、今回は
「値が明らかに範囲外なのに毎周期 FIXED と報告し続ける」形で表面化しました。

同じスクリプトの再生テストも、失敗時に `aplay` の実際のエラーをそのまま
出し、さらに `hw:<card>,0` を直接開いてみて「dmix の定義が悪いのか、
カード自体が塞がっている/固まっているのか」を切り分け、後者なら
`fuser -v /dev/snd/*` で掴んでいるプロセスを名指しします。実機では
`aplay -D sentinel_music` が `audio open error: Invalid argument` で
失敗しており、この 2 つの区別が付かないと次の一手が決められません。

### 45. サービスが「どのユーザーで動くか」を推測しない。3 つの症状が 1 つの原因だったこと

`sentinel-logs` (#42) が failed ユニットの `User=` と生ログを出すようになって
初めて、長く追いかけていた 3 つの症状の正体が判明しました。**どれも推測で
書いたコードが原因で、実機のログ 1 行ずつで確定しました。**

**(1) Syncthing は `dietpi` ではなく `syncthing` ユーザーで動いていた**

```
User=syncthing
ExecStart=/opt/syncthing/syncthing ... --home=/mnt/dietpi_userdata/syncthing
```

CLAUDE.md #40 以降の対策 — `dietpi` を `sentinel` グループへ追加、
`chown dietpi:sentinel`、そして `dietpi` での書き込みテスト — は**すべて
サービスが一度も名乗らないユーザーを対象にしていました**。しかも
書き込みテストは `dietpi can write: yes` と報告し続けたため、**対策が
効いているように見えるのに Syncthing は `syncthing.lock` で
permission denied を出し続ける**という、出力だけ見ると矛盾する状態に
なっていました。exFAT は `uid=984(sentinel)/gid=984(sentinel)`、
`dmask=0002` で `drwxrwxr-x` なので、`syncthing` ユーザーは other 扱い =
書き込み不可です。

修正は `systemctl show syncthing -p User --value` で実際のユーザーを取得し、
グループ追加・chown・書き込みテスト・`syncthing --paths` の実行ユーザーを
すべてそれに合わせることです (`install.sh`・`check_syncthing_storage()`・
`sentinel-fix-syncthing-gui.sh`・`sentinel-logs.sh` の 4 か所)。
**ユーザー名をハードコードした実装に戻さないでください** — DietPi の
パッケージ構成が変われば同じことが起き、しかも「テストは通るのに動かない」
という最も時間を溶かす形で現れます。

**(2) bluealsa は D-Bus 名を取れずに落ち続けていた**

```
bluealsa: E: main.c:137: Couldn't acquire D-Bus name.
          Please check D-Bus configuration. Requested name: org.bluealsa
```

D-Bus の well-known name の所有者は 1 つだけです。取れなければ bluealsa は
終了し、systemd が再起動し、また終了する — 実機では 2 時間で 492 回
失敗していました。ほぼ確実にディストリ側の `bluealsa.service` が同時に
動いています。`scripts/sentinel-fix-bluealsa.sh` が `dbus-send` で
`GetNameOwner` → `GetConnectionUnixProcessID` と辿って**実際の所有者の
PID とユニット名を特定**し、別ユニットならそれを停止・無効化します
(こちらの unit は `-p a2dp-sink` を持つ必要があるため、残すのはこちら)。
所有者が居ないのに取れない場合は D-Bus のポリシー問題なので、推測で
いじらず該当ファイルの確認手順を出して止まります。

**(3) 音楽が鳴らないのは (2) の巻き添えだった**

これが今回いちばん重要な発見です。`sentinel-bluealsa-aplay.service` は
`Requires=sentinel-bluealsa.service` なので、(2) の再起動ループのたびに
道連れで停止・起動を繰り返し、**そのたびに ALSA の既定デバイスを開いて
閉じます**。bcm2835 はこれに耐えきれないことがあり
(`bcm2835-audio: failed to close VCHI service connection (status=-11)`)、
一度おかしくなると dmix が `hw:N,0` を開けなくなります。結果:

- `aplay -D sentinel_music` → `audio open error: Invalid argument`
- mpg123 も開けない → **JACK モジュールへフォールバックして即死**
  (`jack server is not running`) → 5 秒ごとの復帰ループ

**つまり Bluetooth の D-Bus 名前衝突が、Bluetooth とは何の関係もない
「音楽が鳴らない」として現れていました。** 症状ごとに個別対処していた
限り直らなかったのは当然で、直すべき箇所は 1 つでした。

あわせて mpg123 には `-o alsa` を付け、出力モジュールを固定しました。
mpg123 は指定が無いと alsa/jack/pulse を順に試すため、ALSA が開けない
ときに JACK を探しに行って死にます。このプロジェクトは ALSA へ直接書く
前提 (CLAUDE.md #2) なので、**駄目なら ALSA のエラーで正直に落ちる**方が
正しく、実際 JACK のメッセージは本当の原因を隠していました。**この
`-o alsa` を外さないでください。**

**教訓として残すこと**: 今回の 3 つは、どれも「調べれば 1 行で分かる事実」を
推測で埋めたことが原因です。サービスのユーザー、D-Bus 名の所有者、
プロセスの死因 — いずれも `systemctl show` / `dbus-send` / stderr を
読めば確定できました。**次に似た症状が出たら、まず `sentinel-logs` を
取り、事実が出揃うまでコードを書かないでください。**

### 46. named PCM は「一覧に載っているか」ではなく「実際に開けるか」で判定する

「音楽が鳴らない」が #45 の修正後も残りました。`sentinel-logs` は
`sentinel_music PCM present` と報告しており、サービスも全て active、
mpg123 のエラーも 1 行もありません。それでも無音でした。

原因は判定の仕方です。`music.py`/`voice.py` の `_mixing_ready()` は
`aplay -L` の一覧に名前があるかどうかだけを見ていました。**あの一覧は
`/etc/asound.conf` にその定義が書いてあることしか意味しません。** dmix は
スレーブ (`hw:N,0`) を開いて初めて失敗するため、カード番号がズレている・
他のプロセスがカードを直接掴んでいる・bcm2835 が開閉の連発で固まって
いる (#45) のいずれでも、**名前は一覧に出続けるのに一切開けません**
([alsa-lib #426](https://github.com/alsa-project/alsa-lib/issues/426)、
[Arch Forums](https://bbs.archlinux.org/viewtopic.php?id=173709) など、
`unable to open slave` として広く報告されている挙動)。その状態で
mpg123 へ `-a sentinel_music`、aplay へ `-D sentinel_voice` を渡すと、
どちらも「開けないデバイスへ書き込もうとして何も鳴らない」だけで、
ducking へのフォールバックも起きません — 音楽も読み上げも同時に沈黙
します。利用者が報告した「アナウンスが流れた瞬間に音楽もアナウンスも
使えなくなった」という挙動とも一致します。

CLAUDE.md #8 の `can_write()`、#31 の「書いたあと実際に鳴らしてみる」と
同じ原則がここだけ抜けていました。`core/audio.pcm_opens()` を新設し、
`/dev/zero` (デジタル無音) を 1 秒だけ流して実際に開けるか試します
(結果は 30 秒キャッシュ、asound.conf を書き換えたら
`invalidate_pcm_cache()` で捨てる)。**`aplay -L` の一覧を見るだけの
判定に戻さないでください** — 同じ「エラーも音も出ない無音」に戻ります。

あわせて 3 つ直しています。

1. **`voice.py` のフォールバック再生が `-D` 無しだった。** ALSA の既定
   デバイスへ流れるため、複数カードある Pi では HDMI へ出て無音になり
   ます — `music.py` が `plughw:<card>,0` を明示しているのと同じ理由
   (#37)。`_fallback_device()` で揃えました。
2. **`sentinel-fix-audio-output.sh` が報告するだけだった。**
   `sentinel_music` が開けず `hw:N,0` は開ける (= 設定側の問題) なら
   asound.conf を正しいカードで書き直し、それでも駄目なら
   `asound.conf.broken` へ退避します。退避が要るのは、この設定が
   `pcm.!default` も同じ dmix に向けているためです — 開けない dmix を
   置いたままにすると、Sentinel だけでなく bluealsa-aplay も素の
   `aplay` も含めた**機体全体の音**が死にます。逆に `hw:N,0` すら
   開けず `/dev/snd` を誰も掴んでいない場合は ALSA ドライバを再読み込み
   して #45 の「固まった bcm2835」を解きます (10 分のクールダウン付き。
   再生中に引き抜く方が有害なので `fuser` で無人を確認してからのみ)。
3. **`audio_mixing_enabled` (既定 True) を追加。** オフにすると dmix を
   一切使わず、音楽はアナログ出力へ直接、読み上げは曲を止めてから鳴る
   #31 以前の挙動に戻ります。「音声アナウンス・同時再生・EQ のどれが
   原因か」を利用者自身が切り分けられるようにするための元栓です
   (CLAUDE.md #30 のとおり `GROUPS`/`LABELS` にも追加済み)。

`_apply_audio_mixing()` は戻り値を `(適用できたか, 実際に有効になった EQ)`
に変えました。`sentinel-setup-audio-mixing.sh` は LADSPA が無い/EQ 付きの
構成が再生テストに落ちた場合に黙って EQ 無しへ降格します (#32) が、
`Player` 側が「要求した値」を覚えていると、ファイルの実態とずれたまま
毎曲ごとに再構成が走り、曲間にギャップが出続けます。**`EQ_ACTIVE=` の
実測値を覚える形から、要求値を覚える形に戻さないでください。**

### 47. `systemctl is-active` が inactive でも、完了した oneshot は異常ではない

`sentinel-logs` が `FIXED: started hciuart.service x5` を報告しました。
稼働 9 分・Guardian の周期は 2 分なので、**毎周期 1 回ずつ**再起動して
いた計算です。`hciuart.service` は `Type=oneshot` で `RemainAfterExit` を
持たないため、仕事を終えた後の `inactive` が正常な姿です。それを異常と
みなしていました。

無害なノイズでは済みません。再起動のたびに Bluetooth の UART を付け直す
ので `bluetoothd` がコントローラを見失い、`check_bluetooth()` がそれを
「修復」して `bluetooth.service` を再起動し、BlueALSA 系ユニットが道連れ
になり、bcm2835 の ALSA デバイスが開閉を繰り返します。この開閉の連発が
#45 で特定した「音楽が鳴らない」の直接の原因です。**つまり 2 分ごとに
音声を壊しにいくタイマーが仕込まれていました。**

`unit_needs_start()` を追加し、`ActiveState=failed` なら常に、`inactive`
でも oneshot かつ `RemainAfterExit != yes` で一度も起動していない
(`InactiveEnterTimestamp` が空) 場合だけ起動します。**素の
`systemctl is-active` チェックに戻さないでください** — Bluetooth/音声の
再起動カスケードが 2 分周期で復活します。

あわせて、Guardian の実行順で `check_bluealsa_dbus` を `check_services`
の**前**へ移しました。実機のログがこの順序の誤りをそのまま示しています
— 14:20:23 に `started sentinel-bluealsa.service`、その 10 秒後に
`disabled bluealsa.service - it was holding org.bluealsa`。D-Bus 名を
別ユニットが握ったままでは起動は最初から失敗する運命だったので、先に
衝突を解いてから起動すべきです (無駄な BlueALSA 再起動 = ALSA の開閉
churn も 1 往復減ります)。

### 48. Tailscale は `sentinel-tailscale` から操作する

`setup.sh` の H7 は初回導入時に一度しか聞かないため、そこで見送ると
あとから設定する入口がどこにも無く、実際に「Tailscale の設定方法が
わからない」という報告になりました。`scripts/sentinel-tailscale.sh`
(`/usr/local/bin/sentinel-tailscale`) にまとめています —
`status` (既定、何も変更しない) / `up` / `down` / `reset`。

- `up` は必ず `--accept-dns=false` を付けます (CLAUDE.md #39)。
  Tailscale はフラグを記憶しないため、呼ぶ側が毎回明示する必要が
  あります ([Tailscale Docs](https://tailscale.com/docs/install/linux))。
- `status` は tailnet の IP と MagicDNS 名に加えて、そこから開ける
  URL (`:8080`/`:8384`) を出します。さらに `/etc/resolv.conf` が
  `100.100.100.100` を向いていれば警告します — その状態ではこの Pi 自身
  の名前解決が AdGuard を通らなくなり、ネットワークログが静かに欠落
  するためです (#39)。
- `up` の成功後に「管理コンソールで key expiry を無効にする」案内を
  出します。無人運用の機体は鍵の期限切れで tailnet から落ちても誰も
  ログインし直せないためです ([Tailscale Docs](https://tailscale.com/kb/1076/dogcam))。

### 49. index.html は「先にダッシュボードを描いてから正しいページへ切り替える」構造をやめる

「Tailscale 経由でアクセスすると、メニューを押すたびに一旦ダッシュボード
が表示されてから遷移先が表示される」という報告がありました。LAN では
気付かない程度の一瞬で、Tailscale (WireGuard トンネル、経路によっては
DERP リレー越し) では往復が伸びる分、この一瞬が体感できるほど伸びます。

`go(p)` 自体はハッシュの変更だけで完結する純粋なクライアント側ルーティング
で、ページ切り替えは `section.page` の `class` を同期的に付け替えるだけ
なので、ネットワークの遅さでこれ自体が遅くなることはありません。原因は
別の場所にありました — `<section class="page on" id="p-dash">` という
**ダッシュボードを既定で可視にする class がテンプレートに直書き**されて
いたことです。ページの生 HTML がブラウザに届いた瞬間 (メインスクリプトが
まだ 1 行も実行されていない段階) は常にダッシュボードが見えており、
その後メインスクリプトが読み込まれて `go(location.hash.slice(1) ||
"dash")` を実行して初めて正しいページへ切り替わります。この「HTML が
届いてからメインスクリプトが実際に走るまで」の間隔は、上に読み込まれて
いる xterm 関連の 2 本の外部 `<script>` (CDN からの取得、`defer` も
`async` も付けず同期的にブロックする) の分だけ確実に伸び、Tailscale
越しではこの間隔が伸びて「ダッシュボードが一瞬見えてから遷移先が見える」
という体感になります (ブラウザが実際に画面全体を再読み込みしているとき
にも、この直書きされた既定状態が最初に描画されるため、同じ症状になり
ます — モバイル端末が VPN トンネル下でタブをバックグラウンドから復帰
させる際に再読み込みすることがある、という事情とも合致します)。

直したのは 2 点です。

1. `#p-dash` の `class="page on"` を `class="page"` に戻し、既定では
   どのページも見えない状態にしました。代わりに、`</main>` の直後
   (すべての `<section>` が既にパース済みの、ファイル中で一番早い安全な
   地点) に、`location.hash` を読んでその場で該当ページだけを可視化する
   短いインライン `<script>` を追加しました。この script は defer も
   async も付けない素の `<script>` なので、パーサーがここに到達した
   瞬間に同期的に実行され、あとに続くメインスクリプトの読み込みが
   どれだけ遅れても「違うページが一瞬見える」こと自体が起こりません。
   **この即時解決スクリプトを外して `#p-dash` の `on` を直書きに戻さない
   でください** — 同じ「メニュー移動のたびにダッシュボードが一瞬映る」
   症状に戻ります。
2. xterm 関連の 2 本の `<script src=...>` (`xterm.js`/
   `xterm-addon-fit.js`) に `defer` を付けました。`new Terminal(...)` は
   端末タブを実際に開いたとき (ユーザー操作後) にしか呼ばれないため、
   起動直後の描画をこの 2 本の CDN 取得で足止めする理由がありません。
   Tailscale 越しは往復が伸びる分、この足止めの影響も同じだけ伸びます。

### 50. mpg123 の読み取りスレッドは「世代」で古いプロセスの出力を無視する

BGM がようやく安定して鳴るようになった (#41/#45/#46) あと、「場合に
よって 2 秒ほどで次の曲にどんどん変わってしまう」という報告がありました。

`music.py` の `Player` は mpg123 を `-R` (リモート制御モード) で常駐
起動し、専用スレッド (`_read_loop()`) が mpg123 の標準出力を読んで
`@P 0` (停止) を「曲が自然に終わった」と解釈し、`_advance_and_play()`
で次の曲へ進めます。誤って次へ進まないよう、`stop()` (eco/Bluetooth/
voice の退避) は `suspended_by` にその理由の文字列 (`"eco"` など) を
入れてから "S" を送り、`_read_loop()` 側は `not self.suspended_by`
のときだけ次へ進める、という仕組みでした。

これが崩れていたのが `_sync_eq()` (#32 で追加したイコライザー/dmix の
再構成) です。設定変更を反映するために実行中の mpg123 を "S"+"Q" で
落としますが、**`suspended_by` を一切書き換えずに落としていました**。
`play()` の呼び出し順は「`_sync_eq()` で落とす → (必要なら) `_spawn()`
で新しいプロセスを起動 → 最後に `suspended_by` を `""` に戻す」という
1 つの `with self._lock:` ブロックの中で完結します。ところが古い
プロセスの標準出力に対する `_read_loop()` は別スレッドで動いており、
`self._lock` の取得待ちでブロックされているだけで **生きたまま**です。
`play()` がロックを解放した瞬間にこの古いスレッドがロックを取り、
"S" への応答である `@P 0` を処理します。このとき `suspended_by` は
`play()` が既に `""` に戻したあとなので、`not self.suspended_by` が
真になり、**本当は「EQ を再構成するために意図して止めただけ」の
プロセスの出力を「曲が終わった」と誤認して次の曲へ進めて**しまいます。
新しい曲の再生が始まった直後にまた同じ理由で `_sync_eq()` が呼ばれれば
(カード番号の一時的なブレ、あるいは EQ 設定が曲ごとに違う場合など)
この誤認が連鎖し、「2 秒ほどで次々に曲が変わる」という報告どおりの
症状になります (2 秒という間隔は `_apply_audio_mixing()` が呼ぶ
`sentinel-setup-audio-mixing.sh` の実再生テストにかかる時間とほぼ
一致します)。`suspended_by` が空文字列の `stop(reason="")` (#46 で
追加した `restart_playback()` が使う) も同じ穴を持っていました。

`suspended_by` の値をどれだけ丁寧に管理しても、「このプロセスの出力は
もう古い」という区別そのものがない限り同じ穴が別の場所にも開き得ます。
そのため `Player` に `_gen` という世代カウンタを追加し、mpg123 を
**殺すと決めた瞬間** (`_sync_eq()`/`stop(terminate=True)` のどちらも、
"S" を送る前) に `self._gen += 1` します。`_spawn()` は起動したプロセス
にその時点の `self._gen` を割り当て、`_read_loop(gen, proc)` はこの
`gen` を固定引数として受け取ります (`self.proc`/`self._gen` を後から
読み直すのではなく、スレッド開始時のスナップショットとして渡す)。
`@F`/`@P`/`@E` のどの行を処理する前にも、ロックを取った状態で
`gen != self._gen` を確認し、一致しなければ何もせず読み飛ばします。
これにより「意図して落としたプロセスの出力」は `suspended_by` の値に
一切関係なく、確実に無視されるようになりました。

殺すと決めた瞬間 (プロセスの実際の終了より前、"S" を送信する前) に
世代を進めているのが要点です — `play()` は `_sync_eq()` の呼び出しから
ロックを解放するまで世代を進め終えているため、古いスレッドがロックを
取れる頃には必ず新しい世代になっており、「進めるタイミングが遅れて
間に合わない」という余地がありません。**この世代チェックを外して
`suspended_by` だけの判定に戻さないでください** — 同じ「曲が短時間で
次々に変わっていく」不具合に戻ります。stub の `Popen`/`stdout` を差し
込んで `_read_loop()` を直接スレッドなしで呼ぶテストで、(1) 古い世代の
`@P 0` は `suspended_by` の値に関わらず無視される、(2) 現在の世代の
`@P 0` は退避理由が無ければ正しく次の曲へ進める、(3) 現在の世代でも
退避中なら進めない、の 3 パターンを確認済みです。

### 51. `hciuart.service` は `Type=oneshot` ではなく `Type=forking`。Type= だけで「完了扱いか」を判定しない

#47 で `unit_needs_start()` を追加し、「`Type=oneshot` で `RemainAfterExit`
が無ければ、`inactive` は失敗ではなく正常な終わり方」と判定するように
しました。しかし `hciuart.service` (raspberrypi-sys-mods 提供、UART の
Bluetooth アタッチを一度だけ行う) は **`Type=oneshot` ではなく
`Type=forking`** でした。この Type だけを見ていた判定は hciuart には
一切効かず、実機では稼働 2 時間14分の間に `FIXED: started
hciuart.service` が **57 回** — ほぼ Guardian の周期 (2 分) のたびに
1 回、休みなく再起動し続けていました。

これは無害な繰り返しでは済みませんでした。再起動のたびに UART を
付け直すため `bluetoothd` がコントローラを見失いやすくなり (CLAUDE.md
#12 の既知の競合)、`check_bluetooth()` がそれを「修復」しようと
`bluetooth.service` を再起動し、`Requires=` で繋がった BlueALSA 系
ユニットが道連れになり、そのたびに bcm2835 の ALSA デバイスが開閉を
繰り返します。この開閉の連発は CLAUDE.md #45 が特定した「bcm2835 が
壊れて dmix が開けなくなる」不具合の直接の引き金で、実機の
`sentinel-logs` では同じ 2 時間の中で `sentinel_music`/`sentinel_voice`
の open 失敗が連発し、最終的に mpg123 自身が
`[src/mpg123.c:play_frame():857] error: Deep trouble! Cannot flush to my
output anymore!` を出してプロセスごと終了する (mpg123 はこのエラーの
あと終了コード 133 で自発的に落ちる、既知の挙動) のが 18 回記録されて
いました。「hciuart が毎周期再起動している」のと「mpg123 が繰り返し
落ちる」は、別々の不具合ではなく同じ連鎖の両端でした。

`unit_needs_start()` を、Type= だけでなく `systemctl show` の `Result=`
も見るように直しました — `Type` を `oneshot` または `forking` のどちらか
に広げつつ、**`Result` が `success` (前回の起動が実際に正常終了した)
のときだけ**「inactive は正常な終わり方」と判定します。`Type=forking`
だけを見て `Result` を確認しないと、たとえば `hostapd.service` のように
本来ずっと動き続けるべき `Type=forking` の常駐デーモンが実際にクラッシュ
した場合まで「もう仕事は終わったから inactive のままでいい」と誤判定して
しまいます (`Result` はクラッシュなら `exit-code`/`signal`/`timeout` など
`success` 以外になるため、この誤判定を防げます)。**`Type` だけの判定に
戻したり `Result` のチェックを外したりしないでください** — 同じ
「hciuart が毎周期再起動し続ける」不具合、あるいは「本当に落ちている
常駐デーモンを見逃す」不具合のどちらかに戻ります。

`sentinel-logs` にも `hciuart.service` が `inactive` のときだけ実際の
`Type`/`Result`/`RemainAfterExit` を出すようにしました。今回
「`Type=oneshot` のはず」という推測が一度外れているので、次に同じ種類の
報告が来たら CLAUDE.md #45 の教訓どおり、まずこの出力で事実を確認して
から直してください。

`unit_needs_start()` を bash のスタブ `systemctl` に差し込むテストで、
(1) hciuart 相当 (`Type=forking`/`Result=success`/実行済み) は再起動
しない、(2) 一度も実行していない同条件は 1 回だけ起動する、(3)
`Type=forking`/`Result=exit-code` (本当にクラッシュした forking デーモン)
は再起動する、(4) 既存の `Type=oneshot` の挙動は変わらない、の 4 パターン
を確認済みです。

### 52. yt-dlp はプレイリストを取得でき、進捗 ( %/ETA/曲順 ) を表示する

「プレイリスト対応と、ダウンロード中の進捗 (残り時間) を見たい」という
要望がありました。

- **`--no-playlist` を外しただけです。** yt-dlp は渡された URL が単曲か
  プレイリストかを自分で判断するため、こちら側で URL の形を見分ける
  必要はありません。単曲の URL を渡せば従来どおり 1 曲だけ取得されます。
- **`--no-progress` をやめ、`--progress-template` で機械可読な進捗行を
  自前の接頭辞 (`SENTINEL_PROGRESS|`) 付きで出させています。** yt-dlp
  本体の人間向け進捗表示はバージョンによって書式が変わりうるため
  パースの対象にしません。`info.*` (現在ダウンロード中の項目のメタ
  データ、プレイリスト内の位置を含む) と `progress.*` (その項目の
  ダウンロード進捗) は、公式ドキュメントの `--progress-template` 節が
  明記する使い分けどおりです — `info.playlist_index`/`playlist_count`
  は `-o` の出力テンプレートと同じ情報辞書から取っており、プレイリスト
  でなければ `"NA"` になります。
- **`_run_ytdlp()` を `subprocess.run()` (完了を待ってからまとめて処理)
  から `subprocess.Popen()` (1 行ずつ読みながら随時反映) に変更しました。**
  進捗行のたびに `entry["percent"]`/`entry["eta"]`/`entry["item_index"]`/
  `entry["item_count"]`/`entry["message"]` を更新するため、待つだけの
  実装では反映のしようがありません。`entry` は `DOWNLOADS` (Web UI が
  5 秒ごとにポーリングする既存の仕組み、`web/static/index.html` の
  `loadMusic()`) からそのまま参照されるオブジェクトなので、**UI 側の
  変更は不要です** — 既存の「進捗」列がそのまま更新後の `message`
  (例: `3/12曲目 45.2% 残り00:07`) を表示します。
- **進捗行そのものは失敗時のエラー表示から除外しています** (`tail` は
  進捗行以外だけを保持)。進捗行は 1 秒間に何度も流れるため、そのまま
  混ぜるとエラー発生時に本当のエラー行が埋もれます。
- **タイムアウトを 30 分から 3 時間に延ばしました。** プレイリストは
  1 曲よりずっと時間がかかりうるため、単曲向けの 30 分では長いプレイ
  リストの途中で打ち切られてしまいます。

### 53. 時報の既定間隔は 60 分ではなく 30 分にする

時報 (`voice.time_signal_loop()`、CLAUDE.md #27) の壁時計境界判定
(`(hour*60+minute)//interval` が変わった瞬間だけ喋る) 自体は元から正しく
実装されていましたが、既定の `voice_time_interval_minutes` が 60 だった
ため、鳴るのは毎時 `:00` だけで `:30` には鳴っていませんでした。「実際の
時刻の 30 分刻み (`:30`・`:00`) で鳴らしてほしい」という要望を受けて既定値
を 30 に変更しました。60 の約数でなければ壁時計の `:00` と揃わない半端な
時刻に鳴ることになるため、変更する場合は 60 の約数 (1/2/3/4/5/6/10/12/
15/20/30/60) を選んでください — この制約はコードでは強制していないので、
設定 UI のラベルにも明記しています。

### 54. 音声アナウンスは音楽を止めずに重ね、重ねている間だけ音楽の音量を下げる

CLAUDE.md #31 で dmix によるアナウンスと音楽の同時再生を実現しましたが、
「重ねられるなら、重ねている間だけ音楽の音量を自動で下げてほしい」という
要望がありました。それまでは重ねられる場合は音楽が全音量のまま流れ
続けており、アナウンスの声が聞き取りにくいことがありました。

`music.py` に `duck_volume_for_voice()`/`resume_volume_after_voice()` を
追加しました。**`duck_for_voice()` (曲を完全に停止する、dmix が使えない
機体向けの旧経路) とは別物**です — mpg123 を止めも開き直しもせず、
再生中でも即座に効く `V <percent>` リモートコマンドで音量だけを一時的に
動かすため、sudo も asound.conf の書き換えも一切経由しない軽い処理で
完結します。下げる割合は新設した `voice_duck_percent` (既定 35、100 で
下げない) で設定でき、`config.music_volume` (利用者が設定した本来の
音量) 自体は変更しません — アナウンスが終われば `resume_volume_after_
voice()` が下げる前の値へそのまま戻します。

`voice.py` の `loop()`/`speak_test()` は、`_mixing_ready()` が True
(dmix で重ねられる) なら `duck_volume_for_voice()` を、False (重ねられ
ない、曲を完全に止めるしかない) なら従来どおり `duck_for_voice()` を
呼ぶよう分岐しています。**この 2 つの経路を混同して同じ関数にまとめ
たり、`duck_volume_for_voice()` を曲の完全停止と同じタイミングで両方
呼んだりしないでください** — `duck_for_voice()` は `suspended_by` を
使って曲の停止・復帰そのものを制御しており、`duck_volume_for_voice()`
は生きたまま流れている曲の音量だけを動かす別の状態 (`_pre_duck_volume`)
を持ちます。両方を同時に呼ぶことは無いはずですが、もし呼び出し順序を
書き換える場合はこの前提を崩さないよう注意してください。

### 55. hciuart.service の「inactive は毎周期再起動」は Type=/Result= を見るのをやめて解決した

#51 (`unit_needs_start()` を `Type=oneshot` から `Type=forking` +
`Result=success` へ広げる) をデプロイしたあとも、実機の `sentinel-logs`
は `FIXED: started hciuart.service` を 2 時間49分の稼働でほぼ毎周期
(58 回) 報告し続けました。`sentinel-logs` 自身が出す診断行
(`hciuart.service Type/Result forking/no/success`) は #51 の想定どおり
`Type=forking`・`Result=success` に見えましたが、この行は 3 つのプロパティ
を 1 回の `systemctl show -p X -p Y -p Z --value` 呼び出しでまとめて
取っており、**複数プロパティを一度に要求したときの出力順が、要求した
順序どおりとは限らない** (systemd 側の内部順で返る可能性がある) ため、
実際には `Type=`/`RemainAfterExit=`/`Result=` のどれがどの値なのか
確実には読み取れていませんでした (この行自体は CLAUDE.md #51 の時点で
既に不確実な作りだったと判明)。

`Type`/`RemainAfterExit`/`Result` の組み合わせを言い当てようとする
アプローチ自体を 2 回続けて外したため、この方針をやめました。
`check_services()` は hciuart.service を「`ActiveState=failed` のとき
だけ再起動する、`inactive` では一切触らない」という、`unit_needs_start()`
を経由しない別ルートへ切り替えました。理由は単純です — hciuart が
本当に壊れているかどうかを知りたいなら、**それを実際に必要としている
機能 (`check_bluetooth()`、CLAUDE.md #12) が既に判定しています**。
`bluetoothctl show` が実際にコントローラなしを報告したときだけ
hciuart/bluetoothd を再起動する、という症状ベースの判定は Type=/Result=
のどんな組み合わせよりも直接的で、二重に (しかも的外れに) 判定する
理由がそもそもありませんでした。`check_services()` 側は「起動直後に
failed のまま止まっている」という別の既知の競合 (CLAUDE.md #12) を
拾うためだけに残しています。

**このユニットだけ `unit_needs_start()` の inactive 判定へ戻さないで
ください** — Type=/Result= をどう組み合わせても、実機の systemd が
実際に何を返すかはドキュメントの記述と一致するとは限らず (今回がまさに
それでした)、机上の想定が外れるたびに同じ「毎周期再起動」に戻ります。
`sentinel-logs` の診断行も、複数プロパティを 1 回でまとめて取る
`paste -sd/ -` 方式をやめ、`Type=`/`Result=`/`RemainAfterExit=` を
それぞれ独立した `systemctl show` 呼び出しでラベル付きに出すよう直し
ました — 今回のような「どの値がどのプロパティか分からない」不確実性
自体を無くすためです。bash のスタブ `systemctl` を使ったテストで、
(1) hciuart 相当の `inactive` 状態は `ActiveState=failed`/`active`
どちらでもない限り一切再起動されない、(2) `ActiveState=failed` のときは
確実に再起動される、の 2 点を確認済みです。

### 56. 音楽をカテゴリー (MUSIC_DIR 直下のサブフォルダ) で分け、その中だけを再生できるようにする

「勉強用 BGM」「休憩用 BGM」のようにカテゴリー分けし、そのカテゴリー
だけを再生したいという要望がありました。**カテゴリー = `music.MUSIC_DIR`
直下のサブフォルダ**という、追加のデータ構造やメタデータファイルを
持たないシンプルな実装にしています — `Player.scan()` は元々 `rglob("*")`
でサブフォルダも横断して曲を拾っていたため (フラットな一覧として)、
「サブフォルダをカテゴリーとして扱う」という解釈を足すだけで済み、
既存のライブラリ構造を壊しません。深さは 1 段だけを見ます
(`music._category_of()`) — ネストした分類までは想定していません。

- **`music.list_categories()`** が `MUSIC_DIR` 直下のディレクトリ名を
  列挙します。ドットで始まるフォルダ (隠しフォルダ) は除外します。
- **`config.music_category_filter`** (既定 "" = フィルタなし) に
  カテゴリー名を入れると、`Player.scan()` がそのフォルダの曲だけへ
  `self.tracks` を絞り込みます。**存在しない/1 曲も無いカテゴリーを
  指定した場合は全曲へ静かにフォールバックします** — 空の再生対象で
  立ち往生させるより、まず鳴らし続けることを優先しました。設定タブの
  一般設定 (GROUPS/LABELS) には出していません — 音楽タブに専用の
  `<select>` (`#m-cat-filter`) を置き、`music.py` の他の再生制御
  (シャッフル/リピートなど) と同じ「音楽タブ内で完結する」設計に揃えて
  います。
- **`music.move_track(name, category)`** が曲を実際に別のサブフォルダへ
  移動します (`category=""` で「未分類」= MUSIC_DIR 直下へ戻す)。
  カテゴリー名はファイルシステム上のフォルダ名としてそのまま使うため、
  `music._valid_category()` でパス区切り文字・先頭のドット・長すぎる
  名前を弾きます (yt-dlp の URL のような自由入力ではなく実体を作る
  検証が要る、という点は #24 の hciuart とは別文脈ですが同じ考え方)。
  移動先に同名の曲が既にあれば `FileExistsError` で止め、無言で
  上書き/データ消失させることはしません。
- **`music.find_track_path(name)`** を新設しました。カテゴリー分け導入
  前は「曲名 = MUSIC_DIR 直下のファイル名」で済んでいたため、
  `/api/music/track` (削除) は `config.MUSIC_DIR / name` を直接組み
  立てていましたが、これはサブフォルダ内の曲を「見つかりません」と
  誤って 404 にしてしまいます。`find_track_path()` は既にスキャン済みの
  `PLAYER.tracks` (全カテゴリーを横断済み) から曲名で探すため、
  どのカテゴリーにあっても正しく見つかります。**削除ルートを
  `config.MUSIC_DIR / name` の直接組み立てに戻さないでください** —
  同じ「カテゴリー内の曲が消せない」不具合に戻ります。曲名を一意な
  キーとして扱う前提そのものは、既存のイコライザー曲別設定
  (`music_eq_track_overrides`、CLAUDE.md #32) と同じものを踏襲して
  います — 同名ファイルが複数カテゴリーに存在する場合は最初に見つかった
  ものを返す、という仕様も含めて EQ 上書きの前提と揃えています。
- **yt-dlp のダウンロード自体もカテゴリーを直接指定できます**
  (`music.enqueue_download(url, category)` → `_run_ytdlp()` の `-o`
  テンプレートの保存先ディレクトリを切り替えるだけ)。あとから
  `move_track()` で仕分けるのではなく、取得時点で仕分け先を選べる
  ようにするためです。フォルダがまだ無ければ `mkdir(parents=True)` で
  作ります。

### 57. `sentinel-fix-audio-output.sh` の asound.conf 再試行は固定 1 時間ではなく指数バックオフにする

実機の `sentinel-logs` で、カメラの USB 転送エラー (`uvcvideo ... Failed to
resubmit video URB`) と同じ時間帯に `sentinel-fix-audio-output.sh` が
`/etc/asound.conf` を `.broken` へ退避したログが見つかりました。これ自体は
#46 の設計どおり正しい安全策 (開けない dmix を放置すると bluealsa-aplay
まで巻き込んで機体全体の音が死ぬため) ですが、`may_retry()` がこの種の
書き直しを**固定 1 時間**のクールダウンでしか許していませんでした。この
とき `hw:$CARD,0` 自体は問題なく開けており (`sentinel-logs` の `open
hw:0,0 ok`)、壊れていたのは asound.conf の側だけ — つまり USB の瞬間的な
輻輳で一度こけただけの、次のサイクルで直る見込みが高い故障でした。それを
固定 1 時間放置するのは過剰に保守的で、「ミキシングが直らない」という
報告に直結していました。

`may_retry()` を camera.py の `_CORRUPT_RECONNECT_MAX_BACKOFF` (CLAUDE.md
#19) と同じ考え方の指数バックオフに変えました。`/run/sentinel-audio-<key>`
に「最終試行時刻 失敗回数」を書き、待ち時間を 2 分 (`_AUDIO_RETRY_BASE_SEC`)
から失敗のたびに倍々に伸ばし、1 時間 (`_AUDIO_RETRY_MAX_SEC`) を上限に
します。これにより、次の Guardian 周期 (2 分後) にはもう再試行でき、
本当に壊れているカードだけが従来どおり長い間隔まで後退します。修理が
実際に効いた (`pcm_opens sentinel_music` が成功した) ときは `reset_retry()`
でスタンプを消し、次の障害は再び短い間隔から始まるようにしています —
無関係な過去の失敗streakを引き継いで長く待たされることを防ぐためです。
**この指数バックオフを外して固定クールダウンに戻さないでください** —
`hw:$CARD,0` は正常なのに asound.conf の再構成だけ最大 1 時間放置される、
同じ「ミキシングが直らない」不具合に戻ります。

### 58. 時報は 0 分のとき「〜時0分」と言わない。同時に鳴らせる効果音オプションを追加

**0 分の言い方**: `voice.py` の時報テンプレート既定文 `{hour}時{minute}分
です` は、ちょうど正時に「12時0分です」という不自然な言い方になっていま
した。`time_signal_loop()` が新たに `{minute_part}` (0 分のときだけ空
文字、それ以外は `"{分}分"`) を組み立てて渡すようにし、既定テンプレート
を `{hour}時{minute_part}です` に変更しました。`{minute}` (生の数値) は
そのまま渡り続けるので、自前のテンプレートで `{minute}分` を使い続けたい
場合も壊れません。

**同時に鳴らす効果音**: `voice_chime_enabled` (既定 False) を追加しました。
有効かつ dmix でミキシングできる場合 (`_mixing_ready()`)、時報カテゴリの
読み上げだけ、短いチャイム (A5→E6 の 2 音、Pillow のフォント描画と同じ
「バイナリ音源を同梱しない」考え方で、標準ライブラリの `wave`/`math` だけ
で毎回同じ波形を合成し `config.RUNTIME` (tmpfs) にキャッシュする) を
`_speak_sync()` の TTS 合成・再生と**並行**に鳴らします。`subprocess.Popen`
で非同期に開始し、TTS の合成・再生が終わったあと `finally` で `wait()`
して回収するだけなので、チャイムの再生終了を待ってから喋り始めることは
ありません。**ミキシングできない (曲を完全に止める) 経路では鳴らしません**
— そちらは排他デバイスの奪い合いを増やすだけで「同時に」を満たせない
ため、Bluetooth 接続中の読み上げスキップ (#27) と同じ「重ねられないなら
無理に鳴らさない」方針を踏襲しています。チャイムは時報カテゴリ限定です
— キューの要素を `text` だけから `(text, category)` のタプルに広げ、
`loop()` 側で `category == "time"` のときだけ有効にしています。

### 59. 定時処理 (動画生成・再起動) が永久に止まる不具合 — `web/routes.py` の `create_task()` 参照未保持

実機で「定時処理の動画生成が止まり、それに伴って再起動もされない」と
報告がありました。`maintenance.py` の `run_now()` 自体は正しく書かれてお
り (`try/finally` で `STATE["running"]` を必ず戻し、再起動はその外側)、
`loop()` も壁時計に沿って毎日 `run_now()` を呼ぶだけの単純な作りです。
原因は別の場所、`web/routes.py` の `/api/maintenance/run` (Web UI の手動
実行ボタン) にありました:

```python
asyncio.create_task(maintenance.run_now(reboot=reboot))
return {"ok": True, "message": "定時処理を開始しました"}
```

まさに CLAUDE.md #29 で一度踏んだ「`asyncio.create_task()` の戻り値を
どこにも保持せず捨てる」バグそのものです。#29 の修正は `main.py` に
`_background_tasks` という強参照の集合を追加しましたが、これは
`main.py` 内で `create_task()` する箇所しか救っておらず、`web/routes.py`
のこの箇所 (と `/api/system/reboot` の手動再起動 `go()`) は別ファイルで
独立に同じ間違いを踏んでいたため素通りしていました。HTTP ハンドラは
レスポンスを返した時点で呼び出し元のスタックフレームが消えるため、
`create_task()` が返す `Task` を握っている強参照がどこにも残らず、
`run_now()` の実行中 (ffmpeg のタイムラプス生成など、複数の `await` を
挟む長い処理) に GC がタスクを回収してしまうことがあります。回収される
と `try/finally` の `finally: STATE["running"] = False` まで到達せず、
`STATE["running"]` が `True` のまま永久に固定されます。以後、4 時の定時
ループが呼ぶ `run_now()` も、次に誰かが手動実行ボタンを押しても、すべて
「すでに実行中です」で即座に空振りするだけになり、その空振りの中には
動画生成もその後の再起動判定 (`run_now()` の中でしか行われない) も含ま
れないため、両方が同時に永久停止します。報告どおりの症状と一致します。

`web/routes.py` にも `main.py` と同じパターンの `_background_tasks` 集合
と `_spawn()` ヘルパーを追加し、`/api/maintenance/run` と
`/api/system/reboot` の両方の `create_task()` をこれ経由に変えました。
**この参照保持を外して `create_task()` の戻り値を再び捨てる実装に戻さ
ないでください** — 同じ「定時処理が永久に固まる」不具合に戻ります。
`main.py` の `_background_tasks` とは意図的に別の集合にしています —
モジュールをまたいでグローバルな可変集合を共有させる理由がなく、
「`create_task()` する側のモジュールが自分の分の参照を持つ」という
CLAUDE.md #29 の原則をファイル単位でも素直に守るためです。**新しい
fire-and-forget な `create_task()` を `web/routes.py` に書くときも、
必ずこの `_spawn()` を経由してください** — 経由しない呼び出しはこの
節と同じ「レスポンスを返した瞬間に参照が消える」穴に落ちます。

### 60. カメラ破損エスカレーションに「完全切断」の中間段階を追加し、Pi 再起動の頻発を防ぐ

#22 で追加したカメラ破損の緊急再起動エスカレーションについて、「破損が
続くと数分おきに Pi 本体の再起動が繰り返される」という報告がありました。
原因は算数で説明がつきます。強制再接続 (プロセス内で `release()` して
すぐ開き直すだけ、数百ミリ秒) の再試行間隔は `_CORRUPT_RECONNECT_COOLDOWN`
(20 秒) から失敗のたびに倍々に伸び `_CORRUPT_RECONNECT_MAX_BACKOFF`
(300 秒) で頭打ちになりますが (#19)、`_CORRUPT_REBOOT_THRESHOLD` (既定 4)
に到達するまでの累計時間は 20+40+80+160 = 300 秒、たった 5 分です。真の
原因が USB 帯域の逼迫のような「秒単位の間隔では解消しない」ものだった
場合、この程度の待ち時間で解消する見込みは薄く、それでも 5 分おきに
Pi 本体の再起動という最も重い手段へ直行していました。

強制再接続 (プロセスの開き直し) と Pi 本体の再起動 (最終手段) の間に、
「カメラを数分間まるごと切断する」中間段階を挟みました。`_worker()` に
`disconnect_until` (この時刻までは `open_cam()` を一切呼ばない) と
`extended_disconnect_tried` (今のエスカレーションサイクルで中間段階を
試したかどうか) を追加し、forced_reconnect が `corrupt_reboot_threshold`
回に達したとき:

- まだ中間段階を試していなければ、`corrupt_disconnect_seconds` (既定 180
  秒、カメラごとに上書き可能) だけカメラを完全に切断し (`cap = None` の
  ままループを回し続けるだけで、`open_cam()` を呼ばない)、
  `corrupt_unresolved_reconnects` を 0 に戻してから通常の強制再接続の
  カウントをやり直す。ステータスは `state="disconnected"` として公開
  する (Web UI は既知の状態でなくてもそのまま文字列表示するので、
  追加の翻訳テーブルは不要 - `renderCams()` 参照)。
- すでに中間段階を試していて、それでもまた閾値に達した場合だけ、従来
  どおり `corrupt_reboot_request` を書いて Pi 本体の再起動を要求する。

破損が実際に解消した (直近 `_CORRUPT_HIST_LEN` 枚が丸ごと正常、#22 の
「本当に解消した」判定と同じ基準) ときは `extended_disconnect_tried` も
リセットします。これにより、しばらく正常に動いたあとにまた破損が始まった
場合は、いきなり再起動要求ではなく中間段階からやり直します。逆に Pi 再起
動を要求した直後もこのフラグと `corrupt_unresolved_reconnects` をリセット
しています — 万一 sudoers の設定漏れなどで実際には再起動されなかった
場合 (#22 の「クールダウンを外さない」理由と同じ懸念)、次のエスカレー
ションでもまた中間段階から入り、そこでも直らなければ再度再起動を要求する
形で、諦めたままにはなりません。

`corrupt_disconnect_seconds` (既定 180) を `CAMERA_OVERRIDE_KEYS`・
`core/config.py` の `DEFAULTS`/`_RANGES`・`web/static/index.html` の
`GROUPS`/`LABELS`/`CAM_SETTING_LABELS` すべてに追加しています (CLAUDE.md
#30 の監査観点)。**この中間段階を外して forced_reconnect の閾値到達から
直接 Pi 再起動を要求する実装に戻さないでください** — 同じ「数分おきに
Pi が再起動を繰り返す」不具合に戻ります。cv2 をスタブに差し替えて
`_worker()` を別スレッドで走らせるテストで、(1) 閾値到達時にまず
`disconnected` 状態へ入り即座には再起動要求を書かないこと、(2) 切断期間
中は一切カメラを開こうとしないこと、(3) 切断・再接続後もなお破損が続く
場合にのみ `corrupt_reboot_request` を書くこと、の 3 点を確認済みです。

### 61. Obsidian の同期は Syncthing ではなく Lockstep Sync で行う

#40/#41/#45 で Syncthing を選んだ理由 (CouchDB が 32bit ARM に対応しない、
64bit でも公式ガイドの前提 RAM がこの Pi には多すぎる) は変わっていませ
んが、[Lockstep Sync](https://community.obsidian.md/plugins/lockstep-sync)
という Obsidian コミュニティプラグインへの切り替えを依頼されました。
Syncthing はファイル単位の同期 (Vault の実体をそのまま複製する) だった
のに対し、Lockstep Sync は Obsidian プラグインが Vault をエンドツーエンド
暗号化してから小さな自前サーバーへ中継する方式で、Pi 自身は暗号化された
まま (=読めない) データしか持ちません — 「Pi が盗まれても Vault の中身は
守られる」「Pi 側にプラグインを入れる必要がない (サーバーは単なる中継)」
という性質の違いがあります。

**このリポジトリからは切り替えられません。** Lockstep Sync 自体は
Obsidian 側の話で、各端末にプラグインを入れてもらう必要があります。この
節が扱うのは、Pi 側で動く *サーバー* のセットアップと、それに伴う
Syncthing の撤去だけです。

**32bit ARM (armhf) には導入できません。** Lockstep Sync のサーバー
バイナリは `amd64`/`arm64` しか配布されておらず (`ops/install.sh` の
アーキテクチャ判定に `armv7`/`armhf` は一切登場しません)、CouchDB を
却下したのとまったく同じ制約に、今度はこちら側で引っかかります。
`SETUP.md` の Phase 0 は「ARMv7 または ARM64」と両対応で書かれており、
どちらを導入したかは個々の機体次第です。`bootstrap.sh` は `uname -m` で
判定し、対応しないアーキテクチャでは黙ってスキップして理由を表示します
(32bit の機体では Obsidian 同期が未設定のまま残るだけで、他の機能には
一切影響しません)。**この判定を外して 32bit でもダウンロードを試みる
実装にしないでください** — 存在しない `sync-server-linux-arm` を
延々とダウンロードし続けるだけの無意味な失敗ループになります。

**Syncthing の完全撤去。** `bootstrap.sh` が STEP 9 でパッケージ
(`dietpi-software uninstall 50`) を、`install.sh` が STEP 5 で
外部ドライブへのバインドマウント (`$STORAGE/syncthing` ↔
`/mnt/dietpi_userdata/syncthing`、CLAUDE.md #40) を、それぞれ撤去
します。`scripts/sentinel-fix-syncthing-mount.sh`/
`sentinel-fix-syncthing-gui.sh` と、Guardian の `check_syncthing_storage()`/
`check_syncthing_gui()` は削除しました。Syncthing 自身の内部データベース
(`$STORAGE/syncthing`) と、それまで同期されていた実ファイル
(`$STORAGE/obsidian`) はどちらも自動削除していません — 前者は単なる
インデックスで消して構いませんが、後者はユーザーの Vault そのものなので、
勝手に消さず「まだ残っています、バックアップや削除は手動で」と案内する
だけに留めています。

**サーバーは `sentinel` ユーザーとして動かします (専用ユーザーは作らない)。**
Syncthing のときは DietPi がテンプレートする `syncthing.service` の
`ExecStart` を書き換えられなかったため、Syncthing 自身のユーザー
(`dietpi` あるいは `syncthing`、機体によって違う) を `sentinel` グループへ
加えて `$STORAGE` への既存アクセスを共有する、というかなり込み入った
迂回策が必要でした。しかもその過程で「2 人のユーザーが同じ exFAT/NTFS
マウントの `uid=`/`gid=` を取り合い、全サービスが停止する」という実機
障害を実際に起こしています (CLAUDE.md #40)。Lockstep Sync の systemd
ユニットはこのプロジェクト自身が書く (`systemd/sentinel-lockstep-sync.service`
を `install.sh` が `$STORAGE`/`$SVC_USER` で埋めてから配置する) ので、
DietPi が生成する何かを迂回する理由がそもそもありません。データ
ディレクトリ (`$STORAGE/lockstep-sync`) はコマンドラインで直接指定する
だけの、ただのパスです。ここで新しいユーザーを増やして同じ問題を
再現する理由はないと判断し、`$STORAGE` への書き込みが既に
`sentinel-fix-storage-owner.sh` (STEP 4) で保証されている `sentinel`
ユーザーとして動かすことにしました。**この判断を、Syncthing のときと
同じ「専用ユーザー + グループ共有」パターンへ戻さないでください** —
今回はそもそも迂回する対象(DietPi 製 ExecStart)が無いので、専用ユーザー
を増やすことは複雑さを増やすだけで、CLAUDE.md #40 と同じ「2 ユーザーが
同じマウントを取り合う」不具合を作り込むリスクだけが残ります。

**到達性は Sentinel の Web UI (`:8080`) と同じ、LAN 内で開放です。** サーバー
は `0.0.0.0:8384` で待ち受けます。当初は AdGuard の `:8083` と同じ
「ループバック + `tailscale0` 以外を DROP」パターンで Tailscale 経由に
限定していましたが、「Tailscale の外 (素の LAN) でも同期できるように
してほしい」という要望で撤去しました — 経緯と設計は #62 を参照して
ください。

**ペアリングは Web GUI ではなくコマンドライン。** Syncthing の GUI
(`:8384` のブラウザ画面) に相当するものが Lockstep Sync には無く、
`sync-server link`/`token add` サブコマンドが端末ごとのペアリングリンク・
トークンを都度発行する方式です。`setup.sh` H8 は Tailscale の実際の IP
(`tailscale ip -4`、H7 で接続済みであることが前提) を埋め込んだこれらの
コマンドをその場で組み立てて表示し、初回端末用には `qrencode` (新規
apt 依存、CLAUDE.md「依存を増やさない」の範囲内の小さなツール) で
QR コードも表示します。**この一連のコマンドを、存在しない Web GUI へ
誘導する案内に書き換えないでください** — Syncthing と違い、この
プラグインには覗きに行けるポート/画面がそもそもありません。

### 62. Lockstep Sync も Web UI と同じく LAN 内で到達可能にする (Tailscale 限定をやめる)

#61 では Lockstep Sync のサーバーを AdGuard の `:8083` と同じ考え方で
Tailscale 経由限定にしていましたが、「Tailscale の外 (素の LAN) でも
同期できるようにしてほしい」という要望がありました。

AdGuard の `:8083` を厳しく絞っているのは、あちら固有の理由 (実機での
401 トラブルの経緯、CLAUDE.md #5) があってのことで、Lockstep Sync に
同じ理由はそもそもありません。むしろ Sentinel 自身の Web UI (`:8080`) が
最初から LAN 全体に開いている (パスワードは設定できるが、到達性そのもの
はネットワーク層で絞っていない) のが、この機体の既定の信頼境界です
(CLAUDE.md「意図的にしていないこと」— HTTPS を張らない・LAN 内運用が
前提)。Lockstep Sync だけこれより厳しくする理由は無かったと判断し、
`:8080` と同じ扱いに揃えました。

`sentinel-guardian.sh` の `check_lockstep_firewall()` から、DROP ルールを
**適用する**コードを削除し、代わりに (もし過去のバージョンを実行していた
機体に) 残っているかもしれない DROP ルールを**取り除く**だけの関数に
書き換えました。iptables ルールはカーネルメモリ上にしか無く再起動で消え
ますが、逆に「ルールを追加するのをやめる」だけでは、既にルールが入って
しまっている機体では次の再起動まで LAN から到達できないままになります。
Guardian は 2 分ごとに動くので、この能動的な削除によって次の周期までに
古い制限が外れます。**この関数に DROP ルールを追加するコードを書き
戻さないでください** — 同じ「LAN から到達できない」状態に戻ります。

サーバー自体は元々 `0.0.0.0:8384` で待ち受けていた (#61 の時点でファイア
ウォール層だけが絞っていた) ため、`install.sh`/`systemd/sentinel-lockstep-
sync.service` 側の変更は不要でした。

**ペアリングは端末ごとに使うアドレスを選びます。** `link`/`token add` の
`--url` に渡したアドレスがその端末に埋め込まれるため、1 つのアドレスで
「家でも外でも同期できる」という単純な話にはなりません。家から出ない
端末には LAN の IP (`hostname -I`)、外出先からも同期したい端末には
Tailscale の IP (`tailscale ip -4`、H7 で接続済みが前提) を使うよう、
`setup.sh` H8 は両方のアドレスを表示し、どちらを使うか利用者に選ばせます
(Tailscale 未接続なら LAN のアドレスしか出しません)。**この「端末ごとに
アドレスを選ぶ」前提を外して、片方のアドレスだけを常に案内する実装に
戻さないでください** — 外出先用の端末を LAN アドレスでペアリングしてし
まうと、家を出た瞬間に同期できなくなります。

**`http://<Pi の IP>:8384` をブラウザで開くと 404 になるのは正常です。**
Lockstep Sync にはそもそも Web GUI が無く (#61 参照)、サーバーが応答する
のはプラグイン/CLI が話す API エンドポイントだけです。ブラウザでルート
パスを開いて 404 が返るのは「サーバーは動いていて、ちゃんと応答して
いる」ことの確認にはなっても、故障のサインではありません。実際に届く
かどうかを確認したいときは `curl -i http://<Pi の IP>:8384/` のステータス
コードではなく、`setup.sh` H8 の `link`/`token add` コマンドが正常に
実行できるか、または `sudo ss -ltn 'sport = :8384'` で `0.0.0.0` に
listen しているかで判断してください。

### 63. Lockstep Sync のペアリングリンクは「プラグインに貼るもの」ではなく「先にプラグインを入れた端末でブラウザから開くもの」

「`This link has expired or was already used` が毎回出る」「Obsidian の
プラグイン設定に Server URL / Device token の欄はあるが、リンクを貼る欄が
無い」という報告があり、`setup.sh` H8 の案内文自体が誤解を招く書き方をして
いたことが分かりました。

upstream の [obsidian-lockstep-sync](https://github.com/stephansergeev/obsidian-lockstep-sync)
サーバー実装 (`server/cmd/sync-server/link.go`、`server/internal/auth/auth.go`、
`server/internal/api/join.go`) を直接確認したところ、ペアリングの実際の
流れは次のとおりでした。

1. `link` コマンドは `{サーバーURL}/join/{使い捨てコード}` という**Webページ
   のURL**を1行だけ表示します。これは Obsidian プラグインへ貼り付ける値
   ではなく、**そのまま開くべきリンク**です。
2. このページの HTML には、Obsidian プラグインへ処理を引き継ぐための
   `obsidian://lockstep-setup?...` というカスタムURLスキームへのリンクが
   埋め込まれています。ブラウザがこのリンクを開こうとすると、OSが
   Obsidian アプリへ制御を渡し、**Obsidian 側で動いている Lockstep Sync
   プラグインが実際のペアリング処理 (`RedeemJoin`) を行い**、成功すれば
   その端末の Server URL / Device token をプラグインが自動的に設定します。
   つまり**「Server URL」「Device token」欄は利用者が手で埋める入力欄では
   なく、この自動引き継ぎの結果が書き込まれる欄**です。「貼り付ける欄が
   無い」というのはその意味で正しい観察でした。
3. サーバー側の `RedeemJoin` はコードのハッシュ・有効期限・`used_at=0`
   (未使用) の3条件を1本の `UPDATE ... WHERE hash=? AND used_at=0 AND
   expires_at>=?` で同時に確認するアトミックな処理で、1行も更新されなければ
   (=期限切れ、存在しない、またはすでに `used_at` が立っている) 一律で
   `"join code is invalid, expired or already used"` を返します。**ここが
   実際のエラー文言の発生元**です。**引き継ぎが一度でも試みられた時点で
   コードは即座に使用済みになります** — その後の処理 (プラグイン側の鍵
   生成やAPI疎通) が何らかの理由で失敗しても、コード自体は戻りません。

ここから分かる、実機で起きていたと考えられる不具合は次の2つです。

- **Obsidian 側にまだ Lockstep Sync プラグインを入れていない/有効化して
  いない状態でリンクを開いていた。** この状態では `obsidian://` の引き継ぎ
  を受け取る相手がそもそも存在しないため、成功しようがありません。にも
  かかわらず、ページ側がこの引き継ぎを試みる (または利用者が Server URL
  欄を自分で手動入力して迂回しようとする) たびに、コード自身は「使用済み」
  として消費され続けます。**プラグインの導入・有効化を、リンクを開くより
  前に済ませる必要があります。**
- `setup.sh` H8 の案内文が「Paste it into the Lockstep Sync plugin's
  settings in Obsidian on that device, or scan it as a QR code instead」
  (プラグインの設定に貼り付けてください) となっており、**実際の仕組み
  (ブラウザで開く → プラグインへ自動引き継ぎ) と食い違っていました**。
  この案内を信じて Server URL 欄に手でアドレスだけを入力しても、Device
  token 欄を埋める手段がどこにもなく、ペアリングは成立しません。

`setup.sh` の H8、`SETUP.md`/`SETUP.ja.md` の該当節・トラブルシューティング
表を、「プラグインを先に入れる」「リンクは貼るのではなくブラウザで開く」
「一度引き継ぎを試みた時点でそのリンクは使い捨てられる」という実際の仕組み
に合わせて書き直しました。**この案内文を「プラグインの設定に貼り付けて
ください」という表現に戻さないでください** — 貼り付ける欄がそもそも存在
しない以上、同じ「使えない設定欄を探して迷う」報告に戻ります。

なお `CreateJoin()` (サーバー側で新しいリンクを発行する処理) は、呼ばれる
たびに期限切れ・使用済みの古いコードを掃除しますが、**同じ vault+device
名に対して複数回発行しても、未使用の古いリンクを無効化するわけではありま
せん** — 複数回実行してしまっていたこと自体は今回の症状の原因ではない、
という点もこの調査で切り分けられています。**「複数回発行すると衝突する」
という前提で新たな重複排除ロジックを足さないでください** — サーバー側の
実装を確認した限り、その前提自体が誤りです。

### 64. Bluetooth ペアリング: 相手端末の PIN/確認画面は確認するもの。Pi 側 agent が確認プロンプトに無応答だと失敗する

「各機器で PIN 番号が出て、無視してスキップしてもペアリングできませんでした
と表示される」という報告がありました。まず利用者側の操作として明確にして
おきたいのは、**相手端末 (スマホ/PC) に出る確認画面は無視・スキップする
ものではなく、確認して「ペア設定/OK」を押すもの**だという点です。Pi 側の
capability を NoInputNoOutput (無表示・無確認) にしていても、これは
「Pi 側の agent が何を求められても自動で答える」という申告でしかなく、
Bluetooth の SSP (Secure Simple Pairing) は相手側の端末が独自に確認画面を
出すこと自体は妨げません — 実際、CLAUDE.md #16 が参照した既知のリグレッ
ション報告 ([RPi-Distro/repo#291](https://github.com/RPi-Distro/repo/issues/291)
のコメント、[Raspberry Pi Forums](https://forums.raspberrypi.com/viewtopic.php?t=324225))
でも「(NoInputNoOutput でも) 確認は要求され続ける」「相手端末に出る PIN は
無視せず yes を押す」という報告があり、これはこのプロジェクト固有の不具合
ではなく Bluetooth スタック側の一般的な挙動です。

とはいえ、それだけでは説明のつかないもう一つの穴がありました。#16 の
`sentinel-bt-agent.sh` は bluetoothctl の対話セッションを `coproc` で
模倣していますが、監視していたのは `Connected: yes`/`Paired: yes`/
`Bonded: yes` という**接続確立後**のイベント行だけで、ペアリングの過程で
**Pi 側の agent 自身に** `RequestConfirmation`/`RequestAuthorization` 等が
飛んできて `Confirm passkey NNNNNN (yes/no):` のようなプロンプトが
標準出力に出た場合には、一切応答していませんでした。この状態では
Pi 側からの応答がいつまでも来ないため、相手端末の確認画面をどれだけ
正しく操作しても (無視してもきちんと押しても)、最終的に
`AuthenticationTimeout` でペアリングが失敗します。「PIN を無視/スキップ
してもペアリングできない」という報告は、利用者側の誤操作 (無視すべきで
なかった) と、この Pi 側の無応答という 2 つが重なっていた可能性が高いと
判断しました。

`sentinel-bt-agent.sh` の読み取りループに、`(yes/no)` を含む行が来たら
無条件で `yes` を返す処理を追加しました。`Confirm passkey`・
`Confirm pairing`・`Authorize service` はいずれも同じ `(yes/no)` という
書式でプロンプトを出すため、文言ごとに個別分岐する必要はありません。
**この自動応答を外さないでください** — 同じ「相手端末には確認画面が出て
いるのに、Pi 側の agent が無応答のまま固まってペアリングに失敗する」
不具合に戻ります。この Pi 側の agent が NoInputNoOutput で登録されている
以上、真の Just Works が成立する組み合わせでは本来この分岐は素通りする
だけで、実害はありません。

### 65. `sentinel-fix-audio-output.sh` は `sentinel_music` しか実際に開いて確認していなかった

「音声ガイダンスと音楽のミキシングがいまだにできない」という報告があり
ました。CLAUDE.md #31/#41/#46/#57 と手を尽くしてきましたが、確認すると
`sentinel-fix-audio-output.sh` の「実際に開けるか試す」検証区間 (#4) が
`sentinel_music` **だけ**を対象にしており、`sentinel_voice` は一度も
実際に開いて確かめていませんでした。この2つは同じ `/etc/asound.conf` の
同じ `sentinel-setup-audio-mixing.sh` 呼び出しで一緒に書かれますが、
`sentinel_music` は素通し (`type plug`) または LADSPA 段を経て dmix へ
繋がるだけなのに対し、`sentinel_voice` は独自の "SentinelVoice" という
ALSA softvol コントロールを新規に持つ、実体の異なる PCM 定義です
(CLAUDE.md #31)。同じ dmix スレーブ・同じカードを使っていても、
`sentinel_music` が正常に開けることは `sentinel_voice` が開けることを
何も保証しません。

この非対称性のせいで、`sentinel_music` が正常な限り Guardian は「音声
出力は健康」と報告し続ける一方、`voice.py` の `_mixing_ready()` は
独自に `sentinel_voice` を試して失敗し続け、**誰にも修復されないまま**
`duck_for_voice()` (曲を完全に止めてから喋る、重ねない) へ静かに
フォールバックし続けていました。「音楽は普通に鳴るのに、なぜか読み上げ
と重ならない」という症状と一致します。

`sentinel-fix-audio-output.sh` に `sentinel_music` と全く対称な
`sentinel_voice` 専用の検証・修復区間を追加しました。開けなければ
`hw:$CARD,0` 自体が開けるかで「カード/dmix スレーブの問題」か
「`sentinel_voice` 自身の定義 (softvol コントロール) の問題」かを切り分け、
後者なら `sentinel-setup-audio-mixing.sh` を再実行して asound.conf を
書き直します。再試行のバックオフ (CLAUDE.md #57 の指数バックオフパターン)
は `sentinel_music` 用のものと**別のキー**(`voice_rewrite`) を使います —
同じキーを共有すると、無関係などちらかの失敗streakがもう一方の再試行
間隔を不当に伸ばすことになるためです。**この `sentinel_voice` 専用の
検証を外して `sentinel_music` の結果だけで「音声出力は健康」と判定する
実装に戻さないでください** — 同じ「音楽は鳴るのに読み上げとは重ならない」
不具合に戻ります。

### 66. `JustWorksRepairing` を `always` にする — 既知の端末の再ペアリングが無条件で確認画面に戻される

#64 で「以前は別のプロジェクトで PIN 画面が出なかった」という比較報告が
ありました。原因を1つに断定できる証拠はありませんが (利用者もその別
プロジェクトの実装詳細は覚えていない、この節は「確度は中程度だが実害の
無い、documented な改善」として追加しています)、bluetoothd の main.conf
を実際に確認したところ、明確に疑わしい既定値が見つかりました。

BlueZ の `main.conf` には `JustWorksRepairing` という設定があり、
[アップストリームの設定ファイル](https://github.com/Vudentz/BlueZ/blob/master/src/main.conf)
はこう説明しています。

> "Specify the policy to the JUST-WORKS repairing initiated by peer.
> Possible values: 'never', 'confirm', 'always'. Defaults to 'never'"

既定 (`never`) は、**すでにペアリング済みの端末が Just Works で再接続
しようとしても、bluetoothd がそれを無条件で拒否する**という意味です。
拒否された端末は「新規ペアリング」からやり直すしかなく、これは確認画面
(PIN/パスキー表示) が出る通常のフローを通ります。

この Pi は Guardian の自己修復で `bluetooth.service` が再起動されうる
構成です (CLAUDE.md #12/#45/#55)。`bluetoothd` が再起動されるたびに、
**すでに一度ペアリング済みの端末**が再接続しようとした際にこの
`JustWorksRepairing=never` の壁に当たり、毎回「新規ペアリングと同じ確認
画面」を見せられていた可能性があります。「以前は出なかった PIN が今回は
出る」という体感とも矛盾しません — この Pi は他のどのプロジェクトより
bluetoothd の再起動頻度が高い設計だからです。

`install.sh` の main.conf 書き換えループに `JustWorksRepairing=always`
を追加しました。これにより、一度ペアリング済みの端末は
bluetoothd が再起動されたあとでも確認画面なしで静かに再接続できるように
なります。**この設定を外さないでください** — 同じ「既知の端末なのに
毎回確認画面が出る」不具合に戻ります。

**ただし、これは「初回ペアリング」時の確認画面には効きません。**
`JustWorksRepairing` は名前のとおり「再 (re-) ペアリング」だけが対象で、
まだ一度もペアリングしたことのない端末の最初の確認画面 (#64 で対処した、
Pi 側 agent への応答と、相手端末側で表示される確認自体) には無関係です。
初回ペアリングの確認画面がなぜ出るか (SSP のネゴシエーションの問題か、
単に相手端末 OS 自体の UX 仕様か) は、この節の時点でも断定できておらず、
実機の `sentinel-logs`/`journalctl -u bluetooth` で実際のネゴシエーション
ログを見ないと、これ以上の切り分けはできません。

### 67. mpg123 が plughw フォールバックを掴み続けると、dmix は二度と復旧できない

実機の `sentinel-logs` で、`sentinel_music`/`sentinel_voice` の実開テストが
どちらも `aplay: pcm_write:2178: write error: Interrupted system call` で
失敗し続けているのに、`hw:0,0` 単体は開ける、という報告がありました。この
エラー文言自体は #19/#46 で見てきた `unable to open slave`/`Invalid
argument` とは種類が違い、当初は原因不明でした。

同じログの `mpg123` 行が手がかりでした — `mpg123 -o alsa -R --buffer 1024
-a plughw:0,0`。**mpg123 が dmix (`sentinel_music`) ではなく
`hw:0,0` を直接掴んでいました。** `music.py` の `_spawn()` は元々
`_mixing_ready()` が偽なら `plughw:<card>,0` へフォールバックする設計
(CLAUDE.md #37/#46) で、これ自体は正しい段階的劣化です。しかし ALSA の
`hw` (`plughw` 経由でも実体は同じ) は排他デバイスで、**dmix は自分の
スレーブとして同じ `hw:0,0` を掴もうとするため、mpg123 が直接それを
開いている間は dmix 側が永久に開けません**。つまり一度何らかの理由で
dmix が壊れて mpg123 がフォールバックへ落ちると、以後
`sentinel-fix-audio-output.sh` がどれだけ `/etc/asound.conf` を書き直して
検証テストを走らせても、**その検証テスト自身が mpg123 に阻まれて必ず
失敗する**という自己再生産のループになっていました。実機で見えていた
"Interrupted system call" は、この検証用 `aplay` が `hw:0,0` の空きを
待って `open()` 内で長時間ブロックし、`timeout 6` が痺れを切らして
SIGTERM を送った結果です。

`music.py` の `loop()` (mpg123 復帰の自己修復ループ) は、これまで
「mpg123 プロセスが実際に落ちたとき」しか `sentinel_music` を再評価
しませんでした。フォールバックで**生きたまま**鳴り続けている限り、この
再評価が一生発火しないため、dmix 側がどれだけ直っても mpg123 は気付けず
に直接出力を握り続けていました。5 分おきに「`audio_mixing_enabled` が
有効・`alsa_device` の明示指定なし・現在 `plughw:` へフォールバック中・
mpg123 生存中」の場合だけ、実際に `restart_playback()` (mpg123 を止めて
`hw` を手放してから再度 `play()` する、既存の関数) を呼んで dmix が
使えるようになっていないか試すようにしました。**mpg123 を止めずに
`_mixing_ready()` だけ呼ぶ実装には絶対に戻さないでください** —
`hw:0,0` を握ったままではテスト自体が同じ理由で失敗し続けます。復旧に
毎回失敗する環境で 5 分おき無期限に再生が瞬断するのを避けるため、
失敗のたびに次回までの間隔を倍々に伸ばす指数バックオフ (上限 1 時間、
camera.py の `_CORRUPT_RECONNECT_MAX_BACKOFF` と同じ考え方、CLAUDE.md
#19) を掛け、実際に dmix へ切り替われた瞬間に短い間隔へ戻します。

`sentinel-logs` にも、`mpg123` の実際のコマンドラインに `-a plughw:` が
含まれる場合はその旨を明示する行を追加しました。今回この一致に気付くまで
時間がかかったので、次に同じ症状が来たら `sentinel_music`/`sentinel_voice`
の開テストが失敗している行の直前に、まずこの `mpg123 output` 行を見て
ください。**このヒントを外さないでください** — 同じ「エラーの種類だけ
見て原因不明のまま `.broken` へ退避し続ける」切り分けの遠回りに戻ります。

### 68. 時報の効果音は `voice_chime_path` で任意のファイルを指定できる

これまで時報 (CLAUDE.md #58) と同時に鳴らす効果音は、標準ライブラリの
`wave`/`math` で毎回同じ波形を合成する固定音 (A5→E6 の2音) だけでした。
「音楽ファイルを指定したいが、できない」という報告を受け、`voice_chime_path`
(設定タブ、既定 "") を追加しました。Pi 上の実ファイルパスをそのまま
文字列で受け取ります — ファイル選択 UI は用意せず、既存の `alsa_device`
と同じ「空欄ならこの機能自体が既定動作、値があればそれを直接使う」という
テキスト入力パターンに揃えています。

- **`.wav`** はこれまでどおり `aplay` へ、**`.mp3`** は `mpg123` の
  単発起動 (`mpg123 -o alsa -a sentinel_voice <path>`) へ渡します。
  `aplay` は mp3 をデコードできないため、`voice_chime_path` に音楽
  ライブラリの曲 (yt-dlp の取得物は全て mp3、CLAUDE.md #2) を指定したい
  という自然な使い方に応えるには mpg123 経由が必須でした。この単発
  mpg123 は常駐する `music.Player` (`-a sentinel_music`) とは別プロセス
  で `-a sentinel_voice` を使うため、dmix 上の別スロットに入り、鳴って
  いる曲と競合しません (CLAUDE.md #31)。
- **存在しない・.wav/.mp3 以外の拡張子なら、警告ログを残して既定の合成音
  へ静かにフォールバックします。** 「時報自体は鳴らし続ける」という
  このプロジェクト全体の段階的劣化方針 (#27 の Open JTalk→espeak-ng
  フォールバックなどと同じ) を踏襲しています。**この
  フォールバックを外して「ファイルが無ければ無音」にしないでください**
  — 設定ミス 1 つで時報の効果音機能全体が沈黙します。

### 69. Bluetooth の agent 登録は、bluetoothctl を起動した直後だと失敗することがある

「どの不具合も直っていません」という報告があり、更新済みの `sentinel-logs`
(#42/#43 で追加したペアリングエージェントの生ログ) を実際に確認したところ、
#64/#66 (yes/no 自動応答・`JustWorksRepairing=always`) では説明のつかない、
より早い段階の失敗が写っていました。

```
[bluetoothctl起動直後] agent NoInputNoOutput
Failed to register agent object
default-agent
No agent is registered
...(しばらくして)...
Agent registered
...
[iPad とのペアリング試行]
Request confirmation
auth failed with status 0x05 (Authentication Failed)
[agent] Confirm passkey 201492 (yes/no): yes
auth failed with status 0x05 (Authentication Failed)
```

`agent NoInputNoOutput` を送った直後に **"Failed to register agent
object"** が返り、続く `default-agent` も対象が居ないため **"No agent is
registered"** で失敗しています。その後しばらくして (何が引き金かは特定
できていませんが) 自然に `Agent registered` が現れており、`coproc` で
`bluetoothctl` を起動した瞬間はまだ自分自身の D-Bus 接続の確立が終わって
おらず、その状態で送った最初の `agent` コマンドが失敗することがある、と
考えるのが最も筋が通ります。この空白の間にペアリング要求が来ると、
Pi 側には確認を求められる agent 自体が存在しないため、SSP のネゴシエー
ションが `0x05 (Authentication Failed)` として即座に失敗します — これは
#64 で直した「(yes/no) プロンプトに無応答のまま固まる」とは別の失敗の
形で、実際 `auth failed with status 0x05` は `Confirm passkey ...
(yes/no): yes` という応答の**前**に一度出ています。つまり今回のログは、
そもそも応答すべき agent が存在しない瞬間にペアリング要求が来ていた
ことを示しています。#64 の yes/no 自動応答はこの空白そのものには効き
ません — 効くべき対象 (プロンプトへの無応答) がそもそも別の不具合だから
です。

`scripts/sentinel-bt-agent.sh` に `register_agent()` を追加しました。
`agent NoInputNoOutput` を送ったあと、最大 3 秒 (`read -t 1` を 3 回) だけ
出力を監視して文字どおり **"Agent registered"** という行が来るまで確認を
待ちます。来なければ (`"Failed to register agent"` を見た、またはタイム
アウトした) 0.5 秒空けてもう一度最初から送り直し、これを最大 10 回まで
繰り返します。**"Agent registered" を実際に見た場合だけ** `default-agent`
を送ります — 登録に失敗したまま `default-agent` を送っても意味が無いため
です。10 回すべて失敗したら標準エラーへ警告を出しますが、スクリプト
自体は `exit` せず `power on`/`discoverable on`/`pairable on`/既知端末の
trust はそのまま続行します (このプロジェクト全体の段階的劣化方針 —
agent が無くても、せめて電源投入・discoverable 化・既知端末の trust は
やっておいた方がまし)。**この確認なしの一発 `agent NoInputNoOutput` +
`default-agent` に戻さないでください** — このレースは常に起きるわけでは
なく間欠的なので、手元の軽いテストでは直ったように見えて、次のコールド
ブートでまた同じ "Failed to register agent object" に戻ります。

### 70. 音楽ミキシングの復旧は、mpg123 を止めてから最大 120 秒で再試行し、その瞬間にクールダウンも強制解除する。cold boot でカードが未検出の場合もログに残す

#67 で「mpg123 が plughw を掴んだままだと dmix が永久に開けない」問題に
periodic recheck (5 分おき) を追加しましたが、これも同じ「どの不具合も
直っていません」報告で、起動からわずか 2 分のログで `/etc/asound.conf`
が存在しない・`sentinel-setup-audio-mixing.sh` 自体が `Interrupted
system call`/`Aborted by signal Terminated` で失敗している、という
#67 とは違う (もっと早い段階の) 症状が写っていました。2 分しか経って
いない機体では #67 の 5 分おき recheck は一度も発火し得ず、そもそも
別の理由で再試行が起きない、と考えるべき状況でした。

2 つ直しています。

1. **recheck の基準間隔を 300 秒から 120 秒 (`_MIX_RECHECK_BASE`) へ
   短縮し、起動直後にも近い間隔で 1 回目が発火するようにしました**
   (`last_mix_recheck` の初期値を `time.time()` ではなく
   `time.time() - _MIX_RECHECK_BASE` にする — 最初のチェックが「基準
   間隔 + ループの初期待ち」の二重待ちにならないようにするため)。
   120 秒という値は `sentinel-fix-audio-output.sh` 自身の
   `_AUDIO_RETRY_BASE_SEC` (CLAUDE.md #57) に合わせています — 同じ
   asound.conf の修復を、片方 (bash 側) は 2 分おき、もう片方 (Python
   側) は 5 分おきで別々の周期で追いかける理由がありませんでした。
2. **recheck が実際に `restart_playback()` を呼ぶ直前に、
   `_eq_sync_failed_at` (CLAUDE.md #35 のクールダウン) を明示的に
   `0.0` へ戻すようにしました。** mpg123 を止めるだけでは不十分な
   ケースが実機にありました — `/etc/asound.conf` がまだ一度も正しく
   作られていない機体では、`_sync_eq()` の「前回と同じ設定なら何も
   しない」判定 (#32) と `_apply_audio_mixing()` 自身の 60 秒クール
   ダウン (#35) の両方が、この再確認の瞬間に実際の再構成を試みることを
   妨げてしまい、Guardian の独立した周期 (2 分) がたまたま同じ瞬間に
   重ならない限り asound.conf が誰にも作り直されないまま stop()/play()
   を繰り返すだけになっていました。**このクールダウン解除を外さないで
   ください** — 同じ「mpg123 は止まるが asound.conf は誰も作り直さず、
   フォールバックへ戻り続ける」膠着状態に戻ります。

あわせて、`_apply_audio_mixing()` が `_card_index()` (`aplay -l` 経由の
カード検出、CLAUDE.md #37) から `None` を受け取って**何もログを残さず**
`(False, False)` を返していた無言の分岐に、重複を避けた警告ログを追加
しました。起動直後、ALSA/USB の列挙がまだ終わっていないだけなら実害は
無く (上記のクールダウン強制解除により、次の recheck で自然に直ります)、
実際にカードを検出できたときは info ログで復帰を報告します。**この
ログ追加自体は不具合の修正ではなく診断の穴埋めです** — CLAUDE.md #45 の
教訓 (「事実が出揃うまでコードを書かない」) のとおり、次に同じ
「直っていない」報告が来たとき、原因が「まだカードが見えていないだけ」
なのか「本当に何かが壊れている」なのかを、この 1 行の有無だけで最初に
切り分けられるようにするためのものです。

### 71. 時報は境界を待たずに即座にテスト再生できる

「時報もテストできるようにしてください」という要望がありました。既存の
`speak_test()` (設定タブの「音声をテスト再生」) は利用者が入力した自由文
をそのまま読み上げるだけで、時報カテゴリ固有の `{hour}`/`{minute}`/
`{minute_part}` プレースホルダの組み立てや、`voice_chime_enabled` に
従ったチャイム同時再生 (CLAUDE.md #58) を一切経由しません。これを
`speak_test()` へ `category="time"` のような形で押し込むと、`{hour}` 等
を渡さないテンプレートが `_fmt()` の「知らないプレースホルダは既定文へ
静かにフォールバックする」経路を踏んでしまい、実際にカスタマイズした
`voice_time_text` の文面を確認できません。

`voice.py` に `speak_test_time()` を新設しました。`time_signal_loop()`
と全く同じ組み立て (現在時刻から `{hour}`/`{minute}`/`{minute_part}` を
作り `voice_time_text` で整形、`_mixing_ready()` に応じて ducking か
音量だけの一時的な引き下げかを選び、dmix で重ねられる場合だけ
`voice_chime_enabled` に従ってチャイムを同時再生する) を、
`voice_time_interval_minutes` の壁時計境界を待たずに今すぐ 1 回だけ
実行します。`web/routes.py` に `POST /api/voice/test-time` を追加し
(既存の `/api/voice/test` の直後、CLAUDE.md の固定パス優先の原則には
影響しません — どちらも `/api/voice/` 配下の固定パスです)、
`web/static/index.html` の設定タブに「時報をテスト再生」ボタンを
既存の「音声をテスト再生」の隣に追加しました。**この機能を
`speak_test()` への引数追加で済ませず、独立した `speak_test_time()` の
ままにしておいてください** — 時報固有のプレースホルダ・チャイム条件を
テストパスにも本番パスにも同じ 1 か所 (`time_signal_loop()` と
`speak_test_time()` の両方が同じ `_fmt()`/`_CATEGORY_KEYS["time"]` を
参照する) で保つためです。

### 72. 音声ミキシングと Bluetooth ペアリングを抜本的に作り直した (段階的パッチの限界)

#31 から #70 まで、音声ミキシング (asound.conf + LADSPA EQ + 名前付き
PCM) と Bluetooth ペアリング (bluetoothctl のテキストスクレイピング) は
何度も「実機の症状 1 つを直す」パッチを重ねてきました。しかし利用者から
「小手先の変更では無理なようです。現在導入しているパッケージに縛られず
Web で調べ直し、抜本的な変更を厭わないでください」という報告があり、
決定的な手がかりが 2 つ示されました。

1. **`bluealsa-aplay --pcm=default` (`systemd/sentinel-bluealsa-aplay.
   service`、このプロジェクトが一度も書き換えていない既定の引数) は、
   このプロジェクトが `/etc/asound.conf` に `pcm.!default`/`ctl.!default`
   を独自定義するまで、それだけで問題なく動いていました。** 利用者が
   「昔 bluealsa-aplay を使っていた頃は PIN 無しで普通に運用できた」と
   証言したのはこのことで、"default" という ALSA の既定デバイス自体は
   何も壊れていません。壊していたのはこちらが `pcm.!default` を独自の
   (壊れやすい) dmix チェーンへ強制的に向け変えていたことでした。
2. **mpg123 を 2 つ同時に起動してみたところ、何の設定もせずそのまま
   重なって鳴りました。** これは alsa-lib が全カードに対して自動生成
   する `sysdefault:CARD=<N>` という per-card dmix ルートが最初から
   存在するためです (alsa-lib 1.0.9 以降、ハードウェアミキシング非対応
   カードには標準で用意される — [alsa.opensrc.org/Dmix](https://alsa.opensrc.org/Dmix))。
   bcm2835 の素の `dmix:CARD=...` には既知のハングバグがありますが、
   `sysdefault` はまさにその回避策として Raspberry Pi フォーラムで案内
   されている経路です
   ([forums.raspberrypi.com](https://www.raspberrypi.org/forums/viewtopic.php?t=262071))。

つまり `sentinel-setup-audio-mixing.sh` が書いていた `/etc/asound.conf`
(#31) は、**最初から存在した、設定ファイル不要の仕組みを、わざわざ
壊れやすい手書きの代替に置き換えていた**ということです。#32 のイコラ
イザー (LADSPA/mbeq)・#35〜#70 にわたる数々の「asound.conf が壊れた/
ズレた/再生成されない」系の不具合は、すべてこの不要な複雑さから生まれて
いました。以下、抜本的に作り直した箇所です。

**音声出力: `sysdefault:CARD=<N>` に統一し、`/etc/asound.conf` を廃止**

- `core/audio.py` に `analog_device(card=None)` を追加。
  `find_output_card()` (Headphones → bcm2835 → 先頭のカードという優先
  順位、CLAUDE.md #37。ここは変更なし) で見つけたカードから
  `sysdefault:CARD=<N>` を組み立てるだけです。
- `music.py`・`voice.py`・`scripts/sentinel-fix-audio-output.sh` の
  すべてがこれを使います。`sentinel-guardian.sh` の `check_audio()` は
  今までどおり `sentinel-fix-audio-output.sh` (`--print-card` を新設、
  install.sh がカード検出を再利用するため) へ委譲するだけです。
- **`scripts/sentinel-setup-audio-mixing.sh` を削除しました。** これに
  伴い `/etc/sudoers.d/sentinel` からもこのスクリプト用の行を削除して
  います — 書き込むべき root 専用ファイルがそもそも無くなったため、
  sudo 経由の権限昇格ルート自体が 1 つ減りました。
- `sentinel-fix-audio-output.sh` は asound.conf の再構成ロジックを丸ごと
  削除し、「numid=1 の床値解除」「numid=3 のルート復元」
  「`sysdefault:CARD=<N>` が実際に開くかの確認 + 開かなければ ALSA
  ドライバの再読み込み」だけの、ずっと短いスクリプトになりました。
  設定ファイルが存在しない以上、そもそも「ズレて壊れる」という不具合の
  クラス自体が発生し得ません。
- **`systemd/sentinel-bluealsa-aplay.service` は `--pcm=default` から
  `--pcm=sysdefault:CARD=<N>` (install.sh が実際のカードで sed 置換) へ
  変更しました。** 素の `default` に戻すだけでも動作はしたはずですが
  (#1 の発見のとおり)、alsa-lib 自身のカード選択が HDMI と Headphones の
  どちらを優先するかは機体依存で確認しようがなく、CLAUDE.md #37 が
  まさに警告している曖昧さです。`music.py`/`voice.py` と全く同じ
  `sysdefault:CARD=<N>` を明示することで、この 3 つの音声プロデューサー
  すべてが同じ理由で同じデバイスへ書き込む、という一貫した設計にして
  います。**この明示を外して `--pcm=default` に戻さないでください** —
  多カード機で HDMI を掴む曖昧さが復活します。

**イコライザー: mpg123 自身のリアルタイム EQ を直接叩く。LADSPA を廃止**

mpg123 の `-R` リモートプロトコルには元から `E <channel> <band>
<gain>` という実時間イコライザーコマンドがあります (`doc/README.remote`
に明記 — 32 サブバンド、既定ゲイン 1.00、"values work best between
0.00 and 3.00")。**再生中に送るだけで即座に反映され、mpg123 の再起動も
外部設定ファイルも一切要りません。**

`music.py` の EQ 実装をまるごと置き換えました。

- `_eq_subband_gains()` が、UI の 15 バンド (Hz ラベル、既存のまま) の
  dB 値を、44.1kHz を基準にした Nyquist 比から最寄りのサブバンド
  (0-31) へ写像し、`10**(dB/20)` で線形ゲインへ変換、実用域
  `[0.0, 3.0]` へクランプします。
- `Player._apply_eq(track_name)` が、曲を読み込むたび (`play()`)、また
  設定タブで EQ を変更した直後 (`refresh_eq()`) に、この 32 バンドを
  `E 3 <band> <gain>` として mpg123 へ直接送ります。前回送った値と同じ
  なら何もしません (無駄な 32 行の送信を避けるだけの軽いメモで、
  #32/#35 のような「再構成に失敗したら何もかも壊れる」類のクールダウン
  機構ではありません — 送信自体が失敗する余地がほぼ無いため)。
- **削除したもの**: `_apply_audio_mixing()`・`_asound_card()`・
  `_asound_eq_on()`・`_last_applied_eq`・`_eq_sync_failed_at`・
  `_last_effective_eq`・`_EQ_RETRY_COOLDOWN_SEC`・EQ 変更のたびに
  mpg123 を "S"+"Q" で落として再起動していたロジック全体。これらは
  すべて「asound.conf を書き換えて mpg123 を作り直す」という、もう
  存在しない手順のためだけに存在していました。
- UI のバンド範囲を ±20dB から ±12dB へ絞りました
  (`music._EQ_DB_RANGE`)。mpg123 の実用域 (線形 0.00-3.00 ≈
  -∞〜+9.5dB) に合わせた値で、それを超えた値は「動かしても実際には
  頭打ちで変わらない」という誤解を招くだけだったためです。
- `music.py` の `loop()` から、#67/#70 で追加した「mpg123 が
  `plughw:` フォールバックに留まっていないか定期的に再確認する」
  ロジック (`_MIX_RECHECK_BASE`/`last_mix_recheck`/
  `_mix_recheck_backoff`) をまるごと削除しました。`sysdefault:CARD=<N>`
  は設定ファイルに依存しないため、mpg123 が「フォールバック経路に
  留まる」という状態自体が発生し得ません。

**音楽と音声アナウンスの重ね合わせ: sysdefault が構造的に保証する**

以前は `sentinel_music`/`sentinel_voice` という名前付き PCM が実際に
開けるかどうかで「重ねて鳴らせるか、曲を完全に止めるしかないか」を毎回
判定していました (`_mixing_ready()`)。`sysdefault:CARD=<N>` は
alsa-lib 自身が常に用意するため、この判定・この二重の経路そのものが
不要になりました。

- `music.py` から `duck_for_voice()`/`resume_from_voice()` (曲を完全に
  停止する旧経路) を削除しました。`duck_volume_for_voice()`/
  `resume_volume_after_voice()` (音量だけ一時的に下げる、mpg123 の
  `V` コマンドのみを使う軽い経路) だけが残ります。
- `voice.py` から `_mixing_ready()`・`_fallback_device()`・
  `_sound_card()` を削除し、`_device()` 1 本 (`audio.analog_device()`
  を返すだけ) にまとめました。`speak_test()`・`speak_test_time()`・
  `loop()` はどれも「重ねられるか」の分岐が無くなり、常に
  `duck_volume_for_voice()` を使います。
- **Bluetooth 接続中のアナウンススキップは残しています。** これはもう
  技術的な制約ではありません — `bluealsa-aplay` も同じ
  `sysdefault:CARD=<N>` を使うため、原理的には重ねて鳴らせます。
  「電話でストリーミング中の音楽に日本語の時報が混ざる」体験を避ける
  ための、意図した仕様上の選択として残しています。

**音量制御: numid=1 の奪い合いをやめ、ストリームごとに独立させる**

以前は「音楽 (mpg123 の V コマンド、ソフトウェア側)」「音声アナウンス
(`SentinelVoice` という専用の ALSA softvol コントロール)」
「Bluetooth (numid=1、bcm2835 の共有ハードウェアレジスタ)」という
3 つの異なる仕組みが同居していました。numid=1 は音楽・Bluetooth・
この Pi の出力全体で共有される 1 つのレジスタで、#15/#31/#41 が繰り
返し「numid=1 と numid=3 を混同するな」「他の音量を意図せず動かすな」
と警告してきたのはまさにこの共有状態が原因でした。

- **音声アナウンス**: `voice._scale_wav()` が、open_jtalk/espeak-ng が
  書き出す WAV のサンプルそのものを Python 側で直接スケールします
  (標準ライブラリの `wave`/`array` のみ、非推奨化された `audioop` には
  依存しません)。ALSA のミキサー/softvol を一切経由しないため、
  `SentinelVoice` コントロールも、それを書き込むための root 権限も
  不要になりました。`voice_chime_path` が `.mp3` を指す場合は mpg123
  自身の `-f <スケール>` (既定 32768=1.0 倍) を使い、同じ「ツール自身の
  機能で完結させる」方針を踏襲しています。
- **Bluetooth**: `bluetooth._apply_volume()` は numid=1 の代わりに
  `bluealsa-cli` (bluez-alsa-utils に同梱、新規パッケージ不要) を使い
  ます。まず `bluealsa-cli soft-volume <PCM パス> on` でその接続だけの
  ソフトウェア音量を有効化し (既定では相手端末の AVRCP 絶対音量に
  委譲されており、こちらからの書き込みが効かないことがあるため)、
  続けて `bluealsa-cli volume <PCM パス> <0-127>` で直接設定します。
  PCM パスは `/org/bluealsa/hci0/dev_<MAC>/a2dpsnk` という決まった
  形式で組み立てられます (この Pi はアダプタが 1 つだけという前提、
  `bluetooth.set_alias()` の `/org/bluez/hci0/...` と同じ前提)。
  amixer も numid も一切登場しません。
- 音楽 (mpg123 の `V` コマンド) は変更していません — これは元々
  numid=1 を経由していなかったので、混同の当事者ではありませんでした。
- 3 つの音量がそれぞれ完全に独立した経路になったため、numid=1 は
  もう「この機体全体の物理的な音量つまみ」としてだけ存在します。
  `sentinel-fix-audio-output.sh` はこれが床値 (無音) に張り付いていない
  かだけを確認し続けます — 誰も per-stream の目的でこれを書き込まない
  ため、#15/#31/#41 のような混同はもう起こり得ません。

**Bluetooth ペアリング: `bluetoothctl` のテキストスクレイピングをやめ、
D-Bus の Agent1 を直接実装する**

#16/#64/#66/#69 の `scripts/sentinel-bt-agent.sh` は、`bluetoothctl` を
bash の `coproc` として動かし、その標準出力をパースして「ペアリングは
成功したらしい」と推測する方式でした。これは本質的に脆い設計です —
`bluetoothctl` はインタラクティブな CLI ツールであり、確実な IPC 手段
として作られていません。#69 で実機ログから確認した
`"Failed to register agent object"` → `"No agent is registered"` は、
`bluetoothctl` 自身の D-Bus 接続がまだ確立し切っていない一瞬にコマンドを
送ってしまうという、この方式が原理的に抱える弱点の表れでした。

新設した `modules/bt_agent.py` が `org.bluez.Agent1` を D-Bus オブジェ
クトとして直接エクスポートし、`org.bluez.AgentManager1.RegisterAgent()`
/ `RequestDefaultAgent()` を実際に呼び出します。これらは同期的な D-Bus
呼び出しなので、成功したかどうかはその場で例外の有無から確実に分かり
ます — テキスト出力を監視して「それらしい行が来るまで待つ」という
推測が一切不要になりました。

- 依存は `dbus-next` (pure Python、asyncio ネイティブ、zero-dependency、
  `install.sh` の venv へ pip install) を使います。`python-dbus` +
  PyGObject の GLib メインループという定石ではなく、このプロジェクト
  自身が既に asyncio ベース (`core.supervisor.SUPERVISOR`) であるため、
  同じイベントループに乗る実装の方が一貫性があります。
- `main.py` が `SUPERVISOR.spawn("bt-agent", bt_agent.loop)` で通常の
  モジュールと同じパターンで起動します。**独立した systemd サービス
  ではありません** — `scripts/sentinel-bt-agent.sh` と
  `systemd/sentinel-bt-agent.service` は削除し、`install.sh` は
  旧バージョンからのアップグレード時にこのユニットが残っていれば
  無効化・削除します (新しい agent と D-Bus の default-agent 枠を
  取り合うことになるため)。
  `sentinel-guardian.sh`/`sentinel-logs.sh`/`sentinel-diagnose.sh` から
  もこのユニット名への参照をすべて外しました — agent のログは
  `sentinel.service` 自身の journal (`sentinel.bt_agent` logger) に
  そのまま流れるため、専用の抽出ロジックも不要になりました。
- バス切断の検出には `bus.wait_for_disconnect()` を使います (bluetoothd
  の再起動などで接続が切れたことを確実に検知する dbus-next の
  API) — `asyncio.sleep()` でただ待つだけの実装だと、切断されても
  次に何か送信しようとするまで気付けません。切断を検知したら
  `loop()` の外側のリトライ (指数バックオフ、上限 60 秒) が再接続・
  再登録します。
- 端末の自動 trust (旧 #16 の「Authorize service を毎回聞かれると
  接続が確立直後に切れる」対策) は、起動時の一括 trust
  (`_trust_known_devices()`) と、`InterfacesAdded` シグナル購読による
  新規端末の即時 trust (`_watch_new_devices()`) の両方を D-Bus の
  プロパティ書き込みとして直接行います — `bluetoothctl trust <MAC>`
  のサブプロセス呼び出しはもう経由しません。
- `dbus-next` が見つからない環境 (venv の再構築が必要な場合など) では
  警告を出して何もしない、という段階的劣化にしています —
  このプロジェクト全体の「壊れても他の機能は道連れにしない」方針
  (Open JTalk→espeak-ng、LADSPA EQ→off、dmix→direct-hw と同じ考え方)
  を踏襲しています。
- `bluetooth.py` 自身 (接続検知・音量・エイリアス変更) は
  `bluetoothctl` ベースのままです。単純な状態の読み取り・書き込みには
  この方式で十分で、agent 登録のようなタイミング競合の問題を抱えて
  いなかったため、変更する理由がありませんでした。

**この節が置き換えるもの**: #31/#32/#35 (旧 dmix + LADSPA EQ の導入)、
#37 の一部 (find_output_card() 自体は変更なし、使い先が変わっただけ)、
#41 の音声関連部分、#46 の EQ_ACTIVE 関連部分、#57 (asound.conf の
指数バックオフ再試行)、#65 (`sentinel_voice` の対称検証)、#67/#70
(mpg123/dmix デッドロックとその再確認ループ) は、いずれも**もう存在
しないコードの説明**です。#16/#64/#66/#69 (Bluetooth ペアリングの
一連の対策) も同様に、`sentinel-bt-agent.sh` という**もう存在しない
スクリプト**についての記録です。これらの節を削除はしていません —
「何を試して、なぜそれでも直らなかったか」という記録には価値がある
ため、今後の判断のために残します。ただし**今のコードを理解するには
この #72 を読んでください** — 古い節に書かれている `asound.conf`・
`sentinel_music`・`sentinel_voice`・`SentinelVoice`・
`sentinel-bt-agent.sh`・LADSPA・numid=1 の音楽/Bluetooth 用途は、
すべてこの節で置き換えられた設計です。**これらの節の記述を信じて
古い実装へ戻さないでください。**

### 73. Bluetooth ペアリングエージェントを機能停止し、代わりに BGM の Bluetooth 出力を追加。動体検知の破損検出にタイル化けパターンを追加

#72 で dbus-next ベースに刷新した `modules/bt_agent.py` (BlueZ Agent1 の
D-Bus 直接実装) は、それでも実機で "Failed to register agent object" の
再登録ループが収まらないという報告があった。「小手先の変更では無理」
という判断のもと、**この機能自体をいったん停止する**ことにした —
`main.py` はもう `bt_agent.loop()` を spawn しない (import もしない)。
`modules/bt_agent.py` 自体は削除せず、冒頭に機能停止中である旨と復活の
手順を明記して残してある。**この判断は「直せなかったから諦める」では
なく「この用途 (ペアリング要求への自動応答) は代替手段がある」という
判断**である — `bluetoothctl` を対話的に起動すると、ツール自身が自分を
agent として登録し (確認プロンプトには `yes` と答えるだけ)、これは
実機で安定して動く。Web UI の端末タブから `bluetoothctl` を開いて
`scan on` → `pair <MAC>` → `trust <MAC>` で一度ペアリングすれば、
以後の接続・再接続は `modules/bluetooth.py` が (エージェント無しで)
自動的に行う — エージェントは初回ペアリングの確認応答にしか関与しない
ため、機能停止の影響はそこに限られる。**`main.py` の import/spawn を
安易に復活させないでください** — 復活させるなら、まず実機で登録
レースが本当に解消したことを確認してからにすること。

**BGM を Bluetooth のヘッドホン/スピーカーへ流す新機能を追加した。**
これまでの Bluetooth 連携は「電話がこの Pi へ接続し、Pi のスピーカーで
再生する」(Pi が A2DP **シンク**) の一方向だけだった。今回追加したのは
逆方向 — **この Pi が A2DP **ソース**としてヘッドホン/スピーカーへ
能動的に接続し、BGM (音楽ライブラリ) と時報をそこへ流す**機能である。
「昔 bluealsa-aplay を使っていた頃は PIN 無しで普通に運用できた」という
利用者の証言 (#72 の抜本的刷新の発端) とも合致する、bluealsa 自体が
最初から持っている能力を使うだけの実装にしている。

- **`systemd/sentinel-bluealsa.service` の `ExecStart` に `-p
  a2dp-source` を追加した** (`-p a2dp-sink -p a2dp-source`、
  `install.sh` の sed も同様)。同じ bluealsad プロセスが両方の役割を
  同時に持てる — 受信 (電話→Pi) と送信 (Pi→ヘッドホン) は BlueZ の
  プロファイルとしては別物だが、bluealsa 側でデーモンを分ける必要はない。
- **`bootstrap.sh` に `libasound2-plugin-bluez` を追加した** (best-effort
  の別ステップ、失敗しても他のパッケージを巻き込まない)。これが
  `libasound_module_pcm_bluealsa.so` という ALSA I/O プラグイン本体を
  提供する — `bluez-alsa-utils` (bluealsa-cli/bluealsa-aplay) とは
  **別の Debian パッケージ**であることを見落とすと、`bluealsa:DEV=...`
  という拡張デバイス名を mpg123/aplay が一切解決できず、原因不明の
  "unknown pcm" エラーで沈黙する。
- **出力先の指定は `bluealsa:DEV=<MAC>,PROFILE=a2dp` という ALSA の拡張
  デバイス名構文**で、`/etc/asound.conf` への登録は一切不要 (`aplay -D`
  と全く同じ書式)。`core/audio.py` の設計判断 (#72、`sysdefault:CARD=<N>`
  も設定ファイル不要) と同じ精神 — 新しい設定ファイルを増やさない。
  `music.py` の `Player._spawn()` は `self.bt_output_addr` が立っていれば
  この文字列を、無ければ従来どおり `sysdefault:CARD=<N>` を `mpg123 -a`
  に渡す。
- **`modules/bluetooth.py` に `output_loop()` を新設**し、`bt_enabled` の
  下で `main.py` から独立に spawn する (`SUPERVISOR.spawn("bluetooth-output",
  bluetooth.output_loop)`)。`bt_output_device` (設定 = 音楽タブの「BGM
  出力先」セレクタ) に MAC アドレスが入っている間、**解除されるまで
  自動で接続を試み続ける** — 受信側 (`loop()`) は電話が繋ぎに来るのを
  待つだけでよいが、送信側は Pi が能動的に `bluetoothctl connect` を
  送らないと繋がらない。接続の実際の確立は `bluetoothctl` の
  "Connected: yes" だけでは判断せず、`bluealsa-cli list-pcms` の出力に
  `.../a2dpsrc` パスが現れているかで確認する
  (`_output_pcm_ready()`) — ACL 接続は繋がっていても A2DP のプロファイル
  ネゴシエーションがまだ済んでいないことがあり、これは受信側の音量制御
  (`.../a2dpsnk`、#72) が同じ理由で D-Bus 経由の確認を使っているのと
  同じ考え方。接続に失敗し続ける場合は camera.py の破損フレーム再接続
  (#19) と同じ指数バックオフ (10 秒 → 上限 120 秒) をかけ、電源が
  入っていない/範囲外の端末に無意味な `connect` を送り続けない。
  設定を解除 (`bt_output_device` を空文字に) した瞬間には、今まさに
  繋がっている端末があれば明示的に `bluetoothctl disconnect` する —
  「再接続を試みるのをやめる」だけでは端末側は繋がったままバッテリーを
  消費し続けるため。

**出力先ごとに音量・EQ を独立させた** (`bt_output_profiles`: MAC ->
  {volume, eq_enabled, eq_bands})。「デバイスを変えると音量が異常に
  大きくなったりしないように」という要望への直接の対応で、AUX 用の
  `music_volume`/`music_eq_*` とは完全に別領域に保存する
  (`music.py` の `_active_output_profile()` が今の実効出力先を見てどちら
  を使うか判断する唯一の窓口)。`Player._spawn()`/`Player._apply_eq()`/
  `Player.set_volume()` はすべてここを経由するため、AUX で 90% にして
  いたところへ、まだ 30% しか設定していない新しいヘッドホンを繋いでも
  いきなり 90% で鳴り始めることはない — そのヘッドホン自身のプロファイル
  (未設定なら既定 60%) が使われる。曲ごとの EQ 上書き
  (`music_eq_track_overrides`) だけは出力先に関わらず共通のまま
  (`resolve_eq_bands()` の `base_bands` 引数) — 「この曲は低音が強すぎる」
  といった補正は曲自体の性質であって、どのスピーカーで聞くかには依存
  しないと判断した。**この分離をやめて出力先を跨いで音量/EQ を共有する
  実装に戻さないでください** — 同じ「デバイスを変えると音量が急に変わる」
  不具合に戻ります。音楽タブの「イコライザー」カードのスコープ選択に、
  ペアリング済み端末が動的に追加される (`bt:<MAC>` という内部値) ことで、
  AUX の全体設定・曲ごとの上書き・Bluetooth 出力機器ごとの設定を同じ UI
  から切り替えられるようにしている。

**Bluetooth 出力中の音声アナウンスは「重ねる」のではなく「一時停止」に
した。** AUX (`sysdefault:CARD=<N>`) は alsa-lib の dmix が複数ストリーム
を構造的に受け付けるため、音楽とアナウンスを重ねて鳴らせる (#72) が、
**bluealsa の A2DP ソース PCM にはそのような多重化層が無く、同時に開ける
クライアントは 1 つだけ**— 音声アナウンス側が同じデバイスを開こうとする
と "device busy" で失敗する。`music.py` に `pause_for_voice()`/
`resume_after_voice()` (mpg123 の `P` コマンドで一時停止/解除するだけ、
プロセス自体は落とさないので `_gen` 世代カウンタ (#50) は無関係) を
追加し、`begin_voice_interrupt()`/`end_voice_interrupt()` という共通の
窓口で出力先に応じてどちらを使うか (AUX なら `duck_volume_for_voice()`
で音量だけ下げる、Bluetooth なら `pause_for_voice()` で一時停止する) を
自動選択する。`voice.py` の `loop()`/`speak_test()`/`speak_test_time()`
はすべてこの窓口だけを呼ぶ — 出力先の判定を複数箇所に重複させると、
どこか 1 箇所だけ更新し忘れて Bluetooth 出力中に音量制御 (何も効かない)
を呼んでしまう、といった不整合が起きやすいため。`voice.py` の `_device()`
自体も `music.current_output_device()` を呼ぶだけになり、音楽と時報は
常に同じ出力先 (AUX か、選択中の Bluetooth 機器) から聞こえる。

**時報の効果音 (チャイム) が声を「かき消す」報告への対応として、
`voice_chime_volume` を新設し `voice_volume` (読み上げ本体) と分離した。**
これまではどちらも同じ `voice_volume` を共有しており、チャイムが声より
大きく聞こえる場合に両方を一緒に下げるしかなかった。`_speak_sync()` は
チャイムに `voice_chime_volume`、TTS 本体に `voice_volume` を別々に渡す。
**この 2 つを再び同じ設定値にまとめないでください** — 同じ「声が効果音に
負ける」報告に戻ります。

**音声テンプレートのプレースホルダを増やし、文面をより柔軟に組み立て
られるようにした。** `voice.py` に `_time_values()` を新設し (
`time_signal_loop()`/`speak_test_time()` の両方がこれを使う — 片方だけに
新しいプレースホルダを足して食い違う事故を防ぐ)、時報のテンプレートで
`{hour}`/`{minute}`/`{minute_part}` に加えて `{weekday}` (月〜日)・
`{hour12}`・`{ampm}` (午前/午後)・`{month}`・`{day}` が使えるようになった。
さらに `_fmt()` 自身が `{time}` (現在時刻 HH:MM) をどのカテゴリでも
共通して補うようにしたため、`voice_error_text`/`voice_camera_reboot_text`/
`voice_other_text` のような `{message}` だけのテンプレートでも「いつ
起きたか」を文面に含められる。

**動体検知の破損フレーム検出に、単色ブロック化とは別のパターン
(タイル化け/モザイク化) を追加した。** 利用者から「何も無いときに検知
し、逆に人が居るときに検知が外れる」という報告があり、添付されたカメラ
ごとの破損の実例を見ると、片方のカメラは既存の検出器が対象としていた
「一部が単色で埋まる」パターンだったが、もう片方は**同じ小さな画像が
タイル状に何度も繰り返し出現する**、全く別の壊れ方をしていた —
MJPEG のフレーム内で再同期がずれ、デコーダが同じマクロブロックデータを
複数タイル分にわたって読み違える、と考えられる。この壊れ方は #19/#28
のフラット判定 (セルごとの標準偏差が低いか) をすり抜ける — タイル自体は
本物の映像の断片なので内部にちゃんと分散があり、「平坦」には該当しない
ため。すり抜けた結果、この破損フレームが `latest.jpg`・動体判定・`prev`
(次フレームとの比較用基準) にそのまま使われ、「何も無いのに検知する」
(隣接フレーム間でタイルの現れ方が変わるたびに大きな差分が出る) と
「人が居ても検知が外れる」(破損フレームが基準になると、次の正常な
フレームとの差分が破損由来のノイズに埋もれる/`motion_area_max_ratio` の
上限で弾かれる) の両方の原因になっていたと考えられる。

`camera.py` の `_frame_corruption_ratio()` に、各セルをさらに 4x4 へ
縮小し輝度を粗く 8 段階へ量子化した signature を作り、最も多く出現する
signature の面積比が閾値 (`corrupt_tile_repeat_ratio`、既定 0.35、
カメラごとに上書き可能) を超えたら「タイル化けの疑いあり」とする検出を
追加した。平坦セル自身が同じ signature に量子化されて一致するのは当然
なので、それだけでは二重計上しない — 平坦「ではない」セルが同じ
signature で大量に繰り返している場合だけをこの経路で扱う。既存の
`known_ok_patterns` 学習の仕組み (#19、同じ位置・同じ柄が何フレームも
連続したらカメラ本来の絵として許容する) はそのまま両方の検出経路で
共有している — セル単位の「平坦か」と「支配的 signature と一致するか」
の 2 つの真偽値をペアにしたものをパターンのキーとして使うだけで、学習の
ロジック自体には変更が要らなかった。処理コストは 48 セル分の 4x4 への
縮小と Counter 集計だけで、既存のフラット判定と同じ頻度 (`motion_interval`
ごと、カメラごとに 1 回) で回しても Pi 3B+ で無視できる範囲に収まる。
**このタイル化け検出を外して単色ブロック化の判定だけに戻さないで
ください** — 同じ「破損の種類によっては検知をすり抜ける」不具合に
戻ります。

### 74. Bluetooth の discoverable/pairable を常時オンにしていたせいで、身に覚えのない端末が勝手にペアリングされていた

#73 で `modules/bt_agent.py` を機能停止した直後、「勝手に謎のデバイスが
追加されてしまう」という報告があった。原因は bt_agent とは別の場所、
`install.sh` と `sentinel-guardian.sh` が Bluetooth の discoverable/
pairable を**常時オン**に保っていたことだった。

- `install.sh` は `/etc/bluetooth/main.conf` に `AlwaysPairable = true`・
  `DiscoverableTimeout = 0`・`PairableTimeout = 0` を書いていた —
  「常にペアリング可能・タイムアウトで自動的に閉じることは無い」という
  設定。
- `sentinel-guardian.sh` の `check_bluetooth()` は 2 分ごとに
  `bluetoothctl show` を見て、`Discoverable: yes`/`Pairable: yes`
  でなければ即座に `on` へ戻していた。

この 2 つが揃うと、`bluetoothd` が再起動されるたび (Guardian 自身の
自己修復、apt の自動アップグレード、OOM Kill など、この機体では珍しく
ない、CLAUDE.md #12/#45/#55) に discoverable/pairable がリセットされても
Guardian が次の周期までに必ず戻し、かつ main.conf 側は一度戻れば二度と
自分からは閉じない。結果として **この Pi は 24 時間 365 日、近くの
どんな端末からのペアリング要求も受け付け続けていた**。Bluetooth の
Secure Simple Pairing は、双方が入出力機能を持たない (NoInputNoOutput)
組み合わせでは「Just Works」方式で確認なしにペアリングが完了する —
これは #73 で bt_agent を止めたこととは無関係に、双方の capability
ネゴシエーション次第でエージェントの有無を問わず起こり得る。この
組み合わせのもとでは、近くを通っただけのスマホやイヤホンが誤って
ペアリングを試みる・他人のアプリが自動的にペアリングを試す、といった
経路で「身に覚えのない端末がいつの間にかペアリング済みになっている」
ことが起こり得ると判断した。

この節が置き換えるのは以前の設計判断 (#16/#17/#66 あたりで確立された
「iPhone からのペアリングをいつでも受け付けられるように、Bluetooth は
常時 discoverable/pairable にしておく」という前提) そのものである。
当時はこれが自動応答エージェント (bt_agent、当時は
`scripts/sentinel-bt-agent.sh`) とセットで機能する設計だった — 常時
開いている窓口へ、エージェントが確認応答を自動で返す、という組み合わせ
だった。#73 でエージェントを止めた今、常時開いている窓口だけが残り、
確認する主体が誰もいない状態になっていた。ペアリングは既に「端末タブ
で `bluetoothctl` を手動実行する」という、人が明示的に行う操作へ
切り替わっている (#73) ため、**常時開けておく理由自体がもう無い**。

修正は 2 箇所。

1. `install.sh`: `AlwaysPairable = false`・`DiscoverableTimeout = 180`・
   `PairableTimeout = 180` に変更。`AlwaysPairable=false` により、
   ランタイムの pairable トグル (設定タブのボタン、または人が
   `bluetoothctl` で打つ `pairable on`) が実際に意味を持つようになる
   (以前は次の bluetoothd 再起動で `true` へ強制的に戻されていた)。
   180 秒の有限タイムアウトにより、on にしたまま閉じ忘れても
   `bluetoothd` 自身が自動的に `off` へ戻す。さらに、main.conf を
   書き換えたかどうかに関わらず毎回 `bluetoothctl discoverable off`/
   `pairable off` を明示的に呼ぶようにした — 過去のバージョン
   (`AlwaysPairable=true` 時代) からアップグレードした機体では、
   main.conf の既定値を直しただけではランタイム側の D-Bus プロパティは
   `on` のまま残ってしまうため。
2. `sentinel-guardian.sh` の `check_bluetooth()` から、discoverable/
   pairable を強制的に `on` へ戻す 2 つのブロックを削除した。電源が
   落ちていた場合に `power on` する自己修復だけは残している (これは
   #12 の「起動直後の UART アタッチ競合」対策そのもので、discoverable/
   pairable の常時公開とは無関係)。

**この 2 か所を元に戻さないでください** — 同じ「身に覚えのない端末が
勝手にペアリングされる」不具合に戻ります。特に `check_bluetooth()` へ
discoverable/pairable の force-on を書き戻すと、main.conf 側をいくら
閉じる方向へ直しても、次の Guardian 周期 (最大 2 分後) で無条件に
また開かれてしまいます。

**この修正は「新しい端末を二度とペアリングできなくする」ものではない**
— 設定タブの「ペアリングを許可」ボタン (`bluetooth.set_pairable(True)`)
は変更していない。既定でオフになり、3 分で自動的に閉じるようになった
だけで、必要なときに開く手段そのものは残っている。

**既にこの脆弱な期間中にペアリングしてしまった、身に覚えのない端末が
残っている可能性がある。** このスクリプト自身にはペアリング済み端末の
「これは自分が意図して繋いだものか」を判定する手段が無い (端末の
名前・MAC アドレスだけでは判断できない) ため、自動削除はしていない。
設定タブの Bluetooth カードやペアリング済み端末一覧、あるいは端末タブ
から `bluetoothctl devices` / `bluetoothctl paired-devices` を実行して
一覧を確認し、覚えのない端末があれば `bluetoothctl remove <MAC>` で
手動で削除することを推奨する — 特に、音楽タブの「BGM 出力先」
セレクタ (#73) はペアリング済み端末をそのまま候補として出すため、
覚えのない端末が紛れ込んでいると選択肢が汚染される。

### 75. discoverable/pairable の時限窓だけでは不十分。Web UI での逐一承認と、ペアリング済み端末の削除 UI を追加した

#74 は discoverable/pairable を既定オフ・3 分の時限にしたが、これは
「誰でもペアリングできる時間を短くする」だけで、「誰がペアリングできる
か」は一切制御していなかった。利用者から「今は本質的な対策はできて
いません」という明確な指摘があった — その 3 分の窓が開いている間は
結局、近くのどんな端末でも Just Works (NoInputNoOutput 同士の SSP は
双方が確認なしで自動承認する) で確認なしにペアリングできてしまう
ことに変わりはない。加えて「接続機器を消去できるようにしてほしい」
「もっと直感的に」という要望もあった。

**この節が対処するのは discoverable/pairable の窓の長さではなく、窓が
開いている間に実際に「誰の」ペアリングを許すかという、その次の層の
問題である。**

#### 過去の機能停止判断の再検証

`modules/bt_agent.py` (BlueZ Agent1 を D-Bus に直接エクスポートする
実装、#72) は #73/#74 の時点で「実機で "Failed to register agent
object" の再登録ループが収まらない」として機能停止していた。しかし
#74 で見つけた実際のバグ (`Agent` クラスの `@method()` メソッドに
`-> None` という戻り値注釈を書いていたことによる `ValueError: service
annotations must be a string constant (got None)`) を踏まえて読み直すと、
この例外は `bus.export()` の**エージェント登録より前**、クラス定義の
デコレータ処理の時点で毎回確実に発生していた。つまり以前観測されていた
「ループ」は、登録がときどき失敗するレースではなく、**登録そのものが
100% 失敗し続けていた**ことを意味する — `loop()` の `except Exception`
が指数バックオフで再試行するたび、毎回同じ場所で即座に例外落ちして
いただけである。#74 でこのバグ自体は修正済みだったが、その時点では
まだ「機能停止のまま」という判断を変えていなかった。今回、この誤診断
(実際には「レース」ではなく「100% 再現するバグ」だった) を踏まえて
再度有効化した。`main.py` の import と
`SUPERVISOR.spawn("bt-agent", bt_agent.loop)` を戻している。
**もし実機で再度登録に失敗する場合は、まず `modules/bt_agent.py` の
docstring とこの節を読み、本当に別の原因かを疑うこと** — 過去 2 回、
この種の Bluetooth 不具合は「原因を推測で決めつけて別の対策を打つ」
ことで長引いた (CLAUDE.md #45 の教訓と同種)。

#### Web UI での逐一承認 (新設)

`modules/bt_agent.py` の `Agent` クラスのうち `RequestConfirmation`
(SSP のペアリング確認)・`RequestAuthorization` (レガシーの承認要求)・
`AuthorizeService` (信頼前のサービス利用許可要求) の 3 つを、即座に
承認するのをやめ、`PENDING` という module-level の辞書へ一旦積んで
Web UI の判断を待つように変更した。

```python
async def _await_approval(bus, device_path, kind, passkey=None) -> bool:
    ...
    fut = asyncio.get_running_loop().create_future()
    PENDING[req_id] = {"addr": mac, "name": name, "kind": kind,
                        "passkey": passkey, "future": fut, "at": time.time()}
    try:
        return await asyncio.wait_for(fut, timeout=_APPROVAL_TIMEOUT)
    except asyncio.TimeoutError:
        return False
    ...
```

`_APPROVAL_TIMEOUT` (既定 20 秒、bluetoothd 自身の SSP タイムアウトより
確実に短くする) 以内に Web UI から応答が無ければ**拒否**として扱う —
放置された要求を「時間切れだから許可」にしてしまうと、無人運用の機体
では承認ゲート自体が実質無効化されてしまうため、安全側 (ブロック) へ
倒すのが唯一の妥当な既定動作である。これにより、discoverable/pairable
の窓が開いていても、**Web UI で「許可」を押した接続だけ**が実際に
ペアリングされる。窓の中でたまたま Just Works の確認を要求してきた
無関係な端末は、人間が何もしなければ 20 秒後に自動的に拒否される。

`bt_agent.list_pending()`/`bt_agent.decide(id, allow)` を Web UI 側の
唯一の窓口として公開した。この 2 つは dbus-next に依存しない (素の
`dict`/`asyncio.Future` だけを扱う) ため、dbus-next が入っていない環境
でも `web/routes.py` から安全に呼べる (常に空/no-op として振る舞う)。
`bt_agent.loop() の Agent クラスは `PENDING` の `asyncio.Future` を
`web/routes.py` のリクエストハンドラと直接共有している — uvicorn +
`core.supervisor.SUPERVISOR` は単一の asyncio イベントループ上で動く
ため (このプロジェクト全体の設計、#72 参照)、スレッド間同期やロックは
一切不要である。**RequestPinCode/RequestPasskey はこのゲートの対象外の
まま**にしている — レガシー PIN ペアリング向けで NoInputNoOutput 宣言
では基本的に呼ばれず、呼ばれたとしても承認/拒否の二択では意味を成さない
(実際に人間がその場で入力すべき値を要求されている) ため。

Web UI 側は `GET /api/bluetooth/pending`・
`POST /api/bluetooth/pending/decide` (`{"id":..., "allow": bool}`) を
新設し、`web/routes.py._overview()` にも `bluetooth_pending` として
含めた。これにより既存の WebSocket 定期送信にそのまま乗り、**どのタブを
開いていても**承認待ちが 1 件でもあれば次の定期送信 (数秒以内) でモーダル
が表示される — 追加のポーリングを新設する必要が無い。`index.html` は
`#bt-pending-modal` として、要求ごとに端末名・MAC・種別・(あれば)
パスキー・残り秒数の目安・「許可」「拒否」ボタンを表示する。

**この承認ゲートは「discoverable/pairable の時限窓 (#74)」を置き換える
ものではなく、その内側に重ねる追加の層である。** 窓を閉じておけば
そもそも要求自体が来ない (防御の第一層)。窓が開いていても、Web UI で
明示的に許可しない限りペアリングは完了しない (防御の第二層)。**この
承認ゲートを外して #74 の時限窓だけに戻さないでください** — 同じ
「窓が開いている間は誰でもペアリングできる」という、利用者が明確に
指摘した不十分な状態に戻ります。

#### ペアリング済み端末の削除 UI (新設)

`modules/bluetooth.py` に `remove_device(addr)` (`bluetoothctl remove`)
を新設した。#74 の時点では「覚えのない端末があれば手動で
`bluetoothctl remove <MAC>` してください」という CLI 頼みの案内しか
無かった。削除時には `bt_device_volumes` (受信側の端末ごとの音量、#15)
と `bt_output_profiles` (送信側の端末ごとの音量/EQ、#73) からもその
MAC を取り除く (`music.forget_bt_output_profile()`) — 消さずに残すと、
同じ MAC の端末を再ペアリングしたときに前回の値が亡霊のように復活する。
今の BGM 出力先がその端末なら `set_output_device("")` と同じ後始末で
解除する。`web/routes.py` に `POST /api/bluetooth/remove` を新設し、
Web UI の Bluetooth カードに「ペアリング済み端末」一覧 (端末名・MAC・
接続中かどうかのドット・削除ボタン) を追加した — 承認ゲートを誤って
「許可」してしまった端末や、もう使わない端末を Web UI だけで取り消せる。

#### UI の分かりやすさ

Bluetooth カードの案内文を、以前の「discoverable/pairable の切り替え
ボタンの説明」から、「① 許可ボタンを押す → ② 相手端末からペアリングを
開始する → ③ この画面に出る要求を確認して許可する」という 3 ステップの
手順として書き直した。**この 3 ステップの文言を、単に discoverable/
pairable の切り替えだけを説明する文言に戻さないでください** — 承認
ゲートという新しい (かつ本質的な) 手順が抜け落ち、利用者が「ペアリング
を許可」を押しただけで完了すると誤解する、同じ「本質的な対策になって
いない」体験に戻ります。

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
scripts/sentinel-fix-audio-output.sh
                    「再生中と表示されるのに無音」を直す。共有ハード
                    ウェア音量 (numid=1) が最下端 = 消音なら戻し、出力
                    ルート (numid=3) を AUX に固定し、
                    `sysdefault:CARD=<N>` (core/audio.analog_device()、
                    CLAUDE.md #72) が実際に開くかを確認して、開かなければ
                    ALSA ドライバを再読み込みする。設定ファイルは一切
                    書かない (書くものが存在しない)。find_output_card()
                    の bash 版はここ 1 本だけ (Guardian の check_audio()
                    はこのスクリプトへ委譲する、CLAUDE.md #41)。
                    `--print-card` で検出したカード番号だけを出力する
                    モードもあり、install.sh がこれを使って
                    bluealsa-aplay のユニットへ同じカードを渡す
scripts/sentinel-logs.sh
                    `sentinel-logs [時間] [full]` (/usr/local/bin/sentinel-logs)。
                    貼り付け用に「今おかしい所」だけを短く出す。同じ
                    メッセージは PID や行内タイムスタンプの違いを無視して
                    1 行にまとめ、回数を x14 のように付ける (再起動ループは
                    同じ文を何十回も出すため、これだけで実用的な長さに
                    なる)。冒頭にサービス・ストレージ・音声・ポートの状態
                    ブロックを置き、数字に文脈を与える。tar.gz を作る
                    sentinel-diagnose とは用途が別 (CLAUDE.md #42)。
                    Bluetooth ペアリングエージェント (modules/bt_agent.py)
                    は sentinel.service のプロセス内で動く (専用の
                    systemd ユニットを持たない) ため、専用の journal
                    抽出は無く「sentinel (app)」の digest にそのまま
                    含まれる (CLAUDE.md #75 で再度 spawn するようにした)
scripts/sentinel-adguard-8083.sh
                    AdGuard Home の :8083 への直接アクセスを一時的に
                    有効化 / 恒久的に無効化する (人が手動で実行する)
scripts/sentinel-fix-bluealsa.sh
                    bluealsa が org.bluealsa の D-Bus 名を取れずに落ち
                    続ける状態を直す。dbus-send で実際の所有者 PID と
                    ユニットを特定し、別ユニット (ディストリ側の
                    bluealsa.service など) ならそれを停止・無効化する。
                    この再起動ループは bluealsa-aplay を道連れにし、
                    bcm2835 を壊して「音楽が鳴らない」まで波及するため、
                    音声障害の調査でも最初に見る (CLAUDE.md #45)
scripts/sentinel-tailscale.sh
                    `sentinel-tailscale [status|up|down|reset]`
                    (/usr/local/bin/sentinel-tailscale)。Tailscale への
                    参加・離脱・状態確認をまとめた入口。`up` は必ず
                    `--accept-dns=false` を付ける (CLAUDE.md #39)。
                    setup.sh の H7 は初回導入時にしか聞かないため、
                    あとから設定したい人のための恒久的な入口として
                    用意している (CLAUDE.md #46)
scripts/sentinel-set-governor.sh
                    CPU ガバナを切り替える。root しか書き込めないため
                    sudoers で個別に許可し、core/state.py が sudo 経由で
                    呼ぶ (直接書き込みでは権限エラーで無視される)
scripts/sentinel-set-hotspot-ssid.sh
                    WiFi ホットスポットの SSID を実行中に変更する。
                    /etc/hostapd/hostapd.conf は root しか書き込めないため
                    sudoers で個別に許可し、modules/hotspot.py が sudo 経由
                    で呼ぶ (sentinel-set-governor.sh と同じパターン)
scripts/sentinel-autoupdate.sh
                    sentinel-autoupdate.timer (30 分ごと) から起動される。
                    git clone の場所を install.sh/update.sh が書き出す
                    /var/lib/sentinel/repo-path から読み、git fetch して
                    リモートに新しいコミットがあれば update.sh を自動で
                    実行する (CLAUDE.md #26)

core/config.py      設定の唯一の保管場所。型と範囲を強制する
core/state.py       モード状態機械。「今どのモードか」の唯一の決定者
core/supervisor.py  タスク監督。例外で落ちても指数バックオフで再起動する
core/audio.py       ALSA のアナログ出力カード (3.5mm、AUX) を特定する
                    find_output_card()。aplay -l の最初のカードを無条件
                    で使うと機体によって HDMI を掴むため、"Headphones"
                    優先 → "bcm2835" → 最初のカードの順で探す。
                    analog_device() がそこから sysdefault:CARD=<N>
                    (alsa-lib 自身の per-card dmix ルート、設定ファイル
                    不要) を組み立てる — music.py/bluetooth.py/voice.py
                    が共通で使う (CLAUDE.md #37/#72)。pcm_opens() は
                    ALSA デバイス名が実際に開けるかを /dev/zero の 1 秒
                    再生で試す (CLAUDE.md #46)。BGM の Bluetooth 出力先
                    (bluealsa:DEV=<MAC>,PROFILE=a2dp) はこのモジュールを
                    経由しない — こちらは AUX 専用、Bluetooth 出力の
                    デバイス文字列組み立ては music.current_output_device()
                    が担う (CLAUDE.md #73)

modules/camera.py       カメラ (別プロセス)。動体検知 -> MODE.report_motion()
                         個別カメラの上書き設定は config の camera_overrides
                         (カメラID -> {設定キー: 値}) で持つ。camera.py の
                         effective_settings()/set_overrides() が唯一の窓口。
                         オートフォーカスの再合焦を動体と誤検知する機種向けに
                         cam_autofocus (無効化)・motion_area_max_ratio (画面
                         全体が一度に変化するケースを上限で除外)・
                         motion_warmup_seconds (開いた直後は判定を休止) を持つ。
                         USB 帯域不足による破損フレーム (単色ブロック化/フレーム
                         混在、およびタイル状に同じ柄が繰り返し出現するモザイク化)
                         は _frame_corruption_ratio() で検出し、latest.jpg
                         への公開・動体判定・保存の前に捨てる (CLAUDE.md #19/#73)。
                         動体判定自体もヒステリシスを持つ — motion_confirm_checks
                         回連続で閾値超えが続いて初めて「開始」、
                         motion_release_checks 回連続で閾値割れが続いて初めて
                         「終了」とする (1 回の判定をそのまま公開しない、
                         CLAUDE.md #21)。破損が強制再接続 (#19) を
                         _CORRUPT_REBOOT_THRESHOLD 回繰り返しても解消しない
                         場合は、まず corrupt_disconnect_seconds 秒だけ
                         カメラを完全に切断する中間段階へ進み (disconnect_until
                         が明けるまで open_cam() を呼ばない)、それでも解消
                         しなければ corrupt_reboot_request を書き、
                         ON_CORRUPT_REBOOT フック経由で Pi 再起動を要求する
                         (実際の再起動は maintenance.emergency_reboot() に
                         委譲、CLAUDE.md #22/#60)
modules/music.py        mpg123 制御、位置復帰、yt-dlp キュー。出力先は
                         Player.bt_output_addr が立っていれば
                         bluealsa:DEV=<MAC>,PROFILE=a2dp (Bluetooth 出力、
                         bluetooth.output_loop() が set_bt_output() 経由で
                         のみ書き換える)、無ければ alsa_device/
                         core/audio.analog_device() (sysdefault:CARD=<N>、
                         AUX) を使う (CLAUDE.md #72/#73)。音量・EQ は
                         _active_output_profile() が現在の出力先ごとに
                         別領域 (AUX = music_volume/music_eq_*、Bluetooth
                         出力機器 = bt_output_profiles[addr]) から読む —
                         出力先を切り替えても前の出力先の音量を引き継がない
                         (CLAUDE.md #73)。current_output_device() が
                         voice.py 向けに今の実効出力デバイス文字列を返す。
                         イコライザー (enabled/bands は出力先ごとに別、
                         music_eq_track_overrides だけ共通) は mpg123
                         自身のリモート EQ コマンド (`E <ch> <band>
                         <gain>`、32 サブバンド) を Player._apply_eq() が
                         直接送る — 再生中に即座に反映され、mpg123 の
                         再起動も外部設定ファイルも不要 (CLAUDE.md #72、
                         旧 #32/#35 の LADSPA 実装を置き換え)。mpg123
                         プロセスを意図して落とす (eco/Bluetooth 受信/
                         voice の退避、出力先切り替え、stop()) 前には
                         必ず _gen を進める。_read_loop() は自分が読んで
                         いるプロセスの世代を固定引数で持ち、@P 0 などを
                         処理する前に現在の _gen と一致するか確認してから
                         でないと _advance_and_play() を呼ばない — 世代が
                         古ければ suspended_by の値に関わらず無視する
                         (CLAUDE.md #50)。yt-dlp はプレイリスト URL を
                         そのまま取得でき (--no-playlist を付けない)、
                         --progress-template の機械可読な進捗行を都度
                         DOWNLOADS の該当 entry (percent/eta/item_index/
                         item_count/message) へ反映する (CLAUDE.md #52)。
                         begin_voice_interrupt()/end_voice_interrupt() が
                         voice.py の唯一の窓口 — AUX 出力中は
                         duck_volume_for_voice() で音量だけ一時的に下げ、
                         Bluetooth 出力中は pause_for_voice() で一時停止
                         する (bluealsa の A2DP ソース PCM は同時に開ける
                         クライアントが 1 つだけのため、CLAUDE.md #73)。
                         カテゴリー (「勉強用」「休憩用」) は MUSIC_DIR
                         直下のサブフォルダそのもの。music_category_filter
                         で再生対象を絞り込み、move_track()/
                         find_track_path() で曲名からカテゴリーをまたいで
                         実ファイルを扱う (CLAUDE.md #56)
modules/thermal.py      温度と CPU -> MODE.report_temperature()
modules/bluetooth.py    受信 (電話 -> Pi、A2DP シンク) と送信 (Pi -> ヘッド
                         ホン、A2DP ソース) の両方を扱う。受信は loop() が
                         接続検知 -> 音楽の退避と復帰 (STATE)。送信は
                         output_loop() が bt_output_device (設定タブ/
                         音楽タブで選んだ MAC) への接続を解除するまで自動で
                         維持し (OUTPUT_STATE)、状態が変わるたびに
                         music.set_bt_output() で伝える。接続確立の確認は
                         どちらも bluetoothctl の "Connected: yes" だけに
                         頼らず、bluealsa-cli list-pcms の D-Bus パス
                         (受信 = .../a2dpsnk、送信 = .../a2dpsrc) で行う
                         (CLAUDE.md #73)。この Pi 自身の表示名
                         (set_local_name、bluetoothctl system-alias) と
                         相手端末のエイリアス (set_alias、D-Bus 直叩き) は
                         別物なので混同しないこと。受信端末ごとの音量は
                         bluealsa-cli (soft-volume を on にしてから
                         volume を書く、0-127) を使う — numid=1 (共有
                         ハードウェアレジスタ) はもう触らない (CLAUDE.md
                         #72)。送信 (BGM 出力) 側の音量/EQ は music.py の
                         bt_output_profiles が持つ (CLAUDE.md #73)。
                         remove_device() がペアリング済み端末を削除する
                         (bluetoothctl remove、Web UI の削除ボタンから
                         呼ばれる) — bt_device_volumes/bt_output_profiles
                         のその MAC 分もあわせて消す (CLAUDE.md #75)
modules/bt_agent.py     Bluetooth ペアリングエージェント (org.bluez.Agent1
                         を D-Bus に直接エクスポート、dbus-next 使用)。
                         **main.py から spawn されている** (CLAUDE.md #75
                         で再有効化 — 以前の機能停止は登録レースではなく
                         dbus-next の戻り値注釈バグが原因と判明、修正済み)。
                         RequestConfirmation/RequestAuthorization/
                         AuthorizeService は即座に承認せず、`PENDING` へ
                         積んで Web UI の承認 (`list_pending()`/
                         `decide()`、`/api/bluetooth/pending*`) を待つ —
                         20 秒以内に応答が無ければ拒否 (安全側)。
                         discoverable/pairable の時限窓 (#74) の内側に
                         重ねる追加の防御層で、窓が開いていても Web UI で
                         明示的に許可した接続だけがペアリングされる
                         (CLAUDE.md #75)。RegisterAgent()/
                         RequestDefaultAgent() の成否は同期呼び出しの
                         例外の有無でそのまま判定する (bluetoothctl の
                         テキストスクレイピングには依存しない)。
                         InterfacesAdded シグナルを購読して新規端末を
                         即座に trust し、起動時には既知端末も一括で
                         trust する。bus.wait_for_disconnect() でバス
                         切断 (bluetoothd 再起動など) を検知し、外側の
                         ループが指数バックオフで再接続・再登録する。
                         dbus-next が無ければ警告して何もしない段階的
                         劣化 (CLAUDE.md #72)
modules/hotspot.py       WiFi ホットスポット SSID の表示・変更
                         (sentinel-set-hotspot-ssid.sh を sudo 経由で呼ぶ)
modules/terminal.py     pty over WebSocket
modules/netlog.py       AdGuard querylog -> サービス名変換。AdGuard は
                         querylog を新しい順で返すため、日次 JSONL ファイル
                         へは古い順に反転してから書き、RECENT (直近一覧)
                         へは新しい順のまま積む — 両者の想定する向きが逆な
                         ため、揃えて反転させると同じ時刻順の不具合に戻る
                         (CLAUDE.md #34)
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
modules/voice.py        Open JTalk 優先/espeak-ng フォールバックの音声
                         アナウンス (時報・エラー・カメラ再起動・その他
                         システムイベント)。mpg123 の音楽ライブラリとは
                         別経路、_device() は music.current_output_device()
                         をそのまま使う — 音楽と時報は常に同じ出力先
                         (AUX か、選択中の Bluetooth 出力機器) から聞こえる
                         (CLAUDE.md #73)。アナウンス中に音楽をどう扱うかは
                         music.begin_voice_interrupt()/end_voice_interrupt()
                         に一任する — AUX 中は voice_duck_percent の設定に
                         従って音量だけ一時的に下げ、Bluetooth 出力中は
                         一時停止する (bluealsa の A2DP ソース PCM は同時に
                         開けるクライアントが 1 つだけのため、CLAUDE.md
                         #73、旧 #54 の全面 AUX 前提から変更)。音量は
                         _scale_wav() が TTS/効果音の WAV サンプルを
                         Python 側で直接スケールする — ALSA のミキサー/
                         softvol は一切経由しない (CLAUDE.md #72)。効果音
                         (チャイム) 自体の音量は voice_chime_volume で
                         voice_volume (読み上げ本体) と独立している
                         (CLAUDE.md #73)。時報は voice_time_interval_minutes
                         (既定 30 分、60 の約数を推奨) の壁時計境界で鳴る
                         (CLAUDE.md #53)。時報の文面は _time_values()
                         ({hour}/{minute}/{minute_part}/{weekday}/
                         {hour12}/{ampm}/{month}/{day}、CLAUDE.md #73) と
                         _fmt() が全カテゴリ共通で補う {time} を使って
                         組み立てる。既定文は {minute_part} を使い、0 分の
                         ときは「〜時です」(「〜時0分です」にならない、
                         CLAUDE.md #58)。voice_chime_enabled が有効なら
                         時報カテゴリだけ _play_chime() が TTS と並行して
                         短い効果音を鳴らす (Popen で開始し、finally で
                         回収 - 待ってから喋り始めない、CLAUDE.md #58)。
                         voice_chime_path で内蔵の合成音の代わりに任意の
                         .wav/.mp3 を指定できる (.mp3 は単発 mpg123
                         (音量は -f スケール)、.wav は aplay、存在しなけ
                         れば合成音へフォールバック、CLAUDE.md #68/#72)。
                         speak_test_time() は time_signal_loop() と同じ
                         _time_values()・チャイム条件で、壁時計の境界を
                         待たずに今すぐ 1 回だけテスト再生する
                         (CLAUDE.md #71/#73)

web/routes.py           全 HTTP / WebSocket エンドポイント。latest.jpg の
                         ように他プロセスが継続的に上書きするファイルは
                         FileResponse (stat とオープンが別ステップ) では
                         配信せず、read_bytes() で 1 回読んで Response に
                         渡す (CLAUDE.md #23)。fire-and-forget な
                         asyncio.create_task() (定時処理の手動実行・
                         Web UI からの再起動) は必ず _spawn() (main.py の
                         _background_tasks と同じ強参照パターン) を経由
                         する — レスポンスを返した直後にスタックフレームが
                         消える HTTP ハンドラでは、参照を保持しないと GC に
                         タスクを回収され、定時処理が「実行中」のまま永久に
                         固まる (CLAUDE.md #59)。/api/bluetooth/output
                         (GET/POST) は BGM の出力先候補・状態の取得と
                         選択/解除、/api/music/eq/bt/{addr} は Bluetooth
                         出力機器ごとの EQ — どちらも固定パスなので
                         /api/bluetooth/{action} より前に登録すること
                         (このファイル冒頭のコメント参照、CLAUDE.md #73)
web/static/index.html   単一ファイル SPA。イベントページのタイムライン表示
                         (renderEventsRecall() 以下) が既定表示。URL アクセス
                         トラックは buildNetSpans()/packNetRows() で「点」
                         ではなく期間の横棒・複数行として描く。#tl-scroll に
                         ホイール (拡大縮小)・Shift+ホイール (横移動)・
                         ドラッグ (範囲選択 -> tlZoomToRange() でその範囲
                         だけ拡大、本体と同じ見た目のまま) を束縛している
                         (CLAUDE.md #36/#38)。#p-dash に既定表示の class
                         を直書きせず、</main> 直後のインライン script が
                         location.hash を見てその場で正しいページだけを
                         可視化する (メインスクリプトの読み込みを待つと、
                         Tailscale 越しなど往復が伸びる経路で「一瞬
                         ダッシュボードが見えてから遷移先が見える」症状に
                         なる、CLAUDE.md #49)。音楽タブの「BGM 出力先」
                         カードが Bluetooth 出力機器の選択/解除、イコラ
                         イザーカードの #eq-scope は "global"/"track" に
                         加えペアリング済み端末ごとの "bt:<MAC>" を動的に
                         追加する — AUX 全体設定・曲ごとの上書き・
                         Bluetooth 出力機器ごとの設定を同じ UI で切り替える
                         (CLAUDE.md #73)
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
  リバースプロキシ (Caddy など) を前に置いてください。公開 HTTPS 無しに
  外部から安全にアクセスしたいだけなら Tailscale の導入を検討してください
  (CLAUDE.md #39) — この前提を崩さずに到達経路だけを広げられます。
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
