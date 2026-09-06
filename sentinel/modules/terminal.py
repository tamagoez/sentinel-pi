"""疑似 SSH 端末 (pty over WebSocket).

設計上の重要な決定:
    root 権限が必要なときは、利用者が端末の中で `su -` を実行する。
    Sentinel は root パスワードを受け取らず、保存もせず、検証もしない。
    認証は OS の PAM がそのまま担当するため、
      - このアプリの実装ミスでパスワードが漏れる経路が存在しない
      - 失敗回数の制限やログ記録は OS 側の仕組みがそのまま効く
    という利点がある。操作感は通常の SSH と変わらない。

端末セッションの開始と終了は Discord へ通知する (意図しないアクセスの検知用)。
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import signal
import struct
import termios
import time

from ..core import config

log = logging.getLogger("sentinel.terminal")

SESSIONS: dict[int, dict] = {}
_next_id = 0

ON_SESSION = None      # Callable[[str, dict], None]  event in {"open","close"}


class PtySession:
    def __init__(self, cols: int = 100, rows: int = 30) -> None:
        global _next_id
        _next_id += 1
        self.id = _next_id
        self.pid = -1
        self.fd = -1
        self.started = time.time()
        self.cols = cols
        self.rows = rows
        self.closed = False

    def spawn(self) -> None:
        shell = str(config.get("terminal_shell") or "/bin/bash")
        pid, fd = pty.fork()
        if pid == 0:
            # 子プロセス
            os.environ["TERM"] = "xterm-256color"
            os.environ["LANG"] = os.environ.get("LANG", "C.UTF-8")
            os.environ["SENTINEL_TERMINAL"] = "1"
            try:
                os.execvp(shell, [shell, "-l"])
            except Exception:
                os.execvp("/bin/sh", ["/bin/sh"])
            os._exit(1)
        self.pid = pid
        self.fd = fd
        os.set_blocking(fd, False)
        self.resize(self.cols, self.rows)
        SESSIONS[self.id] = {"id": self.id, "pid": pid, "started": self.started}
        log.info("端末セッション %s を開始しました (pid=%s)", self.id, pid)
        if ON_SESSION is not None and config.get("terminal_notify"):
            try:
                ON_SESSION("open", {"id": self.id, "pid": pid})
            except Exception:
                log.exception("端末通知に失敗しました")

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = max(20, cols), max(5, rows)
        if self.fd >= 0:
            try:
                fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                            struct.pack("HHHH", self.rows, self.cols, 0, 0))
            except Exception:
                pass

    def write(self, data: str) -> None:
        if self.fd >= 0:
            try:
                os.write(self.fd, data.encode("utf-8", "ignore"))
            except OSError:
                self.close()

    def read(self, size: int = 65536) -> bytes:
        try:
            return os.read(self.fd, size)
        except (BlockingIOError, InterruptedError):
            return b""
        except OSError:
            self.close()
            return b""

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        duration = time.time() - self.started
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1
        if self.pid > 0:
            for sig in (signal.SIGHUP, signal.SIGKILL):
                try:
                    os.kill(self.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    if os.waitpid(self.pid, os.WNOHANG)[0]:
                        break
                except ChildProcessError:
                    break
                time.sleep(0.1)
        SESSIONS.pop(self.id, None)
        log.info("端末セッション %s を終了しました (%.0f秒)", self.id, duration)
        if ON_SESSION is not None and config.get("terminal_notify"):
            try:
                ON_SESSION("close", {"id": self.id, "duration": round(duration)})
            except Exception:
                pass


async def pump(session: PtySession, send) -> None:
    """pty の出力を WebSocket へ流し続ける。"""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue()

    def on_readable() -> None:
        data = session.read()
        if data:
            queue.put_nowait(data)

    loop.add_reader(session.fd, on_readable)
    try:
        while not session.closed:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if session.pid > 0:
                    try:
                        pid, _ = os.waitpid(session.pid, os.WNOHANG)
                        if pid:
                            break
                    except ChildProcessError:
                        break
                continue
            await send(chunk.decode("utf-8", "replace"))
    finally:
        try:
            loop.remove_reader(session.fd)
        except Exception:
            pass


def shutdown() -> None:
    for info in list(SESSIONS.values()):
        try:
            os.kill(info["pid"], signal.SIGHUP)
        except Exception:
            pass
    SESSIONS.clear()
