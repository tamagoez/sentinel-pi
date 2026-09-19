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
#   8. Install Tailscale (package only - joining a tailnet needs a human to
#      open a login URL in a browser, so that is a manual step in
#      setup.sh, not here).
#   9. Uninstall Syncthing if present (superseded - see CLAUDE.md #61) and
#      install the Lockstep Sync server binary (package only - its storage
#      dir needs the external drive already mounted, so that is done by
#      install.sh; pairing devices is a manual step in setup.sh, same
#      reasoning as Tailscale above).
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
c "STEP 1/9  Force English UTF-8 locale"
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
c "STEP 2/9  Install base packages"
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
c "STEP 3/9  Install AdGuard Home + Unbound"
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
c "STEP 4/9  Install WiFi Hotspot"
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
c "STEP 5/9  Enable Bluetooth"
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
# bluez-alsa-utils: the A2DP sink itself, plus bluealsa-cli (used by
# modules/bluetooth.py for per-device volume - CLAUDE.md's Bluetooth
# redesign section). The pairing agent is modules/bt_agent.py, a D-Bus
# Agent1 implementation running inside sentinel.service itself (via the
# dbus-next pip package, installed by install.sh into the venv) - not
# bluez-tools' bt-agent binary, which has a known NoInputNoOutput
# regression on Bullseye+ that breaks iOS pairing, so bluez-tools is not
# installed here.
# exfatprogs/ntfs-3g: dietpi-drive_manager can mount exFAT/NTFS drives, but
# without these packages that mount can fail outright. Even with them,
# such drives have no real Unix ownership - install.sh and Guardian handle
# that separately by fixing the mount's uid=/gid= options.
# open-jtalk + its mecab dictionary + a voice model: voice announcements
# (modules/voice.py), preferred over espeak-ng for actually-intelligible
# Japanese - real reports on real hardware called espeak-ng's output
# unintelligible. Open JTalk parses text through the same MeCab dictionary
# naist-jdic uses before synthesizing, so it reads kanji correctly instead
# of guessing phonetically. This is heavier than espeak-ng alone (tens of
# MB for the dictionary + voice model combined) but still well inside the
# RAM 1GB / SD-card budget this project is built around (see CLAUDE.md
# "依存を増やさない") - nowhere near a few-hundred-MB engine like VOICEVOX.
# espeak-ng stays installed too as an always-available fallback
# (modules/voice.py falls back to it automatically if Open JTalk's
# packages are ever missing), so voice.py never goes silent outright.
# There used to be a swh-plugins entry here for the mbeq LADSPA plugin the
# music equalizer needed. The equalizer now uses mpg123's own built-in
# real-time equalizer (its remote-control "E" command) instead of an ALSA
# LADSPA stage, so no extra package is needed for it at all - see
# CLAUDE.md's audio-mixing redesign section.
# qrencode: prints the Lockstep Sync device-pairing link as a scannable QR
# code in setup.sh H8 (CLAUDE.md #61) - the same tool upstream's own
# install.sh uses for this, so no protocol/library work is needed here.
apt-get install -y --no-install-recommends \
  bluez bluez-alsa-utils \
  mpg123 v4l-utils python3-opencv python3-pil python3-venv \
  fonts-dejavu-core fonts-noto-cjk iptables \
  exfatprogs ntfs-3g espeak-ng \
  open-jtalk open-jtalk-mecab-naist-jdic hts-voice-nitech-jp-atr503-m001 \
  qrencode >/dev/null 2>&1 \
  && ok "Bluetooth-audio, camera, exFAT/NTFS and voice packages installed" \
  || w "Some packages failed to install."

# ---------------------------------------------------------------- 6. Audio
c "STEP 6/9  Route audio to the 3.5mm jack (AUX)"
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

# Music and voice announcements mix automatically through alsa-lib's own
# per-card sysdefault route (core/audio.analog_device()) - there is no
# configuration file to establish here at all, unlike the hand-written
# /etc/asound.conf this project used to generate at this exact point.
# See CLAUDE.md's audio-mixing redesign section for why.

# ---------------------------------------------------------------- 7. Resources
c "STEP 7/9  Reduce resource usage"
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

