from __future__ import annotations

import os


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
