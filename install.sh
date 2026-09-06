#!/usr/bin/env bash
# Sentinel installer
#
#   Prerequisite: bootstrap.sh has run and the box has rebooted once, and
#   the external drive is mounted (dietpi-drive_manager -> /mnt/VIDEOSD).
#   Run: sudo ./install.sh
#
#   Normally you do not call this directly: setup.sh walks through the whole
#   installation and calls it at the right moment. Calling it on its own is
#   the update path:  git pull && sudo ./install.sh
#
# Idempotent — safe to run again after every update.
#
# Design note: nothing here is "apply once and forget". Guardian
# (installed in STEP 6) re-checks everything every 2 minutes and repairs
# drift — including things that don't naturally persist across reboots,
# such as iptables rules.
set -euo pipefail

APP_DIR=/opt/sentinel
SVC_USER=sentinel
STORAGE="${SENTINEL_STORAGE:-/mnt/VIDEOSD}"

c()  { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok() { printf '  [OK] %s\n' "$*"; }
w()  { printf '  [!!] %s\n' "$*"; }
die(){ printf '[FAIL] %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo ./install.sh)"

# ---------------------------------------------------------------- 0. Locate
# Resolve the project root robustly and verify it actually contains the
# Python package (sentinel/main.py), not just the top-level scripts.
# This turns a confusing 'cp: cannot stat ...' failure (seen when the clone
# is incomplete, or the script was run from the wrong directory) into a
# clear, actionable error up front.
RAW_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$RAW_DIR/sentinel/main.py" ]]; then
  SRC="$RAW_DIR"
elif [[ -f "$RAW_DIR/main.py" && -f "$RAW_DIR/../install.sh" ]]; then
  # install.sh was run from *inside* the inner "sentinel" package folder.
  SRC="$(cd "$RAW_DIR/.." && pwd)"
else
  die "Cannot find sentinel/main.py under $RAW_DIR.
       Run this from the top of the git clone, not from inside the inner
       'sentinel' package folder:
         git clone https://github.com/tamagoez/sentinel-pi.git ~/sentinel-pi
         cd ~/sentinel-pi && sudo ./install.sh"
fi

# ---------------------------------------------------------------- 1. Preflight
c "STEP 1/9  Preflight checks"
MISSING=()
for bin in python3 ffmpeg mpg123 v4l2-ctl; do
  command -v "$bin" >/dev/null || MISSING+=("$bin")
done
if (( ${#MISSING[@]} )); then
  w "Missing: ${MISSING[*]}"
  w "Run bootstrap.sh first."
  if [[ "${SENTINEL_NONINTERACTIVE:-0}" == "1" ]]; then
    w "Non-interactive mode: continuing anyway."
  else
    read -rp "  Continue anyway? [y/N]: " a
    [[ "$a" == [yY] ]] || exit 1
  fi
else
  ok "All required commands are present"
fi

if command -v ffmpeg >/dev/null; then
  ffmpeg -hide_banner -filters 2>/dev/null | grep -q ' drawtext ' \
    && ok "ffmpeg supports drawtext (ticker overlay available)" \
    || w "ffmpeg has no drawtext; the daily ticker overlay will be skipped."
  ffmpeg -hide_banner -encoders 2>/dev/null | grep -q h264_v4l2m2m \
    && ok "Hardware encoder h264_v4l2m2m available" \
    || w "No hardware encoder; falling back to libx264 ultrafast."
fi

pgrep -x pulseaudio >/dev/null && \
  w "PulseAudio is running; consider stopping it if audio stutters."

python3 -c 'import cv2' 2>/dev/null && ok "OpenCV importable" \
  || die "python3-opencv missing (apt install python3-opencv)."

# ---------------------------------------------------------------- 2. User
c "STEP 2/9  Create service user"
id "$SVC_USER" &>/dev/null || \
  useradd --system --create-home --home-dir "/home/$SVC_USER" --shell /bin/bash "$SVC_USER"
for g in video audio bluetooth plugdev systemd-journal; do
  getent group "$g" >/dev/null && usermod -aG "$g" "$SVC_USER"
done
ok "$SVC_USER added to video/audio/bluetooth/plugdev/systemd-journal"

# Only 'reboot' is granted via sudo. Any other root action is done by the
# operator inside the web terminal via 'su -'; the app never sees a
# root password, so there is no password-handling code to leak it.
cat > /etc/sudoers.d/sentinel <<EOF
$SVC_USER ALL=(root) NOPASSWD: /sbin/reboot, /sbin/shutdown, /usr/bin/systemctl reboot
EOF
chmod 440 /etc/sudoers.d/sentinel
visudo -cf /etc/sudoers.d/sentinel >/dev/null && ok "sudoers: reboot only"

# ---------------------------------------------------------------- 3. Deploy
c "STEP 3/9  Deploy application files"
mkdir -p "$APP_DIR/scripts"
rm -rf "$APP_DIR/sentinel"
cp -r "$SRC/sentinel" "$APP_DIR/"
cp -f "$SRC/scripts/"*.sh "$APP_DIR/scripts/"
chmod +x "$APP_DIR/scripts/"*.sh
ln -sf "$APP_DIR/scripts/sentinel-diagnose.sh" /usr/local/bin/sentinel-diagnose
for f in setup.sh bootstrap.sh install.sh; do
  cp -f "$SRC/$f" "$APP_DIR/" 2>/dev/null || true
done
cp -f "$SRC"/*.md "$APP_DIR/" 2>/dev/null || true
ok "Files copied to $APP_DIR; sentinel-diagnose linked into PATH"

if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  # --system-site-packages: reuse apt's OpenCV build; a pip build on a Pi
  # can take hours.
  python3 -m venv --system-site-packages "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet \
  "fastapi>=0.110" "uvicorn[standard]>=0.27" "websockets>=12"

if command -v yt-dlp >/dev/null; then
  ok "yt-dlp: using system package ($(yt-dlp --version 2>/dev/null || echo '?'))"
else
  "$APP_DIR/.venv/bin/pip" install --quiet yt-dlp
  ln -sf "$APP_DIR/.venv/bin/yt-dlp" /usr/local/bin/yt-dlp
  w "yt-dlp installed into the venv (consider 'dietpi-software install 195')"
fi

"$APP_DIR/.venv/bin/python" -c 'import cv2,fastapi;print("  opencv",cv2.__version__,"/ fastapi",fastapi.__version__)'
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR"
ok "Python environment ready"

# ---------------------------------------------------------------- 4. Data
c "STEP 4/9  Prepare data directory"
if [[ -d "$STORAGE" ]] && mountpoint -q "$STORAGE"; then
  DATA="$STORAGE/sentinel"
  ok "Using external storage"
elif [[ -d "$STORAGE" ]]; then
  DATA="$STORAGE/sentinel"
  w "$STORAGE exists but is not a mountpoint; data will live on the SD card."
else
  DATA="/home/$SVC_USER/sentinel-data"
  w "$STORAGE not found; falling back under the home directory."
fi
mkdir -p "$DATA"/{music,captures,archive,netlog}
ok "Data directory: $DATA"

for old in /root/Music /home/*/Music; do
  [[ -d "$old" ]] || continue
  if compgen -G "$old/*.mp3" >/dev/null; then
    cp -n "$old"/*.mp3 "$DATA/music/" 2>/dev/null || true
    ok "Imported tracks from $old"
  fi
done
[[ -d "$STORAGE/camguard_data" ]] && \
  w "Old camguard data left at $STORAGE/camguard_data (clean up manually if desired)."

# Plain chown is not enough on exFAT/NTFS: those filesystems have no Unix
# ownership of their own, so chown either errors ("Operation not
# permitted") or silently no-ops, and dietpi-drive_manager does not add
# the uid=/gid= mount options that would fix it (a known DietPi gap,
# https://github.com/MichaIng/DietPi/issues/4680). This is why the
# service can crash-loop on a PermissionError writing config.json even
# right after a clean install. sentinel-fix-storage-owner.sh checks
# whether $SVC_USER can actually write here and, only if not, fixes the
# fstab mount options (FAT-family filesystems) or falls back to chown
# (everything else) and remounts. Also re-run by Guardian every 2
# minutes, so a drive re-mounted by hand later gets the same repair.
if FIX_OUT=$("$SRC/scripts/sentinel-fix-storage-owner.sh" "$DATA" "$STORAGE" "$SVC_USER" 2>&1); then
  [[ -n "$FIX_OUT" ]] && ok "$FIX_OUT" || ok "$SVC_USER can write to $DATA"
else
  w "$FIX_OUT"
  w "$SVC_USER cannot write to $DATA; the service will fail to start until this is fixed."
fi

# ---------------------------------------------------------------- 5. Old services
c "STEP 5/9  Disable legacy services"
for old in camguard music-player; do
  if systemctl list-unit-files | grep -q "^$old\.service"; then
    systemctl disable --now "$old" 2>/dev/null || true
    ok "Disabled $old"
  fi
done

# ---------------------------------------------------------------- 6. Bluetooth
c "STEP 6/9  Configure Bluetooth services"
BA=$(command -v bluealsad || command -v bluealsa || true)
if [[ -n "$BA" ]]; then
  install -m644 "$SRC/systemd/sentinel-bluealsa.service" /etc/systemd/system/
  sed -i "s|^ExecStart=.*|ExecStart=$BA -p a2dp-sink|" /etc/systemd/system/sentinel-bluealsa.service
  install -m644 "$SRC/systemd/sentinel-bluealsa-aplay.service" /etc/systemd/system/
  ok "BlueALSA (A2DP sink) + playback bridge registered"
else
  w "bluealsa not found; Bluetooth-speaker feature will be unavailable."
fi

if command -v bt-agent >/dev/null; then
  install -m644 "$SRC/systemd/sentinel-bt-agent.service" /etc/systemd/system/
  ok "Persistent pairing agent (bt-agent) registered"
else
  w "bt-agent missing (apt install bluez-tools); new pairings may fail."
fi

if [[ -f /etc/bluetooth/main.conf ]]; then
  cp -n /etc/bluetooth/main.conf /etc/bluetooth/main.conf.sentinel-backup 2>/dev/null || true
  BEFORE_SUM=$(md5sum /etc/bluetooth/main.conf | awk '{print $1}')
  for kv in "DiscoverableTimeout=0" "PairableTimeout=0" "AlwaysPairable=true"; do
    k="${kv%%=*}"; v="${kv#*=}"
    if grep -qE "^\s*#?\s*$k\s*=" /etc/bluetooth/main.conf; then
      sed -i -E "s|^\s*#?\s*$k\s*=.*|$k = $v|" /etc/bluetooth/main.conf
    else
      sed -i "/^\[General\]/a $k = $v" /etc/bluetooth/main.conf
    fi
  done
  grep -q '^\[Policy\]' /etc/bluetooth/main.conf || printf '\n[Policy]\n' >> /etc/bluetooth/main.conf
  if grep -qE '^\s*#?\s*AutoEnable\s*=' /etc/bluetooth/main.conf; then
    sed -i -E 's|^\s*#?\s*AutoEnable\s*=.*|AutoEnable = true|' /etc/bluetooth/main.conf
  else
    sed -i '/^\[Policy\]/a AutoEnable = true' /etc/bluetooth/main.conf
  fi
  AFTER_SUM=$(md5sum /etc/bluetooth/main.conf | awk '{print $1}')
  # Only bounce bluetoothd if something actually changed (or it isn't
  # running at all). Restarting it unconditionally on every re-run of
  # install.sh knocks BlueALSA's D-Bus connection out from under it,
  # which - combined with STEP 8 restarting sentinel-bluealsa(-aplay) at
  # the same time - can burn through enough rapid restarts to hit
  # systemd's default start-limit and leave those units "failed".
  if [[ "$BEFORE_SUM" != "$AFTER_SUM" ]] || ! systemctl is-active --quiet bluetooth; then
    systemctl restart bluetooth 2>/dev/null || true
    ok "bluetoothd set to always-discoverable/pairable (restarted)"
  else
    ok "bluetoothd already configured; left running"
  fi
fi

# ---------------------------------------------------------------- 7. Guardian
c "STEP 7/9  Register Guardian (drift repair)"
install -m644 "$SRC/systemd/sentinel-guardian.service" /etc/systemd/system/
install -m644 "$SRC/systemd/sentinel-guardian.timer" /etc/systemd/system/
sed -i -e "s|^Environment=SENTINEL_DATA=.*|Environment=SENTINEL_DATA=$DATA|" \
       -e "s|^Environment=SENTINEL_STORAGE=.*|Environment=SENTINEL_STORAGE=$STORAGE|" \
  /etc/systemd/system/sentinel-guardian.service
ok "Runs every 2 minutes; repairs AdGuard bind / iptables / audio output /"
echo "     Bluetooth state / storage ownership / service uptime / hotspot DNS /"
echo "     disk space / yt-dlp"

# ---------------------------------------------------------------- 8. Main service
c "STEP 8/9  Register Sentinel service"
sed -e "s|^User=.*|User=$SVC_USER|" \
    -e "s|^Group=.*|Group=$SVC_USER|" \
    -e "s|^Environment=SENTINEL_STORAGE=.*|Environment=SENTINEL_STORAGE=$STORAGE|" \
    "$SRC/systemd/sentinel.service" > /etc/systemd/system/sentinel.service

# sentinel-deploy-resume was the resume unit of the old deploy.sh; a run
# interrupted back then can still leave it enabled.
for stale in sentinel-firewall.service sentinel-bt-discoverable.service \
             sentinel-deploy-resume.service; do
  if systemctl list-unit-files 2>/dev/null | grep -q "^$stale"; then
    systemctl disable --now "$stale" 2>/dev/null || true
    rm -f "/etc/systemd/system/$stale"
    ok "Removed stale unit $stale"
  fi
done

systemctl daemon-reload
UNITS=(sentinel.service sentinel-guardian.timer)
[[ -n "$BA" ]] && UNITS+=(sentinel-bluealsa.service sentinel-bluealsa-aplay.service)
command -v bt-agent >/dev/null && UNITS+=(sentinel-bt-agent.service)
systemctl enable "${UNITS[@]}" >/dev/null 2>&1
ok "Enabled: ${UNITS[*]}"
# Clear any "failed (start-limit-hit)" left over from before this script
# added StartLimitIntervalSec=0 to the Bluetooth units, or from any other
# past burst of restarts - reset-failed is a no-op on a unit that isn't
# in that state, so this is safe to run unconditionally.
systemctl reset-failed "${UNITS[@]}" 2>/dev/null || true
systemctl restart "${UNITS[@]}"

# ---------------------------------------------------------------- 9. First run
c "STEP 9/9  Run Guardian once now"
SENTINEL_DATA="$DATA" "$APP_DIR/scripts/sentinel-guardian.sh" || \
  w "Some Guardian checks failed on first run; see 'journalctl -t sentinel-guardian'."

sleep 3
if command -v ss >/dev/null; then
  if ss -Hltn "sport = :8083" 2>/dev/null | awk '{print $4}' | grep -qvE '^(127\.0\.0\.1|\[::1\]):'; then
    w "Port 8083 is still reachable from outside; check manually."
  else
    ok "Port 8083 is loopback-only"
  fi
fi

sleep 3
if systemctl is-active --quiet sentinel; then
  IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  cat <<EOS

== Install complete ==

  Web UI      http://${IP:-<this-Pi-IP>}:8080
  AdGuard     http://${IP:-<IP>}:8080/adguard/   (direct :8083 is now blocked)
  Data        $DATA
  Logs        journalctl -u sentinel -f
  Guardian    journalctl -t sentinel-guardian -f
  Diagnostics sentinel-diagnose

  Do this next, in the Web UI settings tab:
    1. Set a Web UI password
    2. Set the Discord webhook URL
    3. Set the AdGuard Home password (needed for access logging)

EOS
else
  die "Service failed to start. Check: journalctl -u sentinel -n 60 --no-pager"
fi
