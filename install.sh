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
# sentinel-setup-audio-mixing.sh: /etc/asound.conf is root-writable only.
# modules/music.py calls it whenever the equalizer is toggled/changed so
# music and voice announcements (modules/voice.py) can mix through ALSA's
# dmix plugin with independent volumes instead of one fully stopping the
# other (CLAUDE.md #31). Same argument-validation-is-the-boundary pattern.
cat > /etc/sudoers.d/sentinel <<EOF
$SVC_USER ALL=(root) NOPASSWD: /sbin/reboot, /sbin/shutdown, /usr/bin/systemctl reboot, $APP_DIR/scripts/sentinel-set-governor.sh *, $APP_DIR/scripts/sentinel-set-hotspot-ssid.sh *, $APP_DIR/scripts/sentinel-setup-audio-mixing.sh *
EOF
chmod 440 /etc/sudoers.d/sentinel
visudo -cf /etc/sudoers.d/sentinel >/dev/null && ok "sudoers: reboot + CPU governor switch + hotspot SSID + audio mixing"

# ---------------------------------------------------------------- 3. Deploy
c "STEP 3/10  Deploy application files"
mkdir -p "$APP_DIR/scripts"
rm -rf "$APP_DIR/sentinel"
cp -r "$SRC/sentinel" "$APP_DIR/"
cp -f "$SRC/scripts/"*.sh "$APP_DIR/scripts/"
chmod +x "$APP_DIR/scripts/"*.sh
ln -sf "$APP_DIR/scripts/sentinel-diagnose.sh" /usr/local/bin/sentinel-diagnose
ln -sf "$APP_DIR/scripts/sentinel-adguard-8083.sh" /usr/local/bin/sentinel-adguard-8083
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
c "STEP 4/10  Prepare data directory"
# Repair a stale/duplicate $STORAGE mount before trusting `mountpoint -q`
# below - a mount can appear present while still carrying wrong options
# (mount -a does not fix an already-mounted filesystem) or while the same
# device is also mounted a second time elsewhere (CLAUDE.md #40). Always
# safe to run: a clean machine exits immediately without touching anything.
"$SRC/scripts/sentinel-fix-syncthing-mount.sh" "$SVC_USER" || true

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

