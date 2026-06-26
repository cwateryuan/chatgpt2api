from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.register_service import REGISTER_RUNTIME_CONFIG_KEY, RegisterService


class FakeRuntimeState:
    def acquire_lock(self, *_args, **_kwargs):
        return "owner"

    def extend_lock(self, *_args, **_kwargs):
        return True

    def release_lock(self, *_args, **_kwargs):
        return None


class FakeDB:
    def __init__(self):
        self.configs: dict[str, dict] = {}
        self.saved: list[tuple[str, dict]] = []

    def supports_database_features(self) -> bool:
        return True

    def load_named_config(self, key: str):
        return self.configs.get(key)

    def save_named_config(self, key: str, data: dict) -> None:
        self.configs[key] = dict(data)
        self.saved.append((key, dict(data)))


class RegisterServiceMemoryTests(unittest.TestCase):
    def test_append_log_persists_runtime_only(self):
        db = FakeDB()
        db.configs["register"] = {
            "mail": {
                "providers": [
                    {
                        "type": "outlook_token",
                        "enable": True,
                        "mailboxes": "a@example.com----p----cid----" + "r" * 10000,
                    }
                ]
            },
            "enabled": False,
            "threads": 1,
            "logs": [],
            "stats": {},
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("services.register_service._db_backend", return_value=db),
            mock.patch("services.register_service.runtime_state", FakeRuntimeState()),
        ):
            service = RegisterService(Path(tmp) / "register.json")
            service._last_save_at = 0
            service._append_log("hello", "green")

        runtime_saves = [item for item in db.saved if item[0] == REGISTER_RUNTIME_CONFIG_KEY]
        self.assertTrue(runtime_saves)
        self.assertNotIn("mail", runtime_saves[-1][1])
        self.assertEqual(runtime_saves[-1][1]["logs"][-1]["text"], "hello")
        self.assertNotIn("mailboxes", str(runtime_saves[-1][1]))

    def test_pool_metrics_uses_lightweight_account_metrics(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("services.register_service.account_service") as account_service:
            account_service.get_image_pool_metrics.return_value = {"current_quota": 7, "current_available": 2}
            account_service.list_accounts.side_effect = AssertionError("must not load full accounts")
            service = RegisterService(Path(tmp) / "register.json")

            self.assertEqual(service._pool_metrics(), {"current_quota": 7, "current_available": 2})


if __name__ == "__main__":
    unittest.main()
