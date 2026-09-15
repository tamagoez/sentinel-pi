"""音声アナウンス (時報・エラー通知・カメラ再起動通知・その他システムイベント).

Raspberry Pi 3B+ の制約上の設計判断:

- TTS エンジンは espeak-ng を使う。Open JTalk のような高品質な日本語 TTS
  は辞書だけで数十 MB あり、RAM 1GB・SD カードという制約 (CLAUDE.md
  「依存を増やさない」) に見合わない。espeak-ng は数 MB で完結する代わりに
  発音はかなり機械的で、特に漢字は読み間違えることがある (形態素解析辞書
  を持たないため)。「聞き取れれば十分」という前提で選んでいる — 発音の
  自然さを求めるなら、テキスト側をひらがな中心に書くとある程度改善する。

- 音楽エンジン (mpg123、CLAUDE.md #2) とは完全に別経路で鳴らす。
  espeak-ng は ALSA へ直接書き込み、mpg123 のキュー・音楽ライブラリには
  一切触れない。ただし bcm2835 の出力は dmix なしでは同時に 1 ストリーム
  しか受け付けないため、曲の再生を一時的に完全停止 (music.duck_for_voice()
  / resume_from_voice()、bluetooth.py の退避・復帰と同じパターン) して
  から喋る。Bluetooth 接続中は bluealsa-aplay が同じデバイスを使っている
  ため、割り込むと双方が壊れるだけなので、その間はアナウンス自体を静かに
  スキップする。

- 音量は bluetooth._apply_volume() と同じ ALSA numid=1 (PCM Playback
  Volume) を直接操作する。mpg123 はソフトウェアゲイン (music_volume) で
  音量を持つため ALSA ミキサーを一切操作しないが、espeak-ng はそれを
  バイパスするので、Bluetooth 再生と同じ理由でハードウェア側の音量調整が
  要る (CLAUDE.md #15)。

- カテゴリごとに個別の on/off を持つ (voice_time_enabled/voice_error_enabled/
  voice_camera_reboot_enabled/voice_other_enabled)。時報だけ聞きたい、
  エラーだけ知りたい、といった使い方を分けられるようにするため、
  #18/#20 の「開始・終了は同じ秒数で」とは逆に、ここはあえて独立した
  スイッチにしている (対応関係を保証する必要がないカテゴリ同士のため)。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time

from ..core import config
from . import music

log = logging.getLogger("sentinel.voice")

_QUEUE: "asyncio.Queue[str]" = asyncio.Queue(maxsize=20)
_LOOP: asyncio.AbstractEventLoop | None = None

STATE = {"spoken": 0, "skipped": 0, "last_error": "", "last_spoken_at": 0.0, "last_text": ""}

# カテゴリ -> それを喋ってよいかどうかを持つ設定キー。
_CATEGORY_KEYS = {
    "time": "voice_time_enabled",
    "error": "voice_error_enabled",
    "camera_reboot": "voice_camera_reboot_enabled",
    "other": "voice_other_enabled",
}


def announce(text: str, category: str = "other") -> None:
    """他モジュールから呼ぶ公開 API。キューへ積むだけで即座に戻る
    (Discord 側の notify._enqueue_threadsafe() と同じ非同期呼び出しの
    パターン)。総元栓 (voice_enabled) とカテゴリ別スイッチの両方が
    有効なときだけ実際に積む。"""
    if not config.get("voice_enabled"):
        return
    key = _CATEGORY_KEYS.get(category, "voice_other_enabled")
    if not config.get(key):
        return
    if _LOOP is None:
        return
    _LOOP.call_soon_threadsafe(_enqueue, text)


def _enqueue(text: str) -> None:
    try:
        _QUEUE.put_nowait(text)
    except asyncio.QueueFull:
        STATE["skipped"] += 1
        log.warning("音声キューが満杯のため破棄しました: %s", text)


def _has_espeak() -> bool:
    return shutil.which("espeak-ng") is not None


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


def _speak_sync(text: str) -> None:
    if not _has_espeak():
        STATE["last_error"] = "espeak-ng がインストールされていません"
        log.warning(STATE["last_error"])
        return
    _apply_volume(int(config.get("voice_volume")))
    lang = str(config.get("voice_lang") or "ja")
    rate = int(config.get("voice_rate"))
    try:
        subprocess.run(["espeak-ng", "-v", lang, "-s", str(rate), text],
                       capture_output=True, timeout=30, check=False)
        STATE["spoken"] += 1
        STATE["last_spoken_at"] = time.time()
        STATE["last_text"] = text
        STATE["last_error"] = ""
    except Exception as exc:
        STATE["last_error"] = str(exc)
        log.warning("音声再生に失敗しました: %s", exc)


def speak_test(text: str) -> tuple[bool, str]:
    """設定タブの「テスト再生」用。キューを経由せず即座に鳴らす。"""
    if not _has_espeak():
        return False, "espeak-ng がインストールされていません (bootstrap.sh を再実行してください)"
    ducked = music.duck_for_voice()
    try:
        _speak_sync(text)
    finally:
        if ducked:
            music.resume_from_voice()
    return (STATE["last_error"] == "", STATE["last_error"] or "再生しました")


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
        text = f"{now.tm_hour}時{now.tm_min}分です"
        announce(text, "time")
