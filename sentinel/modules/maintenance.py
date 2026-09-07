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
                     max_width: int | None = None,
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

    max_width を渡すと、その幅に収まるまで font_size を比例縮小してから
    描画する (文字を削るのではなく縮める - カメラ名が長くてタイルからは
    み出す事態を防ぐ)。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return False
    font_path = _font_path()

    def _load(size: int):
        try:
            return ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
        except Exception:
            return ImageFont.load_default()

    font = _load(font_size)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bbox = probe.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if max_width is not None and tw + pad * 2 > max_width and tw > 0:
        shrunk = max(6, int(font_size * (max_width - pad * 2) / tw))
        font = _load(shrunk)
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


def _day_bounds(day: str) -> tuple[datetime, datetime]:
    start = datetime.strptime(day, "%Y-%m-%d")
    return start, start + timedelta(days=1)


def _capture_time(day: str, p: Path) -> datetime:
    """ファイル名 (HH-MM-SS-mmm, modules/camera.py が付ける) から実際の
    撮影時刻を復元する。壊れたファイル名なら mtime にフォールバックする。"""
    try:
        hh, mm, ss, ms = p.stem.split("-")
        return datetime.strptime(f"{day} {hh}:{mm}:{ss}.{ms}", "%Y-%m-%d %H:%M:%S.%f")
    except (ValueError, OSError):
        return datetime.fromtimestamp(p.stat().st_mtime)


def _video_t(real_t: datetime, day_start: datetime, day_span: float,
            target_seconds: float) -> float:
    """実時刻を、その日 1 日 (day_start 〜 +span 秒) を target_seconds に
    圧縮した動画の中の時刻へ変換する。カメラ間・テロップ間のずれをこの
    1 つの対応関係だけに統一することで解消している - 別々に「均等割り」
    していた以前の実装は、カメラごと・テロップで実時間との対応が
    バラバラになり、同じ瞬間を指しているはずのものが動画内では全く
    別の場面になっていた。"""
    frac = (real_t - day_start).total_seconds() / day_span
    return max(0.0, min(target_seconds, frac * target_seconds))


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
                      target_seconds: float, day_start: datetime, day_span: float) -> bool:
    frames = _captures_for(cid, day)
    fps = int(config.get("timelapse_fps"))
    if not frames:
        return _make_nodata_clip(out, f"{cid}  NODATA", target_seconds, width, height)

    # 各フレームを、実際に撮影された時刻に対応する動画内の時刻へ配置する
    # (「動体検知が起きた実時間」と「動画のどこか」が一致するように)。
    # 密集した連写バーストは 1/fps 未満には縮めない (見えなくなるため) が、
    # そのぶん合計が target_seconds を超えることがあるので、超えた場合は
    # 全体を比例縮小して他のカメラ・テロップと尺を揃える。
    vts = [_video_t(_capture_time(day, p), day_start, day_span, target_seconds) for p in frames]
    floor = 1.0 / fps
    durations = []
    for i in range(len(frames)):
        # frame 0 は (実際の撮影時刻に関わらず) 動画の先頭 t=0 から表示を
        # 始める - concat デマルチプレクサはそれより前を表現できないため、
        # 最初の1枚が撮れる前の時間帯は最初の1枚で埋める他ない。これを
        # vts[0] から始めてしまうと (以前のバグ) 動画の冒頭 vts[0] 秒分が
        # どのフレームにも割り当てられず、合計が target_seconds に届かない
        # まま短くなり、他のカメラ・テロップと尺が揃わなくなっていた。
        cur = vts[i] if i > 0 else 0.0
        nxt = vts[i + 1] if i + 1 < len(frames) else target_seconds
        durations.append(max(floor, nxt - cur))
    total = sum(durations)
    if total > target_seconds > 0:
        scale = target_seconds / total
        durations = [d * scale for d in durations]

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as f:
        listfile = Path(f.name)
        # concat デマルチプレクサの "duration" ディレクティブは、静止画
        # (image2 デマルチプレクサ) が相手だと最後のエントリで無視されたり、
        # 内部のタイムベースの丸めで合計が数割ずれたりすることが実機の検証
        # (ffprobe で実測) で分かった。代わりに各フレームを「1/fps 秒 = 1
        # 行」として必要な行数だけ並べる方式にする。入力側に -r fps を
        # 渡すことで各行が正確に1フレーム分になり、合計フレーム数 = 合計
        # 秒数 という誤差の出ない単純な対応になる。
        for p, dur in zip(frames, durations):
            n = max(1, round(dur * fps))
            f.write(f"file '{p}'\n" * n)

    vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease," \
         f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x141618"

    caption_png: Path | None = None
    if _can_render_text():
        caption_png = out.with_suffix(".caption.png")
        # カメラ名が長いとタイルの外までテロップがはみ出していた。幅の
        # 半分強 (60%) を上限にし、超える場合は文字を削らずフォントを
        # 縮めて収める (_render_text_png の max_width)。
        if not _render_text_png(cid, caption_png, font_size=max(12, height // 14),
                                color=(176, 176, 176, 255), bg=(0, 0, 0, 115), pad=6,
                                max_width=int(width * 0.6)):
            caption_png = None

    # -r fps を -i より前 (入力オプション) に置くことで、リストの各行が
    # ちょうど 1/fps 秒として解釈される (concat デマルチプレクサの
    # duration ディレクティブより正確、上のコメント参照)。
    if caption_png is not None:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "concat", "-safe", "0", "-r", str(fps), "-i", str(listfile),
               "-i", str(caption_png),
               "-filter_complex", f"[0:v]{vf}[bg];[bg][1:v]overlay=8:6",
               "-r", str(fps)] + _encoder_args() + [str(out)]
    else:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "concat", "-safe", "0", "-r", str(fps), "-i", str(listfile),
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


