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


def _can_render_text() -> bool:
    """テキストを画像として描画できるか (フォント + Pillow) を確認する。

    以前は ffmpeg の drawtext フィルタ (libharfbuzz 依存) を使っていたが、
    Debian/Raspberry Pi OS の ffmpeg パッケージが harfbuzz 無効でビルドされて
    出荷される事例があり (https://bugs.debian.org/1056597)、しかもその修正が
    安定版リポジトリに永久に来ないことがある — つまり `apt upgrade` では
    直しようがない環境が実在する。テロップ機能を OS のパッケージングに
    左右されないようにするため、drawtext には一切依存しない。Pillow で
    PNG に描画し、ffmpeg には overlay/drawbox という常に存在するコア
    フィルタだけで重ねる。
    """
    try:
        import PIL  # noqa: F401
    except Exception:
        return False
    return _font_path() is not None


def _render_text_png(text: str, path: Path, *, font_size: int,
                     color: tuple[int, int, int, int],
                     canvas: tuple[int, int] | None = None,
                     height: int | None = None,
                     bg: tuple[int, int, int, int] = (0, 0, 0, 0),
                     pad: int = 0) -> bool:
    """テキストを PNG に描画する。canvas 指定時はその中央に、height のみ
    指定時は横幅を文字列に合わせつつ縦だけ固定してその中央に、どちらも
    無指定ならテキストぴったりのサイズ (+pad) で書き出す。

    height を渡すと、フォントの実際の字形が計算上の高さより外側にはみ出す
    場合でも、Pillow は画像のキャンバス外には一切描画しない (=そこで
    自動的に切り取られる) ため、出力 PNG がこの高さを超えることはない。
    ffmpeg の overlay に渡す帯 (drawbox) の高さぴったりに描画したい
    ケース向け - 動画のフレーム自体からテキストがはみ出す事態を防ぐ。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return False
    font_path = _font_path()
    try:
        font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bbox = probe.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if canvas:
        size = canvas
    elif height is not None:
        size = (max(1, tw + pad * 2), max(1, height))
    else:
        size = (max(1, tw + pad * 2), max(1, th + pad * 2))
    img = Image.new("RGBA", size, bg)
    draw = ImageDraw.Draw(img)
    center_y = canvas is not None or height is not None
    x = (size[0] - tw) // 2 - bbox[0] if canvas else pad - bbox[0]
    y = (size[1] - th) // 2 - bbox[1] if center_y else pad - bbox[1]
    draw.text((x, y), text, font=font, fill=color)
    img.save(path)
    return True


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
    fps = int(config.get("timelapse_fps"))
    frame_png: Path | None = None
    if _can_render_text():
        frame_png = out.with_suffix(".frame.png")
        if not _render_text_png(label, frame_png, font_size=max(14, height // 10),
                                color=(96, 96, 96, 255), canvas=(width, height),
                                bg=(20, 22, 24, 255)):
            frame_png = None
    try:
        if frame_png is not None:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                   "-loop", "1", "-i", str(frame_png),
                   "-t", str(max(1.0, seconds)), "-r", str(fps)] \
                + _encoder_args() + [str(out)]
        else:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "lavfi", "-i",
                   f"color=c=0x141618:s={width}x{height}:d={max(1.0, seconds)}:r={fps}"] \
                + _encoder_args() + [str(out)]
        ok, err = _run(cmd, timeout=600)
        if not ok:
            log.warning("NODATA クリップの生成に失敗: %s", err)
        return ok
    finally:
        if frame_png is not None:
            frame_png.unlink(missing_ok=True)


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

    caption_png: Path | None = None
    if _can_render_text():
        caption_png = out.with_suffix(".caption.png")
        if not _render_text_png(cid, caption_png, font_size=max(12, height // 14),
                                color=(176, 176, 176, 255), bg=(0, 0, 0, 115), pad=6):
            caption_png = None

    if caption_png is not None:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "concat", "-safe", "0", "-i", str(listfile),
               "-i", str(caption_png),
               "-filter_complex", f"[0:v]{vf}[bg];[bg][1:v]overlay=8:6",
               "-r", str(fps)] + _encoder_args() + [str(out)]
    else:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "concat", "-safe", "0", "-i", str(listfile),
               "-vf", vf, "-r", str(fps)] + _encoder_args() + [str(out)]
    ok, err = _run(cmd)
    listfile.unlink(missing_ok=True)
    if caption_png is not None:
        caption_png.unlink(missing_ok=True)
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
    if not lines or not _can_render_text():
        shutil.copy2(src, out)
        return True

    band_h = 34
    # テロップ全体を横長の1枚の PNG に描画し、overlay で下から右へ流す
    # (drawbox と overlay はどちらも常に存在するコアフィルタで、drawtext
    # のようにビルドオプション次第で欠けることがない)。
    #
    # height=band_h で縦を帯の高さぴったりに固定する。Pillow はキャンバス
    # の外には描画しない (=はみ出た分は自動的に切り取られる) ため、フォント
    # の実際の字形が計算上の行の高さより大きい場合でも、出力 PNG が
    # band_h を超えることはない。固定しなかった場合、フォント/文字種に
    # よっては実測の高さが帯よりわずかに大きくなり、動画のフレーム自体の
    # 下端からテロップがはみ出す不具合が実際に起きていた。
    ticker_png = out.with_suffix(".ticker.png")
    ok_png = _render_text_png("   ///   ".join(lines[:600]), ticker_png,
                              font_size=18, color=(216, 216, 216, 255),
                              height=band_h, pad=4)
    if not ok_png:
        shutil.copy2(src, out)
        return True

    vf = (f"[0:v]drawbox=x=0:y=ih-{band_h}:w=iw:h={band_h}:color=0x000000@0.72:t=fill[band];"
          f"[band][1:v]overlay=x='W-mod(t*90\\,W+w)':y=H-{band_h}")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-i", str(src), "-i", str(ticker_png),
           "-filter_complex", vf] + _encoder_args() + [str(out)]
    ok, err = _run(cmd)
    ticker_png.unlink(missing_ok=True)
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
