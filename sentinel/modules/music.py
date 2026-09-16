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

        self.tracks: list[Path] = []
        self.order: list[int] = []
        self.cursor: int = 0
        self.position: float = 0.0
        self.duration: float = 0.0
        self.playing: bool = False
        self.suspended_by: str = ""        # "eco" | "bluetooth" | "" (退避理由)
        self.last_error: str = ""
        self.seed: int = 0

        self._dirty = False
        self._last_persist = 0.0

    # -------------------------------------------------- ライブラリ

    def scan(self) -> None:
        with self._lock:
            found = sorted(p for p in config.MUSIC_DIR.rglob("*")
                           if p.is_file() and p.suffix.lower() in AUDIO_EXT)
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
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, bufsize=1)
        except Exception as exc:
            self.last_error = f"mpg123 の起動に失敗: {exc}"
            log.exception(self.last_error)
            return False
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="mpg123-reader")
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

    def _read_loop(self) -> None:
        p = self.proc
        if p is None or p.stdout is None:
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
                            self.position = cur
                            self.duration = cur + rem
                            self.playing = True
                            self._dirty = True
                    except ValueError:
                        pass
            elif line.startswith("@P "):
                st = line.split()[-1]
                with self._lock:
                    if st == "0":
                        self.playing = False
                        finished = True
                    else:
                        self.playing = (st == "2")
                        finished = False
                if finished and not self.suspended_by:
                    # 曲が終わった -> 次へ
                    self._advance_and_play()
            elif line.startswith("@E"):
                with self._lock:
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
            }

    def playlist(self) -> list[dict]:
        with self._lock:
            out = []
            for pos, ti in enumerate(self.order):
                if ti < len(self.tracks):
                    t = self.tracks[ti]
                    out.append({"index": pos, "name": t.name,
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


# ---------------------------------------------------------------- yt-dlp

DOWNLOADS: list[dict] = []
_DL_QUEUE: "asyncio.Queue[str]" = asyncio.Queue()


def enqueue_download(url: str) -> dict:
    entry = {"url": url, "state": "queued", "title": "", "message": "",
             "at": time.time()}
    DOWNLOADS.insert(0, entry)
    del DOWNLOADS[50:]
    _DL_QUEUE.put_nowait(url)
    return entry


def _find_entry(url: str) -> dict | None:
    for e in DOWNLOADS:
        if e["url"] == url and e["state"] in ("queued", "running"):
            return e
    return None


def _run_ytdlp(url: str, entry: dict) -> None:
    """mp3 で取得する。mpg123 が扱えるのが mp3 のみのため形式を固定する。"""
    if shutil.which("yt-dlp") is None:
        entry.update(state="error", message="yt-dlp がインストールされていません")
        return
    cmd = [
        "yt-dlp", "--no-playlist", "--no-progress", "--newline",
        "-x", "--audio-format", "mp3", "--audio-quality", "0",
        "--embed-metadata", "--embed-thumbnail", "--convert-thumbnails", "jpg",
        "--no-overwrites",
        # --restrict-filenames was here previously and stripped every
        # non-ASCII character - it forces filenames down to [A-Za-z0-9_.-]
        # only, so any Japanese title lost its actual characters entirely
        # (not just risky ones). yt-dlp already sanitizes filesystem-unsafe
        # characters (/, control chars, ...) by default without this flag,
        # so dropping it keeps Japanese titles while staying filesystem-safe.
        "-o", str(config.MUSIC_DIR / "%(uploader,artist)s - %(title)s.%(ext)s"),
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        entry.update(state="error", message="タイムアウトしました")
        return
    except Exception as exc:
        entry.update(state="error", message=str(exc))
        return
    if proc.returncode == 0:
        title = ""
        for line in proc.stdout.splitlines():
            if "Destination:" in line or "[ExtractAudio]" in line:
                title = Path(line.split(":")[-1].strip()).name
        entry.update(state="done", title=title or "完了", message="")
        PLAYER.scan()
    else:
        tail = (proc.stderr or proc.stdout).strip().splitlines()
        entry.update(state="error", message=tail[-1] if tail else "不明なエラー")


async def download_loop() -> None:
    while True:
        url = await _DL_QUEUE.get()
        entry = _find_entry(url) or {"url": url, "state": "running", "title": "",
                                     "message": "", "at": time.time()}
        # エコモード中と定時処理中はダウンロードを止める (CPU と I/O を空ける)
        while MODE.mode != NORMAL:
            entry["state"] = "waiting"
            entry["message"] = "通常モードへの復帰を待っています"
            await asyncio.sleep(20)
        entry.update(state="running", message="")
        await asyncio.to_thread(_run_ytdlp, url, entry)


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
            # 死因が ALSA 側なら dmix の判定も古い。キャッシュを捨てて
            # 次の play() で sentinel_music を実際に開き直させる — 開け
            # なければアナログ出力へ直接落ちるので、同じデバイスで死に
            # 続ける復帰ループにはならない。
            audio.invalidate_pcm_cache()
            await asyncio.to_thread(PLAYER.play, PLAYER.position)

        PLAYER.persist()

        if time.time() - last_scan >= 300:
            last_scan = time.time()
            await asyncio.to_thread(PLAYER.scan)


def shutdown() -> None:
    PLAYER.persist(force=True)
    PLAYER.stop(terminate=True, reason="shutdown")
