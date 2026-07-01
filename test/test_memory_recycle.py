from __future__ import annotations

import threading
import unittest
from unittest import mock

from services.memory_recycle import _should_recycle
from services.request_activity import RequestActivity


class MemoryRecycleTests(unittest.TestCase):
    def test_request_activity_tracks_idle_state(self) -> None:
        activity = RequestActivity()
        self.assertEqual(activity.snapshot()["active"], 0)
        activity.begin()
        self.assertEqual(activity.snapshot()["active"], 1)
        activity.end()
        snapshot = activity.snapshot()
        self.assertEqual(snapshot["active"], 0)
        self.assertGreaterEqual(float(snapshot["idle_secs"]), 0.0)

    def test_should_recycle_when_idle_high_rss_and_no_inflight(self) -> None:
        fake_activity = mock.Mock()
        fake_activity.snapshot.return_value = {"active": 0, "idle_secs": 600.0}
        with (
            mock.patch("services.memory_recycle._rss_kb", return_value=2 * 1024 * 1024),
            mock.patch("services.memory_recycle.request_activity", fake_activity),
            mock.patch("services.memory_recycle._runtime_inflight_total", return_value=0),
            mock.patch("services.memory_recycle._active_image_task_threads", return_value=0),
            mock.patch("services.memory_recycle._unfinished_image_tasks", return_value=0),
            mock.patch("services.memory_recycle._has_register_thread", return_value=False),
            mock.patch("services.memory_recycle.time.monotonic", return_value=1000.0),
        ):
            should_recycle, detail = _should_recycle(
                threshold_kb=1024 * 1024,
                idle_secs_required=300,
                min_age_secs=300,
                started_at=0,
            )

        self.assertTrue(should_recycle)
        self.assertEqual(detail["image_inflight_total"], 0)

    def test_should_recycle_with_global_inflight_by_default(self) -> None:
        fake_activity = mock.Mock()
        fake_activity.snapshot.return_value = {"active": 0, "idle_secs": 600.0}
        with (
            mock.patch("services.memory_recycle._rss_kb", return_value=2 * 1024 * 1024),
            mock.patch("services.memory_recycle.request_activity", fake_activity),
            mock.patch("services.memory_recycle._runtime_inflight_total", return_value=1),
            mock.patch("services.memory_recycle._active_image_task_threads", return_value=0),
            mock.patch("services.memory_recycle._unfinished_image_tasks", return_value=0),
            mock.patch("services.memory_recycle._has_register_thread", return_value=False),
            mock.patch("services.memory_recycle.time.monotonic", return_value=1000.0),
        ):
            should_recycle, detail = _should_recycle(
                threshold_kb=1024 * 1024,
                idle_secs_required=300,
                min_age_secs=300,
                started_at=0,
            )

        self.assertTrue(should_recycle)
        self.assertEqual(detail["image_inflight_total"], 1)

    def test_should_not_recycle_with_global_inflight_when_strict(self) -> None:
        fake_activity = mock.Mock()
        fake_activity.snapshot.return_value = {"active": 0, "idle_secs": 600.0}
        with (
            mock.patch("services.memory_recycle._rss_kb", return_value=2 * 1024 * 1024),
            mock.patch("services.memory_recycle.request_activity", fake_activity),
            mock.patch("services.memory_recycle._runtime_inflight_total", return_value=1),
            mock.patch("services.memory_recycle._active_image_task_threads", return_value=0),
            mock.patch("services.memory_recycle._unfinished_image_tasks", return_value=0),
            mock.patch("services.memory_recycle._has_register_thread", return_value=False),
            mock.patch("services.memory_recycle.time.monotonic", return_value=1000.0),
        ):
            self.assertFalse(_should_recycle(
                threshold_kb=1024 * 1024,
                idle_secs_required=300,
                min_age_secs=300,
                started_at=0,
                require_global_idle=True,
            )[0])

    def test_should_not_recycle_with_register_thread(self) -> None:
        fake_activity = mock.Mock()
        fake_activity.snapshot.return_value = {"active": 0, "idle_secs": 600.0}
        common = {
            "threshold_kb": 1024 * 1024,
            "idle_secs_required": 300,
            "min_age_secs": 300,
            "started_at": 0,
        }

        with (
            mock.patch("services.memory_recycle._rss_kb", return_value=2 * 1024 * 1024),
            mock.patch("services.memory_recycle.request_activity", fake_activity),
            mock.patch("services.memory_recycle._runtime_inflight_total", return_value=0),
            mock.patch("services.memory_recycle._active_image_task_threads", return_value=0),
            mock.patch("services.memory_recycle._unfinished_image_tasks", return_value=0),
            mock.patch("services.memory_recycle._has_register_thread", return_value=True),
            mock.patch("services.memory_recycle.time.monotonic", return_value=1000.0),
        ):
            self.assertFalse(_should_recycle(**common)[0])

    def test_should_not_recycle_with_image_task_thread(self) -> None:
        fake_activity = mock.Mock()
        fake_activity.snapshot.return_value = {"active": 0, "idle_secs": 600.0}
        common = {
            "threshold_kb": 1024 * 1024,
            "idle_secs_required": 300,
            "min_age_secs": 300,
            "started_at": 0,
        }
        with (
            mock.patch("services.memory_recycle._rss_kb", return_value=2 * 1024 * 1024),
            mock.patch("services.memory_recycle.request_activity", fake_activity),
            mock.patch("services.memory_recycle._runtime_inflight_total", return_value=0),
            mock.patch("services.memory_recycle._active_image_task_threads", return_value=1),
            mock.patch("services.memory_recycle._unfinished_image_tasks", return_value=0),
            mock.patch("services.memory_recycle._has_register_thread", return_value=False),
            mock.patch("services.memory_recycle.time.monotonic", return_value=1000.0),
        ):
            self.assertFalse(_should_recycle(**common)[0])

    def test_should_not_recycle_with_maintenance_activity(self) -> None:
        fake_activity = mock.Mock()
        fake_activity.snapshot.return_value = {"active": 0, "idle_secs": 600.0}
        fake_maintenance = mock.Mock()
        fake_maintenance.snapshot.return_value = {
            "active": 1,
            "by_kind": {"bulk_image_delete": 1},
            "idle_secs": 0.0,
        }
        with (
            mock.patch("services.memory_recycle._rss_kb", return_value=2 * 1024 * 1024),
            mock.patch("services.memory_recycle.request_activity", fake_activity),
            mock.patch("services.memory_recycle.maintenance_activity", fake_maintenance),
            mock.patch("services.memory_recycle._runtime_inflight_total", return_value=0),
            mock.patch("services.memory_recycle._active_image_task_threads", return_value=0),
            mock.patch("services.memory_recycle._unfinished_image_tasks", return_value=0),
            mock.patch("services.memory_recycle._has_register_thread", return_value=False),
            mock.patch("services.memory_recycle.time.monotonic", return_value=1000.0),
        ):
            should_recycle, detail = _should_recycle(
                threshold_kb=1024 * 1024,
                idle_secs_required=300,
                min_age_secs=300,
                started_at=0,
            )

        self.assertFalse(should_recycle)
        self.assertEqual(detail["maintenance_active"], 1)

    def test_should_not_recycle_with_unfinished_task_when_strict(self) -> None:
        fake_activity = mock.Mock()
        fake_activity.snapshot.return_value = {"active": 0, "idle_secs": 600.0}
        with (
            mock.patch("services.memory_recycle._rss_kb", return_value=2 * 1024 * 1024),
            mock.patch("services.memory_recycle.request_activity", fake_activity),
            mock.patch("services.memory_recycle._runtime_inflight_total", return_value=0),
            mock.patch("services.memory_recycle._active_image_task_threads", return_value=0),
            mock.patch("services.memory_recycle._unfinished_image_tasks", return_value=1),
            mock.patch("services.memory_recycle._has_register_thread", return_value=False),
            mock.patch("services.memory_recycle.time.monotonic", return_value=1000.0),
        ):
            self.assertFalse(_should_recycle(
                threshold_kb=1024 * 1024,
                idle_secs_required=300,
                min_age_secs=300,
                started_at=0,
                require_global_idle=True,
            )[0])

    def test_disabled_scheduler_exits_immediately(self) -> None:
        from services.memory_recycle import start_memory_recycle_scheduler

        with mock.patch.dict("os.environ", {"APP_MEMORY_RECYCLE_ENABLED": ""}, clear=False):
            thread = start_memory_recycle_scheduler(threading.Event())
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
