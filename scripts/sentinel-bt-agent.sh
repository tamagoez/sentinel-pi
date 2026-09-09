#!/usr/bin/env bash
# sentinel-bt-agent.sh
#
# Persistent Bluetooth pairing agent, run by sentinel-bt-agent.service.
#
# This used to be bluez-tools' standalone "bt-agent --capability=
# NoInputNoOutput" binary. Since Raspberry Pi OS Bullseye (bluez 5.55+)
# that binary has a known regression: a NoInputNoOutput agent registered
# through it no longer auto-confirms incoming pairing requests - it just
# sits waiting for a manual approval that never comes in a headless
# systemd service. iOS has no fallback for this: it just reports
# "Pairing Unsuccessful" once its own confirmation request times out.
# (https://github.com/RPi-Distro/repo/issues/291)
#
# bluetoothctl's own built-in agent does not have this regression - typing
# "agent NoInputNoOutput" + "default-agent" in an interactive bluetoothctl
# session has always worked. So instead of the separate bt-agent binary,
# this script drives bluetoothctl the same way an interactive session
# would, then keeps its stdin pipe open forever (via `sleep infinity`)
# so bluetoothctl never sees EOF and never exits - which would tear the
# agent registration down exactly like the one-shot `bluetoothctl <cmd>`
# invocations elsewhere in this project already do.
#
# This needs no extra package: bluetoothctl is part of bluez, already a
# hard dependency (CLAUDE.md "依存を増やさない").
set -uo pipefail

{
  printf 'agent NoInputNoOutput\n'
  printf 'default-agent\n'
  printf 'discoverable on\n'
  printf 'pairable on\n'
  exec sleep infinity
} | exec bluetoothctl
