from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import nullcontext
from typing import Any


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

    def list_image_candidate_accounts(self, excluded_tokens: list[str] | set[str] | None = None) -> list[dict[str, Any]]:
        excluded = {str(token or "").strip() for token in (excluded_tokens or []) if str(token or "").strip()}
        return [
            item
            for item in self.load_accounts()
            if str(item.get("access_token") or item.get("accessToken") or "").strip() not in excluded
        ]

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

    def save_app_config(self, key: str, data: dict[str, Any]) -> None:
        raise NotImplementedError

    def load_app_config(self, key: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def app_config_write_lock(self, key: str):
        return nullcontext()
