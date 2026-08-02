from __future__ import annotations

import os
import time
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
        fake_accounts.list_normal_tokens.assert_not_called()
        fake_accounts.refresh_accounts.assert_called_once_with(["limited"])
        fake_accounts.keepalive_refresh_tokens.assert_called_once_with(["expiring", "keepalive"])
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

    def test_full_refresh_cycle_refreshes_every_stored_account(self) -> None:
        fake_config = mock.Mock(full_account_refresh_enabled=True)
        fake_runtime = mock.Mock()
        fake_runtime.acquire_lock.return_value = "owner"
        fake_accounts = mock.Mock()
        fake_accounts.list_tokens.return_value = ["normal", "limited", "abnormal", "disabled"]
        fake_accounts.refresh_accounts.return_value = {"refreshed": 4, "errors": []}

        with (
            mock.patch.object(support, "config", fake_config),
            mock.patch.object(support, "runtime_state", fake_runtime),
            mock.patch.object(support, "account_service", fake_accounts),
        ):
            ran = support._run_full_account_refresh_cycle(Event())

        self.assertTrue(ran)
        fake_accounts.refresh_accounts.assert_called_once_with(
            ["normal", "limited", "abnormal", "disabled"],
            defer_invalid_removal=False,
            include_items=False,
        )
        fake_runtime.release_lock.assert_called_once_with(support.FULL_ACCOUNT_REFRESH_LOCK, "owner")

    def test_full_refresh_cycle_skips_when_disabled(self) -> None:
        fake_config = mock.Mock(full_account_refresh_enabled=False)
        fake_runtime = mock.Mock()
        fake_accounts = mock.Mock()
        with (
            mock.patch.object(support, "config", fake_config),
            mock.patch.object(support, "runtime_state", fake_runtime),
            mock.patch.object(support, "account_service", fake_accounts),
        ):
            ran = support._run_full_account_refresh_cycle(Event())

        self.assertFalse(ran)
        fake_runtime.acquire_lock.assert_not_called()
        fake_accounts.refresh_accounts.assert_not_called()

    def test_full_refresh_cycle_uses_strict_lock_and_skips_without_it(self) -> None:
        fake_config = mock.Mock(full_account_refresh_enabled=True)
        fake_runtime = mock.Mock()
        fake_runtime.acquire_lock.return_value = ""
        fake_accounts = mock.Mock()

        with (
            mock.patch.dict(os.environ, {"UVICORN_WORKERS": "4"}),
            mock.patch.object(support, "config", fake_config),
            mock.patch.object(support, "runtime_state", fake_runtime),
            mock.patch.object(support, "account_service", fake_accounts),
        ):
            ran = support._run_full_account_refresh_cycle(Event())

        self.assertFalse(ran)
        fake_runtime.acquire_lock.assert_called_once_with(
            support.FULL_ACCOUNT_REFRESH_LOCK,
            ttl_seconds=support.FULL_ACCOUNT_REFRESH_LOCK_TTL_SECONDS,
            allow_memory_fallback=False,
        )
        fake_accounts.list_tokens.assert_not_called()

    def test_full_refresh_cycle_releases_lock_after_error(self) -> None:
        fake_config = mock.Mock(full_account_refresh_enabled=True)
        fake_runtime = mock.Mock()
        fake_runtime.acquire_lock.return_value = "owner"
        fake_accounts = mock.Mock()
        fake_accounts.list_tokens.return_value = ["token"]
        fake_accounts.refresh_accounts.side_effect = RuntimeError("refresh failed")

        with (
            mock.patch.object(support, "config", fake_config),
            mock.patch.object(support, "runtime_state", fake_runtime),
            mock.patch.object(support, "account_service", fake_accounts),
            self.assertRaisesRegex(RuntimeError, "refresh failed"),
        ):
            support._run_full_account_refresh_cycle(Event())

        fake_runtime.release_lock.assert_called_once_with(support.FULL_ACCOUNT_REFRESH_LOCK, "owner")

    def test_full_refresh_cycle_renews_lease_during_long_refresh(self) -> None:
        fake_config = mock.Mock(full_account_refresh_enabled=True)
        fake_runtime = mock.Mock()
        fake_runtime.acquire_lock.return_value = "owner"
        fake_runtime.extend_lock.return_value = True
        fake_accounts = mock.Mock()
        fake_accounts.list_tokens.return_value = ["token"]

        def slow_refresh(*_args, **_kwargs):
            time.sleep(0.05)
            return {"refreshed": 1, "errors": []}

        fake_accounts.refresh_accounts.side_effect = slow_refresh
        with (
            mock.patch.object(support, "config", fake_config),
            mock.patch.object(support, "FULL_ACCOUNT_REFRESH_LOCK_TTL_SECONDS", 0.03),
            mock.patch.object(support, "runtime_state", fake_runtime),
            mock.patch.object(support, "account_service", fake_accounts),
        ):
            support._run_full_account_refresh_cycle(Event())

        self.assertGreaterEqual(fake_runtime.extend_lock.call_count, 1)

    def test_full_refresh_worker_waits_after_cycle_without_overlap(self) -> None:
        class StopAfterFirstWait:
            def __init__(self) -> None:
                self.stopped = False
                self.waits: list[float] = []

            def is_set(self) -> bool:
                return self.stopped

            def wait(self, seconds: float) -> bool:
                self.waits.append(seconds)
                self.stopped = True
                return True

        stop_event = StopAfterFirstWait()
        with mock.patch.object(support, "_run_full_account_refresh_cycle", return_value=True) as run_cycle:
            thread = support.start_full_account_refresh_worker(stop_event)  # type: ignore[arg-type]
            thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        run_cycle.assert_called_once_with(stop_event)
        self.assertEqual(stop_event.waits, [support.FULL_ACCOUNT_REFRESH_INTERVAL_SECONDS])


if __name__ == "__main__":
    unittest.main()
