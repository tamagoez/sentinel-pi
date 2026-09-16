#!/usr/bin/env bash
# Sentinel setup - semi-automated installation on DietPi, run from a git clone.
#
#   sudo ./setup.sh            # run / resume the guided installation
#   sudo ./setup.sh --update   # alias for: sudo ./update.sh
#   sudo ./setup.sh --reset    # forget the saved progress and start over
#
# "Semi-automated" means: everything a script can decide, the script does,
# and everything that needs a human stops and asks. The manual steps are:
#
#   H1  confirm the network address of this Pi (static address recommended)
#   H2  choose the WiFi hotspot SSID / passphrase (or skip the hotspot)
#   H3  reboot after the prerequisites (Bluetooth and audio need it)
#   H4  mount the external drive on /mnt/VIDEOSD (dietpi-drive_manager)
#   H5  log in to AdGuard Home once, while it is still reachable directly
#   H6  set the Web UI password / Discord webhook / AdGuard password
#   H7  connect to Tailscale for secure remote access (optional)
#   H8  pair devices with Lockstep Sync for Obsidian sync (optional)
#
# Progress is kept in /var/lib/sentinel/setup-stage, so after the reboot in
# H3 you run the same command again and it continues where it left off.
#
# All output here is English on purpose: a physical HDMI console or a plain
# serial terminal cannot render Japanese glyphs. See CLAUDE.md.
set -uo pipefail

STATE_DIR=/var/lib/sentinel
STAGE_FILE="$STATE_DIR/setup-stage"
STORAGE="${SENTINEL_STORAGE:-/mnt/VIDEOSD}"
DIETPI_TXT=/boot/dietpi.txt

