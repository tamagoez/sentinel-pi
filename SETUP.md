# Setup — from a blank SD card to a running Sentinel

All console output in this project (DietPi and the scripts) is English by
design — Japanese does not render reliably on a physical HDMI console or a
bare serial terminal. The Web UI itself stays Japanese; it renders in a
browser, which has no such limitation.

## Phase 0 — flash and pre-configure

Flash the official DietPi image (Raspberry Pi 3, ARMv7 or ARM64) and edit
`dietpi.txt` on the boot partition before first boot:

```ini
AUTO_SETUP_LOCALE=en_US.UTF-8
AUTO_SETUP_KEYBOARD_LAYOUT=us
AUTO_SETUP_TIMEZONE=Asia/Tokyo

AUTO_SETUP_NET_ETHERNET_ENABLED=1
# Static IP so the Web UI and AdGuard's DNS reference stay stable.
# Adjust these four to match YOUR router's subnet if it isn't 192.168.0.x.
AUTO_SETUP_NET_USESTATIC=1
AUTO_SETUP_NET_STATIC_IP=192.168.0.100
AUTO_SETUP_NET_STATIC_MASK=255.255.255.0
AUTO_SETUP_NET_STATIC_GATEWAY=192.168.0.1
AUTO_SETUP_NET_STATIC_DNS=192.168.0.1

AUTO_SETUP_SWAPFILE_SIZE=0
# Skips desktop/GUI-related first-boot prompts; keeps setup fully
# unattended on a keyboard/monitor-less (headless) box.
AUTO_SETUP_HEADLESS=1

SURVEY_OPTED_IN=0
CONFIG_CPU_GOVERNOR=ondemand
```

`AUTO_SETUP_NET_USESTATIC=1` is required — the IP/mask/gateway fields are
silently ignored otherwise and DHCP is used regardless.

Before first boot, also copy the **whole** `sentinel` project folder onto
the external drive you intend to use for storage (from your PC — via a
card reader, etc.). You'll mount that drive on the Pi in Phase 2 and pull
the project off it; no network transfer (scp) is needed.

## Phase 1 — first boot

Boot with Ethernet connected, SSH in (`root` / `dietpi`), let the initial
update finish, and change the root password via `dietpi-config` (5:
Security Options). Reboot once:

```bash
reboot
```

## Phase 2 — mount the drive and get the project onto local disk

```bash
dietpi-drive_manager
# select the drive, mount it, confirm it lands on /mnt/VIDEOSD,
# and that it's added to /etc/fstab for auto-mount on boot
```

Copy the project to local disk before running anything. This matters:
exFAT/NTFS drives often mount `noexec` or lose the executable bit, so
scripts must run from the SD card, not from the drive itself.

```bash
cp -r /mnt/VIDEOSD/sentinel ~/sentinel
cd ~/sentinel
chmod +x *.sh scripts/*.sh
```

`~/sentinel` now contains an *outer* project folder with `install.sh`,
`bootstrap.sh`, `deploy.sh`, and an *inner* `sentinel/` folder (the Python
package). Keep both — copy the whole tree, never just the inner one.

## Phase 3 — deploy

```bash
sudo ./deploy.sh
```

This runs unattended: it installs prerequisites (`bootstrap.sh`), reboots
once automatically (Bluetooth/audio changes require it), resumes itself,
then installs Sentinel (`install.sh`). Takes about 5 minutes plus the
reboot. Safe to re-run if interrupted.

`bootstrap.sh` installs, in this order (order matters — AdGuard before the
hotspot, so the hotspot's DHCP can later point at it for DNS):

| Step | What | dietpi-software ID |
|---|---|---|
| 1 | Force English UTF-8 locale (fixes console mojibake even if already booted once) | — |
| 2 | ALSA, FFmpeg, Git, Python3 pip, yt-dlp | 5, 7, 17, 130, 195 |
| 3 | AdGuard Home + Unbound (installed together so DietPi wires them up) | 126, 182 |
| 4 | WiFi Hotspot | 60 |
| 5 | Bluetooth (a `dietpi-config` item, not dietpi-software) | — |
| 6 | Audio routed to the 3.5mm jack | — |
| 7 | SWAP disabled | — |

`install.sh` then creates a dedicated non-root `sentinel` user, deploys
the app to `/opt/sentinel`, sets up the data directory, disables any
legacy `camguard`/`music-player` services, registers the Bluetooth
services, and — importantly — registers **Guardian**, which re-checks
the whole configuration every 2 minutes and repairs drift (see below).

If you'd rather run the two phases yourself: `sudo ./bootstrap.sh`,
reboot, then `sudo ./install.sh`. `deploy.sh` is just an unattended
wrapper around both.

## Phase 4 — first login

Open `http://192.168.0.100:8083` (or your chosen IP) and finish the
AdGuard Home setup wizard (admin user, password, confirm query logging is
enabled). It's still reachable from outside at this point — `install.sh`
locks it to localhost-only.

Then open `http://192.168.0.100:8080` and, in the settings tab, set:

1. **Web UI password** — unset by default; required since the web
   terminal gives shell access
2. **Discord webhook URL**
3. **AdGuard Home password** — needed for access logging

## Verifying it stuck

```bash
systemctl status sentinel
journalctl -t sentinel-guardian --since "10 min ago"   # should be empty/quiet
ss -ltn 'sport = :8083'                                # only 127.0.0.1:8083
ls -l /dev/v4l/by-id/                                  # cameras detected
sudo -u sentinel mpg123 /mnt/VIDEOSD/sentinel/music/<file>.mp3   # no stutter
vcgencmd measure_temp && vcgencmd get_throttled
```

From another device, confirm `http://<IP>:8083` does **not** load —
neither from the LAN nor from the hotspot.

## Why it stays fixed across reboots

`iptables` rules live only in kernel memory and vanish on reboot; the
WiFi Hotspot can also touch them after boot. Rather than apply settings
once, **Guardian** (`sentinel-guardian.timer`) re-checks everything every
2 minutes and repairs it:

| Item | What can break it | Guardian's fix |
|---|---|---|
| AdGuard bind address | AdGuard auto-update | rewrite `AdGuardHome.yaml`, restart |
| Port 8083 actually blocked | reboot, hostapd | re-insert iptables rules |
| Audio output (AUX) | kernel update | reset ALSA `numid=3` |
| Bluetooth discoverable/pairable | bluetoothd restart | re-enable via `bluetoothctl` |
| Service uptime | any crash | re-enable and start |
| Hotspot DNS target | hotspot reconfigured | point back at AdGuard |
| Disk space | accumulation | warn at 92%, keep one diagnostics bundle |
| yt-dlp version | site changes | try an update weekly |

## Updating

```bash
cd ~/sentinel
git pull   # or copy new files in the same way as Phase 2
sudo ./install.sh
```

Config and data are preserved.

## Troubleshooting

| Symptom | Check |
|---|---|
| Web UI won't load | `journalctl -u sentinel -n 60 --no-pager` |
| No cameras | `ls /dev/v4l/by-id/`, `dmesg \| tail -30` |
| Audio stutters | test raw: `mpg123 <file>`; raise `mpg123 buffer` in settings; stop PulseAudio if present |
| Can't pair Bluetooth | `systemctl status sentinel-bt-agent`; `bluetoothctl show` should say `Discoverable: yes` |
| 8083 still reachable | `systemctl start sentinel-guardian`; `journalctl -t sentinel-guardian -n 20` |
| Anything else | `sentinel-diagnose` — bundles system + app state into one archive |
