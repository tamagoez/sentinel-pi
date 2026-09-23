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

# Record where this git clone lives so scripts/sentinel-autoupdate.sh (which
# only ever runs from the *deployed* copy at $APP_DIR, not a git checkout)
# knows where to `git pull` from later. Only when it actually is one -
# leaving this unset is how that script recognizes "not installed from a
# git clone" and stays a no-op instead of guessing a path.
if [[ -d "$SRC/.git" ]]; then
  mkdir -p /var/lib/sentinel
  echo "$SRC" > /var/lib/sentinel/repo-path
fi

# ---------------------------------------------------------------- 1. Preflight
c "STEP 1/10  Preflight checks"
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
  # Text overlays (NODATA label / camera caption / access-log ticker) are
  # drawn with Pillow and composited with ffmpeg's overlay/drawbox - core
  # filters that exist regardless of build flags - specifically because
  # ffmpeg's own drawtext filter needs libharfbuzz and some distro builds
  # ship without it with no fix ever reaching the repo (see CLAUDE.md #10).
  # So the only requirement here is Pillow, not a particular ffmpeg build.
  python3 -c 'import PIL' >/dev/null 2>&1 \
    && ok "Pillow available (text overlays enabled)" \
    || w "python3-pil not found; text overlays (NODATA/caption/ticker) will be skipped."
  ffmpeg -hide_banner -encoders 2>/dev/null | grep -q h264_v4l2m2m \
    && ok "Hardware encoder h264_v4l2m2m available" \
    || w "No hardware encoder; falling back to libx264 ultrafast."
fi

pgrep -x pulseaudio >/dev/null && \
  w "PulseAudio is running; consider stopping it if audio stutters."

python3 -c 'import cv2' 2>/dev/null && ok "OpenCV importable" \
  || die "python3-opencv missing (apt install python3-opencv)."

# ---------------------------------------------------------------- 2. User
c "STEP 2/10  Create service user"
id "$SVC_USER" &>/dev/null || \
  useradd --system --create-home --home-dir "/home/$SVC_USER" --shell /bin/bash "$SVC_USER"
for g in video audio bluetooth plugdev systemd-journal; do
  getent group "$g" >/dev/null && usermod -aG "$g" "$SVC_USER"
done
ok "$SVC_USER added to video/audio/bluetooth/plugdev/systemd-journal"

# 'reboot' and one narrowly-scoped script are granted via sudo. Any other
# root action is done by the operator inside the web terminal via 'su -';
# the app never sees a root password, so there is no password-handling
# code to leak it.
#
# sentinel-set-governor.sh: /sys/.../cpufreq/scaling_governor is root-
# writable only, and eco mode is meaningless without being able to
# actually lower it - core/state.py's _apply_governor() was writing to
# it directly and silently swallowing the resulting PermissionError on
# every real deployment, so eco mode never actually changed the CPU
# governor at all. sudoers can't restrict a wildcarded argument's
# *value*, so the script itself validates it against the kernel's own
# governor names before writing anything (see the script for detail).
#
# sentinel-set-hotspot-ssid.sh: /etc/hostapd/hostapd.conf is root-writable
# only. modules/hotspot.py calls it so the WiFi hotspot's SSID can be
# changed at runtime from the Settings page, not just once via dietpi.txt
# at image-prep time. Same argument-validation-is-the-boundary pattern.
#
# There used to be a third entry here, sentinel-setup-audio-mixing.sh,
# which wrote /etc/asound.conf so music and voice announcements could mix
# through a hand-written ALSA dmix chain. That whole approach - and the
# root-only file it needed - is gone: both now write to alsa-lib's own
# per-card `sysdefault:CARD=<N>` route, which needs no configuration file
# and therefore no root access at all. See CLAUDE.md's audio-mixing
# redesign section for why.
cat > /etc/sudoers.d/sentinel <<EOF
$SVC_USER ALL=(root) NOPASSWD: /sbin/reboot, /sbin/shutdown, /usr/bin/systemctl reboot, $APP_DIR/scripts/sentinel-set-governor.sh *, $APP_DIR/scripts/sentinel-set-hotspot-ssid.sh *
EOF
chmod 440 /etc/sudoers.d/sentinel
visudo -cf /etc/sudoers.d/sentinel >/dev/null && ok "sudoers: reboot + CPU governor switch + hotspot SSID"

