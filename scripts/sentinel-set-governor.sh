#!/usr/bin/env bash
# sentinel-set-governor.sh <governor>
#
# Writes <governor> to every CPU core's scaling_governor. Run as root via
# sudo from core/state.py's _apply_governor() (sentinel.service runs as
# the non-root "sentinel" user - see CLAUDE.md #4 - and those sysfs files
# are root-writable only).
#
# This is the one narrow, argument-validated escalation /etc/sudoers.d/
# sentinel grants for it: sudoers itself cannot restrict *values* of a
# wildcarded argument, so the validation below is what actually keeps
# this from being "write anything as root" - only the governor names the
# kernel itself defines are ever accepted.
set -uo pipefail

GOV="${1:?usage: sentinel-set-governor.sh <governor>}"
case "$GOV" in
  performance|powersave|userspace|ondemand|conservative|schedutil) ;;
  *)
    echo "refusing to set unknown governor: $GOV" >&2
    exit 1
    ;;
esac

applied=0
for p in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
  [[ -e "$p" ]] || continue
  if echo "$GOV" > "$p" 2>/dev/null; then
    applied=1
  fi
done

if (( ! applied )); then
  echo "could not write scaling_governor on any CPU" >&2
  exit 1
fi
