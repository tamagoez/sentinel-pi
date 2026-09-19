#!/usr/bin/env bash
# Repairs the "music says it is playing but nothing comes out of the
# 3.5mm jack" state.
#
# mpg123 reports playing and logs no error in every one of these cases,
# because from its point of view the device opened and accepted the
# samples - the audio just never reaches the speaker:
#
#   1. The shared hardware volume (numid=1, "PCM Playback Volume") is
#      parked at its floor, i.e. silent. Nothing in this project ever puts
#      it back: voice.py scales its own WAV samples instead of touching any
#      ALSA mixer control at all, and bluetooth.py only ever writes
#      bluealsa's own per-connection volume (bluealsa-cli), never numid=1.
#      So a single low value - from an old install, or a value left behind
#      by a previous release that did still touch it - silences music
#      permanently with nothing reporting an error.
#   2. Output routing (numid=3) points somewhere other than the jack.
#   3. The card that `sysdefault:CARD=<N>` actually opens is not the analog
#      output - this happens when ALSA's card numbering shifts across a
#      reboot (a new USB device attached before the audio card, for
#      instance).
#
# Unlike earlier versions of this script, there is no configuration file to
# repair here at all. `sysdefault:CARD=<N>` is alsa-lib's own per-card
# dmix route - it needs no `/etc/asound.conf` and cannot drift out of sync
# with anything, because there is nothing written to disk to drift. See
# CLAUDE.md's audio-mixing redesign section for the full history of why
# this project stopped hand-writing that file.
#
# This is the single bash copy of find_output_card() (CLAUDE.md #37):
# sentinel-guardian.sh's check_audio() delegates here instead of keeping
# its own, so the "Headphones -> bcm2835 -> first card" priority lives in
# exactly one place per language (core/audio.py for Python).
#
#   Usage: sentinel-fix-audio-output.sh [--quiet]
#          sentinel-fix-audio-output.sh --print-card   (no root needed)
#   Exit:  0 nothing needed | 10 fixed something | 1 could not fix
#
# --print-card only prints the detected card index (or nothing, with a
# non-zero exit, if none is found) and does not touch anything - it exists
# so install.sh can reuse this script's find_output_card() priority instead
# of keeping a third copy of it (CLAUDE.md #37: exactly one bash copy).

set -uo pipefail

