from __future__ import annotations

import os
import unittest
from threading import Event
from unittest import mock

from api import support
from services.runtime_state import RuntimeState, is_multi_worker_runtime


class AccountWatcherTests(unittest.TestCase):
    def test_multi_worker_detection(self) -> None:
        with mock.patch.dict(os.environ, {"UVICORN_WORKERS": "6"}):
            self.assertTrue(is_multi_worker_runtime())
        with mock.patch.dict(os.environ, {"UVICORN_WORKERS": "1"}):
            self.assertFalse(is_multi_worker_runtime())

    def test_strict_lock_does_not_fall_back_to_process_memory(self) -> None:
        state = RuntimeState()
        state._redis = mock.Mock()
        state._redis.set.side_effect = ConnectionError("redis unavailable")

        owner = state.acquire_lock("lock:test", allow_memory_fallback=False)

        self.assertEqual(owner, "")
        self.assertEqual(state._memory_locks, {})

    def test_watcher_uses_one_strict_lease_in_multi_worker_mode(self) -> None:
        fake_runtime = mock.Mock()
        fake_runtime.acquire_lock.return_value = "owner"
        fake_accounts = mock.Mock()
        fake_accounts.list_limited_tokens.return_value = ["limited"]
        fake_accounts.list_normal_tokens.return_value = ["normal"]
        fake_accounts.list_expiring_access_tokens.return_value = ["expiring"]
        fake_accounts.list_refresh_token_keepalive_tokens.return_value = ["expiring", "keepalive"]
        fake_accounts.keepalive_refresh_tokens.return_value = {"errors": []}

        with (
            mock.patch.dict(os.environ, {"UVICORN_WORKERS": "6"}),
            mock.patch.object(support, "runtime_state", fake_runtime),
            mock.patch.object(support, "account_service", fake_accounts),
        ):
            ran = support._run_limited_account_refresh_cycle(Event())

        self.assertTrue(ran)
        fake_runtime.acquire_lock.assert_called_once_with(
            support.ACCOUNT_WATCHER_LOCK,
            ttl_seconds=support.ACCOUNT_WATCHER_LOCK_TTL_SECONDS,
            allow_memory_fallback=False,
        )
        fake_accounts.refresh_accounts.assert_called_once_with(["limited", "normal", "expiring"])
        fake_accounts.keepalive_refresh_tokens.assert_called_once_with(["keepalive"])
        fake_runtime.release_lock.assert_called_once_with(support.ACCOUNT_WATCHER_LOCK, "owner")

    def test_watcher_skips_refresh_without_the_lease(self) -> None:
        fake_runtime = mock.Mock()
        fake_runtime.acquire_lock.return_value = ""
        fake_accounts = mock.Mock()

        with (
            mock.patch.object(support, "runtime_state", fake_runtime),
            mock.patch.object(support, "account_service", fake_accounts),
        ):
            ran = support._run_limited_account_refresh_cycle(Event())

        self.assertFalse(ran)
        fake_accounts.refresh_accounts.assert_not_called()


if __name__ == "__main__":
    unittest.main()
