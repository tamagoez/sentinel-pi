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
#      it back: voice.py stopped touching numid=1 when it moved to its own
#      softvol control (CLAUDE.md #31), bluetooth.py only writes it when a
#      Bluetooth device connects (CLAUDE.md #15), and Guardian only ever
#      checked numid=3. So a single low value - from an old voice volume,
#      or a Bluetooth device whose stored volume was near zero - silences
#      music permanently with nothing reporting an error.
#   2. Output routing (numid=3) points somewhere other than the jack.
#   3. /etc/asound.conf's dmix slave (hw:N,0) names a different card than
#      the analog output actually is, so the samples go to HDMI. This
#      happens when asound.conf was written before the card detection fix
#      (CLAUDE.md #37), or when ALSA card numbering shifts across a reboot.
#
# This is the single bash copy of find_output_card() (CLAUDE.md #37):
# sentinel-guardian.sh's check_audio() delegates here instead of keeping
# its own, so the "Headphones -> bcm2835 -> first card" priority lives in
# exactly one place per language (core/audio.py for Python).
#
#   Usage: sentinel-fix-audio-output.sh [--quiet]
#   Exit:  0 nothing needed | 10 fixed something | 1 could not fix

set -uo pipefail

QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1
FIXED=0

[[ $EUID -eq 0 ]] || { echo "sentinel-fix-audio-output.sh: must run as root" >&2; exit 1; }

say() { (( QUIET )) || printf '  %s\n' "$*"; }
ok()  { (( QUIET )) || printf '  [OK] %s\n' "$*"; }
w()   { printf '  [!!] %s\n' "$*" >&2; }
fixed_msg() { FIXED=1; printf '  [FIXED] %s\n' "$*"; }

command -v amixer >/dev/null 2>&1 || exit 0

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

CARD=$(find_output_card) || { w "no ALSA playback card found (aplay -l empty)"; exit 1; }
say "Analog output card: $CARD"

# ---------------------------------------------------------------- 1. volume
# bcm2835's PCM Playback Volume is an INTEGER control in hundredths of a
# dB (typically min=-10239, max=400), where the minimum is silence. Only
# treat the very bottom of the range as broken - anything above that is a
# deliberate "quiet but audible" setting (a Bluetooth per-device volume,
# for instance) and must not be overridden.
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

# ---------------------------------------------------------------- 3. asound.conf
# The dmix slave card is baked into /etc/asound.conf as hw:N,0. Only
# modules/music.py can regenerate it (it owns the equalizer settings that
# go into the same file), so report a mismatch rather than writing a
# version here that would silently drop the user's EQ.
ASOUND=/etc/asound.conf
if [[ -f "$ASOUND" ]]; then
  CONF_CARD=$(grep -oE 'pcm "hw:[0-9]+,0"' "$ASOUND" | head -n1 | grep -oE '[0-9]+' | head -n1)
  if [[ -n "$CONF_CARD" && "$CONF_CARD" != "$CARD" ]]; then
    w "$ASOUND mixes into card $CONF_CARD but the analog output is card $CARD"
    w "music would be playing into the wrong card (usually HDMI = silence)"
    (( QUIET )) || {
      echo
      echo "  Restart Sentinel so it regenerates $ASOUND for card $CARD:"
      echo "    systemctl restart sentinel"
    }
  else
    ok "$ASOUND mixes into card ${CONF_CARD:-?} (matches the analog output)"
  fi
fi

# ---------------------------------------------------------------- 4. real open test
# Same "actually try it" discipline as sentinel-fix-storage-owner.sh's
# can_write() (CLAUDE.md #8) - a config that looks right but will not open
# is exactly the failure this is meant to catch.
if aplay -L 2>/dev/null | grep -qx 'sentinel_music'; then
  if timeout 5 aplay -D sentinel_music -f S16_LE -r 44100 -c 2 -d 1 -q /dev/zero >/dev/null 2>&1; then
    ok "sentinel_music opens and accepts audio"
  else
    w "sentinel_music exists in asound.conf but will not open:"
    timeout 5 aplay -D sentinel_music -f S16_LE -r 44100 -c 2 -d 1 /dev/zero 2>&1 \
      | sed 's/^/       /' >&2
    # Distinguish "the dmix definition is wrong" from "the card itself
    # cannot be opened right now". dmix opens its slave with a fixed
    # format, so a card that is busy, or a bcm2835 left in a stuck state
    # by a crashed client, fails here with a bare "Invalid argument" and
    # no hint as to which of the two it is.
    if timeout 5 aplay -D "hw:$CARD,0" -f S16_LE -r 44100 -c 2 -d 1 -q /dev/zero >/dev/null 2>&1; then
      w "but hw:$CARD,0 itself opens fine - the dmix definition is the problem"
    else
      w "hw:$CARD,0 will not open either - the card is busy or stuck, not a config problem"
      holders=$(fuser -v /dev/snd/* 2>&1 | tail -n +2 | tr -s ' ' | paste -sd' ' -)
      [[ -n "$holders" ]] && w "  /dev/snd holders: $holders"
      (( QUIET )) || {
        echo
        echo "  Find what is holding the sound card, then stop it:"
        echo "    fuser -v /dev/snd/*"
        echo "    systemctl status sentinel-bluealsa-aplay sentinel-bluealsa"
        echo "  A card left stuck by a crashed client usually needs a reboot."
      }
    fi
  fi
else
  # Without this PCM, music.py plays to plughw:<card>,0 directly (it no
  # longer falls back to ALSA's bare default, which on a multi-card Pi is
  # usually HDMI). Mixing music with voice announcements stays off until
  # the dmix setup succeeds, but music itself is audible.
  say "sentinel_music is not defined - music plays straight to card $CARD (no mixing)"
fi

(( FIXED )) && exit 10
exit 0
