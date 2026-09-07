#!/usr/bin/env bash
# Sentinel Guardian — reconcile actual state against the desired state
#
# Why this exists:
#   Applying a setting once is not enough, because:
#     - iptables rules live only in kernel memory and vanish on reboot
#     - WiFi Hotspot (hostapd/dnsmasq) may touch iptables afterwards
#     - AdGuard Home's own auto-update can overwrite AdGuardHome.yaml
#     - ALSA mixer settings can reset on kernel updates
#   So this runs from a systemd timer every 2 minutes and fixes drift.
#
# Idempotent and quiet — only logs when it actually fixes something.
# (journalctl -t sentinel-guardian, or via the Web UI "Errors" tab /
#  diagnostics bundle, which includes a journalctl excerpt.)
set -uo pipefail

# DietPi installs AdGuard Home under /mnt/dietpi_userdata/adguardhome/.
# The older, flat path is kept as a fallback for hand-made installs.
if [[ -z "${AGH_YAML:-}" ]]; then
  for cand in /mnt/dietpi_userdata/adguardhome/AdGuardHome.yaml \
              /mnt/dietpi_userdata/AdGuardHome.yaml \
              /opt/AdGuardHome/AdGuardHome.yaml; do
    [[ -f "$cand" ]] && { AGH_YAML="$cand"; break; }
  done
  AGH_YAML="${AGH_YAML:-/mnt/dietpi_userdata/adguardhome/AdGuardHome.yaml}"
fi
AGH_PORT=8083
LOG_TAG=sentinel-guardian
STATE_DIR=/run/sentinel-guardian
mkdir -p "$STATE_DIR"

FIXED=0
say(){ logger -t "$LOG_TAG" -p daemon.notice -- "$*"; echo "$*"; }
warn(){ logger -t "$LOG_TAG" -p daemon.warning -- "$*"; echo "$*" >&2; }
fixed(){ FIXED=$((FIXED+1)); say "FIXED: $*"; }

