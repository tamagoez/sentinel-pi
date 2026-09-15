#!/usr/bin/env bash
# Repairs the storage-mount fallout from the Syncthing/$STORAGE incident
# (CLAUDE.md #40): a real device on real hardware ended up (a) mounted at
# $STORAGE with options that never picked up a corrected /etc/fstab, and
# (b) mounted a SECOND time, directly (not via bind), at
# /mnt/dietpi_userdata/syncthing. Both were found together after a
# previous ownership fix (since removed) fought Guardian over the same
# mount every 2 minutes.
#
# Root causes this script checks for, in order:
#   1. $STORAGE mounted with stale options - `mount -a` only mounts *new*
#      fstab entries, so correcting /etc/fstab alone never fixes a
#      filesystem that was already mounted before the correction.
#   2. The same block device mounted a second time at some other path -
#      two live mounts of one exFAT/NTFS filesystem, if both are written
#      to, risk real data corruption.
#   3. /mnt/dietpi_userdata/syncthing mounted directly from the raw
#      device instead of via install.sh's own bind mount - DietPi's own
#      drive detection can win a boot-time race for this mountpoint
#      before install.sh's bind mount runs (CLAUDE.md #12 is the same
#      class of race, for Bluetooth/hostapd).
#
# Idempotent: a machine with none of these problems exits with nothing
# touched, no service ever stopped. Called from install.sh (STEP 4), so a
# plain `sudo ./update.sh` re-run picks this up automatically on every
# affected machine (CLAUDE.md #7) with no separate action needed.
#
# Anything this script cannot resolve on its own (e.g. a mount that stays
# busy through umount -l) is never left to fail silently: it is collected
# and printed as ready-to-paste manual commands at the end.

set -uo pipefail
# Deliberately not `-e`: each check must be able to fail and move on to
# the next one, and still print the manual-steps summary at the end.

STORAGE="${SENTINEL_STORAGE:-/mnt/VIDEOSD}"
SVC_USER="${1:-sentinel}"
ST_DEFAULT=/mnt/dietpi_userdata/syncthing
ST_HOME="$STORAGE/syncthing"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ $EUID -eq 0 ]] || { echo "sentinel-fix-syncthing-mount.sh: must run as root" >&2; exit 1; }

c()  { printf '\n\033[1;36m-- %s --\033[0m\n' "$*"; }
ok() { printf '  [OK] %s\n' "$*"; }
w()  { printf '  [!!] %s\n' "$*"; }

MANUAL_STEPS=()
manual() { MANUAL_STEPS+=("$1"); }

can_write() {
  # $1 = directory, $2 = user
  runuser -u "$2" -- sh -c ': > "$1/.sentinel-mount-test.$$" && rm -f "$1/.sentinel-mount-test.$$"' _ "$1" 2>/dev/null
}

force_umount() {
  local target="$1" i
  mountpoint -q "$target" 2>/dev/null || return 0
  for i in 1 2 3 4 5; do
    umount "$target" 2>/dev/null && return 0
    sleep 1
  done
  umount -l "$target" 2>/dev/null
  sleep 1
  ! mountpoint -q "$target" 2>/dev/null
}

STOPPED=0
ensure_stopped() {
  (( STOPPED )) && return 0
  systemctl stop sentinel sentinel-guardian.service syncthing 2>/dev/null || true
  STOPPED=1
}

c "1/3 Checking $STORAGE itself"
if ! mountpoint -q "$STORAGE" 2>/dev/null; then
  mount "$STORAGE" 2>/dev/null && ok "mounted $STORAGE from fstab" \
    || w "$STORAGE is not mounted and could not be mounted from fstab"
fi

