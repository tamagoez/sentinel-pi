"""Sentinel 統合サーバー エントリポイント."""

from __future__ import annotations

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
from .core.state import MODE
from .core.supervisor import SUPERVISOR
from .modules import bluetooth, camera, maintenance, music, netlog, notify, terminal, thermal
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
    terminal.ON_SESSION = notify.on_terminal_session
    MODE.subscribe(camera.on_mode_change)
    MODE.subscribe(music.on_mode_change)
    MODE.subscribe(notify.on_mode_change)

    await _to_thread_safe(camera.reconcile)

    SUPERVISOR.spawn("thermal", thermal.loop)
    SUPERVISOR.spawn("camera", camera.loop)
    SUPERVISOR.spawn("music", music.loop)
    SUPERVISOR.spawn("music-download", music.download_loop)
    SUPERVISOR.spawn("bluetooth", bluetooth.loop)
    SUPERVISOR.spawn("netlog", netlog.loop)
    SUPERVISOR.spawn("notify-send", notify.sender_loop)
    SUPERVISOR.spawn("notify-summary", notify.summary_loop)
    SUPERVISOR.spawn("maintenance", maintenance.loop)

    notify.system_event("Sentinel が起動しました",
                        f"データ: {config.DATA_ROOT}", level="good")
    log.info("Sentinel の起動が完了しました (port %s)", config.get("port"))

    try:
        yield
    finally:
        log.info("Sentinel を停止します")
        notify.system_event("Sentinel を停止します", level="warn")
        await SUPERVISOR.shutdown()
        music.shutdown()
        camera.shutdown()
        terminal.shutdown()
        log.info("停止処理が完了しました")


async def _to_thread_safe(fn):
    import asyncio
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
