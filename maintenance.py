"""毎日 4 時の定時処理.

順序:
    1. カメラを停止し、音楽を止めて資源を空ける
    2. カメラごとのタイムラプスを作り、タイル状に統合する
       - 映像が 1 枚もないカメラの枠は NODATA で埋める
       - 全カメラに映像がない場合も NODATA 画面で動画生成を続行する
    3. アクセスログをテロップとして下部に重ねる
    4. 生成物を archive へ置き、再起動する

Pi 3B+ ではソフトウェアエンコードが重いため、まず h264_v4l2m2m
(ハードウェアエンコーダ) を試し、失敗したら libx264 の ultrafast に落とす。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from ..core import config
from . import camera, music, netlog, notify

log = logging.getLogger("sentinel.maintenance")

STATE = {
    "running": False,
    "stage": "",
    "progress": 0.0,
    "last_run": 0.0,
    "last_result": "",
    "last_output": "",
    "next_run": 0.0,
}

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
]


def _font_path() -> str | None:
    for p in _FONT_CANDIDATES:
        if Path(p).is_file():
            return p
    return None


def _has_drawtext() -> bool:
    """ffmpeg が drawtext (libfreetype) 付きでビルドされているか確認する。"""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                             capture_output=True, text=True, timeout=15).stdout
        return " drawtext " in out
    except Exception:
        return False


def _encoder_args() -> list[str]:
    """利用可能なエンコーダを返す。ハードウェアを優先する。"""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        out = ""
    if "h264_v4l2m2m" in out:
        return ["-c:v", "h264_v4l2m2m", "-b:v", "1500k", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-threads", "2", "-pix_fmt", "yuv420p"]


def _run(cmd: list[str], timeout: float = 3600) -> tuple[bool, str]:
    log.debug("実行: %s", " ".join(cmd))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "タイムアウト"
    except FileNotFoundError:
        return False, "ffmpeg が見つかりません"
    if p.returncode != 0:
        tail = (p.stderr or "").strip().splitlines()[-6:]
        return False, "\n".join(tail)
    return True, ""


# ---------------------------------------------------------------- 素材生成

def _captures_for(cid: str, day: str) -> list[Path]:
    folder = config.CAPTURE_ROOT / cid / day
    if not folder.is_dir():
        return []
    return sorted(folder.glob("*.jpg"))


def _make_nodata_clip(out: Path, label: str, seconds: float,
                      width: int, height: int) -> bool:
    """映像のないカメラ用のプレースホルダ動画。"""
    vf = []
    font = _font_path()
    if font and _has_drawtext():
        text = label.replace(":", r"\:").replace("'", "")
        vf.append(
            f"drawtext=fontfile={font}:text='{text}':fontcolor=0x606060:"
            f"fontsize={max(14, height // 10)}:x=(w-text_w)/2:y=(h-text_h)/2"
        )
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i",
           f"color=c=0x141618:s={width}x{height}:d={max(1.0, seconds)}:r={int(config.get('timelapse_fps'))}"]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += _encoder_args() + [str(out)]
    ok, err = _run(cmd, timeout=600)
    if not ok:
        log.warning("NODATA クリップの生成に失敗: %s", err)
    return ok


def _make_camera_clip(cid: str, day: str, out: Path, width: int, height: int,
                      target_seconds: float) -> bool:
    frames = _captures_for(cid, day)
    fps = int(config.get("timelapse_fps"))
    if not frames:
        return _make_nodata_clip(out, f"{cid}  NODATA", target_seconds, width, height)

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as f:
        listfile = Path(f.name)
        # 各フレームの表示時間を揃え、目標尺に近づける
        per = max(1.0 / fps, target_seconds / max(1, len(frames)))
        for p in frames:
            f.write(f"file '{p}'\nduration {per:.4f}\n")
        f.write(f"file '{frames[-1]}'\n")     # concat demuxer は最後の1枚を重複させる

    vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease," \
         f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x141618"
    font = _font_path()
    if font and _has_drawtext():
        vf += (f",drawtext=fontfile={font}:text='{cid}':fontcolor=0xB0B0B0:"
               f"fontsize={max(12, height // 14)}:x=8:y=6:"
               f"box=1:boxcolor=0x000000@0.45:boxborderw=4")

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", str(listfile),
           "-vf", vf, "-r", str(fps)] + _encoder_args() + [str(out)]
    ok, err = _run(cmd)
    listfile.unlink(missing_ok=True)
    if not ok:
        log.warning("カメラ %s のタイムラプス生成に失敗: %s", cid, err)
        return _make_nodata_clip(out, f"{cid}  ERROR", target_seconds, width, height)
    return True


def _tile(clips: list[Path], out: Path, width: int, height: int) -> bool:
    """複数クリップを横並び / グリッドに合成する。"""
    if len(clips) == 1:
        shutil.copy2(clips[0], out)
        return True
    n = len(clips)
    cols = 2 if n <= 4 else 3
    rows = (n + cols - 1) // cols
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for c in clips:
        cmd += ["-i", str(c)]
    # 足りないタイルは黒で埋める
    filters = []
    inputs = []
    for i in range(n):
        filters.append(f"[{i}:v]scale={width}:{height},setsar=1[v{i}]")
        inputs.append(f"[v{i}]")
    pad = cols * rows - n
    for j in range(pad):
        filters.append(f"color=c=0x141618:s={width}x{height}:d=1[p{j}]")
        inputs.append(f"[p{j}]")
    layout = "|".join(f"{(i % cols) * width}_{(i // cols) * height}"
                      for i in range(cols * rows))
    filters.append(f"{''.join(inputs)}xstack=inputs={cols * rows}:"
                   f"layout={layout}:fill=0x141618[out]")
    cmd += ["-filter_complex", ";".join(filters), "-map", "[out]"]
    cmd += _encoder_args() + [str(out)]
    ok, err = _run(cmd)
    if not ok:
        log.warning("タイル合成に失敗しました: %s。1台目のみ使用します。", err)
        shutil.copy2(clips[0], out)
    return True


def _overlay_ticker(src: Path, out: Path, lines: list[str]) -> bool:
    """アクセスログをテロップとして下部に流す。"""
    font = _font_path()
    if not lines or not font or not _has_drawtext():
        shutil.copy2(src, out)
        return True

    # ffmpeg の textfile を使い、長大なコマンドラインを避ける
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as f:
        textfile = Path(f.name)
        f.write("   ///   ".join(lines[:600]))

    band_h = 34
    vf = (f"drawbox=x=0:y=ih-{band_h}:w=iw:h={band_h}:color=0x000000@0.72:t=fill,"
          f"drawtext=fontfile={font}:textfile={textfile}:fontcolor=0xD8D8D8:"
          f"fontsize=18:y=h-{band_h}+8:x=w-mod(t*90\\,w+text_w):reload=0")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-i", str(src), "-vf", vf] + _encoder_args() + [str(out)]
    ok, err = _run(cmd)
    textfile.unlink(missing_ok=True)
    if not ok:
        log.warning("テロップの重畳に失敗しました: %s", err)
        shutil.copy2(src, out)
    return True


# ---------------------------------------------------------------- 本体

def _build(day: str) -> tuple[bool, str]:
    tile_w = int(config.get("timelapse_tile_width"))
    tile_h = int(tile_w * 3 / 4)
    outdir = config.ARCHIVE_ROOT / day
    outdir.mkdir(parents=True, exist_ok=True)

    cam_ids = sorted(camera.WORKERS.keys())
    if not cam_ids:
        # ワーカーが居なくてもキャプチャ済みのフォルダから拾う
        cam_ids = sorted(p.name for p in config.CAPTURE_ROOT.iterdir() if p.is_dir())
    if not cam_ids:
        cam_ids = ["camera"]

    counts = {cid: len(_captures_for(cid, day)) for cid in cam_ids}
    longest = max(counts.values()) if counts else 0
    fps = int(config.get("timelapse_fps"))
    target = max(8.0, longest / fps) if longest else 12.0

    with tempfile.TemporaryDirectory(prefix="sentinel-tl-") as tmpd:
        tmp = Path(tmpd)
        clips: list[Path] = []
        for i, cid in enumerate(cam_ids):
            STATE["stage"] = f"タイムラプス生成 ({cid})"
            STATE["progress"] = 0.1 + 0.5 * (i / max(1, len(cam_ids)))
            clip = tmp / f"{cid}.mp4"
            if _make_camera_clip(cid, day, clip, tile_w, tile_h, target):
                clips.append(clip)
        if not clips:
            STATE["stage"] = "NODATA 画面を生成中"
            clip = tmp / "nodata.mp4"
            _make_nodata_clip(clip, "NODATA", 12.0, tile_w, tile_h)
            clips = [clip]

        STATE["stage"] = "全カメラを統合中"
        STATE["progress"] = 0.65
        tiled = tmp / "tiled.mp4"
        _tile(clips, tiled, tile_w, tile_h)

        STATE["stage"] = "アクセスログのテロップを重畳中"
        STATE["progress"] = 0.85
        final = outdir / f"{day}_daily.mp4"
        lines = netlog.timeline_for_ticker(day) if config.get("ticker_enabled") else []
        _overlay_ticker(tiled, final, lines)

    if not final.is_file():
        return False, "動画が生成されませんでした"
    size_mb = final.stat().st_size / 1e6
    detail = " / ".join(f"{cid}: {n}枚" for cid, n in counts.items())
    return True, f"{final.name} ({size_mb:.1f} MB) — {detail}"


async def run_now(*, reboot: bool | None = None) -> dict:
    """定時処理を実行する。手動起動にも使える。"""
    if STATE["running"]:
        return {"ok": False, "message": "すでに実行中です"}
    STATE.update(running=True, stage="準備中", progress=0.0, last_result="")
    day = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d")
    started = time.time()
    notify.system_event("定時処理を開始しました", f"対象日: {day}", level="info")

    try:
        STATE["stage"] = "カメラと音楽を停止中"
        await asyncio.to_thread(music.PLAYER.persist, force=True)
        await asyncio.to_thread(music.PLAYER.stop, terminate=True, reason="maintenance")
        await asyncio.to_thread(camera.shutdown)
        await asyncio.sleep(1.0)

        ok, detail = await asyncio.to_thread(_build, day)
        STATE.update(last_result="成功" if ok else "失敗", last_output=detail,
                     progress=1.0, stage="完了", last_run=time.time())
        notify.system_event(
            "定時処理が完了しました" if ok else "定時処理が失敗しました",
            detail, level="good" if ok else "error",
            fields=[{"name": "所要時間", "value": f"{time.time() - started:.0f} 秒",
                     "inline": True}])
    except Exception as exc:
        log.exception("定時処理が失敗しました")
        STATE.update(last_result="例外", last_output=str(exc))
        notify.system_event("定時処理で例外が発生しました", str(exc), level="error")
        ok, detail = False, str(exc)
    finally:
        STATE["running"] = False

    do_reboot = config.get("reboot_after_maintenance") if reboot is None else reboot
    if do_reboot:
        notify.system_event("再起動します", level="warn")
        await asyncio.sleep(4)     # 通知が飛ぶのを待つ
        await asyncio.to_thread(_reboot)
    else:
        await asyncio.to_thread(camera.reconcile)

    return {"ok": ok, "message": detail}


def _reboot() -> None:
    for cmd in (["sudo", "-n", "/sbin/reboot"], ["systemctl", "reboot"],
                ["/sbin/reboot"]):
        try:
            subprocess.run(cmd, timeout=20)
            return
        except Exception:
            continue
    log.error("再起動コマンドを実行できませんでした。sudoers の設定を確認してください。")


def _next_time() -> float:
    now = datetime.now()
    target = now.replace(hour=int(config.get("maintenance_hour")),
                         minute=int(config.get("maintenance_minute")),
                         second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


async def loop() -> None:
    while True:
        if not config.get("maintenance_enabled"):
            STATE["next_run"] = 0.0
            await asyncio.sleep(60)
            continue
        nxt = _next_time()
        STATE["next_run"] = nxt
        wait = nxt - time.time()
        # 設定変更に追従できるよう、細切れに待つ
        while wait > 0:
            await asyncio.sleep(min(30.0, wait))
            new_next = _next_time()
            if abs(new_next - nxt) > 1:
                nxt = new_next
                STATE["next_run"] = nxt
            wait = nxt - time.time()
        await run_now()
        await asyncio.sleep(60)
