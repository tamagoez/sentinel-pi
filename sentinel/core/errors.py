"""エラー収集.

各モジュールは通常どおり `log.warning(...)` `log.exception(...)` を呼ぶだけでよい。
ここで用意する `LogCaptureHandler` をルートロガーに 1 つ付けるだけで、
全モジュールの WARNING 以上がここに自動的に集まる。
モジュール側で `errors.record(...)` を個別に呼ぶ必要はない。

CLI (`scripts/sentinel-diagnose.sh`) と Web UI の両方から
`GET /api/errors` と `GET /api/diagnostics/bundle` 経由で参照する。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter, deque

_LOCK = threading.RLock()
_BUFFER: deque = deque(maxlen=800)
_FMT = logging.Formatter()


class LogCaptureHandler(logging.Handler):
    """WARNING 以上のログレコードをリングバッファへ複製する。"""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            detail = _FMT.formatException(record.exc_info) if record.exc_info else ""
        except Exception:
            detail = ""
        try:
            with _LOCK:
                _BUFFER.appendleft({
                    "t": record.created,
                    "level": record.levelname,
                    "source": record.name,
                    "message": record.getMessage(),
                    "detail": detail,
                })
        except Exception:
            pass


def recent(limit: int = 200, level: str | None = None, source: str | None = None) -> list[dict]:
    with _LOCK:
        items = list(_BUFFER)
    if level:
        items = [i for i in items if i["level"] == level.upper()]
    if source:
        items = [i for i in items if source.lower() in i["source"].lower()]
    return items[:limit]


def recent_count(seconds: float = 900.0, level: str | None = None) -> int:
    cutoff = time.time() - seconds
    with _LOCK:
        items = list(_BUFFER)
    if level:
        items = [i for i in items if i["level"] == level.upper()]
    return sum(1 for i in items if i["t"] >= cutoff)


def counts(limit: int = 20) -> dict[str, int]:
    with _LOCK:
        items = list(_BUFFER)
    return dict(Counter(i["source"] for i in items).most_common(limit))


def clear() -> None:
    with _LOCK:
        _BUFFER.clear()
