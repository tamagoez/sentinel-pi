#!/usr/bin/env bash
# deploy — one command from a bare DietPi to a running Sentinel.
#
# Prerequisite (manual, one-time): mount the external drive yourself first
#   dietpi-drive_manager        # confirm it lands on /mnt/VIDEOSD
#
# Then, assuming the "sentinel" project folder was already copied onto
# that drive from your PC beforehand:
#   cp -r /mnt/VIDEOSD/sentinel ~/sentinel   # exFAT/NTFS often mount
#   cd ~/sentinel && chmod +x *.sh scripts/*.sh   # noexec / lose +x, so
#   sudo ./deploy.sh                              # copy to local disk first
#
# Chains bootstrap.sh -> one automatic reboot -> install.sh.
# Safe to re-run if interrupted; progress is kept in /var/lib/sentinel.
#
# For a manual, step-by-step walkthrough instead, see SETUP.md.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root (sudo ./deploy.sh)" >&2; exit 1; }

RAW_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$RAW_DIR/sentinel/main.py" ]]; then
  SRC="$RAW_DIR"
elif [[ -f "$RAW_DIR/main.py" && -f "$RAW_DIR/../deploy.sh" ]]; then
  SRC="$(cd "$RAW_DIR/.." && pwd)"
else
  echo "FAIL: cannot find sentinel/main.py under $RAW_DIR" >&2
  echo "      Copy the WHOLE project folder (it contains an inner" >&2
  echo "      'sentinel' folder — that's the Python package)." >&2
  exit 1
fi

STATE=/var/lib/sentinel
PERSIST="$STATE/src"
STAGE="$STATE/stage"
UNIT=/etc/systemd/system/sentinel-deploy-resume.service

log(){ printf '\033[1;35m[deploy]\033[0m %s\n' "$*"; }

mkdir -p "$STATE"
stage=$(cat "$STAGE" 2>/dev/null || echo 0)

case "$stage" in
0)
  log "1/2 installing prerequisites (bootstrap.sh)"
  "$SRC/bootstrap.sh"

  rm -rf "$PERSIST"; mkdir -p "$PERSIST"
  cp -r "$SRC"/. "$PERSIST"/
  echo 1 > "$STAGE"

  cat > "$UNIT" <<EOF
[Unit]
Description=Sentinel deploy (resume after reboot)
After=network-online.target bluetooth.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/bash $PERSIST/deploy.sh
EOF
  systemctl daemon-reload
  systemctl enable sentinel-deploy-resume.service >/dev/null

  log "rebooting in 10s to apply Bluetooth/audio changes (Ctrl+C to abort)"
  sleep 10
  reboot
  ;;

1)
  log "2/2 installing Sentinel (install.sh)"
  systemctl disable sentinel-deploy-resume.service >/dev/null 2>&1 || true
  rm -f "$UNIT"; systemctl daemon-reload
  echo 2 > "$STAGE"

  SENTINEL_NONINTERACTIVE=1 "$PERSIST/install.sh"

  IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  log "done: http://${IP:-<this-Pi-IP>}:8080  (set password etc. in the settings tab)"
  rm -rf "$STATE"
  ;;

*)
  log "already completed (stage=$stage). To redo: rm -rf $STATE"
  ;;
esac
