from __future__ import annotations

import time


class ImageDeadlineExpired(TimeoutError):
    """Raised when the image request hard deadline is exhausted."""


class ImageRequestDeadline:
    def __init__(self, timeout_secs: float, *, started_at: float | None = None) -> None:
        self.timeout_secs = max(1.0, float(timeout_secs))
        self.started_at = float(started_at if started_at is not None else time.time())
        self.deadline_at = self.started_at + self.timeout_secs

    def remaining(self) -> float:
        return self.deadline_at - time.time()

    def require(self) -> float:
        remaining = self.remaining()
        if remaining <= 0:
            raise ImageDeadlineExpired(f"ChatGPT 生图超时（已等待 {self.timeout_secs:g} 秒）。")
        return remaining

    def request_timeout(self, default_secs: float) -> float:
        return max(0.001, min(float(default_secs), self.require()))

    def budget(self, default_secs: float) -> float:
        return min(float(default_secs), self.require())

    def sleep(self, seconds: float) -> None:
        sleep_for = min(float(seconds), self.require())
        if sleep_for > 0:
            time.sleep(sleep_for)