# ---------------------------------------------------------------- 8. Tailscale
c "STEP 8/9  Install Tailscale (for secure remote access)"
# There is no dietpi-software ID for it, so this uses Tailscale's own
# install script, which detects the distro and adds its apt repo + signing
# key before installing the package - the officially documented method
# (https://tailscale.com/docs/install/linux). Only the package + tailscaled
# go in here; actually joining a tailnet needs a human to open a login URL
# in a browser and authenticate, so that step lives in setup.sh (H7), not
# here - this script only does things a script can decide on its own.
# No reboot is needed for this step.
if command -v tailscale >/dev/null; then
  ok "Tailscale already installed; skipped"
else
  if curl -fsSL https://tailscale.com/install.sh | sh >/dev/null 2>&1; then
    ok "Tailscale installed (not yet connected to a tailnet - see setup.sh H7)"
  else
    w "Tailscale install failed; install it manually later:"
    w "  curl -fsSL https://tailscale.com/install.sh | sh"
  fi
fi

# ---------------------------------------------------------------- 9. Obsidian sync
c "STEP 9/9  Switch Obsidian sync: uninstall Syncthing, install Lockstep Sync"
# Syncthing (CLAUDE.md #40/#41/#45) is replaced by Lockstep Sync, a small
# self-hosted sync server for the Obsidian community plugin of the same
# name (CLAUDE.md #61). Uninstall Syncthing first - install.sh's storage
# step for it (the external-drive bind mount) is removed together with
# this, so nothing should be left trying to hold that mount open.
if [[ -x /opt/syncthing/syncthing ]] || command -v syncthing >/dev/null 2>&1; then
  systemctl disable --now syncthing >/dev/null 2>&1 || true
  # `</dev/null` + `timeout`: dietpi-software's non-interactive `uninstall`
  # is documented (`dietpi-software uninstall <id>...`), but whether some
  # version of it still shows a confirmation prompt before actually
  # removing software is not something this repo can verify offline. A
  # real run got stuck at this exact line with nothing printed - the
  # signature of a prompt whose text went to the `>/dev/null` above while
  # it waited on stdin. Closing stdin makes any such prompt fail closed
  # instead of hanging, and the timeout is the second line of defense
  # in case something else stalls (e.g. a network call). **Do not remove
  # either of these** - bootstrap.sh must never block indefinitely on an
  # external command's own interactive habits.
  if timeout 120 "$DS" uninstall 50 </dev/null >/dev/null 2>&1; then
    ok "Syncthing uninstalled (superseded by Lockstep Sync)"
  else
    w "Syncthing uninstall via dietpi-software failed or timed out; remove it manually if it lingers:"
    w "  sudo dietpi-software uninstall 50"
  fi
else
  ok "Syncthing not installed; nothing to remove"
fi

# There is no dietpi-software ID for Lockstep Sync, so its server binary is
# fetched straight from its GitHub releases - the same "official
# script/binary, guarded by an architecture check" approach as Tailscale
# above. Upstream ships amd64 and arm64 builds only (no armv7/armhf) -
# CLAUDE.md #61 has the reasoning for why that is fine on this project's
# target hardware, and what to check if a given Pi turns out not to
# qualify (32-bit DietPi).
case "$(uname -m)" in
  aarch64|arm64) LS_ARCH=arm64 ;;
  x86_64|amd64)  LS_ARCH=amd64 ;;
  *)             LS_ARCH="" ;;
esac
LS_BIN=/usr/local/bin/lockstep-sync-server
if [[ -z "$LS_ARCH" ]]; then
  w "Lockstep Sync has no server build for $(uname -m) (only amd64/arm64 exist) - skipped."
  w "This Pi needs a 64-bit (ARM64) DietPi image for Obsidian sync; see CLAUDE.md #61."
elif [[ -x "$LS_BIN" ]]; then
  ok "Lockstep Sync server already installed; skipped"
else
  LS_URL="https://github.com/stephansergeev/obsidian-lockstep-sync/releases/latest/download/sync-server-linux-$LS_ARCH"
  # --connect-timeout/--max-time: same "never hang bootstrap.sh forever"
  # reasoning as the timeout on the Syncthing uninstall above - a stalled
  # connection here should not be able to block the rest of the script.
  if curl -fsSL --connect-timeout 10 --max-time 120 "$LS_URL" -o "$LS_BIN.tmp" \
       && chmod +x "$LS_BIN.tmp" && mv "$LS_BIN.tmp" "$LS_BIN"; then
    ok "Lockstep Sync server installed to $LS_BIN"
  else
    rm -f "$LS_BIN.tmp"
    w "Lockstep Sync server download failed; retry later with:"
    w "  curl -fsSL $LS_URL -o $LS_BIN && chmod +x $LS_BIN"
  fi
fi

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
