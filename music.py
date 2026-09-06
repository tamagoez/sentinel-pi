"""常時 BGM 再生.

再生エンジンは mpg123 のリモート制御モード (-R) を使う。
理由: Pi 3B+ のアナログ出力では ffplay 系がアンダーランを起こしやすい一方、
mpg123 は ALSA へ直接書き込むため軽く、安定している。

mpg123 -R の主なコマンド:
    L  <path>   読み込んで再生開始
    LP <path>   読み込むが再生しない
    P           一時停止 / 解除のトグル
    S           停止 (ファイルを閉じる)
    J <n>s      n 秒の位置へジャンプ
    V <percent> 音量
    Q           終了
出力:
    @F <現在フレーム> <残りフレーム> <現在秒> <残り秒>
    @P <0|1|2>   0=停止 1=一時停止 2=再生中
    @E ...       エラー

再生位置は @F 行から取得し、メモリ上に保持する。
ディスクへの書き込みはモード遷移時と1時間毎のみに限定し、SD カードを守る。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import shutil
import subprocess
import threading
import time
from pathlib import Path

from ..core import config, state
from ..core.state import CRITICAL, ECO, MODE, NORMAL

log = logging.getLogger("sentinel.music")

AUDIO_EXT = {".mp3"}
_SCAN_EXT = {".mp3", ".m4a", ".opus", ".ogg", ".flac", ".wav", ".webm"}


class Player:
    """mpg123 の単一インスタンスを抱えるラッパー。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None

        self.tracks: list[Path] = []
        self.order: list[int] = []
        self.cursor: int = 0
        self.position: float = 0.0
        self.duration: float = 0.0
        self.playing: bool = False
        self.suspended_by: str = ""        # "eco" | "bluetooth" | "" (退避理由)
        self.last_error: str = ""
        self.seed: int = 0

        self._dirty = False
        self._last_persist = 0.0

    # -------------------------------------------------- ライブラリ

    def scan(self) -> None:
        with self._lock:
            found = sorted(p for p in config.MUSIC_DIR.rglob("*")
                           if p.is_file() and p.suffix.lower() in AUDIO_EXT)
            current = self.current_path()
            self.tracks = found
            self._rebuild_order(keep=current)

    def _rebuild_order(self, keep: Path | None = None) -> None:
        n = len(self.tracks)
        idx = list(range(n))
        if config.get("music_shuffle"):
            seed = int(config.get("music_shuffle_seed") or 0)
            if not seed:
                seed = random.randrange(1, 2 ** 31)
                config.update({"music_shuffle_seed": seed})
            self.seed = seed
            random.Random(seed).shuffle(idx)
        else:
            self.seed = 0
        self.order = idx
        if keep is not None and keep in self.tracks:
            t = self.tracks.index(keep)
            if t in self.order:
                self.cursor = self.order.index(t)
        self.cursor = min(self.cursor, max(0, n - 1))

    def current_path(self) -> Path | None:
        with self._lock:
            if not self.order or self.cursor >= len(self.order):
                return None
            i = self.order[self.cursor]
            if i >= len(self.tracks):
                return None
            return self.tracks[i]

    # -------------------------------------------------- プロセス

    def _spawn(self) -> bool:
        if shutil.which("mpg123") is None:
            self.last_error = "mpg123 がインストールされていません"
            log.error(self.last_error)
            return False
        cmd = ["mpg123", "-R", "--buffer", str(int(config.get("mpg123_buffer_kb")))]
        dev = str(config.get("alsa_device") or "").strip()
        if dev:
            cmd += ["-a", dev]
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except Exception as exc:
            self.last_error = f"mpg123 の起動に失敗: {exc}"
            log.exception(self.last_error)
            return False
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="mpg123-reader")
        self._reader.start()
        self._send(f"V {int(config.get('music_volume'))}")
        return True

    def _send(self, cmd: str) -> None:
        p = self.proc
        if p is None or p.poll() is not None or p.stdin is None:
            return
        try:
            p.stdin.write(cmd + "\n")
            p.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    def _read_loop(self) -> None:
        p = self.proc
        if p is None or p.stdout is None:
            return
        for line in p.stdout:
            line = line.strip()
            if line.startswith("@F "):
                parts = line.split()
                if len(parts) >= 5:
                    try:
                        cur = float(parts[3])
                        rem = float(parts[4])
                        with self._lock:
                            self.position = cur
                            self.duration = cur + rem
                            self.playing = True
                            self._dirty = True
                    except ValueError:
                        pass
            elif line.startswith("@P "):
                st = line.split()[-1]
                with self._lock:
                    if st == "0":
                        self.playing = False
                        finished = True
                    else:
                        self.playing = (st == "2")
                        finished = False
                if finished and not self.suspended_by:
                    # 曲が終わった -> 次へ
                    self._advance_and_play()
            elif line.startswith("@E"):
                with self._lock:
                    self.last_error = line[3:].strip()
                log.warning("mpg123 エラー: %s", line)

    # -------------------------------------------------- 操作

    def play(self, seek: float = 0.0) -> None:
        with self._lock:
            path = self.current_path()
            if path is None:
                self.last_error = "再生可能な曲がありません"
                return
            if self.proc is None or self.proc.poll() is not None:
                if not self._spawn():
                    return
            self.suspended_by = ""
            self.position = seek
        if seek > 0.5:
            self._send(f"LP {path}")
            time.sleep(0.15)
            self._send(f"J {int(seek)}s")
            self._send("P")          # LOADPAUSED 後の解除
        else:
            self._send(f"L {path}")
        log.info("再生開始: %s (%.0f秒から)", path.name, seek)

    def pause(self) -> None:
        self._send("P")

    def stop(self, *, terminate: bool = True, reason: str = "") -> None:
        """完全停止。terminate=True なら mpg123 プロセス自体を終了させる。

        エコモードではプロセスを落とすことで CPU・メモリ・ALSA デバイスを解放し、
        ディスク I/O も完全に止める。
        """
        with self._lock:
            self.suspended_by = reason
            self.playing = False
        self._send("S")
        if terminate and self.proc is not None:
            self._send("Q")
            try:
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        self.persist(force=True)
        log.info("再生停止 (理由: %s)", reason or "手動")

    def _advance_and_play(self, delta: int = 1) -> None:
        with self._lock:
            repeat = str(config.get("music_repeat"))
            if repeat == "one":
                pass
            elif not self.order:
                return
            else:
                nxt = self.cursor + delta
                if nxt >= len(self.order):
                    if repeat == "off":
                        self.playing = False
                        return
                    nxt = 0
                elif nxt < 0:
                    nxt = len(self.order) - 1
                self.cursor = nxt
            self._dirty = True
        self.play(0.0)

    def next(self) -> None:
        self._advance_and_play(1)

    def prev(self) -> None:
        self._advance_and_play(-1)

    def jump_to(self, index: int) -> None:
        with self._lock:
            if 0 <= index < len(self.order):
                self.cursor = index
                self._dirty = True
        self.play(0.0)

    def seek(self, seconds: float) -> None:
        self._send(f"J {int(max(0, seconds))}s")

    def set_volume(self, percent: int) -> None:
        percent = max(0, min(100, int(percent)))
        config.update({"music_volume": percent})
        self._send(f"V {percent}")

    # -------------------------------------------------- 永続化

    def persist(self, *, force: bool = False) -> None:
        """再生位置をディスクへ保存。既定では1時間に1回だけ書く。"""
        now = time.time()
        if not force and (not self._dirty or now - self._last_persist < 3600):
            return
        with self._lock:
            path = self.current_path()
            payload = {
                "music": {
                    "path": str(path) if path else "",
                    "position": round(self.position, 2),
                    "cursor": self.cursor,
                    "seed": self.seed,
                    "shuffle": bool(config.get("music_shuffle")),
                    "saved_at": now,
                }
            }
        existing = state.load_snapshot()
        existing.update(payload)
        state.save_snapshot(existing)
        self._last_persist = now
        self._dirty = False

    def restore(self) -> None:
        snap = state.load_snapshot().get("music") or {}
        path = snap.get("path")
        if not path:
            return
        p = Path(path)
        with self._lock:
            if p in self.tracks:
                i = self.tracks.index(p)
                if i in self.order:
                    self.cursor = self.order.index(i)
            self.position = float(snap.get("position") or 0.0)
        log.info("前回の再生位置を復元しました: %s (%.0f秒)", p.name, self.position)

    # -------------------------------------------------- 状態

    def status(self) -> dict:
        with self._lock:
            path = self.current_path()
            return {
                "playing": self.playing,
                "suspended_by": self.suspended_by,
                "track": path.name if path else "",
                "track_path": str(path) if path else "",
                "position": round(self.position, 1),
                "duration": round(self.duration, 1),
                "index": self.cursor,
                "total": len(self.order),
                "volume": int(config.get("music_volume")),
                "shuffle": bool(config.get("music_shuffle")),
                "repeat": str(config.get("music_repeat")),
                "seed": self.seed,
                "error": self.last_error,
                "alive": self.proc is not None and self.proc.poll() is None,
            }

    def playlist(self) -> list[dict]:
        with self._lock:
            out = []
            for pos, ti in enumerate(self.order):
                if ti < len(self.tracks):
                    t = self.tracks[ti]
                    out.append({"index": pos, "name": t.name,
                                "current": pos == self.cursor})
            return out


