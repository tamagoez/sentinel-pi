#!/usr/bin/env bash
# sentinel-adguard-8083.sh enable [MINUTES] | disable | status
#
# Direct access to AdGuard Home's own web UI on :8083 is blocked by
# design (see CLAUDE.md #5): Sentinel's own dashboard on :8080 is the
# only place AdGuard's query log is used, and nobody is expected to log
# into AdGuard Home itself day to day. sentinel-guardian.sh re-applies
# that block every 2 minutes no matter what touches iptables in between
# (hostapd, a reboot, ...), so there is no simple "iptables -D ..." that
# stays undone on its own - hence this script instead of a one-off
# command.
#
# enable  writes a Unix timestamp to a marker file that
#         sentinel-guardian.sh's check_firewall() reads: while that time
#         is in the future, Guardian removes the block instead of
#         reapplying it. So "temporary" needs no extra long-running
#         process or timer of its own - the next Guardian cycle (at most
#         2 minutes later, and this script also fixes it up immediately)
#         puts the block back once the marker expires.
# disable clears the marker and re-applies the block immediately, rather
#         than waiting for the next Guardian cycle.
set -uo pipefail

AGH_PORT=8083
STATE_DIR=/run/sentinel-guardian
UNBLOCK_FILE="$STATE_DIR/adguard-8083-unblock-until"
DEFAULT_MINUTES=15

# Same candidate paths as sentinel-guardian.sh's check_adguard_bind() — kept
# in sync deliberately (see below).
if [[ -z "${AGH_YAML:-}" ]]; then
  for cand in /mnt/dietpi_userdata/adguardhome/AdGuardHome.yaml \
              /mnt/dietpi_userdata/AdGuardHome.yaml \
              /opt/AdGuardHome/AdGuardHome.yaml; do
    [[ -f "$cand" ]] && { AGH_YAML="$cand"; break; }
  done
  AGH_YAML="${AGH_YAML:-/mnt/dietpi_userdata/adguardhome/AdGuardHome.yaml}"
fi

ok(){ printf '  [OK] %s\n' "$*"; }
w(){ printf '  [!!] %s\n' "$*" >&2; }
die(){ printf '[FAIL] %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root: sudo $0 $*"
command -v iptables >/dev/null || die "iptables not found"
mkdir -p "$STATE_DIR"

# Opening the firewall alone does nothing: sentinel-guardian.sh's
# check_adguard_bind() permanently locks AdGuard Home's own http.address to
# 127.0.0.1:8083, so nothing is actually listening on an externally
# reachable address even with the port unblocked. rebind_agh() sets it to
# the given host and restarts AdGuard Home so the change takes effect
# immediately, instead of waiting for the next 2-minute Guardian cycle.
# This mirrors check_adguard_bind()'s own awk logic exactly - keep both in
# sync if AdGuardHome.yaml's format ever changes.
rebind_agh() {
  local want_host="$1"
  [[ -f "$AGH_YAML" ]] || { w "AdGuardHome.yaml not found at $AGH_YAML - cannot rebind, only the firewall was changed"; return 0; }
  local changed=0
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
  elif grep -qE '^bind_host:' "$AGH_YAML"; then
    if ! grep -qE "^bind_host:[[:space:]]*${want_host//./\\.}[[:space:]]*\$" "$AGH_YAML"; then
      sed -i -E "s|^bind_host:.*\$|bind_host: $want_host|" "$AGH_YAML"
      changed=1
    fi
  else
    w "Cannot recognize AdGuardHome.yaml format - only the firewall was changed, manual check needed"
    return 0
  fi
  if (( changed )); then
    systemctl restart adguardhome 2>/dev/null || systemctl restart AdGuardHome 2>/dev/null || true
  fi
}

remove_block() {
  for cmd in iptables ip6tables; do
    command -v "$cmd" >/dev/null || continue
    while "$cmd" -C INPUT -p tcp --dport "$AGH_PORT" ! -i lo -j DROP 2>/dev/null; do
      "$cmd" -D INPUT -p tcp --dport "$AGH_PORT" ! -i lo -j DROP 2>/dev/null
    done
    while "$cmd" -C FORWARD -p tcp --dport "$AGH_PORT" -j DROP 2>/dev/null; do
      "$cmd" -D FORWARD -p tcp --dport "$AGH_PORT" -j DROP 2>/dev/null
    done
  done
}

apply_block() {
  for cmd in iptables ip6tables; do
    command -v "$cmd" >/dev/null || continue
    "$cmd" -C INPUT -p tcp --dport "$AGH_PORT" ! -i lo -j DROP 2>/dev/null || \
      "$cmd" -I INPUT 1 -p tcp --dport "$AGH_PORT" ! -i lo -j DROP 2>/dev/null
    "$cmd" -C FORWARD -p tcp --dport "$AGH_PORT" -j DROP 2>/dev/null || \
      "$cmd" -I FORWARD 1 -p tcp --dport "$AGH_PORT" -j DROP 2>/dev/null
  done
}

ip_addr() { hostname -I 2>/dev/null | awk '{print $1}'; }

cmd="${1:-}"
case "$cmd" in
  enable|on)
    minutes="${2:-$DEFAULT_MINUTES}"
    [[ "$minutes" =~ ^[0-9]+$ && "$minutes" -gt 0 ]] || die "MINUTES must be a positive integer (got: $minutes)"
    until=$(( $(date +%s) + minutes * 60 ))
    echo "$until" > "$UNBLOCK_FILE"
    remove_block
    rebind_agh "0.0.0.0"
    ok "Port $AGH_PORT is open for ${minutes} minute(s), until $(date -d "@$until" '+%H:%M:%S' 2>/dev/null || echo "$until")"
    ip=$(ip_addr)
    [[ -n "$ip" ]] && ok "http://$ip:$AGH_PORT/"
    w "sentinel-guardian re-blocks it automatically once that time passes - no need to remember to disable it."
    ;;
  disable|off)
    rm -f "$UNBLOCK_FILE"
    apply_block
    rebind_agh "127.0.0.1"
    ok "Port $AGH_PORT is blocked (the permanent default)"
    ;;
  status)
    until=0
    [[ -f "$UNBLOCK_FILE" ]] && until=$(cat "$UNBLOCK_FILE" 2>/dev/null || echo 0)
    [[ "$until" =~ ^[0-9]+$ ]] || until=0
    now=$(date +%s)
    if (( until > now )); then
      ok "Temporarily open until $(date -d "@$until" '+%H:%M:%S' 2>/dev/null || echo "$until") ($(( (until - now) / 60 )) minute(s) left)"
    else
      ok "Blocked (the permanent default)"
    fi
    if command -v ss >/dev/null; then
      listening=$(ss -Hltn "sport = :$AGH_PORT" 2>/dev/null | awk '{print $4}' | paste -sd, -)
      if [[ -n "$listening" ]]; then
        ok "AdGuard Home is actually listening on: $listening"
      else
        w "Nothing is listening on :$AGH_PORT at all (is adguardhome running?)"
      fi
    fi
    ;;
  *)
    echo "Usage: sudo $0 enable [MINUTES] | disable | status" >&2
    echo "  enable [MINUTES]  open :$AGH_PORT temporarily (default: ${DEFAULT_MINUTES} minutes)" >&2
    echo "  disable           block :$AGH_PORT immediately (the permanent default)" >&2
    echo "  status            show the current state" >&2
    exit 1
    ;;
esac
