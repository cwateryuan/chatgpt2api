from __future__ import annotations

import unittest
from unittest import mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import api.system as system_module


AUTH_HEADERS = {"Authorization": "Bearer chatgpt2api"}


class FakeAccountService:
    def list_accounts(self) -> list[dict[str, object]]:
        raise AssertionError("task summary must not load full accounts")


class FakeRuntimeState:
    def __init__(self) -> None:
        self.calls = 0

    def image_inflight_total(self) -> int:
        self.calls += 1
        return 5


class TaskSummaryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.admin_calls: list[str | None] = []
        self.runtime_state = FakeRuntimeState()

        def fake_require_admin(authorization: str | None):
            self.admin_calls.append(authorization)
            if authorization != AUTH_HEADERS["Authorization"]:
                raise HTTPException(status_code=403, detail={"error": "admin required"})
            return {"role": "admin"}

        self.patchers = [
            mock.patch.object(system_module, "account_service", FakeAccountService()),
            mock.patch.object(system_module, "runtime_state", self.runtime_state),
            mock.patch.object(system_module, "require_admin", fake_require_admin),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(system_module.create_router("test"))
        self.client = TestClient(app)

    def test_task_summary_requires_admin(self) -> None:
        response = self.client.get("/api/tasks/summary")

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.admin_calls, [None])

    def test_task_summary_sums_image_inflight(self) -> None:
        response = self.client.get("/api/tasks/summary", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"image_inflight": 5, "total": 5})
        self.assertEqual(self.admin_calls, [AUTH_HEADERS["Authorization"]])
        self.assertEqual(self.runtime_state.calls, 1)


if __name__ == "__main__":
    unittest.main()
