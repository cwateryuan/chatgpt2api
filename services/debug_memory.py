from __future__ import annotations

import gc
import json
import os
import signal
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from types import FrameType
from typing import Any

from services.config import DATA_DIR
from services.memory import trim_memory
from utils.log import logger


_SIGNAL_LOCK = threading.Lock()
_SIGNAL_INSTALLED = False
_SCHEDULER_STARTED = False


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _env_float(name: str, default: float, minimum: float = 1.0) -> float:
    try:
        value = float(str(os.getenv(name) or "").strip() or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _diag_output_path() -> Path:
    raw = str(os.getenv("APP_MEMORY_DIAG_OUTPUT") or "").strip()
    return Path(raw) if raw else DATA_DIR / "memory_diagnostics.jsonl"


def _read_key_value_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return result
    for line in lines:
        key, sep, value = line.partition(":")
        if sep:
            result[key.strip()] = value.strip()
    return result


def _read_statm() -> dict[str, int]:
    path = Path("/proc/self/statm")
    try:
        parts = path.read_text(encoding="utf-8").split()
    except Exception:
        return {}
    names = ("size", "resident", "shared", "text", "lib", "data", "dt")
    page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
    result: dict[str, int] = {}
    for name, value in zip(names, parts):
        try:
            result[f"{name}_kb"] = int(value) * page_size // 1024
        except (TypeError, ValueError):
            continue
    return result


def _parse_kb(value: str) -> int | None:
    parts = value.split()
    if not parts:
        return None
    try:
        return int(parts[0])
    except (TypeError, ValueError):
        return None


def _proc_snapshot() -> dict[str, Any]:
    status = _read_key_value_file(Path("/proc/self/status"))
    smaps = _read_key_value_file(Path("/proc/self/smaps_rollup"))
    statm = _read_statm()
    wanted_status = (
        "Name",
        "State",
        "VmPeak",
        "VmSize",
        "VmHWM",
        "VmRSS",
        "VmData",
        "VmStk",
        "VmExe",
        "VmLib",
        "VmSwap",
        "Threads",
    )
    wanted_smaps = (
        "Rss",
        "Pss",
        "Pss_Dirty",
        "Shared_Clean",
        "Shared_Dirty",
        "Private_Clean",
        "Private_Dirty",
        "Referenced",
        "Anonymous",
        "LazyFree",
        "AnonHugePages",
        "Swap",
    )
    parsed_status = {key: status.get(key) for key in wanted_status if key in status}
    parsed_smaps = {key: smaps.get(key) for key in wanted_smaps if key in smaps}
    return {
        "status": parsed_status,
        "smaps_rollup": parsed_smaps,
        "statm": statm,
        "rss_kb": _parse_kb(status.get("VmRSS", "")),
        "hwm_kb": _parse_kb(status.get("VmHWM", "")),
        "data_kb": _parse_kb(status.get("VmData", "")),
        "threads": _safe_int(status.get("Threads")),
    }


def _safe_int(value: object) -> int | None:
    try:
        return int(str(value or "").split()[0])
    except (IndexError, TypeError, ValueError):
        return None


def _thread_snapshot() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for thread in threading.enumerate():
        items.append({
            "name": thread.name,
            "ident": thread.ident,
            "native_id": getattr(thread, "native_id", None),
            "daemon": thread.daemon,
            "alive": thread.is_alive(),
        })
    items.sort(key=lambda item: (str(item.get("name") or ""), int(item.get("ident") or 0)))
    return items


def _gc_snapshot(*, collect: bool = False, object_limit: int = 40) -> dict[str, Any]:
    collected = gc.collect() if collect else None
    objects = gc.get_objects()
    counts = Counter(type(obj).__name__ for obj in objects)
    top = [
        {"type": name, "count": count}
        for name, count in counts.most_common(max(1, int(object_limit)))
    ]
    return {
        "counts": gc.get_count(),
        "thresholds": gc.get_threshold(),
        "garbage_count": len(gc.garbage),
        "object_count": len(objects),
        "collected": collected,
        "top_types": top,
    }


def _safe_json_size(value: object, _seen: set[int] | None = None) -> int:
    if _seen is None:
        _seen = set()
    value_id = id(value)
    if value_id in _seen:
        return 0
    if isinstance(value, (dict, list, tuple, set)):
        _seen.add(value_id)
    if value is None:
        return 4
    if isinstance(value, bool):
        return 4 if value else 5
    if isinstance(value, (int, float)):
        return len(str(value))
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if isinstance(value, dict):
        total = 2
        for key, item in value.items():
            total += len(str(key)) + _safe_json_size(item, _seen) + 4
        return total
    if isinstance(value, (list, tuple, set)):
        total = 2
        for item in value:
            total += _safe_json_size(item, _seen) + 1
        return total
    return len(type(value).__name__)


def _contains_key(value: object, target_key: str) -> bool:
    if isinstance(value, dict):
        if target_key in value:
            return True
        return any(_contains_key(item, target_key) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_key(item, target_key) for item in value)
    return False


def _task_summary(task: dict[str, Any]) -> dict[str, Any]:
    data = task.get("data")
    return {
        "id": str(task.get("id") or "")[:80],
        "status": str(task.get("status") or ""),
        "mode": str(task.get("mode") or ""),
        "updated_at": str(task.get("updated_at") or ""),
        "progress": str(task.get("progress") or "")[:80],
        "data_chars": _safe_json_size(data) if data is not None else 0,
        "has_b64_json": _contains_key(data, "b64_json"),
        "error_chars": len(str(task.get("error") or "")),
    }


def _image_tasks_snapshot() -> dict[str, Any]:
    path = DATA_DIR / "image_tasks.json"
    file_size = None
    try:
        file_size = path.stat().st_size if path.exists() else 0
    except Exception:
        file_size = None

    try:
        from services.image_task_service import image_task_service

        lock = getattr(image_task_service, "_lock", None)
        if lock is None:
            tasks = dict(getattr(image_task_service, "_tasks", {}) or {})
        else:
            with lock:
                tasks = dict(getattr(image_task_service, "_tasks", {}) or {})
    except Exception as exc:
        return {
            "file_size_bytes": file_size,
            "error": repr(exc)[:300],
        }

    status_counts = Counter(str(task.get("status") or "unknown") for task in tasks.values())
    summaries = [_task_summary(task) for task in tasks.values()]
    summaries.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    data_sizes = [int(item["data_chars"]) for item in summaries]
    return {
        "file_size_bytes": file_size,
        "loaded_tasks": len(tasks),
        "status_counts": dict(status_counts),
        "b64_json_tasks": sum(1 for item in summaries if item.get("has_b64_json")),
        "max_data_chars": max(data_sizes) if data_sizes else 0,
        "recent": summaries[:20],
    }


def _runtime_snapshot() -> dict[str, Any]:
    try:
        from services.runtime_state import runtime_state

        return {
            "image_inflight_total": runtime_state.image_inflight_total(),
        }
    except Exception as exc:
        return {
            "image_inflight_total": None,
            "error": repr(exc)[:300],
        }


def build_memory_snapshot(*, reason: str = "scheduled", collect: bool = False) -> dict[str, Any]:
    now = time.time()
    return {
        "schema_version": 1,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "timestamp_epoch": now,
        "reason": str(reason or "scheduled")[:80],
        "pid": os.getpid(),
        "platform": sys.platform,
        "proc": _proc_snapshot(),
        "threads": _thread_snapshot(),
        "gc": _gc_snapshot(collect=collect),
        "image_tasks": _image_tasks_snapshot(),
        "runtime": _runtime_snapshot(),
    }


def write_memory_snapshot(*, reason: str = "scheduled", collect: bool = False, force_trim: bool = False) -> dict[str, Any]:
    if force_trim:
        trim_memory(f"memory_diag:{reason}", force=True)
    snapshot = build_memory_snapshot(reason=reason, collect=collect)
    output_path = _diag_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), default=str)
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    logger.info({
        "event": "memory_diagnostic_snapshot",
        "pid": snapshot.get("pid"),
        "reason": snapshot.get("reason"),
        "rss_kb": (snapshot.get("proc") or {}).get("rss_kb"),
        "threads": (snapshot.get("proc") or {}).get("threads"),
        "output": str(output_path),
    })
    return snapshot


