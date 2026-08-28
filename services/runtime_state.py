from __future__ import annotations

import json
import os
import time
import uuid
from threading import RLock
from typing import Any

from utils.log import logger


def _clean(value: object) -> str:
    return str(value or "").strip()


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: object) -> object:
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        return json.loads(str(value or "{}"))
    except Exception:
        return {}


def is_multi_worker_runtime() -> bool:
    try:
        return int(str(os.getenv("UVICORN_WORKERS") or "1").strip()) > 1
    except (TypeError, ValueError):
        return False


class RuntimeState:
    _ACQUIRE_IMAGE_SLOT_SCRIPT = """
local rr_key = KEYS[1]
local ttl = tonumber(ARGV[1])
local max_slots = tonumber(ARGV[2])
local n = #ARGV - 2
if n <= 0 then
  return {"", ""}
end
local start = redis.call("INCR", rr_key)
local function claim(token)
  local inflight_key = "account:image:inflight:" .. token
  local next_count = redis.call("INCR", inflight_key)
  redis.call("EXPIRE", inflight_key, ttl)
  if next_count <= max_slots then
    local lease_id = redis.call("INCR", "account:image:lease:seq")
    local lease_key = "account:image:lease:" .. lease_id
    redis.call("SET", lease_key, token, "EX", ttl)
    return {token, lease_key}
  end
  redis.call("DECR", inflight_key)
  return nil
end
-- Prefer completely idle accounts so max_concurrency=2 does not create
-- unnecessary duplicate account usage while idle candidates exist.
for i = 0, n - 1 do
  local idx = ((start + i - 1) % n) + 1
  local token = ARGV[idx + 2]
  local inflight_key = "account:image:inflight:" .. token
  if tonumber(redis.call("GET", inflight_key) or "0") == 0 then
    local result = claim(token)
    if result then
      return result
    end
  end
end
for i = 0, n - 1 do
  local idx = ((start + i - 1) % n) + 1
  local token = ARGV[idx + 2]
  local inflight_key = "account:image:inflight:" .. token
  local current = tonumber(redis.call("GET", inflight_key) or "0")
  if current > 0 and current < max_slots then
    local result = claim(token)
    if result then
      return result
    end
  end
end
return {"", ""}
"""

    _RELEASE_IMAGE_SLOT_SCRIPT = """
local token = ARGV[1]
if token == "" then
  return 0
end
local inflight_key = "account:image:inflight:" .. token
local current = tonumber(redis.call("GET", inflight_key) or "0")
if current <= 1 then
  redis.call("DEL", inflight_key)
  return 0
end
return redis.call("DECR", inflight_key)
"""

    def __init__(self) -> None:
        self.redis_url = _clean(os.getenv("REDIS_URL"))
        self._redis = None
        self._redis_error = ""
        self._lock = RLock()
        self._memory_inflight: dict[str, tuple[int, float]] = {}
        self._memory_aliases: dict[str, tuple[str, float]] = {}
        self._memory_progress: dict[str, tuple[dict[str, Any], float]] = {}
        self._memory_locks: dict[str, tuple[str, float]] = {}
        self._memory_rr: dict[str, int] = {}
        if self.redis_url:
            try:
                from redis import Redis

                self._redis = Redis.from_url(self.redis_url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
            except Exception as exc:
                self._redis = None
                self._redis_error = str(exc)

    @property
    def redis_enabled(self) -> bool:
        return self._redis is not None

    def health_check(self) -> dict[str, Any]:
        if self._redis is None:
            return {
                "status": "disabled" if not self.redis_url else "unavailable",
                "backend": "memory",
                "redis_url": self._mask_redis_url(self.redis_url),
                "error": self._redis_error or None,
            }
        try:
            self._redis.ping()
            return {
                "status": "healthy",
                "backend": "redis",
                "redis_url": self._mask_redis_url(self.redis_url),
            }
        except Exception as exc:
            return {
                "status": "unhealthy",
                "backend": "redis",
                "redis_url": self._mask_redis_url(self.redis_url),
                "error": str(exc),
            }

    def get_alias(self, old_token: str) -> str:
        token = _clean(old_token)
        if not token:
            return ""
        if self._redis is not None:
            try:
                return _clean(self._redis.get(f"account:alias:{token}"))
            except Exception:
                return ""
        with self._lock:
            self._cleanup_memory_locked()
            item = self._memory_aliases.get(token)
            return item[0] if item else ""

    def set_alias(self, old_token: str, new_token: str, ttl_seconds: int = 86400) -> None:
        old = _clean(old_token)
        new = _clean(new_token)
        if not old or not new:
            return
        if self._redis is not None:
            try:
                self._redis.set(f"account:alias:{old}", new, ex=max(60, int(ttl_seconds)))
                return
            except Exception:
                pass
        with self._lock:
            self._memory_aliases[old] = (new, time.time() + max(60, int(ttl_seconds)))

    def delete_aliases_for(self, tokens: set[str]) -> None:
        cleaned = {_clean(token) for token in tokens if _clean(token)}
        if not cleaned:
            return
        if self._redis is not None:
            try:
                keys = []
                for token in cleaned:
                    keys.append(f"account:alias:{token}")
                for key in list(self._redis.scan_iter("account:alias:*")):
                    value = _clean(self._redis.get(key))
                    if value in cleaned:
                        keys.append(str(key))
                if keys:
                    self._redis.delete(*list(dict.fromkeys(keys)))
                return
            except Exception:
                pass
        with self._lock:
            self._memory_aliases = {
                old: item
                for old, item in self._memory_aliases.items()
                if old not in cleaned and item[0] not in cleaned
            }

    def acquire_image_slot(
        self,
        tokens: list[str],
        max_concurrency: int,
        ttl_seconds: int,
        probe_limit: int = 128,
    ) -> str:
        candidates = [_clean(token) for token in tokens if _clean(token)]
        if not candidates:
            return ""
        max_slots = max(1, int(max_concurrency or 1))
        ttl = max(30, int(ttl_seconds or 180))
        try:
            requested_limit = int(probe_limit)
        except (TypeError, ValueError):
            requested_limit = 128
        # A non-positive value is an explicit rollback switch for the bounded
        # probe and restores the legacy full candidate window.
        limit = len(candidates) if requested_limit <= 0 else max(1, requested_limit)
        if len(candidates) > limit:
            with self._lock:
                total = len(candidates)
                start = self._memory_rr.get("image_candidates", 0) % total
                candidates = [candidates[(start + offset) % total] for offset in range(limit)]
                self._memory_rr["image_candidates"] = (start + limit) % total
        if self._redis is not None:
            eval_started_at = time.monotonic()
            try:
                result = self._redis.eval(self._ACQUIRE_IMAGE_SLOT_SCRIPT, 1, "account:rr:image", ttl, max_slots, *candidates)
                logger.debug({
                    "event": "image_slot_redis_eval",
                    "candidate_count": len(candidates),
                    "probe_limit": limit,
                    "eval_ms": round((time.monotonic() - eval_started_at) * 1000.0, 1),
                })
                if isinstance(result, list) and result:
                    return _clean(result[0])
                return ""
            except Exception as exc:
                logger.debug({
                    "event": "image_slot_redis_eval_failed",
                    "candidate_count": len(candidates),
                    "probe_limit": limit,
                    "eval_ms": round((time.monotonic() - eval_started_at) * 1000.0, 1),
                    "error": str(exc)[:200],
                })
                pass
        with self._lock:
            self._cleanup_memory_locked()
            start = self._memory_rr.get("image", 0) % len(candidates)
            n = len(candidates)
            ordered = [candidates[(start + offset) % n] for offset in range(n)]
            for token in ordered:
                current, _expires_at = self._memory_inflight.get(token, (0, 0.0))
                if current == 0:
                    self._memory_inflight[token] = (1, time.time() + ttl)
                    self._memory_rr["image"] = (start + candidates.index(token) + 1) % n
                    return token
            for token in ordered:
                current, _expires_at = self._memory_inflight.get(token, (0, 0.0))
                if 0 < current < max_slots:
                    self._memory_inflight[token] = (current + 1, time.time() + ttl)
                    self._memory_rr["image"] = (start + candidates.index(token) + 1) % n
                    return token
            return ""

    def release_image_slot(self, access_token: str) -> None:
        token = _clean(access_token)
        if not token:
            return
        if self._redis is not None:
            try:
                self._redis.eval(self._RELEASE_IMAGE_SLOT_SCRIPT, 0, token)
                return
            except Exception:
                pass
        with self._lock:
            self._cleanup_memory_locked()
            current, expires_at = self._memory_inflight.get(token, (0, 0.0))
            if current <= 1:
                self._memory_inflight.pop(token, None)
            else:
                self._memory_inflight[token] = (current - 1, expires_at)

    def transfer_image_slot(self, old_token: str, new_token: str, ttl_seconds: int) -> None:
        old = _clean(old_token)
        new = _clean(new_token)
        if not old or not new or old == new:
            return
        ttl = max(30, int(ttl_seconds or 180))
        if self._redis is not None:
            try:
                script = """
local old_token = ARGV[1]
local new_token = ARGV[2]
local ttl = tonumber(ARGV[3])
local old_key = "account:image:inflight:" .. old_token
local new_key = "account:image:inflight:" .. new_token
local current = tonumber(redis.call("GET", old_key) or "0")
if current <= 0 then
  return 0
end
redis.call("DEL", old_key)
redis.call("INCRBY", new_key, current)
redis.call("EXPIRE", new_key, ttl)
return current
"""
                self._redis.eval(script, 0, old, new, ttl)
                return
            except Exception:
                pass
        with self._lock:
            self._cleanup_memory_locked()
            current, _old_expires_at = self._memory_inflight.get(old, (0, 0.0))
            if current <= 0:
                return
            self._memory_inflight.pop(old, None)
            new_current, _new_expires_at = self._memory_inflight.get(new, (0, 0.0))
            self._memory_inflight[new] = (new_current + current, time.time() + ttl)

    def clear_image_slots(self, tokens: set[str] | list[str]) -> None:
        cleaned = [_clean(token) for token in tokens if _clean(token)]
        if not cleaned:
            return
        if self._redis is not None:
            try:
                self._redis.delete(*(f"account:image:inflight:{token}" for token in cleaned))
                return
            except Exception:
                pass
        with self._lock:
            for token in cleaned:
                self._memory_inflight.pop(token, None)

    def get_image_inflight(self, access_token: str) -> int:
        token = _clean(access_token)
        if not token:
            return 0
        if self._redis is not None:
            try:
                return max(0, int(self._redis.get(f"account:image:inflight:{token}") or 0))
            except Exception:
                return 0
        with self._lock:
            self._cleanup_memory_locked()
            return int(self._memory_inflight.get(token, (0, 0.0))[0])

    def image_inflight_snapshot(self, tokens: list[str]) -> dict[str, int]:
        cleaned = [_clean(token) for token in tokens if _clean(token)]
        if not cleaned:
            return {}
        if self._redis is not None:
            try:
                values = self._redis.mget([f"account:image:inflight:{token}" for token in cleaned])
                return {
                    token: max(0, int(value or 0))
                    for token, value in zip(cleaned, values)
                }
            except Exception:
                pass
        with self._lock:
            self._cleanup_memory_locked()
            return {token: int(self._memory_inflight.get(token, (0, 0.0))[0]) for token in cleaned}

    def image_inflight_total(self) -> int:
        if self._redis is not None:
            try:
                total = 0
                for key in self._redis.scan_iter("account:image:inflight:*", count=1000):
                    total += max(0, int(self._redis.get(key) or 0))
                return total
            except Exception:
                return 0
        with self._lock:
            self._cleanup_memory_locked()
            return sum(max(0, int(item[0])) for item in self._memory_inflight.values())

    def next_text_index(self) -> int:
        if self._redis is not None:
            try:
                return int(self._redis.incr("account:rr:text"))
            except Exception:
                pass
        with self._lock:
            value = self._memory_rr.get("text", 0) + 1
            self._memory_rr["text"] = value
            return value

    def set_flag(self, key: str, value: str = "1", ttl_seconds: int = 3600) -> None:
        flag_key = _clean(key)
        if not flag_key:
            return
        ttl = max(5, int(ttl_seconds or 3600))
        if self._redis is not None:
            try:
                self._redis.set(flag_key, _clean(value) or "1", ex=ttl)
                return
            except Exception:
                pass
        with self._lock:
            self._memory_progress[flag_key] = ({"value": _clean(value) or "1"}, time.time() + ttl)

    def get_flag(self, key: str) -> str:
        flag_key = _clean(key)
        if not flag_key:
            return ""
        if self._redis is not None:
            try:
                return _clean(self._redis.get(flag_key))
            except Exception:
                return ""
        with self._lock:
            self._cleanup_memory_locked()
            item = self._memory_progress.get(flag_key)
            if not item:
                return ""
            return _clean(item[0].get("value"))

    def delete_flag(self, key: str) -> None:
        flag_key = _clean(key)
        if not flag_key:
            return
        if self._redis is not None:
            try:
                self._redis.delete(flag_key)
                return
            except Exception:
                pass
        with self._lock:
            self._memory_progress.pop(flag_key, None)

    def set_progress(self, kind: str, progress_id: str, data: dict[str, Any], ttl_seconds: int = 3600) -> None:
        key = self._progress_key(kind, progress_id)
        if not key:
            return
        ttl = max(60, int(ttl_seconds or 3600))
        if self._redis is not None:
            try:
                self._redis.set(key, _json_dumps(data), ex=ttl)
                return
            except Exception:
                pass
        with self._lock:
            self._memory_progress[key] = (dict(data), time.time() + ttl)

    def get_progress(self, kind: str, progress_id: str) -> dict[str, Any] | None:
        key = self._progress_key(kind, progress_id)
        if not key:
            return None
        if self._redis is not None:
            try:
                data = _json_loads(self._redis.get(key))
                return data if isinstance(data, dict) else None
            except Exception:
                return None
        with self._lock:
            self._cleanup_memory_locked()
            item = self._memory_progress.get(key)
            return dict(item[0]) if item else None

    def update_progress(self, kind: str, progress_id: str, updater, ttl_seconds: int = 3600) -> dict[str, Any] | None:
        key = self._progress_key(kind, progress_id)
        ttl = max(60, int(ttl_seconds or 3600))
        if key and self._redis is not None:
            try:
                from redis import WatchError

                for _ in range(5):
                    with self._redis.pipeline() as pipe:
                        try:
                            pipe.watch(key)
                            current = _json_loads(pipe.get(key))
                            if not isinstance(current, dict) or not current:
                                pipe.unwatch()
                                return None
                            updated = updater(dict(current))
                            if not isinstance(updated, dict):
                                updated = current
                            pipe.multi()
                            pipe.set(key, _json_dumps(updated), ex=ttl)
                            pipe.execute()
                            return dict(updated)
                        except WatchError:
                            continue
                return self.get_progress(kind, progress_id)
            except Exception:
                pass
        with self._lock:
            current = self.get_progress(kind, progress_id) or {}
            if not current:
                return None
            updated = updater(dict(current))
            if not isinstance(updated, dict):
                updated = current
            self.set_progress(kind, progress_id, updated, ttl_seconds=ttl_seconds)
            return dict(updated)

    def delete_progress(self, kind: str, progress_id: str) -> None:
        key = self._progress_key(kind, progress_id)
        if not key:
            return
        if self._redis is not None:
            try:
                self._redis.delete(key)
                return
            except Exception:
                pass
        with self._lock:
            self._memory_progress.pop(key, None)

    def acquire_lock(
        self,
        key: str,
        ttl_seconds: int = 300,
        *,
        allow_memory_fallback: bool = True,
    ) -> str:
        lock_key = _clean(key)
        if not lock_key:
            return ""
        owner = uuid.uuid4().hex
        ttl = max(5, int(ttl_seconds or 300))
        if self._redis is not None:
            try:
                return owner if self._redis.set(lock_key, owner, nx=True, ex=ttl) else ""
            except Exception:
                if not allow_memory_fallback:
                    return ""
        elif not allow_memory_fallback:
            return ""
        with self._lock:
            self._cleanup_memory_locked()
            if lock_key in self._memory_locks:
                return ""
            self._memory_locks[lock_key] = (owner, time.time() + ttl)
            return owner

    def extend_lock(self, key: str, owner: str, ttl_seconds: int = 300) -> bool:
        lock_key = _clean(key)
        lock_owner = _clean(owner)
        if not lock_key or not lock_owner:
            return False
        ttl = max(5, int(ttl_seconds or 300))
        if self._redis is not None:
            try:
                script = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("EXPIRE", KEYS[1], ARGV[2])
end
return 0
"""
                return bool(self._redis.eval(script, 1, lock_key, lock_owner, ttl))
            except Exception:
                pass
        with self._lock:
            self._cleanup_memory_locked()
            item = self._memory_locks.get(lock_key)
            if item and item[0] == lock_owner:
                self._memory_locks[lock_key] = (lock_owner, time.time() + ttl)
                return True
            return False

    def release_lock(self, key: str, owner: str) -> None:
        lock_key = _clean(key)
        lock_owner = _clean(owner)
        if not lock_key or not lock_owner:
            return
        if self._redis is not None:
            try:
                script = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("DEL", KEYS[1])
end
return 0
"""
                self._redis.eval(script, 1, lock_key, lock_owner)
                return
            except Exception:
                pass
        with self._lock:
            item = self._memory_locks.get(lock_key)
            if item and item[0] == lock_owner:
                self._memory_locks.pop(lock_key, None)

    @staticmethod
    def _progress_key(kind: str, progress_id: str) -> str:
        normalized_kind = _clean(kind).lower()
        normalized_id = _clean(progress_id)
        if not normalized_kind or not normalized_id:
            return ""
        if normalized_kind in {"refresh", "relogin"}:
            return f"account:{normalized_kind}_progress:{normalized_id}"
        if normalized_kind in {"bulk_image_delete", "bulk_account_import", "bulk_account_refresh"}:
            return f"bulk:{normalized_kind}:progress:{normalized_id}"
        return ""

    def _cleanup_memory_locked(self) -> None:
        now = time.time()
        self._memory_inflight = {key: item for key, item in self._memory_inflight.items() if item[1] > now}
        self._memory_aliases = {key: item for key, item in self._memory_aliases.items() if item[1] > now}
        self._memory_progress = {key: item for key, item in self._memory_progress.items() if item[1] > now}
        self._memory_locks = {key: item for key, item in self._memory_locks.items() if item[1] > now}

    @staticmethod
    def _mask_redis_url(url: str) -> str:
        if not url or "://" not in url:
            return url
        try:
            protocol, rest = url.split("://", 1)
            if "@" in rest:
                credentials, host = rest.split("@", 1)
                if ":" in credentials:
                    username, _password = credentials.split(":", 1)
                    return f"{protocol}://{username}:****@{host}"
            return url
        except Exception:
            return url


runtime_state = RuntimeState()
