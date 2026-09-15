#!/usr/bin/env bash
# Sentinel auto-update - notice new commits on the tracked git remote and,
# if any landed, run update.sh (git pull --ff-only -> bootstrap.sh ->
# install.sh) without anyone needing to log in and run it by hand.
#
# Runs from a systemd timer (sentinel-autoupdate.timer), every 30 minutes.
# Quiet by design, like sentinel-guardian.sh: this only logs (and only
# does anything) when there is actually something new to pull. Every step
# update.sh runs is already idempotent (CLAUDE.md "導入は git clone から
# の半自動"), so re-running it on a schedule is safe even with nothing to
# do most cycles.
#
# install.sh and update.sh both record the git clone's path in
# $REPO_FILE right after resolving it (same robust RAW_DIR/SRC logic they
# already use) - this script has no way to find that path on its own,
# since /opt/sentinel (where the *deployed* copy of this very script
# lives) is not a git checkout.
set -uo pipefail

STATE_DIR=/var/lib/sentinel
REPO_FILE="$STATE_DIR/repo-path"
LOG_TAG=sentinel-autoupdate
# Same default/override convention as sentinel-guardian.sh - install.sh
# patches this Environment= line in the deployed systemd unit to the
# actual storage path, same as it already does for sentinel-guardian.service.
DATA="${SENTINEL_DATA:-/mnt/VIDEOSD/sentinel}"
CONFIG_JSON="$DATA/config.json"

say(){ logger -t "$LOG_TAG" -p daemon.notice -- "$*"; echo "$*"; }
warn(){ logger -t "$LOG_TAG" -p daemon.warning -- "$*"; echo "$*" >&2; }

if [[ $EUID -ne 0 ]]; then
  warn "must run as root"
  exit 1
fi

# Respect the "自動更新を有効にする" toggle in the Web UI settings tab
# (system_autoupdate_enabled, core/config.py). This script has no direct
# access to config.py's own path-resolution logic (it's a separate bash
# process), so it reads config.json directly via python3 (already a hard
# dependency of the app itself) - defaulting to enabled if the file is
# missing/unreadable/the key absent, matching config.py's own DEFAULTS
# fallback behavior.
if [[ -f "$CONFIG_JSON" ]]; then
  enabled=$(python3 -c "
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        d = json.load(f)
    print('0' if d.get('system_autoupdate_enabled', True) is False else '1')
except Exception:
    print('1')
" "$CONFIG_JSON" 2>/dev/null || echo 1)
  if [[ "$enabled" != "1" ]]; then
    exit 0
  fi
fi

# Nothing recorded yet (never installed from a git clone, or install.sh
# predates this feature and hasn't been re-run) - silently do nothing
# rather than guessing a path.
[[ -f "$REPO_FILE" ]] || exit 0
REPO=$(cat "$REPO_FILE" 2>/dev/null || true)
[[ -n "$REPO" && -d "$REPO/.git" && -f "$REPO/update.sh" ]] || exit 0
command -v git >/dev/null || exit 0

# This script runs unattended as root, possibly against a clone owned by
# the interactive user who ran setup.sh - avoid a silent, permanent no-op
# from git's "detected dubious ownership" guard (safe by construction:
# this only trusts a path we ourselves resolved and recorded, not one
# taken from the environment).
git config --global --add safe.directory "$REPO" 2>/dev/null || true

# A clone with uncommitted local changes would make update.sh's own
# `git pull --ff-only` fail loudly every single cycle - leave it alone
# and let a human resolve it by hand, exactly as update.sh's own git-pull
# step already asks for when it hits this.
if ! git -C "$REPO" diff --quiet 2>/dev/null || ! git -C "$REPO" diff --cached --quiet 2>/dev/null; then
  exit 0
fi

BEFORE=$(git -C "$REPO" rev-parse HEAD 2>/dev/null) || exit 0
if ! git -C "$REPO" fetch --quiet origin 2>/dev/null; then
  warn "git fetch failed (no network?) - will retry next cycle"
  exit 0
fi

UPSTREAM=$(git -C "$REPO" rev-parse '@{upstream}' 2>/dev/null) || exit 0
[[ "$BEFORE" == "$UPSTREAM" ]] && exit 0   # already current - the common case, stays quiet

say "New commits on the remote (${BEFORE:0:7} -> ${UPSTREAM:0:7}); running update.sh"
if SENTINEL_NONINTERACTIVE=1 "$REPO/update.sh"; then
  say "Auto-update complete ($(git -C "$REPO" rev-parse --short HEAD 2>/dev/null))"
else
  warn "update.sh failed; see 'journalctl -u sentinel-autoupdate -e'"
fi
