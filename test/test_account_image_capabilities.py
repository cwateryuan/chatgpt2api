from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services.account_service import AccountService
from services.auth_service import AuthService
from services.config import config
from services.image_timeout import ImageDeadlineExpired, ImageRequestDeadline
from services.openai_backend_api import InvalidAccessTokenError
from services.runtime_state import runtime_state
from services.storage.database_storage import AccountModel, DatabaseStorageBackend
from services.storage.json_storage import JSONStorageBackend
from utils.helper import anonymize_token, split_image_model


class AccountCapabilityTests(unittest.TestCase):
    def test_remote_refresh_candidates_skip_limited_accounts_until_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {
                        "access_token": "limited-future",
                        "status": "限流",
                        "quota": 0,
                        "restore_at": "2099-01-01T00:00:00+00:00",
                        "last_remote_refresh_at": "2026-08-19T00:00:00+00:00",
                    },
                    {
                        "access_token": "limited-due",
                        "status": "限流",
                        "quota": 0,
                        "restore_at": "2000-01-01T00:00:00+00:00",
                        "last_remote_refresh_at": "2026-08-19T00:00:00+00:00",
                    },
                    {
                        "access_token": "unchecked",
                        "status": "正常",
                        "quota": 0,
                        "image_quota_unknown": True,
                    },
                    {
                        "access_token": "normal-with-quota",
                        "status": "正常",
                        "quota": 4,
                        "last_remote_refresh_at": "2026-08-19T00:00:00+00:00",
                    },
                ]
            )

            candidates = service.list_remote_refresh_candidates(limit=10)

            self.assertNotIn("limited-future", candidates)
            self.assertNotIn("normal-with-quota", candidates)
            self.assertIn("limited-due", candidates)
            self.assertIn("unchecked", candidates)

    def test_unknown_quota_accounts_are_available_only_when_not_throttled(self) -> None:
        self.assertFalse(
            AccountService._is_image_account_available(
                {"status": "限流", "image_quota_unknown": True, "quota": 0}
            )
        )
        self.assertTrue(
            AccountService._is_image_account_available(
                {"status": "正常", "image_quota_unknown": True, "quota": 0}
            )
        )

    def test_prolite_variants_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            self.assertEqual(service._normalize_account_type("prolite"), "ProLite")
            self.assertEqual(service._normalize_account_type("pro_lite"), "ProLite")

    def test_search_account_type_ignores_unrelated_scalar_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            self.assertIsNone(
                service._search_account_type(
                    {
                        "amr": ["pwd", "otp", "mfa"],
                        "chatgpt_compute_residency": "no_constraint",
                        "chatgpt_data_residency": "no_constraint",
                        "user_id": "user-I52GFfLGFM0dokFk2dBiKEBn",
                    }
                )
            )

    def test_mark_image_result_does_not_consume_unknown_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_accounts(["token-1"])
            service.update_account(
                "token-1",
                {
                    "status": "正常",
                    "quota": 0,
                    "image_quota_unknown": True,
                },
            )

            updated = service.mark_image_result("token-1", success=True)

            self.assertIsNotNone(updated)
            self.assertEqual(updated["quota"], 0)
            self.assertEqual(updated["status"], "正常")
            self.assertTrue(updated["image_quota_unknown"])

    def test_split_image_model_supports_plan_type_prefix(self) -> None:
        self.assertEqual(split_image_model("gpt-image-2"), (None, "gpt-image-2"))
        self.assertEqual(split_image_model("plus-codex-gpt-image-2"), ("plus", "codex-gpt-image-2"))
        self.assertEqual(split_image_model("team-codex-gpt-image-2"), ("team", "codex-gpt-image-2"))
        self.assertEqual(split_image_model("pro-codex-gpt-image-2"), ("pro", "codex-gpt-image-2"))
        self.assertEqual(split_image_model("plus-gpt-image-2"), (None, None))
        self.assertEqual(split_image_model("unknown-image-model"), (None, None))

    def test_get_available_access_token_filters_by_plan_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {"access_token": "token-plus", "type": "Plus", "status": "正常", "quota": 3},
                    {"access_token": "token-pro", "type": "Pro", "status": "正常", "quota": 3},
                ]
            )

            service.fetch_remote_info = lambda access_token, event="fetch_remote_info", **_kwargs: service.get_account(access_token)

            plus_token = service.get_available_access_token(plan_type="plus")
            pro_token = service.get_available_access_token(plan_type="pro")
            service.release_image_slot(plus_token)
            service.release_image_slot(pro_token)

            self.assertEqual(plus_token, "token-plus")
            self.assertEqual(pro_token, "token-pro")

    def test_get_available_access_token_rechecks_full_account_before_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "accounts.db"
            storage = DatabaseStorageBackend(f"sqlite:///{db_path}")
            storage.upsert_account(
                {
                    "access_token": "limited-token",
                    "status": "限流",
                    "type": "free",
                    "quota": 0,
                    "image_quota_unknown": True,
                    "restore_at": "2099-08-15T12:56:47+08:00",
                }
            )
            service = AccountService(storage)

            session = storage.Session()
            try:
                row = session.query(AccountModel).filter(AccountModel.access_token == "limited-token").one()
                row.status = "正常"
                session.commit()
            finally:
                session.close()

            fetch_remote_info = Mock()
            service.fetch_remote_info = fetch_remote_info
            try:
                self.assertEqual(storage.get_image_pool_metrics()["current_available"], 1)
                with self.assertRaisesRegex(RuntimeError, "no available image quota"):
                    service.get_available_access_token()
                self.assertEqual(storage.get_image_pool_metrics()["current_available"], 1)
                fetch_remote_info.assert_not_called()
                self.assertEqual(runtime_state.get_image_inflight("limited-token"), 0)
            finally:
                runtime_state.clear_image_slots({"limited-token"})
                storage.engine.dispose()

    def test_image_pool_metrics_counts_only_image_usable_normal_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {"access_token": "zero-normal", "status": "正常", "quota": 0, "image_quota_unknown": False},
                    {"access_token": "quota-normal", "status": "正常", "quota": 2, "image_quota_unknown": False},
                    {"access_token": "unknown-normal", "status": "正常", "quota": 0, "image_quota_unknown": True},
                    {"access_token": "limited", "status": "限流", "quota": 5, "image_quota_unknown": False},
                    {"access_token": "abnormal", "status": "异常", "quota": 5, "image_quota_unknown": False},
                    {"access_token": "disabled", "status": "禁用", "quota": 5, "image_quota_unknown": False},
                ]
            )

            metrics = service.get_image_pool_metrics()

            self.assertEqual(metrics["current_available"], 2)
            self.assertEqual(metrics["current_quota"], 2)

    def test_restore_due_accounts_are_included_in_automatic_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {
                        "access_token": "normal-due",
                        "status": "正常",
                        "quota": 0,
                        "restore_at": "2000-01-01T00:00:00+00:00",
                    },
                    {
                        "access_token": "limited-future",
                        "status": "限流",
                        "quota": 0,
                        "restore_at": "2999-01-01T00:00:00+00:00",
                    },
                    {
                        "access_token": "limited-without-restore",
                        "status": "限流",
                        "quota": 0,
                    },
                    {
                        "access_token": "normal-future",
                        "status": "正常",
                        "quota": 0,
                        "restore_at": "2999-01-01T00:00:00+00:00",
                    },
                    {
                        "access_token": "normal-with-quota",
                        "status": "正常",
                        "quota": 1,
                        "restore_at": "2000-01-01T00:00:00+00:00",
                    },
                    {
                        "access_token": "normal-unknown",
                        "status": "正常",
                        "quota": 0,
                        "image_quota_unknown": True,
                        "restore_at": "2000-01-01T00:00:00+00:00",
                    },
                ]
            )

            recheck_flags: set[str] = set()
            with patch.object(
                runtime_state,
                "get_flag",
                side_effect=lambda key: "1" if key in recheck_flags else "",
            ), patch.object(
                runtime_state,
                "set_flag",
                side_effect=lambda key, **_kwargs: recheck_flags.add(key),
            ) as set_recheck_flag:
                self.assertEqual(
                    service.list_image_recovery_tokens(),
                    ["normal-due", "limited-without-restore"],
                )
                self.assertEqual(
                    service.list_image_recovery_tokens(),
                    ["normal-due"],
                )
                self.assertEqual(set_recheck_flag.call_count, 1)
                self.assertEqual(
                    set_recheck_flag.call_args.kwargs["ttl_seconds"],
                    60 * 60,
                )
            restore_due = {
                item["access_token"]: item["restore_due"]
                for item in service.list_accounts()
            }
            self.assertTrue(restore_due["normal-due"])
            self.assertFalse(restore_due["limited-future"])
            self.assertFalse(restore_due["limited-without-restore"])
            self.assertFalse(restore_due["normal-future"])
            self.assertFalse(restore_due["normal-with-quota"])
            self.assertFalse(restore_due["normal-unknown"])

            delete_result = service.delete_accounts(["normal-future"])
            remaining = {
                item["access_token"]: item
                for item in delete_result["items"]
            }
            self.assertTrue(remaining["normal-due"]["restore_due"])

    def test_image_slot_wait_respects_deadline_when_all_slots_are_full(self) -> None:
        original_concurrency = config.data.get("image_account_concurrency")
        config.data["image_account_concurrency"] = 1
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                runtime_state.clear_image_slots({"busy-token"})
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items([{"access_token": "busy-token", "status": "正常", "quota": 3}])
                service.fetch_remote_info = lambda access_token, event="fetch_remote_info", **_kwargs: service.get_account(access_token)
                first = service.get_available_access_token()
                self.assertEqual(first, "busy-token")

                started = time.time()
                with self.assertRaises(ImageDeadlineExpired):
                    service.get_available_access_token(deadline=ImageRequestDeadline(1))
                self.assertLess(time.time() - started, 2.5)

                service.release_image_slot(first)
                token = service.get_available_access_token()
                service.release_image_slot(token)
                self.assertEqual(token, "busy-token")
        finally:
            runtime_state.clear_image_slots({"busy-token"})
            if original_concurrency is None:
                config.data.pop("image_account_concurrency", None)
            else:
                config.data["image_account_concurrency"] = original_concurrency

    def test_refresh_accounts_can_remove_invalid_token_without_confirmation_delay(self) -> None:
        original_value = config.data.get("auto_remove_invalid_accounts")
        config.data["auto_remove_invalid_accounts"] = True
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items([{"access_token": "invalid-token", "status": "正常"}])

                with patch(
                    "services.openai_backend_api.OpenAIBackendAPI.get_user_info",
                    side_effect=InvalidAccessTokenError("token invalidated (/backend-api/me)"),
                ):
                    result = service.refresh_accounts(["invalid-token"], defer_invalid_removal=False)

                self.assertEqual(result["refreshed"], 0)
                self.assertEqual(len(result["errors"]), 1)
                self.assertEqual(result["items"], [])
                self.assertIsNone(service.get_account("invalid-token"))
        finally:
            if original_value is None:
                config.data.pop("auto_remove_invalid_accounts", None)
            else:
                config.data["auto_remove_invalid_accounts"] = original_value

    def test_refresh_accounts_defers_invalid_token_removal_by_default(self) -> None:
        original_value = config.data.get("auto_remove_invalid_accounts")
        config.data["auto_remove_invalid_accounts"] = True
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items([{"access_token": "invalid-token", "status": "正常"}])

                with patch(
                    "services.openai_backend_api.OpenAIBackendAPI.get_user_info",
                    side_effect=InvalidAccessTokenError("token invalidated (/backend-api/me)"),
                ):
                    result = service.refresh_accounts(["invalid-token"])

                account = service.get_account("invalid-token")
                self.assertEqual(result["refreshed"], 0)
                self.assertEqual(len(result["errors"]), 1)
                self.assertIsNotNone(account)
                self.assertEqual(account["invalid_count"], 1)
        finally:
            if original_value is None:
                config.data.pop("auto_remove_invalid_accounts", None)
            else:
                config.data["auto_remove_invalid_accounts"] = original_value


