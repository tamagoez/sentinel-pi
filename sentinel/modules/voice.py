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
  音楽・音声アナウンスのどちらも `core/audio.analog_device()` が返す
  同じ `sysdefault:CARD=<N>` (alsa-lib 自身の per-card dmix ルート) へ
  書き込むため、設定ファイルを一切必要とせず自動的に重なって鳴る
  (CLAUDE.md の音声ミキシング刷新の節)。以前はここを別々の名前つき PCM
  (`sentinel_music`/`sentinel_voice`) + 手書きの `/etc/asound.conf` で
  実現していたが、カード番号のズレや LADSPA プラグインの欠如で簡単に
  壊れ、壊れると「重ねられない」判定に落ちて曲を毎回完全停止する旧経路
  (`duck_for_voice()`) へ静かにフォールバックしていた。sysdefault は
  常に (追加設定なしに) 使えるため、この「重ねられない」場合分けそのもの
  が無くなった。

  音楽を常に全音量のまま流し続けるわけでもない。「アナウンスの声が音楽に
  埋もれて聞き取りにくい」という要望があり、
  `music.duck_volume_for_voice()`/`resume_volume_after_voice()` が
  `voice_duck_percent` の設定に従ってアナウンス中だけ音楽の音量を
  一時的に下げ、話し終えたら元に戻す。mpg123 を止めも開き直しもせず、
  再生中でも即座に効く `V <percent>` コマンドで音量だけ動かすため、
  sudo も外部設定ファイルの書き換えも一切経由しない。

  Bluetooth 接続中だけは別に例外で、`bluealsa-aplay` から流れてくる
  相手の音声にこちらのアナウンスが割り込むのは体験として望ましくない
  ため、その間はアナウンス自体を静かにスキップする (技術的な制約では
  なく意図した挙動 — sysdefault は複数ストリームを受け付けられるが、
  「電話の音楽に日本語の時報が混ざる」体験を避けるための選択)。