# ---------------------------------------------------------------- 3. Deploy
c "STEP 3/10  Deploy application files"
mkdir -p "$APP_DIR/scripts"
rm -rf "$APP_DIR/sentinel"
cp -r "$SRC/sentinel" "$APP_DIR/"
cp -f "$SRC/scripts/"*.sh "$APP_DIR/scripts/"
chmod +x "$APP_DIR/scripts/"*.sh
ln -sf "$APP_DIR/scripts/sentinel-diagnose.sh" /usr/local/bin/sentinel-diagnose
ln -sf "$APP_DIR/scripts/sentinel-logs.sh" /usr/local/bin/sentinel-logs
ln -sf "$APP_DIR/scripts/sentinel-adguard-8083.sh" /usr/local/bin/sentinel-adguard-8083
ln -sf "$APP_DIR/scripts/sentinel-tailscale.sh" /usr/local/bin/sentinel-tailscale
for f in setup.sh update.sh bootstrap.sh install.sh; do
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
# dbus-next: pure-Python, zero-dependency, asyncio-native D-Bus client -
# modules/bt_agent.py uses it to implement the BlueZ pairing agent
# (org.bluez.Agent1) directly instead of scraping bluetoothctl's
# interactive CLI output. main.py no longer spawns bt_agent.loop() (see
# that module's docstring - the agent kept losing its D-Bus registration
# race on real hardware), but the module and this dependency are kept
# installed so it can be re-enabled once fixed instead of requiring a
# fresh venv rebuild.
"$APP_DIR/.venv/bin/pip" install --quiet \
  "fastapi>=0.110" "uvicorn[standard]>=0.27" "websockets>=12" "dbus-next>=0.2"

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
c "STEP 4/10  Prepare data directory"
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

# ---------------------------------------------------------------- 5. Obsidian sync
c "STEP 5/10  Tear down Syncthing storage, set up Lockstep Sync"
# Syncthing (CLAUDE.md #40/#41/#45) is superseded by Lockstep Sync
# (CLAUDE.md #61). bootstrap.sh's STEP 9 already uninstalled the Syncthing
# package; this step only needs to undo the storage side of it - the bind
# mount that redirected its database off the SD card onto $STORAGE - and
# then set up storage and a systemd unit for the new server.
ST_HOME="$STORAGE/syncthing"
ST_DEFAULT=/mnt/dietpi_userdata/syncthing
if mountpoint -q "$ST_DEFAULT" 2>/dev/null; then
  for i in 1 2 3 4 5; do umount "$ST_DEFAULT" 2>/dev/null && break; sleep 1; done
  umount -l "$ST_DEFAULT" 2>/dev/null || true
  ok "Unmounted the old Syncthing bind mount at $ST_DEFAULT"
fi
sed -i "\\|^$ST_HOME $ST_DEFAULT |d" /etc/fstab 2>/dev/null || true
systemctl daemon-reload
if [[ -d "$ST_HOME" ]] && [[ -n "$(ls -A "$ST_HOME" 2>/dev/null)" ]]; then
  w "Syncthing's old database is still at $ST_HOME - safe to remove manually"
  w "  (it is only Syncthing's internal index, not your notes):  rm -rf $ST_HOME"
fi
if [[ -d "$STORAGE/obsidian" ]]; then
  ok "Your old synced vault is still at $STORAGE/obsidian - Lockstep Sync keeps no"
  echo "     plaintext copy on this Pi, so it does not use this path. Back it up or"
  echo "     remove it once every device has been re-paired through Lockstep Sync."
fi

