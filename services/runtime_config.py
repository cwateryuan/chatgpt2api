from __future__ import annotations

import os
import threading


def configure_thread_stack_size(default_kb: int = 0) -> int | None:
    """Configure stack size for subsequently created Python threads.

    Leave unchanged by default. Set APP_THREAD_STACK_SIZE_KB=1024 or 2048 on
    memory-sensitive multi-worker deployments.
    """
    raw = str(os.getenv("APP_THREAD_STACK_SIZE_KB") or "").strip()
    if not raw:
        size_kb = default_kb
    else:
        try:
            size_kb = int(raw)
        except (TypeError, ValueError):
            size_kb = default_kb
    if size_kb <= 0:
        return None
    size_bytes = max(64 * 1024, size_kb * 1024)
    try:
        threading.stack_size(size_bytes)
        print(f"[runtime] Python thread stack size={size_bytes} bytes", flush=True)
        return size_bytes
    except (RuntimeError, ValueError) as exc:
        print(f"[runtime] failed to configure Python thread stack size: {exc}", flush=True)
        return None


def configure_threadpool_tokens(default: int = 100) -> int | None:
    """Set AnyIO's default thread limiter for blocking FastAPI work."""
    raw = str(os.getenv("APP_THREADPOOL_TOKENS") or "").strip()
    if not raw:
        tokens = default
    else:
        try:
            tokens = int(raw)
        except (TypeError, ValueError):
            tokens = default
    tokens = max(1, tokens)
    try:
        import anyio.to_thread

        limiter = anyio.to_thread.current_default_thread_limiter()
        limiter.total_tokens = tokens
        print(f"[runtime] AnyIO threadpool tokens={tokens}", flush=True)
        return tokens
    except Exception as exc:
        print(f"[runtime] failed to configure AnyIO threadpool tokens: {exc}", flush=True)
        return None
