from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.register_service import REGISTER_RUNTIME_CONFIG_KEY, RegisterService


class FakeRuntimeState:
    def __init__(self):
        self.flags: dict[str, str] = {}
        self.lock_available = True
        self.extend_ok = True
        self.acquire_calls = 0

    def acquire_lock(self, *_args, **_kwargs):
        self.acquire_calls += 1
        return "owner" if self.lock_available else ""

    def extend_lock(self, *_args, **_kwargs):
        return self.extend_ok

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

    def test_supervisor_waits_for_old_lock_then_recovers_without_resetting_logs(self):
        runtime_state = FakeRuntimeState()
        runtime_state.lock_available = False
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("services.register_service._db_backend", return_value=None),
            mock.patch("services.register_service.runtime_state", runtime_state),
            mock.patch.object(RegisterService, "_run", lambda self: None),
        ):
            path = Path(tmp) / "register.json"
            service = RegisterService(path)
            service._config["enabled"] = True
            service._config["threads"] = 1
            service._config["logs"] = [{"time": "now", "text": "keep me", "level": "yellow"}]
            service._config["stats"] = {"success": 3, "fail": 1, "done": 4, "running": 0, "threads": 1}
            service._save()

            waiting = service._supervise_once()
            self.assertEqual(waiting["state"], "waiting_lock")
            self.assertIsNone(service._runner)

            runtime_state.lock_available = True
            recovered = service._supervise_once()
            self.assertEqual(recovered["state"], "recovered")
            self.assertEqual(service._lock_owner, "owner")
            snapshot = service.get()
            texts = [item["text"] for item in snapshot["logs"]]
            self.assertIn("keep me", texts)
            self.assertEqual(snapshot["stats"]["success"], 3)
            from services.register_service import openai_register

            self.assertEqual(openai_register.stats["success"], 3)

    def test_start_keeps_enabled_when_old_lock_blocks_runner(self):
        runtime_state = FakeRuntimeState()
        runtime_state.lock_available = False
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("services.register_service._db_backend", return_value=None),
            mock.patch("services.register_service.runtime_state", runtime_state),
        ):
            service = RegisterService(Path(tmp) / "register.json")
            snapshot = service.start()

        self.assertTrue(snapshot["enabled"])
        self.assertIsNone(service._runner)

    def test_supervisor_does_not_restart_after_user_stop(self):
        runtime_state = FakeRuntimeState()
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("services.register_service._db_backend", return_value=None),
            mock.patch("services.register_service.runtime_state", runtime_state),
            mock.patch.object(RegisterService, "_run", lambda self: None),
        ):
            service = RegisterService(Path(tmp) / "register.json")
            service._config["enabled"] = True
            service._save()
            runtime_state.set_flag("register:stop_requested")

            state = service._supervise_once()

        self.assertEqual(state["state"], "stopped")
        self.assertEqual(runtime_state.acquire_calls, 0)

    def test_bump_marks_lock_lost_when_extend_fails(self):
        runtime_state = FakeRuntimeState()
        runtime_state.extend_ok = False
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("services.register_service._db_backend", return_value=None),
            mock.patch("services.register_service.runtime_state", runtime_state),
        ):
            service = RegisterService(Path(tmp) / "register.json")
            service._lock_owner = "owner"
            service._config["enabled"] = True
            service._bump(running=0)

        self.assertTrue(service._lock_lost)
        self.assertFalse(service._config["enabled"])


if __name__ == "__main__":
    unittest.main()
