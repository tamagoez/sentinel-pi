"""ALSA の実出力カード (3.5mm アナログ出力) を特定する.

`aplay -l` は機体によって複数のカードを列挙することがある — 特に近年の
Raspberry Pi カーネル/DietPi では HDMI 出力ごとに別カード (vc4hdmi0 など)
が先に並び、CLAUDE.md 冒頭の想定どおりの 3.5mm アナログ出力 (bcm2835 の
"Headphones") はそのあとの番号になることがある。単純に「aplay -l の
最初の card 行」を使うと、そのような機体では HDMI カードを掴んでしまう。

実機でこれを踏んだ: sentinel-setup-audio-mixing.sh が dmix の slave を
固定フォーマット (S16_LE/44100/2ch) の `hw:0,0` で開こうとして
"unable to open slave" / "Invalid argument" で失敗した。原因は card 0 が
実際には HDMI 出力で、想定していた bcm2835 のアナログ出力ではなかった
ため — amixer 系の音量操作 (numid=1/numid=3/SentinelVoice) は間違った
カードへ静かに書き込むだけで気付きにくいが、dmix はフォーマットを
literal に要求して開こうとするため、ここで初めて表面化した。

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