# Unlike Syncthing, this project owns the systemd unit outright (DietPi
# does not template one for a package it never packaged), so there is no
# vendor-controlled ExecStart to route around with a bind mount - the data
# directory just needs to be a plain path on $STORAGE, handed to the
# server on its own command line below. And running it as $SVC_USER
# (rather than a second dedicated user) sidesteps the entire
# group-sharing dance CLAUDE.md #40 needed for Syncthing in the first
# place: $STORAGE was already made writable for $SVC_USER by STEP 4 above,
# so there is no second user's access to reconcile against it and no way
# to repeat that incident.
LS_BIN=/usr/local/bin/lockstep-sync-server
LS_DATA="$STORAGE/lockstep-sync"
if [[ -x "$LS_BIN" ]]; then
  mkdir -p "$LS_DATA"
  chown -R "$SVC_USER":"$SVC_USER" "$LS_DATA" 2>/dev/null || true

  sed -e "s|^ExecStart=.*|ExecStart=$LS_BIN serve --data $LS_DATA --addr 0.0.0.0:8384|" \
      -e "s|^User=.*|User=$SVC_USER|" \
      -e "s|^Group=.*|Group=$SVC_USER|" \
      "$SRC/systemd/sentinel-lockstep-sync.service" > /etc/systemd/system/sentinel-lockstep-sync.service
  systemctl daemon-reload
  systemctl reset-failed sentinel-lockstep-sync 2>/dev/null || true
  systemctl enable sentinel-lockstep-sync >/dev/null 2>&1
  # restart, not enable --now: the unit file above is rewritten on every
  # run (in case $STORAGE or $SVC_USER changed), and enable --now would
  # leave an already-running instance on its old ExecStart.
  if systemctl restart sentinel-lockstep-sync 2>/dev/null; then
    ok "Lockstep Sync server running (data: $LS_DATA; reachable over Tailscale only - see setup.sh H8)"
  else
    w "Failed to start sentinel-lockstep-sync.service; check 'journalctl -u sentinel-lockstep-sync'."
  fi
  # Binds 0.0.0.0:8384 (above) rather than a specific address because the
  # Tailscale interface's own IP is only assigned once 'tailscale up' has
  # actually run (setup.sh H7, a manual step) and can change if the node is
  # re-authed - baking a specific address into the unit would leave it
  # broken until the next install.sh/update.sh re-run. Reachability is
  # instead restricted to loopback + the tailscale0 interface by iptables,
  # the same DROP-based pattern already used for AdGuard's :8083
  # (CLAUDE.md #5), applied and kept in place by Guardian
  # (check_lockstep_firewall(), CLAUDE.md #61) rather than here, matching
  # how :8083's firewalling is Guardian's job too - install.sh runs it once
  # at the very end (STEP 10) so the rule is in place before this finishes.
else
  w "Lockstep Sync server not installed; skipping (see bootstrap.sh STEP 9)."
fi

# ---------------------------------------------------------------- 6. Old services
c "STEP 6/10  Disable legacy services"
for old in camguard music-player; do
  if systemctl list-unit-files | grep -q "^$old\.service"; then
    systemctl disable --now "$old" 2>/dev/null || true
    ok "Disabled $old"
  fi
done

# ---------------------------------------------------------------- 7. Bluetooth
c "STEP 7/10  Configure Bluetooth services"
BA=$(command -v bluealsad || command -v bluealsa || true)
if [[ -n "$BA" ]]; then
  install -m644 "$SRC/systemd/sentinel-bluealsa.service" /etc/systemd/system/
  # -p a2dp-sink: a phone connects TO this Pi and plays through its speaker.
  # -p a2dp-source: this Pi connects OUT to a headphone/speaker and plays
  # BGM there (modules/bluetooth.py's output_loop()). Both are needed since
  # either direction may be in use.
  sed -i "s|^ExecStart=.*|ExecStart=$BA -p a2dp-sink -p a2dp-source|" /etc/systemd/system/sentinel-bluealsa.service
  install -m644 "$SRC/systemd/sentinel-bluealsa-aplay.service" /etc/systemd/system/
  # The unit ships with a placeholder card index (0). Rewrite it with the
  # analog output actually detected on this machine - sysdefault:CARD=<N>
  # needs to name the right card explicitly, the same way music.py and
  # voice.py do (core/audio.analog_device()), rather than depend on
  # alsa-lib's own card selection picking analog over HDMI on multi-card
  # Pis (CLAUDE.md #37). If detection fails here, sentinel-guardian.sh's
  # periodic check_audio() (which calls sentinel-fix-audio-output.sh) will
  # not fix this specific unit's argument on its own - this is a one-time
  # install-time value, not something re-checked every cycle - so a
  # missing card at this exact moment is logged and left for a re-run of
  # this script (e.g. via update.sh) once the card is actually present.
  if AUDIO_CARD=$("$SRC/scripts/sentinel-fix-audio-output.sh" --print-card 2>/dev/null); then
    sed -i "s|--pcm=sysdefault:CARD=[0-9]*|--pcm=sysdefault:CARD=$AUDIO_CARD|" \
      /etc/systemd/system/sentinel-bluealsa-aplay.service
    ok "BlueALSA (A2DP sink+source) + playback bridge registered (card $AUDIO_CARD)"
  else
    w "no ALSA output card detected yet; sentinel-bluealsa-aplay.service keeps its placeholder card - re-run install.sh/update.sh once the card is present"
    ok "BlueALSA (A2DP sink+source) + playback bridge registered"
  fi
