from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.config import DATA_DIR
from services.runtime_state import is_multi_worker_runtime, runtime_state


MAIL_HEALTH_CONFIG_KEY = "register_mail_health"
MAIL_HEALTH_LOCK_KEY = "lock:register:mail_health"
MAIL_HEALTH_FILE = DATA_DIR / "mail_provider_health.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MailHealthStore:
    """Persistent, process-safe health state for registration mail providers."""

    def __init__(self, store_file: Path | None = None):
        self.store_file = store_file or MAIL_HEALTH_FILE
        self._lock = threading.RLock()

    @staticmethod
    def _database_backend():
        try:
            from services.config import config

            backend = config.get_storage_backend()
            return backend if backend.supports_database_features() is True else None
        except Exception:
            return None

    def _load_unlocked(self) -> dict[str, Any]:
        backend = self._database_backend()
        if backend is not None:
            value = backend.load_named_config(MAIL_HEALTH_CONFIG_KEY)
            return value if isinstance(value, dict) else {}
        try:
            value = json.loads(self.store_file.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _save_unlocked(self, state: dict[str, Any]) -> None:
        backend = self._database_backend()
        if backend is not None:
            backend.save_named_config(MAIL_HEALTH_CONFIG_KEY, state)
            return
        self.store_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.store_file.with_suffix(f"{self.store_file.suffix}.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.store_file)

    def load(self) -> dict[str, Any]:
        with self._lock:
            return self._load_unlocked()

    def _mutate(self, updater: Callable[[dict[str, Any]], Any]) -> Any:
        with self._lock:
            owner = ""
            deadline = time.monotonic() + 5
            while not owner and time.monotonic() < deadline:
                owner = runtime_state.acquire_lock(
                    MAIL_HEALTH_LOCK_KEY,
                    ttl_seconds=15,
                    allow_memory_fallback=not is_multi_worker_runtime(),
                )
                if not owner:
                    time.sleep(0.05)
            if not owner:
                raise RuntimeError("mail health state lock unavailable")
            try:
                state = self._load_unlocked()
                state.setdefault("version", 1)
                state.setdefault("providers", {})
                result = updater(state)
                self._save_unlocked(state)
                return result
            finally:
                if owner:
                    runtime_state.release_lock(MAIL_HEALTH_LOCK_KEY, owner)

    @staticmethod
    def _provider(state: dict[str, Any], provider_id: str) -> dict[str, Any]:
        providers = state.setdefault("providers", {})
        provider = providers.setdefault(provider_id, {})
        provider.setdefault("disabled", False)
        provider.setdefault("consecutive_failures", 0)
        provider.setdefault("domains", {})
        return provider

    def record_result(
        self,
        provider_id: str,
        *,
        success: bool,
        threshold: int,
        error: str = "",
        domain: str = "",
        domains: list[str] | None = None,
    ) -> dict[str, Any]:
        provider_id = str(provider_id or "").strip()
        if not provider_id:
            return {}
        threshold = max(1, int(threshold or 1))
        clean_domain = str(domain or "").strip().lower()
        configured_domains = [str(item).strip().lower() for item in (domains or []) if str(item).strip()]

        def update(state: dict[str, Any]) -> dict[str, Any]:
            provider = self._provider(state, provider_id)
            now = _now()
            if clean_domain:
                domain_state = provider["domains"].setdefault(clean_domain, {})
                domain_state.setdefault("disabled", False)
                domain_state.setdefault("consecutive_failures", 0)
                if success:
                    if not domain_state["disabled"]:
                        domain_state["consecutive_failures"] = 0
                        domain_state["last_error"] = ""
                else:
                    domain_state["consecutive_failures"] = int(domain_state.get("consecutive_failures") or 0) + 1
                    domain_state["last_error"] = str(error or "")[:500]
                    if domain_state["consecutive_failures"] >= threshold:
                        domain_state["disabled"] = True
                        domain_state.setdefault("disabled_at", now)
                domain_state["updated_at"] = now
                for item in configured_domains:
                    provider["domains"].setdefault(item, {"disabled": False, "consecutive_failures": 0})
                if configured_domains:
                    provider["disabled"] = all(
                        bool(provider["domains"].get(item, {}).get("disabled")) for item in configured_domains
                    )
                    if provider["disabled"]:
                        provider.setdefault("disabled_at", now)
                    else:
                        provider.pop("disabled_at", None)
            elif success:
                if not provider["disabled"]:
                    provider["consecutive_failures"] = 0
                    provider["last_error"] = ""
            else:
                provider["consecutive_failures"] = int(provider.get("consecutive_failures") or 0) + 1
                provider["last_error"] = str(error or "")[:500]
                if provider["consecutive_failures"] >= threshold:
                    provider["disabled"] = True
                    provider.setdefault("disabled_at", now)
            provider["updated_at"] = now
            return json.loads(json.dumps(provider, ensure_ascii=False))

        return self._mutate(update)

    def reset(self, provider_id: str = "", domain: str = "") -> int:
        provider_id = str(provider_id or "").strip()
        domain = str(domain or "").strip().lower()

        def update(state: dict[str, Any]) -> int:
            providers = state.setdefault("providers", {})
            if not provider_id:
                count = len(providers)
                providers.clear()
                return count
            provider = providers.get(provider_id)
            if not isinstance(provider, dict):
                return 0
            if not domain:
                providers.pop(provider_id, None)
                return 1
            domains = provider.get("domains") if isinstance(provider.get("domains"), dict) else {}
            if domain not in domains:
                return 0
            domains.pop(domain, None)
            provider["domains"] = domains
            provider["disabled"] = False
            provider.pop("disabled_at", None)
            provider["updated_at"] = _now()
            return 1

        return int(self._mutate(update) or 0)

    def provider_state(self, provider_id: str) -> dict[str, Any]:
        state = self.load()
        provider = state.get("providers", {}).get(str(provider_id or ""), {})
        return json.loads(json.dumps(provider, ensure_ascii=False)) if isinstance(provider, dict) else {}


mail_health_store = MailHealthStore()
