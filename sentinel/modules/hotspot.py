"""WiFi ホットスポット (hostapd) の SSID 表示・変更.

DietPi はホットスポットの SSID を `dietpi.txt` から一度だけ読んで
`/etc/hostapd/hostapd.conf` に書き込む (画像準備段階、CLAUDE.md フェーズ 0)。
そのため実行中に SSID を変えるには `/etc/hostapd/hostapd.conf` を直接
書き換えて hostapd を再起動する必要がある。このファイルは root しか
書けないため、CPU ガバナ (core/state.py) と同じパターンで、検証済みの
1 本のスクリプト (scripts/sentinel-set-hotspot-ssid.sh) だけを sudoers 経由
で許可する。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..core import config

HOSTAPD_CONF = Path("/etc/hostapd/hostapd.conf")


def current_ssid() -> str:
    try:
        text = HOSTAPD_CONF.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    m = re.search(r"^\s*ssid=(.*)$", text, re.M)
    return m.group(1).strip() if m else ""


def set_ssid(ssid: str) -> tuple[bool, str]:
    ssid = ssid.strip()
    if not ssid:
        return False, "SSID を入力してください"
    # IEEE 802.11 の SSID は最大 32 バイト。制御文字/改行はconfファイルの
    # 行構造を壊すため拒否する。
    if len(ssid.encode("utf-8")) > 32 or "\n" in ssid or "\r" in ssid:
        return False, "SSID が不正です (32 バイト以内、改行不可)"
    script = config.APP_ROOT.parent / "scripts" / "sentinel-set-hotspot-ssid.sh"
    try:
        r = subprocess.run(["sudo", "-n", str(script), ssid],
                           capture_output=True, text=True, timeout=15)
    except Exception as exc:
        return False, str(exc)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        return False, tail[-1] if tail else "変更に失敗しました"
    return True, "変更しました (反映まで数秒かかることがあります)"
