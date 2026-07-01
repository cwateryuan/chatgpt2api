from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator


class MaintenanceActivity:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, int] = {}
        self._last_started_at = 0.0
        self._last_finished_at = time.monotonic()

    def begin(self, kind: str) -> None:
        key = str(kind or "maintenance").strip() or "maintenance"
        with self._lock:
            self._active[key] = self._active.get(key, 0) + 1
            self._last_started_at = time.monotonic()

    def end(self, kind: str) -> None:
        key = str(kind or "maintenance").strip() or "maintenance"
        with self._lock:
            current = max(0, self._active.get(key, 0) - 1)
            if current:
                self._active[key] = current
            else:
                self._active.pop(key, None)
            self._last_finished_at = time.monotonic()

    @contextmanager
    def track(self, kind: str) -> Iterator[None]:
        self.begin(kind)
        try:
            yield
        finally:
            self.end(kind)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            active = dict(self._active)
            total = sum(active.values())
            now = time.monotonic()
            last_activity = max(self._last_started_at, self._last_finished_at)
            return {
                "active": total,
                "by_kind": active,
                "idle_secs": max(0.0, now - last_activity) if total == 0 else 0.0,
                "last_started_at": self._last_started_at,
                "last_finished_at": self._last_finished_at,
            }


maintenance_activity = MaintenanceActivity()