if mountpoint -q "$STORAGE" 2>/dev/null; then
  FSTAB_LINE=$(grep -E "[[:space:]]${STORAGE//\//\\/}[[:space:]]" /etc/fstab 2>/dev/null | grep -v '^#' | tail -n1)
  WANT_UID=$(grep -oE 'uid=[0-9]+' <<<"$FSTAB_LINE" | head -n1)
  LIVE_UID=$(findmnt -no OPTIONS "$STORAGE" 2>/dev/null | grep -oE 'uid=[0-9]+' | head -n1)
  if [[ -n "$WANT_UID" && "$WANT_UID" != "$LIVE_UID" ]]; then
    w "$STORAGE is mounted with stale options ($LIVE_UID; fstab now wants $WANT_UID) - remounting"
    ensure_stopped
    if force_umount "$STORAGE"; then
      systemctl daemon-reload
      mount "$STORAGE" 2>/dev/null
    fi
    LIVE_UID2=$(findmnt -no OPTIONS "$STORAGE" 2>/dev/null | grep -oE 'uid=[0-9]+' | head -n1)
    if [[ "$LIVE_UID2" == "$WANT_UID" ]]; then
      ok "$STORAGE now mounted with $WANT_UID"
    else
      w "$STORAGE still not using $WANT_UID after a remount attempt"
      manual "umount -l $STORAGE; systemctl daemon-reload; mount $STORAGE; findmnt -no OPTIONS $STORAGE"
    fi
  elif [[ -z "$WANT_UID" ]]; then
    w "no uid= option found in the $STORAGE fstab line - cannot verify it is current"
    manual "grep '$STORAGE' /etc/fstab   # confirm the line has uid=/gid=/umask="
  else
    ok "$STORAGE options already match fstab ($LIVE_UID)"
  fi
else
  w "$STORAGE could not be brought up"
  manual "findmnt $STORAGE; grep '$STORAGE' /etc/fstab; dmesg | tail -30"
fi

c "2/3 Checking for a second, direct mount of the same device"
if mountpoint -q "$STORAGE" 2>/dev/null; then
  DEV=$(findmnt -no SOURCE "$STORAGE" 2>/dev/null | tail -n1)
  if [[ -n "$DEV" ]]; then
    mapfile -t DUPES < <(findmnt -rno TARGET,SOURCE 2>/dev/null \
      | awk -v dev="$DEV" -v self="$STORAGE" '$2==dev && $1!=self {print $1}')
    if (( ${#DUPES[@]} )); then
      for dupe in "${DUPES[@]}"; do
        w "$DEV is also mounted directly at $dupe - unmounting the duplicate"
        ensure_stopped
        if force_umount "$dupe"; then
          ok "unmounted duplicate mount at $dupe"
        else
          w "could not unmount $dupe (still busy after retries and a lazy unmount)"
          manual "fuser -vm $dupe; umount -l $dupe"
        fi
      done
    else
      ok "no duplicate mounts of $DEV found"
    fi
  fi
else
  w "skipped - $STORAGE is not mounted"
fi

c "3/3 Checking $ST_DEFAULT (Syncthing's bind target)"
if [[ -d "$ST_DEFAULT" ]] && mountpoint -q "$ST_DEFAULT" 2>/dev/null; then
  CUR_SRC=$(findmnt -no SOURCE "$ST_DEFAULT" 2>/dev/null | tail -n1)
  if [[ "$CUR_SRC" != "$ST_HOME"* ]]; then
    w "$ST_DEFAULT is mounted from $CUR_SRC, not our bind mount ($ST_HOME) - clearing it"
    ensure_stopped
    if force_umount "$ST_DEFAULT"; then
      ok "cleared $ST_DEFAULT (install.sh's own Syncthing step re-binds it next)"
    else
      w "could not unmount $ST_DEFAULT"
      manual "fuser -vm $ST_DEFAULT; umount -l $ST_DEFAULT"
    fi
  else
    ok "$ST_DEFAULT is already our own bind mount"
  fi
else
  ok "$ST_DEFAULT not mounted yet (install.sh's own step sets it up)"
fi

c "Write test"
if mountpoint -q "$STORAGE" 2>/dev/null && [[ -d "$STORAGE/sentinel" ]] && can_write "$STORAGE/sentinel" "$SVC_USER"; then
  ok "$SVC_USER can write to $STORAGE/sentinel"
elif [[ -d "$STORAGE/sentinel" ]]; then
  w "$SVC_USER still cannot write to $STORAGE/sentinel"
  manual "$SELF_DIR/sentinel-fix-storage-owner.sh $STORAGE/sentinel $STORAGE $SVC_USER"
fi

if (( STOPPED )); then
  systemctl start sentinel 2>/dev/null || true
  # sentinel-guardian and syncthing are intentionally left stopped here -
  # install.sh's own later steps (Syncthing storage prep, Guardian
  # registration) bring them back up once this run has actually finished
  # setting ownership/bind mounts; starting them mid-repair would race
  # this script's own checks above.
fi

if (( ${#MANUAL_STEPS[@]} )); then
  echo
  echo "======================================================================"
  echo " sentinel-fix-syncthing-mount.sh could not fix everything automatically."
  echo " Run the following by hand, in order:"
  echo "======================================================================"
  for step in "${MANUAL_STEPS[@]}"; do
    echo
    echo "  $step"
  done
  echo
fi

exit 0
