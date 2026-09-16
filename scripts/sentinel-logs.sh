#!/usr/bin/env bash
# sentinel-logs - print a short, copy-pasteable digest of what is wrong
#
#   sentinel-logs          the last 2 hours
#   sentinel-logs 12       the last 12 hours
#   sentinel-logs 2 full   do not truncate long messages
#
# Why this exists, next to sentinel-diagnose:
#   sentinel-diagnose writes a tar.gz for deep investigation. That is the
#   wrong shape when someone just needs to paste "what is broken" into a
#   chat or an issue. The raw journal is far too long for that, and the
#   interesting lines repeat - a permission failure that restarts every
#   second produces the same sentence dozens of times, which buries the
#   one line that actually differs.
#
# So this collapses identical messages into a single line with a count
# ("x14"), keeps only lines that look like a problem, and puts a short
# state block on top so the numbers have context. Output is plain text on
# stdout: select all, paste, done.
#
# Root (or membership of systemd-journal) is needed to read the journal.
# Without it the state block still prints and the script says what it
# could not read, rather than silently showing nothing.
set -uo pipefail

HOURS="${1:-2}"
FULL="${2:-}"
[[ "$HOURS" =~ ^[0-9]+$ ]] || { echo "usage: sentinel-logs [hours] [full]" >&2; exit 1; }
MAXLEN=200
[[ "$FULL" == "full" ]] && MAXLEN=100000
MAXMSG=25

STORAGE="${SENTINEL_STORAGE:-/mnt/VIDEOSD}"
SVC_USER=sentinel

hdr() { printf '\n----- %s -----\n' "$*"; }
kv()  { printf '%-22s %s\n' "$1" "$2"; }

echo "===== sentinel-logs (last ${HOURS}h) ====="
kv "date" "$(date '+%Y-%m-%d %H:%M:%S %Z')"
kv "uptime" "$(uptime -p 2>/dev/null || uptime)"
command -v vcgencmd >/dev/null 2>&1 && kv "temp" "$(vcgencmd measure_temp 2>/dev/null)"

hdr "services"
for u in sentinel.service sentinel-guardian.timer sentinel-lockstep-sync.service \
         bluetooth.service hciuart.service hostapd.service adguardhome.service \
         sentinel-bluealsa.service sentinel-bluealsa-aplay.service \
         sentinel-bt-agent.service sentinel-autoupdate.timer; do
  systemctl list-unit-files "$u" >/dev/null 2>&1 || continue
  state=$(systemctl is-active "$u" 2>/dev/null)
  [[ -n "$state" ]] || state="unknown"
  systemctl is-failed --quiet "$u" 2>/dev/null && state="$state (FAILED)"
  # A unit pinned at start-limit-hit ignores every `systemctl start` until
  # reset-failed, so call that out by name rather than just "failed"
  # (CLAUDE.md #9 - this has bitten the old Syncthing setup and the
  # BlueALSA units).
  if systemctl show -p Result --value "$u" 2>/dev/null | grep -q start-limit; then
    state="$state (start-limit-hit: needs systemctl reset-failed $u)"
  fi
  kv "$u" "$state"
  # hciuart.service sitting at "inactive" is its normal resting state once
  # it has attached the UART. Guardian no longer tries to model this from
  # Type=/Result= (CLAUDE.md #51 guessed Type=oneshot, then Type=forking
  # with Result=success, and real hardware kept restarting it every cycle
  # regardless of both) - check_services() now only restarts it on a
  # genuine ActiveState=failed and leaves "inactive" alone (CLAUDE.md #52).
  # Still print the actual property values here, labelled (not the
  # earlier `paste -sd/ -` line, whose field order depended on what
  # systemd happened to return for `-p X -p Y -p Z` in one call rather
  # than the order requested - it read "forking/no/success" on real
  # hardware and could not be matched back to Type=/RemainAfterExit=/
  # Result= with confidence). Kept for whatever the next unrelated
  # hciuart report turns out to need.
  if [[ "$u" == "hciuart.service" && "$state" == inactive* ]]; then
    kv "  $u" "Type=$(systemctl show "$u" -p Type --value 2>/dev/null) Result=$(systemctl show "$u" -p Result --value 2>/dev/null) RemainAfterExit=$(systemctl show "$u" -p RemainAfterExit --value 2>/dev/null)"
  fi