class TokenLogTests(unittest.TestCase):
    def test_anonymize_token_hides_raw_value(self) -> None:
        token = "super-secret-token"
        token_ref = anonymize_token(token)

        self.assertTrue(token_ref.startswith("token:"))
        self.assertNotIn(token, token_ref)


class AuthServiceTests(unittest.TestCase):
    def test_create_authenticate_disable_and_delete_user_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))

            item, raw_key = service.create_key(role="user", name="Alice")

            self.assertEqual(item["role"], "user")
            self.assertEqual(item["name"], "Alice")
            self.assertTrue(item["enabled"])
            self.assertTrue(raw_key.startswith("sk-"))

            authed = service.authenticate(raw_key)
            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])
            self.assertEqual(authed["role"], "user")
            self.assertIsNotNone(authed["last_used_at"])

            updated = service.update_key(item["id"], {"enabled": False}, role="user")
            self.assertIsNotNone(updated)
            self.assertFalse(updated["enabled"])
            self.assertIsNone(service.authenticate(raw_key))

            self.assertTrue(service.delete_key(item["id"], role="user"))
            self.assertFalse(service.delete_key(item["id"], role="user"))
            self.assertEqual(service.list_keys(role="user"), [])

    def test_authenticate_ignores_last_used_save_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            item, raw_key = service.create_key(role="user", name="Alice")

            def fail_save() -> None:
                raise OSError("disk unavailable")

            service._save = fail_save

            authed = service.authenticate(raw_key)

            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])
            self.assertIsNotNone(authed["last_used_at"])

    def test_update_user_key_replaces_raw_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            item, raw_key = service.create_key(role="user", name="Alice")

            updated = service.update_key(item["id"], {"key": "sk-user-custom-key"}, role="user")

            self.assertIsNotNone(updated)
            self.assertIsNone(service.authenticate(raw_key))

            authed = service.authenticate("sk-user-custom-key")
            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])

    def test_user_key_name_must_be_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            first, _ = service.create_key(role="user", name="Alice")
            second, _ = service.create_key(role="user", name="Bob")

            with self.assertRaisesRegex(ValueError, "这个名称已经在使用中了"):
                service.create_key(role="user", name="Alice")

            with self.assertRaisesRegex(ValueError, "这个名称已经在使用中了"):
                service.update_key(second["id"], {"name": "Alice"}, role="user")

            updated = service.update_key(first["id"], {"name": "Alice"}, role="user")
            self.assertIsNotNone(updated)
            self.assertEqual(updated["name"], "Alice")


if __name__ == "__main__":
    unittest.main()
