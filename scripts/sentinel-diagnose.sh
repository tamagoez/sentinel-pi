#!/usr/bin/env bash
# sentinel-diagnose — collect system-wide + app-internal state into one archive
#
#   sentinel-diagnose               writes under /tmp
#   sentinel-diagnose /path/to/dir  choose the output directory
#
# Root not required, but running as root captures more (full journalctl,
# iptables, dmesg).
set -uo pipefail

OUT="${1:-/tmp}/sentinel-diag-$(date +%Y%m%d-%H%M%S)"
API="${SENTINEL_API:-http://127.0.0.1:8080}"
DATA="${SENTINEL_DATA:-/mnt/VIDEOSD/sentinel}"
[[ -d "$DATA" ]] || DATA=/home/sentinel/sentinel-data

mkdir -p "$OUT"
say(){ printf '\033[1;36m==>\033[0m %s\n' "$*"; }
run(){ # run <output-file> <command...>
  local f="$1"; shift
  { "$@"; } >"$OUT/$f" 2>&1 || echo "(failed: $*)" >> "$OUT/$f"
}

say "collecting system information -> $OUT"

run system.txt bash -c '
  echo "== date =="; date
  echo; echo "== uptime =="; uptime
  echo; echo "== model =="; cat /proc/device-tree/model 2>/dev/null; echo
  echo; echo "== os-release =="; cat /etc/os-release 2>/dev/null
  echo; echo "== dietpi version =="; cat /boot/dietpi/.version 2>/dev/null
  echo; echo "== free =="; free -h
  echo; echo "== df =="; df -h
  echo; echo "== vcgencmd =="
  command -v vcgencmd >/dev/null && { vcgencmd measure_temp; vcgencmd get_throttled; vcgencmd measure_clock arm; }
  echo; echo "== cpufreq =="
  for f in /sys/devices/system/cpu/cpu0/cpufreq/{scaling_governor,scaling_cur_freq,scaling_available_governors}; do
    echo "$f: $(cat "$f" 2>/dev/null)"
  done
'

run network.txt bash -c '
  echo "== ip a =="; ip -brief addr 2>/dev/null || ifconfig -a
  echo; echo "== listening ports =="; ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null
  echo; echo "== resolv.conf =="; cat /etc/resolv.conf 2>/dev/null
  if command -v iptables >/dev/null; then
    echo; echo "== iptables -L -n -v =="; iptables -L -n -v 2>&1
    echo; echo "== iptables -t nat -L -n -v =="; iptables -t nat -L -n -v 2>&1
  fi
'

run camera.txt bash -c '
  echo "== v4l2 devices =="; v4l2-ctl --list-devices 2>&1
  echo; echo "== /dev/v4l/by-id =="; ls -l /dev/v4l/by-id/ 2>&1
  echo; echo "== dmesg (usb/uvc) =="; dmesg 2>/dev/null | grep -iE "usb|uvcvideo" | tail -80
'

run bluetooth.txt bash -c '
  echo "== bluetoothctl show =="; bluetoothctl show 2>&1
  echo; echo "== bluetoothctl devices =="; bluetoothctl devices 2>&1
  echo; echo "== main.conf =="; cat /etc/bluetooth/main.conf 2>&1
  echo; echo "== aplay -l =="; aplay -l 2>&1
  echo; echo "== amixer numid=3 =="; amixer -c0 cget numid=3 2>&1
'

run packages.txt bash -c '
  for p in ffmpeg mpg123 bluez bluez-alsa-utils bluez-tools python3-opencv python3-pil yt-dlp; do
    printf "%-20s " "$p"
    dpkg -l 2>/dev/null | awk -v p="$p" "\$2==p{print \$3}" || echo "?"
  done
  echo; echo "== yt-dlp version =="; yt-dlp --version 2>&1
  echo; echo "== ffmpeg =="; ffmpeg -version 2>&1 | head -1
  echo "Pillow (text overlays): $(python3 -c "import PIL; print(PIL.__version__)" 2>&1)"
  echo "h264_v4l2m2m: $(ffmpeg -hide_banner -encoders 2>/dev/null | grep -c h264_v4l2m2m)"
'

say "collecting systemd status"
run systemd.txt bash -c '
  for u in sentinel sentinel-guardian.timer sentinel-guardian.service \
           sentinel-bluealsa sentinel-bluealsa-aplay sentinel-bt-agent \
           bluetooth adguardhome AdGuardHome dietpi-wifi-hotspot hostapd; do
    echo "---- $u ----"
    systemctl status "$u" --no-pager -l 2>&1 | head -15
    echo
  done
'

say "collecting recent logs"
if [[ $EUID -eq 0 ]] || groups 2>/dev/null | grep -qw systemd-journal; then
  run journal-sentinel.log journalctl -u sentinel -n 3000 --no-pager
  run journal-guardian.log journalctl -t sentinel-guardian -n 500 --no-pager
  run journal-bluetooth.log journalctl -u bluetooth -n 300 --no-pager
else
  echo "(journalctl needs the systemd-journal group; skipped)" > "$OUT/journal-sentinel.log"
fi

if [[ -f "$DATA/config.json" ]]; then
  say "reading app config (secrets redacted)"
  python3 - "$DATA/config.json" > "$OUT/app-config.json" 2>&1 <<'PY' || true
import json, sys
d = json.load(open(sys.argv[1]))
for k in ("password", "adguard_password", "discord_webhook"):
    if d.get(k):
        d[k] = "********"
print(json.dumps(d, ensure_ascii=False, indent=2))
PY
fi

say "fetching live state from the running Sentinel"
if curl -fsS -m 5 "$API/api/auth" >/dev/null 2>&1; then
  COOKIE=$(mktemp)
  PW=""
  [[ -f "$DATA/config.json" ]] && PW=$(python3 -c "
import json
try: print(json.load(open('$DATA/config.json')).get('password',''))
except Exception: print('')
" 2>/dev/null)
  curl -fsS -m 5 -c "$COOKIE" -H 'Content-Type: application/json' \
    -d "{\"password\":\"${PW//\"/\\\"}\"}" "$API/api/login" >/dev/null 2>&1 || true
  if curl -fsS -m 15 -b "$COOKIE" "$API/api/diagnostics/bundle" -o "$OUT/app-bundle.tar.gz" 2>/dev/null; then
    echo "  got app-bundle.tar.gz (error history + per-module state)"
  else
    echo "(failed to fetch app-bundle; password mismatch?)" > "$OUT/app-bundle-error.txt"
  fi
  rm -f "$COOKIE"
else
  echo "(cannot reach Sentinel at $API; system info only)" > "$OUT/app-unreachable.txt"
fi

ARCHIVE="${OUT}.tar.gz"
tar czf "$ARCHIVE" -C "$(dirname "$OUT")" "$(basename "$OUT")"
rm -rf "$OUT"

say "done: $ARCHIVE"
du -h "$ARCHIVE" | awk '{print "  size: " $1}'
echo
echo "  Extract to inspect:"
echo "    tar xzf $ARCHIVE -C /tmp && ls /tmp/$(basename "$OUT")"
