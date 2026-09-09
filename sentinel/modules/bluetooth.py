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
from ..core.state import NORMAL, MODE
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


def paired_devices() -> list[dict]:
    """ペアリング済み端末の一覧 (接続中かどうかは問わない)。エイリアス /
    音量の設定 UI に、今つながっていない端末も出せるようにするため。"""
    out = _run(["bluetoothctl", "devices"])
    devices = []
    for line in out.splitlines():
        m = re.match(r"Device\s+([0-9A-F:]{17})\s+(.*)", line.strip(), re.I)
        if m:
            devices.append({"addr": m.group(1), "name": m.group(2).strip()})
    return devices


def _card() -> str | None:
    out = _run(["aplay", "-l"])
    m = re.search(r"^card\s+(\d+)", out, re.M)
    return m.group(1) if m else None


def _apply_volume(addr: str) -> None:
    """その端末向けに保存済みの音量があれば ALSA の再生音量へ反映する。
    mpg123 の音量 (music_volume, ソフトウェア側のゲイン) とは別物 -
    Bluetooth から流れてくる音声は mpg123 を経由せず bluealsa-aplay が
    直接 ALSA へ書き込むため、ハードウェア側のミキサーを直接操作する
    必要がある。numid=1 は bcm2835 サウンドカードの "PCM Playback Volume"
    (numid=3 の出力ルート選択とは別のコントロール、sentinel-guardian.sh の
    check_audio() と対になる)。"""
    vols = config.get("bt_device_volumes") or {}
    pct = vols.get(addr)
    if pct is None:
        return
    card = _card()
    if card is None:
        return
    _run(["amixer", "-c", card, "cset", "numid=1", f"{int(pct)}%"])


def set_device_volume(addr: str, pct: int) -> None:
    pct = max(0, min(100, int(pct)))
    vols = dict(config.get("bt_device_volumes") or {})
    vols[addr] = pct
    config.update({"bt_device_volumes": vols})
    if STATE.get("connected") and STATE.get("device_addr") == addr:
        _apply_volume(addr)


def set_alias(addr: str, alias: str) -> tuple[bool, str]:
    """接続済み/ペアリング済み端末の表示名 (org.bluez.Device1.Alias) を
    変更する。bluetoothctl の対話コマンドにはこれを変更する手段が無く
    (device.alias はローカル側=この Pi 自身の名前を変えるだけ)、D-Bus の
    プロパティを直接書き換える必要がある。dbus-send は BlueZ 自体が
    D-Bus 無しには動作しない以上 bluez と一緒に必ず入っている
    (dbus-python のような新規依存の追加ではない)。"""
    alias = alias.strip()
    if not alias:
        return False, "名前を入力してください"
    if not re.fullmatch(r"[0-9A-Fa-f:]{17}", addr):
        return False, "MAC アドレスが不正です"
    path = f"/org/bluez/hci0/dev_{addr.upper().replace(':', '_')}"
    try:
        r = subprocess.run(
            ["dbus-send", "--system", "--print-reply", "--dest=org.bluez", path,
             "org.freedesktop.DBus.Properties.Set",
             "string:org.bluez.Device1", "string:Alias",
             f"variant:string:{alias}"],
            capture_output=True, text=True, timeout=5)
    except Exception as exc:
        return False, str(exc)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        return False, tail[-1] if tail else "変更に失敗しました"
    if STATE.get("device_addr") == addr:
        STATE["device_name"] = alias
    return True, "変更しました"


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
            await asyncio.to_thread(_apply_volume, addr)
        elif not connected and STATE["connected"]:
            log.info("Bluetooth 切断: %s — BGM を復帰させます", STATE["device_name"])
            STATE.update(connected=False, device_addr="", device_name="", since=0.0)
            await asyncio.to_thread(music.resume_from_bluetooth)

        # eco/critical では接続確認の間隔を延ばし、bluetoothctl の呼び出し
        # (プロセス起動を伴う) 頻度を落とす。BGM 自体エコでは全停止するため、
        # ここでの即応性を多少犠牲にしても実害は小さい。
        base = float(config.get("bt_poll_seconds"))
        await asyncio.sleep(base * (3 if MODE.mode != NORMAL else 1))
