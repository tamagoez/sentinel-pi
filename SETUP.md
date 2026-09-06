# Setup — from a blank SD card to a running Sentinel

日本語版: [SETUP.ja.md](SETUP.ja.md)

Sentinel is installed from a git clone, and `setup.sh` walks you through it.
It automates everything a script can decide and stops at the six points that
need a person (marked **H1**–**H6** below).

All console output — DietPi's and this project's — is English by design.
Japanese does not render on a physical HDMI console or a bare serial
terminal. The Web UI stays Japanese; it renders in a browser.

## Overview

| Phase | Command | What happens |
|---|---|---|
| 0 | (on your PC) | flash DietPi, edit `dietpi.txt` |
| 1 | first boot | DietPi's own first-run setup |
| 2 | `sudo ./setup.sh` | H1, H2 → `bootstrap.sh` → H3 (reboot) |
| 3 | `sudo ./setup.sh` | H4, H5 → `install.sh` → H6 |

`setup.sh` remembers where it stopped in `/var/lib/sentinel/setup-stage`, so
after the reboot you run the same command again and it resumes. Re-running it
at any time is safe.

## Phase 0 — flash and pre-configure

Flash the official DietPi image for Raspberry Pi (ARMv7 or ARM64) and edit
`dietpi.txt` on the boot partition before the first boot:

```ini
AUTO_SETUP_LOCALE=en_US.UTF-8
AUTO_SETUP_KEYBOARD_LAYOUT=us
AUTO_SETUP_TIMEZONE=Asia/Tokyo

AUTO_SETUP_NET_ETHERNET_ENABLED=1
# Static address, so the Web UI bookmark and the DNS target stay put.
# Adjust these four to your router's subnet if it isn't 192.168.0.x.
AUTO_SETUP_NET_USESTATIC=1
AUTO_SETUP_NET_STATIC_IP=192.168.0.100
AUTO_SETUP_NET_STATIC_MASK=255.255.255.0
AUTO_SETUP_NET_STATIC_GATEWAY=192.168.0.1
AUTO_SETUP_NET_STATIC_DNS=192.168.0.1

AUTO_SETUP_SWAPFILE_SIZE=0

SURVEY_OPTED_IN=0
CONFIG_CPU_GOVERNOR=ondemand

# Optional: set your own password here instead of the default "dietpi".
# It becomes the root/dietpi login password AND the AdGuard Home admin
# password, and is removed from this file during the first boot.
#AUTO_SETUP_GLOBAL_PASSWORD=dietpi
```

`AUTO_SETUP_NET_USESTATIC=1` is required — the IP/mask/gateway/DNS fields
below it are ignored otherwise and DHCP is used regardless.

Nothing else needs to be copied onto the SD card or an external drive: the
project comes from git in phase 2.

**On Windows**, `windows/Configure-DietPi.ps1` automates this edit instead of
copy/pasting the block above by hand:

```powershell
# from a PowerShell prompt, with the SD card's boot partition inserted
.\windows\Configure-DietPi.ps1
```

It auto-detects the boot drive, applies the same defaults as the block
above, takes a timestamped backup before writing, and only touches the keys
it knows about. Pass parameters to change any value (`-StaticIP`,
`-Timezone`, `-HotspotSsid`/`-HotspotPassphrase` to pre-set the WiFi hotspot
so H2 has nothing left to ask, `-WhatIf` for a dry run) — see
`Get-Help .\windows\Configure-DietPi.ps1 -Full` for all of them. Its own
output is English too, for the same reason as everything else on this page.

## Phase 1 — first boot

Boot with Ethernet connected and SSH in as `root` / `dietpi` (or the
password you set above). DietPi runs its first-run setup: let the update
finish, accept the license, and change the password when it asks. You can
leave the software selection empty — `bootstrap.sh` installs what Sentinel
needs.

## Phase 2 — clone and start setup.sh

A fresh DietPi has no git, so install it first:

```bash
apt-get update && apt-get install -y git     # or: dietpi-software install 17
git clone https://github.com/tamagoez/sentinel-pi.git ~/sentinel-pi
cd ~/sentinel-pi
sudo ./setup.sh
```

`setup.sh` then asks you for:

- **H1 — the network address.** It prints the current IPv4 address and warns
  if it came from DHCP. A drifting address breaks both your bookmark and the
  DNS address the hotspot hands out.
