"""動作モードの状態機械.

このモジュールが「今どのモードか」の唯一の決定者である。
各モジュールは購読 (subscribe) して通知を受け取り、自分の負荷を調整する。

モード:
    normal   通常運転。人が居る、または温度に余裕がある。
    eco      省電力。全カメラ無検知が一定時間続いた状態。
    critical 緊急。温度が危険域。カメラを1台に絞り、音楽は止める。

遷移の優先順位:
    1. 温度が critical 閾値以上          -> critical (強制)
    2. 温度が eco 閾値以上                -> eco (強制)
    3. mode_override が auto 以外         -> その値
    4. 直近の動体検知からの経過時間で判定 -> normal / eco
"""

from __future__ import annotations

import glob
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from . import config

log = logging.getLogger("sentinel.state")

NORMAL = "normal"
ECO = "eco"
CRITICAL = "critical"

_GOVERNOR_GLOB = "/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"


class ModeManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._mode = NORMAL
        self._reason = "起動直後"
        self._last_motion = time.time()
        self._last_change = time.time()
        self._temp_c = 0.0
        self._subscribers: list[Callable[[str, str], None]] = []
        self._forced_by_temp = False
        self._history: list[dict] = []          # 直近のモード遷移履歴

    # ---------------- 購読 ----------------

    def subscribe(self, fn: Callable[[str, str], None]) -> None:
        """モード変更時に fn(new_mode, old_mode) が呼ばれる。例外は握り潰す。"""
        with self._lock:
            self._subscribers.append(fn)

    def _notify(self, new: str, old: str) -> None:
        for fn in list(self._subscribers):
            try:
                fn(new, old)
            except Exception:
                log.exception("mode subscriber failed: %s", getattr(fn, "__qualname__", fn))

    # ---------------- 入力 ----------------

    def report_motion(self) -> None:
        """カメラモジュールが動体を検知したときに呼ぶ。"""
        with self._lock:
            self._last_motion = time.time()
        self.evaluate()

    def report_temperature(self, celsius: float) -> None:
        with self._lock:
            self._temp_c = celsius
        self.evaluate()

    # ---------------- 参照 ----------------

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    @property
    def temperature(self) -> float:
        with self._lock:
            return self._temp_c

    @property
    def idle_seconds(self) -> float:
        with self._lock:
            return time.time() - self._last_motion

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "mode": self._mode,
                "reason": self._reason,
                "temperature_c": round(self._temp_c, 1),
                "idle_seconds": round(time.time() - self._last_motion, 1),
                "since": self._last_change,
                "forced_by_temp": self._forced_by_temp,
                "override": config.get("mode_override"),
                "history": list(self._history[-20:]),
            }

    # ---------------- 判定 ----------------

    def _decide(self) -> tuple[str, str, bool]:
        temp = self._temp_c
        crit = float(config.get("temp_critical_c"))
        eco_t = float(config.get("temp_eco_c"))
        recover = float(config.get("temp_recover_c"))

        # 温度による強制。ヒステリシスを入れて振動を防ぐ。
        if temp >= crit:
            return CRITICAL, f"CPU温度 {temp:.1f}℃ が緊急閾値 {crit:.0f}℃ 以上です", True
        if self._forced_by_temp and temp > recover:
            if self._mode == CRITICAL and temp >= eco_t:
                return CRITICAL, f"CPU温度 {temp:.1f}℃ が復帰閾値まで下がっていません", True
            return ECO, f"CPU温度 {temp:.1f}℃ が復帰閾値 {recover:.0f}℃ を上回っています", True
        if temp >= eco_t:
            return ECO, f"CPU温度 {temp:.1f}℃ がエコ閾値 {eco_t:.0f}℃ 以上です", True

        override = str(config.get("mode_override"))
        if override == NORMAL:
            return NORMAL, "手動で通常モードに固定されています", False
        if override == ECO:
            return ECO, "手動でエコモードに固定されています", False

        idle = time.time() - self._last_motion
        limit = float(config.get("eco_idle_minutes")) * 60.0
        if idle >= limit:
            return ECO, f"全カメラで {idle / 60:.0f} 分間 動体を検知していません", False
        return NORMAL, "動体を検知しています", False

    def evaluate(self) -> str:
        with self._lock:
            new, reason, forced = self._decide()
            old = self._mode
            self._reason = reason
            self._forced_by_temp = forced
            if new == old:
                return old
            self._mode = new
            self._last_change = time.time()
            self._history.append({"at": time.time(), "from": old, "to": new, "reason": reason})
            self._history = self._history[-100:]

        log.info("モード遷移 %s -> %s (%s)", old, new, reason)
        _apply_governor(new)
        self._notify(new, old)
        return new


def sync_governor() -> None:
    """起動直後に一度だけ呼ぶ。

    _apply_governor() は ModeManager.evaluate() がモード「遷移」を検知した
    ときにしか呼ばれない。起動直後は _mode の初期値が既に "normal" で、
    最初の evaluate() も大抵 normal のままと判定するため遷移が起きず、
    起動時点で実際にどのガバナが設定されているか (前回の eco 運用の
    名残や DietPi 既定の ondemand など) は一度も確認・強制されない。
    main.py の起動処理から一度呼ぶことでこの隙間を埋める。
    """
    _apply_governor(MODE.mode)


def _apply_governor(mode: str) -> None:
    """CPU ガバナを切り替える。

    sentinel は非 root ユーザーで動作するため (CLAUDE.md #4)、
    scaling_governor への直接書き込みは PermissionError になる。以前は
    それをここで黙って握り潰していたため、実機では eco モードに入っても
    CPU ガバナが一度も切り替わっておらず、「eco でも負荷がほぼ変わらない」
    という報告の直接の原因だった。sudoers で個別に許可した
    scripts/sentinel-set-governor.sh を sudo 経由で呼ぶことで、実際に
    書き込めるようにしている。それでも失敗する場合 (sudoers 未設定の
    古い導入など) は引き続き致命的にはしない。
    """
    target = str(config.get("eco_governor") if mode != NORMAL else config.get("normal_governor"))

    paths = glob.glob(_GOVERNOR_GLOB)
    if not paths:
        return
    # 利用可能なガバナを確認してから書く
    avail_path = Path(paths[0]).parent / "scaling_available_governors"
    try:
        available = avail_path.read_text().split()
    except Exception:
        available = []
    if available and target not in available:
        log.warning("ガバナ %s は利用できません (利用可能: %s)", target, " ".join(available))
        return

    script = config.APP_ROOT.parent / "scripts" / "sentinel-set-governor.sh"
    try:
        p = subprocess.run(["sudo", "-n", str(script), target],
                           capture_output=True, text=True, timeout=5)
        if p.returncode != 0:
            log.warning("ガバナ %s への切り替えに失敗しました: %s", target,
                       (p.stderr or p.stdout).strip())
    except Exception as exc:
        log.warning("ガバナ %s への切り替えに失敗しました: %s", target, exc)


# ---------------- 永続スナップショット ----------------


def save_snapshot(payload: dict) -> None:
    """再生位置などをディスクへ保存する。呼び出し頻度は極力抑えること。"""
    tmp = config.STATE_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, config.STATE_PATH)
    except Exception:
        log.exception("状態の保存に失敗しました")


def load_snapshot() -> dict:
    try:
        return json.loads(config.STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


MODE = ModeManager()
