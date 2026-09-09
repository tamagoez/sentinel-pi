"""USB カメラ監視.

各カメラを独立プロセスで回す。OpenCV は GIL を握るため、スレッドではなく
プロセスで分離することでメインの Web サーバーが固まらないようにする。

プロセス間のやり取りは /dev/shm 上のファイル経由 (SD カードを摩耗させない)。
    latest.jpg   最新フレーム
    status.json  状態
    mode         親が書き、ワーカーが読む現在モード
    live         ライブ視聴中のクライアント数
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from multiprocessing import Event, Process
from pathlib import Path

from ..core import config
from ..core.state import CRITICAL, ECO, MODE, NORMAL

log = logging.getLogger("sentinel.camera")

WORKERS: dict[str, "CameraWorker"] = {}
_DISCOVER_INTERVAL = 15.0

# 個別カメラごとに上書きできる設定キー (config.DEFAULTS のサブセット)。
# camera_overrides[cid] にこの中のキーがあれば、共有設定 (config.get(key))
# より優先する。設定 UI 側もこのキー集合をそのまま使う。
CAMERA_OVERRIDE_KEYS = (
    "cam_width", "cam_height", "cam_eco_width", "cam_eco_height",
    "jpeg_quality", "live_fps", "normal_fps", "eco_fps",
    "motion_threshold", "motion_area_ratio", "motion_area_max_ratio",
    "motion_interval", "motion_warmup_seconds", "cam_autofocus",
    "save_cooldown", "reconnect_seconds",
)


def effective_settings(cid: str) -> dict:
    """カメラ cid に実際に適用される設定 (共有値 + 個別上書き)。"""
    base = {k: config.get(k) for k in CAMERA_OVERRIDE_KEYS}
    overrides = (config.get("camera_overrides") or {}).get(cid) or {}
    base.update({k: v for k, v in overrides.items() if k in CAMERA_OVERRIDE_KEYS})
    return base


def set_overrides(cid: str, patch: dict) -> dict:
    """cid の個別設定を部分更新する。値が None のキーは削除 (共有値に戻す)。
    patch が空、または全キー削除の結果 cid の上書きが空になれば
    camera_overrides から cid ごと取り除く。"""
    all_overrides = dict(config.get("camera_overrides") or {})
    cur = dict(all_overrides.get(cid) or {})
    for k, v in patch.items():
        if k not in CAMERA_OVERRIDE_KEYS:
            continue
        if v is None:
            cur.pop(k, None)
            continue
        cur[k] = config.coerce_value(k, v)
    if cur:
        all_overrides[cid] = cur
    else:
        all_overrides.pop(cid, None)
    config.update({"camera_overrides": all_overrides})
    return cur

# 動体検知イベントを通知モジュールへ渡すためのフック (notify 側が差し込む)
ON_MOTION = None   # Callable[[str, str], None] -> (camera_id, capture_path)


# ---------------------------------------------------------------- 検出

def _v4l2_info(device: str) -> tuple[str | None, str]:
    try:
        out = subprocess.check_output(["v4l2-ctl", "-d", device, "--all"],
                                      stderr=subprocess.DEVNULL, text=True, timeout=3)
    except Exception:
        return None, ""
    if "Video Capture" not in out or "Streaming" not in out:
        return None, ""
    if "bcm2835" in out.lower():
        return None, ""     # Pi 内蔵の ISP/コーデックデバイスは除外
    card = next((x.split(":", 1)[1].strip() for x in out.splitlines() if "Card type" in x), None)
    bus = next((x.split(":", 1)[1].strip() for x in out.splitlines() if "Bus info" in x), "")
    return card or Path(device).name, bus


def discover() -> list[dict]:
    """/dev/v4l/by-id を走査する。by-id は再接続しても不変なので ID が安定する。"""
    base = Path("/dev/v4l/by-id")
    found: list[dict] = []
    if not base.is_dir():
        return found
    for link in sorted(base.glob("*-video-index0")):
        try:
            target = link.resolve()
            if not str(target).startswith("/dev/video") or not target.exists():
                continue
            name, bus = _v4l2_info(str(target))
            if name is None:
                continue
            sysfs = Path("/sys/class/video4linux") / target.name / "device"
            if sysfs.exists() and "usb" not in str(sysfs.resolve()).lower():
                continue
            cid = re.sub(r"-video-index0$", "", link.name)
            cid = re.sub(r"[^A-Za-z0-9_.-]+", "_", cid).strip("._-") or target.name
            found.append({"id": cid, "device": str(target), "name": name, "bus": bus})
        except Exception:
            log.warning("カメラ検出に失敗: %s", link, exc_info=True)
    return found


# ---------------------------------------------------------------- IPC

def rt(cid: str) -> Path:
    d = config.RUNTIME / "cam" / cid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def read_status(cid: str) -> dict:
    try:
        return json.loads((rt(cid) / "status.json").read_text(encoding="utf-8"))
    except Exception:
        return {"id": cid, "state": "unknown", "fps": 0, "motion": False,
                "reconnects": 0, "errors": 0}


def _read_int(path: Path, default: int = 0) -> int:
    try:
        return int(path.read_text().strip())
    except Exception:
        return default


def set_mode_files(mode: str) -> None:
    for cid in list(WORKERS):
        try:
            (rt(cid) / "mode").write_text(mode)
        except Exception:
            pass


def bump_live(cid: str, delta: int) -> int:
    p = rt(cid) / "live"
    n = max(0, _read_int(p) + delta)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(str(n))
    os.replace(tmp, p)
    return n


def live_clients(cid: str) -> int:
    return _read_int(rt(cid) / "live")


# --------------------------------------------------------- 動体検知の診断ログ

# motion_debug_log 有効時、判定のたびに実際の数値を残す。tmpfs 上とはいえ
# 無制限に太らせない (1GB RAM 機での既定方針)。追記のたびにファイルサイズだけ
# 安く確認し、超えたときだけ直近分を残して巻き戻す。
_MOTION_DEBUG_MAX_BYTES = 200_000
_MOTION_DEBUG_KEEP_LINES = 300


def _log_motion_debug(cid: str, entry: dict) -> None:
    path = rt(cid) / "motion_debug.jsonl"
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if path.stat().st_size > _MOTION_DEBUG_MAX_BYTES:
            lines = path.read_text(encoding="utf-8").splitlines()[-_MOTION_DEBUG_KEEP_LINES:]
            _write_atomic(path, ("\n".join(lines) + "\n").encode("utf-8"))
    except Exception:
        pass


def read_motion_debug_log(cid: str) -> str:
    try:
        return (rt(cid) / "motion_debug.jsonl").read_text(encoding="utf-8")
    except Exception:
        return ""


# ---------------------------------------------------------------- ワーカー

def _worker(cid: str, device: str, stop: "Event", cfg: dict) -> None:
    """別プロセスで動く。cfg は起動時のスナップショット (モードは都度ファイル参照)。"""
    import cv2  # プロセス内で import する (fork 後の状態を汚さない)

    d = config.RUNTIME / "cam" / cid
    d.mkdir(parents=True, exist_ok=True)

    def emit_status(**kw) -> None:
        try:
            cur = json.loads((d / "status.json").read_text(encoding="utf-8"))
        except Exception:
            cur = {}
        cur.update(kw)
        cur["id"] = cid
        tmp = d / "status.tmp"
        tmp.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, d / "status.json")

    def current_mode() -> str:
        try:
            return (d / "mode").read_text().strip() or NORMAL
        except Exception:
            return NORMAL

    cap = None
    prev = None
    failures = 0
    reconnects = 0
    last_motion_check = 0.0
    last_save = 0.0
    frames = 0
    fps_t0 = time.monotonic()
    applied_res: tuple[int, int] | None = None
    cam_opened_at = 0.0

    def target_res(mode: str) -> tuple[int, int]:
        if mode == NORMAL:
            return int(cfg["cam_width"]), int(cfg["cam_height"])
        return int(cfg["cam_eco_width"]), int(cfg["cam_eco_height"])

    def open_cam(mode: str) -> bool:
        nonlocal cap, applied_res, cam_opened_at, prev
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
            cap = None
        if not os.path.exists(device):
            return False
        try:
            c = cv2.VideoCapture(device, cv2.CAP_V4L2)
            if not c.isOpened():
                c.release()
                return False
            w, h = target_res(mode)
            try:
                c.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            except Exception:
                pass
            c.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            c.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            c.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            try:
                # 対応機種のみ有効。オートフォーカスの再合焦そのものを動体と
                # 誤検知するカメラ向けに無効化できるようにしている
                # (CAMERA_OVERRIDE_KEYS の cam_autofocus)。非対応機種では
                # 単に無視される。
                c.set(cv2.CAP_PROP_AUTOFOCUS, 1 if cfg.get("cam_autofocus", True) else 0)
            except Exception:
                pass
            ok, frame = c.read()
            if not ok or frame is None:
                c.release()
                return False
            cap = c
            applied_res = (w, h)
            cam_opened_at = time.monotonic()
            prev = None   # 開き直した直後の基準フレームは古いので作り直す
            return True
        except Exception:
            return False

    emit_status(device=device, state="starting", started=time.time(),
                reconnects=0, errors=0, fps=0, motion=False)

    while not stop.is_set():
        try:
            mode = current_mode()
            viewers = _read_int(d / "live")

            # Critical では最初の1台以外は完全停止する (親が停止させるが保険)
            if mode == CRITICAL and (d / "halt").exists():
                if cap is not None:
                    cap.release()
                    cap = None
                emit_status(state="halted", fps=0, motion=False)
                time.sleep(2)
                continue

            if viewers > 0:
                fps = float(cfg["live_fps"])
            elif mode == NORMAL:
                fps = float(cfg["normal_fps"])
            else:
                fps = float(cfg["eco_fps"])
            interval = 1.0 / max(0.05, fps)

            if cap is None:
                if open_cam(mode):
                    failures = 0
                    reconnects += 1
                    emit_status(state="online", reconnects=reconnects, clients=viewers)
                else:
                    failures += 1
                    emit_status(state="offline", errors=failures, clients=viewers, fps=0)
                    delay = min(60.0, float(cfg["reconnect_seconds"]) * (2 ** min(failures - 1, 4)))
                    time.sleep(delay)
                    continue

            # モードが変わって解像度が変わるべきなら開き直す
            if applied_res != target_res(mode):
                open_cam(mode)
                if cap is None:
                    continue

            t0 = time.monotonic()
            ok, frame = cap.read()
            if not ok or frame is None:
                failures += 1
                emit_status(state="reconnecting", errors=failures)
                if failures >= 2:
                    try:
                        cap.release()
                    except Exception:
                        pass
                    cap = None
                time.sleep(0.3)
                continue
            failures = 0
            frames += 1

            q = int(cfg["jpeg_quality"]) if mode == NORMAL else max(35, int(cfg["jpeg_quality"]) - 20)
            ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), q])
            if ok:
                tmp = d / "latest.tmp"
                tmp.write_bytes(jpg.tobytes())
                os.replace(tmp, d / "latest.jpg")

            now = time.monotonic()
            if now - last_motion_check >= float(cfg["motion_interval"]):
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                small = cv2.GaussianBlur(cv2.resize(gray, (160, 120)), (21, 21), 0)
                motion = False
                ratio = None
                lo = float(cfg["motion_area_ratio"])
                hi = float(cfg.get("motion_area_max_ratio", 1.0))
                # カメラを開いた直後 (再接続・解像度切り替え含む) はオートフォーカス
                # の再合焦が起きやすく、画面全体がぼけて戻るだけで動体と誤検知
                # しやすい。この間は判定そのものをスキップし、基準フレームだけ
                # 更新しておく (ウォームアップが明けた瞬間に古い基準フレームと
                # 比べて誤検知しないように)。
                warm = now - cam_opened_at < float(cfg.get("motion_warmup_seconds", 0.0))
                if not warm and prev is not None:
                    diff = cv2.absdiff(prev, small)
                    _, th = cv2.threshold(diff, int(cfg["motion_threshold"]), 255, cv2.THRESH_BINARY)
                    th = cv2.dilate(th, None, iterations=2)
                    ratio = cv2.countNonZero(th) / th.size
                    # 上限 (motion_area_max_ratio) は、画面のほとんどが一度に
                    # 変化するケース (オートフォーカスの再合焦・露出/照明の変化)
                    # を、局所的な物体の動きと区別して除外するためのもの。
                    motion = lo <= ratio < hi
                prev = small
                last_motion_check = now
                if cfg.get("motion_debug_log", True):
                    _log_motion_debug(cid, {
                        "t": datetime.now().strftime("%H:%M:%S"),
                        "mode": mode, "warm": warm,
                        "ratio": round(ratio, 4) if ratio is not None else None,
                        "lo": lo, "hi": hi, "threshold": int(cfg["motion_threshold"]),
                        "motion": motion,
                    })
                if motion:
                    emit_status(motion=True, last_motion=time.time())
                    (d / "motion_flag").write_text(str(time.time()))
                    if now - last_save >= float(cfg["save_cooldown"]):
                        folder = config.CAPTURE_ROOT / cid / datetime.now().strftime("%Y-%m-%d")
                        folder.mkdir(parents=True, exist_ok=True)
                        stamp = datetime.now().strftime("%H-%M-%S-%f")[:-3]
                        path = folder / f"{stamp}.jpg"
                        cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), q])
                        last_save = now
                        (d / "last_capture").write_text(str(path))
                else:
                    emit_status(motion=False)

            if time.monotonic() - fps_t0 >= 5:
                elapsed = time.monotonic() - fps_t0
                emit_status(fps=round(frames / elapsed, 2), clients=viewers,
                            state="online", mode=mode,
                            resolution=f"{applied_res[0]}x{applied_res[1]}" if applied_res else "")
                frames = 0
                fps_t0 = time.monotonic()

            # eco/critical で誰も見ておらず、かつ次のフレームまで十分な間隔が
            # あるときは、待っている間 USB カメラを開いたまま (=V4L2 の
            # STREAMON 状態のまま) にしない。UVC カメラは dequeue の頻度に
            # 関わらずストリーミング状態である限り USB 上へフレームを流し
            # 続けることが多く、CLAUDE.md にある「USB 2.0 ハブを Ethernet と
            # 共有」という制約下ではその帯域そのものが負荷になる。ここで
            # release して次サイクルで開き直すことで、待機中は実際に何も
            # 流れていない状態にする (間隔が短いとオープンのやり直しの方が
            # 高くつくので、ある程度長い間隔のときだけ行う)。
            if mode != NORMAL and viewers == 0 and interval >= 2.0 and cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
                cap = None
                applied_res = None

            rest = interval - (time.monotonic() - t0)
            if rest > 0:
                time.sleep(rest)

        except Exception as exc:
            emit_status(state="error", error=str(exc))
            time.sleep(1)

    if cap is not None:
        try:
            cap.release()
        except Exception:
            pass
    emit_status(state="stopped", fps=0, motion=False)


class CameraWorker:
    def __init__(self, cid: str, device: str, name: str = "", bus: str = "") -> None:
        self.id = cid
        self.device = device
        self.name = name or device
        self.bus = bus
        self.proc: Process | None = None
        self.stop_event = None
        self.halted = False

    def start(self) -> None:
        self.stop()
        self.stop_event = Event()
        snapshot = effective_settings(self.id)
        (rt(self.id) / "mode").write_text(MODE.mode)
        self.proc = Process(target=_worker, args=(self.id, self.device, self.stop_event, snapshot),
                            daemon=True, name=f"cam-{self.id}")
        self.proc.start()
        log.info("カメラワーカー起動: %s (%s)", self.id, self.device)

    def stop(self) -> None:
        if self.stop_event is not None:
            self.stop_event.set()
        if self.proc is not None and self.proc.is_alive():
            self.proc.join(timeout=4)
            if self.proc.is_alive():
                self.proc.kill()
                self.proc.join(timeout=2)
        self.proc = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.is_alive()


# ---------------------------------------------------------------- 統括

def reconcile() -> None:
    """実際に繋がっているカメラとワーカーの集合を一致させる。"""
    found = discover()
    ids = {c["id"] for c in found}
    for cam in found:
        cid = cam["id"]
        w = WORKERS.get(cid)
        if w is None:
            w = WORKERS[cid] = CameraWorker(cid, cam["device"], cam["name"], cam["bus"])
            w.start()
        elif w.device != cam["device"]:
            w.device, w.name, w.bus = cam["device"], cam["name"], cam["bus"]
            w.start()
        elif not w.alive and not w.halted:
            log.warning("カメラワーカー %s が停止していたため再起動します", cid)
            w.start()
    for cid, w in list(WORKERS.items()):
        if cid not in ids:
            w.stop()
            WORKERS.pop(cid, None)
            log.info("カメラ切断: %s", cid)


def on_mode_change(new: str, old: str) -> None:
    set_mode_files(new)
    if new == CRITICAL:
        # 1台だけ残して他は完全停止する
        for i, (cid, w) in enumerate(sorted(WORKERS.items())):
            if i == 0:
                (rt(cid) / "halt").unlink(missing_ok=True)
                continue
            (rt(cid) / "halt").write_text("1")
            w.halted = True
            w.stop()
        log.warning("緊急モード: カメラを1台に縮退しました")
    elif old == CRITICAL:
        for cid, w in WORKERS.items():
            (rt(cid) / "halt").unlink(missing_ok=True)
            if w.halted:
                w.halted = False
                w.start()
        log.info("緊急モードから復帰しました")


async def loop() -> None:
    """検出・保守ループ。動体フラグを拾ってモード管理と通知へ渡す。"""
    last_discover = 0.0
    last_cleanup = 0.0
    seen_motion: dict[str, float] = {}
    while True:
        now = time.time()
        if now - last_discover >= _DISCOVER_INTERVAL:
            await asyncio.to_thread(reconcile)
            last_discover = now

        # 動体フラグの収集
        for cid in list(WORKERS):
            flag = rt(cid) / "motion_flag"
            try:
                ts = float(flag.read_text().strip())
            except Exception:
                continue
            if ts > seen_motion.get(cid, 0.0):
                seen_motion[cid] = ts
                MODE.report_motion()
                if ON_MOTION is not None:
                    cap_path = ""
                    try:
                        cap_path = (rt(cid) / "last_capture").read_text().strip()
                    except Exception:
                        pass
                    try:
                        ON_MOTION(cid, cap_path)
                    except Exception:
                        log.exception("動体通知フックが失敗しました")

        # 古いキャプチャの削除 (1時間毎)
        if now - last_cleanup >= 3600:
            last_cleanup = now
            await asyncio.to_thread(_cleanup)

        await asyncio.sleep(1.0)


def _cleanup() -> None:
    cutoff = datetime.now() - timedelta(days=int(config.get("retention_days")))
    for p in config.CAPTURE_ROOT.glob("*/*/*.jpg"):
        try:
            if datetime.fromtimestamp(p.stat().st_mtime) < cutoff:
                p.unlink()
        except Exception:
            pass
    for d in config.CAPTURE_ROOT.glob("*/*"):
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except Exception:
            pass


def status() -> list[dict]:
    out = []
    for cid, w in sorted(WORKERS.items()):
        s = read_status(cid)
        s.update(name=w.name, device=w.device, bus=w.bus, alive=w.alive,
                 halted=w.halted, clients=live_clients(cid),
                 has_frame=(rt(cid) / "latest.jpg").exists())
        if s.get("last_motion"):
            s["motion_age"] = round(time.time() - s["last_motion"], 1)
        out.append(s)
    return out


def shutdown() -> None:
    for w in WORKERS.values():
        w.stop()
    WORKERS.clear()
