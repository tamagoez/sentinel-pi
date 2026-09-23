"""Bluetooth ペアリングエージェント (BlueZ Agent1, D-Bus 直接実装)。

**現在 main.py から spawn しています。** 過去 (CLAUDE.md #73/#74) は
実機で "Failed to register agent object" が収まらないとして機能停止して
いたが、実際に実機ログで確認できていたのは別のバグだった —
`Agent` クラスの `@method()` デコレータ付きメソッドに書いていた `-> None`
という戻り値注釈が、dbus-next 側で `ValueError: service annotations
must be a string constant (got None)` として**エージェントの登録
(`bus.export()`) より前に**例外落ちしていた。この例外は `loop()` の
`except Exception` に毎回捕まり、指数バックオフで再試行 → また同じ場所で
即座に例外、を繰り返していただけで、これは「登録がときどき失敗する
レース」ではなく「登録そのものが 100% 失敗し続けていた」ことを意味する。
CLAUDE.md #74 で戻り値注釈のバグ自体は修正済みだったが、その時点では
まだ機能停止のままだった。今回、この誤診断を踏まえて再度有効化した。
**このバグが再発していないか (=正常に "Agent registered" のログが出て
いるか) は、再度問題が起きた場合まずここを疑うこと。**

## Web UI からの承認ゲート (新設)

以前の実装は NoInputNoOutput 宣言のとおり全メソッドが無条件で承認して
いた (= 近くのどんな端末でも Just Works で確認なしにペアリングできて
しまう)。discoverable/pairable を既定オフ + 180 秒タイムアウトにしただけ
(CLAUDE.md #74) では、その 180 秒の窓が開いている間は結局「誰でも」
ペアリングできてしまい、利用者から「本質的な対策になっていない」という
指摘を受けた。

`RequestConfirmation`/`RequestAuthorization`/`AuthorizeService` の 3 つを
(SSP のペアリング確認・レガシー承認・信頼前のサービス許可要求) を、
即座に承認/拒否するのではなく `PENDING` という module-level の辞書へ
一旦積み、Web UI が `list_pending()`/`decide(id, allow)` 経由で人間の
判断を返すまで `asyncio.Future` で待機するようにした。Web UI が
`_APPROVAL_TIMEOUT` (既定 20 秒、bluetoothd 自身の SSP タイムアウトより
確実に短くする) 以内に応答しなければ、安全側 (=拒否) へ倒す — 誰も
見ていない/放置された要求を「タイムアウトしたから許可」にしてしまうと
無人運用の機体では実質オフと同じになるため。

これにより、たとえ discoverable/pairable の窓が開いていても、Web UI で
「許可」を押した接続だけが実際にペアリングされる。近くを通っただけの
無関係な端末が Just Works で勝手に繋がる、という #74 の根本原因に対する
実質的な対策になる。

**RequestPinCode/RequestPasskey は承認ゲートの対象外のまま。** これらは
レガシー PIN ペアリング (相手が KeyboardOnly/DisplayYesNo 等を要求する
場合) 向けで、NoInputNoOutput を宣言している以上 bluetoothd がこれらを
呼ぶことはまず無い。呼ばれた場合に備えて固定値 (`"0000"`/`0`) を返す
従来どおりの動作のままにしている — 実際に人間が入力すべき値を要求されて
いるので、承認/拒否の二択では意味を成さないため。

## Web UI 側の実装

`web/routes.py` の `/api/bluetooth/pending` (GET) が `list_pending()` を、
`/api/bluetooth/pending/decide` (POST) が `decide()` を呼ぶ。
`web/routes.py._overview()` にも `bluetooth_pending` として含めており、
既存の WebSocket 定期送信にそのまま乗るため、Web UI 側で追加のポーリング
を新設する必要はない — どのタブを開いていても、承認待ちが 1 件でも
あれば次の定期送信でモーダルが出る。

## 端末の管理

ペアリング済み端末の削除は `modules/bluetooth.py` の `remove_device()`
(`bluetoothctl remove <MAC>`) を新設し、Web UI の端末一覧に「削除」
ボタンを追加した。承認せずに放置した/誤って承認してしまった端末を、
Web UI だけで後から取り消せる。

## dbus-next が使えない場合

`import dbus_next` に失敗する環境では `loop()` が警告を出して即座に
戻る (何もしない) — 段階的劣化の方針どおり、他の機能は道連れにしない。
`list_pending()`/`decide()` はこの場合も呼び出し自体は安全 (`PENDING` が
常に空のまま) なので、Web UI 側で dbus-next の有無を意識する必要はない。
この場合、新しい端末とのペアリングには Web UI の端末タブから
`bluetoothctl` を対話的に実行する方法が使える (`bluetoothctl` 自身が
自分を agent として登録するため) — ただし承認ゲートは経由しない。

## 端末の自動 trust

BlueZ は Trusted な端末には Authorize service (プロファイル接続の
たびに毎回聞かれる、ペアリング確認とは別の許可要求) を一切聞かない
(CLAUDE.md 旧#16)。起動時に既知の端末をまとめて trust し、新しい端末が
現れた瞬間 (`InterfacesAdded` シグナル) にも trust することで、
ペアリング直後に接続がすぐ切れる問題を避ける。**この自動 trust は Web UI
での承認とは別物** — 承認はペアリングそのもの (RequestConfirmation 等)
の可否を決めるゲートで、trust はペアリングが完了した後、そのつど毎回
Authorize service を聞かれずに済むようにするための後続処理。承認して
いない端末が trust されることはない (RequestConfirmation で拒否すれば
ペアリング自体が失敗し、Device1 オブジェクトも InterfacesAdded として
現れない)。

## 依存

`dbus-next` (pure Python、asyncio ネイティブ) を使う。このプロジェクト
自身が既に asyncio ベース (FastAPI + `core.supervisor.SUPERVISOR`) で
あるため、同じイベントループに乗る実装にしている — `PENDING` の
`asyncio.Future` を `web/routes.py` のリクエストハンドラと直接共有できる
のもこのおかげ (uvicorn + SUPERVISOR は単一の asyncio イベントループ上で
動くため、スレッド間同期が一切不要)。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

log = logging.getLogger("sentinel.bt_agent")

AGENT_PATH = "/sentinel/agent"
BLUEZ_SERVICE = "org.bluez"

_RETRY_MIN = 5.0
_RETRY_MAX = 60.0

# ペアリング/接続の承認待ちリクエスト。Web UI がここを見て許可/拒否する。
# dbus-next が使えない環境でも import 自体は必ず成功する (この辞書と
# list_pending()/decide() は dbus_next に依存しない) ため、web/routes.py
# 側は常に安全にこれらを呼べる。
PENDING: dict[str, dict] = {}
_APPROVAL_TIMEOUT = 20.0  # bluetoothd 自身の SSP タイムアウトより十分短くする


def list_pending() -> list[dict]:
    """保留中の承認待ちリクエスト一覧 (Web UI 向け)。新しい順。"""
    now = time.time()
    out = [
        {
            "id": req_id,
            "addr": entry["addr"],
            "name": entry["name"],
            "kind": entry["kind"],
            "passkey": entry.get("passkey"),
            "age": round(now - entry["at"], 1),
        }
        for req_id, entry in PENDING.items()
    ]
    out.sort(key=lambda e: e["age"])
    return out


def decide(req_id: str, allow: bool) -> bool:
    """Web UI からの承認/拒否をリクエストへ反映する。対象が既に無ければ
    (タイムアウト済み/別タブで既に決着済みなど) False を返す。"""
    entry = PENDING.get(req_id)
    if entry is None or entry["future"].done():
        return False
    entry["future"].set_result(bool(allow))
    return True


def _mac_from_path(device_path: str) -> str:
    tail = device_path.rsplit("dev_", 1)[-1]
    return tail.replace("_", ":").upper()


async def _device_name(bus, device_path: str, mac: str) -> str:
    try:
        props = await _iface(bus, BLUEZ_SERVICE, device_path, "org.freedesktop.DBus.Properties")
        variant = await props.call_get("org.bluez.Device1", "Name")
        name = getattr(variant, "value", variant)
        if isinstance(name, str) and name.strip():
            return name.strip()
    except Exception:
        pass
    return mac


async def _await_approval(bus, device_path: str, kind: str, passkey=None) -> bool:
    """Web UI の承認を待つ。タイムアウト・Web UI 側からの拒否はどちらも
    「拒否」として扱う (安全側へ倒す) — 誰も操作しなかった要求を
    「時間切れだから許可」にしてしまうと、無人運用の機体では承認ゲート
    自体が実質無効化されてしまうため。承認された場合だけ True を返す。"""
    mac = _mac_from_path(device_path)
    name = await _device_name(bus, device_path, mac)
    req_id = uuid.uuid4().hex
    fut = asyncio.get_running_loop().create_future()
    PENDING[req_id] = {"addr": mac, "name": name, "kind": kind, "passkey": passkey,
                        "future": fut, "at": time.time()}
    log.info("Bluetooth ペアリング要求 (%s): %s (%s) — Web UI の承認待ちです", kind, name, mac)
    try:
        allowed = await asyncio.wait_for(fut, timeout=_APPROVAL_TIMEOUT)
    except asyncio.TimeoutError:
        allowed = False
        log.info("Bluetooth ペアリング要求がタイムアウトしました (未承認のため拒否): %s (%s)", name, mac)
    finally:
        PENDING.pop(req_id, None)
    log.info("Bluetooth ペアリング要求を%sしました: %s (%s)",
             "承認" if allowed else "拒否", name, mac)
    return allowed


async def loop() -> None:
    try:
        from dbus_next import DBusError
        from dbus_next.aio import MessageBus
        from dbus_next.constants import BusType
        from dbus_next.service import ServiceInterface, method
    except ImportError:
        log.warning("dbus-next が見つからないため Bluetooth ペアリングエージェントを"
                    "起動できません (.venv/bin/pip install dbus-next、update.sh を"
                    "再実行してください)。ペアリング時に承認プロンプトが出ません —"
                    "端末タブで bluetoothctl を対話的に実行してペアリングしてください。")
        return

    class Agent(ServiceInterface):
        """NoInputNoOutput (Just Works) capability を申告するエージェント。
        RequestConfirmation/RequestAuthorization/AuthorizeService は
        Web UI の承認待ち (_await_approval()) を経てから可否を返す —
        承認されなければ DBusError を投げて拒否する。それ以外のメソッドは
        例外を投げずに正常終了する = 常に承認する (org.bluez.Agent1 の
        仕様どおり)。"""

        def __init__(self, bus) -> None:
            super().__init__("org.bluez.Agent1")
            self._bus = bus

        # dbus-next の @method() は関数の型注釈をそのまま D-Bus シグネチャ
        # として解釈する ("o"/"s"/"u"/"q" などの文字列リテラルを使う、
        # dbus-next 自身の慣習)。**戻り値なしのメソッドに `-> None` を
        # 書かないこと** — dbus-next は戻り値注釈を「文字列定数のはず」
        # として処理するため、Python の None 型 (文字列ではない) を渡すと
        # `ValueError: service annotations must be a string constant
        # (got None)` でエージェント登録そのものが例外落ちする (この
        # ファイル冒頭の docstring を参照、実機で確認済み)。戻り値注釈を
        # 丸ごと省略すれば「出力引数なし」を意味し、この問題を避けられる。

        @method()
        def Release(self):
            pass

        @method()
        def RequestPinCode(self, device: "o") -> "s":  # noqa: F821 (dbus-next シグネチャ注釈)
            return "0000"

        @method()
        def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: F821
            pass

        @method()
        def RequestPasskey(self, device: "o") -> "u":  # noqa: F821
            return 0

        @method()
        def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: F821
            pass

        @method()
        async def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: F821
            if not await _await_approval(self._bus, device, "confirm", passkey=passkey):
                raise DBusError("org.bluez.Error.Rejected", "Sentinel Web UI で拒否/未承認のため")

        @method()
        async def RequestAuthorization(self, device: "o"):  # noqa: F821
            if not await _await_approval(self._bus, device, "authorize"):
                raise DBusError("org.bluez.Error.Rejected", "Sentinel Web UI で拒否/未承認のため")

        @method()
        async def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: F821
            if not await _await_approval(self._bus, device, "service"):
                raise DBusError("org.bluez.Error.Rejected", "Sentinel Web UI で拒否/未承認のため")

        @method()
        def Cancel(self):
            pass

    backoff = _RETRY_MIN
    while True:
        bus = None
        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            bus.export(AGENT_PATH, Agent(bus))

            manager = await _iface(bus, BLUEZ_SERVICE, "/org/bluez", "org.bluez.AgentManager1")
            await manager.call_register_agent(AGENT_PATH, "NoInputNoOutput")
            await manager.call_request_default_agent(AGENT_PATH)
            log.info("Bluetooth ペアリングエージェントを登録しました (承認ゲート有効)")
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
            # このエージェント接続にひもづいていた承認待ちは、もう応答が
            # 届く見込みが無いので即座に拒否として畳む (放置すると
            # Web UI にいつまでも古いモーダルが残り続ける)。
            for req_id, entry in list(PENDING.items()):
                if not entry["future"].done():
                    entry["future"].set_result(False)
                PENDING.pop(req_id, None)
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