- 音量は TTS/効果音が書き出す WAV のサンプルを Python 側で直接スケール
  する (`_scale_wav()`、標準ライブラリの `wave`/`array` のみ)。
  **`bluetooth._apply_volume()` や以前のこのモジュールが使っていた
  numid=1 (PCM Playback Volume、ハードウェアのアンプそのもの) や、
  専用の ALSA softvol コントロールとは別物** — numid=1 は音楽・
  Bluetooth・この Pi の出力全体で共有される 1 つのハードウェアレジスタ
  なので、ここを声のたびに書き換えると「音楽の音量がいつの間にか変わる」
  ことになっていた (実際に報告された不具合)。専用の softvol コントロール
  は問題自体は避けられるが、`/etc/asound.conf` を経由する外部状態を
  もう一つ増やすことになり、カード番号がズレる・定義が壊れるといった
  この項目独自の故障モードを抱えていた。WAV のサンプルを直接スケール
  すれば、どちらの問題も原理的に起こらない — 音量はファイルの中身その
  ものに反映され、ALSA 側の状態には一切依存しない。

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
    書式が来ても例外で読み上げ全体を落とさず、既定文へ静かに戻す。
    `{time}` (現在時刻 HH:MM) はどのカテゴリでも共通して使えるよう、
    呼び出し元が明示的に渡していなければここで補う — voice_error_text/
    voice_other_text/voice_camera_reboot_text のような {message} だけの
    テンプレートでも「いつ起きたか」を文面に含められるようにするため
    (時報カテゴリは hour/minute など専用のプレースホルダを別途渡す)。"""
    values = {"time": time.strftime("%H:%M"), **values}
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


def _device() -> str | None:
    """aplay/mpg123 へ渡す出力デバイス。music.current_output_device() を
    そのまま使う — BGM が Bluetooth 出力機器 (bt_output_device) へ流れて
    いるときはそちらへ、そうでなければ AUX (core/audio.analog_device()、
    `sysdefault:CARD=<N>`) へ、常に音楽と同じ場所から音声アナウンスが
    聞こえるようにする。"""
    return music.current_output_device()


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


def _scale_wav(path, percent: int) -> None:
    """WAV ファイルのサンプル振幅を percent (0-100) 倍にその場で書き換える。

    ALSA 側のミキサー/softvol を一切経由しない — ファイルの中身そのものを
    変えるので、mpg123・Bluetooth と共有する出力経路の状態には何も依存
    しない (このモジュール docstring の音量に関する節を参照)。標準
    ライブラリの `wave`/`array` のみで完結し、Python 3.13 で削除された
    `audioop` には依存しない。16bit PCM 前提 — open_jtalk/espeak-ng の
    WAV 出力、および合成チャイムはどちらもこの形式で書き出している。"""
    import array
    import wave

    percent = max(0, min(100, percent))
    if percent == 100:
        return
    factor = percent / 100.0
    with wave.open(str(path), "rb") as wf:
        params = wf.getparams()
        frames = wf.readframes(wf.getnframes())
    if params.sampwidth != 2:
        return
    samples = array.array("h")
    samples.frombytes(frames)
    for i, s in enumerate(samples):
        samples[i] = max(-32768, min(32767, int(s * factor)))
    with wave.open(str(path), "wb") as wf:
        wf.setparams(params)
        wf.writeframes(samples.tobytes())


def _play_chime(device: str | None, percent: int) -> "subprocess.Popen | None":
    """効果音を非同期に鳴らし始める。呼び出し側が、これと並行して TTS の
    合成・再生を進めることで「重ねて鳴る」を実現する — チャイムの再生
    終了を待たずに戻る。"""
    try:
        path, is_mp3 = _chime_path()
    except Exception as exc:
        log.warning("効果音の生成に失敗しました: %s", exc)
        return None
    percent = max(0, min(100, percent))
    if is_mp3:
        # mpg123 の単発再生。常駐する music.Player とは別プロセスで、同じ
        # sysdefault:CARD=<N> (dmix 経由) を使うため、音楽ライブラリの
        # 常駐 mpg123 とは別ストリームとして互いに干渉せず重なる。
        # 音量は mpg123 自身の -f (スケールファクタ、既定 32768=1.0) で
        # 掛ける — .wav 側の _scale_wav() と揃えて、ここも ALSA 側の
        # 状態には一切触れない。
        scale = int(32768 * percent / 100)
        cmd = (["mpg123", "-q", "-o", "alsa", "-f", str(scale)]
               + (["-a", device] if device else []) + [str(path)])
    else:
        if percent < 100:
            try:
                _scale_wav(path, percent)
            except Exception as exc:
                log.warning("効果音の音量調整に失敗しました: %s", exc)
        cmd = ["aplay", "-q"] + (["-D", device] if device else []) + [str(path)]
    try:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        log.warning("効果音の再生に失敗しました: %s", exc)
        return None


def _speak_open_jtalk(text: str, rate: float, device: str | None, percent: int) -> bool:
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
        _scale_wav(wav_path, percent)
        cmd = ["aplay", "-q"] + (["-D", device] if device else []) + [str(wav_path)]
        subprocess.run(cmd, capture_output=True, timeout=30, check=False)
        return True
    except Exception as exc:
        log.warning("open_jtalk での再生に失敗しました: %s", exc)
        return False
    finally:
        wav_path.unlink(missing_ok=True)


def _speak_espeak(text: str, rate: float, device: str | None, percent: int) -> bool:
    if not _has_espeak():
        return False
    lang = str(config.get("voice_lang") or "ja")
    # voice_rate は「速さの倍率」(0.5-2.0) という、Open JTalk の -r と
    # 共通の意味で持っている。espeak-ng は words-per-minute を取るため、
    # 既定 150wpm を基準に変換する。
    wpm = int(max(80, min(400, 150 * rate)))
    # --stdout の出力を一旦 WAV ファイルへ落としてから音量を調整する —
    # open_jtalk と同じ経路に揃えることで、音量スケールを一箇所
    # (_scale_wav()) だけに保てる (ストリームを直接 aplay へパイプすると
    # 音量調整を挟む場所がなくなる)。
    wav_path = config.RUNTIME / f"voice-{uuid.uuid4().hex}.wav"
    try:
        with open(wav_path, "wb") as f:
            subprocess.run(["espeak-ng", "-v", lang, "-s", str(wpm), "--stdout", text],
                           stdout=f, stderr=subprocess.DEVNULL, timeout=20, check=False)
        if not wav_path.exists() or wav_path.stat().st_size < 100:
            return False
        _scale_wav(wav_path, percent)
        cmd = ["aplay", "-q"] + (["-D", device] if device else []) + [str(wav_path)]
        subprocess.run(cmd, capture_output=True, timeout=30, check=False)
        return True
    except Exception as exc:
        log.warning("espeak-ng での再生に失敗しました: %s", exc)
        return False
    finally:
        wav_path.unlink(missing_ok=True)


def _speak_sync(text: str, device: str | None, chime: bool = False) -> None:
    percent = max(0, min(100, int(config.get("voice_volume"))))
    # チャイム自体の音量は voice_volume (読み上げ本体) とは独立した
    # voice_chime_volume を使う。同じ値を共有していると、チャイムが声を
    # かき消して聞き取れない場合にどちらも一緒に下げるしかなかった
    # (実際に報告された不具合)。
    chime_percent = max(0, min(100, int(config.get("voice_chime_volume"))))
    # チャイムは TTS の合成 (open_jtalk/espeak-ng) を待たずに鳴らし始める。
    # 合成には短い時間がかかるが、鳴らし終わりは finally で必ず回収する
    # (回収しないと aplay の短命プロセスがゾンビのまま残り続ける)。
    chime_proc = _play_chime(device, chime_percent) if chime else None
    try:
        rate = float(config.get("voice_rate"))
        ok = False
        if _has_open_jtalk():
            ok = _speak_open_jtalk(text, rate, device, percent)
            if ok:
                STATE["engine"] = "open_jtalk"
        if not ok:
            ok = _speak_espeak(text, rate, device, percent)
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


def announce_blocking(message: str = "", category: str = "other", **extra) -> bool:
    """announce() の同期版。キューへ積んで即座に戻るのではなく、実際に
    読み上げ (合成+再生) が終わるまでブロックしてから戻る。

    Pi を実際に再起動する直前 (maintenance.emergency_reboot()) のように
    「読み上げが確実に終わってから次の処理へ進みたい」場面のために追加
    した。announce() はキューへ積むだけで即座に戻り、実際の合成・再生は
    別タスクの loop() が非同期に処理するため、呼び出し元が「アナウンス
    した」つもりで先に進んでも、実際にはまだ鳴り始めていない/鳴り終えて
    いないことがある。緊急再起動が絡む場面はまさに Pi が USB/CPU 負荷で
    不安定になっている状況そのもので、Open JTalk の合成にも普段より
    時間がかかりやすい — 固定の数秒だけ待って reboot する実装では、
    読み上げの途中、あるいは始まる前に電源が落ちることがあった。
    呼び出し元は `asyncio.to_thread()` 経由で呼ぶこと (このモジュールの
    他の TTS 合成/再生と同じブロッキング呼び出しのため)。

    voice_enabled とカテゴリ別スイッチ、Bluetooth 接続中のスキップ判定は
    announce()/loop() と揃えている — この経路だけ判定が緩いと、ミュート
    設定にしているのに緊急時だけ喋る、という食い違いになる。"""
    if not config.get("voice_enabled"):
        return False
    enabled_key, text_key, default_tpl = _CATEGORY_KEYS.get(category, _CATEGORY_KEYS["other"])
    if not config.get(enabled_key):
        return False
    from . import bluetooth as _bt
    if _bt.STATE.get("connected"):
        STATE["skipped"] += 1
        return False
    text = _fmt(text_key, default_tpl, {"message": message, **extra})
    interrupt = music.begin_voice_interrupt()
    try:
        _speak_sync(text, _device())
    finally:
        if interrupt:
            music.end_voice_interrupt(interrupt)
    return not STATE["last_error"]


def speak_test(text: str) -> tuple[bool, str]:
    """設定タブの「テスト再生」用。キューを経由せず即座に鳴らす。AUX 出力
    中は voice_duck_percent の設定に従って音量だけ一時的に下げ、
    Bluetooth 出力中は曲を一時停止する (music.begin_voice_interrupt() が
    出力先に応じてどちらか選ぶ)。"""
    interrupt = music.begin_voice_interrupt()
    try:
        _speak_sync(text, _device())
    finally:
        if interrupt:
            music.end_voice_interrupt(interrupt)
    if STATE["last_error"]:
        return False, STATE["last_error"]
    return True, f"再生しました ({STATE['engine']})"


_WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def _time_values(now: "time.struct_time") -> dict:
    """時報テンプレート用のプレースホルダをまとめて組み立てる。
    time_signal_loop() と speak_test_time() の両方がこれを使う — 片方だけ
    に新しいプレースホルダを足して食い違う、という事故を避けるため。

    {hour}/{minute}: 生の数値。{minute_part}: 0 分のとき空文字 ({minute}分
    ではなく「〜時です」と言うための既定文専用、CLAUDE.md 参照)。
    {weekday}: 「月」〜「日」(曜日の 1 文字、「{weekday}曜日」のように
    テンプレート側で組み立てる)。{hour12}/{ampm}: 12時間表記が読み上げに
    向く場合向け。{month}/{day}: 日付を読み上げたいテンプレート向け。"""
    minute_part = f"{now.tm_min}分" if now.tm_min else ""
    hour12 = now.tm_hour % 12 or 12
    return {
        "hour": now.tm_hour, "minute": now.tm_min, "minute_part": minute_part,
        "weekday": _WEEKDAY_JA[now.tm_wday], "hour12": hour12,
        "ampm": "午前" if now.tm_hour < 12 else "午後",
        "month": now.tm_mon, "day": now.tm_mday,
    }


def speak_test_time() -> tuple[bool, str]:
    """設定タブの「時報をテスト」用。voice_time_interval_minutes の境界を
    待たず、今すぐ 1 回だけ time_signal_loop() と全く同じ組み立て
    (_time_values()、voice_time_text テンプレート、voice_chime_enabled に
    従ったチャイム同時再生) で鳴らす。speak_test() は利用者が入力した
    自由文をそのまま読むだけで、時報カテゴリ固有のプレースホルダ組み立て
    やチャイム同時再生を経由しないため、時報の文面・音量・効果音を実際に
    確認したいという要望には別関数が必要だった。**speak_test() を time
    カテゴリで呼び出すだけの実装にしないでください** — {hour}/{minute}
    などを渡さないためテンプレートが `_fmt()` の例外経路 (既定文への
    静かなフォールバック) を踏んでしまい、実際にカスタマイズした文面を
    確認できません。"""
    text = _fmt("voice_time_text", _CATEGORY_KEYS["time"][2],
                {"message": "", **_time_values(time.localtime())})
    interrupt = music.begin_voice_interrupt()
    chime = bool(config.get("voice_chime_enabled"))
    try:
        _speak_sync(text, _device(), chime)
    finally:
        if interrupt:
            music.end_voice_interrupt(interrupt)
    if STATE["last_error"]:
        return False, STATE["last_error"]
    return True, f"再生しました ({STATE['engine']}): {text}"


async def loop() -> None:
    global _LOOP
    _LOOP = asyncio.get_running_loop()
    while True:
        text, category = await _QUEUE.get()
        # Bluetooth 接続中の相手の音声にこちらのアナウンスが割り込むのは
        # 体験として望ましくないため、その間はスキップする (技術的な制約
        # ではなく意図した挙動 — モジュール読み込み順の都合で遅延 import)。
        from . import bluetooth as _bt
        if _bt.STATE.get("connected"):
            STATE["skipped"] += 1
            continue
        # AUX 出力中は音楽の音量だけ一時的に下げ (voice_duck_percent)、
        # Bluetooth 出力中は曲を一時停止する — begin_voice_interrupt() が
        # 現在の出力先を見てどちらか選ぶ。話し終えたら元に戻す。
        interrupt = await asyncio.to_thread(music.begin_voice_interrupt)
        chime = category == "time" and bool(config.get("voice_chime_enabled"))
        try:
            await asyncio.to_thread(_speak_sync, text, _device(), chime)
        finally:
            if interrupt:
                await asyncio.to_thread(music.end_voice_interrupt, interrupt)


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
        # _time_values() が hour/minute/minute_part に加え weekday/hour12/
        # ampm/month/day も渡す — speak_test_time() と全く同じプレース
        # ホルダ集合 (CLAUDE.md 参照)。
        announce("", "time", **_time_values(now))
