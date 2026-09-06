"""Discord Webhook 通知.

Discord の Webhook は 1 本あたり「2 秒に 5 リクエスト」の制限があり、
失敗したリクエストも制限にカウントされる。そのため必ずキューを経由し、
429 応答時は X-RateLimit-Reset-After に従って待つ。

Webhook は 1 本に集約し、カメラの区別は embed の色と author で行う。
複数本に分けるより管理が容易で、レート制限の制御も一箇所で済む。

送信内容:
    - 動体検知の立ち上がりで即時通知 (同一カメラは最短間隔を設ける)
    - 一定時間 無検知が続いたら、その期間の統計を 1 通に集約
    - モード遷移、温度異常、端末セッションなどのシステムイベント
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime

from ..core import config
from ..core.state import CRITICAL, ECO, MODE, NORMAL

log = logging.getLogger("sentinel.notify")

_QUEUE: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=200)
_LOOP: asyncio.AbstractEventLoop | None = None

# カメラ ID -> 色 (安定した割り当てのため ID のハッシュから決める)
_PALETTE = [0x4C8DFF, 0x30B27B, 0xE0913C, 0xB86BD8, 0xD8566B, 0x39B5B5]

# 動体イベントの集計
_last_sent: dict[str, float] = {}
_window_counts: Counter = Counter()
_window_start: float = 0.0
_last_motion_any: float = 0.0
_window_open: bool = False

STATE = {"sent": 0, "failed": 0, "queued": 0, "last_error": "", "last_sent_at": 0.0}


def _color_for(cid: str) -> int:
    return _PALETTE[sum(cid.encode()) % len(_PALETTE)]


def _post(payload: dict) -> float:
    """送信して、次に待つべき秒数を返す。"""
    url = str(config.get("discord_webhook") or "").strip()
    if not url:
        return 0.0
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            remaining = resp.headers.get("X-RateLimit-Remaining")
            reset_after = resp.headers.get("X-RateLimit-Reset-After")
            STATE["sent"] += 1
            STATE["last_sent_at"] = time.time()
            STATE["last_error"] = ""
            if remaining is not None and reset_after is not None:
                try:
                    if int(remaining) <= 0:
                        return float(reset_after)
                except ValueError:
                    pass
            return 0.35
    except urllib.error.HTTPError as exc:
        STATE["failed"] += 1
        if exc.code == 429:
            try:
                body = json.loads(exc.read().decode())
                # retry_after はミリ秒で返ることがあるため両対応にする
                ra = float(body.get("retry_after", 1))
                return ra / 1000.0 if ra > 100 else ra
            except Exception:
                return 2.0
        if exc.code == 404:
            STATE["last_error"] = "Webhook が存在しません (404)。設定を確認してください。"
            log.error(STATE["last_error"])
            return 60.0
        STATE["last_error"] = f"HTTP {exc.code}"
        return 2.0
    except Exception as exc:
        STATE["failed"] += 1
        STATE["last_error"] = str(exc)
        return 5.0


def _enqueue(payload: dict) -> None:
    if not config.get("discord_webhook"):
        return
    try:
        _QUEUE.put_nowait(payload)
        STATE["queued"] = _QUEUE.qsize()
    except asyncio.QueueFull:
        log.warning("通知キューが満杯のため破棄しました")


def _enqueue_threadsafe(payload: dict) -> None:
    """別スレッド (カメラループなど) からの投入用。"""
    if _LOOP is None:
        return
    _LOOP.call_soon_threadsafe(_enqueue, payload)


def _embed(title: str, description: str = "", *, color: int = 0x8A8A8A,
           fields: list[dict] | None = None, author: str = "") -> dict:
    e: dict = {
        "title": title,
        "color": color,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "footer": {"text": "Sentinel"},
    }
    if description:
        e["description"] = description
    if fields:
        e["fields"] = fields
    if author:
        e["author"] = {"name": author}
    return {"embeds": [e]}


# ---------------------------------------------------------------- 公開 API

def system_event(title: str, description: str = "", *, level: str = "info",
                 fields: list[dict] | None = None) -> None:
    if not config.get("notify_system_events"):
        return
    color = {"info": 0x5B8DEF, "warn": 0xE0913C, "error": 0xD8566B,
             "good": 0x30B27B}.get(level, 0x8A8A8A)
    _enqueue(_embed(title, description, color=color, fields=fields))


def on_motion(camera_id: str, capture_path: str = "") -> None:
    """カメラモジュールから呼ばれる (別スレッド)。"""
    global _window_start, _last_motion_any, _window_open
    now = time.time()
    _last_motion_any = now
    _window_counts[camera_id] += 1
    if not _window_open:
        _window_open = True
        _window_start = now

    if not config.get("notify_motion"):
        return
    if now - _last_sent.get(camera_id, 0.0) < float(config.get("notify_min_interval")):
        return
    _last_sent[camera_id] = now

    fields = [
        {"name": "時刻", "value": datetime.now().strftime("%H:%M:%S"), "inline": True},
        {"name": "モード", "value": MODE.mode, "inline": True},
        {"name": "CPU温度", "value": f"{MODE.temperature:.1f}℃", "inline": True},
    ]
    payload = _embed("動体を検知しました", color=_color_for(camera_id),
                     fields=fields, author=camera_id)
    _enqueue_threadsafe(payload)


def on_terminal_session(event: str, info: dict) -> None:
    if event == "open":
        system_event("端末セッションが開始されました",
                     f"セッション {info.get('id')} (pid {info.get('pid')})",
                     level="warn")
    else:
        system_event("端末セッションが終了しました",
                     f"セッション {info.get('id')} — {info.get('duration')} 秒",
                     level="info")


def on_mode_change(new: str, old: str) -> None:
    snap = MODE.snapshot()
    level = {"critical": "error", "eco": "info", "normal": "good"}.get(new, "info")
    system_event(
        f"モードが {old} から {new} へ変わりました",
        snap.get("reason", ""),
        level=level,
        fields=[{"name": "CPU温度", "value": f"{snap['temperature_c']}℃", "inline": True}],
    )


# ---------------------------------------------------------------- ループ

async def sender_loop() -> None:
    global _LOOP
    _LOOP = asyncio.get_running_loop()
    while True:
        payload = await _QUEUE.get()
        STATE["queued"] = _QUEUE.qsize()
        wait = await asyncio.to_thread(_post, payload)
        if wait > 0:
            await asyncio.sleep(min(wait + 0.1, 120.0))


async def summary_loop() -> None:
    """無検知が続いたら、その期間の統計を 1 通にまとめて送る。"""
    global _window_open, _window_counts, _window_start
    while True:
        await asyncio.sleep(15)
        if not _window_open or not config.get("notify_motion"):
            continue
        quiet = time.time() - _last_motion_any
        if quiet < float(config.get("notify_summary_after")):
            continue

        total = sum(_window_counts.values())
        if total == 0:
            _window_open = False
            continue
        duration = _last_motion_any - _window_start
        fields = [{"name": cid, "value": f"{n} 回", "inline": True}
                  for cid, n in _window_counts.most_common(10)]
        fields.append({"name": "検知期間",
                       "value": f"{datetime.fromtimestamp(_window_start):%H:%M} 〜 "
                                f"{datetime.fromtimestamp(_last_motion_any):%H:%M} "
                                f"({duration / 60:.0f} 分)",
                       "inline": False})
        fields.append({"name": "静穏時間", "value": f"{quiet / 60:.0f} 分", "inline": True})
        fields.append({"name": "現在のモード", "value": MODE.mode, "inline": True})
        _enqueue(_embed(f"検知が落ち着きました — 合計 {total} 回",
                        color=0x6C7A89, fields=fields))
        _window_counts = Counter()
        _window_open = False
