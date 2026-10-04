from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any

from services.image_cooldown import cooldown_started_at, cooldown_until
from services.storage.base import StorageBackend


class JSONStorageBackend(StorageBackend):
    """本地 JSON 文件存储后端"""

    def __init__(self, file_path: Path, auth_keys_path: Path | None = None):
        self.file_path = file_path
        self.auth_keys_path = auth_keys_path or file_path.with_name("auth_keys.json")
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.auth_keys_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    @staticmethod
    def _load_json_list(file_path: Path) -> list[dict[str, Any]]:
        if not file_path.exists():
            return []
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, Exception):
            return []

    @staticmethod
    def _save_json_list(file_path: Path, items: list[dict[str, Any]]) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            json.dumps(items, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def load_accounts(self) -> list[dict[str, Any]]:
        """从 JSON 文件加载账号数据"""
        with self._lock:
            return self._load_json_list(self.file_path)

    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        """保存账号数据到 JSON 文件"""
        with self._lock:
            self._save_json_list(self.file_path, accounts)

    def recalculate_image_cooldowns(
        self, *, old_minutes: int, new_minutes: int, changed_at: float,
    ) -> int:
        if new_minutes <= 0:
            return 0
        with self._lock:
            accounts = self._load_json_list(self.file_path)
            count = 0
            window_start = float(changed_at)
            for account in accounts:
                until = cooldown_until(account)
                if until <= window_start:
                    continue
                started = cooldown_started_at(account)
                if not started:
                    started = until - old_minutes * 60 if old_minutes > 0 else window_start
                account["image_cooldown_started_at"] = started
                account["image_cooldown_until"] = started + new_minutes * 60
                count += 1
            if count:
                self._save_json_list(self.file_path, accounts)
            return count

    def clear_image_cooldowns(self) -> int:
        with self._lock:
            accounts = self._load_json_list(self.file_path)
            count = 0
            for account in accounts:
                if cooldown_until(account) <= 0 and cooldown_started_at(account) <= 0:
                    continue
                account["image_cooldown_until"] = 0
                account["image_cooldown_started_at"] = 0
                count += 1
            if count:
                self._save_json_list(self.file_path, accounts)
            return count

    def load_auth_keys(self) -> list[dict[str, Any]]:
        """从 JSON 文件加载鉴权密钥数据"""
        if not self.auth_keys_path.exists():
            return []
        try:
            data = json.loads(self.auth_keys_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception):
            return []
        if isinstance(data, dict):
            data = data.get("items")
        return data if isinstance(data, list) else []

    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        """保存鉴权密钥数据到 JSON 文件"""
        self.auth_keys_path.parent.mkdir(parents=True, exist_ok=True)
        self.auth_keys_path.write_text(
            json.dumps({"items": auth_keys}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def health_check(self) -> dict[str, Any]:
        """健康检查"""
        try:
            # 检查文件是否可读写
            if self.file_path.exists():
                self.file_path.read_text(encoding="utf-8")
            return {
                "status": "healthy",
                "backend": "json",
                "file_exists": self.file_path.exists(),
                "file_path": str(self.file_path),
                "auth_keys_file_exists": self.auth_keys_path.exists(),
                "auth_keys_file_path": str(self.auth_keys_path),
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "backend": "json",
                "error": str(e),
            }

    def get_backend_info(self) -> dict[str, Any]:
        """获取存储后端信息"""
        return {
            "type": "json",
            "description": "本地 JSON 文件存储",
            "file_path": str(self.file_path),
            "file_exists": self.file_path.exists(),
            "auth_keys_file_path": str(self.auth_keys_path),
            "auth_keys_file_exists": self.auth_keys_path.exists(),
        }
