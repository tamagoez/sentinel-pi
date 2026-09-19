"""常時 BGM 再生.

再生エンジンは mpg123 のリモート制御モード (-R) を使う。
理由: Pi 3B+ のアナログ出力では ffplay 系がアンダーランを起こしやすい一方、
mpg123 は ALSA へ直接書き込むため軽く、安定している。

mpg123 -R の主なコマンド:
    L  <path>   読み込んで再生開始
    LP <path>   読み込むが再生しない
    P           一時停止 / 解除のトグル
    S           停止 (ファイルを閉じる)
    J <n>s      n 秒の位置へジャンプ
    V <percent> 音量
    Q           終了
出力:
    @F <現在フレーム> <残りフレーム> <現在秒> <残り秒>
    @P <0|1|2>   0=停止 1=一時停止 2=再生中
    @E ...       エラー

再生位置は @F 行から取得し、メモリ上に保持する。
ディスクへの書き込みはモード遷移時と1時間毎のみに限定し、SD カードを守る。

`alsa_device` が空のときは、既定の ALSA デバイスではなく
`sentinel-setup-audio-mixing.sh` (CLAUDE.md #31) が用意する
`sentinel_music` という named PCM (ALSA の dmix 経由) を使う。これにより
音楽と音声アナウンス (modules/voice.py) が同時に重ねて鳴らせる。イコライザー
(music_eq_enabled/music_eq_bands/music_eq_track_overrides) は同じスクリプトが
書く asound.conf の中に LADSPA (mbeq、swh-plugins) 段として挟み込むため、
有効/無効の切り替えやバンド設定の変更は asound.conf の書き換え + mpg123 の
再起動を伴う (ALSA の LADSPA プラグインは alsaequal のような専用の ctl
プラグインを使わない限りライブ調整できないため、設定を跨いだ「聞こえ方の
変化」は曲の切れ目で起きる)。曲ごとに設定が違わない限りは何も再構成せず、
曲間で途切れない。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import collections
import random
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from ..core import audio, config, state
from ..core.state import CRITICAL, ECO, MODE, NORMAL

log = logging.getLogger("sentinel.music")

AUDIO_EXT = {".mp3"}
_SCAN_EXT = {".mp3", ".m4a", ".opus", ".ogg", ".flac", ".wav", ".webm"}

# カテゴリー名 (「勉強用」「休憩用」のような MUSIC_DIR 直下のサブフォルダ名)
# として許す文字。フォルダ名としてそのまま使うため、パス区切りや隠し
# フォルダ化に繋がる文字は避ける。camera_overrides のキーのような自由入力
# ではなくファイルシステム上の実体になるため、ここだけ検証が要る。
_CATEGORY_RE = re.compile(r"^[^/\\.\x00][^/\\\x00]{0,63}$")


def _valid_category(name: str) -> bool:
    """カテゴリー名としてフォルダ名に使って安全か。空文字列 (=「未分類」/
    フィルタなし) は別扱いなのでここでは弾かない — 呼び出し元で判定する。"""
    return bool(_CATEGORY_RE.match(name)) and name not in (".", "..")


def list_categories() -> list[str]:
    """MUSIC_DIR 直下のサブフォルダ名を一覧する。深さ 1 段だけを見る —
    「勉強用」「休憩用」のような分類が目的で、ネストした構造は想定して
    いない。"""
    try:
        return sorted(p.name for p in config.MUSIC_DIR.iterdir()
                     if p.is_dir() and not p.name.startswith("."))
    except Exception:
        return []


def _category_of(track: Path) -> str:
    """MUSIC_DIR からの相対パスの最初のフォルダ名。直下に置かれた曲
    (未分類) は空文字列。"""
    try:
        rel = track.relative_to(config.MUSIC_DIR)
    except ValueError:
        return ""
    return rel.parts[0] if len(rel.parts) > 1 else ""


def find_track_path(name: str) -> Path | None:
    """曲名 (拡張子付きのファイル名) から実際の場所を探す。カテゴリー分け
    (サブフォルダ) 導入前は「曲名 = MUSIC_DIR 直下のファイル名」で済んで
    いたが、カテゴリーは MUSIC_DIR のサブフォルダとして持つため、
    `config.MUSIC_DIR / name` を直接組み立てるだけではカテゴリー内の曲を
    見つけられない。既にスキャン済みの PLAYER.tracks (rglob で全カテゴリー
    を横断済み) から一致するものを探す。同名ファイルが複数カテゴリーに
    存在する場合は最初に見つかったものを返す — 曲名をキーにした既存の
    イコライザー上書き (CLAUDE.md #32、music_eq_track_overrides) と同じ
    「ファイル名で一意」という前提をここでも踏襲している。"""
    for t in PLAYER.tracks:
        if t.name == name:
            return t
    return None


def move_track(name: str, category: str) -> Path:
    """曲をカテゴリー (MUSIC_DIR 直下のサブフォルダ) へ移動する。
    category="" は「未分類」、つまり MUSIC_DIR 直下へ戻すことを意味する。
    呼び出し元は成功後に PLAYER.scan() を呼んで一覧へ反映すること
    (delete_track の既存route と同じパターン、CLAUDE.md 各所)。"""
    src = find_track_path(name)
    if src is None:
        raise FileNotFoundError(name)
    if category and not _valid_category(category):
        raise ValueError("カテゴリー名が使えません (パス区切りや先頭のドットは不可)")
    dest_dir = config.MUSIC_DIR / category if category else config.MUSIC_DIR
    dest = dest_dir / src.name
    if dest.resolve() == src.resolve():
        return dest
    if dest.exists():
        raise FileExistsError(f"「{category or '未分類'}」に同名の曲が既にあります")
    dest_dir.mkdir(parents=True, exist_ok=True)
    src.rename(dest)
    return dest

# mbeq (swh-plugins) の 15 バンドの中心周波数。
# scripts/sentinel-setup-audio-mixing.sh へ渡す順序と一致させること。
EQ_BAND_HZ = ["50", "100", "156", "220", "311", "440", "622", "880",
             "1250", "1750", "2500", "3500", "5000", "10000", "20000"]

# 直近に実際に適用した (enabled, bands) の組。同じ組が来たら asound.conf の
# 再構成・mpg123 の再起動をスキップする — 曲を切り替えるたびに無条件で
# 再構成すると、EQ 設定が変わっていない曲間にも毎回ギャップができてしまう。
_last_applied_eq: tuple | None = None

# _apply_audio_mixing() が直近に失敗した時刻。mpg123 が停止して loop() の
# 自己修復ループが 5 秒おきに play() を呼び直すとき、_sync_eq() が毎回
# _apply_audio_mixing() (sudo 経由の外部スクリプト、最大 30 秒かかる) を
# 再試行すると、self._lock を握ったまま 30 秒ブロックする試行が 5 秒おきに
# 積み重なり、status() など他の Player 操作も巻き添えで固まる — 実機の
# 「mpg123 が停止して、復帰を試みても復帰できない」不具合の原因だった。
# 一度失敗したら _EQ_RETRY_COOLDOWN_SEC が経つまで _apply_audio_mixing()
# を呼ばない (mpg123 自体の再起動 = _spawn() はこの成否に関わらず進む)。
_eq_sync_failed_at: float = 0.0

# 直近に asound.conf へ実際に書かれた EQ の有無 (要求値ではない)。
_last_effective_eq: bool | None = None
_EQ_RETRY_COOLDOWN_SEC = 60.0


# 同じ dmix エラーでログを埋めないための直近メッセージ。
_mix_warned: str = ""


def _card_index() -> int | None:
    return audio.find_output_card()


def _asound_card() -> int | None:
    """/etc/asound.conf の dmix スレーブ (`pcm "hw:N,0"`) に実際に焼き込まれて
    いるカード番号。読めなければ None。"""
    try:
        text = Path("/etc/asound.conf").read_text(errors="replace")
    except Exception:
        return None
    m = re.search(r'pcm\s+"hw:(\d+),\d+"', text)
    return int(m.group(1)) if m else None


def _asound_eq_on() -> bool:
    """/etc/asound.conf に LADSPA (mbeq) 段が実際に書かれているか。
    sentinel-setup-audio-mixing.sh は EQ 付きの構成が開けなければ黙って
    EQ 無しへ落とすし、sentinel-fix-audio-output.sh も壊れた asound.conf
    を EQ 無しで書き直す。Player 側の記憶だけを信じると、ファイルの実態と
    ズレたまま二度と直らない。"""
    try:
        return "type ladspa" in Path("/etc/asound.conf").read_text(errors="replace")
    except Exception:
        return False


def _mixing_ready() -> bool:
    """sentinel_music (dmix 経由) が **実際に開けるか** (voice.py の
    _mixing_ready() と対になる)。

    以前は `aplay -L` の一覧に名前があるかどうかだけを見ていた。しかし
    その一覧は /etc/asound.conf に定義が書いてあることしか意味せず、dmix
    はスレーブ (`hw:N,0`) を開いて初めて失敗する — カード番号のズレ、他の
    プロセスによるカードの占有、bcm2835 が開閉の連発で固まった状態
    (CLAUDE.md #45) のいずれでも、名前は一覧に出続けるのに開けない。
    その状態で mpg123 に `-a sentinel_music` を渡すと、**エラーも音も
    出ないまま無音になる**。core/audio.pcm_opens() で実物を試す
    (CLAUDE.md #8 の can_write() と同じ原則)。

    設定タブの audio_mixing_enabled を切ると、この経路自体を使わずに
    常にアナログ出力へ直接書く (dmix / 音声アナウンスとの同時再生 /
    イコライザーを疑うときの切り分け用スイッチ)。"""
    if not config.get("audio_mixing_enabled"):
        return False
    ok, err = audio.pcm_opens("sentinel_music")
    if not ok and err:
        global _mix_warned
        if err != _mix_warned:
            _mix_warned = err
            log.warning("sentinel_music (dmix) を開けないため、アナログ出力へ直接再生します: %s", err)
    return ok


def resolve_eq_bands(track_name: str | None) -> list[float]:
    """曲名 (track_path.name) に対して実際に使うべき 15 バンドのゲイン
    (dB) を返す。曲ごとの上書き (music_eq_track_overrides) があればそれを
    優先し、無ければ全体設定 (music_eq_bands)、それも無ければ 0dB (フラット)。"""
    global_bands = config.get("music_eq_bands") or {}
    overrides = (config.get("music_eq_track_overrides") or {}).get(track_name or "") or {}
    out = []
    for hz in EQ_BAND_HZ:
        if hz in overrides:
            out.append(float(overrides[hz]))
        elif hz in global_bands:
            out.append(float(global_bands[hz]))
        else:
            out.append(0.0)
    return out


def _apply_audio_mixing(enabled: bool, bands: list[float] | None) -> tuple[bool, bool]:
    card = _card_index()
    if card is None:
        return False, False
    script = config.APP_ROOT.parent / "scripts" / "sentinel-setup-audio-mixing.sh"
    args = ["sudo", "-n", str(script), str(card), "on" if enabled else "off"]
    if enabled:
        args += [f"{b:g}" for b in (bands or [0.0] * len(EQ_BAND_HZ))]
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except Exception as exc:
        log.warning("音声ミキシング設定の更新に失敗しました: %s", exc)
        return False, False
    if p.returncode != 0:
        log.warning("sentinel-setup-audio-mixing.sh が失敗しました: %s",
                    (p.stderr or p.stdout or "").strip())
        audio.invalidate_pcm_cache()
        return False, False
    # asound.conf が書き換わった = 前回の「開ける/開けない」の判定はもう古い。
    audio.invalidate_pcm_cache()
    # スクリプトは LADSPA プラグインが無い / EQ 付きの構成が再生テストに
    # 失敗した場合、黙って EQ 無しへ落として成功する (CLAUDE.md #32)。
    # 最後の EQ_ACTIVE= 行がそのときの実際の状態なので、こちらを覚えて
    # おかないと「要求は on、ファイルは off」というズレが毎曲ごとの再構成
    # (= 曲間ギャップ) を招く。
    effective = enabled
    for line in (p.stdout or "").splitlines():
        if line.startswith("EQ_ACTIVE="):
            effective = line.split("=", 1)[1].strip() == "on"
    return True, effective


class Player:
    """mpg123 の単一インスタンスを抱えるラッパー。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        # mpg123 プロセスを spawn するたびに +1 する世代カウンタ。
        # _read_loop() が読む「@P 0 (停止)」は、EQ 再構成 (_sync_eq()) や
        # eco/Bluetooth/voice の退避のように、こちらが意図してプロセスを
        # 殺したときにも届く。そのプロセスの stdout パイプに既に溜まって
        # いた行は、次の spawn で新しい read_loop スレッドに置き換わった
        # 「あと」でも古いスレッドがまだ処理し続けるため、殺した直後に
        # (suspended_by を見る前に) play() 側が suspended_by を "" へ
        # 戻してしまうと、古いスレッドはそれを「本当に曲が終わった」と
        # 誤認して次の曲へ進めてしまう — 実機で「曲が2秒ほどで次々に
        # 変わっていく」として報告された不具合の原因。世代が変われば
        # そのメッセージは無条件で無視する (suspended_by の有無に関わらず)。
        self._gen = 0

        self.tracks: list[Path] = []
        self.order: list[int] = []
        self.cursor: int = 0
        self.position: float = 0.0
        self.duration: float = 0.0
        self.playing: bool = False
        self.suspended_by: str = ""        # "eco" | "bluetooth" | "" (退避理由)
        self.last_error: str = ""
        self.seed: int = 0
        # _spawn() が実際に mpg123 へ渡した -a の値。dmix (sentinel_music)
        # が使えず plughw:N,0 へフォールバックした状態を loop() が検知し、
        # dmix が復旧していないか定期的に確認できるようにするため
        # (CLAUDE.md #67)。
        self.active_device: str = ""

        self._dirty = False
        self._last_persist = 0.0

    # -------------------------------------------------- ライブラリ

    def scan(self) -> None:
        with self._lock:
            found = sorted(p for p in config.MUSIC_DIR.rglob("*")
                           if p.is_file() and p.suffix.lower() in AUDIO_EXT)
            cat_filter = str(config.get("music_category_filter") or "")
            if cat_filter:
                # 「勉強用」「休憩用」のようなカテゴリー分け。フォルダが
                # 消えている/リネームされているなど、フィルタ先が実際には
                # 1 曲も無ければ全曲へ静かにフォールバックする — 空の
                # 再生対象で立ち往生するより、まず鳴らし続ける方を優先。
                narrowed = [p for p in found if _category_of(p) == cat_filter]
                if narrowed:
                    found = narrowed
            current = self.current_path()
            self.tracks = found
            self._rebuild_order(keep=current)

    def _rebuild_order(self, keep: Path | None = None) -> None:
        n = len(self.tracks)
        idx = list(range(n))
        if config.get("music_shuffle"):
            seed = int(config.get("music_shuffle_seed") or 0)
            if not seed:
                seed = random.randrange(1, 2 ** 31)
                config.update({"music_shuffle_seed": seed})
            self.seed = seed
            random.Random(seed).shuffle(idx)
        else:
            self.seed = 0
        self.order = idx
        if keep is not None and keep in self.tracks:
            t = self.tracks.index(keep)
            if t in self.order:
                self.cursor = self.order.index(t)
        self.cursor = min(self.cursor, max(0, n - 1))

    def current_path(self) -> Path | None:
        with self._lock:
            if not self.order or self.cursor >= len(self.order):
                return None
            i = self.order[self.cursor]
            if i >= len(self.tracks):
                return None
            return self.tracks[i]

    # -------------------------------------------------- プロセス

    def _spawn(self) -> bool:
        if shutil.which("mpg123") is None:
            self.last_error = "mpg123 がインストールされていません"
            log.error(self.last_error)
            return False
        # -o alsa: mpg123 は複数の出力モジュール (alsa/jack/pulse/...) を
        # 持ち、指定が無いと順に試して最初に開けたものを使う。実機では
        # ALSA (dmix) が開けなかったときに JACK モジュールへ落ち、
        # "jack server is not running" で即死する → 5 秒ごとの復帰ループ、
        # という遠回りな壊れ方をした。このプロジェクトは ALSA へ直接
        # 書く前提 (CLAUDE.md #2) なので、モジュールを固定して「駄目なら
        # ALSA のエラーで正直に落ちる」ようにする。**この -o alsa を
        # 外さないでください** — 同じ「JACK を探しに行って死ぬ」に戻り、
        # 本当の ALSA のエラーがログから消えます。
        cmd = ["mpg123", "-o", "alsa", "-R",
               "--buffer", str(int(config.get("mpg123_buffer_kb")))]
        dev = str(config.get("alsa_device") or "").strip()
        if not dev:
            if _mixing_ready():
                dev = "sentinel_music"   # dmix 経由。音声アナウンスと同時に鳴らせる
            else:
                # dmix のセットアップが失敗している機体で、-a を付けずに
                # mpg123 を起動すると ALSA の既定デバイスへ流れる。複数
                # カードある Pi では既定が HDMI (card 0) になることが多く、
                # mpg123 は正常に開けてしまうので「再生中と表示されるのに
                # 3.5mm から何も聞こえない」という、エラーの出ない無音に
                # なる (実機で踏んだ)。CLAUDE.md #37 と同じ優先順位で
                # 見つけたアナログ出力カードを明示的に指定する。
                card = _card_index()
                if card is not None:
                    dev = f"plughw:{card},0"
        if dev:
            cmd += ["-a", dev]
        self.active_device = dev
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, bufsize=1)
        except Exception as exc:
            self.last_error = f"mpg123 の起動に失敗: {exc}"
            log.exception(self.last_error)
            return False
        self._gen += 1
        self._reader = threading.Thread(target=self._read_loop, args=(self._gen, self.proc),
                                        daemon=True, name="mpg123-reader")
        self._reader.start()
        # stderr を捨てない。ALSA の「デバイスを開けない/使用中」系の
        # エラーは全部こちらに出るため、DEVNULL にしていたときは
        # 「mpg123 が即死して復帰ループが回り続けるのに理由が
        # 分からない」状態になっていた (実機で踏んだ)。パイプを誰も
        # 読まないと mpg123 側が詰まるので、専用スレッドで読み続けつつ
        # 直近数行だけ保持する。
        self._err_tail = collections.deque(maxlen=5)
        threading.Thread(target=self._read_err_loop, args=(self.proc,),
                         daemon=True, name="mpg123-stderr").start()
        self._send(f"V {int(config.get('music_volume'))}")
        return True

    def _read_err_loop(self, p: subprocess.Popen) -> None:
        if p.stderr is None:
            return
        try:
            for line in p.stderr:
                line = line.strip()
                if line:
                    self._err_tail.append(line)
        except Exception:
            pass

    def stderr_tail(self) -> str:
        return " / ".join(getattr(self, "_err_tail", ()))

    def _send(self, cmd: str) -> None:
        p = self.proc
        if p is None or p.poll() is not None or p.stdin is None:
            return
        try:
            p.stdin.write(cmd + "\n")
            p.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    def _read_loop(self, gen: int, p: subprocess.Popen) -> None:
        """`gen` は _spawn() がこのプロセスに割り振った世代番号、`p` はその
        プロセス自身 (spawn 時点の self.proc のスナップショット)。

        どちらも `self.proc`/`self._gen` を後から読み直すのではなく、
        呼び出し時に固定で受け取る。理由: このスレッドは EQ 再構成
        (_sync_eq()) や eco/Bluetooth/voice の退避で "S"+"Q" を送られて
        `p` が終了した**あと**も、新しい世代の mpg123 が spawn され
        `self.proc`/`self._gen` が入れ替わった状態でまだ走り続けている
        ことがある (daemon スレッドを明示的に join/停止していないため)。
        そのタイミングで「@P 0 (停止)」を読むと、次の曲へ進めてよい
        自然な曲終わりなのか、こちらが意図して止めた再構成なのかを
        判断する必要がある。"""
        if p.stdout is None:
            return
        for line in p.stdout:
            line = line.strip()
            if line.startswith("@F "):
                parts = line.split()
                if len(parts) >= 5:
                    try:
                        cur = float(parts[3])
                        rem = float(parts[4])
                        with self._lock:
                            if gen != self._gen:
                                continue
                            self.position = cur
                            self.duration = cur + rem
                            self.playing = True
                            self._dirty = True
                    except ValueError:
                        pass
            elif line.startswith("@P "):
                st = line.split()[-1]
                with self._lock:
                    # この世代が既に入れ替わっているなら、この "@P 0" は
                    # 必ずこちらが意図して殺したプロセスからの最後の出力
                    # であり、自然な曲終わりではない。suspended_by の値に
                    # 関わらず無視する — _sync_eq() は EQ 再構成のために
                    # suspended_by を一切変更せずにプロセスを殺すため、
                    # suspended_by だけを見ていると「殺した直後、次の
                    # play() が suspended_by を "" に戻した後」に届いた
                    # この行を「本当に曲が終わった」と誤認して次の曲へ
                    # 進めてしまう (実機で「曲が2秒ほどで次々に変わって
                    # いく」として報告された不具合の直接の原因)。
                    # **この gen チェックを外して suspended_by だけの判定に
                    # 戻さないでください** — 同じ誤検知に戻ります。
                    if gen != self._gen:
                        continue
                    if st == "0":
                        self.playing = False
                        finished = not self.suspended_by
                    else:
                        self.playing = (st == "2")
                        finished = False
                if finished:
                    # 曲が終わった -> 次へ
                    self._advance_and_play()
            elif line.startswith("@E"):
                with self._lock:
                    if gen != self._gen:
                        continue
                    self.last_error = line[3:].strip()
                log.warning("mpg123 エラー: %s", line)

    # -------------------------------------------------- 操作

    def _sync_eq(self, track_name: str) -> bool:
        """イコライザー設定が前回適用時から変わっていれば asound.conf を
        再構成し、実行中の mpg123 を落とす (次の呼び出し元が新しい設定で
        開き直す)。変わっていなければ何もしない (曲間の無用なギャップを
        避ける、モジュール docstring 参照)。戻り値は実際に再構成したか。

        _apply_audio_mixing() は sudo 経由の外部スクリプトで最大 30 秒
        ブロックしうる。直近で同じ適用に失敗したばかりなら
        _EQ_RETRY_COOLDOWN_SEC が経過するまで再試行しない — mpg123 停止
        からの自己修復ループ (5 秒おきに play() を呼ぶ) が、失敗し続ける
        限り毎回この 30 秒ブロックを踏んで実質的に復帰できなくなるのを
        防ぐため。mpg123 自体の再起動 (_spawn()) は、この EQ 再同期が
        失敗しても play() 側でそのまま続行される。"""
        global _last_applied_eq, _eq_sync_failed_at, _last_effective_eq
        if not config.get("audio_mixing_enabled"):
            # dmix を使わない設定。asound.conf を書き換える意味がない
            # (音楽はアナログ出力へ直接流れ、EQ は LADSPA 段ごと無効)。
            return False
        enabled = bool(config.get("music_eq_enabled"))
        bands = resolve_eq_bands(track_name) if enabled else None
        card = _card_index()
        # カード番号を key に含める理由: asound.conf の dmix スレーブは
        # `hw:N,0` と焼き込まれるので、N が実際のアナログ出力とズレたら
        # 音は HDMI 側へ流れて 3.5mm からは何も聞こえなくなる (エラーは
        # 出ない)。EQ 設定だけを key にしていたときは、一度書かれた
        # asound.conf が二度と再生成されず、この状態から復帰できなかった。
        key = (enabled, tuple(bands) if bands is not None else None, card)
        # ファイルの実体も見る。setup スクリプトが再生テストに失敗して
        # asound.conf をバックアップへ戻した場合 (CLAUDE.md #31)、key 上は
        # 「適用済み」なのに実際には sentinel_music が存在しない、という
        # ズレが残るため。
        stale_file = (card is not None and _asound_card() != card) or \
                     (_last_effective_eq is not None and _asound_eq_on() != _last_effective_eq)
        if key == _last_applied_eq and not stale_file:
            return False
        now = time.time()
        if now - _eq_sync_failed_at < _EQ_RETRY_COOLDOWN_SEC:
            return False
        applied, effective = _apply_audio_mixing(enabled, bands)
        if not applied:
            _eq_sync_failed_at = now
            return False
        _last_applied_eq = key
        # 要求ではなく「実際にファイルへ書かれた状態」を覚える。ここで
        # 要求側 (enabled) を覚えてしまうと、スクリプトが EQ 無しへ落とした
        # 機体で毎曲ごとに再構成が走り、曲間にギャップが出続ける。
        _last_effective_eq = effective
        if self.proc is not None:
            log.info("イコライザー設定が変わったため mpg123 を再起動します (enabled=%s)", enabled)
            # プロセスを殺すと決めた瞬間に世代を進める。これから送る "S"
            # への応答 ("@P 0") はまだこの古い世代のプロセスから届くが、
            # _read_loop() 側はここで既に新しい世代を見ることになるので
            # 「本当の曲終わり」と区別できる。呼び出し元 (play()) がこの
            # あと suspended_by を "" に戻すタイミングとは無関係に安全 —
            # suspended_by だけに頼っていた旧実装が実機で「曲が2秒ほどで
            # 次々に変わっていく」不具合の原因だった。
            self._gen += 1
            self._send("S")
            self._send("Q")
            try:
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        return True

    def play(self, seek: float = 0.0) -> None:
        with self._lock:
            path = self.current_path()
            if path is None:
                self.last_error = "再生可能な曲がありません"
                return
            self._sync_eq(path.name)
            if self.proc is None or self.proc.poll() is not None:
                if not self._spawn():
                    return
            self.suspended_by = ""
            self.position = seek
        if seek > 0.5:
            self._send(f"LP {path}")
            time.sleep(0.15)
            self._send(f"J {int(seek)}s")
            self._send("P")          # LOADPAUSED 後の解除
        else:
            self._send(f"L {path}")
        log.info("再生開始: %s (%.0f秒から)", path.name, seek)

    def pause(self) -> None:
        self._send("P")

    def stop(self, *, terminate: bool = True, reason: str = "") -> None:
        """完全停止。terminate=True なら mpg123 プロセス自体を終了させる。

        エコモードではプロセスを落とすことで CPU・メモリ・ALSA デバイスを解放し、
        ディスク I/O も完全に止める。
        """
        with self._lock:
            self.suspended_by = reason
            self.playing = False
            if terminate:
                # suspended_by が空文字 (restart_playback() など、意図した
                # 停止だが「退避理由」ではない呼び出し) だと、上の
                # suspended_by だけでは古い読み取りスレッドの誤検知を
                # 防げない。_sync_eq() と同じ理由で世代も進めておく。
                self._gen += 1
        self._send("S")
        if terminate and self.proc is not None:
            self._send("Q")
            try:
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        self.persist(force=True)
        log.info("再生停止 (理由: %s)", reason or "手動")

    def _advance_and_play(self, delta: int = 1) -> None:
        with self._lock:
            repeat = str(config.get("music_repeat"))
            if repeat == "one":
                pass
            elif not self.order:
                return
            else:
                nxt = self.cursor + delta
                if nxt >= len(self.order):
                    if repeat == "off":
                        self.playing = False
                        return
                    nxt = 0
                elif nxt < 0:
                    nxt = len(self.order) - 1
                self.cursor = nxt
            self._dirty = True
        self.play(0.0)

    def next(self) -> None:
        self._advance_and_play(1)

    def prev(self) -> None:
        self._advance_and_play(-1)

    def jump_to(self, index: int) -> None:
        with self._lock:
            if 0 <= index < len(self.order):
                self.cursor = index
                self._dirty = True
        self.play(0.0)

    def seek(self, seconds: float) -> None:
        self._send(f"J {int(max(0, seconds))}s")

    def set_volume(self, percent: int) -> None:
        percent = max(0, min(100, int(percent)))
        config.update({"music_volume": percent})
        self._send(f"V {percent}")

    # -------------------------------------------------- 永続化

    def persist(self, *, force: bool = False) -> None:
        """再生位置をディスクへ保存。既定では1時間に1回だけ書く。"""
        now = time.time()
        if not force and (not self._dirty or now - self._last_persist < 3600):
            return
        with self._lock:
            path = self.current_path()
            payload = {
                "music": {
                    "path": str(path) if path else "",
                    "position": round(self.position, 2),
                    "cursor": self.cursor,
                    "seed": self.seed,
                    "shuffle": bool(config.get("music_shuffle")),
                    "saved_at": now,
                }
            }
        existing = state.load_snapshot()
        existing.update(payload)
        state.save_snapshot(existing)
        self._last_persist = now
        self._dirty = False

    def restore(self) -> None:
        snap = state.load_snapshot().get("music") or {}
        path = snap.get("path")
        if not path:
            return
        p = Path(path)
        with self._lock:
            if p in self.tracks:
                i = self.tracks.index(p)
                if i in self.order:
                    self.cursor = self.order.index(i)
            self.position = float(snap.get("position") or 0.0)
        log.info("前回の再生位置を復元しました: %s (%.0f秒)", p.name, self.position)

    # -------------------------------------------------- 状態

    def status(self) -> dict:
        with self._lock:
            path = self.current_path()
            return {
                "playing": self.playing,
                "suspended_by": self.suspended_by,
                "track": path.name if path else "",
                "track_path": str(path) if path else "",
                "category": _category_of(path) if path else "",
                "position": round(self.position, 1),
                "duration": round(self.duration, 1),
                "index": self.cursor,
                "total": len(self.order),
                "volume": int(config.get("music_volume")),
                "shuffle": bool(config.get("music_shuffle")),
                "repeat": str(config.get("music_repeat")),
                "seed": self.seed,
                "error": self.last_error,
                "alive": self.proc is not None and self.proc.poll() is None,
                "category_filter": str(config.get("music_category_filter") or ""),
                "categories": list_categories(),
            }

    def playlist(self) -> list[dict]:
        with self._lock:
            out = []
            for pos, ti in enumerate(self.order):
                if ti < len(self.tracks):
                    t = self.tracks[ti]
                    out.append({"index": pos, "name": t.name,
                                "category": _category_of(t),
                                "current": pos == self.cursor})
            return out


PLAYER = Player()


def set_eq_bands(bands: dict) -> dict:
    """イコライザーの全体設定を部分更新する。値が None のキーは削除
    (0dB=フラットへ戻す)。camera.set_overrides() と同じパターン。"""
    cur = dict(config.get("music_eq_bands") or {})
    for hz, v in bands.items():
        if hz not in EQ_BAND_HZ:
            continue
        if v is None:
            cur.pop(hz, None)
        else:
            cur[hz] = max(-20.0, min(20.0, float(v)))
    config.update({"music_eq_bands": cur})
    refresh_eq()
    return cur


def set_track_eq_bands(track_name: str, bands: dict) -> dict:
    """曲ごとのイコライザー上書きを部分更新する。全バンドが空になったら
    その曲のエントリごと削除する (bt_device_volumes と同じパターン)。"""
    all_overrides = dict(config.get("music_eq_track_overrides") or {})
    cur = dict(all_overrides.get(track_name) or {})
    for hz, v in bands.items():
        if hz not in EQ_BAND_HZ:
            continue
        if v is None:
            cur.pop(hz, None)
        else:
            cur[hz] = max(-20.0, min(20.0, float(v)))
    if cur:
        all_overrides[track_name] = cur
    else:
        all_overrides.pop(track_name, None)
    config.update({"music_eq_track_overrides": all_overrides})
    refresh_eq()
    return cur


def restart_playback() -> None:
    """audio_mixing_enabled を切り替えた直後に呼ぶ (routes.py の
    put_config から)。mpg123 の出力先 (`-a sentinel_music` か
    `-a plughw:N,0` か) は起動時の引数で決まるため、プロセスを作り直さ
    ないと切り替わらない — 設定を変えたのに次に曲が変わるまで何も起き
    ない、という「効いていないように見える」状態を避けるため。"""
    audio.invalidate_pcm_cache()
    if PLAYER.proc is None and not PLAYER.playing:
        return
    pos = PLAYER.position
    PLAYER.stop(terminate=True, reason="")
    PLAYER.play(pos)


def refresh_eq() -> None:
    """設定タブでイコライザーの有効/バンドを変更した直後に呼ぶ (routes.py
    の put_config から)。次に曲が切り替わるのを待たず、今かかっている曲
    に対してすぐ反映させる。設定が実際には変わっていなければ何もしない
    (Player._sync_eq() が判定する) ので、無関係な設定変更のたびに呼んでも
    安全。"""
    path = PLAYER.current_path()
    if path is None:
        return
    with PLAYER._lock:
        changed = PLAYER._sync_eq(path.name)
    if changed:
        PLAYER.play(PLAYER.position)


# ---------------------------------------------------------------- モード連動

def on_mode_change(new: str, old: str) -> None:
    if not config.get("music_enabled"):
        return
    if new in (ECO, CRITICAL):
        if PLAYER.playing or PLAYER.proc is not None:
            PLAYER.persist(force=True)
            PLAYER.stop(terminate=True, reason="eco")
    elif new == NORMAL and old in (ECO, CRITICAL):
        if PLAYER.suspended_by in ("eco", ""):
            if config.get("music_autoplay_on_presence"):
                PLAYER.play(PLAYER.position)


def suspend_for_bluetooth() -> None:
    """Bluetooth 接続時。現在位置を保存してから完全に手を引く。"""
    if PLAYER.proc is not None or PLAYER.playing:
        PLAYER.persist(force=True)
        PLAYER.stop(terminate=True, reason="bluetooth")


def resume_from_bluetooth() -> None:
    if PLAYER.suspended_by != "bluetooth":
        return
    if MODE.mode != NORMAL or not config.get("music_enabled"):
        PLAYER.suspended_by = ""
        return
    PLAYER.play(PLAYER.position)


def duck_for_voice() -> bool:
    """voice.py が音声アナウンスを再生する直前に呼ぶ。

    mpg123 は ALSA へ直接書き込んでおり (CLAUDE.md #2)、bcm2835 の出力は
    dmix なしでは同時に 1 ストリームしか受け付けないため、曲の再生中に
    espeak-ng|aplay を鳴らすとデバイス競合で espeak-ng 側が失敗する
    (もしくは音が割れる)。suspend_for_bluetooth() と全く同じ理由・同じ
    「完全に手を引いてから (reason だけ変えて) 復帰する」仕組みを
    "voice" という別の reason で使う — "bluetooth" と衝突させないため
    (Bluetooth 接続中は既に suspended_by="bluetooth" のはずなので、その
    場合はここでは何もしない。voice.py 側も Bluetooth 接続中は別途
    アナウンス自体をスキップする)。

    戻り値は実際に一時停止したかどうか。呼んでいないのに
    resume_from_voice() を呼んで再生位置を巻き戻さないよう、呼び出し元
    (voice.py) はこの戻り値を見てから resume を呼ぶこと。"""
    if PLAYER.suspended_by == "" and (PLAYER.proc is not None or PLAYER.playing):
        PLAYER.persist(force=True)
        PLAYER.stop(terminate=True, reason="voice")
        return True
    return False


def resume_from_voice() -> None:
    if PLAYER.suspended_by != "voice":
        return
    if MODE.mode != NORMAL or not config.get("music_enabled"):
        PLAYER.suspended_by = ""
        return
    PLAYER.play(PLAYER.position)


# アナウンス直前に覚えた「下げる前の音量」。None なら現在下げていない。
_pre_duck_volume: int | None = None


def duck_volume_for_voice() -> bool:
    """dmix でアナウンスと曲を重ねて鳴らせるとき (voice.py の
    _mixing_ready()) に、曲を止めずに voice_duck_percent の設定に従って
    一時的に音量だけ下げる。duck_for_voice() (曲を完全に停止する経路)
    とは別物 — こちらは mpg123 を止めも開き直しもしない。

    mpg123 の `V <percent>` は再生中に送っても即座に反映されるリモート
    コマンドなので (再起動が要る asound.conf 書き換えとは違う)、この
    ducking は sudo も asound.conf の書き換えも一切経由しない、ごく軽い
    処理で完結する。`config.music_volume` (利用者が設定した本来の音量)
    自体は一切変更しない — アナウンスが終わればそのまま元の値に戻る。

    戻り値は実際に下げたかどうか。呼んでいないのに
    resume_volume_after_voice() を呼んで音量を戻さないよう、呼び出し元
    (voice.py) はこの戻り値を見てから resume を呼ぶこと。"""
    global _pre_duck_volume
    if PLAYER.proc is None or not PLAYER.playing:
        return False
    if _pre_duck_volume is not None:
        # 何らかの理由で前回の resume が呼ばれていない (二重にアナウンスが
        # 重なった等)。既に下げた状態のまま新たに基準を取り直すと、次の
        # resume で「下げた後の音量」を「元の音量」として書き戻してしまう
        # ため、ここでは何もしない (呼び出し元は False を見て、この回は
        # 自分では戻さないと判断する)。
        return False
    percent = int(config.get("voice_duck_percent"))
    if percent >= 100:
        return False
    current = int(config.get("music_volume"))
    _pre_duck_volume = current
    ducked = max(0, min(current, current * percent // 100))
    PLAYER._send(f"V {ducked}")
    return True


def resume_volume_after_voice() -> None:
    global _pre_duck_volume
    if _pre_duck_volume is None:
        return
    if PLAYER.proc is not None:
        PLAYER._send(f"V {_pre_duck_volume}")
    _pre_duck_volume = None


# ---------------------------------------------------------------- yt-dlp

DOWNLOADS: list[dict] = []
_DL_QUEUE: "asyncio.Queue[tuple[str, str]]" = asyncio.Queue()


def enqueue_download(url: str, category: str = "") -> dict:
    if category and not _valid_category(category):
        raise ValueError("カテゴリー名が使えません (パス区切りや先頭のドットは不可)")
    entry = {"url": url, "state": "queued", "title": "", "message": "",
             "category": category, "at": time.time()}
    DOWNLOADS.insert(0, entry)
    del DOWNLOADS[50:]
    _DL_QUEUE.put_nowait((url, category))
    return entry


def _find_entry(url: str) -> dict | None:
    for e in DOWNLOADS:
        if e["url"] == url and e["state"] in ("queued", "running"):
            return e
    return None


_YTDLP_TIMEOUT_SEC = 3 * 3600  # プレイリストは 1 曲より遥かに時間がかかりうる

# --progress-template が出す機械可読な進捗行の接頭辞。yt-dlp 本体の人間
#向け表示 (バージョンによって書式が変わりうる) はパースせず、この専用の
# 行だけを見る。info.* は現在ダウンロード中の項目のメタデータ (プレイ
# リスト内の位置を含む)、progress.* はその項目のダウンロード進捗 —
# どちらも yt-dlp 公式ドキュメントの --progress-template 節が明記する
# 区別どおり (info 側は -o の出力テンプレートと同じ辞書)。プレイリストで
# なければ playlist_index/playlist_count は "NA" になる。
_PROGRESS_PREFIX = "SENTINEL_PROGRESS|"
_PROGRESS_TEMPLATE = (
    "download:" + _PROGRESS_PREFIX +
    "%(progress._percent_str)s|%(progress._eta_str)s|"
    "%(info.playlist_index)s|%(info.playlist_count)s|%(info.title)s"
)


def _run_ytdlp(url: str, entry: dict, category: str = "") -> None:
    """mp3 で取得する。mpg123 が扱えるのが mp3 のみのため形式を固定する。

    プレイリスト URL なら全曲取得する (--no-playlist を付けない)。単曲の
    URL であれば従来どおり 1 曲だけ取得される — yt-dlp 自身がその区別を
    URL から判断するので、こちら側で URL の形を見分ける必要はない。

    category を指定すると、取得した曲を MUSIC_DIR 直下ではなくそのサブ
    フォルダへ直接保存する (「勉強用」「休憩用」のような分類、
    list_categories() 参照) — あとから move_track() で移すのではなく、
    ダウンロードの時点で仕分け先を選べるようにするため。"""
    if shutil.which("yt-dlp") is None:
        entry.update(state="error", message="yt-dlp がインストールされていません")
        return
    if category and not _valid_category(category):
        entry.update(state="error", message="カテゴリー名が使えません")
        return
    out_dir = config.MUSIC_DIR / category if category else config.MUSIC_DIR
    out_dir.mkdir(parents=True, exist_ok=True)  # 新しいカテゴリーへ直接ダウンロードする場合、フォルダがまだ無い
    cmd = [
        "yt-dlp", "--newline",
        "-x", "--audio-format", "mp3", "--audio-quality", "0",
        "--embed-metadata", "--embed-thumbnail", "--convert-thumbnails", "jpg",
        "--no-overwrites",
        "--progress-template", _PROGRESS_TEMPLATE,
        # --restrict-filenames was here previously and stripped every
        # non-ASCII character - it forces filenames down to [A-Za-z0-9_.-]
        # only, so any Japanese title lost its actual characters entirely
        # (not just risky ones). yt-dlp already sanitizes filesystem-unsafe
        # characters (/, control chars, ...) by default without this flag,
        # so dropping it keeps Japanese titles while staying filesystem-safe.
        "-o", str(out_dir / "%(uploader,artist)s - %(title)s.%(ext)s"),
        url,
    ]
    entry.update(percent="", eta="", item_index=None, item_count=None)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
    except Exception as exc:
        entry.update(state="error", message=str(exc))
        return

    # 直近の生ログを少しだけ保持し、失敗時にエラーメッセージとして使う
    # (進捗行はここに積まない — 大量に流れるため失敗理由が埋もれる)。
    tail: collections.deque[str] = collections.deque(maxlen=20)
    got_title = ""
    finished_count = 0  # プレイリストで実際に何曲仕上がったか
    try:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if line.startswith(_PROGRESS_PREFIX):
                parts = line[len(_PROGRESS_PREFIX):].split("|", 4)
                if len(parts) == 5:
                    pct, eta, idx, cnt, title = (p.strip() for p in parts)
                    msg = []
                    if idx not in ("", "NA") and cnt not in ("", "NA"):
                        entry["item_index"] = idx
                        entry["item_count"] = cnt
                        msg.append(f"{idx}/{cnt}曲目")
                    if pct and pct != "NA":
                        entry["percent"] = pct
                        msg.append(pct)
                    if eta and eta not in ("NA", "Unknown"):
                        entry["eta"] = eta
                        msg.append(f"残り{eta}")
                    if msg:
                        entry["message"] = " ".join(msg)
                    if title and title != "NA":
                        entry["title"] = title
                continue
            tail.append(line)
            if "[ExtractAudio]" in line:
                # mp3 への変換が終わった = その曲は仕上がった。プレイリスト
                # では複数回出るため、最後の 1 回だけでなく件数も数える。
                finished_count += 1
                got_title = Path(line.split(":", 1)[-1].strip()).name
    except Exception:
        pass
    try:
        proc.wait(timeout=_YTDLP_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        proc.kill()
        entry.update(state="error", message="タイムアウトしました")
        return

    if proc.returncode == 0:
        # 2 曲以上仕上がっていればプレイリストとして扱う。最後の 1 曲名
        # だけを出すと「1 曲しか取れなかった」ように見えてしまうため。
        title = f"{finished_count}曲 完了" if finished_count > 1 else \
            (got_title or entry.get("title") or "完了")
        entry.update(state="done", title=title, message="", percent="", eta="")
        PLAYER.scan()
    else:
        err_lines = [l for l in tail if l.strip()]
        entry.update(state="error", message=err_lines[-1] if err_lines else "不明なエラー")


async def download_loop() -> None:
    while True:
        url, category = await _DL_QUEUE.get()
        entry = _find_entry(url) or {"url": url, "state": "running", "title": "",
                                     "message": "", "category": category, "at": time.time()}
        # エコモード中と定時処理中はダウンロードを止める (CPU と I/O を空ける)
        while MODE.mode != NORMAL:
            entry["state"] = "waiting"
            entry["message"] = "通常モードへの復帰を待っています"
            await asyncio.sleep(20)
        entry.update(state="running", message="")
        await asyncio.to_thread(_run_ytdlp, url, entry, category)


# ---------------------------------------------------------------- ループ

async def loop() -> None:
    PLAYER.scan()
    PLAYER.restore()
    if config.get("music_enabled") and config.get("music_autoplay_on_presence"):
        if MODE.mode == NORMAL:
            await asyncio.to_thread(PLAYER.play, PLAYER.position)

    last_scan = time.time()
    last_mix_recheck = time.time()
    _mix_recheck_backoff = 300
    while True:
        await asyncio.sleep(5)
        # mpg123 が落ちていたら復帰させる (通常モードかつ退避中でないとき)
        if (config.get("music_enabled") and MODE.mode == NORMAL
                and not PLAYER.suspended_by
                and (PLAYER.proc is None or PLAYER.proc.poll() is not None)
                and PLAYER.tracks):
            tail = PLAYER.stderr_tail()
            if tail:
                PLAYER.last_error = tail
                log.warning("mpg123 が停止していたため復帰させます (mpg123: %s)", tail)
            else:
                log.warning("mpg123 が停止していたため復帰させます")
            # 死因が ALSA 側なら dmix の判定も古い。キャッシュを捨てて
            # 次の play() で sentinel_music を実際に開き直させる — 開け
            # なければアナログ出力へ直接落ちるので、同じデバイスで死に
            # 続ける復帰ループにはならない。
            audio.invalidate_pcm_cache()
            await asyncio.to_thread(PLAYER.play, PLAYER.position)

        # mpg123 が生きたまま plughw:N,0 へフォールバックしている場合、
        # 上の「落ちていたら」の分岐には一生入らないため、dmix
        # (sentinel_music) が後から復旧しても誰も気付かなかった
        # (CLAUDE.md #67)。さらに悪いことに、mpg123 が plughw:N,0 を
        # 直接掴み続けている間は、その同じハードウェアデバイスを自分の
        # スレーブとして開こうとする dmix 側が絶対に開けない — つまり
        # sentinel-fix-audio-output.sh がどれだけ asound.conf を直しても、
        # mpg123 が直接出力を掴んだままである限り検証テスト自体が失敗し
        # 続ける「片方が生きている限りもう片方が直せない」膠着状態になって
        # いた。**この再確認の前に mpg123 を実際に止めてデバイスを手放さ
        # ないと、テストは一生失敗し続けます** — 生かしたまま
        # `_mixing_ready()` を呼ぶだけの実装に戻さないでください。
        # `_mix_recheck_backoff` は camera.py の再接続バックオフ
        # (CLAUDE.md #19) と同じ考え方の指数バックオフです。本当に直って
        # いない環境で毎回 restart_playback() を呼ぶと、5 分おき無期限に
        # 再生が瞬断し続けるだけになるため、失敗のたびに次回までの間隔を
        # 倍々に伸ばし (上限 1 時間)、実際に dmix へ切り替われた瞬間に
        # 短い間隔へ戻します。
        if (time.time() - last_mix_recheck >= _mix_recheck_backoff
                and config.get("audio_mixing_enabled")
                and not str(config.get("alsa_device") or "").strip()
                and PLAYER.active_device.startswith("plughw:")
                and PLAYER.proc is not None and PLAYER.proc.poll() is None):
            last_mix_recheck = time.time()
            await asyncio.to_thread(restart_playback)
            if PLAYER.active_device == "sentinel_music":
                log.info("dmix (sentinel_music) が復旧したため、直接出力から切り替えました")
                _mix_recheck_backoff = 300
            else:
                _mix_recheck_backoff = min(_mix_recheck_backoff * 2, 3600)

        PLAYER.persist()

        if time.time() - last_scan >= 300:
            last_scan = time.time()
            await asyncio.to_thread(PLAYER.scan)


def shutdown() -> None:
    PLAYER.persist(force=True)
    PLAYER.stop(terminate=True, reason="shutdown")
