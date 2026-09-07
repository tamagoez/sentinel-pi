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

ok(){ printf '  [OK] %s\n' "$*"; }
w(){ printf '  [!!] %s\n' "$*" >&2; }
die(){ printf '[FAIL] %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root: sudo $0 $*"
command -v iptables >/dev/null || die "iptables not found"
mkdir -p "$STATE_DIR"

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
    ok "Port $AGH_PORT is open for ${minutes} minute(s), until $(date -d "@$until" '+%H:%M:%S' 2>/dev/null || echo "$until")"
    ip=$(ip_addr)
    [[ -n "$ip" ]] && ok "http://$ip:$AGH_PORT/"
    w "sentinel-guardian re-blocks it automatically once that time passes - no need to remember to disable it."
    ;;
  disable|off)
    rm -f "$UNBLOCK_FILE"
    apply_block
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
    ;;
  *)
    echo "Usage: sudo $0 enable [MINUTES] | disable | status" >&2
    echo "  enable [MINUTES]  open :$AGH_PORT temporarily (default: ${DEFAULT_MINUTES} minutes)" >&2
    echo "  disable           block :$AGH_PORT immediately (the permanent default)" >&2
    echo "  status            show the current state" >&2
    exit 1
    ;;
esac
