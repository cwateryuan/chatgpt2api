from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, TimeoutError as FutureTimeoutError, wait
from datetime import datetime, timezone
from pathlib import Path

from services.account_service import account_service
from services.config import DATA_DIR
from services.memory import trim_memory
from services.register import browser_register, mail_provider, openai_register
from services.runtime_state import is_multi_worker_runtime, runtime_state
from utils.log import logger


REGISTER_FILE = DATA_DIR / "register.json"
REGISTER_RUN_LOCK = "lock:register:run"
REGISTER_RUN_LOCK_TTL_SECONDS = 120
REGISTER_STOP_KEY = "register:stop_requested"
REGISTER_STOP_TTL_SECONDS = 86400
REGISTER_LOG_LIMIT = 120
REGISTER_SAVE_INTERVAL_SECONDS = 5.0
REGISTER_LOCK_REFRESH_INTERVAL_SECONDS = 30.0
REGISTER_SUPERVISOR_INTERVAL_SECONDS = 10.0
REGISTER_SUPERVISOR_WAIT_LOG_INTERVAL_SECONDS = 30.0
REGISTER_FULL_REFRESH_BATCH_SIZE = 50
REGISTER_RUNTIME_KEYS = {"enabled", "stats", "logs"}
REGISTER_RUNTIME_CONFIG_KEY = "register_runtime"


def _db_backend():
    try:
        from services.config import config

        backend = config.get_storage_backend()
        return backend if backend.supports_database_features() is True else None
    except Exception:
        return None


def _serialize_outlook_pool(credentials: list[dict]) -> str:
    return "\n".join(
        f'{c["email"]}----{c.get("password", "")}----{c["client_id"]}----{c["refresh_token"]}' for c in credentials
    )


def _merge_outlook_pool(old_text: str, new_text: str) -> str:
    """合并已存邮箱池与新导入文本，按邮箱去重，新导入的同名邮箱覆盖旧凭据。"""
    merged: dict[str, dict] = {}
    for credential in mail_provider.parse_outlook_credentials(old_text or ""):
        merged[credential["email"].strip().lower()] = credential
    for credential in mail_provider.parse_outlook_credentials(new_text or ""):
        merged[credential["email"].strip().lower()] = credential
    return _serialize_outlook_pool(list(merged.values()))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_config() -> dict:
    return {
        **openai_register.config,
        "engine": "http",
        "browser_token_mode": "session",
        "mode": "total",
        "target_quota": 100,
        "target_available": 10,
        "check_interval": 5,
        "enabled": False,
        "stats": {
            "success": 0,
            "fail": 0,
            "done": 0,
            "running": 0,
            "threads": openai_register.config["threads"],
            "elapsed_seconds": 0,
            "avg_seconds": 0,
            "success_rate": 0,
            "current_quota": 0,
            "current_available": 0,
            "full_refresh_batches": 0,
        },
    }


