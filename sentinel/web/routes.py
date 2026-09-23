"""HTTP / WebSocket ルーティング."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from ..core import config
from ..core import errors as errors_mod
from ..core.state import MODE
from ..core.supervisor import SUPERVISOR
from ..modules import bluetooth, bt_agent, camera, diagnostics, hotspot, maintenance, music, netlog, notify, terminal, thermal, voice

log = logging.getLogger("sentinel.web")
router = APIRouter()

COOKIE = "sentinel_session"
_SESSIONS: dict[str, float] = {}

# asyncio.create_task() が返す Task はイベントループから弱参照でしか保持
# されない (main.py の _background_tasks と同じ理由、CLAUDE.md #29)。この
# ルーターにも「HTTP ハンドラがレスポンスを返してすぐ戻る = 呼び出し元の
# スタックフレームがすぐ消える」ため強参照が一切残らない fire-and-forget
# な create_task() が複数あり、main.py 側の対策だけではここは救われない。
# 定時処理の手動起動 (maintenance_run) がこの穴に落ちると、実行中に GC が
# タスクを回収し、maintenance.run_now() の finally で STATE["running"] が
# False に戻る前に消える — 以後 STATE["running"] が True のまま固定され、
# 4時の定時ループ (maintenance.loop()) が呼ぶ run_now() も毎回「すでに
# 実行中です」で即座に空振りし続け、動画生成も (run_now() 内でしか呼ばれ
# ない) 再起動も二度と起こらなくなる。実機で報告された「定時処理の動画
# 生成が止まり、再起動もされない」症状と一致する。
# **この参照保持を外して create_task() の戻り値を再び捨てる実装に戻さ
# ないでください** — 同じ「定時処理が永久に固まる」不具合に戻ります。
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


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
        "bluetooth_output": bluetooth.output_status(),
        "bluetooth_pending": bt_agent.list_pending(),
        "netlog": {"state": netlog.STATE, "summary": netlog.today_summary()[:10]},
        "notify": notify.STATE,
        "voice": voice.STATE,
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


@router.get("/api/config/export")
async def export_config(request: Request):
    require(request)
    # バックアップ・復元用途のため、伏字にせず実際の値を返す (認証済みの
    # 操作者のみが到達できるエンドポイント)。
    data = config.all_values(hide_secrets=False)
    body = json.dumps(data, indent=2, ensure_ascii=False)
    fname = f"sentinel-config-{datetime.now():%Y%m%d-%H%M%S}.json"
    return Response(content=body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.post("/api/config/import")
async def import_config(request: Request):
    require(request)
    try:
        patch = await request.json()
    except Exception:
        raise HTTPException(400, "JSON として読み込めません")
    if not isinstance(patch, dict):
        raise HTTPException(400, "設定ファイルの形式が不正です")
    changed = config.update(patch)
    if changed:
        log.info("設定をインポートしました: %s", ", ".join(changed))
    return {"ok": True, "changed": changed, "config": config.all_values()}


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
    if any(k in changed for k in ("music_eq_enabled", "music_eq_bands", "music_eq_track_overrides")):
        await asyncio.to_thread(music.refresh_eq)
    if "alsa_device" in changed:
        # mpg123 の出力先は起動引数で決まるので、プロセスごと作り直す。
        await asyncio.to_thread(music.restart_playback)
    if any(k in changed for k in ("eco_idle_minutes", "temp_eco_c",
                                  "temp_critical_c", "temp_recover_c",
                                  "mode_override")):
        MODE.evaluate()
    restart_needed = any(
        k.startswith("cam_") or k.startswith("motion_")
        or k in ("jpeg_quality", "save_cooldown", "live_fps", "normal_fps", "eco_fps", "camera_overrides")
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


@router.get("/api/camera/{cid}/settings")
async def camera_get_settings(cid: str, request: Request):
    require(request)
    overrides = (config.get("camera_overrides") or {}).get(cid) or {}
    return {"keys": list(camera.CAMERA_OVERRIDE_KEYS), "overrides": overrides,
            "effective": camera.effective_settings(cid)}


@router.put("/api/camera/{cid}/settings")
async def camera_put_settings(cid: str, request: Request):
    """カメラ個別の設定を部分更新する。値が null のキーは共有設定に戻す
    (削除)。この端点は他のカメラには一切触れない - 「別々に撮影」と
    「まとめて撮影」を、カメラごとに選べるようにするためのもの。"""
    require(request)
    patch = await request.json()
    if not isinstance(patch, dict):
        raise HTTPException(400, "JSON オブジェクトが必要です")
    try:
        cur = await asyncio.to_thread(camera.set_overrides, cid, patch)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc))
    w = camera.WORKERS.get(cid)
    if w is not None and not w.halted:
        await asyncio.to_thread(w.start)
    return {"ok": True, "overrides": cur}


@router.get("/api/motion/debug_log")
async def motion_debug_log(request: Request):
    """全カメラ分の動体判定の診断ログをカメラごとに見出しをつけて
    1本のテキストにまとめて返す。設定タブの「診断ログを取得」からコピーし、
    次回のやり取りに貼り付けて調整に使う想定。"""
    require(request)
    parts = []
    for cid in sorted(camera.WORKERS.keys()):
        text = await asyncio.to_thread(camera.read_motion_debug_log, cid)
        if text.strip():
            parts.append(f"==== {cid} ====\n{text.strip()}")
    return {"text": "\n\n".join(parts)}


@router.get("/api/camera/{cid}/snapshot")
async def snapshot(cid: str, request: Request):
    require(request)
    p = camera.rt(cid) / "latest.jpg"
    # FileResponse は os.stat() で Content-Length を決めたあと、パス名を
    # 開き直して本文を送る。latest.jpg はカメラワーカーが毎フレーム
    # os.replace() で差し替えているため、この 2 段階の間に差し替わると
    # stat 時のサイズと実際に送るバイト数がずれ、uvicorn 側で
    # "Response content shorter/longer than Content-Length" になる。
    # read_bytes() の 1 回読みなら os.replace() の原子性により古い版・
    # 新しい版のどちらかを完全な形で読めるので、そのバイト列から
    # Content-Length を計算する Response にする。
    try:
        data = await asyncio.to_thread(p.read_bytes)
    except FileNotFoundError:
        raise HTTPException(404, "フレームがありません")
    return Response(content=data, media_type="image/jpeg",
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


@router.delete("/api/capture/{cid}/{day}/{filename}")
async def capture_delete(cid: str, day: str, filename: str, request: Request):
    require(request)
    p = (config.CAPTURE_ROOT / cid / day / filename).resolve()
    if not p.is_file() or config.CAPTURE_ROOT.resolve() not in p.parents:
        raise HTTPException(404, "見つかりません")
    p.unlink()
    return {"ok": True}


@router.delete("/api/captures")
async def captures_delete_day(request: Request, day: str = Query(...), camera: str = ""):
    """1 日分のイベント (静止画) をまとめて消す。camera を指定すると
    そのカメラだけに絞る。"""
    require(request)
    removed = 0
    targets = [config.CAPTURE_ROOT / camera / day] if camera else \
        list(config.CAPTURE_ROOT.glob(f"*/{day}"))
    for d in targets:
        d = d.resolve()
        if not d.is_dir() or config.CAPTURE_ROOT.resolve() not in d.parents:
            continue
        for f in d.glob("*.jpg"):
            f.unlink()
            removed += 1
        try:
            d.rmdir()
        except OSError:
            pass    # 他に何か残っていれば無理に消さない
    return {"ok": True, "removed": removed}


# ---------------------------------------------------------------- 音楽

@router.get("/api/music")
async def music_status(request: Request):
    require(request)
    return {"status": music.PLAYER.status(),
            "playlist": music.PLAYER.playlist(),
            "downloads": music.DOWNLOADS[:20]}


@router.get("/api/music/eq")
async def music_eq_get(request: Request):
    require(request)
    return {
        "bands_hz": music.EQ_BAND_HZ,
        "enabled": bool(config.get("music_eq_enabled")),
        "global": config.get("music_eq_bands") or {},
        "track_overrides": config.get("music_eq_track_overrides") or {},
        "current_track": (music.PLAYER.current_path().name
                          if music.PLAYER.current_path() else ""),
    }


@router.put("/api/music/eq/global")
async def music_eq_set_global(request: Request):
    require(request)
    patch = await request.json()
    if not isinstance(patch, dict):
        raise HTTPException(400, "バンドの指定が不正です")
    cur = await asyncio.to_thread(music.set_eq_bands, patch)
    return {"ok": True, "bands": cur}


@router.put("/api/music/eq/track/{name}")
async def music_eq_set_track(name: str, request: Request):
    require(request)
    patch = await request.json()
    if not isinstance(patch, dict):
        raise HTTPException(400, "バンドの指定が不正です")
    cur = await asyncio.to_thread(music.set_track_eq_bands, name, patch)
    return {"ok": True, "bands": cur}


@router.get("/api/music/eq/bt/{addr}")
async def music_eq_get_bt(addr: str, request: Request):
    """Bluetooth 出力機器ごとの EQ。AUX の /api/music/eq とは別領域 —
    出力先を切り替えても他方の設定に影響しない (music.py の
    _active_output_profile() 参照)。"""
    require(request)
    addr = addr.upper()
    profiles = config.get("bt_output_profiles") or {}
    p = profiles.get(addr) or {}
    return {
        "bands_hz": music.EQ_BAND_HZ,
        "enabled": bool(p.get("eq_enabled", False)),
        "bands": p.get("eq_bands") or {},
        "volume": int(p.get("volume", 60)),
    }


@router.put("/api/music/eq/bt/{addr}")
async def music_eq_set_bt(addr: str, request: Request):
    require(request)
    body = await request.json()
    bands = body.get("bands")
    enabled = body.get("enabled")
    if bands is not None and not isinstance(bands, dict):
        raise HTTPException(400, "バンドの指定が不正です")
    cur = await asyncio.to_thread(
        music.set_bt_output_eq, addr.upper(),
        enabled=(bool(enabled) if enabled is not None else None), bands=bands)
    return {"ok": True, "profile": cur}


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
        try:
            music.enqueue_download(url, str(body.get("category") or ""))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    elif action == "category_filter":
        # "" = フィルタなし (全曲)。存在しないフォルダ名を指定しても
        # scan() 側が静かに全曲へフォールバックするだけなので、ここでは
        # 検証せず素通しする。
        config.update({"music_category_filter": str(body.get("category") or "")})
        await asyncio.to_thread(p.scan)
    else:
        raise HTTPException(400, "不明な操作です")
    return {"ok": True, "status": p.status()}


@router.delete("/api/music/track")
async def delete_track(request: Request, name: str = Query(...)):
    require(request)
    # music.find_track_path() を使う理由: カテゴリー分け (CLAUDE.md #55)
    # 導入後、曲は MUSIC_DIR 直下とは限らずサブフォルダの中にあることが
    # ある。`config.MUSIC_DIR / name` を直接組み立てるだけでは、カテゴリー
    # 内の曲を「見つかりません」と誤って 404 にしてしまう。
    p = music.find_track_path(name)
    if p is None or not p.is_file() or config.MUSIC_DIR.resolve() not in p.resolve().parents:
        raise HTTPException(404, "見つかりません")
    p.unlink()
    await asyncio.to_thread(music.PLAYER.scan)
    return {"ok": True}


@router.put("/api/music/track/category")
async def move_track_category(request: Request):
    """曲をカテゴリー (MUSIC_DIR 直下のサブフォルダ) へ移動する。
    body: {"name": "曲名.mp3", "category": "勉強用"} — category は
    空文字列で「未分類」(MUSIC_DIR 直下) へ戻す。"""
    require(request)
    body = await request.json()
    name = str(body.get("name") or "")
    category = str(body.get("category") or "")
    if not name:
        raise HTTPException(400, "曲名が指定されていません")
    try:
        dest = await asyncio.to_thread(music.move_track, name, category)
    except FileNotFoundError:
        raise HTTPException(404, "見つかりません")
    except (ValueError, FileExistsError) as exc:
        raise HTTPException(400, str(exc))
    await asyncio.to_thread(music.PLAYER.scan)
    return {"ok": True, "path": str(dest.relative_to(config.MUSIC_DIR))}


@router.get("/api/music/categories")
async def music_categories(request: Request):
    require(request)
    return {"categories": music.list_categories()}


# ---------------------------------------------------------------- 通知

@router.post("/api/notify/test")
async def notify_test(request: Request):
    require(request)
    ok, message, diagnosis = await asyncio.to_thread(notify.send_test)
    return {"ok": ok, "message": message, "diagnosis": diagnosis}


@router.post("/api/notify/unblock-webhook-host")
async def notify_unblock_webhook_host(request: Request):
    require(request)
    ok, message = await asyncio.to_thread(notify.unblock_webhook_host)
    return {"ok": ok, "message": message}


# ---------------------------------------------------------------- 音声アナウンス

@router.post("/api/voice/test")
async def voice_test(request: Request):
    require(request)
    body = await request.json()
    text = str(body.get("text") or "").strip() or "音声アナウンスのテストです"
    ok, message = await asyncio.to_thread(voice.speak_test, text)
    return {"ok": ok, "message": message}


@router.post("/api/voice/test-time")
async def voice_test_time(request: Request):
    require(request)
    ok, message = await asyncio.to_thread(voice.speak_test_time)
    return {"ok": ok, "message": message}


# ---------------------------------------------------------------- Bluetooth

# 固定パスのルートは、"/{action}" のような可変パスのルートより必ず先に
# 登録すること。FastAPI/Starlette はルートを登録順に試すため、後から
# 登録すると "/api/bluetooth/alias" や ".../volume" も action="alias" 等
# として先に "/api/bluetooth/{action}" 側に食われてしまい、常に
# 「不明な操作です」になって新しいエンドポイントに一生届かない
# (実際に踏んだ不具合)。**この順序を入れ替えないでください。**

@router.get("/api/bluetooth/devices")
async def bluetooth_devices(request: Request):
    require(request)
    devices = await asyncio.to_thread(bluetooth.paired_devices)
    volumes = config.get("bt_device_volumes") or {}
    for d in devices:
        d["volume"] = volumes.get(d["addr"])
    return {"devices": devices}


@router.post("/api/bluetooth/alias")
async def bluetooth_alias(request: Request):
    require(request)
    body = await request.json()
    addr = str(body.get("addr") or "")
    alias = str(body.get("alias") or "")
    ok, message = await asyncio.to_thread(bluetooth.set_alias, addr, alias)
    return {"ok": ok, "message": message, "bluetooth": bluetooth.status()}


@router.post("/api/bluetooth/volume")
async def bluetooth_volume(request: Request):
    require(request)
    body = await request.json()
    addr = str(body.get("addr") or "")
    if not addr:
        raise HTTPException(400, "addr が必要です")
    volume = int(body.get("volume") or 0)
    await asyncio.to_thread(bluetooth.set_device_volume, addr, volume)
    return {"ok": True}


@router.get("/api/bluetooth/local_name")
async def bluetooth_local_name(request: Request):
    require(request)
    name = await asyncio.to_thread(bluetooth.local_name)
    return {"name": name}


@router.post("/api/bluetooth/local_name")
async def bluetooth_set_local_name(request: Request):
    require(request)
    body = await request.json()
    name = str(body.get("name") or "")
    ok, message = await asyncio.to_thread(bluetooth.set_local_name, name)
    return {"ok": ok, "message": message}


@router.get("/api/bluetooth/output")
async def bluetooth_output_get(request: Request):
    """BGM の出力先 (Pi -> ヘッドホン/スピーカー) の候補一覧と現在の状態。
    受信側の /api/bluetooth/devices とは別の向き — ペアリング済み端末の
    一覧を候補として使い回しているだけで、対象・状態は完全に独立。"""
    require(request)
    candidates = await asyncio.to_thread(bluetooth.output_candidates)
    return {"status": bluetooth.output_status(), "candidates": candidates,
            "selected": str(config.get("bt_output_device") or "")}


@router.post("/api/bluetooth/output")
async def bluetooth_output_set(request: Request):
    """出力先を設定/解除する。addr="" で解除 (AUX へ戻る)。設定すると
    bluetooth.output_loop() が解除されるまで自動で接続を試み続ける。"""
    require(request)
    body = await request.json()
    addr = str(body.get("addr") or "")
    ok, message = await asyncio.to_thread(bluetooth.set_output_device, addr)
    return {"ok": ok, "message": message, "status": bluetooth.output_status()}


@router.get("/api/bluetooth/pending")
async def bluetooth_pending(request: Request):
    """承認待ちのペアリング/接続要求一覧 (modules/bt_agent.py)。
    _overview() 経由の WebSocket 定期送信にも同じ内容が乗るため、Web UI
    はこのエンドポイントを追加でポーリングしなくてもよい — 個別に取得
    したい場面 (承認直後の即時再確認など) のために残してある。"""
    require(request)
    return {"pending": bt_agent.list_pending()}


@router.post("/api/bluetooth/pending/decide")
async def bluetooth_pending_decide(request: Request):
    """承認待ちの要求 1 件に対する人間の判断を反映する。対象が既に
    タイムアウト/別タブで決着済みなら ok=False を返す (エラーではなく、
    「もう手遅れでした」を示すだけ)。"""
    require(request)
    body = await request.json()
    req_id = str(body.get("id") or "")
    allow = bool(body.get("allow"))
    ok = bt_agent.decide(req_id, allow)
    return {"ok": ok, "pending": bt_agent.list_pending()}


@router.post("/api/bluetooth/remove")
async def bluetooth_remove(request: Request):
    """ペアリング済み端末を削除する。承認ゲートで誤って許可してしまった
    端末や、もう使わない端末を Web UI だけで取り消せるようにするため
    (CLAUDE.md #74 では bluetoothctl remove を手動で、としか案内できて
    いなかった)。"""
    require(request)
    body = await request.json()
    addr = str(body.get("addr") or "")
    ok, message = await asyncio.to_thread(bluetooth.remove_device, addr)
    return {"ok": ok, "message": message, "devices": await asyncio.to_thread(bluetooth.paired_devices)}


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


# ----------------------------------------------------------------- Hotspot

@router.get("/api/hotspot/ssid")
async def hotspot_get_ssid(request: Request):
    require(request)
    ssid = await asyncio.to_thread(hotspot.current_ssid)
    return {"ssid": ssid}


@router.post("/api/hotspot/ssid")
async def hotspot_set_ssid(request: Request):
    require(request)
    body = await request.json()
    ssid = str(body.get("ssid") or "")
    ok, message = await asyncio.to_thread(hotspot.set_ssid, ssid)
    return {"ok": ok, "message": message}


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


@router.delete("/api/netlog/{day}")
async def netlog_delete_day(day: str, request: Request):
    require(request)
    p = (config.NETLOG_ROOT / f"{day}.jsonl").resolve()
    if not p.is_file() or config.NETLOG_ROOT.resolve() not in p.parents:
        raise HTTPException(404, "見つかりません")
    p.unlink()
    return {"ok": True}


# ---------------------------------------------------------------- ファイル管理 (疑似 FTP)

# 「外部ストレージから普通のストレージまで」閲覧できるようにする一方、
# 本体の R/W は増やしたくない (SD カード保護)。そのため、フォルダサイズの
# 再帰計算・サムネイル生成・変更監視は一切行わず、要求されたディレクトリを
# os.scandir() で 1 回読むだけに留める。real FTP は実装しない (要件どおり)。
FILE_ROOTS = {
    "drive": config.EXTERNAL_STORAGE,   # 外部ストレージ全体 (マウントされていなければ使えない)
    "local": config.DATA_ROOT,          # sentinel のデータ本体
    # 本体ファイルシステム全体。web 端末 (/ws/terminal) が認証済みユーザーに
    # 素の `sentinel` ユーザー権限のシェルをすでに渡しているため、ここを
    # "/" に開放しても実質的な権限は増えない (シェルで到達できる場所と
    # 同じ範囲になるだけ)。
    "root": Path("/"),
}


def _fm_resolve(root: str, rel: str) -> Path:
    base = FILE_ROOTS.get(root)
    if base is None or not base.is_dir():
        raise HTTPException(404, "このストレージは利用できません")
    base = base.resolve()
    p = (base / (rel or "")).resolve()
    if p != base and base not in p.parents:
        raise HTTPException(400, "不正なパスです")
    return p


@router.get("/api/files/roots")
async def files_roots(request: Request):
    require(request)
    return {"roots": [{"key": k, "path": str(b)} for k, b in FILE_ROOTS.items() if b.is_dir()]}


_FM_MAX_ENTRIES = 2000   # "root" (本体全体) の追加で /proc や大きな
                        # パッケージディレクトリにも入れるようになった。
                        # 上限なしに全件返すと、閲覧している側のブラウザが
                        # 数万行の表を一度に描画することになり、実際に
                        # そこで重くなる/固まる原因になっていた。


@router.get("/api/files")
async def files_list(request: Request, root: str = Query(...), path: str = ""):
    require(request)
    p = _fm_resolve(root, path)
    if not p.is_dir():
        raise HTTPException(404, "ディレクトリが見つかりません")
    entries = []
    total = 0
    with os.scandir(p) as it:
        for e in it:
            try:
                is_dir = e.is_dir(follow_symlinks=False)
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            total += 1
            entries.append({"name": e.name, "is_dir": is_dir,
                            "size": None if is_dir else st.st_size,
                            "mtime": st.st_mtime})
    entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    truncated = len(entries) > _FM_MAX_ENTRIES
    entries = entries[:_FM_MAX_ENTRIES]
    return {"root": root, "path": path, "entries": entries,
            "total": total, "truncated": truncated}


@router.get("/api/files/download")
async def files_download(request: Request, root: str = Query(...), path: str = Query(...)):
    require(request)
    p = _fm_resolve(root, path)
    if not p.is_file():
        raise HTTPException(404, "ファイルが見つかりません")
    return FileResponse(p, filename=p.name)


@router.put("/api/files/upload")
async def files_upload(request: Request, root: str = Query(...), path: str = "",
                       name: str = Query(...)):
    require(request)
    p = _fm_resolve(root, path)
    if not p.is_dir():
        raise HTTPException(404, "ディレクトリが見つかりません")
    if not name or "/" in name or name in (".", ".."):
        raise HTTPException(400, "ファイル名が不正です")
    dest = p / name
    # multipart/form-data ではなく、リクエストボディをそのままファイルへ
    # ストリーム書き込みする単純な方式にしている (python-multipart 等の
    # 追加依存を避けるため - CLAUDE.md「依存を増やさない」)。1GB RAM 機
    # でも、ボディ全体をメモリに載せずに済む。
    try:
        with dest.open("wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return {"ok": True, "name": name}


_MAX_EDIT_BYTES = 2_000_000   # テキスト編集で書き戻せる上限 (誤って巨大/
                              # バイナリファイルを丸ごと書き換えさせないための保険)


@router.put("/api/files/content")
async def files_write_content(request: Request, root: str = Query(...), path: str = Query(...)):
    require(request)
    p = _fm_resolve(root, path)
    if p.is_dir():
        raise HTTPException(400, "フォルダには書き込めません")
    # ボディ全体を先に body() でメモリへ確保すると、上限チェックの前に
    # 巨大なファイルがまるごと RAM に載ってしまう (1GB 機での OOM リスク)。
    # ストリームで少しずつ受け取り、上限を超えた時点で即座に打ち切る。
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_EDIT_BYTES:
            raise HTTPException(413, f"{_MAX_EDIT_BYTES // 1_000_000}MB を超えるファイルはこの編集欄では保存できません")
        chunks.append(chunk)
    p.write_bytes(b"".join(chunks))
    return {"ok": True}


@router.post("/api/files/mkdir")
async def files_mkdir(request: Request):
    require(request)
    body = await request.json()
    p = _fm_resolve(str(body.get("root") or ""), str(body.get("path") or ""))
    name = str(body.get("name") or "").strip()
    if not name or "/" in name or name in (".", ".."):
        raise HTTPException(400, "フォルダ名が不正です")
    try:
        (p / name).mkdir()
    except FileExistsError:
        raise HTTPException(409, "その名前はすでに使われています")
    return {"ok": True}


@router.post("/api/files/rename")
async def files_rename(request: Request):
    require(request)
    body = await request.json()
    p = _fm_resolve(str(body.get("root") or ""), str(body.get("path") or ""))
    new_name = str(body.get("new_name") or "").strip()
    if not new_name or "/" in new_name or new_name in (".", ".."):
        raise HTTPException(400, "名前が不正です")
    if not p.exists():
        raise HTTPException(404, "見つかりません")
    dest = p.parent / new_name
    if dest.exists():
        raise HTTPException(409, "その名前はすでに使われています")
    p.rename(dest)
    return {"ok": True}


@router.delete("/api/files")
async def files_delete(request: Request, root: str = Query(...), path: str = Query(...),
                       recursive: bool = Query(False)):
    require(request)
    p = _fm_resolve(root, path)
    if not p.exists():
        raise HTTPException(404, "見つかりません")
    if p.is_dir():
        if recursive:
            await asyncio.to_thread(shutil.rmtree, p)
        else:
            try:
                p.rmdir()
            except OSError:
                raise HTTPException(400, "フォルダが空ではありません")
    else:
        p.unlink()
    return {"ok": True}


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


@router.delete("/api/archive/{day}/{filename}")
async def archive_delete(day: str, filename: str, request: Request):
    require(request)
    p = (config.ARCHIVE_ROOT / day / filename).resolve()
    if not p.is_file() or config.ARCHIVE_ROOT.resolve() not in p.parents:
        raise HTTPException(404, "見つかりません")
    p.unlink()
    try:
        p.parent.rmdir()
    except OSError:
        pass
    return {"ok": True}


@router.post("/api/maintenance/run")
async def maintenance_run(request: Request):
    require(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    reboot = bool(body.get("reboot", False))
    _spawn(maintenance.run_now(reboot=reboot))
    return {"ok": True, "message": "定時処理を開始しました"}


@router.post("/api/system/reboot")
async def system_reboot(request: Request):
    require(request)
    notify.system_event("Web UI から再起動が要求されました", level="warn")
    voice.announce("Web UIから再起動が要求されました", "other")

    async def go():
        await asyncio.sleep(2)
        await asyncio.to_thread(maintenance._reboot)

    _spawn(go())
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
            # eco/critical では、この定期送信自体 (_overview() の構築・
            # JSON 化・送信) を含めて頻度を落とす。「WebUI との同期」の一部:
            # ブラウザ側の再描画もそのぶん減る。
            await asyncio.sleep({"eco": 4.0, "critical": 6.0}.get(MODE.mode, 1.5))
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