# ---------------------------------------------------------------- 5. Syncthing
c "STEP 5/10  Prepare Syncthing storage (Obsidian sync)"
# Syncthing (bootstrap.sh STEP 9) keeps its home/config/index database, by
# default, under /mnt/dietpi_userdata/syncthing - normally on the SD card,
# same as everything else DietPi installs there unless dietpi_userdata
# itself was redirected during DietPi's own first-run setup. A synced
# Obsidian vault writes far more often than anything else this project
# touches (every edit, from every device), so that directory is bind-
# mounted onto $STORAGE here instead, matching CLAUDE.md #3's reasoning
# for keeping high-frequency writes off the SD card.
#
# A bind mount, not a symlink or an edited systemd unit: DietPi generates
# (and can regenerate) syncthing.service's ExecStart line, so hard-coding
# its exact -home flag syntax here would be guessing at something this
# project does not own. A bind mount needs none of that - it works at the
# filesystem level, transparently to Syncthing, and (unlike a symlink
# pointing outside dietpi_userdata) survives any systemd path sandboxing
# DietPi's unit applies to the literal /mnt/dietpi_userdata/syncthing path.
#
# Ownership: Syncthing runs as 'dietpi', a *different* user than $SVC_USER,
# on the *same* $STORAGE mount that STEP 4 above just fixed for $SVC_USER.
# sentinel-fix-storage-owner.sh must never be called a second time here
# with 'dietpi' as the target user: on exFAT/NTFS its exFAT/NTFS branch
# rewrites uid=/gid= *mount options*, which apply to the whole mount, not
# a single directory (CLAUDE.md #8) - calling it again for a different
# user overwrites the uid=/gid= that was just set for $SVC_USER, and a
# real incident on real hardware showed what that does: Guardian's
# check_storage_owner() (for $SVC_USER) and this step (for dietpi) fought
# over the same mount every cycle, each one's fix_fat_mount() re-running
# umount/mount (falling back to umount -l) on a mount every other service
# still had files open on - which took the whole box's services down and
# left the drive mounted somewhere other than $STORAGE. Adding 'dietpi' to
# $SVC_USER's group instead lets it use the *same* already-fixed mount
# options (umask=002 already grants group write) without ever touching
# fstab or the mount a second time - safe on exFAT/NTFS *and* ext4, so no
# filesystem-specific branching is needed here at all.
if [[ -x /opt/syncthing/syncthing ]]; then
  ST_HOME="$STORAGE/syncthing"
  ST_DEFAULT=/mnt/dietpi_userdata/syncthing
  ST_VAULTS="$STORAGE/obsidian"
  mkdir -p "$ST_HOME" "$ST_VAULTS"

  # A supplementary group only applies to processes started *after* the
  # change - an already-running syncthing.service keeps its old groups
  # until restarted. Only force that restart when the membership is
  # actually new (checked before usermod, which is itself always a
  # successful no-op when already a member and so cannot tell new from
  # existing on its own) - no need to bounce Syncthing on every run once
  # this has already applied once.
  ST_NEED_RESTART=0
  if ! id -nG dietpi 2>/dev/null | grep -qw "$SVC_USER"; then
    usermod -aG "$SVC_USER" dietpi 2>/dev/null && {
      ok "dietpi added to the $SVC_USER group (shares its already-fixed $STORAGE access)"
      ST_NEED_RESTART=1
    }
  fi
  # Belt-and-braces group ownership on the directories themselves. A no-op
  # on exFAT/NTFS (chgrp/chmod cannot do anything there - group access
  # already comes from the mount's own gid=/umask= options above) but
  # matters on a real Unix filesystem (ext4, ...), where each directory
  # has its own ownership independent of the mount. Never touches $STORAGE
  # itself or any mount option - purely a directory-level chgrp/chmod.
  chgrp -R "$SVC_USER" "$ST_HOME" "$ST_VAULTS" 2>/dev/null || true
  chmod -R g+rwX "$ST_HOME" "$ST_VAULTS" 2>/dev/null || true
  chmod g+s "$ST_HOME" "$ST_VAULTS" 2>/dev/null || true

  if runuser -u dietpi -- sh -c ': > "$1/.st-write-test.$$" && rm -f "$1/.st-write-test.$$"' _ "$ST_HOME" 2>/dev/null; then
    ok "dietpi can write to $ST_HOME"
  else
    w "dietpi still cannot write to $ST_HOME; Syncthing may fail to start."
    w "A reboot (or logging dietpi out/in) may be needed for the new group membership to take effect."
  fi

  # Defensive: something other than our own bind mount can occasionally
  # grab this directory first - DietPi's own drive-detection auto-mounting
  # a newly-visible partition directly onto an existing empty mountpoint is
  # a boot-time race in the same family as CLAUDE.md #12, and a real
  # incident showed the same partition ending up mounted here directly
  # (not via bind - `mount`/`findmnt` show the raw device as SOURCE, not
  # "$ST_HOME[/...]") instead of at $STORAGE. If left alone, the
  # `mount --bind` below would stack a *second* independent mount of the
  # same filesystem on top of it rather than replacing it - two live
  # mounts of one exFAT/NTFS filesystem can be written out of sync with
  # each other and risk real data corruption. Clear anything that isn't
  # our bind mount before proceeding.
  if mountpoint -q "$ST_DEFAULT" 2>/dev/null; then
    CUR_SRC=$(findmnt -no SOURCE "$ST_DEFAULT" 2>/dev/null | tail -n1)
    if [[ "$CUR_SRC" != "$ST_HOME"* ]]; then
      w "$ST_DEFAULT is already mounted from $CUR_SRC (not our bind mount) - clearing it first"
      for i in 1 2 3 4 5; do umount "$ST_DEFAULT" 2>/dev/null && break; sleep 1; done
      umount -l "$ST_DEFAULT" 2>/dev/null || true
    fi
  fi

  # Already bind-mounted from a previous run? A bind mount makes the target
  # report the *source* directory's device+inode, so comparing those is a
  # reliable, filesystem-agnostic way to tell "already redirected" apart
  # from "still the plain directory DietPi created" without depending on
  # mount option text (which findmnt can report in a confusing, stacked
  # way for autofs-backed drives - see sentinel-fix-storage-owner.sh).
  SAME_MOUNT=0
  if [[ -d "$ST_DEFAULT" ]]; then
    A=$(stat -c '%d:%i' "$ST_HOME" 2>/dev/null || echo a)
    B=$(stat -c '%d:%i' "$ST_DEFAULT" 2>/dev/null || echo b)
    [[ -n "$A" && "$A" == "$B" ]] && SAME_MOUNT=1
  fi

  if (( SAME_MOUNT )); then
    ok "Syncthing home already redirected to $ST_HOME"
  else
    ST_WAS_RUNNING=0
    if systemctl is-active --quiet syncthing 2>/dev/null; then
      ST_WAS_RUNNING=1
      systemctl stop syncthing 2>/dev/null || true
    fi

    # One-time migration, and only one-time: only when the default location
    # actually has data and the target is still empty, so re-running this
    # step (every install.sh / update.sh) never overwrites either side.
    if [[ -d "$ST_DEFAULT" ]] && [[ -n "$(ls -A "$ST_DEFAULT" 2>/dev/null)" ]] \
       && [[ -z "$(ls -A "$ST_HOME" 2>/dev/null)" ]]; then
      if cp -a "$ST_DEFAULT"/. "$ST_HOME"/; then
        ok "Migrated existing Syncthing data to $ST_HOME"
      else
        w "Failed to migrate existing Syncthing data from $ST_DEFAULT"
      fi
    fi

    mkdir -p "$ST_DEFAULT"
    grep -qF " $ST_DEFAULT " /etc/fstab || \
      echo "$ST_HOME $ST_DEFAULT none bind 0 0" >> /etc/fstab
    systemctl daemon-reload

    if MNT_OUT=$(mount --bind "$ST_HOME" "$ST_DEFAULT" 2>&1); then
      ok "Bind-mounted $ST_HOME onto $ST_DEFAULT"
    else
      w "Bind mount failed: $MNT_OUT"
      w "Syncthing will keep using $ST_DEFAULT on the SD card until this is fixed."
    fi

    (( ST_WAS_RUNNING )) && { systemctl start syncthing 2>/dev/null || true; }
  fi
  if (( ST_NEED_RESTART )) && systemctl is-active --quiet syncthing 2>/dev/null; then
    systemctl restart syncthing 2>/dev/null && ok "Restarted Syncthing to pick up its new group membership"
  fi
  systemctl enable --now syncthing >/dev/null 2>&1 || true

  # Syncthing's own default GUI bind (upstream default, unchanged by
  # DietPi's package) is 127.0.0.1:8384 - loopback only. This script's own
  # closing summary, and SETUP.md/SETUP.ja.md, tell the user to open
  # http://<Pi-IP>:8384 from a LAN browser to set the GUI password
  # (setup.sh H8) - a loopback-only bind makes that unreachable. Syncthing's
  # own GUI password is the access control here, the same "reachable,
  # password-gated" model as Sentinel's own Web UI on :8080 - unlike
  # AdGuard's :8083 (CLAUDE.md #5), which stays loopback-only for reasons
  # specific to that incident's history, nothing here calls for the same
  # restriction, so this one binds openly. config.xml does not exist until
  # Syncthing has started at least once and generated it, so a first-ever
  # install run may not see it yet - the next update.sh run (and Guardian's
  # own periodic check) picks it up.
  ST_CONFIG="$ST_HOME/config.xml"
  if [[ -f "$ST_CONFIG" ]] && grep -q 'address="127\.0\.0\.1:8384"' "$ST_CONFIG"; then
    sed -i 's#address="127\.0\.0\.1:8384"#address="0.0.0.0:8384"#' "$ST_CONFIG"
    if systemctl restart syncthing 2>/dev/null; then
      ok "Syncthing GUI now reachable at :8384 (was loopback-only)"
    else
      w "edited Syncthing's GUI bind but could not restart syncthing"
    fi
  fi
