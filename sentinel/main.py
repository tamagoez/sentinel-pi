"""Sentinel 統合サーバー エントリポイント."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import multiprocessing
import os
import signal
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .core import config
from .core import errors as errors_mod
from .core import state
from .core.state import MODE
from .core.supervisor import SUPERVISOR
from .modules import bluetooth, bt_agent, camera, maintenance, music, netlog, notify, terminal, thermal, voice
from .web import routes


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-20s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    # ログの肥大化で SD カードを埋めないようローテーションする
    fileh = logging.handlers.RotatingFileHandler(
        config.LOG_PATH, maxBytes=4 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fileh.setFormatter(fmt)
    root.addHandler(fileh)

    # WARNING 以上を自動でリングバッファへ複製する。
    # 各モジュールは log.warning / log.exception を呼ぶだけでよく、
    # errors.record() を個別に呼ぶ必要はない。
    root.addHandler(errors_mod.LogCaptureHandler())

    for noisy in ("uvicorn.access", "uvicorn.error", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


log = logging.getLogger("sentinel")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("=" * 62)
    log.info("Sentinel を起動します (uid=%s, data=%s)", os.getuid(), config.DATA_ROOT)

    # モジュール間のフックを接続する
    camera.ON_MOTION = notify.on_motion
    camera.ON_CORRUPT_REBOOT = _on_corrupt_reboot
    terminal.ON_SESSION = notify.on_terminal_session
    MODE.subscribe(camera.on_mode_change)
    MODE.subscribe(music.on_mode_change)
    MODE.subscribe(notify.on_mode_change)
    await _to_thread_safe(state.sync_governor)

    await _to_thread_safe(camera.reconcile)

    SUPERVISOR.spawn("thermal", thermal.loop)
    SUPERVISOR.spawn("camera", camera.loop)
    SUPERVISOR.spawn("music", music.loop)
    SUPERVISOR.spawn("music-download", music.download_loop)
    SUPERVISOR.spawn("bluetooth", bluetooth.loop)
    SUPERVISOR.spawn("bt-agent", bt_agent.loop)
    SUPERVISOR.spawn("netlog", netlog.loop)
    SUPERVISOR.spawn("notify-send", notify.sender_loop)
    SUPERVISOR.spawn("notify-summary", notify.summary_loop)
    SUPERVISOR.spawn("maintenance", maintenance.loop)
    SUPERVISOR.spawn("voice", voice.loop)
    SUPERVISOR.spawn("voice-time-signal", voice.time_signal_loop)

    notify.system_event("Sentinel が起動しました",
                        f"データ: {config.DATA_ROOT}", level="good")
    voice.announce("Sentinel が起動しました", "other")
    log.info("Sentinel の起動が完了しました (port %s)", config.get("port"))

    try:
        yield
    finally:
        log.info("Sentinel を停止します")
        notify.system_event("Sentinel を停止します", level="warn")
        voice.announce("Sentinel を停止します", "other")
        await SUPERVISOR.shutdown()
        music.shutdown()
        camera.shutdown()
        terminal.shutdown()
        log.info("停止処理が完了しました")


# asyncio.create_task() が返す Task はイベントループから弱参照でしか
# 保持されない。どこにも強参照を残さずに create_task() の戻り値を捨てると、
# ガベージコレクタがタスクを実行途中で回収してしまうことがある (公式ドキュ
# メントが明記している既知の落とし穴)。_on_corrupt_reboot() は以前この
# 戻り値を捨てていたため、緊急再起動 (CLAUDE.md #22) が「要求はログに出る
# のに実際には Pi が再起動しない」という実害のある不具合になっていた —
# emergency_reboot() 自体が複数回 await asyncio.sleep() を挟む (通知が
# 飛ぶのを待つ、など) ため、GC に回収される猶予が十分にあった。
# **この参照を外して `asyncio.create_task(...)` の戻り値を捨てる実装に
# 戻さないでください** — 同じ「要求だけ出て実際には再起動されない」
# 不具合に戻ります。
_background_tasks: set[asyncio.Task] = set()


def _on_corrupt_reboot(cid: str, info: dict) -> None:
    """camera.ON_CORRUPT_REBOOT フック。破損フレームが繰り返しの再接続
    でも解消しないときに camera.py から呼ばれる (同期呼び出し、CLAUDE.md
    #22)。実際の停止・通知・再起動は maintenance.emergency_reboot() に
    任せ、ここではそれをバックグラウンドタスクとして起動するだけにする
    (フック自体は camera.loop() のループを止めてはいけないため)。"""
    reason = (f"再接続 {info.get('unresolved_reconnects')} 回でも解消せず"
             f" (device={info.get('device')}, corrupt_frames={info.get('corrupt_frames')},"
             f" corrupt_tolerated={info.get('corrupt_tolerated')}, reconnects={info.get('reconnects')})")
    task = asyncio.create_task(maintenance.emergency_reboot(cid, reason))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _to_thread_safe(fn):
    try:
        await asyncio.to_thread(fn)
    except Exception:
        log.exception("起動時処理に失敗しました: %s", getattr(fn, "__name__", fn))


def create_app() -> FastAPI:
    app = FastAPI(title="Sentinel", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.include_router(routes.router)
    if config.STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)),
                  name="static")
    return app


app = create_app()


def main() -> None:
    setup_logging()
    try:
        multiprocessing.set_start_method("fork")
    except RuntimeError:
        pass

    def on_term(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_term)

    uvicorn.run(app, host=str(config.get("host")), port=int(config.get("port")),
                access_log=False, log_config=None, timeout_graceful_shutdown=10)


if __name__ == "__main__":
    main()
