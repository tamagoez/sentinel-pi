"""Sentinel 設定管理.

設定は単一の JSON ファイルに集約する。全モジュールはここを唯一の参照先とする。
書き込みは atomic (tmp -> rename) で行い、SD カード上での破損を避ける。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# パス定義
# --------------------------------------------------------------------------

APP_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = APP_ROOT / "web" / "static"

# 外部ストレージ (永続データ)。存在しなければフォールバックする。
EXTERNAL_STORAGE = Path(os.environ.get("SENTINEL_STORAGE", "/mnt/VIDEOSD"))
FALLBACK_ROOT = Path.home() / "sentinel-data"

if EXTERNAL_STORAGE.is_dir():
    DATA_ROOT = EXTERNAL_STORAGE / "sentinel"
else:
    DATA_ROOT = FALLBACK_ROOT

CONFIG_PATH = DATA_ROOT / "config.json"
MUSIC_DIR = DATA_ROOT / "music"
CAPTURE_ROOT = DATA_ROOT / "captures"
ARCHIVE_ROOT = DATA_ROOT / "archive"      # 4時処理で生成した動画
NETLOG_ROOT = DATA_ROOT / "netlog"        # サービス別アクセス記録 (日次 JSONL)
LOG_PATH = DATA_ROOT / "sentinel.log"
STATE_PATH = DATA_ROOT / "state.json"     # 再生位置などの永続スナップショット

# RAM ディスク (高頻度書き込み用)。SD カードの摩耗を避けるため必ず tmpfs を使う。
RAM_ROOT = Path("/dev/shm") if Path("/dev/shm").is_dir() else DATA_ROOT
RUNTIME = RAM_ROOT / "sentinel-runtime"

for _d in (DATA_ROOT, MUSIC_DIR, CAPTURE_ROOT, ARCHIVE_ROOT, NETLOG_ROOT, RUNTIME):
    _d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# 既定値
# --------------------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    # --- Web ---
    "host": "0.0.0.0",
    "port": 8080,
    "password": "",                       # 空なら認証なし (LAN 内限定運用向け)
    "session_hours": 168,

    # --- モード制御 ---
    "eco_idle_minutes": 10.0,             # 全カメラ無検知がこれだけ続いたら Eco
    "temp_eco_c": 72.0,                   # これ以上で強制 Eco
    "temp_critical_c": 80.0,              # これ以上で Critical
    "temp_recover_c": 68.0,               # Critical/強制 Eco からの復帰閾値
    "mode_override": "auto",              # auto | normal | eco
    "eco_governor": "powersave",           # powersave | conservative | ondemand
                                           # powersave はクロックを最低固定にする。
                                           # conservative は負荷次第で結局上まで
                                           # 伸びうるため、eco の既定には向かない
    "normal_governor": "ondemand",

    # --- カメラ ---
    "cam_width": 320,
    "cam_height": 240,
    "cam_eco_width": 160,
    "cam_eco_height": 120,
    "jpeg_quality": 75,
    "live_fps": 2.0,
    "normal_fps": 1.0,
    "eco_fps": 0.2,
    "motion_threshold": 25,
    "motion_area_ratio": 0.02,
    "motion_area_max_ratio": 0.6,         # これ以上は「局所的な動体」ではなく画面
                                           # 全体の変化 (オートフォーカスの再合焦や
                                           # 露出/照明の変化) とみなし、動体扱いしない
    "motion_interval": 1.0,
    "motion_warmup_seconds": 2.0,         # カメラを開いた直後 (再接続・解像度変更を
                                           # 含む) はオートフォーカスの再合焦が起きや
                                           # すいため、この秒数は動体判定そのものを
                                           # スキップする
    "cam_autofocus": True,                # False でオートフォーカスを無効化 (対応
                                           # している機種のみ)。合焦動作そのものを
                                           # 動体と誤検知するカメラ向け
    "motion_debug_log": True,             # 動体判定のたびに、判定に使った実際の
                                           # 数値 (面積比・しきい値・warm 中かどうか
                                           # など) を tmpfs 上へ記録する。設定タブから
                                           # まとめて閲覧・コピーでき、次回のやり取りに
                                           # 貼り付けて調整に使う想定 (無制限には太ら
                                           # せず、カメラごとに直近分だけ保持する)
    "save_cooldown": 5.0,
    "retention_days": 14,
    "reconnect_seconds": 3,
    "camera_overrides": {},               # カメラID -> {設定キー: 値, ...}
                                           # 個別カメラだけ上の共有値を上書きする。
                                           # キーが無い/空ならそのカメラは共有値を使う。

    # --- 音楽 ---
    "music_enabled": True,
    "music_volume": 60,                   # 0-100
    "music_shuffle": True,
    "music_repeat": "all",                # all | one | off
    "music_shuffle_seed": 0,              # 0 なら起動時に生成
    "music_autoplay_on_presence": True,
    "mpg123_buffer_kb": 1024,             # アンダーラン対策のバッファ
    "alsa_device": "",                    # 空なら既定デバイス

    # --- Bluetooth ---
    "bt_enabled": True,
    "bt_poll_seconds": 3.0,
    "bt_device_volumes": {},              # MAC アドレス -> 音量(%) (接続時に自動適用)

    # --- 通知 ---
    "discord_webhook": "",
    "notify_motion": True,
    "notify_motion_grouped": False,       # True: 複数カメラの検知を 1 通にまとめる
                                           # (False: 今までどおりカメラごとに送る)
    "notify_min_interval": 60.0,          # 即時通知の最短間隔 (秒、下限として働く)
    "motion_notify_reset_seconds": 45.0,  # この秒数、検知が完全に途切れたら
                                           # 「動いていない」とみなす。動体が途切れず
                                           # 続いている間は (notify_min_interval が
                                           # 経過していても) 再通知しない — 継続中の
                                           # 同じイベントとして扱う。途切れてからの
                                           # 次の検知だけが新しいイベントとして通知
                                           # される。grouped=False では同一カメラ単位、
                                           # grouped=True では全カメラ合算単位で効く。
    "notify_summary": True,               # 無検知が続いたときの集計通知
    "notify_summary_after": 300.0,        # 無検知がこれだけ続いたら統計を送る (秒)
    "notify_mode_change": True,           # モード遷移 (通常/エコ/緊急) の通知
    "notify_system_events": True,
    # プレースホルダ: {camera} {mode} {temp} {time} (動体) /
    # {total} {duration_min} {quiet_min} (集計) / {old} {new} {reason} (モード)。
    # 知らないプレースホルダは無視され、書式が壊れている場合は既定文へ戻る。
    "notify_motion_title": "動体を検知しました",
    "notify_summary_title": "検知が落ち着きました — 合計 {total} 回",
    "notify_mode_title": "モードが {old} から {new} へ変わりました",

    # --- ネットワークログ ---
    "netlog_enabled": True,
    "adguard_url": "http://127.0.0.1:8083",
    "adguard_user": "admin",
    "adguard_password": "",
    "netlog_poll_seconds": 30.0,
    "netlog_retention_days": 14,

    # --- 定時処理 ---
    "maintenance_enabled": True,
    "maintenance_hour": 4,
    "maintenance_minute": 0,
    "reboot_after_maintenance": True,
    "timelapse_fps": 12,
    "timelapse_tile_width": 640,
    "ticker_enabled": True,

    # --- 端末 ---
    "terminal_enabled": True,
    "terminal_shell": "/bin/bash",
    "terminal_notify": True,
}

_LOCK = threading.RLock()
_CACHE: dict[str, Any] = {}


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load() -> dict[str, Any]:
    """設定を読み込む。未知キーは捨て、欠損キーは既定値で埋める。"""
    global _CACHE
    with _LOCK:
        raw: dict[str, Any] = {}
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
        merged = dict(DEFAULTS)
        for k, v in raw.items():
            if k in DEFAULTS:
                merged[k] = v
        _CACHE = merged
        _atomic_write(CONFIG_PATH, json.dumps(merged, indent=2, ensure_ascii=False))
        return merged


def get(key: str, default: Any = None) -> Any:
    with _LOCK:
        if not _CACHE:
            load()
        return _CACHE.get(key, DEFAULTS.get(key, default))


def all_values(hide_secrets: bool = True) -> dict[str, Any]:
    with _LOCK:
        if not _CACHE:
            load()
        out = dict(_CACHE)
    if hide_secrets:
        for k in ("password", "adguard_password", "discord_webhook"):
            if out.get(k):
                out[k] = "********"
    return out


# 型の丸め込み。UI からは文字列で来ることがあるため既定値の型に合わせる。
_INT_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, int) and not isinstance(v, bool)}
_FLOAT_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, float)}
_BOOL_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, bool)}
# dict 型の既定値を持つキー (例: bt_device_volumes) は、他の型のように
# 文字列化・数値化しては壊れるので、そのまま (dict であることだけ検証して)
# 通す。設定 UI の一般入力欄からは編集させず、専用の API からのみ書く前提。
_DICT_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, dict)}

# 安全域。範囲外の値でハードウェアを壊さないための制限。
_RANGES: dict[str, tuple[float, float]] = {
    "eco_idle_minutes": (1.0, 120.0),
    "temp_eco_c": (55.0, 82.0),
    "temp_critical_c": (65.0, 85.0),
    "temp_recover_c": (40.0, 80.0),
    "cam_width": (160, 1280),
    "cam_height": (120, 960),
    "cam_eco_width": (160, 640),
    "cam_eco_height": (120, 480),
    "jpeg_quality": (30, 95),
    "live_fps": (0.5, 10.0),
    "normal_fps": (0.1, 5.0),
    "eco_fps": (0.05, 2.0),
    "motion_threshold": (5, 100),
    "motion_area_ratio": (0.001, 0.5),
    "motion_area_max_ratio": (0.05, 1.0),
    "motion_interval": (0.2, 10.0),
    "motion_warmup_seconds": (0.0, 30.0),
    "motion_notify_reset_seconds": (5.0, 1800.0),
    "save_cooldown": (1.0, 300.0),
    "retention_days": (1, 3650),
    "music_volume": (0, 100),
    "mpg123_buffer_kb": (64, 8192),
    "notify_min_interval": (5.0, 3600.0),
    "notify_summary_after": (30.0, 86400.0),
    "netlog_poll_seconds": (5.0, 600.0),
    "netlog_retention_days": (1, 365),
    "maintenance_hour": (0, 23),
    "maintenance_minute": (0, 59),
    "timelapse_fps": (1, 30),
    "timelapse_tile_width": (160, 1280),
    "session_hours": (1, 8760),
}


def _coerce(key: str, value: Any) -> Any:
    if key in _DICT_KEYS:
        if not isinstance(value, dict):
            raise TypeError(f"{key} には object (dict) が必要です")
        return value
    if key in _BOOL_KEYS:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "on", "yes")
        return bool(value)
    if key in _INT_KEYS:
        value = int(float(value))
    elif key in _FLOAT_KEYS:
        value = float(value)
    else:
        value = str(value)
    if key in _RANGES:
        lo, hi = _RANGES[key]
        value = max(lo, min(hi, value))
        value = int(value) if key in _INT_KEYS else value
    return value


def coerce_value(key: str, value: Any) -> Any:
    """_coerce() の公開ラッパー。カメラごとの上書き設定 (camera_overrides)
    のように、update() を経由せず個々の値を DEFAULTS と同じ型・範囲に
    丸めたいモジュールから使う。未知キーや不正値は TypeError/ValueError。"""
    if key not in DEFAULTS:
        raise KeyError(key)
    return _coerce(key, value)


def update(patch: dict[str, Any]) -> dict[str, Any]:
    """部分更新。未知キーは無視し、既知キーは型と範囲を強制する。"""
    with _LOCK:
        if not _CACHE:
            load()
        changed: dict[str, Any] = {}
        for k, v in patch.items():
            if k not in DEFAULTS:
                continue
            if k in ("password", "adguard_password", "discord_webhook") and v == "********":
                continue          # UI が伏字をそのまま返してきた場合は無視
            try:
                coerced = _coerce(k, v)
            except (TypeError, ValueError):
                continue
            if _CACHE.get(k) != coerced:
                _CACHE[k] = coerced
                changed[k] = coerced
        if changed:
            _atomic_write(CONFIG_PATH, json.dumps(_CACHE, indent=2, ensure_ascii=False))
        return changed


load()
