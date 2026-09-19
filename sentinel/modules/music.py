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

`alsa_device` が空のときは、`core/audio.analog_device()` が返す
`sysdefault:CARD=<N>` (alsa-lib 自身が用意する per-card dmix ルート、
CLAUDE.md の音声ミキシング刷新の節を参照) を使う。これは設定ファイルを
一切必要とせず、mpg123・音声アナウンス (modules/voice.py)・
bluealsa-aplay のどれが同時に書き込んでも自動的に重なって鳴る。イコライザー
(music_eq_enabled/music_eq_bands/music_eq_track_overrides) は mpg123 -R
リモートプロトコルが元から持つ実時間イコライザー (`E <channel> <band>
<gain>`、32 サブバンド) を直接叩くだけで、asound.conf の書き換えも
mpg123 の再起動も一切要らない — 設定を変えた次の瞬間から効く。
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

# 全体設定 UI に出す 15 バンドの目安周波数 (Hz)。mpg123 のリモート
# プロトコルはこれを直接は知らない — MPEG のサブバンド (0-31) というだけ
# なので、_eq_band_index() で各 Hz を最寄りのサブバンドへ写像する。
EQ_BAND_HZ = ["50", "100", "156", "220", "311", "440", "622", "880",
             "1250", "1750", "2500", "3500", "5000", "10000", "20000"]

# mpg123 のリモート "E" コマンド (doc/README.remote) が受け付けるサブ
# バンド数と、公式ドキュメントが明記する「実用的な範囲」。ゲインは dB
# ではなく乗算的な線形ゲイン (既定 1.00 = 変化なし) で、"values work best
# between 0.00 and 3.00" とある — 3.00 を超えると歪みが目立ちやすい。
_EQ_SUBBANDS = 32
_EQ_GAIN_MIN = 0.0
_EQ_GAIN_MAX = 3.0
# 44.1kHz を基準にした Nyquist (22050Hz)。mp3 は実際にはファイルごとに
# サンプルレートが違いうるが、グラフィック EQ は元々「目安の帯域」を
# 動かす道具であり厳密な周波数対応を要求されないため、単一の代表値で
# 十分と判断している。
_EQ_NYQUIST_HZ = 44100 / 2


def _eq_band_index(hz: float) -> int:
    idx = round(hz / _EQ_NYQUIST_HZ * _EQ_SUBBANDS - 0.5)
    return max(0, min(_EQ_SUBBANDS - 1, idx))


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


def _eq_subband_gains(enabled: bool, bands_db: list[float] | None) -> tuple[float, ...]:
    """UI の 15 バンド (dB) を、mpg123 の 32 サブバンド (線形ゲイン) へ
    写像した固定長タプルにする。無効時・未指定バンドは 1.00 (フラット)。
    複数の Hz ラベルが同じサブバンドへ写像された場合は後勝ち — グラフィック
    EQ の近似として許容範囲であり、厳密な帯域分離を保証する道具ではない。"""
    gains = [1.0] * _EQ_SUBBANDS
    if enabled and bands_db:
        for hz_label, db in zip(EQ_BAND_HZ, bands_db):
            if not db:
                continue
            idx = _eq_band_index(float(hz_label))
            linear = 10 ** (float(db) / 20.0)
            gains[idx] = max(_EQ_GAIN_MIN, min(_EQ_GAIN_MAX, linear))
    return tuple(gains)