done

hdr "storage"
kv "$STORAGE" "$(findmnt -no SOURCE,FSTYPE,OPTIONS "$STORAGE" 2>/dev/null | tail -n1 || echo 'NOT MOUNTED')"
if runuser -u "$SVC_USER" -- test -w "$STORAGE/sentinel" 2>/dev/null; then
  kv "$SVC_USER can write" "yes ($STORAGE/sentinel)"
else
  kv "$SVC_USER can write" "NO ($STORAGE/sentinel) <-- sentinel will crash-loop"
fi
if [[ -x /usr/local/bin/lockstep-sync-server ]]; then
  LS_DATA="$STORAGE/lockstep-sync"
  # Runs as $SVC_USER (CLAUDE.md #61 - unlike the old Syncthing setup,
  # there is no separate user here to get wrong), so this is the same
  # write test as $STORAGE/sentinel above, just against its own directory.
  if runuser -u "$SVC_USER" -- test -w "$LS_DATA" 2>/dev/null; then
    kv "$SVC_USER can write" "yes ($LS_DATA)"
  else
    kv "$SVC_USER can write" "NO ($LS_DATA) <-- Lockstep Sync cannot start"
  fi
fi

hdr "audio"
CARD=$(aplay -l 2>/dev/null | grep -im1 'headphones' | grep -oE '^card [0-9]+' | awk '{print $2}')
[[ -n "$CARD" ]] || CARD=$(aplay -l 2>/dev/null | grep -im1 'bcm2835' | grep -oE '^card [0-9]+' | awk '{print $2}')
[[ -n "$CARD" ]] || CARD=$(aplay -l 2>/dev/null | grep -m1 -oE '^card [0-9]+' | awk '{print $2}')
kv "analog card" "${CARD:-NONE FOUND}"
if [[ -n "$CARD" ]]; then
  # Print each numid WITH the control's own name. A numid is only an
  # index, so the same number means different things on different cards -
  # labelling numid=3 "(route)" unconditionally is how real hardware came
  # to report "numid=3 (route) 230", a value no route enum can hold
  # (CLAUDE.md #44). The name is the fact; the number is just where it sat.
  for n in 1 2 3; do
    info=$(amixer -c "$CARD" cget "numid=$n" 2>/dev/null) || continue
    [[ -n "$info" ]] || continue
    nm=$(grep -m1 -oE "name='[^']*'" <<<"$info" | cut -d"'" -f2)
    [[ -n "$nm" ]] || continue
    kv "numid=$n $nm" "$(grep -m1 -oE ': values=[^ ]+' <<<"$info" | cut -d= -f2) $(grep -m1 -oE 'min=-?[0-9]+,max=-?[0-9]+' <<<"$info")"
  done
fi
kv "asound.conf card" "$(grep -oE 'pcm "hw:[0-9]+,0"' /etc/asound.conf 2>/dev/null | head -n1 | grep -oE '[0-9]+' | head -n1 || echo 'no /etc/asound.conf')"
kv "asound.conf EQ" "$(grep -q 'type ladspa' /etc/asound.conf 2>/dev/null && echo 'on (ladspa/mbeq)' || echo off)"