else
  w "bluealsa not found; Bluetooth-speaker feature will be unavailable."
fi

# A second bluealsa (typically the distribution's own bluealsa.service)
# takes the org.bluealsa D-Bus name, ours then exits on every start, and
# the restart churn that follows can wedge the bcm2835 card badly enough
# that music stops too (CLAUDE.md #45).
"$SRC/scripts/sentinel-fix-bluealsa.sh" || true

# Nothing in this project ever restores the shared hardware volume
# (numid=1) once something leaves it at zero, which silences music with no
# error anywhere - mpg123 still reports playing. Check the whole output
# path (volume, routing, whether sysdefault:CARD=<N> actually opens) here
# and every Guardian cycle (CLAUDE.md #41).
"$SRC/scripts/sentinel-fix-audio-output.sh" || true

# The pairing agent (modules/bt_agent.py) is no longer spawned by main.py
# (it kept losing the D-Bus agent-registration race on real hardware and
# never settled) - nothing to install or enable for it here. Pairing a new
# device is now done manually with `bluetoothctl` from the web UI's
# terminal tab, which registers its own agent reliably. See that module's
# docstring and CLAUDE.md's Bluetooth section for the history.

# JustWorksRepairing defaults to "never" upstream - bluetoothd rejects a
# peer-initiated Just-Works re-pair outright rather than silently accepting
# it. Any event that resets the bond state on either side (a bluetoothd
# restart from Guardian's own self-healing, CLAUDE.md #12/#45/#55, or the
# phone simply forgetting the pairing) then forces a *full* new pairing
# with a visible confirmation/passkey screen on the phone, even for a
# device this Pi has paired with many times before. "always" lets an
# already-known device silently re-pair with no prompt at all, which is
# the behavior CLAUDE.md #64's report compared this project against
# (CLAUDE.md #66).
if [[ -f /etc/bluetooth/main.conf ]]; then
  cp -n /etc/bluetooth/main.conf /etc/bluetooth/main.conf.sentinel-backup 2>/dev/null || true
  BEFORE_SUM=$(md5sum /etc/bluetooth/main.conf | awk '{print $1}')
  for kv in "DiscoverableTimeout=0" "PairableTimeout=0" "AlwaysPairable=true" "JustWorksRepairing=always"; do
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

# ---------------------------------------------------------------- 8. Guardian
c "STEP 8/10  Register Guardian (drift repair)"
install -m644 "$SRC/systemd/sentinel-guardian.service" /etc/systemd/system/
install -m644 "$SRC/systemd/sentinel-guardian.timer" /etc/systemd/system/
sed -i -e "s|^Environment=SENTINEL_DATA=.*|Environment=SENTINEL_DATA=$DATA|" \
       -e "s|^Environment=SENTINEL_STORAGE=.*|Environment=SENTINEL_STORAGE=$STORAGE|" \
  /etc/systemd/system/sentinel-guardian.service
