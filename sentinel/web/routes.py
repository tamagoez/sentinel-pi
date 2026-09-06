"""HTTP / WebSocket ルーティング."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from ..core import config
from ..core import errors as errors_mod
from ..core.state import MODE
from ..core.supervisor import SUPERVISOR
from ..modules import bluetooth, camera, diagnostics, maintenance, music, netlog, notify, terminal, thermal

log = logging.getLogger("sentinel.web")
router = APIRouter()

COOKIE = "sentinel_session"
_SESSIONS: dict[str, float] = {}


# ---------------------------------------------------------------- 認証

def _issue() -> str:
    tok = secrets.token_urlsafe(32)
    _SESSIONS[tok] = time.time() + float(config.get("session_hours")) * 3600
    # 期限切れの掃除
    now = time.time()
    for k, exp in list(_SESSIONS.items()):
        if exp < now:
            _SESSIONS.pop(k, None)
    return tok


def _valid(token: str | None) -> bool:
    if not config.get("password"):
        return True
    if not token:
        return False
    exp = _SESSIONS.get(token)
    return bool(exp and exp > time.time())


def require(request: Request) -> None:
    if not _valid(request.cookies.get(COOKIE)):
        raise HTTPException(401, "認証が必要です")


def _ws_ok(ws: WebSocket) -> bool:
    return _valid(ws.cookies.get(COOKIE))


@router.post("/api/login")
async def login(request: Request):
    body = await request.json()
    pw = str(config.get("password") or "")
    if pw and not hmac.compare_digest(str(body.get("password") or ""), pw):
        await asyncio.sleep(1.0)      # 総当たりを遅くする
        raise HTTPException(401, "パスワードが違います")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, _issue(), httponly=True, samesite="lax",
                    max_age=int(float(config.get("session_hours")) * 3600))
    return resp


@router.post("/api/logout")
async def logout(request: Request):
    _SESSIONS.pop(request.cookies.get(COOKIE) or "", None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@router.get("/api/auth")
async def auth_state(request: Request):
    return {"required": bool(config.get("password")),
            "authenticated": _valid(request.cookies.get(COOKIE))}


# ---------------------------------------------------------------- 総合状態

def _overview() -> dict:
    return {
        "time": time.time(),
        "mode": MODE.snapshot(),
        "system": thermal.snapshot(),
        "cameras": camera.status(),
        "music": music.PLAYER.status(),
        "bluetooth": bluetooth.status(),
        "netlog": {"state": netlog.STATE, "summary": netlog.today_summary()[:10]},
        "notify": notify.STATE,
        "maintenance": maintenance.STATE,
        "tasks": SUPERVISOR.stats(),
        "terminal_sessions": len(terminal.SESSIONS),
        "errors": {
            "recent_count": errors_mod.recent_count(900),
            "error_count": errors_mod.recent_count(900, level="ERROR"),
        },
    }


@router.get("/api/overview")
async def overview(request: Request):
    require(request)
    return _overview()


@router.get("/api/metrics")
async def metrics(request: Request, minutes: int = Query(30, ge=1, le=60)):
    require(request)
    cutoff = time.time() - minutes * 60
    return {"points": [p for p in thermal.HISTORY if p["t"] >= cutoff]}


# ---------------------------------------------------------------- エラー / 診断

@router.get("/api/errors")
async def errors_view(request: Request, limit: int = Query(300, ge=1, le=800),
                      level: str = "", source: str = ""):
    require(request)
    return {"errors": errors_mod.recent(limit, level or None, source or None),
            "counts": errors_mod.counts()}


@router.post("/api/errors/clear")
async def errors_clear(request: Request):
    require(request)
    errors_mod.clear()
    return {"ok": True}


@router.get("/api/diagnostics/bundle")
async def diagnostics_bundle(request: Request):
    require(request)
    data = await asyncio.to_thread(diagnostics.build_bundle)
    return Response(content=data, media_type="application/gzip",
                    headers={"Content-Disposition":
                             f'attachment; filename="{diagnostics.filename()}"'})


# ---------------------------------------------------------------- 設定

@router.get("/api/config")
async def get_config(request: Request):
    require(request)
    return {"config": config.all_values(), "defaults": config.DEFAULTS}


@router.put("/api/config")
async def put_config(request: Request):
    require(request)
    patch = await request.json()
    changed = config.update(patch)
    if changed:
        log.info("設定を更新しました: %s", ", ".join(changed))
    # 即時反映が必要なもの
    if "music_volume" in changed:
        music.PLAYER.set_volume(int(changed["music_volume"]))
    if "music_shuffle" in changed or "music_shuffle_seed" in changed:
        music.PLAYER.scan()
    if any(k in changed for k in ("eco_idle_minutes", "temp_eco_c",
                                  "temp_critical_c", "temp_recover_c",
                                  "mode_override")):
        MODE.evaluate()
    restart_needed = any(k.startswith("cam_") or k in ("jpeg_quality", "motion_threshold",
                                                       "motion_area_ratio", "motion_interval",
                                                       "save_cooldown", "live_fps",
                                                       "normal_fps", "eco_fps")
                         for k in changed)
    if restart_needed:
        for w in camera.WORKERS.values():
            if not w.halted:
                w.start()
    return {"ok": True, "changed": changed, "config": config.all_values()}


# ---------------------------------------------------------------- モード

@router.post("/api/mode")
async def set_mode(request: Request):
    require(request)
    body = await request.json()
    value = str(body.get("mode") or "auto")
    if value not in ("auto", "normal", "eco"):
        raise HTTPException(400, "mode は auto / normal / eco のいずれかです")
    config.update({"mode_override": value})
    return {"ok": True, "mode": MODE.evaluate(), "snapshot": MODE.snapshot()}


# ---------------------------------------------------------------- カメラ

@router.post("/api/camera/{cid}/restart")
async def restart_camera(cid: str, request: Request):
    require(request)
    w = camera.WORKERS.get(cid)
    if w is None:
        raise HTTPException(404, "カメラが見つかりません")
    w.halted = False
    camera.rt(cid).joinpath("halt").unlink(missing_ok=True)
    await asyncio.to_thread(w.start)
    return {"ok": True}


@router.get("/api/camera/{cid}/snapshot")
async def snapshot(cid: str, request: Request):
    require(request)
    p = camera.rt(cid) / "latest.jpg"
    if not p.is_file():
        raise HTTPException(404, "フレームがありません")
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


def _mjpeg(cid: str):
    camera.bump_live(cid, 1)
    last = 0
    try:
        p = camera.rt(cid) / "latest.jpg"
        idle = 0
        while idle < 600:      # 30秒 無フレームなら切る
            try:
                mt = p.stat().st_mtime_ns
                if mt != last:
                    last = mt
                    data = p.read_bytes()
                    idle = 0
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                           + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
                else:
                    idle += 1
            except FileNotFoundError:
                idle += 1
            time.sleep(0.05)
    finally:
        camera.bump_live(cid, -1)


@router.get("/api/camera/{cid}/stream")
async def stream(cid: str, request: Request):
    require(request)
    if cid not in camera.WORKERS:
        raise HTTPException(404, "カメラが見つかりません")
    return StreamingResponse(_mjpeg(cid),
                             media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})


@router.get("/api/captures")
async def captures(request: Request, day: str = "", limit: int = Query(120, ge=1, le=500)):
    require(request)
    day = day or datetime.now().strftime("%Y-%m-%d")
    items = []
    for p in sorted(config.CAPTURE_ROOT.glob(f"*/{day}/*.jpg"),
                    key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        items.append({"camera": p.parent.parent.name, "day": day, "file": p.name,
                      "at": p.stat().st_mtime,
                      "url": f"/api/capture/{p.parent.parent.name}/{day}/{p.name}"})
    days = sorted({d.name for d in config.CAPTURE_ROOT.glob("*/*") if d.is_dir()},
                  reverse=True)[:60]
    return {"items": items, "days": days, "day": day}


@router.get("/api/capture/{cid}/{day}/{filename}")
async def capture_file(cid: str, day: str, filename: str, request: Request):
    require(request)
    p = (config.CAPTURE_ROOT / cid / day / filename).resolve()
    if not p.is_file() or config.CAPTURE_ROOT.resolve() not in p.parents:
        raise HTTPException(404, "見つかりません")
    return FileResponse(p, media_type="image/jpeg")


# ---------------------------------------------------------------- 音楽

@router.get("/api/music")
async def music_status(request: Request):
    require(request)
    return {"status": music.PLAYER.status(),
            "playlist": music.PLAYER.playlist(),
            "downloads": music.DOWNLOADS[:20]}


@router.post("/api/music/{action}")
async def music_action(action: str, request: Request):
    require(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    p = music.PLAYER
    if action == "play":
        await asyncio.to_thread(p.play, float(body.get("position") or p.position))
    elif action == "pause":
        p.pause()
    elif action == "stop":
        await asyncio.to_thread(p.stop, terminate=True, reason="manual")
    elif action == "next":
        await asyncio.to_thread(p.next)
    elif action == "prev":
        await asyncio.to_thread(p.prev)
    elif action == "seek":
        p.seek(float(body.get("position") or 0))
    elif action == "volume":
        p.set_volume(int(body.get("volume") or 0))
    elif action == "jump":
        await asyncio.to_thread(p.jump_to, int(body.get("index") or 0))
    elif action == "rescan":
        await asyncio.to_thread(p.scan)
    elif action == "reshuffle":
        config.update({"music_shuffle_seed": 0})
        await asyncio.to_thread(p.scan)
    elif action == "download":
        url = str(body.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, "URL が不正です")
        music.enqueue_download(url)
    else:
        raise HTTPException(400, "不明な操作です")
    return {"ok": True, "status": p.status()}


@router.delete("/api/music/track")
async def delete_track(request: Request, name: str = Query(...)):
    require(request)
    p = (config.MUSIC_DIR / name).resolve()
    if not p.is_file() or config.MUSIC_DIR.resolve() not in p.parents:
        raise HTTPException(404, "見つかりません")
    p.unlink()
    await asyncio.to_thread(music.PLAYER.scan)
    return {"ok": True}


# ---------------------------------------------------------------- Bluetooth

@router.post("/api/bluetooth/{action}")
async def bluetooth_action(action: str, request: Request):
    require(request)
    if action == "disconnect":
        await asyncio.to_thread(bluetooth.disconnect)
    elif action in ("pairable_on", "pairable_off"):
        await asyncio.to_thread(bluetooth.set_pairable, action == "pairable_on")
    else:
        raise HTTPException(400, "不明な操作です")
    return {"ok": True, "bluetooth": bluetooth.status()}


# ---------------------------------------------------------------- ネットログ

@router.get("/api/netlog")
async def netlog_view(request: Request, day: str = "", limit: int = Query(300, ge=1, le=2000)):
    require(request)
    if day:
        records = netlog.read_day(day)[-limit:]
        records.reverse()
    else:
        records = netlog.RECENT[:limit]
    days = sorted((p.stem for p in config.NETLOG_ROOT.glob("*.jsonl")), reverse=True)[:60]
    return {"records": records, "summary": netlog.today_summary(),
            "state": netlog.STATE, "days": days}


# ---------------------------------------------------------------- AdGuard プロキシ

_AG_HOP = {"content-encoding", "content-length", "transfer-encoding", "connection"}


@router.api_route("/adguard/{path:path}",
                  methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def adguard_proxy(path: str, request: Request):
    """AdGuard Home の Web UI を 8080 経由でのみ見せるためのリバースプロキシ.

    AdGuard 側は http.address を 127.0.0.1 に縛るため、LAN からもホットスポットからも
    直接は届かない。ここを通った場合のみ到達できる。
    """
    require(request)
    base = str(config.get("adguard_url") or "").rstrip("/")
    if not base:
        raise HTTPException(503, "AdGuard Home の URL が未設定です")
    qs = request.url.query
    url = f"{base}/{path}" + (f"?{qs}" if qs else "")
    body = await request.body()
    req = urllib.request.Request(url, data=body or None, method=request.method)
    for k, v in request.headers.items():
        if k.lower() in ("host", "cookie", "content-length", "connection"):
            continue
        req.add_header(k, v)
    for k, v in netlog._auth_header().items():
        req.add_header(k, v)
    try:
        resp = await asyncio.to_thread(
            lambda: urllib.request.urlopen(req, timeout=20))
    except urllib.error.HTTPError as exc:
        return Response(content=exc.read(), status_code=exc.code,
                        media_type=exc.headers.get("Content-Type", "text/plain"))
    except Exception as exc:
        raise HTTPException(502, f"AdGuard Home へ到達できません: {exc}")
    data = await asyncio.to_thread(resp.read)
    headers = {k: v for k, v in resp.headers.items() if k.lower() not in _AG_HOP}
    return Response(content=data, status_code=resp.status,
                    media_type=resp.headers.get("Content-Type"), headers=headers)


# ---------------------------------------------------------------- 定時処理

@router.get("/api/archive")
async def archive_list(request: Request):
    require(request)
    items = []
    for p in sorted(config.ARCHIVE_ROOT.glob("*/*.mp4"),
                    key=lambda x: x.stat().st_mtime, reverse=True)[:120]:
        items.append({"day": p.parent.name, "file": p.name,
                      "size_mb": round(p.stat().st_size / 1e6, 1),
                      "at": p.stat().st_mtime,
                      "url": f"/api/archive/{p.parent.name}/{p.name}"})
    return {"items": items, "state": maintenance.STATE}


@router.get("/api/archive/{day}/{filename}")
async def archive_file(day: str, filename: str, request: Request):
    require(request)
    p = (config.ARCHIVE_ROOT / day / filename).resolve()
    if not p.is_file() or config.ARCHIVE_ROOT.resolve() not in p.parents:
        raise HTTPException(404, "見つかりません")
    return FileResponse(p, media_type="video/mp4")


@router.post("/api/maintenance/run")
async def maintenance_run(request: Request):
    require(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    reboot = bool(body.get("reboot", False))
    asyncio.create_task(maintenance.run_now(reboot=reboot))
    return {"ok": True, "message": "定時処理を開始しました"}


@router.post("/api/system/reboot")
async def system_reboot(request: Request):
    require(request)
    notify.system_event("Web UI から再起動が要求されました", level="warn")

    async def go():
        await asyncio.sleep(2)
        await asyncio.to_thread(maintenance._reboot)

    asyncio.create_task(go())
    return {"ok": True}


# ---------------------------------------------------------------- ログ

@router.get("/api/logs")
async def logs(request: Request, lines: int = Query(200, ge=1, le=2000)):
    require(request)
    try:
        data = config.LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        data = []
    return {"lines": data[-lines:]}


# ---------------------------------------------------------------- WebSocket

@router.websocket("/ws/status")
async def ws_status(ws: WebSocket):
    if not _ws_ok(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        while True:
            await ws.send_text(json.dumps(_overview(), ensure_ascii=False, default=str))
            await asyncio.sleep(1.5)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        log.debug("status ws 終了", exc_info=True)


@router.websocket("/ws/terminal")
async def ws_terminal(ws: WebSocket):
    if not _ws_ok(ws):
        await ws.close(code=1008)
        return
    if not config.get("terminal_enabled"):
        await ws.close(code=1008)
        return
    await ws.accept()
    session = terminal.PtySession()
    try:
        await asyncio.to_thread(session.spawn)
    except Exception as exc:
        await ws.send_text(json.dumps({"type": "error", "data": str(exc)}))
        await ws.close()
        return

    async def send_out(text: str) -> None:
        await ws.send_text(json.dumps({"type": "out", "data": text}))

    pump = asyncio.create_task(terminal.pump(session, send_out))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("type") == "in":
                session.write(str(msg.get("data") or ""))
            elif msg.get("type") == "resize":
                session.resize(int(msg.get("cols") or 100), int(msg.get("rows") or 30))
    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("terminal ws 終了", exc_info=True)
    finally:
        pump.cancel()
        session.close()


# ---------------------------------------------------------------- 静的

@router.get("/")
async def index():
    return FileResponse(config.STATIC_DIR / "index.html")