# "Listed in aplay -L" only means asound.conf parses. dmix opens its slave
# (hw:N,0) lazily, so a PCM can be listed and still refuse every open -
# and in that state music and voice announcements are both silent with no
# error anywhere. Test the real thing (CLAUDE.md #8); /dev/zero is digital
# silence, so nothing is audible. Skipped in the default run because each
# test costs a second - `sentinel-logs <hours> full` includes it.
if [[ "$FULL" == "full" ]]; then
  for dev in sentinel_music sentinel_voice "hw:${CARD:-0},0"; do
    if timeout 6 aplay -D "$dev" -f S16_LE -r 44100 -c 2 -d 1 -q /dev/zero >/dev/null 2>&1; then
      kv "open $dev" "ok"
    else
      kv "open $dev" "FAILS: $(timeout 6 aplay -D "$dev" -f S16_LE -r 44100 -c 2 -d 1 /dev/zero 2>&1 | tail -n1)"
    fi
  done
  holders=$(fuser -v /dev/snd/* 2>&1 | tail -n +2 | tr -s ' ' | paste -sd' ' -)
  [[ -n "$holders" ]] && kv "/dev/snd holders" "$holders"
else
  kv "sentinel_music PCM" "$(aplay -L 2>/dev/null | grep -qx sentinel_music && echo 'defined (run: sentinel-logs 2 full  to test it opens)' || echo MISSING)"
fi
kv "mpg123" "$(pgrep -a mpg123 2>/dev/null | head -n1 | cut -c1-120 || echo 'not running')"

hdr "tailscale"
if command -v tailscale >/dev/null 2>&1; then
  tsip=$(tailscale ip -4 2>/dev/null | head -n1)
  if [[ -n "$tsip" ]]; then
    kv "tailnet IP" "$tsip"
  else
    kv "tailnet" "not connected (run: sudo sentinel-tailscale up)"
  fi
else
  kv "tailscale" "not installed"
fi

hdr "listening ports"
if command -v ss >/dev/null 2>&1; then
  ss -ltn 2>/dev/null | awk 'NR>1 && ($4 ~ /:8080$/ || $4 ~ /:8384$/ || $4 ~ /:8083$/) {print "  " $4}' \
    | sort -u || true
  for p in 8080 8384 8083; do
    ss -ltn 2>/dev/null | awk -v p=":$p\$" '$4 ~ p {f=1} END {exit f?0:1}' \
      || echo "  :$p NOT LISTENING"
  done
else
  echo "  (ss not installed)"
fi

# ------------------------------------------------------------------ digest
# Collapse identical messages. The journal prefix (timestamp, host,
# unit[pid]) differs on every line even when the message is the same, so
# strip it before counting - that is what turns 40 pasted lines into 3.
digest() {
  local label="$1"; shift
  local raw
  raw=$("$@" 2>/dev/null) || true
  [[ -n "$raw" ]] || return

  # Emit "<last-seen>\t<count>\t<message>" per distinct message, then let
  # sort/tail pick the most RECENT ones. Showing the first N found is the
  # wrong end: after a fix is applied, the lines worth reading are the
  # newest, and a long-running crash loop would otherwise push them all
  # out with hours-old repeats (seen on real hardware - 167 newer messages
  # were dropped in favour of 25 stale ones).
  local rows total
  rows=$(printf '%s\n' "$raw" | awk '
    # Keep only lines that look like a problem. Deliberately broad: a
    # missed line costs another round-trip, an extra line costs nothing.
    !/ERR|WRN|Error|error|Warning|WARNING|Traceback|Failed|failed|FAILED|denied|refus|Cannot|cannot|Exception|FIXED|start-limit|repeated too quickly|Invalid|invalid|No such|timeout|Timeout/ { next }
    {
      ts = $1
      msg = $0
      # "<ts> <host> <unit[pid]>: <message>" - the unit token never
      # contains a colon, so [^:]* stops exactly at its trailing one.
      sub(/^[^ ]+ [^ ]+ [^:]*: /, "", msg)
      # Many daemons print their own timestamp inside the message as well.
      # Python logging adds milliseconds after a comma ("02:01:14,926");
      # without consuming those, every repeat of one message stays
      # "distinct" and nothing collapses at all - which is exactly what
      # happened to 180 identical mpg123 recovery lines on real hardware.
      sub(/^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][ T][0-9][0-9]:[0-9][0-9]:[0-9][0-9]([.,][0-9]+)? /, "", msg)
      if (msg == "") next
      if (!(msg in cnt)) n++
      cnt[msg]++
      last[msg] = ts
    }
    END { for (m in cnt) printf "%s\t%d\t%s\n", last[m], cnt[m], m }' | sort)
  [[ -n "$rows" ]] || return
  total=$(printf '%s\n' "$rows" | wc -l)

  hdr "$label"
  printf '%s\n' "$rows" | tail -n "$MAXMSG" | awk -F'\t' -v maxlen="$MAXLEN" '
    {
      t = $1
      # 2026-09-16T01:59:39+0900 -> 09-16 01:59:39
      if (length(t) > 18) t = substr(t, 6, 5) " " substr(t, 12, 8)
      m = $3
      if (length(m) > maxlen) m = substr(m, 1, maxlen) "..."
      # A few lines are loud but expected on this hardware. Say so inline
      # rather than hiding them: a reader who does not know that exFAT has
      # no Unix permissions will otherwise chase this one every time.
      if (m ~ /Failed to correct directory permissions/) m = m "   [expected: exFAT has no Unix permissions - harmless]"
      printf "  %s  x%-3d %s\n", t, $2, m
    }'
  if (( total > MAXMSG )); then
    echo "  (showing the $MAXMSG most recent of $total distinct messages - full list: sentinel-logs $HOURS full)"
  fi
}

export HOURS
SINCE="${HOURS} hours ago"

if journalctl -n1 --no-pager >/dev/null 2>&1; then
  digest "sentinel (app)"   journalctl -u sentinel --since "$SINCE" -o short-iso --no-pager
  digest "guardian"         journalctl -t sentinel-guardian --since "$SINCE" -o short-iso --no-pager
  digest "lockstep sync"    journalctl -u sentinel-lockstep-sync --since "$SINCE" -o short-iso --no-pager
  digest "bluetooth"        journalctl -u bluetooth -u sentinel-bluealsa -u sentinel-bluealsa-aplay \
                                       --since "$SINCE" -o short-iso --no-pager
  digest "system (errors)"  journalctl -p err --since "$SINCE" -o short-iso --no-pager
else
  hdr "journal"
  echo "  cannot read the journal as $(id -un) - re-run with: sudo sentinel-logs $HOURS"
fi

# Anything currently failed or stuck activating gets its last lines
# verbatim, filter bypassed. A crash-looping daemon is often the cause of
# everything else in the digest, and the filter above can miss its actual
# message entirely - real hardware showed sentinel-bluealsa failing 312
# times with only systemd's own "Failed with result 'exit-code'" visible
# and not one line saying why.
if journalctl -n1 --no-pager >/dev/null 2>&1; then
  BROKEN=""
  for u in sentinel.service sentinel-lockstep-sync.service bluetooth.service hciuart.service \
           hostapd.service adguardhome.service sentinel-bluealsa.service \
           sentinel-bluealsa-aplay.service sentinel-bt-agent.service; do
    systemctl list-unit-files "$u" >/dev/null 2>&1 || continue
    st=$(systemctl is-active "$u" 2>/dev/null)
    [[ "$st" == "failed" || "$st" == "activating" ]] || continue
    BROKEN="yes"
    hdr "last lines: $u ($st)"
    journalctl -u "$u" -n 12 -o cat --no-pager 2>/dev/null | sed 's/^/  /'
    # Which user it runs as, and whether systemd gives it a private mount
    # namespace. The latter matters more than it looks: any sandboxing
    # directive makes systemd build the unit its own mount namespace at
    # start, and a bind mount created on the host afterwards is not
    # visible inside it. The service then sees the plain underlying
    # directory while the host sees the bind-mounted one - so a write test
    # run from a shell passes while the service still gets EACCES, with
    # nothing in either output explaining the contradiction.
    systemctl show "$u" -p User -p Group -p SupplementaryGroups -p ExecStart \
      -p PrivateTmp -p PrivateMounts -p ProtectSystem -p ProtectHome \
      -p ReadWritePaths -p StateDirectory 2>/dev/null \
      | grep -vE '=$' | sed 's/^/    /'
  done
fi

echo
echo "===== end (paste everything above) ====="
