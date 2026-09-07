#!/usr/bin/env bash
# sentinel-fix-storage-owner.sh <data-dir> <mountpoint> <user>
#
# Makes sure <user> can write inside <data-dir>. Shared by install.sh
# (called once, unconditionally, right after the data directory is
# created) and by sentinel-guardian.sh (called every 2 minutes, but only
# pays for anything beyond the cheap write-test below when that test
# actually fails).
#
# Why this needs more than "chown -R": exFAT, NTFS and vfat have no Unix
# ownership of their own. The kernel/FUSE driver reports a fixed
# uid/gid/mode taken from the mount options to every process, regardless
# of what chown is asked to set - so chown either fails outright
# ("Operation not permitted") or silently no-ops. dietpi-drive_manager
# does not add uid=/gid= options when it mounts such a drive, which is a
# known DietPi gap (https://github.com/MichaIng/DietPi/issues/4680) and
# the reason the sentinel service can crash-loop on
# "PermissionError: ... config.json.tmp" even though install.sh ran
# chown without printing a fatal error.
#
# The fix for those filesystems is to add uid=/gid=/umask= to their
# /etc/fstab line and remount - not to chown, which cannot work there.
# For a normal Unix filesystem (ext4, btrfs, ...), chown is the right
# tool and is used as before.
set -uo pipefail

DATA="${1:?usage: sentinel-fix-storage-owner.sh <data-dir> <mountpoint> <user>}"
MNT="${2:?usage: sentinel-fix-storage-owner.sh <data-dir> <mountpoint> <user>}"
SVC_USER="${3:?usage: sentinel-fix-storage-owner.sh <data-dir> <mountpoint> <user>}"

mkdir -p "$DATA" 2>/dev/null || true

# runuser (util-linux) is effectively always present, but if it's
# somehow missing, fail loudly here rather than let every can_write()
# call below silently return "command not found" (exit 127) forever,
# which would misreport a perfectly fine mount as unwritable and loop on
# fixes that can never help.
command -v runuser >/dev/null || {
  echo "runuser not found (part of util-linux) - cannot check $SVC_USER's write access" >&2
  exit 1
}

can_write() {
  runuser -u "$SVC_USER" -- sh -c 'f="$1/.sentinel-write-test.$$"; : > "$f" 2>/dev/null && rm -f "$f" 2>/dev/null' _ "$DATA" 2>/dev/null
}

# Printed only on final failure, so the next bug report already carries the
# facts needed to tell "the fix didn't run" apart from "the fix ran but
# genuinely didn't work" - guessing blind at this from a two-line error
# message is how the previous two fixes each missed the real cause.
dump_diagnostics() {
  echo "--- diagnostics ---" >&2
  echo "id $SVC_USER: $(id "$SVC_USER" 2>&1)" >&2
  echo "findmnt $MNT: $(findmnt -no SOURCE,FSTYPE,OPTIONS "$MNT" 2>&1)" >&2
  echo "fstab line for $MNT: $(grep -F "$MNT" /etc/fstab 2>/dev/null || echo '(none found)')" >&2
  echo "ls -ld $MNT $DATA: $(ls -ld "$MNT" "$DATA" 2>&1)" >&2
  echo "-------------------" >&2
}

# Already fine - the overwhelmingly common case once this has run once -
# so nothing recursive runs at all.
if can_write; then
  exit 0
fi

