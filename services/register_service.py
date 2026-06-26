from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

from services.account_service import account_service
from services.config import DATA_DIR
from services.register import mail_provider, openai_register
from services.runtime_state import runtime_state


REGISTER_FILE = DATA_DIR / "register.json"
REGISTER_RUN_LOCK = "lock:register:run"
REGISTER_RUN_LOCK_TTL_SECONDS = 120
REGISTER_LOG_LIMIT = 120
REGISTER_SAVE_INTERVAL_SECONDS = 2.0
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
    return {**openai_register.config, "mode": "total", "target_quota": 100, "target_available": 10, "check_interval": 5, "enabled": False, "stats": {"success": 0, "fail": 0, "done": 0, "running": 0, "threads": openai_register.config["threads"], "elapsed_seconds": 0, "avg_seconds": 0, "success_rate": 0, "current_quota": 0, "current_available": 0}}


def _normalize(raw: dict) -> dict:
    cfg = _default_config()
    cfg.update({k: v for k, v in raw.items() if k not in {"stats", "logs"}})
    cfg["total"] = max(1, int(cfg.get("total") or 1))
    cfg["threads"] = max(1, int(cfg.get("threads") or 1))
    cfg["mode"] = str(cfg.get("mode") or "total").strip() if str(cfg.get("mode") or "total").strip() in {"total", "quota", "available"} else "total"
    cfg["target_quota"] = max(1, int(cfg.get("target_quota") or 1))
    cfg["target_available"] = max(1, int(cfg.get("target_available") or 1))
    cfg["check_interval"] = max(1, int(cfg.get("check_interval") or 5))
    cfg["proxy"] = str(cfg.get("proxy") or "").strip()
    if isinstance(cfg.get("mail"), dict):
        cfg["mail"].pop("proxy", None)
    cfg["enabled"] = bool(cfg.get("enabled"))
    stats = {**_default_config()["stats"], **(raw.get("stats") if isinstance(raw.get("stats"), dict) else {}),
             "threads": cfg["threads"]}
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
    def __init__(self, store_file: Path):
        self._store_file = store_file
        self._lock = threading.RLock()
        self._runner: threading.Thread | None = None
        self._lock_owner = ""
        self._last_save_at = 0.0
        self._last_metrics_log: dict[str, tuple[int, int, float]] = {}
        openai_register.register_log_sink = self._append_log
        self._config = self._load()
        if self._config["enabled"]:
            self.start()

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
        if runtime_only:
            stats = self._config.get("stats") if isinstance(self._config.get("stats"), dict) else {}
            logs = self._config.get("logs") if isinstance(self._config.get("logs"), list) else []
            return {
                "enabled": bool(self._config.get("enabled")),
                "mode": self._config.get("mode"),
                "total": self._config.get("total"),
                "threads": self._config.get("threads"),
                "target_quota": self._config.get("target_quota"),
                "target_available": self._config.get("target_available"),
                "check_interval": self._config.get("check_interval"),
                "stats": json.loads(json.dumps(stats, ensure_ascii=False)),
                "logs": json.loads(json.dumps(logs[-REGISTER_LOG_LIMIT:], ensure_ascii=False)),
            }
        snapshot = json.loads(json.dumps(self._config, ensure_ascii=False))
        if redact:
            self._redact_outlook_pools(snapshot)
        return snapshot

    def _runtime_cfg_locked(self) -> dict:
        return {
            "enabled": bool(self._config.get("enabled")),
            "mode": str(self._config.get("mode") or "total"),
            "total": int(self._config.get("total") or 1),
            "threads": int(self._config.get("threads") or 1),
            "target_quota": int(self._config.get("target_quota") or 1),
            "target_available": int(self._config.get("target_available") or 1),
            "check_interval": int(self._config.get("check_interval") or 5),
        }

    def runtime_snapshot(self) -> dict:
        with self._lock:
            if not self._lock_owner:
                self._refresh_persisted_runtime_locked()
            return self._snapshot(runtime_only=True)

    def get(self) -> dict:
        with self._lock:
            loaded = self._load()
            if loaded:
                self._config = loaded
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
            credentials = mail_provider.parse_outlook_credentials(str(provider.get("mailboxes") or ""))
            provider["mailboxes"] = ""
            provider["mailboxes_count"] = len(credentials)
            provider["mailboxes_preview"] = [self._mask_email(c["email"]) for c in credentials]
            provider["mailboxes_stats"] = mail_provider.outlook_token_pool_stats(credentials)

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
            for key in ("mailboxes_count", "mailboxes_preview", "mailboxes_stats"):
                provider.pop(key, None)

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
            kept, removed = mail_provider.prune_outlook_unused_credentials(credentials)
            if removed:
                provider["mailboxes"] = _serialize_outlook_pool(kept)
                total_removed += removed
            for key in ("mailboxes_count", "mailboxes_preview", "mailboxes_stats"):
                provider.pop(key, None)
        return total_removed

    def update(self, updates: dict) -> dict:
        with self._lock:
            loaded = self._load()
            if loaded:
                self._config = loaded
            self._merge_outlook_pools(updates)
            self._config = _normalize({**self._config, **updates})
            self._drop_mail_proxy()
            openai_register.config.update({k: self._config[k] for k in ("mail", "proxy", "total", "threads")})
            self._save()
            return self.get()

    def start(self) -> dict:
        with self._lock:
            loaded = self._load()
            if loaded:
                self._config = loaded
            if self._runner and self._runner.is_alive():
                self._config["enabled"] = True
                self._save()
                return self.get()
            lock_owner = runtime_state.acquire_lock(REGISTER_RUN_LOCK, ttl_seconds=REGISTER_RUN_LOCK_TTL_SECONDS)
            if not lock_owner:
                self._config = self._load()
                self._config["enabled"] = True
                self._save()
                return self.get()
            self._lock_owner = lock_owner
            self._config["enabled"] = True
            self._drop_mail_proxy()
            self._config["logs"] = []
            metrics = self._pool_metrics()
            self._config["stats"] = {"job_id": uuid.uuid4().hex, "success": 0, "fail": 0, "done": 0, "running": 0, "threads": self._config["threads"], **metrics, "started_at": _now(), "updated_at": _now()}
            openai_register.config.update({k: self._config[k] for k in ("mail", "proxy", "total", "threads")})
            with openai_register.stats_lock:
                openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": time.time()})
            self._save()
            db = _db_backend()
            if db is not None:
                try:
                    db.save_named_config(REGISTER_RUNTIME_CONFIG_KEY, self._snapshot(runtime_only=True))
                except Exception:
                    pass
            self._runner = threading.Thread(target=self._run, daemon=True, name="openai-register")
            self._runner.start()
            self._append_log(f"注册任务启动，模式={self._config['mode']}，线程数={self._config['threads']}", "yellow")
            return self.get()

    def stop(self) -> dict:
        with self._lock:
            loaded = self._load()
            if loaded:
                self._config = loaded
            self._config["enabled"] = False
            self._config["stats"]["updated_at"] = _now()
            self._save()
            self._append_log("已请求停止注册任务，正在等待当前运行任务结束", "yellow")
            return self.get()

    def reset(self) -> dict:
        with self._lock:
            loaded = self._load()
            if loaded:
                self._config = loaded
            self._config["logs"] = []
            self._config["stats"] = {"success": 0, "fail": 0, "done": 0, "running": 0, "threads": self._config["threads"], "elapsed_seconds": 0, "avg_seconds": 0, "success_rate": 0, **self._pool_metrics(), "updated_at": _now()}
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
                openai_register.config.update({k: self._config[k] for k in ("mail", "proxy", "total", "threads")})
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

    def _append_log(self, text: str, color: str = "") -> None:
        with self._lock:
            logs = self._config.get("logs") if isinstance(self._config.get("logs"), list) else []
            logs.append({"time": _now(), "text": str(text), "level": str(color or "info")})
            self._config["logs"] = logs[-REGISTER_LOG_LIMIT:]
            self._save_runtime()

    def _pool_metrics(self) -> dict:
        return account_service.get_image_pool_metrics()

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
                self._append_log(f"检查号池：当前正常账号={metrics['current_available']}，当前剩余额度={metrics['current_quota']}，目标额度={cfg.get('target_quota')}，{'跳过注册' if reached else '继续注册'}", "yellow")
            return reached
        if mode == "available":
            reached = metrics["current_available"] >= int(cfg.get("target_available") or 1)
            key = "available"
            last_available, last_quota, last_at = self._last_metrics_log.get(key, (-1, -1, 0.0))
            if reached or metrics["current_available"] != last_available or metrics["current_quota"] != last_quota or now - last_at >= 30:
                self._last_metrics_log[key] = (metrics["current_available"], metrics["current_quota"], now)
                self._append_log(f"检查号池：当前正常账号={metrics['current_available']}，目标账号={cfg.get('target_available')}，当前剩余额度={metrics['current_quota']}，{'跳过注册' if reached else '继续注册'}", "yellow")
            return reached
        return submitted >= int(cfg.get("total") or 1)

    def _bump(self, **updates) -> None:
        with self._lock:
            if self._lock_owner:
                runtime_state.extend_lock(
                    REGISTER_RUN_LOCK,
                    self._lock_owner,
                    ttl_seconds=REGISTER_RUN_LOCK_TTL_SECONDS,
                )
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

    def _run(self) -> None:
        with self._lock:
            cfg = self._runtime_cfg_locked()
        threads = int(cfg["threads"])
        submitted, done, success, fail = 0, 0, 0, 0
        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = set()
            while True:
                with self._lock:
                    cfg = self._runtime_cfg_locked()
                while bool(cfg.get("enabled")) and not self._target_reached(cfg, submitted) and len(futures) < threads:
                    submitted += 1
                    futures.add(executor.submit(openai_register.worker, submitted))
                self._bump(running=len(futures), done=done, success=success, fail=fail)
                with self._lock:
                    enabled = bool(self._config.get("enabled"))
                if not futures and (not enabled or str(cfg.get("mode") or "total") == "total"):
                    break
                if not futures:
                    time.sleep(max(1, int(cfg.get("check_interval") or 5)))
                    continue
                finished, futures = wait(futures, return_when=FIRST_COMPLETED)
                for future in finished:
                    done += 1
                    try:
                        result = future.result()
                        success += 1 if result.get("ok") else 0
                        fail += 0 if result.get("ok") else 1
                    except Exception:
                        fail += 1
        self._bump(running=0, done=done, success=success, fail=fail, finished_at=_now())
        with self._lock:
            self._config["enabled"] = False
            self._save()
            self._save_runtime(force=True)
            lock_owner = self._lock_owner
            self._lock_owner = ""
            if lock_owner:
                runtime_state.release_lock(REGISTER_RUN_LOCK, lock_owner)
        self._append_log(f"注册任务结束，成功{success}，失败{fail}", "yellow")


register_service = RegisterService(REGISTER_FILE)
