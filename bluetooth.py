"""Bluetooth スピーカー (A2DP シンク) 連携.

BlueALSA が BlueZ と ALSA の間を仲介し、bluealsa-aplay が受信音声を
ALSA デバイスへ流す。Sentinel 側の役割は「接続の検知」と「BGM の退避・復帰」。

接続検知は bluetoothctl の info 出力を定期的に読む方式にする。
D-Bus を直接叩く方が上品だが、dbus-python への依存を増やさないこと、
そして bluetoothd の再起動をまたいでも壊れないことを優先した。
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess

from ..core import config
from . import music

log = logging.getLogger("sentinel.bluetooth")

STATE = {
    "available": False,
    "connected": False,
    "device_name": "",
    "device_addr": "",
    "since": 0.0,
    "error": "",
}


def _run(args: list[str], timeout: float = 5.0) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:
        return ""


def _connected_devices() -> list[tuple[str, str]]:
    """(MAC, 名前) の一覧。A2DP で接続中のものだけを返す。"""
    out = _run(["bluetoothctl", "devices", "Connected"])
    devices: list[tuple[str, str]] = []
    for line in out.splitlines():
        m = re.match(r"Device\s+([0-9A-F:]{17})\s+(.*)", line.strip(), re.I)
        if m:
            devices.append((m.group(1), m.group(2).strip()))
    if devices:
        return devices
    # 古い bluetoothctl には "devices Connected" がないため info で代替する
    out = _run(["bluetoothctl", "devices"])
    for line in out.splitlines():
        m = re.match(r"Device\s+([0-9A-F:]{17})\s+(.*)", line.strip(), re.I)
        if not m:
            continue
        info = _run(["bluetoothctl", "info", m.group(1)])
        if re.search(r"Connected:\s*yes", info, re.I):
            devices.append((m.group(1), m.group(2).strip()))
    return devices


def status() -> dict:
    return dict(STATE)


def disconnect(addr: str = "") -> bool:
    addr = addr or STATE.get("device_addr", "")
    if not addr:
        return False
    _run(["bluetoothctl", "disconnect", addr])
    return True


def set_pairable(on: bool) -> None:
    v = "on" if on else "off"
    for cmd in (["bluetoothctl", "discoverable", v],
                ["bluetoothctl", "pairable", v]):
        _run(cmd)


async def loop() -> None:
    if shutil.which("bluetoothctl") is None:
        STATE["error"] = "bluetoothctl が見つかりません"
        log.warning(STATE["error"])
        return
    STATE["available"] = True

    while True:
        if not config.get("bt_enabled"):
            await asyncio.sleep(10)
            continue
        try:
            devices = await asyncio.to_thread(_connected_devices)
        except Exception as exc:
            STATE["error"] = str(exc)
            devices = []

        connected = bool(devices)
        if connected and not STATE["connected"]:
            addr, name = devices[0]
            STATE.update(connected=True, device_addr=addr, device_name=name,
                         since=asyncio.get_event_loop().time(), error="")
            log.info("Bluetooth 接続: %s (%s) — BGM を退避します", name, addr)
            await asyncio.to_thread(music.suspend_for_bluetooth)
        elif not connected and STATE["connected"]:
            log.info("Bluetooth 切断: %s — BGM を復帰させます", STATE["device_name"])
            STATE.update(connected=False, device_addr="", device_name="", since=0.0)
            await asyncio.to_thread(music.resume_from_bluetooth)

        await asyncio.sleep(float(config.get("bt_poll_seconds")))
