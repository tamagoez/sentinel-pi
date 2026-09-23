"""Bluetooth 音声連携 (受信 = A2DP シンク / 送信 = A2DP ソース).

このモジュールは 2 つの向きを扱う。

- **受信 (既存)**: 電話・PC がこの Pi へ接続し、Pi のスピーカーで再生する。
  BlueALSA が `-p a2dp-sink` としてこれを受け、`bluealsa-aplay` が ALSA
  デバイスへ流す。`STATE` / `loop()` が接続を検知し、`music.py` の BGM を
  退避・復帰させる。
- **送信 (新設)**: BGM をこの Pi からヘッドホン/スピーカーへ Bluetooth 経由
  で流す。BlueALSA を `-p a2dp-source` でも動かし (systemd/sentinel-bluealsa.
  service)、`OUTPUT_STATE` / `output_loop()` が `bt_output_device` (設定
  タブ/音楽タブで選んだ MAC アドレス) への接続を維持し、`music.py` へ
  `set_bt_output()` で伝える。一度選んだ端末は、解除するまで自動で
  接続を試み続ける — 一時的に切れても Bluetooth 側の再接続を待つのではなく
  こちらから明示的に `bluetoothctl connect` を送り直す (電話側からの
  受信と違い、Pi 側が能動的に繋ぎに行く必要があるため)。

接続検知はどちらの向きも bluetoothctl の出力を定期的に読む方式 —
単なる状態の読み取りにはこれで十分で、書き換える理由が無い。

**ペアリング応答 (SSP エージェント) は現在 modules/bt_agent.py が
担っていない — main.py から spawn していない (機能停止中、詳細は
bt_agent.py の docstring)。** 新しい端末とのペアリングは、Web UI の
端末タブから `bluetoothctl` を対話的に実行して手動で行う。一度ペアリング
(`pair`)・信頼 (`trust`) してしまえば、以後の接続・再接続はこのモジュール
が自動で行う (受信は `loop()` の検知、送信は `output_loop()` の
`bluetoothctl connect` 再試行) — エージェントは初回ペアリングの確認応答
にしか関与しないため、機能停止の影響はそこに限られる。
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import time

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

# 送信側 (Pi -> ヘッドホン/スピーカー、BGM 出力) の状態。受信側の STATE と
# は完全に独立している — 別の役割・別の (potentially 別の) 端末を指すため。
OUTPUT_STATE = {
    "connected": False,
    "device_addr": "",
    "device_name": "",
    "connecting": False,
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


# ------------------------------------------------------- 送信 (BGM 出力)

def output_candidates() -> list[dict]:
    """出力先として選べる端末の一覧。ペアリング済みなら接続中でなくても
    出す — output_loop() がこちらから能動的に繋ぎに行くため、選んだ時点で
    繋がっている必要はない。paired_devices() をそのまま使う (受信側の
    設定 UI と同じ一覧の使い回し)。"""
    return paired_devices()


def output_status() -> dict:
    return dict(OUTPUT_STATE)


def _output_pcm_ready(addr: str) -> bool:
    """指定端末との A2DP ソース側 (Pi -> 端末) の PCM が実際に確立して
    いるかを、`bluealsa-cli list-pcms` の出力で確認する。`bluetoothctl` の
    "Connected: yes" は ACL 接続が繋がっているだけのことがあり、A2DP の
    プロファイルネゴシエーションが済んでいなければ mpg123 が
    `bluealsa:DEV=...,PROFILE=a2dp` を開いても失敗する — 実際に
    ストリーミングできる状態かどうかは、この PCM パスの有無でしか
    確実に判断できない (受信側の音量制御が同じ理由で bluealsa-cli 経由の
    D-Bus パスを使っているのと同じ考え方、#72 参照)。パスの末尾
    "a2dpsrc" は BlueALSA が A2DP ソース方向 (=この Pi が送る側) の
    トランスポートに付ける名前で、受信側の音量制御が使う "a2dpsnk"
    (BlueZ から見てこの Pi がシンク=受け手になる方向) と対になる。"""
    out = _run(["bluealsa-cli", "-q", "list-pcms"], timeout=5)
    needle = f"dev_{addr.upper().replace(':', '_')}/a2dpsrc"
    return needle in out


def set_output_device(addr: str) -> tuple[bool, str]:
    """BGM の出力先を設定する。addr="" で解除 (AUX へ戻す)。一度設定すると
    output_loop() が解除されるまで自動で接続を試み続ける。"""
    addr = addr.strip().upper()
    if addr and not re.fullmatch(r"[0-9A-F:]{17}", addr):
        return False, "MAC アドレスが不正です"
    config.update({"bt_output_device": addr})
    if not addr:
        # 解除。今まさにその端末へ繋がっているなら、電池を無駄に
        # 消費させないよう明示的に切断する (再接続を試み続けるのをやめる
        # だけでは、端末側は繋がったままになってしまう)。
        cur = OUTPUT_STATE.get("device_addr")
        if cur:
            _run(["bluetoothctl", "disconnect", cur])
        OUTPUT_STATE.update(connected=False, device_addr="", device_name="",
                            connecting=False, error="")
        music.set_bt_output(None)
        return True, "出力先を解除しました (AUX へ戻ります)"
    return True, "出力先を設定しました。接続を試みます"


async def output_loop() -> None:
    """bt_output_device が設定されている間、接続を維持する。

    受信側 (loop()) は電話側が繋ぎに来るのを待つだけでよいが、送信側は
    Pi が能動的に `bluetoothctl connect` を送らないと繋がらない (相手が
    ヘッドホンの電源を入れ直した・Pi 自身の bluetoothd が再起動した、等で
    切れた場合も含む)。接続に失敗し続ける場合は指数バックオフで再試行
    間隔を伸ばす (camera.py の破損フレーム再接続バックオフと同じ考え方、
    CLAUDE.md #19) — 電源が入っていない/範囲外の端末に何度も
    `connect` を送り続けて無駄にポーリングし続けないため。"""
    base = 10.0
    max_backoff = 120.0
    backoff = base
    last_attempt = 0.0
    while True:
        if not config.get("bt_enabled"):
            await asyncio.sleep(10)
            continue
        addr = str(config.get("bt_output_device") or "").strip().upper()
        if not addr:
            if OUTPUT_STATE["connected"] or OUTPUT_STATE["connecting"]:
                OUTPUT_STATE.update(connected=False, device_addr="", device_name="",
                                    connecting=False, error="")
                await asyncio.to_thread(music.set_bt_output, None)
            backoff = base
            await asyncio.sleep(5)
            continue

        ready = await asyncio.to_thread(_output_pcm_ready, addr)
        if ready and not OUTPUT_STATE["connected"]:
            name = addr
            for d in await asyncio.to_thread(paired_devices):
                if d["addr"] == addr:
                    name = d["name"] or addr
                    break
            OUTPUT_STATE.update(connected=True, device_addr=addr, device_name=name,
                                connecting=False, error="")
            backoff = base
            log.info("Bluetooth 出力に接続しました: %s (%s)", name, addr)
            await asyncio.to_thread(music.set_bt_output, addr, name)
        elif not ready and OUTPUT_STATE["connected"] and OUTPUT_STATE["device_addr"] == addr:
            log.info("Bluetooth 出力が切断されました: %s — AUX へ戻します (再接続を試み続けます)",
                     OUTPUT_STATE["device_name"])
            OUTPUT_STATE.update(connected=False, device_name="", error="")
            await asyncio.to_thread(music.set_bt_output, None)

        if not ready:
            now = time.monotonic()
            if now - last_attempt >= backoff:
                last_attempt = now
                OUTPUT_STATE["connecting"] = True
                r = await asyncio.to_thread(_run, ["bluetoothctl", "connect", addr], 12.0)
                if "Connection successful" in r or "already connected" in r.lower():
                    backoff = base
                else:
                    OUTPUT_STATE["error"] = "接続できません (ペアリング済みか確認してください)"
                    backoff = min(max_backoff, backoff * 2)
                OUTPUT_STATE["connecting"] = False

        base_poll = float(config.get("bt_poll_seconds"))
        await asyncio.sleep(max(3.0, base_poll))


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
