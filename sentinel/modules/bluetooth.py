"""Bluetooth スピーカー (A2DP シンク) 連携.

BlueALSA が BlueZ と ALSA の間を仲介し、bluealsa-aplay が受信音声を
ALSA デバイスへ流す。Sentinel 側の役割は「接続の検知」と「BGM の退避・復帰」。

接続検知は bluetoothctl の info 出力を定期的に読む方式のまま — 単なる
状態の読み取りにはこれで十分で、書き換える理由が無い。ペアリング要求への
応答 (SSP エージェント) だけは modules/bt_agent.py が dbus-next 経由で
D-Bus の Agent1 を実装している。bluetoothctl の対話セッションをテキスト
スクレイピングしていた旧実装は、bluetoothctl 自身がまだ D-Bus 接続を
確立し切る前にエージェント登録を試みて失敗することがあり (実機で
"Failed to register agent object" として確認)、この種のタイミング競合は
CLI の出力を読むだけでは確実に検出できないため、実際の D-Bus 呼び出しの
成否で判断できる実装に置き換えた (CLAUDE.md の Bluetooth ペアリング
刷新の節を参照)。
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


def local_name() -> str:
    """この Pi 自身 (ローカルアダプタ) の表示名。`bluetoothctl show` の
    "Alias:" 行 (未設定なら Name と同じ値になる)。"""
    out = _run(["bluetoothctl", "show"])
    m = re.search(r"^\s*Alias:\s*(.+)$", out, re.M)
    return m.group(1).strip() if m else ""


def set_local_name(name: str) -> tuple[bool, str]:
    """この Pi 自身の表示名を変更する。相手端末から見える名前で、
    set_alias() が変更する「相手端末側のエイリアス」とは別物。
    `bluetoothctl system-alias <name>` はローカルアダプタの Alias を
    BlueZ の永続設定 (/var/lib/bluetooth/<adapter>/settings) へ書き込む
    ため、reboot 後も残る。"""
    name = name.strip()
    if not name:
        return False, "名前を入力してください"
    if len(name) > 248 or "\n" in name:
        return False, "名前が不正です"
    r = _run(["bluetoothctl", "system-alias", name])
    if "not available" in r.lower() or "no default controller" in r.lower():
        return False, "コントローラが見つかりません"
    return True, "変更しました"


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


def _pcm_path(addr: str) -> str:
    """BlueALSA の PCM オブジェクトの D-Bus パス。この Pi はアダプタが
    hci0 の 1 つだけという前提を置いている — set_alias() の
    `/org/bluez/hci0/dev_...` と同じ前提 (このプロジェクト全体で単一
    アダプタ構成のみ対象、CLAUDE.md 冒頭のハードウェア表を参照)。"""
    return f"/org/bluealsa/hci0/dev_{addr.upper().replace(':', '_')}/a2dpsnk"


def _apply_volume(addr: str) -> None:
    """その端末向けに保存済みの音量があれば bluealsa 側の再生音量へ
    反映する。mpg123 の音量 (music_volume, ソフトウェア側のゲイン) とは
    別物 - Bluetooth から流れてくる音声は mpg123 を経由せず
    bluealsa-aplay が直接鳴らすため、bluealsa 側で音量を持つ必要がある。

    **共有ハードウェアレジスタ (numid=1) はもう操作しない。** 以前は
    `amixer -c <card> cset numid=1 <%>` で bcm2835 の "PCM Playback
    Volume" を直接書き換えていたが、これは音楽・音声アナウンス・この Pi
    の出力全体で共有される 1 つのレジスタで、Bluetooth の端末ごとの
    音量を変えるたびに他の音量まで意図せず動いてしまっていた
    (実際に報告された不具合、CLAUDE.md #31 と同種の混同)。`bluealsa-cli`
    (bluez-alsa-utils に同梱) はこの接続だけに閉じた音量コントロールを
    D-Bus 経由で公開しており、SoftVolume を明示的に有効化したうえで
    そちらへ直接書き込む — 他のどの音量にも触れない。A2DP の音量範囲は
    0-127 (bluealsa-cli(1) 参照)。"""
    vols = config.get("bt_device_volumes") or {}
    pct = vols.get(addr)
    if pct is None:
        return
    path = _pcm_path(addr)
    # SoftVolume が off (= 相手端末の AVRCP 絶対音量にまかせる) のままだと
    # このあとの volume 書き込みが効かないことがあるため、毎回明示的に
    # on にしてから書く。
    _run(["bluealsa-cli", "soft-volume", path, "on"])
    value = round(max(0, min(100, int(pct))) * 127 / 100)
    _run(["bluealsa-cli", "volume", path, str(value)])


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
