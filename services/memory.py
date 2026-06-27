from __future__ import annotations

import ctypes
import gc
import os
import sys
import threading
import time

from utils.log import logger

_TRIM_LOCK = threading.Lock()
_LAST_TRIM_AT = 0.0


def _env_bool(name: str, default: bool = True) -> bool:
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


def trim_memory(reason: str = "", *, force: bool = False) -> bool:
    """Best-effort RSS trim after large transient image allocations.

    Python may free objects while glibc keeps arenas mapped in the process RSS.
    A rate-limited malloc_trim helps long-running workers return idle memory to
    the container without changing request semantics.
    """
    if not _env_bool("APP_MEMORY_TRIM_ENABLED", True):
        return False
    interval = _env_float("APP_MEMORY_TRIM_INTERVAL_SECS", 30.0, 0.0)
    now = time.monotonic()
    global _LAST_TRIM_AT
    with _TRIM_LOCK:
        if not force and interval > 0 and now - _LAST_TRIM_AT < interval:
            return False
        _LAST_TRIM_AT = now

    gc.collect()
    if not sys.platform.startswith("linux"):
        return True
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
        logger.debug({"event": "memory_trim", "reason": reason})
        return True
    except Exception as exc:
        logger.debug({"event": "memory_trim_failed", "reason": reason, "error": repr(exc)})
        return False


def start_memory_trim_scheduler(stop_event: threading.Event) -> threading.Thread:
    if not _env_bool("APP_MEMORY_TRIM_ENABLED", True):
        thread = threading.Thread(target=lambda: None, daemon=True, name="memory-trim-disabled")
        thread.start()
        return thread
    interval = _env_float("APP_MEMORY_TRIM_INTERVAL_SECS", 30.0, 1.0)

    def _worker() -> None:
        while not stop_event.wait(interval):
            trim_memory("idle_scheduler")

    thread = threading.Thread(target=_worker, daemon=True, name="memory-trim")
    thread.start()
    return thread
