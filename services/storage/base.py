from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, TypedDict


class CandidateToken(TypedDict):
    """Minimal account metadata used by the image scheduler."""

    access_token: str
    status: str
    type: str
    source_type: str
    quota: int
    image_quota_unknown: bool


class StorageBackend(ABC):
    """抽象存储后端基类"""

    @abstractmethod
    def load_accounts(self) -> list[dict[str, Any]]:
        """加载所有账号数据"""
        pass

    @abstractmethod
    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        """保存所有账号数据"""
        pass

    @abstractmethod
    def load_auth_keys(self) -> list[dict[str, Any]]:
        """加载所有鉴权密钥数据"""
        pass

    @abstractmethod
    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        """保存所有鉴权密钥数据"""
        pass

    def upsert_auth_key(self, item: dict[str, Any]) -> None:
        raise NotImplementedError

    def delete_auth_key_ids(self, key_ids: list[str]) -> int:
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> dict[str, Any]:
        """健康检查，返回存储后端状态"""
        pass

    @abstractmethod
    def get_backend_info(self) -> dict[str, Any]:
        """获取存储后端信息"""
        pass

    # Optional high-volume storage APIs. Backends that do not implement these
    # methods should keep returning falsy values so callers can fall back to the
    # existing JSON file paths.

    def supports_database_features(self) -> bool:
        return False

    def upsert_account(self, account: dict[str, Any]) -> None:
        raise NotImplementedError

    def delete_account_tokens(self, tokens: list[str]) -> int:
        raise NotImplementedError

    def get_account(self, access_token: str) -> dict[str, Any] | None:
        return None

    def mutate_account(
        self,
        access_token: str,
        mutator: Callable[[dict[str, Any] | None], dict[str, Any] | None],
    ) -> dict[str, Any] | None:
        """Read, transform, and persist one account.

        Database-backed implementations can override this with a row-locked
        transaction.  The fallback keeps custom/legacy storage backends
        compatible with the existing read-modify-write behavior.
        """
        current = self.get_account(access_token)
        updated = mutator(dict(current) if isinstance(current, dict) else None)
        if updated is not None:
            self.upsert_account(updated)
        return updated

    def list_image_cooldown_candidates(self, *, now: float, after: tuple[float, str],
                                       limit: int = 128, source_type: str = "",
                                       plan_type: str = "", plan_types: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        """Database-only bounded candidate page, ordered by cooldown and token hash."""
        raise NotImplementedError

    def get_image_cooldown_metrics(self, now: float, cooldown_minutes: int = 60) -> dict[str, Any]:
        """Database-only aggregate; legacy pool metrics keep their original meaning."""
        raise NotImplementedError

    def recalculate_image_cooldowns(
        self, *, old_minutes: int, new_minutes: int, changed_at: float,
    ) -> int:
        """Recalculate active image cooldowns in one backend operation."""
        if new_minutes <= 0:
            return 0
        from services.image_cooldown import cooldown_started_at, cooldown_until
        accounts = self.load_accounts()
        changed = 0
        for account in accounts:
            until = cooldown_until(account)
            if until <= changed_at:
                continue
            started = cooldown_started_at(account)
            if not started:
                started = until - old_minutes * 60 if old_minutes > 0 else changed_at
            account["image_cooldown_started_at"] = started
            account["image_cooldown_until"] = started + new_minutes * 60
            changed += 1
        if changed:
            self.save_accounts(accounts)
        return changed

    def clear_image_cooldowns(self) -> int:
        """Clear all persisted image cooldown timestamps in one backend operation."""
        from services.image_cooldown import cooldown_started_at, cooldown_until
        accounts = self.load_accounts()
        changed = 0
        for account in accounts:
            if cooldown_until(account) <= 0 and cooldown_started_at(account) <= 0:
                continue
            account["image_cooldown_until"] = 0
            account["image_cooldown_started_at"] = 0
            changed += 1
        if changed:
            self.save_accounts(accounts)
        return changed

    def list_image_candidate_accounts(self, excluded_tokens: list[str] | set[str] | None = None) -> list[dict[str, Any]]:
        excluded = {str(token or "").strip() for token in (excluded_tokens or []) if str(token or "").strip()}
        return [
            item
            for item in self.load_accounts()
            if str(item.get("access_token") or item.get("accessToken") or "").strip() not in excluded
        ]

    def list_image_candidate_tokens(
        self,
        plan_type: str | None = None,
        source_type: str | None = None,
        plan_types: set[str] | tuple[str, ...] | None = None,
    ) -> list[CandidateToken]:
        """Return lightweight image candidates for high-volume selection.

        Database-backed implementations should filter and project this query at
        the storage layer.  The base fallback keeps the JSON backend compatible
        without changing its existing account semantics.
        """
        normalized_plan = str(plan_type or "").strip().lower()
        normalized_source = str(source_type or "").strip().lower()
        normalized_plans = {
            str(item or "").strip().lower()
            for item in (plan_types or ())
            if str(item or "").strip()
        }
        items: list[CandidateToken] = []
        for raw in self.load_accounts():
            token = str(raw.get("access_token") or raw.get("accessToken") or "").strip()
            if not token:
                continue
            status = str(raw.get("status") or "正常").strip()
            if status in {"禁用", "限流", "异常"}:
                continue
            unknown = bool(raw.get("image_quota_unknown"))
            try:
                quota = int(raw.get("quota") or 0)
            except (TypeError, ValueError):
                quota = 0
            if not unknown and quota <= 0:
                continue
            account_type = str(raw.get("type") or "free").strip().lower()
            account_source = str(raw.get("source_type") or "web").strip().lower()
            if normalized_plan and account_type != normalized_plan:
                continue
            if normalized_plans and account_type not in normalized_plans:
                continue
            if normalized_source and account_source != normalized_source:
                continue
            items.append({
                "access_token": token,
                "status": status,
                "source_type": account_source,
                "type": account_type,
                "quota": quota,
                "image_quota_unknown": unknown,
            })
        return items

    def get_image_pool_metrics(self) -> dict[str, int] | None:
        """Return fresh aggregate image-pool metrics when the backend supports it."""
        return None

    def save_image(self, item: dict[str, Any]) -> None:
        raise NotImplementedError

    def get_image(self, rel: str) -> dict[str, Any] | None:
        return None

    def list_images(
        self,
        *,
        start_date: str = "",
        end_date: str = "",
        tag: str = "",
        q: str = "",
        page: int = 1,
        page_size: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        raise NotImplementedError

    def delete_image_records(self, rels: list[str]) -> int:
        raise NotImplementedError

    def image_storage_stats(self) -> dict[str, Any]:
        raise NotImplementedError

    def load_image_tags(self) -> dict[str, list[str]]:
        raise NotImplementedError

    def set_image_tags(self, image_rel: str, tags: list[str]) -> list[str]:
        raise NotImplementedError

    def remove_image_tags(self, image_rel: str) -> None:
        raise NotImplementedError

    def remove_many_image_tags(self, image_rels: list[str] | set[str]) -> None:
        raise NotImplementedError

    def delete_image_tag(self, tag: str) -> int:
        raise NotImplementedError

    def list_image_tags(self) -> list[str]:
        raise NotImplementedError

    def add_log(self, item: dict[str, Any]) -> None:
        raise NotImplementedError

    def list_logs(
        self,
        *,
        type: str = "",
        start_date: str = "",
        end_date: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def delete_logs(self, ids: list[str]) -> int:
        raise NotImplementedError

    def save_named_items(self, table: str, items: list[dict[str, Any]], key_field: str = "id") -> None:
        raise NotImplementedError

    def load_named_items(self, table: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def save_named_config(self, key: str, data: dict[str, Any]) -> None:
        raise NotImplementedError

    def load_named_config(self, key: str) -> dict[str, Any] | None:
        raise NotImplementedError