# ------------------------------------------------------------------ 1. AdGuard bind
# Lock the web UI to localhost. This is the primary line of defense
# (it's a config file, so it's durable across reboots on its own).
check_adguard_bind() {
  [[ -f "$AGH_YAML" ]] || return 0
  local changed=0

  # New format: top-level "http:" block contains "address:".
  # awk (not a blind sed) so we only touch that one block — the file also
  # has a "tls: address:" block that must stay untouched.
  if awk '/^http:/{inblk=1;next} /^[^[:space:]]/{inblk=0} inblk && /^[[:space:]]{2}address:[[:space:]]/{found=1} END{exit !found}' "$AGH_YAML"; then
    local cur
    cur=$(awk '/^http:/{inblk=1;next} /^[^[:space:]]/{inblk=0}
               inblk && /^[[:space:]]{2}address:[[:space:]]/{sub(/^[[:space:]]*address:[[:space:]]*/,"");print;exit}' "$AGH_YAML")
    if [[ "$cur" != "127.0.0.1:$AGH_PORT" ]]; then
      awk -v want="127.0.0.1:$AGH_PORT" '
        /^http:/{inblk=1;print;next}
        /^[^[:space:]]/{inblk=0}
        inblk && /^[[:space:]]{2}address:[[:space:]]/{print "  address: " want; next}
        {print}' "$AGH_YAML" > "$AGH_YAML.tmp" && mv "$AGH_YAML.tmp" "$AGH_YAML"
      changed=1
    fi
  # Old format: top-level bind_host / bind_port.
  elif grep -qE '^bind_host:' "$AGH_YAML"; then
    if ! grep -qE '^bind_host:[[:space:]]*127\.0\.0\.1[[:space:]]*$' "$AGH_YAML"; then
      sed -i -E 's|^bind_host:.*$|bind_host: 127.0.0.1|' "$AGH_YAML"
      changed=1
    fi
    if grep -qE '^bind_port:' "$AGH_YAML" && \
       ! grep -qE "^bind_port:[[:space:]]*$AGH_PORT[[:space:]]*$" "$AGH_YAML"; then
      sed -i -E "s|^bind_port:.*$|bind_port: $AGH_PORT|" "$AGH_YAML"
      changed=1
    fi
  else
    warn "Cannot recognize AdGuardHome.yaml format; manual check needed."
    return 0
  fi

  if (( changed )); then
    fixed "reset AdGuard Home web UI to 127.0.0.1:$AGH_PORT"
    systemctl restart adguardhome 2>/dev/null || systemctl restart AdGuardHome 2>/dev/null || true
  fi
}

# ------------------------------------------------------------------ 2. Verify listen
# Don't trust the config alone — check what's actually listening.
check_adguard_listen() {
  command -v ss >/dev/null || return 0
  local bad
  bad=$(ss -Hltn "sport = :$AGH_PORT" 2>/dev/null \
        | awk '{print $4}' | grep -vE '^(127\.0\.0\.1|\[::1\]):' || true)
  if [[ -n "$bad" ]]; then
    warn "port $AGH_PORT is listening on a non-loopback address: $bad"
    check_adguard_bind
  fi
}

# ------------------------------------------------------------------ 3. Firewall
# Second line of defense: covers the config being reverted for any reason.
# Re-checked every run, so anything that clears it (e.g. hostapd) is
# corrected within 2 minutes.
#
# scripts/sentinel-adguard-8083.sh writes $UNBLOCK_FILE with a Unix
# timestamp when an admin wants direct :8083 access for a while (to log
# into AdGuard Home itself, which Sentinel otherwise never needs). While
# that timestamp is still in the future, this function removes the block
# instead of reapplying it - so "temporary" is enforced by this same
# 2-minute cycle expiring it, not by a separate long-running process.
UNBLOCK_FILE="$STATE_DIR/adguard-8083-unblock-until"
check_firewall() {
  command -v iptables >/dev/null || return 0

  local until=0
  [[ -f "$UNBLOCK_FILE" ]] && until=$(cat "$UNBLOCK_FILE" 2>/dev/null || echo 0)
  [[ "$until" =~ ^[0-9]+$ ]] || until=0

  if (( until > $(date +%s) )); then
    local removed=0
    for cmd in iptables ip6tables; do
      command -v "$cmd" >/dev/null || continue
      while "$cmd" -C INPUT -p tcp --dport "$AGH_PORT" ! -i lo -j DROP 2>/dev/null; do
        "$cmd" -D INPUT -p tcp --dport "$AGH_PORT" ! -i lo -j DROP 2>/dev/null && removed=1
      done
      while "$cmd" -C FORWARD -p tcp --dport "$AGH_PORT" -j DROP 2>/dev/null; do
        "$cmd" -D FORWARD -p tcp --dport "$AGH_PORT" -j DROP 2>/dev/null && removed=1
      done
    done
    (( removed )) && fixed "removed the port $AGH_PORT block rules (temporary access until $(date -d "@$until" '+%H:%M:%S' 2>/dev/null || echo "$until"))"
    return 0
  fi

  # Expired or never requested - the enforced default. Also clean up a
  # stale marker file so a later `sentinel-adguard-8083.sh status` doesn't
  # report a bogus "until" time.
  [[ -f "$UNBLOCK_FILE" ]] && rm -f "$UNBLOCK_FILE"

  local applied=0
  for cmd in iptables ip6tables; do
    command -v "$cmd" >/dev/null || continue
    if ! "$cmd" -C INPUT -p tcp --dport "$AGH_PORT" ! -i lo -j DROP 2>/dev/null; then
      "$cmd" -I INPUT 1 -p tcp --dport "$AGH_PORT" ! -i lo -j DROP 2>/dev/null && applied=1
    fi
    # Also block via the forwarding path (hotspot clients -> Pi).
    if ! "$cmd" -C FORWARD -p tcp --dport "$AGH_PORT" -j DROP 2>/dev/null; then
      "$cmd" -I FORWARD 1 -p tcp --dport "$AGH_PORT" -j DROP 2>/dev/null && applied=1
    fi
  done
  (( applied )) && fixed "reapplied the port $AGH_PORT block rules"
  return 0
}

# ------------------------------------------------------------------ 4. Audio
# Pin output to the 3.5mm jack. numid=3 value 1 = headphone jack.
check_audio() {
  command -v amixer >/dev/null || return 0
  local card
  card=$(aplay -l 2>/dev/null | grep -m1 -oE '^card [0-9]+' | awk '{print $2}')
  [[ -n "$card" ]] || return 0
  local cur
  cur=$(amixer -c "$card" cget numid=3 2>/dev/null | grep -m1 -oE ': values=[0-9]+' | grep -oE '[0-9]+$')
  if [[ -n "$cur" && "$cur" != "1" ]]; then
    amixer -c "$card" cset numid=3 1 >/dev/null 2>&1 && \
      fixed "reset audio output to AUX (headphone jack)"
    alsactl store >/dev/null 2>&1 || true
  fi
}

# ------------------------------------------------------------------ 5. Bluetooth
# Keep the adapter powered, discoverable and pairable. bluetoothd restarts
# reset these, so they're re-checked every run.
#
# On the RPi 3B+ the Bluetooth chip hangs off the UART, attached at boot by
# hciuart.service (raspberrypi-sys-mods). That attach can lose the race
# against bluetooth.service starting - bluetoothd then comes up with zero
# controllers, which is exactly "Bluetooth won't connect after a reboot,
# but works after 'sudo systemctl restart bluetooth'" (a known class of
# issue on RPi 3/3B+: https://github.com/MichaIng/DietPi/issues/2390). A
# plain reboot doesn't reliably fix it either, since it's a race, not a
# one-time fault - so this has to be detected and repaired here rather
# than just told to the user as "reboot again".
check_bluetooth() {
  command -v bluetoothctl >/dev/null || return 0
  systemctl is-active --quiet bluetooth || return 0
  local info
  info=$(bluetoothctl show 2>&1)
  if [[ -z "$info" ]] || grep -qi 'no default controller' <<<"$info"; then
    if systemctl list-unit-files hciuart.service &>/dev/null; then
      systemctl restart hciuart.service 2>/dev/null
      sleep 2   # give the UART attach a moment before bluetoothd retries
    fi
    systemctl restart bluetooth.service 2>/dev/null
    fixed "no Bluetooth controller was found; restarted hciuart/bluetoothd"
    return 0
  fi

  if ! grep -qE 'Powered:\s*yes' <<<"$info"; then
    bluetoothctl power on >/dev/null 2>&1 && fixed "powered the Bluetooth adapter back on"
  fi
  if ! grep -qE 'Discoverable:\s*yes' <<<"$info"; then
    bluetoothctl discoverable on >/dev/null 2>&1 && fixed "made Bluetooth discoverable again"
  fi
  if ! grep -qE 'Pairable:\s*yes' <<<"$info"; then
    bluetoothctl pairable on >/dev/null 2>&1 && fixed "made Bluetooth pairable again"
  fi
}

# ------------------------------------------------------------------ 6. Services
check_services() {
  local units=(sentinel.service bluetooth.service)
  { command -v bluealsad >/dev/null || command -v bluealsa >/dev/null; } && \
    units+=(sentinel-bluealsa.service sentinel-bluealsa-aplay.service sentinel-bt-agent.service)
  # hciuart.service (RPi's UART-attached Bluetooth chip) and hostapd.service
  # (WiFi Hotspot) both start very early at boot and can fail outright if
  # the underlying interface/UART isn't ready yet - the same class of race
  # as check_bluetooth()'s "no controller" case above, just surfacing as a
  # plain failed unit instead. Catching that here means the next cycle
  # (45s after boot, then every 2 minutes) retries them automatically
  # instead of the hotspot or Bluetooth staying down until a fresh reboot.
  systemctl list-unit-files hciuart.service &>/dev/null && units+=(hciuart.service)
  systemctl list-unit-files hostapd.service &>/dev/null && units+=(hostapd.service)

  for u in "${units[@]}"; do
    systemctl list-unit-files "$u" &>/dev/null || continue
    systemctl is-enabled --quiet "$u" 2>/dev/null || {
      systemctl enable "$u" >/dev/null 2>&1 && fixed "enabled $u"; }
    systemctl is-active --quiet "$u" || {
      # A unit stuck "failed (start-limit-hit)" ignores a plain start
      # ("start request repeated too quickly"); reset-failed clears that
      # counter and is a harmless no-op otherwise.
      systemctl reset-failed "$u" 2>/dev/null
      systemctl start "$u" >/dev/null 2>&1 && fixed "started $u"; }
  done
}

# ------------------------------------------------------------------ 6b. BlueALSA freshness
# bluealsad/bluealsa-aplay hold a D-Bus connection to bluetoothd. If
# bluetooth.service restarts for any reason - check_bluetooth() above,
# install.sh re-run, an apt upgrade, an OOM kill - that connection goes
# stale: systemd still reports the BlueALSA units as "active" (the process
# didn't crash, it's just talking to a socket nobody answers any more), so
# check_services() never touches them, and Bluetooth audio silently stops
# working until something restarts them by hand. Comparing "when did each
# unit last become active" catches this in every case, not just the one
# check_bluetooth() just fixed - a plain is-active check can't tell a live
# connection from a stale one.
check_bluealsa_freshness() {
  systemctl is-active --quiet bluetooth || return 0
  local bt_start bt_epoch
  bt_start=$(systemctl show -p ActiveEnterTimestamp --value bluetooth.service 2>/dev/null)
  [[ -n "$bt_start" && "$bt_start" != "n/a" ]] || return 0
  bt_epoch=$(date -d "$bt_start" +%s 2>/dev/null) || return 0

  local u u_start u_epoch
  for u in sentinel-bluealsa.service sentinel-bluealsa-aplay.service sentinel-bt-agent.service; do
    systemctl list-unit-files "$u" &>/dev/null || continue
    systemctl is-active --quiet "$u" || continue     # check_services() 側で扱う
    u_start=$(systemctl show -p ActiveEnterTimestamp --value "$u" 2>/dev/null)
    [[ -n "$u_start" && "$u_start" != "n/a" ]] || continue
    u_epoch=$(date -d "$u_start" +%s 2>/dev/null) || continue
    if (( bt_epoch > u_epoch )); then
      systemctl restart "$u" 2>/dev/null && \
        fixed "restarted $u (bluetoothd restarted after it did; its D-Bus connection had gone stale)"
    fi
  done
}

# ------------------------------------------------------------------ 7. Hotspot DNS
# Point hotspot clients at AdGuard for DNS.
check_hotspot_dns() {
  local conf=/etc/dnsmasq.d/dietpi-wifi_hotspot.conf
  local dhcpd=/etc/dhcp/dhcpd.conf
  local gw=192.168.42.1
  if [[ -f "$dhcpd" ]] && grep -q 'option domain-name-servers' "$dhcpd"; then
    if ! grep -qE "option domain-name-servers\s+$gw\s*;" "$dhcpd"; then
      sed -i -E "s|option domain-name-servers.*;|option domain-name-servers $gw;|" "$dhcpd"
      fixed "pointed hotspot DNS at $gw (AdGuard)"
      systemctl restart isc-dhcp-server 2>/dev/null || true
    fi
  fi
  if [[ -f "$conf" ]] && ! grep -qE "^dhcp-option=6,$gw" "$conf"; then
    echo "dhcp-option=6,$gw" >> "$conf"
    fixed "set dnsmasq to hand out $gw as DNS"
    systemctl restart dnsmasq 2>/dev/null || true
  fi
}

# ------------------------------------------------------------------ 8. Storage ownership
# A drive re-mounted by hand (dietpi-drive_manager run again, a swapped
# card, a reboot before the fstab fix below took effect) can silently
# revert to being unwritable by the sentinel user - especially on
# exFAT/NTFS, which have no real Unix ownership of their own and need
# uid=/gid= mount options rather than chown. sentinel-fix-storage-owner.sh
# does nothing (no output, cheap) once this is already fine; it only
# escalates - rewriting /etc/fstab and remounting, or falling back to
# chown - when the quick write-test below actually fails.
check_storage_owner() {
  local data="${SENTINEL_DATA:-/mnt/VIDEOSD/sentinel}"
  local mnt="${SENTINEL_STORAGE:-/mnt/VIDEOSD}"
  local script="/opt/sentinel/scripts/sentinel-fix-storage-owner.sh"
  [[ -x "$script" ]] || return 0
  local out
  if out=$("$script" "$data" "$mnt" sentinel 2>&1); then
    [[ -n "$out" ]] && fixed "$out"
  else
    warn "sentinel still cannot write to $data: $out"
  fi
}

# ------------------------------------------------------------------ 9. Storage space
# Full storage would stall every feature, so warn early and keep one
# diagnostics bundle on record for later investigation.
check_storage() {
  local data="${SENTINEL_DATA:-/mnt/VIDEOSD/sentinel}"
  [[ -d "$data" ]] || return 0
  local used
  used=$(df --output=pcent "$data" 2>/dev/null | tail -1 | tr -dc '0-9')
  [[ -n "$used" ]] || return 0
  if (( used >= 92 )); then
    warn "storage usage is ${used}%; consider pruning old recordings"
    local marker="$STATE_DIR/storage-diag-done"
    if [[ ! -f "$marker" ]] && command -v sentinel-diagnose >/dev/null; then
      sentinel-diagnose "$data/diagnostics" >/dev/null 2>&1 && touch "$marker"
    fi
  else
    rm -f "$STATE_DIR/storage-diag-done"
  fi
}

# ------------------------------------------------------------------ 10. yt-dlp
# Try an update once a week; site changes break extraction otherwise.
check_ytdlp() {
  local stamp="$STATE_DIR/ytdlp-updated"
  local persist=/var/lib/sentinel/ytdlp-updated
  mkdir -p /var/lib/sentinel
  [[ -f "$persist" ]] && cp -f "$persist" "$stamp" 2>/dev/null
  if [[ -f "$stamp" ]] && (( $(date +%s) - $(stat -c %Y "$stamp") < 604800 )); then
    return 0
  fi
  command -v yt-dlp >/dev/null || return 0
  timeout 300 yt-dlp -U >/dev/null 2>&1 && say "updated yt-dlp"
  touch "$persist"
}

# ------------------------------------------------------------------ run
check_adguard_bind
check_adguard_listen
check_firewall
check_audio
check_bluetooth
check_services
check_bluealsa_freshness
check_hotspot_dns
check_storage_owner
check_storage
check_ytdlp

(( FIXED )) && say "reconcile complete: fixed $FIXED item(s)"
exit 0
