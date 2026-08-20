from __future__ import annotations

import copy
from dataclasses import dataclass
import json
import os
import sys
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from services.storage.base import StorageBackend

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = BASE_DIR / "config.json"
VERSION_FILE = BASE_DIR / "VERSION"
BACKUP_STATE_FILE = DATA_DIR / "backup_state.json"

DEFAULT_IMAGE_RETENTION_MINUTES = 30 * 24 * 60
MIN_IMAGE_RETENTION_MINUTES = 30
IMAGE_CLEANUP_INTERVAL_SECONDS = 30 * 60
IMAGE_CLEANUP_DB_BATCH_SIZE = 500

DEFAULT_BACKUP_INCLUDE = {
    "config": True,
    "register": True,
    "cpa": True,
    "sub2api": True,
    "logs": True,
    "image_tasks": True,
    "accounts_snapshot": True,
    "auth_keys_snapshot": True,
    "images": False,
}

DEFAULT_IMAGE_STORAGE = {
    "enabled": False,
    "mode": "local",
    "webdav_url": "",
    "webdav_username": "",
    "webdav_password": "",
    "webdav_root_path": "chatgpt2api/images",
    "public_base_url": "",
}

DEFAULT_CHAT_COMPLETION_CACHE = {
    "enabled": True,
    "ttl_seconds": 60,
    "max_entries": 256,
    "dedupe_inflight": True,
    "stream_cache": True,
    "normalize_messages": True,
    "drop_adjacent_duplicates": True,
    "drop_assistant_history": False,
}

DEFAULT_PROXY_RUNTIME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

DEFAULT_PROXY_RUNTIME = {
    "enabled": False,
    "egress_mode": "direct",
    "proxy_url": "",
    "resource_proxy_url": "",
    "account_refresh_proxy_pool": [],
    "skip_ssl_verify": False,
    "reset_session_status_codes": [403],
    "clearance": {
        "enabled": False,
        "mode": "none",
        "cf_cookies": "",
        "cf_clearance": "",
        "user_agent": DEFAULT_PROXY_RUNTIME_USER_AGENT,
        "browser": "chrome",
        "flaresolverr_url": "",
        "timeout_sec": 60,
        "refresh_interval": 3600,
        "warm_up_on_start": False,
    },
}

DEFAULT_THIRD_PARTY_APPS = {
    "infinite_canvas": {
        "enabled": False,
        "url": "https://canvas.best",
    },
}


def _normalize_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        return default
    if value is None:
        return default
    return bool(value)


def _normalize_image_retention_minutes(value: object, default: int) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not number.is_integer():
        return default
    return max(MIN_IMAGE_RETENTION_MINUTES, int(number))


def _normalize_positive_int(value: object, default: int, minimum: int = 0) -> int:
    try:
        normalized = int(value)
    except (OverflowError, TypeError, ValueError):
        normalized = default
    return max(minimum, normalized)


def _normalize_log_levels(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    allowed = {"debug", "info", "warning", "error"}
    return [level for item in value if (level := str(item or "").strip().lower()) in allowed]


def _normalize_backup_include(value: object) -> dict[str, bool]:
    source = value if isinstance(value, dict) else {}
    normalized = dict(DEFAULT_BACKUP_INCLUDE)
    for key in normalized:
        normalized[key] = _normalize_bool(source.get(key), normalized[key])
    return normalized


def _normalize_backup_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "enabled": _normalize_bool(source.get("enabled"), False),
        "provider": "cloudflare_r2",
        "account_id": str(source.get("account_id") or "").strip(),
        "access_key_id": str(source.get("access_key_id") or "").strip(),
        "secret_access_key": str(source.get("secret_access_key") or "").strip(),
        "bucket": str(source.get("bucket") or "").strip(),
        "prefix": str(source.get("prefix") or "backups").strip().strip("/") or "backups",
        "interval_minutes": _normalize_positive_int(source.get("interval_minutes"), 360, 1),
        "rotation_keep": _normalize_positive_int(source.get("rotation_keep"), 10, 0),
        "encrypt": _normalize_bool(source.get("encrypt"), False),
        "passphrase": str(source.get("passphrase") or "").strip(),
        "include": _normalize_backup_include(source.get("include")),
    }


