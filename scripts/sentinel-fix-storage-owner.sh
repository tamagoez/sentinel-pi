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

can_write() {
  runuser -u "$SVC_USER" -- sh -c 'f="$1/.sentinel-write-test.$$"; : > "$f" 2>/dev/null && rm -f "$f" 2>/dev/null' _ "$DATA" 2>/dev/null
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

  escaped_mnt=$(printf '%s' "$mnt" | sed 's/[.[\*^$()+?{|]/\\&/g')
  fstab_line=$(grep -E "^[^#][^[:space:]]*[[:space:]]+${escaped_mnt}[[:space:]]" /etc/fstab 2>/dev/null || true)

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
    new_opts=$(awk -v mnt="$mnt" -v want="$want" 'BEGIN{OFS="\t"}
      $0 !~ /^#/ && $2 == mnt {
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
  fi

  # Remount to apply. A full umount+mount (not "-o remount") is required:
  # FUSE-backed ntfs-3g/exfat-fuse mounts generally do not accept
  # in-place uid/gid changes via remount.
  svc_was_active=0
  if systemctl is-active --quiet sentinel 2>/dev/null; then
    svc_was_active=1
    systemctl stop sentinel 2>/dev/null || true
  fi
  remounted=0
  if umount "$mnt" 2>/dev/null && mount "$mnt" 2>/dev/null; then
    remounted=1
  fi
  (( svc_was_active )) && systemctl start sentinel 2>/dev/null || true
  (( remounted )) || { echo "could not remount $mnt (busy?); a reboot will apply the new options" >&2; return 1; }
  return 0
}

fstype=""
if mountpoint -q "$MNT" 2>/dev/null; then
  fstype=$(findmnt -no FSTYPE "$MNT" 2>/dev/null)
fi

case "$fstype" in
  vfat|exfat|ntfs|ntfs3|fuseblk)
    if fix_fat_mount "$MNT" && can_write; then
      echo "fixed: applied mount options for $MNT ($fstype)"
      exit 0
    fi
    ;;
esac

# Either a normal Unix filesystem, or the mount-options fix above didn't
# fully resolve it (e.g. no fstab entry to rewrite) - chown is still the
# right fallback there.
chown -R "$SVC_USER:$SVC_USER" "$DATA" 2>/dev/null || \
  echo "chown on $DATA reported an error" >&2

if can_write; then
  echo "fixed: chown -R $SVC_USER $DATA"
  exit 0
fi

echo "$SVC_USER still cannot write to $DATA" >&2
exit 1
