from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.register_service import REGISTER_RUNTIME_CONFIG_KEY, RegisterService


class FakeRuntimeState:
    def __init__(self):
        self.flags: dict[str, str] = {}

    def acquire_lock(self, *_args, **_kwargs):
        return "owner"

    def extend_lock(self, *_args, **_kwargs):
        return True

    def release_lock(self, *_args, **_kwargs):
        return None

    def set_flag(self, key: str, value: str = "1", **_kwargs):
        self.flags[key] = value

    def get_flag(self, key: str):
        return self.flags.get(key, "")

    def delete_flag(self, key: str):
        self.flags.pop(key, None)


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

    def test_get_merges_persisted_runtime_state(self):
        db = FakeDB()
        db.configs["register"] = {"enabled": False, "threads": 1, "logs": [], "stats": {"running": 0}}
        db.configs[REGISTER_RUNTIME_CONFIG_KEY] = {
            "enabled": True,
            "threads": 1,
            "logs": [{"time": "now", "text": "running", "level": "yellow"}],
            "stats": {"running": 3, "done": 4},
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("services.register_service._db_backend", return_value=db),
            mock.patch("services.register_service.runtime_state", FakeRuntimeState()),
        ):
            service = RegisterService(Path(tmp) / "register.json")
            snapshot = service.get()

        self.assertTrue(snapshot["enabled"])
        self.assertEqual(snapshot["stats"]["running"], 3)
        self.assertEqual(snapshot["logs"][-1]["text"], "running")

    def test_owner_runtime_snapshot_applies_shared_stop_flag(self):
        runtime_state = FakeRuntimeState()
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("services.register_service._db_backend", return_value=None),
            mock.patch("services.register_service.runtime_state", runtime_state),
        ):
            service = RegisterService(Path(tmp) / "register.json")
            service._lock_owner = "owner"
            service._config["enabled"] = True
            service._config["stats"]["running"] = 2
            runtime_state.set_flag("register:stop_requested")
            snapshot = service.runtime_snapshot()

        self.assertFalse(snapshot["enabled"])
        self.assertEqual(snapshot["stats"]["running"], 2)


if __name__ == "__main__":
    unittest.main()