def _normalize_backup_state(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "last_started_at": str(source.get("last_started_at") or "").strip() or None,
        "last_finished_at": str(source.get("last_finished_at") or "").strip() or None,
        "last_status": str(source.get("last_status") or "idle").strip() or "idle",
        "last_error": str(source.get("last_error") or "").strip() or None,
        "last_object_key": str(source.get("last_object_key") or "").strip() or None,
    }


def _normalize_image_storage_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    mode = str(source.get("mode") or "local").strip().lower()
    if mode not in {"local", "webdav", "both"}:
        mode = "local"
    enabled = _normalize_bool(source.get("enabled"), False)
    if not enabled:
        mode = "local"
    root_path = str(source.get("webdav_root_path") or DEFAULT_IMAGE_STORAGE["webdav_root_path"]).strip().strip("/")
    return {
        "enabled": enabled,
        "mode": mode,
        "webdav_url": str(source.get("webdav_url") or "").strip().rstrip("/"),
        "webdav_username": str(source.get("webdav_username") or "").strip(),
        "webdav_password": str(source.get("webdav_password") or "").strip(),
        "webdav_root_path": root_path or str(DEFAULT_IMAGE_STORAGE["webdav_root_path"]),
        "public_base_url": str(source.get("public_base_url") or "").strip().rstrip("/"),
    }


def _normalize_chat_completion_cache_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "enabled": _normalize_bool(source.get("enabled"), DEFAULT_CHAT_COMPLETION_CACHE["enabled"]),
        "ttl_seconds": _normalize_positive_int(
            source.get("ttl_seconds"),
            int(DEFAULT_CHAT_COMPLETION_CACHE["ttl_seconds"]),
            0,
        ),
        "max_entries": _normalize_positive_int(
            source.get("max_entries"),
            int(DEFAULT_CHAT_COMPLETION_CACHE["max_entries"]),
            1,
        ),
        "dedupe_inflight": _normalize_bool(
            source.get("dedupe_inflight"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["dedupe_inflight"]),
        ),
        "stream_cache": _normalize_bool(
            source.get("stream_cache"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["stream_cache"]),
        ),
        "normalize_messages": _normalize_bool(
            source.get("normalize_messages"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["normalize_messages"]),
        ),
        "drop_adjacent_duplicates": _normalize_bool(
            source.get("drop_adjacent_duplicates"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["drop_adjacent_duplicates"]),
        ),
        "drop_assistant_history": _normalize_bool(
            source.get("drop_assistant_history"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["drop_assistant_history"]),
        ),
    }


def _normalize_status_codes(value: object) -> list[int]:
    items = value if isinstance(value, list) else DEFAULT_PROXY_RUNTIME["reset_session_status_codes"]
    normalized: list[int] = []
    for item in items:
        if isinstance(item, bool):
            continue
        try:
            status = int(item)
        except (OverflowError, TypeError, ValueError):
            continue
        if 100 <= status <= 599 and status not in normalized:
            normalized.append(status)
    if not normalized:
        return list(DEFAULT_PROXY_RUNTIME["reset_session_status_codes"])
    return normalized


