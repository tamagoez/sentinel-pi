"""ホットスポット上のアクセス記録.

AdGuard Home の /control/querylog を定期取得し、ドメインをサービス名へ
変換して日次 JSONL に記録する。

HTTPS の中身は見えないため、取得できるのは DNS の問い合わせ先ドメインである。
「サービス名で識別する」という要件はここで満たす。

誤判定を避けるため、汎用的すぎるドメイン (例: amazonaws.com) は
単独ではサービス判定に使わない方針とする。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from ..core import config

log = logging.getLogger("sentinel.netlog")

# ドメイン断片 -> サービス名。上から順に照合し、最初に当たったものを採用する。
SERVICE_RULES: list[tuple[tuple[str, ...], str]] = [
    (("googlevideo.com", "youtube.com", "youtu.be", "ytimg.com", "yt3.ggpht.com",
      "youtubei.googleapis.com"), "YouTube"),
    (("twitter.com", "x.com", "twimg.com", "t.co", "twttr.com"), "X (Twitter)"),
    (("line.me", "line-apps.com", "line-scdn.net", "linecorp.com",
      "line-cdn.net", "line.naver.jp", "line-apps-beta.com",
      "linepay.me", "lin.ee"), "LINE"),
    (("instagram.com", "cdninstagram.com"), "Instagram"),
    (("facebook.com", "fbcdn.net", "fb.com", "facebook.net"), "Facebook"),
    (("tiktokv.com", "tiktokcdn.com", "tiktok.com", "byteoversea.com",
      "musical.ly"), "TikTok"),
    (("discord.com", "discordapp.com", "discordapp.net", "discord.gg",
      "discord.media"), "Discord"),
    (("spotify.com", "scdn.co", "spotifycdn.com"), "Spotify"),
    (("netflix.com", "nflxvideo.net", "nflximg.net", "nflxso.net"), "Netflix"),
    (("amazonvideo.com", "primevideo.com", "aiv-cdn.net"), "Prime Video"),
    (("twitch.tv", "ttvnw.net", "jtvnw.net"), "Twitch"),
    (("nicovideo.jp", "nimg.jp", "dmc.nico"), "ニコニコ動画"),
    (("abema.tv", "abema.io", "ameba.jp"), "ABEMA"),
    (("pixiv.net", "pximg.net"), "pixiv"),
    (("github.com", "githubusercontent.com", "githubassets.com",
      "ghcr.io"), "GitHub"),
    (("reddit.com", "redd.it", "redditmedia.com", "redditstatic.com"), "Reddit"),
    (("slack.com", "slack-edge.com", "slack-msgs.com"), "Slack"),
    (("whatsapp.net", "whatsapp.com"), "WhatsApp"),
    (("telegram.org", "t.me", "telegram.me", "tdesktop.com"), "Telegram"),
    (("zoom.us", "zoom.com", "zoomgov.com"), "Zoom"),
    (("teams.microsoft.com", "teams.live.com", "skype.com"), "Teams / Skype"),
    (("openai.com", "chatgpt.com", "oaistatic.com", "oaiusercontent.com"), "ChatGPT"),
    (("anthropic.com", "claude.ai"), "Claude"),
    (("gemini.google.com", "bard.google.com", "generativelanguage.googleapis.com"),
     "Gemini"),
    (("steamcommunity.com", "steampowered.com", "steamstatic.com",
      "steamcontent.com"), "Steam"),
    (("nintendo.net", "nintendo.com", "nintendowifi.net", "nintendo.co.jp"),
     "Nintendo"),
    (("playstation.net", "playstation.com", "sonyentertainmentnetwork.com"),
     "PlayStation"),
    (("xboxlive.com", "xbox.com"), "Xbox"),
    (("apple.com", "icloud.com", "mzstatic.com", "cdn-apple.com",
      "push.apple.com"), "Apple"),
    (("windowsupdate.com", "microsoft.com", "msftconnecttest.com",
      "live.com", "office.com", "office365.com"), "Microsoft"),
    (("gstatic.com", "googleapis.com", "google.com", "googleusercontent.com",
      "doubleclick.net", "googlesyndication.com", "google-analytics.com",
      "gvt1.com", "gvt2.com"), "Google"),
    (("amazon.co.jp", "amazon.com", "media-amazon.com", "ssl-images-amazon.com",
      "amazon-adsystem.com"), "Amazon"),
    (("rakuten.co.jp", "r10s.jp", "rakuten.com"), "楽天"),
    (("yahoo.co.jp", "yimg.jp", "yahooapis.jp", "yahoo.com"), "Yahoo!"),
    (("dropbox.com", "dropboxstatic.com"), "Dropbox"),
    (("cloudflare.com", "cloudflare-dns.com", "cdnjs.cloudflare.com"), "Cloudflare"),
    (("ntp.org", "ntp.nict.jp", "time.windows.com", "time.apple.com"), "NTP (時刻同期)"),
]

# 名前解決の基盤であり、サービスとしては意味が薄いもの
IGNORE_SUFFIXES = ("in-addr.arpa", "ip6.arpa", "local", "lan", "home.arpa")

# 集計 (メモリ上)
RECENT: list[dict] = []          # 直近のイベント (最大 500)
TODAY_COUNTS: Counter = Counter()
STATE = {"last_poll": 0.0, "ok": False, "error": "", "entries": 0}
_seen_keys: set[str] = set()


def classify(domain: str) -> str:
    d = domain.lower().rstrip(".")
    for suffixes, name in SERVICE_RULES:
        for s in suffixes:
            if d == s or d.endswith("." + s):
                return name
    # 未知のものは登録可能ドメイン相当まで縮めて表示する
    parts = d.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "or", "ne", "ac", "go", "com", "net", "org"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else d


def _auth_header() -> dict[str, str]:
    user = str(config.get("adguard_user") or "")
    pw = str(config.get("adguard_password") or "")
    if not user:
        return {}
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _fetch_querylog(limit: int = 200) -> list[dict]:
    base = str(config.get("adguard_url") or "").rstrip("/")
    if not base:
        raise RuntimeError("AdGuard Home の URL が設定されていません")
    url = f"{base}/control/querylog?limit={limit}"
    req = urllib.request.Request(url, headers=_auth_header())
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("data") or []


def _log_path(day: datetime | None = None) -> Path:
    day = day or datetime.now()
    return config.NETLOG_ROOT / f"{day:%Y-%m-%d}.jsonl"


def _append(records: list[dict]) -> None:
    if not records:
        return
    path = _log_path()
    with path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _process(entries: list[dict]) -> list[dict]:
    fresh: list[dict] = []
    for e in entries:
        q = e.get("question") or {}
        domain = str(q.get("name") or "").rstrip(".")
        if not domain or domain.lower().endswith(IGNORE_SUFFIXES):
            continue
        ts = str(e.get("time") or "")
        client = str(e.get("client") or "")
        key = f"{ts}|{client}|{domain}"
        if key in _seen_keys:
            continue
        _seen_keys.add(key)
        service = classify(domain)
        blocked = str(e.get("reason") or "").lower().startswith("filtered")
        rec = {
            "time": ts,
            "client": client,
            "domain": domain,
            "service": service,
            "blocked": blocked,
        }
        fresh.append(rec)
    if len(_seen_keys) > 20000:
        _seen_keys.clear()
    return fresh


def today_summary() -> list[dict]:
    return [{"service": s, "count": c} for s, c in TODAY_COUNTS.most_common(40)]


def read_day(day: str) -> list[dict]:
    p = config.NETLOG_ROOT / f"{day}.jsonl"
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def timeline_for_ticker(day: str) -> list[str]:
    """テロップ用の1行テキスト列を作る。連続する同一サービスはまとめる。"""
    records = read_day(day)
    lines: list[str] = []
    last = None
    for r in records:
        t = r["time"][11:16] if len(r["time"]) >= 16 else ""
        label = f"{t}  {r['service']}"
        if r.get("blocked"):
            label += "  [遮断]"
        if label != last:
            lines.append(label)
            last = label
    return lines


def _cleanup() -> None:
    cutoff = datetime.now() - timedelta(days=int(config.get("netlog_retention_days")))
    for p in config.NETLOG_ROOT.glob("*.jsonl"):
        try:
            if datetime.strptime(p.stem, "%Y-%m-%d") < cutoff:
                p.unlink()
        except Exception:
            continue


async def loop() -> None:
    last_cleanup = 0.0
    current_day = datetime.now().strftime("%Y-%m-%d")
    while True:
        interval = float(config.get("netlog_poll_seconds"))
        if not config.get("netlog_enabled"):
            await asyncio.sleep(30)
            continue
        try:
            entries = await asyncio.to_thread(_fetch_querylog)
            fresh = _process(entries)
            if fresh:
                await asyncio.to_thread(_append, fresh)
                for r in fresh:
                    TODAY_COUNTS[r["service"]] += 1
                RECENT[:0] = reversed(fresh)
                del RECENT[500:]
            STATE.update(ok=True, error="", last_poll=time.time(),
                         entries=STATE["entries"] + len(fresh))
        except urllib.error.HTTPError as exc:
            STATE.update(ok=False, error=f"HTTP {exc.code}: 認証情報を確認してください")
        except Exception as exc:
            STATE.update(ok=False, error=str(exc))

        day = datetime.now().strftime("%Y-%m-%d")
        if day != current_day:
            current_day = day
            TODAY_COUNTS.clear()

        if time.time() - last_cleanup >= 3600:
            last_cleanup = time.time()
            await asyncio.to_thread(_cleanup)

        await asyncio.sleep(interval)
