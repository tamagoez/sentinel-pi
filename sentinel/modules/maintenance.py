"""毎日 4 時の定時処理.

順序:
    1. カメラを停止し、音楽を止めて資源を空ける
    2. カメラごとのタイムラプスを作り、時計/アクセスログの帯と一緒に
       1 回の ffmpeg 呼び出しでタイル状に統合する (CLAUDE.md 該当節参照)
       - 映像が 1 枚もないカメラの枠は NODATA で埋める
       - 全カメラに映像がない場合も NODATA 画面で動画生成を続行する
    3. 生成物を archive へ置き、再起動する

エンコーダは常に libx264 (ultrafast) を使う。以前は h264_v4l2m2m
(ハードウェアエンコーダ) が使えれば優先していたが、Pi 3B+ では
CBR 指定との組み合わせで固まる既知の不具合があり、しかも速度面の
利点も実測ではほぼ無かったため廃止した (`_encoder_args()` 参照)。

run_now() は「実行中のまま二度と進まない」状態に陥らないよう、前段の
停止処理・ビルド全体のどちらにも上限時間を設けている (`_PRESTOP_STEP_
TIMEOUT`/`_BUILD_TIMEOUT`)。それでも `STATE["running"]` が長時間 True
のまま固定された場合は、次の呼び出しが `_STALE_RUN_SECONDS` を基準に
「放棄された実行」とみなして自動的に回収する。
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
from . import camera, music, netlog, notify, voice

log = logging.getLogger("sentinel.maintenance")

STATE = {
    "running": False,
    "started_at": 0.0,
    "stage": "",
    "progress": 0.0,
    "last_run": 0.0,
    "last_result": "",
    "last_output": "",
    "next_run": 0.0,
}

# run_now() が「これ以上待っても進まない」と判断するまでの上限。単発の
# ffmpeg 呼び出しには _run() 自身のタイムアウトがあるが、それでも
# 「サブプロセスが SIGKILL に応答しない (D-state で固まった V4L2 デバイス
# など)」場合は _run() のタイムアウト機構ごと無力化される
# (_encoder_args() のコメント参照)。この上限は「あり得る最悪の合計」
# よりまだ十分大きい (小さい tile_w・ultrafast なら通常は数分で終わる)
# 一方、STATE["running"] が永久に True のまま固定される事態は避ける
# ためのもの — これが無いと、毎日 4 時の loop() が「すでに実行中です」
# で永久に空振りし続け、動画生成も再起動も二度と起こらなくなる
# (CLAUDE.md #59 と同じ「一度詰まると永久に直らない」不具合の別の経路)。
_STALE_RUN_SECONDS = 2 * 3600
# music.PLAYER.stop()/camera.shutdown() は通常 1 秒未満で終わる軽い処理
# だが、万一どこかで詰まった場合に run_now() 全体を無期限に止めないため
# の上限。
_PRESTOP_STEP_TIMEOUT = 30.0
# _build() 全体 (全カメラのクリップ生成 + 帯 + 統合) の上限。ultrafast +
# 既定の tile 幅であれば通常数分で終わるはずだが、カメラが多い/連写が
# 激しい日でも余裕を持たせてある。これを超えたら「タイムアウト」として
# 失敗扱いにし、run_now() の finally で確実に STATE["running"] を戻す —
# _STALE_RUN_SECONDS による次回の「詰まった実行の回収」を待たずに、
# その日のうちに再起動判定まで進められるようにするため。
_BUILD_TIMEOUT = 40 * 60

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
    if path.suffix.lower() in (".jpg", ".jpeg"):
        # JPEG は透過を持てないので、不透明前提でも RGBA のままだと保存に
        # 失敗する (呼び出し側が concat リストで JPEG フレームと混在させる
        # ときに使う - 詳細は _make_camera_clip のコメント参照)。
        img = img.convert("RGB")
    img.save(path)
    return True


def _encoder_args() -> list[str]:
    """常に libx264 (ultrafast) を使う。

    以前はハードウェアエンコーダ `h264_v4l2m2m` を検出できれば優先して
    いたが、Pi 3B+ ではこの選択が「動かない」報告の一因になっていた。
    `h264_v4l2m2m` は固定ビットレート (CBR) 指定と組み合わさると
    `VIDIOC_STREAMON failed` で失敗する既知の不具合を持ち、失敗の仕方に
    よっては V4L2 デバイスをカーネル内で D-state のまま掴んだ状態にし、
    呼び出し元の ffmpeg プロセスが `SIGKILL` にも応答しなくなることが
    ある — `subprocess.run(..., timeout=...)` はタイムアウト時に
    `kill()` を送るだけなので、この状態になると `_run()` のタイムアウト
    machinery ごと無力化され、定時処理がそのステップで実質的に永久停止
    する ([Raspberry Pi Forums](https://forums.raspberrypi.com/viewtopic.php?t=330999))。
    さらに、この「ハードウェア」エンコーダは Pi 3B+ 実測でも 1 コア
    フル稼働の libx264 (ソフトウェア) と大差ない速度しか出ない
    ([Raspberry Pi Forums](https://forums.raspberrypi.com/viewtopic.php?t=353958))
    ため、動かないリスクを取ってまで選ぶ利点が無い。CLAUDE.md 全体の
    「ドライバ依存の機能はソフトウェア側の確実な実装に置き換える」方針
    (drawtext→Pillow、asound.conf→sysdefault 等) と同じ判断で、常に
    libx264 ultrafast だけを使う。**この h264_v4l2m2m の検出・優先を
    復活させないでください** — 同じ「映像生成がどこかで止まって二度と
    進まない」不具合に戻ります。"""
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

    # 最初の撮影より前 / 最後の撮影より後を NODATA で埋める。以前はここを
    # 「最初のフレームを動画の先頭まで、最後のフレームを動画の末尾まで
    # 引き伸ばす」ことで埋めていた (concat デマルチプレクサが負の時刻を
    # 表現できないための回避策)。しかしこれは「まだ撮っていない/もう
    # 撮らなくなった」時間帯を、あたかも撮影が続いていたかのように見せて
    # しまう。テキスト描画が使える環境ではこの見せかけをやめ、実際に
    # NODATA と表示する。フォント/Pillow が無い環境では、処理を止めない
    # という既定方針 (CLAUDE.md「意図的にしていないこと」) のとおり、
    # 従来どおりの引き伸ばしにフォールバックする。
    #
    # NODATA フレームは .jpg で保存する (.png ではなく)。実際に撮影された
    # フレームは全て .jpg で、この concat リストの中では両者が同じ 1 本の
    # フレーム列として混在する。実機で検証したところ、ffmpeg の concat
    # デマルチプレクサはリスト先頭のファイルで検出したコーデック
    # (image2 の png/mjpeg 判定) をストリーム全体に固定してしまい、
    # 途中から別形式が混じると "Invalid PNG signature" のようなデコード
    # エラーで大量にフレームを落とし、動画が target_seconds よりずっと
    # 短くなる (他のカメラ・帯映像と尺が揃わなくなる) という不具合を
    # 実際に踏んだ。**この拡張子を .png に戻さないでください** — 同じ
    # 尺のずれに戻ります。
    nodata_png: Path | None = None
    if _can_render_text():
        candidate = out.with_suffix(".nodata.jpg")
        if _render_text_png(f"{cid}  NODATA", candidate, font_size=max(14, height // 10),
                            color=(96, 96, 96, 255), canvas=(width, height),
                            bg=(20, 22, 24, 255)):
            nodata_png = candidate

    segs: list[tuple[Path, float]] = []
    if nodata_png is not None and vts[0] > floor / 2:
        segs.append((nodata_png, vts[0]))
    for i in range(len(frames)):
        cur = vts[i] if (nodata_png is not None or i > 0) else 0.0
        if i + 1 < len(frames):
            nxt = vts[i + 1]
        elif nodata_png is not None:
            nxt = vts[i] + floor      # 最後の1枚はここでは引き伸ばさない
        else:
            nxt = target_seconds      # フォールバック: 従来どおり末尾まで
        segs.append((frames[i], max(floor, nxt - cur)))
    if nodata_png is not None:
        trailing = target_seconds - sum(d for _, d in segs)
        if trailing > floor / 2:
            segs.append((nodata_png, trailing))

    total = sum(d for _, d in segs)
    if total > target_seconds > 0:
        scale = target_seconds / total
        segs = [(p, d * scale) for p, d in segs]

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
        for p, dur in segs:
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
    if nodata_png is not None:
        nodata_png.unlink(missing_ok=True)
    if not ok:
        log.warning("カメラ %s のタイムラプス生成に失敗: %s", cid, err)
        return _make_nodata_clip(out, f"{cid}  ERROR", target_seconds, width, height)
    return True


def _grid_dims(n: int) -> tuple[int, int]:
    """タイル枚数から (列数, 行数) を決める。_tile() と、帯映像の幅を
    最終的なタイル映像の幅に合わせる _build() の両方から使う - 別々に
    計算すると片方だけ列数の考え方がずれる事故につながる。"""
    if n <= 1:
        return 1, 1
    cols = 2 if n <= 4 else 3
    rows = (n + cols - 1) // cols
    return cols, rows


def _tile(clips: list[Path], out: Path, width: int, height: int,
         band: Path | None = None) -> bool:
    """複数クリップを横並び / グリッドに合成し、同じ ffmpeg 呼び出しの
    中で時刻/アクセスログの帯 (band) も一緒に重ねる。

    以前は「タイルへ合成する (_tile)」と「帯を重ねる (旧
    _overlay_info_band)」がそれぞれ独立に最終解像度の映像をフル再
    エンコードしていた — 定時処理 1 回につきタイル映像の全長を 2 回
    エンコードすることになり、Pi 3B+ のソフトウェアエンコードでは
    これが生成時間の大きな割合を占めていた。帯を xstack の出力へ
    そのまま filter_complex でつなげるだけで 1 回のエンコードに減らせる
    — 「URL 表示が映像に間に合うよう爆速に」という要望への直接の対応
    でもある (帯の内容自体は _build_info_band() が既に瞬時切り替え・
    キャッシュ済みの PNG で作っており、ここでの高速化はその帯を映像へ
    貼り付ける工程の話)。**この 2 回のフルサイズ再エンコードへ戻さない
    でください** — 同じ「生成に時間が掛かりすぎる」不具合に戻ります。"""
    if len(clips) == 1 and band is None:
        shutil.copy2(clips[0], out)
        return True
    n = len(clips)
    cols, rows = _grid_dims(n)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for c in clips:
        cmd += ["-i", str(c)]
    band_idx = None
    if band is not None:
        cmd += ["-i", str(band)]
        band_idx = n

    filters = []
    if n == 1:
        filters.append(f"[0:v]scale={width}:{height},setsar=1[tiled]")
    else:
        inputs = []
        for i in range(n):
            filters.append(f"[{i}:v]scale={width}:{height},setsar=1[v{i}]")
            inputs.append(f"[v{i}]")
        pad = cols * rows - n
        for j in range(pad):
            # 足りないタイルは黒で埋める
            filters.append(f"color=c=0x141618:s={width}x{height}:d=1[p{j}]")
            inputs.append(f"[p{j}]")
        layout = "|".join(f"{(i % cols) * width}_{(i // cols) * height}"
                          for i in range(cols * rows))
        filters.append(f"{''.join(inputs)}xstack=inputs={cols * rows}:"
                       f"layout={layout}:fill=0x141618[tiled]")

    map_label = "tiled"
    if band_idx is not None:
        filters.append(f"[tiled][{band_idx}:v]overlay=x=0:y=H-h[out]")
        map_label = "out"

    cmd += ["-filter_complex", ";".join(filters), "-map", f"[{map_label}]"]
    cmd += _encoder_args() + [str(out)]
    ok, err = _run(cmd)
    if not ok:
        log.warning("タイル/帯の合成に失敗しました: %s。1台目のみ使用します。", err)
        shutil.copy2(clips[0], out)
    return True


def _render_band_png(clock_label: str, domain_label: str, path: Path,
                     width: int, band_h: int) -> bool:
    """時計 (左) と、その瞬間にアクセスしていたドメイン (右) を1枚の帯に
    描く。以前は URL を右から左へスクロールさせていたが、動画の横幅に
    対して1件ごとの表示時間が短すぎ、件数が多い日は文字が重なって読めない
    (=横に流せるだけの表示余地がそもそも無い) と分かったため、流さず
    「その瞬間の1件」をそのまま静止表示する形に変えた。"""
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

    font = _load(max(10, band_h - 14))
    img = Image.new("RGB", (max(1, width), band_h), (16, 18, 20))
    draw = ImageDraw.Draw(img)
    pad = 8

    cbbox = draw.textbbox((0, 0), clock_label, font=font)
    cy = (band_h - (cbbox[3] - cbbox[1])) // 2 - cbbox[1]
    draw.text((pad, cy), clock_label, font=font, fill=(210, 210, 210))
    cw = cbbox[2] - cbbox[0]

    if domain_label:
        dx = pad * 2 + cw + 10
        max_w = width - dx - pad
        if max_w > 24:
            dfont = font
            dbbox = draw.textbbox((0, 0), domain_label, font=dfont)
            dw = dbbox[2] - dbbox[0]
            if dw > max_w and dw > 0:
                # 文字を削らず、幅に収まるまでフォントを縮める。
                shrunk = max(6, int(font.size * max_w / dw))
                dfont = _load(shrunk)
                dbbox = draw.textbbox((0, 0), domain_label, font=dfont)
            dy = (band_h - (dbbox[3] - dbbox[1])) // 2 - dbbox[1]
            draw.text((dx, dy), domain_label, font=dfont, fill=(150, 190, 230))

    img.save(path)
    return True


def _build_info_band(entries: list[tuple[datetime, str]], day_start: datetime,
                     day_span: float, target_seconds: float, width: int,
                     tmp: Path, fps: int) -> Path | None:
    """時計 + そのときアクセスしていたドメインを、カメラ映像と全く同じ
    実時刻対応 (_video_t) で表示する帯を、1本の映像として作る。内容が
    変わらない区間は同じ PNG を繰り返し指定するだけなので (カメラ映像の
    concat と同じ手法)、実際に描画する PNG の枚数は「時計の分が変わる
    回数」と「アクセス先が変わる回数」の合計程度に収まる。"""
    if not _can_render_text():
        return None
    band_h = 30
    fps = max(1, fps)
    total_frames = max(1, round(target_seconds * fps))
    entries_sorted = sorted(entries, key=lambda e: e[0])
    ei = 0
    domain_label = ""
    cache: dict[str, Path] = {}
    lines: list[str] = []
    for k in range(total_frames):
        video_t = k / fps
        frac = video_t / target_seconds if target_seconds > 0 else 0.0
        real_t = day_start + timedelta(seconds=frac * day_span)
        while ei < len(entries_sorted) and entries_sorted[ei][0] <= real_t:
            domain_label = entries_sorted[ei][1]
            ei += 1
        clock_label = real_t.strftime("%H:%M")
        key = f"{clock_label}|{domain_label}"
        png = cache.get(key)
        if png is None:
            png = tmp / f"band-{len(cache)}.png"
            if not _render_band_png(clock_label, domain_label, png, width, band_h):
                return None
            cache[key] = png
        lines.append(f"file '{png}'\n")

    listfile = tmp / "band-list.txt"
    listfile.write_text("".join(lines), encoding="utf-8")
    out = tmp / "band.mp4"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-r", str(fps), "-i", str(listfile),
           "-r", str(fps)] + _encoder_args() + [str(out)]
    ok, err = _run(cmd)
    if not ok:
        log.warning("時刻/アクセスログの帯映像の生成に失敗しました: %s", err)
        return None
    return out


# ---------------------------------------------------------------- 本体

def _build(day: str) -> tuple[bool, str]:
    tile_w = int(config.get("timelapse_tile_width"))
    tile_h = int(tile_w * 3 / 4)
    outdir = config.ARCHIVE_ROOT / day
    outdir.mkdir(parents=True, exist_ok=True)

    if not _can_render_text():
        # NODATA ラベル・カメラ名キャプション・時計/URL の帯のすべてが
        # ここに依存する (CLAUDE.md #10)。Pillow または対応フォントが
        # 見つからない環境ではこれらが全部揃って静かに消えるだけで、
        # これまで一切ログに残していなかった — 「動画は生成されるのに
        # 時計や URL が出ない」という報告を切り分けるための最初の一手が
        # 無い状態だった。ここで一度だけ警告を出す (build 1 回あたり 1 行、
        # 実際に描画を試みるたびに毎回出すとログが埋まるため)。
        log.warning("テキスト描画ができないため、NODATA 表示・カメラ名・"
                   "時計/URL の帯を省略します (python3-pil / "
                   "fonts-dejavu-core が入っているか確認してください)")

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

        band = None
        if config.get("ticker_enabled"):
            STATE["stage"] = "時刻・アクセスログの帯を生成中"
            STATE["progress"] = 0.65
            cols, rows = _grid_dims(len(clips))
            entries = netlog.ticker_entries(day)[:2000]
            band = _build_info_band(entries, day_start, day_span, target,
                                    tile_w * cols, tmp, fps)

        STATE["stage"] = "全カメラと帯を統合中"
        STATE["progress"] = 0.8
        final = outdir / f"{day}_daily.mp4"
        _tile(clips, final, tile_w, tile_h, band)

    if not final.is_file():
        return False, "動画が生成されませんでした"
    size_mb = final.stat().st_size / 1e6
    detail = " / ".join(f"{cid}: {n}枚" for cid, n in counts.items())
    return True, f"{final.name} ({size_mb:.1f} MB) — {detail}"


def _kill_stray_ffmpeg() -> None:
    """スタックした前回実行の後始末。この Pi で ffmpeg を使う処理は
    定時処理 (このモジュール) 以外に存在しないため、`pkill -f ffmpeg`
    で無差別に片付けても他機能を巻き込む心配がない。プロセスが無い/
    `pkill` が無い場合も含めて best-effort — 失敗しても run_now() 側の
    処理自体は続行する。"""
    try:
        subprocess.run(["pkill", "-9", "-f", "ffmpeg"], timeout=10)
    except Exception:
        pass


async def run_now(*, reboot: bool | None = None) -> dict:
    """定時処理を実行する。手動起動にも使える。"""
    if STATE["running"]:
        age = time.time() - STATE.get("started_at", 0.0)
        if age < _STALE_RUN_SECONDS:
            return {"ok": False, "message": "すでに実行中です"}
        # 前回の実行が _STALE_RUN_SECONDS を超えてなお「実行中」のまま
        # 固定されている。どこかのサブプロセスが SIGKILL にも応答せず
        # 固まった (V4L2 デバイスが D-state で掴まれた場合など、
        # _encoder_args() のコメント参照) 可能性が高い。ここで永久に
        # 諦めて次の日も再度スキップし続けるより、居座っている ffmpeg を
        # 掃除してから新しい実行を始める方が、CLAUDE.md 全体の「諦めた
        # ままにしない」方針 (#22 の再起動クールダウンなど) に沿っている。
        log.warning("前回の定時処理が %.0f 秒間「実行中」のまま固まっているため、"
                   "放棄されたとみなして新たに実行します", age)
        await asyncio.to_thread(_kill_stray_ffmpeg)

    STATE.update(running=True, started_at=time.time(), stage="準備中",
                progress=0.0, last_result="")
    day = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d")
    started = time.time()
    notify.system_event("定時処理を開始しました", f"対象日: {day}", level="info")
    voice.announce("定時処理を開始します", "other")

    try:
        STATE["stage"] = "カメラと音楽を停止中"
        # 通常は 1 秒未満で終わる軽い処理だが、万一どこかで詰まった場合に
        # run_now() 全体を無期限に止めないよう上限を掛ける。
        # asyncio.to_thread が包むスレッド自体は取り消せない (asyncio の
        # 既知の制約) ため、詰まった場合そのスレッドは残ってしまうが、
        # run_now() のこのコルーチンは先へ進める — 「二度と完了しない」
        # 状態だけは避けるための限定的な保険。
        for fn, kwargs, label in (
            (music.PLAYER.persist, {"force": True}, "音楽の位置保存"),
            (music.PLAYER.stop, {"terminate": True, "reason": "maintenance"}, "音楽の停止"),
            (camera.shutdown, {}, "カメラの停止"),
        ):
            try:
                await asyncio.wait_for(asyncio.to_thread(fn, **kwargs),
                                       timeout=_PRESTOP_STEP_TIMEOUT)
            except asyncio.TimeoutError:
                log.warning("定時処理の前処理 (%s) が %.0f 秒経っても終わらないため、"
                           "スキップして続行します", label, _PRESTOP_STEP_TIMEOUT)
        await asyncio.sleep(1.0)

        try:
            ok, detail = await asyncio.wait_for(asyncio.to_thread(_build, day),
                                                timeout=_BUILD_TIMEOUT)
        except asyncio.TimeoutError:
            ok, detail = False, f"タイムアウト ({_BUILD_TIMEOUT}秒経過)"
            log.error("定時処理の動画生成が %d 秒を超えたためタイムアウトさせました",
                     _BUILD_TIMEOUT)
        STATE.update(last_result="成功" if ok else "失敗", last_output=detail,
                     progress=1.0, stage="完了", last_run=time.time())
        notify.system_event(
            "定時処理が完了しました" if ok else "定時処理が失敗しました",
            detail, level="good" if ok else "error",
            fields=[{"name": "所要時間", "value": f"{time.time() - started:.0f} 秒",
                     "inline": True}])
        voice.announce("定時処理が完了しました" if ok else "定時処理が失敗しました",
                       "other" if ok else "error")
    except Exception as exc:
        log.exception("定時処理が失敗しました")
        STATE.update(last_result="例外", last_output=str(exc))
        notify.system_event("定時処理で例外が発生しました", str(exc), level="error")
        voice.announce("定時処理で例外が発生しました", "error")
        ok, detail = False, str(exc)
    finally:
        STATE["running"] = False

    do_reboot = config.get("reboot_after_maintenance") if reboot is None else reboot
    if do_reboot:
        notify.system_event("再起動します", level="warn")
        voice.announce("定時処理が完了しました。Piを再起動します", "other")
        await asyncio.sleep(4)     # 通知が飛ぶのを待つ
        await asyncio.to_thread(_reboot)
    else:
        await asyncio.to_thread(camera.reconcile)

    return {"ok": ok, "message": detail}


async def emergency_reboot(cid: str, reason: str) -> None:
    """カメラの破損フレームが、ヒステリシス付きの再接続 (camera.py
    _CORRUPT_REBOOT_THRESHOLD 回) を繰り返しても解消しないときに、
    camera.py の ON_CORRUPT_REBOOT フック経由で main.py から呼ばれる。

    真の原因が USB コントローラの詰まりなど、プロセスの再接続では届かない
    ところにある場合、実機で試せる最後の手段は Pi 自体の再起動しかない
    (CLAUDE.md #22)。日次の run_now() と違いタイムラプス生成は行わない —
    異常系なので原因究明を優先し、時間のかかる処理を挟まない。カメラ・
    音楽を止めてから再起動するという手順自体と _reboot() の sudo
    フォールバックは run_now() と共通の実装を再利用し、重複させない。

    再起動の「理由」は Discord とログの両方に残す。実機で同じ報告が
    来たとき、どのカメラが・何回再接続を試みて・破損/許容件数がどうで
    再起動に至ったかを、後から (機体が再起動されて手元に無くても)
    確認できるようにするため。"""
    if STATE["running"]:
        log.warning("定時処理の実行中のため、破損検知による緊急再起動は見送ります (カメラ %s)", cid)
        return
    log.error("カメラ %s の破損が繰り返し解消しないため緊急再起動します: %s", cid, reason)
    notify.system_event(
        "カメラの破損が繰り返し解消しないため、Pi を再起動します",
        f"カメラ: {cid}\n{reason}", level="error")
    voice.announce(f"カメラ {cid} の破損が解消しないため、Piを再起動します", "camera_reboot")
    try:
        await asyncio.to_thread(music.PLAYER.persist, force=True)
        await asyncio.to_thread(music.PLAYER.stop, terminate=True, reason="corrupt-reboot")
        await asyncio.to_thread(camera.shutdown)
        await asyncio.sleep(1.0)
    except Exception:
        log.exception("緊急再起動前の停止処理に失敗しました (再起動は続行します)")
    await asyncio.sleep(4)     # 通知が飛ぶのを待つ (run_now() と同じ)
    await asyncio.to_thread(_reboot)


def _reboot() -> None:
    """再起動を試みる。3 つのコマンドを順に試すが、**戻り値を確認しない
    まま最初の 1 回で `return` していたため、`sudo -n /sbin/reboot` が
    (sudoers のズレ・sudo が PATH に無い等の理由で) 非ゼロ終了しても
    「成功した」と誤認してそのまま抜けていた** — `subprocess.run()` は
    `check=True` を渡さない限り非ゼロ終了でも例外を投げないため、この
    関数の `try/except` は何も捕まえず、後続のフォールバックも一切
    試されないまま、ログにも一切残らず静かに再起動が起きない、という
    不具合になっていた (`core/state.py`/`modules/hotspot.py` の同種の
    sudo 呼び出しはどちらも `returncode` を確認しており、この関数だけが
    それを欠いていた)。**この確認を省略した実装に戻さないでください**
    — 同じ「定時処理は完了ログが出るのに Pi が再起動しない」不具合に
    戻ります。2 番目のフォールバックも `sudo -n` を欠いていたため
    (`/etc/sudoers.d/sentinel` が許可しているのは `sudo -n systemctl
    reboot` であり、素の `systemctl reboot` ではない)、非 root ユーザー
    からは最初から失敗する運命だった箇所も合わせて直した。"""
    for cmd in (["sudo", "-n", "/sbin/reboot"],
                ["sudo", "-n", "systemctl", "reboot"],
                ["/sbin/reboot"]):
        try:
            r = subprocess.run(cmd, timeout=20, capture_output=True, text=True)
        except Exception as exc:
            log.warning("再起動コマンド %s の実行に失敗しました: %s", cmd, exc)
            continue
        if r.returncode == 0:
            log.info("再起動コマンド %s を実行しました", cmd)
            return
        tail = (r.stderr or r.stdout or "").strip()
        log.warning("再起動コマンド %s が非ゼロ終了 (code=%s) でした: %s",
                   cmd, r.returncode, tail)
    log.error("再起動コマンドをすべて試しましたが、いずれも失敗しました。"
             "sudoers (/etc/sudoers.d/sentinel) の設定を確認してください。")


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