def _normalize_proxy_runtime_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    default_clearance = DEFAULT_PROXY_RUNTIME["clearance"]
    clearance_source = source.get("clearance") if isinstance(source.get("clearance"), dict) else {}

    egress_mode = str(source.get("egress_mode") or DEFAULT_PROXY_RUNTIME["egress_mode"]).strip().lower()
    if egress_mode not in {"direct", "single_proxy"}:
        egress_mode = str(DEFAULT_PROXY_RUNTIME["egress_mode"])

    clearance_mode = str(clearance_source.get("mode") or default_clearance["mode"]).strip().lower()
    if clearance_mode not in {"none", "manual", "flaresolverr"}:
        clearance_mode = str(default_clearance["mode"])

    user_agent = str(clearance_source.get("user_agent") or default_clearance["user_agent"]).strip()
    browser = str(clearance_source.get("browser") or default_clearance["browser"]).strip()

    existing_clearance_cookies = str(source.get("_existing_cf_cookies") or "").strip()
    existing_cf_clearance = str(source.get("_existing_cf_clearance") or "").strip()
    cf_cookies = str(clearance_source.get("cf_cookies") or "").strip()
    cf_clearance = str(clearance_source.get("cf_clearance") or "").strip()
    if not cf_cookies and _normalize_bool(clearance_source.get("has_cf_cookies"), False):
        cf_cookies = existing_clearance_cookies
    if not cf_clearance and _normalize_bool(clearance_source.get("has_cf_clearance"), False):
        cf_clearance = existing_cf_clearance

    raw_refresh_pool = source.get("account_refresh_proxy_pool")
    if isinstance(raw_refresh_pool, str):
        raw_refresh_pool = raw_refresh_pool.splitlines()
    refresh_pool: list[str] = []
    if isinstance(raw_refresh_pool, (list, tuple)):
        from urllib.parse import urlparse
        for item in raw_refresh_pool:
            candidate = str(item or "").strip()
            parsed = urlparse(candidate)
            if candidate and parsed.scheme.lower() in {"http", "https", "socks5", "socks5h"} and parsed.netloc:
                if candidate not in refresh_pool:
                    refresh_pool.append(candidate)

    return {
        "enabled": _normalize_bool(source.get("enabled"), bool(DEFAULT_PROXY_RUNTIME["enabled"])),
        "egress_mode": egress_mode,
        "proxy_url": str(source.get("proxy_url") or "").strip(),
        "resource_proxy_url": str(source.get("resource_proxy_url") or "").strip(),
        "account_refresh_proxy_pool": refresh_pool,
        "skip_ssl_verify": _normalize_bool(
            source.get("skip_ssl_verify"),
            bool(DEFAULT_PROXY_RUNTIME["skip_ssl_verify"]),
        ),
        "reset_session_status_codes": _normalize_status_codes(source.get("reset_session_status_codes")),
        "clearance": {
            "enabled": _normalize_bool(clearance_source.get("enabled"), bool(default_clearance["enabled"])),
            "mode": clearance_mode,
            "cf_cookies": cf_cookies,
            "cf_clearance": cf_clearance,
            "user_agent": user_agent or str(default_clearance["user_agent"]),
            "browser": browser or str(default_clearance["browser"]),
            "flaresolverr_url": str(clearance_source.get("flaresolverr_url") or "").strip(),
            "timeout_sec": _normalize_positive_int(
                clearance_source.get("timeout_sec"),
                int(default_clearance["timeout_sec"]),
                1,
            ),
            "refresh_interval": _normalize_positive_int(
                clearance_source.get("refresh_interval"),
                int(default_clearance["refresh_interval"]),
                60,
            ),
            "warm_up_on_start": _normalize_bool(
                clearance_source.get("warm_up_on_start"),
                bool(default_clearance["warm_up_on_start"]),
            ),
        },
    }


def _mask_proxy_url(value: object) -> str:
    """Keep a proxy endpoint useful in the UI without exposing credentials."""
    candidate = str(value or "").strip()
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return candidate
    if not parsed.scheme or "@" not in parsed.netloc:
        return candidate
    host = parsed.netloc.rsplit("@", 1)[1]
    return urlunsplit((parsed.scheme, f"***@{host}", parsed.path, parsed.query, parsed.fragment))


