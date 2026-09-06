"""バックグラウンドタスクの監督.

各モジュールのループをここに登録すると、例外で落ちても指数バックオフで
自動再起動する。手放し運用のための中核。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

log = logging.getLogger("sentinel.supervisor")


class Supervisor:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._stats: dict[str, dict] = {}
        self._stopping = False

    def spawn(self, name: str, coro_factory: Callable[[], Awaitable[None]],
              *, restart: bool = True, max_backoff: float = 60.0) -> None:
        if name in self._tasks and not self._tasks[name].done():
            return
        self._stats.setdefault(name, {"restarts": 0, "errors": 0, "last_error": "",
                                      "started": time.time(), "alive": True})

        async def runner() -> None:
            backoff = 1.0
            while not self._stopping:
                self._stats[name]["alive"] = True
                self._stats[name]["started"] = time.time()
                try:
                    await coro_factory()
                    if not restart:
                        break
                    log.info("タスク %s が正常終了しました。再起動します。", name)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._stats[name]["errors"] += 1
                    self._stats[name]["last_error"] = f"{type(exc).__name__}: {exc}"
                    log.exception("タスク %s が例外で停止しました", name)
                    if not restart:
                        break
                self._stats[name]["alive"] = False
                if self._stopping:
                    break
                self._stats[name]["restarts"] += 1
                await asyncio.sleep(backoff)
                backoff = min(max_backoff, backoff * 2)

        self._tasks[name] = asyncio.create_task(runner(), name=name)

    def stats(self) -> dict[str, dict]:
        out = {}
        for name, st in self._stats.items():
            t = self._tasks.get(name)
            out[name] = dict(st, done=bool(t and t.done()),
                             uptime=round(time.time() - st["started"], 1))
        return out

    async def shutdown(self) -> None:
        self._stopping = True
        for t in self._tasks.values():
            t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()


SUPERVISOR = Supervisor()
