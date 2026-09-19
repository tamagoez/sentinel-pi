#!/usr/bin/env bash
# Resolves "bluealsa cannot acquire its D-Bus name", which on real
# hardware cascaded into the whole audio stack being unusable.
#
# The symptom in the journal is one line, repeated forever:
#
#   bluealsa: E: main.c:137: Couldn't acquire D-Bus name.
#             Please check D-Bus configuration. Requested name: org.bluealsa
#
# A D-Bus well-known name has exactly one owner. bluealsa exits when it
# cannot take org.bluealsa, systemd restarts it, and it exits again -
# 492 failures in two hours on the reported machine. That alone would
# only cost Bluetooth audio, but it does not stop there:
# sentinel-bluealsa-aplay.service (Requires= the above) is torn down and
# started again on every one of those cycles, and each cycle opens and
# closes the ALSA default device. The bcm2835 driver does not always
# survive that ("failed to close VCHI service connection (status=-11)"),
# and once it is wedged, dmix can no longer open hw:N,0 - so
# `aplay -D sysdefault:CARD=N` fails with "Invalid argument", mpg123 cannot
# start either, and music stops working for a reason that looks nothing
# like a Bluetooth problem.
#
# So this fixes the one root cause rather than its three symptoms: find
# who actually owns org.bluealsa and get out of their way (or them out of
# ours). Almost always that is a second bluealsa instance - the
# distribution's own bluealsa.service running alongside the one this
# project installs, which needs `-p a2dp-sink`.
#
# dbus-send is used to ask the bus directly rather than guessing from
# process lists; it ships with bluez itself, so it is always present
# wherever bluealsa is (same reasoning as CLAUDE.md #15).
#
#   Usage: sentinel-fix-bluealsa.sh [--quiet]
#   Exit:  0 nothing needed | 10 fixed something | 1 could not fix

set -uo pipefail

QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1
OURS=sentinel-bluealsa.service
BUS_NAME=org.bluealsa

[[ $EUID -eq 0 ]] || { echo "sentinel-fix-bluealsa.sh: must run as root" >&2; exit 1; }

say() { (( QUIET )) || printf '  %s\n' "$*"; }
ok()  { (( QUIET )) || printf '  [OK] %s\n' "$*"; }
w()   { printf '  [!!] %s\n' "$*" >&2; }
fixed_msg() { printf '  [FIXED] %s\n' "$*"; }

systemctl list-unit-files "$OURS" >/dev/null 2>&1 || exit 0

# Healthy already? Do nothing at all - this runs every Guardian cycle.
if systemctl is-active --quiet "$OURS" 2>/dev/null \
   && ! systemctl is-failed --quiet "$OURS" 2>/dev/null; then
  ok "$OURS is running"
  exit 0
fi

# Only act on the specific failure this script understands. Any other
# reason for being down is someone else's to diagnose - starting to guess
# here is how a repair script becomes the next bug.
if ! journalctl -u "$OURS" -n 50 --no-pager 2>/dev/null \
     | grep -q "Couldn't acquire D-Bus name"; then
  say "$OURS is down, but not with a D-Bus name conflict - not this script's case"
  exit 0
fi

owner_pid() {
  local owner pid
  command -v dbus-send >/dev/null 2>&1 || return 1
  owner=$(dbus-send --system --dest=org.freedesktop.DBus --print-reply \
            /org/freedesktop/DBus org.freedesktop.DBus.GetNameOwner \
            "string:$BUS_NAME" 2>/dev/null | awk -F'"' '/string/ {print $2}')
  [[ -n "$owner" ]] || return 1
  pid=$(dbus-send --system --dest=org.freedesktop.DBus --print-reply \
          /org/freedesktop/DBus org.freedesktop.DBus.GetConnectionUnixProcessID \
          "string:$owner" 2>/dev/null | awk '/uint32/ {print $2}')
  [[ -n "$pid" ]] || return 1
  echo "$pid"
}

PID=$(owner_pid) || {
  w "$BUS_NAME has no owner, yet bluealsa still cannot acquire it"
  w "that points at D-Bus policy, not a conflict - check /etc/dbus-1/system.d/"
  (( QUIET )) || {
    echo
    echo "  Look for the bluealsa policy file and whether root may own the name:"
    echo "    ls -l /etc/dbus-1/system.d/ /usr/share/dbus-1/system.d/ | grep -i bluealsa"
    echo "    journalctl -u dbus -n 40 --no-pager"
  }
  exit 1
}

# Whose process is it? A systemd unit gives us something we can disable;
# an orphan does not.
UNIT=$(ps -o unit= -p "$PID" 2>/dev/null | tr -d ' ')
CMD=$(ps -o cmd= -p "$PID" 2>/dev/null)
say "$BUS_NAME is owned by pid $PID (${UNIT:-no unit}): $CMD"

if [[ -n "$UNIT" && "$UNIT" != "-" && "$UNIT" != "$OURS" ]]; then
  # A second bluealsa under its own unit. Ours carries the -p a2dp-sink
  # this project needs (install.sh writes that ExecStart), so ours is the
  # one to keep; stop and disable the other rather than leaving two units
  # fighting over one name forever.
  systemctl disable --now "$UNIT" >/dev/null 2>&1 || true
  if systemctl is-active --quiet "$UNIT" 2>/dev/null; then
    w "could not stop the conflicting unit $UNIT"
    exit 1
  fi
  fixed_msg "disabled $UNIT - it was holding $BUS_NAME and blocking $OURS"
  systemctl reset-failed "$OURS" 2>/dev/null || true
  systemctl start "$OURS" 2>/dev/null || true
  sleep 2
  if systemctl is-active --quiet "$OURS" 2>/dev/null; then
    fixed_msg "$OURS is running now"
    exit 10
  fi
  w "$OURS still will not start after freeing $BUS_NAME"
  exit 1
fi

if [[ "$UNIT" == "$OURS" ]]; then
  # Our own unit already owns the name while systemd thinks it is down -
  # a leftover process from a previous start. Restarting the unit makes
  # systemd adopt one process again instead of racing its own orphan.
  systemctl reset-failed "$OURS" 2>/dev/null || true
  systemctl restart "$OURS" 2>/dev/null || true
  sleep 2
  systemctl is-active --quiet "$OURS" 2>/dev/null && { fixed_msg "restarted $OURS over its own stale process"; exit 10; }
  w "$OURS still down after a restart"
  exit 1
fi

w "$BUS_NAME is held by pid $PID which belongs to no systemd unit (orphan)"
(( QUIET )) || {
  echo
  echo "  Inspect it, and if it is a stray bluealsa, stop it and start ours:"
  echo "    ps -fp $PID"
  echo "    kill $PID && systemctl reset-failed $OURS && systemctl start $OURS"
}
exit 1