def _restore_masked_proxy_pool(incoming: object, existing: object) -> object:
    """Preserve stored credentials when the settings UI posts its redacted copy."""
    values = incoming.splitlines() if isinstance(incoming, str) else incoming
    saved = existing if isinstance(existing, list) else []
    raw_by_mask = {_mask_proxy_url(item): item for item in saved}
    if not isinstance(values, (list, tuple)):
        return incoming
    return [raw_by_mask.get(str(item or "").strip(), item) for item in values]


def _validate_account_refresh_proxy_pool(value: object) -> None:
    values = value.splitlines() if isinstance(value, str) else value
    if values is None:
        return
    if not isinstance(values, (list, tuple)):
        raise ValueError("账号刷新代理池必须是代理 URL 列表")
    for item in values:
        candidate = str(item or "").strip()
        if not candidate:
            continue
        try:
            parsed = urlsplit(candidate)
        except ValueError as exc:
            raise ValueError("账号刷新代理池包含无效代理 URL") from exc
        if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"} or not parsed.netloc:
            raise ValueError("账号刷新代理池仅支持带主机的 http/https/socks5/socks5h URL")


def _normalize_third_party_apps_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    canvas_source = source.get("infinite_canvas") if isinstance(source.get("infinite_canvas"), dict) else {}
    return {
        "infinite_canvas": {
            "enabled": _normalize_bool(canvas_source.get("enabled"), False),
            "url": str(canvas_source.get("url") or DEFAULT_THIRD_PARTY_APPS["infinite_canvas"]["url"]).strip(),
        },
    }


def _validate_image_storage_settings(settings: dict[str, object]) -> None:
    if not _normalize_bool(settings.get("enabled"), False):
        return
    if not str(settings.get("webdav_url") or "").strip():
        raise ValueError("启用 WebDAV 图片存储后必须填写 WebDAV URL")
    if not str(settings.get("webdav_password") or "").strip():
        raise ValueError("启用 WebDAV 图片存储后必须填写 WebDAV 密码")


@dataclass(frozen=True)
class LoadedSettings:
    auth_key: str
    refresh_account_interval_minute: int


def _normalize_auth_key(value: object) -> str:
    return str(value or "").strip()


def _is_invalid_auth_key(value: object) -> bool:
    return _normalize_auth_key(value) == ""


