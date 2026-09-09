#!/usr/bin/env bash
# Sentinel bootstrap — run once, right after first DietPi boot.
#
#   sudo ./bootstrap.sh
#
# What it does, in order (order matters):
#   1. Force an English UTF-8 locale (fixes console mojibake even if the
#      image was first booted with a CJK locale already).
#   2. Install base tools (ALSA, FFmpeg, Git, Python3 pip, yt-dlp).
#   3. Install AdGuard Home + Unbound together (so DietPi wires them up:
#      Unbound moves to port 5335 and becomes AdGuard's upstream).
#   4. Install WiFi Hotspot *after* AdGuard, so its DHCP can later be
#      pointed at AdGuard for DNS (done by install.sh / the Guardian).
#   5. Enable Bluetooth (a dietpi-config item, not dietpi-software).
#   6. Fix audio output to the 3.5mm jack.
#   7. Disable SWAP (protects the SD card; 2 cameras fit in 1GB RAM).
#
# A reboot is only needed the first time, when Bluetooth/audio/SWAP
# actually change - this script tracks that and says so at the end, so
# it stays truthful when re-run later (update.sh calls it on every
# update: it must not claim a reboot is needed when nothing changed).
set -uo pipefail

c()  { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok() { printf '  [OK] %s\n' "$*"; }
w()  { printf '  [!!] %s\n' "$*"; }
die(){ printf '[FAIL] %s\n' "$*" >&2; exit 1; }

NEEDS_REBOOT=0

[[ $EUID -eq 0 ]] || die "run as root (sudo ./bootstrap.sh)"

# DietPi's own commands normally resolve via ~/.bashrc on an interactive
# login shell, but a non-login "sudo ./bootstrap.sh" invocation can start
# with a stripped-down PATH that doesn't include them - which then looks
# exactly like "this isn't DietPi" even though it is. Widen PATH first, so
# the check right below reflects whether DietPi is actually here.
export PATH="/boot/dietpi:/usr/local/bin:$PATH"

command -v dietpi-software >/dev/null || [[ -x /boot/dietpi/dietpi-software ]] || [[ -d /boot/dietpi ]] || \
  die "this must run on DietPi (/boot/dietpi not found)"

DS=/boot/dietpi/dietpi-software
[[ -x "$DS" ]] || DS=$(command -v dietpi-software)
SETHW=/boot/dietpi/func/dietpi-set_hardware
SETSW=/boot/dietpi/func/dietpi-set_software

# ---------------------------------------------------------------- 1. Locale
c "STEP 1/7  Force English UTF-8 locale"
CUR_LANG=$(grep -m1 '^LANG=' /etc/default/locale 2>/dev/null | cut -d= -f2)
if [[ "$CUR_LANG" != "en_US.UTF-8" && "$CUR_LANG" != "en_GB.UTF-8" && "$CUR_LANG" != "C.UTF-8" ]]; then
  if [[ -x "$SETSW" ]]; then
    "$SETSW" locale en_US.UTF-8 && { ok "Locale set to en_US.UTF-8 (was: ${CUR_LANG:-unset})"; NEEDS_REBOOT=1; } \
      || w "Locale change failed; run 'dietpi-config' -> Language/Regional Options manually."
  else
    w "$SETSW not found; set the locale manually via dietpi-config."
  fi
else
  ok "Locale already English ($CUR_LANG)"
fi

# ---------------------------------------------------------------- 2. Base
c "STEP 2/7  Install base packages"
# 5=ALSA  7=FFmpeg  17=Git  130=Python 3 pip  195=yt-dlp
# Checked by the artifact each ID actually installs, not dietpi-software's
# own bookkeeping, so a re-run only calls dietpi-software for what is
# genuinely still missing (faster, and one less thing that can go wrong on
# something already working).
NEED2=()
dpkg -s alsa-utils >/dev/null 2>&1 || NEED2+=(5)
command -v ffmpeg  >/dev/null      || NEED2+=(7)
command -v git      >/dev/null     || NEED2+=(17)
command -v pip3     >/dev/null     || NEED2+=(130)
command -v yt-dlp   >/dev/null     || NEED2+=(195)
if (( ${#NEED2[@]} )); then
  "$DS" install "${NEED2[@]}" && ok "ALSA / FFmpeg / Git / Python3-pip / yt-dlp" \
    || w "Some base packages failed to install; check manually."
else
  ok "ALSA / FFmpeg / Git / Python3-pip / yt-dlp already installed; skipped"
fi

# ffmpeg's drawtext filter needed libharfbuzz as well as libfreetype since
# ffmpeg 6.1, and Debian/Raspberry Pi OS have shipped ffmpeg builds without
# it (https://bugs.debian.org/1056597). On a frozen stable release,
# "apt-get install --only-upgrade ffmpeg" can never fix that - there may
# simply be no newer package in the repo to upgrade to, ever, until the
# next stable release. Chasing an OS packaging fix that may never arrive
# was the wrong approach. The daily archive's text overlays (NODATA label,
# camera caption, access-log ticker) are drawn as PNGs with Pillow instead
# and composited with ffmpeg's overlay/drawbox filters, which are core
# filters always present regardless of how ffmpeg was built - see
# sentinel/modules/maintenance.py. Nothing here depends on drawtext any
# more; python3-pil above is the only requirement, and it has no such
# build-flag pitfall since it does not link ffmpeg at all.

# ---------------------------------------------------------------- 3. DNS
c "STEP 3/7  Install AdGuard Home + Unbound"
# 126=AdGuard Home  182=Unbound
# Installing both in one call makes DietPi wire them together automatically,
# so re-run it if EITHER is missing rather than checking them separately.
NEED3=()
[[ -x /mnt/dietpi_userdata/adguardhome/AdGuardHome ]] || NEED3+=(126)
{ command -v unbound >/dev/null || dpkg -s unbound >/dev/null 2>&1; } || NEED3+=(182)
if (( ${#NEED3[@]} )); then
  "$DS" install 126 182 && ok "AdGuard Home (port 8083) / Unbound (port 5335)" \
    || w "AdGuard/Unbound install failed."
else
  ok "AdGuard Home / Unbound already installed; skipped"
fi

# ---------------------------------------------------------------- 4. Hotspot
c "STEP 4/7  Install WiFi Hotspot"
# 60=WiFi Hotspot (hostapd). Installed after AdGuard so its DHCP can later
# be pointed at AdGuard for DNS logging.
if [[ "${SKIP_HOTSPOT:-0}" != "1" ]]; then
  if command -v hostapd >/dev/null; then
    ok "WiFi Hotspot already installed; skipped"
  else
    "$DS" install 60 && ok "WiFi Hotspot (default subnet 192.168.42.0/24)" \
      || w "Hotspot install failed."
  fi
else
  w "SKIP_HOTSPOT=1 set; skipped."
fi

# ---------------------------------------------------------------- 5. Bluetooth
c "STEP 5/7  Enable Bluetooth"
# Bluetooth is a dietpi-config item, not a dietpi-software package,
# so we call the internal hardware-setup function directly.
# GUI equivalent: dietpi-config -> 4 Advanced Options -> Bluetooth.
BT_WAS_READY=0
systemctl is-active --quiet bluetooth 2>/dev/null && BT_WAS_READY=1
if [[ -x "$SETHW" ]]; then
  if "$SETHW" bluetooth enable; then
    ok "Bluetooth enabled"
    (( BT_WAS_READY )) || NEEDS_REBOOT=1
  else
    w "Enable failed; check dietpi-config -> Advanced Options -> Bluetooth."
  fi
else
  w "$SETHW not found; enable Bluetooth manually via dietpi-config."
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# bluez-alsa-utils: the A2DP sink itself. The persistent pairing agent
# (scripts/sentinel-bt-agent.sh) drives bluetoothctl directly instead of
# bluez-tools' bt-agent binary (which has a known NoInputNoOutput
# regression on Bullseye+ that breaks iOS pairing - see
# systemd/sentinel-bt-agent.service), so bluez-tools is not installed here.
# exfatprogs/ntfs-3g: dietpi-drive_manager can mount exFAT/NTFS drives, but
# without these packages that mount can fail outright. Even with them,
# such drives have no real Unix ownership - install.sh and Guardian handle
# that separately by fixing the mount's uid=/gid= options.
apt-get install -y --no-install-recommends \
  bluez bluez-alsa-utils \
  mpg123 v4l-utils python3-opencv python3-pil python3-venv \
  fonts-dejavu-core fonts-noto-cjk iptables \
  exfatprogs ntfs-3g >/dev/null 2>&1 \
  && ok "Bluetooth-audio, camera and exFAT/NTFS packages installed" \
  || w "Some packages failed to install."

# ---------------------------------------------------------------- 6. Audio
c "STEP 6/7  Route audio to the 3.5mm jack (AUX)"
# "rpi-bcm2835-3.5mm" is the exact name dietpi-set_hardware expects; any
# other string is treated as "unknown card" and silently resets to default.
# The helper also sets dtparam=audio=on and snd_bcm2835.enable_hdmi=0 for us,
# so config.txt is only touched by hand if the helper is missing.
AUDIO_WAS_READY=0
aplay -l 2>/dev/null | grep -qi 'bcm2835\|headphones' && AUDIO_WAS_READY=1
AUDIO_DONE=0
if [[ -x "$SETHW" ]]; then
  "$SETHW" soundcard rpi-bcm2835-3.5mm >/dev/null 2>&1 \
    && { AUDIO_DONE=1; ok "Sound card set to rpi-bcm2835-3.5mm"; } \
    || w "Sound card selection failed; check dietpi-config -> Audio Options."
fi
(( AUDIO_DONE )) && (( ! AUDIO_WAS_READY )) && NEEDS_REBOOT=1
if (( ! AUDIO_DONE )); then
  BOOTCFG=$(ls /boot/firmware/config.txt /boot/config.txt 2>/dev/null | head -1 || true)
  if [[ -n "$BOOTCFG" ]]; then
    grep -qE '^dtparam=audio=on' "$BOOTCFG" || echo 'dtparam=audio=on' >> "$BOOTCFG"
    ok "dtparam=audio=on written to $BOOTCFG (fallback)"
  fi
fi

# ---------------------------------------------------------------- 7. Resources
c "STEP 7/7  Reduce resource usage"
# Disable SWAP: fewer SD card writes, longer card life.
# 2 cameras + music fit comfortably in 1GB RAM without it.
SWAP_WAS_ON=0
swapon --show 2>/dev/null | grep -q . && SWAP_WAS_ON=1
if grep -qE '^AUTO_SETUP_SWAPFILE_SIZE=' /boot/dietpi.txt 2>/dev/null; then
  sed -i 's/^AUTO_SETUP_SWAPFILE_SIZE=.*/AUTO_SETUP_SWAPFILE_SIZE=0/' /boot/dietpi.txt
fi
if /boot/dietpi/func/dietpi-set_swapfile 0 >/dev/null 2>&1; then
  ok "SWAP disabled"
  (( SWAP_WAS_ON )) && NEEDS_REBOOT=1
else
  w "SWAP disable failed; check via dietpi-config."
fi

systemctl is-active --quiet dietpi-ramlog 2>/dev/null && ok "DietPi-RAMlog active" \
  || w "DietPi-RAMlog inactive; consider 'dietpi-software install 103'."

if (( NEEDS_REBOOT )); then
  cat <<'EOS'

== Prerequisites done ==

A reboot is required now: Bluetooth, the audio route or the SWAP setting
actually changed just now, and none of those take effect until a restart.

If you started from setup.sh, go back to it - it asks for the reboot and
then continues with the storage, AdGuard and install steps:

    reboot
    # after logging back in
    cd ~/sentinel-pi && sudo ./setup.sh

If you are driving the steps yourself, the remaining ones are:

    1. reboot
    2. dietpi-drive_manager   -> mount the external drive on /mnt/VIDEOSD
    3. log in to AdGuard Home once, at http://<this-Pi-IP>:8083, while it
       is still reachable directly (set/confirm its admin password there)
    4. sudo ./install.sh     -> locks AdGuard's web UI to localhost and
       blocks :8083 from outside (Guardian keeps it that way); afterwards,
       use 'sudo sentinel-adguard-8083 enable [MINUTES]' for direct access

EOS
else
  cat <<'EOS'

== Prerequisites done ==

Nothing that needs a reboot changed this run (Bluetooth/audio/SWAP were
already in place). No reboot needed - continue with:

    sudo ./install.sh

or, from a git clone, sudo ./update.sh / sudo ./setup.sh --update.

EOS
fi