def _normalize_mail_config(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    result = {
        **source,
        "request_timeout": max(1, float(source.get("request_timeout") or 30)),
        "wait_timeout": max(1, float(source.get("wait_timeout") or 30)),
        "wait_interval": max(0.2, float(source.get("wait_interval") or 2)),
        "auto_disable": bool(source.get("auto_disable", True)),
        "failure_threshold": max(1, int(source.get("failure_threshold") or 10)),
        "providers": [],
    }
    result.pop("proxy", None)
    providers = source.get("providers") if isinstance(source.get("providers"), list) else []
    seen_ids: set[str] = set()
    for index, raw_provider in enumerate(providers):
        if not isinstance(raw_provider, dict):
            continue
        provider = dict(raw_provider)
        provider.pop("health", None)
        provider_type = str(provider.get("type") or "").strip()
        provider_id = str(provider.get("id") or f"{provider_type}#{index + 1}").strip()
        if provider_id in seen_ids:
            provider_id = f"{provider_id}-{index + 1}"
        seen_ids.add(provider_id)
        provider["id"] = provider_id
        try:
            provider["priority"] = max(1, int(provider.get("priority") or index + 1))
        except (TypeError, ValueError):
            provider["priority"] = index + 1
        if provider_type == "mailpit":
            provider["domain"] = mail_provider.normalize_mailpit_domains(provider.get("domain") or provider.get("suffix"))
            provider["domain_mode"] = "sequential" if str(provider.get("domain_mode")) == "sequential" else "round_robin"
        elif provider_type == "outlook_token":
            provider["alias_enabled"] = mail_provider._normalize_bool(provider.get("alias_enabled"), False)
            provider["alias_per_email"] = mail_provider._normalize_int(provider.get("alias_per_email"), 5, 0, 200)
            provider["alias_prefix"] = str(provider.get("alias_prefix") or "c2api").strip() or "c2api"
            provider["alias_include_original"] = mail_provider._normalize_bool(provider.get("alias_include_original"), True)
            for key in ("mailboxes_count", "mailboxes_base_count", "mailboxes_alias_count", "mailboxes_preview", "mailboxes_stats", "alias_preview"):
                provider.pop(key, None)
        result["providers"].append(provider)
    return result


def _normalize(raw: dict) -> dict:
    cfg = _default_config()
    cfg.update({k: v for k, v in raw.items() if k not in {"stats", "logs"}})
    cfg["total"] = max(1, int(cfg.get("total") or 1))
    cfg["threads"] = max(1, int(cfg.get("threads") or 1))
    cfg["engine"] = str(cfg.get("engine") or "http").strip().lower()
    if cfg["engine"] not in {"http", "browser"}:
        cfg["engine"] = "http"
    cfg["browser_token_mode"] = str(cfg.get("browser_token_mode") or "session").strip().lower()
    if cfg["browser_token_mode"] not in {"session", "oauth"}:
        cfg["browser_token_mode"] = "session"
    cfg["mode"] = str(cfg.get("mode") or "total").strip() if str(cfg.get("mode") or "total").strip() in {"total", "quota", "available"} else "total"
    cfg["target_quota"] = max(1, int(cfg.get("target_quota") or 1))
    cfg["target_available"] = max(1, int(cfg.get("target_available") or 1))
    cfg["check_interval"] = max(1, int(cfg.get("check_interval") or 5))
    cfg["proxy"] = openai_register.normalize_registration_proxy_text(cfg.get("proxy"))
    cfg["mail"] = _normalize_mail_config(cfg.get("mail"))
    cfg["enabled"] = bool(cfg.get("enabled"))
    raw_stats = raw.get("stats") if isinstance(raw.get("stats"), dict) else {}
    stats = {
        **_default_config()["stats"],
        **raw_stats,
        "threads": cfg["threads"],
    }
    if "full_refresh_batches" not in raw_stats:
        stats["full_refresh_batches"] = max(0, int(stats.get("success") or 0)) // REGISTER_FULL_REFRESH_BATCH_SIZE
    cfg["stats"] = stats
    logs = raw.get("logs") if isinstance(raw.get("logs"), list) else []
    cfg["logs"] = [
        {
            "time": str(item.get("time") or _now()),
            "text": str(item.get("text") or ""),
            "level": str(item.get("level") or "info"),
        }
        for item in logs[-REGISTER_LOG_LIMIT:]
        if isinstance(item, dict) and str(item.get("text") or "")
    ]
    return cfg


class RegisterService:
    def __init__(self, store_file: Path, *, start_supervisor: bool = False):
        self._store_file = store_file
        self._lock = threading.RLock()
        self._runner: threading.Thread | None = None
        self._lock_owner = ""
        self._lock_lost = False
        self._last_lock_extend_at = 0.0
        self._last_save_at = 0.0
        self._last_metrics_log: dict[str, tuple[int, int, float]] = {}
        self._last_supervisor_wait_log_at = 0.0
        self._supervisor_stop = threading.Event()
        self._supervisor: threading.Thread | None = None
        openai_register.register_log_sink = self._append_log
        self._config = self._load()
        if start_supervisor:
            self.start_supervisor()

    def start_supervisor(self) -> None:
        with self._lock:
            if self._supervisor and self._supervisor.is_alive():
                return
            self._supervisor_stop.clear()
            self._supervisor = threading.Thread(
                target=self._supervise_loop,
                daemon=True,
                name="register-supervisor",
            )
            self._supervisor.start()

    def stop_supervisor(self, *, timeout: float | None = None) -> None:
        self._supervisor_stop.set()
        if timeout is not None and self._supervisor is not None:
            self._supervisor.join(timeout=timeout)

    def _load(self) -> dict:
        db = _db_backend()
        if db is not None:
            try:
                data = db.load_named_config("register")
                if isinstance(data, dict) and data:
                    return _normalize(data)
                return _normalize({})
            except Exception:
                return _normalize({})
        try:
            return _normalize(json.loads(self._store_file.read_text(encoding="utf-8")))
        except Exception:
            return _normalize({})

    def _save(self) -> None:
        db = _db_backend()
        if db is not None:
            db.save_named_config("register", self._config)
            return
        self._store_file.parent.mkdir(parents=True, exist_ok=True)
        self._store_file.write_text(json.dumps(self._config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _save_runtime(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_save_at < REGISTER_SAVE_INTERVAL_SECONDS:
            return
        self._last_save_at = now
        runtime_payload = self._snapshot(runtime_only=True)
        db = _db_backend()
        if db is not None:
            try:
                db.save_named_config(REGISTER_RUNTIME_CONFIG_KEY, runtime_payload)
                return
            except Exception:
                pass
        self._save()

    def _refresh_persisted_runtime_locked(self) -> None:
        db = _db_backend()
        if db is not None:
            try:
                runtime_payload = db.load_named_config(REGISTER_RUNTIME_CONFIG_KEY)
            except Exception:
                runtime_payload = None
            if isinstance(runtime_payload, dict) and runtime_payload:
                for key in REGISTER_RUNTIME_KEYS:
                    if key in runtime_payload:
                        self._config[key] = runtime_payload[key]
                return
        loaded = self._load()
        if not loaded:
            return
        for key in REGISTER_RUNTIME_KEYS:
            if key in loaded:
                self._config[key] = loaded[key]

    def _snapshot(self, *, redact: bool = True, runtime_only: bool = False) -> dict:
        browser_status = browser_register.browser_runtime_status()
        if runtime_only:
            stats = self._config.get("stats") if isinstance(self._config.get("stats"), dict) else {}
            logs = self._config.get("logs") if isinstance(self._config.get("logs"), list) else []
            return {
                "enabled": bool(self._config.get("enabled")),
                "mode": self._config.get("mode"),
                "engine": self._config.get("engine"),
                "browser_token_mode": self._config.get("browser_token_mode"),
                "total": self._config.get("total"),
                "threads": self._config.get("threads"),
                "target_quota": self._config.get("target_quota"),
                "target_available": self._config.get("target_available"),
                "check_interval": self._config.get("check_interval"),
                "stats": json.loads(json.dumps(stats, ensure_ascii=False)),
                "logs": json.loads(json.dumps(logs[-REGISTER_LOG_LIMIT:], ensure_ascii=False)),
                **browser_status,
            }
        snapshot = json.loads(json.dumps(self._config, ensure_ascii=False))
        snapshot.update(browser_status)
        if isinstance(snapshot.get("mail"), dict):
            snapshot["mail"] = mail_provider.mail_config_with_health(snapshot["mail"])
        if redact:
            self._redact_outlook_pools(snapshot)
        return snapshot

    def _runtime_cfg_locked(self) -> dict:
        return {
            "enabled": bool(self._config.get("enabled")),
            "mode": str(self._config.get("mode") or "total"),
            "engine": str(self._config.get("engine") or "http"),
            "total": int(self._config.get("total") or 1),
            "threads": int(self._config.get("threads") or 1),
            "target_quota": int(self._config.get("target_quota") or 1),
            "target_available": int(self._config.get("target_available") or 1),
            "check_interval": int(self._config.get("check_interval") or 5),
        }

    def _runner_alive_locked(self) -> bool:
        return self._runner is not None and self._runner.is_alive()

    def _sync_register_stats_locked(self) -> None:
        stats = self._config.get("stats") if isinstance(self._config.get("stats"), dict) else {}
        started_at = str(stats.get("started_at") or "")
        start_time = time.time()
        if started_at:
            try:
                start_time = datetime.fromisoformat(started_at).timestamp()
            except Exception:
                start_time = time.time()
        with openai_register.stats_lock:
            openai_register.stats.update({
                "done": int(stats.get("done") or 0),
                "success": int(stats.get("success") or 0),
                "fail": int(stats.get("fail") or 0),
                "start_time": start_time,
            })

    def _start_runner_locked(self, *, reset_runtime: bool, recovered: bool) -> None:
        self._lock_lost = False
        self._config["enabled"] = True
        self._drop_mail_proxy()
        if reset_runtime:
            self._config["logs"] = []
            metrics = self._pool_metrics()
            self._config["stats"] = {
                "job_id": uuid.uuid4().hex,
                "success": 0,
                "fail": 0,
                "done": 0,
                "running": 0,
                "threads": self._config["threads"],
                "full_refresh_batches": 0,
                **metrics,
                "started_at": _now(),
                "updated_at": _now(),
            }
        else:
            existing_stats = self._config.get("stats") if isinstance(self._config.get("stats"), dict) else {}
            completed_batches = existing_stats.get("full_refresh_batches")
            if completed_batches is None:
                completed_batches = max(0, int(existing_stats.get("success") or 0)) // REGISTER_FULL_REFRESH_BATCH_SIZE
            self._config["stats"] = {
                **_default_config()["stats"],
                **existing_stats,
                "threads": self._config["threads"],
                "full_refresh_batches": max(0, int(completed_batches or 0)),
                "updated_at": _now(),
            }
            self._config["stats"].setdefault("job_id", uuid.uuid4().hex)
            self._config["stats"].setdefault("started_at", _now())
        openai_register.config.update({k: self._config[k] for k in ("mail", "proxy", "total", "threads", "mode", "engine", "browser_token_mode")})
        self._sync_register_stats_locked()
        if reset_runtime:
            self._save()
        self._save_runtime(force=True)
        self._runner = threading.Thread(target=self._run, daemon=True, name="openai-register")
        self._runner.start()
        event = "register_runner_recovered" if recovered else "register_runner_started"
        logger.warning({
            "event": event,
            "pid": os.getpid(),
            "mode": self._config.get("mode"),
            "threads": self._config.get("threads"),
        })

    def _log_recovery_wait_locked(self) -> None:
        now = time.monotonic()
        if now - self._last_supervisor_wait_log_at < REGISTER_SUPERVISOR_WAIT_LOG_INTERVAL_SECONDS:
            return
        self._last_supervisor_wait_log_at = now
        self._append_log(
            f"注册任务恢复等待锁释放：当前 worker 暂未拿到注册锁，最多等待旧锁 TTL {REGISTER_RUN_LOCK_TTL_SECONDS}s",
            "yellow",
            force=True,
        )
        logger.warning({
            "event": "register_recovery_wait_lock",
            "pid": os.getpid(),
            "lock_ttl_secs": REGISTER_RUN_LOCK_TTL_SECONDS,
        })

    def _supervise_once(self) -> dict[str, object]:
        with self._lock:
            if self._runner_alive_locked():
                return {"state": "running", "pid": os.getpid()}
            if self._lock_owner:
                lock_owner = self._lock_owner
                self._lock_owner = ""
                self._lock_lost = False
                runtime_state.release_lock(REGISTER_RUN_LOCK, lock_owner)
                self._append_log("注册 runner 异常停止，已释放本进程注册锁并等待恢复", "yellow", force=True)
                logger.warning({"event": "register_runner_stale_lock_released", "pid": os.getpid()})
            loaded = self._load()
            if loaded:
                self._config = loaded
            self._refresh_persisted_runtime_locked()
            if self._apply_stop_request_locked() or self._stop_requested():
                return {"state": "stopped", "pid": os.getpid()}
            if not bool(self._config.get("enabled")):
                return {"state": "disabled", "pid": os.getpid()}
            if self._config.get("engine") == "browser" and not browser_register.browser_runtime_status(refresh=True)["browser_available"]:
                self._config["enabled"] = False
                self._save_runtime(force=True)
                self._append_log("浏览器运行环境不可用，注册任务恢复已停止", "red", force=True)
                return {"state": "browser_unavailable", "pid": os.getpid()}
            lock_owner = runtime_state.acquire_lock(
                REGISTER_RUN_LOCK,
                ttl_seconds=REGISTER_RUN_LOCK_TTL_SECONDS,
                allow_memory_fallback=not is_multi_worker_runtime(),
            )
            if not lock_owner:
                self._log_recovery_wait_locked()
                return {"state": "waiting_lock", "pid": os.getpid()}
            self._lock_owner = lock_owner
            self._start_runner_locked(reset_runtime=False, recovered=True)
            self._append_log(
                f"注册任务恢复启动，模式={self._config['mode']}，线程数={self._config['threads']}",
                "yellow",
                force=True,
            )
            return {"state": "recovered", "pid": os.getpid()}

    def _supervise_loop(self) -> None:
        while not self._supervisor_stop.is_set():
            try:
                self._supervise_once()
            except Exception as exc:
                logger.warning({
                    "event": "register_supervisor_error",
                    "pid": os.getpid(),
                    "error": str(exc)[:300],
                })
            if self._supervisor_stop.wait(REGISTER_SUPERVISOR_INTERVAL_SECONDS):
                return

    def runtime_snapshot(self) -> dict:
        with self._lock:
            if self._lock_owner:
                self._apply_stop_request_locked()
            else:
                self._refresh_persisted_runtime_locked()
            return self._snapshot(runtime_only=True)

    def _stop_requested(self) -> bool:
        return bool(runtime_state.get_flag(REGISTER_STOP_KEY))

    def _set_stop_requested(self) -> None:
        runtime_state.set_flag(REGISTER_STOP_KEY, "1", ttl_seconds=REGISTER_STOP_TTL_SECONDS)

    def _clear_stop_requested(self) -> None:
        runtime_state.delete_flag(REGISTER_STOP_KEY)

    def get(self) -> dict:
        with self._lock:
            if self._lock_owner:
                self._apply_stop_request_locked()
            else:
                loaded = self._load()
                if loaded:
                    self._config = loaded
                self._refresh_persisted_runtime_locked()
            return self._snapshot()

    @staticmethod
    def _mask_email(email: str) -> str:
        local, sep, domain = str(email or "").partition("@")
        if not sep:
            return "***"
        masked = (local[:2] + "***" + local[-1:]) if len(local) > 2 else (local[:1] + "***")
        return f"{masked}@{domain}"

    def _redact_outlook_pools(self, snapshot: dict) -> None:
        """把 outlook_token 邮箱池里的密码/refresh_token 从对外输出中抹掉，仅保留脱敏预览与统计。

        mailboxes 改为只写导入框（输出为空），避免把密码与 refresh_token 通过 GET/SSE 反复广播。
        """
        mail = snapshot.get("mail")
        if not isinstance(mail, dict):
            return
        providers = mail.get("providers")
        if not isinstance(providers, list):
            return
        for provider in providers:
            if not isinstance(provider, dict) or provider.get("type") != "outlook_token":
                continue
            pool_text = str(provider.get("mailboxes") or "")
            base_credentials = mail_provider.parse_outlook_credentials(pool_text)
            credentials = mail_provider.expand_outlook_aliases(base_credentials, provider)
            provider["mailboxes"] = ""
            provider["mailboxes_count"] = len(credentials)
            provider["mailboxes_base_count"] = len(base_credentials)
            provider["mailboxes_alias_count"] = sum(1 for credential in credentials if credential.get("alias_of"))
            provider["mailboxes_preview"] = [self._mask_email(c["email"]) for c in credentials]
            provider["mailboxes_stats"] = mail_provider.outlook_token_pool_stats(credentials)
            provider["alias_preview"] = [self._mask_email(email) for email in mail_provider.outlook_alias_preview({**provider, "mailboxes": pool_text})]

    def _drop_mail_proxy(self) -> None:
        if isinstance(self._config.get("mail"), dict):
            self._config["mail"].pop("proxy", None)

    def _merge_outlook_pools(self, updates: dict) -> None:
        """对 outlook_token provider：把前端新导入的 mailboxes 与已存池按邮箱合并去重。

        前端 mailboxes 是只写导入框，留空表示不改动；填入的新行追加/覆盖已存凭据。
        按数组下标与已存的同类型 provider 对齐。
        """
        mail = updates.get("mail")
        if not isinstance(mail, dict) or not isinstance(mail.get("providers"), list):
            return
        old_mail = self._config.get("mail") if isinstance(self._config.get("mail"), dict) else {}
        old_providers = old_mail.get("providers") if isinstance(old_mail.get("providers"), list) else []
        for index, provider in enumerate(mail["providers"]):
            if not isinstance(provider, dict) or provider.get("type") != "outlook_token":
                continue
            old = old_providers[index] if index < len(old_providers) and isinstance(old_providers[index], dict) else {}
            old_text = str(old.get("mailboxes") or "") if old.get("type") == "outlook_token" else ""
            new_text = str(provider.get("mailboxes") or "")
            provider["mailboxes"] = _merge_outlook_pool(old_text, new_text) if (old_text or new_text) else ""
            for key in ("mailboxes_count", "mailboxes_base_count", "mailboxes_alias_count", "mailboxes_preview", "mailboxes_stats", "alias_preview"):
                provider.pop(key, None)

    def _drop_changed_provider_ids(self, updates: dict) -> None:
        mail = updates.get("mail")
        if not isinstance(mail, dict) or not isinstance(mail.get("providers"), list):
            return
        old_mail = self._config.get("mail") if isinstance(self._config.get("mail"), dict) else {}
        old_providers = old_mail.get("providers") if isinstance(old_mail.get("providers"), list) else []
        for index, provider in enumerate(mail["providers"]):
            if not isinstance(provider, dict) or index >= len(old_providers) or not isinstance(old_providers[index], dict):
                continue
            old_type = str(old_providers[index].get("type") or "")
            new_type = str(provider.get("type") or "")
            if old_type and new_type and old_type != new_type:
                provider.pop("id", None)
                provider.pop("health", None)

    def _prune_unused_outlook_pools(self) -> int:
        mail = self._config.get("mail")
        if not isinstance(mail, dict):
            return 0
        providers = mail.get("providers")
        if not isinstance(providers, list):
            return 0
        total_removed = 0
        for provider in providers:
            if not isinstance(provider, dict) or provider.get("type") != "outlook_token":
                continue
            credentials = mail_provider.parse_outlook_credentials(str(provider.get("mailboxes") or ""))
            kept, removed = mail_provider.prune_outlook_unused_credentials(credentials, provider)
            if removed:
                provider["mailboxes"] = _serialize_outlook_pool(kept)
                total_removed += removed
            for key in ("mailboxes_count", "mailboxes_base_count", "mailboxes_alias_count", "mailboxes_preview", "mailboxes_stats", "alias_preview"):
                provider.pop(key, None)
        return total_removed

    def update(self, updates: dict) -> dict:
        with self._lock:
            loaded = self._load()
            if loaded:
                self._config = loaded
            self._drop_changed_provider_ids(updates)
            self._merge_outlook_pools(updates)
            self._config = _normalize({**self._config, **updates})
            self._drop_mail_proxy()
            openai_register.config.update({k: self._config[k] for k in ("mail", "proxy", "total", "threads", "mode", "engine", "browser_token_mode")})
            self._save()
            return self.get()

    def start(self) -> dict:
        with self._lock:
            if self._runner_alive_locked():
                if not self._stop_requested():
                    self._config["enabled"] = True
                    self._save_runtime(force=True)
                return self.get()
            loaded = self._load()
            if loaded:
                self._config = loaded
            if self._config.get("engine") == "browser" and not browser_register.browser_runtime_status(refresh=True)["browser_available"]:
                raise RuntimeError("browser_runtime_unavailable")
            lock_owner = runtime_state.acquire_lock(
                REGISTER_RUN_LOCK,
                ttl_seconds=REGISTER_RUN_LOCK_TTL_SECONDS,
                allow_memory_fallback=not is_multi_worker_runtime(),
            )
            if not lock_owner:
                self._refresh_persisted_runtime_locked()
                self._clear_stop_requested()
                self._config["enabled"] = True
                self._config["stats"]["updated_at"] = _now()
                self._save_runtime(force=True)
                self._log_recovery_wait_locked()
                return self.get()
            self._lock_owner = lock_owner
            self._clear_stop_requested()
            self._start_runner_locked(reset_runtime=True, recovered=False)
            self._append_log(
                f"注册任务启动，引擎={self._config['engine']}，凭据={self._config['browser_token_mode']}，"
                f"模式={self._config['mode']}，线程数={self._config['threads']}",
                "yellow",
            )
            return self.get()

    def stop(self) -> dict:
        with self._lock:
            self._set_stop_requested()
            if not self._lock_owner:
                loaded = self._load()
                if loaded:
                    self._config = loaded
                self._refresh_persisted_runtime_locked()
            self._config["enabled"] = False
            self._config["stats"]["updated_at"] = _now()
            self._save()
            self._save_runtime(force=True)
            self._append_log("已请求停止注册任务，正在等待当前运行任务结束", "yellow", force=True)
            return self.get()

    def reset(self) -> dict:
        with self._lock:
            loaded = self._load()
            if loaded:
                self._config = loaded
            self._config["logs"] = []
            self._config["stats"] = {
                "success": 0,
                "fail": 0,
                "done": 0,
                "running": 0,
                "threads": self._config["threads"],
                "elapsed_seconds": 0,
                "avg_seconds": 0,
                "success_rate": 0,
                "full_refresh_batches": 0,
                **self._pool_metrics(),
                "updated_at": _now(),
            }
            with openai_register.stats_lock:
                openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": 0.0})
            self._save()
            return self.get()

    def reset_outlook_pool(self, scope: str = "all") -> dict:
        scope = str(scope or "all").strip().lower()
        if scope == "unused":
            with self._lock:
                loaded = self._load()
                if loaded:
                    self._config = loaded
                removed = self._prune_unused_outlook_pools()
                openai_register.config.update({k: self._config[k] for k in ("mail", "proxy", "total", "threads", "mode", "engine", "browser_token_mode")})
                self._save()
                self._append_log(f"已清空 Outlook 邮箱池未使用邮箱，移除 {removed} 个", "yellow")
            return self.get()
        scope = "failed" if str(scope) == "failed" else "all"
        cleared = mail_provider.reset_outlook_token_pool_state(scope)
        with self._lock:
            self._append_log(
                f"已重置 Outlook 邮箱池状态（范围={'仅失败/占用' if scope == 'failed' else '全部'}），清除 {cleared} 条记录",
                "yellow",
            )
        return self.get()

    def reset_mail_health(self, provider_id: str = "", domain: str = "") -> dict:
        from services.register.mail_health import mail_health_store

        reset_count = mail_health_store.reset(provider_id, domain)
        target = f"渠道 {provider_id}"
        if provider_id and domain:
            target = f"Mailpit 域名 {domain}（渠道 {provider_id}）"
        elif not provider_id:
            target = "全部邮箱渠道"
        with self._lock:
            self._append_log(f"已手动恢复{target}的自动健康状态，清除 {reset_count} 条记录", "yellow")
        return self.get()

    def _append_log(self, text: str, color: str = "", *, force: bool = False) -> None:
        with self._lock:
            logs = self._config.get("logs") if isinstance(self._config.get("logs"), list) else []
            logs.append({"time": _now(), "text": str(text), "level": str(color or "info")})
            self._config["logs"] = logs[-REGISTER_LOG_LIMIT:]
            self._save_runtime(force=force)

    def _pool_metrics(self) -> dict:
        return account_service.get_image_pool_metrics()

    def _refresh_accounts_after_registration_batch(self, batch_number: int, success: int) -> None:
        access_tokens = account_service.list_tokens()
        self._append_log(
            f"注册成功累计{success}个，开始第{batch_number}次全量刷新，账号数={len(access_tokens)}",
            "yellow",
            force=True,
        )
        try:
            with ThreadPoolExecutor(max_workers=1) as refresh_executor:
                refresh_future = refresh_executor.submit(
                    account_service.refresh_accounts,
                    access_tokens,
                    defer_invalid_removal=False,
                    include_items=False,
                )
                while True:
                    try:
                        result = refresh_future.result(timeout=REGISTER_LOCK_REFRESH_INTERVAL_SECONDS / 2)
                        break
                    except FutureTimeoutError:
                        self._bump()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            self._append_log(
                f"第{batch_number}次全量刷新未完成: {error.__class__.__name__}",
                "red",
                force=True,
            )
        else:
            self._append_log(
                f"第{batch_number}次全量刷新完成，成功={int(result.get('refreshed') or 0)}，"
                f"错误={len(result.get('errors') or [])}",
                "yellow",
                force=True,
            )
        finally:
            try:
                metrics = self._pool_metrics()
            except Exception as error:
                self._append_log(
                    f"第{batch_number}次全量刷新后号池统计失败: {error.__class__.__name__}",
                    "red",
                    force=True,
                )
            else:
                self._bump(**metrics)

    def _target_reached(self, cfg: dict, submitted: int) -> bool:
        mode = str(cfg.get("mode") or "total")
        if mode == "total":
            return submitted >= int(cfg.get("total") or 1)
        metrics = self._pool_metrics()
        self._bump(**metrics)
        now = time.monotonic()
        if mode == "quota":
            reached = metrics["current_quota"] >= int(cfg.get("target_quota") or 1)
            key = "quota"
            last_available, last_quota, last_at = self._last_metrics_log.get(key, (-1, -1, 0.0))
            if reached or metrics["current_available"] != last_available or metrics["current_quota"] != last_quota or now - last_at >= 30:
                self._last_metrics_log[key] = (metrics["current_available"], metrics["current_quota"], now)
                self._append_log(f"检查号池：当前可生图账号={metrics['current_available']}，当前剩余额度={metrics['current_quota']}，目标额度={cfg.get('target_quota')}，{'跳过注册' if reached else '继续注册'}", "yellow")
            return reached
        if mode == "available":
            reached = metrics["current_available"] >= int(cfg.get("target_available") or 1)
            key = "available"
            last_available, last_quota, last_at = self._last_metrics_log.get(key, (-1, -1, 0.0))
            if reached or metrics["current_available"] != last_available or metrics["current_quota"] != last_quota or now - last_at >= 30:
                self._last_metrics_log[key] = (metrics["current_available"], metrics["current_quota"], now)
                self._append_log(f"检查号池：当前可生图账号={metrics['current_available']}，目标账号={cfg.get('target_available')}，当前剩余额度={metrics['current_quota']}，{'跳过注册' if reached else '继续注册'}", "yellow")
            return reached
        return submitted >= int(cfg.get("total") or 1)

    def _bump(self, **updates) -> None:
        with self._lock:
            lock_lost_now = False
            now = time.monotonic()
            should_extend = now - self._last_lock_extend_at >= REGISTER_LOCK_REFRESH_INTERVAL_SECONDS
            if self._lock_owner and not self._lock_lost and should_extend:
                extended = runtime_state.extend_lock(
                    REGISTER_RUN_LOCK,
                    self._lock_owner,
                    ttl_seconds=REGISTER_RUN_LOCK_TTL_SECONDS,
                )
                if not extended:
                    self._lock_lost = True
                    self._config["enabled"] = False
                    lock_lost_now = True
                else:
                    self._last_lock_extend_at = now
            self._config["stats"].update(updates)
            stats = self._config["stats"]
            started_at = str(stats.get("started_at") or "")
            if started_at:
                try:
                    elapsed = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(started_at)).total_seconds())
                except Exception:
                    elapsed = 0.0
                done = int(stats.get("done") or 0)
                success = int(stats.get("success") or 0)
                fail = int(stats.get("fail") or 0)
                stats["elapsed_seconds"] = round(elapsed, 1)
                stats["avg_seconds"] = round(elapsed / success, 1) if success else 0
                stats["success_rate"] = round(success * 100 / max(1, success + fail), 1)
            self._config["stats"]["updated_at"] = _now()
            self._save_runtime()
        if lock_lost_now:
            self._append_log("注册锁续租失败，本 worker 停止提交新注册任务并退出", "red", force=True)
            logger.warning({"event": "register_lock_lost_exit", "pid": os.getpid()})

    def _apply_stop_request_locked(self) -> bool:
        if not self._stop_requested():
            return False
        if self._config.get("enabled"):
            self._config["enabled"] = False
            self._save_runtime(force=True)
        return True

    def _refresh_runtime_cfg_for_runner(self) -> dict:
        with self._lock:
            cfg = self._runtime_cfg_locked()
            if self._lock_lost:
                cfg["enabled"] = False
            if self._apply_stop_request_locked():
                cfg["enabled"] = False
            return cfg

    def _sleep_interruptible(self, seconds: int) -> None:
        deadline = time.monotonic() + max(1, int(seconds or 1))
        while time.monotonic() < deadline:
            if self._stop_requested():
                return
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))

    def _run(self) -> None:
        cfg = self._refresh_runtime_cfg_for_runner()
        engine = str(cfg.get("engine") or "http")
        threads = int(cfg["threads"])
        worker = browser_register.worker if engine == "browser" else openai_register.worker
        with self._lock:
            stats = self._config.get("stats") if isinstance(self._config.get("stats"), dict) else {}
            done = max(0, int(stats.get("done") or 0))
            success = max(0, int(stats.get("success") or 0))
            fail = max(0, int(stats.get("fail") or 0))
            full_refresh_batches = max(0, int(stats.get("full_refresh_batches") or 0))
        submitted = max(done, success + fail)
        lock_lost_exit = False
        mail_unavailable_exit = False
        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = set()
            while True:
                cfg = self._refresh_runtime_cfg_for_runner()
                if self._lock_lost:
                    lock_lost_exit = True
                next_full_refresh_at = (full_refresh_batches + 1) * REGISTER_FULL_REFRESH_BATCH_SIZE
                if bool(cfg.get("enabled")) and not futures and success >= next_full_refresh_at:
                    self._bump(running=0, done=done, success=success, fail=fail)
                    if self._lock_lost:
                        lock_lost_exit = True
                        continue
                    full_refresh_batches += 1
                    self._bump(full_refresh_batches=full_refresh_batches)
                    self._refresh_accounts_after_registration_batch(full_refresh_batches, success)
                    cfg = self._refresh_runtime_cfg_for_runner()
                    next_full_refresh_at = (full_refresh_batches + 1) * REGISTER_FULL_REFRESH_BATCH_SIZE
                while (
                    bool(cfg.get("enabled"))
                    and not self._target_reached(cfg, submitted)
                    and len(futures) < threads
                    and success + len(futures) < next_full_refresh_at
                ):
                    submitted += 1
                    futures.add(executor.submit(worker, submitted))
                self._bump(running=len(futures), done=done, success=success, fail=fail)
                cfg = self._refresh_runtime_cfg_for_runner()
                if self._lock_lost:
                    lock_lost_exit = True
                enabled = bool(cfg.get("enabled"))
                if not futures and (not enabled or str(cfg.get("mode") or "total") == "total"):
                    break
                if not futures:
                    self._sleep_interruptible(max(1, int(cfg.get("check_interval") or 5)))
                    continue
                finished, futures = wait(futures, timeout=1.0, return_when=FIRST_COMPLETED)
                if not finished:
                    continue
                for future in finished:
                    done += 1
                    try:
                        result = future.result()
                        success += 1 if result.get("ok") else 0
                        fail += 0 if result.get("ok") else 1
                        if result.get("stop_reason") == "all_mail_providers_unavailable":
                            if not mail_unavailable_exit:
                                mail_unavailable_exit = True
                                with self._lock:
                                    self._config["enabled"] = False
                                    self._config["stats"]["stop_reason"] = "all_mail_providers_unavailable"
                                    self._save_runtime(force=True)
                                self._append_log("所有邮箱渠道均已自动禁用、手动停用或耗尽，停止提交新任务并等待运行中的任务结束", "red", force=True)
                    except Exception:
                        fail += 1
        self._bump(running=0, done=done, success=success, fail=fail, finished_at=_now())
        with self._lock:
            stopped_by_user = self._stop_requested()
            keep_enabled_for_recovery = bool(self._lock_lost or lock_lost_exit) and not stopped_by_user
            self._config["enabled"] = bool(keep_enabled_for_recovery)
            if not keep_enabled_for_recovery:
                self._save()
            self._save_runtime(force=True)
            lock_owner = self._lock_owner
            self._lock_owner = ""
            if lock_owner:
                runtime_state.release_lock(REGISTER_RUN_LOCK, lock_owner)
            if not keep_enabled_for_recovery:
                self._clear_stop_requested()
            self._lock_lost = False
        suffix = "，邮箱渠道不可用" if mail_unavailable_exit else ("，等待其他 worker 恢复" if keep_enabled_for_recovery else "")
        self._append_log(f"注册任务结束，成功{success}，失败{fail}{suffix}", "yellow", force=True)
        trim_memory("register_run_finished", force=True)


register_service = RegisterService(REGISTER_FILE)