def _read_json_object(path: Path, *, name: str) -> dict[str, object]:
    if not path.exists():
        return {}
    if path.is_dir():
        print(
            f"Warning: {name} at '{path}' is a directory, ignoring it and falling back to other configuration sources.",
            file=sys.stderr,
        )
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_settings() -> LoadedSettings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    raw_config = _read_json_object(CONFIG_FILE, name="config.json")
    auth_key = _normalize_auth_key(os.getenv("CHATGPT2API_AUTH_KEY") or raw_config.get("auth-key"))
    if _is_invalid_auth_key(auth_key):
        raise ValueError(
            "❌ auth-key 未设置！\n"
            "请在环境变量 CHATGPT2API_AUTH_KEY 中设置，或者在 config.json 中填写 auth-key。"
        )

    try:
        refresh_interval = int(raw_config.get("refresh_account_interval_minute", 5))
    except (TypeError, ValueError):
        refresh_interval = 5

    return LoadedSettings(
        auth_key=auth_key,
        refresh_account_interval_minute=refresh_interval,
    )


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._data = self._load()
        self._file_signature = self._stat_signature()
        self._storage_backend: StorageBackend | None = None
        self._image_cleanup_lock = threading.Lock()
        self._last_image_cleanup_at = 0.0
        if _is_invalid_auth_key(self.auth_key):
            raise ValueError(
                "❌ auth-key 未设置！\n"
                "请按以下任意一种方式解决：\n"
                "1. 在 Render 的 Environment 变量中添加：\n"
                "   CHATGPT2API_AUTH_KEY = your_real_auth_key\n"
                "2. 或者在 config.json 中填写：\n"
                '   "auth-key": "your_real_auth_key"'
            )

    def _load(self) -> dict[str, object]:
        return _read_json_object(self.path, name="config.json")

    def _stat_signature(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    @property
    def data(self) -> dict[str, object]:
        self.reload_if_changed()
        return self._data

    @data.setter
    def data(self, value: dict[str, object]) -> None:
        with self._lock:
            self._data = value
            self._file_signature = self._stat_signature()

    def reload_if_changed(self, *, force: bool = False) -> None:
        with self._lock:
            signature = self._stat_signature()
            if not force and signature == self._file_signature:
                return
            self._data = self._load()
            self._file_signature = self._stat_signature()

    def _save(self, data: dict[str, object] | None = None) -> None:
        payload = self._data if data is None else data
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self._data = payload
        self._file_signature = self._stat_signature()

    def _normalize_proxy_runtime_for_update(
        self,
        value: object,
        base_data: dict[str, object],
    ) -> dict[str, object]:
        if isinstance(value, dict):
            incoming = dict(value)
            previous_runtime = _normalize_proxy_runtime_settings(base_data.get("proxy_runtime"))
            previous_clearance = previous_runtime.get("clearance")
            if isinstance(previous_clearance, dict):
                incoming["_existing_cf_cookies"] = previous_clearance.get("cf_cookies")
                incoming["_existing_cf_clearance"] = previous_clearance.get("cf_clearance")
            incoming["account_refresh_proxy_pool"] = _restore_masked_proxy_pool(
                incoming.get("account_refresh_proxy_pool"),
                previous_runtime.get("account_refresh_proxy_pool"),
            )
            _validate_account_refresh_proxy_pool(incoming.get("account_refresh_proxy_pool"))
            return _normalize_proxy_runtime_settings(incoming)
        return _normalize_proxy_runtime_settings(value)

    def _is_stale_update_value(
        self,
        key: str,
        value: object,
        previous_data: dict[str, object],
        latest_data: dict[str, object],
    ) -> bool:
        if key == "proxy_runtime":
            previous_value = self._normalize_proxy_runtime_for_update(value, previous_data)
            latest_value = self._normalize_proxy_runtime_for_update(value, latest_data)
            previous_current = _normalize_proxy_runtime_settings(previous_data.get("proxy_runtime"))
            latest_current = _normalize_proxy_runtime_settings(latest_data.get("proxy_runtime"))
            return previous_value == previous_current and latest_value != latest_current
        if key == "log_levels":
            incoming_value = _normalize_log_levels(value)
            previous_value = _normalize_log_levels(previous_data.get("log_levels"))
            latest_value = _normalize_log_levels(latest_data.get("log_levels"))
            return incoming_value == previous_value and incoming_value != latest_value
        if key == "image_poll_timeout_secs":
            incoming_value = _normalize_positive_int(value, 120, 1)
            previous_value = _normalize_positive_int(previous_data.get("image_poll_timeout_secs"), 120, 1)
            latest_value = _normalize_positive_int(latest_data.get("image_poll_timeout_secs"), 120, 1)
            return incoming_value == previous_value and incoming_value != latest_value
        return previous_data.get(key) == value and latest_data.get(key) != previous_data.get(key)

    def _drop_stale_update_values(
        self,
        incoming: dict[str, object],
        previous_data: dict[str, object],
        latest_data: dict[str, object],
    ) -> dict[str, object]:
        protected_keys = {"proxy_runtime", "log_levels", "image_poll_timeout_secs"}
        result = dict(incoming)
        for key in protected_keys.intersection(result):
            if self._is_stale_update_value(key, result[key], previous_data, latest_data):
                result.pop(key, None)
        return result

    @property
    def auth_key(self) -> str:
        return _normalize_auth_key(os.getenv("CHATGPT2API_AUTH_KEY") or self.data.get("auth-key"))

    @property
    def accounts_file(self) -> Path:
        return DATA_DIR / "accounts.json"

    @property
    def refresh_account_interval_minute(self) -> int:
        try:
            return int(self.data.get("refresh_account_interval_minute", 5))
        except (TypeError, ValueError):
            return 5

    @property
    def full_account_refresh_enabled(self) -> bool:
        return _normalize_bool(self.data.get("full_account_refresh_enabled"), True)

    @property
    def image_retention_minutes(self) -> int:
        value = self.data.get("image_retention_minutes")
        if value is not None:
            return _normalize_image_retention_minutes(value, MIN_IMAGE_RETENTION_MINUTES)
        try:
            legacy_minutes = float(self.data.get("image_retention_days", 30)) * 24 * 60
        except (TypeError, ValueError):
            return DEFAULT_IMAGE_RETENTION_MINUTES
        return _normalize_image_retention_minutes(legacy_minutes, DEFAULT_IMAGE_RETENTION_MINUTES)

    @property
    def image_retention_days(self) -> float:
        """Backward-compatible view for older API clients."""
        return self.image_retention_minutes / (24 * 60)

    @property
    def image_poll_timeout_secs(self) -> int:
        try:
            return max(1, int(self.data.get("image_poll_timeout_secs", 120)))
        except (TypeError, ValueError):
            return 120

    @property
    def image_poll_interval_secs(self) -> float:
        try:
            return max(0.5, float(self.data.get("image_poll_interval_secs", 10.0)))
        except (TypeError, ValueError):
            return 10.0

    @property
    def image_poll_initial_wait_secs(self) -> float:
        """Image generation upstream takes ~30s; polling immediately wastes requests
        and trips a transient 429. Default 10s gives the conversation document time
        to commit before the first poll."""
        try:
            return max(0.0, float(self.data.get("image_poll_initial_wait_secs", 10.0)))
        except (TypeError, ValueError):
            return 10.0

    @property
    def image_account_concurrency(self) -> int:
        try:
            return max(1, int(self.data.get("image_account_concurrency", 3)))
        except (TypeError, ValueError):
            return 3

    @property
    def image_parallel_generation(self) -> bool:
        value = self.data.get("image_parallel_generation", True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def image_settle_enabled(self) -> bool:
        """图片二次确认机制：找到 file_ids 后等待一段时间再次确认。"""
        value = self.data.get("image_settle_enabled", True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def image_check_before_hit_enabled(self) -> bool:
        """先check再hit：通过轮询确认 file_ids 存在后再返回，而非仅依赖 SSE 事件。"""
        value = self.data.get("image_check_before_hit_enabled", True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def image_settle_secs(self) -> float:
        """二次确认等待时间（秒）。"""
        try:
            return max(0.5, float(self.data.get("image_settle_secs", 2.0)))
        except (TypeError, ValueError):
            return 2.0

    @property
    def auto_remove_invalid_accounts(self) -> bool:
        value = self.data.get("auto_remove_invalid_accounts", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def auto_remove_rate_limited_accounts(self) -> bool:
        value = self.data.get("auto_remove_rate_limited_accounts", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def auto_relogin_after_refresh(self) -> bool:
        value = self.data.get("auto_relogin_after_refresh", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def log_levels(self) -> list[str]:
        return _normalize_log_levels(self.data.get("log_levels"))

    @property
    def sensitive_words(self) -> list[str]:
        words = self.data.get("sensitive_words")
        return [word for item in words if (word := str(item or "").strip())] if isinstance(words, list) else []

    @property
    def ai_review(self) -> dict[str, object]:
        value = self.data.get("ai_review")
        return value if isinstance(value, dict) else {}

    @property
    def global_system_prompt(self) -> str:
        return str(self.data.get("global_system_prompt") or "").strip()

    @property
    def images_dir(self) -> Path:
        path = DATA_DIR / "images"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def image_thumbnails_dir(self) -> Path:
        path = DATA_DIR / "image_thumbnails"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def cleanup_old_images(self, *, force: bool = False) -> int:
        with self._image_cleanup_lock:
            now = time.monotonic()
            if not force and now - self._last_image_cleanup_at < IMAGE_CLEANUP_INTERVAL_SECONDS:
                return 0

            cutoff = time.time() - self.image_retention_minutes * 60
            removed = 0
            backend: StorageBackend | None = None
            try:
                candidate = self.get_storage_backend()
                if candidate.supports_database_features():
                    backend = candidate
            except Exception:
                pass

            removed_rels: list[str] = []

            def flush_removed_records() -> None:
                if not backend or not removed_rels:
                    return
                batch = list(removed_rels)
                removed_rels.clear()
                try:
                    backend.delete_image_records(batch)
                except Exception:
                    pass

            root = self.images_dir
            for path in root.rglob("*"):
                try:
                    if not path.is_file() or path.stat().st_mtime >= cutoff:
                        continue
                    rel = path.relative_to(root).as_posix()
                    path.unlink()
                except (FileNotFoundError, OSError, ValueError):
                    continue
                removed += 1
                if backend is not None:
                    removed_rels.append(rel)
                    if len(removed_rels) >= IMAGE_CLEANUP_DB_BATCH_SIZE:
                        flush_removed_records()
            flush_removed_records()

            for path in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                try:
                    path.rmdir()
                except OSError:
                    pass
            self._last_image_cleanup_at = time.monotonic()
            return removed

    @property
    def base_url(self) -> str:
        return str(
            self.data.get("base_url")
            or os.getenv("CHATGPT2API_BASE_URL")
            or ""
        ).strip().rstrip("/")

    @property
    def app_version(self) -> str:
        try:
            value = VERSION_FILE.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return "0.0.0"
        return value or "0.0.0"

    def get(self) -> dict[str, object]:
        data = dict(self.data)
        data["refresh_account_interval_minute"] = self.refresh_account_interval_minute
        data["full_account_refresh_enabled"] = self.full_account_refresh_enabled
        data["image_retention_minutes"] = self.image_retention_minutes
        data["image_retention_days"] = self.image_retention_days
        data["image_poll_timeout_secs"] = self.image_poll_timeout_secs
        data["image_poll_interval_secs"] = self.image_poll_interval_secs
        data["image_poll_initial_wait_secs"] = self.image_poll_initial_wait_secs
        data["image_account_concurrency"] = self.image_account_concurrency
        data["image_parallel_generation"] = self.image_parallel_generation
        data["auto_remove_invalid_accounts"] = self.auto_remove_invalid_accounts
        data["auto_remove_rate_limited_accounts"] = self.auto_remove_rate_limited_accounts
        data["auto_relogin_after_refresh"] = self.auto_relogin_after_refresh
        data["log_levels"] = self.log_levels
        data["sensitive_words"] = self.sensitive_words
        data["ai_review"] = self.ai_review
        data["global_system_prompt"] = self.global_system_prompt
        data["backup"] = self.get_backup_settings()
        data["image_storage"] = self.get_image_storage_settings()
        data["chat_completion_cache"] = self.get_chat_completion_cache_settings()
        data["proxy_runtime"] = self.get_public_proxy_runtime_settings()
        data["third_party_apps"] = self.get_third_party_apps_settings()
        data.pop("auth-key", None)
        return data

    def get_proxy_settings(self) -> str:
        return str(self.data.get("proxy") or "").strip()

    def get_proxy_runtime_settings(self) -> dict[str, object]:
        return _normalize_proxy_runtime_settings(self.data.get("proxy_runtime"))

    def get_public_proxy_runtime_settings(self) -> dict[str, object]:
        runtime = copy.deepcopy(self.get_proxy_runtime_settings())
        pool = runtime.get("account_refresh_proxy_pool")
        if isinstance(pool, list):
            runtime["account_refresh_proxy_pool"] = [_mask_proxy_url(item) for item in pool]
        clearance = runtime.get("clearance") if isinstance(runtime.get("clearance"), dict) else {}
        if isinstance(clearance, dict):
            cf_cookies = str(clearance.get("cf_cookies") or "").strip()
            cf_clearance = str(clearance.get("cf_clearance") or "").strip()
            clearance["cf_cookies"] = ""
            clearance["cf_clearance"] = ""
            clearance["has_cf_cookies"] = bool(cf_cookies)
            clearance["has_cf_clearance"] = bool(cf_clearance)
        return runtime

    def get_third_party_apps_settings(self) -> dict[str, object]:
        return _normalize_third_party_apps_settings(self.data.get("third_party_apps"))

    def update(self, data: dict[str, object]) -> dict[str, object]:
        with self._lock:
            previous_data = copy.deepcopy(self._data)
            latest_data = self._load()
            self._data = latest_data
            self._file_signature = self._stat_signature()

            incoming = self._drop_stale_update_values(dict(data or {}), previous_data, latest_data)
            if "image_retention_minutes" in incoming:
                incoming["image_retention_minutes"] = _normalize_image_retention_minutes(
                    incoming.get("image_retention_minutes"),
                    MIN_IMAGE_RETENTION_MINUTES,
                )
                incoming.pop("image_retention_days", None)
            elif "image_retention_days" in incoming:
                try:
                    legacy_minutes = float(incoming.get("image_retention_days", 30)) * 24 * 60
                except (TypeError, ValueError):
                    legacy_minutes = DEFAULT_IMAGE_RETENTION_MINUTES
                incoming["image_retention_minutes"] = _normalize_image_retention_minutes(
                    legacy_minutes,
                    DEFAULT_IMAGE_RETENTION_MINUTES,
                )
                incoming.pop("image_retention_days", None)
            next_data = dict(latest_data)
            next_data.update(incoming)
            if "image_retention_minutes" in next_data:
                next_data.pop("image_retention_days", None)
            if "backup" in next_data:
                next_data["backup"] = _normalize_backup_settings(next_data.get("backup"))
            if "image_storage" in next_data:
                next_data["image_storage"] = _normalize_image_storage_settings(next_data.get("image_storage"))
                _validate_image_storage_settings(next_data["image_storage"])
            if "chat_completion_cache" in next_data:
                next_data["chat_completion_cache"] = _normalize_chat_completion_cache_settings(
                    next_data.get("chat_completion_cache")
                )
            if "third_party_apps" in next_data:
                next_data["third_party_apps"] = _normalize_third_party_apps_settings(next_data.get("third_party_apps"))
            if "proxy_runtime" in next_data:
                next_data["proxy_runtime"] = self._normalize_proxy_runtime_for_update(
                    next_data.get("proxy_runtime"),
                    latest_data,
                )
            next_data.pop("backup_state", None)
            self._save(next_data)
            return self.get()

    def get_backup_settings(self) -> dict[str, object]:
        return _normalize_backup_settings(self.data.get("backup"))

    def get_image_storage_settings(self) -> dict[str, object]:
        return _normalize_image_storage_settings(self.data.get("image_storage"))

    def get_chat_completion_cache_settings(self) -> dict[str, object]:
        return _normalize_chat_completion_cache_settings(self.data.get("chat_completion_cache"))

    def get_storage_backend(self) -> StorageBackend:
        """获取存储后端实例（单例）"""
        if self._storage_backend is None:
            from services.storage.factory import create_storage_backend
            self._storage_backend = create_storage_backend(DATA_DIR)
        return self._storage_backend


def load_backup_state() -> dict[str, object]:
    return _normalize_backup_state(_read_json_object(BACKUP_STATE_FILE, name="backup_state.json"))


def save_backup_state(state: dict[str, object]) -> dict[str, object]:
    normalized = _normalize_backup_state(state)
    BACKUP_STATE_FILE.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return normalized


config = ConfigStore(CONFIG_FILE)
