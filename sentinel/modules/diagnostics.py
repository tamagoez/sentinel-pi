"""診断バンドルの生成.

稼働中プロセスのメモリ上の状態 (エラーバッファ、各モジュールの status())
を集めて tar.gz にする。CLI (`sentinel-diagnose.sh`) と Web UI の
「診断バンドルをダウンロード」の両方がこの関数を経由する。

プロセスを跨いだ情報は含めない (それは CLI 側が journalctl 等で別途集める)。
ここで作るのは「今動いているプロセスでなければ分からない情報」に限る。
"""

from __future__ import annotations

import io
import platform
import sys
import tarfile
import time
from datetime import datetime
from typing import Any

from ..core import config, errors
from ..core.state import MODE
from ..core.supervisor import SUPERVISOR
from . import bluetooth, camera, maintenance, music, netlog, notify, terminal, thermal


def _add_json(tar: tarfile.TarFile, name: str, obj: Any) -> None:
    import json
    data = json.dumps(obj, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(data))


def _add_text(tar: tarfile.TarFile, name: str, text: str) -> None:
    data = text.encode("utf-8", "replace")
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(data))


def build_bundle(log_tail_lines: int = 1500) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add_json(tar, "meta.json", {
            "generated_at": datetime.now().isoformat(),
            "python": sys.version,
            "platform": platform.platform(),
            "data_root": str(config.DATA_ROOT),
        })
        _add_json(tar, "config.json", config.all_values(hide_secrets=True))
        _add_json(tar, "errors.json", {
            "recent": errors.recent(500),
            "counts": errors.counts(),
        })
        _add_json(tar, "mode.json", MODE.snapshot())
        _add_json(tar, "system.json", thermal.snapshot())
        _add_json(tar, "system_history.json", thermal.HISTORY[-720:])
        _add_json(tar, "cameras.json", camera.status())
        _add_json(tar, "music.json", {
            "status": music.PLAYER.status(),
            "playlist_len": len(music.PLAYER.tracks),
            "downloads": music.DOWNLOADS[:30],
        })
        _add_json(tar, "bluetooth.json", bluetooth.status())
        _add_json(tar, "netlog.json", {
            "state": netlog.STATE,
            "today_summary": netlog.today_summary(),
        })
        _add_json(tar, "notify.json", notify.STATE)
        _add_json(tar, "maintenance.json", maintenance.STATE)
        _add_json(tar, "tasks.json", SUPERVISOR.stats())
        _add_json(tar, "terminal.json", {"active_sessions": len(terminal.SESSIONS)})

        try:
            lines = config.LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
            _add_text(tar, "sentinel.log.tail", "\n".join(lines[-log_tail_lines:]))
        except Exception as exc:
            _add_text(tar, "sentinel.log.tail", f"読み込み失敗: {exc}")

    return buf.getvalue()


def filename() -> str:
    return f"sentinel-diag-{datetime.now():%Y%m%d-%H%M%S}.tar.gz"
