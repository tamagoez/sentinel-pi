#!/usr/bin/env bash
# Sentinel update — pull the latest code and re-apply every fix.
#
#   sudo ./update.sh
#
# This is the update path, once the guided setup.sh flow has already
# been completed once. It does not ask the H1-H6 questions setup.sh asks
# on a first install - those are one-time decisions, not something a
# routine update needs to touch.
#
# What it does, in order:
#   1. git pull (if this is a git checkout) - the single most common
#      reason a fix "doesn't seem to be fixed yet" is that the box is
#      still running the code from before the fix landed. `setup.sh
#      --update` used to skip this and jump straight to install.sh,
#      which is exactly how that happened.
#   2. Re-run bootstrap.sh. Every step in it is idempotent (skips
#      anything already installed/configured) except for a few cheap,
#      always-safe calls (apt-get update, re-asserting the sound card),
#      so re-running it is how package- and OS-level fixes (like the
#      ffmpeg drawtext check) actually reach a box that was set up
#      before that fix existed. It only asks for a reboot if something
#      that genuinely needs one changed just now.
#   3. Re-run install.sh - idempotent: redeploys the app, re-registers
#      systemd units, and re-checks/repairs external-storage permissions.
#
# Safe to re-run any time; every step here is designed to be a no-op
# when there is nothing to fix. All output is English on purpose - see
# CLAUDE.md.
set -uo pipefail

c()  { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok() { printf '  [OK] %s\n' "$*"; }
w()  { printf '  [!!] %s\n' "$*"; }
die(){ printf '\n[FAIL] %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root: sudo ./update.sh"

# Same robust path resolution as setup.sh/install.sh: a clear message
# instead of a raw "cp: cannot stat" if the clone is incomplete or this
# was run from one level inside it.
RAW_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$RAW_DIR/sentinel/main.py" ]]; then
  SRC="$RAW_DIR"
elif [[ -f "$RAW_DIR/main.py" && -f "$RAW_DIR/../update.sh" ]]; then
  SRC="$(cd "$RAW_DIR/.." && pwd)"
else
  die "Cannot find sentinel/main.py under $RAW_DIR.
       Run this from the top of the git clone:
         cd ~/sentinel-pi && sudo ./update.sh"
fi
for f in bootstrap.sh install.sh; do
  [[ -f "$SRC/$f" ]] || die "$f is missing from $SRC."
done
chmod +x "$SRC"/*.sh "$SRC"/scripts/*.sh 2>/dev/null || true

# ------------------------------------------------------------------ 1. git pull
c "STEP 1/3  Pulling the latest code"
if [[ -d "$SRC/.git" ]]; then
  if command -v git >/dev/null; then
    BEFORE=$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)
    if git -C "$SRC" pull --ff-only 2>&1 | sed 's/^/  /'; then
      AFTER=$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)
      if [[ "$BEFORE" == "$AFTER" ]]; then
        ok "Already up to date ($AFTER)"
      else
        ok "Updated $BEFORE -> $AFTER"
      fi
    else
      w "git pull failed - uncommitted local changes, or no network."
      w "Resolve it by hand (git status / git stash), then re-run: sudo ./update.sh"
      w "Continuing with the code already on disk ($BEFORE) for now."
    fi
  else
    w "git not found; cannot check for updates. Continuing with the code on disk."
  fi
else
  w "$SRC is not a git checkout; skipping git pull. Copy in new files yourself before running this."
fi

# ------------------------------------------------------------------ 2. bootstrap.sh
c "STEP 2/3  Re-applying prerequisites (bootstrap.sh)"
"$SRC/bootstrap.sh" || die "bootstrap.sh failed. Fix the reported problem, then re-run: sudo ./update.sh"

# ------------------------------------------------------------------ 3. install.sh
c "STEP 3/3  Redeploying the application (install.sh)"
"$SRC/install.sh" || die "install.sh failed. Fix the reported problem above, then re-run: sudo ./update.sh"

ok "Update complete."
