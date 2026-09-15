"""音声アナウンス (時報・エラー通知・カメラ再起動通知・その他システムイベント).

Raspberry Pi 3B+ の制約上の設計判断:

- **TTS エンジンは Open JTalk を優先し、espeak-ng へフォールバックする。**
  当初は espeak-ng だけを使っていたが、実機で「発音が機械的すぎて聞き
  取れない」という報告があった。espeak-ng は言語ごとの発音ルールだけで
  音素を組み立てる方式で、日本語用の形態素解析辞書を持たないため、特に
  漢字を含む文で読み違えが多発していた。Open JTalk は MeCab 由来の辞書
  (naist-jdic) でまず形態素解析してから HMM 音声合成するため、同じ短い
  システムメッセージでも読み違えが大幅に減り、聞き取りやすさが桁違いに
  上がる。辞書 + 音声モデルで数十 MB 追加が必要になるが (espeak-ng 単体
  より重い)、VOICEVOX のような数百 MB 級のエンジンに比べれば RAM 1GB・
  SD カードという制約(CLAUDE.md「依存を増やさない」)の範囲内に収まる、
  という判断でこちらを選んでいる。**Open JTalk が使えない環境 (未インス
  トール・辞書/音声モデルが見つからない) では espeak-ng に自動フォール
  バックし、無音にはしない** — bootstrap.sh の再実行を忘れていても
  アナウンス機能そのものは (品質は落ちても) 動き続ける。

- 音楽エンジン (mpg123、CLAUDE.md #2) とは完全に別経路で鳴らす。
  Open JTalk/espeak-ng はどちらも ALSA へ直接書き込み、mpg123 のキュー・
  音楽ライブラリには一切触れない。ただし bcm2835 の出力は dmix なしでは
  同時に 1 ストリームしか受け付けないため、曲の再生を一時的に完全停止
  (music.duck_for_voice()/resume_from_voice()、bluetooth.py の退避・復帰
  と同じパターン) してから喋る。Bluetooth 接続中は bluealsa-aplay が同じ
  デバイスを使っているため、割り込むと双方が壊れるだけなので、その間は
  アナウンス自体を静かにスキップする。

- 音量は bluetooth._apply_volume() と同じ ALSA numid=1 (PCM Playback
  Volume) を直接操作する。mpg123 はソフトウェアゲイン (music_volume) で
  音量を持つため ALSA ミキサーを一切操作しないが、TTS エンジンはそれを
  バイパスするので、Bluetooth 再生と同じ理由でハードウェア側の音量調整が
  要る (CLAUDE.md #15)。

- カテゴリごとに個別の on/off + 読み上げ文のテンプレートを持つ
  (voice_time_enabled/voice_error_enabled/voice_camera_reboot_enabled/
  voice_other_enabled と、対になる voice_*_text)。時報だけ聞きたい、
  エラーだけ知りたい、といった使い方を分けられるようにするため、
  #18/#20 の「開始・終了は同じ秒数で」とは逆に、ここはあえて独立した
  スイッチにしている (対応関係を保証する必要がないカテゴリ同士のため)。
  テンプレートは notify.py の notify_motion_title などと同じ「知らない
  プレースホルダは無視し、壊れていれば既定文へ戻す」方式 (_fmt())。
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import shutil
import subprocess
import time
import uuid

from ..core import config
from . import music

log = logging.getLogger("sentinel.voice")

_QUEUE: "asyncio.Queue[str]" = asyncio.Queue(maxsize=20)
_LOOP: asyncio.AbstractEventLoop | None = None

STATE = {"spoken": 0, "skipped": 0, "last_error": "", "last_spoken_at": 0.0,
        "last_text": "", "engine": ""}

# カテゴリ -> (有効フラグの設定キー, 読み上げ文テンプレートの設定キー, 既定テンプレート)
_CATEGORY_KEYS = {
    "time": ("voice_time_enabled", "voice_time_text", "{hour}時{minute}分です"),
    "error": ("voice_error_enabled", "voice_error_text", "{message}"),
    "camera_reboot": ("voice_camera_reboot_enabled", "voice_camera_reboot_text", "{message}"),
    "other": ("voice_other_enabled", "voice_other_text", "{message}"),
}


def _fmt(key: str, default: str, values: dict) -> str:
    """設定タブのテンプレート文字列を安全に .format() する
    (notify.py の _fmt() と同じ方式)。知らない {プレースホルダ} や壊れた
    書式が来ても例外で読み上げ全体を落とさず、既定文へ静かに戻す。"""
    template = str(config.get(key) or default)
    try:
        return template.format(**values)
    except Exception:
        log.warning("音声テンプレート %s の書式が不正です。既定文を使います: %r", key, template)
        return default.format(**values)


def announce(message: str = "", category: str = "other", **extra) -> None:
    """他モジュールから呼ぶ公開 API。キューへ積むだけで即座に戻る
    (Discord 側の notify._enqueue_threadsafe() と同じ非同期呼び出しの
    パターン)。総元栓 (voice_enabled) とカテゴリ別スイッチの両方が
    有効なときだけ、カテゴリごとのテンプレートで整形してから積む。
    `message` は {message} プレースホルダに、`extra` の各キーはそのまま
    プレースホルダとして渡る (例: time カテゴリの {hour}/{minute})。"""
    if not config.get("voice_enabled"):
        return
    enabled_key, text_key, default_tpl = _CATEGORY_KEYS.get(category, _CATEGORY_KEYS["other"])
    if not config.get(enabled_key):
        return
    if _LOOP is None:
        return
    text = _fmt(text_key, default_tpl, {"message": message, **extra})
    _LOOP.call_soon_threadsafe(_enqueue, text)


def _enqueue(text: str) -> None:
    try:
        _QUEUE.put_nowait(text)
    except asyncio.QueueFull:
        STATE["skipped"] += 1
        log.warning("音声キューが満杯のため破棄しました: %s", text)


# ---------------------------------------------------------------- TTS エンジン

def _has_espeak() -> bool:
    return shutil.which("espeak-ng") is not None


# open-jtalk-mecab-naist-jdic (辞書) と hts-voice-nitech-jp-atr503-m001
# (音声モデル) の実際のインストール先はディストリのバージョンで多少ずれる
# ため、決め打ちのパスではなく glob で探す (maintenance.py の
# _FONT_CANDIDATES や sentinel-guardian.sh の AGH_YAML 探索と同じ
# 「堅牢なパス解決」の考え方)。
_OJT_DIC_GLOBS = (
    "/var/lib/mecab/dic/open-jtalk/naist-jdic",
    "/usr/share/hts-engine/dic",
    "/usr/lib/*/open-jtalk/dic",
    "/usr/share/open-jtalk/dic",
    "/usr/share/*/open-jtalk/dic",
)
_OJT_VOICE_GLOB = "/usr/share/hts-voice/**/*.htsvoice"


def _ojt_dic_dir() -> str | None:
    for pattern in _OJT_DIC_GLOBS:
        for p in glob.glob(pattern):
            if os.path.isdir(p) and glob.glob(os.path.join(p, "*.dic")):
                return p
    return None


def _ojt_voice_file() -> str | None:
    matches = sorted(glob.glob(_OJT_VOICE_GLOB, recursive=True))
    return matches[0] if matches else None


def _has_open_jtalk() -> bool:
    return (shutil.which("open_jtalk") is not None
            and _ojt_dic_dir() is not None and _ojt_voice_file() is not None)


def _sound_card() -> int | None:
    try:
        out = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if line.startswith("card "):
            try:
                return int(line.split()[1].rstrip(":"))
            except Exception:
                continue
    return None


def _apply_volume(percent: int) -> None:
    card = _sound_card()
    if card is None:
        return
    percent = max(0, min(100, percent))
    try:
        subprocess.run(["amixer", "-c", str(card), "cset", "numid=1", f"{percent}%"],
                       capture_output=True, timeout=5, check=False)
    except Exception:
        pass


def _speak_open_jtalk(text: str, rate: float) -> bool:
    dic = _ojt_dic_dir()
    voice = _ojt_voice_file()
    if not dic or not voice:
        return False
    wav_path = config.RUNTIME / f"voice-{uuid.uuid4().hex}.wav"
    try:
        subprocess.run(
            ["open_jtalk", "-x", dic, "-m", voice, "-r", f"{max(0.5, min(2.0, rate)):.2f}",
             "-ow", str(wav_path)],
            input=text.encode("utf-8"), capture_output=True, timeout=20, check=False)
        if not wav_path.exists() or wav_path.stat().st_size < 100:
            return False
        subprocess.run(["aplay", "-q", str(wav_path)], capture_output=True, timeout=30, check=False)
        return True
    except Exception as exc:
        log.warning("open_jtalk での再生に失敗しました: %s", exc)
        return False
    finally:
        wav_path.unlink(missing_ok=True)


def _speak_espeak(text: str, rate: float) -> bool:
    if not _has_espeak():
        return False
    lang = str(config.get("voice_lang") or "ja")
    # voice_rate は「速さの倍率」(0.5-2.0) という、Open JTalk の -r と
    # 共通の意味で持っている。espeak-ng は words-per-minute を取るため、
    # 既定 150wpm を基準に変換する。
    wpm = int(max(80, min(400, 150 * rate)))
    try:
        subprocess.run(["espeak-ng", "-v", lang, "-s", str(wpm), text],
                       capture_output=True, timeout=30, check=False)
        return True
    except Exception as exc:
        log.warning("espeak-ng での再生に失敗しました: %s", exc)
        return False


def _speak_sync(text: str) -> None:
    _apply_volume(int(config.get("voice_volume")))
    rate = float(config.get("voice_rate"))
    ok = False
    if _has_open_jtalk():
        ok = _speak_open_jtalk(text, rate)
        if ok:
            STATE["engine"] = "open_jtalk"
    if not ok:
        ok = _speak_espeak(text, rate)
        if ok:
            STATE["engine"] = "espeak-ng"
    if not ok:
        STATE["last_error"] = "open_jtalk も espeak-ng も利用できません (bootstrap.sh を再実行してください)"
        STATE["engine"] = ""
        log.warning(STATE["last_error"])
        return
    STATE["spoken"] += 1
    STATE["last_spoken_at"] = time.time()
    STATE["last_text"] = text
    STATE["last_error"] = ""


def speak_test(text: str) -> tuple[bool, str]:
    """設定タブの「テスト再生」用。キューを経由せず即座に鳴らす。"""
    ducked = music.duck_for_voice()
    try:
        _speak_sync(text)
    finally:
        if ducked:
            music.resume_from_voice()
    if STATE["last_error"]:
        return False, STATE["last_error"]
    return True, f"再生しました ({STATE['engine']})"


async def loop() -> None:
    global _LOOP
    _LOOP = asyncio.get_running_loop()
    while True:
        text = await _QUEUE.get()
        # Bluetooth 接続中は bluealsa-aplay が同じ ALSA デバイスを排他的に
        # 使っている。割り込むと相手の再生を壊すだけなので静かに諦める
        # (モジュール読み込み順の都合で遅延 import する)。
        from . import bluetooth as _bt
        if _bt.STATE.get("connected"):
            STATE["skipped"] += 1
            continue
        ducked = await asyncio.to_thread(music.duck_for_voice)
        try:
            await asyncio.to_thread(_speak_sync, text)
        finally:
            if ducked:
                await asyncio.to_thread(music.resume_from_voice)


async def time_signal_loop() -> None:
    """時報。voice_time_interval_minutes 分ごとの境界 (毎時 0 分からの
    倍数) を跨いだ瞬間に現在時刻を読み上げる。壁時計の分に揃えることで
    「n 分ごと」がいつも同じ分に鳴る、実際の時報らしい挙動にしている。"""
    last_bucket: int | None = None
    while True:
        await asyncio.sleep(20)
        if not config.get("voice_enabled") or not config.get("voice_time_enabled"):
            last_bucket = None   # 無効化されている間の変化を境界跨ぎとして扱わない
            continue
        interval = max(1, int(config.get("voice_time_interval_minutes")))
        now = time.localtime()
        bucket = (now.tm_hour * 60 + now.tm_min) // interval
        if bucket == last_bucket:
            continue
        last_bucket = bucket
        announce("", "time", hour=now.tm_hour, minute=now.tm_min)