- **H2 — the WiFi hotspot.** Whether to install it, and the SSID and
  passphrase (written to `SOFTWARE_WIFI_HOTSPOT_SSID` / `_KEY` in
  `/boot/dietpi.txt`, which is what DietPi's hotspot installer reads). Say no
  and the hotspot is skipped entirely.

It then runs `bootstrap.sh` unattended, which installs — in this order,
because the order matters:

| Step | What | dietpi-software ID |
|---|---|---|
| 1 | Force an English UTF-8 locale (fixes console mojibake) | — |
| 2 | ALSA, FFmpeg, Git, Python 3, yt-dlp | 5, 7, 17, 130, 195 |
| 3 | AdGuard Home + Unbound (one call, so DietPi wires them together: Unbound moves to port 5335 and becomes AdGuard's upstream) | 126, 182 |
| 4 | WiFi Hotspot, after AdGuard so its DHCP can be pointed at it | 60 |
| 5 | Bluetooth, plus bluez / bluez-alsa-utils / bluez-tools / mpg123 / v4l-utils / python3-opencv from APT | — |
| 6 | Audio routed to the 3.5mm jack (`dietpi-set_hardware soundcard rpi-bcm2835-3.5mm`) | — |
| 7 | SWAP disabled | — |

- **H3 — the reboot.** Bluetooth, the audio route and the SWAP change need a
  restart. `setup.sh` offers to reboot; nothing has been installed to `/opt`
  yet, so it is a safe point to stop.

## Phase 3 — resume setup.sh

```bash
cd ~/sentinel-pi
sudo ./setup.sh
```

- **H4 — the external drive.** If `/mnt/VIDEOSD` is not a mount point,
  `setup.sh` opens `dietpi-drive_manager` for you. Mount your drive there and
  set the mount point to exactly `/mnt/VIDEOSD`; the tool writes the
  `/etc/fstab` entry so it comes back after a reboot. You may decline and let
  everything live on the SD card, but that wears the card out. ext4 is the
  simplest choice; exFAT and NTFS work too - `install.sh` and Guardian fix
  the mount's `uid=`/`gid=` options automatically so the `sentinel` user can
  write there, since those filesystems have no Unix ownership of their own
  and plain `chown` cannot fix them.
- **H5 — AdGuard Home.** DietPi pre-configures it, so **there is no setup
  wizard**: it already listens on `0.0.0.0:8083` with the user `admin`, the
  DietPi global software password, and query logging enabled. Open
  `http://<Pi-IP>:8083` now — while it is still directly reachable — log in,
  and change the password if you want to. Afterwards the only way in is
  Sentinel's `/adguard/` proxy.

`install.sh` then runs unattended: it creates the non-root `sentinel` user,
deploys to `/opt/sentinel`, builds the venv, prepares the data directory,
disables any legacy `camguard` / `music-player` services, registers the
Bluetooth units, locks AdGuard to localhost, and registers **Guardian**,
which re-checks the whole configuration every 2 minutes and repairs drift.

- **H6 — the application settings.** Open `http://<Pi-IP>:8080` and, in the
  settings tab, set:
  1. the **Web UI password** — empty by default, and the web terminal is a
     shell for anyone who can reach port 8080
  2. the **Discord webhook URL**
  3. the **AdGuard Home password** — the one from H5, needed to read the
     query log

## Running the phases by hand

`setup.sh` only sequences things; the two scripts underneath stand alone:

```bash
sudo ./bootstrap.sh      # prerequisites
reboot
dietpi-drive_manager     # mount /mnt/VIDEOSD
sudo ./install.sh        # the application itself
```

## Verifying it stuck

```bash
systemctl status sentinel
journalctl -t sentinel-guardian --since "10 min ago"   # should be quiet
ss -ltn 'sport = :8083'                                # only 127.0.0.1:8083
ls -l /dev/v4l/by-id/                                  # cameras detected
sudo -u sentinel mpg123 /mnt/VIDEOSD/sentinel/music/<file>.mp3   # no stutter
vcgencmd measure_temp && vcgencmd get_throttled
```

From another device, confirm `http://<IP>:8083` does **not** load — neither
from the LAN nor from the hotspot.

## Why it stays fixed across reboots

`iptables` rules live only in kernel memory and vanish on reboot; the WiFi
Hotspot can also touch them after boot. Rather than apply settings once,
**Guardian** (`sentinel-guardian.timer`) re-checks everything every 2 minutes
and repairs it:

| Item | What can break it | Guardian's fix |
|---|---|---|
| AdGuard bind address | AdGuard auto-update | rewrite `AdGuardHome.yaml`, restart |
| Port 8083 actually blocked | reboot, hostapd | re-insert iptables rules |
| Audio output (AUX) | kernel update | reset ALSA `numid=3` |
| Bluetooth discoverable/pairable | bluetoothd restart | re-enable via `bluetoothctl` |
| Service uptime | any crash | re-enable and start |
| Storage ownership | drive re-mounted, exFAT/NTFS reset by dietpi-drive_manager | fix `/etc/fstab` `uid=`/`gid=` (or `chown` on ext4), remount |
| Hotspot DNS target | hotspot reconfigured | point back at AdGuard |
| Disk space | accumulation | warn at 92%, keep one diagnostics bundle |
| yt-dlp version | site changes | try an update weekly |

## Updating

```bash
cd ~/sentinel-pi
git pull
sudo ./setup.sh --update      # equivalent to: sudo ./install.sh
```

Config and data are preserved. `sudo ./setup.sh --reset` forgets the saved
progress if you want to walk through the whole guided flow again.

## Troubleshooting

| Symptom | Check |
|---|---|
| Web UI won't load | `journalctl -u sentinel -n 60 --no-pager` |
| Service restart-loops with `PermissionError: ... config.json.tmp` | `sudo /opt/sentinel/scripts/sentinel-fix-storage-owner.sh /mnt/VIDEOSD/sentinel /mnt/VIDEOSD sentinel` (Guardian also retries this every 2 minutes; see "Why it stays fixed" below) |
| No cameras | `ls /dev/v4l/by-id/`, `dmesg \| tail -30` |
| Audio stutters | test raw: `mpg123 <file>`; raise `mpg123 buffer` in settings; stop PulseAudio if present |
| No sound at all | `aplay -l`; re-run `/boot/dietpi/func/dietpi-set_hardware soundcard rpi-bcm2835-3.5mm` and reboot |
| Can't pair Bluetooth | `systemctl status sentinel-bt-agent`; `bluetoothctl show` should say `Discoverable: yes` |
| 8083 still reachable | `systemctl start sentinel-guardian`; `journalctl -t sentinel-guardian -n 20` |
| Network log empty | AdGuard password wrong in the settings tab, or query logging off in AdGuard |
| `setup.sh` starts from the wrong phase | `sudo ./setup.sh --reset` |
| Anything else | `sentinel-diagnose` — bundles system + app state into one archive |