PLAYER = Player()


# ---------------------------------------------------------------- モード連動

def on_mode_change(new: str, old: str) -> None:
    if not config.get("music_enabled"):
        return
    if new in (ECO, CRITICAL):
        if PLAYER.playing or PLAYER.proc is not None:
            PLAYER.persist(force=True)
            PLAYER.stop(terminate=True, reason="eco")
    elif new == NORMAL and old in (ECO, CRITICAL):
        if PLAYER.suspended_by in ("eco", ""):
            if config.get("music_autoplay_on_presence"):
                PLAYER.play(PLAYER.position)


def suspend_for_bluetooth() -> None:
    """Bluetooth 接続時。現在位置を保存してから完全に手を引く。"""
    if PLAYER.proc is not None or PLAYER.playing:
        PLAYER.persist(force=True)
        PLAYER.stop(terminate=True, reason="bluetooth")


def resume_from_bluetooth() -> None:
    if PLAYER.suspended_by != "bluetooth":
        return
    if MODE.mode != NORMAL or not config.get("music_enabled"):
        PLAYER.suspended_by = ""
        return
    PLAYER.play(PLAYER.position)


# ---------------------------------------------------------------- yt-dlp

DOWNLOADS: list[dict] = []
_DL_QUEUE: "asyncio.Queue[str]" = asyncio.Queue()


