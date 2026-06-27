from __future__ import annotations

import os
import signal
import threading
import time
from typing import Any

from services.request_activity import request_activity
from utils.log import logger


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(str(os.getenv(name) or "").strip() or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(str(os.getenv(name) or "").strip() or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _rss_kb() -> int:
    try:
        for line in open("/proc/self/status", "r", encoding="utf-8", errors="replace"):
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except Exception:
        return 0
    return 0


def _has_register_thread() -> bool:
    return any(thread.is_alive() and thread.name == "openai-register" for thread in threading.enumerate())


def _runtime_inflight_total() -> int:
    try:
        from services.runtime_state import runtime_state

        return int(runtime_state.image_inflight_total())
    except Exception:
        return -1


def _active_image_task_threads() -> int:
    prefixes = ("image-task-", "image-resume-")
    return sum(1 for thread in threading.enumerate() if thread.is_alive() and thread.name.startswith(prefixes))


def _unfinished_image_tasks() -> int:
    try:
        from services.config import config
        from services.image_task_service import UNFINISHED_STATUSES, _file_lock, _task_activity_ts, image_task_service

        try:
            poll_timeout_secs = int(config.image_poll_timeout_secs)
        except Exception:
            poll_timeout_secs = 120
        stale_cutoff = time.time() - max(300, poll_timeout_secs * 2)
        with image_task_service._lock:
            with _file_lock(image_task_service.path):
                tasks = list(image_task_service._load_locked().values())
        total = 0
        for task in tasks:
            if str(task.get("status") or "") not in UNFINISHED_STATUSES:
                continue
            activity_ts = _task_activity_ts(task)
            if not activity_ts or activity_ts > stale_cutoff:
                total += 1
        return total
    except Exception:
        return -1


def _should_recycle(*, threshold_kb: int, idle_secs_required: float, min_age_secs: float, started_at: float) -> tuple[bool, dict[str, Any]]:
    rss_kb = _rss_kb()
    activity = request_activity.snapshot()
    inflight = _runtime_inflight_total()
    image_task_threads = _active_image_task_threads()
    unfinished_image_tasks = _unfinished_image_tasks()
    register_running = _has_register_thread()
    age_secs = max(0.0, time.monotonic() - started_at)
    detail: dict[str, Any] = {
        "pid": os.getpid(),
        "rss_kb": rss_kb,
        "threshold_kb": threshold_kb,
        "request_active": activity.get("active"),
        "request_idle_secs": round(float(activity.get("idle_secs") or 0.0), 3),
        "image_inflight_total": inflight,
        "image_task_threads": image_task_threads,
        "unfinished_image_tasks": unfinished_image_tasks,
        "register_running": register_running,
        "age_secs": round(age_secs, 3),
    }
    if rss_kb < threshold_kb:
        return False, detail
    if age_secs < min_age_secs:
        return False, detail
    if int(activity.get("active") or 0) > 0:
        return False, detail
    if float(activity.get("idle_secs") or 0.0) < idle_secs_required:
        return False, detail
    if inflight != 0:
        return False, detail
    if image_task_threads != 0:
        return False, detail
    if unfinished_image_tasks != 0:
        return False, detail
    if register_running:
        return False, detail
    return True, detail


def start_memory_recycle_scheduler(stop_event: threading.Event) -> threading.Thread:
    if not _env_bool("APP_MEMORY_RECYCLE_ENABLED", False):
        thread = threading.Thread(target=lambda: None, daemon=True, name="memory-recycle-disabled")
        thread.start()
        return thread

    threshold_mb = _env_int("APP_MEMORY_RECYCLE_RSS_MB", 1024, 128)
    threshold_kb = threshold_mb * 1024
    idle_secs_required = _env_float("APP_MEMORY_RECYCLE_IDLE_SECS", 300.0, 5.0)
    interval_secs = _env_float("APP_MEMORY_RECYCLE_INTERVAL_SECS", 30.0, 5.0)
    min_age_secs = _env_float("APP_MEMORY_RECYCLE_MIN_AGE_SECS", 300.0, 0.0)
    started_at = time.monotonic()

    def _worker() -> None:
        while not stop_event.wait(interval_secs):
            should_recycle, detail = _should_recycle(
                threshold_kb=threshold_kb,
                idle_secs_required=idle_secs_required,
                min_age_secs=min_age_secs,
                started_at=started_at,
            )
            if not should_recycle:
                logger.debug({"event": "memory_recycle_skip", **detail})
                continue
            logger.warning({"event": "memory_recycle_worker_exit", **detail})
            os.kill(os.getpid(), signal.SIGTERM)
            return

    thread = threading.Thread(target=_worker, daemon=True, name="memory-recycle")
    thread.start()
    return thread