class Player:
    """mpg123 の単一インスタンスを抱えるラッパー。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        # mpg123 プロセスを spawn するたびに +1 する世代カウンタ。
        # _read_loop() が読む「@P 0 (停止)」は、stop() による
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
        # _spawn() が実際に mpg123 へ渡した -a の値。状態表示・診断用。
        self.active_device: str = ""
        # 直近に mpg123 へ送った EQ ゲイン (32 サブバンド)。変化が無ければ
        # 再送を省く軽いメモ — 送らなくても実害は無いが、曲が切り替わる
        # たびに 32 行を無条件で送るのは無駄なので memo する。
        self._last_sent_eq: tuple[float, ...] | None = None

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
        # 空なら core/audio.analog_device() が返す sysdefault:CARD=<N> を
        # 使う — alsa-lib 自身が用意する per-card dmix ルートで、設定
        # ファイル無しに音声アナウンス (voice.py) や bluealsa-aplay と
        # 自動的に重なって鳴る (CLAUDE.md の音声ミキシング刷新の節)。
        dev = str(config.get("alsa_device") or "").strip() or audio.analog_device() or ""
        if dev:
            cmd += ["-a", dev]
        self.active_device = dev
        self._last_sent_eq = None
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
        呼び出し時に固定で受け取る。理由: このスレッドは eco/Bluetooth/
        voice の退避 (stop()) で "S"+"Q" を送られて `p` が終了した
        **あと**も、新しい世代の mpg123 が spawn され
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
                    # 関わらず無視する — stop(reason="") のように
                    # suspended_by を空文字のまま殺す呼び出しもあるため、
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

    def _apply_eq(self, track_name: str | None) -> None:
        """曲に対する実効 EQ (resolve_eq_bands()) を mpg123 のリモート
        "E" コマンド (32 サブバンド、実時間反映) で直接送る。asound.conf
        の書き換えも mpg123 の再起動も不要 — mpg123 自身のドキュメント
        (doc/README.remote) が "built-in equalizer runs real-time" と
        明記するとおり、再生中に送るだけで即座に効く。

        前回送った値と同じなら何もしない (32 行を毎曲無条件で送るのは
        無駄なだけ)。mpg123 を spawn し直した直後は _last_sent_eq が
        None にリセットされる (_spawn() 参照) ので、新しいプロセスには
        必ず一度送り直される。"""
        if self.proc is None or self.proc.poll() is not None:
            return
        enabled = bool(config.get("music_eq_enabled"))
        bands_db = resolve_eq_bands(track_name) if enabled else None
        gains = _eq_subband_gains(enabled, bands_db)
        if gains == self._last_sent_eq:
            return
        for band, gain in enumerate(gains):
            self._send(f"E 3 {band} {gain:.4f}")
        self._last_sent_eq = gains

    def play(self, seek: float = 0.0) -> None:
        with self._lock:
            path = self.current_path()
            if path is None:
                self.last_error = "再生可能な曲がありません"
                return
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
        # L/LP のあとに送る — mpg123 が曲の読み込みで内部フィルタ状態を
        # リセットする可能性があるため、読み込みより前に送っても意味が
        # 保証されない (念のための順序、実害があっても軽い再送で直る)。
        self._apply_eq(path.name)
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
                # 防げない (CLAUDE.md #50)。世代も進めておく。
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


# ±12dB は mpg123 のリモート EQ が「実用的」とする線形ゲイン 0.00-3.00
# (doc/README.remote) の上限 (20*log10(3.0) ≈ +9.5dB) に少し余裕を足した
# 値。これより極端な入力は _eq_subband_gains() が最終的に線形ゲインの
# 側で安全域へクランプするので害は無いが、UI 側のスライダーがその上限を
# 大きく超えた値を許すのは「動かしても実際には頭打ちで変わらない」誤解を
# 招くだけなので、ここで先に絞っておく。旧 LADSPA (mbeq) 実装時代の
# ±20dB という範囲は、そちらのプラグインの許容幅であって mpg123 とは
# 無関係だった。
_EQ_DB_RANGE = 12.0


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
            cur[hz] = max(-_EQ_DB_RANGE, min(_EQ_DB_RANGE, float(v)))
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
            cur[hz] = max(-_EQ_DB_RANGE, min(_EQ_DB_RANGE, float(v)))
    if cur:
        all_overrides[track_name] = cur
    else:
        all_overrides.pop(track_name, None)
    config.update({"music_eq_track_overrides": all_overrides})
    refresh_eq()
    return cur


def restart_playback() -> None:
    """`alsa_device` (明示的な出力デバイス上書き) を切り替えた直後に呼ぶ
    (routes.py の put_config から)。mpg123 の出力先は起動時の引数で決まる
    ため、プロセスを作り直さないと切り替わらない — 設定を変えたのに次に
    曲が変わるまで何も起きない、という「効いていないように見える」状態を
    避けるため。"""
    audio.invalidate_pcm_cache()
    if PLAYER.proc is None and not PLAYER.playing:
        return
    pos = PLAYER.position
    PLAYER.stop(terminate=True, reason="")
    PLAYER.play(pos)


def refresh_eq() -> None:
    """設定タブでイコライザーの有効/バンドを変更した直後に呼ぶ (routes.py
    の put_config から)。mpg123 のリモート EQ コマンドは再生中でも即座に
    効くため、曲の切り替えも再起動も要らない — 今かかっている曲へ
    そのまま送るだけでよい。"""
    with PLAYER._lock:
        PLAYER._apply_eq(PLAYER.current_path().name if PLAYER.current_path() else None)


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


# アナウンス直前に覚えた「下げる前の音量」。None なら現在下げていない。
_pre_duck_volume: int | None = None


def duck_volume_for_voice() -> bool:
    """voice.py が音声アナウンスを再生する直前に呼ぶ。曲を止めずに
    voice_duck_percent の設定に従って一時的に音量だけ下げる。

    音楽 (mpg123)・音声アナウンス・Bluetooth (bluealsa-aplay) はどれも
    core/audio.analog_device() の同じ sysdefault:CARD=<N> へ書き込んで
    おり、alsa-lib の dmix が構造的に重ねて鳴らす (設定ファイルもフラグ
    も要らない) ため、以前あった「重ねられないなら曲を完全に止める」旧
    経路 (duck_for_voice()/resume_from_voice()) は不要になった — 曲を
    止める必要がある場面はもう無い。

    mpg123 の `V <percent>` は再生中に送っても即座に反映されるリモート
    コマンドなので、sudo も外部設定ファイルの書き換えも一切経由しない、
    ごく軽い処理で完結する。`config.music_volume` (利用者が設定した本来の
    音量) 自体は一切変更しない — アナウンスが終わればそのまま元の値に戻る。

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
            audio.invalidate_pcm_cache()
            await asyncio.to_thread(PLAYER.play, PLAYER.position)

        PLAYER.persist()

        if time.time() - last_scan >= 300:
            last_scan = time.time()
            await asyncio.to_thread(PLAYER.scan)


def shutdown() -> None:
    PLAYER.persist(force=True)
    PLAYER.stop(terminate=True, reason="shutdown")
