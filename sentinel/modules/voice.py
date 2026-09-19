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
  音楽ライブラリには一切触れない。**曲を止めずに重ねて鳴らす** —
  `sentinel-setup-audio-mixing.sh` が設定する ALSA の dmix (CLAUDE.md
  #31) 経由で、音楽は `sentinel_music`、音声は `sentinel_voice` という
  別々の名前つき PCM へ書き込み、dmix がハードウェア側で 1 本にミックス
  する。以前は bcm2835 の出力が dmix なしでは同時に 1 ストリームしか
  受け付けないため曲を毎回完全停止していたが (`music.duck_for_voice()`)、
  「音楽の音量が意図せず変わる」「重ねて鳴らしたい」という報告を受けて
  dmix 導入に切り替えた。`_mixing_ready()` が `sentinel_voice` という
  named PCM の存在を都度確認し、用意できていれば曲を止めずに重ねて鳴らす
  — 用意できていない場合 (LADSPA プラグイン欠如以外の何らかの理由で
  `sentinel-setup-audio-mixing.sh` が失敗していた場合など) だけ、
  `duck_for_voice()`/`resume_from_voice()` による旧来の「完全に止めて
  から喋る」経路に自動でフォールバックする。

  重ねて鳴らす場合も、音楽を常に全音量のまま流し続けるわけではない。
  「アナウンスの声が音楽に埋もれて聞き取りにくい」という要望があり、
  `music.duck_volume_for_voice()`/`resume_volume_after_voice()` が
  `voice_duck_percent` の設定に従ってアナウンス中だけ音楽の音量を
  一時的に下げ、話し終えたら元に戻す。`duck_for_voice()` (曲を完全に
  停止する) とは別の、より軽い経路 — mpg123 を止めも開き直しもせず、
  再生中でも即座に効く `V <percent>` コマンドで音量だけ動かすため、
  sudo も asound.conf の書き換えも一切経由しない。

  Bluetooth 接続中だけは別に例外で、`bluealsa-aplay` がこの dmix を
  経由せず ALSA デバイスを直接掴むため、割り込むと双方が壊れる。その間は
  アナウンス自体を静かにスキップする (元々の設計のまま)。

- 音量は `sentinel_voice` PCM 自身が持つ ALSA softvol コントロール
  ("SentinelVoice"、`amixer -c <card> sset SentinelVoice <%>`) を操作
  する。**bluetooth._apply_volume() や以前のこのモジュールが使っていた
  numid=1 (PCM Playback Volume、ハードウェアのアンプそのもの) とは別物**
  — numid=1 は音楽・Bluetooth・この Pi の出力全体で共有される 1 つの
  ハードウェアレジスタなので、ここを声のたびに書き換えると、たとえ
  ducking で曲を止めていても「音楽の音量がいつの間にか変わる」ことに
  なっていた (実際に報告された不具合)。softvol はストリームごとに独立
  したソフトウェアゲインを持つため、この問題自体が起こらない。

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

from ..core import audio, config
from . import music

log = logging.getLogger("sentinel.voice")

# 同じ dmix エラーでログを埋めないための直近メッセージ。
_mix_warned: str = ""

_QUEUE: "asyncio.Queue[tuple[str, str]]" = asyncio.Queue(maxsize=20)
_LOOP: asyncio.AbstractEventLoop | None = None

STATE = {"spoken": 0, "skipped": 0, "last_error": "", "last_spoken_at": 0.0,
        "last_text": "", "engine": ""}

# カテゴリ -> (有効フラグの設定キー, 読み上げ文テンプレートの設定キー, 既定テンプレート)
_CATEGORY_KEYS = {
    "time": ("voice_time_enabled", "voice_time_text", "{hour}時{minute_part}です"),
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
    _LOOP.call_soon_threadsafe(_enqueue, text, category)


def _enqueue(text: str, category: str) -> None:
    try:
        _QUEUE.put_nowait((text, category))
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


def _mixing_ready() -> bool:
    """sentinel-setup-audio-mixing.sh (CLAUDE.md #31) が sentinel_voice/
    sentinel_music という named PCM を実際に用意できているかどうか。
    bootstrap.sh は毎回これを試みるが、対応する LADSPA プラグインの
    欠如以外にも、カードが検出できない等で結局書き込めていない可能性は
    ゼロではない。用意できていない状態でその名前を渡しても ALSA が開けず
    ただ無音になるだけなので、そのときだけ曲を完全に止めてから喋る
    旧来の経路 (music.duck_for_voice()/resume_from_voice()) へ自動的に
    フォールバックする — 「重ねて鳴らせないなら、せめて交互にでも鳴らす」
    という段階的劣化。

    **`aplay -L` に名前があるかどうかでは判定しない。** あの一覧は
    /etc/asound.conf に定義が書いてあることしか意味せず、dmix はスレーブ
    (`hw:N,0`) を開いて初めて失敗する。名前は出続けるのに開けない状態で
    `aplay -D sentinel_voice` を実行すると、ducking もされないまま何も
    鳴らず、しかも音楽側も同じ理由で無音、という「エラーが無いのに
    どちらも鳴らない」状態になっていた。core/audio.pcm_opens() で実物を
    試す (CLAUDE.md #8 の can_write() と同じ原則)。"""
    if not config.get("audio_mixing_enabled"):
        return False
    ok, err = audio.pcm_opens("sentinel_voice")
    if not ok and err:
        global _mix_warned
        if err != _mix_warned:
            _mix_warned = err
            log.warning("sentinel_voice (dmix) を開けないため、曲を止めてから読み上げます: %s", err)
    return ok


def _fallback_device() -> str | None:
    """dmix が使えないときに aplay へ渡すデバイス。

    `-D` を付けずに鳴らすと ALSA の既定デバイスへ流れる。複数カードある
    Pi では既定が HDMI になることが多く、「読み上げたつもりなのに 3.5mm
    からは何も聞こえない」というエラーの出ない無音になる — music.py の
    _spawn() が同じ理由で plughw を明示しているのと同じ対策
    (CLAUDE.md #37)。"""
    card = _sound_card()
    return f"plughw:{card},0" if card is not None else None


def _sound_card() -> int | None:
    # core/audio.find_output_card() を使う理由は core/audio.py の docstring
    # 参照 — aplay -l の最初のカードを無条件で使うと、機体によっては
    # bcm2835 のアナログ出力ではなく HDMI を掴んでしまう。
    return audio.find_output_card()


_CHIME_PATH = config.RUNTIME / "voice-chime.wav"

# 直近に警告した voice_chime_path の値。同じ壊れたパスを設定したまま毎回
# 時報が鳴るたびにログを埋めないための、_mix_warned と同じパターン。
_chime_path_warned: str = ""


def _chime_path() -> tuple["Path", bool]:
    """時報と重ねて鳴らす効果音の実ファイルと、mp3 かどうかを返す。

    `voice_chime_path` (設定タブ) に実在するファイルパスが入っていれば
    それを使う — .wav ならそのまま aplay へ、.mp3 なら音楽ライブラリと
    同じ mp3 前提 (CLAUDE.md #2) で mpg123 の単発再生に渡す。空文字・
    存在しない・対応しない拡張子のいずれかであれば、標準ライブラリの
    wave/math で合成した既定のチャイムへ静かにフォールバックする —
    「時報自体は鳴らし続ける」という CLAUDE.md 全体の段階的劣化方針と
    同じ考え方。合成音は初回だけ生成してキャッシュする — バイナリ音源を
    同梱しない (CLAUDE.md「依存を増やさない」と同じ判断)。生成先は
    config.RUNTIME (tmpfs) — 高頻度書き込みではなく初回の 1 回きりだが、
    他の実行時生成物 (motion_debug.jsonl など) と同じ置き場所に揃えて
    いる (CLAUDE.md #3)。"""
    from pathlib import Path

    global _chime_path_warned
    custom = str(config.get("voice_chime_path") or "").strip()
    if custom:
        p = Path(custom)
        suffix = p.suffix.lower()
        if suffix in (".wav", ".mp3") and p.is_file():
            _chime_path_warned = ""
            return p, suffix == ".mp3"
        if custom != _chime_path_warned:
            _chime_path_warned = custom
            log.warning("voice_chime_path (%s) が見つからないか .wav/.mp3 以外のため、既定の効果音を使います", custom)

    path: Path = _CHIME_PATH
    if not path.exists() or path.stat().st_size <= 44:
        _synthesize_chime(path)
    return path, False


def _synthesize_chime(path) -> None:
    import math
    import struct
    import wave

    rate = 22050
    # A5 -> E6 の 2 音、ベル風の減衰チャイム。全体で 0.5 秒弱 - 時報の
    # 発話時間 (少なくとも数秒) の頭に確実に収まり、聞こえた瞬間に
    # 「時報が来た」と分かる程度の長さにしている。
    notes = ((880.0, 0.16), (1318.51, 0.26))
    samples: list[int] = []
    for freq, dur in notes:
        n = int(rate * dur)
        fade = max(1, int(n * 0.12))
        for i in range(n):
            env = 1.0
            if i < fade:
                env = i / fade
            elif i > n - fade:
                env = (n - i) / fade
            val = math.sin(2 * math.pi * freq * i / rate) * env * 0.5
            samples.append(int(max(-1.0, min(1.0, val)) * 32767))
        samples.extend([0] * int(rate * 0.02))
    tmp = path.with_suffix(".tmp")
    with wave.open(str(tmp), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack("<%dh" % len(samples), *samples))
    tmp.replace(path)


def _play_chime(device: str | None) -> "subprocess.Popen | None":
    """効果音を非同期に鳴らし始める。呼び出し側が、これと並行して TTS の
    合成・再生を進めることで「重ねて鳴る」を実現する — チャイムの再生
    終了を待たずに戻る。dmix (sentinel_voice) が使えないときは呼ばない
    (曲を完全に止める旧経路と衝突させても意味がないため、_speak_sync 側
    で mixing 中のみ呼ぶ)。"""
    try:
        path, is_mp3 = _chime_path()
    except Exception as exc:
        log.warning("効果音の生成に失敗しました: %s", exc)
        return None
    if is_mp3:
        # mpg123 の単発再生。常駐する music.Player とは別プロセスで、
        # -a には sentinel_voice (dmix 経由) を渡すため、音楽ライブラリの
        # 常駐 mpg123 (-a sentinel_music) とは別の PCM スロットに入り、
        # 互いに干渉しない (CLAUDE.md #31)。
        cmd = ["mpg123", "-q", "-o", "alsa"] + (["-a", device] if device else []) + [str(path)]
    else:
        cmd = ["aplay", "-q"] + (["-D", device] if device else []) + [str(path)]
    try:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        log.warning("効果音の再生に失敗しました: %s", exc)
        return None


def _apply_volume(percent: int) -> None:
    card = _sound_card()
    if card is None:
        return
    percent = max(0, min(100, percent))
    try:
        subprocess.run(["amixer", "-c", str(card), "sset", "SentinelVoice", f"{percent}%"],
                       capture_output=True, timeout=5, check=False)
    except Exception:
        pass


def _speak_open_jtalk(text: str, rate: float, device: str | None) -> bool:
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
        cmd = ["aplay", "-q"] + (["-D", device] if device else []) + [str(wav_path)]
        subprocess.run(cmd, capture_output=True, timeout=30, check=False)
        return True
    except Exception as exc:
        log.warning("open_jtalk での再生に失敗しました: %s", exc)
        return False
    finally:
        wav_path.unlink(missing_ok=True)


def _speak_espeak(text: str, rate: float, device: str | None) -> bool:
    if not _has_espeak():
        return False
    lang = str(config.get("voice_lang") or "ja")
    # voice_rate は「速さの倍率」(0.5-2.0) という、Open JTalk の -r と
    # 共通の意味で持っている。espeak-ng は words-per-minute を取るため、
    # 既定 150wpm を基準に変換する。
    wpm = int(max(80, min(400, 150 * rate)))
    try:
        # --stdout で WAV をパイプへ吐かせ、open_jtalk と同じ経路で aplay
        # に流す。espeak-ng 自身のデバイス選択機構 (環境変数頼み) に任せる
        # より、経路を両エンジンで揃えた方が確実。
        espeak = subprocess.Popen(
            ["espeak-ng", "-v", lang, "-s", str(wpm), "--stdout", text],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        cmd = ["aplay", "-q"] + (["-D", device] if device else [])
        subprocess.run(cmd, stdin=espeak.stdout, capture_output=True, timeout=30, check=False)
        espeak.wait(timeout=5)
        return True
    except Exception as exc:
        log.warning("espeak-ng での再生に失敗しました: %s", exc)
        return False


def _speak_sync(text: str, device: str | None, chime: bool = False) -> None:
    _apply_volume(int(config.get("voice_volume")))
    # チャイムは TTS の合成 (open_jtalk/espeak-ng) を待たずに鳴らし始める。
    # 合成には短い時間がかかるが、鳴らし終わりは finally で必ず回収する
    # (回収しないと aplay の短命プロセスがゾンビのまま残り続ける)。
    chime_proc = _play_chime(device) if chime else None
    try:
        rate = float(config.get("voice_rate"))
        ok = False
        if _has_open_jtalk():
            ok = _speak_open_jtalk(text, rate, device)
            if ok:
                STATE["engine"] = "open_jtalk"
        if not ok:
            ok = _speak_espeak(text, rate, device)
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
    finally:
        if chime_proc is not None:
            try:
                chime_proc.wait(timeout=5)
            except Exception:
                pass


def speak_test(text: str) -> tuple[bool, str]:
    """設定タブの「テスト再生」用。キューを経由せず即座に鳴らす。"""
    mixing = _mixing_ready()
    # 重ねて鳴らせる場合は曲を止めず、voice_duck_percent の設定に従って
    # 音量だけ一時的に下げる (duck_volume_for_voice())。重ねられない場合
    # だけ、以前どおり曲を完全に止める (duck_for_voice())。
    stopped = music.duck_for_voice() if not mixing else False
    ducked = music.duck_volume_for_voice() if mixing else False
    try:
        _speak_sync(text, "sentinel_voice" if mixing else _fallback_device())
    finally:
        if stopped:
            music.resume_from_voice()
        if ducked:
            music.resume_volume_after_voice()
    if STATE["last_error"]:
        return False, STATE["last_error"]
    return True, f"再生しました ({STATE['engine']})"


async def loop() -> None:
    global _LOOP
    _LOOP = asyncio.get_running_loop()
    while True:
        text, category = await _QUEUE.get()
        # Bluetooth 接続中は bluealsa-aplay がこの dmix を経由せず ALSA
        # デバイスを直接掴んでいる。割り込むと相手の再生を壊すだけなので
        # 静かに諦める (モジュール読み込み順の都合で遅延 import する)。
        from . import bluetooth as _bt
        if _bt.STATE.get("connected"):
            STATE["skipped"] += 1
            continue
        # dmix ミキシングが用意できていれば曲を止めずに重ねて鳴らす
        # (CLAUDE.md #31)。voice_duck_percent の設定に従って音楽の音量
        # だけ一時的に下げ (duck_volume_for_voice())、話し終えたら元の
        # 音量に戻す。重ねられない場合だけ、以前どおり曲を完全に止めて
        # から喋る (_mixing_ready() 参照)。
        mixing = await asyncio.to_thread(_mixing_ready)
        stopped = await asyncio.to_thread(music.duck_for_voice) if not mixing else False
        ducked = await asyncio.to_thread(music.duck_volume_for_voice) if mixing else False
        # 効果音は時報だけに付け、かつ dmix で重ねられるときだけ鳴らす —
        # 重ねられない (曲を完全に止める) 経路では、効果音自体もう1つの
        # 排他デバイス争いを増やすだけで「同時に」という要望を満たせない
        # ため、鳴らさずに諦める (Bluetooth 接続中の読み上げスキップと
        # 同じ「重ねられないなら無理に鳴らさない」方針)。
        chime = mixing and category == "time" and bool(config.get("voice_chime_enabled"))
        try:
            await asyncio.to_thread(_speak_sync, text,
                                    "sentinel_voice" if mixing else _fallback_device(), chime)
        finally:
            if stopped:
                await asyncio.to_thread(music.resume_from_voice)
            if ducked:
                await asyncio.to_thread(music.resume_volume_after_voice)


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
        # ちょうど 0 分のときに「12時0分です」と言うと不自然なので、その
        # 場合だけ {minute_part} を空文字にする ({minute} は生の数値のまま
        # 残すので、自前のテンプレートで "{minute}分" を使い続けたい場合も
        # 壊れない)。
        minute_part = f"{now.tm_min}分" if now.tm_min else ""
        announce("", "time", hour=now.tm_hour, minute=now.tm_min, minute_part=minute_part)
