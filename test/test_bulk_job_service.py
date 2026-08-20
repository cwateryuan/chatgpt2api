from __future__ import annotations

import unittest
from unittest import mock

from fastapi import HTTPException

from services.bulk_job_service import BulkJobService
from services.runtime_state import RuntimeState


class BulkJobServiceTests(unittest.TestCase):
    def test_runtime_state_keeps_bulk_progress_separate(self) -> None:
        state = RuntimeState()
        state.set_progress("bulk_account_import", "job-1", {"done": False})
        state.set_progress("refresh", "job-1", {"done": True})

        self.assertEqual(state.get_progress("bulk_account_import", "job-1"), {"done": False})
        self.assertEqual(state.get_progress("refresh", "job-1"), {"done": True})
        self.assertIsNone(state.get_progress("unknown-kind", "job-1"))

    def test_requires_redis_for_bulk_jobs(self) -> None:
        service = BulkJobService()
        with mock.patch("services.bulk_job_service.runtime_state._redis", None):
            with self.assertRaises(HTTPException) as ctx:
                service.submit_image_delete(paths=["a.png"])
        self.assertEqual(ctx.exception.status_code, 503)

    def test_account_payload_progress_does_not_store_full_token(self) -> None:
        service = BulkJobService()
        token = "sk-test-secret-token"
        with mock.patch("services.bulk_job_service.runtime_state._redis", mock.Mock()), \
             mock.patch("services.bulk_job_service.runtime_state.set_progress") as set_progress, \
             mock.patch.object(service, "_start_thread"):
            job_id = service.submit_account_import(tokens=[token], accounts=[])

        self.assertTrue(job_id)
        payload = set_progress.call_args.args[2]
        self.assertNotIn(token, str(payload))
        self.assertEqual(payload["total"], 1)

    def test_global_lock_rejects_second_running_job(self) -> None:
        service = BulkJobService()
        service._threads["job-1"] = mock.Mock()
        with mock.patch("services.bulk_job_service.runtime_state.acquire_lock", return_value=""), \
             mock.patch.object(service, "_mark_done") as mark_done:
            service._run_with_global_lock(service.IMAGE_DELETE_KIND, "job-1", lambda _owner: None)

        mark_done.assert_called_once()
        self.assertEqual(mark_done.call_args.kwargs["status"], "error")
        self.assertNotIn("job-1", service._threads)

    def test_image_delete_stops_when_global_lock_is_lost(self) -> None:
        service = BulkJobService()
        with mock.patch.object(service, "_resolve_image_targets", return_value=["a.png"]), \
             mock.patch.object(service, "_lock_still_owned", return_value=False), \
             mock.patch.object(service, "_delete_image_batch") as delete_batch, \
             mock.patch.object(service, "_mark_done") as mark_done, \
             mock.patch.object(service, "_update"):
            service._execute_image_delete("job-1", [], "", "", False, "owner")

        delete_batch.assert_not_called()
        mark_done.assert_called_once()
        self.assertEqual(mark_done.call_args.kwargs["status"], "error")

    def test_progress_update_preserves_cancel_request(self) -> None:
        state = RuntimeState()
        state.set_progress("bulk_image_delete", "job-1", {
            "processed": 0,
            "cancel_requested": True,
        })
        updated = state.update_progress(
            "bulk_image_delete",
            "job-1",
            lambda progress: {**progress, "processed": 1},
        )

        self.assertIsNotNone(updated)
        self.assertTrue(updated["cancel_requested"])
        self.assertEqual(updated["processed"], 1)

    def test_account_import_queues_a_separate_refresh_after_releasing_lock(self) -> None:
        service = BulkJobService()
        imported = ["token-a", "token-b"]
        with mock.patch.object(service, "_run_with_global_lock", return_value=imported) as run_locked, \
             mock.patch.object(service, "submit_account_refresh", return_value="refresh-job") as submit_refresh, \
             mock.patch.object(service, "_update") as update, \
             mock.patch.object(service, "_mark_done") as mark_done:
            service._run_account_import("import-job", imported, [])

        run_locked.assert_called_once()
        submit_refresh.assert_called_once_with(tokens=imported, source="import")
        update.assert_called_once()
        self.assertEqual(update.call_args.args[:2], (service.ACCOUNT_IMPORT_KIND, "import-job"))
        mark_done.assert_called_once_with(service.ACCOUNT_IMPORT_KIND, "import-job", status="success")


if __name__ == "__main__":
    unittest.main()