def _handle_sigusr1(signum: int, _frame: FrameType | None) -> None:
    def _worker() -> None:
        try:
            write_memory_snapshot(reason=f"signal:{signum}", collect=True, force_trim=True)
        except Exception as exc:
            logger.warning({"event": "memory_diagnostic_signal_failed", "signal": signum, "error": repr(exc)})

    threading.Thread(target=_worker, daemon=True, name="memory-diagnostic-signal").start()


def _install_signal_handler() -> None:
    global _SIGNAL_INSTALLED
    if not hasattr(signal, "SIGUSR1"):
        return
    with _SIGNAL_LOCK:
        if _SIGNAL_INSTALLED:
            return
        try:
            signal.signal(signal.SIGUSR1, _handle_sigusr1)
            _SIGNAL_INSTALLED = True
        except Exception as exc:
            logger.warning({"event": "memory_diagnostic_signal_install_failed", "error": repr(exc)})


def start_memory_diagnostic_scheduler(stop_event: threading.Event) -> threading.Thread:
    global _SCHEDULER_STARTED
    enabled = _env_bool("APP_MEMORY_DIAG_ENABLED", False)
    _install_signal_handler()
    if not enabled:
        thread = threading.Thread(target=lambda: None, daemon=True, name="memory-diagnostic-disabled")
        thread.start()
        return thread

    interval = _env_float("APP_MEMORY_DIAG_INTERVAL_SECS", 60.0, 1.0)

    def _worker() -> None:
        write_memory_snapshot(reason="startup", collect=False)
        while not stop_event.wait(interval):
            try:
                write_memory_snapshot(reason="scheduled", collect=False)
            except Exception as exc:
                logger.warning({"event": "memory_diagnostic_snapshot_failed", "error": repr(exc)})

    _SCHEDULER_STARTED = True
    thread = threading.Thread(target=_worker, daemon=True, name="memory-diagnostic")
    thread.start()
    return thread