fix_fat_mount() {
  local mnt="$1" uid gid want escaped_mnt fstab_line new_opts remounted svc_was_active
  uid=$(id -u "$SVC_USER") || return 1
  gid=$(id -g "$SVC_USER") || return 1
  want="uid=$uid,gid=$gid,umask=002"

  # Tolerate a trailing slash on the fstab side ("/mnt/VIDEOSD/") even
  # though $mnt itself never has one - some tools write mount points
  # that way, and a mismatch here used to mean "no fstab entry" (and a
  # dead stop) even though one clearly existed.
  escaped_mnt=$(printf '%s' "$mnt" | sed 's/[.[\*^$()+?{|]/\\&/g')
  fstab_line=$(grep -E "^[^#][^[:space:]]*[[:space:]]+${escaped_mnt}/?[[:space:]]" /etc/fstab 2>/dev/null || true)

  if [[ -z "$fstab_line" ]]; then
    echo "no /etc/fstab entry for $mnt; not touching it" >&2
    return 1
  fi
  if grep -q "uid=$uid" <<<"$fstab_line" && grep -q "gid=$gid" <<<"$fstab_line"; then
    # Options already correct; the mount itself must just be stale
    # (mounted before the sentinel user existed, say). Remount to refresh.
    :
  else
    cp -n /etc/fstab /etc/fstab.sentinel-backup 2>/dev/null || true
    new_opts=$(awk -v mnt="$mnt" -v mnt_slash="$mnt/" -v want="$want" 'BEGIN{OFS="\t"}
      $0 !~ /^#/ && ($2 == mnt || $2 == mnt_slash) {
        n = split($4, existing, ",")
        keep = ""
        for (i = 1; i <= n; i++) {
          if (existing[i] !~ /^(uid|gid|umask|fmask|dmask)=/) {
            keep = keep (keep == "" ? "" : ",") existing[i]
          }
        }
        $4 = (keep == "" ? want : keep "," want)
      }
      { print }' /etc/fstab)
    if [[ -z "$new_opts" ]]; then
      echo "failed to rewrite /etc/fstab for $mnt" >&2
      return 1
    fi
    printf '%s\n' "$new_opts" > /etc/fstab.tmp && mv /etc/fstab.tmp /etc/fstab
    echo "rewrote /etc/fstab options for $mnt to include $want" >&2
    # systemd generates a transient .mount unit from /etc/fstab (via
    # systemd-fstab-generator) and normally only re-reads it at boot or on
    # daemon-reload. If this mount is systemd-managed (dietpi-drive_manager
    # mounts often are, especially with "nofail"), a plain umount+mount
    # below can succeed immediately yet leave systemd's cached unit
    # pointing at the *old* options - and something that later re-triggers
    # that unit (a timer, "systemctl restart", another reboot) would then
    # silently put the old, broken options back. daemon-reload keeps
    # systemd's view in sync with the fstab we just wrote.
    systemctl daemon-reload 2>/dev/null || true
  fi

  # Remount to apply. A full umount+mount (not "-o remount") is required:
  # FUSE-backed ntfs-3g/exfat-fuse mounts generally do not accept
  # in-place uid/gid changes via remount. Stopping sentinel first should
  # be enough to release its file handles, but retry a few times before
  # giving up - a process can take a moment to actually exit, and other
  # things (a shell cd'd into the mount, sentinel-diagnose, the web
  # terminal) can also be holding it open transiently.
  svc_was_active=0
  if systemctl is-active --quiet sentinel 2>/dev/null; then
    svc_was_active=1
    systemctl stop sentinel 2>/dev/null || true
  fi
  remounted=0
  for _ in 1 2 3; do
    if umount "$mnt" 2>/dev/null && mount "$mnt" 2>/dev/null; then
      remounted=1
      break
    fi
    sleep 1
  done
  if (( ! remounted )); then
    # Last resort: detach now, finish unmounting once the last reference
    # drops, and mount fresh. Safe here because nothing this script cares
    # about writes to $mnt directly (only to $DATA underneath it) and
    # sentinel has already been stopped above.
    if umount -l "$mnt" 2>/dev/null && mount "$mnt" 2>/dev/null; then
      remounted=1
    fi
  fi
  (( svc_was_active )) && systemctl start sentinel 2>/dev/null || true
  if (( ! remounted )); then
    echo "could not remount $mnt (still busy after retries); a reboot will apply the new options" >&2
    return 1
  fi
  # Not verified further here: a FUSE-backed mount (ntfs-3g, exfat-fuse)
  # reports its *own* user_id=/group_id= in mount options - the FUSE
  # daemon's caller, always root, not the uid= fstab option that actually
  # controls file ownership as seen by other processes - so grepping
  # mount options for "uid=$uid" here would be checking the wrong thing
  # and can fail even when the fix worked. can_write() below is what
  # actually matters and is filesystem-agnostic.
  return 0
}

fstype=""
if mountpoint -q "$MNT" 2>/dev/null; then
  # dietpi-drive_manager mounts with "noauto,x-systemd.automount" produce
  # TWO stacked mounts at the same path: an autofs trigger (mounted first,
  # persistent) and the real filesystem underneath it (mounted on first
  # access, and what "findmnt $MNT" without qualifiers actually shows
  # matters here). `findmnt -no FSTYPE "$MNT"` with no other options
  # prints ALL of them, one per line, oldest-mounted first - so on such a
  # drive it returned "autofs\nexfat", which matched none of the case
  # patterns below and silently fell through to the ext4/chown branch,
  # which can never work on exFAT (this was found from a real diagnostics
  # dump: findmnt showed exactly that "systemd-1 autofs ..." / "/dev/sda1
  # exfat ..." pair, and chown reported an error as a result). The last
  # line is always the currently effective, topmost filesystem - the one
  # that read/write actually goes through - so take that one only.
  fstype=$(findmnt -no FSTYPE "$MNT" 2>/dev/null | tail -n1)
fi

case "$fstype" in
  vfat|exfat|ntfs|ntfs3|fuseblk)
    if fix_fat_mount "$MNT" && can_write; then
      echo "fixed: applied mount options for $MNT ($fstype)"
      exit 0
    fi
    # chown cannot do anything useful on these filesystems (see header
    # comment) - stop here with a specific reason instead of trying it
    # anyway and reporting a generic, misleading "still cannot write".
    echo "$SVC_USER still cannot write to $DATA ($fstype mount options could not be fixed - see the message above)" >&2
    dump_diagnostics
    exit 1
    ;;
esac

# A normal Unix filesystem (ext4, btrfs, ...) - chown is the right tool.
#
# Also make sure the mount's own root directory ($MNT, e.g. /mnt/VIDEOSD
# itself - not $DATA underneath it) grants traversal. A drive reused or
# formatted elsewhere can keep restrictive root-directory permissions
# (e.g. 0700 root:root) from wherever it came from; chown -R on $DATA
# alone then fixes ownership of the sentinel/ subdirectory perfectly but
# sentinel still can never reach it, since it isn't root's own account
# and can't even list/enter $MNT to get there. This was found and
# reproduced directly: a correctly-owned, correctly-permissioned $DATA
# still failed can_write() with EACCES solely because of $MNT's own
# mode, and the previous version of this script had nothing that would
# ever detect or fix that. Only the execute (traverse) bit is added here
# - nothing is made readable or writable that wasn't already meant to be
# a shared storage mount.
chmod o+x "$MNT" 2>/dev/null || true

chown -R "$SVC_USER:$SVC_USER" "$DATA" 2>/dev/null || \
  echo "chown on $DATA reported an error" >&2

if can_write; then
  echo "fixed: chown -R $SVC_USER $DATA (and made $MNT traversable)"
  exit 0
fi

echo "$SVC_USER still cannot write to $DATA" >&2
dump_diagnostics
exit 1
