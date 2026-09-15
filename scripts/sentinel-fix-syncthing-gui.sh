#!/usr/bin/env bash
# Makes Syncthing's GUI reachable at :8384 from a LAN browser.
#
# Syncthing binds its GUI to 127.0.0.1:8384 by default (upstream default,
# unchanged by DietPi's package - see
# https://github.com/MichaIng/DietPi/issues/3329). setup.sh H8,
# SETUP.md/SETUP.ja.md and install.sh's own closing summary all tell the
# user to open http://<Pi-IP>:8384 to set the GUI password, which a
# loopback-only bind makes impossible (ERR_CONNECTION_REFUSED - the kernel
# RSTs because nothing is bound on the LAN interface).
#
# Two earlier attempts at this failed on real hardware, and both reasons
# are baked into this script:
#
#   1. The GUI address is an XML ELEMENT, <address>127.0.0.1:8384</address>,
#      not an address="..." attribute on <gui>
#      (https://docs.syncthing.net/users/config.html). A sed pattern for the
#      attribute form silently matches nothing.
#   2. **Syncthing keeps its whole config in memory and writes it back over
#      config.xml when it shuts down.** Editing the file while Syncthing is
#      running and then restarting therefore throws the edit away - the
#      shutdown write wins. Syncthing's own docs and forum say to stop
#      Syncthing, edit, then start. This script does exactly that, and it
#      is the reason the previous "sed then systemctl restart" version
#      reported success while changing nothing.
#
# It also never assumes where config.xml lives. `syncthing --paths` is
# authoritative, and matters here: Syncthing 1.27+ moved the default
# config location on Unix to $XDG_STATE_HOME/syncthing, so the
# DietPi/bind-mount path this project uses is not guaranteed to be the
# file the running daemon actually reads.
#
# Idempotent: a GUI already bound to a non-loopback address exits
# immediately without stopping anything. Called from install.sh (STEP 5)
# and Guardian, so `sudo ./update.sh` applies it with no separate action.
#
#   Usage: sentinel-fix-syncthing-gui.sh [--quiet]
#   Exit:  0 nothing needed | 10 fixed something | 1 could not fix

set -uo pipefail

STORAGE="${SENTINEL_STORAGE:-/mnt/VIDEOSD}"
ST_USER=dietpi
PORT=8384
QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1

[[ $EUID -eq 0 ]] || { echo "sentinel-fix-syncthing-gui.sh: must run as root" >&2; exit 1; }

say() { (( QUIET )) || printf '  %s\n' "$*"; }
ok()  { (( QUIET )) || printf '  [OK] %s\n' "$*"; }
w()   { printf '  [!!] %s\n' "$*" >&2; }

command -v syncthing >/dev/null 2>&1 || [[ -x /opt/syncthing/syncthing ]] || exit 0

ST_BIN=$(command -v syncthing 2>/dev/null)
[[ -n "$ST_BIN" ]] || ST_BIN=/opt/syncthing/syncthing

listening_non_loopback() {
  # Anything bound to a non-loopback address (0.0.0.0, ::, or a real IP)
  # on $PORT counts as reachable. Checking the live socket rather than the
  # config file is the same "test the real thing" discipline the storage
  # and audio repair scripts use (CLAUDE.md #8).
  command -v ss >/dev/null 2>&1 || return 1
  ss -ltn 2>/dev/null | awk -v p=":$PORT" '
    $4 ~ p"$" {
      addr = substr($4, 1, length($4) - length(p))
      if (addr != "127.0.0.1" && addr != "[::1]") { found = 1 }
    }
    END { exit found ? 0 : 1 }'
}

find_config() {
  local c
  # Authoritative: ask Syncthing itself, as the user the service runs as
  # (the answer depends on that user's HOME).
  c=$(runuser -u "$ST_USER" -- "$ST_BIN" --paths 2>/dev/null \
        | grep -oE '/[^[:space:]]*config\.xml' | head -n1)
  [[ -n "$c" && -f "$c" ]] && { echo "$c"; return 0; }
  for c in /mnt/dietpi_userdata/syncthing/config.xml \
           "$STORAGE/syncthing/config.xml" \
           "/home/$ST_USER/.local/state/syncthing/config.xml" \
           "/home/$ST_USER/.config/syncthing/config.xml" \
           /root/.local/state/syncthing/config.xml \
           /root/.config/syncthing/config.xml; do
    [[ -f "$c" ]] && { echo "$c"; return 0; }
  done
  return 1
}

if listening_non_loopback; then
  ok "Syncthing GUI is already reachable on :$PORT"
  exit 0
fi

CONFIG=$(find_config) || {
  w "could not locate Syncthing's config.xml (has Syncthing ever started?)"
  (( QUIET )) || {
    echo
    echo "  Run this to find it, then re-run this script:"
    echo "    runuser -u $ST_USER -- $ST_BIN --paths"
  }
  exit 1
}
say "Using config: $CONFIG"

if ! grep -qE '<address>(127\.0\.0\.1|\[?::1\]?|localhost):[0-9]+</address>' "$CONFIG"; then
  # Not loopback in the file, yet nothing is listening off-loopback:
  # Syncthing is probably just not running (or is still starting).
  w "GUI address in $CONFIG is not loopback, but nothing is listening on :$PORT"
  (( QUIET )) || {
    echo
    echo "  Check whether Syncthing is running at all:"
    echo "    systemctl status syncthing --no-pager; journalctl -u syncthing -n 40 --no-pager"
  }
  exit 1
fi

# Stop first. Syncthing rewrites config.xml from memory on shutdown, so
# editing a running instance and restarting silently discards the edit.
WAS_ACTIVE=0
if systemctl is-active --quiet syncthing 2>/dev/null; then
  WAS_ACTIVE=1
  systemctl stop syncthing 2>/dev/null || true
  # Give it a moment to finish its own config write before we touch the file.
  for _ in 1 2 3 4 5; do
    systemctl is-active --quiet syncthing 2>/dev/null || break
    sleep 1
  done
fi

cp -a "$CONFIG" "$CONFIG.sentinel-backup" 2>/dev/null || true
sed -i -E 's#<address>(127\.0\.0\.1|\[?::1\]?|localhost):([0-9]+)</address>#<address>0.0.0.0:\2</address>#' "$CONFIG"

if grep -qE '<address>0\.0\.0\.0:[0-9]+</address>' "$CONFIG"; then
  ok "GUI address set to 0.0.0.0:$PORT in $CONFIG"
else
  w "failed to rewrite the GUI address in $CONFIG"
  (( WAS_ACTIVE )) && systemctl start syncthing 2>/dev/null
  exit 1
fi

systemctl start syncthing 2>/dev/null || systemctl enable --now syncthing >/dev/null 2>&1 || true

for _ in 1 2 3 4 5 6 7 8 9 10; do
  listening_non_loopback && break
  sleep 1
done

if listening_non_loopback; then
  ok "Syncthing GUI is now reachable at http://<this-Pi-IP>:$PORT"
  exit 10
fi

w "rewrote the GUI address but nothing is listening on :$PORT yet"
(( QUIET )) || {
  echo
  echo "  Check these by hand:"
  echo "    systemctl status syncthing --no-pager"
  echo "    journalctl -u syncthing -n 40 --no-pager"
  echo "    grep -A2 '<gui' $CONFIG"
  echo "    ss -ltnp | grep $PORT"
}
exit 1
