#!/usr/bin/env bash
# sentinel-tailscale [status|up|down|reset]
#
# One command for the whole Tailscale side of this Pi, so nobody has to
# remember the flags. setup.sh H7 asks about Tailscale during the initial
# install, but that question is easy to skip and impossible to find again
# afterwards - this is the "any time later" entry point, installed as
# /usr/local/bin/sentinel-tailscale.
#
#   status  what the tailnet sees: connected or not, this Pi's tailnet IP
#           and name, and the URLs to open from a phone/laptop
#   up      join the tailnet (prints a login URL to open in any browser)
#   down    leave the tailnet (tailscaled keeps running; `up` rejoins
#           without logging in again)
#   reset   log out completely, so the next `up` asks for a fresh login
#           (use when moving the Pi to a different tailnet/account)
#
# --accept-dns=false is passed on EVERY `tailscale up`, and that is not
# optional here (CLAUDE.md #39). By default Tailscale repoints this Pi's
# DNS resolver at itself. modules/netlog.py has no DNS of its own - it
# reads AdGuard Home's query log - so the moment this Pi stops resolving
# through AdGuard, its own lookups vanish from the network log while
# AdGuard keeps looking perfectly healthy. Nothing errors; the log just
# quietly goes incomplete. Tailscale does not remember flags between runs
# either, so every invocation has to pass it again.
#
# Usage is intentionally read-only by default: plain `sentinel-tailscale`
# is the same as `status` and changes nothing.

set -uo pipefail

ACTION="${1:-status}"

say() { printf '  %s\n' "$*"; }
ok()  { printf '  [OK] %s\n' "$*"; }
w()   { printf '  [!!] %s\n' "$*" >&2; }

if ! command -v tailscale >/dev/null 2>&1; then
  w "Tailscale is not installed."
  say "Install it, then run this again:"
  say "  curl -fsSL https://tailscale.com/install.sh | sh"
  say "  sudo sentinel-tailscale up"
  exit 1
fi

need_root() {
  [[ $EUID -eq 0 ]] && return 0
  w "This needs root. Run: sudo sentinel-tailscale $ACTION"
  exit 1
}

ts_ip() { tailscale ip -4 2>/dev/null | head -n1; }

show_status() {
  local st ip name
  st=$(tailscale status 2>&1)
  if grep -qi 'logged out\|Log in at\|NeedsLogin' <<<"$st"; then
    w "Not connected to a tailnet yet."
    say "Connect with:  sudo sentinel-tailscale up"
    return 1
  fi
  if grep -qi 'stopped' <<<"$st"; then
    w "Tailscale is installed and logged in, but stopped."
    say "Bring it back up:  sudo sentinel-tailscale up"
    return 1
  fi
  ip=$(ts_ip)
  name=$(tailscale status --json 2>/dev/null \
          | grep -m1 -oE '"DNSName"[^,]*' | cut -d'"' -f4 | sed 's/\.$//')
  ok "Connected to a tailnet."
  [[ -n "$ip"   ]] && say "This Pi's tailnet IP: $ip"
  [[ -n "$name" ]] && say "This Pi's tailnet name: $name"
  if [[ -n "$ip" ]]; then
    say ""
    say "From any device signed in to the same tailnet:"
    say "  Sentinel web UI  http://$ip:8080"
    say "  Syncthing GUI    http://$ip:8384"
    [[ -n "$name" ]] && say "  (the name also works, e.g. http://$name:8080)"
  fi
  # A tailnet that resolves DNS through Tailscale silently breaks the
  # network log (see the header) - surface it rather than letting the log
  # go quietly incomplete.
  if tailscale status --json 2>/dev/null | grep -q '"MagicDNSSuffix"' \
     && grep -q '100\.100\.100\.100' /etc/resolv.conf 2>/dev/null; then
    w "This Pi is resolving DNS through Tailscale (100.100.100.100)."
    w "AdGuard Home no longer sees this Pi's own lookups, so the network"
    w "log will be incomplete. Fix it with:"
    say "  sudo sentinel-tailscale up"
  fi
  return 0
}

case "$ACTION" in
  status|"")
    show_status
    ;;
  up)
    need_root
    say "Joining the tailnet. If a login URL appears below, open it in a"
    say "browser on any device (phone, laptop) and sign in with the same"
    say "account as the rest of your tailnet."
    say ""
    # --accept-dns=false: see the header. Never drop this flag.
    if tailscale up --accept-dns=false; then
      say ""
      show_status
      say ""
      say "Tip: on the Machines page of the Tailscale admin console, use"
      say "     'Disable key expiry' for this Pi - it runs unattended, and"
      say "     an expired key would drop it off the tailnet with nobody"
      say "     there to log in again."
    else
      w "tailscale up did not complete."
      say "Check the daemon first, then try again:"
      say "  systemctl status tailscaled --no-pager"
      say "  sudo sentinel-tailscale up"
      exit 1
    fi
    ;;
  down)
    need_root
    tailscale down && ok "Left the tailnet (still logged in; 'up' rejoins)."
    ;;
  reset)
    need_root
    tailscale logout && ok "Logged out. The next 'up' will ask for a fresh login."
    ;;
  *)
    w "Unknown action: $ACTION"
    say "Usage: sentinel-tailscale [status|up|down|reset]"
    exit 1
    ;;
esac
