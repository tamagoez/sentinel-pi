#!/usr/bin/env bash
# sentinel-bt-agent.sh
#
# Persistent Bluetooth pairing agent, run by sentinel-bt-agent.service.
#
# This used to be bluez-tools' standalone "bt-agent --capability=
# NoInputNoOutput" binary. Since Raspberry Pi OS Bullseye (bluez 5.55+)
# that binary has a known regression: a NoInputNoOutput agent registered
# through it no longer auto-confirms incoming pairing requests - it just
# sits waiting for a manual approval that never comes in a headless
# systemd service. iOS has no fallback for this: it just reports
# "Pairing Unsuccessful" once its own confirmation request times out.
# (https://github.com/RPi-Distro/repo/issues/291)
#
# bluetoothctl's own built-in agent does not have this regression - typing
# "agent NoInputNoOutput" + "default-agent" in an interactive bluetoothctl
# session has always worked. So instead of the separate bt-agent binary,
# this script drives bluetoothctl the same way an interactive session
# would.
#
# Fixing the pairing prompt was not enough on its own: phones would still
# pair (bond) successfully but then show a connection that drops again
# within a second or two (iOS: briefly shows "Connected" in Settings, then
# reverts; Windows: fails outright). The agent's capability only covers
# the *pairing* confirmation - a later per-profile "Authorize service"
# request (asked again on every connection, e.g. for the A2DP sink) is a
# separate step that is NOT auto-answered by NoInputNoOutput, and
# bluetoothctl's own agent prints that prompt to stdout and waits to read
# an answer from stdin. In a piped, non-interactive session nothing ever
# answers it, so the service-level connection just times out right after
# the bond succeeds - exactly the "connects then immediately disconnects"
# symptom. BlueZ skips this prompt entirely for devices already marked
# Trusted, so this script drives bluetoothctl via a `coproc` (giving it
# both a writable stdin and a readable stdout in the same process) and
# watches its output for "Connected: yes" / "Paired: yes" lines, sending
# `trust <MAC>` back the moment a device appears - before the phone's own
# connection attempt has a chance to time out. It also trusts every
# already-paired device once at startup, since a device paired before
# this fix existed is stuck in exactly this same never-trusted state.
#
# This needs no extra package: bluetoothctl is part of bluez, already a
# hard dependency (CLAUDE.md "依存を増やさない").
#
# Trusting devices was still not enough for every report: some pairing
# attempts show a PIN/passkey confirmation on the remote device that, when
# dismissed instead of confirmed, ends in "could not pair" - and separately,
# bluetoothctl's agent can itself receive a "Confirm passkey NNNNNN
# (yes/no):" (or similar) prompt on its own stdin that nothing was ever
# answering, leaving the Pi side silently stuck while the remote device's
# own confirmation screen times out. The read loop below now answers any
# "(yes/no)" prompt with "yes" unconditionally (CLAUDE.md #64).
set -uo pipefail

coproc BTCTL { bluetoothctl; }

send() { printf '%s\n' "$1" >&"${BTCTL[1]}"; }

send "agent NoInputNoOutput"
send "default-agent"
send "power on"
send "discoverable on"
send "pairable on"

# 既にペアリング済みだが、この修正より前に接続していたため未信頼のままに
# なっている端末を、起動のたびに救済する。
for mac in $(bluetoothctl devices Paired 2>/dev/null | awk '{print $2}'); do
  send "trust $mac"
done

# bluetoothctl 自身のイベント出力 ("[CHG] Device XX:.. Connected: yes" 等)
# を監視し、端末が現れた瞬間に trust を打ち返す。journalctl にもそのまま
# 出力を残し、切り分けに使えるようにする。
#
# NoInputNoOutput の capability はあくまで「こちら側の agent が確認を
# 求められたときに何を答えるか」の申告でしかなく、ネゴシエーションが
# 必ず Just Works (双方とも無表示・無確認) に倒れることを保証するわけ
# ではない。実機以外でも広く報告されている挙動として、相手側の端末は
# capability に関わらず PIN/パスキーの確認画面を出すことがあり、それ
# 自体は正常 (ユーザーが Pair/確認を押せば繋がる)。問題は、まれに
# Pi 側の bluetoothctl agent 自身にも RequestConfirmation/RequestPinCode
# 等が飛んできて "Confirm passkey NNNNNN (yes/no):" のようなプロンプトを
# 標準出力へ出し、標準入力からの応答を待つケースがあることで、これまで
# このループは Connected/Paired/Bonded の行しか見ておらず、この種の
# プロンプトには一切応答しないまま固まっていた。相手側は「Pi 側からの
# 応答がいつまでも来ない」状態になり、結局 AuthenticationTimeout で
# ペアリング失敗として片付けられる — 「PIN が出て、無視/スキップしても
# ペアリングできなかったと表示される」という報告はこの形と一致する。
# "(yes/no)" を含むプロンプト行が来たら無条件で "yes" を返す (Confirm
# passkey・Confirm pairing・Authorize service のいずれも同じ書式なので
# 個別に文言を見分ける必要はない)。**この自動応答を外さないでください**
# — 同じ「Pi 側の agent が無応答のまま固まり、相手には PIN 画面だけが
# 残ってペアリングできない」不具合に戻ります。
while IFS= read -r line <&"${BTCTL[0]}"; do
  echo "$line"
  if [[ "$line" =~ Device\ ([0-9A-Fa-f:]{17})\ .*(Connected:\ yes|Paired:\ yes|Bonded:\ yes) ]]; then
    send "trust ${BASH_REMATCH[1]}"
  elif [[ "$line" == *'(yes/no)'* ]]; then
    send "yes"
  fi
done

# bluetoothctl 側が終了 (D-Bus 切断や bluetoothd の再起動など) すると上の
# read ループが抜けてここに来る。Restart=always (systemd unit 側) に
# 任せてそのまま終了する - 中途半端に居座らない。
