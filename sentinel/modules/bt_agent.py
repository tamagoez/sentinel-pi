"""Bluetooth ペアリングエージェント (BlueZ Agent1, D-Bus 直接実装).

これは `scripts/sentinel-bt-agent.sh` (bluetoothctl の対話セッションを
bash の coproc でテキストスクレイピングする実装) の置き換えです。

## なぜ bluetoothctl のテキストスクレイピングをやめたか

`bluetoothctl` は本来インタラクティブな CLI ツールであり、その標準出力を
読んで成否を判定する方式には構造的な弱点があります。実機の
`sentinel-logs` で実際にこの弱点が表面化しました:

```
agent NoInputNoOutput
Failed to register agent object
default-agent
No agent is registered
...(数秒後)...
Agent registered
```

`bluetoothctl` を coproc として起動した直後は、`bluetoothctl` 自身の
D-Bus 接続がまだ確立し切っていないことがあり、その状態で送った最初の
`agent NoInputNoOutput` が失敗します。この空白の間にペアリング要求が
届くと、確認を求められる agent がそもそも存在しないため、SSP の
ネゴシエーションが `0x05 (Authentication Failed)` として即座に失敗
します。リトライを重ねる自己修復 (`register_agent()`、CLAUDE.md #69)を
一度実装しましたが、これは「実際に D-Bus 呼び出しが成功したか」を
テキスト行の出現で推測しているだけで、根本的には同じ弱さを抱えたまま
でした。

## この実装の方針

`org.bluez.Agent1` を D-Bus オブジェクトとして直接エクスポートし、
`org.bluez.AgentManager1.RegisterAgent()` / `RequestDefaultAgent()` を
実際に呼び出します。これらは同期的な D-Bus メソッド呼び出しなので、
成功したかどうかはその場で例外の有無から確実に分かります —
「しばらく出力を監視して、それらしい行が来るまで待つ」という推測は
一切不要になります。

依存は `dbus-next` (pure Python、asyncio ネイティブ、zero-dependency) を
使います。`python-dbus` + PyGObject の GLib メインループを別プロセス/
別スレッドで回す従来の定石ではなく、このプロジェクト自身が既に
asyncio ベース (FastAPI + `core.supervisor.SUPERVISOR`) であるため、
同じイベントループに乗る asyncio ネイティブな実装の方が一貫性があり、
別スレッド・別プロセスとの同期を一切要しません。CLAUDE.md「依存を
増やさない」は今回のような構造的な不具合を D-Bus の外側から推測で
直そうとし続けることの方がずっとコストが高いと判断し、この 1 つの
軽量な pip 依存を許容しています。

## 端末の自動 trust

BlueZ は Trusted な端末には Authorize service (プロファイル接続の
たびに毎回聞かれる、ペアリング確認とは別の許可要求) を一切聞きません
(CLAUDE.md 旧#16)。起動時に既知の端末をまとめて trust し、新しい端末が
現れた瞬間 (`InterfacesAdded` シグナル) にも trust することで、
ペアリング直後に接続がすぐ切れる問題を避けます。
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("sentinel.bt_agent")

AGENT_PATH = "/sentinel/agent"
BLUEZ_SERVICE = "org.bluez"

_RETRY_MIN = 5.0
_RETRY_MAX = 60.0


async def loop() -> None:
    try:
        from dbus_next.aio import MessageBus
        from dbus_next.constants import BusType
        from dbus_next.service import ServiceInterface, method
    except ImportError:
        log.warning("dbus-next が見つからないため Bluetooth ペアリングエージェントを"
                    "起動できません (.venv/bin/pip install dbus-next、update.sh を"
                    "再実行してください)。ペアリング時に確認画面が出ても応答されません。")
        return

    class Agent(ServiceInterface):
        """NoInputNoOutput (Just Works) capability を申告するエージェント。
        すべてのメソッドは例外を投げずに正常終了する = 常に承認する、
        という意味になる (org.bluez.Agent1 の仕様どおり)。"""

        def __init__(self) -> None:
            super().__init__("org.bluez.Agent1")

        @method()
        def Release(self) -> None:
            pass

        @method()
        def RequestPinCode(self, device: "o") -> "s":  # noqa: F821 (dbus-next シグネチャ注釈)
            return "0000"

        @method()
        def DisplayPinCode(self, device: "o", pincode: "s") -> None:  # noqa: F821
            pass

        @method()
        def RequestPasskey(self, device: "o") -> "u":  # noqa: F821
            return 0

        @method()
        def DisplayPasskey(self, device: "o", passkey: "u", entered: "q") -> None:  # noqa: F821
            pass

        @method()
        def RequestConfirmation(self, device: "o", passkey: "u") -> None:  # noqa: F821
            pass

        @method()
        def RequestAuthorization(self, device: "o") -> None:  # noqa: F821
            pass

        @method()
        def AuthorizeService(self, device: "o", uuid: "s") -> None:  # noqa: F821
            pass

        @method()
        def Cancel(self) -> None:
            pass

    backoff = _RETRY_MIN
    while True:
        bus = None
        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            bus.export(AGENT_PATH, Agent())

            manager = await _iface(bus, BLUEZ_SERVICE, "/org/bluez", "org.bluez.AgentManager1")
            await manager.call_register_agent(AGENT_PATH, "NoInputNoOutput")
            await manager.call_request_default_agent(AGENT_PATH)
            log.info("Bluetooth ペアリングエージェントを登録しました")
            backoff = _RETRY_MIN

            await _trust_known_devices(bus)
            await _watch_new_devices(bus)  # 接続が切れるまで戻らない
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Bluetooth ペアリングエージェントが失敗しました。%.0f 秒後に"
                       "再試行します: %s", backoff, exc)
        finally:
            if bus is not None:
                try:
                    bus.disconnect()
                except Exception:
                    pass
        await asyncio.sleep(backoff)
        backoff = min(_RETRY_MAX, backoff * 2)


async def _iface(bus, service: str, path: str, name: str):
    introspection = await bus.introspect(service, path)
    obj = bus.get_proxy_object(service, path, introspection)
    return obj.get_interface(name)


async def _set_trusted(bus, path: str) -> None:
    from dbus_next import Variant

    try:
        props = await _iface(bus, BLUEZ_SERVICE, path, "org.freedesktop.DBus.Properties")
        await props.call_set("org.bluez.Device1", "Trusted", Variant("b", True))
    except Exception as exc:
        log.debug("端末 (%s) の trust に失敗しました: %s", path, exc)


async def _trust_known_devices(bus) -> None:
    """起動時、既にペアリング済みの端末を一括で trust し直す。この修正
    より前にペアリングして未信頼のまま固まっていた端末も次回起動で
    救済する (旧 sentinel-bt-agent.sh から引き継いだ挙動)。"""
    try:
        om = await _iface(bus, BLUEZ_SERVICE, "/", "org.freedesktop.DBus.ObjectManager")
        objects = await om.call_get_managed_objects()
    except Exception as exc:
        log.debug("既知端末の列挙に失敗しました: %s", exc)
        return
    for path, ifaces in objects.items():
        if "org.bluez.Device1" in ifaces:
            await _set_trusted(bus, path)


async def _watch_new_devices(bus) -> None:
    """新しい端末が現れた瞬間 (ペアリング直後など) に trust する。
    `InterfacesAdded` を購読したまま戻らない — `bus.wait_for_disconnect()`
    でバス自体の切断 (bluetoothd の再起動など) を明示的に待ち、切断され
    たら戻って呼び出し元 (loop()) の再接続・再登録ループに委ねる。
    `asyncio.sleep()` で待つだけの実装だと、切断されても次に何かを送信
    しようとするまで気付けない (最大で sleep の残り時間ぶん遅れる) ため、
    これは避けている。"""
    om = await _iface(bus, BLUEZ_SERVICE, "/", "org.freedesktop.DBus.ObjectManager")

    def on_interfaces_added(path, interfaces):
        if "org.bluez.Device1" in interfaces:
            asyncio.ensure_future(_set_trusted(bus, path))

    om.on_interfaces_added(on_interfaces_added)
    try:
        await bus.wait_for_disconnect()
    finally:
        try:
            om.off_interfaces_added(on_interfaces_added)
        except Exception:
            pass