find_output_card() {
  # Same priority as core/audio.py's find_output_card(): prefer the
  # analog jack ("Headphones" on current kernels), then the older
  # single-card "bcm2835" naming, and only then fall back to the first
  # card. Never just take the first card - on current Pi kernels card 0
  # is frequently an HDMI output (CLAUDE.md #37).
  local out first="" idx line
  out=$(aplay -l 2>/dev/null) || return 1
  while IFS= read -r line; do
    [[ "$line" == card\ * ]] || continue
    idx=${line#card }
    idx=${idx%%:*}
    [[ "$idx" =~ ^[0-9]+$ ]] || continue
    [[ -z "$first" ]] && first="$idx"
    if [[ "${line,,}" == *headphones* ]]; then echo "$idx"; return 0; fi
  done <<<"$out"
  while IFS= read -r line; do
    [[ "$line" == card\ * ]] || continue
    idx=${line#card }
    idx=${idx%%:*}
    [[ "$idx" =~ ^[0-9]+$ ]] || continue
    if [[ "${line,,}" == *bcm2835* ]]; then echo "$idx"; return 0; fi
  done <<<"$out"
  [[ -n "$first" ]] && { echo "$first"; return 0; }
  return 1
}

if [[ "${1:-}" == "--print-card" ]]; then
  find_output_card
  exit $?
fi

QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1
FIXED=0

[[ $EUID -eq 0 ]] || { echo "sentinel-fix-audio-output.sh: must run as root" >&2; exit 1; }

say() { (( QUIET )) || printf '  %s\n' "$*"; }
ok()  { (( QUIET )) || printf '  [OK] %s\n' "$*"; }
w()   { printf '  [!!] %s\n' "$*" >&2; }
fixed_msg() { FIXED=1; printf '  [FIXED] %s\n' "$*"; }

command -v amixer >/dev/null 2>&1 || exit 0

CARD=$(find_output_card) || { w "no ALSA playback card found (aplay -l empty)"; exit 1; }
say "Analog output card: $CARD"

# ---------------------------------------------------------------- 1. volume
# bcm2835's PCM Playback Volume is an INTEGER control in hundredths of a
# dB (typically min=-10239, max=400), where the minimum is silence. Only
# treat the very bottom of the range as broken - anything above that is a
# deliberate "quiet but audible" setting and must not be overridden.
VOL_INFO=$(amixer -c "$CARD" cget numid=1 2>/dev/null)
VOL_NAME=$(grep -m1 -oE "name='[^']*'" <<<"$VOL_INFO" | cut -d"'" -f2)
VOL_LINE=$(grep -m1 'type=INTEGER' <<<"$VOL_INFO")
if [[ -n "$VOL_NAME" && "$VOL_NAME" != *Volume* ]]; then
  w "numid=1 on card $CARD is '$VOL_NAME', not a volume control - not touching it"
  VOL_LINE=""
fi
VMIN=$(grep -oE 'min=-?[0-9]+' <<<"$VOL_LINE" | head -n1 | cut -d= -f2)
VMAX=$(grep -oE 'max=-?[0-9]+' <<<"$VOL_LINE" | head -n1 | cut -d= -f2)
VCUR=$(grep -m1 -oE ': values=-?[0-9]+' <<<"$VOL_INFO" | grep -oE '\-?[0-9]+$')

if [[ -n "$VMIN" && -n "$VMAX" && -n "$VCUR" ]] && (( VMAX > VMIN )); then
  FLOOR=$(( VMIN + (VMAX - VMIN) / 100 ))
  if (( VCUR <= FLOOR )); then
    if amixer -c "$CARD" cset numid=1 80% >/dev/null 2>&1; then
      fixed_msg "hardware volume (numid=1) was at its floor ($VCUR) - raised to 80%"
      alsactl store >/dev/null 2>&1 || true
    else
      w "hardware volume (numid=1) is at its floor ($VCUR) and could not be raised"
    fi
  else
    ok "hardware volume (numid=1) is $VCUR (min $VMIN, max $VMAX)"
  fi
else
  say "could not read numid=1 on card $CARD - skipping the volume check"
fi

# A muted switch silences everything just as effectively as a zero volume,
# and which switch exists depends on the kernel's naming. Try both and
# ignore the ones that are not present.
for sw in PCM Headphone Master; do
  amixer -c "$CARD" sset "$sw" unmute >/dev/null 2>&1 || true
done

# ---------------------------------------------------------------- 2. routing
# numid=3 is the analog/HDMI route selector on bcm2835 - but a numid is
# just an index, so on any other card it addresses something else
# entirely. Check the control's NAME before writing: real hardware showed
# numid=3 reading 230 (no route enum has that value) while this check
# "fixed" it back to 1 on every Guardian cycle, i.e. it was repeatedly
# writing into an unrelated control. Never write a numid without
# confirming what it is (same class of mistake as CLAUDE.md #15's
# numid=1/numid=3 mix-up).
ROUTE_INFO=$(amixer -c "$CARD" cget numid=3 2>/dev/null)
ROUTE_NAME=$(grep -m1 -oE "name='[^']*'" <<<"$ROUTE_INFO" | cut -d"'" -f2)
ROUTE=$(grep -m1 -oE ': values=-?[0-9]+' <<<"$ROUTE_INFO" | grep -oE '\-?[0-9]+$')
if [[ "$ROUTE_NAME" != *Route* ]]; then
  w "numid=3 on card $CARD is '${ROUTE_NAME:-unknown}', not a playback route - not touching it"
  w "  (this card's routing control is elsewhere; value read: ${ROUTE:-?})"
elif [[ -n "$ROUTE" && "$ROUTE" != "1" ]]; then
  if amixer -c "$CARD" cset numid=3 1 >/dev/null 2>&1; then
    fixed_msg "output routing ($ROUTE_NAME) was $ROUTE - reset to AUX (headphone jack)"
    alsactl store >/dev/null 2>&1 || true
  fi
elif [[ -n "$ROUTE" ]]; then
  ok "output routing ($ROUTE_NAME) is already AUX"
fi

# ---------------------------------------------------------------- 3. real open test
# Same "actually try it" discipline as sentinel-fix-storage-owner.sh's
# can_write() (CLAUDE.md #8) - a card that looks right but will not open is
# exactly the failure this is meant to catch. `sysdefault:CARD=<N>` needs no
# configuration file, so there is nothing here to rewrite - only the card
# itself can be at fault, which unstick_card() below addresses.
pcm_opens() {
  timeout 6 aplay -D "$1" -f S16_LE -r 44100 -c 2 -d 1 -q /dev/zero >/dev/null 2>&1
}

# Guardian runs this every 2 minutes; repeatedly opening and closing
# bcm2835 is itself the churn that can wedge the card (CLAUDE.md #45), so a
# driver reload that did not help must not be retried on every cycle.
_RELOAD_COOLDOWN_SEC=600

unstick_card() {
  # A bcm2835 left wedged by a client that crashed mid-stream refuses
  # every open until the driver is reloaded - the state CLAUDE.md #45
  # describes ("failed to close VCHI service connection"). Reloading the
  # ALSA driver is the documented remedy for this class of "card listed
  # but will not open"
  # (https://bbs.archlinux.org/viewtopic.php?id=173709). Only ever do this
  # when nothing holds /dev/snd - yanking the driver out from under a live
  # player would be the more damaging bug - and at most once every 10
  # minutes so a card that is broken for some other reason is not reloaded
  # on every Guardian cycle.
  local stamp=/run/sentinel-alsa-reload
  if fuser /dev/snd/* >/dev/null 2>&1; then
    w "not reloading the ALSA driver: something still has /dev/snd open"
    return 1
  fi
  if [[ -f "$stamp" ]] && (( $(date +%s) - $(stat -c %Y "$stamp" 2>/dev/null || echo 0) < _RELOAD_COOLDOWN_SEC )); then
    w "not reloading the ALSA driver again yet (tried within the last 10 minutes)"
    return 1
  fi
  : > "$stamp"
  # Reload only the analog driver first. `alsa force-reload` unloads every
  # sound module, which on this machine includes snd_usb_audio for the USB
  # camera's microphone - and any module reload can renumber the cards, so
  # the narrower action is the safer one. CARD is re-resolved by the caller
  # afterwards for exactly that reason.
  if lsmod 2>/dev/null | grep -q '^snd_bcm2835 ' \
     && modprobe -r snd_bcm2835 >/dev/null 2>&1 \
     && modprobe snd_bcm2835 >/dev/null 2>&1; then
    sleep 2; return 0
  fi
  if command -v alsa >/dev/null 2>&1 && alsa force-reload >/dev/null 2>&1; then
    sleep 2; return 0
  fi
  w "could not reload the ALSA driver (it may be built into the kernel)"
  return 1
}

if pcm_opens "sysdefault:CARD=$CARD"; then
  ok "sysdefault:CARD=$CARD opens and accepts audio"
else
  w "sysdefault:CARD=$CARD will not open:"
  timeout 6 aplay -D "sysdefault:CARD=$CARD" -f S16_LE -r 44100 -c 2 -d 1 /dev/zero 2>&1 \
    | sed 's/^/       /' >&2
  holders=$(fuser -v /dev/snd/* 2>&1 | tail -n +2 | tr -s ' ' | paste -sd' ' -)
  [[ -n "$holders" ]] && w "  /dev/snd holders: $holders"
  if unstick_card; then
    # A module reload can change card numbering, so ask again rather than
    # trusting the index we resolved before the reload.
    NEWCARD=$(find_output_card) && [[ -n "$NEWCARD" ]] && CARD="$NEWCARD"
    if pcm_opens "sysdefault:CARD=$CARD"; then
      fixed_msg "the sound card was stuck; reloading the ALSA driver cleared it (card is now $CARD, sysdefault opens)"
    else
      w "sysdefault:CARD=$CARD still will not open after reloading the driver"
    fi
  fi
fi

(( FIXED )) && exit 10
exit 0
