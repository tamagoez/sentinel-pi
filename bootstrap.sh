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
# Reboot after this script finishes — Bluetooth and audio changes require it.
set -uo pipefail

c()  { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok() { printf '  [OK] %s\n' "$*"; }
w()  { printf '  [!!] %s\n' "$*"; }
die(){ printf '[FAIL] %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo ./bootstrap.sh)"
command -v dietpi-software >/dev/null || die "this must run on DietPi"

DS=/boot/dietpi/dietpi-software
[[ -x "$DS" ]] || DS=$(command -v dietpi-software)
SETHW=/boot/dietpi/func/dietpi-set_hardware
SETSW=/boot/dietpi/func/dietpi-set_software

# ---------------------------------------------------------------- 1. Locale
c "STEP 1/7  Force English UTF-8 locale"
CUR_LANG=$(grep -m1 '^LANG=' /etc/default/locale 2>/dev/null | cut -d= -f2)
if [[ "$CUR_LANG" != "en_US.UTF-8" && "$CUR_LANG" != "en_GB.UTF-8" && "$CUR_LANG" != "C.UTF-8" ]]; then
  if [[ -x "$SETSW" ]]; then
    "$SETSW" locale en_US.UTF-8 && ok "Locale set to en_US.UTF-8 (was: ${CUR_LANG:-unset})" \
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
# ALSA first: later audio steps assume a sound card already exists.
"$DS" install 5 7 17 130 195 || w "Some base packages failed to install; check manually."
ok "ALSA / FFmpeg / Git / Python3-pip / yt-dlp"

# ---------------------------------------------------------------- 3. DNS
c "STEP 3/7  Install AdGuard Home + Unbound"
# 126=AdGuard Home  182=Unbound
# Installing both in one call makes DietPi wire them together automatically.
"$DS" install 126 182 || w "AdGuard/Unbound install failed."
ok "AdGuard Home (port 8083) / Unbound (port 5335)"

# ---------------------------------------------------------------- 4. Hotspot
c "STEP 4/7  Install WiFi Hotspot"
# 60=WiFi Hotspot (hostapd). Installed after AdGuard so its DHCP can later
# be pointed at AdGuard for DNS logging.
if [[ "${SKIP_HOTSPOT:-0}" != "1" ]]; then
  "$DS" install 60 || w "Hotspot install failed."
  ok "WiFi Hotspot (default subnet 192.168.42.0/24)"
else
  w "SKIP_HOTSPOT=1 set; skipped."
fi

# ---------------------------------------------------------------- 5. Bluetooth
c "STEP 5/7  Enable Bluetooth"
# Bluetooth is a dietpi-config item, not a dietpi-software package,
# so we call the internal hardware-setup function directly.
# GUI equivalent: dietpi-config -> 4 Advanced Options -> Bluetooth.
if [[ -x "$SETHW" ]]; then
  "$SETHW" bluetooth enable && ok "Bluetooth enabled" \
    || w "Enable failed; check dietpi-config -> Advanced Options -> Bluetooth."
else
  w "$SETHW not found; enable Bluetooth manually via dietpi-config."
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# bluez-alsa-utils: the A2DP sink itself / bluez-tools: the persistent bt-agent
apt-get install -y --no-install-recommends \
  bluez bluez-alsa-utils bluez-tools \
  mpg123 v4l-utils python3-opencv python3-venv \
  fonts-dejavu-core fonts-noto-cjk iptables >/dev/null 2>&1 \
  && ok "Bluetooth-audio and camera packages installed" \
  || w "Some packages failed to install."

# ---------------------------------------------------------------- 6. Audio
c "STEP 6/7  Route audio to the 3.5mm jack (AUX)"
# "rpi-bcm2835-3.5mm" is the exact name dietpi-set_hardware expects; any
# other string is treated as "unknown card" and silently resets to default.
# The helper also sets dtparam=audio=on and snd_bcm2835.enable_hdmi=0 for us,
# so config.txt is only touched by hand if the helper is missing.
AUDIO_DONE=0
if [[ -x "$SETHW" ]]; then
  "$SETHW" soundcard rpi-bcm2835-3.5mm >/dev/null 2>&1 \
    && { AUDIO_DONE=1; ok "Sound card set to rpi-bcm2835-3.5mm"; } \
    || w "Sound card selection failed; check dietpi-config -> Audio Options."
fi
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
if grep -qE '^AUTO_SETUP_SWAPFILE_SIZE=' /boot/dietpi.txt 2>/dev/null; then
  sed -i 's/^AUTO_SETUP_SWAPFILE_SIZE=.*/AUTO_SETUP_SWAPFILE_SIZE=0/' /boot/dietpi.txt
fi
/boot/dietpi/func/dietpi-set_swapfile 0 >/dev/null 2>&1 && ok "SWAP disabled" \
  || w "SWAP disable failed; check via dietpi-config."

systemctl is-active --quiet dietpi-ramlog 2>/dev/null && ok "DietPi-RAMlog active" \
  || w "DietPi-RAMlog inactive; consider 'dietpi-software install 103'."

cat <<'EOS'

== Prerequisites done ==

A reboot is required now: Bluetooth, the audio route and the SWAP change
only take effect after a restart.

If you started from setup.sh, go back to it - it asks for the reboot and
then continues with the storage, AdGuard and install steps:

    reboot
    # after logging back in
    cd ~/sentinel-pi && sudo ./setup.sh

If you are driving the steps yourself, the remaining ones are:

    1. reboot
    2. dietpi-drive_manager   -> mount the external drive on /mnt/VIDEOSD
    3. http://<this-Pi-IP>:8083  -> log in to AdGuard Home (user: admin,
       password: the DietPi global software password) and confirm that
       query logging is on. It is still reachable directly at this point;
       install.sh locks it to localhost.
    4. sudo ./install.sh

EOS
