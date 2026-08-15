from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from services.config import DATA_DIR


ICLOUD_DOMAIN = "@icloud.com"
ICLOUD_STATS_FILE = DATA_DIR / "icloud_stats.json"
GENERATION_MILESTONE = 40
DEFAULT_STATS = {
    "version": 2,
    "baseline_initialized": False,
    "registered_success_total": 0,
    "deleted_accounts": 0,
    "deleted_images": 0,
    "deleted_over_25_accounts": 0,
    "deleted_over_40_accounts": 0,
    "deleted_429_errors": 0,
    "initialized_at": "",
    "updated_at": "",
}


def is_icloud_account(account: dict[str, Any]) -> bool:
    return str(account.get("email") or "").strip().lower().endswith(ICLOUD_DOMAIN)


def _counter(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        try:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class ICloudStatsService:
    """Small file-backed counters kept off the business database hot path."""

    def __init__(self, path: Path = ICLOUD_STATS_FILE) -> None:
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self._thread_lock = threading.Lock()

    def _load_unlocked(self) -> dict[str, Any]:
        raw: dict[str, Any] = {}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                raw = loaded if isinstance(loaded, dict) else {}
            except (OSError, json.JSONDecodeError):
                raw = {}
        stats = dict(DEFAULT_STATS)
        stats.update(raw)
        for key in (
            "registered_success_total",
            "deleted_accounts",
            "deleted_images",
            "deleted_over_25_accounts",
            "deleted_over_40_accounts",
            "deleted_429_errors",
        ):
            stats[key] = _counter(stats.get(key))
        stats["baseline_initialized"] = bool(stats.get("baseline_initialized"))
        stats["version"] = 2
        return stats

    def _save_unlocked(self, stats: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        temp_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(self.path)

    @contextmanager
    def _locked(self):
        with self._thread_lock:
            with _file_lock(self.lock_path):
                yield

    def ensure_baseline(self, accounts: Iterable[dict[str, Any]]) -> None:
        current = [account for account in accounts if is_icloud_account(account)]
        with self._locked():
            stats = self._load_unlocked()
            if stats["baseline_initialized"]:
                return
            now = _now()
            stats["baseline_initialized"] = True
            stats["registered_success_total"] = len(current)
            stats["initialized_at"] = now
            stats["updated_at"] = now
            self._save_unlocked(stats)

    def record_registered(self, accounts: Iterable[dict[str, Any]]) -> None:
        count = sum(1 for account in accounts if is_icloud_account(account))
        if count <= 0:
            return
        with self._locked():
            stats = self._load_unlocked()
            stats["registered_success_total"] += count
            stats["updated_at"] = _now()
            self._save_unlocked(stats)

    def record_deleted(self, accounts: Iterable[dict[str, Any]]) -> None:
        deleted = [account for account in accounts if is_icloud_account(account)]
        if not deleted:
            return
        with self._locked():
            stats = self._load_unlocked()
            stats["deleted_accounts"] += len(deleted)
            stats["deleted_images"] += sum(_counter(account.get("success")) for account in deleted)
            stats["deleted_over_40_accounts"] += sum(
                1 for account in deleted if _counter(account.get("success")) > GENERATION_MILESTONE
            )
            stats["deleted_429_errors"] += sum(
                _counter(account.get("rate_limit_429")) for account in deleted
            )
            stats["updated_at"] = _now()
            self._save_unlocked(stats)

    def snapshot(self, accounts: Iterable[dict[str, Any]]) -> dict[str, Any]:
        current = [account for account in accounts if is_icloud_account(account)]
        normal_or_limited = [
            account for account in current if account.get("status") in {"正常", "限流"}
        ]
        with self._locked():
            stats = self._load_unlocked()

        current_429 = sum(_counter(account.get("rate_limit_429")) for account in current)
        current_over_40 = sum(
            1 for account in current if _counter(account.get("success")) > GENERATION_MILESTONE
        )
        return {
            "domain": ICLOUD_DOMAIN,
            "registered_success_total": stats["registered_success_total"],
            "current_accounts": len(normal_or_limited),
            "current_images": sum(_counter(account.get("success")) for account in normal_or_limited),
            "deleted_accounts": stats["deleted_accounts"],
            "deleted_images": stats["deleted_images"],
            "over_40_accounts": current_over_40 + stats["deleted_over_40_accounts"],
            "rate_limit_429_errors": current_429 + stats["deleted_429_errors"],
            "current_429_errors": current_429,
            "deleted_429_errors": stats["deleted_429_errors"],
            "initialized_at": stats.get("initialized_at") or "",
            "updated_at": stats.get("updated_at") or "",
        }


icloud_stats_service = ICloudStatsService()
