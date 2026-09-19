"""ALSA の実出力カード (3.5mm アナログ出力) を特定する.

`aplay -l` は機体によって複数のカードを列挙することがある — 特に近年の
Raspberry Pi カーネル/DietPi では HDMI 出力ごとに別カード (vc4hdmi0 など)
が先に並び、CLAUDE.md 冒頭の想定どおりの 3.5mm アナログ出力 (bcm2835 の
"Headphones") はそのあとの番号になることがある。単純に「aplay -l の
最初の card 行」を使うと、そのような機体では HDMI カードを掴んでしまう。

実機でこれを踏んだ: dmix (`sysdefault:CARD=0`) を固定フォーマット
(S16_LE/44100/2ch) で開こうとして "unable to open slave" /
"Invalid argument" で失敗した。原因は card 0 が実際には HDMI 出力で、
想定していた bcm2835 のアナログ出力ではなかったため — amixer 系の音量
操作 (numid=1/numid=3) は間違ったカードへ静かに書き込むだけで気付き
にくいが、dmix はフォーマットを literal に要求して開こうとするため、
ここで初めて表面化した。

この関数は "Headphones" (現行の Pi OS/DietPi カーネルでの命名) を優先し、
無ければ旧来の単一カード構成向けに "bcm2835" を探し、それも無ければ
(未知のハードウェア向けの最後の手段として) 最初のカードにフォールバック
する。**この優先順位を外して「最初に見つかったカード」に戻さないで
ください** — 同じ「dmix がアナログ出力ではなく HDMI を掴んで開けない」
不具合に戻ります。music.py・bluetooth.py・voice.py の 3 か所が全く同じ
理由でこの関数を使う — どれか 1 つだけ直して他を「最初のカード」の
ままにすると、音楽の dmix ミキシングは直っても Bluetooth 音量や音声
アナウンスの音量だけ違うカードを操作し続けることになる。
scripts/sentinel-guardian.sh の check_audio() は Python を呼べない
(bash のみで完結させる設計) ため、同じ優先順位を bash 側にも複製して
ある — こちらを直すときはそちらも忘れずに揃えること。
"""

from __future__ import annotations

import subprocess


def analog_device(card: int | None = None) -> str | None:
    """`sysdefault:CARD=<N>` for the analog output card - the one ALSA
    device string every audio producer in this project should target.

    `sysdefault` is alsa-lib's own auto-generated per-card dmix route: it
    exists for every card with no `/etc/asound.conf` at all, already mixes
    any number of simultaneous streams (confirmed by testing two `mpg123`
    processes against it directly - they overlapped and played together
    with zero configuration), and is the ALSA project's own documented
    workaround for the bcm2835 driver's plain `dmix:CARD=...` hanging
    (https://www.raspberrypi.org/forums/viewtopic.php?t=262071). A previous
    version of this project instead maintained a hand-written
    `/etc/asound.conf` (`pcm.sentinel_music`/`pcm.sentinel_voice` over a
    custom `dmix` slave, with a LADSPA EQ stage) that had to be regenerated
    by a root-only helper script on every card change or EQ setting change,
    and it also overrode `pcm.!default` - which silently redirected
    `bluealsa-aplay --pcm=default` (its systemd unit's own argument, never
    written by this project) onto that same fragile custom chain. That is
    what broke Bluetooth playback and mixing at the same time, and why
    reverting to a plain, distro-provided route fixes both at once. See
    CLAUDE.md's audio-mixing section for the full history.

    **Do not go back to a custom `/etc/asound.conf`** - `sysdefault` needs
    none, and every prior attempt at hand-writing one produced a new class
    of "audio.conf drifted from the actual card" bug.
    """
    if card is None:
        card = find_output_card()
    return f"sysdefault:CARD={card}" if card is not None else None


def find_output_card() -> int | None:
    try:
        out = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    cards: list[tuple[int, str]] = []
    for line in out.splitlines():
        if not line.startswith("card "):
            continue
        try:
            idx = int(line.split()[1].rstrip(":"))
        except (IndexError, ValueError):
            continue
        cards.append((idx, line))
    if not cards:
        return None
    for idx, line in cards:
        if "headphones" in line.lower():
            return idx
    for idx, line in cards:
        if "bcm2835" in line.lower():
            return idx
    return cards[0][0]


# ---------------------------------------------------------------- 開けるか

# pcm_opens() の結果キャッシュ。 名前 -> (判定時刻, 開けたか, エラー文)
_OPEN_CACHE: dict[str, tuple[float, bool, str]] = {}
_OPEN_CACHE_TTL = 30.0


def pcm_opens(name: str, *, ttl: float = _OPEN_CACHE_TTL) -> tuple[bool, str]:
    """ALSA デバイス名 (`sysdefault:CARD=<N>` など) が **実際に開けるか**
    を試す。

    dmix はスレーブ (`hw:N,0`) を開いて初めて失敗するため、カード番号が
    ズレていたり、他のプロセスがカードを直接掴んでいたりすると
    `unable to open slave` / `Invalid argument` で開けないことがある —
    名前を渡すだけでは分からない。CLAUDE.md #8 の can_write() と同じ
    「実物を試す」原則をここで守る。

    /dev/zero を 0.4 秒だけ流すのでデジタル無音、実際には何も聞こえない。
    毎回の play() で走ると重いので ttl 秒だけ結果をキャッシュする。
    戻り値は (開けたか, 開けなかったときの理由)。
    """
    import time

    now = time.time()
    hit = _OPEN_CACHE.get(name)
    if hit and now - hit[0] < ttl:
        return hit[1], hit[2]
    ok, err = False, ""
    try:
        p = subprocess.run(
            ["aplay", "-D", name, "-f", "S16_LE", "-r", "44100", "-c", "2",
             "-d", "1", "-q", "/dev/zero"],
            capture_output=True, text=True, timeout=8)
        ok = p.returncode == 0
        if not ok:
            lines = (p.stderr or p.stdout or "").strip().splitlines()
            err = lines[-1].strip() if lines else f"aplay が終了コード {p.returncode} で失敗しました"
    except Exception as exc:
        err = str(exc)
    _OPEN_CACHE[name] = (now, ok, err)
    return ok, err


def invalidate_pcm_cache() -> None:
    """asound.conf を書き換えた直後に呼ぶ (次の pcm_opens() で必ず再試行)。"""
    _OPEN_CACHE.clear()