ok "Runs every 2 minutes; repairs AdGuard bind / iptables / audio output /"
echo "     Bluetooth state (including a missing controller after a boot-time"
echo "     hciuart race, and stale BlueALSA D-Bus connections after any"
echo "     bluetoothd restart) / hostapd boot races / storage ownership /"
echo "     service uptime / hotspot DNS / disk space / yt-dlp"

install -m644 "$SRC/systemd/sentinel-autoupdate.service" /etc/systemd/system/
install -m644 "$SRC/systemd/sentinel-autoupdate.timer" /etc/systemd/system/
sed -i -e "s|^Environment=SENTINEL_DATA=.*|Environment=SENTINEL_DATA=$DATA|" \
  /etc/systemd/system/sentinel-autoupdate.service
ok "Auto-update registered; checks the git remote every 30 minutes and runs"
echo "     update.sh automatically once new commits land on it (toggle:"
echo "     system_autoupdate_enabled in the Web UI settings tab)"

# ---------------------------------------------------------------- 9. Main service
c "STEP 9/10  Register Sentinel service"
sed -e "s|^User=.*|User=$SVC_USER|" \
    -e "s|^Group=.*|Group=$SVC_USER|" \
    -e "s|^Environment=SENTINEL_STORAGE=.*|Environment=SENTINEL_STORAGE=$STORAGE|" \
    "$SRC/systemd/sentinel.service" > /etc/systemd/system/sentinel.service

# sentinel-deploy-resume was the resume unit of the old deploy.sh; a run
# interrupted back then can still leave it enabled. sentinel-bt-agent was
# the standalone pairing-agent unit retired in favor of modules/bt_agent.py
# running inside sentinel.service itself (CLAUDE.md's Bluetooth pairing
# redesign section) - an upgrade from an older install can still have it
# enabled, and leaving it running would fight the in-process agent for the
# D-Bus default-agent slot.
for stale in sentinel-firewall.service sentinel-bt-discoverable.service \
             sentinel-deploy-resume.service sentinel-bt-agent.service; do
  if systemctl list-unit-files 2>/dev/null | grep -q "^$stale"; then
    systemctl disable --now "$stale" 2>/dev/null || true
    rm -f "/etc/systemd/system/$stale"
    ok "Removed stale unit $stale"
  fi
done

systemctl daemon-reload
UNITS=(sentinel.service sentinel-guardian.timer sentinel-autoupdate.timer)
[[ -n "$BA" ]] && UNITS+=(sentinel-bluealsa.service sentinel-bluealsa-aplay.service)
systemctl enable "${UNITS[@]}" >/dev/null 2>&1
ok "Enabled: ${UNITS[*]}"
# Clear any "failed (start-limit-hit)" left over from before this script
# added StartLimitIntervalSec=0 to the Bluetooth units, or from any other
# past burst of restarts - reset-failed is a no-op on a unit that isn't
# in that state, so this is safe to run unconditionally.
systemctl reset-failed "${UNITS[@]}" 2>/dev/null || true
systemctl restart "${UNITS[@]}"

# ---------------------------------------------------------------- 10. First run
c "STEP 10/10  Run Guardian once now"
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
  LS_LINE=""
  if systemctl is-active --quiet sentinel-lockstep-sync 2>/dev/null; then
    LS_LINE="  Lockstep    reachable only over Tailscale, at :8384 - pair devices
              during H8 of setup.sh if you have not yet
"
  fi
  cat <<EOS

== Install complete ==

  Web UI      http://${IP:-<this-Pi-IP>}:8080
  AdGuard     locked to localhost now; blocked externally by default - run
              'sudo sentinel-adguard-8083 enable [MINUTES]' to open :8083
              (log in with the admin password set during H5 of setup.sh)
${LS_LINE}  Data        $DATA
  Logs        journalctl -u sentinel -f
  Guardian    journalctl -t sentinel-guardian -f
  Diagnostics sentinel-diagnose

  Do this next, in the Web UI settings tab:
    1. Set a Web UI password
    2. Set the Discord webhook URL

EOS
else
  die "Service failed to start. Check: journalctl -u sentinel -n 60 --no-pager"
fi
