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

# scripts/sentinel-adguard-8083.sh writes this with a Unix timestamp when an
# admin wants direct :8083 access for a while. While that timestamp is still
# in the future, AdGuard's web UI needs to actually be reachable from
# outside — binding it to 127.0.0.1 (below) while only the firewall is open
# would leave nothing listening on any externally-reachable address, so the
# "temporary access" would silently do nothing. Both the bind-lock and the
# firewall block (checked later) read this same marker so they agree on
# whether we're inside that window.
UNBLOCK_FILE="$STATE_DIR/adguard-8083-unblock-until"
agh_unblock_active() {
  local until=0
  [[ -f "$UNBLOCK_FILE" ]] && until=$(cat "$UNBLOCK_FILE" 2>/dev/null || echo 0)
  [[ "$until" =~ ^[0-9]+$ ]] || until=0
  (( until > $(date +%s) ))
}

# ------------------------------------------------------------------ 1. AdGuard bind
# Lock the web UI to localhost — except during a sentinel-adguard-8083.sh
# enable window, when it must bind to 0.0.0.0 instead so the port being
# opened in the firewall (below) actually reaches a listening socket.
# This is the primary line of defense the rest of the time (it's a config
# file, so it's durable across reboots on its own).
check_adguard_bind() {
  [[ -f "$AGH_YAML" ]] || return 0
  local changed=0
  local want_host="127.0.0.1"
  agh_unblock_active && want_host="0.0.0.0"

  # New format: top-level "http:" block contains "address:".
  # awk (not a blind sed) so we only touch that one block — the file also
  # has a "tls: address:" block that must stay untouched.
  if awk '/^http:/{inblk=1;next} /^[^[:space:]]/{inblk=0} inblk && /^[[:space:]]{2}address:[[:space:]]/{found=1} END{exit !found}' "$AGH_YAML"; then
    local cur
    cur=$(awk '/^http:/{inblk=1;next} /^[^[:space:]]/{inblk=0}
               inblk && /^[[:space:]]{2}address:[[:space:]]/{sub(/^[[:space:]]*address:[[:space:]]*/,"");print;exit}' "$AGH_YAML")
    if [[ "$cur" != "$want_host:$AGH_PORT" ]]; then
      awk -v want="$want_host:$AGH_PORT" '
        /^http:/{inblk=1;print;next}
        /^[^[:space:]]/{inblk=0}
        inblk && /^[[:space:]]{2}address:[[:space:]]/{print "  address: " want; next}
        {print}' "$AGH_YAML" > "$AGH_YAML.tmp" && mv "$AGH_YAML.tmp" "$AGH_YAML"
      changed=1
    fi
  # Old format: top-level bind_host / bind_port.
  elif grep -qE '^bind_host:' "$AGH_YAML"; then
    if ! grep -qE "^bind_host:[[:space:]]*${want_host//./\\.}[[:space:]]*\$" "$AGH_YAML"; then
      sed -i -E "s|^bind_host:.*\$|bind_host: $want_host|" "$AGH_YAML"
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
    fixed "reset AdGuard Home web UI to $want_host:$AGH_PORT"
    systemctl restart adguardhome 2>/dev/null || systemctl restart AdGuardHome 2>/dev/null || true
  fi
}

# ------------------------------------------------------------------ 2. Verify listen
# Don't trust the config alone — check what's actually listening. Skipped
# during an active unblock window since 0.0.0.0 is then the intended state,
# not something to warn about and revert.
check_adguard_listen() {
  command -v ss >/dev/null || return 0
  agh_unblock_active && return 0
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
# $UNBLOCK_FILE (see agh_unblock_active() above) is written by
# scripts/sentinel-adguard-8083.sh when an admin wants direct :8083 access
# for a while (to log into AdGuard Home itself, which Sentinel otherwise
# never needs). While that timestamp is still in the future, this function
# removes the block instead of reapplying it - so "temporary" is enforced
# by this same 2-minute cycle expiring it, not by a separate long-running
# process.
check_firewall() {
  command -v iptables >/dev/null || return 0

  if agh_unblock_active; then
    local until
    until=$(cat "$UNBLOCK_FILE" 2>/dev/null || echo 0)
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
# The whole analog-output path (shared hardware volume numid=1, routing
# numid=3, and the card /etc/asound.conf actually mixes into) is checked
# by sentinel-fix-audio-output.sh, which also owns the single bash copy of
# find_output_card(). Delegating rather than duplicating: the previous
# version here only ever checked numid=3, so a hardware volume parked at
# zero silenced music indefinitely with nothing reporting an error
# (CLAUDE.md #41).
check_audio() {
  local script=/opt/sentinel/scripts/sentinel-fix-audio-output.sh
  [[ -x "$script" ]] || return 0
  local out rc
  out=$("$script" --quiet 2>&1); rc=$?
  if (( rc == 10 )); then
    while IFS= read -r line; do
      [[ -n "$line" ]] && fixed "${line#*] }"
    done <<<"$out"
  elif (( rc != 0 )) && [[ -n "$out" ]]; then
    warn "audio output check: $(printf '%s' "$out" | tr '\n' ' ')"
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
  systemctl list-unit-files sentinel-lockstep-sync.service &>/dev/null && units+=(sentinel-lockstep-sync.service)

  # hciuart.service is the one unit here whose "inactive" resting state
  # unit_needs_start() cannot reliably tell apart from broken. CLAUDE.md
  # #51 tried modelling it via Type=/Result= - first assuming
  # Type=oneshot, then widening to Type=forking with Result=success - and
  # real hardware kept restarting it on essentially every single 2-minute
  # cycle regardless (`FIXED: started hciuart.service` logged dozens of
  # times per hour, still happening after both attempted fixes were
  # deployed). Modelling this unit's properties has failed twice, so this
  # stops trying: hciuart already has a strictly better supervisor.
  # check_bluetooth() above restarts hciuart *and* bluetooth.service the
  # moment `bluetoothctl show` actually reports no controller - the one
  # symptom that matters - so nothing here needs to re-derive "is hciuart
  # actually broken" from ActiveState/Type/Result at all. For this unit,
  # only a genuine ActiveState=failed (systemd's own unambiguous "this
  # errored out" signal, needed for the CLAUDE.md #12 startup-race case:
  # hciuart failing outright before bluetoothd ever gets to notice) counts
  # as broken here; merely "inactive" does not restart it.
  # **Do not route hciuart back through unit_needs_start()'s inactive
  # handling** - that is exactly the restart storm this replaces, twice
  # confirmed on real hardware.
  local failed_only_units=(hciuart.service)

  for u in "${units[@]}"; do
    systemctl list-unit-files "$u" &>/dev/null || continue
    systemctl is-enabled --quiet "$u" 2>/dev/null || {
      systemctl enable "$u" >/dev/null 2>&1 && fixed "enabled $u"; }
    if [[ " ${failed_only_units[*]} " == *" $u "* ]]; then
      systemctl is-failed --quiet "$u" 2>/dev/null || continue
    else
      unit_needs_start "$u" || continue
    fi
    # A unit stuck "failed (start-limit-hit)" ignores a plain start
    # ("start request repeated too quickly"); reset-failed clears that
    # counter and is a harmless no-op otherwise.
    systemctl reset-failed "$u" 2>/dev/null
    systemctl start "$u" >/dev/null 2>&1 && fixed "started $u"
  done
}

# `systemctl is-active` reports "inactive" for a Type=oneshot unit that ran
# to completion and does not RemainAfterExit - that is its normal resting
# state, not a fault. hciuart.service (the RPi's UART Bluetooth attach) is
# exactly such a unit, and treating "inactive" as broken made Guardian
# restart it on every single cycle: real hardware logged
# "FIXED: started hciuart.service x5" within 9 minutes of uptime, i.e. one
# per cycle, forever. That is not harmless noise - each restart
# re-attaches the Bluetooth UART, which can knock bluetoothd's controller
# out, which check_bluetooth() then "repairs" by restarting
# bluetooth.service, which drags the BlueALSA units with it, which opens
# and closes the bcm2835 ALSA device over and over. That open/close churn
# is precisely what leaves the card in the wedged state behind
# "music is playing but silent" (CLAUDE.md #45).
# **Do not go back to a bare `systemctl is-active` check here** - it puts
# the whole Bluetooth/audio restart cascade back on a 2-minute timer.
unit_needs_start() {
  local u="$1" state type rae result since
  state=$(systemctl show "$u" -p ActiveState --value 2>/dev/null)
  [[ "$state" == "failed" ]] && return 0
  case "$state" in active|activating|reloading|deactivating) return 1 ;; esac
  # state is "inactive" here. Whether that is healthy or broken depends on
  # what the unit is *for*, not just its Type=. This project's first cut
  # at this check only recognised Type=oneshot, on the assumption that
  # hciuart.service (the unit this was written for, CLAUDE.md #47) is one.
  # It is not - Raspberry Pi OS/DietPi ship it as Type=forking (it runs
  # btuart/hciattach once to attach the UART, then settles to "inactive"
  # with nothing left for systemd to track, exactly like a oneshot in
  # every way that matters here, just reported under a different Type=).
  # Checking Type=oneshot alone missed it completely, and real hardware
  # showed the exact consequence: hciuart.service getting restarted on
  # essentially every single 2-minute cycle for the unit's entire uptime
  # (`FIXED: started hciuart.service` logged dozens of times over two
  # hours) - each restart re-attaches the UART, which can cost bluetoothd
  # its controller (CLAUDE.md #12), which check_bluetooth() then "fixes"
  # by restarting bluetooth.service, which drags the BlueALSA units along,
  # churning the bcm2835 ALSA device open/close on every one of those
  # cycles - the exact mechanism CLAUDE.md #45 traces to mpg123 dying with
  # "Deep trouble! Cannot flush to my output anymore!".
  #
  # Type= alone was never trustworthy for this and still is not - so this
  # also checks Result=. A oneshot/forking unit that genuinely crashed
  # reports something other than "success" (exit-code, signal, timeout,
  # watchdog, ...) even though it is just as "inactive" as a healthy one
  # that finished its one job and stopped on purpose. Without this check,
  # widening Type= to include forking would risk treating a real crash of
  # some other forking daemon (hostapd.service ships as Type=forking on
  # some DietPi/distro combinations too) as "fine, leave it".
  # **Do not narrow this back to Type=oneshot only, and do not drop the
  # Result= check** - either one reopens this exact restart storm.
  type=$(systemctl show "$u" -p Type --value 2>/dev/null)
  rae=$(systemctl show "$u" -p RemainAfterExit --value 2>/dev/null)
  result=$(systemctl show "$u" -p Result --value 2>/dev/null)
  if [[ "$rae" != "yes" && ( "$type" == "oneshot" || "$type" == "forking" ) \
        && "$result" == "success" ]]; then
    # Has it ever run? An empty InactiveEnterTimestamp means it never did,
    # so one start is a real repair; anything else means it already did
    # its job and going inactive was the expected ending.
    since=$(systemctl show "$u" -p InactiveEnterTimestamp --value 2>/dev/null)
    [[ -z "$since" ]] && return 0
    return 1
  fi
  return 0
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

# ------------------------------------------------------------------ 8b. Lockstep Sync firewall
# sentinel-lockstep-sync.service (CLAUDE.md #61) binds 0.0.0.0:8384 rather
# than a specific address (the Tailscale interface's own IP is only
# assigned after 'tailscale up' has run and can change if the node is
# re-authed), so reachability is restricted here instead, the same
# DROP-based pattern check_firewall() above already uses for AdGuard's
# :8083 - except this one allows the tailscale0 interface through as well
# as loopback, rather than blocking every non-loopback source outright.
# Re-checked every cycle for the same reason as :8083's rule: iptables
# rules live only in kernel memory and do not survive a reboot on their
# own.
LOCKSTEP_PORT=8384
check_lockstep_firewall() {
  command -v iptables >/dev/null || return 0
  systemctl list-unit-files sentinel-lockstep-sync.service &>/dev/null || return 0

  local applied=0
  for cmd in iptables ip6tables; do
    command -v "$cmd" >/dev/null || continue
    if ! "$cmd" -C INPUT -p tcp --dport "$LOCKSTEP_PORT" ! -i lo ! -i tailscale0 -j DROP 2>/dev/null; then
      "$cmd" -I INPUT 1 -p tcp --dport "$LOCKSTEP_PORT" ! -i lo ! -i tailscale0 -j DROP 2>/dev/null && applied=1
    fi
    # Forwarded traffic (hotspot clients -> Pi) never needs to reach this
    # port - Tailscale traffic terminates locally at the tailscale0
    # interface and is handled by the INPUT rule above, not FORWARD - so
    # this blocks it unconditionally, same as AdGuard's :8083 FORWARD rule.
    if ! "$cmd" -C FORWARD -p tcp --dport "$LOCKSTEP_PORT" -j DROP 2>/dev/null; then
      "$cmd" -I FORWARD 1 -p tcp --dport "$LOCKSTEP_PORT" -j DROP 2>/dev/null && applied=1
    fi
  done
  (( applied )) && fixed "reapplied the port $LOCKSTEP_PORT firewall rules (Tailscale-only)"
  return 0
}

# ------------------------------------------------------------------ 8d. BlueALSA D-Bus name
# bluealsa exits whenever it cannot own org.bluealsa, and systemd restarts
# it forever. The churn tears sentinel-bluealsa-aplay down and up with it,
# and that repeated opening of the ALSA device can leave bcm2835 wedged -
# at which point music fails too, for a reason that looks nothing like
# Bluetooth (CLAUDE.md #45).
check_bluealsa_dbus() {
  local script=/opt/sentinel/scripts/sentinel-fix-bluealsa.sh
  [[ -x "$script" ]] || return 0
  local out rc
  out=$("$script" --quiet 2>&1); rc=$?
  if (( rc == 10 )); then
    while IFS= read -r line; do
      [[ -n "$line" ]] && fixed "${line#*] }"
    done <<<"$out"
  elif (( rc != 0 )) && [[ -n "$out" ]]; then
    warn "bluealsa check: $(printf '%s' "$out" | tr '\n' ' ')"
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
check_lockstep_firewall
check_audio
check_bluetooth
# Before check_services, not after: if the distro's own bluealsa.service is
# holding the org.bluealsa D-Bus name, sentinel-bluealsa.service cannot
# start no matter how many times check_services tries. Real hardware showed
# the wrong order plainly - "started sentinel-bluealsa.service" at 14:20:23,
# then "disabled bluealsa.service - it was holding org.bluealsa" ten seconds
# later, i.e. the start was doomed before it was attempted. Clearing the
# conflict first means check_services starts a unit that can actually run,
# and saves a round of BlueALSA restarts (which churn the ALSA device -
# CLAUDE.md #45).
check_bluealsa_dbus
check_services
check_bluealsa_freshness
check_hotspot_dns
check_storage_owner
check_storage
check_ytdlp

(( FIXED )) && say "reconcile complete: fixed $FIXED item(s)"
exit 0