def enqueue_download(url: str) -> dict:
    entry = {"url": url, "state": "queued", "title": "", "message": "",
             "at": time.time()}
    DOWNLOADS.insert(0, entry)
    del DOWNLOADS[50:]
    _DL_QUEUE.put_nowait(url)
    return entry


def _find_entry(url: str) -> dict | None:
    for e in DOWNLOADS:
        if e["url"] == url and e["state"] in ("queued", "running"):
            return e
    return None


def _run_ytdlp(url: str, entry: dict) -> None:
    """mp3 で取得する。mpg123 が扱えるのが mp3 のみのため形式を固定する。"""
    if shutil.which("yt-dlp") is None:
        entry.update(state="error", message="yt-dlp がインストールされていません")
        return
    cmd = [
        "yt-dlp", "--no-playlist", "--no-progress", "--newline",
        "-x", "--audio-format", "mp3", "--audio-quality", "0",
        "--embed-metadata", "--embed-thumbnail", "--convert-thumbnails", "jpg",
        "--no-overwrites", "--restrict-filenames",
        "-o", str(config.MUSIC_DIR / "%(uploader,artist)s - %(title)s.%(ext)s"),
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        entry.update(state="error", message="タイムアウトしました")
        return
    except Exception as exc:
        entry.update(state="error", message=str(exc))
        return
    if proc.returncode == 0:
        title = ""
        for line in proc.stdout.splitlines():
            if "Destination:" in line or "[ExtractAudio]" in line:
                title = Path(line.split(":")[-1].strip()).name
        entry.update(state="done", title=title or "完了", message="")
        PLAYER.scan()
    else:
        tail = (proc.stderr or proc.stdout).strip().splitlines()
        entry.update(state="error", message=tail[-1] if tail else "不明なエラー")


async def download_loop() -> None:
    while True:
        url = await _DL_QUEUE.get()
        entry = _find_entry(url) or {"url": url, "state": "running", "title": "",
                                     "message": "", "at": time.time()}
        # エコモード中と定時処理中はダウンロードを止める (CPU と I/O を空ける)
        while MODE.mode != NORMAL:
            entry["state"] = "waiting"
            entry["message"] = "通常モードへの復帰を待っています"
            await asyncio.sleep(20)
        entry.update(state="running", message="")
        await asyncio.to_thread(_run_ytdlp, url, entry)


# ---------------------------------------------------------------- ループ

async def loop() -> None:
    PLAYER.scan()
    PLAYER.restore()
    if config.get("music_enabled") and config.get("music_autoplay_on_presence"):
        if MODE.mode == NORMAL:
            await asyncio.to_thread(PLAYER.play, PLAYER.position)

    last_scan = time.time()
    while True:
        await asyncio.sleep(5)
        # mpg123 が落ちていたら復帰させる (通常モードかつ退避中でないとき)
        if (config.get("music_enabled") and MODE.mode == NORMAL
                and not PLAYER.suspended_by
                and (PLAYER.proc is None or PLAYER.proc.poll() is not None)
                and PLAYER.tracks):
            log.warning("mpg123 が停止していたため復帰させます")
            await asyncio.to_thread(PLAYER.play, PLAYER.position)

        PLAYER.persist()

        if time.time() - last_scan >= 300:
            last_scan = time.time()
            await asyncio.to_thread(PLAYER.scan)


def shutdown() -> None:
    PLAYER.persist(force=True)
    PLAYER.stop(terminate=True, reason="shutdown")
