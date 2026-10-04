"""Small, bounded scheduling indexes; never duplicate complete account records."""
from __future__ import annotations

import hashlib
from bisect import bisect_right, insort
from heapq import merge
from itertools import islice
from threading import RLock


class ImageSchedulingUnavailable(RuntimeError):
    def __init__(self, message: str, retry_after: int = 1):
        super().__init__(message)
        self.retry_after = max(1, retry_after)


def cooldown_until(account: dict) -> float:
    try:
        value = float(account.get("image_cooldown_until") or 0)
        return value if 0 < value < float("inf") else 0.0
    except (TypeError, ValueError):
        return 0.0


def cooldown_started_at(account: dict) -> float:
    try:
        value = float(account.get("image_cooldown_started_at") or 0)
        return value if 0 < value < float("inf") else 0.0
    except (TypeError, ValueError):
        return 0.0


def public_account(account: dict) -> dict:
    return {
        key: value
        for key, value in account.items()
        if key not in {"image_cooldown_until", "image_cooldown_started_at"}
    }


class MemoryImageCandidateIndex:
    """Sorted compact references for file backends, updated only on account changes.

    List insertions move references in C; reads and range counts use binary search.
    There are no stale heap entries, background timers, or per-request pool copies.
    """

    def __init__(self):
        self._lock = RLock()
        self._groups: dict[tuple[str, str], list[tuple[float, str]]] = {}
        self._entries: dict[str, tuple[tuple[str, str], tuple[float, str], str]] = {}

    def update(self, account: dict, available: bool, *, plan_type: str | None = None) -> None:
        token = str(account.get("access_token") or "")
        key = hashlib.sha256(token.encode()).hexdigest()
        group = (str(account.get("source_type") or "web").lower(), str(plan_type or account.get("type") or "free").lower())
        entry = (group, (cooldown_until(account), key), token)
        with self._lock:
            if self._entries.get(key) == entry and available:
                return
            self.remove(token)
            if available:
                insort(self._groups.setdefault(group, []), entry[1])
                self._entries[key] = entry

    def remove(self, token: str) -> None:
        key = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            old = self._entries.pop(key, None)
            if old:
                items = self._groups[old[0]]
                items.pop(bisect_right(items, old[1]) - 1)
                if not items:
                    del self._groups[old[0]]

    def page(self, now: float, after: tuple[float, str], limit: int = 128,
             source_type: str = "", plan_type: str = "", plan_types: tuple[str, ...] = ()) -> list[dict]:
        with self._lock:
            windows = []
            for (source, plan), items in self._groups.items():
                if source_type and source != source_type:
                    continue
                if plan_type and plan != plan_type or plan_types and plan not in plan_types:
                    continue
                start = bisect_right(items, after)
                stop = min(start + limit, bisect_right(items, (now, "f" * 64)))
                windows.append(items[start:stop])
            return [{"access_token": self._entries[key][2], "image_cooldown_until": until,
                     "cursor_hash": key} for until, key in islice(merge(*windows), limit)]

    def metrics(self, now: float, cooldown_minutes: int = 60) -> dict:
        if cooldown_minutes <= 0:
            return {"cooling_accounts": 0, "thawing_within_hour": 0,
                    "next_thaw_at": None, "cooldown_minutes": 0, "as_of": now}
        cooling = thawing = 0
        next_thaw = None
        with self._lock:
            for items in self._groups.values():
                start = bisect_right(items, (now, "f" * 64))
                stop = bisect_right(items, (now + cooldown_minutes * 60, "f" * 64))
                cooling += len(items) - start
                thawing += stop - start
                if start < len(items):
                    until = items[start][0]
                    next_thaw = until if next_thaw is None else min(next_thaw, until)
        return {"cooling_accounts": cooling, "thawing_within_hour": thawing,
                "next_thaw_at": next_thaw, "cooldown_minutes": cooldown_minutes, "as_of": now}