# ------------------------------------------------------------------ output
c()    { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
human(){ printf '\n\033[1;33m-- MANUAL STEP: %s --\033[0m\n' "$*"; }
ok()   { printf '  [OK] %s\n' "$*"; }
w()    { printf '  [!!] %s\n' "$*"; }
die()  { printf '\n[FAIL] %s\n' "$*" >&2; exit 1; }

ask_yn() { # ask_yn "question" "Y"|"N"  ->  exit status 0 means yes
  local q="$1" def="${2:-Y}" a
  if [[ ! -t 0 ]]; then
    printf '  %s [%s] (no terminal attached; using the default)\n' "$q" "$def"
    [[ "$def" == [yY] ]]
    return
  fi
  local hint='[Y/n]'; [[ "$def" == [yY] ]] || hint='[y/N]'
  read -rp "  $q $hint: " a
  a="${a:-$def}"
  [[ "$a" == [yY] ]]
}

pause() { # wait while the operator does something outside this script
  [[ -t 0 ]] || { w "No terminal attached; not waiting."; return; }
  read -rp "  Press ENTER when done: " _
}

stage_get() { cat "$STAGE_FILE" 2>/dev/null || echo 0; }
stage_set() { mkdir -p "$STATE_DIR"; echo "$1" > "$STAGE_FILE"; }

my_ip() { hostname -I 2>/dev/null | awk '{print $1}'; }

# ------------------------------------------------------------------ locate
# Same robust resolution as install.sh: a clear message instead of a raw
# "cp: cannot stat" when the clone is incomplete or we are one level in.
RAW_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$RAW_DIR/sentinel/main.py" ]]; then
  SRC="$RAW_DIR"
elif [[ -f "$RAW_DIR/main.py" && -f "$RAW_DIR/../setup.sh" ]]; then
  SRC="$(cd "$RAW_DIR/.." && pwd)"
else
  die "Cannot find sentinel/main.py under $RAW_DIR.
       Run this from the top of the git clone:
         git clone https://github.com/tamagoez/sentinel-pi.git ~/sentinel-pi
         cd ~/sentinel-pi && sudo ./setup.sh"
fi
for f in bootstrap.sh install.sh; do
  [[ -f "$SRC/$f" ]] || die "$f is missing from the clone at $SRC."
done
chmod +x "$SRC"/*.sh "$SRC"/scripts/*.sh 2>/dev/null || true

# ------------------------------------------------------------------ args
MODE=setup
for arg in "$@"; do
  case "$arg" in
    --update) MODE=update;;
    --reset)  MODE=reset;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 0;;
    *) die "Unknown option: $arg (try --help)";;
  esac
done

[[ $EUID -eq 0 ]] || die "Run as root: sudo ./setup.sh"

if [[ "$MODE" == reset ]]; then
  rm -f "$STAGE_FILE"
  ok "Progress cleared. Run 'sudo ./setup.sh' to start over."
  exit 0
fi

if [[ "$MODE" == update ]]; then
  [[ -f "$SRC/update.sh" ]] || die "update.sh is missing from $SRC."
  exec "$SRC/update.sh"
fi

# DietPi's own commands normally resolve via ~/.bashrc on an interactive
# login shell, but a non-login "sudo ./setup.sh" invocation can start with
# a stripped-down PATH that doesn't include them - which then looks
# exactly like "this isn't DietPi" even though it is. Widen PATH first, so
# bootstrap.sh and dietpi-drive_manager (H4) resolve the same way here.
export PATH="/boot/dietpi:/usr/local/bin:$PATH"

command -v dietpi-software >/dev/null || [[ -x /boot/dietpi/dietpi-software ]] || [[ -d /boot/dietpi ]] || \
  die "This script targets DietPi (/boot/dietpi not found)."

STAGE=$(stage_get)

#####################################################################
# PHASE 1 - prerequisites (everything before the reboot)
#####################################################################
if (( STAGE < 1 )); then

c "PHASE 1/2  Prerequisites"
cat <<'EOS'
  This phase installs, through DietPi:
    ALSA (5), FFmpeg (7), Git (17), Python 3 (130), yt-dlp (195),
    AdGuard Home (126) + Unbound (182), optionally WiFi Hotspot (60),
    and Bluetooth, mpg123, v4l-utils and OpenCV from APT.
  It also forces an English UTF-8 locale, routes audio to the 3.5mm jack
  and disables SWAP. A reboot is required at the end of this phase.
EOS
ask_yn "Continue?" Y || { ok "Nothing was changed."; exit 0; }

# ---- H1: network --------------------------------------------------
human "H1  Confirm this Pi's network address"
IP=$(my_ip)
echo "     Current IPv4 address : ${IP:-<none>}"
if grep -qE '^[[:blank:]]*AUTO_SETUP_NET_USESTATIC=1' "$DIETPI_TXT" 2>/dev/null; then
  ok "dietpi.txt asks for a static address"
else
  w "dietpi.txt has AUTO_SETUP_NET_USESTATIC=0, so this address came from DHCP."
  w "A changing address breaks your bookmarks and the DNS target you hand out."
  w "Fix it with 'dietpi-config' -> 7: Network Options: Adapters, or reserve"
  w "the address on your router. Not fatal - you may continue either way."
fi
ask_yn "Is the address above the one you want to keep?" Y || \
  die "Set the address first (dietpi-config), then run 'sudo ./setup.sh' again."

# ---- H2: hotspot --------------------------------------------------
human "H2  WiFi hotspot"
echo "     The hotspot lets phones reach Sentinel without your home router."
echo "     On the Pi 3B+, WiFi and Bluetooth share one chip, so running the"
echo "     hotspot can cause occasional Bluetooth audio dropouts."
if ask_yn "Install the WiFi hotspot?" Y; then
  export SKIP_HOTSPOT=0
  cur_ssid=$(sed -n '/^[[:blank:]]*SOFTWARE_WIFI_HOTSPOT_SSID=/{s/^[^=]*=//p;q}' "$DIETPI_TXT" 2>/dev/null)
  echo "     DietPi reads the SSID and passphrase from $DIETPI_TXT."
  echo "     Current SSID: ${cur_ssid:-DietPi-HotSpot}"
  if [[ -t 0 ]] && ask_yn "Set the SSID and passphrase now?" Y; then
    read -rp "  SSID [${cur_ssid:-Sentinel}]: " new_ssid
    new_ssid="${new_ssid:-${cur_ssid:-Sentinel}}"
    while :; do
      read -rsp "  Passphrase (8-63 characters): " new_key; echo
      (( ${#new_key} >= 8 && ${#new_key} <= 63 )) && break
      w "WPA2 requires 8 to 63 characters."
    done
    if grep -q '^[[:blank:]]*SOFTWARE_WIFI_HOTSPOT_SSID=' "$DIETPI_TXT"; then
      sed -i "s|^[[:blank:]]*SOFTWARE_WIFI_HOTSPOT_SSID=.*|SOFTWARE_WIFI_HOTSPOT_SSID=$new_ssid|" "$DIETPI_TXT"
      sed -i "s|^[[:blank:]]*SOFTWARE_WIFI_HOTSPOT_KEY=.*|SOFTWARE_WIFI_HOTSPOT_KEY=$new_key|" "$DIETPI_TXT"
    else
      printf 'SOFTWARE_WIFI_HOTSPOT_SSID=%s\nSOFTWARE_WIFI_HOTSPOT_KEY=%s\n' \
        "$new_ssid" "$new_key" >> "$DIETPI_TXT"
    fi
    unset new_key
    ok "Hotspot SSID set to '$new_ssid'"
    w "The passphrase is stored in clear text in $DIETPI_TXT (root-only file)."
  else
    w "Keeping the current values. DietPi's default passphrase is"
    w "'dietpihotspot' - change it later via 'dietpi-config' if it still applies."
  fi
else
  export SKIP_HOTSPOT=1
  ok "Hotspot skipped."
fi

# ---- automatic ----------------------------------------------------
c "Running bootstrap.sh (several minutes)"
"$SRC/bootstrap.sh" || \
  die "bootstrap.sh failed. Fix the reported problem and re-run 'sudo ./setup.sh'."
stage_set 1

# ---- H3: reboot ---------------------------------------------------
human "H3  Reboot"
echo "     Bluetooth, the audio route and the SWAP change only take effect"
echo "     after a restart. Nothing has been installed to /opt yet."
echo "     After the reboot, log in again and run:"
echo
echo "         cd $SRC && sudo ./setup.sh"
echo
if ask_yn "Reboot now?" Y; then
  ok "Rebooting."
  sync; reboot
  exit 0
fi
w "Reboot skipped. Phase 2 will ask for it again before it does anything."
exit 0

fi

#####################################################################
# PHASE 2 - storage, AdGuard check, install
#####################################################################
if (( STAGE >= 2 )); then
  c "Setup already finished"
  echo "  Update (pulls + redeploys): sudo ./update.sh"
  echo "  Start over from scratch   : sudo ./setup.sh --reset"
  exit 0
fi

c "PHASE 2/2  Storage, AdGuard Home and the Sentinel service"

# Guard against running phase 2 without the reboot phase 1 asked for:
# if the machine has been up longer than the stage file is old, it has not
# rebooted since phase 1 finished.
BOOT_S=$(awk '{print int($1)}' /proc/uptime 2>/dev/null || echo 0)
STAGE_AGE=$(( $(date +%s) - $(stat -c %Y "$STAGE_FILE" 2>/dev/null || echo 0) ))
if (( BOOT_S > STAGE_AGE )); then
  w "This machine does not look like it has rebooted since phase 1."
  if ask_yn "Reboot now (recommended)?" Y; then sync; reboot; exit 0; fi
  w "Continuing without it; Bluetooth audio may not work until you restart."
fi

# ---- H4: storage --------------------------------------------------
human "H4  Mount the external drive on $STORAGE"
if mountpoint -q "$STORAGE"; then
  ok "$STORAGE is already mounted ($(findmnt -no SOURCE,FSTYPE "$STORAGE" 2>/dev/null))"
else
  echo "     Recordings, music and the daily videos live on the external drive."
  echo "     dietpi-drive_manager opens next. In it:"
  echo "       1. select your USB drive"
  echo "       2. mount it, and set the mount point to exactly:  $STORAGE"
  echo "       3. confirm it is added to /etc/fstab (mounts again on boot)"
  echo "       4. leave the tool with 'Exit'"
  if ask_yn "Open dietpi-drive_manager now?" Y; then
    dietpi-drive_manager || w "dietpi-drive_manager exited with an error."
  fi
  if mountpoint -q "$STORAGE"; then
    ok "$STORAGE is mounted now"
  else
    w "$STORAGE is still not a mount point."
    w "Sentinel can run without it, but everything is then written to the SD"
    w "card, which wears it out and is far smaller."
    ask_yn "Continue anyway (data on the SD card)?" N || \
      die "Mount the drive, then run 'sudo ./setup.sh' again."
  fi
fi

# ---- H5: AdGuard Home ---------------------------------------------
human "H5  Log in to AdGuard Home once, now"
IP=$(my_ip)
AGH_YAML=$(ls /mnt/dietpi_userdata/adguardhome/AdGuardHome.yaml \
              /mnt/dietpi_userdata/AdGuardHome.yaml 2>/dev/null | head -1 || true)
if [[ -z "$AGH_YAML" ]]; then
  w "AdGuardHome.yaml not found, so AdGuard Home is probably not installed."
  w "Install it with 'dietpi-software install 126', then re-run this script."
  w "Without it, Sentinel's network log stays empty; everything else works."
  pause
else
  echo "     DietPi pre-configures AdGuard Home, so there is no setup wizard:"
  echo "       URL      http://${IP:-<this-Pi-IP>}:8083"
  echo "       User     admin"
  echo "       Password your DietPi global software password ('dietpi' unless"
  echo "                you changed it during the DietPi first-run setup)"
  echo
  echo "     Open it now and check that:"
  echo "       - you can log in (change the password here if you want to)"
  echo "       - Settings -> General settings -> query logging is enabled;"
  echo "         Sentinel's network log reads exactly that query log"
  echo
  echo "     Once this script finishes, port 8083 is blocked from the LAN/"
  echo "     hotspot; run 'sudo sentinel-adguard-8083 enable [MINUTES]' to"
  echo "     open it again for a while."
  pause
fi

# ---- automatic ----------------------------------------------------
c "Running install.sh"
"$SRC/install.sh" || \
  die "install.sh failed. Read the message above, then re-run 'sudo ./setup.sh'."
stage_set 2

# ---- H6: application settings -------------------------------------
IP=$(my_ip)
human "H6  Finish the configuration in the Web UI"
cat <<EOS
     Open  http://${IP:-<this-Pi-IP>}:8080  and set, in the settings tab:

       1. Web UI password       - empty by default. Set it: the web terminal
                                  is a shell for anyone who reaches port 8080.
       2. Discord webhook URL   - motion and status notifications.
       3. AdGuard Home password - the one from H5; needed to read the query log.

     Verify:

       systemctl status sentinel
       journalctl -u sentinel -f
       journalctl -t sentinel-guardian --since "10 min ago"   # should be quiet
       ss -ltn 'sport = :8083'                                # 127.0.0.1 only
       ls -l /dev/v4l/by-id/                                  # cameras
       vcgencmd measure_temp && vcgencmd get_throttled        # >60C is normal
       sentinel-diagnose                                      # full bundle

     Later updates:

       cd $SRC && sudo ./update.sh

EOS

# ---- H7: Tailscale (optional) --------------------------------------
human "H7  Connect to Tailscale for secure remote access (optional)"
if ! command -v tailscale >/dev/null; then
  w "tailscale is not installed (bootstrap.sh should have installed it)."
  w "Install it with: curl -fsSL https://tailscale.com/install.sh | sh"
  w "then run: sudo tailscale up --accept-dns=false"
elif tailscale status >/dev/null 2>&1 && ! tailscale status 2>/dev/null | grep -q '^Logged out'; then
  ok "Already connected to a Tailscale network."
else
  cat <<'EOS'
     Tailscale gives this Pi a private, encrypted address reachable from
     anywhere, without opening any port on your router or running a
     public HTTPS reverse proxy - the encryption is WireGuard, handled by
     Tailscale itself, on top of the LAN-only design everything else here
     assumes (see CLAUDE.md, "Deliberate omissions").

     --accept-dns=false keeps this Pi resolving DNS through AdGuard Home
     (127.0.0.1) instead of switching to Tailscale's own resolver -
     otherwise Sentinel's network log, which reads straight from AdGuard's
     query log, would go quiet for anything this Pi itself looks up.
     See CLAUDE.md #39 if you ever run 'tailscale up' again by hand: this
     flag is not remembered between runs, so it needs repeating each time.
EOS
  if ask_yn "Connect to Tailscale now?" Y; then
    echo "     Starting the login flow - open the URL it prints, on any device,"
    echo "     to authenticate. This command waits until you do (or times out)."
    if tailscale up --accept-dns=false; then
      ok "Connected to Tailscale."
    else
      w "tailscale up did not complete. Run it again any time:"
      w "  sudo tailscale up --accept-dns=false"
    fi
  else
    w "Skipped. Connect later with: sudo tailscale up --accept-dns=false"
  fi
fi

# ---- H8: Lockstep Sync (optional) ------------------------------------
human "H8  Pair devices with Lockstep Sync for Obsidian sync (optional)"
LS_BIN=/usr/local/bin/lockstep-sync-server
LS_DATA="$STORAGE/lockstep-sync"
if [[ ! -x "$LS_BIN" ]]; then
  w "Lockstep Sync server is not installed. bootstrap.sh only ships a server"
  w "build for amd64/arm64 - check with: uname -m (see CLAUDE.md #61)"
elif ! systemctl is-active --quiet sentinel-lockstep-sync 2>/dev/null; then
  w "sentinel-lockstep-sync.service is not running; install.sh should have"
  w "started it. Check: systemctl status sentinel-lockstep-sync"
else
  IP=$(my_ip)
  TS_IP=""
  command -v tailscale >/dev/null && TS_IP=$(tailscale ip -4 2>/dev/null | head -1)
  cat <<EOS
     Lockstep Sync keeps no plaintext copy of your notes on this Pi - it
     only relays end-to-end encrypted data between your devices. Pairing
     works from the command line, one device at a time, using a
     short-lived link/QR code. Each pairing bakes in the address that
     device will use, so pick per device: the LAN address below for one
     that never leaves home, the Tailscale address (if shown) for one
     that needs to sync from away too.

     IMPORTANT - install the Lockstep Sync plugin in Obsidian on the
     device FIRST, before opening the link below. The link opens a page
     on this Pi that hands off to Obsidian through an "obsidian://"
     link; if the plugin is not installed and enabled yet on that device,
     there is nothing to receive the handoff, the page cannot fill in
     that device's Server URL / Device token for you, and the link gets
     shown as "expired or already used" the next time you try it (each
     link is single-use once the handoff is attempted, and old links do
     not come back if you generate a new one). There is no field in the
     plugin to paste this link into - the browser page IS the pairing
     step, you do not type anything into the plugin yourself.

       1. First device - normally your desktop, with your existing vault.
          Install + enable the Lockstep Sync plugin in Obsidian there,
          then run:

            sudo -u sentinel $LS_BIN link --data $LS_DATA \\
              --vault main --name desktop --url http://${IP:-<this-Pi-IP>}:8384 --minutes 60

          The last line printed is a one-time pairing link, valid for 60
          minutes. Open it in a web browser on that same device (not in
          this terminal, not on a different device) - the page it loads
          hands off to the Obsidian plugin automatically and fills in
          Server URL / Device token for you. If you would rather scan a
          QR code with a phone/tablet that already has the plugin ready,
          use this variant instead:

            sudo -u sentinel $LS_BIN link --data $LS_DATA \\
              --vault main --name desktop --url http://${IP:-<this-Pi-IP>}:8384 --minutes 60 \\
              | tail -1 | xargs qrencode -t ANSIUTF8 -m 2

       2. Each additional device (phone, tablet, another PC) - install +
          enable the plugin there first too, then repeat with a new
          --name, and swap in the Tailscale address below instead of the
          LAN one for a device that needs away-from-home access:

            sudo -u sentinel $LS_BIN link --data $LS_DATA \\
              --vault main --name phone --url http://${IP:-<this-Pi-IP>}:8384 --minutes 60

EOS
  if [[ -n "$TS_IP" ]]; then
    cat <<EOS
     Tailscale address (for a device that also needs "away from home"
     access): http://$TS_IP:8384 - install Tailscale on that device too
     (the same official app used for H7) and join it to this tailnet.
     If this Pi's own Tailscale IP changes later (e.g. after a re-auth),
     re-check it with 'tailscale ip -4' before pairing a new device.

EOS
  else
    cat <<EOS
     The commands above only reach this Pi from your home LAN. For a
     device that also needs "away from home" access, connect this Pi to
     Tailscale (H7 above) and install it on that device too, then
     re-run this step for its tailnet address to appear here.

EOS
  fi
fi

ok "Setup complete."
