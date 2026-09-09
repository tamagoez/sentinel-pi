#!/usr/bin/env bash
# sentinel-set-hotspot-ssid.sh <ssid>
#
# Rewrites the ssid= line in /etc/hostapd/hostapd.conf and restarts
# hostapd.service. Run as root via sudo from modules/hotspot.py
# (sentinel.service runs as the non-root "sentinel" user - CLAUDE.md #4 -
# and /etc/hostapd/hostapd.conf is root-writable only).
#
# DietPi normally sets the hotspot SSID once from dietpi.txt at image-prep
# time (setup.sh phase 0 / H2); this script is the runtime equivalent so it
# can be changed later from the Settings page without re-running setup.
#
# sudoers cannot restrict a wildcarded argument's *value*, so the
# validation below (length, no control characters) is the actual
# escalation boundary - the same pattern as sentinel-set-governor.sh.
set -uo pipefail

CONF="/etc/hostapd/hostapd.conf"
SSID="${1:?usage: sentinel-set-hotspot-ssid.sh <ssid>}"

# IEEE 802.11 SSIDs are at most 32 bytes; reject anything that could break
# the conf file's line structure.
byte_len=$(printf '%s' "$SSID" | wc -c)
if (( byte_len < 1 || byte_len > 32 )); then
  echo "refusing SSID with length $byte_len (must be 1-32 bytes)" >&2
  exit 1
fi
if [[ "$SSID" == *$'\n'* || "$SSID" == *$'\r'* ]]; then
  echo "refusing SSID containing a newline" >&2
  exit 1
fi

[[ -f "$CONF" ]] || { echo "$CONF not found (hostapd not installed?)" >&2; exit 1; }

# awk (not sed) so the SSID's literal bytes never pass through a regex
# replacement - an SSID containing '&', '|', or a backslash would
# otherwise corrupt the substitution or break out of it.
tmp="$(mktemp)"
awk -v ssid="$SSID" '
  BEGIN { done = 0 }
  /^[[:blank:]]*ssid=/ { print "ssid=" ssid; done = 1; next }
  { print }
  END { if (!done) print "ssid=" ssid }
' "$CONF" > "$tmp" && mv "$tmp" "$CONF"

systemctl restart hostapd.service