else
  w "Syncthing not installed; skipping storage redirect (see bootstrap.sh STEP 9)."
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
  sed -i "s|^ExecStart=.*|ExecStart=$BA -p a2dp-sink|" /etc/systemd/system/sentinel-bluealsa.service
  install -m644 "$SRC/systemd/sentinel-bluealsa-aplay.service" /etc/systemd/system/
  ok "BlueALSA (A2DP sink) + playback bridge registered"
else
  w "bluealsa not found; Bluetooth-speaker feature will be unavailable."
fi

if command -v bluetoothctl >/dev/null; then
  install -m644 "$SRC/systemd/sentinel-bt-agent.service" /etc/systemd/system/
  ok "Persistent pairing agent registered"
else
  w "bluetoothctl missing (apt install bluez); new pairings may fail."
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
UNITS=(sentinel.service sentinel-guardian.timer sentinel-autoupdate.timer)
[[ -n "$BA" ]] && UNITS+=(sentinel-bluealsa.service sentinel-bluealsa-aplay.service)
command -v bluetoothctl >/dev/null && UNITS+=(sentinel-bt-agent.service)
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
  ST_LINE=""
  if [[ -x /opt/syncthing/syncthing ]]; then
    ST_LINE="  Syncthing   http://${IP:-<this-Pi-IP>}:8384 (Obsidian sync) - set a GUI
              password and pair devices during H8 of setup.sh if you have not yet
"
  fi
  cat <<EOS

== Install complete ==

  Web UI      http://${IP:-<this-Pi-IP>}:8080
  AdGuard     locked to localhost now; blocked externally by default - run
              'sudo sentinel-adguard-8083 enable [MINUTES]' to open :8083
              (log in with the admin password set during H5 of setup.sh)
${ST_LINE}  Data        $DATA
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