def _render_ticker_strip(entries: list[tuple[datetime, str]], path: Path, *,
                         day_start: datetime, day_span: float,
                         target_seconds: float, band_h: int, px_per_sec: float) -> bool:
    """アクセスログを、実時刻に比例した横位置に配置した 1 枚の帯画像に
    描画する。全項目を等間隔で連結していた以前の実装は文字数依存の速さで
    流れるだけで、動画内のどのカメラ映像が同時刻かとは無関係だった。
    ここでは項目 i の横位置を _video_t() と同じ対応関係 (実時刻 -> 動画内
    時刻) x px_per_sec で決めるため、_overlay_ticker() が
    `x = W - t*px_per_sec` という一定速度のスクロールで重ねるだけで、
    各項目が画面に入ってくる瞬間が必ずその実時刻に対応する動画内時刻と
    一致する。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return False
    font_path = _font_path()
    if not font_path:
        return False
    try:
        font = ImageFont.truetype(font_path, max(10, band_h - 16))
    except Exception:
        font = ImageFont.load_default()
    strip_w = max(1, int(target_seconds * px_per_sec) + 1)
    img = Image.new("RGBA", (strip_w, band_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for t, label in entries:
        vt = _video_t(t, day_start, day_span, target_seconds)
        x = int(vt * px_per_sec)
        bbox = draw.textbbox((0, 0), label, font=font)
        th = bbox[3] - bbox[1]
        y = (band_h - th) // 2 - bbox[1]
        draw.text((x, y), label, font=font, fill=(216, 216, 216, 255))
    img.save(path)
    return True


def _overlay_ticker(src: Path, out: Path, entries: list[tuple[datetime, str]],
                    day_start: datetime, day_span: float, target_seconds: float) -> bool:
    """アクセスログをテロップとして下部に流す。動画の再生時刻と、実際に
    その URL へアクセスしていた時刻が対応するように配置する
    (_render_ticker_strip / _video_t を参照)。"""
    if not entries or not _can_render_text():
        shutil.copy2(src, out)
        return True

    band_h = 34
    px_per_sec = 90.0
    # drawbox と overlay はどちらも常に存在するコアフィルタで、drawtext の
    # ようにビルドオプション次第で欠けることがない。
    ticker_png = out.with_suffix(".ticker.png")
    ok_png = _render_ticker_strip(entries[:2000], ticker_png, day_start=day_start,
                                  day_span=day_span, target_seconds=target_seconds,
                                  band_h=band_h, px_per_sec=px_per_sec)
    if not ok_png:
        shutil.copy2(src, out)
        return True

    # x = W - t*px_per_sec: 一定速度の右->左スクロール。帯画像の中の項目 i
    # の横位置は _video_t(項目iの実時刻)*px_per_sec なので、画面右端
    # (x=W) をその項目が通過する瞬間はちょうど t = video_t(項目i) になる -
    # 同時刻のカメラ映像と必ず重なる。
    vf = (f"[0:v]drawbox=x=0:y=ih-{band_h}:w=iw:h={band_h}:color=0x000000@0.72:t=fill[band];"
          f"[band][1:v]overlay=x='W-t*{px_per_sec}':y=H-{band_h}")
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
    day_start, day_end = _day_bounds(day)
    day_span = max(1.0, (day_end - day_start).total_seconds())

    with tempfile.TemporaryDirectory(prefix="sentinel-tl-") as tmpd:
        tmp = Path(tmpd)
        clips: list[Path] = []
        for i, cid in enumerate(cam_ids):
            STATE["stage"] = f"タイムラプス生成 ({cid})"
            STATE["progress"] = 0.1 + 0.5 * (i / max(1, len(cam_ids)))
            clip = tmp / f"{cid}.mp4"
            if _make_camera_clip(cid, day, clip, tile_w, tile_h, target, day_start, day_span):
                clips.append(clip)
        if not clips:
            STATE["stage"] = "NODATA 画面を生成中"
            clip = tmp / "nodata.mp4"
            _make_nodata_clip(clip, "NODATA", target, tile_w, tile_h)
            clips = [clip]

        STATE["stage"] = "全カメラを統合中"
        STATE["progress"] = 0.65
        tiled = tmp / "tiled.mp4"
        _tile(clips, tiled, tile_w, tile_h)

        STATE["stage"] = "アクセスログのテロップを重畳中"
        STATE["progress"] = 0.85
        final = outdir / f"{day}_daily.mp4"
        entries = netlog.ticker_entries(day) if config.get("ticker_enabled") else []
        _overlay_ticker(tiled, final, entries, day_start, day_span, target)

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
