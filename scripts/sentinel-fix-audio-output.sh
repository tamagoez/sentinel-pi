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
#   4. sentinel_music opens fine but sentinel_voice - a separate PCM
#      definition in the same file, with its own "SentinelVoice" softvol
#      control - does not, so voice announcements silently fall back to
#      stopping music instead of mixing with it (CLAUDE.md #65). This used
#      to go completely unchecked here.
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
    say "the open test below rewrites it for card $CARD if it really will not play"
  else
    ok "$ASOUND mixes into card ${CONF_CARD:-?} (matches the analog output)"
  fi
fi

# ---------------------------------------------------------------- 4. real open test
# Same "actually try it" discipline as sentinel-fix-storage-owner.sh's
# can_write() (CLAUDE.md #8) - a config that looks right but will not open
# is exactly the failure this is meant to catch.
#
# This section REPAIRS rather than only reports, because the failure mode
# it catches takes the whole machine's audio down, not just mixing:
# sentinel-setup-audio-mixing.sh points pcm.!default at the same dmix, so
# a dmix that cannot open its slave silences bluealsa-aplay and every
# bare `aplay` too, not only Sentinel's music.
SETUP_MIX="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/sentinel-setup-audio-mixing.sh"

pcm_opens() {
  timeout 6 aplay -D "$1" -f S16_LE -r 44100 -c 2 -d 1 -q /dev/zero >/dev/null 2>&1
}

# Guardian runs this every 2 minutes. Rewriting /etc/asound.conf costs
# several real playback opens (the setup script tests what it wrote), and
# repeatedly opening and closing bcm2835 is the very churn that wedges the
# card (CLAUDE.md #45) - so a repair that did not take must not be retried
# on every cycle. This used to be a flat 1-hour cooldown, but that is too
# conservative for the common case: a burst of USB contention (camera
# resubmit errors, a Bluetooth churn storm) breaks the dmix config while
# hw:$CARD,0 itself still opens fine, meaning the break is isolated to the
# asound.conf layer and a retry a few minutes later is very likely to
# succeed - yet the flat cooldown left mixing off for up to an hour after
# a single bad moment. Back off exponentially instead, the same pattern
# camera.py already uses for its own reconnect storms
# (_CORRUPT_RECONNECT_MAX_BACKOFF, CLAUDE.md #19): start at 2 minutes so
# the very next Guardian cycle can retry, double on each further failure,
# and cap at 1 hour so a truly broken card does not get hammered forever.
# **Do not collapse this back into a flat cooldown** - that is the exact
# regression this section fixes (mixing staying off for up to an hour after
# a transient, already-recovered failure).
_AUDIO_RETRY_BASE_SEC=120
_AUDIO_RETRY_MAX_SEC=3600

may_retry() {
  local stamp="/run/sentinel-audio-$1" now last_try=0 count=0 wait
  now=$(date +%s)
  if [[ -f "$stamp" ]]; then
    read -r last_try count < "$stamp" 2>/dev/null || { last_try=0; count=0; }
  fi
  wait=$(( _AUDIO_RETRY_BASE_SEC * (1 << count) ))
  (( wait > _AUDIO_RETRY_MAX_SEC )) && wait=$_AUDIO_RETRY_MAX_SEC
  if (( now - last_try < wait )); then
    return 1
  fi
  (( count < 10 )) && count=$((count + 1))
  printf '%s %s\n' "$now" "$count" > "$stamp"
  return 0
}

# Called once a repair actually took (pcm_opens sentinel_music succeeded
# afterwards) so the *next* failure starts backing off from the short
# interval again, instead of inheriting a long wait earned by a previous,
# unrelated failure streak.
reset_retry() {
  rm -f "/run/sentinel-audio-$1"
}

