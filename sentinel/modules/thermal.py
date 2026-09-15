"""温度と CPU の監視.

Raspberry Pi 3B+ の挙動 (実装上の前提):
  - 既定のソフトリミットは 60℃。ここで 1.4GHz -> 1.2GHz へ自動降格する。
    これは正常動作であり、危険信号ではない。
  - 80℃ 以上で段階的にクロックが下がる。
  - 85℃ で 600MHz まで落ちる。
したがって「異常」として扱うのは 72℃ 以上 (設定可能) からとする。

vcgencmd get_throttled によるスロットリング検出・低電圧検出は監視しない
(CLAUDE.md #24)。実機では常時何らかのビットが立ち続け、エラー扱いにする
意味がなかったため、監視自体を削除した。電源トラブルの切り分けは
`vcgencmd get_throttled` を手動で叩けば従来どおり確認できる。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

from ..core import config
from ..core.state import MODE

log = logging.getLogger("sentinel.thermal")

THERMAL_ZONE = Path("/sys/class/thermal/thermal_zone0/temp")
CPU_FREQ = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
GOVERNOR = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")

HISTORY: list[dict] = []          # (メモリ上のみ。ディスクには書かない)
_MAX_HISTORY = 720                # 5秒間隔で約1時間分

_prev_cpu: tuple[int, int] | None = None


def read_temp() -> float:
    try:
        return int(THERMAL_ZONE.read_text().strip()) / 1000.0
    except Exception:
        return 0.0


def read_cpu_percent() -> float:
    """/proc/stat の差分から全体使用率を求める (psutil 非依存)。"""
    global _prev_cpu
    try:
        line = Path("/proc/stat").read_text().splitlines()[0]
    except Exception:
        return 0.0
    parts = [int(x) for x in line.split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
    total = sum(parts)
    if _prev_cpu is None:
        _prev_cpu = (idle, total)
        return 0.0
    d_idle = idle - _prev_cpu[0]
    d_total = total - _prev_cpu[1]
    _prev_cpu = (idle, total)
    if d_total <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * (1.0 - d_idle / d_total)))


def read_memory() -> dict:
    info: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            info[k] = int(v.strip().split()[0])
    except Exception:
        return {"total_mb": 0, "available_mb": 0, "used_percent": 0.0}
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    return {
        "total_mb": round(total / 1024),
        "available_mb": round(avail / 1024),
        "used_percent": round(100.0 * (1 - avail / total), 1) if total else 0.0,
    }


def read_freq_mhz() -> int:
    try:
        return int(CPU_FREQ.read_text().strip()) // 1000
    except Exception:
        return 0


def read_governor() -> str:
    try:
        return GOVERNOR.read_text().strip()
    except Exception:
        return "unknown"


def read_load() -> list[float]:
    try:
        return list(os.getloadavg())
    except Exception:
        return [0.0, 0.0, 0.0]


def read_uptime() -> float:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except Exception:
        return 0.0


def read_disk() -> dict:
    try:
        total, used, free = shutil.disk_usage(config.DATA_ROOT)
    except Exception:
        return {"total_gb": 0, "used_gb": 0, "free_gb": 0, "used_percent": 0.0}
    return {
        "total_gb": round(total / 1e9, 1),
        "used_gb": round(used / 1e9, 1),
        "free_gb": round(free / 1e9, 1),
        "used_percent": round(100.0 * used / total, 1) if total else 0.0,
    }


def snapshot() -> dict:
    return {
        "temperature_c": round(read_temp(), 1),
        "cpu_percent": round(read_cpu_percent(), 1),
        "freq_mhz": read_freq_mhz(),
        "governor": read_governor(),
        "load": [round(x, 2) for x in read_load()],
        "memory": read_memory(),
        "disk": read_disk(),
        "uptime_seconds": round(read_uptime()),
    }


async def loop() -> None:
    while True:
        temp = read_temp()
        cpu = read_cpu_percent()
        MODE.report_temperature(temp)

        now = time.time()
        HISTORY.append({
            "t": round(now),
            "temp": round(temp, 1),
            "cpu": round(cpu, 1),
            "freq": read_freq_mhz(),
            "mode": MODE.mode,
        })
        del HISTORY[:-_MAX_HISTORY]

        await asyncio.sleep(5)
