"""温度と CPU の監視.

Raspberry Pi 3B+ の挙動 (実装上の前提):
  - 既定のソフトリミットは 60℃。ここで 1.4GHz -> 1.2GHz へ自動降格する。
    これは正常動作であり、危険信号ではない。
  - 80℃ 以上で段階的にクロックが下がり、vcgencmd get_throttled にビットが立つ。
  - 85℃ で 600MHz まで落ちる。
したがって「異常」として扱うのは 72℃ 以上 (設定可能) からとする。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
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

# vcgencmd get_throttled のビット定義
_THROTTLE_BITS = {
    0: "低電圧を検出中",
    1: "ARM周波数を制限中",
    2: "スロットリング中",
    3: "温度リミットに到達中",
    16: "低電圧を検出した履歴あり",
    17: "ARM周波数制限の履歴あり",
    18: "スロットリングの履歴あり",
    19: "温度リミット到達の履歴あり",
}


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


def read_throttled() -> dict:
    if shutil.which("vcgencmd") is None:
        return {"raw": "", "flags": [], "available": False}
    try:
        out = subprocess.check_output(["vcgencmd", "get_throttled"], text=True, timeout=3)
    except Exception:
        return {"raw": "", "flags": [], "available": False}
    m = re.search(r"0x([0-9a-fA-F]+)", out)
    if not m:
        return {"raw": out.strip(), "flags": [], "available": True}
    value = int(m.group(1), 16)
    flags = [desc for bit, desc in _THROTTLE_BITS.items() if value & (1 << bit)]
    return {"raw": f"0x{value:x}", "flags": flags, "available": True, "value": value}


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
        "throttled": read_throttled(),
    }


async def loop() -> None:
    last_throttle_check = 0.0
    throttle_info = {"raw": "", "flags": [], "available": False}
    while True:
        temp = read_temp()
        cpu = read_cpu_percent()
        MODE.report_temperature(temp)

        now = time.time()
        if now - last_throttle_check >= 30:
            last_throttle_check = now
            throttle_info = await asyncio.to_thread(read_throttled)

        HISTORY.append({
            "t": round(now),
            "temp": round(temp, 1),
            "cpu": round(cpu, 1),
            "freq": read_freq_mhz(),
            "mode": MODE.mode,
        })
        del HISTORY[:-_MAX_HISTORY]

        if throttle_info.get("flags"):
            active = [f for f in throttle_info["flags"] if "履歴" not in f]
            if active:
                log.warning("スロットリング検出: %s", " / ".join(active))

        await asyncio.sleep(5)