unstick_card() {
  # A bcm2835 left wedged by a client that crashed mid-stream refuses
  # every open until the driver is reloaded - the state CLAUDE.md #45
  # describes ("failed to close VCHI service connection"), which until now
  # needed a reboot to clear. Reloading the ALSA driver is the documented
  # remedy for this class of "card listed but will not open"
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
  if [[ -f "$stamp" ]] && (( $(date +%s) - $(stat -c %Y "$stamp" 2>/dev/null || echo 0) < 600 )); then
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

if aplay -L 2>/dev/null | grep -qx 'sentinel_music'; then
  if pcm_opens sentinel_music; then
    ok "sentinel_music opens and accepts audio"
    # Healthy right now - if an earlier failure left a backoff stamp behind,
    # drop it so the next real failure starts from the short retry interval
    # again instead of inheriting a wait earned by an unrelated past streak.
    reset_retry rewrite
    reset_retry create
  else
    w "sentinel_music exists in asound.conf but will not open:"
    timeout 6 aplay -D sentinel_music -f S16_LE -r 44100 -c 2 -d 1 /dev/zero 2>&1 \
      | sed 's/^/       /' >&2
    # Distinguish "the dmix definition is wrong" from "the card itself
    # cannot be opened right now". dmix opens its slave with a fixed
    # format, so a card that is busy, or a bcm2835 left in a stuck state
    # by a crashed client, fails here with a bare "Invalid argument" and
    # no hint as to which of the two it is.
    if ! pcm_opens "hw:$CARD,0"; then
      w "hw:$CARD,0 will not open either - the card is busy or stuck, not a config problem"
      holders=$(fuser -v /dev/snd/* 2>&1 | tail -n +2 | tr -s ' ' | paste -sd' ' -)
      [[ -n "$holders" ]] && w "  /dev/snd holders: $holders"
      if unstick_card; then
        # A module reload can change card numbering, so ask again rather
        # than trusting the index we resolved before the reload.
        NEWCARD=$(find_output_card) && [[ -n "$NEWCARD" ]] && CARD="$NEWCARD"
        if pcm_opens "hw:$CARD,0"; then
          fixed_msg "the sound card was stuck; reloading the ALSA driver cleared it (card is now $CARD)"
        fi
      fi
    fi
    if ! pcm_opens sentinel_music && pcm_opens "hw:$CARD,0"; then
      # The hardware is fine, so the dmix definition is what is wrong -
      # a stale card index, or a file left behind by an older release.
      # Rewrite it for the card we actually found. EQ is written off:
      # modules/music.py re-applies the user's bands on the next track
      # (its staleness check compares the file, not just its own memo),
      # and audible music without EQ beats silent music with it.
      if [[ -x "$SETUP_MIX" ]] && may_retry rewrite && "$SETUP_MIX" "$CARD" off >/dev/null 2>&1 \
           && pcm_opens sentinel_music; then
        fixed_msg "rewrote $ASOUND for card $CARD - sentinel_music opens again"
        reset_retry rewrite
      elif [[ -f "$ASOUND" ]]; then
        # Last resort. asound.conf also redefines pcm.!default as this
        # same dmix, so leaving a dmix that cannot open in place silences
        # bluealsa-aplay and every other ALSA client on the machine, not
        # just Sentinel. Moving it aside restores the plain hardware
        # default: mixing and EQ stop, but sound comes back.
        mv -f "$ASOUND" "$ASOUND.broken" 2>/dev/null
        fixed_msg "moved an unopenable $ASOUND to $ASOUND.broken - audio falls back to the card directly (no mixing/EQ)"
        w "  re-enable mixing later with: $SETUP_MIX $CARD off"
      fi
    fi
  fi
else
  # Without this PCM, music.py plays to plughw:<card>,0 directly (it no
  # longer falls back to ALSA's bare default, which on a multi-card Pi is
  # usually HDMI). Mixing music with voice announcements stays off until
  # the dmix setup succeeds, but music itself is audible.
  say "sentinel_music is not defined - music plays straight to card $CARD (no mixing)"
  if [[ -x "$SETUP_MIX" ]] && may_retry create && "$SETUP_MIX" "$CARD" off >/dev/null 2>&1 \
       && pcm_opens sentinel_music; then
    fixed_msg "created $ASOUND for card $CARD - music and voice can be mixed again"
    reset_retry create
  fi
fi

# sentinel_voice is written by the same sentinel-setup-audio-mixing.sh call
# as sentinel_music, into the same /etc/asound.conf, but it is its own PCM
# definition (a "type softvol" stage with its own "SentinelVoice" ALSA
# control, CLAUDE.md #31) - not just an alias for sentinel_music. Nothing
# above actually opens it: the block that repairs sentinel_music only ever
# tests sentinel_music, so a break isolated to the voice PCM (its softvol
# control failing to create/open even though the shared dmix slave and
# sentinel_music both work fine) was invisible here and to voice.py's own
# _mixing_ready() check, which just falls back to duck_for_voice() forever
# without anything ever repairing the actual cause. Test and repair it with
# the same "actually try it" discipline, independently and with its own
# backoff key so its retry schedule cannot borrow or donate wait time to
# the unrelated sentinel_music one.
if aplay -L 2>/dev/null | grep -qx 'sentinel_voice'; then
  if pcm_opens sentinel_voice; then
    ok "sentinel_voice opens and accepts audio"
    reset_retry voice_rewrite
  else
    w "sentinel_voice exists in asound.conf but will not open:"
    timeout 6 aplay -D sentinel_voice -f S16_LE -r 44100 -c 2 -d 1 /dev/zero 2>&1 \
      | sed 's/^/       /' >&2
    if pcm_opens "hw:$CARD,0"; then
      # The card and the shared dmix slave both work (or the sentinel_music
      # block above already proved so) - this PCM's own definition is what
      # is broken. Rewriting asound.conf recreates the "SentinelVoice"
      # control from scratch. EQ is written off here for the same reason as
      # the sentinel_music repair above: modules/music.py re-applies the
      # user's bands on the next track, and a working mix beats a silent
      # one with EQ intact.
      if [[ -x "$SETUP_MIX" ]] && may_retry voice_rewrite && "$SETUP_MIX" "$CARD" off >/dev/null 2>&1 \
           && pcm_opens sentinel_voice; then
        fixed_msg "rewrote $ASOUND for card $CARD - sentinel_voice opens again"
        reset_retry voice_rewrite
      else
        w "sentinel_voice still will not open - voice announcements will duck (stop) music instead of mixing with it"
      fi
    else
      w "hw:$CARD,0 will not open either - see the sentinel_music section above"
    fi
  fi
else
  say "sentinel_voice is not defined - voice announcements will duck (stop) music instead of mixing with it"
fi

(( FIXED )) && exit 10
exit 0
