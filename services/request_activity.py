from __future__ import annotations

import threading
import time
from typing import Any, Awaitable, Callable


class RequestActivity:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self._last_started_at = 0.0
        self._last_finished_at = time.monotonic()

    def begin(self) -> None:
        with self._lock:
            self._active += 1
            self._last_started_at = time.monotonic()

    def end(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)
            self._last_finished_at = time.monotonic()

    def snapshot(self) -> dict[str, float | int]:
        with self._lock:
            now = time.monotonic()
            last_activity = max(self._last_started_at, self._last_finished_at)
            return {
                "active": self._active,
                "idle_secs": max(0.0, now - last_activity) if self._active == 0 else 0.0,
                "last_started_at": self._last_started_at,
                "last_finished_at": self._last_finished_at,
            }


request_activity = RequestActivity()


class RequestActivityMiddleware:
    def __init__(self, app: Callable[..., Awaitable[Any]]) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Awaitable[Any]], send: Callable[..., Awaitable[Any]]) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request_activity.begin()
        finished = False

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal finished
            await send(message)
            if message.get("type") == "http.response.body" and not message.get("more_body", False):
                if not finished:
                    finished = True
                    request_activity.end()

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if not finished:
                request_activity.end()
